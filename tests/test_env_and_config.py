"""Tests for .env loading and configuration discovery."""

from __future__ import annotations

import logging

import pytest

from assets.config_loader import Config
from switch_audit.env_file import load_dotenv, parse_env_text

# ── .env parsing ──────────────────────────────────────────────────────────────

def test_parse_env_handles_comments_quotes_and_export() -> None:
    parsed = parse_env_text(
        "\n"
        "# a comment\n"
        "SWITCH_USER=admin\n"
        "export SWITCH_PASS='p@ss word'\n"
        'CREDENTIAL_TARGET="SwitchAudit/User"\n'
        "USE_ENABLE=false  # trailing comment\n"
        "MALFORMED\n"
    )

    assert parsed == {
        "SWITCH_USER": "admin",
        "SWITCH_PASS": "p@ss word",
        "CREDENTIAL_TARGET": "SwitchAudit/User",
        "USE_ENABLE": "false",
    }


def test_a_password_containing_a_hash_survives_when_quoted() -> None:
    assert parse_env_text('SWITCH_PASS="a#b"')["SWITCH_PASS"] == "a#b"


def test_load_dotenv_does_not_override_the_real_environment(tmp_path, monkeypatch) -> None:
    """.env.example promises the shell always wins."""
    env_file = tmp_path / ".env"
    env_file.write_text("SWITCH_USER=from_file\nJUMP_HOST=from_file\n", encoding="utf-8")
    monkeypatch.setenv("SWITCH_USER", "from_shell")
    monkeypatch.delenv("JUMP_HOST", raising=False)

    applied = load_dotenv(env_file)

    import os
    assert os.environ["SWITCH_USER"] == "from_shell"
    assert os.environ["JUMP_HOST"] == "from_file"
    assert applied == {"JUMP_HOST": "from_file"}
    monkeypatch.delenv("JUMP_HOST", raising=False)


def test_load_dotenv_is_a_no_op_when_absent(tmp_path) -> None:
    assert load_dotenv(tmp_path / "nope.env") == {}


# ── Config discovery ──────────────────────────────────────────────────────────

def test_missing_config_falls_back_to_defaults(tmp_path, monkeypatch, caplog) -> None:
    """
    A pip-installed console script has no config.yaml beside the package.
    Raising there made the installed entry point unusable.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SWITCH_AUDIT_CONFIG", str(tmp_path / "absent.yaml"))
    monkeypatch.delenv("JUMP_HOST", raising=False)
    monkeypatch.delenv("CRED_TARGET", raising=False)
    monkeypatch.delenv("CREDENTIAL_TARGET", raising=False)

    with caplog.at_level(logging.WARNING):
        config = Config("absent.yaml")

    assert "falling back to built-in defaults" in caplog.text
    assert config.default_workers == 10
    assert config.connection_timeout == 25
    assert config.retry_attempts == 3
    assert config.jump_host is None


def test_explicit_config_is_read(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("JUMP_HOST", raising=False)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "jump_host: 192.0.2.7\nconcurrency:\n  default_workers: 4\n", encoding="utf-8"
    )

    config = Config(cfg)

    assert config.jump_host == "192.0.2.7"
    assert config.default_workers == 4
    assert config.read_timeout == 60  # untouched section still defaults


def test_empty_config_uses_defaults(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("JUMP_HOST", raising=False)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("# nothing here\n", encoding="utf-8")

    assert Config(cfg).default_workers == 10


def test_credential_target_accepts_both_environment_spellings(tmp_path, monkeypatch) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("credentials:\n  target: FromFile\n", encoding="utf-8")

    monkeypatch.delenv("CRED_TARGET", raising=False)
    monkeypatch.delenv("CREDENTIAL_TARGET", raising=False)
    assert Config(cfg).cred_target == "FromFile"

    monkeypatch.setenv("CREDENTIAL_TARGET", "FromEnv")
    assert Config(cfg).cred_target == "FromEnv"

    monkeypatch.setenv("CRED_TARGET", "Preferred")
    assert Config(cfg).cred_target == "Preferred"


def test_invalid_yaml_is_reported_clearly(tmp_path) -> None:
    from assets.config_loader import ConfigError

    cfg = tmp_path / "config.yaml"
    cfg.write_text("connection:\n  timeout: [unclosed\n", encoding="utf-8")

    with pytest.raises(ConfigError):
        Config(cfg)


def test_shipped_config_matches_the_documented_defaults() -> None:
    """The repository's config.yaml must stay loadable and sane."""
    config = Config()

    assert config.connection_timeout > 0
    assert config.read_timeout >= config.connection_timeout
    assert config.retry_attempts >= 1
    assert 1 <= config.default_workers <= 100
    assert config.min_excel_column_width < config.max_excel_column_width


