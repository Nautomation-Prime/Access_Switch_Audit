from __future__ import annotations

import openpyxl

from switch_audit.formatters import format_worksheet, sheet_name_from, unique_sheet_name


def test_sheet_name_from_sanitizes_invalid_characters() -> None:
    sheet_name = sheet_name_from("branch/core:switch?01", "fallback")

    assert sheet_name == "branch_core_switch_01"
    assert len(sheet_name) <= 31


def test_unique_sheet_name_is_case_insensitive() -> None:
    existing = {"SUMMARY"}

    assert unique_sheet_name("summary", existing) == "summary-2"
    assert "summary-2" in existing


def _sheet(headers, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(headers)
    for row in rows:
        ws.append(row)
    return ws


def test_format_worksheet_sets_filters_and_freeze() -> None:
    ws = _sheet(["Device", "Status"], [["sw-a", "connected"]])

    format_worksheet(ws)

    assert ws.freeze_panes == "A2"
    assert ws.auto_filter.ref == "A1:B2"


def test_new_signal_columns_get_conditional_formatting() -> None:
    """Violations, PoE headroom, PortFast and empty ports should stand out."""
    headers = ["Status", "PortSec Violations", "PoE Used %", "STP Edge", "MAC Count"]
    ws = _sheet(headers, [["connected", 0, 42.0, True, 3]])

    format_worksheet(ws)

    formatted = {str(rng) for rng in ws.conditional_formatting}
    # One rule range per formatted column, plus the Status column.
    assert len(formatted) == len(headers)


def test_formatting_an_empty_sheet_is_a_no_op() -> None:
    wb = openpyxl.Workbook()
    format_worksheet(wb.active)  # must not raise
