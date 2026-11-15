"""Package metadata for Cisco Access Switch Audit."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version

from .version import get_version

__title__ = "cisco-access-switch-audit"
__app_name__ = __title__
__description__ = "Cisco access switch audit tool with CLI, TUI, jump host support, and Excel reporting"
__license__ = "Apache-2.0"
__author__ = "Christopher Davies"

try:
    __version__ = package_version(__title__)
except PackageNotFoundError:
    __version__ = get_version()