#!/usr/bin/env python3
"""
ECOSTRESS vs. Buoy Water Temperature Scatter Plot

Place this script in the same folder as the matchup CSV.

The script:
1. Reads the CSV from the script's folder.
2. Classifies observations as day or night using buoy local time.
3. Plots buoy temperature against ECOSTRESS LST.
4. Reports R², RMSE, MAE, bias, and sample count for day, night, and all data.
"""

from pathlib import Path
import sys

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score


# =============================================================================
# USER CUSTOMIZATIONS
# =============================================================================

# CSV file located in the same folder as this script.
CSV_FILENAME = "<<< REPLACE_THIS_TEXT_WITH_MATCHED_CSV.csv >>>"

# Axis limits and tick spacing.
X_AXIS_MIN = 20
X_AXIS_MAX = 35
Y_AXIS_MIN = 20
Y_AXIS_MAX = 35
X_TICK_INTERVAL = 5
Y_TICK_INTERVAL = 5
REFERENCE_LINE_PADDING = 1.0

# Plot text.
PLOT_TITLE = "<<< Title >>>"
X_AXIS_LABEL = "Buoy Water Temperature (°C)"
Y_AXIS_LABEL = "ECOSTRESS LST (°C)"
LEGEND_LOCATION = "lower right"

# Font sizes.
TITLE_FONT_SIZE = 16
AXIS_LABEL_FONT_SIZE = 14
STATS_FONT_SIZE = 13

# CSV column names.
TIMESTAMP_COLUMN = "buoy_timestamp"
BUOY_TEMP_COLUMN = "buoy_temp"
ECOSTRESS_TEMP_COLUMN = "ecostress_lst"

# Timestamp format used in TIMESTAMP_COLUMN.
# Example: 201808271430 means August 27, 2018 at 2:30 PM.
TIMESTAMP_FORMAT = "%Y%m%d%H%M"

# Day/night classification. Day includes both endpoints.
DAY_START_HOUR = 6
DAY_END_HOUR = 17

# Data filtering.
MIN_VALID_BUOY_TEMP = 0.0
MIN_VALID_ECOSTRESS_TEMP = 0.0

# Figure appearance.
FIGURE_SIZE = (10, 10)
PLOT_STYLE = "whitegrid"
DAY_COLOR = "red"
NIGHT_COLOR = "blue"
POINT_ALPHA = 0.60
POINT_EDGE_COLOR = "black"
POINT_SIZE = 36

# Statistics block positions in axes coordinates.
DAY_STATS_Y = 0.97
NIGHT_STATS_Y = 0.77
ALL_STATS_Y = 0.57
STATS_X = 0.05
STATS_BOX_OFFSET = 0.04

# Output behavior.
SHOW_PLOT = True
SAVE_PLOT = False
OUTPUT_IMAGE_FILENAME = "ECOSTRESS_vs_Buoy_Scatter.png"
OUTPUT_DPI = 300

# =============================================================================
# MAIN SCRIPT
# =============================================================================


def get_script_folder() -> Path:
    """Return the folder containing this Python script."""
    return Path(__file__).resolve().parent


