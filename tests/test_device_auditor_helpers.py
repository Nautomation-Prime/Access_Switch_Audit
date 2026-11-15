from __future__ import annotations

from switch_audit.device_auditor import _parse_ios_version, _parse_last_input_seconds


def test_parse_last_input_seconds_handles_clock_and_compact_formats() -> None:
    assert _parse_last_input_seconds("00:01:23") == 83
    assert _parse_last_input_seconds("1d2h30m") == 95400


def test_parse_last_input_seconds_returns_none_for_never_or_invalid() -> None:
    assert _parse_last_input_seconds("never") is None
    assert _parse_last_input_seconds("not-a-timer") is None


def test_parse_ios_version_prefers_ios_xe_banner() -> None:
    output = "Cisco IOS XE Software, Version 17.6.5a\nCompiled Tue 01-Jan-26"

    assert _parse_ios_version(output) == "IOS-XE 17.6.5a"


def test_parse_ios_version_handles_classic_ios_banner() -> None:
    output = "Cisco IOS Software, C2960X Software, Version 15.2(7)E8, RELEASE SOFTWARE"

    assert _parse_ios_version(output) == "IOS 15.2(7)E8"