"""Tests for the shared bastion session."""

from __future__ import annotations

import threading

import pytest

from switch_audit import jump_manager, ssh_security
from switch_audit.jump_manager import JumpManager


class FakeTransport:
    def __init__(self, active: bool = True):
        self._active = active
        self.keepalive = None
        self.opened: list[tuple] = []
        self.open_error: Exception | None = None

    def is_active(self) -> bool:
        return self._active

    def set_keepalive(self, interval) -> None:
        self.keepalive = interval

    def open_channel(self, kind, dest, src):
        if self.open_error is not None:
            raise self.open_error
        self.opened.append((kind, dest, src))
        return f"channel->{dest[0]}:{dest[1]}"

    def die(self) -> None:
        self._active = False


class FakeSSHClient:
    instances: list[FakeSSHClient] = []

    def __init__(self):
        self.transport = FakeTransport()
        self.connect_kwargs = None
        self.closed = False
        self.policy = None
        self.loaded_host_keys: list[str] = []
        self.saved_host_keys: list[str] = []
        FakeSSHClient.instances.append(self)

    def set_missing_host_key_policy(self, policy) -> None:
        self.policy = policy

    def load_host_keys(self, path) -> None:
        self.loaded_host_keys.append(str(path))

    def load_system_host_keys(self, path=None) -> None:
        self.loaded_host_keys.append(str(path))

    def save_host_keys(self, path) -> None:
        self.saved_host_keys.append(str(path))

    def connect(self, **kwargs) -> None:
        self.connect_kwargs = kwargs

    def get_transport(self):
        return self.transport

    def close(self) -> None:
        self.closed = True
        self.transport.die()


@pytest.fixture
def fake_ssh(monkeypatch):
    FakeSSHClient.instances = []
    monkeypatch.setattr(jump_manager.paramiko, "SSHClient", FakeSSHClient)
    return FakeSSHClient.instances


def test_connect_is_established_once_and_reused(fake_ssh) -> None:
    jump = JumpManager("bastion.example.com", "user", "pass", policy="insecure")

    jump.connect()
    jump.connect()
    jump.open_channel("192.0.2.10")

    assert len(fake_ssh) == 1
    assert fake_ssh[0].connect_kwargs["hostname"] == "bastion.example.com"


def test_keepalive_is_enabled_on_the_transport(fake_ssh) -> None:
    JumpManager("bastion", "user", "pass", keepalive=45, policy="insecure").connect()

    assert fake_ssh[0].transport.keepalive == 45


def test_a_dead_session_is_rebuilt(fake_ssh) -> None:
    """
    connect() previously returned early whenever a client object existed, so a
    bastion that dropped the session was never re-established and every
    remaining device in the run failed.
    """
    jump = JumpManager("bastion", "user", "pass", policy="insecure")
    jump.connect()
    fake_ssh[0].transport.die()

    jump.open_channel("192.0.2.10")

    assert len(fake_ssh) == 2
    assert fake_ssh[1].transport.opened == [("direct-tcpip", ("192.0.2.10", 22), ("127.0.0.1", 0))]


def test_channel_open_failure_on_a_live_transport_is_not_a_reconnect(fake_ssh) -> None:
    """One unreachable device must not recycle the session other workers share."""
    jump = JumpManager("bastion", "user", "pass", policy="insecure")
    jump.connect()
    fake_ssh[0].transport.open_error = OSError("Connection refused")

    with pytest.raises(OSError):
        jump.open_channel("192.0.2.10")

    assert len(fake_ssh) == 1
    assert fake_ssh[0].closed is False


def test_open_channel_is_safe_from_many_threads(fake_ssh) -> None:
    jump = JumpManager("bastion", "user", "pass", policy="insecure")
    channels = []
    errors = []

    def _worker(n: int) -> None:
        try:
            channels.append(jump.open_channel(f"192.0.2.{n}"))
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(1, 21)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(channels) == 20
    # One authentication for the whole fan-out, not one per device.
    assert len(fake_ssh) == 1


def test_context_manager_closes_the_session(fake_ssh) -> None:
    with JumpManager("bastion", "user", "pass", policy="insecure") as jump:
        jump.open_channel("192.0.2.10")

    assert fake_ssh[0].closed is True
    assert jump.client is None


def test_connect_failure_propagates_and_leaves_no_client(monkeypatch, fake_ssh) -> None:
    def _boom(self, **kwargs):
        raise OSError("No route to host")

    monkeypatch.setattr(FakeSSHClient, "connect", _boom)
    jump = JumpManager("bastion", "user", "pass", policy="insecure")

    with pytest.raises(OSError):
        jump.connect()

    assert jump.client is None


# ── Host key verification ────────────────────────────────────────────────────

