
# Tutorial Completo: Construindo um Sistema do Zero com Cross-Toolchain Temporária usando SBM

Este tutorial explica **passo a passo**, de forma prática e detalhada, como usar o **SBM (Simple Build Manager)** para:

1. Criar uma **cross-toolchain temporária** usando apenas o host.
2. Usar essa toolchain para construir um **sistema base funcional** (estilo LFS).
3. Evoluir do bootstrap inicial para um ambiente consistente e reutilizável.

O modelo é inspirado em Linux From Scratch, mas **totalmente automatizado e organizado** via SBM.

---

## 1. Conceitos Fundamentais

### 1.1 O que é o SBM

O SBM é um gerenciador de build baseado em:
- Build a partir de source
- Manifestos `package.yml`
- Dependências declarativas
- `DESTDIR` e `ROOTFS` isolados
- Hooks explícitos (configure, build, install)

Ele **não substitui** conhecimento de toolchains — ele **organiza e automatiza**.

---

### 1.2 O que é uma Cross-Toolchain Temporária

Uma cross-toolchain temporária é um conjunto de ferramentas:
- binutils
- gcc
- headers do kernel
- libc

construídas:
- **no host**
- **para um target diferente**
- instaladas em um **sysroot isolado**
- usadas apenas para construir o sistema final

Depois, ela pode ser descartada.

---

## 2. Layout do Projeto

Vamos assumir:

```bash
$HOME/sbm-os/
├── sbm/              # script sbm
├── repo/             # manifests
├── sources/          # tarballs
├── build/            # diretórios temporários
├── packages/         # pacotes .tar.zst
└── rootfs/           # sistema alvo
```

Defina o ambiente:

```bash
export SBM_HOME=$HOME/sbm-os
export SBM_ROOTFS=$HOME/sbm-os/rootfs
export TARGET=x86_64-myOS-linux-gnu
```

---

## 3. Estrutura do Repositório

Usaremos categorias:

```text
repo/
└── toolchain/
    ├── linux-headers/
    ├── binutils-pass1/
    ├── gcc-pass1/
    ├── glibc-headers/
    ├── glibc-startfiles/
    ├── gcc-pass2/
    └── binutils-final/
```

Cada diretório contém um `package.yml`.

---

## 4. Passo 1 – Linux Headers

### Objetivo
Instalar headers do kernel no sysroot.

### Exemplo de package.yml

```yaml
name: linux-headers
version: 6.6.30
release: 1
arch: [any]

source:
  url: https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.6.30.tar.xz
  filename: linux-6.6.30.tar.xz
  sha256: <sha256-real>

build:
  build: |
    make mrproper
    make headers

  install: |
    make INSTALL_HDR_PATH="${DESTDIR}/usr" headers_install
```

Execute:

```bash
./sbm i toolchain/linux-headers
```

---

## 5. Passo 2 – Binutils Pass 1

### Objetivo
Criar assembler e linker para o target.

### Flags importantes
- `--target=$TARGET`
- `--with-sysroot=$SBM_ROOTFS`
- `--disable-nls`

### Dependência

```yaml
depends:
  - toolchain/linux-headers
```

Após build:

```bash
$SBM_ROOTFS/usr/bin/$TARGET-ld
```

---

## 6. Passo 3 – GCC Pass 1 (C apenas)

### Objetivo
Criar compilador C mínimo para construir glibc.

### Flags críticas

```bash
--target=$TARGET
--with-sysroot=$SBM_ROOTFS
--disable-nls
--enable-languages=c
--without-headers
```

Instale em `/usr` do sysroot.

---

## 7. Passo 4 – glibc headers e startfiles

### Objetivo
Preparar libc sem dependência circular.

### Estratégia
- Instalar headers
- Compilar startfiles (`crt*.o`)
- Não instalar libc completa ainda

Isso permite GCC pass2.

---

## 8. Passo 5 – GCC Pass 2

Agora:
- Headers da libc existem
- Startfiles existem

Reconstrua o GCC com:

```bash
--enable-languages=c,c++
--with-sysroot=$SBM_ROOTFS
```

Agora você tem uma **cross-toolchain funcional**.

---

## 9. Passo 6 – Toolchain Final (opcional)

Reconstrua:
- binutils
- gcc
- glibc

agora **dentro do sysroot**, sem hacks temporários.

---

## 10. Construindo o Sistema Base

A partir daqui, tudo é nativo para o target:

### Ordem típica

```text
busybox
bash
coreutils
util-linux
musl ou glibc final
init system
```

Cada pacote é um `package.yml` normal.

---

## 11. Uso Inteligente do SBM

### Upgrade

```bash
./sbm upgrade toolchain/gcc-pass2
```

### Limpeza

```bash
./sbm clean -a
```

### Sincronização

```bash
./sbm sync --pull
```

---

## 12. Resultado Final

Ao final você terá:

- Sysroot isolado
- Toolchain própria
- Sistema base funcional
- Builds reproduzíveis
- Estrutura clara e versionável

O SBM passa a ser **o coração do seu sistema**.

---

## 13. Próximos Passos

- Adicionar suporte a múltiplos targets
- Criar profiles de build
- Adicionar banco de dados de pacotes instalados
- Bootstrapping automático

---

## Conclusão

Sim, é **totalmente viável** construir um sistema inteiro do zero com o SBM.

Ele não esconde a complexidade — ele **organiza**.

Se você entende LFS, o SBM vira uma ferramenta poderosa.
Se não entende, o SBM te força a aprender do jeito certo.

Boa construção.
