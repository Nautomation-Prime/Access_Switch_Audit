"""Import-level smoke tests.

Every shipped module is imported here. The suite previously exercised only leaf
helpers, so a broken top-level import in cli.py (it imported a `lib` package
that does not exist in this repository) left CI green while both entry points —
`python -m switch_audit` and the console script — failed instantly for users.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest

import switch_audit

MODULES = [
    "switch_audit.app_config",
    "switch_audit.cli",
    "switch_audit.collectors",
    "switch_audit.credentials",
    "switch_audit.device_auditor",
    "switch_audit.env_file",
    "switch_audit.excel_reporter",
    "switch_audit.formatters",
    "switch_audit.jump_manager",
    "switch_audit.logging_setup",
    "switch_audit.netmiko_utils",
    "switch_audit.options",
    "switch_audit.oui",
    "switch_audit.ssh_security",
    "switch_audit.tui",
    "switch_audit.validators",
    "switch_audit.version",
]


@pytest.mark.parametrize("module_name", MODULES)
def test_module_imports(module_name: str) -> None:
    assert importlib.import_module(module_name) is not None


def test_every_submodule_is_covered_by_the_smoke_list() -> None:
    """A new module must be added above, so it cannot silently escape this net."""
    discovered = {
        f"switch_audit.{info.name}"
        for info in pkgutil.iter_modules(switch_audit.__path__)
        if not info.name.startswith("_")
    }
    assert discovered <= set(MODULES), f"unlisted modules: {sorted(discovered - set(MODULES))}"


def test_entrypoint_module_exposes_main() -> None:
    """pyproject maps the console script to switch_audit.__main__:main."""
    entry = importlib.import_module("switch_audit.__main__")
    assert callable(entry.main)


def test_parser_builds_and_help_is_encodable() -> None:
    """--help must render on a Windows ANSI code page without raising."""
    from switch_audit.cli import build_parser

    help_text = build_parser().format_help()
    assert "--devices" in help_text
    help_text.encode("cp1252", errors="strict")


def test_parser_defaults() -> None:
    from switch_audit.cli import build_parser

    args = build_parser().parse_args([])
    assert args.devices == "devices.txt"
    assert args.workers >= 1
    assert args.tui is False
    assert args.direct is False


def test_list_collectors_runs_without_a_device(capsys) -> None:
    """--list-collectors documents the config.yaml surface; it must never dial out."""
    from switch_audit.cli import main

    assert main(["--list-collectors"]) == 0
    out = capsys.readouterr().out
    assert "switchport" in out
    assert "mac_table" in out
    assert "Voice VLAN" in out
