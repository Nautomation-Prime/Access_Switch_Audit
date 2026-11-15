"""Device audit module - connection, data collection, and analysis logic."""

import logging
import re
import time
from typing import Any

import pandas as pd

from .app_config import config
from .collectors import enabled_device_columns, enabled_zero_columns, run_collectors
from .formatters import all_aliases, normalize_ifname
from .jump_manager import JumpManager
from .netmiko_utils import connect_to_device

log = logging.getLogger(__name__)


# ---------------- Time parsing for "Last input" -------------
_TIME_RE = re.compile(
    r"(?:(?P<y>\d+)\s*y)?\s*(?:(?P<w>\d+)\s*w)?\s*(?:(?P<d>\d+)\s*d)?\s*(?P<h>\d+?\s*h)?\s*(?P<m>\d+?\s*m)?\s*(?P<s>\d+?\s*s)?",
    re.IGNORECASE
)

def _parse_last_input_seconds(s: str) -> float | None:
    """
    Parse Cisco 'Last input' timer strings into seconds.

    Handles formats like:
    - "00:01:23"
    - "1d2h30m"
    - "never"

    Args:
        s: Raw timer string.

    Returns:
        Seconds as float, or None if unparsable/never.
    """
    s = (s or "").strip().lower()
    if not s or s == "never":
        return None
    # hh:mm:ss
    if re.match(r"^\d{1,2}:\d{2}:\d{2}$", s):
        hh, mm, ss = s.split(":")
        return int(hh) * 3600 + int(mm) * 60 + int(ss)
    # compact duration (y, w, d, h, m, s)
    m = _TIME_RE.fullmatch(s.replace(" ", ""))
    if not m:
        return None
    y = int(m.group("y") or 0)
    w = int(m.group("w") or 0)
    d = int(m.group("d") or 0)
    h = int((m.group("h") or "0").rstrip("h") or 0)
    mn = int((m.group("m") or "0").rstrip("m") or 0)
    sc = int((m.group("s") or "0").rstrip("s") or 0)
    days = y * 365 + w * 7 + d
    return days * 86400 + h * 3600 + mn * 60 + sc


# ------------- LLDP / CDP neighbors -------------
def _first_match(pattern: str, text: str) -> str:
    """Return the first capture group for ``pattern`` in ``text``, or ''."""
    m = re.search(pattern, text, flags=re.I | re.M)
    return m.group(1).strip() if m else ""


def _get_lldp_neighbors(conn) -> dict[str, tuple[str, str]]:
    """
    Parse 'show lldp neighbors detail' output.

    IOS prints one block per neighbour, introduced by 'Local Intf:' and naming
    the remote end via 'System Name:'. There is no 'Device ID:' field — that is
    CDP's wording — so the previous line-at-a-time parser required a field that
    never appears and returned no LLDP neighbours at all, on any device. Ports
    whose only neighbour speaks LLDP were therefore mis-flagged as stale.

    Returns:
        Dict mapping local interface -> (remote device, remote port).
    """
    neighbors: dict[str, tuple[str, str]] = {}
    try:
        out = conn.send_command(
            "show lldp neighbors detail", read_timeout=config.read_timeout, use_textfsm=False
        )
    except Exception as exc:  # noqa: BLE001 - enrichment is best-effort
        log.debug("LLDP neighbour collection failed: %s", exc)
        return neighbors

    # Split before each 'Local Intf:' line so one chunk == one neighbour,
    # independent of how the platform delimits blocks.
    for chunk in re.split(r"(?=^Local Intf:)", out or "", flags=re.M | re.I):
        local = _first_match(r"^(?:Local Intf|Local Port id)\s*:\s*(\S+)", chunk)
        if not local:
            continue
        device = (
            _first_match(r"^System Name\s*:\s*(\S+)", chunk)
            or _first_match(r"^Device ID\s*:\s*(\S+)", chunk)
            or _first_match(r"^Chassis id\s*:\s*(\S+)", chunk)
        )
        port = _first_match(r"^Port id\s*:\s*(\S+)", chunk)
        if device or port:
            neighbors[normalize_ifname(local)[0]] = (device, port)
    return neighbors


