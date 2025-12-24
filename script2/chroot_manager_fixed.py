#!/usr/bin/env python3
"""
chroot_manager.py — Robust chroot mount/enter manager

Goals:
- Safe, repeatable mounts (proc/sys/dev/devpts/tmp/run/resolv.conf)
- Safe teardown with reverse-order unmount, retries, lazy fallback
- Clean exit on Ctrl+C / termination
- Optional colored, informative prompt inside the chroot

This tool assumes you already have a root filesystem at --root (e.g. debootstrap, pacstrap, tarball).
It does NOT download or create a distro rootfs for you.

Requires: root privileges, Linux.
"""

from __future__ import annotations

import argparse
import atexit
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple


# ----------- Global configuration -----------

_COLOR_ENABLED: bool = True

def set_color_enabled(enabled: bool) -> None:
    global _COLOR_ENABLED
    _COLOR_ENABLED = bool(enabled)

def color_enabled() -> bool:
    # Respect NO_COLOR (https://no-color.org/) and common non-interactive cases.
    if os.environ.get("NO_COLOR") is not None:
        return False
    return _COLOR_ENABLED and sys.stdout.isatty() and os.environ.get("TERM", "") != "dumb"

# ----------- ANSI coloring (no external deps) -----------

class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"

def color(s: str, *codes: str) -> str:
    if not color_enabled() or not codes:
        return s
    return "".join(codes) + s + C.RESET

def info(msg: str) -> None:
    print(color("INFO ", C.GREEN, C.BOLD) + msg)

def warn(msg: str) -> None:
    print(color("WARN ", C.YELLOW, C.BOLD) + msg)

def err(msg: str) -> None:
    print(color("ERRO ", C.RED, C.BOLD) + msg, file=sys.stderr)

# ----------- Utilities -----------

def require_root() -> None:
    if os.geteuid() != 0:
        err("Este comando precisa ser executado como root (sudo).")
        raise SystemExit(1)

