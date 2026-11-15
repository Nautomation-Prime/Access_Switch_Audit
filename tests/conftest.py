"""Pytest fixtures for the audit test-suite."""

from __future__ import annotations

import pytest
from fake_device import FakeConnection


@pytest.fixture
def fake_switch(monkeypatch):
    """
    Patch device_auditor.connect_to_device to return a FakeConnection.

    Yields the list of connections handed out, so tests can assert on the
    commands issued and on session teardown.
    """
    from switch_audit import device_auditor

    handed_out: list[FakeConnection] = []

    def _connect(ip, username, password, **kwargs):
        conn = FakeConnection()
        handed_out.append(conn)
        return conn

    monkeypatch.setattr(device_auditor, "connect_to_device", _connect)
    return handed_out