def _get_cdp_neighbors(conn) -> dict[str, tuple[str, str]]:
    """
    Parse 'show cdp neighbors detail' output.

    Returns:
        Dict mapping local interface -> (remote device, remote port).
    """
    neighbors: dict[str, tuple[str, str]] = {}
    try:
        out = conn.send_command("show cdp neighbors detail", read_timeout=config.read_timeout, use_textfsm=False)
        cur_dev = None
        for line in out.splitlines():
            m = re.search(r"Device ID:\s*(\S+)", line)
            if m:
                cur_dev = m.group(1)
            m = re.search(r"Interface:\s*([A-Za-z]+[A-Za-z]?[0-9/\.]+),\s*Port ID.*:\s*(\S+)", line)
            if m and cur_dev:
                local = normalize_ifname(m.group(1))[0]
                neighbors[local] = (cur_dev, m.group(2))
                cur_dev = None
    except Exception as exc:  # noqa: BLE001 - enrichment is best-effort
        log.debug("CDP neighbour collection failed: %s", exc)
    return neighbors


def _parse_ios_version(show_ver_text: str) -> str:
    """
    Extract a concise OS version string from 'show version' output.
    
    Tries common IOS/IOS-XE patterns; falls back to the first 'Version x.y...' hit.
    
    Returns:
        Short text like 'IOS-XE 17.6.5a' or 'IOS 15.2(7)E8' if found, else ''.
    """
    s = show_ver_text or ""
    # Common IOS-XE banner: 'Cisco IOS XE Software, Version 17.6.5a'
    m = re.search(r"Cisco IOS XE Software,\s*Version\s*([^\s,]+)", s, flags=re.I)
    if m:
        return f"IOS-XE {m.group(1)}"
    # Common IOS banner: 'Cisco IOS Software, ... Version 15.2(7)E8'
    m = re.search(r"Cisco IOS Software.*Version\s*([^\s,]+)", s, flags=re.I)
    if m:
        return f"IOS {m.group(1)}"
    # NX-OS/others pattern (just in case): 'system:    version 9.3(10)'
    m = re.search(r"\bversion\s+([0-9][^ \r\n\t,;)]*)", s, flags=re.I)
    if m:
        return m.group(1)
    return ""


def _parse_hardware_from_version(show_ver_text: str) -> dict[str, str]:
    """
    Extract hardware details (model, serial, memory) from 'show version' output.
    
    Attempts TextFSM parsing first, falls back to regex patterns if needed.
    
    Args:
        show_ver_text: Raw output from 'show version' command.
    
    Returns:
        Dict with keys: model, serial, memory (all strings, empty if not found).
    """
    data = {'model': '', 'serial': '', 'memory': ''}
    s = show_ver_text or ""
    
    # ===== TEXTFSM PARSING ATTEMPT =====
    try:
        import textfsm
        from netmiko.utilities import get_template_file
        
        template_path = None
        try:
            template_path = get_template_file('cisco_ios', 'show_version')
        except (OSError, ValueError, KeyError):
            pass
        
        if template_path:
            try:
                with open(template_path, encoding='utf-8') as f:
                    fsm = textfsm.TextFSM(f)
                    result = fsm.ParseText(s)
                    if result:
                        headers = fsm.header
                        first_row = result[0]
                        parsed = dict(zip(headers, first_row, strict=True))
                        
                        # TextFSM field names vary by device; try common variations
                        # Check ALL available fields and attempt extraction
                        if not data['model']:
                            for key in parsed.keys():
                                if 'model' in key.lower() and parsed[key]:
                                    data['model'] = parsed[key].strip()
                                    break
                        
                        if not data['serial']:
                            for key in parsed.keys():
                                if any(term in key.lower() for term in ['serial', 'chassis_serial']):
                                    if parsed[key]:
                                        data['serial'] = parsed[key].strip()
                                        break
                        
                        if not data['memory']:
                            for key in parsed.keys():
                                if 'memory' in key.lower() and parsed[key]:
                                    mem_str = parsed[key].strip()
                                    if mem_str and not any(u in mem_str for u in ['B', 'K', 'M', 'G']):
                                        data['memory'] = f"{mem_str} B"
                                    else:
                                        data['memory'] = mem_str
                                    break
                        
                        # If TextFSM succeeded in finding at least one field, return
                        if data['model'] or data['serial'] or data['memory']:
                            return data
            except (OSError, ValueError, IndexError):
                pass
    except ImportError:
        pass
    
    # ===== REGEX FALLBACK PATTERNS =====
    
    # Model patterns (try multiple patterns for different device families)
    # Order matters - check specific patterns first, then generic ones
    model_patterns = [
        r"^Model number\s*:\s*([A-Z0-9\-]+)",  # Exact match for "Model number" line
        r"cisco\s+([A-Z][A-Z0-9\-]*(?:-[A-Z0-9]+)*)\s*\(",  # cisco WS-C2960X-24PS-L ( format
        r"Switch Ports Model\s+SW.*\n.*?\*\s+\d+\s+\d+\s+(\S+)",  # From switch info table
    ]
    
    for pattern in model_patterns:
        m = re.search(pattern, s, flags=re.MULTILINE | re.IGNORECASE)
        if m:
            candidate = m.group(1).strip()
            # Filter out non-model strings
            if candidate and not any(x in candidate.lower() for x in ['bytes', 'software', 'flash', 'version']):
                # Reject short codes like C0, R0, B0, E0 unless they're part of a larger model number
                if len(candidate) > 2 or '-' in candidate:
                    data['model'] = candidate
                    break
    
    # Serial patterns
    serial_patterns = [
        r"^System [Ss]erial [Nn]umber\s*:\s*(\S+)",  # System Serial Number (most reliable)
        r"^Processor board ID\s*:\s*(\S+)",  # Processor board ID (backup)
        r"^Motherboard [Ss]erial [Nn]umber\s*:\s*(\S+)",  # Motherboard Serial
    ]
    
    for pattern in serial_patterns:
        m = re.search(pattern, s, flags=re.IGNORECASE | re.MULTILINE)
        if m:
            data['serial'] = m.group(1).strip()
            break
    
    return data


