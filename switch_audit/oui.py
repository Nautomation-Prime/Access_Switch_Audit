"""MAC OUI to vendor lookup.

A small built-in table covering the device types that actually turn up on an
access switch — IP phones, wireless APs, printers, cameras and common NIC
vendors. It is deliberately not a full IEEE registry: the goal is to make the
majority of learned MACs self-explanatory in the workbook, not to be exhaustive.

Point ``audit.oui_file`` at a CSV (``oui,vendor`` per line, ``#`` comments
allowed) to extend or override it with the full IEEE OUI list or your own
device inventory.
"""

from __future__ import annotations

import csv
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

# oui (first three octets, uppercase hex, no separators) -> vendor
BUILTIN_OUI: dict[str, str] = {
    # Cisco
    "00000C": "Cisco", "000142": "Cisco", "000163": "Cisco", "0001C7": "Cisco",
    "000A41": "Cisco", "000D28": "Cisco", "001121": "Cisco", "0016C7": "Cisco",
    "001B54": "Cisco", "0021A0": "Cisco", "002497": "Cisco", "00E0FE": "Cisco",
    "1CDEA7": "Cisco", "2C3ECF": "Cisco", "3C5EC3": "Cisco", "4C710D": "Cisco",
    "5057A8": "Cisco", "6400F1": "Cisco", "70DB98": "Cisco", "88908D": "Cisco",
    "A0EC F9": "Cisco", "B0AA77": "Cisco", "C4B239": "Cisco", "F02572": "Cisco",
    # Cisco IP phones (SEP*)
    "001A2F": "Cisco IP Phone", "0022905": "Cisco IP Phone", "00260B": "Cisco IP Phone",
    "1CE85D": "Cisco IP Phone", "2C542D": "Cisco IP Phone",
    # Cisco Meraki
    "0018 0A": "Cisco Meraki", "88153": "Cisco Meraki", "E0CB BC": "Cisco Meraki",
    # Phones / UC
    "0004F2": "Poly (Polycom)", "00907A": "Polycom", "48256D": "Poly (Plantronics)",
    "000B82": "Grandstream", "000413": "Snom", "0015 65": "Yealink",
    "805EC0": "Yealink", "249AD8": "Yealink", "00085D": "Aastra/Mitel",
    "080023": "Panasonic", "001DE1": "Avaya", "00040D": "Avaya", "3CB15B": "Avaya",
    # Wireless APs
    "000B86": "Aruba (HPE)", "6CF37F": "Aruba (HPE)", "204C03": "Aruba (HPE)",
    "0024 6C": "Aruba (HPE)", "58C17F": "Ubiquiti", "788A20": "Ubiquiti",
    "24A43C": "Ubiquiti", "F09FC2": "Ubiquiti", "802AA8": "Ubiquiti",
    "000C42": "MikroTik", "4C5E0C": "Ruckus", "C4014D": "Ruckus",
    # Printers / MFP
    "0000AA": "Xerox", "9C934E": "Xerox", "000480": "Xerox",
    "00000F": "NeXT/HP", "001B78": "HP", "3464A9": "HP", "9C8E99": "HP",
    "0017C8": "Kyocera", "002673": "Kyocera", "00206B": "Konica Minolta",
    "0000 74": "Ricoh", "00268D": "Ricoh", "002481": "Brother",
    "008077": "Brother", "0080 92": "Silex/Printer", "00808C": "Lexmark",
    "0021B7": "Lexmark", "000174": "Canon", "2CDDA3": "Canon",
    "00 00 48": "Seiko Epson", "44D884": "Seiko Epson",
    # Cameras / physical security
    "00408C": "Axis Communications", "ACCC8E": "Axis Communications",
    "B8A44F": "Axis Communications", "0012 12": "Hikvision", "4419B6": "Hikvision",
    "C0561D": "Hikvision", "BCAD28": "Hanwha/Samsung", "0007 5F": "Bosch Security",
    # Servers / desktops / NICs
    "000C29": "VMware", "005056": "VMware", "001C14": "VMware",
    "00155D": "Microsoft Hyper-V", "0003FF": "Microsoft",
    "0050 56": "VMware", "525400": "QEMU/KVM", "080027": "VirtualBox",
    "001A A0": "Dell", "B8CA3A": "Dell", "F8BC12": "Dell", "18DBF2": "Dell",
    "D4AE52": "Dell", "0021 9B": "Dell", "00219B": "Dell",
    "3C D9 2B": "HPE", "9457A5": "HPE", "00 25 B3": "HPE",
    "001E4F": "Dell", "E4434B": "Dell",
    "6C3BE5": "HPE", "94F128": "HPE",
    "00A0C9": "Intel", "001B21": "Intel", "3C FD FE": "Intel", "A0369F": "Intel",
    "B4969D": "Intel", "8CEC4B": "Intel",
    "F4CE46": "HPE", "001E68": "Wistron",
    "3C2C30": "ASUSTek", "1C1B0D": "Gigabyte", "D8CB8A": "Micro-Star",
    # Apple
    "001451": "Apple", "0025 00": "Apple", "3C0754": "Apple", "7CD1C3": "Apple",
    "A45E60": "Apple", "F0189: ": "Apple", "AC BC 32": "Apple", "8C8590": "Apple",
    # Building / IoT
    "000039": "Toshiba", "00 12 3F": "Dell", "001D7E": "Cisco-Linksys",
    "0007AB": "Samsung", "00166C": "Samsung", "C87E75": "Samsung",
    "001A79": "Cisco TelePresence", "E4C722": "Cisco",
}


