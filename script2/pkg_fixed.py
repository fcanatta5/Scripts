#!/usr/bin/env python3
import argparse
import hashlib
import json
import yaml
import logging
import os
import shutil
import shlex
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
from urllib.request import urlopen

# Diret?rios padr?o (podem ser alterados via env)
BASE_DIR = Path(os.environ.get("SRCPKG_ROOT", Path.home() / ".srcpkg")).expanduser()
PKG_TREE = Path(os.environ.get("SRCPKG_TREE", Path.cwd() / "packages")).expanduser()
SRC_CACHE = BASE_DIR / "sources"
BIN_CACHE = BASE_DIR / "binpkgs"
BUILD_ROOT = BASE_DIR / "build"
LOG_DIR = BASE_DIR / "logs"
DB_PATH = BASE_DIR / "db.json"

DEFAULT_PREFIX = Path(os.environ.get("SRCPKG_PREFIX", "/usr/local"))
DEFAULT_JOBS = int(os.environ.get("SRCPKG_JOBS", os.cpu_count() or 1))


@dataclass
class SourceInfo:
    url: str
    sha256: str


@dataclass
class BuildConfig:
    system: str  # autotools, cmake, make, custom
    configure_flags: List[str] = field(default_factory=list)
    make_flags: List[str] = field(default_factory=list)
    cmake_flags: List[str] = field(default_factory=list)
    custom_script: Optional[str] = None


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
        # id sem "/" para ser seguro em caminhos de arquivo
        return f"{self.category}-{self.name}-{self.version}"

