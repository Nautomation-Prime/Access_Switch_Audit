"""Tests for the optional enrichment collectors."""

from __future__ import annotations

import pytest
from fake_device import FakeConnection

from switch_audit import collectors, device_auditor
from switch_audit.collectors import (
    Collector,
    CollectorOutput,
    collect_dot1x,
    collect_environment,
    collect_etherchannel,
    collect_inventory,
    collect_mac_table,
    collect_poe_budget,
    collect_port_security,
    collect_spanning_tree,
    collect_switchport,
    collect_transceivers,
    collect_version_detail,
    collect_vlans,
    run_collectors,
)
from switch_audit.oui import build_lookup, load_oui_file, mac_to_oui, vendor_for

# ── Per-interface collectors ─────────────────────────────────────────────────

def test_switchport_reports_access_and_voice_vlans() -> None:
    """Voice VLAN is invisible in 'show interfaces status', which is all we had."""
    out = collect_switchport(FakeConnection())

    assert out.interfaces["Gi1/0/2"]["Access VLAN"] == "10"
    assert out.interfaces["Gi1/0/2"]["Voice VLAN"] == "110"
    assert out.interfaces["Gi1/0/2"]["Admin Mode"] == "static access"
    # "none" is noise, not a VLAN.
    assert out.interfaces["Gi1/0/1"]["Voice VLAN"] == ""
    assert out.interfaces["Gi1/0/1"]["Native VLAN"] == "99"
    assert out.interfaces["Gi1/0/1"]["Trunk VLANs"] == "10,20,30"


def test_mac_table_counts_per_port_and_skips_cpu_entries() -> None:
    out = collect_mac_table(FakeConnection())

    assert out.interfaces["Gi1/0/1"]["MAC Count"] == 2
    assert out.interfaces["Gi1/0/2"]["MAC Count"] == 2
    assert "Gi1/0/3" not in out.interfaces
    # The CPU pseudo-port must not become a row.
    assert not any(p.lower().startswith("cpu") for p in out.interfaces)
    assert out.interfaces["Gi1/0/1"]["MAC VLANs"] == "10,20"


def test_mac_table_resolves_known_vendors() -> None:
    out = collect_mac_table(FakeConnection())

    assert "VMware" in out.interfaces["Gi1/0/2"]["MAC Vendors"]


def test_mac_list_is_truncated_to_the_configured_limit(monkeypatch) -> None:
    monkeypatch.setattr(type(collectors.config), "mac_addresses_per_port", property(lambda self: 1))

    out = collect_mac_table(FakeConnection())

    assert out.interfaces["Gi1/0/1"]["MAC Addresses"].endswith("...")
    assert out.interfaces["Gi1/0/1"]["MAC Count"] == 2  # count is not truncated


def test_vlans_provide_names_and_counts() -> None:
    out = collect_vlans(FakeConnection())

    assert out.device["VLANs Defined"] == 5
    assert out.device["VLANs Active"] == 4  # one is suspended
    assert out.device["_vlan_names"]["10"] == "USER-DATA"


def test_spanning_tree_detects_portfast_edge_ports() -> None:
    out = collect_spanning_tree(FakeConnection())

    assert out.interfaces["Gi1/0/2"]["STP Edge"] is True
    assert out.interfaces["Gi1/0/1"]["STP Edge"] is False
    assert out.interfaces["Gi1/0/1"]["STP Role"] == "Root"


def test_transceivers_report_optical_levels() -> None:
    out = collect_transceivers(FakeConnection())

    assert out.interfaces["Gi1/0/1"]["Optic Rx (dBm)"] == -5.8
    assert out.interfaces["Gi1/0/1"]["Optic Temp (C)"] == 34.5


def test_dot1x_sessions_are_reported() -> None:
    out = collect_dot1x(FakeConnection())

    assert out.interfaces["Gi1/0/2"]["Auth Method"] == "dot1x"
    assert out.interfaces["Gi1/0/2"]["Auth Status"] == "Authorized"
    assert out.device["Authenticated Ports"] == 1


def test_etherchannel_maps_members_to_their_bundle() -> None:
    out = collect_etherchannel(FakeConnection())

    assert out.interfaces["Gi1/0/1"]["Port-Channel"] == "Po1"
    assert out.interfaces["Gi1/0/1"]["PortChannel Member State"] == "P"
    assert out.device["Port-Channels"] == 1


def test_port_security_table_is_parsed() -> None:
    out = collect_port_security(FakeConnection())

    assert out.interfaces["Gi1/0/2"]["PortSec Max"] == 2
    assert out.interfaces["Gi1/0/3"]["PortSec Violations"] == 4
    assert out.interfaces["Gi1/0/3"]["PortSec Action"] == "Restrict"


