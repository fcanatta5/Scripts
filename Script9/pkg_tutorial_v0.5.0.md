# pkg v0.5.0 — Tutorial Completo
## Construindo um sistema Linux do zero (source-based)

Este tutorial mostra como construir um sistema Linux completo usando o **pkg**, inspirado no CRUX.

---
## 1. Pré-requisitos
- Sistema Linux funcional (host)
- bash, coreutils, git, tar
- Acesso root ou sudo

---
## 2. Instalação do pkg
```bash
chmod +x pkg_v0.5.0
sudo install -m 0755 pkg_v0.5.0 /usr/local/bin/pkg
pkg --version
```

---
## 3. Estrutura de ports
```
ports/
  core/
    musl/
      Pkgfile
    gcc/
      Pkgfile
```

---
## 4. Criando root vazio
```bash
export ROOT=/mnt/pkgroot
sudo mkdir -p $ROOT
sudo pkg --root $ROOT cache
```

---
## 5. Toolchain básica
```bash
sudo pkg --root $ROOT build musl
sudo pkg --root $ROOT install musl
sudo pkg --root $ROOT build gcc
sudo pkg --root $ROOT install gcc
```

---
## 6. Sistema base
Instale:
- busybox
- bash
- coreutils
- util-linux
- eudev

```bash
sudo pkg --root $ROOT build busybox
sudo pkg --root $ROOT install busybox
```

---
## 7. Usuários e init
```bash
sudo chroot $ROOT /bin/sh
adduser user
```

---
## 8. Repositórios
```bash
pkg repo add base git https://example.com/ports.git
pkg repo update
pkg cache
```

---
## 9. Manutenção
```bash
pkg search bash
pkg upgrade all
pkg clean --all
```

---
## 10. Recuperação
```bash
pkg list
pkg info bash
pkg remove bash
```

---
Fim do tutorial.
