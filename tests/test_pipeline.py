"""End-to-end tests: argv in, workbook out."""

from __future__ import annotations

import openpyxl
import pytest
from fake_device import FakeConnection
from paramiko.ssh_exception import SSHException

from switch_audit import device_auditor
from switch_audit.cli import main


@pytest.fixture
def run_dir(tmp_path, monkeypatch):
    """Run the CLI from a scratch directory so logs/ and output/ stay out of the repo."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SWITCH_USER", "auditor")
    monkeypatch.setenv("SWITCH_PASS", "s3cret")
    monkeypatch.delenv("USE_ENABLE", raising=False)
    return tmp_path


def _devices_file(path, *ips: str):
    target = path / "devices.txt"
    target.write_text("# inventory\n" + "\n".join(ips) + "\n", encoding="utf-8")
    return str(target)


def test_full_run_writes_summary_and_device_sheets(run_dir, fake_switch) -> None:
    devices = _devices_file(run_dir, "192.0.2.10", "192.0.2.11")
    out = run_dir / "report.xlsx"

    code = main([
        "--devices", devices, "--output", str(out),
        "--direct", "--non-interactive", "--workers", "2",
    ])

    assert code == 0
    wb = openpyxl.load_workbook(out)
    assert wb.sheetnames[0] == "SUMMARY"
    # SUMMARY + INVENTORY (from the inventory collector) + two device sheets

    ws = wb["SUMMARY"]
    headers = [c.value for c in ws[1]]
    rows = [dict(zip(headers, [c.value for c in r], strict=True)) for r in ws.iter_rows(min_row=2)]
    # Both fake devices report the same hostname; the Mgmt IP distinguishes
    # them in SUMMARY and the sheet name is de-duplicated.
    assert [r["Device"] for r in rows] == ["access-sw-01", "access-sw-01", "TOTAL"]
    assert sorted(r["Mgmt IP"] for r in rows[:-1]) == ["192.0.2.10", "192.0.2.11"]
    # INVENTORY comes from the inventory collector; it aggregates all devices.
    assert sorted(wb.sheetnames) == ["INVENTORY", "SUMMARY", "access-sw-01", "access-sw-01-2"]
    assert rows[-1]["Total Ports (phy)"] == 8
    assert all(r["Audit Result"] == "OK" for r in rows[:-1])


def test_one_unreachable_device_does_not_sink_the_run(run_dir, monkeypatch) -> None:
    """
    The regression that mattered most: paramiko's SSHException is not an
    OSError, so it escaped the pool's result handling and aborted the whole
    audit, discarding every device that had already succeeded.
    """
    def _connect(ip, username, password, **kwargs):
        if ip.endswith(".99"):
            raise SSHException("Authentication failed")
        return FakeConnection()

    monkeypatch.setattr(device_auditor, "connect_to_device", _connect)
    monkeypatch.setattr(device_auditor, "_retry_wait", lambda attempt: 0.0)

    devices = _devices_file(run_dir, "192.0.2.10", "192.0.2.99", "192.0.2.11")
    out = run_dir / "report.xlsx"

    code = main([
        "--devices", devices, "--output", str(out),
        "--direct", "--non-interactive", "--workers", "3",
    ])

    assert code == 0, "a partial success is still a success"
    wb = openpyxl.load_workbook(out)
    ws = wb["SUMMARY"]
    headers = [c.value for c in ws[1]]
    rows = [dict(zip(headers, [c.value for c in r], strict=True)) for r in ws.iter_rows(min_row=2)]

    # All three devices are represented, and the failure is named.
    assert len(rows) == 4  # 3 devices + TOTAL
    failed = [r for r in rows if r["Audit Result"] == "ERROR"]
    assert len(failed) == 1
    assert failed[0]["Mgmt IP"] == "192.0.2.99"
    assert "Authentication failed" in failed[0]["Error"]
    assert rows[-1]["Audit Result"] == "1 FAILED"


def test_run_where_every_device_fails_exits_non_zero(run_dir, monkeypatch) -> None:
    def _connect(ip, username, password, **kwargs):
        raise SSHException("Authentication failed")

    monkeypatch.setattr(device_auditor, "connect_to_device", _connect)
    monkeypatch.setattr(device_auditor, "_retry_wait", lambda attempt: 0.0)

    devices = _devices_file(run_dir, "192.0.2.10")
    code = main([
        "--devices", devices, "--output", str(run_dir / "r.xlsx"),
        "--direct", "--non-interactive",
    ])

    assert code == 1


def test_missing_devices_file_is_a_clean_failure(run_dir) -> None:
    code = main([
        "--devices", str(run_dir / "nope.txt"), "--direct", "--non-interactive",
    ])

    assert code == 1


def test_invalid_worker_count_is_rejected(run_dir) -> None:
    devices = _devices_file(run_dir, "192.0.2.10")

    assert main(["--devices", devices, "--workers", "0", "--direct", "--non-interactive"]) == 1


def test_non_interactive_without_credentials_fails_instead_of_prompting(
    run_dir, monkeypatch, fake_switch
) -> None:
    """A scheduled run must never block on stdin."""
    monkeypatch.delenv("SWITCH_USER")
    monkeypatch.delenv("SWITCH_PASS")
    monkeypatch.setattr(
        "switch_audit.credentials._credentials_from_win_cred", lambda target: None
    )

    def _no_input(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("prompted for input in non-interactive mode")

    monkeypatch.setattr("builtins.input", _no_input)

    devices = _devices_file(run_dir, "192.0.2.10")
    assert main(["--devices", devices, "--direct", "--non-interactive"]) == 1


def test_jump_host_is_opened_once_for_the_whole_run(run_dir, fake_switch, monkeypatch) -> None:
    connects = []

    class SpyJump:
        def __init__(self, host, user, password, **kwargs):
            self.host = host

        def connect(self):
            connects.append(self.host)

        def close(self):
            pass

    monkeypatch.setattr("switch_audit.cli.JumpManager", SpyJump)

    devices = _devices_file(run_dir, "192.0.2.10", "192.0.2.11", "192.0.2.12")
    code = main([
        "--devices", devices, "--output", str(run_dir / "r.xlsx"),
        "--jump-host", "bastion.example.com", "--non-interactive", "--workers", "3",
    ])

    assert code == 0
    assert connects == ["bastion.example.com"], "one bastion login, not one per device"


def test_unreachable_jump_host_stops_before_dialling_devices(run_dir, monkeypatch) -> None:
    class DeadJump:
        def __init__(self, *args, **kwargs):
            pass

        def connect(self):
            raise SSHException("bastion unreachable")

    monkeypatch.setattr("switch_audit.cli.JumpManager", DeadJump)

    def _must_not_connect(*args, **kwargs):  # pragma: no cover
        raise AssertionError("dialled a device despite an unreachable bastion")

    monkeypatch.setattr(device_auditor, "connect_to_device", _must_not_connect)

    devices = _devices_file(run_dir, "192.0.2.10")
    assert main(["--devices", devices, "--jump-host", "bastion", "--non-interactive"]) == 1


def test_direct_overrides_a_configured_jump_host(run_dir, fake_switch, monkeypatch) -> None:
    def _must_not_build(*args, **kwargs):  # pragma: no cover
        raise AssertionError("built a JumpManager despite --direct")

    monkeypatch.setattr("switch_audit.cli.JumpManager", _must_not_build)

    devices = _devices_file(run_dir, "192.0.2.10")
    assert main([
        "--devices", devices, "--output", str(run_dir / "r.xlsx"),
        "--direct", "--jump-host", "bastion", "--non-interactive",
    ]) == 0


def test_a_run_with_every_collector_disabled_still_produces_a_report(
    run_dir, fake_switch, monkeypatch
) -> None:
    """The collectors are optional; turning them all off must not break anything."""
    from switch_audit import collectors

    monkeypatch.setattr(
        type(collectors.config), "collector_enabled",
        lambda self, name, default=True: False,
    )

    devices = _devices_file(run_dir, "192.0.2.10")
    out = run_dir / "report.xlsx"

    assert main([
        "--devices", devices, "--output", str(out), "--direct", "--non-interactive",
    ]) == 0

    wb = openpyxl.load_workbook(out)
    assert wb.sheetnames == ["SUMMARY", "access-sw-01"]  # no INVENTORY sheet
    headers = [c.value for c in wb["SUMMARY"][1]]
    assert "Uptime" not in headers
    assert "PoE Available (W)" not in headers
    # The core audit columns survive.
    assert "Total Ports (phy)" in headers and headers[-1] == "Error"


def test_collector_columns_survive_the_round_trip_to_excel(run_dir, fake_switch) -> None:
    devices = _devices_file(run_dir, "192.0.2.10")
    out = run_dir / "report.xlsx"

    assert main([
        "--devices", devices, "--output", str(out), "--direct", "--non-interactive",
    ]) == 0

    wb = openpyxl.load_workbook(out)
    ws = wb["access-sw-01"]
    headers = [c.value for c in ws[1]]
    rows = [dict(zip(headers, [c.value for c in r], strict=True)) for r in ws.iter_rows(min_row=2)]
    by_port = {r["Interface"]: r for r in rows}

    # VLAN ids stay text, consistent with the VLAN column which also holds "trunk".
    assert by_port["GigabitEthernet1/0/2"]["Voice VLAN"] == "110"
    assert by_port["GigabitEthernet1/0/2"]["VLAN Name"] == "USER-DATA"
    assert by_port["GigabitEthernet1/0/3"]["MAC Count"] == 0
    assert "INVENTORY" in wb.sheetnames