# ---------------- 'show interfaces status' robust parser (fallback) ----------------
def _find_columns(header_line: str) -> dict[str, slice]:
    """
    Discover fixed-column slices from the header line of 'show interfaces status'.

    Args:
        header_line: The header line containing column names.

    Returns:
        Mapping from lowercase column name to slice object.
    """
    tokens = [("Port", None), ("Name", None), ("Status", None), ("Vlan", None),
              ("Duplex", None), ("Speed", None), ("Type", None)]
    for i, (tok, _) in enumerate(tokens):
        idx = header_line.find(tok)
        if idx == -1:
            raise ValueError("Unrecognized header for 'show interfaces status': " + header_line)
        tokens[i] = (tok, idx)
    positions = [idx for _, idx in tokens]
    positions_sorted = sorted(positions)
    slices = {}
    for tok, idx in tokens:
        start = idx
        next_starts = [p for p in positions_sorted if p > start]
        end = next_starts[0] if next_starts else None
        slices[tok.lower()] = slice(start, end)
    return slices


def parse_show_interfaces_status(output: str) -> list[dict[str, str]]:
    """
    Parse the text output of 'show interfaces status' into records.

    Args:
        output: Raw multiline string from the device.

    Returns:
        List of dict rows with keys: port, name, status, vlan, duplex, speed, type.
    """
    lines = [ln.rstrip("\r") for ln in output.splitlines() if ln.strip()]
    records: list[dict[str, str]] = []

    def is_header(ln: str) -> bool:
        return ("Port" in ln and "Status" in ln and "Vlan" in ln and "Speed" in ln)

    i, n = 0, len(lines)
    while i < n:
        ln = lines[i]
        if not is_header(ln):
            i += 1
            continue

        try:
            col = _find_columns(ln)
        except ValueError:
            i += 1
            continue

        i += 1
        while i < n:
            row = lines[i]
            if is_header(row):
                break
            if set(row.strip()) == {"-"}:
                i += 1
                continue
            if row.strip().lower().startswith(("port ", "----")):
                i += 1
                continue
            try:
                rec = {
                    'port':   row[col['port']].strip().lstrip("*^!"),
                    'name':   row[col['name']].strip(),
                    'status': row[col['status']].strip().lower(),
                    'vlan':   row[col['vlan']].strip().lower(),
                    'duplex': row[col['duplex']].strip().lower(),
                    'speed':  row[col['speed']].strip().lower(),
                    'type':   row[col['type']].strip(),
                }
            except (KeyError, IndexError, ValueError):
                i += 1
                continue
            if rec['port']:
                records.append(rec)
            i += 1

    return records


