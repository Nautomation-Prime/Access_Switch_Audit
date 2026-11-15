"""SSH jump host (bastion) support for device connections."""

from __future__ import annotations

import logging
import threading

import paramiko

from . import ssh_security
from .app_config import config

log = logging.getLogger(__name__)

for name in (
    "paramiko",                 # top-level
    "paramiko.transport",       # connection handshake / auth spam
    "paramiko.client",
    "paramiko.hostkeys",
    "paramiko.kex",
    "paramiko.auth_handler",
    "netmiko",                  # Netmiko wraps Paramiko
):
    logging.getLogger(name).setLevel(logging.WARNING)


class JumpManager:
    """
    Maintains a single SSH connection to the jump server and opens per-device
    'direct-tcpip' channels for Netmiko via the 'sock' parameter.

    One instance is designed to be shared by every worker thread in a run:
    a single authentication against the bastion, many multiplexed channels.
    That avoids one bastion login per device, which commonly trips rate
    limiting or account lockout on production jump hosts.

    Session setup and teardown are guarded by a lock; channel opening is
    delegated to the paramiko Transport, which supports concurrent use.

    Use as a context manager to ensure the jump connection is closed.

    Example:
        >>> with JumpManager('jump.example.com', 'user', 'pass') as jump:
        ...     channel = jump.open_channel('10.1.1.1', 22)
        ...     # Pass channel to Netmiko as the 'sock' kwarg
    """

    def __init__(
        self,
        jump_host: str,
        username: str,
        password: str,
        port: int = 22,
        look_for_keys: bool = False,
        allow_agent: bool = False,
        host_key_policy: paramiko.MissingHostKeyPolicy | None = None,
        timeout: int = 20,
        keepalive: int = 30,
        policy: str | None = None,
        known_hosts: str | None = None,
        save_host_keys: bool | None = None,
    ):
        """
        Initialize the JumpManager.

        Args:
            jump_host: Jump server hostname or IP
            username: SSH username
            password: SSH password
            port: SSH port (default: 22)
            look_for_keys: Whether to search for SSH keys (default: False)
            allow_agent: Whether to use SSH agent (default: False)
            host_key_policy: An explicit paramiko policy object. Overrides
                ``policy`` entirely; provided for callers that need full
                control. Leave unset to use the configured policy.
            timeout: TCP connect timeout in seconds (default: 20)
            keepalive: Transport keepalive interval in seconds, 0 to disable.
                Long audits can otherwise have the bastion drop an idle control
                connection out from under in-flight workers.
            policy: Host key policy name ('strict', 'auto-add', 'insecure').
                Defaults to security.host_key_policy from config.yaml.
            known_hosts: known_hosts path; defaults to the configured value.
            save_host_keys: Persist newly accepted keys; defaults to config.
        """
        self.jump_host = jump_host
        self.username = username
        self.password = password
        self.port = port
        self.look_for_keys = look_for_keys
        self.allow_agent = allow_agent
        self.host_key_policy = host_key_policy
        self.policy = ssh_security.normalize_policy(
            policy if policy is not None else config.host_key_policy
        )
        self.known_hosts = (
            known_hosts if known_hosts is not None else config.known_hosts
        )
        self.save_host_keys = (
            config.save_host_keys if save_host_keys is None else save_host_keys
        )
        self.timeout = timeout
        self.keepalive = keepalive
        self.client: paramiko.SSHClient | None = None
        self._lock = threading.RLock()

    # ── Internals ────────────────────────────────────────────────────────────

    def _transport(self) -> paramiko.Transport | None:
        """Return the live transport, or None if there is not one."""
        if self.client is None:
            return None
        transport = self.client.get_transport()
        if transport is None or not transport.is_active():
            return None
        return transport

    def _close_locked(self) -> None:
        """Close the client. Caller must hold the lock."""
        if self.client is not None:
            try:
                self.client.close()
            except Exception:  # noqa: BLE001 - teardown must never raise
                log.debug("Error closing jump client", exc_info=True)
            finally:
                self.client = None

    # ── Public API ───────────────────────────────────────────────────────────

    def connect(self, force: bool = False) -> None:
        """
        Establish the persistent SSH connection to the jump host.

        Rebuilds the session when the existing transport has died. The previous
        implementation returned early whenever a client object existed, even a
        dead one, so a dropped bastion session was never recovered.

        Args:
            force: Tear down and rebuild even if the session looks alive.
        """
        with self._lock:
            if not force and self._transport() is not None:
                return

            self._close_locked()

            cli = paramiko.SSHClient()
            if self.host_key_policy is not None:
                # Explicit policy object supplied by the caller wins.
                cli.set_missing_host_key_policy(self.host_key_policy)
            else:
                ssh_security.apply_to_client(
                    cli, self.policy, self.known_hosts, self.save_host_keys
                )
            try:
                cli.connect(
                    hostname=self.jump_host,
                    port=self.port,
                    username=self.username,
                    password=self.password,
                    look_for_keys=self.look_for_keys,
                    allow_agent=self.allow_agent,
                    timeout=self.timeout,
                )
            except paramiko.BadHostKeyException as exc:
                try:
                    cli.close()
                except Exception:  # noqa: BLE001
                    pass
                log.error(
                    "Host key mismatch for jump host %s: the server presented a key that does "
                    "not match the one recorded in known_hosts. This can mean the host was "
                    "rebuilt - or that the connection is being intercepted. Verify the new "
                    "fingerprint out of band, then remove the stale entry. (%s)",
                    self.jump_host, exc,
                )
                raise
            except Exception:
                try:
                    cli.close()
                except Exception:  # noqa: BLE001
                    pass
                log.exception("Failed to connect to jump host %s:%s", self.jump_host, self.port)
                raise

            transport = cli.get_transport()
            if transport is not None and self.keepalive:
                transport.set_keepalive(self.keepalive)

            self.client = cli
            if self.host_key_policy is None and self.policy == ssh_security.AUTO_ADD and self.save_host_keys:
                ssh_security.persist_host_keys(cli, self.known_hosts)
            log.debug("Connected to jump host %s:%s", self.jump_host, self.port)

    def open_channel(self, target_host: str, target_port: int = 22) -> paramiko.Channel:
        """
        Open a direct-tcpip channel from jump -> target_host:target_port.

        If the bastion transport has dropped, the session is rebuilt once and
        the channel open is retried.

        Args:
            target_host: Destination device hostname or IP
            target_port: Destination SSH port (default: 22)

        Returns:
            paramiko.Channel suitable for Netmiko's 'sock' kwarg.

        Raises:
            Exception: If channel creation fails.
        """
        for attempt in (0, 1):
            self.connect()
            transport = self._transport()
            if transport is None:
                # Lost a race against another thread's teardown; try again.
                continue
            try:
                chan = transport.open_channel(
                    "direct-tcpip", (target_host, target_port), ("127.0.0.1", 0)
                )
                log.debug("Opened channel to %s:%s via jump host", target_host, target_port)
                return chan
            except Exception:
                # Only a dead transport is worth a rebuild. A refused or filtered
                # target is the device's problem, not the bastion's, and recycling
                # the shared session would disrupt every other in-flight worker.
                if attempt == 0 and not transport.is_active():
                    log.warning(
                        "Jump host transport dropped while opening a channel to %s; reconnecting",
                        target_host,
                    )
                    with self._lock:
                        self._close_locked()
                    continue
                log.exception(
                    "Failed to open channel to %s:%s via jump host", target_host, target_port
                )
                raise

        raise RuntimeError(f"Unable to open a jump channel to {target_host}:{target_port}")

    def close(self) -> None:
        """Close the persistent jump connection."""
        with self._lock:
            if self.client is not None:
                self._close_locked()
                log.debug("Closed jump host connection %s", self.jump_host)

    def __enter__(self) -> JumpManager:
        """Context manager entry."""
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        """Context manager exit."""
        self.close()
