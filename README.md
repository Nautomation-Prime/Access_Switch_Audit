
# Network Automation Utilities — Cisco Access Switch Audit ➜ Excel

[![Tests](https://github.com/Nautomation-Prime/Access_Switch_Audit/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/Nautomation-Prime/Access_Switch_Audit/actions/workflows/test.yml?query=branch%3Amain)

A modular Python utility that connects to Cisco switches (optionally through an SSH jump host),
collects interface details, PoE information, and neighbor presence, then exports a clean,
**filters‑only** Excel workbook with a **SUMMARY** sheet and one sheet per device.

This README provides a full, practical guide to install, configure, run, and troubleshoot the tool.

> **Running this in anger?** See the [Operations Runbook](docs/RUNBOOK.md) — setup, scheduled
> runs, credential and host-key handling, reading the workbook, exit codes, and a
> symptom-by-symptom troubleshooting section. Also available as a
> [styled HTML page](docs/RUNBOOK.html).

---

## ✨ What this tool does

- **Audits access ports** across multiple Cisco switches in parallel.
- **Normalizes** interface names (e.g., `GigabitEthernet1/0/1` → `Gi1/0/1`) for cross‑command matching.
- **Enriches** interfaces with:
  - PoE draw and state (`show power inline`)
  - LLDP/CDP neighbor presence (`show lldp neighbors detail`, `show cdp neighbors detail`)
  - Access and **voice** VLANs, trunk native/allowed VLANs (`show interfaces switchport`)
  - MAC addresses learned per port, with vendor lookup (`show mac address-table`)
  - Spanning-tree role, state and **PortFast/edge** status (`show spanning-tree`)
  - Optical transceiver Tx/Rx levels (`show interfaces transceiver`)
  - Optionally 802.1X/MAB sessions, port-security counters and port-channel membership
- **Records device health**: uptime, reload reason, config register, PSU and
  temperature state, chassis PoE budget, stack members, and a full module and
  optic **inventory with serial numbers**.
- **Classifies port mode & VLAN** using `show interfaces status` (access / trunk / routed).
- **Flags stale access ports** using conservative rules, informed by PoE draw,
  neighbour presence and learned MACs.
- **Verifies SSH host keys** for the bastion and every device.
- **Exports Excel** with an at‑a‑glance **SUMMARY** and one sheet per device, with filters, frozen header, column auto‑size, and conditional formatting.
- **Shows a progress bar** while running concurrent device jobs.

> Design note: The workbook intentionally uses **filters only** (no Excel tables), and places **SUMMARY first**.

---

## 🧱 Project layout

```text
.
├── .github/
│   └── workflows/
│       └── test.yml      # GitHub Actions pytest workflow
├── pyproject.toml         # Canonical package metadata and version
├── switch_audit/           # Main package (modular design)
│   ├── __about__.py       # Package metadata exports
│   ├── __init__.py
│   ├── __main__.py        # Entry point for python -m switch_audit
│   ├── cli.py             # Command-line interface and orchestration
│   ├── device_auditor.py  # Device connection and data collection
│   ├── excel_reporter.py  # Excel workbook generation
│   ├── collectors.py      # Optional enrichment collectors (config-driven)
│   ├── credentials.py     # Secure credential management
│   ├── env_file.py        # .env loader (no third-party dependency)
│   ├── oui.py             # MAC OUI to vendor lookup
│   ├── ssh_security.py    # SSH host key verification policy
│   ├── logging_setup.py   # Rotating file + console logging
│   ├── jump_manager.py    # SSH jump host (bastion) support
│   ├── netmiko_utils.py   # Network device connection utilities
│   ├── formatters.py      # Excel formatting and interface name normalization
│   ├── tui.py             # Full-screen Textual terminal UI
│   ├── validators.py      # Input validation functions
│   ├── options.py         # Validated run options shared by the CLI and TUI (pydantic)
│   ├── version.py         # Version helper backed by pyproject.toml
│   └── app_config.py      # Configuration access wrapper
├── assets/
│   ├── config_loader.py   # YAML configuration loader
│   └── config_schema.py   # Typed config.yaml schema (pydantic models)
├── config.yaml            # User-editable configuration
├── config.schema.json     # JSON Schema for config.yaml (editor completion)
├── devices.txt            # Device list (one IP/hostname per line)
├── output/                # Default location for generated workbooks
├── setup.bat              # First-time setup (portable Python + dependencies)
├── run.bat                # Windows launcher script
├── docs/
│   ├── RUNBOOK.md         # Operations runbook (source of truth)
│   └── RUNBOOK.html       # The same runbook as a standalone page
├── .env.example           # Copy to .env for credentials/overrides
├── .gitattributes         # Line-ending normalisation (LF for *.sh)
├── tests/                 # Pytest suite: parsers, reporter, bastion, end-to-end
├── requirements.txt       # Python dependencies (lower bounds)
├── requirements.lock.txt  # Pinned, cross-platform lock used by the installers
└── README.md              # This guide
```

> The modular structure separates concerns, making it easy to extend, test, and maintain.
> Each module has a single, well-defined responsibility.

---

## 📌 Reproducible installs

`requirements.txt` carries lower bounds; `requirements.lock.txt` is a pinned,
cross-platform lock (generated with `uv pip compile --universal`). Both
`setup.sh` and `setup.bat` install from the lock when it is present, so every
machine resolves to the same versions.

```bash
# Regenerate the lock after changing requirements.txt
uv pip compile requirements.txt --universal --python-version 3.10 \
    --output-file requirements.lock.txt
```

Delete `requirements.lock.txt` to let the installers float to the newest
compatible releases.

---

## 📦 Requirements

- **Python**: 3.10+
- **Python packages** (install with pip):
  - `netmiko`
  - `paramiko`
  - `pandas`
  - `openpyxl`
  - `pydantic` *(validates `config.yaml` and run options)*
  - `pywin32` *(Windows only; used for Windows Credential Manager integration)*

```bash
pip install -r requirements.txt

# Optional developer / packaging-aligned install
pip install -e .[dev]
```

The canonical application version now lives in `pyproject.toml`, which keeps package metadata,
CLI `--version`, and the TUI subtitle aligned from one source.

### Optional but recommended

- **TextFSM templates** (NTC templates) for robust parsing of `show interfaces` when `use_textfsm=True`.
  - If templates are available and the `NET_TEXTFSM` environment variable points to them, parsing accuracy improves.
  - If not available, the script still works and falls back where needed (e.g., it has its own
    fixed‑width parser for `show interfaces status`).

---

## 🔐 Credentials & Security

Credentials are resolved by `switch_audit/credentials.py`, in this order:

1. **Environment variables** — `SWITCH_USER` / `SWITCH_PASS`, then `CREDENTIAL_USER` / `CREDENTIAL_PASS`.
2. **Windows Credential Manager** — target name from `config.yaml` (`credentials.target`), overridable
   with `CREDENTIAL_TARGET`.
3. **Interactive prompt** — username, then a non-echoed password.

- **Enable secret**: used only when `USE_ENABLE` is truthy; the secret is read from `ENABLE_SECRET`.
- On Linux/macOS, Credential Manager is unavailable — use the environment or the prompt.
  `pywin32` enables Credential Manager access on Windows.

### `.env` file

Copy `.env.example` to `.env` and fill it in. It is read automatically at start-up, and
**variables already set in your shell always take precedence**. `.env` is gitignored.

### Unattended runs

Pass `--non-interactive` for scheduled tasks and CI. The tool will then never read from stdin:
credentials must come from the environment or Credential Manager, and a missing credential is a clean
error instead of a process that hangs forever waiting on a prompt.

**Never** hard-code credentials in the repository. Use the secure store, `.env`, or the prompt.

### SSH host key verification

Both the bastion session and each device session verify the SSH host key. The
policy is set by `security.host_key_policy` in `config.yaml`:

| Policy | Behaviour |
|---|---|
| `strict` | Only hosts already in `known_hosts` are accepted. Populate it first (`ssh-keyscan`), or connections are refused. |
| `auto-add` *(default)* | Trust on first use. An unknown host is accepted and recorded; a host whose key has **changed** is refused. |
| `insecure` | Accept any key, remember nothing. Lab use only. |

```yaml
security:
  host_key_policy: auto-add
  known_hosts: null      # null = ~/.ssh/known_hosts; supports ~ and $VARS
  save_host_keys: true   # persist newly accepted keys (auto-add only)
```

Overridable with `SWITCH_AUDIT_HOST_KEY_POLICY` and `SWITCH_AUDIT_KNOWN_HOSTS`.

> A key mismatch is reported explicitly rather than swallowed: it means the host
> was rebuilt, or the session is being intercepted. Verify the new fingerprint
> out of band before clearing the stale `known_hosts` entry.
>
> Netmiko does not write host keys back, so under `auto-add` a **device** key is
> verified only once it is already recorded in `known_hosts`. The bastion key,
> which is the one shared by every connection, is recorded on first use.

---

## 🛰️ Jump host (bastion) behaviour

- The tool reads `jump_host` from `config.yaml`.
- **Default**: the tool **uses the jump host** if `--direct` is *not* supplied.
- `--direct` will skip the jump host entirely and attempt direct SSH connections.

```yaml
# config.yaml (example)
jump_host: jump-gateway.example.com  # or null to disable by default
```

- `--jump-host HOST` overrides `config.yaml` for a single run (ignored when `--direct` is given).

> The `JumpManager` opens **one** SSH session to the bastion for the whole run and multiplexes a
> `direct-tcpip` channel per device through it, rather than authenticating once per device — which
> on a busy inventory trips bastion rate-limiting or account lockout. The session carries a keepalive
> and is rebuilt automatically if the bastion drops it mid-run.
>
> If the bastion cannot be reached, the run stops immediately rather than failing device by device.
> You can also override the jump host using the `JUMP_HOST` environment variable.

---

## 🗂️ Device list file

Provide a plain‑text file with **one device per line**. Lines that are blank or start with `#` are ignored.

```text
# devices.txt
192.0.2.11
192.0.2.12  # inline comments are not parsed; this whole token must be a host/IP only
core-switch-01
access-sw-22
```

> Hostnames must be resolvable from the machine (or via the jump host, depending on your SSH setup).

---

## 🚀 Quick start

1. **Install dependencies** (see [Requirements](#-requirements)).
2. **Create `devices.txt`** with your targets (see [Device list file](#%EF%B8%8F-device-list-file)).
3. **(Optional) Configure** `config.yaml` with your `jump_host` and other settings.
4. **Run** the audit:

```bash
# Using jump host from config.yaml
python -m switch_audit --devices devices.txt --output access_port_audit.xlsx

# Check the packaged version metadata
python -m switch_audit --version

# Or use the Windows launcher:
run.bat

# Direct connections (no bastion), 5 workers, different stale threshold
python -m switch_audit --direct -w 5 --stale-days 60 -d devices.txt -o results.xlsx

# Verbose debugging
python -m switch_audit --debug --devices devices.txt

# If installed with pip, the console script is also available
cisco-access-switch-audit --devices devices.txt --output access_port_audit.xlsx
```

The tool prints an event‑driven progress bar like:

```text
Progress: [██████████░░░░░░░░░░░░░░] 12/30 started: 15/30
```

On completion, the Excel workbook is written to the filename you specify (default `audit.xlsx`).

---

## 🧭 CLI reference

The `switch_audit` module exposes the following command‑line options:

```text
--devices, -d     (optional)  Path to the devices file (one IP/hostname per line; '#' comments allowed). Default: devices.txt
--output,  -o     (optional)  Output Excel file name. Default: output/audit.xlsx (from config.yaml)
--workers, -w     (optional)  Max concurrent device sessions (threads). Default: 10 (from config.yaml)
--stale-days      (optional)  Days threshold for stale access ports. 0 disables stale flagging. Default: 30 (from config.yaml)
--direct          (optional)  Connect directly (do not use jump host)
--jump-host       (optional)  Bastion host for this run; overrides config.yaml. Ignored with --direct
--non-interactive (optional)  Never prompt for input; fail cleanly if credentials are unavailable
--list-collectors (optional)  List the optional collectors and whether each is enabled, then exit
--show-config     (optional)  Print the effective configuration (config.yaml + defaults + env overrides), then exit
--config-schema   (optional)  Print the JSON Schema for config.yaml, then exit
--tui             (optional)  Launch the full-screen Textual terminal UI
--debug           (optional)  Enable verbose logging/prints
--version         (optional)  Print version information and exit
```

### Exit codes

| Code | Meaning |
|-----:|---------|
| `0`  | At least one device was audited successfully (the workbook was written) |
| `1`  | Fatal error, or every device failed |
| `2`  | `config.yaml` (or an environment override) failed validation; nothing was run |

A run where *some* devices fail still exits `0` and still writes the workbook: failures appear in
`SUMMARY` with `Audit Result = ERROR` and the reason in the `Error` column.

### Required vs optional

- **Required**: none if you use the default `devices.txt`; pass `--devices` to override it
- **Optional**: everything else

> All defaults can be customized in `config.yaml`.

### Configuration validation

Every value in `config.yaml` is validated at startup against a typed schema
(`assets/config_schema.py`). A wrong type, an out-of-range number or a misspelt
host key policy stops the tool before it connects to anything, with a message
naming the key:

```text
Configuration error: Invalid configuration in config.yaml:
  - connection.timeout: Input should be greater than 0 (got -5)
  - security.host_key_policy: Unknown host_key_policy 'strcit'. Valid values: strict, auto-add, insecure
```

Environment overrides such as `CONNECTION_TIMEOUT` go through the same checks.
Unknown keys are logged as warnings rather than rejected, so a config written
for another release still loads. `--show-config` prints what the tool will
actually use.

`config.yaml` declares `config.schema.json` on its first line, so editors with
YAML language support (e.g. VS Code's Red Hat YAML extension) offer completion,
hover documentation and inline errors as you edit.

---

## 🧪 Running tests

The pytest suite covers the show-command parsers, the workbook writer, the bastion session, the
credential and configuration fallbacks, and a full argv-to-workbook run against a fake switch.
GitHub Actions runs it on Python 3.10-3.13 on Linux plus Python 3.12 on Windows, and separately
lints with `ruff` and `yamllint`.

```bash
# Local development environment
pip install -e .[dev]
pytest tests/ -v --tb=short

# Bundled runtime on Windows
python_runtime\python.exe -m pytest tests/ -v --tb=short
```

---

## 🧪 What the script collects

For each device the script attempts to gather:

- **Hostname** (from `show running-config | include ^hostname` or CLI prompt fallback)
- **Interfaces** via TextFSM (`show interfaces`) when available
- **Port mode & VLAN** via a robust, fixed‑width parser of `show interfaces status`
- **PoE details**: admin/oper state, power draw (W), class, device (`show power inline`)
- **Neighbor presence**: LLDP/CDP seen on the port (boolean)
- **Error counters**: input, output, CRC (from `show interfaces` parsed data)
- **Activity indicator**: "Last input" time (seconds parsed when present)

Plus whatever the enabled [optional collectors](#-optional-collectors) add —
voice VLANs, learned MACs, spanning-tree edge status, optics, inventory,
environment and PoE budget.

---

## 🛑 Stale logic — how ports are flagged

A conservative approach is used **only for ports in `access` mode** and when `--stale-days > 0`:

- **If Status = `administratively down` or `err-disabled`** → **never stale**. These ports are already
  out of service and are reported under their own categories; counting them again inflated the total.
- **If Status = `connected`** → mark **stale = True** **only** if `Last input ≥ <stale-days>`.
- **Otherwise** → mark **stale = True** when **all** of these hold:
  1. **No PoE draw** (PoE power is blank/`-`/0.0), and
  2. **No LLDP/CDP neighbor present** on the port, and
  3. **No MAC addresses learned** on the port (requires the `mac_table`
     collector; this is what catches printers, IoT devices and unmanaged
     switches, which speak no discovery protocol and draw no PoE).

Both extra rules are configurable:

```yaml
audit:
  stale:
    include_admin_down: false   # count admin-down ports as stale
    use_mac_table: true         # treat learned MACs as proof of use
```

This tends to avoid false positives on trunk/routed ports and on access ports actively in use.

> You can disable stale flagging entirely by setting `--stale-days 0`.

---

## 🔍 Optional collectors

Beyond the core interface audit, the tool runs a set of **optional collectors**,
each of which issues one extra `show` command and contributes extra columns.
They are switched individually under `collectors:` in `config.yaml`, so you can
trade audit depth against runtime.

```bash
python -m switch_audit --list-collectors
```

### Per-port enrichment

| Collector | Command | Adds |
|---|---|---|
| `switchport` | `show interfaces switchport` | `Admin Mode`, `Access VLAN`, **`Voice VLAN`**, `Native VLAN`, `Trunk VLANs`, `Negotiation` |
| `mac_table` | `show mac address-table` | `MAC Count`, `MAC Addresses`, `MAC Vendors`, `MAC VLANs` |
| `vlans` | `show vlan` | `VLAN Name` (plus `VLANs Defined` / `VLANs Active`) |
| `spanning_tree` | `show spanning-tree` | `STP Role`, `STP State`, `STP Type`, **`STP Edge`** (PortFast) |
| `transceivers` | `show interfaces transceiver` | `Optic Temp (C)`, `Optic Voltage (V)`, `Optic Tx (dBm)`, `Optic Rx (dBm)` |
| `dot1x` | `show authentication sessions` | `Auth Method`, `Auth Domain`, `Auth Status`, `Auth MAC` |
| `etherchannel` | `show etherchannel summary` | `Port-Channel` and bundle status |
| `port_security` | `show port-security` | `PortSec Max`, `PortSec Current`, `PortSec Violations`, `PortSec Action` |

### Device-level facts (SUMMARY sheet)

| Collector | Command | Adds |
|---|---|---|
| `version_detail` | `show version` | `Uptime`, `Reload Reason`, `Config Register` (+ `Config Register OK`), `ROMMON`, `Running Image`, `Base MAC`, `Stack Members` / `Models` / `Serials` |
| `inventory` | `show inventory` | An **INVENTORY** worksheet: module and optic PIDs, VIDs and serial numbers |
| `environment` | `show environment power all`, `... temperature` | `PSUs Present`, `PSUs OK`, `PSU Status`, `Inlet Temp (C)`, `Temp Status` |
| `poe_budget` | `show power inline` | `PoE Available (W)`, `PoE Used (W)`, `PoE Remaining (W)`, `PoE Used %` |
| `cpu_memory` | `show processes cpu` | `CPU 5s %`, `CPU 1m %`, `CPU 5m %` |

Parsing uses the TextFSM templates bundled with Netmiko (`ntc-templates`) where
one exists, and a purpose-written parser only where none does.

> **Every collector is best-effort.** A platform that does not implement a
> command (a non-PoE switch, a device with no optics, an image without
> `show authentication sessions`) simply contributes nothing — it never fails
> the device, and never fails the run.

### MAC vendor lookup

`mac_table` resolves the OUI of each learned MAC against a small built-in table
covering phones, APs, printers, cameras and common NIC vendors. Point
`audit.oui_file` at a CSV (`oui,vendor` per line) — the full IEEE registry, or
your own inventory — to extend or override it.

```yaml
audit:
  mac_addresses_per_port: 5     # how many MACs to list per port
  oui_file: null                # e.g. /etc/switch-audit/oui.csv
```

---

## 📤 Excel output structure

The workbook contains:

### 1) `SUMMARY` sheet (first)

- One row per device — including devices that failed — with a final **TOTAL** row.
- The TOTAL row **sums count columns** and **recomputes percentages** from those sums
  (adding percentages down the column would give meaningless figures such as 160%).
- Columns:
  - `Device`, `Mgmt IP`, `Audit Result` (`OK` / `ERROR` / `NO DATA`)
  - `Model`, `Serial`, `IOS Version`
  - `Total Ports (phy)`, `Access Ports`, `Trunk Ports`, `Routed Ports`
  - `Connected`, `Not Connected`, `Admin Down`, `Err-Disabled`
  - `Stale Ports`, `Ports w/ Errors`, `PoE Active Ports`
  - `% Access of Total`, `% Trunk of Total`, `% Routed of Total`, `% Connected of Total`,
    `% AdminDown of Total`
  - `Error` — the failure reason, blank on success

Every device contributes the same columns in the same order, so the sheet stays rectangular
whether the run succeeded or not.

### 2) One sheet per device

Columns typically include (when available):

- `Device`, `Mgmt IP`, `Interface` (long form), `Description`
- `Status` (normalized: connected / notconnect / administratively down / err-disabled)
- `AdminDown`, `Connected`, `ErrDisabled` (booleans for quick filters)
- `Mode` (access/trunk/routed), `VLAN`
- `Duplex`, `Speed`, `Type`
- `Input Errors`, `Output Errors`, `CRC Errors`
- `Last Input` (raw text)
- `PoE Power (W)`, `PoE Oper`, `PoE Admin`
- `LLDP/ CDP Neighbor` (boolean)
- `Stale (≥<N> d)` (boolean)

### Formatting

- **Frozen header** (`A2`) and **AutoFilter** across all columns
- **Auto‑sized columns** with sensible min/max widths
- **Conditional formatting**:
  - `Status = connected` → green
  - `Status = notconnect` / `err-disabled` → red
  - `Status = administratively down` → grey
  - Any `*Errors` > 0 → red
  - `PoE Power (W)` > 0 → green
  - `Stale (≥N d)` = TRUE → red

---

## ⚙️ Performance & concurrency

- Uses a `ThreadPoolExecutor` with `--workers` threads (default 10).
- An **event-driven progress bar** updates as jobs start/finish. It is suppressed automatically when
  stdout is not a terminal, so redirected/scheduled runs produce readable logs.
- Each device is independent; a failure on one does **not** stop others, and partial results are
  still written to the workbook.
- Failed connections are retried with exponential backoff (`retry_base_wait × 2ⁿ`, capped at 30s);
  see `connection.retries` in `config.yaml`.
- When a jump host is used, all workers share one bastion session.

---

## 🐛 Logging, debug, and errors

- Add `--debug` to surface additional prints (e.g., enable mode attempts, jump host info, file counts).
- Per‑device errors are captured into the device’s summary row (and a minimal sheet may be created
  with the error text so the workbook always reflects all devices).

Common runtime issues & tips:

- **Authentication failures** → check Credential Manager entry or typed credentials.
- **SSH connectivity** → verify reachability from the workstation *or* via the jump host.
- **TextFSM templates missing** → parsing still proceeds, but some fields may be blank.
- **Channel/line rate limits** on older devices → consider lowering `--workers`.

---

## 🔧 Extending and customizing

- **Configuration**: declare a new setting with its type, default and limits in `assets/config_schema.py`, expose it from `assets/config_loader.py`, then regenerate the editor schema with `python -m switch_audit --config-schema > config.schema.json` (a test fails if you forget).
- **Credentials**: adapt `switch_audit/credentials.py` to your environment (Linux keyring, Azure Key Vault, etc.).
- **Jump host**: tune `switch_audit/jump_manager.py` (keep‑alive, ciphers, auth methods) as needed.
- **Connection behaviour**: modify `switch_audit/netmiko_utils.py` for device types, timeouts, or SSH options.
- **Output columns**: adjust record construction in `switch_audit/device_auditor.py` (search for `detailed.append(record)`).
- **Conditional formatting**: tweak `format_worksheet()` in `switch_audit/formatters.py`.

---

## 🔒 Security considerations

- Prefer secure stores over plaintext. Credentials are never written to disk by
  the tool, never placed in the process environment, and never logged.
- SSH host keys are verified — see
  [SSH host key verification](#ssh-host-key-verification). Set
  `security.host_key_policy: strict` in hardened environments.
- Limit who can run the tool and who can read the generated Excel: the workbook
  contains serial numbers, MAC addresses and topology detail.
- When using a jump host, ensure strong authentication and proper network segmentation.
- The audit is strictly read-only: it issues `show` commands and never
  configures anything. `enable` is entered only when `USE_ENABLE` is set.

---

## 🧩 Compatibility

- Target devices: Cisco IOS/IOS‑XE access and distribution switches reachable via SSH.
- The tool relies on Netmiko; specify the right device type(s) inside `netmiko_utils.py`.
- TextFSM/NTC templates significantly improve interface parsing fidelity but are not strictly required.

---

## ✅ Examples

```bash
# Basic, with jump host
python -m switch_audit -d devices.txt -o audit.xlsx

# Direct (no bastion), 20 workers, stale disabled
python -m switch_audit --direct -w 20 --stale-days 0 -d devices.txt -o audit.xlsx

# Conservative concurrency, higher stale threshold, verbose
python -m switch_audit -w 4 --stale-days 90 --debug -d devices.txt -o siteA.xlsx
```

---

## 🧠 FAQs

**Q: Do I need NTC TextFSM templates?**  
*A:* They are recommended for better `show interfaces` parsing. Without them, the script still works and
uses its internal parser for `show interfaces status` and best‑effort logic elsewhere.

**Q: Where do credentials come from?**  
*A:* On Windows, from Credential Manager (default target `MyApp/ADM`). Otherwise, you are prompted interactively
or you can adapt `credentials.py` to your secret store.

**Q: How is `Mode` determined?**  
*A:* From `show interfaces status`: if VLAN column is `trunk`/`rspan` → `trunk`; if `routed` → `routed`; otherwise `access`.

**Q: How is a port considered *stale*?**  
*A:* Only for access ports and when `--stale-days > 0`. Connected ports are flagged stale only if `Last input ≥ N days`.
Disconnected ports require **both** no PoE draw and no LLDP/CDP neighbor to be flagged stale.

---

## 📝 License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE). Free to use, modify and redistribute. If you'd like it tailored, deployed or extended for your estate, see [Work With Me](https://www.nautomationprime.io/work-with-me/).

## 👤 Author

Christopher Davies  
**Email:** <enquiries@nautomationprime.io>
