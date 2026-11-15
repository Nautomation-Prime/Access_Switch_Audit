"""Excel formatting utilities for worksheet styling and conditional formatting."""

import re

from openpyxl.formatting.rule import CellIsRule, FormulaRule
from openpyxl.styles import PatternFill
from openpyxl.utils import get_column_letter

from .app_config import config


def format_worksheet(ws) -> None:
    """
    Apply formatting to an Excel worksheet.

    - Freezes header row
    - Adds auto-filter across all columns
    - Auto-sizes column widths based on content
    - Applies conditional formatting:
        * Status values (connected, notconnect, admin down, err-disabled)
        * Error counters > 0 flagged red
        * PoE power > 0 flagged green
        * Stale flags highlighted red

    Args:
        ws: openpyxl worksheet object
    """
    max_row = ws.max_row
    max_col = ws.max_column
    if max_row < 1 or max_col < 1:
        return
    
    ws.freeze_panes = "A2"
    ref = f"A1:{get_column_letter(max_col)}{max_row}"
    ws.auto_filter.ref = ref

    # Auto column widths
    min_width = config.min_excel_column_width
    max_width = config.max_excel_column_width
    
    for col_idx in range(1, max_col + 1):
        col_letter = get_column_letter(col_idx)
        max_len = len(str(ws.cell(row=1, column=col_idx).value or ""))  # header
        for row in range(2, max_row + 1):
            val = ws.cell(row=row, column=col_idx).value
            if val is None:
                continue
            s = str(val)
            if len(s) > max_len:
                max_len = len(s)
        width = max(min_width, min(max_width, max_len + 2))
        ws.column_dimensions[col_letter].width = width

    # Conditional formatting fills
    green_fill = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
    red_fill   = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
    grey_fill  = PatternFill(start_color="D9D9D9", end_color="D9D9D9", fill_type="solid")
    amber_fill = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")

    # Apply rules based on headers
    for col_idx in range(1, max_col + 1):
        header = str(ws.cell(row=1, column=col_idx).value or "").lower()
        col_letter = get_column_letter(col_idx)
        rng = f"{col_letter}2:{col_letter}{max_row}"

        if header == "status":
            ws.conditional_formatting.add(rng,
                CellIsRule(operator="equal", formula=['"connected"'], fill=green_fill))
            ws.conditional_formatting.add(rng,
                CellIsRule(operator="equal", formula=['"notconnect"'], fill=red_fill))
            ws.conditional_formatting.add(rng,
                CellIsRule(operator="equal", formula=['"err-disabled"'], fill=red_fill))
            ws.conditional_formatting.add(rng,
                CellIsRule(operator="equal", formula=['"administratively down"'], fill=grey_fill))
            ws.conditional_formatting.add(rng,
                CellIsRule(operator="equal", formula=['"disabled"'], fill=grey_fill))

        elif "errors" in header:  # Input/Output/CRC Errors
            ws.conditional_formatting.add(rng,
                CellIsRule(operator="greaterThan", formula=['0'], fill=red_fill))

        elif header.startswith("poe power"):
            ws.conditional_formatting.add(rng,
                CellIsRule(operator="greaterThan", formula=['0'], fill=green_fill))

        elif "stale" in header:
            # True values flagged red
            ws.conditional_formatting.add(rng,
                FormulaRule(formula=[f'${col_letter}2=TRUE'], fill=red_fill))

        elif "violation" in header:
            # Port-security violations are always worth a look
            ws.conditional_formatting.add(rng,
                CellIsRule(operator="greaterThan", formula=['0'], fill=red_fill))

        elif header == "poe used %":
            # Little headroom left in the chassis power budget
            ws.conditional_formatting.add(rng,
                CellIsRule(operator="greaterThan", formula=['85'], fill=red_fill))
            ws.conditional_formatting.add(rng,
                CellIsRule(operator="between", formula=['70', '85'], fill=amber_fill))

        elif header in {"config register ok", "stp edge"}:
            # FALSE is the exception worth seeing: a non-standard config
            # register, or an access port without PortFast.
            ws.conditional_formatting.add(rng,
                FormulaRule(formula=[f'${col_letter}2=FALSE'], fill=amber_fill))

        elif header == "mac count":
            # No MACs learned - a strong hint the port is genuinely unused
            ws.conditional_formatting.add(rng,
                CellIsRule(operator="equal", formula=['0'], fill=grey_fill))


