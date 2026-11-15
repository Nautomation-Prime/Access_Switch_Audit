"""A fake Cisco switch that answers every show command the audit issues."""

from __future__ import annotations

from typing import Any

# ── Fixed-width 'show interfaces status' table ────────────────────────────────
_STATUS_COLUMNS = (
    ("Port", 10),
    ("Name", 19),
    ("Status", 13),
    ("Vlan", 11),
    ("Duplex", 8),
    ("Speed", 6),
    ("Type", 20),
)


def _fixed_row(*values: str) -> str:
    """Render one fixed-width row aligned to the status-table column widths."""
    return "".join(
        str(value).ljust(width) for value, (_, width) in zip(values, _STATUS_COLUMNS, strict=True)
    ).rstrip()


SHOW_INTERFACES_STATUS = "\n".join([
    _fixed_row(*(name for name, _ in _STATUS_COLUMNS)),
    _fixed_row("Gi1/0/1", "Uplink to core", "connected", "trunk", "full", "1000", "10/100/1000BaseTX"),
    _fixed_row("Gi1/0/2", "Desk 12", "connected", "10", "full", "100", "10/100/1000BaseTX"),
    _fixed_row("Gi1/0/3", "", "notconnect", "20", "auto", "auto", "10/100/1000BaseTX"),
    _fixed_row("Gi1/0/4", "Decommissioned", "disabled", "30", "auto", "auto", "10/100/1000BaseTX"),
])

# ── 'show power inline' ───────────────────────────────────────────────────────
_POE_COLUMNS = (("Interface", 11), ("Admin", 7), ("Oper", 8), ("Power", 8), ("Class", 7), ("Device", 20))


def _poe_row(*values: str) -> str:
    return "".join(
        str(value).ljust(width) for value, (_, width) in zip(values, _POE_COLUMNS, strict=True)
    ).rstrip()


SHOW_POWER_INLINE = "\n".join([
    _poe_row(*(name for name, _ in _POE_COLUMNS)),
    _poe_row("Gi1/0/1", "auto", "off", "0.0", "n/a", "n/a"),
    _poe_row("Gi1/0/2", "auto", "on", "7.0", "2", "IP Phone 7962"),
    _poe_row("Gi1/0/3", "auto", "off", "0.0", "n/a", "n/a"),
    _poe_row("Gi1/0/4", "auto", "off", "0.0", "n/a", "n/a"),
])

# ── 'show lldp neighbors detail' — note: real IOS has no "Device ID" field ────
SHOW_LLDP_DETAIL = """\
------------------------------------------------
Local Intf: Gi1/0/1
Chassis id: 0011.2233.4455
Port id: Gi0/24
Port Description: GigabitEthernet0/24
System Name: core-sw-01

System Description:
Cisco IOS Software

Time remaining: 96 seconds
------------------------------------------------
Local Intf: Gi1/0/2
Chassis id: 0011.2233.6677
Port id: 1
Port Description: SW PORT
System Name: SEP001122334455

Time remaining: 130 seconds

Total entries displayed: 2
"""

SHOW_CDP_DETAIL = """\
-------------------------
Device ID: core-sw-01.example.com
Entry address(es):
  IP address: 192.0.2.10
Platform: cisco WS-C3850-24T,  Capabilities: Switch IGMP
Interface: GigabitEthernet1/0/1,  Port ID (outgoing port): GigabitEthernet0/24
Holdtime : 143 sec
"""

SHOW_VERSION = """\
Cisco IOS XE Software, Version 17.6.5a
Cisco IOS Software [Bengaluru], Catalyst L3 Switch Software

cisco C9300-24T (X86) processor with 1432032K/6147K bytes of memory.
Processor board ID FCW2140L0GH
Model number                         : C9300-24T
System serial number                 : FCW2140L0GH
"""


