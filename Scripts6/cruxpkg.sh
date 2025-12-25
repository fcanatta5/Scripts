#!/usr/bin/env bash
# cruxpkg: single-file CRUX-inspired pkgutils + ports helper "pkg" (clean-room).
# Provides: pkgadd, pkgrm, pkginfo, pkgmk, rejmerge, pkg
#
# This file is an extended/rewritten variant of the earlier cruxpkg.sh uploaded in this chat. fileciteturn0file0L1-L20
#
# Highlights:
# - Safer pkgadd/pkgrm behavior (ownership-aware removals).
# - Improved pkgmk with source+binary caches and optional checksums.
# - "pkg" ports frontend: update (git/rsync), sysup, diff, readme, depinst, install, upgrade, depends, depgraph, list, search, info, revdep.

set -euo pipefail
IFS=$'\n\t'

# ----------------------------
# Paths (CRUX-style)
# ----------------------------
PKG_DB_ROOT="${PKG_DB_ROOT:-/var/lib/pkg}"
PKG_DB="${PKG_DB_ROOT}/db"
PKG_REJECTED="${PKG_DB_ROOT}/rejected"
PKGADD_CONF="${PKGADD_CONF:-/etc/pkgadd.conf}"
PKGMK_CONF="${PKGMK_CONF:-/etc/pkgmk.conf}"

# Caches
CACHE_ROOT="${CACHE_ROOT:-/var/cache/cruxpkg}"
SRC_CACHE="${SRC_CACHE:-${CACHE_ROOT}/srcfiles}"       # downloaded sources (shared)
BIN_CACHE="${BIN_CACHE:-${CACHE_ROOT}/packages}"       # built packages (shared)
STATE_DIR="${STATE_DIR:-${CACHE_ROOT}/state}"          # resolver / metadata

# Ports configuration (for "pkg update"):
# - PORTS_METHOD: git|rsync (default git)
# - PORTS_REPO: git URL (for git) or rsync URL (for rsync)
# - PORTS_DIR: local ports checkout dir (default /usr/ports)
# - PORTS_BRANCH: for git (default master)
# - RSYNC_OPTS: extra rsync options
PORTS_METHOD="${PORTS_METHOD:-git}"
PORTS_REPO="${PORTS_REPO:-}"
PORTS_DIR="${PORTS_DIR:-/usr/ports}"
PORTS_BRANCH="${PORTS_BRANCH:-master}"
RSYNC_OPTS="${RSYNC_OPTS:--avz --delete}"

# Optional logs
LOG_ROOT="${LOG_ROOT:-/var/log/pkgutils}"

die()  { echo "$*" >&2; exit 1; }
warn() { echo "warning: $*" >&2; }
info() { echo "$*" >&2; }

need_root() { [[ "${EUID:-$(id -u)}" -eq 0 ]] || die "error: this operation requires root"; }
have() { command -v "$1" >/dev/null 2>&1; }
mktempdir() { mktemp -d "${TMPDIR:-/tmp}/cruxpkg.XXXXXXXX"; }

ensure_dirs() {
  mkdir -p "$PKG_DB" "$PKG_REJECTED" "$SRC_CACHE" "$BIN_CACHE" "$STATE_DIR" "$LOG_ROOT" 2>/dev/null || true
}

# ----------------------------
# Small utils
# ----------------------------
relpath() {
  local p="$1"
  p="${p#./}"
  p="${p#/}"
  echo "$p"
}

parse_pkg_filename() {
  local base name verrel
  base="$(basename "$1")"
  [[ "$base" == *"#"*".pkg.tar."* ]] || die "pkgadd error: invalid package filename: $base"
  name="${base%%#*}"
  verrel="${base#*#}"
  verrel="${verrel%%.pkg.tar.*}"
  echo "$name" "$verrel"
}

pkg_tar_list() { tar -tf "$1" | sed -e 's#^\./##' -e 's#^/##'; }

# ----------------------------
# DB helpers
# ----------------------------
db_version() { [[ -f "$PKG_DB/$1/version" ]] && cat "$PKG_DB/$1/version"; }
db_files()   { [[ -f "$PKG_DB/$1/files" ]] && cat "$PKG_DB/$1/files"; }

