# pkg — Tutorial (v2.2)

Esta versão evolui o pkg para uso diário em rolling:

- deps separadas: **bdeps** (build) e **rdeps** (runtime)
- rebuild automático de reverse-deps quando há **remoção de SONAME**
- instalação mais robusta com **owners** (não remove arquivo de outro pacote)
- `index.tsv` com **search avançado por filtros**
- comandos operacionais: `upgrade`, `check`, `verify`, `explain`, hooks e métricas
- modo toolchain: `PKG_TARGET` + `PKG_SYSROOT` (opcional)

## 1) Instalação
```bash
chmod +x ./pkg
./pkg init
```

## 2) Recipe (modelo)
```ini
name=zlib
version=1.3.1
url=https://zlib.net/zlib-1.3.1.tar.gz
sha256=...
stage=make
bdeps=
rdeps=
```

Compatibilidade:
- `deps=` antigo é tratado como `rdeps=`.

## 3) Index inteligente
Atualizar:
```bash
pkg index --refresh
```

Buscar:
```bash
pkg search zlib
pkg search "cat:libs stage:cmake"
pkg search "dep:zlib"
pkg -s "name:wayland"
```

## 4) Fetch-only
```bash
pkg fetch zlib
pkg fetch --sources zlib
pkg fetch @world
```

## 5) Upgrade (política com waves + locks)
Waves (heurística por categoria):
- toolchain: categoria contém `toolchain`
- libs: categoria contém `lib`/`libs`
- apps: resto

Executar upgrade:
```bash
pkg upgrade
pkg upgrade --waves toolchain,libs,apps
pkg upgrade --no-rebuild
```

Locks:
```bash
pkg lock mesa   # (lock/unlock continuam por arquivo em locks/)
pkg unlock mesa
```

## 6) Check (integridade operacional)
Verifica:
- arquivos faltando do manifest
- libs quebradas via `ldd` (se disponível)
- RPATH suspeito (se readelf/objdump disponível)

```bash
pkg check
pkg check openssl
```

## 7) Verify
- `verify distfiles`: confere sha256 dos distfiles em cache (se sha256 existe e não está skip)
- `verify prefix`: confere se cada arquivo do owners existe no prefix

```bash
pkg verify
pkg verify distfiles
pkg verify prefix
```

## 8) Explain
```bash
pkg explain foo
pkg explain foo --tree
```

## 9) Hooks e métricas
Hooks opcionais:
- `~/.local/share/pkg/hooks/post-install.sh`
- `~/.local/share/pkg/hooks/post-remove.sh`

Assinatura:
```bash
post-install.sh <pkg> <prefix> <home>
post-remove.sh  <pkg> <prefix> <home>
```

Métricas:
- `logs/registry.jsonl` recebe eventos `metric` com duração por pacote.

## 10) Toolchain: target/sysroot (opcional)
```bash
export PKG_TARGET=x86_64-linux-musl
export PKG_SYSROOT=$PKG_PREFIX/$PKG_TARGET/sysroot
pkg -i binutils
```

Observação objetiva:
- isto fornece base para passar `--target/--host` e `--with-sysroot` em binutils/gcc.
- um bootstrap completo ainda depende de recipes adicionais (gmp/mpfr/mpc/isl + headers etc.).
