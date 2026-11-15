# Access Switch Audit — Operations Runbook

Operational guide for running the Cisco access switch audit, interpreting its
output, and recovering when a run fails.

**Applies to:** `cisco-access-switch-audit` 0.9.0
**Audience:** network engineers running the audit interactively or on a schedule
**Read time:** ~15 minutes. Jump to [Troubleshooting](#troubleshooting) if a run has already failed.

---

## 1. What this tool does

Connects to a list of Cisco switches (directly or through an SSH bastion), collects
interface, PoE, neighbour, VLAN and hardware data, and writes an Excel workbook
with a `SUMMARY` sheet, an `INVENTORY` sheet, and one sheet per device.

It is **strictly read-only**. It issues `show` commands only and never changes
configuration. `enable` is entered only when `USE_ENABLE` is set.

**Run it when you need to:**

- Find reclaimable access ports before buying more switches
- Produce a port-utilisation figure for a site
- Inventory modules and optics with serial numbers (for RMAs or refresh planning)
- Spot access-layer config drift — missing PortFast, non-standard config register, port-security violations
- Check PoE headroom before deploying more phones, APs or cameras

**Typical runtime:** roughly 20–40 seconds per device, 10 devices in parallel by
default. A 200-switch estate takes about 10–15 minutes with all collectors enabled.

---

## 2. Prerequisites

| Requirement | Detail |
|---|---|
| Python | 3.10 or later. Windows users can skip this — `setup.bat` downloads a private 3.12 runtime. |
| Network path | TCP/22 to the bastion, or to every device when running `--direct`. |
| Credentials | A read-only account with privilege to run `show` commands on every device. |
| Disk | ~200 MB for dependencies, plus workbook and log space. |
| Excel | Any version that opens `.xlsx`. LibreOffice works; conditional formatting is preserved. |

A read-only TACACS/RADIUS account is sufficient and recommended. `enable` is only
needed if your `show` commands are restricted at privilege level 1.

---

## 3. First-time setup

### Windows

```bat
setup.bat
```

Downloads the Python 3.12.9 embeddable runtime into `python_runtime\`, installs
pip, then installs dependencies. Requires internet access once. `run.bat` calls
this automatically if `python_runtime\` is missing.

### Linux / WSL / macOS

```bash
./setup.sh
```

Creates `.venv/` and installs dependencies. Requires Python 3.10+ already installed.

### Developer install

```bash
pip install -e ".[dev]"
pytest -q
```

> **Reproducible installs.** Both installers prefer `requirements.lock.txt` when
> present — a cross-platform pinned lock. Delete it to float to the newest
> compatible releases. Regenerate with:
> `uv pip compile requirements.txt --universal --python-version 3.10 --output-file requirements.lock.txt`

### Verify the install

```bash
python -m switch_audit --version
python -m switch_audit --list-collectors
```

Both should print and exit 0 without touching the network.

---

## 4. Running an audit

### 4.1 Interactive (TUI) — recommended for ad-hoc runs

```bat
run.bat
```

```bash
./run.sh
```

Opens a full-screen form: device file, output name, stale threshold, worker count,
credentials, jump host. Live statistics and a log pane during the run, a summary
screen at the end. `Ctrl+Q` quits.

### 4.2 Command line

```bash
# Use the jump host from config.yaml
python -m switch_audit --devices devices.txt --output output/site-a.xlsx

# Direct connections, no bastion
python -m switch_audit --devices devices.txt --direct

# Override the bastion for one run
python -m switch_audit --devices devices.txt --jump-host bastion.example.com

# Higher concurrency, 60-day stale threshold
python -m switch_audit --devices devices.txt --workers 20 --stale-days 60

# Verbose, for diagnosing a problem run
python -m switch_audit --devices devices.txt --debug
```

### 4.3 Unattended / scheduled

Always pass `--non-interactive`. Without it, a missing credential makes the process
wait on a prompt that no one will ever answer.

```bash
SWITCH_USER=svc-audit SWITCH_PASS=... \
  python -m switch_audit --devices devices.txt \
    --output "output/audit-$(date +%F).xlsx" --non-interactive
```

**Windows Task Scheduler.** Store the credential once, then reference it:

```bat
cmdkey /generic:SwitchAudit/User /user:svc-audit /pass
```

Action: `C:\path\to\python_runtime\python.exe`
Arguments: `-m switch_audit --devices devices.txt --non-interactive`
Start in: `C:\path\to\Access_Switch_Audit`

The `Start in` directory matters — `config.yaml`, `devices.txt`, `logs\` and
`output\` are all resolved relative to the working directory.

**cron:**

```cron
0 2 * * 1 cd /opt/switch-audit && ./.venv/bin/python -m switch_audit \
  --devices devices.txt --output "output/audit-$(date +\%F).xlsx" \
  --non-interactive >> logs/cron.log 2>&1
```

### 4.4 The device list

One IP or hostname per line. Blank lines and `#` comments are ignored.

```text
# Site A — access layer
192.0.2.11
192.0.2.12
access-sw-03.example.com
```

---

## 5. Credentials

Resolved in this order; the first complete pair wins.

1. `SWITCH_USER` / `SWITCH_PASS`
2. `CREDENTIAL_USER` / `CREDENTIAL_PASS`
3. Windows Credential Manager, target from `credentials.target` in `config.yaml`
   (override with `CREDENTIAL_TARGET`)
4. Interactive prompt — **suppressed by `--non-interactive`**

A `.env` file in the working directory is loaded automatically at start-up.
Variables already set in your shell always win. Copy `.env.example` to `.env`.

```ini
SWITCH_USER=svc-audit
SWITCH_PASS=your_password
USE_ENABLE=false
# ENABLE_SECRET=...
```

`.env` is gitignored. Credentials are never written to disk by the tool, never
exported into the process environment, and never logged.

### Enable mode

Only used when `USE_ENABLE` is truthy (`1`, `true`, `yes`, `y`) **and**
`ENABLE_SECRET` is set. If `USE_ENABLE` is set but the secret is empty, the tool
logs a warning and continues at whatever privilege the login gives.

---

## 6. SSH host key verification

Set under `security:` in `config.yaml`. Applies to the bastion **and** every device.

| Policy | Behaviour | Use when |
|---|---|---|
| `strict` | Only hosts already in `known_hosts` are accepted. | Hardened environments with a managed `known_hosts`. |
| `auto-add` *(default)* | Trust on first use. Unknown hosts accepted and recorded; a **changed** key is refused. | Normal operation. |
| `insecure` | Accept any key, remember nothing. | Lab only. |

```yaml
security:
  host_key_policy: auto-add
  known_hosts: null        # null = ~/.ssh/known_hosts
  save_host_keys: true
```

Environment overrides: `SWITCH_AUDIT_HOST_KEY_POLICY`, `SWITCH_AUDIT_KNOWN_HOSTS`.

> Netmiko does not write host keys back, so under `auto-add` a **device** key is
> verified only once it is already recorded. The bastion key — shared by every
> connection in the run — is recorded on first use.

To pre-populate for `strict`:

```bash
ssh-keyscan -H bastion.example.com >> ~/.ssh/known_hosts
ssh-keyscan -H -f devices.txt >> ~/.ssh/known_hosts
```

---

## 7. Configuration reference

`config.yaml` in the working directory, or the path in `SWITCH_AUDIT_CONFIG`.
If no file is found the tool logs a warning and uses built-in defaults.

Every value is validated at startup. A wrong type, an out-of-range number or a
misspelt policy stops the run before anything is contacted, with exit `2` and a
message naming the key. Environment overrides are checked the same way. Unknown
keys are only warned about. To see the values the tool will actually use:

```bash
python -m switch_audit --show-config
```

`config.yaml` references `config.schema.json`, so VS Code (with the YAML
extension) completes keys and flags bad values while you edit.

### Connection

| Key | Default | Notes |
|---|---|---|
| `jump_host` | — | Bastion address. `null` for direct. Env: `JUMP_HOST` |
| `connection.timeout` | `25` | TCP/auth timeout, seconds. Env: `CONNECTION_TIMEOUT` |
| `connection.banner_timeout` | `30` | SSH banner negotiation |
| `connection.read_timeout` | `60` | Per-command read timeout. Raise for large chassis. |
| `connection.retries` | `3` | Attempts per device |
| `connection.retry_base_wait` | `1` | Backoff is `base × 2ⁿ`, capped at 30s → 1s, 2s, 4s |
| `concurrency.default_workers` | `10` | Parallel device sessions |

### Audit behaviour

| Key | Default | Notes |
|---|---|---|
| `audit.default_stale_days` | `30` | `0` disables stale flagging |
| `audit.stale.include_admin_down` | `false` | Count admin-down ports as stale |
| `audit.stale.use_mac_table` | `true` | Learned MACs prove a port is in use |
| `audit.enable_port_categorization` | `true` | Adds the `Port Category` column |
| `audit.mac_addresses_per_port` | `5` | MACs listed per port before truncating |
| `audit.oui_file` | `null` | CSV of `oui,vendor` extending the built-in table |

### Output

| Key | Default |
|---|---|
| `output.default_filename` | `output/audit.xlsx` |
| `output.min_column_width` | `8` |
| `output.max_column_width` | `60` |

### Other environment variables

| Variable | Purpose |
|---|---|
| `SWITCH_AUDIT_CONFIG` | Alternate `config.yaml` path |
| `LOG_DIR` | Log directory (default `logs`) |
| `LOGGING_CONFIG` | Path to a logging INI file, overriding the built-in setup |
| `NET_TEXTFSM` | TextFSM template directory, if not using the bundled `ntc-templates` |

---

## 8. Collectors

Each collector runs one extra `show` command and adds columns. Switch them under
`collectors:` in `config.yaml`. Disable what you don't need to shorten the run.

```bash
python -m switch_audit --list-collectors
```

### Per-port

| Collector | Command | Default | Adds |
|---|---|---|---|
| `switchport` | `show interfaces switchport` | on | Admin Mode, Access VLAN, **Voice VLAN**, Native VLAN, Trunk VLANs |
| `mac_table` | `show mac address-table` | on | MAC Count, MAC Addresses, MAC Vendors, MAC VLANs |
| `vlans` | `show vlan` | on | VLAN Name, VLANs Defined/Active |
| `spanning_tree` | `show spanning-tree` | on | STP Role, State, Type, **STP Edge** (PortFast) |
| `transceivers` | `show interfaces transceiver` | on | Optic Temp, Voltage, Tx/Rx dBm |
| `dot1x` | `show authentication sessions` | off | Auth Method, Domain, Status, MAC |
| `etherchannel` | `show etherchannel summary` | off | Port-Channel membership and state |
| `port_security` | `show port-security` | off | PortSec Max, Current, Violations, Action |

### Device level

| Collector | Command | Default | Adds |
|---|---|---|---|
| `version_detail` | `show version` | on | Uptime, Reload Reason, Config Register, ROMMON, Stack members |
| `inventory` | `show inventory` | on | The `INVENTORY` sheet — module and optic serials |
| `environment` | `show environment power all` / `temperature` | on | PSUs Present/OK, Inlet Temp, Temp Status |
| `poe_budget` | `show power inline` | on | PoE Available / Used / Remaining watts, Used % |
| `cpu_memory` | `show processes cpu` | off | CPU 5s / 1m / 5m % |

Every collector is best-effort. A platform that doesn't implement a command — a
non-PoE switch, a device with no optics — contributes nothing and never fails
the device.

---

## 9. Reading the output

### SUMMARY sheet (first)

One row per device including failures, plus a `TOTAL` row. Count columns are
summed; percentages are recomputed from those sums; temperature and CPU take the
peak, not a sum.

Key columns:

- `Audit Result` — `OK`, `ERROR`, or `NO DATA`
- `Error` — the failure reason, blank on success
- `Total Ports (phy)`, `Access / Trunk / Routed Ports`
- `Connected`, `Not Connected`, `Admin Down`, `Err-Disabled`
- `Stale Ports`, `Ports w/ Errors`, `PoE Active Ports`
- `Uptime`, `Config Register` (+ `Config Register OK`), `Reload Reason`
- `PoE Available / Used / Remaining (W)`, `PoE Used %`
- `PSUs Present / OK`, `Inlet Temp (C)`

### Per-device sheets

One row per interface, ~43 columns with all collectors on. Frozen header,
auto-filter on every column.

`Port Category` is the quickest triage column:

| Value | Meaning |
|---|---|
| `connected` | Up and in use |
| `powered-device` | Up and drawing PoE |
| `available` | Down, no evidence of use, below the stale threshold |
| `stale-unused` | Down and meets the stale criteria — a reclaim candidate |
| `disabled` | Administratively down |
| `error` | err-disabled |
| `trunk/routed` | Not an access port |

### INVENTORY sheet

Every module and pluggable optic across all devices, with PID, VID and serial —
what you need for an RMA.

### Conditional formatting

| Colour | Meaning |
|---|---|
| Green | `Status = connected`, PoE power > 0 |
| Red | `notconnect`, `err-disabled`, any error counter > 0, stale = TRUE, port-security violations > 0, PoE Used % > 85 |
| Amber | PoE Used % 70–85, `Config Register OK` = FALSE, `STP Edge` = FALSE |
| Grey | `administratively down`, MAC Count = 0 |

### How stale is decided

Only for **access** ports, and only when `--stale-days > 0`.

- Administratively down or err-disabled → **never stale** (reported under their own category), unless `audit.stale.include_admin_down` is true
- `connected` → stale if `Last input ≥ N days`
- Otherwise → stale only if **all** hold: no PoE draw, no LLDP/CDP neighbour, and no learned MACs (the MAC check requires the `mac_table` collector; disable with `audit.stale.use_mac_table: false`)

---

## 10. Exit codes

| Code | Meaning | Action |
|---|---|---|
| `0` | At least one device audited successfully. The workbook was written. | Check the SUMMARY for any `ERROR` rows. |
| `1` | Fatal error, or every device failed. | See Troubleshooting. |
| `2` | `config.yaml` or an environment override is invalid. Nothing ran. | Fix the key named in the message. |

A run where *some* devices fail still exits `0` and still writes the workbook —
partial success is success. Failed devices appear in `SUMMARY` with
`Audit Result = ERROR` and a reason in `Error`.

For monitoring, alert on exit `1` and on the `ERROR` row count exceeding your
tolerance.

---

## 11. Troubleshooting

Logs are in `logs/debug.log` (rotating, 5 MB × 5). Re-run with `--debug` for
verbose console output.

### `Configuration error: Invalid configuration in ...` — exit 2

A value in `config.yaml`, or an environment override, failed validation. Each
problem is listed with its key, for example
`connection.timeout (from CONNECTION_TIMEOUT): Input should be a valid integer`.
Fix the value (or unset the variable named in brackets) and confirm with
`python -m switch_audit --show-config`.

### `No devices were audited successfully` — exit 1

Every device failed. The lines immediately above list each device and its reason.

- **`Authentication failed`** — wrong credentials, or the account is locked. Verify by hand: `ssh svc-audit@<device>`. If you use a bastion, check whether repeated runs locked the account.
- **`TCP connection to device failed`** — no network path, or ACL. Check reachability from the host actually running the audit.
- Confirm which credential source was used — the log records `Using credentials from SWITCH_USER / SWITCH_PASS` or `Found stored credentials for ... (target: ...)`.

### `Cannot reach jump host X` — exit 1

The run stops before dialling any device. Test the bastion directly:

```bash
ssh -v svc-audit@bastion.example.com
```

If the bastion is fine by hand but not here, check `security.host_key_policy` and
whether the key changed.

### Host key mismatch

```
Host key mismatch for jump host X: the server presented a key that does not
match the one recorded in known_hosts.
```

The host was rebuilt, or the connection is being intercepted. **Verify the new
fingerprint out of band before clearing anything.** Then:

```bash
ssh-keygen -R bastion.example.com
```

### `host_key_policy is 'strict' but no known_hosts file was found`

Populate it with `ssh-keyscan` (see §6), point `security.known_hosts` at the right
file, or switch to `auto-add`.

### `No credentials available and prompting is disabled`

`--non-interactive` with nothing to use. Set `SWITCH_USER` / `SWITCH_PASS`, or
store the credential under the `credentials.target` name. Under Task Scheduler,
remember that Credential Manager entries are per-user — store them as the account
the task runs as.

### `Devices file not found` / `No devices found in ...`

Wrong path, or the file is all comments and blanks. Paths are relative to the
working directory, which for a scheduled task is the `Start in` field.

### `<path> is locked (is it open in Excel?) — writing to <path>_<timestamp>.xlsx`

Not an error; the run completed. The workbook was open in Excel, so the results
were written alongside it rather than lost. Close Excel before the next run.

### `Audit Result = NO DATA`

The device was reached but no interface records could be parsed. Usually means
`show interfaces` timed out or returned unparsed output.

- Raise `connection.read_timeout` — large stacks can take well over 60s
- Check for `'show interfaces' returned unparsed output; is the TextFSM template available?` in the log, which points at a TextFSM problem rather than the device

### Missing TextFSM templates

Templates ship with Netmiko via `ntc-templates`. If they're missing, reinstall
dependencies, or set `NET_TEXTFSM` to a template directory.

### Expected columns are missing

The collector is off, or the platform doesn't support the command. Check
`--list-collectors`, then search the log for the command name — a rejected
command is recorded at DEBUG level.

### The run is too slow

1. Raise `--workers` (20–30 is reasonable; the bastion is usually the limit, not the host)
2. Turn off collectors you don't need — each is one extra command per device
3. Lower `connection.read_timeout` if failures are dominated by unreachable devices
4. Lower `connection.retries` for a fast inventory sweep

### The stale count looks wrong

Check the rules in §9. The usual causes: `mac_table` is disabled so occupied ports
look idle, or `audit.stale.include_admin_down` doesn't match your policy. `Stale
Ports` in SUMMARY always equals the number of ports categorised `stale-unused`.

### Account lockouts

The audit opens **one** bastion session per run and multiplexes device channels
through it, so a run costs one bastion login. If you're still seeing lockouts,
check for overlapping scheduled runs.

---

## 12. Health checks

Run after any change to the tool, Python, or the environment:

```bash
python -m switch_audit --version          # metadata loads
python -m switch_audit --show-config      # config validates
python -m switch_audit --devices one-device.txt --output /tmp/smoke.xlsx --debug
pytest -q                                 # 165 tests, if installed with [dev]
```

A single-device run against a known-good switch is the fastest end-to-end check.

---

## 13. Escalation

Collect before escalating:

- `logs/debug.log` (the failing run's section)
- The exact command line, and whether `--direct` was used
- `config.yaml` with `jump_host` and `credentials.target` **redacted**
- The generated workbook, if one was produced
- `python -m switch_audit --version`

> **Handle the workbook as sensitive.** It contains serial numbers, MAC
> addresses, VLAN names and topology detail. Treat it at the same level as a
> configuration backup.

Repository: <https://github.com/Nautomation-Prime/Access_Switch_Audit>
Issues: <https://github.com/Nautomation-Prime/Access_Switch_Audit/issues>
