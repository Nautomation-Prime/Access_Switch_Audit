"""Minimal .env support.

The repository ships a ``.env.example`` telling users to "copy this file to
.env", but nothing ever read it, so every variable set there was silently
ignored. This module closes that gap without adding a dependency.

Real environment variables always win, exactly as ``.env.example`` promises.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

_ENV_FILENAME = ".env"


def _strip_quotes(value: str) -> str:
    """Remove one layer of matching surrounding quotes."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def parse_env_text(text: str) -> dict[str, str]:
    """
    Parse ``KEY=value`` lines from .env content.

    Supports ``#`` comments, blank lines, surrounding quotes, and an optional
    ``export`` prefix. Malformed lines are skipped rather than raising, so a
    stray line cannot stop the tool from starting.
    """
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        # An unquoted trailing comment is not part of the value.
        value = value.strip()
        if value[:1] not in {"'", '"'}:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = _strip_quotes(value)
    return values


def find_env_file(start: Path | None = None) -> Path | None:
    """Locate a .env file in the working directory or the project root."""
    candidates = []
    if start is not None:
        candidates.append(Path(start))
    candidates.append(Path.cwd() / _ENV_FILENAME)
    candidates.append(Path(__file__).resolve().parent.parent / _ENV_FILENAME)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def load_dotenv(path: Path | None = None, override: bool = False) -> dict[str, str]:
    """
    Load a .env file into ``os.environ``.

    Args:
        path: Explicit file to read. When omitted, the working directory and
            then the project root are searched.
        override: When False (the default) a variable already present in the
            environment is left untouched.

    Returns:
        The variables that were applied to the environment.
    """
    env_path = path if path is not None else find_env_file()
    if env_path is None or not env_path.is_file():
        return {}

    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError as exc:
        log.debug("Could not read %s: %s", env_path, exc)
        return {}

    applied: dict[str, str] = {}
    for key, value in parse_env_text(text).items():
        if not override and key in os.environ:
            continue
        os.environ[key] = value
        applied[key] = value

    if applied:
        # Values may include a password; log names only.
        log.debug("Loaded %d variable(s) from %s: %s", len(applied), env_path, ", ".join(sorted(applied)))
    return applied