def sheet_name_from(base: str, fallback: str) -> str:
    """
    Generate a safe Excel sheet name from a base string.

    Args:
        base: Preferred name (e.g., hostname).
        fallback: Fallback string (e.g., IP address).

    Returns:
        Sanitized sheet name (<= 31 chars, valid characters only).
    """
    name = base or fallback
    sheet = re.sub(r"[^A-Za-z0-9_-]", "_", name)[:31]
    return sheet or re.sub(r"[^A-Za-z0-9_-]", "_", fallback)[:31]


def unique_sheet_name(suggested: str, existing: set[str], limit: int = 31) -> str:
    """
    Ensure sheet names are unique within the workbook.

    Args:
        suggested: Initial sheet name suggestion.
        existing: Set of already-used sheet names.
        limit: Max length allowed by Excel.

    Returns:
        Unique sheet name string.
    """
    lower = {e.lower() for e in existing}
    base = suggested[:limit]
    if base.lower() not in lower:
        existing.add(base)
        return base
    
    for i in range(2, 1000):
        suffix = f"-{i}"
        cand = (base[: max(0, limit - len(suffix))] + suffix)
        if cand.lower() not in lower:
            existing.add(cand)
            return cand
    
    # Fallback if all else fails
    k = 1
    while True:
        cand = f"Sheet{k}"[:limit]
        if cand.lower() not in lower:
            existing.add(cand)
            return cand
        k += 1


def normalize_ifname(ifname: str) -> tuple[str, str]:
    """
    Normalize interface names to canonical short and long forms.

    Args:
        ifname: Raw interface name string.

    Returns:
        (short_form, long_form) tuple, e.g. ("Gi1/0/1", "GigabitEthernet1/0/1").
    """
    _IF_MAP = {
        "gigabitethernet": "Gi",
        "gig": "Gi",
        "gi": "Gi",
        "fastethernet": "Fa",
        "fast": "Fa",
        "fa": "Fa",
        "tengigabitethernet": "Te",
        "tengige": "Te",
        "ten": "Te",
        "te": "Te",
        "twentyfivegige": "Twe",
        "twentyfivegigabitethernet": "Twe",
        "twentyfivegig": "Twe",
        "twe": "Twe",
        "twogige": "Tw",
        "twogigabitethernet": "Tw",
        "twogig": "Tw",
        "tw": "Tw",
        "fivegige": "Fi",
        "fivegigabitethernet": "Fi",
        "fivegig": "Fi",
        "fi": "Fi",
        "fortygige": "Fo",
        "fortygigabitethernet": "Fo",
        "fortygig": "Fo",
        "fo": "Fo",
        "hundredgige": "Hu",
        "hundredgigabit": "Hu",
        "hundredgigabitethernet": "Hu",
        "hu": "Hu",
        "fourhundredgige": "Fh",
        "fourhundredgigabitethernet": "Fh",
        "fourhundredgig": "Fh",
        "fh": "Fh",
        "ethernet": "Eth",
        "eth": "Eth",
        "et": "Eth",
    }
    
    s = (ifname or "").strip()
    if not s:
        return ("", "")
    m = re.match(r"([A-Za-z]+)([0-9/\.]+.*)", s)
    if not m:
        return (s, s)
    prefix_raw = m.group(1)
    rest = m.group(2)
    key = prefix_raw.lower()
    short_prefix = _IF_MAP.get(key, prefix_raw)
    long_prefix = {
        "Gi": "GigabitEthernet",
        "Fa": "FastEthernet",
        "Te": "TenGigabitEthernet",
        "Twe": "TwentyFiveGigE",
        "Tw": "TwoGigE",
        "Fi": "FiveGigE",
        "Fo": "FortyGigE",
        "Hu": "HundredGigE",
        "Fh": "FourHundredGigE",
        "Eth": "Ethernet",
    }.get(short_prefix, prefix_raw)
    return (f"{short_prefix}{rest}", f"{long_prefix}{rest}")


def all_aliases(ifname: str) -> list[str]:
    """
    Return a list of likely alias strings for matching across commands.

    Args:
        ifname: Raw interface name string.

    Returns:
        List of alias strings (short, long, and original).
    """
    sh, lo = normalize_ifname(ifname)
    aliases = {sh, lo, ifname}
    return [a for a in aliases if a]