def load_and_prepare_data(csv_path: Path) -> pd.DataFrame:
    """Load the matchup CSV, validate required columns, and prepare plot fields."""
    if not csv_path.exists():
        raise FileNotFoundError(
            f"CSV file not found:\n  {csv_path}\n\n"
            "Place the CSV in the same folder as this script or update "
            "CSV_FILENAME at the top of the script."
        )

    try:
        df = pd.read_csv(csv_path)
    except Exception as exc:
        raise RuntimeError(f"Could not read CSV file '{csv_path.name}': {exc}") from exc

    required_columns = {
        TIMESTAMP_COLUMN,
        BUOY_TEMP_COLUMN,
        ECOSTRESS_TEMP_COLUMN,
    }
    missing_columns = sorted(required_columns.difference(df.columns))
    if missing_columns:
        raise ValueError(
            "The CSV is missing required column(s): "
            + ", ".join(missing_columns)
            + "\nAvailable columns: "
            + ", ".join(map(str, df.columns))
        )

    # Convert the temperature columns to numeric. Invalid entries become NaN.
    df[BUOY_TEMP_COLUMN] = pd.to_numeric(df[BUOY_TEMP_COLUMN], errors="coerce")
    df[ECOSTRESS_TEMP_COLUMN] = pd.to_numeric(
        df[ECOSTRESS_TEMP_COLUMN], errors="coerce"
    )

    # Convert timestamps. String cleanup handles values sometimes read as floats,
    # such as 201808271430.0.
    timestamp_text = (
        df[TIMESTAMP_COLUMN]
        .astype("string")
        .str.strip()
        .str.replace(r"\.0$", "", regex=True)
    )
    df["buoy_datetime"] = pd.to_datetime(
        timestamp_text,
        format=TIMESTAMP_FORMAT,
        errors="coerce",
    )

    rows_before = len(df)

    # Remove rows that cannot be plotted or classified reliably.
    df = df.dropna(
        subset=["buoy_datetime", BUOY_TEMP_COLUMN, ECOSTRESS_TEMP_COLUMN]
    ).copy()
    df = df[
        (df[BUOY_TEMP_COLUMN] > MIN_VALID_BUOY_TEMP)
        & (df[ECOSTRESS_TEMP_COLUMN] > MIN_VALID_ECOSTRESS_TEMP)
    ].copy()

    if df.empty:
        raise ValueError(
            "No valid rows remain after timestamp, missing-value, and temperature "
            "filters were applied. Check the column names, timestamp format, and "
            "minimum-temperature settings at the top of the script."
        )

    df["hour"] = df["buoy_datetime"].dt.hour
    df["time_of_day"] = np.where(
        df["hour"].between(DAY_START_HOUR, DAY_END_HOUR),
        "day",
        "night",
    )

    removed_rows = rows_before - len(df)
    print(f"Loaded: {csv_path.name}")
    print(f"Valid rows used: {len(df):,}")
    print(f"Rows removed: {removed_rows:,}")
    print(f"Day rows: {(df['time_of_day'] == 'day').sum():,}")
    print(f"Night rows: {(df['time_of_day'] == 'night').sum():,}")

    return df


def fit_and_annotate(
    ax: plt.Axes,
    data: pd.DataFrame,
    label: str,
    color: str,
    y_position: float,
) -> None:
    """Fit linear regression and annotate statistics on the supplied axes."""
    if len(data) < 2:
        ax.text(
            STATS_X,
            y_position,
            f"{label}\nInsufficient data",
            transform=ax.transAxes,
            fontsize=STATS_FONT_SIZE,
            fontweight="bold",
            color=color,
            verticalalignment="top",
        )
        return

    x_values = data[BUOY_TEMP_COLUMN].to_numpy().reshape(-1, 1)
    y_values = data[ECOSTRESS_TEMP_COLUMN].to_numpy()

    regression = LinearRegression().fit(x_values, y_values)
    predicted = regression.predict(x_values)

    r_squared = r2_score(y_values, predicted)
    rmse = mean_squared_error(y_values, predicted) ** 0.5
    mae = mean_absolute_error(y_values, predicted)
    bias = np.mean(y_values - x_values.ravel())
    sample_count = len(data)

    ax.text(
        STATS_X,
        y_position,
        label,
        transform=ax.transAxes,
        fontsize=STATS_FONT_SIZE,
        fontweight="bold",
        color=color,
        verticalalignment="top",
    )

    ax.text(
        STATS_X,
        y_position - STATS_BOX_OFFSET,
        (
            f"N={sample_count}\n"
            f"R²={r_squared:.2f}\n"
            f"RMSE={rmse:.2f}\n"
            f"MAE={mae:.2f}\n"
            f"Bias={bias:.2f}"
        ),
        transform=ax.transAxes,
        fontsize=STATS_FONT_SIZE,
        color="black",
        verticalalignment="top",
        bbox={"boxstyle": "round", "facecolor": "white", "alpha": 0.8},
    )


