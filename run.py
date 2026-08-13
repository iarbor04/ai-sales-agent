"""Точка входа. Боты, планировщик и панель поднимаются одним процессом."""
from __future__ import annotations

import logging

import uvicorn

from app import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

if __name__ == "__main__":
    uvicorn.run(
        "app.web.main:app",
        host=config.HOST,
        port=config.PORT,
        log_level="info",
        access_log=False,
    )