def parse_show_power_inline(output: str) -> dict[str, dict[str, Any]]:
    """
    Parse 'show power inline' and return a map keyed by interface aliases.
    
    Example rows often look like:
      Interface  Admin  Oper  Power   Class  Device
      Gi1/0/1    auto   on    7.0 W   2      IP Phone 7962

    Returns:
        Dict[str, Dict[str, Any]]: per-interface PoE details keyed by short, long, and raw names.
    """
    lines = [ln for ln in output.splitlines() if ln.strip()]
    header_idx = None
    for i, ln in enumerate(lines):
        if 'Interface' in ln and 'Admin' in ln and 'Oper' in ln:
            header_idx = i
            break
    if header_idx is None:
        return {}

    header = lines[header_idx]
    def idx(tok: str) -> int | None:
        p = header.find(tok)
        return p if p != -1 else None
    starts = {
        'if': idx('Interface'),
        'admin': idx('Admin'),
        'oper': idx('Oper'),
        'power': idx('Power'),
        'class': idx('Class'),
        'device': idx('Device'),
    }
    keys_order = [k for k, v in starts.items() if v is not None]
    pos_sorted = sorted([(k, starts[k]) for k in keys_order], key=lambda x: x[1])
    slices: dict[str, slice] = {}
    for i, (k, start) in enumerate(pos_sorted):
        next_start = pos_sorted[i + 1][1] if i + 1 < len(pos_sorted) else None
        slices[k] = slice(start, next_start)

    data: dict[str, dict[str, Any]] = {}
    for ln in lines[header_idx + 1:]:
        if set(ln.strip()) == {"-"}:
            continue
        iface_raw = ln[slices['if']].strip() if 'if' in slices else None
        if not iface_raw:
            continue
        short, longf = normalize_ifname(iface_raw)

        entry = {
            'poe_admin':  ln[slices['admin']].strip().lower() if 'admin' in slices else '',
            'poe_oper':   ln[slices['oper']].strip().lower() if 'oper' in slices else '',
            'poe_power_w': ln[slices['power']].strip() if 'power' in slices else '',
            'poe_class':  ln[slices['class']].strip() if 'class' in slices else '',
            'poe_device': ln[slices['device']].strip() if 'device' in slices else '',
        }
        # Store under multiple keys to maximize match rate
        data[short] = entry
        data[longf] = entry
        data[iface_raw] = entry

    return data


def get_interfaces_via_show_interfaces(conn) -> list[dict[str, Any]]:
    """
    Use TextFSM to parse 'show interfaces' for all ports.
    
    Expected keys include (template-dependent):
      - interface, description
      - status/link_status, protocol/protocol_status, admin_state
      - duplex, speed, hardware_type
      - input_errors, output_errors, crc
      - last_input

    Returns:
        List[Dict[str, Any]] of parsed records; empty list on failure.
    """
    try:
        output = conn.send_command("show interfaces", use_textfsm=True)
    except Exception as exc:  # noqa: BLE001 - reported by the caller as NO DATA
        log.warning("'show interfaces' failed: %s", exc)
        return []
    if isinstance(output, list):
        return output
    log.warning("'show interfaces' returned unparsed output; is the TextFSM template available?")
    return []


def _poe_watts(raw: Any) -> float:
    """Parse a PoE power cell such as "7.0 W" or "" into watts (0.0 when absent)."""
    try:
        first = str(raw or "").split()[0]
    except (AttributeError, IndexError):
        return 0.0
    if first in {"", "-", "n/a", "N/A"}:
        return 0.0
    try:
        return float(first)
    except ValueError:
        return 0.0


def _categorize_port(row: dict[str, Any], stale_days: int) -> str:
    """
    Classify an access port for quick inventory analysis.
    
    Returns:
        Category string: 'trunk/routed', 'disabled', 'error', 'stale-unused',
                       'powered-device', 'connected', or 'available'.
    """
    status = (row.get('Status', '') or '').lower()
    mode = (row.get('Mode', '') or '').lower()
    
    if mode != 'access':
        return 'trunk/routed'
    if status == 'administratively down':
        return 'disabled'
    if status == 'err-disabled':
        return 'error'
    if row.get(f'Stale (≥{stale_days} d)', False):
        return 'stale-unused'
    if status == 'connected':
        if _poe_watts(row.get('PoE Power (W)')) > 0.0:
            return 'powered-device'
        return 'connected'
    return 'available'


# Canonical SUMMARY schema. Every device contributes the same keys in the same
# order so the workbook has stable columns and pandas does not have to union
# mismatched dicts (which previously produced NaN-riddled summary rows).
SUMMARY_COUNT_FIELDS = (
    "Total Ports (phy)",
    "Access Ports",
    "Trunk Ports",
    "Routed Ports",
    "Connected",
    "Not Connected",
    "Admin Down",
    "Err-Disabled",
    "Stale Ports",
    "Ports w/ Errors",
    "PoE Active Ports",
)
SUMMARY_PCT_FIELDS = (
    "% Access of Total",
    "% Trunk of Total",
    "% Routed of Total",
    "% Connected of Total",
    "% AdminDown of Total",
)


