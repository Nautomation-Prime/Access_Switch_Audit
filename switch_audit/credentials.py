"""Credential collection and storage helpers."""

from __future__ import annotations

import getpass
import logging
import os
import sys

from .app_config import config

log = logging.getLogger(__name__)

_ENV_PAIRS = (("SWITCH_USER", "SWITCH_PASS"), ("CREDENTIAL_USER", "CREDENTIAL_PASS"))


def _credentials_from_env() -> tuple[str, str] | None:
    """Return (username, password) from the first fully-populated env pair."""
    for user_env, pass_env in _ENV_PAIRS:
        user = os.environ.get(user_env)
        pwd = os.environ.get(pass_env)
        if user and pwd:
            log.info("Using credentials from %s / %s", user_env, pass_env)
            return user.strip(), pwd
    return None


def _credentials_from_win_cred(cred_target: str) -> tuple[str, str] | None:
    """Return (username, password) from Windows Credential Manager, if present."""
    try:
        import win32cred  # type: ignore

        cred = win32cred.CredRead(cred_target, win32cred.CRED_TYPE_GENERIC)  # type: ignore
    except Exception:
        log.debug(
            "Credential Manager read failed or unavailable for target %s",
            cred_target, exc_info=True,
        )
        return None

    user = cred.get("UserName")
    blob = cred.get("CredentialBlob")
    pwd = None
    if blob:
        # CredentialBlob is typically UTF-16LE for Windows generic credentials.
        try:
            pwd = blob.decode("utf-16le")
        except Exception:
            try:
                pwd = blob.decode("utf-8", errors="ignore")
            except Exception:
                pwd = None
    if user and pwd:
        return user, pwd
    return None


def get_secret_with_fallback(interactive: bool = True) -> tuple[str, str]:
    """
    Resolve SSH credentials from the environment, a credential store, or a prompt.

    Sources are tried in order:
      1. SWITCH_USER / SWITCH_PASS, then CREDENTIAL_USER / CREDENTIAL_PASS
      2. Windows Credential Manager (target from CREDENTIAL_TARGET, else config)
      3. Interactive prompt

    Args:
        interactive: When False, never read from stdin. Stored credentials are
            used as-is and a missing credential is an error instead of a hang.
            Scheduled and CI runs should pass False.

    Returns:
        Tuple of (username, password)

    Raises:
        RuntimeError: If credentials cannot be obtained.
    """
    env_creds = _credentials_from_env()
    if env_creds:
        return env_creds

    cred_target = os.environ.get("CREDENTIAL_TARGET", config.cred_target)
    stored = _credentials_from_win_cred(cred_target)

    if stored:
        user, pwd = stored
        log.info("Found stored credentials for %s (target: %s)", user, cred_target)
        if not interactive:
            return user, pwd
        # Offer an override, but a bare Enter keeps the stored pair.
        override = input(f"Press Enter to use '{user}', or type a different username: ").strip()
        if not override:
            return user, pwd
        new_pass = getpass.getpass(f"Enter password for {override}: ")
        if not new_pass:
            raise RuntimeError("No password supplied for the overridden username.")
        if _prompt_yes_no(f"Save these credentials to Credential Manager as '{cred_target}'?"):
            _write_win_cred(cred_target, override, new_pass)
        return override, new_pass

    if not interactive:
        raise RuntimeError(
            "No credentials available and prompting is disabled. Set SWITCH_USER and "
            f"SWITCH_PASS, or store them in Credential Manager under '{cred_target}'."
        )

    user = input("Enter switch/jump username: ").strip()
    pwd = getpass.getpass("Enter switch/jump password: ")
    if not user or not pwd:
        raise RuntimeError(
            "Credentials not found in the environment or Credential Manager, "
            "and none were provided interactively."
        )
    if _prompt_yes_no(f"Save these credentials to Credential Manager as '{cred_target}'?"):
        _write_win_cred(cred_target, user, pwd)
    return user, pwd


def _write_win_cred(target: str, username: str, password: str, persist: int = 2) -> bool:
    """
    Write or update a generic credential in Windows Credential Manager.

    Args:
        target: Credential target name (e.g., 'SwitchAudit/User').
        username: Username to store.
        password: Password to store.
        persist: Persistence (2 = local machine).

    Returns:
        True if the write succeeded, False otherwise.
    """
    try:
        if not sys.platform.startswith("win"):
            log.warning("Not a Windows platform; cannot store credentials in Credential Manager.")
            return False

        import win32cred  # type: ignore

        # Prefer bytes; fall back to str if the installed pywin32 expects unicode.
        credential = {
            "Type": win32cred.CRED_TYPE_GENERIC,
            "TargetName": target,
            "UserName": username,
            "CredentialBlob": password.encode("utf-16le"),
            "Comment": "Created by Access Switch Audit tool",
            "Persist": persist,
        }
        try:
            win32cred.CredWrite(credential, 0)
        except TypeError as te:
            log.debug("CredWrite rejected bytes for CredentialBlob (%s); retrying as unicode.", te)
            credential["CredentialBlob"] = password
            win32cred.CredWrite(credential, 0)
        log.info("Stored/updated credentials in Windows Credential Manager: %s", target)
        return True
    except Exception:
        log.exception("Failed to write credentials for '%s'", target)
        return False


def get_enable_secret() -> str | None:
    """
    Retrieve the enable secret if configured.

    Checks the USE_ENABLE environment variable to determine whether enable mode
    is needed. If enabled, reads the password from ENABLE_SECRET.

    Returns:
        Enable password string, or None if not configured.
    """
    use_enable = os.environ.get("USE_ENABLE", "false").lower() in {"1", "true", "yes", "y"}
    if not use_enable:
        return None
    secret = os.environ.get("ENABLE_SECRET")
    if not secret:
        log.warning("USE_ENABLE is set but ENABLE_SECRET is empty; enable mode will be skipped.")
        return None
    return secret


def _prompt_yes_no(msg: str, default_no: bool = True) -> bool:
    """
    Simple interactive [y/N] or [Y/n] prompt.

    Args:
        msg: Prompt message to display
        default_no: If True, the default answer is No; otherwise Yes

    Returns:
        True if the user answered yes, False otherwise.
    """
    suffix = " [y/N] " if default_no else " [Y/n] "
    try:
        ans = input(msg + suffix).strip().lower()
    except EOFError:
            return False  # never perform a side effect on an unattended stdin
    if ans == "":
        return not default_no
    return ans in ("y", "yes")
