"""Full-screen Textual TUI for Access Switch Audit."""

from __future__ import annotations

import logging
import os

from rich.markup import escape as markup_escape
from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, ScrollableContainer, Vertical
from textual.message import Message
from textual.screen import Screen
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    Label,
    RichLog,
    Static,
)

from .app_config import config
from .logging_setup import configure_logging
from .version import get_version

log = logging.getLogger(__name__)

# Loggers that produce very high-volume DEBUG output — suppress to WARNING in the TUI
_NOISY_LOGGERS = (
    "paramiko",
    "paramiko.transport",
    "netmiko.base_connection",
    "netmiko.utilities",
)


def _get_stored_credentials() -> tuple[str, str]:
    """
    Try to pre-fill credentials from env vars then Windows Credential Manager.

    Returns a (username, password) tuple — both may be empty strings.
    """
    for user_env, pass_env in (
        ("SWITCH_USER", "SWITCH_PASS"),
        ("CREDENTIAL_USER", "CREDENTIAL_PASS"),
    ):
        user = os.environ.get(user_env, "")
        pwd = os.environ.get(pass_env, "")
        if user and pwd:
            return user.strip(), pwd

    cred_target = os.environ.get("CREDENTIAL_TARGET", config.cred_target)
    try:
        import win32cred  # type: ignore

        cred = win32cred.CredRead(cred_target, win32cred.CRED_TYPE_GENERIC)
        user = cred.get("UserName") or ""
        blob = cred.get("CredentialBlob")
        pwd = ""
        if blob:
            try:
                pwd = blob.decode("utf-16le")
            except Exception:
                try:
                    pwd = blob.decode("utf-8", errors="ignore")
                except Exception:
                    pwd = ""
        return user.strip(), pwd
    except Exception:
        return "", ""


# ---------------------------------------------------------------------------
# Log message (routes log records through Textual's thread-safe message bus)
# ---------------------------------------------------------------------------


def _tally(results) -> tuple[int, int]:
    """
    Return (total ports, stale ports) across the device results collected so far.

    Counts the per-interface stale flag directly instead of the optional
    "Port Category" label, which is absent when port categorisation is disabled
    in config.yaml and silently reported every run as zero stale ports.
    """
    total_ports = 0
    stale_ports = 0
    for _ip, _hostname, detailed, _summary in results:
        total_ports += len(detailed)
        for iface in detailed:
            for key, value in iface.items():
                if key.startswith("Stale (") and value is True:
                    stale_ports += 1
                    break
    return total_ports, stale_ports


class AppendLog(Message):
    """Internal message that carries a single formatted log line."""

    def __init__(self, markup: str) -> None:
        super().__init__()
        self.markup = markup


# ---------------------------------------------------------------------------
# Log handler
# ---------------------------------------------------------------------------