db_owner_of() {
  local rel pkg
  rel="$(relpath "$1")"
  [[ -n "$rel" ]] || return 1
  for pkg in "$PKG_DB"/*; do
    [[ -d "$pkg" && -f "$pkg/files" ]] || continue
    if grep -Fxq "$rel" "$pkg/files"; then
      basename "$pkg"
      return 0
    fi
  done
  return 1
}

rm_path_prune() {
  local abs="$1"
  if [[ -L "$abs" || -f "$abs" ]]; then
    rm -f -- "$abs"
  elif [[ -d "$abs" ]]; then
    rmdir --ignore-fail-on-non-empty -- "$abs" 2>/dev/null || true
  fi
}

# pkgadd.conf rules: EVENT <regex> YES|NO. Last matching rule wins.
pkgadd_rule_action() {
  local event="$1" rel="$2"
  local action="YES"
  [[ -f "$PKGADD_CONF" ]] || { echo "$action"; return 0; }

  while IFS= read -r line; do
    line="${line%%#*}"
    [[ "$line" =~ ^[[:space:]]*$ ]] && continue
    [[ "$line" =~ ^[[:space:]]*${event}[[:space:]]+ ]] || continue
    # shellcheck disable=SC2206
    local parts=($line)
    local pat="${parts[1]-}"
    local act="${parts[2]-}"
    [[ -n "$pat" && -n "$act" ]] || continue
    if [[ "$rel" =~ $pat ]]; then
      action="$act"
    fi
  done <"$PKGADD_CONF"

  echo "$action"
}

# ----------------------------
# pkgadd
# ----------------------------
pkgadd_main() {
  ensure_dirs
  need_root

  local force=0 upgrade=0
  local -a pkgs=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -f|--force) force=1; shift;;
      -u|--upgrade) upgrade=1; shift;;
      -h|--help)
        cat <<'EOF'
Usage: pkgadd [options] <packagefile> [packagefile...]
Options:
  -f, --force     overwrite conflicting files (transfer ownership)
  -u, --upgrade   upgrade existing package (preserve files per /etc/pkgadd.conf)
EOF
        exit 0
        ;;
      --) shift; break;;
      -*) die "pkgadd error: unknown option: $1";;
      *) pkgs+=("$1"); shift;;
    esac
  done
  [[ ${#pkgs[@]} -gt 0 ]] || die "pkgadd error: missing package file"

  local pkgfile name verrel
  for pkgfile in "${pkgs[@]}"; do
    [[ -f "$pkgfile" ]] || die "pkgadd error: not found: $pkgfile"
    read -r name verrel < <(parse_pkg_filename "$pkgfile")

    if [[ $upgrade -eq 1 && ! -d "$PKG_DB/$name" ]]; then
      die "pkgadd error: cannot upgrade '$name' because it is not installed"
    fi

    local tmp exdir filelist keepmap
    tmp="$(mktempdir)"
    exdir="$tmp/extract"
    mkdir -p "$exdir"
    filelist="$tmp/pkgfiles"
    keepmap="$tmp/keep_upgrade"
    : >"$keepmap"

    pkg_tar_list "$pkgfile" >"$filelist"

    # Conflicts
    local -a conflicts=()
    while IFS= read -r rel; do
      [[ -n "$rel" ]] || continue
      [[ "$rel" == */ ]] && continue
      local owner=""
      if owner="$(db_owner_of "$rel" 2>/dev/null)"; then
        [[ "$owner" == "$name" ]] || conflicts+=("$rel")
      elif [[ -e "/$rel" ]]; then
        conflicts+=("$rel")
      fi
    done <"$filelist"

    if [[ ${#conflicts[@]} -gt 0 && $force -ne 1 ]]; then
      printf '%s\n' "${conflicts[@]}"
      rm -rf "$tmp"
      die "pkgadd error: listed file(s) already installed (use -f to ignore and overwrite)"
    fi

    # Keep list on upgrade
    if [[ $upgrade -eq 1 ]]; then
      while IFS= read -r rel; do
        [[ -n "$rel" ]] || continue
        [[ "$rel" == */ ]] && continue
        if [[ "$(pkgadd_rule_action "UPGRADE" "$rel")" == "NO" ]] && [[ -e "/$rel" ]]; then
          echo "$rel" >>"$keepmap"
        fi
      done <"$filelist"
      sort -u "$keepmap" -o "$keepmap"
    fi

    # Remove obsolete old files (ownership-aware)
    if [[ $upgrade -eq 1 ]]; then
      local old="$tmp/oldfiles" new="$tmp/newfiles"
      db_files "$name" >"$old" || true
      grep -vE '/$' "$filelist" | sort -u >"$new"
      while IFS= read -r rel; do
        [[ -n "$rel" ]] || continue
        [[ "$rel" == */ ]] && continue
        grep -Fxq "$rel" "$new" && continue
        grep -Fxq "$rel" "$keepmap" && continue
        local owner=""
        if owner="$(db_owner_of "$rel" 2>/dev/null)"; then
          [[ "$owner" == "$name" ]] || continue
        else
          continue
        fi
        rm_path_prune "/$rel"
      done <"$old"
    fi

    tar -xpf "$pkgfile" -C "$exdir"

    # Reject kept files
    if [[ $upgrade -eq 1 && -s "$keepmap" ]]; then
      while IFS= read -r rel; do
        [[ -n "$rel" ]] || continue
        [[ -e "$exdir/$rel" ]] || continue
        local dst="$PKG_REJECTED/$rel"
        mkdir -p "$(dirname "$dst")"
        if [[ -e "$dst" ]]; then
          dst="${dst}.$(date +%s)"
        fi
        mv -f "$exdir/$rel" "$dst"
        info "pkgadd: rejecting $rel, keeping existing version"
      done <"$keepmap"
    fi

    # Install
    ( cd "$exdir" && tar -cpf - . ) | ( cd / && tar -xpf - )

    # Update DB
    mkdir -p "$PKG_DB/$name"
    echo "$verrel" >"$PKG_DB/$name/version"
    if [[ $upgrade -eq 1 && -s "$keepmap" ]]; then
      grep -vxFf "$keepmap" "$filelist" >"$PKG_DB/$name/files"
    else
      cp "$filelist" "$PKG_DB/$name/files"
    fi

    # Footprint (best-effort)
    local fp="$tmp/footprint"
    : >"$fp"
    while IFS= read -r rel; do
      [[ -n "$rel" ]] || continue
      local abs="/${rel%/}"
      [[ -e "$abs" || -L "$abs" ]] || continue
      if have stat && stat -c '%a %U %G' "$abs" >/dev/null 2>&1; then
        printf '%s %s %s %s\n' "$(stat -c '%a' "$abs")" "$(stat -c '%U' "$abs")" "$(stat -c '%G' "$abs")" "$rel" >>"$fp"
      elif have stat; then
        printf '%s %s %s %s\n' "$(stat -f '%Lp' "$abs" 2>/dev/null || echo '?')" "$(stat -f '%Su' "$abs" 2>/dev/null || echo '?')" "$(stat -f '%Sg' "$abs" 2>/dev/null || echo '?')" "$rel" >>"$fp"
      fi
    done <"$PKG_DB/$name/files"
    mv -f "$fp" "$PKG_DB/$name/footprint" 2>/dev/null || true

    info "pkgadd: installed $name $verrel"
    rm -rf "$tmp"
  done
}

# ----------------------------
# pkgrm
# ----------------------------
pkgrm_main() {
  ensure_dirs
  need_root

  if [[ $# -lt 1 || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    cat <<'EOF'
Usage: pkgrm <package>
Removes files currently owned by the package, then deletes its record from /var/lib/pkg/db.
EOF
    exit 0
  fi

  local name="$1"
  [[ -d "$PKG_DB/$name" ]] || die "pkgrm error: package not installed: $name"

  local tmp files
  tmp="$(mktempdir)"
  files="$tmp/files"
  db_files "$name" >"$files"

  grep -vE '/$' "$files" | while IFS= read -r rel; do
    [[ -n "$rel" ]] || continue
    local owner=""
    if owner="$(db_owner_of "$rel" 2>/dev/null)"; then
      [[ "$owner" == "$name" ]] || continue
    else
      continue
    fi
    rm_path_prune "/$rel"
  done

  grep -E '/$' "$files" | sed 's#/$##' | awk '{print length, $0}' | sort -rn | cut -d' ' -f2- | \
    while IFS= read -r rel; do
      [[ -n "$rel" ]] || continue
      rm_path_prune "/$rel"
    done

  rm -rf "$PKG_DB/$name"
  info "pkgrm: removed $name"
  rm -rf "$tmp"
}

# ----------------------------
# pkginfo
# ----------------------------
pkginfo_main() {
  ensure_dirs
  local opt_installed=0 opt_owner=0 opt_list=0 opt_foot=0 opt_version=0
  local owner_pat="" list_arg="" foot_pkg="" ver_pkg=""

  if [[ $# -eq 0 ]]; then
    cat <<'EOF'
Usage: pkginfo [options]
Options:
  -i, --installed            list installed packages and their version
  -o, --owner <pattern>      list owner(s) of file(s) matching pattern
  -l, --list <pkg|pkgfile>   list files owned by package OR contained in a pkg tarball
  -f <package>               show footprint for installed package
  -v <package>               show installed version (only)
EOF
    exit 0
  fi

  while [[ $# -gt 0 ]]; do
    case "$1" in
      -i|--installed) opt_installed=1; shift;;
      -o|--owner) opt_owner=1; owner_pat="${2:-}"; [[ -n "$owner_pat" ]] || die "pkginfo error: -o requires a pattern"; shift 2;;
      -l|--list) opt_list=1; list_arg="${2:-}"; [[ -n "$list_arg" ]] || die "pkginfo error: -l requires an argument"; shift 2;;
      -f) opt_foot=1; foot_pkg="${2:-}"; [[ -n "$foot_pkg" ]] || die "pkginfo error: -f requires a package"; shift 2;;
      -v) opt_version=1; ver_pkg="${2:-}"; [[ -n "$ver_pkg" ]] || die "pkginfo error: -v requires a package"; shift 2;;
      -h|--help) "$0"; exit 0;;
      *) die "pkginfo error: unknown argument: $1";;
    esac
  done

  if [[ $opt_installed -eq 1 ]]; then
    for d in "$PKG_DB"/*; do
      [[ -d "$d" ]] || continue
      local pkg; pkg="$(basename "$d")"
      local ver; ver="$(cat "$d/version" 2>/dev/null || echo "?")"
      echo "$pkg $ver"
    done | sort
  fi

  if [[ $opt_version -eq 1 ]]; then
    [[ -d "$PKG_DB/$ver_pkg" ]] || die "pkginfo error: package not installed: $ver_pkg"
    cat "$PKG_DB/$ver_pkg/version"
  fi

  if [[ $opt_list -eq 1 ]]; then
    if [[ -f "$list_arg" ]]; then
      pkg_tar_list "$list_arg"
    else
      [[ -d "$PKG_DB/$list_arg" ]] || die "pkginfo error: package not installed: $list_arg"
      db_files "$list_arg"
    fi
  fi

  if [[ $opt_foot -eq 1 ]]; then
    [[ -d "$PKG_DB/$foot_pkg" ]] || die "pkginfo error: package not installed: $foot_pkg"
    [[ -f "$PKG_DB/$foot_pkg/footprint" ]] || die "pkginfo error: footprint not available for $foot_pkg"
    cat "$PKG_DB/$foot_pkg/footprint"
  fi

  if [[ $opt_owner -eq 1 ]]; then
    local pat="$owner_pat"
    if [[ "$pat" == /* || "$pat" == .* || "$pat" == *"/"* ]]; then
      local rel; rel="$(relpath "$pat")"
      local owner=""
      if owner="$(db_owner_of "$rel" 2>/dev/null)"; then
        echo "$owner $rel"
      fi
    else
      for d in "$PKG_DB"/*; do
        [[ -d "$d" && -f "$d/files" ]] || continue
        local pkg; pkg="$(basename "$d")"
        grep -F "$pat" "$d/files" | while IFS= read -r rel; do
          echo "$pkg $rel"
        done
      done
    fi
  fi
}

# ----------------------------
# pkgmk (cache + checksums)
# ----------------------------
_pkgmk_fetch_one() {
  local url="$1" dst="$2"
  [[ -f "$dst" ]] && return 0
  if [[ "$url" =~ ^https?:// ]]; then
    if have curl; then
      curl -L --fail --retry 3 --retry-delay 2 -o "$dst" "$url"
    elif have wget; then
      wget --tries=3 --timeout=30 -O "$dst" "$url"
    else
      die "pkgmk error: need curl or wget to download sources"
    fi
  else
    [[ -f "$url" ]] || die "pkgmk error: source not found: $url"
    cp -f "$url" "$dst"
  fi
}

_pkgmk_verify_sums() {
  local srcfiles=("$@")
  if declare -p sha256sums >/dev/null 2>&1; then
    have sha256sum || die "pkgmk error: sha256sums defined but sha256sum not found"
    local i=0
    for f in "${srcfiles[@]}"; do
      local got; got="$(sha256sum "$f" | awk '{print $1}')"
      [[ "${sha256sums[$i]-}" == "$got" ]] || die "pkgmk error: sha256 mismatch for $(basename "$f")"
      i=$((i+1))
    done
  elif declare -p md5sums >/dev/null 2>&1; then
    have md5sum || die "pkgmk error: md5sums defined but md5sum not found"
    local i=0
    for f in "${srcfiles[@]}"; do
      local got; got="$(md5sum "$f" | awk '{print $1}')"
      [[ "${md5sums[$i]-}" == "$got" ]] || die "pkgmk error: md5 mismatch for $(basename "$f")"
      i=$((i+1))
    done
  fi
}

pkgmk_main() {
  ensure_dirs
  local do_install=0 do_download=0 update_fp=0 ignore_fp=0 clean=0 keep_work=0
  local jobs="${JOBS:-}"
  local use_bin_cache=1

  while [[ $# -gt 0 ]]; do
    case "$1" in
      -i) do_install=1; shift;;
      -d) do_download=1; shift;;
      -uf) update_fp=1; shift;;
      -if) ignore_fp=1; shift;;
      -c) clean=1; shift;;
      -k) keep_work=1; shift;;
      -j) jobs="${2:-}"; [[ -n "$jobs" ]] || die "pkgmk error: -j requires N"; shift 2;;
      --no-bincache) use_bin_cache=0; shift;;
      -h|--help)
        cat <<'EOF'
Usage: pkgmk [options]
Options:
  -d            download sources into cache
  -i            install built package via pkgadd (requires root)
  -uf           update .footprint
  -if           ignore footprint mismatch
  -c            clean work directory after build
  -k            keep work directory (for debugging)
  -j N          set MAKEFLAGS=-jN
  --no-bincache disable binary cache usage
EOF
        exit 0
        ;;
      *) die "pkgmk error: unknown option: $1";;
    esac
  done

  [[ -f "Pkgfile" ]] || die "pkgmk error: Pkgfile not found in current directory"

  if [[ -f "$PKGMK_CONF" ]]; then
    # shellcheck disable=SC1090
    source "$PKGMK_CONF"
  fi

  : "${PKGMK_SOURCE_DIR:=$SRC_CACHE}"
  : "${PKGMK_WORK_DIR:=/tmp/work}"
  : "${PKGMK_PACKAGE_DIR:=$BIN_CACHE}"
  : "${PKGMK_COMPRESSION_MODE:=gz}"

  mkdir -p "$PKGMK_SOURCE_DIR" "$PKGMK_WORK_DIR" "$PKGMK_PACKAGE_DIR" 2>/dev/null || true

  # shellcheck disable=SC1091
  source "./Pkgfile"
  [[ -n "${name:-}" && -n "${version:-}" && -n "${release:-}" ]] || die "pkgmk error: Pkgfile must define name, version, release"
  [[ "$(declare -F build || true)" == "build" ]] || die "pkgmk error: Pkgfile must define build()"

  [[ -z "$jobs" ]] || export MAKEFLAGS="-j${jobs}"

  local ext
  case "$PKGMK_COMPRESSION_MODE" in
    gz)  ext="gz";;
    bz2) ext="bz2";;
    xz)  ext="xz";;
    zst|zstd) ext="zst";;
    lz|lzip) ext="lz";;
    *) warn "unknown PKGMK_COMPRESSION_MODE=$PKGMK_COMPRESSION_MODE, defaulting to gz"; ext="gz";;
  esac

  local pkgfn="${name}#${version}-${release}.pkg.tar.${ext}"
  local outpkg="${PKGMK_PACKAGE_DIR%/}/${pkgfn}"

  if [[ $use_bin_cache -eq 1 && "${FORCE_REBUILD:-0}" -ne 1 && -f "$outpkg" ]]; then
    info "pkgmk: using cached package $outpkg"
  else
    local work SRC PKG
    work="$(mktemp -d "${PKGMK_WORK_DIR%/}/${name}.XXXXXXXX")"
    SRC="${work}/src"
    PKG="${work}/pkg"
    mkdir -p "$SRC" "$PKG"

    local -a fetched=()
    if [[ ${#source[@]:-0} -gt 0 ]]; then
      local s base dst
      for s in "${source[@]}"; do
        base="$(basename "$s")"
        dst="${PKGMK_SOURCE_DIR%/}/${base}"
        if [[ $do_download -eq 1 || ! -f "$dst" ]]; then
          _pkgmk_fetch_one "$s" "$dst"
        fi
        fetched+=("$dst")
      done
      _pkgmk_verify_sums "${fetched[@]}"
    else
      [[ $do_download -eq 0 ]] || warn "pkgmk: -d specified but Pkgfile has no source[]"
    fi

    local f
    for f in "${fetched[@]}"; do
      case "$f" in
        *.tar.gz|*.tgz) tar -xzf "$f" -C "$SRC" ;;
        *.tar.bz2|*.tbz2) tar -xjf "$f" -C "$SRC" ;;
        *.tar.xz|*.txz) tar -xJf "$f" -C "$SRC" ;;
        *.tar.zst) tar --zstd -xf "$f" -C "$SRC" 2>/dev/null || true ;;
        *.zip) have unzip && unzip -q "$f" -d "$SRC" || true ;;
        *) : ;;
      esac
    done

    export SRC PKG
    ( set -e; cd "$work"; build )

    local fp_new="${work}/.footprint.new"
    ( cd "$PKG" && find . -mindepth 1 -print0 | LC_ALL=C sort -z | xargs -0 -I{} echo "{}" ) \
      | sed -e 's#^\./##' -e '/^$/d' >"$fp_new"

    if [[ ! -f ".footprint" || $update_fp -eq 1 ]]; then
      cp -f "$fp_new" ".footprint"
      info "pkgmk: wrote .footprint"
    else
      if ! diff -u ".footprint" "$fp_new" >/dev/null 2>&1; then
        if [[ $ignore_fp -eq 1 ]]; then
          warn "pkgmk: footprint mismatch ignored (-if)"
        else
          diff -u ".footprint" "$fp_new" || true
          die "pkgmk error: footprint mismatch (use -if or -uf)"
        fi
      fi
    fi

    local tmpout="${outpkg}.tmp.$$"
    rm -f "$tmpout"
    case "$ext" in
      gz)  ( cd "$PKG" && tar -czf "$tmpout" . ) ;;
      bz2) ( cd "$PKG" && tar -cjf "$tmpout" . ) ;;
      xz)  ( cd "$PKG" && tar -cJf "$tmpout" . ) ;;
      zst)
        if tar --help 2>/dev/null | grep -q -- '--zstd'; then
          ( cd "$PKG" && tar --zstd -cf "$tmpout" . )
        elif have zstd; then
          ( cd "$PKG" && tar -cf - . | zstd -T0 -19 -o "$tmpout" )
        else
          die "pkgmk error: zstd requested but tar --zstd or zstd not available"
        fi
        ;;
      lz)
        have lzip || die "pkgmk error: lzip requested but lzip not available"
        ( cd "$PKG" && tar -cf - . | lzip -9 >"$tmpout" )
        ;;
    esac
    mv -f "$tmpout" "$outpkg"
    info "pkgmk: built $outpkg"

    if [[ $clean -eq 1 && $keep_work -ne 1 ]]; then
      rm -rf "$work"
    else
      info "pkgmk: workdir kept at $work"
    fi
  fi

  if [[ $do_install -eq 1 ]]; then
    need_root
    if [[ -d "$PKG_DB/$name" ]]; then
      pkgadd_main -u "$outpkg"
    else
      pkgadd_main "$outpkg"
    fi
  fi
}

# ----------------------------
# rejmerge
# ----------------------------
rejmerge_main() {
  ensure_dirs
  need_root

  [[ "${1:-}" != "-h" && "${1:-}" != "--help" ]] || {
    cat <<'EOF'
Usage: rejmerge
Scans /var/lib/pkg/rejected and offers:
  (k) keep installed (delete rejected)
  (u) use rejected (replace installed with rejected)
  (m) merge (opens $MERGETOOL or falls back to vimdiff)
EOF
    exit 0
  }

  [[ -d "$PKG_REJECTED" ]] || exit 0

  local mergetool="${MERGETOOL:-}"
  if [[ -z "$mergetool" ]]; then
    if have vimdiff; then mergetool="vimdiff"
    elif have nvim; then mergetool="nvim -d"
    elif have diff3; then mergetool="diff3 -m"
    else mergetool=""
    fi
  fi

  local rej
  mapfile -t rej < <(cd "$PKG_REJECTED" && find . -type f -print | sed 's#^\./##' | LC_ALL=C sort)
  [[ ${#rej[@]} -gt 0 ]] || { info "rejmerge: no rejected files"; exit 0; }

  for rel in "${rej[@]}"; do
    local inst="/$rel"
    local rejf="$PKG_REJECTED/$rel"

    echo "==== $rel ===="
    [[ -e "$inst" ]] && diff -u -- "$inst" "$rejf" || info "(installed missing; rejected exists)"

    while true; do
      printf "Action: [k]eep, [u]se rejected, [m]erge, [s]kip, [q]uit: " >&2
      read -r ans
      case "$ans" in
        k|K)
          rm -f -- "$rejf"
          rmdir --ignore-fail-on-non-empty -- "$(dirname "$rejf")" 2>/dev/null || true
          info "rejmerge: removed rejected $rel"
          break
          ;;
        u|U)
          mkdir -p "$(dirname "$inst")"
          cp -a -- "$rejf" "$inst"
          rm -f -- "$rejf"
          rmdir --ignore-fail-on-non-empty -- "$(dirname "$rejf")" 2>/dev/null || true
          info "rejmerge: replaced installed with rejected $rel"
          break
          ;;
        m|M)
          [[ -e "$inst" ]] || { warn "installed missing; use (u)"; break; }
          if [[ -n "$mergetool" ]]; then
            # shellcheck disable=SC2086
            $mergetool "$inst" "$rejf" || true
            printf "Remove rejected after merge? [y/N]: " >&2
            read -r yn
            if [[ "$yn" =~ ^[Yy]$ ]]; then
              rm -f -- "$rejf"
              rmdir --ignore-fail-on-non-empty -- "$(dirname "$rejf")" 2>/dev/null || true
            fi
          else
            warn "no merge tool found (set MERGETOOL=... or install vimdiff/diff3)"
          fi
          break
          ;;
        s|S) break;;
        q|Q) exit 0;;
        *) echo "invalid choice" >&2;;
      esac
    done
  done
}

# ----------------------------
# Ports helpers (pkg frontend)
# ----------------------------
ports_roots() {
  # Prefer PORTS_DIR; also scan /etc/ports for additional trees.
  local -a roots=()
  [[ -d "$PORTS_DIR" ]] && roots+=("$PORTS_DIR")

  if [[ -f /etc/ports ]]; then
    while IFS= read -r line; do
      line="${line%%#*}"
      [[ "$line" =~ ^[[:space:]]*$ ]] && continue
      local tok
      for tok in $line; do
        if [[ "$tok" == /* && -d "$tok" ]]; then
          roots+=("$tok")
        fi
      done
    done </etc/ports
  fi

  printf '%s\n' "${roots[@]}" | awk 'NF' | sort -u
}

# Cache an index for faster lookups
ports_index_build() {
  local idx="${STATE_DIR}/ports.index"
  : >"$idx"
  while IFS= read -r root; do
    [[ -d "$root" ]] || continue
    find "$root" -mindepth 2 -maxdepth 2 -type f -name Pkgfile -printf '%h\n' 2>/dev/null \
      | while IFS= read -r d; do
          echo "$(basename "$d")|$d"
        done
  done < <(ports_roots)
  sort -u -o "$idx" "$idx"
}

port_dir_of() {
  local pname="$1"
  local idx="${STATE_DIR}/ports.index"
  if [[ ! -f "$idx" || "${REINDEX_PORTS:-0}" -eq 1 ]]; then
    ports_index_build
  fi
  local line
  line="$(grep -m1 -F "${pname}|" "$idx" 2>/dev/null || true)"
  [[ -n "$line" ]] || return 1
  echo "${line#*|}"
}

port_meta() {
  local dir="$1"
  [[ -f "$dir/Pkgfile" ]] || return 1
  ( set -e
    cd "$dir"
    # shellcheck disable=SC1091
    source "./Pkgfile"
    echo "name=${name:-}"
    echo "version=${version:-}"
    echo "release=${release:-}"
    if declare -p depends >/dev/null 2>&1; then declare -p depends; else echo "declare -a depends=()"; fi
    if declare -p source >/dev/null 2>&1; then declare -p source; else echo "declare -a source=()"; fi
  )
}

# DFS topo sort with cycle detection
resolve_deps() {
  local target="$1"
  local tmp="$STATE_DIR/resolve.$$"
  mkdir -p "$tmp"
  : >"$tmp/order"; : >"$tmp/vis"; : >"$tmp/stack"

  _dfs() {
    local p="$1"
    grep -Fxq "$p" "$tmp/vis" 2>/dev/null && return 0
    if grep -Fxq "$p" "$tmp/stack" 2>/dev/null; then
      echo "$p" >&2
      die "pkg error: dependency cycle detected (see above)"
    fi
    echo "$p" >>"$tmp/stack"

    local dir; dir="$(port_dir_of "$p" 2>/dev/null || true)"
    [[ -n "$dir" ]] || die "pkg error: port not found: $p"
    local meta; meta="$(port_meta "$dir")"
    local deps_decl; deps_decl="$(printf '%s\n' "$meta" | grep -E '^declare -a depends=' || true)"
    # shellcheck disable=SC1090
    eval "$deps_decl"

    local dep
    for dep in "${depends[@]}"; do
      dep="${dep%%[<>=]*}"
      [[ -n "$dep" ]] || continue
      _dfs "$dep"
    done

    grep -Fxv "$p" "$tmp/stack" >"$tmp/stack.new" || true
    mv -f "$tmp/stack.new" "$tmp/stack"

    echo "$p" >>"$tmp/vis"
    echo "$p" >>"$tmp/order"
  }

  _dfs "$target"
  cat "$tmp/order"
  rm -rf "$tmp"
}

is_installed() { [[ -d "$PKG_DB/$1" ]]; }

build_port() {
  local p="$1"
  local dir; dir="$(port_dir_of "$p")"
  ( cd "$dir" && PKGMK_PACKAGE_DIR="$BIN_CACHE" PKGMK_SOURCE_DIR="$SRC_CACHE" pkgmk_main -d )
  local meta; meta="$(port_meta "$dir")"
  local name version release
  name="$(printf '%s\n' "$meta" | sed -n 's/^name=//p')"
  version="$(printf '%s\n' "$meta" | sed -n 's/^version=//p')"
  release="$(printf '%s\n' "$meta" | sed -n 's/^release=//p')"
  local pkg
  pkg="$(ls -1 "$BIN_CACHE"/"${name}"#*.pkg.tar.* 2>/dev/null | grep -F "#${version}-${release}.pkg.tar" | head -n 1 || true)"
  [[ -n "$pkg" && -f "$pkg" ]] || pkg="$(ls -1 "$BIN_CACHE"/"${name}"#*.pkg.tar.* 2>/dev/null | sort | tail -n 1 || true)"
  [[ -n "$pkg" && -f "$pkg" ]] || die "pkg error: built package not found for $p"
  echo "$pkg"
}

install_pkg_file() {
  local pkgfile="$1" pkgname="$2"
  if is_installed "$pkgname"; then
    pkgadd_main -u "$pkgfile"
  else
    pkgadd_main "$pkgfile"
  fi
}

# ----------------------------
# update (git/rsync)
# ----------------------------
pkg_sync() {
  ensure_dirs
  need_root

  mkdir -p "$PORTS_DIR" 2>/dev/null || true

  case "$PORTS_METHOD" in
    git)
      have git || die "pkg sync error: git not found"
      [[ -n "$PORTS_REPO" ]] || die "pkg sync error: PORTS_REPO is required for git (e.g., https://... or git@...)"
      if [[ -d "$PORTS_DIR/.git" ]]; then
        info "pkg: syncing ports (git pull) in $PORTS_DIR"
        ( cd "$PORTS_DIR" && git fetch --all --prune && git checkout "$PORTS_BRANCH" && git pull --ff-only )
      else
        info "pkg: cloning ports repo to $PORTS_DIR"
        rm -rf "$PORTS_DIR"
        git clone --branch "$PORTS_BRANCH" --depth 1 "$PORTS_REPO" "$PORTS_DIR"
      fi
      ;;
    rsync)
      have rsync || die "pkg sync error: rsync not found"
      [[ -n "$PORTS_REPO" ]] || die "pkg sync error: PORTS_REPO is required for rsync (e.g., rsync://host/...)"
      info "pkg: syncing ports (rsync) into $PORTS_DIR"
      # shellcheck disable=SC2086
      rsync $RSYNC_OPTS "$PORTS_REPO" "$PORTS_DIR"
      ;;
    *)
      die "pkg sync error: unknown PORTS_METHOD=$PORTS_METHOD (use git or rsync)"
      ;;
  esac

  # Reindex
  ports_index_build
  info "pkg: ports index updated"
}

# ----------------------------
# prt-get compatible operations (implemented under "pkg")
# ----------------------------

LOCK_FILE="${LOCK_FILE:-${STATE_DIR}/locked.list}"
PKG_CONF="${PKG_CONF:-/etc/pkg.conf}"
PRTGET_CONF_COMPAT="${PRTGET_CONF_COMPAT:-/etc/prt-get.conf}"

locked_has() { [[ -f "$LOCK_FILE" ]] && grep -Fxq "$1" "$LOCK_FILE"; }
locked_add() { mkdir -p "$(dirname "$LOCK_FILE")"; ( [[ -f "$LOCK_FILE" ]] && cat "$LOCK_FILE"; echo "$1" ) | awk 'NF' | sort -u >"${LOCK_FILE}.tmp"; mv -f "${LOCK_FILE}.tmp" "$LOCK_FILE"; }
locked_del() { [[ -f "$LOCK_FILE" ]] || return 0; grep -Fxv "$1" "$LOCK_FILE" >"${LOCK_FILE}.tmp" || true; mv -f "${LOCK_FILE}.tmp" "$LOCK_FILE"; }
locked_list() { [[ -f "$LOCK_FILE" ]] && cat "$LOCK_FILE" || true; }

# Read prtdir entries from /etc/pkg.conf and /etc/prt-get.conf (compat).
# Supports: "prtdir /path/to/tree" and ignores per-tree allowlists for now.
ports_roots_from_conf() {
  local f line
  for f in "$PKG_CONF" "$PRTGET_CONF_COMPAT"; do
    [[ -f "$f" ]] || continue
    while IFS= read -r line; do
      line="${line%%#*}"
      [[ "$line" =~ ^[[:space:]]*$ ]] && continue
      if [[ "$line" =~ ^[[:space:]]*prtdir[[:space:]]+ ]]; then
        line="${line#*prtdir}"
        line="${line#"${line%%[![:space:]]*}"}"
        # handle /path:allow1,allow2
        line="${line%%:*}"
        [[ "$line" == /* && -d "$line" ]] && echo "$line"
      fi
    done <"$f"
  done
}

# Override ports_roots() to include config roots with precedence.
ports_roots() {
  local -a roots=()
  [[ -d "$PORTS_DIR" ]] && roots+=("$PORTS_DIR")
  while IFS= read -r r; do roots+=("$r"); done < <(ports_roots_from_conf)

  if [[ -f /etc/ports ]]; then
    while IFS= read -r line; do
      line="${line%%#*}"
      [[ "$line" =~ ^[[:space:]]*$ ]] && continue
      local tok
      for tok in $line; do
        if [[ "$tok" == /* && -d "$tok" ]]; then
          roots+=("$tok")
        fi
      done
    done </etc/ports
  fi

  printf '%s\n' "${roots[@]}" | awk 'NF' | sort -u
}

# Parse prt-get style options: --margs=..., --aargs=..., -fr, -um, -uf, -f, --test, --log, --cache, --pre-install, --post-install, --install-scripts
parse_prt_opts() {
  PRT_MARGS=""
  PRT_AARGS=""
  PRT_TEST=0
  PRT_LOG=0
  PRT_CACHE=0
  PRT_RUN_PRE=0
  PRT_RUN_POST=0

  local -a rest=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --margs=*) PRT_MARGS="${1#*=}"; shift;;
      --aargs=*) PRT_AARGS="${1#*=}"; shift;;
      -fr) PRT_MARGS="${PRT_MARGS} -f"; shift;;
      -um) PRT_MARGS="${PRT_MARGS} -um"; shift;;
      -uf) PRT_MARGS="${PRT_MARGS} -uf"; shift;;
      -f)  PRT_AARGS="${PRT_AARGS} -f"; shift;;
      --test) PRT_TEST=1; shift;;
      --log)  PRT_LOG=1; shift;;
      --cache) PRT_CACHE=1; shift;;
      --pre-install) PRT_RUN_PRE=1; shift;;
      --post-install) PRT_RUN_POST=1; shift;;
      --install-scripts) PRT_RUN_PRE=1; PRT_RUN_POST=1; shift;;
      --) shift; break;;
      *) rest+=("$1"); shift;;
    esac
  done
  # shellcheck disable=SC2145
  echo "${rest[@]}"
}

run_install_script() {
  local when="$1" dir="$2" port="$3"
  local s=""
  case "$when" in
    pre)  for s in "$dir/pre-install" "$dir/pre-install.sh" "$dir/preinstall" "$dir/preinstall.sh"; do [[ -x "$s" ]] && break; s=""; done ;;
    post) for s in "$dir/post-install" "$dir/post-install.sh" "$dir/postinstall" "$dir/postinstall.sh"; do [[ -x "$s" ]] && break; s=""; done ;;
  esac
  [[ -n "$s" ]] || return 0
  info "pkg: running ${when}-install script for $port"
  ( cd "$dir" && "$s" ) || warn "pkg: ${when}-install script failed for $port (continuing)"
}

build_port_with_args() {
  local p="$1" margs="$2"
  local dir; dir="$(port_dir_of "$p")"
  ( cd "$dir" && PKGMK_PACKAGE_DIR="$BIN_CACHE" PKGMK_SOURCE_DIR="$SRC_CACHE" pkgmk_main -d $margs )
  local meta; meta="$(port_meta "$dir")"
  local name version release
  name="$(printf '%s\n' "$meta" | sed -n 's/^name=//p')"
  version="$(printf '%s\n' "$meta" | sed -n 's/^version=//p')"
  release="$(printf '%s\n' "$meta" | sed -n 's/^release=//p')"
  local pkg
  pkg="$(ls -1 "$BIN_CACHE"/"${name}"#*.pkg.tar.* 2>/dev/null | grep -F "#${version}-${release}.pkg.tar" | head -n 1 || true)"
  [[ -n "$pkg" && -f "$pkg" ]] || pkg="$(ls -1 "$BIN_CACHE"/"${name}"#*.pkg.tar.* 2>/dev/null | sort | tail -n 1 || true)"
  [[ -n "$pkg" && -f "$pkg" ]] || die "pkg error: built package not found for $p"
  echo "$pkg"
}

install_one_port() {
  local p="$1" mode="$2" margs="$3" aargs="$4" run_pre="$5" run_post="$6"
  local dir; dir="$(port_dir_of "$p")"
  [[ "$run_pre" -eq 1 ]] && run_install_script pre "$dir" "$p"
  local pkgfile; pkgfile="$(build_port_with_args "$p" "$margs")"

  if [[ "$mode" == "install" ]]; then
    if is_installed "$p"; then
      info "pkg: $p already installed (skipping)"
    else
      [[ "$PRT_TEST" -eq 1 ]] && { echo "TEST: would pkgadd $aargs $pkgfile"; return 0; }
      # shellcheck disable=SC2086
      pkgadd_main $aargs "$pkgfile"
    fi
  else # update/upgrade
    if is_installed "$p"; then
      [[ "$PRT_TEST" -eq 1 ]] && { echo "TEST: would pkgadd -u $aargs $pkgfile"; return 0; }
      # shellcheck disable=SC2086
      pkgadd_main -u $aargs "$pkgfile"
    else
      [[ "$PRT_TEST" -eq 1 ]] && { echo "TEST: would pkgadd $aargs $pkgfile"; return 0; }
      # shellcheck disable=SC2086
      pkgadd_main $aargs "$pkgfile"
    fi
  fi

  [[ "$run_post" -eq 1 ]] && run_install_script post "$dir" "$p"
}

pkg_install() { # prt-get install: install without dep resolution
  ensure_dirs; need_root; ports_index_build
  local args; args="$(parse_prt_opts "$@")"
  # shellcheck disable=SC2206
  local -a pkgs=($args)
  [[ ${#pkgs[@]} -gt 0 ]] || die "pkg install: missing package name(s)"
  local p
  for p in "${pkgs[@]}"; do
    if [[ "$PRT_TEST" -eq 0 ]]; then
      info "pkg: installing $p"
    fi
    install_one_port "$p" install "$PRT_MARGS" "$PRT_AARGS" "$PRT_RUN_PRE" "$PRT_RUN_POST"
  done
}

pkg_update_pkgs() { # prt-get update: rebuild+upgrade given packages
  ensure_dirs; need_root; ports_index_build
  local args; args="$(parse_prt_opts "$@")"
  # shellcheck disable=SC2206
  local -a pkgs=($args)
  [[ ${#pkgs[@]} -gt 0 ]] || die "pkg update: missing package name(s)"
  local p
  for p in "${pkgs[@]}"; do
    install_one_port "$p" update "$PRT_MARGS" "$PRT_AARGS" "$PRT_RUN_PRE" "$PRT_RUN_POST"
  done
}

pkg_grpinst() { # stop on first failure
  ensure_dirs; need_root; ports_index_build
  local args; args="$(parse_prt_opts "$@")"
  # shellcheck disable=SC2206
  local -a pkgs=($args)
  [[ ${#pkgs[@]} -gt 0 ]] || die "pkg grpinst: missing package name(s)"
  local p
  for p in "${pkgs[@]}"; do
    install_one_port "$p" install "$PRT_MARGS" "$PRT_AARGS" "$PRT_RUN_PRE" "$PRT_RUN_POST"
  done
}

pkg_depinst_full() { # prt-get depinst: deps + grpinst semantics
  ensure_dirs; need_root; ports_index_build
  local args; args="$(parse_prt_opts "$@")"
  # shellcheck disable=SC2206
  local -a targets=($args)
  [[ ${#targets[@]} -gt 0 ]] || die "pkg depinst: missing package name(s)"

  local tmp="$STATE_DIR/depinst.$$"
  mkdir -p "$tmp"; : >"$tmp/order"
  local t
  for t in "${targets[@]}"; do
    resolve_deps "$t" >>"$tmp/order"
  done
  awk 'NF{a[$0]++; if(a[$0]==1) print $0}' "$tmp/order" >"$tmp/order.uniq"

  local p
  while IFS= read -r p; do
    [[ -n "$p" ]] || continue
    # depinst installs missing deps and targets; skip already installed unless target explicitly passed? prt-get depinst will update? We'll only install if not installed.
    if is_installed "$p"; then
      continue
    fi
    install_one_port "$p" install "$PRT_MARGS" "$PRT_AARGS" "$PRT_RUN_PRE" "$PRT_RUN_POST"
  done <"$tmp/order.uniq"

  rm -rf "$tmp"
}

pkg_remove_multi() {
  ensure_dirs; need_root
  [[ $# -gt 0 ]] || die "pkg remove: missing package name(s)"
  local p
  for p in "$@"; do
    pkgrm_main "$p"
  done
}

pkg_isinst() {
  local p="${1:-}"; [[ -n "$p" ]] || die "pkg isinst: requires a package name"
  if is_installed "$p"; then
    echo "yes"
    return 0
  fi
  echo "no"
  return 1
}

pkg_current() {
  local p="${1:-}"; [[ -n "$p" ]] || die "pkg current: requires a package name"
  is_installed "$p" || die "pkg current: not installed: $p"
  db_version "$p"
}

pkg_listinst() {
  ensure_dirs
  local v=0 vv=0 filter="*"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -v) v=1; shift;;
      -vv) vv=1; shift;;
      *) filter="$1"; shift;;
    esac
  done
  local line
  while IFS= read -r line; do
    local p ver
    p="${line%% *}"; ver="${line#* }"
    [[ "$p" == $filter ]] || continue
    if [[ $vv -eq 1 || $v -eq 1 ]]; then
      echo "$p $ver"
    else
      echo "$p"
    fi
  done < <(pkginfo_main -i)
}

pkg_listorphans() {
  ensure_dirs
  ports_index_build
  local v=0 vv=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -v) v=1; shift;;
      -vv) vv=1; shift;;
      *) shift;;
    esac
  done

  # Build reverse dependency map for installed ports that have dependency data.
  local tmp="$STATE_DIR/orphans.$$"; mkdir -p "$tmp"
  : >"$tmp/rev"

  local p dir meta deps_decl dep
  while IFS='|' read -r p dir; do
    is_installed "$p" || continue
    meta="$(port_meta "$dir")"
    deps_decl="$(printf '%s\\n' "$meta" | grep -E '^declare -a depends=' || true)"
    # shellcheck disable=SC1090
    eval "$deps_decl"
    for dep in "${depends[@]}"; do
      dep="${dep%%[<>=]*}"
      [[ -n "$dep" ]] || continue
      echo "$dep|$p" >>"$tmp/rev"
    done
  done <"${STATE_DIR}/ports.index"

  sort -u "$tmp/rev" >"$tmp/rev.s"

  # Orphan = installed package with no installed dependents (best-effort; ignores deps not declared in ports).
  while IFS= read -r line; do
    local pkg="${line%% *}"
    [[ -n "$pkg" ]] || continue
    grep -Fq "^$pkg|" "$tmp/rev.s" && continue
    if [[ $vv -eq 1 || $v -eq 1 ]]; then
      echo "$pkg $(db_version "$pkg" || echo '-')"
    else
      echo "$pkg"
    fi
  done < <(pkginfo_main -i)

  rm -rf "$tmp"
}

port_desc() {
  local dir="$1"
  local desc=""
  if grep -q '^description=' "$dir/Pkgfile" 2>/dev/null; then
    desc="$(awk -F= '/^description=/{print substr($0, index($0,"=")+1)}' "$dir/Pkgfile" | head -n1 | sed 's/^\"//;s/\"$//')"
  fi
  echo "$desc"
}

pkg_list() {
  ensure_dirs
  ports_index_build
  local v=0 vv=0 with_path=0 filter="*"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -v) v=1; shift;;
      -vv) vv=1; shift;;
      --path) with_path=1; shift;;
      *) filter="$1"; shift;;
    esac
  done
  local p dir meta verrel
  while IFS='|' read -r p dir; do
    [[ -n "$p" ]] || continue
    [[ "$p" == $filter ]] || continue
    if [[ $vv -eq 1 || $v -eq 1 ]]; then
      meta="$(port_meta "$dir")"
      verrel="$(printf '%s\n' "$meta" | awk -F= '/^version=/{v=$2} /^release=/{r=$2} END{print v "-" r}')"
    fi
    if [[ $vv -eq 1 ]]; then
      local d; d="$(port_desc "$dir")"
      if [[ $with_path -eq 1 ]]; then
        echo "$dir $p $verrel ${d}"
      else
        echo "$p $verrel ${d}"
      fi
    elif [[ $v -eq 1 ]]; then
      if [[ $with_path -eq 1 ]]; then
        echo "$dir $p $verrel"
      else
        echo "$p $verrel"
      fi
    else
      if [[ $with_path -eq 1 ]]; then
        echo "$dir $p"
      else
        echo "$p"
      fi
    fi
  done <"${STATE_DIR}/ports.index"
}

pkg_search() {
  ensure_dirs
  ports_index_build
  local v=0 vv=0 with_path=0 regex=0
  local expr="${1:-}"; [[ -n "$expr" ]] || die "pkg search: requires an expression"
  shift || true
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -v) v=1; shift;;
      -vv) vv=1; shift;;
      --path) with_path=1; shift;;
      --regex) regex=1; shift;;
      *) break;;
    esac
  done

  local p dir
  while IFS='|' read -r p dir; do
    [[ -n "$p" ]] || continue
    if [[ $regex -eq 1 ]]; then
      echo "$p" | grep -Eq "$expr" || continue
    else
      echo "$p" | grep -qi -- "$expr" || continue
    fi
    if [[ $vv -eq 1 || $v -eq 1 ]]; then
      pkg_list ${vv:+-vv} ${v:+-v} ${with_path:+--path} "$p"
    else
      [[ $with_path -eq 1 ]] && echo "$dir $p" || echo "$p"
    fi
  done <"${STATE_DIR}/ports.index"
}

pkg_dsearch() {
  ensure_dirs
  ports_index_build
  local v=0 vv=0 with_path=0 regex=0
  local expr="${1:-}"; [[ -n "$expr" ]] || die "pkg dsearch: requires an expression"
  shift || true
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -v) v=1; shift;;
      -vv) vv=1; shift;;
      --path) with_path=1; shift;;
      --regex) regex=1; shift;;
      *) break;;
    esac
  done

  local p dir desc hay
  while IFS='|' read -r p dir; do
    [[ -n "$p" ]] || continue
    desc="$(port_desc "$dir")"
    hay="$p $desc"
    if [[ $regex -eq 1 ]]; then
      echo "$hay" | grep -Eq "$expr" || continue
    else
      echo "$hay" | grep -qi -- "$expr" || continue
    fi
    if [[ $vv -eq 1 || $v -eq 1 ]]; then
      pkg_list ${vv:+-vv} ${v:+-v} ${with_path:+--path} "$p"
    else
      [[ $with_path -eq 1 ]] && echo "$dir $p" || echo "$p"
    fi
  done <"${STATE_DIR}/ports.index"
}

pkg_fsearch() {
  ensure_dirs
  ports_index_build
  local full=0 regex=0 pattern="${1:-}"
  [[ -n "$pattern" ]] || die "pkg fsearch: requires a pattern"
  shift || true
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --full) full=1; shift;;
      --regex) regex=1; shift;;
      *) break;;
    esac
  done

  local p dir fp
  while IFS='|' read -r p dir; do
    fp="$dir/.footprint"
    [[ -f "$fp" ]] || continue
    if [[ $full -eq 1 ]]; then
      if [[ $regex -eq 1 ]]; then
        grep -Eq "$pattern" "$fp" && echo "$p"
      else
        grep -Fq -- "$pattern" "$fp" && echo "$p"
      fi
    else
      if [[ $regex -eq 1 ]]; then
        sed 's#.*/##' "$fp" | grep -Eq "$pattern" && echo "$p"
      else
        sed 's#.*/##' "$fp" | grep -Fq -- "$pattern" && echo "$p"
      fi
    fi
  done <"${STATE_DIR}/ports.index"
}

pkg_info() {
  ensure_dirs
  local p="${1:-}"; [[ -n "$p" ]] || die "pkg info: requires a port name"
  ports_index_build
  local dir; dir="$(port_dir_of "$p")"
  local meta; meta="$(port_meta "$dir")"
  local name version release desc url deps
  name="$(printf '%s\n' "$meta" | sed -n 's/^name=//p')"
  version="$(printf '%s\n' "$meta" | sed -n 's/^version=//p')"
  release="$(printf '%s\n' "$meta" | sed -n 's/^release=//p')"
  desc="$(port_desc "$dir")"
  url="$(awk -F= '/^url=/{print substr($0, index($0,"=")+1)}' "$dir/Pkgfile" | head -n1 | sed 's/^\"//;s/\"$//')"
  local deps_decl; deps_decl="$(printf '%s\n' "$meta" | grep -E '^declare -a depends=' || true)"
  # shellcheck disable=SC1090
  eval "$deps_decl"
  deps="$(printf '%s,' "${depends[@]}" | sed 's/,$//')"

  echo "Name:         $name"
  echo "Path:         $dir"
  echo "Version:      $version"
  echo "Release:      $release"
  [[ -n "$desc" ]] && echo "Description:  $desc"
  [[ -n "$url" ]] && echo "URL:          $url"
  [[ -n "$deps" ]] && echo "Dependencies: $deps"
  [[ -f "$dir/README" ]] && echo "Readme:       Yes"
}

pkg_path() {
  ensure_dirs
  local p="${1:-}"; [[ -n "$p" ]] || die "pkg path: requires a port name"
  ports_index_build
  port_dir_of "$p"
}

pkg_printf() {
  ensure_dirs
  ports_index_build
  local fmt="${1:-}"; [[ -n "$fmt" ]] || die "pkg printf: requires a format string"
  shift || true
  local sortfmt="" filter="*"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --sort=*) sortfmt="${1#*=}"; shift;;
      --filter=*) filter="${1#*=}"; shift;;
      *) shift;;
    esac
  done

  local tmp="$STATE_DIR/printf.$$"; mkdir -p "$tmp"
  : >"$tmp/out"
  local p dir meta v r d deps readme pre post status
  while IFS='|' read -r p dir; do
    [[ "$p" == $filter ]] || continue
    meta="$(port_meta "$dir")"
    v="$(printf '%s\n' "$meta" | sed -n 's/^version=//p')"
    r="$(printf '%s\n' "$meta" | sed -n 's/^release=//p')"
    d="$(port_desc "$dir")"
    local deps_decl; deps_decl="$(printf '%s\n' "$meta" | grep -E '^declare -a depends=' || true)"
    # shellcheck disable=SC1090
    eval "$deps_decl"
    deps="$(printf '%s,' "${depends[@]}" | sed 's/,$//')"
    readme="no"; [[ -f "$dir/README" || -f "$dir/README.md" ]] && readme="yes"
    pre="no"; [[ -x "$dir/pre-install" || -x "$dir/pre-install.sh" || -x "$dir/preinstall" || -x "$dir/preinstall.sh" ]] && pre="yes"
    post="no"; [[ -x "$dir/post-install" || -x "$dir/post-install.sh" || -x "$dir/postinstall" || -x "$dir/postinstall.sh" ]] && post="yes"

    status="no"
    if is_installed "$p"; then
      local inst; inst="$(db_version "$p" || true)"
      local portver="${v}-${r}"
      if [[ "$inst" == "$portver" ]]; then status="yes"; else status="diff"; fi
    fi

    local line="$fmt"
    line="${line//%n/$p}"
    line="${line//%p/$dir}"
    line="${line//%v/$v}"
    line="${line//%r/$r}"
    line="${line//%d/$d}"
    line="${line//%e/$deps}"
    line="${line//%R/$readme}"
    line="${line//%E/$pre}"
    line="${line//%O/$post}"
    line="${line//%i/$status}"
    printf "%b" "$line" >>"$tmp/out"
  done <"${STATE_DIR}/ports.index"

  if [[ -n "$sortfmt" ]]; then
    # derive key for each port, then sort by key.
    local tmp2="$STATE_DIR/printf2.$$"; : >"$tmp2"
    while IFS='|' read -r p dir; do
      [[ "$p" == $filter ]] || continue
      local key="$sortfmt"
      local st="no"
      if is_installed "$p"; then
        local inst; inst="$(db_version "$p" || true)"
        local meta2; meta2="$(port_meta "$dir")"
        local vv; vv="$(printf '%s\n' "$meta2" | sed -n 's/^version=//p')"
        local rr; rr="$(printf '%s\n' "$meta2" | sed -n 's/^release=//p')"
        if [[ "$inst" == "${vv}-${rr}" ]]; then st="yes"; else st="diff"; fi
      fi
      key="${key//%i/$st}"
      key="${key//%n/$p}"
      echo "$key|$p"
    done <"${STATE_DIR}/ports.index" | sort | cut -d'|' -f2- >"$tmp2"
    # print in that order by re-running fmt for each p (simple, acceptable)
    while IFS= read -r p; do
      pkg_printf "$fmt" --filter="$p"
    done <"$tmp2"
    rm -f "$tmp2" "$tmp/out"
    return 0
  fi

  cat "$tmp/out"
  rm -rf "$tmp"
}