# ── Device-level collectors ──────────────────────────────────────────────────

def test_version_detail_captures_uptime_and_config_register() -> None:
    out = collect_version_detail(FakeConnection())

    assert out.device["Uptime"].startswith("41 weeks")
    assert out.device["Config Register"] == "0x2102"
    assert out.device["Config Register OK"] is True
    assert out.device["Reload Reason"] == "Reload Command"


def test_a_non_standard_config_register_is_flagged(monkeypatch) -> None:
    from fake_device import SHOW_VERSION_TEXTFSM

    altered = [dict(SHOW_VERSION_TEXTFSM[0], config_register="0x2142")]
    conn = FakeConnection()
    monkeypatch.setitem(conn.STRUCTURED, "show version", lambda: altered)

    out = collect_version_detail(conn)

    assert out.device["Config Register OK"] is False


def test_inventory_becomes_its_own_sheet_and_counts_optics() -> None:
    out = collect_inventory(FakeConnection())

    assert out.device["Inventory Items"] == 2
    assert out.device["Optics Installed"] == 1
    rows = out.sheets["INVENTORY"]
    assert rows[1]["Serial"] == "AVD2035A1BC"


def test_environment_summarises_psu_and_temperature() -> None:
    out = collect_environment(FakeConnection())

    assert out.device["PSUs Present"] == 2
    assert out.device["PSUs OK"] == 1  # the second is "Not Present"
    assert out.device["Inlet Temp (C)"] == 26.0
    assert out.device["Temp Status"] == "GREEN"


def test_poe_budget_is_read_from_the_show_power_inline_header() -> None:
    out = collect_poe_budget(FakeConnection())

    assert out.device["PoE Available (W)"] == 740.0
    assert out.device["PoE Used (W)"] == 132.4
    assert out.device["PoE Remaining (W)"] == 607.6
    assert out.device["PoE Used %"] == 17.9


# ── Robustness ───────────────────────────────────────────────────────────────

def test_a_command_the_platform_rejects_yields_nothing() -> None:
    """An older or non-PoE switch answers '% Invalid input'; that is not an error."""
    conn = FakeConnection(unsupported={"show interfaces transceiver", "show power inline"})

    assert collect_transceivers(conn).interfaces == {}
    assert collect_poe_budget(conn).device == {}


def test_a_command_that_raises_does_not_propagate() -> None:
    conn = FakeConnection(failures={"show vlan": RuntimeError("read timeout")})

    assert collect_vlans(conn).device == {}


def test_a_broken_collector_does_not_sink_the_device(caplog) -> None:
    def _explode(conn):
        raise RuntimeError("bad parser")

    good = Collector("good", "", True, (), lambda c: CollectorOutput(device={"ok": 1}))
    bad = Collector("bad", "", True, (), _explode)

    merged = run_collectors(FakeConnection(), [bad, good])

    assert merged.device == {"ok": 1}


def test_run_collectors_merges_per_port_fields() -> None:
    merged = run_collectors(FakeConnection())

    port = merged.interfaces["Gi1/0/2"]
    assert port["Voice VLAN"] == "110"      # switchport
    assert port["MAC Count"] == 2           # mac_table
    assert port["STP Edge"] is True         # spanning_tree
    assert merged.device["Uptime"]          # version_detail
    assert "INVENTORY" in merged.sheets     # inventory


def test_disabled_collectors_do_not_run(monkeypatch) -> None:
    monkeypatch.setattr(
        type(collectors.config), "collector_enabled",
        lambda self, name, default=True: name == "vlans",
    )

    names = [c.name for c in collectors.enabled_collectors()]

    assert names == ["vlans"]


def test_enabled_device_columns_follow_the_enabled_set(monkeypatch) -> None:
    monkeypatch.setattr(
        type(collectors.config), "collector_enabled",
        lambda self, name, default=True: name == "poe_budget",
    )

    assert collectors.enabled_device_columns() == (
        "PoE Available (W)", "PoE Used (W)", "PoE Remaining (W)", "PoE Used %",
    )


# ── OUI lookup ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "mac, expected",
    [
        ("0050.5611.2233", "005056"),
        ("00:50:56:11:22:33", "005056"),
        ("00-50-56-11-22-33", "005056"),
        ("bad", ""),
        ("", ""),
    ],
)
def test_mac_to_oui_accepts_every_notation(mac, expected) -> None:
    assert mac_to_oui(mac) == expected