def _tfsm_record(interface: str, description: str, link: str, proto: str, **extra: Any) -> dict[str, Any]:
    record = {
        "interface": interface,
        "description": description,
        "link_status": link,
        "protocol_status": proto,
        "admin_state": "",
        "duplex": "full",
        "speed": "1000",
        "hardware_type": "Gigabit Ethernet",
        "input_errors": "0",
        "output_errors": "0",
        "crc": "0",
        "last_input": "00:00:01",
        "last_output": "00:00:01",
        "vlan": "",
    }
    record.update(extra)
    return record


SHOW_INTERFACES_TEXTFSM: list[dict[str, Any]] = [
    _tfsm_record("GigabitEthernet1/0/1", "Uplink to core", "up", "up"),
    _tfsm_record("GigabitEthernet1/0/2", "Desk 12", "up", "up"),
    # Idle for well over a year, no PoE, no neighbour -> stale.
    _tfsm_record("GigabitEthernet1/0/3", "", "down", "down", last_input="1y2w", input_errors="14"),
    _tfsm_record(
        "GigabitEthernet1/0/4", "Decommissioned", "administratively down",
        "down", last_input="never",
    ),
]


class FakeConnection:
    """Stands in for a Netmiko connection to a Cisco access switch."""

    def __init__(
        self,
        hostname: str = "access-sw-01",
        failures: dict[str, Exception] | None = None,
        unsupported: set[str] | None = None,
    ):
        self.hostname = hostname
        self.failures = failures or {}
        self.unsupported = unsupported or set()
        self.commands: list[str] = []
        self.disconnected = False
        self.enabled = False

    #: Commands answered with TextFSM-shaped rows (use_textfsm=True).
    STRUCTURED = {
        "show interfaces": lambda: SHOW_INTERFACES_TEXTFSM,
        "show interfaces switchport": lambda: SHOW_INTERFACES_SWITCHPORT,
        "show mac address-table": lambda: SHOW_MAC_ADDRESS_TABLE,
        "show vlan": lambda: SHOW_VLAN,
        "show spanning-tree": lambda: SHOW_SPANNING_TREE,
        "show interfaces transceiver": lambda: SHOW_INTERFACE_TRANSCEIVER,
        "show inventory": lambda: SHOW_INVENTORY,
        "show version": lambda: SHOW_VERSION_TEXTFSM,
        "show environment power all": lambda: SHOW_ENVIRONMENT_POWER,
        "show environment temperature": lambda: SHOW_ENVIRONMENT_TEMPERATURE,
        "show authentication sessions": lambda: SHOW_AUTHENTICATION_SESSIONS,
        "show etherchannel summary": lambda: SHOW_ETHERCHANNEL_SUMMARY,
        "show processes cpu": lambda: [],
    }

    #: Commands answered with raw text (use_textfsm=False).
    RAW = {
        "show version": lambda: SHOW_VERSION,
        "show interfaces status": lambda: SHOW_INTERFACES_STATUS,
        "show power inline": lambda: POE_BUDGET_HEADER + SHOW_POWER_INLINE,
        "show lldp neighbors detail": lambda: SHOW_LLDP_DETAIL,
        "show cdp neighbors detail": lambda: SHOW_CDP_DETAIL,
        "show port-security": lambda: SHOW_PORT_SECURITY,
    }

    def send_command(self, command: str, **kwargs: Any):
        self.commands.append(command)
        for needle, exc in self.failures.items():
            if needle in command:
                raise exc
        if command in self.unsupported:
            # How a device rejects a command it does not implement.
            return "% Invalid input detected at '^' marker."

        if "hostname" in command:
            return f"hostname {self.hostname}"
        if kwargs.get("use_textfsm"):
            factory = self.STRUCTURED.get(command)
            return factory() if factory else ""
        factory = self.RAW.get(command)
        return factory() if factory else ""

    def find_prompt(self) -> str:
        return f"{self.hostname}#"

    def enable(self) -> None:
        self.enabled = True

    def disconnect(self) -> None:
        self.disconnected = True