pkg_depends() {
  ensure_dirs; ports_index_build
  [[ $# -gt 0 ]] || die "pkg depends: requires package(s)"
  local tmp="$STATE_DIR/depends.$$"; mkdir -p "$tmp"; : >"$tmp/out"
  local p
  for p in "$@"; do
    resolve_deps "$p" | sed "1d" >>"$tmp/out"
  done
  awk 'NF{a[$0]++; if(a[$0]==1) print $0}' "$tmp/out"
  rm -rf "$tmp"
}

pkg_quickdep() {
  pkg_depends "$@" | tr '\n' ' ' | sed 's/[[:space:]]*$//'
  echo
}

pkg_dependent() {
  ensure_dirs; ports_index_build
  local all=0 rec=0 tree=0 v=0 vv=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --all) all=1; shift;;
      --recursive) rec=1; shift;;
      --tree) tree=1; rec=1; shift;;
      -v) v=1; shift;;
      -vv) vv=1; shift;;
      *) break;;
    esac
  done
  local target="${1:-}"; [[ -n "$target" ]] || die "pkg dependent: requires a package"

  _dependers_once() {
    local t="$1"
    local p dir meta deps_decl dep
    while IFS='|' read -r p dir; do
      [[ -n "$p" ]] || continue
      if [[ $all -eq 0 && ! -d "$PKG_DB/$p" ]]; then
        continue
      fi
      meta="$(port_meta "$dir")"
      deps_decl="$(printf '%s\n' "$meta" | grep -E '^declare -a depends=' || true)"
      # shellcheck disable=SC1090
      eval "$deps_decl"
      for dep in "${depends[@]}"; do
        dep="${dep%%[<>=]*}"
        [[ "$dep" == "$t" ]] && { echo "$p"; break; }
      done
    done <"${STATE_DIR}/ports.index"
  }

  if [[ $tree -eq 1 ]]; then
    local tmp="$STATE_DIR/dependent.$$"; mkdir -p "$tmp"
    : >"$tmp/seen"
    _tree() {
      local t="$1" indent="$2"
      local p
      while IFS= read -r p; do
        [[ -n "$p" ]] || continue
        if grep -Fxq "$p" "$tmp/seen" 2>/dev/null; then
          echo "${indent}${p} -->"
          continue
        fi
        echo "$p" >>"$tmp/seen"
        echo "${indent}${p}"
        _tree "$p" "  $indent"
      done < <(_dependers_once "$t" | sort -u)
    }
    _tree "$target" ""
    rm -rf "$tmp"
    return 0
  fi

  if [[ $rec -eq 1 ]]; then
    local tmp="$STATE_DIR/dependent.$$"; mkdir -p "$tmp"
    : >"$tmp/q"; : >"$tmp/seen"
    echo "$target" >"$tmp/q"
    while IFS= read -r cur; do
      _dependers_once "$cur" | while IFS= read -r p; do
        grep -Fxq "$p" "$tmp/seen" 2>/dev/null && continue
        echo "$p" >>"$tmp/seen"
        echo "$p" >>"$tmp/q"
      done
    done <"$tmp/q"
    sort -u "$tmp/seen"
    rm -rf "$tmp"
  else
    _dependers_once "$target" | sort -u
  fi
}

