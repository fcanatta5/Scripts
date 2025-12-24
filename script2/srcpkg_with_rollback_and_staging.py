#!/usr/bin/env python3
"""
srcpkg: um gerenciador simples de pacotes source-based.

Principais recursos (versão evoluída):
- Download de fontes via HTTP/HTTPS/FTP (tar.*) com validação SHA256.
- Download de fontes via Git (GitHub/GitLab/repositórios genéricos) com pin por tag/commit/branch.
- Extração segura de tarball (proteção contra path traversal).
- Build systems: autotools, cmake, make, meson, cargo (Rust), go, python (PEP 517), custom.
- Empacotamento em tar.zst.
- Instalação com detecção de conflitos de arquivos (ownership) e DB com manifesto + hashes.
- Uninstall "inteligente": remove apenas o que foi instalado e não foi modificado (hash diferente é preservado).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import shlex
import subprocess
import sys
import tarfile
import tempfile
import contextlib
try:
    import fcntl  # type: ignore
except Exception:  # pragma: no cover
    fcntl = None
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

import yaml
from urllib.request import urlopen

# ----------------------------
# Config e diretórios
# ----------------------------

PKG_HOME = Path(os.environ.get("SRCPKG_HOME", Path.home() / ".srcpkg"))
PKG_TREE = Path(os.environ.get("SRCPKG_TREE", Path.cwd() / "packages"))

SRC_CACHE = PKG_HOME / "src"
VCS_CACHE = SRC_CACHE / "vcs"
BIN_CACHE = PKG_HOME / "bin"
BUILD_ROOT = PKG_HOME / "build"
LOG_DIR = PKG_HOME / "logs"
DB_PATH = PKG_HOME / "db.json"
DB_LOCK_PATH = PKG_HOME / "db.lock"
LOCKFILE_PATH = PKG_HOME / "lockfile.json"
BUILD_LOCK_DIR = PKG_HOME / "locks"
HISTORY_LIMIT = int(os.environ.get("SRCPKG_HISTORY_LIMIT", "5"))

DEFAULT_PREFIX = Path(os.environ.get("SRCPKG_PREFIX", "/usr/local"))
DEFAULT_JOBS = int(os.environ.get("SRCPKG_JOBS", os.cpu_count() or 1))

# ----------------------------
# Logging
# ----------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
)
log = logging.getLogger("pkg")


# ----------------------------
# Modelos de Recipe
# ----------------------------

@dataclass
class GitRef:
    repo: str
    # Um e apenas um entre: tag, commit, branch pode ser fornecido.
    tag: Optional[str] = None
    commit: Optional[str] = None
    branch: Optional[str] = None
    # Submódulos
    submodules: bool = False
    # Shallow clone quando possível
    shallow: bool = True

    def resolved_ref(self) -> str:
        if self.commit:
            return self.commit
        if self.tag:
            return f"refs/tags/{self.tag}"
        if self.branch:
            return f"refs/heads/{self.branch}"
        # Default: HEAD
        return "HEAD"

    def ref_label(self) -> str:
        if self.commit:
            return f"commit-{self.commit[:12]}"
        if self.tag:
            return f"tag-{self.tag}"
        if self.branch:
            return f"branch-{self.branch}"
        return "head"


@dataclass
class SourceInfo:
    # Tipos: "tar" ou "git"
    kind: str
    # Para tar: url + sha256
    url: Optional[str] = None
    sha256: Optional[str] = None
    # Para git:
    git: Optional[GitRef] = None

    @staticmethod
    def from_recipe(obj: Any) -> "SourceInfo":
        """
        Aceita formatos:
          source:
            url: ...
            sha256: ...
        ou
          source:
            kind: tar
            url: ...
            sha256: ...
        ou
          source:
            kind: git
            repo: ...
            tag: v1.2.3
        """
        if not isinstance(obj, dict):
            raise ValueError("Campo 'source' deve ser um objeto (dict) no YAML")

        # Retrocompat: se tiver url + sha256 e sem kind, assume tar.
        if "kind" not in obj and "url" in obj:
            return SourceInfo(kind="tar", url=str(obj.get("url")), sha256=str(obj.get("sha256", "")).strip() or None)

        kind = str(obj.get("kind", "")).strip().lower()
        if kind in ("tar", "archive"):
            url = str(obj.get("url", "")).strip()
            sha = str(obj.get("sha256", "")).strip() or None
            if not url:
                raise ValueError("source.kind=tar requer 'url'")
            if not sha:
                raise ValueError("source.kind=tar requer 'sha256'")
            return SourceInfo(kind="tar", url=url, sha256=sha)

        if kind in ("git", "vcs"):
            repo = str(obj.get("repo", obj.get("url", ""))).strip()
            if not repo:
                raise ValueError("source.kind=git requer 'repo' (ou 'url')")
            gr = GitRef(
                repo=repo,
                tag=(str(obj.get("tag")).strip() if obj.get("tag") is not None else None),
                commit=(str(obj.get("commit")).strip() if obj.get("commit") is not None else None),
                branch=(str(obj.get("branch")).strip() if obj.get("branch") is not None else None),
                submodules=bool(obj.get("submodules", False)),
                shallow=bool(obj.get("shallow", True)),
            )
            # Validar exclusividade de ref
            refs = [r for r in (gr.tag, gr.commit, gr.branch) if r]
            if len(refs) > 1:
                raise ValueError("Em source.kind=git, use apenas um entre 'tag', 'commit' ou 'branch'")
            return SourceInfo(kind="git", git=gr)

        raise ValueError(f"source.kind inválido: {kind!r} (esperado: tar|git)")


@dataclass
class BuildConfig:
    system: str  # autotools, cmake, make, meson, cargo, go, python, custom
    configure_flags: List[str] = field(default_factory=list)  # autotools
    make_flags: List[str] = field(default_factory=list)       # autotools/make
    cmake_flags: List[str] = field(default_factory=list)      # cmake
    meson_flags: List[str] = field(default_factory=list)      # meson
    cargo_flags: List[str] = field(default_factory=list)      # cargo
    go_flags: List[str] = field(default_factory=list)         # go
    python_flags: List[str] = field(default_factory=list)     # python build
    custom_script: str = "build.sh"

    @staticmethod
    def from_recipe(obj: Any) -> "BuildConfig":
        if not isinstance(obj, dict):
            raise ValueError("Campo 'build' deve ser um objeto (dict) no YAML")

        def _list(key: str) -> List[str]:
            v = obj.get(key, [])
            if v is None:
                return []
            if isinstance(v, list):
                if not all(isinstance(x, (str, int, float)) for x in v):
                    raise ValueError(f"build.{key} deve ser uma lista de strings")
                return [str(x) for x in v]
            # permitir string única
            if isinstance(v, (str, int, float)):
                return [str(v)]
            raise ValueError(f"build.{key} deve ser lista ou string")

        system = str(obj.get("system", "")).strip().lower()
        if not system:
            raise ValueError("Campo obrigatório ausente: build.system")

        return BuildConfig(
            system=system,
            configure_flags=_list("configure_flags"),
            make_flags=_list("make_flags"),
            cmake_flags=_list("cmake_flags"),
            meson_flags=_list("meson_flags"),
            cargo_flags=_list("cargo_flags"),
            go_flags=_list("go_flags"),
            python_flags=_list("python_flags"),
            custom_script=str(obj.get("custom_script", "build.sh")).strip() or "build.sh",
        )


@dataclass
class PackageMeta:
    category: str
    name: str
    version: str
    source: SourceInfo
    build: BuildConfig
    depends: List[str] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        return f"{self.category}/{self.name}"

    @property
    def id(self) -> str:
        safe = lambda s: "".join(c if c.isalnum() or c in ".-_" else "_" for c in s)
        return f"{safe(self.category)}-{safe(self.name)}-{safe(self.version)}"


# ----------------------------
# DB: installed packages + ownership
# ----------------------------

def _empty_db() -> Dict[str, Any]:
    # schema 3: adiciona histórico e marcação de instalação explícita
    return {
        "schema": 3,
        "installed": {},  # full_name -> record
        "owners": {},     # path (posix, rel to /) -> full_name
        "history": {},    # full_name -> [records antigos], mais recente primeiro
    }

def load_db() -> Dict[str, Any]:
    """
    Carrega o DB (schema 3). Migra automaticamente schemas antigos.
    Observação: o locking é feito em nível de comando via db_lock().
    """
    if not DB_PATH.exists():
        return _empty_db()
    try:
        data = json.loads(DB_PATH.read_text(encoding="utf-8"))
    except Exception:
        return _empty_db()

    if isinstance(data, dict):
        schema = int(data.get("schema", 1) or 1)

        if schema == 3:
            # Garantir campos
            base = _empty_db()
            base.update({k: v for k, v in data.items() if k in base})
            base["installed"] = data.get("installed", {}) if isinstance(data.get("installed", {}), dict) else {}
            base["owners"] = data.get("owners", {}) if isinstance(data.get("owners", {}), dict) else {}
            base["history"] = data.get("history", {}) if isinstance(data.get("history", {}), dict) else {}
            return base

        if schema == 2:
            migrated = _empty_db()
            migrated["installed"] = data.get("installed", {}) if isinstance(data.get("installed", {}), dict) else {}
            migrated["owners"] = data.get("owners", {}) if isinstance(data.get("owners", {}), dict) else {}
            # schema 2 não tinha histórico
            return migrated

        # schema 1 (legado): map full_name->record
        if schema == 1:
            migrated = _empty_db()
            for k, v in data.items():
                if isinstance(v, dict) and "version" in v:
                    migrated["installed"][k] = v
            return migrated

    return _empty_db()

def save_db(db: Dict[str, Any]) -> None:
    ensure_dirs()
    # normalizar schema/campos
    if not isinstance(db, dict):
        db = _empty_db()
    db.setdefault("schema", 3)
    db.setdefault("installed", {})
    db.setdefault("owners", {})
    db.setdefault("history", {})
    tmp = DB_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(db, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(DB_PATH)


@contextlib.contextmanager
def db_lock() -> Any:
    """
    Lock exclusivo para operações que leem+escrevem o DB (evita corrupção em execuções concorrentes).
    Implementação via flock (POSIX). Em plataformas sem fcntl, vira no-op.
    """
    ensure_dirs()
    if fcntl is None:
        yield
        return
    DB_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with DB_LOCK_PATH.open("a+", encoding="utf-8") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def build_lock(lock_id: str) -> Any:
    """
    Lock exclusivo por pacote/build id, para evitar duas builds do mesmo pacote ao mesmo tempo.
    """
    ensure_dirs()
    BUILD_LOCK_DIR.mkdir(parents=True, exist_ok=True)
    if fcntl is None:
        yield
        return
    lock_path = BUILD_LOCK_DIR / f"{lock_id}.lock"
    with lock_path.open("a+", encoding="utf-8") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

def ensure_dirs() -> None:
    for d in (SRC_CACHE, VCS_CACHE, BIN_CACHE, BUILD_ROOT, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
    PKG_HOME.mkdir(parents=True, exist_ok=True)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except Exception:
        return False


def load_lockfile() -> Dict[str, Any]:
    if not LOCKFILE_PATH.exists():
        return {"schema": 1, "packages": {}}
    try:
        data = json.loads(LOCKFILE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"schema": 1, "packages": {}}
    if not isinstance(data, dict):
        return {"schema": 1, "packages": {}}
    data.setdefault("schema", 1)
    data.setdefault("packages", {})
    if not isinstance(data["packages"], dict):
        data["packages"] = {}
    return data


def save_lockfile(lf: Dict[str, Any]) -> None:
    ensure_dirs()
    if not isinstance(lf, dict):
        lf = {"schema": 1, "packages": {}}
    lf.setdefault("schema", 1)
    lf.setdefault("packages", {})
    tmp = LOCKFILE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(lf, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(LOCKFILE_PATH)


def lockfile_get(full_name: str) -> Optional[Dict[str, str]]:
    lf = load_lockfile()
    entry = lf.get("packages", {}).get(full_name)
    return entry if isinstance(entry, dict) else None


def lockfile_set(full_name: str, entry: Dict[str, str]) -> None:
    lf = load_lockfile()
    lf["packages"][full_name] = entry
    save_lockfile(lf)

def which(cmd: str) -> Optional[str]:
    return shutil.which(cmd)


def require_tools(tools: List[str]) -> None:
    missing = [t for t in tools if which(t) is None]
    if missing:
        raise SystemExit(f"Ferramentas ausentes no PATH: {', '.join(missing)}")


def run_cmd(
    cmd: List[str],
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    log_file: Optional[Path] = None,
    dry_run: bool = False,
) -> None:
    pretty = " ".join(shlex.quote(c) for c in cmd)
    if dry_run:
        log.info("[dry-run] %s", pretty)
        return

    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as lf:
            lf.write(f"\n$ {pretty}\n")
            lf.flush()
            p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env, stdout=lf, stderr=subprocess.STDOUT, text=True)
    else:
        p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env)

    if p.returncode != 0:
        raise SystemExit(f"Falha ao executar: {pretty} (exit={p.returncode})")


# ----------------------------
# Parsing e validação do recipe
# ----------------------------

def _validate_full_name(pkg: str) -> None:
    if "/" not in pkg:
        raise ValueError(f"Dependência inválida {pkg!r}; esperado formato 'categoria/nome'")


def load_package_meta(pkg_full: str, tree: Path = PKG_TREE) -> Tuple[PackageMeta, Path]:
    """
    Retorna (meta, pkg_dir)
    """
    _validate_full_name(pkg_full)
    cat, name = pkg_full.split("/", 1)
    pkg_dir = tree / cat / name
    recipe_path = pkg_dir / "package.yml"
    if not recipe_path.exists():
        raise SystemExit(f"Recipe não encontrado: {recipe_path}")

    data = yaml.safe_load(recipe_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"Recipe inválido (esperado dict): {recipe_path}")

    # Campos obrigatórios
    for k in ("category", "name", "version", "source", "build"):
        if k not in data:
            raise SystemExit(f"Campo obrigatório ausente em {recipe_path}: {k}")

    category = str(data["category"]).strip()
    name2 = str(data["name"]).strip()
    version = str(data["version"]).strip()
    if not category or not name2 or not version:
        raise SystemExit(f"category/name/version não podem ser vazios em {recipe_path}")

    if category != cat or name2 != name:
        # Evita inconsistência recipe vs path
        raise SystemExit(f"Inconsistência: path={pkg_full} recipe={category}/{name2} em {recipe_path}")

    # Depends
    depends_raw = data.get("depends", [])
    depends: List[str] = []
    if depends_raw is None:
        depends = []
    elif isinstance(depends_raw, list):
        depends = [str(x).strip() for x in depends_raw if str(x).strip()]
    elif isinstance(depends_raw, (str, int, float)):
        depends = [str(depends_raw).strip()]
    else:
        raise SystemExit(f"'depends' inválido em {recipe_path}: esperado lista/string")

    for dep in depends:
        _validate_full_name(dep)

    try:
        source = SourceInfo.from_recipe(data["source"])
        build = BuildConfig.from_recipe(data["build"])
    except ValueError as e:
        raise SystemExit(f"{recipe_path}: {e}")

    meta = PackageMeta(category=category, name=name2, version=version, source=source, build=build, depends=depends)
    return meta, pkg_dir


# ----------------------------
# Download e preparação de fonte
# ----------------------------

def _source_cache_key(meta: PackageMeta) -> str:
    if meta.source.kind == "git" and meta.source.git:
        return f"{meta.id}-{meta.source.git.ref_label()}"
    return meta.id


def download_source(meta: PackageMeta, dry_run: bool = False) -> Path:
    """
    Retorna:
      - tar: path para o arquivo baixado (tarball)
      - git: path para um checkout local (diretório)
    """
    ensure_dirs()

    if meta.source.kind == "tar":
        assert meta.source.url and meta.source.sha256
        url = meta.source.url
        filename = Path(url.split("?")[0]).name or "source.tar"
        cache_path = SRC_CACHE / f"{meta.id}-{filename}"

        if cache_path.exists():
            h = sha256_file(cache_path)
            if h.lower() == meta.source.sha256.lower():
                log.info("Source em cache: %s", cache_path)
                return cache_path
            log.warning("SHA256 divergente no cache; rebaixando: %s", cache_path)
            if not dry_run:
                cache_path.unlink(missing_ok=True)

        if dry_run:
            log.info("[dry-run] download %s -> %s", url, cache_path)
            return cache_path

        log.info("Baixando: %s", url)
        try:
            with urlopen(url, timeout=60) as r, cache_path.open("wb") as f:
                shutil.copyfileobj(r, f)
        except Exception as e:
            cache_path.unlink(missing_ok=True)
            raise SystemExit(f"Falha no download: {url} ({e})")

        got = sha256_file(cache_path)
        if got.lower() != meta.source.sha256.lower():
            cache_path.unlink(missing_ok=True)
            raise SystemExit(f"SHA256 inválido para {meta.full_name}: esperado {meta.source.sha256}, obtido {got}")

        return cache_path

    if meta.source.kind == "git":
        gr = meta.source.git
        assert gr is not None
        require_tools(["git"])

        repo = gr.repo
        key = _source_cache_key(meta)
        repo_dir = VCS_CACHE / key

        if dry_run:
            log.info("[dry-run] git checkout %s (%s) -> %s", repo, gr.resolved_ref(), repo_dir)
            return repo_dir

        # Se já existe, atualizar com fetch e checkout do ref
        if repo_dir.exists():
            log.info("Atualizando repo em cache: %s", repo_dir)
            run_cmd(["git", "fetch", "--all", "--tags"], cwd=repo_dir)
        else:
            log.info("Clonando: %s -> %s", repo, repo_dir)
            clone_cmd = ["git", "clone"]
            if gr.shallow and (gr.branch or gr.tag):
                # shallow clone funciona melhor com branch/tag
                clone_cmd += ["--depth", "1"]
            if gr.branch:
                clone_cmd += ["--branch", gr.branch]
            clone_cmd += [repo, str(repo_dir)]
            run_cmd(clone_cmd)

        # Checkout

        # Lockfile: se existir commit travado para este pacote, priorizar
        locked = lockfile_get(meta.full_name)
        locked_commit = None
        if locked and locked.get("repo") == repo and locked.get("commit"):
            locked_commit = locked["commit"]
            log.info("Usando commit travado no lockfile para %s: %s", meta.full_name, locked_commit)


        if locked_commit:
            run_cmd(["git", "checkout", "--detach", locked_commit], cwd=repo_dir)
        elif gr.commit:
            run_cmd(["git", "checkout", "--detach", gr.commit], cwd=repo_dir)
        elif gr.tag:
            run_cmd(["git", "checkout", "--detach", gr.tag], cwd=repo_dir)
        elif gr.branch:
            run_cmd(["git", "checkout", gr.branch], cwd=repo_dir)
            run_cmd(["git", "pull", "--ff-only"], cwd=repo_dir)
        else:
            run_cmd(["git", "checkout", "--detach"], cwd=repo_dir)
            run_cmd(["git", "pull", "--ff-only"], cwd=repo_dir)

        # Registrar commit efetivo no lockfile (reprodutibilidade)
        try:
            cp = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_dir), capture_output=True, text=True, check=True)
            head = cp.stdout.strip()
            lockfile_set(meta.full_name, {"repo": repo, "commit": head, "ref": gr.resolved_ref()})
        except Exception:
            pass

        if gr.submodules:
            run_cmd(["git", "submodule", "update", "--init", "--recursive"], cwd=repo_dir)

        # Registrar commit efetivo (útil para versionamento)
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo_dir), text=True).strip()
        (repo_dir / ".srcpkg_commit").write_text(commit + "\n", encoding="utf-8")

        return repo_dir

    raise SystemExit(f"source.kind não suportado: {meta.source.kind}")


def extract_source(meta: PackageMeta, src_obj: Path, workdir: Path, dry_run: bool = False) -> Path:
    """
    Para tarball: extrai em workdir e retorna diretório raiz do fonte.
    Para git: faz uma cópia limpa (rsync-like) para workdir/src e retorna.
    """
    if dry_run:
        return workdir / "src"

    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    if meta.source.kind == "git":
        # Copiar checkout para workdir/src (evita build "sujo" no cache)
        src_dir = workdir / "src"
        shutil.copytree(src_obj, src_dir, symlinks=True, dirs_exist_ok=False)
        # Remover .git para não confundir builds
        git_dir = src_dir / ".git"
        if git_dir.exists():
            shutil.rmtree(git_dir, ignore_errors=True)
        return src_dir

    # tarball
    tar_path = src_obj
    base = workdir.resolve()
    with tarfile.open(tar_path, "r:*") as tf:
        # Segurança: impedir path traversal e links perigosos
        for m in tf.getmembers():
            # Tar pode trazer paths absolutos; normalize
            name = m.name.lstrip("/")

            # Bloqueia '..' na path
            if ".." in Path(name).parts:
                raise SystemExit(f"Tar inseguro (..): {m.name}")

            dest = (workdir / name).resolve()
            if not is_relative_to(dest, base):
                raise SystemExit(f"Tar inseguro (path traversal): {m.name}")


            # Endurecimento: bloquear hardlinks/symlinks perigosos
            if m.islnk() or m.issym():
                raw_link = m.linkname or ""
                # alvo absoluto: rejeitar
                if raw_link.startswith("/"):
                    raise SystemExit(f"Tar inseguro (link absoluto): {m.name} -> {raw_link}")

                linkname = raw_link
                # proíbe .. no alvo
                if ".." in Path(linkname).parts:
                    raise SystemExit(f"Tar inseguro (link ..): {m.name} -> {raw_link}")

                # verifica se o alvo resolve dentro do workdir (com base no diretório do link)
                link_parent = (workdir / name).parent
                resolved_target = (link_parent / linkname).resolve()
                if not is_relative_to(resolved_target, base):
                    raise SystemExit(f"Tar inseguro (link fora): {m.name} -> {raw_link}")

        tf.extractall(workdir)

    # Se o tar tem uma raiz única, usar ela
    entries = [p for p in workdir.iterdir() if p.name not in (".", "..")]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    src_dir = workdir / "src"
    src_dir.mkdir()
    for p in entries:
        shutil.move(str(p), str(src_dir / p.name))
    return src_dir


def apply_patches(src_dir: Path, pkg_dir: Path, log_file: Optional[Path], dry_run: bool = False) -> None:
    patches_dir = pkg_dir / "patches"
    if not patches_dir.exists():
        return
    require_tools(["patch"])
    for patch in sorted(patches_dir.glob("*.patch")):
        log.info("Aplicando patch: %s", patch.name)
        run_cmd(["patch", "-p1", "-i", str(patch)], cwd=src_dir, log_file=log_file, dry_run=dry_run)


# ----------------------------
# Manifesto e empacotamento
# ----------------------------

def build_manifest(destdir: Path) -> Dict[str, Any]:
    """
    Gera manifesto determinístico do conteúdo de destdir.
    paths são POSIX relativos à raiz '/'.
    Para cada entrada:
      - type: file|dir|symlink
      - sha256 (file)
      - target (symlink)
    """
    out: Dict[str, Any] = {"entries": {}}
    entries: Dict[str, Dict[str, Any]] = out["entries"]

    # Caminhar em ordem determinística
    all_paths: List[Path] = []
    for root, dirs, files in os.walk(destdir, topdown=True, followlinks=False):
        dirs.sort()
        files.sort()
        rootp = Path(root)
        for d in dirs:
            all_paths.append(rootp / d)
        for f in files:
            all_paths.append(rootp / f)

    for p in all_paths:
        rel = p.relative_to(destdir)
        rel_posix = "/" + rel.as_posix()
        try:
            st = p.lstat()
        except FileNotFoundError:
            continue

        if p.is_symlink():
            entries[rel_posix] = {"type": "symlink", "target": os.readlink(p)}
        elif p.is_dir():
            entries[rel_posix] = {"type": "dir"}
        elif p.is_file():
            entries[rel_posix] = {"type": "file", "sha256": sha256_file(p)}
        else:
            # tipos especiais (device, fifo) — ignorar para segurança
            entries[rel_posix] = {"type": "special"}
    return out


def package_destdir(destdir: Path, out_pkg: Path, dry_run: bool = False) -> None:
    require_tools(["tar", "zstd"])
    cmd = f"cd {shlex.quote(str(destdir))} && tar -cf - . | zstd -T0 -q -o {shlex.quote(str(out_pkg))}"
    run_cmd(["sh", "-c", cmd], dry_run=dry_run)


def _extract_pkg_to_dir(pkg_path: Path, out_dir: Path, dry_run: bool = False, keep_perms: bool = False) -> None:
    """
    Extrai um pacote .tar.zst para out_dir usando tar+zstd.
    Endurecido: --no-same-owner por padrão e (por segurança) --no-same-permissions, a menos que keep_perms=True.
    """
    require_tools(["tar", "zstd"])
    if dry_run:
        log.info("[dry-run] extrair %s -> %s", pkg_path, out_dir)
        return
    out_dir.mkdir(parents=True, exist_ok=True)

    tar_flags = ["tar", "-xpf", "-", "-C", str(out_dir), "--no-same-owner"]
    if not keep_perms:
        tar_flags.append("--no-same-permissions")

    # zstd -d -c pkg | tar ...
    p1 = subprocess.Popen(["zstd", "-d", "-c", str(pkg_path)], stdout=subprocess.PIPE)
    assert p1.stdout is not None
    p2 = subprocess.Popen(tar_flags, stdin=p1.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False)
    p1.stdout.close()
    out, err = p2.communicate()
    rc1 = p1.wait()
    if rc1 != 0 or p2.returncode != 0:
        raise SystemExit(f"Falha ao extrair pacote: {pkg_path}")


def _ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _copy_tree_atomic(src_root: Path, dest_root: Path, manifest: Dict[str, Any], backups_dir: Path, dry_run: bool = False) -> None:
    """
    Aplica o filesystem do staging ao destino (normalmente '/'), com backups para rollback local em caso de falha.
    """
    entries = manifest.get("entries", {})
    # Ordenar: dirs primeiro para criação de árvore; depois files/symlinks
    dirs = [p for p, info in entries.items() if info.get("type") == "dir"]
    others = [p for p, info in entries.items() if info.get("type") in ("file", "symlink")]

    def _abspath(posix_path: str) -> Path:
        return dest_root / posix_path.lstrip("/")

    # Criar dirs
    for p in sorted(dirs, key=lambda x: x.count("/")):
        dst = _abspath(p)
        if dry_run:
            log.info("[dry-run] mkdir -p %s", dst)
            continue
        dst.mkdir(parents=True, exist_ok=True)

    # Aplicar files/symlinks com backup
    for p in sorted(others):
        info = entries[p]
        dst = _abspath(p)
        src = src_root / p.lstrip("/")

        if dry_run:
            log.info("[dry-run] instalar %s -> %s", src, dst)
            continue

        _ensure_parent_dir(dst)

        # backup se existir
        if dst.exists() or dst.is_symlink():
            rel = Path(p.lstrip("/"))
            bkp = backups_dir / rel
            bkp.parent.mkdir(parents=True, exist_ok=True)
            try:
                if dst.is_symlink():
                    target = os.readlink(dst)
                    bkp.parent.mkdir(parents=True, exist_ok=True)
                    # guardar symlink como arquivo texto simples + marker
                    (bkp.parent / (bkp.name + ".symlink")).write_text(target, encoding="utf-8")
                elif dst.is_file():
                    shutil.copy2(dst, bkp)
                elif dst.is_dir():
                    # diretório: nada para copiar
                    pass
            except Exception:
                pass

        # instalar
        if info.get("type") == "symlink":
            if dst.exists() or dst.is_symlink():
                dst.unlink()
            target = os.readlink(src)
            os.symlink(target, dst)
        else:
            # arquivo normal
            tmp = dst.with_suffix(dst.suffix + ".tmp.srcpkg")
            if tmp.exists():
                tmp.unlink()
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)

    # Ajustar permissões de dirs conforme staging (opcional)
    # Mantemos permissões padrão do sistema por segurança (as permissões do tar já são aplicadas no staging).


def _rollback_local_from_backups(dest_root: Path, backups_dir: Path) -> None:
    """
    Rollback local (transacional) durante uma instalação que falhou: restaura backups coletados.
    """
    if not backups_dir.exists():
        return
    for root, dirs, files in os.walk(backups_dir, topdown=False):
        rootp = Path(root)
        for f in files:
            fp = rootp / f
            rel = fp.relative_to(backups_dir)
            dst = dest_root / rel
            if f.endswith(".symlink"):
                # restaurar symlink
                target = fp.read_text(encoding="utf-8")
                dst = dest_root / rel.parent / rel.name.replace(".symlink", "")
                try:
                    if dst.exists() or dst.is_symlink():
                        dst.unlink()
                    os.symlink(target, dst)
                except Exception:
                    pass
            else:
                try:
                    _ensure_parent_dir(dst)
                    shutil.copy2(fp, dst)
                except Exception:
                    pass


def install_binary(
    meta: PackageMeta,
    pkg_path: Path,
    manifest: Dict[str, Any],
    db: Dict[str, Any],
    dry_run: bool = False,
    *,
    force: bool = False,
    explicit: bool = True,
    keep_perms: bool = False,
    use_staging: bool = True,
) -> None:
    """
    Instala com staging (default): extrai para diretório temporário e aplica ao '/' com cópia atômica + backups.
    Atualiza DB (installed, owners e history).
    """
    full = meta.full_name
    owners: Dict[str, str] = db.get("owners", {})
    installed: Dict[str, Any] = db.get("installed", {})
    history: Dict[str, Any] = db.get("history", {})

    existing = installed.get(full)

    # Checar conflitos (DB e FS)
    for path, info in manifest.get("entries", {}).items():
        et = info.get("type")
        if et not in ("file", "symlink"):
            continue

        other_owner = owners.get(path)
        if other_owner and other_owner != full:
            raise SystemExit(f"Conflito: {path} é de {other_owner} (tentando instalar {full})")

        abs_path = Path(path)
        if not abs_path.is_absolute():
            abs_path = Path("/") / path.lstrip("/")

        # Se existe no FS e não é do próprio pacote, tratar como conflito (mais seguro)
        if abs_path.exists() or abs_path.is_symlink():
            if other_owner is None and not (existing and owners.get(path) == full):
                if not force:
                    raise SystemExit(f"Conflito: {path} já existe no sistema e não pertence a nenhum pacote (use --force)")
        # paths do próprio pacote (upgrade/reinstall) são permitidos

    # Histórico para rollback
    if existing:
        rec = dict(existing)
        # preservar artefato antigo, se existir
        if "artifact" in rec:
            pass
        history.setdefault(full, [])
        history[full].insert(0, rec)
        history[full] = history[full][:HISTORY_LIMIT]

    # Instalação
    if dry_run:
        log.info("[dry-run] instalar %s (%s)", full, meta.version)
    else:
        if use_staging:
            with tempfile.TemporaryDirectory(prefix="srcpkg-stage-") as td:
                stage = Path(td) / "rootfs"
                backups = Path(td) / "backups"
                _extract_pkg_to_dir(pkg_path, stage, dry_run=False, keep_perms=keep_perms)
                try:
                    _copy_tree_atomic(stage, Path("/"), manifest, backups, dry_run=False)
                except Exception:
                    _rollback_local_from_backups(Path("/"), backups)
                    raise
        else:
            # modo legado: tar direto em /
            require_tools(["tar", "zstd"])
            cmd = f"zstd -d -c {shlex.quote(str(pkg_path))} | tar -xpf - -C / --no-same-owner"
            if not keep_perms:
                cmd += " --no-same-permissions"
            run_cmd(["sh", "-c", cmd], dry_run=False)

    # Atualizar owners (somente file/symlink)
    for path, info in manifest.get("entries", {}).items():
        if info.get("type") in ("file", "symlink"):
            owners[path] = full

    # Gravar registro instalado
    installed[full] = {
        "version": meta.version,
        "id": meta.id,
        "depends": list(meta.depends),
        "manifest": manifest,
        "explicit": bool(explicit),
        "artifact": str(pkg_path),
    }

    db["owners"] = owners
    db["installed"] = installed
    db["history"] = history
    if not dry_run:
        save_db(db)

def uninstall_package(pkg_full: str, db: Dict[str, Any], dry_run: bool = False) -> None:
    """
    Uninstall inteligente:
    - Remove apenas o que pertence ao pacote (owners)
    - Para arquivos, checa hash atual; se divergente, preserva e reporta
    - Remove diretórios vazios no final (best-effort)
    """
    installed = db.get("installed", {})
    rec = installed.get(pkg_full)
    if not rec:
        raise SystemExit(f"Pacote não instalado: {pkg_full}")

    manifest = rec.get("manifest") or {}
    entries: Dict[str, Any] = manifest.get("entries", {})
    owners: Dict[str, str] = db.get("owners", {})

    # Ordenar: arquivos/symlinks primeiro (mais profundos), depois dirs (também profundos)
    paths = sorted(entries.keys(), key=lambda p: (p.count("/"), p), reverse=True)

    kept_modified: List[str] = []
    removed: List[str] = []

    for path in paths:
        info = entries[path]
        t = info.get("type")
        if t not in ("file", "symlink", "dir"):
            continue

        # ownership check (não remover o que não é nosso)
        if t in ("file", "symlink") and owners.get(path) != pkg_full:
            continue

        abs_path = Path(path)
        # path começa com '/', então Path('/usr/...') ok
        if dry_run:
            log.info("[dry-run] remover %s", abs_path)
            continue

        try:
            if t == "symlink":
                if abs_path.is_symlink():
                    abs_path.unlink()
                    removed.append(path)
            elif t == "file":
                if abs_path.is_file():
                    cur = sha256_file(abs_path)
                    expected = info.get("sha256")
                    if expected and cur.lower() != str(expected).lower():
                        kept_modified.append(path)
                    else:
                        abs_path.unlink()
                        removed.append(path)
            elif t == "dir":
                # remove dir apenas se vazio (best-effort)
                if abs_path.is_dir():
                    try:
                        abs_path.rmdir()
                    except OSError:
                        pass
        except PermissionError:
            raise SystemExit(f"Permissão negada ao remover {abs_path}; execute com sudo")
        except FileNotFoundError:
            pass

    if not dry_run:
        # Atualizar owners removendo os paths removidos
        for p in removed:
            if owners.get(p) == pkg_full:
                owners.pop(p, None)

        db["owners"] = owners
        installed.pop(pkg_full, None)
        db["installed"] = installed
        save_db(db)

    if kept_modified:
        log.warning("Arquivos modificados preservados (%d):", len(kept_modified))
        for p in kept_modified[:50]:
            log.warning("  %s", p)
        if len(kept_modified) > 50:
            log.warning("  ... e mais %d", len(kept_modified) - 50)


# ----------------------------
# Build systems
# ----------------------------

def _env_base(prefix: Path, destdir: Path, jobs: int) -> Dict[str, str]:
    env = os.environ.copy()
    env["PREFIX"] = str(prefix)
    env["DESTDIR"] = str(destdir)
    env["MAKEFLAGS"] = f"-j{jobs}"
    # Ajuda alguns builds
    env.setdefault("PKG_CONFIG_PATH", str(prefix / "lib/pkgconfig") + ":" + str(prefix / "share/pkgconfig"))
    return env


def build_with_autotools(src_dir: Path, env: Dict[str, str], flags: BuildConfig, log_file: Path, jobs: int, dry_run: bool) -> None:
    # Suporte a autoreconf se não existir configure, mas houver configure.ac
    if not (src_dir / "configure").exists():
        if (src_dir / "configure.ac").exists() or (src_dir / "configure.in").exists():
            require_tools(["autoreconf"])
            run_cmd(["autoreconf", "-fi"], cwd=src_dir, env=env, log_file=log_file, dry_run=dry_run)

    if not (src_dir / "configure").exists():
        raise SystemExit("autotools: ./configure não encontrado (e não foi possível gerar)")

    conf = ["./configure", f"--prefix={env['PREFIX']}"] + flags.configure_flags
    run_cmd(conf, cwd=src_dir, env=env, log_file=log_file, dry_run=dry_run)

    mk = ["make", f"-j{jobs}"] + flags.make_flags
    run_cmd(mk, cwd=src_dir, env=env, log_file=log_file, dry_run=dry_run)

    # DESTDIR também como arg para maior compat
    mk_install = ["make", "install", f"DESTDIR={env['DESTDIR']}"] + flags.make_flags
    run_cmd(mk_install, cwd=src_dir, env=env, log_file=log_file, dry_run=dry_run)


def build_with_cmake(src_dir: Path, env: Dict[str, str], flags: BuildConfig, log_file: Path, jobs: int, dry_run: bool) -> None:
    require_tools(["cmake"])
    build_dir = src_dir / "build"
    if not dry_run:
        build_dir.mkdir(exist_ok=True)
    generator = os.environ.get("SRCPKG_CMAKE_GENERATOR")  # opcional
    cmd = ["cmake", "-S", str(src_dir), "-B", str(build_dir), f"-DCMAKE_INSTALL_PREFIX={env['PREFIX']}"]
    if generator:
        cmd += ["-G", generator]
    cmd += flags.cmake_flags
    run_cmd(cmd, env=env, log_file=log_file, dry_run=dry_run)

    run_cmd(["cmake", "--build", str(build_dir), "--parallel", str(jobs)], env=env, log_file=log_file, dry_run=dry_run)

    # DESTDIR via env é amplamente suportado
    run_cmd(["cmake", "--install", str(build_dir)], env=env, log_file=log_file, dry_run=dry_run)


def build_with_make(src_dir: Path, env: Dict[str, str], flags: BuildConfig, log_file: Path, jobs: int, dry_run: bool) -> None:
    require_tools(["make"])
    run_cmd(["make", f"-j{jobs}"] + flags.make_flags, cwd=src_dir, env=env, log_file=log_file, dry_run=dry_run)
    run_cmd(["make", "install", f"DESTDIR={env['DESTDIR']}", f"PREFIX={env['PREFIX']}"] + flags.make_flags,
            cwd=src_dir, env=env, log_file=log_file, dry_run=dry_run)


def build_with_meson(src_dir: Path, env: Dict[str, str], flags: BuildConfig, log_file: Path, jobs: int, dry_run: bool) -> None:
    require_tools(["meson", "ninja"])
    build_dir = src_dir / "build"
    if not dry_run:
        build_dir.mkdir(exist_ok=True)
    setup = ["meson", "setup", str(build_dir), str(src_dir), f"--prefix={env['PREFIX']}"] + flags.meson_flags
    run_cmd(setup, env=env, log_file=log_file, dry_run=dry_run)
    run_cmd(["ninja", "-C", str(build_dir), f"-j{jobs}"], env=env, log_file=log_file, dry_run=dry_run)
    # DESTDIR respeitado por meson/ninja em env
    run_cmd(["ninja", "-C", str(build_dir), "install"], env=env, log_file=log_file, dry_run=dry_run)


def build_with_cargo(src_dir: Path, env: Dict[str, str], flags: BuildConfig, log_file: Path, jobs: int, dry_run: bool) -> None:
    require_tools(["cargo"])
    # Instala em DESTDIR + PREFIX usando cargo install (mais previsível)
    # OBS: alguns projetos não suportam "cargo install --path ." adequadamente, mas é padrão.
    target_root = Path(env["DESTDIR"]) / env["PREFIX"].lstrip("/")
    cmd = ["cargo", "install", "--path", ".", "--root", str(target_root)] + flags.cargo_flags
    run_cmd(cmd, cwd=src_dir, env=env, log_file=log_file, dry_run=dry_run)


def build_with_go(src_dir: Path, env: Dict[str, str], flags: BuildConfig, log_file: Path, jobs: int, dry_run: bool) -> None:
    require_tools(["go"])
    # Convenção: construir binário principal e instalar em PREFIX/bin.
    # Recipe pode customizar via go_flags, por ex.: ["./cmd/foo", "-ldflags=..."]
    outdir = Path(env["DESTDIR"]) / env["PREFIX"].lstrip("/") / "bin"
    if not dry_run:
        outdir.mkdir(parents=True, exist_ok=True)
    # Se o recipe não especificar pacote, assume "./..."
    pkg = flags.go_flags[0] if flags.go_flags else "."
    cmd = ["go", "build", "-o", str(outdir / src_dir.name)]
    # flags adicionais após pacote
    extra = flags.go_flags[1:] if flags.go_flags else []
    cmd += extra + [pkg]
    run_cmd(cmd, cwd=src_dir, env=env, log_file=log_file, dry_run=dry_run)

def build_with_python(src_dir: Path, env: Dict[str, str], flags: BuildConfig, log_file: Path, jobs: int, dry_run: bool) -> None:
    # PEP 517: python -m build (requer build) + pip install --prefix/--root
    require_tools([sys.executable])
    # Preferir pip (muito comum)
    # pip install . --prefix PREFIX --root DESTDIR
    cmd = [sys.executable, "-m", "pip", "install", ".", "--no-deps", f"--prefix={env['PREFIX']}", f"--root={env['DESTDIR']}"] + flags.python_flags
    run_cmd(cmd, cwd=src_dir, env=env, log_file=log_file, dry_run=dry_run)


def build_custom(src_dir: Path, env: Dict[str, str], flags: BuildConfig, log_file: Path, dry_run: bool) -> None:
    script = flags.custom_script or "build.sh"
    run_cmd(["sh", script], cwd=src_dir, env=env, log_file=log_file, dry_run=dry_run)


def artifact_paths(meta: PackageMeta) -> Tuple[Path, Path, Path]:
    """
    Retorna (pkg_path, manifest_path, latest_symlink).
    Artefatos são versionados para suportar rollback.
    """
    pkg = BIN_CACHE / f"{meta.id}-{meta.version}.tar.zst"
    man = BIN_CACHE / f"{meta.id}-{meta.version}.manifest.json"
    latest = BIN_CACHE / f"{meta.id}.tar.zst"
    return pkg, man, latest


def build_package(meta: PackageMeta, pkg_dir: Path, prefix: Path, jobs: int, dry_run: bool = False) -> Tuple[Path, Dict[str, Any]]:
    """
    Retorna (bin_pkg_path, manifest). Usa cache binário versionado.
    """
    ensure_dirs()
    log_file = LOG_DIR / f"{meta.id}.log"

    with build_lock(meta.id):
        # Preparar diretórios
        workdir = BUILD_ROOT / meta.id
        destdir = workdir / "dest"
        pkg_path, man_path, latest_link = artifact_paths(meta)

        if pkg_path.exists() and man_path.exists() and not dry_run:
            log.info("Binário em cache: %s", pkg_path)
            try:
                manifest = json.loads(man_path.read_text(encoding="utf-8"))
            except Exception:
                manifest = {}
            try:
                if latest_link.is_symlink() or latest_link.exists():
                    latest_link.unlink()
                latest_link.symlink_to(pkg_path.name)
            except Exception:
                pass
            return pkg_path, manifest

        # Limpar workdir anterior
        if workdir.exists() and not dry_run:
            shutil.rmtree(workdir, ignore_errors=True)
        if not dry_run:
            workdir.mkdir(parents=True, exist_ok=True)
            destdir.mkdir(parents=True, exist_ok=True)

        # Fonte
        src_dir = fetch_source(meta, workdir=workdir, dry_run=dry_run)
        apply_patches(src_dir, pkg_dir, log_file=log_file, dry_run=dry_run)

        env = _env_base(prefix=prefix, destdir=destdir, jobs=jobs)

        # Build
        sysname = meta.build.system
        log.info("Build %s (%s)", meta.full_name, sysname)

        if not dry_run:
            if destdir.exists():
                shutil.rmtree(destdir)
            destdir.mkdir(parents=True, exist_ok=True)

        try:
            if sysname == "autotools":
                build_with_autotools(src_dir, env, meta.build, log_file, jobs, dry_run)
            elif sysname == "cmake":
                build_with_cmake(src_dir, env, meta.build, log_file, jobs, dry_run)
            elif sysname == "make":
                build_with_make(src_dir, env, meta.build, log_file, jobs, dry_run)
            elif sysname == "meson":
                build_with_meson(src_dir, env, meta.build, log_file, jobs, dry_run)
            elif sysname == "cargo":
                build_with_cargo(src_dir, env, meta.build, log_file, jobs, dry_run)
            elif sysname == "go":
                build_with_go(src_dir, env, meta.build, log_file, jobs, dry_run)
            elif sysname == "python":
                build_with_python(src_dir, env, meta.build, log_file, jobs, dry_run)
            elif sysname == "custom":
                build_custom(src_dir, env, meta.build, log_file, jobs, dry_run)
            else:
                raise SystemExit(f"Sistema de build desconhecido: {sysname}")
        except subprocess.CalledProcessError as e:
            raise SystemExit(f"{e}\\nVeja o log: {log_file}")

        # Manifest + package
        manifest = build_manifest(destdir) if not dry_run else {"entries": {}}
        log.info("Empacotando: %s", pkg_path)
        package_destdir(destdir, pkg_path, dry_run=dry_run)
        if not dry_run:
            man_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
            try:
                if latest_link.is_symlink() or latest_link.exists():
                    latest_link.unlink()
                latest_link.symlink_to(pkg_path.name)
            except Exception:
                pass

        return pkg_path, manifest


# ----------------------------
# Dependências e ordens
# ----------------------------

def resolve_with_deps(pkg_full: str, tree: Path = PKG_TREE) -> Dict[str, Tuple[PackageMeta, Path]]:
    """
    Retorna dict full_name -> (meta, pkg_dir) incluindo dependências recursivas.
    """
    metas: Dict[str, Tuple[PackageMeta, Path]] = {}

    def _rec(p: str) -> None:
        if p in metas:
            return
        meta, pkg_dir = load_package_meta(p, tree=tree)
        metas[p] = (meta, pkg_dir)
        for dep in meta.depends:
            _rec(dep)

    _rec(pkg_full)
    return metas


def topo_sort(metas: Dict[str, Tuple[PackageMeta, Path]]) -> List[str]:
    graph: Dict[str, List[str]] = {}
    for name, (m, _) in metas.items():
        graph[name] = list(m.depends)

    temp: set[str] = set()
    perm: set[str] = set()
    order: List[str] = []

    def dfs(n: str) -> None:
        if n in perm:
            return
        if n in temp:
            raise SystemExit(f"Ciclo de dependência detectado em {n}")
        temp.add(n)
        for d in graph.get(n, []):
            dfs(d)
        temp.remove(n)
        perm.add(n)
        order.append(n)

    for n in graph:
        dfs(n)

    return order


# ----------------------------
# Operações principais
# ----------------------------

def resolve_and_build(pkg_full: str, prefix: Path, jobs: int, tree: Path, dry_run: bool) -> None:
    metas = resolve_with_deps(pkg_full, tree=tree)
    order = topo_sort(metas)
    for p in order:
        meta, pkg_dir = metas[p]
        build_package(meta, pkg_dir, prefix=prefix, jobs=jobs, dry_run=dry_run)


def resolve_and_install(
    pkg_full: str,
    prefix: Path,
    jobs: int,
    tree: Path,
    dry_run: bool,
    *,
    force: bool = False,
    keep_perms: bool = False,
    use_staging: bool = True,
) -> None:
    with db_lock():
        db = load_db()
        metas = resolve_with_deps(pkg_full, tree=tree)
        order = topo_sort(metas)

        explicit_set = {pkg_full}

        for p in order:
            meta, pkg_dir = metas[p]
            pkg_path, man_path, _latest = artifact_paths(meta)

            manifest: Dict[str, Any] = {}
            if pkg_path.exists() and man_path.exists() and not dry_run:
                try:
                    manifest = json.loads(man_path.read_text(encoding="utf-8"))
                except Exception:
                    manifest = {}

                installed = db.get("installed", {}).get(meta.full_name)
                if installed and installed.get("version") == meta.version:
                    log.info("Já instalado (mesma versão): %s", meta.full_name)
                    continue

            if not manifest.get("entries"):
                pkg_path, manifest = build_package(meta, pkg_dir, prefix=prefix, jobs=jobs, dry_run=dry_run)

                if not manifest.get("entries") and not dry_run:
                    # última chance: tentar no manifesto do DB (upgrade)
                    old = db.get("installed", {}).get(meta.full_name)
                    if old and old.get("manifest"):
                        manifest = old["manifest"]
                    else:
                        raise SystemExit(f"Manifesto ausente para {meta.full_name}. Rebuild necessário.")

            install_binary(
                meta,
                pkg_path,
                manifest,
                db=db,
                dry_run=dry_run,
                force=force,
                explicit=(meta.full_name in explicit_set),
                keep_perms=keep_perms,
                use_staging=use_staging,
            )

def rebuild_all(prefix: Path, jobs: int, tree: Path, dry_run: bool, *, keep_perms: bool = False, use_staging: bool = True) -> None:
    with db_lock():
        db = load_db()
        installed = db.get("installed", {})
        if not installed:
            log.info("Nenhum pacote instalado.")
            return

        metas: Dict[str, Tuple[PackageMeta, Path]] = {}
        for full in installed.keys():
            meta, pkg_dir = load_package_meta(full, tree=tree)
            metas[full] = (meta, pkg_dir)

        order = topo_sort(metas)
        for full in order:
            meta, pkg_dir = metas[full]
            bin_path, manifest = build_package(meta, pkg_dir, prefix=prefix, jobs=jobs, dry_run=dry_run)
            if not manifest.get("entries") and not dry_run:
                raise SystemExit(f"Manifesto ausente após rebuild para {full}")

            explicit = bool(installed.get(full, {}).get("explicit", False))
            install_binary(meta, bin_path, manifest, db=db, dry_run=dry_run, explicit=explicit, keep_perms=keep_perms, use_staging=use_staging)

def upgrade_changed(prefix: Path, jobs: int, tree: Path, dry_run: bool, *, keep_perms: bool = False, use_staging: bool = True) -> None:
    with db_lock():
        db = load_db()
        installed = db.get("installed", {})
        if not installed:
            log.info("Nenhum pacote instalado.")
            return

        to_upgrade: List[str] = []
        for full, rec in installed.items():
            meta, _ = load_package_meta(full, tree=tree)
            if str(rec.get("version")) != meta.version:
                to_upgrade.append(full)

        if not to_upgrade:
            log.info("Nenhum upgrade pendente (versões iguais).")
            return

        metas: Dict[str, Tuple[PackageMeta, Path]] = {}
        for full in to_upgrade:
            meta, pkg_dir = load_package_meta(full, tree=tree)
            metas[full] = (meta, pkg_dir)

        order = topo_sort(metas)
        for full in order:
            meta, pkg_dir = metas[full]
            bin_path, manifest = build_package(meta, pkg_dir, prefix=prefix, jobs=jobs, dry_run=dry_run)
            if not manifest.get("entries") and not dry_run:
                raise SystemExit(f"Manifesto ausente após upgrade para {full}")
            explicit = bool(installed.get(full, {}).get("explicit", False))
            install_binary(meta, bin_path, manifest, db=db, dry_run=dry_run, explicit=explicit, keep_perms=keep_perms, use_staging=use_staging)

def list_installed() -> None:
    db = load_db()
    installed = db.get("installed", {})
    if not installed:
        print("(nenhum)")
        return
    for full in sorted(installed.keys()):
        rec = installed[full]
        print(f"{full} {rec.get('version','?')}")


def sync_git(tree: Path, push: bool = False, dry_run: bool = False) -> None:
    require_tools(["git"])
    if not tree.exists():
        raise SystemExit(f"Árvore não encontrada: {tree}")
    run_cmd(["git", "pull", "--rebase"], cwd=tree, dry_run=dry_run)
    if push:
        run_cmd(["git", "push"], cwd=tree, dry_run=dry_run)


# ----------------------------
# CLI
# ----------------------------

def rollback(pkg_full: str, dry_run: bool = False, *, keep_perms: bool = False, use_staging: bool = True, force: bool = False) -> None:
    """
    Rollback: reinstala a versão anterior do pacote a partir do histórico/cache.
    """
    with db_lock():
        db = load_db()
        installed = db.get("installed", {})
        history = db.get("history", {})
        if pkg_full not in installed:
            raise SystemExit(f"{pkg_full} não está instalado.")
        hist = history.get(pkg_full, [])
        if not hist:
            raise SystemExit(f"Sem histórico para rollback de {pkg_full}.")

        target = hist.pop(0)  # versão anterior mais recente
        # empurrar o atual de volta para histórico
        current = dict(installed[pkg_full])
        hist.insert(0, current)
        history[pkg_full] = hist[:HISTORY_LIMIT]
        db["history"] = history

        artifact = Path(str(target.get("artifact", "")))
        if not artifact.exists():
            # tentar reconstruir nome comum
            pid = target.get("id")
            ver = target.get("version")
            if pid and ver:
                artifact = BIN_CACHE / f"{pid}-{ver}.tar.zst"
        if not artifact.exists():
            raise SystemExit(f"Artefato de rollback não encontrado no cache: {artifact}")

        manifest = target.get("manifest") or {}
        if not manifest.get("entries"):
            raise SystemExit(f"Manifesto ausente no histórico para {pkg_full}.")

        # instalar usando fluxo normal (staging), mas meta mínimo
        meta = PackageMeta(
            category=pkg_full.split("/")[0],
            name=pkg_full.split("/")[1],
            version=str(target.get("version", "")),
            depends=list(target.get("depends", [])) if isinstance(target.get("depends", []), list) else [],
            source=SourceInfo(kind="tar", url="", sha256="0"*64),
            build=BuildConfig(system="custom"),
        )

        install_binary(meta, artifact, manifest, db=db, dry_run=dry_run, force=force, explicit=bool(target.get("explicit", False)), keep_perms=keep_perms, use_staging=use_staging)


def autoremove(dry_run: bool = False) -> None:
    """
    Remove pacotes órfãos (não explícitos) que não são dependência de nenhum pacote explícito.
    Estratégia conservadora: usa depends registrados no DB (não avalia deps do sistema).
    """
    with db_lock():
        db = load_db()
        installed: Dict[str, Any] = db.get("installed", {})
        if not installed:
            log.info("Nenhum pacote instalado.")
            return

        roots = {p for p, rec in installed.items() if rec.get("explicit")}
        if not roots:
            log.info("Nenhum pacote marcado como explícito. Nada a fazer.")
            return

        required = set()
        stack = list(roots)
        while stack:
            p = stack.pop()
            if p in required:
                continue
            required.add(p)
            deps = installed.get(p, {}).get("depends", [])
            if isinstance(deps, list):
                for d in deps:
                    if d in installed and d not in required:
                        stack.append(d)

        candidates = [p for p, rec in installed.items() if not rec.get("explicit") and p not in required]
        if not candidates:
            log.info("Nenhum órfão detectado.")
            return

        # remover em ordem que reduz risco: primeiro os que não são deps de outros candidatos
        # (topo invertido do grafo local)
        metas = {p: (load_package_meta(p, tree=PKG_TREE)[0], load_package_meta(p, tree=PKG_TREE)[1]) for p in candidates if True}
        # fallback: se recipe sumiu, ainda remove via uninstall usando manifesto
        for p in sorted(candidates):
            log.info("Autoremove: %s", p)
            uninstall_package(p, db=db, dry_run=dry_run)


def verify(pkg_full: Optional[str] = None) -> None:
    """
    Verifica integridade do filesystem comparando hashes/targets com o manifesto do DB.
    """
    db = load_db()
    installed: Dict[str, Any] = db.get("installed", {})
    targets = [pkg_full] if pkg_full else sorted(installed.keys())
    if pkg_full and pkg_full not in installed:
        raise SystemExit(f"{pkg_full} não está instalado.")

    problems = 0
    for p in targets:
        rec = installed[p]
        manifest = rec.get("manifest") or {}
        entries = manifest.get("entries", {}) if isinstance(manifest, dict) else {}
        for path, info in entries.items():
            t = info.get("type")
            abs_path = Path(path)
            if not abs_path.is_absolute():
                abs_path = Path("/") / path.lstrip("/")
            if t == "file":
                if not abs_path.exists():
                    problems += 1
                    log.warning("[%s] faltando: %s", p, path)
                    continue
                if not abs_path.is_file():
                    problems += 1
                    log.warning("[%s] tipo incorreto: %s", p, path)
                    continue
                h = sha256_file(abs_path)
                if h != info.get("sha256"):
                    problems += 1
                    log.warning("[%s] modificado: %s", p, path)
            elif t == "symlink":
                if not abs_path.is_symlink():
                    problems += 1
                    log.warning("[%s] symlink ausente/tipo incorreto: %s", p, path)
                    continue
                target = os.readlink(abs_path)
                if target != info.get("target"):
                    problems += 1
                    log.warning("[%s] symlink divergente: %s", p, path)

    if problems:
        raise SystemExit(f"Verify encontrou {problems} problema(s).")
    log.info("Verify OK.")


def doctor() -> None:
    """
    Diagnóstico do estado do DB/cache/owners.
    """
    db = load_db()
    installed: Dict[str, Any] = db.get("installed", {})
    owners: Dict[str, str] = db.get("owners", {})
    history: Dict[str, Any] = db.get("history", {})

    issues = 0

    # installed -> artefato/manifest
    for p, rec in installed.items():
        art = rec.get("artifact")
        if art and not Path(str(art)).exists():
            issues += 1
            log.warning("[%s] artefato ausente no cache: %s", p, art)
        man = rec.get("manifest")
        if not isinstance(man, dict) or not man.get("entries"):
            issues += 1
            log.warning("[%s] manifesto ausente/inválido no DB", p)

    # owners -> installed
    for path, owner in owners.items():
        if owner not in installed:
            issues += 1
            log.warning("[owners] %s aponta para pacote inexistente: %s", path, owner)

    # history -> artefatos
    for p, hist in history.items():
        if not isinstance(hist, list):
            continue
        for rec in hist[:HISTORY_LIMIT]:
            art = rec.get("artifact")
            if art and not Path(str(art)).exists():
                issues += 1
                log.warning("[history:%s] artefato ausente: %s", p, art)

    if issues:
        raise SystemExit(f"Doctor encontrou {issues} issue(s).")
    log.info("Doctor OK.")

def main() -> None:
    parser = argparse.ArgumentParser(prog="pkg", description="Gerenciador source-based (protótipo evoluído)")
    parser.add_argument("--tree", default=str(PKG_TREE), help="Diretório da árvore de packages")
    parser.add_argument("--prefix", default=str(DEFAULT_PREFIX), help="Prefixo de instalação (ex.: /usr/local)")
    parser.add_argument("-j", "--jobs", type=int, default=DEFAULT_JOBS, help="Paralelismo de build")
    parser.add_argument("--dry-run", action="store_true", help="Simula as ações (não executa comandos)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Mais logs")
    parser.add_argument("--force", action="store_true", help="Permite sobrescrever arquivos existentes não registrados no DB (cuidado)")
    parser.add_argument("--no-staging", action="store_true", help="Instalação legada: extrai direto em / (menos segura)")
    parser.add_argument("--keep-perms", action="store_true", help="Preservar permissões do tar na instalação (padrão é mais seguro)")

    sub = parser.add_subparsers(dest="cmd", required=True)

    p_b = sub.add_parser("b", help="Build (com dependências), sem instalar")
    p_b.add_argument("pkg", help="categoria/nome")

    p_i = sub.add_parser("i", help="Build se necessário e instala (com dependências)")
    p_i.add_argument("pkg", help="categoria/nome")

    p_rb = sub.add_parser("rb", help="Rebuild todos os instalados (reinstala)")
    p_u = sub.add_parser("u", help="Upgrade dos pacotes com versão do recipe diferente do instalado")

    p_sync = sub.add_parser("sync", help="git pull na árvore de packages (opcional: push)")
    p_sync.add_argument("--push", action="store_true", help="Faz git push após pull")

    sub.add_parser("l", help="Lista instalados")

    p_un = sub.add_parser("uninstall", help="Remove pacote (e apenas arquivos não modificados)")
    p_un.add_argument("pkg", help="categoria/nome")

    p_roll = sub.add_parser("rollback", help="Reinstala a versão anterior a partir do cache/histórico")
    p_roll.add_argument("pkg", help="categoria/nome")

    sub.add_parser("autoremove", help="Remove órfãos não explícitos (seguro e conservador)")

    p_ver = sub.add_parser("verify", help="Verifica integridade (hashes/targets) contra manifesto do DB")
    p_ver.add_argument("pkg", nargs="?", default=None, help="categoria/nome (opcional)")

    sub.add_parser("doctor", help="Diagnóstico do DB/cache")

    args = parser.parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    tree = Path(args.tree)
    prefix = Path(args.prefix)
    jobs = int(args.jobs)
    dry = bool(args.dry_run)
    force = bool(args.force)
    keep_perms = bool(args.keep_perms)
    use_staging = not bool(args.no_staging)

    if args.cmd == "b":
        metas = resolve_with_deps(args.pkg, tree=tree)
        order = topo_sort(metas)
        for full in order:
            meta, pkg_dir = metas[full]
            build_package(meta, pkg_dir, prefix=prefix, jobs=jobs, dry_run=dry)
    elif args.cmd == "i":
        resolve_and_install(args.pkg, prefix=prefix, jobs=jobs, tree=tree, dry_run=dry, force=force, keep_perms=keep_perms, use_staging=use_staging)
    elif args.cmd == "rb":
        rebuild_all(prefix=prefix, jobs=jobs, tree=tree, dry_run=dry, keep_perms=keep_perms, use_staging=use_staging)
    elif args.cmd == "u":
        upgrade_changed(prefix=prefix, jobs=jobs, tree=tree, dry_run=dry, keep_perms=keep_perms, use_staging=use_staging)
    elif args.cmd == "sync":
        sync_git(tree, push=bool(getattr(args, "push", False)), dry_run=dry)
    elif args.cmd == "l":
        list_installed()
    elif args.cmd == "uninstall":
        with db_lock():
            db = load_db()
            uninstall_package(args.pkg, db=db, dry_run=dry)
    elif args.cmd == "rollback":
        rollback(args.pkg, dry_run=dry, keep_perms=keep_perms, use_staging=use_staging, force=force)
    elif args.cmd == "autoremove":
        autoremove(dry_run=dry)
    elif args.cmd == "verify":
        verify(args.pkg)
    elif args.cmd == "doctor":
        doctor()
    else:
        parser.error("Comando desconhecido")