def test_auto_add_loads_known_hosts_and_persists_new_keys(fake_ssh, tmp_path) -> None:
    """
    Trust on first use: known_hosts is loaded so paramiko can detect a CHANGED
    key, and newly accepted keys are written back.
    """
    known = tmp_path / "known_hosts"
    known.write_text("", encoding="utf-8")

    JumpManager(
        "bastion", "user", "pass", policy="auto-add",
        known_hosts=str(known), save_host_keys=True,
    ).connect()

    client = fake_ssh[0]
    assert client.loaded_host_keys == [str(known)]
    assert client.saved_host_keys == [str(known)]
    assert isinstance(client.policy, jump_manager.paramiko.AutoAddPolicy)


def test_auto_add_can_skip_persisting(fake_ssh, tmp_path) -> None:
    known = tmp_path / "known_hosts"
    known.write_text("", encoding="utf-8")

    JumpManager(
        "bastion", "user", "pass", policy="auto-add",
        known_hosts=str(known), save_host_keys=False,
    ).connect()

    assert fake_ssh[0].saved_host_keys == []


def test_strict_uses_reject_policy(fake_ssh, tmp_path) -> None:
    known = tmp_path / "known_hosts"
    known.write_text("bastion ssh-rsa AAAA", encoding="utf-8")

    JumpManager(
        "bastion", "user", "pass", policy="strict", known_hosts=str(known)
    ).connect()

    client = fake_ssh[0]
    assert isinstance(client.policy, jump_manager.paramiko.RejectPolicy)
    assert client.loaded_host_keys == [str(known)]


def test_strict_without_a_known_hosts_file_refuses_to_connect(fake_ssh, tmp_path) -> None:
    """Failing loudly beats silently downgrading to accept-anything."""
    jump = JumpManager(
        "bastion", "user", "pass", policy="strict",
        known_hosts=str(tmp_path / "absent"),
    )

    with pytest.raises(ssh_security.HostKeyPolicyError, match="strict"):
        jump.connect()

    assert jump.client is None


def test_insecure_loads_nothing(fake_ssh, tmp_path) -> None:
    known = tmp_path / "known_hosts"
    known.write_text("", encoding="utf-8")

    JumpManager(
        "bastion", "user", "pass", policy="insecure", known_hosts=str(known)
    ).connect()

    client = fake_ssh[0]
    assert client.loaded_host_keys == []
    assert client.saved_host_keys == []
    assert isinstance(client.policy, jump_manager.paramiko.AutoAddPolicy)


def test_a_changed_host_key_is_reported_clearly(fake_ssh, tmp_path, monkeypatch, caplog) -> None:
    import logging

    known = tmp_path / "known_hosts"
    known.write_text("", encoding="utf-8")

    class _Key:
        def get_base64(self):
            return "AAAAB3NzaC1yc2E"

    def _bad_key(self, **kwargs):
        raise jump_manager.paramiko.BadHostKeyException("bastion", _Key(), _Key())

    monkeypatch.setattr(FakeSSHClient, "connect", _bad_key)
    jump = JumpManager("bastion", "user", "pass", policy="auto-add", known_hosts=str(known))

    with caplog.at_level(logging.ERROR), pytest.raises(jump_manager.paramiko.BadHostKeyException):
        jump.connect()

    assert "does not match the one recorded in known_hosts" in caplog.text
    assert jump.client is None


def test_an_explicit_policy_object_still_wins(fake_ssh) -> None:
    """Back-compatible escape hatch for callers passing a paramiko policy."""
    policy = jump_manager.paramiko.RejectPolicy()

    JumpManager("bastion", "user", "pass", host_key_policy=policy).connect()

    assert fake_ssh[0].policy is policy
    assert fake_ssh[0].loaded_host_keys == []


# ── Policy normalisation and Netmiko translation ─────────────────────────────

@pytest.mark.parametrize(
    "value, expected",
    [
        (None, "auto-add"), ("", "auto-add"), ("AUTO", "auto-add"), ("auto_add", "auto-add"),
        ("strict", "strict"), ("Reject", "strict"),
        ("insecure", "insecure"), ("off", "insecure"),
    ],
)
def test_policy_names_are_normalised(value, expected) -> None:
    assert ssh_security.normalize_policy(value) == expected


def test_an_unknown_policy_is_rejected_rather_than_downgraded() -> None:
    with pytest.raises(ssh_security.HostKeyPolicyError):
        ssh_security.normalize_policy("strcit")


def test_netmiko_kwargs_track_the_policy(tmp_path) -> None:
    default = ssh_security.netmiko_kwargs("strict", None)
    assert default == {"ssh_strict": True, "system_host_keys": True}

    tofu = ssh_security.netmiko_kwargs("auto-add", None)
    assert tofu == {"ssh_strict": False, "system_host_keys": True}

    off = ssh_security.netmiko_kwargs("insecure", None)
    assert off == {"ssh_strict": False, "system_host_keys": False}

    custom = ssh_security.netmiko_kwargs("strict", tmp_path / "kh")
    assert custom["alt_host_keys"] is True
    assert custom["alt_key_file"] == str(tmp_path / "kh")