pkg_deptree() {
  ensure_dirs; ports_index_build
  local all=0; [[ "${1:-}" == "--all" ]] && { all=1; shift; }
  local target="${1:-}"; [[ -n "$target" ]] || die "pkg deptree: requires a port"

  local tmp="$STATE_DIR/deptree.$$"; mkdir -p "$tmp"; : >"$tmp/seen"
  echo "-- dependencies ([i] = installed, '-->' = seen before)"
  _tree() {
    local p="$1" indent="$2"
    if grep -Fxq "$p" "$tmp/seen" 2>/dev/null && [[ $all -eq 0 ]]; then
      echo "${indent}--> $p"
      return 0
    fi
    echo "$p" >>"$tmp/seen"
    local tag="   "
    is_installed "$p" && tag="[i]"
    echo "${indent}${tag} $p"
    local dir; dir="$(port_dir_of "$p" 2>/dev/null || true)"
    [[ -n "$dir" ]] || return 0
    local meta; meta="$(port_meta "$dir")"
    local deps_decl; deps_decl="$(printf '%s\n' "$meta" | grep -E '^declare -a depends=' || true)"
    # shellcheck disable=SC1090
    eval "$deps_decl"
    local dep
    for dep in "${depends[@]}"; do
      dep="${dep%%[<>=]*}"
      [[ -n "$dep" ]] || continue
      _tree "$dep" "  $indent"
    done
  }
  _tree "$target" ""
  rm -rf "$tmp"
}

