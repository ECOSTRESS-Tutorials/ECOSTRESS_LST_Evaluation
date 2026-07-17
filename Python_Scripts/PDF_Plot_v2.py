#!/usr/bin/env python3
"""
ECOSTRESS - Buoy Temperature Residual PDF Plot

Reads one CSV located in the same folder as this script, calculates residuals
(ECOSTRESS temperature minus buoy temperature), and saves one PDF density plot.

Usage:
1. Place this script and the CSV in the same folder.
2. Change CSV_FILENAME in USER CUSTOMIZATIONS.
3. Run: python PDF_Plot_v2.py
"""

# =============================================================================
# USER CUSTOMIZATIONS
# =============================================================================

# Input CSV filename. The CSV must be in the same folder as this script.
CSV_FILENAME = "<<<< REPLACE_THIS_TEXT_WITH_MATCHED_CSV.csv >>>"

# Plot text
PLOT_TITLE = "Title"
X_AXIS_LABEL = "Temperature Difference (°C)"
Y_AXIS_LABEL = "Density"
DATA_LABEL = "Residual Distribution"

# CSV column names
ECOSTRESS_COLUMN = "ecostress_lst"
BUOY_COLUMN = "buoy_temp"

# Figure size in inches
FIGURE_SIZE = (10, 6)

# Axis limits. Set either to None for automatic scaling.
X_LIMITS = (-6, 6)
Y_LIMITS = (0, 0.8)

# Density curve
CURVE_COLOR = "tab:blue"
CURVE_LINE_WIDTH = 2.5
CURVE_FILL = False
CURVE_ALPHA = 0.25
KDE_BANDWIDTH_ADJUST = 1.0

# Zero reference line
SHOW_ZERO_LINE = True
ZERO_LINE_COLOR = "black"
ZERO_LINE_STYLE = "--"
ZERO_LINE_WIDTH = 1.5
ZERO_LINE_LABEL = "Zero Difference"

# Target-error reference lines
SHOW_TARGET_ERROR_LINES = True
TARGET_ERROR_RANGE = (-1, 1)
TARGET_LINE_COLOR = "black"
TARGET_LINE_STYLE = ":"
TARGET_LINE_WIDTH = 1.5
TARGET_LINE_LABEL = "Target Error [-1, 1]"

# General styling
SEABORN_STYLE = "whitegrid"
TITLE_FONT_SIZE = 16
LABEL_FONT_SIZE = 14
TICK_FONT_SIZE = 11
LEGEND_FONT_SIZE = 10
SHOW_LEGEND = True
SHOW_GRID = True

# Output settings
# Set to None to automatically use: <CSV filename>_PDF_plot.pdf
OUTPUT_PDF_FILENAME = None
PDF_DPI = 300
SHOW_PLOT = True

# =============================================================================
# MAIN SCRIPT
# =============================================================================

from pathlib import Path
import sys

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns


def get_script_folder():
    """Return the folder containing this script."""
    try:
        return Path(__file__).resolve().parent
    except NameError:
        return Path.cwd()


def validate_limits(name, limits):
    """Validate an optional two-value axis-limit setting."""
    if limits is None:
        return

    if not isinstance(limits, (tuple, list)) or len(limits) != 2:
        raise ValueError(
            f"{name} must be None or a two-value tuple/list, such as (-6, 6)."
        )

    if limits[0] >= limits[1]:
        raise ValueError(
            f"{name} minimum must be smaller than its maximum: {limits}"
        )


def load_residuals(csv_path):
    """Read the input CSV and return valid ECOSTRESS-minus-buoy residuals."""
    try:
        dataframe = pd.read_csv(csv_path)
    except Exception as exc:
        raise RuntimeError(f"Could not read CSV file:\n{csv_path}\n\n{exc}") from exc

    required_columns = {ECOSTRESS_COLUMN, BUOY_COLUMN}
    missing_columns = sorted(required_columns.difference(dataframe.columns))

    if missing_columns:
        available = ", ".join(map(str, dataframe.columns))
        missing = ", ".join(missing_columns)
        raise ValueError(
            "The CSV is missing required column(s): "
            f"{missing}\n\nAvailable columns:\n{available}"
        )

    ecostress = pd.to_numeric(dataframe[ECOSTRESS_COLUMN], errors="coerce")
    buoy = pd.to_numeric(dataframe[BUOY_COLUMN], errors="coerce")
    residuals = (ecostress - buoy).dropna()

    if residuals.empty:
        raise ValueError(
            "No valid paired numeric values were found in "
            f"'{ECOSTRESS_COLUMN}' and '{BUOY_COLUMN}'."
        )

    if residuals.nunique() < 2:
        raise ValueError(
            "At least two distinct residual values are required to create "
            "a probability-density curve."
        )

    removed_rows = len(dataframe) - len(residuals)
    return residuals, len(dataframe), removed_rows


