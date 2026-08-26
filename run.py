"""Точка входа. Боты, планировщик и панель поднимаются одним процессом."""
from __future__ import annotations

import argparse
import logging
import os

import uvicorn

parser = argparse.ArgumentParser(description="AI Sales Agent")
parser.add_argument("--portable", action="store_true", help="run without systemd or OS changes")
args, _ = parser.parse_known_args()
if args.portable:
    os.environ["PORTABLE_MODE"] = "1"

from app.portable import bootstrap

bootstrap()

from app import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ %(levelname)s %(name)s: %(message)s",
)

if __name__ == "__main__":
    uvicorn.run(
        "app.web.main:app",
        host=config.HOST,
        port=config.PORT,
        log_level=config.LOG_LEVEL.lower(),
        access_log=False,
    )
