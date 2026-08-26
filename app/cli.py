"""Maintenance commands that work without systemd."""
from __future__ import annotations

import argparse
import secrets
import shutil
import sqlite3
from datetime import datetime, timezone

from . import config, db


def backup() -> int:
    db.init()
    target = config.BACKUP_DIR / f"data-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.db"
    with sqlite3.connect(target) as destination:
        db.connect().backup(destination)
    print(target)
    return 0


def check_db() -> int:
    db.init()
    result = db.q1("PRAGMA integrity_check")
    ok = bool(result and result[0] == "ok")
    print("DATABASE: OK" if ok else "DATABASE: FAIL")
    db.close()
    return 0 if ok else 1


def restore(source: str) -> int:
    path = config.BACKUP_DIR / source
    path = path.resolve()
    if path.parent != config.BACKUP_DIR.resolve() or not path.is_file():
        raise SystemExit("backup must be a file directly inside BACKUP_DIR")
    db.close()
    shutil.copy2(path, config.DB_PATH)
    print(config.DB_PATH)
    return check_db()


def reset_password() -> int:
    password = secrets.token_urlsafe(18)
    credentials = config.DATA_DIR / ".credentials.env"
    secret = config.SECRET_KEY or secrets.token_urlsafe(48)
    credentials.write_text(f"ADMIN_PASSWORD={password}\nSECRET_KEY={secret}\n", encoding="utf-8")
    credentials.chmod(0o600)
    print(password)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("backup")
    commands.add_parser("check-db")
    commands.add_parser("reset-password")
    restore_parser = commands.add_parser("restore")
    restore_parser.add_argument("file")
    args = parser.parse_args()
    if args.command == "backup": return backup()
    if args.command == "check-db": return check_db()
    if args.command == "reset-password": return reset_password()
    return restore(args.file)


if __name__ == "__main__":
    raise SystemExit(main())
