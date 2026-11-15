"""Validated parameters for a single audit run.

The CLI and the TUI collect the same handful of run settings - devices file,
output workbook, stale threshold, worker count and jump host - from different
front ends. Validating them through one pydantic model keeps the rules in one
place, so the two cannot drift apart (the TUI previously accepted a negative
stale threshold that the CLI rejected).
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    NonNegativeInt,
    PositiveInt,
    ValidationError,
)

from assets.config_schema import describe_errors

from .validators import validate_output_filename


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip() or None
    return value


class AuditOptions(BaseModel):
    """Everything one audit run needs apart from credentials."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    devices_file: str = Field("devices.txt", min_length=1)
    output: Annotated[str, AfterValidator(validate_output_filename)]
    stale_days: NonNegativeInt
    workers: PositiveInt
    jump_host: Annotated[str | None, BeforeValidator(_blank_to_none)] = None


class AuditOptionsError(ValueError):
    """Raised by :func:`build_options` with one readable line per problem."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def build_options(labels: dict[str, str] | None = None, **values: Any) -> AuditOptions:
    """
    Validate run settings, naming fields the way the caller's UI does.

    Args:
        labels: Display names for each field, e.g. ``{"workers": "--workers"}``.
        **values: AuditOptions fields.

    Raises:
        AuditOptionsError: If any value is invalid.
    """
    try:
        return AuditOptions(**values)
    except ValidationError as exc:
        raise AuditOptionsError(describe_errors(exc, labels=labels)) from None