def base_summary(device: str, ip: str, **overrides: Any) -> dict[str, Any]:
    """
    Build a summary row pre-populated with the canonical schema.

    Counts default to 0 rather than a sentinel string so the column keeps a
    numeric dtype and the workbook TOTAL row stays meaningful.
    """
    summary: dict[str, Any] = {
        "Device": device,
        "Mgmt IP": ip,
        "Audit Result": "OK",
        "Model": "",
        "Serial": "",
        "IOS Version": "",
    }
    for field in SUMMARY_COUNT_FIELDS:
        summary[field] = 0
    for field in SUMMARY_PCT_FIELDS:
        summary[field] = 0.0
    # Columns the enabled collectors will fill in. None (not 0) so an
    # un-collected value renders blank instead of claiming a real zero.
    for field in enabled_device_columns():
        summary[field] = None
    summary["Error"] = ""
    summary.update(overrides)
    return summary


def error_summary(ip: str, error: Any, hostname: str | None = None) -> dict[str, Any]:
    """Build a summary row for a device that could not be audited."""
    return base_summary(
        hostname or ip,
        ip,
        **{"Audit Result": "ERROR", "Error": str(error) or error.__class__.__name__},
    )


def _retry_wait(attempt: int) -> float:
    """
    Seconds to wait before retry number ``attempt`` (0-based).

    Uses base * 2**attempt so the documented 1s / 2s / 4s progression holds.
    The previous base**(attempt+1) formula degenerated to a flat 1s with the
    shipped default of retry_base_wait: 1.
    """
    try:
        base = float(config.retry_base_wait)
    except (TypeError, ValueError):
        base = 1.0
    return min(base * (2 ** attempt), 30.0)


def audit_device(
    ip: str,
    username: str,
    password: str,
    enable_secret: str | None,
    jump_host: str | None,
    stale_days: int,
    debug: bool = False,
    jump: JumpManager | None = None,
) -> tuple[str, str | None, list[dict[str, Any]], dict[str, Any]]:
    """
    Connect to a single device and perform an audit, with backoff retries.

    - Establishes an SSH session (direct or via jump host) with retry logic
    - Collects interface data using TextFSM
    - Enriches with PoE and LLDP/CDP neighbours
    - Computes stale flags and error counters
    - Builds a per-device summary dictionary

    Args:
        jump: A shared JumpManager to multiplex through. When omitted and
            ``jump_host`` is set, a private one is created and closed here.
            Callers auditing many devices should pass a shared instance so the
            bastion sees one login for the whole run.

    Returns:
        (ip, hostname, detailed_records, summary_dict). Failures are reported
        through the summary rather than by raising, so one unreachable device
        cannot abort a whole run.
    """
    owns_jump = False
    if jump is None and jump_host:
        jump = JumpManager(jump_host, username, password)
        owns_jump = True

    attempts = max(1, int(config.retry_attempts or 1))
    last_error: BaseException | None = None

    try:
        for attempt in range(attempts):
            conn = None
            try:
                conn = connect_to_device(ip, username, password, jump=jump)
                return _audit_connected_device(conn, ip, enable_secret, stale_days, debug)
            except Exception as exc:  # noqa: BLE001 - see note below
                # Netmiko and paramiko failures (timeouts, auth rejections)
                # derive from SSHException, not OSError, so the previous narrow
                # except never retried them and let them escape to the caller.
                last_error = exc
                log.debug(
                    "[%s] attempt %d/%d failed: %s", ip, attempt + 1, attempts, exc,
                    exc_info=debug,
                )
                if attempt < attempts - 1:
                    wait_time = _retry_wait(attempt)
                    log.info(
                        "[%s] attempt %d/%d failed (%s); retrying in %.0fs",
                        ip, attempt + 1, attempts, str(exc)[:120], wait_time,
                    )
                    time.sleep(wait_time)
            finally:
                if conn is not None:
                    try:
                        conn.disconnect()
                    except Exception:  # noqa: BLE001 - teardown must never raise
                        log.debug("[%s] error closing session", ip, exc_info=True)
    finally:
        if owns_jump and jump is not None:
            jump.close()

    log.error("[%s] all %d attempt(s) failed: %s", ip, attempts, last_error)
    return ip, None, [], error_summary(ip, last_error or "connection failed")


