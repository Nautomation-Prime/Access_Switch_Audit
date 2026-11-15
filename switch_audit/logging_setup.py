"""Logging bootstrap for Cisco Access Switch Audit."""

from __future__ import annotations

import logging
import logging.config
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_LOGGING_CONF = _ROOT / "assets" / "config_files" / "logging.conf"

# A long-running scheduled audit writes a lot of DEBUG; cap the file so it
# cannot fill the disk. Five files of 5 MB is roughly a fortnight of daily runs.
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 5

_NOISY_LOGGERS = (
    "paramiko",
    "paramiko.transport",
    "netmiko",
    "netmiko.utilities",
    "netmiko.base_connection",
)


def log_directory() -> Path:
    """Return the directory log files are written to (override with LOG_DIR)."""
    return Path(os.getenv("LOG_DIR", "logs"))


def configure_logging(debug: bool = False) -> None:
    """Configure logging using the INI file if available; otherwise fall back
    to a basic rotating file + console setup.

    The LOGGING_CONFIG environment variable can override the default INI path,
    and LOG_DIR can relocate the log directory. The log directory is created
    automatically if it does not yet exist.

    Safe to call more than once: an already-configured root logger is left
    alone rather than accumulating duplicate handlers.

    Args:
        debug: When True, the console handler is set to DEBUG.
    """
    cfg_env = os.getenv("LOGGING_CONFIG", "").strip()
    cfg_path = Path(cfg_env) if cfg_env else _DEFAULT_LOGGING_CONF

    log_dir = log_directory()
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        # A read-only or unwritable location must not stop the audit; the
        # console handler below still works.
        log_dir = None

    if cfg_path.exists():
        logging.config.fileConfig(str(cfg_path), disable_existing_loggers=False)
        return

    root = logging.getLogger()
    if root.handlers:
        return  # Already configured — don't add duplicate handlers

    root.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s - %(levelname)-8s  %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG if debug else logging.INFO)
    console_handler.setFormatter(fmt)
    root.addHandler(console_handler)

    if log_dir is not None:
        try:
            file_handler = RotatingFileHandler(
                log_dir / "debug.log",
                maxBytes=_MAX_BYTES,
                backupCount=_BACKUP_COUNT,
                encoding="utf-8",
            )
        except OSError:
            file_handler = None
        if file_handler is not None:
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(fmt)
            root.addHandler(file_handler)

    # Silence noisy third-party loggers in the file too
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
