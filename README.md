# Cross Musl Toolchain – Repositório SBM

Este repositório contém um conjunto de manifests `package.yml` para construir
um **cross-toolchain real baseado em musl** usando o **SBM (Simple Build Manager)**.

O objetivo é fornecer um toolchain de bootstrap simples, auditável e
reprodutível, sem depender de ferramentas externas como crosstool-ng.

---

## Visão geral

Fluxo de construção do toolchain:

1. `linux-headers` – instala os headers do kernel para o target
2. `cross-binutils` – binutils configurado como cross
3. `cross-gcc-bootstrap` – GCC inicial (C-only, sem libc)
4. `cross-musl` – musl libc para o target
5. `cross-gcc` – GCC final (C/C++) ligado contra musl

Todos esses passos são descritos em `package.yml` sob `repo/cross`.

---

## Target atual

Configuração padrão deste repo:

- Host: `x86_64`
- Target: `x86_64-linux-musl`
- Arquitetura: `x86_64`
- Libc: musl
- Prefixo do toolchain: `/opt/cross`
- Sysroot: `/usr/x86_64-linux-musl`

Para portar para outro target (por exemplo `aarch64-linux-musl`), é
necessário ajustar apenas:

- o valor de `TARGET` nos scripts de `build.configure`;
- o `SYSROOT` (quando usado);
- o campo `arch:` nos manifests.

---

## Estrutura do repositório

```text
SBM_HOME/
├── sbm
├── repo/
│   └── cross/
│       ├── linux-headers/
│       ├── binutils/
│       ├── gcc-bootstrap/
│       ├── musl/
│       └── gcc/
├── sources/
├── build/
└── packages/
```
---

#### Licença

Este repositório contém apenas manifests de build (package.yml) e não redistribui os tarballs de terceiros (kernel, binutils, gcc, musl, etc.).
Cada projeto externo segue sua própria licença.
Verifique os termos de:
Linux kernel
GNU Binutils
GCC
musl libc
Antes de redistribuir binários, verifique as licenças aplicáveis.