def create_scatter_plot(df: pd.DataFrame, output_path: Path) -> None:
    """Create, optionally save, and display the scatter plot."""
    sns.set_style(PLOT_STYLE)
    figure, ax = plt.subplots(figsize=FIGURE_SIZE)

    day_data = df[df["time_of_day"] == "day"]
    night_data = df[df["time_of_day"] == "night"]

    if not day_data.empty:
        sns.scatterplot(
            data=day_data,
            x=BUOY_TEMP_COLUMN,
            y=ECOSTRESS_TEMP_COLUMN,
            color=DAY_COLOR,
            label="Day",
            alpha=POINT_ALPHA,
            edgecolor=POINT_EDGE_COLOR,
            s=POINT_SIZE,
            ax=ax,
        )

    if not night_data.empty:
        sns.scatterplot(
            data=night_data,
            x=BUOY_TEMP_COLUMN,
            y=ECOSTRESS_TEMP_COLUMN,
            color=NIGHT_COLOR,
            label="Night",
            alpha=POINT_ALPHA,
            edgecolor=POINT_EDGE_COLOR,
            s=POINT_SIZE,
            ax=ax,
        )

    # Preserve the original dynamic 1:1 reference-line calculation.
    minimum_temperature = min(
        df[BUOY_TEMP_COLUMN].min(),
        df[ECOSTRESS_TEMP_COLUMN].min(),
    ) - REFERENCE_LINE_PADDING
    maximum_temperature = max(
        df[BUOY_TEMP_COLUMN].max(),
        df[ECOSTRESS_TEMP_COLUMN].max(),
    ) + REFERENCE_LINE_PADDING

    ax.plot(
        [minimum_temperature, maximum_temperature],
        [minimum_temperature, maximum_temperature],
        "k--",
        linewidth=1,
        label="1:1 Line",
    )

    fit_and_annotate(ax, day_data, "Day", DAY_COLOR, DAY_STATS_Y)
    fit_and_annotate(ax, night_data, "Night", NIGHT_COLOR, NIGHT_STATS_Y)
    fit_and_annotate(ax, df, "All", "black", ALL_STATS_Y)

    ax.set_xlim(X_AXIS_MIN, X_AXIS_MAX)
    ax.set_ylim(Y_AXIS_MIN, Y_AXIS_MAX)
    ax.set_xticks(np.arange(X_AXIS_MIN, X_AXIS_MAX + X_TICK_INTERVAL, X_TICK_INTERVAL))
    ax.set_yticks(np.arange(Y_AXIS_MIN, Y_AXIS_MAX + Y_TICK_INTERVAL, Y_TICK_INTERVAL))
    ax.set_xlabel(X_AXIS_LABEL, fontsize=AXIS_LABEL_FONT_SIZE, fontweight="bold")
    ax.set_ylabel(Y_AXIS_LABEL, fontsize=AXIS_LABEL_FONT_SIZE, fontweight="bold")
    ax.set_title(PLOT_TITLE, fontsize=TITLE_FONT_SIZE, fontweight="bold")
    ax.legend(loc=LEGEND_LOCATION)

    figure.tight_layout()

    if SAVE_PLOT:
        figure.savefig(output_path, dpi=OUTPUT_DPI, bbox_inches="tight")
        print(f"Plot saved to: {output_path}")

    if SHOW_PLOT:
        plt.show()
    else:
        plt.close(figure)


def main() -> None:
    """Run the complete scatter-plot workflow."""
    script_folder = get_script_folder()
    csv_path = script_folder / CSV_FILENAME
    output_path = script_folder / OUTPUT_IMAGE_FILENAME

    data = load_and_prepare_data(csv_path)
    create_scatter_plot(data, output_path)


if __name__ == "__main__":
    try:
        main()
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"\nERROR: {exc}\n", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nPlot creation cancelled by user.")
        sys.exit(130)
