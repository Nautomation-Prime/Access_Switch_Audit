"""Validation helpers for input data and files."""

import logging
from pathlib import Path

log = logging.getLogger(__name__)


def validate_devices_file(filepath: str) -> list[str]:
    """
    Validate and parse a devices file.
    
    Args:
        filepath: Path to devices file (one IP/hostname per line, # for comments)
    
    Returns:
        List of device IPs/hostnames
    
    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If file is empty or contains no valid devices
    """
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"Devices file not found: {filepath}")
    
    with open(filepath, encoding="utf-8") as f:
        devices = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]
    
    if not devices:
        raise ValueError(f"No devices found in {filepath}. File must contain at least one IP/hostname.")
    
    log.info("Loaded %d device(s) from %s", len(devices), filepath)
    return devices


def validate_output_filename(filename: str) -> str:
    """
    Validate and normalize output Excel filename.
    
    Args:
        filename: Desired output filename
    
    Returns:
        Normalized filename with .xlsx extension
    
    Raises:
        ValueError: If filename is invalid
    """
    if not filename or filename.strip() == "":
        raise ValueError("Output filename cannot be empty")
    
    filename = filename.strip()
    
    # Ensure .xlsx extension
    if not filename.lower().endswith('.xlsx'):
        filename += '.xlsx'
    
    # Validate only the final filename component (path separators are allowed)
    from pathlib import Path as _Path
    stem = _Path(filename).name
    invalid_chars = '<>":|?*'
    for char in invalid_chars:
        if char in stem:
            raise ValueError(f"Invalid character '{char}' in filename: {stem}")
    
    return filename
