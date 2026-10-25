"""structlog configuration shared by every process.

``json`` format for production (one JSON object per line), ``console`` for humans.
Standard-library loggers (uvicorn, sqlalchemy, ...) are routed through the same
processor chain so all output has one shape.
"""

from __future__ import annotations

import logging
import sys
from typing import Literal

import structlog

_CONFIGURED = False


def configure_logging(level: str = "info", fmt: Literal["json", "console"] = "json") -> None:
    global _CONFIGURED
    numeric = logging.getLevelName(level.upper())
    if not isinstance(numeric, int):
        numeric = logging.INFO

    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer: structlog.types.Processor
    if fmt == "console":
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(numeric)
    for noisy in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(max(numeric, logging.WARNING))
    _CONFIGURED = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    if not _CONFIGURED:
        configure_logging()
    return structlog.get_logger(name)  # type: ignore[no-any-return]
