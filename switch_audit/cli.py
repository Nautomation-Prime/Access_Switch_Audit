"""Command-line interface and main orchestration logic."""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import logging
import sys
import time
from queue import Empty, Queue
from typing import Any

from .app_config import config
from .credentials import get_enable_secret, get_secret_with_fallback
from .device_auditor import audit_device, error_summary
from .excel_reporter import ExcelReporter
from .jump_manager import JumpManager
from .logging_setup import configure_logging
from .options import AuditOptionsError, build_options
from .validators import validate_devices_file
from .version import get_version_info

log = logging.getLogger(__name__)

AuditResult = tuple[str, "str | None", list[dict[str, Any]], dict[str, Any]]

# How each AuditOptions field is named on the command line, for error messages.
_OPTION_FLAGS = {
    "devices_file": "--devices",
    "output": "--output",
    "stale_days": "--stale-days",
    "workers": "--workers",
    "jump_host": "--jump-host",
}


def _prepare_console() -> None:
    """
    Make stdout/stderr tolerate the glyphs this tool prints.

    On Windows a redirected stream defaults to the ANSI code page (cp1252),
    where a single '≥' in the --help text raises UnicodeEncodeError and kills
    the process before any work starts. Re-encoding as UTF-8 with replacement
    keeps redirected and scheduled runs alive.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover - stream already closed/detached
            pass


def _bar_glyphs() -> tuple[str, str]:
    """Return (filled, empty) bar characters the current stdout can render."""
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "█░".encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return ("#", "-")
    return ("█", "░")


# ---------------- Progress bar (event-driven with final drain) ----------------
def _print_progress_extended(started: int, done: int, total: int, current_ip: str = "", width: int = 30) -> None:
    """
    Render a simple, event-driven progress bar (non-thread-safe write).

    Skipped when stdout is not a terminal: a carriage-return progress bar
    redirected into a log file produces one enormous unreadable line.

    Args:
        started: Number of jobs that have started
        done: Number of jobs completed
        total: Total jobs
        current_ip: Currently processing device (optional)
        width: Bar width in characters
    """
    if not sys.stdout.isatty():
        return
    ratio = 0 if total == 0 else done / total
    filled = int(ratio * width)
    fill_char, empty_char = _bar_glyphs()
    bar = fill_char * filled + empty_char * (width - filled)
    current_str = f" | Current: {current_ip[:30]}" if current_ip else ""
    sys.stdout.write(f"\rProgress: [{bar}] {done}/{total}  |  started: {started}/{total}{current_str}")
    sys.stdout.flush()
    if done == total:
        sys.stdout.write("\n")


def _worker_wrapper(func, ip: str, event_q: Queue, **kwargs):
    """
    Wrap a worker so start/done events always reach the progress queue.

    The 'done' event is emitted from a finally block: an earlier version only
    emitted it for a hand-picked list of exception types, so any other failure
    left the progress counter permanently short of the total.
    """
    event_q.put(("start", ip, time.time()))
    try:
        return func(ip, **kwargs)
    finally:
        event_q.put(("done", ip, time.time()))


def _drain_events(event_q: Queue, started: int, done: int) -> tuple[int, int]:
    """Consume all pending progress events, returning updated counters."""
    while True:
        try:
            ev, _ev_ip, _ev_ts = event_q.get_nowait()
        except Empty:
            return started, done
        if ev == "start":
            started += 1
        elif ev == "done":
            done += 1


def _print_collectors() -> None:
    """Print the collector registry and which entries config.yaml switches on."""
    from .collectors import REGISTRY, enabled_collectors

    enabled = {c.name for c in enabled_collectors()}
    width = max(len(c.name) for c in REGISTRY)

    print("Optional collectors (configure under the collectors: key in config.yaml)")
    print()
    for collector in REGISTRY:
        mark = "on " if collector.name in enabled else "off"
        print(f"  [{mark}] {collector.name.ljust(width)}  {collector.description}")
        columns = collector.columns + collector.device_columns
        if columns:
            pad = " " * width
            print(f"        {pad}  columns: {', '.join(columns)}")
    print()
    print("Disable a collector to shorten the audit; a command the platform")
    print("does not support is skipped automatically.")


def _print_config() -> None:
    """Print the effective configuration: config.yaml, defaults and env overrides."""
    import yaml

    source = config.path if config.path.exists() else f"{config.path} (not found - built-in defaults)"
    print(f"# Effective configuration from {source}")
    for key, var in sorted(config.env_overrides.items()):
        print(f"#   {key} overridden by ${var}")
    print(yaml.safe_dump(config.settings.model_dump(mode="json"), sort_keys=False), end="")


def _print_config_schema() -> None:
    """Print the JSON Schema for config.yaml."""
    import json

    from assets.config_schema import json_schema

    print(json.dumps(json_schema(), indent=2))


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser (separated out so tests can exercise it)."""
    parser = argparse.ArgumentParser(
        prog="switch-audit",
        description="Cisco Access Switch Audit -> Excel (TextFSM 'show interfaces')",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Launch the full-screen TUI (recommended)
  python -m switch_audit --tui

  # Audit devices with the configured jump host
  python -m switch_audit --devices devices.txt --output report.xlsx

  # Direct connections without a jump host
  python -m switch_audit --devices devices.txt --direct

  # Override the jump host for a single run
  python -m switch_audit --devices devices.txt --jump-host bastion.example.com

  # Custom stale threshold and worker count
  python -m switch_audit --devices devices.txt --stale-days 60 --workers 20

  # Non-interactive run (credentials from the environment)
  SWITCH_USER=admin SWITCH_PASS=... python -m switch_audit --non-interactive

  # Enable debug logging
  python -m switch_audit --devices devices.txt --debug
        """,
    )

    parser.add_argument("--version", action="version", version=get_version_info())
    parser.add_argument("--tui", action="store_true", help="Launch the full-screen Textual terminal UI")
    parser.add_argument(
        "--list-collectors",
        action="store_true",
        help="List the optional data collectors, whether each is enabled, and exit",
    )
    parser.add_argument(
        "--show-config",
        action="store_true",
        help="Print the effective configuration (config.yaml, defaults and env overrides) and exit",
    )
    parser.add_argument(
        "--config-schema",
        action="store_true",
        help="Print the JSON Schema for config.yaml and exit",
    )
    parser.add_argument("--direct", action="store_true", help="Connect directly (no jump host)")
    parser.add_argument(
        "--jump-host",
        type=str,
        default=None,
        help="Jump/bastion host to use for this run (overrides config.yaml). Ignored with --direct.",
    )
    parser.add_argument(
        "--stale-days",
        type=int,
        default=config.default_stale_days,
        help=f"Days to consider 'stale' for access ports (0 to disable). Default: {config.default_stale_days}",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug output")
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Never prompt for input. Credentials must come from the environment or a credential store.",
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default=config.default_output_format,
        help=f"Output workbook filename (default: {config.default_output_format})",
    )
    parser.add_argument(
        "--devices", "-d",
        type=str,
        default="devices.txt",
        help="Path to devices file (one IP/hostname per line, # comments allowed)",
    )
    parser.add_argument(
        "--workers", "-w",
        type=int,
        default=config.default_workers,
        help=f"Number of worker threads (default: {config.default_workers})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """
    CLI entrypoint:
    - Parses args
    - Loads device list
    - Runs audit concurrently with progress
    - Writes SUMMARY + per-device sheets to Excel

    Returns:
        Process exit code. 0 when at least one device was audited successfully,
        1 on a fatal error or when every device failed.
    """
    _prepare_console()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_collectors:
        _print_collectors()
        return 0
    if args.show_config:
        _print_config()
        return 0
    if args.config_schema:
        _print_config_schema()
        return 0

    # Launch TUI if requested
    if args.tui:
        from .tui import SwitchAuditApp

        SwitchAuditApp().run()
        return 0

    # Configure logging
    configure_logging(debug=args.debug)

    # Determine jump host: --direct wins, then --jump-host, then config.
    if args.direct and args.jump_host:
        log.warning("--jump-host ignored because --direct was specified")

    try:
        options = build_options(
            labels=_OPTION_FLAGS,
            devices_file=args.devices,
            output=args.output,
            stale_days=args.stale_days,
            workers=args.workers,
            jump_host=None if args.direct else (args.jump_host or config.jump_host),
        )
    except AuditOptionsError as e:
        for problem in e.problems:
            log.error("Invalid argument %s", problem)
        return 1

    output_filename = options.output
    jump_host = options.jump_host

    if jump_host:
        log.info("Using jump host %s to reach devices", jump_host)
    else:
        log.info("Connecting directly to devices (no jump host)")

    # Acquire credentials
    try:
        username, password = get_secret_with_fallback(interactive=not args.non_interactive)
        enable_secret = get_enable_secret()
    except RuntimeError as e:
        log.error("Failed to acquire credentials: %s", e)
        return 1

    # Load and validate devices file
    try:
        device_list = validate_devices_file(options.devices_file)
    except (FileNotFoundError, ValueError) as e:
        log.error("%s", e)
        return 1

    log.info(
        "Auditing %d device(s) with %d worker(s); stale threshold %s; output %s",
        len(device_list),
        options.workers,
        f">={options.stale_days} days" if options.stale_days else "disabled",
        output_filename,
    )

    reporter = ExcelReporter()
    results: list[AuditResult] = []
    total = len(device_list)

    # One shared bastion session for the whole run rather than one per device.
    jump: JumpManager | None = None
    if jump_host:
        jump = JumpManager(jump_host, username, password)
        try:
            jump.connect()
        except Exception as e:  # noqa: BLE001 - report and stop, nothing can run
            log.error("Cannot reach jump host %s: %s", jump_host, e)
            return 1

    event_q: Queue = Queue()
    started = 0
    done = 0
    _print_progress_extended(started, done, total)

    try:
        with cf.ThreadPoolExecutor(max_workers=options.workers, thread_name_prefix="audit") as ex:
            future_to_ip = {
                ex.submit(
                    _worker_wrapper,
                    audit_device,
                    ip,
                    event_q,
                    username=username,
                    password=password,
                    enable_secret=enable_secret,
                    jump_host=jump_host,
                    stale_days=options.stale_days,
                    debug=args.debug,
                    jump=jump,
                ): ip
                for ip in device_list
            }

            pending = set(future_to_ip)
            while pending:
                started, done = _drain_events(event_q, started, done)
                _print_progress_extended(started, done, total)

                done_set, pending = cf.wait(pending, timeout=0.2, return_when=cf.FIRST_COMPLETED)
                for fut in done_set:
                    ip = future_to_ip[fut]
                    try:
                        results.append(fut.result())
                    except Exception as e:  # noqa: BLE001 - one device must not sink the run
                        # Netmiko/paramiko failures derive from SSHException, not
                        # OSError, so a narrow except here used to escape the pool
                        # and discard every already-completed device.
                        log.error("Audit of %s failed: %s", ip, e)
                        log.debug("Traceback for %s", ip, exc_info=True)
                        results.append((ip, None, [], error_summary(ip, e)))
                _print_progress_extended(started, done, total)
    except KeyboardInterrupt:
        log.warning("Interrupted by user. Partial results will be saved.")
    finally:
        started, done = _drain_events(event_q, started, done)
        done = max(done, len(results))
        started = max(started, total)
        _print_progress_extended(started, done, total)
        if jump is not None:
            jump.close()

    succeeded = [r for r in results if r[3].get("Audit Result") == "OK"]
    failed = [r for r in results if r[3].get("Audit Result") != "OK"]

    # Write Excel
    try:
        written_path = reporter.save_to_excel(results, output_filename)
        log.info("Audit complete. Results saved to: %s", written_path)
    except Exception as e:  # noqa: BLE001 - surface any writer failure clearly
        log.error("Failed to save Excel workbook: %s", e)
        log.debug("Full traceback:", exc_info=True)
        return 1

    log.info(
        "Devices audited: %d/%d succeeded, %d failed",
        len(succeeded), total, len(failed),
    )
    if failed:
        for ip, _hostname, _detail, summary in failed:
            log.warning("  %s: %s", ip, summary.get("Error", "unknown error"))

    if total and not succeeded:
        log.error("No devices were audited successfully.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
