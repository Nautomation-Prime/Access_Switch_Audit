"""Behavioural tests for the per-device audit."""

from __future__ import annotations

import pytest
from fake_device import (
    SHOW_INTERFACES_STATUS,
    SHOW_POWER_INLINE,
    FakeConnection,
)
from paramiko.ssh_exception import SSHException

from switch_audit import device_auditor
from switch_audit.device_auditor import (
    _get_cdp_neighbors,
    _get_lldp_neighbors,
    _poe_watts,
    audit_device,
    error_summary,
    parse_show_interfaces_status,
    parse_show_power_inline,
)

# ── Parsers ───────────────────────────────────────────────────────────────────

def test_show_interfaces_status_is_parsed_into_rows() -> None:
    rows = parse_show_interfaces_status(SHOW_INTERFACES_STATUS)
    by_port = {r["port"]: r for r in rows}

    assert set(by_port) == {"Gi1/0/1", "Gi1/0/2", "Gi1/0/3", "Gi1/0/4"}
    assert by_port["Gi1/0/1"]["vlan"] == "trunk"
    assert by_port["Gi1/0/2"]["status"] == "connected"
    assert by_port["Gi1/0/4"]["status"] == "disabled"


def test_power_inline_is_keyed_by_short_and_long_names() -> None:
    poe = parse_show_power_inline(SHOW_POWER_INLINE)

    assert poe["Gi1/0/2"]["poe_oper"] == "on"
    assert poe["GigabitEthernet1/0/2"]["poe_power_w"] == "7.0"
    assert poe["Gi1/0/1"]["poe_oper"] == "off"


def test_lldp_detail_is_parsed_without_a_device_id_field() -> None:
    """Real IOS LLDP detail output identifies neighbours by System Name."""
    conn = FakeConnection()

    neighbors = _get_lldp_neighbors(conn)

    assert neighbors["Gi1/0/1"] == ("core-sw-01", "Gi0/24")
    assert neighbors["Gi1/0/2"] == ("SEP001122334455", "1")


def test_cdp_detail_is_parsed() -> None:
    neighbors = _get_cdp_neighbors(FakeConnection())

    assert neighbors["Gi1/0/1"][0] == "core-sw-01.example.com"


@pytest.mark.parametrize(
    "raw, expected",
    [("7.0 W", 7.0), ("7.0", 7.0), ("0.0", 0.0), ("", 0.0), ("-", 0.0), (None, 0.0), ("n/a", 0.0)],
)
def test_poe_watts_handles_the_shapes_devices_emit(raw, expected) -> None:
    assert _poe_watts(raw) == expected


# ── audit_device ──────────────────────────────────────────────────────────────

def test_audit_device_builds_records_and_summary(fake_switch) -> None:
    ip, hostname, detailed, summary = audit_device(
        "192.0.2.10", "user", "pass", None, None, stale_days=30
    )

    assert ip == "192.0.2.10"
    assert hostname == "access-sw-01"
    assert summary["Audit Result"] == "OK"
    assert summary["Total Ports (phy)"] == 4
    assert summary["Model"] == "C9300-24T"
    assert summary["Serial"] == "FCW2140L0GH"
    assert summary["IOS Version"] == "IOS-XE 17.6.5a"

    by_port = {r["Interface"]: r for r in detailed}
    assert by_port["GigabitEthernet1/0/1"]["Mode"] == "trunk"
    assert by_port["GigabitEthernet1/0/2"]["Mode"] == "access"
    assert by_port["GigabitEthernet1/0/2"]["VLAN"] == "10"
    assert by_port["GigabitEthernet1/0/4"]["Status"] == "administratively down"

    # Neighbour presence must reflect the LLDP block, not only CDP.
    assert by_port["GigabitEthernet1/0/2"]["LLDP/ CDP Neighbor"] is True

    assert fake_switch[0].disconnected is True


def test_audit_device_counts_stale_error_and_poe_ports(fake_switch) -> None:
    _ip, _hostname, _detailed, summary = audit_device(
        "192.0.2.10", "user", "pass", None, None, stale_days=30
    )

    # Gi1/0/3: access, notconnect, no PoE draw, no neighbour -> stale.
    # Gi1/0/4 is administratively down: reported as 'disabled', not as stale.
    assert summary["Stale Ports"] == 1
    assert summary["Ports w/ Errors"] == 1   # Gi1/0/3 has 14 input errors
    assert summary["PoE Active Ports"] == 1  # Gi1/0/2 draws 7.0 W
    assert summary["Access Ports"] == 3
    assert summary["Trunk Ports"] == 1
    assert summary["% Connected of Total"] == 50.0