def create_pdf_plot(residuals, output_path):
    """Create and save one residual probability-density plot."""
    sns.set_style(SEABORN_STYLE)
    figure, axis = plt.subplots(figsize=FIGURE_SIZE)

    sns.kdeplot(
        x=residuals,
        ax=axis,
        label=DATA_LABEL,
        color=CURVE_COLOR,
        linewidth=CURVE_LINE_WIDTH,
        fill=CURVE_FILL,
        alpha=CURVE_ALPHA if CURVE_FILL else None,
        bw_adjust=KDE_BANDWIDTH_ADJUST,
    )

    if SHOW_ZERO_LINE:
        axis.axvline(
            0,
            color=ZERO_LINE_COLOR,
            linestyle=ZERO_LINE_STYLE,
            linewidth=ZERO_LINE_WIDTH,
            label=ZERO_LINE_LABEL,
        )

    if SHOW_TARGET_ERROR_LINES:
        target_min, target_max = TARGET_ERROR_RANGE
        axis.axvline(
            target_min,
            color=TARGET_LINE_COLOR,
            linestyle=TARGET_LINE_STYLE,
            linewidth=TARGET_LINE_WIDTH,
            label=TARGET_LINE_LABEL,
        )
        axis.axvline(
            target_max,
            color=TARGET_LINE_COLOR,
            linestyle=TARGET_LINE_STYLE,
            linewidth=TARGET_LINE_WIDTH,
        )

    if X_LIMITS is not None:
        axis.set_xlim(X_LIMITS)

    if Y_LIMITS is not None:
        axis.set_ylim(Y_LIMITS)

    axis.set_title(PLOT_TITLE, fontsize=TITLE_FONT_SIZE, fontweight="bold")
    axis.set_xlabel(X_AXIS_LABEL, fontsize=LABEL_FONT_SIZE, fontweight="bold")
    axis.set_ylabel(Y_AXIS_LABEL, fontsize=LABEL_FONT_SIZE, fontweight="bold")
    axis.tick_params(axis="both", labelsize=TICK_FONT_SIZE)
    axis.grid(SHOW_GRID)

    if SHOW_LEGEND:
        axis.legend(fontsize=LEGEND_FONT_SIZE)

    figure.tight_layout()
    figure.savefig(output_path, format="pdf", dpi=PDF_DPI, bbox_inches="tight")

    if SHOW_PLOT:
        plt.show()
    else:
        plt.close(figure)


def main():
    """Run the single-CSV residual PDF plotting workflow."""
    validate_limits("X_LIMITS", X_LIMITS)
    validate_limits("Y_LIMITS", Y_LIMITS)

    script_folder = get_script_folder()
    csv_path = script_folder / CSV_FILENAME

    if not csv_path.is_file():
        csv_files = sorted(path.name for path in script_folder.glob("*.csv"))
        detected = "\n".join(f"  - {name}" for name in csv_files) or "  None found"
        raise FileNotFoundError(
            f"CSV file not found:\n{csv_path}\n\n"
            "Make sure the CSV is in the same folder as this script and that "
            "CSV_FILENAME matches it exactly.\n\n"
            f"CSV files detected in the folder:\n{detected}"
        )

    output_name = OUTPUT_PDF_FILENAME or f"{csv_path.stem}_PDF_plot.pdf"
    if not output_name.lower().endswith(".pdf"):
        output_name += ".pdf"

    output_path = script_folder / output_name
    residuals, total_rows, removed_rows = load_residuals(csv_path)

    print(f"Input CSV: {csv_path.name}")
    print(f"Total rows read: {total_rows}")
    print(f"Valid residuals plotted: {len(residuals)}")
    print(f"Rows excluded because of missing/non-numeric values: {removed_rows}")
    print(f"Mean residual: {residuals.mean():.3f} °C")
    print(f"Median residual: {residuals.median():.3f} °C")

    create_pdf_plot(residuals, output_path)
    print(f"\nPDF plot saved to:\n{output_path}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"\nERROR: {error}", file=sys.stderr)
        sys.exit(1)
