"""Logging setup module for the Yeedu MCP Server."""

from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterable, Optional


# Disk ceiling per user is MAX_BYTES * (BACKUP_COUNT + 1) = 20 MB
_DEFAULT_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
_DEFAULT_BACKUP_COUNT = 3
# User-level so stdio launches (e.g. via uvx) don't drop logs/ into the client's cwd
_DEFAULT_LOG_DIR = "~/.yeedu-mcp/logs"
_DEFAULT_LOG_LEVEL = "INFO"
_DEFAULT_LOG_FORMAT = (
    "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _coerce_int_env(value: Optional[str], default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _normalize_service_name(service_name: str) -> str:
    value = (service_name or "").strip() or "app"
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", value)


def _has_file_handler(logger: logging.Logger, log_path: Path) -> bool:
    target = str(log_path.resolve())
    for handler in logger.handlers:
        base_filename = getattr(handler, "baseFilename", None)
        if base_filename and str(Path(base_filename).resolve()) == target:
            return True
    return False


def configure_logging(
    *,
    service_name: str,
    log_dir: Optional[str] = None,
    log_file: Optional[str] = None,
    max_bytes: Optional[int] = None,
    backup_count: Optional[int] = None,
    level: Optional[str] = None,
    log_format: Optional[str] = None,
    configure_console: bool = True,
    extra_loggers: Optional[Iterable[str]] = None,
) -> tuple[logging.Logger, Path]:
    """Configure rotating file logging for the current process.

    Environment overrides (all optional):
        LOG_DIR, LOG_LEVEL, LOG_FORMAT, LOG_MAX_BYTES, LOG_BACKUP_COUNT,
        <SERVICE>_LOG_FILE (e.g. SERVER_LOG_FILE)

    Returns:
        tuple[logging.Logger, Path]: Configured logger and log file path.
    """
    normalized = _normalize_service_name(service_name)

    resolved_log_dir = Path(
        log_dir or os.getenv("LOG_DIR", _DEFAULT_LOG_DIR)
    ).expanduser()
    resolved_log_dir.mkdir(parents=True, exist_ok=True)

    env_log_file_key = f"{normalized.upper()}_LOG_FILE"
    env_log_file = os.getenv(env_log_file_key)

    filename = (env_log_file or log_file or f"{normalized}.log").strip()
    log_path = (
        resolved_log_dir / filename
        if not Path(filename).is_absolute()
        else Path(filename)
    )
    log_path = log_path.expanduser().resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    resolved_level = (level or os.getenv("LOG_LEVEL", _DEFAULT_LOG_LEVEL)).upper()
    if resolved_level not in _VALID_LOG_LEVELS:
        resolved_level = _DEFAULT_LOG_LEVEL

    resolved_format = log_format or os.getenv("LOG_FORMAT", _DEFAULT_LOG_FORMAT)

    resolved_max_bytes = (
        max_bytes
        if max_bytes is not None
        else _coerce_int_env(os.getenv("LOG_MAX_BYTES"), _DEFAULT_MAX_BYTES)
    )
    resolved_backup_count = (
        backup_count
        if backup_count is not None
        else _coerce_int_env(os.getenv("LOG_BACKUP_COUNT"), _DEFAULT_BACKUP_COUNT)
    )

    root_logger = logging.getLogger()

    if configure_console:
        has_console_handler = any(
            isinstance(h, logging.StreamHandler) and not getattr(h, "baseFilename", None)
            for h in root_logger.handlers
        )
        if not has_console_handler:
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(logging.Formatter(resolved_format))
            root_logger.addHandler(console_handler)

    if not _has_file_handler(root_logger, log_path):
        file_handler = RotatingFileHandler(
            filename=str(log_path),
            maxBytes=resolved_max_bytes,
            backupCount=resolved_backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter(resolved_format))
        root_logger.addHandler(file_handler)

    root_logger.setLevel(resolved_level)

    root_file_handlers = [
        h for h in root_logger.handlers if getattr(h, "baseFilename", None)
    ]
    if root_file_handlers:
        handler = root_file_handlers[-1]
        for logger_name in extra_loggers or ():
            child_logger = logging.getLogger(logger_name)
            if handler not in child_logger.handlers:
                child_logger.addHandler(handler)

    logger = logging.getLogger(service_name)
    return logger, log_path