# ── Collector fixtures ────────────────────────────────────────────────────────
# Shapes match the ntc-templates cisco_ios_* definitions that Netmiko uses, i.e.
# lower-cased header names, with TextFSM List values arriving as lists.

SHOW_INTERFACES_SWITCHPORT = [
    {
        "interface": "GigabitEthernet1/0/1", "switchport": "Enabled",
        "switchport_monitor": "", "switchport_negotiation": "On",
        "mode": "trunk", "admin_mode": "trunk",
        "access_vlan": "1", "native_vlan": "99", "voice_vlan": "none",
        "trunking_vlans": ["10,20,30"],
    },
    {
        "interface": "GigabitEthernet1/0/2", "switchport": "Enabled",
        "switchport_monitor": "", "switchport_negotiation": "Off",
        "mode": "static access", "admin_mode": "static access",
        "access_vlan": "10", "native_vlan": "1", "voice_vlan": "110",
        "trunking_vlans": ["ALL"],
    },
    {
        "interface": "GigabitEthernet1/0/3", "switchport": "Enabled",
        "switchport_monitor": "", "switchport_negotiation": "Off",
        "mode": "static access", "admin_mode": "static access",
        "access_vlan": "20", "native_vlan": "1", "voice_vlan": "none",
        "trunking_vlans": ["ALL"],
    },
    {
        "interface": "GigabitEthernet1/0/4", "switchport": "Enabled",
        "switchport_monitor": "", "switchport_negotiation": "Off",
        "mode": "static access", "admin_mode": "static access",
        "access_vlan": "30", "native_vlan": "1", "voice_vlan": "none",
        "trunking_vlans": ["ALL"],
    },
]

SHOW_MAC_ADDRESS_TABLE = [
    # Uplink carries several MACs learned through it.
    {"destination_address": "0011.2233.4455", "type": "DYNAMIC", "vlan_id": "10",
     "destination_port": ["Gi1/0/1"]},
    {"destination_address": "0011.2233.6677", "type": "DYNAMIC", "vlan_id": "20",
     "destination_port": ["Gi1/0/1"]},
    # A Cisco IP phone (OUI 1CE85D) and a VM behind it (OUI 005056).
    {"destination_address": "1ce8.5daa.bbcc", "type": "DYNAMIC", "vlan_id": "110",
     "destination_port": ["Gi1/0/2"]},
    {"destination_address": "0050.5611.2233", "type": "DYNAMIC", "vlan_id": "10",
     "destination_port": ["Gi1/0/2"]},
    # CPU entries must be ignored.
    {"destination_address": "0100.0ccc.cccc", "type": "STATIC", "vlan_id": "All",
     "destination_port": ["CPU"]},
    # Gi1/0/3 and Gi1/0/4 deliberately have no entries.
]

SHOW_VLAN = [
    {"vlan_id": "1", "vlan_name": "default", "status": "active", "interfaces": []},
    {"vlan_id": "10", "vlan_name": "USER-DATA", "status": "active",
     "interfaces": ["Gi1/0/2"]},
    {"vlan_id": "20", "vlan_name": "PRINTERS", "status": "active",
     "interfaces": ["Gi1/0/3"]},
    {"vlan_id": "30", "vlan_name": "QUARANTINE", "status": "suspended",
     "interfaces": ["Gi1/0/4"]},
    {"vlan_id": "110", "vlan_name": "VOICE", "status": "active", "interfaces": []},
]

SHOW_SPANNING_TREE = [
    {"vlan_id": "10", "interface": "Gi1/0/1", "role": "Root", "status": "FWD",
     "cost": "4", "port_priority": "128", "port_id": "1", "type": "P2p"},
    {"vlan_id": "10", "interface": "Gi1/0/2", "role": "Desg", "status": "FWD",
     "cost": "19", "port_priority": "128", "port_id": "2", "type": "P2p Edge"},
    {"vlan_id": "20", "interface": "Gi1/0/3", "role": "Desg", "status": "FWD",
     "cost": "19", "port_priority": "128", "port_id": "3", "type": "P2p Edge"},
]