# ── Schema validation ─────────────────────────────────────────────────────────

def _write(tmp_path, text: str):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(text, encoding="utf-8")
    return cfg


@pytest.fixture
def clean_env(monkeypatch):
    """Strip every variable that overrides config.yaml."""
    from assets.config_schema import ENV_OVERRIDES

    for names in ENV_OVERRIDES.values():
        for name in names:
            monkeypatch.delenv(name, raising=False)


def test_invalid_values_are_reported_by_key(tmp_path, clean_env) -> None:
    from assets.config_loader import ConfigError

    cfg = _write(
        tmp_path,
        "connection:\n  timeout: -5\n  retries: three\n"
        "output:\n  min_column_width: 80\n  max_column_width: 60\n",
    )

    with pytest.raises(ConfigError) as exc_info:
        Config(cfg)

    message = str(exc_info.value)
    assert "connection.timeout" in message
    assert "connection.retries" in message
    assert "'three'" in message
    assert "min_column_width (80) must not exceed max_column_width (60)" in message


def test_a_misspelt_host_key_policy_fails_at_startup(tmp_path, clean_env) -> None:
    """Silently downgrading 'strcit' would defeat the point of the setting."""
    from assets.config_loader import ConfigError

    cfg = _write(tmp_path, "security:\n  host_key_policy: strcit\n")

    with pytest.raises(ConfigError, match="security.host_key_policy"):
        Config(cfg)


def test_host_key_policy_aliases_are_normalised(tmp_path, clean_env) -> None:
    cfg = _write(tmp_path, "security:\n  host_key_policy: TOFU\n")

    assert Config(cfg).host_key_policy == "auto-add"


def test_a_bad_environment_override_names_the_variable(tmp_path, clean_env, monkeypatch) -> None:
    from assets.config_loader import ConfigError

    monkeypatch.setenv("CONNECTION_TIMEOUT", "soon")

    with pytest.raises(ConfigError, match=r"connection\.timeout \(from CONNECTION_TIMEOUT\)"):
        Config(_write(tmp_path, ""))


def test_environment_overrides_are_validated_and_recorded(tmp_path, clean_env, monkeypatch) -> None:
    monkeypatch.setenv("CONNECTION_TIMEOUT", "40")
    monkeypatch.setenv("SWITCH_AUDIT_HOST_KEY_POLICY", "strict")

    config = Config(_write(tmp_path, "connection:\n  timeout: 10\n"))

    assert config.connection_timeout == 40
    assert config.host_key_policy == "strict"
    assert config.env_overrides == {
        "connection.timeout": "CONNECTION_TIMEOUT",
        "security.host_key_policy": "SWITCH_AUDIT_HOST_KEY_POLICY",
    }


def test_an_empty_section_header_means_defaults(tmp_path, clean_env) -> None:
    config = Config(_write(tmp_path, "connection:\naudit:\n  stale:\ncollectors:\n"))

    assert config.connection_timeout == 25
    assert config.stale_use_mac_table is True
    assert config.collector_enabled("switchport") is True


def test_a_quoted_false_really_disables_a_collector(tmp_path, clean_env) -> None:
    """The old loader used bool(value), and bool("false") is True."""
    config = Config(_write(tmp_path, 'collectors:\n  dot1x: "false"\n  vlans: "no"\n'))

    assert config.collector_enabled("dot1x") is False
    assert config.collector_enabled("vlans") is False


def test_unknown_keys_warn_but_still_load(tmp_path, clean_env, caplog) -> None:
    cfg = _write(tmp_path, "conection:\n  timeout: 5\naudit:\n  stale_dayz: 9\n")

    with caplog.at_level(logging.WARNING):
        config = Config(cfg)

    assert "'conection'" in caplog.text
    assert "'audit.stale_dayz'" in caplog.text
    assert config.connection_timeout == 25


def test_blank_jump_host_means_direct(tmp_path, clean_env) -> None:
    assert Config(_write(tmp_path, 'jump_host: "  "\n')).jump_host is None


def test_shipped_config_has_no_unknown_keys(clean_env) -> None:
    """Every key documented in config.yaml must be one the schema understands."""
    from assets.config_schema import unknown_keys

    assert unknown_keys(Config().settings) == []


def test_committed_json_schema_is_current() -> None:
    """Regenerate with: python -m switch_audit --config-schema > config.schema.json"""
    import json
    from pathlib import Path

    from assets.config_schema import json_schema

    committed = Path(__file__).resolve().parent.parent / "config.schema.json"
    assert json.loads(committed.read_text(encoding="utf-8")) == json_schema()


def test_settings_are_immutable(clean_env) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Config().settings.connection.timeout = 1  # type: ignore[misc]