def _audit_connected_device(
    conn,
    ip: str,
    enable_secret: str | None,
    stale_days: int,
    debug: bool
) -> tuple[str, str | None, list[dict[str, Any]], dict[str, Any]]:
    """
    Perform the audit once connected to a device.
    
    Extended logic uses 'show interfaces status' (fixed-width parser) to classify 
    Mode (access/trunk/routed) and fill VLAN reliably, overriding the TextFSM 
    'show interfaces' record when present.
    """
    hostname: str | None = None

    # --- Identify hostname
    try:
        out_hn = conn.send_command("show running-config | include ^hostname ", read_timeout=config.read_timeout)
        m = re.search(r"^hostname\s+(\S+)", out_hn, flags=re.M)
        if m:
            hostname = m.group(1).strip()
    except Exception:  # noqa: BLE001 - fall through to find_prompt()
        log.debug("hostname lookup via running-config failed for %s", ip, exc_info=True)
    if not hostname:
        try:
            hostname = conn.find_prompt().rstrip(">#").strip()
        except Exception:  # noqa: BLE001 - the IP is a usable fallback name
            log.debug("find_prompt() failed for %s", ip, exc_info=True)
            hostname = None

    # --- Enter enable mode if a secret is configured
    if enable_secret:
        try:
            conn.enable()
            log.debug("[%s] enable mode entered", hostname or ip)
        except Exception as e:  # noqa: BLE001 - continue at whatever privilege we have
            log.warning("[%s] enable failed: %s", hostname or ip, e)

    # --- Get version and hardware info (single command)
    ios_version = ""
    hardware = {'model': '', 'serial': '', 'memory': ''}
    try:
        out_ver = conn.send_command("show version", read_timeout=config.read_timeout, use_textfsm=False)
        ios_version = _parse_ios_version(out_ver)
        hardware = _parse_hardware_from_version(out_ver)
    except Exception:  # noqa: BLE001 - version detail is not essential
        log.debug("'show version' failed for %s", ip, exc_info=True)

    # --- Primary interface records via TextFSM
    tfsm_records = get_interfaces_via_show_interfaces(conn)
    detailed: list[dict[str, Any]] = []

    # --- Build a per-interface mode/VLAN map from 'show interfaces status'
    sif_status_map: dict[str, dict[str, str]] = {}
    try:
        out_status = conn.send_command("show interfaces status", read_timeout=config.read_timeout, use_textfsm=False)
        for rec in parse_show_interfaces_status(out_status):
            # Normalize status row port name to short form (e.g., 'Gi1/0/1')
            port_norm = normalize_ifname(rec.get('port', ''))[0]
            if not port_norm:
                continue

            vlan_raw   = (rec.get('vlan')   or '').strip().lower()
            status_raw = (rec.get('status') or '').strip().lower()

            if vlan_raw in {'trunk', 'rspan'}:
                mode = 'trunk'
                vlan = vlan_raw
            elif vlan_raw == 'routed':
                mode = 'routed'
                vlan = vlan_raw
            else:
                # numeric/none/blank -> treat as access
                mode = 'access'
                vlan = vlan_raw

            # Stash status so we can override the final Status if present
            sif_status_map[port_norm] = {'mode': mode, 'vlan': vlan, 'status': status_raw}
    except Exception:  # noqa: BLE001 - fall back to 'show interfaces' fields
        log.debug("'show interfaces status' failed for %s", ip, exc_info=True)
        sif_status_map = {}

    # --- Optional collectors (config.yaml -> collectors:)
    collected = run_collectors(conn)
    vlan_names = collected.device.pop("_vlan_names", {}) or {}
    extra_sheets = collected.sheets
    # Union of the per-port columns any collector actually returned, so every
    # row on the device sheet carries the same columns and filters behave.
    collector_columns = sorted({
        column for fields in collected.interfaces.values() for column in fields
    })
    zero_columns = {c for c in enabled_zero_columns() if c in collector_columns}

    if tfsm_records:
        # Enrichment: PoE + neighbors
        try:
            out_poe = conn.send_command("show power inline", read_timeout=config.read_timeout, use_textfsm=False)
            poe_map = parse_show_power_inline(out_poe)
        except Exception:  # noqa: BLE001 - non-PoE switches reject this command
            log.debug("'show power inline' unavailable on %s", ip, exc_info=True)
            poe_map = {}

        lldp_ifaces = _get_lldp_neighbors(conn)
        cdp_ifaces = _get_cdp_neighbors(conn)

        for r in tfsm_records:
            port_raw = r.get('interface') or ""
            short_port, long_port = normalize_ifname(port_raw)
            if not short_port:
                continue

            description = r.get('description', '')

            # --- Normalize operational/admin/protocol state into a canonical Status
            line_state  = (r.get('link_status') or r.get('status') or '').strip().lower().replace('_', '-')
            admin_state = (r.get('admin_state') or '').strip().lower().replace('_', '-')
            proto_state = (r.get('protocol') or r.get('protocol_status') or '').strip().lower().replace('_', '-')

            def _canon(s: str) -> str:
                return ' '.join(s.split())

            line_state  = _canon(line_state)
            admin_state = _canon(admin_state)
            proto_state = _canon(proto_state)

            # Base status from detailed ('show interfaces') fields
            if "administratively down" in (line_state, admin_state, proto_state):
                status = "administratively down"
            elif "err-disabled" in (line_state, admin_state, proto_state) or "errdisabled" in (line_state, admin_state, proto_state):
                status = "err-disabled"
            elif line_state == "up" and proto_state == "up":
                status = "connected"
            elif line_state == "down" and proto_state == "down":
                status = "notconnect"
            elif line_state == "up":
                status = "connected"
            elif line_state == "down":
                status = "notconnect"
            else:
                status = "unknown"

            _ovr = None
            if short_port in sif_status_map:
                _ovr = (sif_status_map[short_port].get('status') or '').strip().lower()
            else:
                for alias in all_aliases(short_port):
                    if alias in sif_status_map:
                        _ovr = (sif_status_map[alias].get('status') or '').strip().lower()
                        break

            if _ovr:
                # Normalize common variants to canonical values
                if _ovr in {"errdisabled"}:
                    _ovr = "err-disabled"
                # Accept typical table statuses as-is
                if _ovr in {"connected", "notconnect", "administratively down", "err-disabled", "inactive", "monitoring"}:
                    status = _ovr

            # OLD values (kept as fallback if 'show interfaces status' is unavailable)
            vlan = (r.get('vlan') or '').lower()
            is_trunk = vlan in {"trunk", "rspan"}
            mode = "trunk" if is_trunk else ("routed" if vlan == "routed" else "access")

            # NEW: Prefer classification from 'show interfaces status'
            if short_port in sif_status_map:
                mode = sif_status_map[short_port]['mode']
                vlan = sif_status_map[short_port]['vlan']
            else:
                # Try aliases just in case there are naming differences
                for alias in all_aliases(short_port):
                    if alias in sif_status_map:
                        mode = sif_status_map[alias]['mode']
                        vlan = sif_status_map[alias]['vlan']
                        break

            duplex = (r.get('duplex') or '').lower()
            speed = (str(r.get('speed') or '')).lower()
            hwtype = r.get('hardware_type', '')

            # Error counters
            try:
                in_err = int(r.get('input_errors') or 0)
            except (ValueError, TypeError):
                in_err = 0
            try:
                out_err = int(r.get('output_errors') or 0)
            except (ValueError, TypeError):
                out_err = 0
            try:
                crc_err = int(r.get('crc') or 0)
            except (ValueError, TypeError):
                crc_err = 0

            # Last input / output times
            last_input_raw = r.get('last_input', '') or r.get('last_input_text', '') or ''
            last_input_secs = _parse_last_input_seconds(last_input_raw) if last_input_raw else None

            last_output_raw = r.get('last_output', '') or r.get('last_output_text', '') or ''

            # PoE lookup using aliases (short/long/raw)
            poe = {}
            for alias in all_aliases(short_port):
                if alias in poe_map:
                    poe = poe_map[alias]
                    break

            # Convert "7.0 W" => 7.0
            poe_active = _poe_watts(poe.get('poe_power_w')) > 0.0

            has_neighbor = (short_port in lldp_ifaces) or (short_port in cdp_ifaces)

            # Fields contributed by the optional collectors for this port
            extra = dict(collected.interfaces.get(short_port, {}))
            try:
                mac_count = int(extra.get("MAC Count") or 0)
            except (TypeError, ValueError):
                mac_count = 0

            # Conservative stale definition for access ports
            stale_flag = None
            if stale_days > 0 and mode == "access":
                out_of_service = status in {"administratively down", "err-disabled"}
                if out_of_service and not config.stale_include_admin_down:
                    # Already out of service, and reported under their own
                    # categories. Counting them as stale as well double-reported
                    # them and made the "Stale Ports" total disagree with the
                    # number of ports labelled 'stale-unused'.
                    stale_flag = False
                elif status == "connected":
                    stale_flag = (last_input_secs is not None and last_input_secs >= (stale_days * 86400))
                else:
                    # A port with learned MACs is demonstrably in use even when
                    # the endpoint speaks no discovery protocol and draws no PoE.
                    in_use = poe_active or has_neighbor
                    if config.stale_use_mac_table and mac_count > 0:
                        in_use = True
                    stale_flag = not in_use

            record = {
                'Device': hostname or ip,
                'Mgmt IP': ip,
                'Interface': long_port,
                'Description': description,
                'Status': status,
                'AdminDown': status == "administratively down",
                'Connected': status == "connected",
                'ErrDisabled': status == "err-disabled",
                'Mode': mode,
                'VLAN': vlan,
                'Duplex': duplex,
                'Speed': speed,
                'Type': hwtype,
                'Input Errors': in_err,
                'Output Errors': out_err,
                'CRC Errors': crc_err,
                'Last Input': last_input_raw,
                'Last Output': last_output_raw,
                'PoE Power (W)': poe.get('poe_power_w', ''),
                'PoE Oper': poe.get('poe_oper', ''),
                'PoE Admin': poe.get('poe_admin', ''),
                'LLDP/ CDP Neighbor': has_neighbor,
                f'Stale (≥{stale_days} d)': stale_flag,
            }

            # Columns contributed by the optional collectors. Seeded first, in
            # a stable order, so every row on the sheet has identical columns
            # whether or not a collector had data for that port.
            for column in collector_columns:
                record.setdefault(column, 0 if column in zero_columns else None)
            record.update(extra)
            if vlan_names:
                record['VLAN Name'] = vlan_names.get(str(vlan), '')

            # Add port category if enabled
            if config.enable_port_categorization:
                record['Port Category'] = _categorize_port(record, stale_days)

            detailed.append(record)

    # --- Build summary
    device_name = hostname or ip
    summary = base_summary(
        device_name,
        ip,
        Model=hardware.get("model", ""),
        Serial=hardware.get("serial", ""),
    )
    summary["IOS Version"] = ios_version

    df = pd.DataFrame(detailed)
    if not df.empty:
        stale_col = f"Stale (\u2265{stale_days} d)"
        total = len(df)

        def _count(column: str, value: str) -> int:
            return int((df[column] == value).sum()) if column in df.columns else 0

        access_cnt = _count("Mode", "access")
        trunk_cnt = _count("Mode", "trunk")
        routed_cnt = _count("Mode", "routed")
        connected_cnt = _count("Status", "connected")
        notconnect_cnt = _count("Status", "notconnect")
        admindown_cnt = _count("Status", "administratively down")
        errdisabled_cnt = _count("Status", "err-disabled")

        stale_cnt = int(df[stale_col].fillna(False).astype(bool).sum()) if stale_col in df.columns else 0

        err_cols = [c for c in ("Input Errors", "Output Errors", "CRC Errors") if c in df.columns]
        if err_cols:
            err_ports = int((df[err_cols].sum(axis=1) > 0).sum())
        else:
            err_ports = 0

        poe_active = 0
        if "PoE Power (W)" in df.columns:
            poe_active = int(sum(1 for v in df["PoE Power (W)"] if _poe_watts(v) > 0.0))

        def _pct(n: int, d: int) -> float:
            return round((n / d * 100.0), 1) if d else 0.0

        summary.update({
            "Total Ports (phy)": total,
            "Access Ports": access_cnt,
            "Trunk Ports": trunk_cnt,
            "Routed Ports": routed_cnt,
            "Connected": connected_cnt,
            "Not Connected": notconnect_cnt,
            "Admin Down": admindown_cnt,
            "Err-Disabled": errdisabled_cnt,
            "Stale Ports": stale_cnt,
            "Ports w/ Errors": err_ports,
            "PoE Active Ports": poe_active,
            "% Access of Total": _pct(access_cnt, total),
            "% Trunk of Total": _pct(trunk_cnt, total),
            "% Routed of Total": _pct(routed_cnt, total),
            "% Connected of Total": _pct(connected_cnt, total),
            "% AdminDown of Total": _pct(admindown_cnt, total),
        })
    else:
        # Reached the device but got no parsable interface data — a real
        # condition worth flagging rather than reporting as a clean zero-port run.
        summary["Audit Result"] = "NO DATA"
        summary["Error"] = "Connected, but no interface records could be parsed"

    # Device-level facts from the collectors. The Error column stays last so the
    # SUMMARY sheet keeps its shape.
    trailing_error = summary.pop("Error", "")
    for key, value in collected.device.items():
        if not key.startswith("_"):
            summary[key] = value
    summary["Error"] = trailing_error

    # Extra worksheets travel under an underscore-prefixed key; the reporter
    # strips every such key before writing, so they never become columns.
    if extra_sheets:
        summary["_extra_sheets"] = {
            name: [{"Device": device_name, "Mgmt IP": ip, **row} for row in rows]
            for name, rows in extra_sheets.items()
        }

    return ip, hostname, detailed, summary
