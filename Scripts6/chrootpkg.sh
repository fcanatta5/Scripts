#!/usr/bin/env bash
# chrootpkg.sh — Secure chroot/rootfs manager + "pkg" runner/installer
# Features:
# - Safe chroot execution with reduced env leakage (env -i).
# - Comprehensive logging, dry-run, lock to avoid concurrent operations.
# - Robust mount management with rbind and rslave semantics.
# - Bind-mount /etc/resolv.conf (optional, default ON) instead of copying.
# - Bind-mount arbitrary host directories into rootfs (e.g., ports/Pkgfile tree).
# - Profiles: load settings from a profile file.conf (no env exports needed).
# - Convenience "mu" (mount/unmount toggle) and "m"/"u" aliases.

set -euo pipefail
IFS=$'\n\t'

# ----------------------------
# Defaults
# ----------------------------
DEFAULT_ROOTFS="/srv/chroot/rootfs"
DEFAULT_LOGDIR="/var/log/chrootpkg"
DEFAULT_NAME="rootfs"
DEFAULT_SHELL="/bin/bash"

# Mount points inside rootfs
MNT_PROC="proc"
MNT_SYS="sys"
MNT_DEV="dev"
MNT_RUN="run"
MNT_DEVPTS="dev/pts"

# "Safe" env inside chroot
CHROOT_ENV_VARS=(
  "PATH=/usr/sbin:/usr/bin:/sbin:/bin"
  "HOME=/root"
  "TERM=${TERM:-xterm-256color}"
  "LANG=${LANG:-C}"
  "LC_ALL=${LC_ALL:-C}"
)

# Color
C_RESET=$'\033[0m'
C_BOLD=$'\033[1m'
C_RED=$'\033[31m'
C_GREEN=$'\033[32m'
C_YELLOW=$'\033[33m'
C_BLUE=$'\033[34m'
C_MAGENTA=$'\033[35m'
C_CYAN=$'\033[36m'

# ----------------------------
# Globals
# ----------------------------
ROOTFS="$DEFAULT_ROOTFS"
LOGDIR="$DEFAULT_LOGDIR"
NAME="$DEFAULT_NAME"
DRY_RUN=0
QUIET=0
NO_MOUNTS=0
ENTER_SHELL="$DEFAULT_SHELL"

# resolv.conf bind-mount behavior
RESOLV_MODE="bind"      # bind|copy|off
RESOLV_SRC="/etc/resolv.conf"
RESOLV_DST="etc/resolv.conf"
PRESERVE_RESOLV=0       # when copy mode

# Custom binds: each entry "SRC:DST" (DST relative to rootfs or absolute inside rootfs)
declare -a EXTRA_BINDS=()

LOCK_FD=9
LOCKFILE=""
LOGFILE=""

# ----------------------------
# Logging helpers
# ----------------------------
ts() { date '+%Y-%m-%d %H:%M:%S%z'; }

logfile_path() {
  mkdir -p "$LOGDIR" 2>/dev/null || true
  echo "$LOGDIR/chrootpkg-${NAME}-$(date +%Y%m%d).log"
}

say() { [[ "$QUIET" -eq 1 ]] && return 0; echo -e "$*"; }
note() { say "${C_CYAN}${C_BOLD}::${C_RESET} $*"; }
ok()   { say "${C_GREEN}${C_BOLD}OK${C_RESET} $*"; }
warn() { say "${C_YELLOW}${C_BOLD}WARN${C_RESET} $*"; }
err()  { say "${C_RED}${C_BOLD}ERR${C_RESET} $*"; }

append_log() {
  [[ -n "$LOGFILE" ]] || return 0
  printf '[%s] %s\n' "$(ts)" "$*" >>"$LOGFILE" 2>/dev/null || true
}

run_cmd() {
  append_log "RUN: $*"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    note "(dry-run) $*"
    return 0
  fi
  "$@"
}

die() { err "$*"; append_log "FATAL: $*"; exit 1; }
need_root() { [[ "${EUID:-$(id -u)}" -eq 0 ]] || die "Esta operação requer root. Execute com sudo."; }

# ----------------------------
# Safe path / profile
# ----------------------------
abspath() {
  local p="$1"
  if command -v realpath >/dev/null 2>&1; then
    realpath -m -- "$p"
  else
    python3 - <<'PY' "$p"
import os,sys
print(os.path.abspath(sys.argv[1]))
PY
  fi
}