class TUILogHandler(logging.Handler):
    """Pipes log records into the RichLog widget on the audit screen.

    Uses Textual's thread-safe ``post_message`` so that log records emitted
    from background worker threads are delivered to the UI without blocking
    the caller and without going through ``App.call_from_thread``.
    """

    _LEVEL_MARKUP = {
        logging.DEBUG: "dim",
        logging.WARNING: "bold yellow",
        logging.ERROR: "bold red",
        logging.CRITICAL: "bold white on red",
    }

    def __init__(self, screen: AuditScreen) -> None:
        super().__init__()
        self._screen = screen
        self.setFormatter(
            logging.Formatter(
                "%(asctime)s  %(levelname)-8s  %(message)s",
                datefmt="%H:%M:%S",
            )
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
            style = self._LEVEL_MARKUP.get(record.levelno, "")
            # Escape any Rich markup characters so patterns like "[hostname]"
            # are rendered literally rather than as markup tags.
            escaped = markup_escape(text)
            markup = f"[{style}]{escaped}[/]" if style else escaped
            self._screen.post_message(AppendLog(markup))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Setup screen
# ---------------------------------------------------------------------------


# How each AuditOptions field is labelled on the setup form, for error messages.
_OPTION_LABELS = {
    "devices_file": "Devices File",
    "output": "Output Workbook",
    "stale_days": "Stale Port Threshold",
    "workers": "Worker Threads",
    "jump_host": "Jump Host",
}


class SetupScreen(Screen):
    """Form for entering audit configuration and credentials."""

    BINDINGS = [Binding("ctrl+q", "app.quit", "Quit")]

    def compose(self) -> ComposeResult:
        stored_user, stored_pass = _get_stored_credentials()
        cred_target = os.environ.get("CREDENTIAL_TARGET", config.cred_target)
        creds_ok = bool(stored_user and stored_pass)
        jump_default = str(config.jump_host or "").strip()

        yield Header(show_clock=True)
        with ScrollableContainer(id="setup-outer"):
            with Container(id="setup-inner"):
                yield Static("Access Switch Audit", id="app-title")

                # ── Audit configuration ───────────────────────────────────
                yield Label("─── Audit Configuration", classes="section-label")

                yield Label("Devices File", classes="field-label")
                yield Input(
                    value="devices.txt",
                    placeholder="Path to device list (one IP per line)",
                    id="devices-file",
                )

                yield Label("Output Workbook  (optional — leave blank for auto-name)", classes="field-label")
                yield Input(
                    placeholder="e.g. audit_report.xlsx",
                    id="output-file",
                )

                yield Label(
                    "Stale Port Threshold (days)  — 0 to disable",
                    classes="field-label",
                )
                yield Input(
                    value=str(config.default_stale_days),
                    placeholder="e.g. 30",
                    id="stale-days",
                )

                yield Label("Worker Threads", classes="field-label")
                yield Input(
                    value=str(config.default_workers),
                    placeholder="e.g. 10",
                    id="workers",
                )

                yield Checkbox("Skip jump / bastion host", id="skip-jump", value=False)

                # ── Credentials ──────────────────────────────────────────
                yield Label("─── Credentials", classes="section-label")
                yield Label("Username", classes="field-label")
                yield Input(
                    value=stored_user,
                    placeholder="SSH username",
                    id="username",
                )
                yield Label("Password", classes="field-label")
                yield Input(
                    value=stored_pass,
                    placeholder="SSH password",
                    password=True,
                    id="password",
                )

                if creds_ok:
                    yield Label(
                        f"✓  Loaded from Windows Credential Manager ({cred_target})",
                        classes="cred-ok",
                    )
                else:
                    yield Label(
                        "⚠  Not found in Credential Manager — enter manually",
                        classes="cred-missing",
                    )

                yield Checkbox(
                    "Save credentials to Windows Credential Manager after starting",
                    id="save-creds",
                    value=False,
                )

                # ── Jump / Bastion ───────────────────────────────────────
                yield Label("─── Jump / Bastion Host", classes="section-label")
                yield Label(
                    "Leave blank to connect directly to devices",
                    classes="field-label",
                )
                yield Input(
                    value=jump_default,
                    placeholder="e.g. 10.0.0.254  or  bastion.company.com",
                    id="jump-host",
                )

                # ── Buttons ──────────────────────────────────────────────
                with Horizontal(id="btn-row"):
                    yield Button("Start Audit", variant="primary", id="btn-start")
                    yield Button("Quit", variant="error", id="btn-quit")

        yield Footer()

    @on(Button.Pressed, "#btn-start")
    def _on_start(self) -> None:
        """Validate form inputs and transition to the audit screen."""
        devices_file = self.query_one("#devices-file", Input).value.strip() or "devices.txt"
        output_file = self.query_one("#output-file", Input).value.strip() or None
        stale_raw = self.query_one("#stale-days", Input).value.strip()
        workers_raw = self.query_one("#workers", Input).value.strip()
        skip_jump = self.query_one("#skip-jump", Checkbox).value
        username = self.query_one("#username", Input).value.strip()
        password = self.query_one("#password", Input).value
        save_creds = self.query_one("#save-creds", Checkbox).value
        jump_host_field = self.query_one("#jump-host", Input).value.strip()

        if not username:
            self.notify("Username is required.", severity="error")
            return
        if not password:
            self.notify("Password is required.", severity="error")
            return

        from .options import AuditOptionsError, build_options

        try:
            options = build_options(
                labels=_OPTION_LABELS,
                devices_file=devices_file,
                output=output_file or config.default_output_format,
                stale_days=stale_raw,
                workers=workers_raw,
                jump_host=None if skip_jump else (jump_host_field or config.jump_host),
            )
        except AuditOptionsError as exc:
            for problem in exc.problems:
                self.notify(problem, severity="error")
            return

        if save_creds:
            from .credentials import _write_win_cred  # type: ignore
            cred_target = os.environ.get("CREDENTIAL_TARGET", config.cred_target)
            try:
                _write_win_cred(cred_target, username, password)
                self.notify("Credentials saved to Windows Credential Manager.", severity="information")
            except Exception as exc:
                self.notify(f"Credential save failed: {exc}", severity="warning")

        self.app.push_screen(
            AuditScreen(
                devices_file=options.devices_file,
                output_file=options.output,
                stale_days=options.stale_days,
                workers=options.workers,
                jump_host=options.jump_host,
                username=username,
                password=password,
            )
        )

    @on(Button.Pressed, "#btn-quit")
    def _on_quit(self) -> None:
        self.app.exit()


# ---------------------------------------------------------------------------
# Audit screen
# ---------------------------------------------------------------------------


class AuditScreen(Screen):
    """Live audit dashboard with statistics panel and scrollable log."""

    BINDINGS = [Binding("ctrl+q", "app.quit", "Quit")]

    def __init__(
        self,
        *,
        devices_file: str,
        output_file: str | None,
        stale_days: int,
        workers: int,
        jump_host: str | None,
        username: str,
        password: str,
    ) -> None:
        super().__init__()
        self._devices_file = devices_file
        self._output_file = output_file
        self._stale_days = stale_days
        self._workers = workers
        self._jump_host = jump_host
        self._username = username
        self._password = password

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="audit-body"):
            with Vertical(id="stats-pane"):
                yield Static("STATISTICS", id="stats-heading")
                yield Static("", classes="stat-item")
                yield Static("Devices:    –", id="stat-devices", classes="stat-item")
                yield Static("", classes="stat-item")
                yield Static("Completed:  0", id="stat-complete", classes="stat-item")
                yield Static("Succeeded:  0", id="stat-ok",       classes="stat-item")
                yield Static("Errors:     0", id="stat-err",      classes="stat-item")
                yield Static("", classes="stat-item")
                yield Static("Ports:      –", id="stat-ports",    classes="stat-item")
                yield Static("Stale:      –", id="stat-stale",    classes="stat-item")
            with Vertical(id="log-pane"):
                yield Static("LIVE LOG", id="log-heading")
                yield RichLog(id="audit-log", highlight=True, markup=True, wrap=True)
        with Horizontal(id="status-bar"):
            yield Static("●  Initialising…", id="status-text")
        yield Footer()

    def on_mount(self) -> None:
        # Establish the file handler first so a TUI run leaves the same
        # logs/debug.log trail a CLI run does, then layer the widget handler on.
        configure_logging()
        self._attach_tui_handler()
        self._run_audit()

    # ── Internals ─────────────────────────────────────────────────────────────

    def _attach_tui_handler(self) -> None:
        """Replace console (stream) handlers with the TUI log handler."""
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.handlers = [
            h for h in root.handlers
            if isinstance(h, logging.FileHandler)
            or not isinstance(h, logging.StreamHandler)
        ]
        tui_handler = TUILogHandler(self)
        tui_handler.setLevel(logging.INFO)
        root.addHandler(tui_handler)
        for name in _NOISY_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)

    def on_append_log(self, event: AppendLog) -> None:
        """Receive a log line from TUILogHandler and write it to the RichLog widget."""
        try:
            self.query_one("#audit-log", RichLog).write(event.markup)
        except Exception:
            pass

    def _set_status(self, msg: str) -> None:
        """Thread-safe status bar update."""
        def _update() -> None:
            try:
                self.query_one("#status-text", Static).update(msg)
            except Exception:
                pass
        self.app.call_from_thread(_update)

    def _update_stats(
        self,
        total: int,
        completed: int,
        succeeded: int,
        errors: int,
        total_ports: int,
        stale_ports: int,
    ) -> None:
        """Refresh the statistics panel."""
        def _update() -> None:
            try:
                self.query_one("#stat-devices",  Static).update(f"Devices:    {total}")
                self.query_one("#stat-complete", Static).update(f"Completed:  {completed}")
                self.query_one("#stat-ok",       Static).update(f"Succeeded:  {succeeded}")
                self.query_one("#stat-err",      Static).update(f"Errors:     {errors}")
                if total_ports >= 0:
                    self.query_one("#stat-ports", Static).update(f"Ports:      {total_ports}")
                if stale_ports >= 0:
                    self.query_one("#stat-stale", Static).update(f"Stale:      {stale_ports}")
            except Exception:
                pass
        self.app.call_from_thread(_update)

    # ── Audit worker ──────────────────────────────────────────────────────────

    @work(thread=True)
    def _run_audit(self) -> None:
        """Full audit pipeline — runs in a background thread."""
        import concurrent.futures as cf
        from queue import Empty, Queue

        from .cli import _worker_wrapper
        from .credentials import get_enable_secret
        from .device_auditor import audit_device, error_summary
        from .excel_reporter import ExcelReporter
        from .jump_manager import JumpManager
        from .validators import validate_devices_file, validate_output_filename

        try:
            # Credentials are passed to the workers explicitly; they are
            # deliberately not exported to the process environment, where any
            # child process could read the password.
            self._set_status("●  Loading device list…")
            log.info("TUI audit starting — devices: %s", self._devices_file)

            try:
                device_list = validate_devices_file(self._devices_file)
            except (FileNotFoundError, ValueError) as exc:
                log.error("Device file error: %s", exc)
                self._set_status(f"✗  {exc}")
                return

            total = len(device_list)
            self._update_stats(total, 0, 0, 0, -1, -1)

            try:
                output_filename = validate_output_filename(self._output_file or config.default_output_format)
            except ValueError as exc:
                log.error("Invalid output filename: %s", exc)
                self._set_status(f"✗  {exc}")
                return

            reporter = ExcelReporter()
            results = []
            completed = 0
            succeeded = 0
            errors = 0

            event_q: Queue = Queue()

            # A single bastion session shared by every worker: one login for the
            # whole run rather than one per device.
            jump = None
            if self._jump_host:
                self._set_status(f"●  Connecting to jump host {self._jump_host}…")
                jump = JumpManager(self._jump_host, self._username, self._password)
                try:
                    jump.connect()
                except Exception as exc:
                    log.error("Cannot reach jump host %s: %s", self._jump_host, exc)
                    self._set_status(f"✗  Jump host unreachable: {exc}")
                    return

            self._set_status(f"●  Auditing {total} device(s)…")
            enable_secret = get_enable_secret()

            try:
                with cf.ThreadPoolExecutor(
                    max_workers=self._workers, thread_name_prefix="audit"
                ) as ex:
                    future_to_ip = {
                        ex.submit(
                            _worker_wrapper,
                            audit_device,
                            ip,
                            event_q,
                            username=self._username,
                            password=self._password,
                            enable_secret=enable_secret,
                            jump_host=self._jump_host,
                            stale_days=self._stale_days,
                            debug=False,
                            jump=jump,
                        ): ip
                        for ip in device_list
                    }

                    pending = set(future_to_ip)
                    while pending:
                        while True:
                            try:
                                event_q.get_nowait()
                            except Empty:
                                break

                        done_set, pending = cf.wait(
                            pending, timeout=0.5, return_when=cf.FIRST_COMPLETED
                        )
                        for fut in done_set:
                            completed += 1
                            ip = future_to_ip[fut]
                            try:
                                result = fut.result()
                            except Exception as exc:
                                log.error("Audit of %s failed: %s", ip, exc)
                                result = (ip, None, [], error_summary(ip, exc))

                            # Every device reaches the workbook, failures included,
                            # and success is judged on the audit result rather than
                            # on whether the worker happened to raise. Devices that
                            # exhausted their retries return normally, so the old
                            # check counted them as successes and the report showed
                            # "Succeeded: all" for a run where nothing connected.
                            results.append(result)
                            if result[3].get("Audit Result") == "OK":
                                succeeded += 1
                            else:
                                errors += 1
                                log.warning(
                                    "%s: %s", ip, result[3].get("Error", "audit failed")
                                )

                            total_ports, stale_ports = _tally(results)
                            self._update_stats(
                                total, completed, succeeded, errors, total_ports, stale_ports
                            )
            finally:
                if jump is not None:
                    jump.close()

            self._set_status("●  Writing Excel report…")
            log.info("Writing Excel report to %s", output_filename)
            written_path = reporter.save_to_excel(results, output_filename)
            output_filename = str(written_path)

            total_ports, stale_ports = _tally(results)
            self._update_stats(total, completed, succeeded, errors, total_ports, stale_ports)
            self._set_status(f"✓  Complete — {output_filename}")
            log.info("Audit complete — %d device(s), %d succeeded, %d errors", total, succeeded, errors)

            def _show_complete() -> None:
                self.app.push_screen(
                    CompleteScreen(
                        total=total,
                        succeeded=succeeded,
                        errors=errors,
                        total_ports=total_ports,
                        stale_ports=stale_ports,
                        output_file=output_filename,
                    )
                )

            self.app.call_from_thread(_show_complete)

        except Exception as exc:
            log.exception("Audit pipeline failed unexpectedly")
            self._set_status(f"✗  Error: {exc}")