def test_vendor_lookup_falls_back_to_blank() -> None:
    assert vendor_for("0050.5611.2233") == "VMware"
    assert vendor_for("FFFF.FFFF.FFFF") == ""


def test_a_user_supplied_oui_file_extends_and_overrides(tmp_path) -> None:
    path = tmp_path / "oui.csv"
    path.write_text(
        "# oui,vendor\n"
        "AABBCC,Acme Widgets\n"
        "005056,Our VM Platform\n"
        "bad-row\n",
        encoding="utf-8",
    )

    table = build_lookup(path)

    assert vendor_for("AABB.CCDD.EEFF", table) == "Acme Widgets"
    assert vendor_for("0050.5611.2233", table) == "Our VM Platform"


def test_a_missing_oui_file_is_not_fatal(tmp_path) -> None:
    assert load_oui_file(tmp_path / "nope.csv") == {}


# ── Integration with the audit ───────────────────────────────────────────────

def test_collector_columns_reach_the_interface_records(fake_switch) -> None:
    _ip, _hostname, detailed, summary = device_auditor.audit_device(
        "192.0.2.10", "user", "pass", None, None, stale_days=30
    )
    by_port = {r["Interface"]: r for r in detailed}

    row = by_port["GigabitEthernet1/0/2"]
    assert row["Voice VLAN"] == "110"
    assert row["VLAN Name"] == "USER-DATA"
    assert row["MAC Count"] == 2
    assert row["STP Edge"] is True

    assert summary["PoE Available (W)"] == 740.0
    assert summary["VLANs Defined"] == 5
    assert summary["Uptime"].startswith("41 weeks")
    assert summary["_extra_sheets"]["INVENTORY"][0]["Mgmt IP"] == "192.0.2.10"


def test_every_interface_row_carries_the_same_columns(fake_switch) -> None:
    """Ports with no collector data still get the columns, so filters work."""
    _ip, _hostname, detailed, _summary = device_auditor.audit_device(
        "192.0.2.10", "user", "pass", None, None, stale_days=30
    )

    keysets = {tuple(row.keys()) for row in detailed}
    assert len(keysets) == 1


def test_learned_macs_stop_a_port_being_called_stale(fake_switch, monkeypatch) -> None:
    """
    A printer that speaks no CDP/LLDP and draws no PoE looked abandoned. Its
    MAC in the CAM table proves otherwise.
    """
    from fake_device import SHOW_MAC_ADDRESS_TABLE

    with_printer = SHOW_MAC_ADDRESS_TABLE + [
        {"destination_address": "0080.7712.3456", "type": "DYNAMIC",
         "vlan_id": "20", "destination_port": ["Gi1/0/3"]},
    ]
    monkeypatch.setattr(
        FakeConnection, "STRUCTURED",
        {**FakeConnection.STRUCTURED, "show mac address-table": lambda: with_printer},
    )

    _ip, _hostname, detailed, summary = device_auditor.audit_device(
        "192.0.2.10", "user", "pass", None, None, stale_days=30
    )
    by_port = {r["Interface"]: r for r in detailed}

    assert by_port["GigabitEthernet1/0/3"]["MAC Count"] == 1
    assert summary["Stale Ports"] == 0


def test_the_mac_check_can_be_switched_off(fake_switch, monkeypatch) -> None:
    monkeypatch.setattr(
        type(device_auditor.config), "stale_use_mac_table", property(lambda self: False)
    )
    from fake_device import SHOW_MAC_ADDRESS_TABLE

    with_printer = SHOW_MAC_ADDRESS_TABLE + [
        {"destination_address": "0080.7712.3456", "type": "DYNAMIC",
         "vlan_id": "20", "destination_port": ["Gi1/0/3"]},
    ]
    monkeypatch.setattr(
        FakeConnection, "STRUCTURED",
        {**FakeConnection.STRUCTURED, "show mac address-table": lambda: with_printer},
    )

    _ip, _hostname, _detailed, summary = device_auditor.audit_device(
        "192.0.2.10", "user", "pass", None, None, stale_days=30
    )

    assert summary["Stale Ports"] == 1


def test_admin_down_ports_can_be_counted_as_stale_when_configured(
    fake_switch, monkeypatch
) -> None:
    monkeypatch.setattr(
        type(device_auditor.config), "stale_include_admin_down", property(lambda self: True)
    )

    _ip, _hostname, _detailed, summary = device_auditor.audit_device(
        "192.0.2.10", "user", "pass", None, None, stale_days=30
    )

    # Gi1/0/3 (idle) plus Gi1/0/4 (administratively down).
    assert summary["Stale Ports"] == 2
