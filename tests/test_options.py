"""Tests for the shared audit-run options and the config CLI flags."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys

import pytest

from switch_audit.cli import main
from switch_audit.options import AuditOptionsError, build_options


def _options(**overrides):
    values = {"output": "report", "stale_days": 30, "workers": 4}
    values.update(overrides)
    return build_options(**values)


def test_text_from_a_form_is_coerced_and_normalised() -> None:
    options = _options(stale_days=" 60 ", workers="8", output=" r ", jump_host="  ")

    assert options.stale_days == 60
    assert options.workers == 8
    assert options.output == "r.xlsx"
    assert options.jump_host is None
    assert options.devices_file == "devices.txt"


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("workers", 0, "Worker Threads: Input should be greater than 0"),
        ("stale_days", -1, "Stale Port Threshold: Input should be greater than or equal to 0"),
        ("stale_days", "abc", "Stale Port Threshold: Input should be a valid integer"),
        ("output", "bad?.xlsx", "Output Workbook: Invalid character '?'"),
    ],
)
def test_problems_are_reported_with_the_callers_labels(field, value, expected) -> None:
    labels = {"workers": "Worker Threads", "stale_days": "Stale Port Threshold", "output": "Output Workbook"}

    with pytest.raises(AuditOptionsError) as exc_info:
        _options(labels=labels, **{field: value})

    assert len(exc_info.value.problems) == 1
    assert exc_info.value.problems[0].startswith(expected)


def test_cli_rejects_a_negative_stale_threshold(tmp_path, caplog) -> None:
    devices = tmp_path / "devices.txt"
    devices.write_text("192.0.2.10\n", encoding="utf-8")

    with caplog.at_level(logging.ERROR):
        code = main(["--devices", str(devices), "--stale-days", "-1", "--direct", "--non-interactive"])

    assert code == 1
    assert "--stale-days" in caplog.text


def test_show_config_prints_the_effective_settings(capsys) -> None:
    assert main(["--show-config"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Effective configuration from")
    assert "default_workers:" in out
    assert "host_key_policy:" in out


def test_config_schema_flag_prints_json_schema(capsys) -> None:
    assert main(["--config-schema"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert schema["title"] == "Access Switch Audit configuration"
    assert "connection" in schema["properties"]


def test_entry_point_reports_a_bad_config_without_a_traceback(tmp_path) -> None:
    bad = tmp_path / "config.yaml"
    bad.write_text("concurrency:\n  default_workers: 0\n", encoding="utf-8")
    env = {**os.environ, "SWITCH_AUDIT_CONFIG": str(bad), "PYTHONIOENCODING": "utf-8"}

    result = subprocess.run(
        [sys.executable, "-m", "switch_audit", "--version"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
    )

    assert result.returncode == 2
    assert "Configuration error" in result.stderr
    assert "concurrency.default_workers" in result.stderr
    assert "Traceback" not in result.stderr
