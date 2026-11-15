"""Cisco Access Switch Audit package."""

# Applied before any other import: assets.config_loader caches environment
# lookups the moment it is constructed, so a .env loaded later would be missed.
# Real environment variables always take precedence over the file.
from .env_file import load_dotenv as _load_dotenv

_load_dotenv()

from .__about__ import __author__, __license__, __version__  # noqa: E402

__all__ = [
    "__author__",
    "__license__",
    "__version__",
    "cli",
    "device_auditor",
    "excel_reporter",
    "credentials",
    "jump_manager",
    "netmiko_utils",
    "formatters",
    "validators",
    "app_config",
    "env_file",
]