def run(cmd: List[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    # Use a clean, predictable locale for parsing
    env = os.environ.copy()
    env.setdefault("LC_ALL", "C")
    if capture:
        return subprocess.run(cmd, check=check, text=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return subprocess.run(cmd, check=check, env=env)

def sh(cmd: str, *, check: bool = True) -> None:
    run(["/bin/sh", "-c", cmd], check=check)


_MOUNTINFO_CACHE: Tuple[float, List[str]] | None = None

def _unescape_mount_path(s: str) -> str:
    # /proc/*/mountinfo uses octal escapes like   for spaces.
    def repl(m: re.Match[str]) -> str:
        return chr(int(m.group(1), 8))
    return re.sub(r"\([0-7]{3})", repl, s)

def read_mountinfo_lines() -> List[str]:
    # Lightweight caching: mount table can change; cache only briefly (0.2s).
    global _MOUNTINFO_CACHE
    now = time.time()
    if _MOUNTINFO_CACHE is not None and (now - _MOUNTINFO_CACHE[0]) < 0.2:
        return _MOUNTINFO_CACHE[1]
    lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace").splitlines()
    _MOUNTINFO_CACHE = (now, lines)
    return lines

def mountpoints_set() -> set[str]:
    mps: set[str] = set()
    for line in read_mountinfo_lines():
        parts = line.split()
        if len(parts) < 5:
            continue
        mp = _unescape_mount_path(parts[4])
        mps.add(os.path.normpath(mp))
    return mps

def path_is_mountpoint(p: Path) -> bool:
    # Avoid external dependencies (mountpoint(1)) by reading /proc/self/mountinfo.
    # Use realpath to match kernel-reported paths.
    rp = os.path.normpath(os.path.realpath(str(p)))
    return rp in mountpoints_set()


def ensure_dir(p: Path, mode: int = 0o755) -> None:
    p.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p, mode)
    except PermissionError:
        # Best-effort; ignore if FS disallows (e.g. some bind mounts)
        pass

def atomic_write(path: Path, data: str, mode: int = 0o644) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    os.chmod(tmp, mode)
    tmp.replace(path)

def read_mountinfo() -> str:
    return Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace")

def list_mounts_under(root: Path) -> List[Path]:
    """
    Return mountpoints under root sorted by descending depth (deepest first).

    Notes:
    - Uses /proc/self/mountinfo and decodes octal escapes (e.g.  ).
    - Avoids Path.resolve() to prevent following symlinks inside the rootfs.
    """
    root_real = os.path.normpath(os.path.realpath(str(root)))
    mounts: List[str] = []
    for line in read_mountinfo_lines():
        parts = line.split()
        if len(parts) < 5:
            continue
        mp = os.path.normpath(_unescape_mount_path(parts[4]))
        # commonpath is robust for "is under" checks.
        try:
            if os.path.commonpath([root_real, mp]) != root_real:
                continue
        except ValueError:
            # Different drives / invalid paths (shouldn't happen on Linux)
            continue
        mounts.append(mp)

    # De-dup, deepest first.
    unique = sorted(set(mounts), key=lambda s: (s.count(os.sep), len(s)), reverse=True)
    return [Path(s) for s in unique]


def umount(path: Path, *, lazy_fallback: bool = True, retries: int = 3) -> None:
    if not path_is_mountpoint(path):
        return
    for i in range(retries):
        cp = run(["umount", str(path)], check=False, capture=True)
        if cp.returncode == 0:
            return
        # EBUSY is common; wait a bit and retry
        time.sleep(0.2 * (i + 1))
    if lazy_fallback:
        cp = run(["umount", "-l", str(path)], check=False, capture=True)
        if cp.returncode != 0:
            raise RuntimeError(f"Falha ao desmontar {path}: {cp.stderr.strip() or cp.stdout.strip()}")

# ----------- Mount plan -----------

@dataclass(frozen=True)
class MountSpec:
    kind: str               # "bind" | "proc" | "sysfs" | "tmpfs" | "devpts"
    src: Optional[Path]
    dst_rel: Path           # destination relative to chroot root
    opts: Optional[str] = None

def _tty_gid_from_group_file(group_path: Path) -> Optional[int]:
    try:
        for line in group_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line or line.startswith("#"):
                continue
            # format: name:passwd:gid:members
            parts = line.split(":")
            if len(parts) >= 3 and parts[0] == "tty":
                return int(parts[2])
    except (OSError, ValueError):
        return None
    return None

def detect_tty_gid(root: Path) -> Optional[int]:
    """Best-effort detection of the 'tty' group id inside the chroot."""
    gid = _tty_gid_from_group_file(root / "etc/group")
    if gid is not None:
        return gid
    # Fallback: host group database.
    try:
        import grp
        return grp.getgrnam("tty").gr_gid
    except Exception:
        return None

def build_devpts_opts(root: Path, devpts_gid: str) -> str:
    """
    Build devpts mount options.

    devpts_gid:
      - 'auto'  : detect tty gid (rootfs -> host fallback); omit if unavailable
      - 'none'  : omit gid=
      - '<int>' : explicit gid
    """
    base = "newinstance,ptmxmode=0666,mode=0620"
    gid_opt: Optional[int] = None
    if devpts_gid == "auto":
        gid_opt = detect_tty_gid(root)
    elif devpts_gid == "none":
        gid_opt = None
    else:
        gid_opt = int(devpts_gid)

    if gid_opt is None:
        return base
    return f"{base},gid={gid_opt}"

def default_mount_plan(root: Path, *, devpts_gid: str = "auto") -> List[MountSpec]:
    return [
        # Minimal pseudo-filesystems
        MountSpec("proc", None, Path("proc")),
        MountSpec("sysfs", None, Path("sys")),
        # /dev from host (bind) for typical toolchains; safer is devtmpfs + selective nodes,
        # but bind is more practical for general builds.
        MountSpec("bind", Path("/dev"), Path("dev"), opts="rbind"),
        # devpts with a best-effort gid for 'tty' (configurable)
        MountSpec("devpts", None, Path("dev/pts"), opts=build_devpts_opts(root, devpts_gid)),
        # volatile dirs
        MountSpec("tmpfs", None, Path("tmp"), opts="mode=1777"),
        MountSpec("tmpfs", None, Path("run"), opts="mode=0755"),
        # resolv.conf from host
        MountSpec("bind", Path("/etc/resolv.conf"), Path("etc/resolv.conf")),
    ]

# ----------- Core operations -----------

def _assert_within_root(dst: Path, root: Path) -> Path:
    root_abs = Path(os.path.abspath(str(root)))
    dst_abs = Path(os.path.abspath(str(dst)))
    try:
        dst_abs.relative_to(root_abs)
    except ValueError:
        raise RuntimeError(f"Destino fora do rootfs (path traversal): {dst_abs} (root={root_abs})")
    return dst_abs

def _reject_symlink_components(dst_abs: Path, root: Path) -> None:
    """Block mounts where any existing component under rootfs is a symlink."""
    root_abs = Path(os.path.abspath(str(root)))
    rel = dst_abs.relative_to(root_abs)
    cur = root_abs
    for part in rel.parts:
        cur = cur / part
        try:
            if cur.exists() and cur.is_symlink():
                raise RuntimeError(f"Destino contém symlink (potencial escape): {cur}")
        except OSError:
            # If we cannot lstat, treat as unsafe.
            raise RuntimeError(f"Falha ao inspecionar caminho (unsafe): {cur}")

def mount_one(root: Path, ms: MountSpec) -> None:
    # IMPORTANT: do not resolve() here; it follows symlinks inside rootfs.
    dst = root / ms.dst_rel
    dst_abs = _assert_within_root(dst, root)
    _reject_symlink_components(dst_abs, root)

    if ms.kind == "bind":
        assert ms.src is not None
        src = Path(os.path.realpath(str(ms.src)))

        if src.is_file():
            ensure_dir(dst_abs.parent)
            if not dst_abs.exists():
                dst_abs.touch(mode=0o644, exist_ok=True)  # type: ignore[arg-type]
        else:
            ensure_dir(dst_abs)

        if ms.opts == "rbind":
            run(["mount", "--rbind", str(src), str(dst_abs)])
            # Make the bind mount private to avoid propagating mounts back to host
            run(["mount", "--make-rprivate", str(dst_abs)])
        else:
            run(["mount", "--bind", str(src), str(dst_abs)])

    elif ms.kind == "proc":
        ensure_dir(dst_abs)
        run(["mount", "-t", "proc", "proc", str(dst_abs)])

    elif ms.kind == "sysfs":
        ensure_dir(dst_abs)
        run(["mount", "-t", "sysfs", "sysfs", str(dst_abs)])

    elif ms.kind == "tmpfs":
        ensure_dir(dst_abs)
        opts = ms.opts or "mode=0755"
        run(["mount", "-t", "tmpfs", "-o", opts, "tmpfs", str(dst_abs)])

    elif ms.kind == "devpts":
        ensure_dir(dst_abs)
        opts = ms.opts or "newinstance,ptmxmode=0666,mode=0620"
        run(["mount", "-t", "devpts", "-o", opts, "devpts", str(dst_abs)])

    else:
        raise ValueError(f"Tipo de mount desconhecido: {ms.kind}")


def do_mount(root: Path, plan: List[MountSpec]) -> None:
    require_root()
    root = root.resolve()
    if not root.exists():
        raise SystemExit(f"Rootfs não existe: {root}")
    info(f"Montando ambientes em {root}")
    for ms in plan:
        dst = root / ms.dst_rel
        if path_is_mountpoint(dst):
            info(f"Já montado: {dst}")
            continue
        info(f"Mount {ms.kind}: {ms.dst_rel}")
        mount_one(root, ms)
    info("Mount concluído.")

def do_umount(root: Path) -> None:
    require_root()
    root = root.resolve()
    info(f"Desmontando mounts sob {root}")
    mounts = list_mounts_under(root)
    if not mounts:
        info("Nenhum mount encontrado sob o chroot.")
        return
    for mp in mounts:
        # Never attempt to unmount the root itself here; only submounts
        if mp == root:
            continue
        try:
            info(f"Umount: {mp}")
            umount(mp)
        except Exception as e:
            warn(str(e))
    info("Desmontagem concluída.")

def ensure_base_dirs(root: Path) -> None:
    # Create common directories expected by builds
    for d in ["proc", "sys", "dev", "dev/pts", "tmp", "run", "etc", "usr", "var", "home", "root"]:
        ensure_dir(root / d)

def write_chroot_profile(root: Path, name: str, target: Optional[str]) -> None:
    """
    Creates a small profile snippet that ensures a nice prompt and sane env.
    This is appended in /etc/profile.d/pkg-chroot.sh
    """
    prof_dir = root / "etc" / "profile.d"
    ensure_dir(prof_dir)
    tag = f"{name}{(':' + target) if target else ''}"
    # Color prompt that works in most POSIX shells (bash, sh)
    script = f"""\
# Generated by chroot_manager.py
# Sets a clear, colored prompt inside the chroot
export CHROOT_NAME="{tag}"
if [ -n "$PS1" ]; then
  # user@host in cyan, chroot tag in magenta, path in blue
  PS1='\\[\\033[1;36m\\]\\u@\\h\\[\\033[0m\\] \\[\\033[1;35m\\][chroot:${{CHROOT_NAME}}]\\[\\033[0m\\] \\[\\033[1;34m\\]\\w\\[\\033[0m\\]\\n$ '
fi
"""
    atomic_write(prof_dir / "pkg-chroot.sh", script, mode=0o644)

def check_rootfs_sanity(root: Path) -> None:
    """
    Basic sanity checks so the user gets actionable errors early.
    """
    # Need a shell inside the chroot
    sh_paths = [root / "bin/sh", root / "usr/bin/sh", root / "bin/bash", root / "usr/bin/bash"]
    if not any(p.exists() for p in sh_paths):
        warn("Nenhum shell encontrado (bin/sh, usr/bin/sh, bin/bash). Você conseguirá entrar apenas se fornecer --shell apontando para um shell existente.")
    # Basic dirs
    ensure_base_dirs(root)

def enter_chroot(
    root: Path,
    *,
    shell: str = "/bin/sh",
    extra_env: Optional[List[str]] = None,
    workdir: str = "/root",
    name: str = "pkg",
    target: Optional[str] = None,
    login: bool = False,
    devpts_gid: str = "auto",
) -> int:
    """
    Enters the chroot with a clean environment and a pretty prompt.

    extra_env: list of KEY=VALUE
    """
    require_root()
    root = root.resolve()
    check_rootfs_sanity(root)
    write_chroot_profile(root, name=name, target=target)

    # Ensure mounts are present
    do_mount(root, default_mount_plan(root, devpts_gid=ns.devpts_gid))

    # Cleanup on exit, even on signals
    cleaned = {"done": False}

    def _cleanup(*_args) -> None:
        if cleaned["done"]:
            return
        cleaned["done"] = True
        try:
            do_umount(root)
        except Exception as e:
            warn(f"Erro durante cleanup: {e}")

    atexit.register(_cleanup)
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: (_cleanup(), sys.exit(130)))

    # Build a clean environment
    env_pairs = {
        "HOME": "/root",
        "TERM": os.environ.get("TERM", "xterm-256color"),
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", ""),
    }
    # Optional toolchain / build vars
    if target:
        env_pairs["TARGET"] = target

    if extra_env:
        for item in extra_env:
            if "=" not in item:
                raise SystemExit(f"Env inválido (esperado KEY=VALUE): {item}")
            k, v = item.split("=", 1)
            env_pairs[k] = v

    # Ensure workdir exists inside rootfs
    ensure_dir(root / workdir.lstrip("/"))

    # Use chroot with env -i for cleanliness
    def _shell_supports_login(shell_path: str) -> bool:
        base = os.path.basename(shell_path)
        return base in {"bash", "dash", "sh", "zsh", "ksh", "mksh", "yash"}

    cmd = [
        "chroot",
        str(root),
        "/usr/bin/env",
        "-i",
        *[f"{k}={v}" for k, v in env_pairs.items() if v != ""],
        shell,
    ]

    if login:
        if _shell_supports_login(shell):
            cmd.append("-l")
        else:
            warn(f"--login ignorado: shell não reconhecido como login-capable: {shell}")


    info("Entrando no chroot. Ao sair do shell, o tool fará desmontagem e limpeza.")
    try:
        cp = run(cmd, check=False)
        return cp.returncode
    finally:
        _cleanup()

def status(root: Path) -> None:
    root = root.resolve()
    mounts = list_mounts_under(root)
    if not mounts:
        info("Nenhum mount detectado sob o chroot.")
        return
    info("Mounts detectados (mais profundos primeiro):")
    for mp in mounts:
        if mp == root:
            continue
        print(" -", mp)

def init_layout(root: Path) -> None:
    require_root()
    root = root.resolve()
    ensure_base_dirs(root)
    # Put a minimal resolv.conf if it doesn't exist (will be bind-mounted when entering)
    rc = root / "etc" / "resolv.conf"
    if not rc.exists():
        atomic_write(rc, "nameserver 1.1.1.1\nnameserver 8.8.8.8\n", mode=0o644)
    info(f"Estrutura mínima criada em {root}")
    warn("Este comando NÃO cria um rootfs de distro. Use debootstrap/pacstrap/tarball para popular o diretório.")

# ----------- CLI -----------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="chroot_manager.py",
        description="Gerenciador robusto de chroot (mount/umount/enter) com prompt colorido e cleanup seguro.",
    )
    p.add_argument("--root", required=True, help="Caminho do rootfs do chroot (ex.: /srv/chroots/alpine)")
    p.add_argument("--no-color", action="store_true", help="Desativa cores (também respeita NO_COLOR).")
    p.add_argument("--devpts-gid", default="auto", help="GID do devpts: auto|none|<gid>. (padrão: auto)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Cria diretórios mínimos esperados dentro do rootfs (não baixa distro).")
    sub.add_parser("mount", help="Monta proc/sys/dev/devpts/tmp/run/resolv.conf no chroot.")
    sub.add_parser("umount", help="Desmonta tudo sob o chroot (ordem segura).")
    sub.add_parser("status", help="Mostra mounts atuais sob o chroot.")

    ep = sub.add_parser("enter", help="Entra no chroot com env limpo, prompt colorido e cleanup automático.")
    ep.add_argument("--shell", default="/bin/sh", help="Shell dentro do chroot (padrão: /bin/sh)")
    ep.add_argument("--login", action="store_true", help="Executa o shell como login (-l) quando suportado.")
    ep.add_argument("--workdir", default="/root", help="Diretório inicial dentro do chroot (padrão: /root)")
    ep.add_argument("--name", default="pkg", help="Nome/tag do chroot para mostrar no prompt (padrão: pkg)")
    ep.add_argument("--target", default=None, help="Target opcional (ex.: aarch64-linux-musl) para exibir no prompt e exportar TARGET")
    ep.add_argument("--env", action="append", default=[], help="Variáveis extras KEY=VALUE (pode repetir)")

    return p.parse_args()

def main() -> None:
    ns = parse_args()
    set_color_enabled(not ns.no_color)
    root = Path(ns.root)

    try:
        if ns.cmd == "init":
            init_layout(root)
        elif ns.cmd == "mount":
            do_mount(root, default_mount_plan(root, devpts_gid=ns.devpts_gid))
        elif ns.cmd == "umount":
            do_umount(root)
        elif ns.cmd == "status":
            status(root)
        elif ns.cmd == "enter":
            code = enter_chroot(
                root,
                shell=ns.shell,
                extra_env=list(ns.env) if ns.env else None,
                workdir=ns.workdir,
                name=ns.name,
                target=ns.target,
                login=ns.login,
                devpts_gid=ns.devpts_gid,
            )
            raise SystemExit(code)
        else:
            raise SystemExit(f"Comando desconhecido: {ns.cmd}")
    except KeyboardInterrupt:
        err("Interrompido.")
        raise SystemExit(130)
    except subprocess.CalledProcessError as e:
        err(f"Falha ao executar: {' '.join(e.cmd)}")
        raise SystemExit(e.returncode if e.returncode is not None else 1)
    except Exception as e:
        err(str(e))
        raise SystemExit(1)

if __name__ == "__main__":
    main()