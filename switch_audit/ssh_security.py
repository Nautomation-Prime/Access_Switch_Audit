"""SSH host-key verification policy shared by the bastion and device sessions.

Before this module existed, both the jump host and every device were connected
with ``paramiko.AutoAddPolicy()`` and no ``known_hosts`` loaded at all. Because
paramiko only consults the missing-host-key policy for hosts it does not already
know, an empty host-key store meant *every* host was "new" — so a substituted or
changed host key was silently accepted, and the connection was open to a
man-in-the-middle.

Three policies are offered, selected by ``security.host_key_policy``:

``strict``
    Only hosts already present in ``known_hosts`` are accepted. Most secure;
    requires the file to be populated up front.

``auto-add`` (default)
    Trust on first use. An unknown host is accepted and remembered, but a host
    whose key has *changed* is rejected — which is the case that actually
    matters. Non-breaking for existing deployments.

``insecure``
    Accept anything, remember nothing. The pre-1.0 behaviour, kept as an
    explicit escape hatch for lab environments.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import paramiko

log = logging.getLogger(__name__)

STRICT = "strict"
AUTO_ADD = "auto-add"
INSECURE = "insecure"

VALID_POLICIES = (STRICT, AUTO_ADD, INSECURE)
DEFAULT_POLICY = AUTO_ADD

# Spellings users reasonably reach for, mapped onto the canonical names.
_ALIASES = {
    "reject": STRICT,
    "strict": STRICT,
    "yes": STRICT,
    "auto": AUTO_ADD,
    "auto-add": AUTO_ADD,
    "auto_add": AUTO_ADD,
    "autoadd": AUTO_ADD,
    "tofu": AUTO_ADD,
    "insecure": INSECURE,
    "none": INSECURE,
    "no": INSECURE,
    "off": INSECURE,
    "accept-any": INSECURE,
}


class HostKeyPolicyError(ValueError):
    """Raised when an unrecognised host-key policy is configured."""


def normalize_policy(value: Any) -> str:
    """
    Map a configured value onto one of ``VALID_POLICIES``.

    Raises:
        HostKeyPolicyError: If the value is not recognised. Failing loudly is
            deliberate — silently downgrading a misspelled "strcit" to
            accept-anything would defeat the point of the setting.
    """
    if value is None:
        return DEFAULT_POLICY
    key = str(value).strip().lower()
    if not key:
        return DEFAULT_POLICY
    if key in _ALIASES:
        return _ALIASES[key]
    raise HostKeyPolicyError(
        f"Unknown host_key_policy {value!r}. Valid values: {', '.join(VALID_POLICIES)}"
    )


def default_known_hosts() -> Path:
    """Return the standard OpenSSH known_hosts location for this user."""
    return Path.home() / ".ssh" / "known_hosts"


def resolve_known_hosts(configured: str | os.PathLike[str] | None) -> Path:
    """Return the known_hosts path to use, expanding ``~`` and env vars."""
    if configured:
        return Path(os.path.expandvars(str(configured))).expanduser()
    return default_known_hosts()


def apply_to_client(
    client: paramiko.SSHClient,
    policy: str,
    known_hosts: Path | None = None,
    save_host_keys: bool = True,
) -> None:
    """
    Configure a paramiko ``SSHClient`` for the given policy.

    Loads ``known_hosts`` for the verifying policies so that paramiko raises
    ``BadHostKeyException`` when a known host presents a different key.

    Args:
        client: The client to configure, before ``connect()`` is called.
        policy: One of ``VALID_POLICIES``.
        known_hosts: Path to the known_hosts file, or None for the default.
        save_host_keys: Persist newly accepted keys (``auto-add`` only).
    """
    policy = normalize_policy(policy)

    if policy == INSECURE:
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        log.warning(
            "SSH host key verification is disabled (security.host_key_policy: insecure). "
            "Connections are not protected against man-in-the-middle interception."
        )
        return

    path = resolve_known_hosts(known_hosts)
    if path.is_file():
        try:
            # load_host_keys (not load_system_host_keys) keeps the file
            # writable, which is what makes save_host_keys() meaningful.
            client.load_host_keys(str(path))
            log.debug("Loaded known hosts from %s", path)
        except (OSError, paramiko.SSHException) as exc:
            log.warning("Could not read known_hosts at %s: %s", path, exc)
            if policy == STRICT:
                raise
    elif policy == STRICT:
        raise HostKeyPolicyError(
            f"host_key_policy is 'strict' but no known_hosts file was found at {path}. "
            "Populate it (ssh-keyscan, or one manual SSH login per host), point "
            "security.known_hosts at the right file, or use 'auto-add'."
        )
    else:
        log.info("No known_hosts at %s; first-use keys will be recorded there.", path)

    if policy == STRICT:
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())


def persist_host_keys(client: paramiko.SSHClient, known_hosts: Path | None = None) -> None:
    """
    Write newly accepted host keys back to known_hosts.

    Best-effort: an unwritable path is logged, never fatal.
    """
    path = resolve_known_hosts(known_hosts)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        client.save_host_keys(str(path))
        log.debug("Saved host keys to %s", path)
    except (OSError, paramiko.SSHException) as exc:
        log.debug("Could not save host keys to %s: %s", path, exc)


def netmiko_kwargs(
    policy: str,
    known_hosts: Path | None = None,
) -> dict[str, Any]:
    """
    Translate the policy into Netmiko ``ConnectHandler`` keyword arguments.

    Netmiko uses ``ssh_strict`` to choose RejectPolicy over AutoAddPolicy, and
    ``system_host_keys`` / ``alt_key_file`` to decide which file to load.

    Note: Netmiko never writes host keys back, so under ``auto-add`` a device
    key is verified only if it is already recorded in known_hosts.
    """
    policy = normalize_policy(policy)
    if policy == INSECURE:
        return {"ssh_strict": False, "system_host_keys": False}

    kwargs: dict[str, Any] = {"ssh_strict": policy == STRICT}
    path = resolve_known_hosts(known_hosts)
    if path == default_known_hosts():
        kwargs["system_host_keys"] = True
    else:
        kwargs["alt_host_keys"] = True
        kwargs["alt_key_file"] = str(path)
    return kwargs
