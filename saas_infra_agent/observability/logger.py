from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONFIGURED = False
_LOG_FILE: Path | None = None


def configure_logging(
    *,
    log_dir: str | Path | None = None,
    log_file_name: str = "saas-cli.log",
    console_level: int = logging.WARNING,
    file_level: int = logging.INFO,
) -> Path:
    """Configure logging once for the CLI.

    - Console stays clean by default (WARNING+).
    - Full logs go to a rotating file (INFO+).
    """
    global _CONFIGURED, _LOG_FILE
    if _CONFIGURED and _LOG_FILE is not None:
        return _LOG_FILE

    base_dir = Path.cwd()
    log_dir_path = Path(log_dir) if log_dir is not None else (base_dir / ".logs")
    log_dir_path.mkdir(parents=True, exist_ok=True)
    log_file = log_dir_path / log_file_name

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    for handler in list(root.handlers):
        root.removeHandler(handler)

    file_fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    # Console handler (pretty if rich is available).
    try:
        from rich.logging import RichHandler

        console_handler: logging.Handler = RichHandler(
            rich_tracebacks=True,
            markup=True,
            show_time=False,
            show_level=True,
            show_path=False,
        )
        console_handler.setFormatter(logging.Formatter("%(message)s"))
    except Exception:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(file_fmt)

    console_handler.setLevel(console_level)

    # File handler (always).
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(file_level)
    file_handler.setFormatter(file_fmt)

    root.addHandler(console_handler)
    root.addHandler(file_handler)

    # Keep noisy third-party logs off the console; they still go to the file.
    for noisy in (
        "openai",
        "httpx",
        "httpcore",
        "urllib3",
        "asyncio",
        "langchain",
        "langsmith",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True
    _LOG_FILE = log_file
    return log_file


def get_logger(name: str) -> logging.Logger:
    # Our loggers run at DEBUG; handlers decide what is emitted.
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    return logger


def get_log_file() -> Path | None:
    return _LOG_FILE