# ---------------------------------------------------------------------------
# Complete screen
# ---------------------------------------------------------------------------


class CompleteScreen(Screen):
    """Summary screen shown after a successful audit run."""

    BINDINGS = [
        Binding("escape", "back_to_log", "Back to Log"),
        Binding("ctrl+q", "app.quit", "Quit"),
    ]

    def __init__(
        self,
        *,
        total: int,
        succeeded: int,
        errors: int,
        total_ports: int,
        stale_ports: int,
        output_file: str,
    ) -> None:
        super().__init__()
        self._total = total
        self._succeeded = succeeded
        self._errors = errors
        self._total_ports = total_ports
        self._stale_ports = stale_ports
        self._output_file = output_file

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="complete-container"):
            yield Static("AUDIT COMPLETE", id="complete-title")
            yield Static(self._make_table(), id="complete-table")
            yield Static(f"Output → {self._output_file}", id="output-path")
            with Horizontal(id="complete-btn-row"):
                yield Button("Back to Log", variant="default", id="btn-back")
                yield Button("Quit", variant="primary", id="btn-quit-final")
        yield Footer()

    def _make_table(self) -> Table:
        table = Table(box=None, padding=(0, 1), show_header=False, expand=True)
        table.add_column("label", style="dim", no_wrap=True)
        table.add_column("value", justify="right", no_wrap=True)
        table.add_row("Devices audited", str(self._total))
        table.add_row(
            "Succeeded",
            Text(str(self._succeeded), style="green" if self._succeeded == self._total else "yellow"),
        )
        table.add_row(
            "Errors",
            Text(str(self._errors), style="bold yellow" if self._errors else "green"),
        )
        table.add_row("", "")
        table.add_row("Total ports", str(self._total_ports))
        table.add_row(
            "Stale ports",
            Text(str(self._stale_ports), style="bold yellow" if self._stale_ports else "green"),
        )
        return table

    @on(Button.Pressed, "#btn-back")
    def _back(self) -> None:
        self.app.pop_screen()

    @on(Button.Pressed, "#btn-quit-final")
    def _quit(self) -> None:
        self.app.exit()

    def action_back_to_log(self) -> None:
        self.app.pop_screen()


