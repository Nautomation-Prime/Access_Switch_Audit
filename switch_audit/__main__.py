"""Module entry point for running as python -m switch_audit."""

import sys

from assets.config_loader import ConfigError

try:
    from .cli import main
except ConfigError as _exc:
    # config.yaml is loaded and validated at import time. Report a bad value as
    # a plain message naming the key, not as an import traceback.
    _CONFIG_ERROR = str(_exc)

    def main(argv: list[str] | None = None) -> int:
        """Stand-in entry point used when config.yaml failed validation."""
        print(f"Configuration error: {_CONFIG_ERROR}", file=sys.stderr)
        return 2

if __name__ == "__main__":
    sys.exit(main())