ensure_dirs() {
  mkdir -p "$LOGDIR" 2>/dev/null || true
}

ensure_rootfs_valid() {
  ROOTFS="$(abspath "$ROOTFS")"
  [[ -n "$ROOTFS" ]] || die "Rootfs inválido"
  [[ "$ROOTFS" != "/" ]] || die "Rootfs não pode ser /"
  [[ -d "$ROOTFS" ]] || die "Rootfs não existe: $ROOTFS"
}

load_profile() {
  local f="$1"
  [[ -f "$f" ]] || die "Profile não encontrado: $f"
  append_log "PROFILE load: $f"
  # shellcheck disable=SC1090
  source "$f"
  # profile may set ROOTFS, NAME, LOGDIR, RESOLV_MODE, EXTRA_BINDS, ENTER_SHELL, NO_MOUNTS, etc.
}

# ----------------------------
# Locking
# ----------------------------
lock_acquire() {
  mkdir -p /run 2>/dev/null || true
  LOCKFILE="/run/chrootpkg.${NAME}.lock"
  exec {LOCK_FD}>"$LOCKFILE" || die "Falha ao abrir lockfile: $LOCKFILE"
  if ! flock -n "$LOCK_FD"; then
    die "Outro processo já está usando este perfil/rootfs (lock: $LOCKFILE)"
  fi
  append_log "LOCK acquired: $LOCKFILE"
}

lock_release() {
  append_log "LOCK release: $LOCKFILE"
  true
}

# ----------------------------
# Mount helpers
# ----------------------------
is_mounted() {
  local target="$1"
  if command -v mountpoint >/dev/null 2>&1; then
    mountpoint -q -- "$target"
  else
    grep -qsE "[[:space:]]$(printf '%s' "$target" | sed 's/[.[\*^$(){}?+|/]/\\&/g')[[:space:]]" /proc/mounts
  fi
}

ensure_mountpoints() {
  run_cmd mkdir -p \
    "$ROOTFS/$MNT_PROC" \
    "$ROOTFS/$MNT_SYS" \
    "$ROOTFS/$MNT_DEV" \
    "$ROOTFS/$MNT_RUN" \
    "$ROOTFS/$MNT_DEVPTS" \
    "$ROOTFS/etc"
}

mount_base() {
  [[ "$NO_MOUNTS" -eq 1 ]] && { warn "Montagens desabilitadas (--no-mounts)"; return 0; }

  ensure_mountpoints

  if ! is_mounted "$ROOTFS/$MNT_PROC"; then
    run_cmd mount -t proc proc "$ROOTFS/$MNT_PROC"
  fi

  if ! is_mounted "$ROOTFS/$MNT_SYS"; then
    run_cmd mount --rbind /sys "$ROOTFS/$MNT_SYS"
    run_cmd mount --make-rslave "$ROOTFS/$MNT_SYS" || true
  fi

  if ! is_mounted "$ROOTFS/$MNT_DEV"; then
    run_cmd mount --rbind /dev "$ROOTFS/$MNT_DEV"
    run_cmd mount --make-rslave "$ROOTFS/$MNT_DEV" || true
  fi

  if [[ -d /run ]]; then
    if ! is_mounted "$ROOTFS/$MNT_RUN"; then
      run_cmd mount --rbind /run "$ROOTFS/$MNT_RUN"
      run_cmd mount --make-rslave "$ROOTFS/$MNT_RUN" || true
    fi
  fi

  run_cmd mkdir -p "$ROOTFS/$MNT_DEVPTS"
}

