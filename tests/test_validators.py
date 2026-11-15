from __future__ import annotations

import pytest

from switch_audit.validators import validate_devices_file, validate_output_filename


def test_validate_devices_file_ignores_comments_and_blank_lines(tmp_path) -> None:
    devices_file = tmp_path / "devices.txt"
    devices_file.write_text("\n# comment\n192.0.2.10\n\ncore-sw-01\n", encoding="utf-8")

    assert validate_devices_file(str(devices_file)) == ["192.0.2.10", "core-sw-01"]


def test_validate_devices_file_raises_for_empty_inventory(tmp_path) -> None:
    devices_file = tmp_path / "devices.txt"
    devices_file.write_text("\n# only comments\n", encoding="utf-8")

    with pytest.raises(ValueError):
        validate_devices_file(str(devices_file))


def test_validate_output_filename_appends_xlsx_extension() -> None:
    assert validate_output_filename("report") == "report.xlsx"


def test_validate_output_filename_rejects_invalid_name() -> None:
    with pytest.raises(ValueError):
        validate_output_filename("bad*name.xlsx")