# ---------------------------------------------------------------------------
# Application shell
# ---------------------------------------------------------------------------


class SwitchAuditApp(App):
    """Textual application shell for Access Switch Audit."""

    TITLE = "Access Switch Audit"
    BINDINGS = [Binding("ctrl+q", "quit", "Quit")]

    CSS = """
    /* ─── Global ─────────────────────────────────────────────────────────── */

    Screen {
        background: $background;
    }

    /* ─── Setup screen ──────────────────────────────────────────────────── */

    #setup-outer {
        width: 100%;
        height: 1fr;
        align: center top;
    }

    #setup-inner {
        width: 88;
        height: auto;
        border: round $primary;
        padding: 1 3;
        background: $surface;
    }

    #app-title {
        text-align: center;
        text-style: bold;
        color: $primary;
        margin-bottom: 1;
        padding-bottom: 1;
        border-bottom: solid $primary;
    }

    .section-label {
        text-style: bold;
        color: $accent;
        margin-top: 1;
        margin-bottom: 0;
    }

    .field-label {
        color: $text-muted;
        margin-bottom: 0;
    }

    .cred-ok {
        color: $success;
    }

    .cred-missing {
        color: $warning;
    }

    #btn-row {
        align: center middle;
        height: 3;
        margin-top: 1;
    }

    #btn-start {
        margin-right: 2;
    }

    /* ─── Audit screen ──────────────────────────────────────────────────── */

    #audit-body {
        height: 1fr;
    }

    #stats-pane {
        width: 28;
        min-width: 24;
        border-right: solid $primary;
        padding: 1 2;
        background: $surface;
    }

    #stats-heading {
        text-align: center;
        text-style: bold;
        color: $primary;
        margin-bottom: 1;
    }

    .stat-item {
        height: 1;
    }

    #log-pane {
        width: 1fr;
        padding: 0 1;
    }

    #log-heading {
        text-style: bold;
        color: $primary;
    }

    #status-bar {
        height: 3;
        border-top: solid $primary;
        background: $surface;
        padding: 0 2;
        align: left middle;
    }

    /* ─── Complete screen ─────────────────────────────────────────────────── */

    CompleteScreen {
        align: center middle;
    }

    #complete-container {
        width: 60;
        border: round $success;
        padding: 2 4;
        background: $surface;
    }

    #complete-title {
        text-align: center;
        text-style: bold;
        color: $success;
        margin-bottom: 1;
        padding-bottom: 1;
        border-bottom: solid $success;
    }

    #complete-table {
        margin-bottom: 1;
    }

    #output-path {
        color: $text;
        text-style: italic;
        margin-bottom: 1;
    }

    #complete-btn-row {
        align: center middle;
        height: 3;
        margin-top: 1;
    }

    #btn-back {
        margin-right: 2;
    }
    """

    def on_mount(self) -> None:
        self.sub_title = f"v{get_version()}  ·  Cisco Access Switch Auditor"
        self.push_screen(SetupScreen())
