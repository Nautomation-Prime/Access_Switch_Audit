"""Excel workbook generation and formatting for audit results."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .formatters import format_worksheet, sheet_name_from, unique_sheet_name

log = logging.getLogger(__name__)

# Percentage columns are derived, not additive: summing them down the device
# column produces a meaningless number like 780%. Recompute each from its
# underlying counts on the TOTAL row instead.
_PCT_SOURCE_COLUMNS: dict[str, tuple[str, str]] = {
    "% Access of Total": ("Access Ports", "Total Ports (phy)"),
    "% Trunk of Total": ("Trunk Ports", "Total Ports (phy)"),
    "% Routed of Total": ("Routed Ports", "Total Ports (phy)"),
    "% Connected of Total": ("Connected", "Total Ports (phy)"),
    "% AdminDown of Total": ("Admin Down", "Total Ports (phy)"),
    # Chassis PoE utilisation across the estate, not the sum of percentages.
    "PoE Used %": ("PoE Used (W)", "PoE Available (W)"),
}

# Columns where the meaningful estate-wide figure is the worst case, not a sum:
# adding four switches' inlet temperatures together says nothing, but the
# hottest switch in the estate is exactly what you want to see.
_MAX_COLUMNS = (
    "Inlet Temp (C)",
    "CPU 5s %",
    "CPU 1m %",
    "CPU 5m %",
)


def _public_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """
    Strip internal metadata from a summary row.

    Collectors pass extra worksheets to the reporter through underscore-prefixed
    keys on the summary dict. Those are plumbing, not report columns, so they are
    removed before the frame is built.
    """
    return {k: v for k, v in summary.items() if not k.startswith("_")}


def _collect_extra_sheets(
    results: list[tuple[str, str | None, list[dict[str, Any]], dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """Merge every device's collector worksheets into one sheet per name."""
    sheets: dict[str, list[dict[str, Any]]] = {}
    for _ip, _hostname, _detailed, summary in results:
        for name, rows in (summary.get("_extra_sheets") or {}).items():
            sheets.setdefault(name, []).extend(rows)
    return sheets


class ExcelReporter:
    """
    Handles writing the discovery results to an Excel workbook.

    Creates a filters-only workbook with:
    - SUMMARY sheet (first): aggregated per-device metrics + TOTAL row
    - Per-device sheets: detailed interface records with conditional formatting
    """

    def _build_summary_frame(self, summary_rows: list[dict[str, Any]]) -> pd.DataFrame:
        """
        Build the SUMMARY frame and append a grand-total row.

        Count columns are summed; percentage columns are recomputed from the
        summed counts; text columns are left blank.
        """
        df_sum = pd.DataFrame([_public_summary(row) for row in summary_rows])
        if df_sum.empty:
            return df_sum

        totals: dict[str, Any] = {}
        for col in df_sum.columns:
            if col in _PCT_SOURCE_COLUMNS:
                totals[col] = 0.0  # filled in below, once the counts are known
            elif col in _MAX_COLUMNS and pd.api.types.is_numeric_dtype(df_sum[col]):
                peak = df_sum[col].max()
                totals[col] = "" if pd.isna(peak) else peak
            elif pd.api.types.is_numeric_dtype(df_sum[col]):
                totals[col] = df_sum[col].sum()
            else:
                totals[col] = ""

        for pct_col, (count_col, denom_col) in _PCT_SOURCE_COLUMNS.items():
            if pct_col not in totals:
                continue
            # float() rather than isinstance: a pandas column sum is a numpy
            # scalar, which is not an instance of int/float on every platform.
            try:
                numerator = float(totals.get(count_col))
                denominator = float(totals.get(denom_col))
            except (TypeError, ValueError):
                totals[pct_col] = 0.0
                continue
            totals[pct_col] = round(numerator / denominator * 100.0, 1) if denominator else 0.0

        totals["Device"] = "TOTAL"
        totals["Mgmt IP"] = ""
        # A per-device verdict does not aggregate; show the failure count instead.
        if "Audit Result" in totals:
            failures = int((df_sum["Audit Result"] != "OK").sum())
            totals["Audit Result"] = "OK" if not failures else f"{failures} FAILED"
        if "Error" in totals:
            totals["Error"] = ""

        return pd.concat([df_sum, pd.DataFrame([totals])], ignore_index=True)

    def save_to_excel(
        self,
        results: list[tuple[str, str | None, list[dict[str, Any]], dict[str, Any]]],
        output_filename: str,
    ) -> Path:
        """
        Generate and save the Excel workbook with audit results.

        If the target file is locked (typically open in Excel), the workbook is
        written alongside it with a timestamp suffix rather than losing the run.

        Args:
            results: List of (ip, hostname, detailed_records, summary_dict) tuples
            output_filename: Output Excel filename

        Returns:
            The path actually written.

        Raises:
            Exception: If Excel writing fails for any reason other than a lock.
        """
        target = Path(output_filename)
        log.info("Generating Excel workbook: %s", target)

        target.parent.mkdir(parents=True, exist_ok=True)

        try:
            self._write_workbook(results, target)
            log.info("Excel workbook saved successfully: %s", target)
            return target
        except PermissionError:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            fallback = target.with_name(f"{target.stem}_{stamp}{target.suffix}")
            log.warning(
                "%s is locked (is it open in Excel?) — writing to %s instead",
                target, fallback,
            )
            self._write_workbook(results, fallback)
            log.info("Excel workbook saved successfully: %s", fallback)
            return fallback

    def _write_workbook(
        self,
        results: list[tuple[str, str | None, list[dict[str, Any]], dict[str, Any]]],
        path: Path,
    ) -> None:
        """Write the SUMMARY sheet plus one sheet per device to ``path``."""
        with pd.ExcelWriter(path, engine="openpyxl") as xw:
            used_sheet_names: set[str] = set()
            sorted_results = sorted(results, key=lambda x: (x[1] or x[0]))

            # ===== SUMMARY SHEET =====
            # Reserve the name so a device called "SUMMARY" cannot collide with it.
            used_sheet_names.add("SUMMARY")
            df_sum = self._build_summary_frame([s for _, _, _, s in sorted_results])
            df_sum.to_excel(xw, index=False, sheet_name="SUMMARY")
            format_worksheet(xw.sheets["SUMMARY"])
            log.debug("Created SUMMARY sheet with %d rows", len(df_sum))

            # ===== COLLECTOR SHEETS (e.g. INVENTORY) =====
            for name, rows in _collect_extra_sheets(sorted_results).items():
                sheet = unique_sheet_name(name, used_sheet_names)
                pd.DataFrame(rows).to_excel(xw, index=False, sheet_name=sheet)
                format_worksheet(xw.sheets[sheet])
                log.debug("Created collector sheet '%s' with %d rows", sheet, len(rows))

            # ===== PER-DEVICE SHEETS =====
            for ip, hostname, detailed, summary in sorted_results:
                if detailed:
                    df = pd.DataFrame(detailed)
                else:
                    df = pd.DataFrame([{
                        "Device": summary.get("Device", hostname or ip),
                        "Mgmt IP": ip,
                        "Result": summary.get("Audit Result", "ERROR"),
                        "Info": summary.get("Error") or "No interface data returned",
                    }])

                suggested = sheet_name_from(hostname or "", ip)
                sheet = unique_sheet_name(suggested, used_sheet_names)

                df.to_excel(xw, index=False, sheet_name=sheet)
                format_worksheet(xw.sheets[sheet])
                log.debug("Created device sheet '%s' with %d rows", sheet, len(df))
