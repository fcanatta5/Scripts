# mpkg

**mpkg** é um gerenciador de pacotes em Python, projetado para trabalhar em conjunto com o **mkpkg-lite**.  
Ele gerencia **PKGFILEs** , resolve dependências, constrói pacotes, instala, remove e mantém um banco de dados local de arquivos instalados — tudo sem depender de um sistema de pacotes global.

---

## ✨ Principais recursos

- Resolução de dependências (`depends` e `makedepends`) com **detecção de ciclos**
- Execução completa do pipeline do `mkpkg-lite`:
  - `fetch`, `prepare`, `build`, `install`, `package`, `post_install`
- **Download HTTP/FTP com progresso real (percentual)** para `source=()` não‑git
- Cache de fontes e reuso automático pelo `mkpkg-lite`
- Verificação de integridade:
  - `sha256sums`
  - `b2sums` (blake2b)
  - `validpgpkeys` (GPG, best‑effort)
- **pkgver() dinâmica**
- Registro completo de arquivos instalados por *diff* do prefixo
- Remoção segura:
  - padrão: remove apenas arquivos novos
  - `--force`: remove também arquivos alterados
- Remoção de órfãos
- Rebuild completo de pacotes instalados
- Upgrade por comparação de versões
- Suporte a múltiplos repositórios Git ou locais
- Instalação a partir de pacotes binários (`.tar.zst` / `.tar.gz`)
- Interface CLI com **cores e negrito** (sem TUI)
- Logs persistentes e consultáveis
- `dry-run` para simulação segura

---

## 📦 Requisitos

- Python ≥ 3.9
- bash
- mkpkg-lite
- Ferramentas comuns de build (dependem dos PKGFILEs):
  - gcc, make, tar, patch, etc.
- Opcional:
  - git (repos Git)
  - gpg (verificação PGP)
  - zstd (pacotes `.tar.zst`)

---

## 🚀 Instalação

```bash
tar -xzf mpkg_fixed.tar.gz
cd mpkg
chmod +x mpkg
sudo install -m 755 mpkg /usr/local/bin/mpkg
```

---

## ⚙️ Configuração

Na primeira execução, o mpkg cria:

```
~/.config/mpkg/config.json
```

Exemplo:

```json
{
  "mkpkg_lite": "mkpkg-lite",
  "prefix": "/usr/local",
  "repos": [
    {
      "name": "local",
      "path": "/caminho/para/repositorio"
    },
    {
      "name": "cross",
      "git": "https://github.com/usuario/meu-repo.git",
      "path": "~/.cache/mpkg/repos/cross"
    }
  ]
}
```

---

## 🧭 Comandos principais

### Procurar pacotes
```bash
mpkg -s binutils
```

### Instalar pacotes
```bash
mpkg -i binutils
```

### Instalar com prefixo customizado
```bash
mpkg -i gcc -p /opt/cross
```

### Informações do pacote
```bash
mpkg -q musl
```

### Listar instalados
```bash
mpkg -l
```

### Remover pacote (seguro)
```bash
mpkg -r musl
```

### Remover forçando arquivos alterados
```bash
mpkg -r musl --force
```

### Upgrade inteligente
```bash
mpkg -g --update
```

### Rebuild de todos os pacotes instalados
```bash
mpkg -b
```

### Remover órfãos
```bash
mpkg -o --remove
```

### Instalar pacote binário
```bash
mpkg -I ./gcc-15.2.0-1.tar.zst
```

### Limpar caches
```bash
mpkg -c
```

### Ver logs
```bash
mpkg -L --tail 200
```

### Simular ações
```bash
mpkg --dry-run -i gcc
```

---

## 📁 Logs

Os logs ficam em:

```
~/.local/state/mpkg/logs/
```

Cada execução gera um arquivo de log separado, sem códigos ANSI.

---

## 🔐 Segurança

- PKGFILEs são **scripts shell** e **não são sandboxados**
- Execute apenas PKGFILEs de fontes confiáveis
- Remoção padrão nunca apaga arquivos alterados
- `--force` existe, mas é intencionalmente explícito

---

## 🧩 Casos de uso

- Construção de toolchains temporários (cross‑compile)
- Ambientes isolados em `/opt`
- Sistemas minimalistas sem gerenciador de pacotes
- Builds reproduzíveis baseados em PKGFILE

---

## 📜 Licença

MIT License

---

## 📌 Status

Projeto funcional, em evolução contínua.  
Contribuições, testes e auditorias são bem‑vindos.
