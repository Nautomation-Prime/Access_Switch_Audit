from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

from switch_audit import __version__
from switch_audit.version import get_version, get_version_info


def test_get_version_matches_pyproject() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    expected = data["project"]["version"]

    assert get_version() == expected
    assert __version__ == expected


def test_get_version_info_includes_name_and_version() -> None:
    info = get_version_info()

    assert "Cisco Access Switch Audit" in info
    assert f"Version: {get_version()}" in info