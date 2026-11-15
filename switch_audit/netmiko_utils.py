"""Network device connection utilities using Netmiko."""
import logging
from typing import Any

from netmiko import ConnectHandler
from netmiko.base_connection import BaseConnection

from . import ssh_security
from .app_config import config
from .jump_manager import JumpManager

log = logging.getLogger(__name__)

def connect_to_device(
    ip: str,
    username: str,
    password: str,
    device_type: str = "cisco_ios",
    jump: JumpManager | None = None,
    port: int = 22,
    allow_agent: bool = False,
    look_for_keys: bool = False,
    **extras: Any,
) -> BaseConnection:
    """
    Lightweight wrapper around Netmiko.ConnectHandler.
    
    Accepts optional jump (JumpManager) which must provide a direct-tcpip channel
    via .open_channel().
    
    Args:
        ip: Device IP address or hostname
        username: SSH username
        password: SSH password
        device_type: Netmiko device type (default: cisco_ios)
        jump: Optional JumpManager for bastion connectivity
        port: SSH port (default: 22)
        allow_agent: Allow SSH agent (default: False)
        look_for_keys: Search for SSH keys (default: False)
        **extras: Additional parameters to pass to ConnectHandler
    
    Returns:
        BaseConnection: Netmiko connection object
    
    Raises:
        Exception: If connection fails
    """
    kwargs: dict[str, Any] = {
        "device_type": device_type,
        "host": ip,
        "username": username,
        "password": password,
        "port": port,
        "allow_agent": allow_agent,
        "look_for_keys": look_for_keys,
        "auth_timeout": config.connection_timeout,
        "banner_timeout": config.banner_timeout,
        "conn_timeout": config.connection_timeout,
        "fast_cli": False,
    }
    # Host key verification for the device session itself. Without this the
    # device leg was accept-anything even when the bastion leg was verified.
    kwargs.update(ssh_security.netmiko_kwargs(config.host_key_policy, config.known_hosts))
    kwargs.update(extras)
    
    if jump:
        try:
            sock = jump.open_channel(ip, port)
            kwargs["sock"] = sock
            log.debug("Opened jump channel to %s:%s", ip, port)
        except Exception:
            log.exception("Failed to open jump channel to %s:%s", ip, port)
            raise

    # Some netmiko/BaseConnection variants don't accept paramiko-specific
    # flags (look_for_keys / allow_agent) in their __init__; remove them
    # so they aren't forwarded to BaseConnection.__init__.
    for _k in ("look_for_keys", "allow_agent"):
        if _k in kwargs:
            log.debug("Removing unsupported kwarg %s before ConnectHandler()", _k)
            kwargs.pop(_k)

    log.debug("Connecting to device %s (%s)", ip, device_type)
    try:
        return ConnectHandler(**kwargs)
    except Exception:
        log.exception("ConnectHandler failed for %s", ip)
        raise
