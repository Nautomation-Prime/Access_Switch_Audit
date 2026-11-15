"""Optional data collectors that enrich the audit.

Each collector runs one or more ``show`` commands, parses the result, and
contributes any of three things:

``interfaces``
    Extra columns on the per-device interface sheet, keyed by short interface
    name (``Gi1/0/1``).
``device``
    Extra columns on the SUMMARY row for the device.
``sheets``
    Whole extra worksheets (used by the inventory collector).

Collectors are individually switchable from ``config.yaml`` under
``collectors:``, so an operator can trade audit depth against runtime. Every
collector is best-effort: a platform that does not support the command (a
non-PoE switch, a device without transceivers) simply contributes nothing, and
never fails the device.

Parsing prefers the TextFSM templates that ship with Netmiko (``ntc-templates``)
and falls back to a purpose-written parser only where no template exists.

References: Cisco Catalyst 9300 Command Reference (Interface and Hardware
Commands, VLAN Commands) and the ntc-templates ``cisco_ios_*`` definitions.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .app_config import config
from .formatters import normalize_ifname
from .oui import build_lookup, vendor_for

log = logging.getLogger(__name__)


@dataclass
class CollectorOutput:
    """What a single collector contributed."""

    interfaces: dict[str, dict[str, Any]] = field(default_factory=dict)
    device: dict[str, Any] = field(default_factory=dict)
    sheets: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


@dataclass(frozen=True)
class Collector:
    """A named, switchable enrichment step."""

    name: str
    description: str
    default: bool
    columns: tuple[str, ...]
    run: Callable[[Any], CollectorOutput]
    device_columns: tuple[str, ...] = ()
    #: Per-port columns that mean "none found" rather than "not collected", so
    #: they seed to 0 instead of blank when the collector ran.
    zero_columns: tuple[str, ...] = ()


# ── Low-level helpers ────────────────────────────────────────────────────────

def _send(conn, command: str, use_textfsm: bool = False) -> Any:
    """Run a show command, returning '' (or []) if the device rejects it."""
    try:
        return conn.send_command(
            command, read_timeout=config.read_timeout, use_textfsm=use_textfsm
        )
    except Exception as exc:  # noqa: BLE001 - every collector is best-effort
        log.debug("%r failed: %s", command, exc)
        return [] if use_textfsm else ""


def _rows(conn, command: str) -> list[dict[str, Any]]:
    """Run a command through TextFSM, returning [] when it cannot be parsed."""
    out = _send(conn, command, use_textfsm=True)
    if isinstance(out, list) and out and isinstance(out[0], dict):
        return out
    if out:
        log.debug("%r returned output but no TextFSM template matched", command)
    return []


def _first(value: Any) -> str:
    """TextFSM List values arrive as lists; take the first meaningful entry."""
    if isinstance(value, list):
        for item in value:
            if str(item).strip():
                return str(item).strip()
        return ""
    return str(value or "").strip()


def _as_list(value: Any) -> list[str]:
    """Normalise a TextFSM value into a list of non-empty strings."""
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _short(ifname: str) -> str:
    """Short canonical interface name, or '' when unparsable."""
    return normalize_ifname(_first(ifname))[0]


def _to_float(value: Any) -> float | None:
    try:
        return float(str(value).strip().split()[0])
    except (ValueError, IndexError, AttributeError):
        return None


# ── Per-interface collectors ─────────────────────────────────────────────────

def collect_switchport(conn) -> CollectorOutput:
    """
    ``show interfaces switchport`` - access, voice and native VLAN per port.

    The voice VLAN is the single most useful field missing from the original
    audit: on an access switch most user ports carry a phone on a voice VLAN,
    and ``show interfaces status`` reports only the data VLAN.
    """
    out = CollectorOutput()
    for row in _rows(conn, "show interfaces switchport"):
        port = _short(row.get("interface"))
        if not port:
            continue
        access_vlan = _first(row.get("access_vlan"))
        voice_vlan = _first(row.get("voice_vlan"))
        out.interfaces[port] = {
            "Admin Mode": _first(row.get("admin_mode")),
            "Access VLAN": "" if access_vlan.lower() in {"none", "n/a"} else access_vlan,
            "Voice VLAN": "" if voice_vlan.lower() in {"none", "n/a"} else voice_vlan,
            "Native VLAN": _first(row.get("native_vlan")),
            "Trunk VLANs": ",".join(_as_list(row.get("trunking_vlans")))[:200],
            "Negotiation": _first(row.get("switchport_negotiation")),
        }
    return out


def collect_mac_table(conn) -> CollectorOutput:
    """
    ``show mac address-table`` - what is actually learned on each port.

    This is the strongest evidence of occupancy available: a port with learned
    MACs is in use even when the endpoint speaks neither CDP nor LLDP and draws
    no PoE (printers, IoT, unmanaged switches). Feeds stale detection.
    """
    out = CollectorOutput()
    oui_table = build_lookup(config.oui_file)
    per_port: dict[str, list[tuple[str, str]]] = {}

    for row in _rows(conn, "show mac address-table"):
        mac = _first(row.get("destination_address"))
        vlan = _first(row.get("vlan_id"))
        for raw_port in _as_list(row.get("destination_port")):
            port = _short(raw_port)
            # CPU / drop / router entries are not physical ports.
            if not port or port.lower().startswith(("cpu", "switch", "drop", "router")):
                continue
            per_port.setdefault(port, []).append((mac, vlan))

    limit = config.mac_addresses_per_port
    for port, entries in per_port.items():
        macs = [m for m, _ in entries]
        vendors = []
        for mac in macs[:limit]:
            vendor = vendor_for(mac, oui_table)
            if vendor and vendor not in vendors:
                vendors.append(vendor)
        out.interfaces[port] = {
            "MAC Count": len(macs),
            "MAC Addresses": ", ".join(macs[:limit]) + (" ..." if len(macs) > limit else ""),
            "MAC Vendors": ", ".join(vendors),
            "MAC VLANs": ",".join(sorted({v for _, v in entries if v})),
        }
    return out


def collect_vlans(conn) -> CollectorOutput:
    """``show vlan`` - VLAN names, so the VLAN column reads as more than a number."""
    out = CollectorOutput()
    names: dict[str, str] = {}
    active = 0
    for row in _rows(conn, "show vlan"):
        vlan_id = _first(row.get("vlan_id"))
        if not vlan_id:
            continue
        names[vlan_id] = _first(row.get("vlan_name"))
        if _first(row.get("status")).lower() == "active":
            active += 1
    if names:
        out.device["VLANs Defined"] = len(names)
        out.device["VLANs Active"] = active
        # Consumed by the auditor to annotate each interface row.
        out.device["_vlan_names"] = names
    return out


def collect_spanning_tree(conn) -> CollectorOutput:
    """
    ``show spanning-tree`` - port role/state, and whether the port is an edge port.

    An access port that is not an edge (PortFast) port takes ~30s of listening
    and learning before it forwards, which users experience as "the network is
    broken". Auditing for it is a classic access-layer compliance check.
    """
    out = CollectorOutput()
    for row in _rows(conn, "show spanning-tree"):
        port = _short(row.get("interface"))
        if not port:
            continue
        stp_type = _first(row.get("type"))
        entry = out.interfaces.setdefault(port, {})
        entry["STP Role"] = _first(row.get("role"))
        entry["STP State"] = _first(row.get("status"))
        entry["STP Type"] = stp_type
        # IOS renders PortFast ports as "P2p Edge" / "Edge P2p".
        entry["STP Edge"] = "edge" in stp_type.lower()
    return out


def collect_dot1x(conn) -> CollectorOutput:
    """
    ``show authentication sessions`` - 802.1X / MAB state per port.

    Tells you which access ports are actually protected by identity-based
    access control and which are open.
    """
    out = CollectorOutput()
    rows = _rows(conn, "show authentication sessions")
    for row in rows:
        port = _short(row.get("interface"))
        if not port:
            continue
        out.interfaces[port] = {
            "Auth Method": _first(row.get("method")),
            "Auth Domain": _first(row.get("domain")),
            "Auth Status": _first(row.get("status")),
            "Auth MAC": _first(row.get("mac_address")),
        }
    if rows:
        out.device["Authenticated Ports"] = len(out.interfaces)
    return out


def collect_etherchannel(conn) -> CollectorOutput:
    """``show etherchannel summary`` - which ports are bundled, and bundle health."""
    out = CollectorOutput()
    bundles = 0
    for row in _rows(conn, "show etherchannel summary"):
        bundle = _first(row.get("bundle_name"))
        status = _first(row.get("bundle_status"))
        protocol = _first(row.get("bundle_protocol"))
        members = _as_list(row.get("member_interface"))
        states = _as_list(row.get("member_interface_status"))
        if bundle:
            bundles += 1
        for index, member in enumerate(members):
            port = _short(member)
            if not port:
                continue
            out.interfaces[port] = {
                "Port-Channel": bundle,
                "PortChannel Status": status,
                "PortChannel Protocol": protocol,
                "PortChannel Member State": states[index] if index < len(states) else "",
            }
    if bundles:
        out.device["Port-Channels"] = bundles
    return out


def collect_transceivers(conn) -> CollectorOutput:
    """
    ``show interfaces transceiver`` - optical DOM readings on uplinks.

    Rx power drifting toward the receiver sensitivity floor is the earliest
    warning of a dirty connector or a failing optic, well before errors appear.
    """
    out = CollectorOutput()
    for row in _rows(conn, "show interfaces transceiver"):
        port = _short(row.get("interface"))
        if not port:
            continue
        out.interfaces[port] = {
            "Optic Temp (C)": _to_float(row.get("temperature")),
            "Optic Voltage (V)": _to_float(row.get("voltage")),
            "Optic Tx (dBm)": _to_float(row.get("tx_pwr")),
            "Optic Rx (dBm)": _to_float(row.get("rx_pwr")),
        }
    return out


_PORT_SECURITY_RE = re.compile(
    r"^\s*(?P<port>[A-Za-z]+[\d/.]+)\s+(?P<max>\d+)\s+(?P<current>\d+)\s+"
    r"(?P<violations>\d+)\s+(?P<action>\S+)\s*$"
)


def collect_port_security(conn) -> CollectorOutput:
    """
    ``show port-security`` - per-port limits, current count and violations.

    No ntc-template exists for this one, so the fixed table is parsed directly.
    """
    out = CollectorOutput()
    text = _send(conn, "show port-security")
    for line in str(text).splitlines():
        match = _PORT_SECURITY_RE.match(line)
        if not match:
            continue
        port = _short(match.group("port"))
        if not port:
            continue
        out.interfaces[port] = {
            "PortSec Max": int(match.group("max")),
            "PortSec Current": int(match.group("current")),
            "PortSec Violations": int(match.group("violations")),
            "PortSec Action": match.group("action"),
        }
    if out.interfaces:
        out.device["Port-Security Ports"] = len(out.interfaces)
    return out


# ── Device-level collectors ──────────────────────────────────────────────────

def collect_version_detail(conn) -> CollectorOutput:
    """
    ``show version`` via TextFSM - uptime, config register, reload reason.

    A config register other than 0x2102 and an unexpected reload reason are
    both things an audit should surface; neither was captured before.
    """
    out = CollectorOutput()
    rows = _rows(conn, "show version")
    if not rows:
        return out
    row = rows[0]
    config_register = _first(row.get("config_register"))
    out.device = {
        "Uptime": _first(row.get("uptime")),
        "Reload Reason": _first(row.get("reload_reason")),
        "Config Register": config_register,
        "Config Register OK": (config_register.lower() in {"", "0x2102", "0x102"}),
        "ROMMON": _first(row.get("rommon")),
        "Running Image": _first(row.get("running_image")),
        "Base MAC": _first(row.get("mac_address")),
    }
    # Stack members appear as repeated hardware/serial entries.
    hardware = _as_list(row.get("hardware"))
    serials = _as_list(row.get("serial"))
    if len(hardware) > 1 or len(serials) > 1:
        out.device["Stack Members"] = max(len(hardware), len(serials))
        out.device["Stack Models"] = ", ".join(dict.fromkeys(hardware))
        out.device["Stack Serials"] = ", ".join(dict.fromkeys(serials))
    return out


def collect_inventory(conn) -> CollectorOutput:
    """
    ``show inventory`` - chassis, modules and pluggable optics with serials.

    Produces its own worksheet: SFP serial numbers are exactly what you need
    for an RMA and are invisible everywhere else in the audit.
    """
    out = CollectorOutput()
    rows = _rows(conn, "show inventory")
    items = []
    optics = 0
    optic_pattern = re.compile(
        r"(?<![A-Za-z])(?:sfp\d*|qsfp\d*|gbic|xcvr|transceiver|glc)(?![A-Za-z])",
        flags=re.IGNORECASE,
    )
    for row in rows:
        name = _first(row.get("name"))
        descr = _first(row.get("descr"))
        pid = _first(row.get("pid"))

        if not name and not pid:
            continue
        items.append({
            "Name": name,
            "Description": descr,
            "PID": pid,
            "VID": _first(row.get("vid")),
            "Serial": _first(row.get("sn")),
        })
        # Some older switches report the optic PID as "Unspecified",
        # so the description must also be checked.
        search_text = f"{name or ''} {descr or ''} {pid or ''}"

        if optic_pattern.search(search_text):
            optics += 1
    if items:
        out.sheets["INVENTORY"] = items
        out.device["Inventory Items"] = len(items)
        out.device["Optics Installed"] = optics
    return out


def collect_environment(conn) -> CollectorOutput:
    """``show environment ...`` - power supply health and inlet temperature."""
    out = CollectorOutput()

    supplies = _rows(conn, "show environment power all")
    if supplies:
        states = [_first(r.get("status")) for r in supplies if _first(r.get("status"))]
        healthy = [s for s in states if s.lower() in {"ok", "good", "normal"}]
        out.device["PSUs Present"] = len(states)
        out.device["PSUs OK"] = len(healthy)
        out.device["PSU Status"] = ", ".join(dict.fromkeys(states))

    temps = _rows(conn, "show environment temperature")
    if temps:
        values = [_to_float(r.get("inlet_temperature_value")) for r in temps]
        values = [v for v in values if v is not None]
        states = [
            _first(r.get("inlet_temperature_state")) for r in temps
            if _first(r.get("inlet_temperature_state"))
        ]
        if values:
            out.device["Inlet Temp (C)"] = max(values)
        if states:
            out.device["Temp Status"] = ", ".join(dict.fromkeys(states))
    return out


_POE_BUDGET_RE = re.compile(
    r"Available:\s*(?P<available>[\d.]+)\s*\(?w?\)?\s*"
    r"Used:\s*(?P<used>[\d.]+)\s*\(?w?\)?\s*"
    r"Remaining:\s*(?P<remaining>[\d.]+)",
    re.I,
)


def collect_poe_budget(conn) -> CollectorOutput:
    """
    PoE budget from the ``show power inline`` header.

    The per-port draw was already collected; the chassis budget line
    (``Available: 740.0(w) Used: 132.4(w) Remaining: 607.6(w)``) is what tells
    you whether the switch can take more powered devices.
    """
    out = CollectorOutput()
    text = str(_send(conn, "show power inline"))
    match = _POE_BUDGET_RE.search(text)
    if not match:
        return out
    available = float(match.group("available"))
    used = float(match.group("used"))
    out.device = {
        "PoE Available (W)": available,
        "PoE Used (W)": used,
        "PoE Remaining (W)": float(match.group("remaining")),
        "PoE Used %": round(used / available * 100.0, 1) if available else 0.0,
    }
    return out


def collect_cpu_memory(conn) -> CollectorOutput:
    """``show processes cpu`` - five-second/one-minute/five-minute CPU load."""
    out = CollectorOutput()
    rows = _rows(conn, "show processes cpu")
    if rows:
        row = rows[0]
        for key, label in (
            ("cpu_usage_5_sec", "CPU 5s %"),
            ("cpu_usage_1_min", "CPU 1m %"),
            ("cpu_usage_5_min", "CPU 5m %"),
        ):
            value = _to_float(row.get(key))
            if value is not None:
                out.device[label] = value
    return out


# ── Registry ─────────────────────────────────────────────────────────────────

REGISTRY: tuple[Collector, ...] = (
    Collector(
        "switchport", "Access/voice/native VLAN and admin mode per port",
        True,
        ("Admin Mode", "Access VLAN", "Voice VLAN", "Native VLAN", "Trunk VLANs", "Negotiation"),
        collect_switchport,
    ),
    Collector(
        "mac_table", "MAC addresses learned per port (feeds stale detection)",
        True,
        ("MAC Count", "MAC Addresses", "MAC Vendors", "MAC VLANs"),
        collect_mac_table,
        zero_columns=("MAC Count",),
    ),
    Collector(
        "vlans", "VLAN names and counts", True, ("VLAN Name",), collect_vlans,
        device_columns=("VLANs Defined", "VLANs Active"),
    ),
    Collector(
        "spanning_tree", "STP role/state and PortFast (edge) status",
        True,
        ("STP Role", "STP State", "STP Type", "STP Edge"),
        collect_spanning_tree,
    ),
    Collector(
        "dot1x", "802.1X / MAB authentication sessions",
        False,
        ("Auth Method", "Auth Domain", "Auth Status", "Auth MAC"),
        collect_dot1x,
        device_columns=("Authenticated Ports",),
    ),
    Collector(
        "etherchannel", "Port-channel membership and bundle health",
        False,
        ("Port-Channel", "PortChannel Status", "PortChannel Protocol", "PortChannel Member State"),
        collect_etherchannel,
        device_columns=("Port-Channels",),
    ),
    Collector(
        "transceivers", "Optical transceiver DOM readings",
        True,
        ("Optic Temp (C)", "Optic Voltage (V)", "Optic Tx (dBm)", "Optic Rx (dBm)"),
        collect_transceivers,
    ),
    Collector(
        "port_security", "Port-security limits and violation counts",
        False,
        ("PortSec Max", "PortSec Current", "PortSec Violations", "PortSec Action"),
        collect_port_security,
        device_columns=("Port-Security Ports",),
    ),
    Collector(
        "version_detail", "Uptime, config register, reload reason, stack members",
        True, (), collect_version_detail,
        device_columns=(
            "Uptime", "Reload Reason", "Config Register", "Config Register OK",
            "ROMMON", "Running Image", "Base MAC",
            "Stack Members", "Stack Models", "Stack Serials",
        ),
    ),
    Collector(
        "inventory", "Modules and optics with serials (INVENTORY sheet)",
        True, (), collect_inventory,
        device_columns=("Inventory Items", "Optics Installed"),
    ),
    Collector(
        "environment", "Power supply and temperature health",
        True, (), collect_environment,
        device_columns=("PSUs Present", "PSUs OK", "PSU Status", "Inlet Temp (C)", "Temp Status"),
    ),
    Collector(
        "poe_budget", "Chassis PoE budget and utilisation",
        True, (), collect_poe_budget,
        device_columns=(
            "PoE Available (W)", "PoE Used (W)", "PoE Remaining (W)", "PoE Used %",
        ),
    ),
    Collector(
        "cpu_memory", "CPU utilisation", False, (), collect_cpu_memory,
        device_columns=("CPU 5s %", "CPU 1m %", "CPU 5m %"),
    ),
)

BY_NAME: dict[str, Collector] = {c.name: c for c in REGISTRY}


def enabled_collectors() -> list[Collector]:
    """Return the collectors switched on in config.yaml, in registry order."""
    return [c for c in REGISTRY if config.collector_enabled(c.name, c.default)]


def enabled_device_columns() -> tuple[str, ...]:
    """
    Device-level columns contributed by the enabled collectors.

    The summary schema seeds these so every device - including one that failed
    before a single collector ran - carries the same columns in the same order.
    Seeding with None rather than 0 keeps numeric columns numeric (so the TOTAL
    row still sums them) while rendering as an empty cell.
    """
    columns: list[str] = []
    for collector in enabled_collectors():
        columns.extend(collector.device_columns)
    return tuple(dict.fromkeys(columns))


def enabled_zero_columns() -> tuple[str, ...]:
    """
    Per-port columns that should read 0, not blank, when their collector ran.

    A port absent from the MAC table has zero MACs - a fact worth filtering on -
    rather than an unknown value.
    """
    columns: list[str] = []
    for collector in enabled_collectors():
        columns.extend(collector.zero_columns)
    return tuple(dict.fromkeys(columns))


def run_collectors(conn, collectors: list[Collector] | None = None) -> CollectorOutput:
    """
    Run the enabled collectors against a connected device and merge the results.

    A collector that raises is logged and skipped: enrichment must never cost
    us the device.
    """
    merged = CollectorOutput()
    for collector in enabled_collectors() if collectors is None else collectors:
        try:
            result = collector.run(conn)
        except Exception:  # noqa: BLE001 - one bad collector must not fail the device
            log.warning("Collector %r failed; continuing without it", collector.name, exc_info=True)
            continue
        for port, fields in result.interfaces.items():
            merged.interfaces.setdefault(port, {}).update(fields)
        merged.device.update(result.device)
        merged.sheets.update(result.sheets)
    return merged
