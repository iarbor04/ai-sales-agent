"""Portable runtime bootstrap. It only writes inside the configured data directory."""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path


def bootstrap() -> None:
    if os.environ.get("PORTABLE_MODE", "0").lower() not in {"1", "true", "yes", "on"}:
        return
    root = Path(__file__).resolve().parent.parent
    data = Path(os.environ.get("DATA_DIR", root / "data")).expanduser().resolve()
    data.mkdir(parents=True, exist_ok=True)
    credentials = data / ".credentials.env"
    saved: dict[str, str] = {}
    if credentials.exists():
        for line in credentials.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                saved[key] = value
    created = False
    if not saved.get("ADMIN_PASSWORD"):
        saved["ADMIN_PASSWORD"] = secrets.token_urlsafe(18)
        created = True
    if not saved.get("SECRET_KEY"):
        saved["SECRET_KEY"] = secrets.token_urlsafe(48)
        created = True
    credentials.write_text(
        f"ADMIN_PASSWORD={saved['ADMIN_PASSWORD']}\nSECRET_KEY={saved['SECRET_KEY']}\n",
        encoding="utf-8",
    )
    try:
        credentials.chmod(0o600)
    except OSError:
        pass
    os.environ.setdefault("ADMIN_PASSWORD", saved["ADMIN_PASSWORD"])
    os.environ.setdefault("SECRET_KEY", saved["SECRET_KEY"])
    os.environ.setdefault("DATA_DIR", str(data))
    os.environ.setdefault("DB_PATH", str(data / "data.db"))
    os.environ.setdefault("MEDIA_DIR", str(data / "media"))
    os.environ.setdefault("HOST", "0.0.0.0")
    os.environ.setdefault("PORT", "8000")
    if created:
        print(f"ADMIN_LOGIN={os.environ.get('ADMIN_LOGIN', 'admin')}")
        print(f"ADMIN_PASSWORD={saved['ADMIN_PASSWORD']}")
        print("Save this password; it will not be printed again.")
    if not os.environ.get("PUBLIC_URL"):
        print("PUBLIC_URL is not configured", file=sys.stderr)
    if os.environ.get("PERSISTENT_STORAGE", "0") != "1":
        print("WARNING: storage may be ephemeral; data may be lost when the runtime restarts", file=sys.stderr)
