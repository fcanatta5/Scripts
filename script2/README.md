# pkg — Source-Based Package Builder em Python

`pkg` é um gerenciador de pacotes **source-based**, minimalista e explícito, escrito em Python.  
Ele constrói pacotes a partir do código-fonte, empacota o resultado em binários (`.tar.zst`) e permite reinstalações rápidas e reproduzíveis.

O projeto é inspirado em Gentoo Ports e BSD Ports, com foco em:

- simplicidade  
- previsibilidade  
- controle total do processo de build  
- suporte nativo a toolchains temporárias e cross-compilação  

---

## Principais características

- Receitas declarativas em **YAML (`package.yml`)**
- Build isolado usando `DESTDIR`
- Cache de código-fonte
- Cache de pacotes binários
- Instalação rápida a partir de binários
- Suporte a:
  - autotools
  - cmake
  - scripts de build customizados
- Resolução de dependências com ordenação topológica
- Suporte completo a **cross-toolchains temporárias (musl, glibc, bare-metal)**

---

## Estrutura do repositório

packages/  
└── categoria/  
    └── nome-do-pacote/  
        └── package.yml  

---

## Conceitos fundamentais

### PREFIX

O `pkg` **não impõe** um prefixo fixo.

Padrão:

/usr/local

Você pode sobrescrever com:

export SRCPKG_PREFIX=/tmp/meu-prefix

---

### DESTDIR

Durante o build, nada é instalado diretamente no sistema.

Os arquivos são instalados em:

~/.srcpkg/build/<id-do-pacote>/dest

Depois disso, esse conteúdo é empacotado em:

~/.srcpkg/binpkgs/<id-do-pacote>.tar.zst

---

## Dependências no host

O `pkg` **não resolve dependências do sistema**.

Você deve ter instalado:

- Python ≥ 3.10  
- make  
- gcc / g++  
- binutils  
- tar  
- zstd  

---

## Licença

MIT License
