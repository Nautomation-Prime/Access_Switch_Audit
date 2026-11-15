"""Tests for workbook generation."""

from __future__ import annotations

import openpyxl

from switch_audit.device_auditor import base_summary, error_summary
from switch_audit.excel_reporter import ExcelReporter


def _device(name: str, ip: str, total: int, access: int, connected: int):
    summary = base_summary(name, ip, Model="C9300-24T", Serial=f"SER{total}")
    summary.update({
        "Total Ports (phy)": total,
        "Access Ports": access,
        "Connected": connected,
        "% Access of Total": round(access / total * 100, 1),
        "% Connected of Total": round(connected / total * 100, 1),
    })
    rows = [{"Device": name, "Interface": f"Gi1/0/{i}", "Status": "connected"} for i in range(1, total + 1)]
    return (ip, name, rows, summary)


def _read(path):
    wb = openpyxl.load_workbook(path)
    return wb


def test_summary_total_row_sums_counts_and_recomputes_percentages(tmp_path) -> None:
    """
    Percentages are derived, not additive. Summing them down the column, as the
    previous implementation did, produced nonsense like 160% access ports.
    """
    results = [
        _device("sw-a", "192.0.2.1", total=10, access=8, connected=5),
        _device("sw-b", "192.0.2.2", total=30, access=24, connected=9),
    ]
    out = tmp_path / "audit.xlsx"

    ExcelReporter().save_to_excel(results, str(out))

    ws = _read(out)["SUMMARY"]
    headers = [c.value for c in ws[1]]
    total_row = dict(zip(headers, [c.value for c in ws[ws.max_row]], strict=True))

    assert total_row["Device"] == "TOTAL"
    assert total_row["Total Ports (phy)"] == 40
    assert total_row["Access Ports"] == 32
    # 32/40, not 80.0 + 80.0
    assert total_row["% Access of Total"] == 80.0
    assert total_row["% Connected of Total"] == 35.0
    # Text columns are blanked on the TOTAL row (openpyxl reads "" back as None).
    assert total_row["Serial"] in ("", None)


def test_failed_devices_keep_a_numeric_port_column(tmp_path) -> None:
    """An 'ERROR' string in a count column made the whole column non-numeric."""
    results = [
        _device("sw-a", "192.0.2.1", total=10, access=8, connected=5),
        ("192.0.2.9", None, [], error_summary("192.0.2.9", "Authentication failed")),
    ]
    out = tmp_path / "audit.xlsx"

    ExcelReporter().save_to_excel(results, str(out))

    ws = _read(out)["SUMMARY"]
    headers = [c.value for c in ws[1]]
    total_row = dict(zip(headers, [c.value for c in ws[ws.max_row]], strict=True))

    assert total_row["Total Ports (phy)"] == 10
    assert total_row["Audit Result"] == "1 FAILED"


def test_failed_device_gets_a_sheet_explaining_why(tmp_path) -> None:
    results = [("192.0.2.9", None, [], error_summary("192.0.2.9", "Authentication failed"))]
    out = tmp_path / "audit.xlsx"

    ExcelReporter().save_to_excel(results, str(out))

    wb = _read(out)
    assert "192_0_2_9" in wb.sheetnames
    ws = wb["192_0_2_9"]
    values = [c.value for c in ws[2]]
    assert "Authentication failed" in values


def test_a_device_named_summary_does_not_collide_with_the_summary_sheet(tmp_path) -> None:
    results = [_device("SUMMARY", "192.0.2.1", total=4, access=4, connected=4)]
    out = tmp_path / "audit.xlsx"

    ExcelReporter().save_to_excel(results, str(out))

    wb = _read(out)
    assert "SUMMARY" in wb.sheetnames
    # The device sheet must exist alongside it, under a de-duplicated name.
    assert len(wb.sheetnames) == 2


def test_a_locked_target_falls_back_to_a_timestamped_file(tmp_path, monkeypatch) -> None:
    """Losing an entire audit because the workbook is open in Excel is not acceptable."""
    results = [_device("sw-a", "192.0.2.1", total=4, access=4, connected=4)]
    out = tmp_path / "audit.xlsx"
    reporter = ExcelReporter()
    real_write = reporter._write_workbook
    calls = {"n": 0}

    def _write(res, path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(13, "being used by another process")
        return real_write(res, path)

    monkeypatch.setattr(reporter, "_write_workbook", _write)

    written = reporter.save_to_excel(results, str(out))

    assert written != out
    assert written.exists()
    assert written.name.startswith("audit_") and written.suffix == ".xlsx"


def test_output_directory_is_created(tmp_path) -> None:
    out = tmp_path / "nested" / "dir" / "audit.xlsx"

    written = ExcelReporter().save_to_excel(
        [_device("sw-a", "192.0.2.1", total=2, access=2, connected=1)], str(out)
    )

    assert written.exists()


def test_empty_result_set_still_produces_a_workbook(tmp_path) -> None:
    out = tmp_path / "audit.xlsx"

    ExcelReporter().save_to_excel([], str(out))

    assert "SUMMARY" in _read(out).sheetnames


def test_total_row_takes_the_worst_case_for_peak_columns(tmp_path) -> None:
    """Summing inlet temperatures is meaningless; the hottest switch is not."""
    def _dev(name, ip, temp, poe_used, poe_avail):
        summary = base_summary(name, ip)
        summary.update({
            "Total Ports (phy)": 4, "Access Ports": 4, "Connected": 2,
            "Inlet Temp (C)": temp,
            "PoE Used (W)": poe_used, "PoE Available (W)": poe_avail,
            "PoE Used %": round(poe_used / poe_avail * 100, 1),
        })
        return (ip, name, [{"Interface": "Gi1/0/1"}], summary)

    results = [
        _dev("sw-a", "192.0.2.1", 26.0, 100.0, 740.0),
        _dev("sw-b", "192.0.2.2", 41.0, 500.0, 740.0),
    ]
    out = tmp_path / "audit.xlsx"

    ExcelReporter().save_to_excel(results, str(out))

    ws = _read(out)["SUMMARY"]
    headers = [c.value for c in ws[1]]
    total = dict(zip(headers, [c.value for c in ws[ws.max_row]], strict=True))

    assert total["Inlet Temp (C)"] == 41.0          # hottest, not 67.0
    assert total["PoE Used (W)"] == 600.0           # watts do add up
    assert total["PoE Used %"] == 40.5              # 600/1480, not 13.5+67.6
