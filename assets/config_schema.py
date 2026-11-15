"""
Typed schema for config.yaml
============================

Every setting the tool reads from ``config.yaml`` is declared here as a
pydantic model, together with its type, its default and its permitted range.
Validating the whole file up front means a mistake such as ``timeout: -5``,
``default_workers: ten`` or ``host_key_policy: strcit`` is reported once, at
startup, naming the offending key - instead of surfacing later as a confusing
failure halfway through an audit.

The same models generate ``config.schema.json`` (``--config-schema``), which
YAML-aware editors use for completion and inline validation of config.yaml.

Unknown keys are tolerated and logged as warnings rather than rejected, so a
config file written for a newer or older release still loads.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveInt,
    ValidationError,
    model_validator,
)

log = logging.getLogger(__name__)

HostKeyPolicy = Literal["strict", "auto-add", "insecure"]


def _normalize_host_key_policy(value: Any) -> Any:
    """Accept the spellings ssh_security understands ('tofu', 'auto_add', ...)."""
    # Imported lazily so the schema can be loaded without pulling in paramiko.
    from switch_audit.ssh_security import normalize_policy

    return normalize_policy(value)


def _blank_to_none(value: Any) -> Any:
    """Treat an empty or whitespace-only string as 'not set'."""
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


OptionalText = Annotated[str | None, BeforeValidator(_blank_to_none)]


class _Section(BaseModel):
    """Base for every config.yaml mapping: immutable, tolerant of unknown keys."""

    model_config = ConfigDict(
        extra="allow",
        frozen=True,
        validate_default=True,
        # At runtime an unknown key is only a warning, but editors using the
        # generated schema should still underline it as a probable typo.
        json_schema_extra={"additionalProperties": False},
    )

    @model_validator(mode="before")
    @classmethod
    def _empty_subsections_use_defaults(cls, data: Any) -> Any:
        """
        A section header with nothing under it (``connection:``) parses as None.

        The previous loader quietly fell back to defaults for that case, so a
        commented-out section must keep meaning "use the defaults".
        """
        if not isinstance(data, dict):
            return data
        sections = {
            name for name, info in cls.model_fields.items()
            if isinstance(info.annotation, type) and issubclass(info.annotation, BaseModel)
        }
        return {k: v for k, v in data.items() if not (k in sections and v is None)}


# ── Sections ─────────────────────────────────────────────────────────────────

class CredentialSettings(_Section):
    target: str = Field(
        "MyApp/ADM",
        min_length=1,
        description="Windows Credential Manager target holding the SSH username and password. "
                    "Env override: CRED_TARGET or CREDENTIAL_TARGET.",
    )


class ConnectionSettings(_Section):
    timeout: PositiveInt = Field(
        25, description="SSH connection establishment timeout (seconds). Env override: CONNECTION_TIMEOUT.",
    )
    banner_timeout: PositiveInt = Field(30, description="SSH banner negotiation timeout (seconds).")
    read_timeout: PositiveInt = Field(60, description="Show-command read timeout (seconds).")
    retries: NonNegativeInt = Field(
        3, description="Connection attempts per device before it is reported as failed.",
    )
    retry_base_wait: NonNegativeFloat = Field(
        1.0,
        description="Base delay for exponential backoff (seconds). "
                    "Wait = retry_base_wait * 2^(attempt - 1), capped at 30s.",
    )


class SecuritySettings(_Section):
    host_key_policy: Annotated[HostKeyPolicy, BeforeValidator(_normalize_host_key_policy)] = Field(
        "auto-add",
        description="How SSH host keys are verified: strict (known_hosts only), auto-add "
                    "(trust on first use, reject changed keys) or insecure (accept anything). "
                    "Env override: SWITCH_AUDIT_HOST_KEY_POLICY.",
    )
    known_hosts: OptionalText = Field(
        None,
        description="known_hosts file to verify against; null uses ~/.ssh/known_hosts. "
                    "Supports ~ and environment variables. Env override: SWITCH_AUDIT_KNOWN_HOSTS.",
    )
    save_host_keys: bool = Field(
        True, description="Write newly accepted keys back to known_hosts (auto-add only).",
    )


class StaleSettings(_Section):
    include_admin_down: bool = Field(
        False, description="Count administratively-down access ports as stale.",
    )
    use_mac_table: bool = Field(
        True,
        description="Treat MAC addresses learned on a port as proof it is in use. "
                    "Requires the mac_table collector.",
    )


class AuditSettings(_Section):
    default_stale_days: NonNegativeInt = Field(
        30, description="Days of inactivity after which an access port is stale; 0 disables stale detection.",
    )
    enable_port_categorization: bool = Field(
        True, description="Label every access port with a category in the report.",
    )
    error_threshold: NonNegativeInt = Field(
        100, description="Interface error count above which a port is highlighted.",
    )
    stale: StaleSettings = Field(default_factory=StaleSettings, description="Stale detection tuning.")
    mac_addresses_per_port: PositiveInt = Field(
        5, description="Learned MAC addresses listed per port before truncating.",
    )
    oui_file: OptionalText = Field(
        None, description="Optional CSV ('oui,vendor' per line) extending the built-in MAC vendor table.",
    )


class ConcurrencySettings(_Section):
    default_workers: PositiveInt = Field(10, description="Devices audited concurrently.")


class OutputSettings(_Section):
    default_filename: str = Field(
        "audit.xlsx", min_length=1, description="Default output workbook (overridden by --output).",
    )
    min_column_width: PositiveInt = Field(8, description="Minimum Excel column width (characters).")
    max_column_width: PositiveInt = Field(60, description="Maximum Excel column width (characters).")

    @model_validator(mode="after")
    def _widths_are_ordered(self) -> OutputSettings:
        if self.min_column_width > self.max_column_width:
            raise ValueError(
                f"min_column_width ({self.min_column_width}) must not exceed "
                f"max_column_width ({self.max_column_width})"
            )
        return self


class TextFsmSettings(_Section):
    enabled: bool = Field(True, description="Parse 'show interfaces' with TextFSM.")
    template_path: OptionalText = Field(
        None, description="Template directory override; null uses the NET_TEXTFSM environment variable.",
    )


class AppSettings(_Section):
    """The complete, validated contents of config.yaml."""

    model_config = ConfigDict(title="Access Switch Audit configuration")

    jump_host: OptionalText = Field(
        None,
        description="SSH bastion used to reach the switches; null connects directly. "
                    "Env override: JUMP_HOST.",
    )
    credentials: CredentialSettings = Field(default_factory=CredentialSettings)
    connection: ConnectionSettings = Field(default_factory=ConnectionSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    audit: AuditSettings = Field(default_factory=AuditSettings)
    collectors: dict[str, bool] = Field(
        default_factory=dict,
        description="Optional data collectors to switch on or off by name. "
                    "Run --list-collectors for the available names and their defaults.",
    )
    concurrency: ConcurrencySettings = Field(default_factory=ConcurrencySettings)
    output: OutputSettings = Field(default_factory=OutputSettings)
    textfsm: TextFsmSettings = Field(default_factory=TextFsmSettings)

    @model_validator(mode="before")
    @classmethod
    def _null_collectors_means_none(cls, data: Any) -> Any:
        if isinstance(data, dict) and "collectors" in data and data["collectors"] is None:
            data = {k: v for k, v in data.items() if k != "collectors"}
        return data


# ── Environment overrides ────────────────────────────────────────────────────

#: Environment variables that override a config.yaml value, in priority order.
ENV_OVERRIDES: dict[tuple[str, ...], tuple[str, ...]] = {
    ("jump_host",): ("JUMP_HOST",),
    # .env.example documents CREDENTIAL_TARGET; CRED_TARGET is the older name.
    ("credentials", "target"): ("CRED_TARGET", "CREDENTIAL_TARGET"),
    ("connection", "timeout"): ("CONNECTION_TIMEOUT",),
    ("security", "host_key_policy"): ("SWITCH_AUDIT_HOST_KEY_POLICY",),
    ("security", "known_hosts"): ("SWITCH_AUDIT_KNOWN_HOSTS",),
}


def apply_env_overrides(
    data: dict[str, Any], environ: dict[str, str]
) -> tuple[dict[str, Any], dict[tuple[str, ...], str]]:
    """
    Layer environment overrides onto raw config data, before validation.

    Overrides go through the same validation as the file, so a bad
    ``CONNECTION_TIMEOUT`` is reported just like a bad ``connection.timeout``.

    Returns:
        (merged data, {config path: name of the variable that set it})
    """
    merged = {k: (dict(v) if isinstance(v, dict) else v) for k, v in data.items()}
    sources: dict[tuple[str, ...], str] = {}
    for path, names in ENV_OVERRIDES.items():
        name = next((n for n in names if environ.get(n, "").strip()), None)
        if name is None:
            continue
        target = merged
        for key in path[:-1]:
            if not isinstance(target.get(key), dict):
                target[key] = {}
            target = target[key]
        target[path[-1]] = environ[name].strip()
        sources[path] = name
    return merged, sources


# ── Reporting ────────────────────────────────────────────────────────────────

def describe_errors(
    exc: ValidationError,
    sources: dict[tuple[str, ...], str] | None = None,
    labels: dict[str, str] | None = None,
) -> list[str]:
    """
    Turn a ValidationError into one readable line per problem.

    Args:
        exc: The error raised by pydantic.
        sources: Config paths that came from an environment variable, so the
            message can name the variable the user actually needs to fix.
        labels: Friendlier display names keyed by dotted field path.
    """
    sources = sources or {}
    labels = labels or {}
    lines = []
    for err in exc.errors(include_url=False):
        loc = tuple(str(part) for part in err["loc"])
        dotted = ".".join(loc)
        where = labels.get(dotted, dotted) or "configuration"
        if loc in sources:
            where = f"{where} (from {sources[loc]})"
        message = err["msg"].removeprefix("Value error, ")
        # Echo scalar inputs only: a whole section dict is noise, and a message
        # that already quotes the value does not need it twice.
        value = err.get("input")
        if err["type"] != "missing" and not isinstance(value, (dict, list)) and repr(value) not in message:
            message = f"{message} (got {value!r})"
        lines.append(f"{where}: {message}")
    return lines


def unknown_keys(model: BaseModel, prefix: str = "") -> list[str]:
    """Dotted paths of keys present in the file that no setting recognises."""
    found = [f"{prefix}{key}" for key in (model.model_extra or {})]
    for name in type(model).model_fields:
        value = getattr(model, name)
        if isinstance(value, BaseModel):
            found.extend(unknown_keys(value, f"{prefix}{name}."))
    return found


def json_schema() -> dict[str, Any]:
    """JSON Schema for config.yaml, for editor completion and validation."""
    schema = AppSettings.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    return schema
