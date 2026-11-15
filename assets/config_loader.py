"""
Configuration Loader for Cisco Access Switch Audit Tool
=======================================================

This module provides a robust, user-friendly configuration system that:
- Loads settings from a YAML file for easy user customization
- Supports environment variable overrides for sensitive values
- Validates every value against a typed pydantic schema at startup
- Provides property accessors with sensible defaults

Architecture
------------
The Config class acts as the single source of truth for all tool settings.
The YAML file and any environment overrides are merged and then validated
against ``assets.config_schema.AppSettings``; the properties below read from
that validated model. A bad value (a negative timeout, a misspelt host key
policy, ``default_workers: ten``) is reported once, at startup, naming the key.

Usage Pattern
-------------
```python
from assets.config_loader import Config

config = Config()  # Loads config.yaml from project root
print(config.jump_host)  # Access settings via properties
print(config.connection_timeout)
print(config.settings.connection)  # Or the typed model directly
```

Thread Safety
-------------
Config instances and their settings models are immutable after
initialization and safe to share across threads. The audit tool creates a
single Config instance at startup and passes it to all worker threads.

Extensibility
-------------
To add new configuration options:
  1. Declare the field, with its default and constraints, in config_schema.py
  2. Add the setting to config.yaml with descriptive comments
  3. Create a @property accessor method below
  4. Regenerate config.schema.json: python -m switch_audit --config-schema
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .config_schema import AppSettings, apply_env_overrides, describe_errors, unknown_keys

try:
    import yaml
except ImportError as exc:
    raise ImportError(
        "PyYAML is required for configuration loading. "
        "Install it with: pip install pyyaml"
    ) from exc

log = logging.getLogger(__name__)


class ConfigError(Exception):
    """Raised when configuration is invalid or missing required values."""


class Config:
    """
    Centralized configuration manager for the Cisco Access Switch Audit tool.
    
    Loads settings from a YAML file, layers environment overrides on top, and
    validates the result against the pydantic schema in config_schema.py.
    
    Attributes:
        settings (AppSettings): The validated, immutable configuration model
        path (Path): The YAML file the settings were read from
        env_overrides (dict): Dotted setting names set from environment variables
    
    Raises:
        ConfigError: If the YAML is malformed or a value fails validation
    
    Example:
        >>> config = Config("config.yaml")
        >>> print(config.jump_host)
        '10.112.250.6'
        >>> print(config.connection_timeout)
        25
    """

    def __init__(self, config_file: str | Path = "config.yaml"):
        """
        Initialize the configuration loader.
        
        Args:
            config_file: Path to YAML config file (relative to project root or absolute)
        
        Raises:
            ConfigError: If the YAML syntax is invalid or a value fails validation
        """
        # Resolve relative paths from the working directory first so editable
        # installs and console-script entry points behave the same as repo runs.
        if Path(config_file).is_absolute():
            self._config_file = Path(config_file)
        else:
            override = os.getenv("SWITCH_AUDIT_CONFIG", "").strip()
            candidates = [Path(override)] if override else []
            candidates.append(Path.cwd() / config_file)
            candidates.append(Path(__file__).resolve().parent.parent / config_file)
            self._config_file = next((c for c in candidates if c.exists()), candidates[-1])

        log.debug("Loading configuration from: %s", self._config_file)

        self._config = self._load_yaml()
        # Environment variables take precedence over config file values, and
        # are validated by the same rules.
        merged, sources = apply_env_overrides(self._config, dict(os.environ))
        try:
            self.settings: AppSettings = AppSettings.model_validate(merged)
        except ValidationError as exc:
            problems = "\n".join(f"  - {line}" for line in describe_errors(exc, sources))
            raise ConfigError(f"Invalid configuration in {self._config_file}:\n{problems}") from None

        #: Settings taken from the environment, as {"dotted.key": "VARIABLE"}.
        self.env_overrides: dict[str, str] = {".".join(path): var for path, var in sources.items()}

        for key in unknown_keys(self.settings):
            log.warning("Ignoring unknown setting '%s' in %s", key, self._config_file.name)

        log.info("Configuration loaded successfully from %s", self._config_file.name)

    @property
    def path(self) -> Path:
        """The YAML file the configuration was read from (it may not exist)."""
        return self._config_file

    def _load_yaml(self) -> dict[str, Any]:
        """
        Load and parse the YAML configuration file.
        
        Returns:
            Parsed configuration as a dictionary
        
        Raises:
            ConfigError: If the YAML syntax is invalid or the root is not a mapping
        """
        if not self._config_file.exists():
            # Every setting below has a documented default, so a missing file is
            # a warning rather than a fatal error. Raising here made the
            # installed console script unusable from any directory that did not
            # happen to contain a config.yaml.
            log.warning(
                "Configuration file not found: %s - falling back to built-in defaults. "
                "Place a config.yaml in the working directory, or set "
                "SWITCH_AUDIT_CONFIG, to customise settings.",
                self._config_file,
            )
            return {}

        try:
            with open(self._config_file, encoding="utf-8") as f:
                config = yaml.safe_load(f)
                if config is None:
                    log.warning("%s is empty; using built-in defaults", self._config_file)
                    return {}
                if not isinstance(config, dict):
                    raise ConfigError("Configuration must be a YAML dictionary/mapping")
                return config
        except yaml.YAMLError as e:
            raise ConfigError(f"Invalid YAML syntax in {self._config_file}: {e}") from e

    # ========================================================================
    # Jump Host & Network Access
    # ========================================================================

    @property
    def jump_host(self) -> str | None:
        """
        SSH jump host/bastion server IP or hostname.
        
        Used to proxy connections when switches aren't directly accessible.
        Set to None to connect directly to devices.
        
        Environment variable override: JUMP_HOST
        
        Returns:
            Jump host address or None for direct connections
        """
        return self.settings.jump_host

    # ========================================================================
    # Credential Management
    # ========================================================================

    @property
    def cred_target(self) -> str:
        """
        Windows Credential Manager target name for credential storage/retrieval.
        
        The tool stores and retrieves username/password pairs under this target.
        Users can pre-store credentials using Windows Credential Manager GUI
        or let the tool prompt and save them on first run.
        
        Environment variable override: CRED_TARGET or CREDENTIAL_TARGET
        
        Returns:
            Credential Manager target name (default: 'MyApp/ADM')
        """
        return self.settings.credentials.target

    # ========================================================================
    # Connection Timeouts & Retry Behavior
    # ========================================================================

    @property
    def connection_timeout(self) -> int:
        """
        SSH connection establishment timeout in seconds.
        
        How long to wait when initiating an SSH connection before giving up.
        Increase for slow/high-latency networks.
        
        Environment variable override: CONNECTION_TIMEOUT
        
        Returns:
            Timeout in seconds (default: 25)
        """
        return self.settings.connection.timeout

    @property
    def banner_timeout(self) -> int:
        """
        SSH banner negotiation timeout in seconds.
        
        Time to wait for the SSH banner exchange during connection setup.
        Rarely needs adjustment unless dealing with very slow devices.
        
        Returns:
            Timeout in seconds (default: 30)
        """
        return self.settings.connection.banner_timeout

    @property
    def read_timeout(self) -> int:
        """
        Command execution read timeout in seconds.
        
        Maximum time to wait for a show command to complete and return output.
        Commands like 'show interfaces' on large switches may take 30+ seconds.
        
        Returns:
            Timeout in seconds (default: 60)
        """
        return self.settings.connection.read_timeout

    @property
    def retry_attempts(self) -> int:
        """
        Maximum number of connection retry attempts per device.
        
        If a device connection fails, the tool will retry this many times
        with exponential backoff before marking the device as unreachable.
        
        Returns:
            Number of retry attempts (default: 3)
        """
        return self.settings.connection.retries

    @property
    def retry_base_wait(self) -> float:
        """
        Base wait time in seconds for exponential backoff retry logic.
        
        Actual wait time = retry_base_wait * 2^(attempt - 1), capped at 30s
        Example with base=1: 1s, 2s, 4s before attempts 2, 3, 4
        
        Returns:
            Base wait time in seconds (default: 1.0)
        """
        return self.settings.connection.retry_base_wait

    # ========================================================================
    # SSH Host Key Verification
    # ========================================================================

    @property
    def host_key_policy(self) -> str:
        """
        How SSH host keys are verified: 'strict', 'auto-add', or 'insecure'.

        Environment variable override: SWITCH_AUDIT_HOST_KEY_POLICY

        Returns:
            Policy name (default: 'auto-add' - trust on first use).
        """
        return self.settings.security.host_key_policy

    @property
    def known_hosts(self) -> str | None:
        """
        Path to the known_hosts file, or None to use ~/.ssh/known_hosts.

        Environment variable override: SWITCH_AUDIT_KNOWN_HOSTS

        Returns:
            Path string or None.
        """
        return self.settings.security.known_hosts

    @property
    def save_host_keys(self) -> bool:
        """
        Whether newly accepted host keys are written back to known_hosts.

        Only meaningful under the 'auto-add' policy.

        Returns:
            True to persist keys (default: True).
        """
        return self.settings.security.save_host_keys

    # ========================================================================
    # Audit Behavior & Port Analysis
    # ========================================================================

    @property
    def default_stale_days(self) -> int:
        """
        Number of days of inactivity to consider an access port "stale".
        
        Stale detection logic:
        - For connected ports: Stale if 'Last input' >= N days
        - For disconnected ports: Stale if no PoE draw AND no LLDP/CDP neighbor
        
        Set to 0 to disable stale detection entirely.
        
        Returns:
            Threshold in days (default: 30)
        """
        return self.settings.audit.default_stale_days

    @property
    def enable_port_categorization(self) -> bool:
        """
        Enable automatic port categorization for access ports.
        
        When enabled, each access port receives a category label:
        - trunk/routed: Non-access ports
        - disabled: Administratively down
        - error: err-disabled state
        - stale-unused: Meets stale criteria
        - powered-device: Connected with PoE draw
        - connected: Active connection
        - available: Disconnected and available for use
        
        Returns:
            True to enable categorization, False to disable (default: True)
        """
        return self.settings.audit.enable_port_categorization

    @property
    def error_counter_threshold(self) -> int:
        """
        Error counter threshold for flagging problematic interfaces.
        
        Interfaces with input/output errors exceeding this value are
        highlighted in the Excel output for attention.
        
        Returns:
            Error count threshold (default: 100)
        """
        return self.settings.audit.error_threshold

    @property
    def stale_include_admin_down(self) -> bool:
        """
        Whether administratively-down access ports count as stale.

        Default False: such ports are already out of service and are reported
        under their own category, so counting them again inflates the total.

        Returns:
            True to include admin-down ports in the stale count.
        """
        return self.settings.audit.stale.include_admin_down

    @property
    def stale_use_mac_table(self) -> bool:
        """
        Whether learned MAC addresses suppress the stale flag.

        A port with MACs in the CAM table is demonstrably in use even when the
        endpoint speaks neither CDP nor LLDP and draws no PoE - printers, IoT
        devices and unmanaged switches all look idle without this check.
        Requires the 'mac_table' collector.

        Returns:
            True to treat learned MACs as evidence of use (default: True).
        """
        return self.settings.audit.stale.use_mac_table

    @property
    def oui_file(self) -> str | None:
        """
        Optional CSV of 'oui,vendor' rows extending the built-in vendor table.

        Returns:
            Path string, or None to use only the built-in table.
        """
        return self.settings.audit.oui_file

    @property
    def mac_addresses_per_port(self) -> int:
        """
        How many learned MAC addresses to list per port before truncating.

        Returns:
            Maximum MACs per cell (default: 5).
        """
        return self.settings.audit.mac_addresses_per_port

    # ========================================================================
    # Optional Collectors
    # ========================================================================

    def collector_enabled(self, name: str, default: bool = True) -> bool:
        """
        Whether an optional data collector is switched on.

        Each collector under the 'collectors:' section of config.yaml runs an
        extra show command and contributes extra columns. Turning collectors
        off trades audit depth for runtime.

        Args:
            name: Collector name (e.g. 'switchport', 'mac_table').
            default: Value to use when the key is absent.

        Returns:
            True if the collector should run.
        """
        return self.settings.collectors.get(name, default)

    @property
    def collectors(self) -> dict[str, bool]:
        """Return the validated 'collectors:' mapping from config.yaml."""
        return dict(self.settings.collectors)

    # ========================================================================
    # Concurrency & Performance
    # ========================================================================

    @property
    def default_workers(self) -> int:
        """
        Default number of concurrent device audit worker threads.
        
        Higher values speed up multi-device audits but increase memory/CPU load.
        Recommended range: 5-20 depending on system resources and network capacity.
        
        Can be overridden via --workers CLI argument.
        
        Returns:
            Worker thread count (default: 10)
        """
        return self.settings.concurrency.default_workers

    # ========================================================================
    # Excel Output Configuration
    # ========================================================================

    @property
    def default_output_format(self) -> str:
        """
        Default output filename for audit results Excel workbook.
        
        Can be overridden via --output CLI argument.
        
        Returns:
            Filename string (default: 'audit.xlsx')
        """
        return self.settings.output.default_filename

    @property
    def min_excel_column_width(self) -> int:
        """
        Minimum column width for Excel auto-sizing (in characters).
        
        Prevents columns from being too narrow to read comfortably.
        
        Returns:
            Minimum width in characters (default: 8)
        """
        return self.settings.output.min_column_width

    @property
    def max_excel_column_width(self) -> int:
        """
        Maximum column width for Excel auto-sizing (in characters).
        
        Prevents overly wide columns (e.g., long descriptions) from
        dominating the spreadsheet layout.
        
        Returns:
            Maximum width in characters (default: 60)
        """
        return self.settings.output.max_column_width

    # ========================================================================
    # TextFSM Configuration
    # ========================================================================

    @property
    def textfsm_enabled(self) -> bool:
        """
        Enable TextFSM parsing for structured command output.
        
        TextFSM provides detailed parsing of 'show interfaces' output.
        Disabling falls back to simpler regex-based parsing with less detail.
        
        Returns:
            True to use TextFSM parsing, False for basic parsing (default: True)
        """
        return self.settings.textfsm.enabled

    @property
    def textfsm_template_path(self) -> str | None:
        """
        Optional override path for TextFSM templates directory.
        
        If None, uses the NET_TEXTFSM environment variable.
        Allows customization of parsing templates without modifying environment.
        
        Returns:
            Template directory path or None to use NET_TEXTFSM (default: None)
        """
        return self.settings.textfsm.template_path

    # ========================================================================
    # Utility Methods
    # ========================================================================

    def __repr__(self) -> str:
        """
        String representation showing config file path and loaded sections.
        
        Returns:
            Readable config summary string
        """
        sections = list(self._config.keys())
        return f"<Config file={self._config_file.name} sections={sections}>"

    def __str__(self) -> str:
        """User-friendly string representation."""
        return f"Config({self._config_file.name})"