def test_summary_schema_is_identical_for_success_and_failure(fake_switch) -> None:
    """
    Mismatched keys used to produce NaN-riddled SUMMARY rows. Collector columns
    are seeded up front so a device that failed before any collector ran still
    carries the same columns in the same order.
    """
    _ip, _hostname, _detailed, ok = audit_device(
        "192.0.2.10", "user", "pass", None, None, stale_days=30
    )
    failed = error_summary("192.0.2.99", SSHException("unreachable"))

    # Underscore-prefixed keys are internal plumbing, stripped by the reporter.
    public = lambda row: [k for k in row if not k.startswith("_")]  # noqa: E731
    assert public(ok) == public(failed)
    assert failed["Audit Result"] == "ERROR"
    assert failed["Total Ports (phy)"] == 0  # numeric, so the TOTAL row still sums


def test_ssh_failures_are_retried_then_reported_not_raised(monkeypatch) -> None:
    """
    Netmiko/paramiko errors derive from SSHException, not OSError. The old
    except list missed them: they skipped the retry loop entirely and escaped
    the thread pool, discarding every already-completed device.
    """
    attempts = []

    def _always_fail(ip, username, password, **kwargs):
        attempts.append(ip)
        raise SSHException("Authentication failed")

    monkeypatch.setattr(device_auditor, "connect_to_device", _always_fail)
    monkeypatch.setattr(device_auditor, "_retry_wait", lambda attempt: 0.0)

    ip, hostname, detailed, summary = audit_device(
        "192.0.2.99", "user", "bad", None, None, stale_days=30
    )

    assert len(attempts) == device_auditor.config.retry_attempts
    assert hostname is None
    assert detailed == []
    assert summary["Audit Result"] == "ERROR"
    assert "Authentication failed" in summary["Error"]


def test_transient_failure_recovers_on_a_later_attempt(monkeypatch) -> None:
    calls = {"n": 0}

    def _flaky(ip, username, password, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise SSHException("Timed out")
        return FakeConnection()

    monkeypatch.setattr(device_auditor, "connect_to_device", _flaky)
    monkeypatch.setattr(device_auditor, "_retry_wait", lambda attempt: 0.0)

    _ip, hostname, _detailed, summary = audit_device(
        "192.0.2.10", "user", "pass", None, None, stale_days=30
    )

    assert calls["n"] == 2
    assert hostname == "access-sw-01"
    assert summary["Audit Result"] == "OK"


def test_retry_wait_backs_off_exponentially_and_is_capped() -> None:
    """base**(attempt+1) degenerated to a flat 1s at the shipped default of 1."""
    waits = [device_auditor._retry_wait(i) for i in range(8)]

    assert waits[1] == waits[0] * 2
    assert waits[2] == waits[0] * 4
    assert max(waits) <= 30.0


def test_no_interface_data_is_flagged_rather_than_reported_as_zero(monkeypatch) -> None:
    def _connect(ip, username, password, **kwargs):
        return FakeConnection(failures={"show interfaces": RuntimeError("read timeout")})

    monkeypatch.setattr(device_auditor, "connect_to_device", _connect)

    _ip, _hostname, detailed, summary = audit_device(
        "192.0.2.10", "user", "pass", None, None, stale_days=30
    )

    assert detailed == []
    assert summary["Audit Result"] == "NO DATA"
    assert summary["Error"]


def test_a_shared_jump_manager_is_not_closed_by_the_worker(fake_switch) -> None:
    class SpyJump:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    jump = SpyJump()
    audit_device(
        "192.0.2.10", "user", "pass", None, "bastion", stale_days=30, jump=jump
    )

    assert jump.closed is False, "a caller-owned bastion session must outlive one device"


def test_stale_count_agrees_with_the_stale_unused_category(fake_switch) -> None:
    """The summary total and the per-port label are two views of one fact."""
    _ip, _hostname, detailed, summary = audit_device(
        "192.0.2.10", "user", "pass", None, None, stale_days=30
    )

    flagged = sum(
        1
        for row in detailed
        for key, value in row.items()
        if key.startswith("Stale (") and value is True
    )
    categorised = sum(1 for row in detailed if row.get("Port Category") == "stale-unused")

    assert summary["Stale Ports"] == flagged == categorised
