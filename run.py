"""Запуск микросервиса заявок: ``python run.py``."""

from __future__ import annotations

import uvicorn

from app.config import get_settings

if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.tickets_host,
        port=settings.tickets_port,
        reload=False,
        log_level="info",
    )