mount_bind_one() {
  local src="$1" dst_rel="$2"
  local dst
  if [[ "$dst_rel" == /* ]]; then
    dst="$ROOTFS${dst_rel}"
  else
    dst="$ROOTFS/$dst_rel"
  fi
  src="$(abspath "$src")"
  [[ -e "$src" ]] || die "Bind source não existe: $src"
  run_cmd mkdir -p "$(dirname "$dst")"
  if [[ -d "$src" ]]; then
    run_cmd mkdir -p "$dst"
  else
    run_cmd touch "$dst"
  fi
  if ! is_mounted "$dst"; then
    run_cmd mount --bind "$src" "$dst"
    run_cmd mount --make-rslave "$dst" 2>/dev/null || true
  fi
  append_log "BIND: $src -> $dst"
}

mount_resolv() {
  case "$RESOLV_MODE" in
    off) return 0;;
    copy)
      [[ -f "$RESOLV_SRC" ]] || return 0
      local dest="$ROOTFS/$RESOLV_DST"
      run_cmd mkdir -p "$(dirname "$dest")"
      if [[ "$PRESERVE_RESOLV" -eq 1 && -f "$dest" ]]; then
        warn "Preservando $dest (PRESERVE_RESOLV=1)."
        return 0
      fi
      run_cmd cp -f "$RESOLV_SRC" "$dest"
      append_log "RESOLV copy: $RESOLV_SRC -> $dest"
      ;;
    bind)
      [[ -f "$RESOLV_SRC" ]] || { warn "resolv.conf do host não encontrado: $RESOLV_SRC"; return 0; }
      mount_bind_one "$RESOLV_SRC" "$RESOLV_DST"
      ;;
    *) die "RESOLV_MODE inválido: $RESOLV_MODE (use bind|copy|off)";;
  esac
}

mount_extra_binds() {
  local entry src dst
  for entry in "${EXTRA_BINDS[@]}"; do
    src="${entry%%:*}"
    dst="${entry#*:}"
    [[ -n "$src" && -n "$dst" && "$dst" != "$entry" ]] || die "Bind inválido (use SRC:DST): $entry"
    mount_bind_one "$src" "$dst"
  done
}

mount_all() {
  mount_base
  mount_resolv
  mount_extra_binds
  ok "Montagens prontas."
}

umount_one() {
  local t="$1"
  if is_mounted "$t"; then
    append_log "UMOUNT: $t"
    if [[ "$DRY_RUN" -eq 1 ]]; then
      note "(dry-run) umount $t"
      return 0
    fi
    umount "$t" 2>/dev/null || umount -l "$t" 2>/dev/null || true
  fi
}

umount_all() {
  [[ "$NO_MOUNTS" -eq 1 ]] && return 0

  # Unmount extra binds first (deep paths first for safety)
  if [[ ${#EXTRA_BINDS[@]} -gt 0 ]]; then
    local -a dsts=()
    local entry dst
    for entry in "${EXTRA_BINDS[@]}"; do
      dst="${entry#*:}"
      if [[ "$dst" == /* ]]; then
        dsts+=("$ROOTFS${dst}")
      else
        dsts+=("$ROOTFS/$dst")
      fi
    done
    # sort by length desc
    printf '%s\n' "${dsts[@]}" | awk '{print length, $0}' | sort -rn | cut -d' ' -f2- | \
      while IFS= read -r m; do umount_one "$m"; done
  fi

  # resolv bind mount
  if [[ "$RESOLV_MODE" == "bind" ]]; then
    local rd="$ROOTFS/$RESOLV_DST"
    umount_one "$rd"
  fi

  # base mounts reverse order
  umount_one "$ROOTFS/$MNT_RUN"
  umount_one "$ROOTFS/$MNT_DEVPTS"
  umount_one "$ROOTFS/$MNT_DEV"
  umount_one "$ROOTFS/$MNT_SYS"
  umount_one "$ROOTFS/$MNT_PROC"

  ok "Montagens desmontadas."
}

# ----------------------------
# Chroot prompt customization
# ----------------------------
install_chroot_prompt() {
  local profiled="$ROOTFS/etc/profile.d"
  run_cmd mkdir -p "$profiled"
  local f="$profiled/chrootpkg.sh"
  append_log "WRITE: $f"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    note "(dry-run) write $f"
    return 0
  fi
  cat >"$f" <<'EOF'
#!/usr/bin/env sh
if [ -n "$CHROOTPKG_NAME" ]; then
  if [ -t 1 ]; then
    PS1="\[\033[1;35m\](${CHROOTPKG_NAME})\[\033[0m\] \[\033[1;34m\]\u@\h\[\033[0m\]:\[\033[1;36m\]\w\[\033[0m\]\$ "
  else
    PS1="(${CHROOTPKG_NAME}) \u@\h:\w\$ "
  fi
fi
EOF
  chmod 0755 "$f" 2>/dev/null || true
  ok "Prompt do chroot configurado."
}

# ----------------------------
# Safe chroot execution
# ----------------------------
chroot_exec() {
  # usage: chroot_exec <command...>
  ensure_rootfs_valid
  lock_acquire

  trap 'rc=$?; append_log "EXIT rc=$rc"; umount_all || true; lock_release || true; exit $rc' EXIT INT TERM

  mount_all
  install_chroot_prompt

  local -a envargs=()
  local kv
  for kv in "${CHROOT_ENV_VARS[@]}"; do envargs+=("$kv"); done
  envargs+=("CHROOTPKG_NAME=$NAME")

  append_log "CHROOT: $ROOTFS CMD: $*"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    note "(dry-run) chroot $ROOTFS env -i ${envargs[*]} $*"
    return 0
  fi
  chroot "$ROOTFS" /usr/bin/env -i "${envargs[@]}" "$@"
}

# ----------------------------
# Commands
# ----------------------------
cmd_init() {
  need_root
  ensure_rootfs_valid
  run_cmd mkdir -p "$ROOTFS"/{etc,root,usr/bin,usr/sbin,bin,sbin,var,proc,sys,dev,run,tmp}
  run_cmd chmod 1777 "$ROOTFS/tmp" 2>/dev/null || true

  if [[ ! -f "$ROOTFS/etc/profile" ]]; then
    append_log "WRITE: $ROOTFS/etc/profile"
    if [[ "$DRY_RUN" -eq 1 ]]; then
      note "(dry-run) write $ROOTFS/etc/profile"
    else
      cat >"$ROOTFS/etc/profile" <<'EOF'
# /etc/profile
if [ -d /etc/profile.d ]; then
  for f in /etc/profile.d/*.sh; do
    [ -r "$f" ] && . "$f"
  done
fi
EOF
    fi
  fi

  ok "Rootfs inicializado."
}

require_pkg_in_chroot() {
  [[ -x "$ROOTFS/usr/bin/pkg" || -x "$ROOTFS/bin/pkg" ]] || die "Não encontrei 'pkg' dentro do rootfs em /usr/bin/pkg ou /bin/pkg."
}

cmd_pkg() {
  need_root
  ensure_rootfs_valid
  require_pkg_in_chroot
  local pkgpath="/usr/bin/pkg"
  [[ -x "$ROOTFS$pkgpath" ]] || pkgpath="/bin/pkg"
  chroot_exec "$pkgpath" "$@"
}

cmd_install() { [[ $# -ge 1 ]] || die "Uso: install <port> [port...]"; cmd_pkg install "$@"; }
cmd_sysup() { cmd_pkg sysup; }

cmd_run() {
  [[ "${1:-}" == "--" ]] || die "Uso: run -- <comando...>"
  shift
  [[ $# -ge 1 ]] || die "Uso: run -- <comando...>"
  need_root
  ensure_rootfs_valid
  chroot_exec "$@"
}

cmd_enter() { need_root; ensure_rootfs_valid; chroot_exec "$ENTER_SHELL" -l; }

cmd_mount() {
  need_root
  ensure_rootfs_valid
  lock_acquire
  trap 'rc=$?; umount_all || true; lock_release || true; exit $rc' EXIT INT TERM
  mount_all
  ok "Montado."
}

cmd_umount() {
  need_root
  ensure_rootfs_valid
  lock_acquire
  trap 'rc=$?; lock_release || true; exit $rc' EXIT INT TERM
  umount_all
}

cmd_status() {
  ensure_rootfs_valid
  note "Rootfs: $ROOTFS"
  note "Name: $NAME"
  note "Log: $LOGFILE"
  note "RESOLV_MODE: $RESOLV_MODE"
  if [[ ${#EXTRA_BINDS[@]} -gt 0 ]]; then
    note "Extra binds:"
    printf '  - %s\n' "${EXTRA_BINDS[@]}"
  fi

  local t
  for t in "$ROOTFS/$MNT_PROC" "$ROOTFS/$MNT_SYS" "$ROOTFS/$MNT_DEV" "$ROOTFS/$MNT_RUN" "$ROOTFS/$RESOLV_DST"; do
    if [[ -e "$t" ]]; then
      if is_mounted "$t"; then ok "mounted: $t"; else warn "not mounted: $t"; fi
    fi
  done
}

cmd_mu() {
  # mount/unmount toggle
  ensure_rootfs_valid
  if is_mounted "$ROOTFS/$MNT_PROC"; then
    cmd_umount
  else
    cmd_mount
  fi
}

# ----------------------------
# CLI parsing
# ----------------------------
usage() {
  cat <<EOF
${C_BOLD}chrootpkg.sh${C_RESET} — Gerenciador seguro de chroot/rootfs + runner do "pkg"

${C_BOLD}Opções globais:${C_RESET}
  --profile <arquivo.conf>        Carrega configuração de perfil (sem exportar variáveis)
  --rootfs <dir>                  Caminho do rootfs (default: $DEFAULT_ROOTFS)
  --name <name>                   Nome do chroot (default: $DEFAULT_NAME)
  --logdir <dir>                  Diretório de logs (default: $DEFAULT_LOGDIR)
  --dry-run                        Não executa; apenas mostra ações
  --quiet                          Menos saída
  --no-mounts                       Não monta /proc,/sys,/dev,/run (assume já montado)
  --shell <path>                   Shell para "enter" (default: $DEFAULT_SHELL)

${C_BOLD}DNS (/etc/resolv.conf):${C_RESET}
  --resolv bind|copy|off           Modo (default: bind)
  --resolv-src <path>              Origem (default: /etc/resolv.conf)
  --resolv-dst <path>              Destino dentro do rootfs (default: etc/resolv.conf)
  --preserve-resolv                Só para modo copy: não sobrescreve se já existir

${C_BOLD}Bind-mounts extras:${C_RESET}
  --bind SRC:DST                   Faz bind-mount de SRC (host) para DST (dentro do rootfs).
                                   Pode repetir. Ex: --bind /usr/ports:/usr/ports

${C_BOLD}Comandos:${C_RESET}
  init | mount | umount | status | enter | run -- <cmd...>
  pkg <args...>                    Executa "pkg" dentro do chroot
  install <port...>                Atalho para: pkg install ...
  sysup                            Atalho para: pkg sysup
  mu                               Toggle: mount se não montado; umount se montado
  m                                Alias de mu
  u                                Alias de umount

Exemplo com perfil:
  sudo $0 --profile crux.conf init
  sudo $0 --profile crux.conf pkg update
EOF
}

parse_args() {
  ensure_dirs
  LOGFILE="$(logfile_path)"

  local -a positional=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --profile) load_profile "${2:-}"; shift 2;;
      --rootfs) ROOTFS="${2:-}"; shift 2;;
      --name) NAME="${2:-}"; shift 2;;
      --logdir) LOGDIR="${2:-}"; shift 2;;
      --dry-run) DRY_RUN=1; shift;;
      --quiet) QUIET=1; shift;;
      --no-mounts) NO_MOUNTS=1; shift;;
      --shell) ENTER_SHELL="${2:-}"; shift 2;;

      --resolv) RESOLV_MODE="${2:-}"; shift 2;;
      --resolv-src) RESOLV_SRC="${2:-}"; shift 2;;
      --resolv-dst) RESOLV_DST="${2:-}"; shift 2;;
      --preserve-resolv) PRESERVE_RESOLV=1; shift;;

      --bind) EXTRA_BINDS+=("${2:-}"); shift 2;;

      -h|--help) usage; exit 0;;
      --) shift; positional+=("$@"); break;;
      -*) die "Opção desconhecida: $1";;
      *) positional+=("$1"); shift;;
    esac
  done

  set -- "${positional[@]}"
  [[ $# -ge 1 ]] || { usage; exit 1; }
  local cmd="$1"; shift

  ensure_rootfs_valid
  LOGFILE="$(logfile_path)"
  append_log "START cmd=$cmd rootfs=$ROOTFS name=$NAME dry_run=$DRY_RUN resolv=$RESOLV_MODE binds=${#EXTRA_BINDS[@]}"

  case "$cmd" in
    init) cmd_init "$@";;
    mount) cmd_mount "$@";;
    umount) cmd_umount "$@";;
    status) cmd_status "$@";;
    enter) cmd_enter "$@";;
    run) cmd_run "$@";;
    pkg) cmd_pkg "$@";;
    install) cmd_install "$@";;
    sysup) cmd_sysup "$@";;
    mu|m) cmd_mu "$@";;
    u) cmd_umount "$@";;
    *) die "Comando desconhecido: $cmd (use --help)";;
  esac
}

parse_args "$@"