SHOW_INTERFACE_TRANSCEIVER = [
    {"interface": "Gi1/0/1", "temperature": "34.5", "voltage": "3.29",
     "current": "6.2", "tx_pwr": "-2.3", "rx_pwr": "-5.8"},
]

SHOW_INVENTORY = [
    {"name": "1", "descr": "C9300-24T", "pid": "C9300-24T", "vid": "V02",
     "sn": "FCW2140L0GH"},
    {"name": "Te1/1/1", "descr": "SFP-10GBase-SR", "pid": "SFP-10G-SR",
     "vid": "V03", "sn": "AVD2035A1BC"},
]

SHOW_VERSION_TEXTFSM = [{
    "software_image": "CAT9K_IOSXE", "version": "17.6.5a", "release": "fc2",
    "rommon": "IOS-XE", "hostname": "access-sw-01",
    "uptime": "41 weeks, 2 days, 6 hours, 11 minutes",
    "uptime_years": "", "uptime_weeks": "41", "uptime_days": "2",
    "uptime_hours": "6", "uptime_minutes": "11",
    "reload_reason": "Reload Command", "running_image": "packages.conf",
    "hardware": ["C9300-24T"], "serial": ["FCW2140L0GH"],
    "config_register": "0x2102", "mac_address": ["00:11:22:33:44:55"],
    "restarted": "",
}]

SHOW_ENVIRONMENT_POWER = [
    {"ps_module": "1", "pid": "PWR-C1-715WAC", "serial": "ART2043F1AB",
     "status": "OK", "sys_pwr": "Good", "poe_pwr": "Good", "watts": "715"},
    {"ps_module": "2", "pid": "PWR-C1-715WAC", "serial": "ART2043F1CD",
     "status": "Not Present", "sys_pwr": "", "poe_pwr": "", "watts": ""},
]

SHOW_ENVIRONMENT_TEMPERATURE = [{
    "switch": "1", "switch_temperature": "GREEN",
    "inlet_temperature_value": "26", "inlet_temperature_state": "GREEN",
    "inlet_yellow_threshold": "46", "inlet_red_threshold": "56",
    "hotspot_temperature_value": "38", "hotspot_temperature_state": "GREEN",
    "hotspot_yellow_threshold": "105", "hotspot_red_threshold": "125",
}]

SHOW_AUTHENTICATION_SESSIONS = [
    {"interface": "Gi1/0/2", "mac_address": "1caa.aabb.ccdd", "method": "dot1x",
     "domain": "DATA", "status": "Authorized", "session": "0A0A0A0A0000001"},
]

SHOW_ETHERCHANNEL_SUMMARY = [
    {"group": "1", "bundle_name": "Po1", "bundle_status": "SU",
     "bundle_protocol": "LACP", "member_interface": ["Gi1/0/1"],
     "member_interface_status": ["P"]},
]

SHOW_PORT_SECURITY = """\
Secure Port  MaxSecureAddr  CurrentAddr  SecurityViolation  Security Action
                (Count)       (Count)          (Count)
---------------------------------------------------------------------------
      Gi1/0/2              2            2                  0        Shutdown
      Gi1/0/3              1            0                  4        Restrict
---------------------------------------------------------------------------
"""

# The chassis budget header that collect_poe_budget looks for.
POE_BUDGET_HEADER = (
    "Module   Available     Used     Remaining\n"
    "          (Watts)     (Watts)    (Watts)\n"
    "------   ---------   --------   ---------\n"
    "1           740.0       132.4       607.6\n"
    "Available: 740.0(w)  Used: 132.4(w)  Remaining: 607.6(w)\n"
    "\n"
)
