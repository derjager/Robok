"""Token de acceso. El robot se mueve, así que la API exige un código aunque la red sea LAN."""
from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

from robok.config import ROOT, Config

TOKEN_FILE = ROOT / ".robok_token"


def resolve_token(cfg: Config, token_file: Path = TOKEN_FILE) -> str:
    """Prioridad: ROBOK_TOKEN > web.token de la configuración > archivo .robok_token (se crea solo)."""
    env = os.environ.get("ROBOK_TOKEN", "").strip()
    if env:
        return env
    if cfg.web.token:
        return cfg.web.token
    if token_file.exists():
        saved = token_file.read_text().strip()
        if saved:
            return saved
    token = secrets.token_hex(4)
    fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    return token


def same(candidate: str | None, token: str) -> bool:
    return bool(candidate) and hmac.compare_digest(candidate.encode(), token.encode())
