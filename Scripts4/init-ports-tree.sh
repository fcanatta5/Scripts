#!/bin/sh
set -eu

# init-ports-tree.sh
# Cria um ports tree "inteligente" com estrutura padrão, documentação e templates.

ROOT="${1:-ports}"

die() { printf '%s\n' "error: $*" >&2; exit 2; }

[ -e "$ROOT" ] && die "Diretório já existe: $ROOT"

mkdir -p "$ROOT"

# Categorias comuns (ajuste livremente)
CATS="base core devel lib net xorg audio video desktop utils server python ruby go rust lua font doc"

for c in $CATS; do
  mkdir -p "$ROOT/$c"
done

# Diretórios de templates e ferramentas
mkdir -p "$ROOT/.templates" "$ROOT/.tools"

# .gitignore (cache local e arquivos temporários)
cat > "$ROOT/.gitignore" <<'EOF'
# build tools / caches
.cache/
*.swp
*~
.DS_Store

# footprints/md5 gerados localmente (opcional: remover se você versiona)
# Descomente se quiser ignorar:
# **/.md5sum
# **/.footprint

# logs
**/*.log
EOF

# README do tree
cat > "$ROOT/README.md" <<'EOF'
# Ports Tree

Estrutura:
  ports/<categoria>/<port>/{PKGFILE,.md5sum,.footprint,patches/,files/,README}

Convenções:
- O nome real do port é `name=` no PKGFILE.
- `patches/` contém patches aplicados em ordem alfabética.
- `files/` é overlay copiado para $PKG.
- `.md5sum` e `.footprint` recomendados.

Comandos (exemplos):
  cruxbuild.py --root . list
  cruxbuild.py --root . build -d --dry-run <port>
  sudo cruxbuild.py --root . build -d --auto-install --skip-installed <port>
EOF

# TEMPLATE OFICIAL DE PKGFILE (compatível com CRUX + cruxbuild)
cat > "$ROOT/.templates/PKGFILE" <<'EOF'
# Description: <DESCRIÇÃO CURTA>
# URL:         <URL DO PROJETO>
# Maintainer:  <NOME> <EMAIL>
# Depends on:  <lista opcional>

name=<nome-do-port>
version=<versao>
release=1

# Sources podem ser:
# - URL http/https/ftp
# - arquivo local dentro do port
# - git: git+https://... (será espelhado em cache)
source=(
  <url-ou-arquivo>
)

# Dependências (extensão usada pelo cruxbuild para ordenação e auto-install)
# (CRUX puro não padroniza isso; esta árvore usa)
depends=(
  # exemplo: zlib openssl
)

# (Opcional) prepare() roda após extração + patches automáticos e antes do build()
prepare() {
    # Exemplo:
    # cd "$SRC/<dir-extraido>"
    # sed -i 's/-Werror//g' configure
    :
}

# build() é obrigatório
build() {
    # Exemplo (autotools):
    # cd "$SRC/<dir-extraido>"
    # ./configure --prefix=/usr
    # make
    # make DESTDIR="$PKG" install

    # Exemplo (cmake out-of-tree):
    # cd "$SRC/<dir-extraido>"
    # mkdir -p build && cd build
    # cmake -DCMAKE_INSTALL_PREFIX=/usr ..
    # make
    # make DESTDIR="$PKG" install

    :
}

# (Opcional) post_install() roda após build() e após overlay de files/ no $PKG
post_install() {
    # Exemplo:
    # rm -f "$PKG/usr/lib/libiberty.a"
    :
}
EOF

# Helper: criar um novo port com layout completo
cat > "$ROOT/.tools/newport.sh" <<'EOF'
#!/bin/sh
set -eu

ROOT="${1:-.}"
CATEGORY="${2:-}"
PORTDIR="${3:-}"

die() { printf '%s\n' "error: $*" >&2; exit 2; }

[ -z "$CATEGORY" ] && die "Uso: newport.sh <root> <categoria> <nome-do-port>"
[ -z "$PORTDIR" ] && die "Uso: newport.sh <root> <categoria> <nome-do-port>"

[ -d "$ROOT/$CATEGORY" ] || die "Categoria não existe: $ROOT/$CATEGORY"

DEST="$ROOT/$CATEGORY/$PORTDIR"
[ -e "$DEST" ] && die "Port já existe: $DEST"

mkdir -p "$DEST/patches" "$DEST/files"

cp "$ROOT/.templates/PKGFILE" "$DEST/PKGFILE"
cat > "$DEST/README" <<'R'
Notas do port:
- Dependências e opções
- Passos de build
- Observações de runtime
R

printf '%s\n' "Criado: $DEST"
printf '%s\n' "Edite:  $DEST/PKGFILE"
EOF
chmod +x "$ROOT/.tools/newport.sh"

# Script opcional: validar estrutura básica do tree
cat > "$ROOT/.tools/doctor-tree.sh" <<'EOF'
#!/bin/sh
set -eu

ROOT="${1:-.}"

fail=0

# Verifica se templates existem
[ -f "$ROOT/.templates/PKGFILE" ] || { echo "missing: .templates/PKGFILE"; fail=1; }
[ -x "$ROOT/.tools/newport.sh" ] || { echo "missing: .tools/newport.sh (executable)"; fail=1; }

# Lista ports quebrados (sem PKGFILE)
# (só olha um nível abaixo das categorias)
for cat in "$ROOT"/*; do
  [ -d "$cat" ] || continue
  case "$(basename "$cat")" in
    .templates|.tools) continue ;;
  esac
  for port in "$cat"/*; do
    [ -d "$port" ] || continue
    [ -f "$port/PKGFILE" ] || { echo "missing PKGFILE: $port"; fail=1; }
  done
done

[ "$fail" -eq 0 ] && echo "doctor-tree: ok" || exit 2
EOF
chmod +x "$ROOT/.tools/doctor-tree.sh"

# Sugestão de comando para o usuário
cat > "$ROOT/INIT_DONE.txt" <<EOF
Ports tree criado em: $ROOT

Próximos passos:
  1) Criar um port:
     $ROOT/.tools/newport.sh $ROOT <categoria> <nome-do-port>

  2) Validar estrutura:
     $ROOT/.tools/doctor-tree.sh $ROOT

  3) Construir com cruxbuild:
     cruxbuild.py --root $ROOT list
EOF

printf '%s\n' "OK: ports tree criado em $ROOT"
printf '%s\n' "Template: $ROOT/.templates/PKGFILE"
printf '%s\n' "Ferramenta: $ROOT/.tools/newport.sh"