pkg_diff_prt() {
  ensure_dirs; ports_index_build
  local all=0 v=0 vv=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --all) all=1; shift;;
      -v) v=1; shift;;
      -vv) vv=1; shift;;
      *) break;;
    esac
  done
  local -a pkgs=("$@")
  local filter="*"
  if [[ ${#pkgs[@]} -gt 0 ]]; then
    # only supports one pattern for now; join with OR by multiple passes.
    :
  fi
  local p dir meta portver inst desc
  while IFS='|' read -r p dir; do
    [[ -n "$p" ]] || continue
    if [[ ${#pkgs[@]} -gt 0 ]]; then
      local ok=0 x
      for x in "${pkgs[@]}"; do
        [[ "$p" == $x ]] && ok=1
      done
      [[ $ok -eq 1 ]] || continue
    fi
    is_installed "$p" || continue
    if [[ $all -eq 0 ]] && locked_has "$p"; then
      continue
    fi
    inst="$(db_version "$p" || true)"
    meta="$(port_meta "$dir")"
    portver="$(printf '%s\n' "$meta" | awk -F= '/^version=/{v=$2} /^release=/{r=$2} END{print v "-" r}')"
    if [[ "$inst" != "$portver" ]]; then
      if [[ $vv -eq 1 ]]; then
        desc="$(port_desc "$dir")"
        echo "$p $inst -> $portver $desc"
      elif [[ $v -eq 1 ]]; then
        echo "$p $inst -> $portver"
      else
        echo "$p"
      fi
    fi
  done <"${STATE_DIR}/ports.index"
}

pkg_quickdiff() { pkg_diff_prt "$@" | awk '{print $1}'; }

pkg_lock() { ensure_dirs; need_root; [[ $# -gt 0 ]] || die "pkg lock: requires package(s)"; local p; for p in "$@"; do locked_add "$p"; done; }
pkg_unlock() { ensure_dirs; need_root; [[ $# -gt 0 ]] || die "pkg unlock: requires package(s)"; local p; for p in "$@"; do locked_del "$p"; done; }
pkg_listlocked() {
  ensure_dirs
  local v=0 vv=0
  [[ "${1:-}" == "-v" ]] && { v=1; shift; }
  [[ "${1:-}" == "-vv" ]] && { vv=1; shift; }
  local p
  while IFS= read -r p; do
    [[ -n "$p" ]] || continue
    if [[ $vv -eq 1 || $v -eq 1 ]]; then
      if is_installed "$p"; then
        local ver; ver="$(db_version "$p" || true)"
        echo "$p $ver"
      else
        echo "$p -"
      fi
    else
      echo "$p"
    fi
  done < <(locked_list)
}

pkg_dup() {
  ensure_dirs
  local tmp="$STATE_DIR/dup.$$"; mkdir -p "$tmp"
  : >"$tmp/list"
  while IFS= read -r root; do
    find "$root" -mindepth 2 -maxdepth 2 -type f -name Pkgfile -printf '%h\n' 2>/dev/null |       while IFS= read -r d; do echo "$(basename "$d")|$d" >>"$tmp/list"; done
  done < <(ports_roots)
  sort "$tmp/list" >"$tmp/sorted"
  cut -d'|' -f1 "$tmp/sorted" | uniq -d | while IFS= read -r name; do
    echo "$name"
    grep -F "^${name}|" "$tmp/sorted" | cut -d'|' -f2- | sed 's/^/  /'
  done
  rm -rf "$tmp"
}

pkg_dumpconfig() {
  ensure_dirs
  echo "Configuration file: ${PKG_CONF} (and compat: ${PRTGET_CONF_COMPAT})"
  echo "Lock file:          ${LOCK_FILE}"
  echo "Ports dir:          ${PORTS_DIR}"
  echo "Ports method:       ${PORTS_METHOD}"
  echo "Ports repo:         ${PORTS_REPO:-<unset>}"
  echo "Source cache:       ${SRC_CACHE}"
  echo "Binary cache:       ${BIN_CACHE}"
  echo "DB root:            ${PKG_DB_ROOT}"
  echo
  echo "Port directories:"
  ports_roots | sed 's/^/ /'
}

# ----------------------------
# sysup (upgrade outdated installed packages that exist as ports)
# ----------------------------
pkg_sysup() {
  ensure_dirs
  need_root
  ports_index_build

  local -a targets=()
  local pkgdir pkg name instver dir meta portver
  for pkgdir in "$PKG_DB"/*; do
    [[ -d "$pkgdir" ]] || continue
    name="$(basename "$pkgdir")"
    instver="$(cat "$pkgdir/version" 2>/dev/null || true)"
    dir="$(port_dir_of "$name" 2>/dev/null || true)"
    [[ -n "$dir" ]] || continue
    meta="$(port_meta "$dir")"
    portver="$(printf '%s\n' "$meta" | awk -F= '/^version=/{v=$2} /^release=/{r=$2} END{print v "-" r}')"
    if [[ -n "$instver" && -n "$portver" && "$instver" != "$portver" ]]; then
      targets+=("$name")
    fi
  done

  if [[ ${#targets[@]} -eq 0 ]]; then
    info "pkg sysup: no upgrades needed"
    return 0
  fi

  # Resolve combined install order by doing deps for each target then stable-unique
  local tmp="$STATE_DIR/sysup.$$"
  mkdir -p "$tmp"
  : >"$tmp/order"
  local t p
  for t in "${targets[@]}"; do
    resolve_deps "$t" >>"$tmp/order"
  done
  awk 'NF{a[$0]=a[$0]+1; if(a[$0]==1) print $0}' "$tmp/order" >"$tmp/order.uniq"

  # Build+upgrade in order; skip deps not installed if not required? sysup upgrades as needed; we'll upgrade everything in list if installed OR is a target.
  while IFS= read -r p; do
    [[ -n "$p" ]] || continue
    if is_installed "$p" || printf '%s\n' "${targets[@]}" | grep -Fxq "$p"; then
      local pkgfile; pkgfile="$(build_port "$p")"
      install_pkg_file "$pkgfile" "$p"
    fi
  done <"$tmp/order.uniq"

  rm -rf "$tmp"
}

# ----------------------------
# diff (show port Pkgfile vs installed version, and git diff if applicable)
# ----------------------------
pkg_diff() {
  ensure_dirs
  local p="${1:-}"; [[ -n "$p" ]] || die "pkg diff error: requires a port/package name"
  ports_index_build
  local dir; dir="$(port_dir_of "$p" 2>/dev/null || true)"
  [[ -n "$dir" ]] || die "pkg diff error: port not found: $p"

  local inst=""
  if is_installed "$p"; then
    inst="$(db_version "$p" || true)"
  fi

  local meta; meta="$(port_meta "$dir")"
  local portver; portver="$(printf '%s\n' "$meta" | awk -F= '/^version=/{v=$2} /^release=/{r=$2} END{print v "-" r}')"

  echo "Port: $p"
  echo "Port dir: $dir"
  echo "Port version: $portver"
  echo "Installed: ${inst:-<not installed>}"

  # If ports tree is git repo, show git diff for this port (local modifications).
  if [[ -d "$PORTS_DIR/.git" ]] && have git; then
    ( cd "$PORTS_DIR" && git diff -- "$dir" || true )
  else
    info "pkg diff: git diff unavailable (ports tree not a git repo or git missing)"
  fi
}

# ----------------------------
# readme (print README in port dir if exists)
# ----------------------------
pkg_readme() {
  ensure_dirs
  local p="${1:-}"; [[ -n "$p" ]] || die "pkg readme error: requires a port name"
  ports_index_build
  local dir; dir="$(port_dir_of "$p" 2>/dev/null || true)"
  [[ -n "$dir" ]] || die "pkg readme error: port not found: $p"
  local readme=""
  for readme in "$dir/README" "$dir/Readme" "$dir/readme" "$dir/README.md"; do
    [[ -f "$readme" ]] && { cat "$readme"; return 0; }
  done
  die "pkg readme: no README found for $p"
}

# ----------------------------
# depinst (install missing dependencies only)
# ----------------------------
pkg_depinst() {
  ensure_dirs
  need_root
  local p="${1:-}"; [[ -n "$p" ]] || die "pkg depinst error: requires a port name"
  ports_index_build
  local ordered=()
  mapfile -t ordered < <(resolve_deps "$p")
  local x
  for x in "${ordered[@]}"; do
    [[ "$x" == "$p" ]] && continue
    if is_installed "$x"; then
      continue
    fi
    local pkgfile; pkgfile="$(build_port "$x")"
    install_pkg_file "$pkgfile" "$x"
  done
}

# ----------------------------
# revdep audit (missing shared libs)
# ----------------------------
revdep_audit() {
  ensure_dirs
  have ldd || die "revdep error: ldd not found"

  local -a broken=()
  local pkg pkgdir rel abs
  for pkgdir in "$PKG_DB"/*; do
    [[ -d "$pkgdir" ]] || continue
    pkg="$(basename "$pkgdir")"
    while IFS= read -r rel; do
      [[ -n "$rel" && "$rel" != */ ]] || continue
      abs="/$rel"
      [[ -e "$abs" && -x "$abs" ]] || continue
      head -c 4 "$abs" 2>/dev/null | grep -q $'\x7fELF' || continue
      if ldd "$abs" 2>/dev/null | grep -q 'not found'; then
        broken+=("$pkg:$rel")
        break
      fi
    done < <(db_files "$pkg" 2>/dev/null || true)
  done

  if [[ ${#broken[@]} -eq 0 ]]; then
    info "revdep: no missing shared library dependencies detected"
    return 0
  fi

  printf '%s\n' "${broken[@]}" | sort
  return 2
}

# ----------------------------
# pkg frontend usage + main
# ----------------------------
pkg_usage() {
  cat <<'EOF'
Usage: pkg <command> [args...]

Core (pkgutils-compatible)
  pkgadd <pkgfile> [options]     Install/upgrade binary package(s)
  pkgrm <name> [options]         Remove installed package
  pkginfo [options]              Query installed package database
  pkgmk [options]                Build package from Pkgfile in current directory
  rejmerge [path]                Merge .rej files (defaults to /etc)

Ports / prt-get compatible (command set exposed as "pkg", not "prt-get")
  install [opts] <port...>       Build+install ports (no dep resolution)
  update  [opts] <port...>       Rebuild+upgrade listed ports
  upgrade [opts] <port...>       Alias of update
  grpinst [opts] <port...>       Like install, stop on first failure
  depinst [opts] <port...>       Install ports with dependency resolution (cycle-detect)
  remove <pkg...>                Alias of pkgrm (multiple supported)
  sysup  [opts]                  Update all outdated installed ports (skips locked)

  list    [-v|-vv] [--path] [pat] List available ports
  listinst [-v|-vv] [pat]         List installed ports
  listorphans [-v|-vv]            List installed ports not required by others (best-effort)
  search  [-v|-vv] [--path] [--regex] <expr>   Search port names
  dsearch [-v|-vv] [--path] [--regex] <expr>   Search port names+descriptions
  fsearch [--full] [--regex] <pat>             Search file names in footprints
  info <port>                     Show metadata about a port
  path <port>                     Print port directory
  readme <port>                   Show README if present
  printf <fmt> [--sort=fmt] [--filter=pat]     Formatted listing

  depends <port...>               Print dependency closure (excluding the port itself)
  quickdep <port...>              Like depends, but in one line
  dependent [opts] <port>         Reverse dependencies (--all/--recursive/--tree)
  deptree [--all] <port>          Dependency tree

  diff [--all|-v|-vv] [pat...]    Show installed packages with newer port versions
  quickdiff                        Machine-friendly list for update/sysup
  isinst <pkg>                     Print yes/no; exit 0 if installed
  current <pkg>                    Print installed version-release
  dup                              Show duplicate ports in the configured trees

  lock <port...>                  Prevent sysup/diff from considering these ports
  unlock <port...>
  listlocked [-v|-vv]

Ports tree sync (your git/rsync repo)
  sync                             Update ports tree (PORTS_METHOD=git|rsync, PORTS_REPO=..., PORTS_DIR=...)

Config / diagnostics
  dumpconfig                        Print effective configuration

Notes
  * Options for install/update/grpinst/depinst/sysup: --margs=..., --aargs=..., -fr, -um, -uf, -f, --cache, --log,
    --test, --pre-install, --post-install, --install-scripts
  * This script reads /etc/pkg.conf and /etc/prt-get.conf for 'prtdir' entries. If neither exists, PORTS_DIR is used.

EOF
}


pkg_main() {
  ensure_dirs
  local cmd="${1:-}"
  shift || true

  case "$cmd" in
    ""|-h|--help) pkg_usage; exit 0;;

    # ports tree sync
    sync) pkg_sync "$@";;

    # prt-get compatible install/update/remove
    install) pkg_install "$@";;
    update) pkg_update_pkgs "$@";;
    upgrade) pkg_update_pkgs "$@";;
    grpinst) pkg_grpinst "$@";;
    depinst) pkg_depinst_full "$@";;
    remove) pkg_remove_multi "$@";;

    sysup) pkg_sysup "$@";;

    # info/list/search
    list) pkg_list "$@";;
    listinst) pkg_listinst "$@";;
    listorphans) pkg_listorphans "$@";;
    search) pkg_search "$@";;
    dsearch) pkg_dsearch "$@";;
    fsearch) pkg_fsearch "$@";;
    info) pkg_info "$@";;
    path) pkg_path "$@";;
    readme) pkg_readme "$@";;
    printf) pkg_printf "$@";;

    # dependencies / status
    depends) pkg_depends "$@";;
    quickdep) pkg_quickdep "$@";;
    dependent) pkg_dependent "$@";;
    deptree) pkg_deptree "$@";;

    diff) pkg_diff_prt "$@";;
    quickdiff) pkg_quickdiff "$@";;

    isinst) pkg_isinst "$@";;
    current) pkg_current "$@";;
    dup) pkg_dup "$@";;

    lock) pkg_lock "$@";;
    unlock) pkg_unlock "$@";;
    listlocked) pkg_listlocked "$@";;

    dumpconfig) pkg_dumpconfig;;

    # keep legacy aliases from earlier revisions
    revdep) pkg_revdep "$@";;

    *) die "pkg error: unknown command: $cmd (use --help)";;
  esac
}