def ensure_dirs():
    for d in (BASE_DIR, SRC_CACHE, BIN_CACHE, BUILD_ROOT, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load_db() -> Dict[str, dict]:
    if not DB_PATH.exists():
        return {}
    with DB_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_db(db: Dict[str, dict]) -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with DB_PATH.open("w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, sort_keys=True)


def find_package_dir(pkg: str) -> Path:
    """
    pkg deve ser "categoria/nome".
    """
    if "/" not in pkg:
        raise SystemExit(f"Especifique o pacote como categoria/nome, recebido: {pkg}")
    cat, name = pkg.split("/", 1)
    p = PKG_TREE / cat / name
    if not p.is_dir():
        raise SystemExit(f"Pacote n?o encontrado: {p}")
    return p


def load_package_meta(pkg: str) -> PackageMeta:
    pkg_dir = find_package_dir(pkg)
    meta_path = pkg_dir / "package.yml"
    if not meta_path.exists():
        raise SystemExit(f"package.yml não encontrado em {pkg_dir}")
    with meta_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise SystemExit(f"Conteúdo inválido em {meta_path}: esperado um mapeamento YAML")
    try:
        source = SourceInfo(
            url=data["source"]["url"],
            sha256=data["source"]["sha256"],
        )
        build = BuildConfig(
            system=data["build"]["system"],
            configure_flags=data["build"].get("configure_flags", []),
            make_flags=data["build"].get("make_flags", []),
            cmake_flags=data["build"].get("cmake_flags", []),
            custom_script=data["build"].get("custom_script"),
        )
        depends = data.get("depends", [])
        return PackageMeta(
            category=data["category"],
            name=data["name"],
            version=data["version"],
            source=source,
            build=build,
            depends=depends,
        )
    except KeyError as e:
        raise SystemExit(f"Campo obrigatório ausente em package.yml: {e}")

def sha256sum(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download_source(meta: PackageMeta, dry_run: bool = False) -> Path:
    SRC_CACHE.mkdir(parents=True, exist_ok=True)
    # inclui id no nome para diferenciar vers?es
    filename = meta.id + "-" + Path(meta.source.url).name.split("/")[-1]
    dest = SRC_CACHE / filename
    if dest.exists():
        current = sha256sum(dest)
        if current == meta.source.sha256:
            logging.info("Fonte j? em cache e sha256 ok: %s", dest)
            return dest
        else:
            logging.warning("SHA256 incorreto no cache, baixando de novo: %s", dest)
            if not dry_run:
                dest.unlink()

    logging.info("Baixando %s para %s", meta.source.url, dest)
    if dry_run:
        return dest

    try:
        with urlopen(meta.source.url, timeout=30) as resp, dest.open("wb") as out:
            shutil.copyfileobj(resp, out)
    except Exception as e:
        dest.unlink(missing_ok=True)
        raise SystemExit(f"Falha ao baixar {meta.source.url}: {e}")

    h = sha256sum(dest)
    if h != meta.source.sha256:
        dest.unlink(missing_ok=True)
        raise SystemExit(
            f"SHA256 n?o confere para {meta.full_name}: "
            f"esperado {meta.source.sha256}, obtido {h}"
        )
    return dest


def extract_source(tarball: Path, workdir: Path, dry_run: bool = False) -> Path:
    if dry_run:
        logging.info("[dry-run] Extraindo %s em %s", tarball, workdir)
        return workdir / "src"
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tarball, "r:*") as tf:
        members = tf.getmembers()
        base = workdir.resolve()
        for m in members:
            dest = (workdir / m.name).resolve()
            if not str(dest).startswith(str(base)):
                raise SystemExit(f"Tarball {tarball} contém caminho inseguro: {m.name}")
        tf.extractall(workdir, members=members)
    root_entries = list(workdir.iterdir())
    if len(root_entries) == 1 and root_entries[0].is_dir():
        return root_entries[0]
    src_dir = workdir / "src"
    src_dir.mkdir(exist_ok=True)
    for entry in root_entries:
        shutil.move(str(entry), src_dir / entry.name)
    return src_dir


def run_cmd(cmd, cwd: Optional[Path] = None, log_file=None, dry_run: bool = False, env=None) -> None:
    logging.info("Executando: %s", " ".join(cmd))
    if dry_run:
        return
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=log_file or sys.stdout,
        stderr=subprocess.STDOUT,
        check=False,
        text=True,
        env=env,
    )
    if proc.returncode != 0:
        raise SystemExit(f"Comando falhou ({proc.returncode}): {' '.join(cmd)}")


def apply_patches(pkg_dir: Path, src_dir: Path, log_file=None, dry_run: bool = False) -> None:
    patches_dir = pkg_dir / "patches"
    if not patches_dir.is_dir():
        return
    for patch in sorted(patches_dir.glob("*.patch")):
        logging.info("Aplicando patch %s", patch.name)
        cmd = ["patch", "-p1", "-i", str(patch)]
        run_cmd(cmd, cwd=src_dir, log_file=log_file, dry_run=dry_run)


def build_package(meta: PackageMeta, dry_run: bool = False) -> Path:
    ensure_dirs()
    pkg_dir = find_package_dir(meta.full_name)
    tarball = download_source(meta, dry_run=dry_run)
    build_dir = BUILD_ROOT / meta.id
    log_path = LOG_DIR / f"{meta.id}.log"
    if dry_run:
        log_file = None
    else:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = log_path.open("w", encoding="utf-8")
    try:
        src_dir = extract_source(tarball, build_dir, dry_run=dry_run)
        apply_patches(pkg_dir, src_dir, log_file=log_file, dry_run=dry_run)

        env = os.environ.copy()
        destdir = build_dir / "dest"
        destdir.mkdir(parents=True, exist_ok=True)
        env["DESTDIR"] = str(destdir)
        env.setdefault("PREFIX", str(DEFAULT_PREFIX))
        jobs = str(DEFAULT_JOBS)

        # sistema de build
        if meta.build.system == "autotools":
            if (src_dir / "configure").exists():
                run_cmd(
                    ["./configure", f"--prefix={env['PREFIX']}"] + meta.build.configure_flags,
                    cwd=src_dir,
                    log_file=log_file,
                    dry_run=dry_run,
                    env=env,
                )
            run_cmd(
                ["make", f"-j{jobs}"] + meta.build.make_flags,
                cwd=src_dir,
                log_file=log_file,
                dry_run=dry_run,
                env=env,
            )
            run_cmd(
                ["make", "install"],
                cwd=src_dir,
                log_file=log_file,
                dry_run=dry_run,
                env=env,
            )
        elif meta.build.system == "cmake":
            build_sub = src_dir / "build"
            build_sub.mkdir(exist_ok=True)
            run_cmd(
                ["cmake", "-S", str(src_dir), "-B", str(build_sub), f"-DCMAKE_INSTALL_PREFIX={env['PREFIX']}"]
                + meta.build.cmake_flags,
                cwd=src_dir,
                log_file=log_file,
                dry_run=dry_run,
                env=env,
            )
            run_cmd(
                ["cmake", "--build", str(build_sub), "--parallel", jobs],
                cwd=src_dir,
                log_file=log_file,
                dry_run=dry_run,
                env=env,
            )
            run_cmd(
                ["cmake", "--install", str(build_sub)],
                cwd=src_dir,
                log_file=log_file,
                dry_run=dry_run,
                env=env,
            )
        elif meta.build.system == "make":
            run_cmd(
                ["make", f"-j{jobs}"] + meta.build.make_flags,
                cwd=src_dir,
                log_file=log_file,
                dry_run=dry_run,
                env=env,
            )
            run_cmd(
                ["make", "install"],
                cwd=src_dir,
                log_file=log_file,
                dry_run=dry_run,
                env=env,
            )
        elif meta.build.system == "custom":
            script = meta.build.custom_script or "build.sh"
            run_cmd(
                ["sh", script],
                cwd=src_dir,
                log_file=log_file,
                dry_run=dry_run,
                env=env,
            )
        else:
            raise SystemExit(f"Sistema de build desconhecido: {meta.build.system}")

        # copiar files
        files_dir = pkg_dir / "files"
        if files_dir.is_dir():
            logging.info("Copiando arquivos extra de %s", files_dir)
            if not dry_run:
                for root, _, files in os.walk(files_dir):
                    rel = Path(root).relative_to(files_dir)
                    target_root = destdir / rel
                    target_root.mkdir(parents=True, exist_ok=True)
                    for fn in files:
                        src_f = Path(root) / fn
                        dst_f = target_root / fn
                        if dst_f.exists():
                            dst_f.unlink()
                        shutil.copy2(src_f, dst_f)

        # garantir que há algo para empacotar
        if not destdir.exists():
            raise SystemExit(f"Nenhum arquivo instalado em {destdir}, nada para empacotar.")
        # empacotar em tar.zst (usa tar + zstd externos)
        BIN_CACHE.mkdir(parents=True, exist_ok=True)
        out_pkg = BIN_CACHE / f"{meta.id}.tar.zst"
        logging.info("Empacotando binário em %s", out_pkg)
        if not dry_run:
            if out_pkg.exists():
                out_pkg.unlink()
            quoted_destdir = shlex.quote(str(destdir))
            quoted_out = shlex.quote(str(out_pkg))
            cmd = [
                "sh",
                "-c",
                f"cd {quoted_destdir} && tar -cf - . | zstd -T0 -q -o {quoted_out}",
            ]
            run_cmd(cmd, cwd=destdir, log_file=log_file, dry_run=dry_run, env=env)
        return out_pkg
    finally:
        if log_file is not None:
            log_file.close()


def install_binary(meta: PackageMeta, dry_run: bool = False) -> None:
    pkg_file = BIN_CACHE / f"{meta.id}.tar.zst"
    if not pkg_file.exists():
        raise SystemExit(f"Bin?rio n?o encontrado no cache: {pkg_file}, construa antes com 'pkg b {meta.full_name}'")
    logging.info("Instalando %s em /", meta.full_name)
    if dry_run:
        return
    # IMPORTANTE: isso extrai como root — para uso real, rode com sudo ou ajuste o prefixo/DESTDIR
    quoted_pkg = shlex.quote(str(pkg_file))
    cmd = ["sh", "-c", f"zstd -d -c {quoted_pkg} | tar -xf - -C /"]
    run_cmd(cmd, cwd=None, log_file=None, dry_run=False)
    db = load_db()
    db[meta.full_name] = {
        "version": meta.version,
        "id": meta.id,
        "depends": meta.depends,
    }
    save_db(db)


def build_dep_graph(pkgs: List[PackageMeta]) -> Dict[str, List[str]]:
    graph: Dict[str, List[str]] = {}
    for meta in pkgs:
        graph[meta.full_name] = meta.depends
    return graph


def topo_sort(graph: Dict[str, List[str]]) -> List[str]:
    visited: Dict[str, str] = {}
    order: List[str] = []

    def dfs(node: str):
        state = visited.get(node)
        if state == "temp":
            raise SystemExit(f"Ciclo de depend?ncias detectado em {node}")
        if state == "perm":
            return
        visited[node] = "temp"
        for dep in graph.get(node, []):
            dfs(dep)
        visited[node] = "perm"
        order.append(node)

    for n in graph:
        if n not in visited:
            dfs(n)
    return order


def resolve_with_deps(root_pkg: str) -> Dict[str, PackageMeta]:
    metas: Dict[str, PackageMeta] = {}

    def load_recursive(p: str):
        if p in metas:
            return
        m = load_package_meta(p)
        metas[p] = m
        for d in m.depends:
            load_recursive(d)

    load_recursive(root_pkg)
    return metas


def resolve_and_build(pkg: str, dry_run: bool = False) -> None:
    metas = resolve_with_deps(pkg)
    graph = build_dep_graph(list(metas.values()))
    order = topo_sort(graph)
    logging.info("Ordem de build: %s", ", ".join(order))
    for name in order:
        meta = metas[name]
        logging.info("Construindo %s", meta.full_name)
        build_package(meta, dry_run=dry_run)


def resolve_and_install(pkg: str, dry_run: bool = False) -> None:
    metas = resolve_with_deps(pkg)
    graph = build_dep_graph(list(metas.values()))
    order = topo_sort(graph)
    logging.info("Ordem de instala??o: %s", ", ".join(order))
    for name in order:
        meta = metas[name]
        pkg_file = BIN_CACHE / f"{meta.id}.tar.zst"
        if not pkg_file.exists():
            logging.info("Bin?rio de %s n?o encontrado, construindo?", meta.full_name)
            build_package(meta, dry_run=dry_run)
        install_binary(meta, dry_run=dry_run)


def rebuild_all(dry_run: bool = False) -> None:
    db = load_db()
    if not db:
        print("Nenhum pacote instalado no DB.")
        return
    metas: Dict[str, PackageMeta] = {}
    for full_name, _info in db.items():
        metas[full_name] = load_package_meta(full_name)
    graph = build_dep_graph(list(metas.values()))
    order = topo_sort(graph)
    logging.info("Rebuild na ordem: %s", ", ".join(order))
    for name in order:
        meta = metas.get(name)
        if meta is None:
            logging.warning("Ignorando dependência não instalada %s no rebuild_all", name)
            continue
        logging.info("Rebuild de %s", meta.full_name)
        build_package(meta, dry_run=dry_run)
        install_binary(meta, dry_run=dry_run)


def upgrade_changed(dry_run: bool = False) -> None:
    """
    Upgrade ?inteligente?: compara vers?o atual do recipe com a vers?o registrada no DB.
    Se mudou, recompila e reinstala.
    """
    db = load_db()
    if not db:
        print("Nenhum pacote instalado no DB.")
        return
    metas: Dict[str, PackageMeta] = {}
    changed: Dict[str, PackageMeta] = {}
    for full_name, info in db.items():
        meta = load_package_meta(full_name)
        metas[full_name] = meta
        if meta.version != info.get("version"):
            changed[full_name] = meta
    if not changed:
        print("Nenhuma atualiza??o detectada (vers?es iguais ?s instaladas).")
        return
    graph = build_dep_graph(list(metas.values()))
    order = topo_sort(graph)
    to_upgrade = [name for name in order if name in changed]
    logging.info("Pacotes a atualizar: %s", ", ".join(to_upgrade))
    for name in to_upgrade:
        meta = metas[name]
        logging.info("Atualizando %s", meta.full_name)
        build_package(meta, dry_run=dry_run)
        install_binary(meta, dry_run=dry_run)


def sync_git(tree: Path, dry_run: bool = False) -> None:
    if not (tree / ".git").is_dir():
        raise SystemExit(f"{tree} n?o parece ser um reposit?rio git")
    logging.info("Sincronizando reposit?rio git em %s", tree)
    if dry_run:
        return
    run_cmd(["git", "pull", "--rebase"], cwd=tree, log_file=None, dry_run=False)
    run_cmd(["git", "push"], cwd=tree, log_file=None, dry_run=False)


def list_installed() -> None:
    db = load_db()
    if not db:
        print("Nenhum pacote instalado.")
        return
    for name, info in sorted(db.items()):
        print(f"{name} {info.get('version', '?')}")


def main(argv=None):
    ensure_dirs()
    parser = argparse.ArgumentParser(
        prog="pkg",
        description="Gerenciador de pacotes source-based em Python (prot?tipo).",
    )
    parser.add_argument("-n", "--dry-run", action="store_true", help="Mostrar a??es sem execut?-las.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Log detalhado.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_build = sub.add_parser("b", help="Construir pacote e gerar bin?rio (pkg b categoria/nome).")
    p_build.add_argument("pkg")

    p_install = sub.add_parser("i", help="Instalar pacote (usa cache bin?rio ou constr?i).")
    p_install.add_argument("pkg")

    sub.add_parser("r", help="Rebuild de todos os pacotes instalados.")
    sub.add_parser("u", help="Upgrade inteligente (quando a vers?o do recipe muda).")

    p_sync = sub.add_parser("sync", help="Sincronizar ?rvore de pacotes via git.")
    p_sync.add_argument("--tree", type=str, default=str(PKG_TREE), help="Diret?rio da ?rvore de pacotes (git).")

    sub.add_parser("l", help="Listar pacotes instalados.")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    dry = args.dry_run

    if args.cmd == "b":
        resolve_and_build(args.pkg, dry_run=dry)
    elif args.cmd == "i":
        resolve_and_install(args.pkg, dry_run=dry)
    elif args.cmd == "r":
        rebuild_all(dry_run=dry)
    elif args.cmd == "u":
        upgrade_changed(dry_run=dry)
    elif args.cmd == "sync":
        sync_git(Path(args.tree), dry_run=dry)
    elif args.cmd == "l":
        list_installed()
    else:
        parser.error("Comando desconhecido")


if __name__ == "__main__":
    main()