def _clean(oui: str) -> str:
    """Normalise an OUI string to six uppercase hex characters."""
    return re.sub(r"[^0-9A-Fa-f]", "", oui).upper()[:6]


# Normalise the built-in table once, tolerating the spacing above.
_BUILTIN: dict[str, str] = {}
for _raw, _vendor in BUILTIN_OUI.items():
    _key = _clean(_raw)
    if len(_key) == 6:
        _BUILTIN.setdefault(_key, _vendor)


def mac_to_oui(mac: str) -> str:
    """
    Return the six-hex-digit OUI for a MAC address, or '' if unparsable.

    Accepts the dotted (``0011.2233.4455``), colon and hyphen notations.
    """
    hex_only = re.sub(r"[^0-9A-Fa-f]", "", mac or "")
    if len(hex_only) < 6:
        return ""
    return hex_only[:6].upper()


def load_oui_file(path: str | Path) -> dict[str, str]:
    """
    Load extra OUI mappings from a two-column CSV (``oui,vendor``).

    Blank lines and ``#`` comments are ignored, as is a header row starting
    with "oui". Unreadable files are logged and skipped rather than fatal.
    """
    mapping: dict[str, str] = {}
    file_path = Path(path).expanduser()
    try:
        with open(file_path, newline="", encoding="utf-8") as handle:
            for row in csv.reader(handle):
                if not row or not row[0].strip() or row[0].lstrip().startswith("#"):
                    continue
                if len(row) < 2:
                    continue
                key = _clean(row[0])
                if len(key) != 6 or key == "OUI":
                    continue
                mapping[key] = row[1].strip()
    except OSError as exc:
        log.warning("Could not read OUI file %s: %s", file_path, exc)
        return {}
    log.info("Loaded %d OUI entries from %s", len(mapping), file_path)
    return mapping


def build_lookup(extra_file: str | Path | None = None) -> dict[str, str]:
    """Return the OUI table, with any user-supplied file layered on top."""
    table = dict(_BUILTIN)
    if extra_file:
        table.update(load_oui_file(extra_file))
    return table


def vendor_for(mac: str, table: dict[str, str] | None = None) -> str:
    """Return the vendor for a MAC address, or '' when it is not in the table."""
    oui = mac_to_oui(mac)
    if not oui:
        return ""
    return (table if table is not None else _BUILTIN).get(oui, "")