# ----------------------------
# Dispatch by argv[0]
# ----------------------------
dispatch() {
  ensure_dirs
  local me; me="$(basename "$0")"
  case "$me" in
    pkgadd)    pkgadd_main "$@";;
    pkgrm)     pkgrm_main "$@";;
    pkginfo)   pkginfo_main "$@";;
    pkgmk)     pkgmk_main "$@";;
    rejmerge)  rejmerge_main "$@";;
    pkg)       pkg_main "$@";;
    *)
      cat <<'EOF'
cruxpkg (single file) - install as one script with CRUX-style symlinks.

Install:
  install -m 0755 cruxpkg.sh /usr/bin/cruxpkg
  ln -sf /usr/bin/cruxpkg /usr/bin/pkgadd
  ln -sf /usr/bin/cruxpkg /usr/bin/pkgrm
  ln -sf /usr/bin/cruxpkg /usr/bin/pkginfo
  ln -sf /usr/bin/cruxpkg /usr/bin/pkgmk
  ln -sf /usr/bin/cruxpkg /usr/bin/rejmerge
  ln -sf /usr/bin/cruxpkg /usr/bin/pkg

Update configuration (examples):
  PORTS_METHOD=git PORTS_REPO=git@example.com:ports.git PORTS_DIR=/usr/ports pkg update
  PORTS_METHOD=rsync PORTS_REPO=rsync://example.com/ports/ PORTS_DIR=/usr/ports pkg update
EOF
      exit 1
      ;;
  esac
}

dispatch "$@"
