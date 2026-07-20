#!/usr/bin/env python3
"""
ECOSTRESS Pixel Extraction

Run this script from the same folder as your ECOSTRESS TIFF files and buoy CSV.

What it does:
- Finds all .tif/.tiff files in the working folder.
- Finds the buoy CSV in the working folder automatically.
- Extracts a 12-digit binned timestamp from anywhere in each TIFF filename.
- Matches TIFF timestamps to buoy timestamps.
- Extracts an NxN pixel window with the center pixel containing the buoy station.
- Filters invalid/nodata pixels.
- Converts ECOSTRESS LST from Kelvin to Celsius.
- Applies a valid-pixel threshold and homogeneity filter.
- Writes the matched output CSV.

Expected TIFF timestamp format after the binning step:
    YYYYMMDDHHMM

Examples this script can read:
    doy201807281900_aid0001_18N_1_2.tif
    doy201807281900_aid0001_18N_1.tif
    ECO_L2T_LSTE.002_LST_doy201807290100.tif
    ECO_L2T_LSTE.002_LST_doy201808042200_aid0001_18N_1.tif
    201807281900_filtered.tif

Buoy CSV requirements:
    timestamp, station_id, latitude, longitude, water_temperature
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import rasterio
from rasterio.crs import CRS
from rasterio.transform import rowcol
from rasterio.windows import Window
from rasterio.warp import transform as transform_coords

try:
    from tqdm import tqdm
except ImportError:  # Keeps script usable even if tqdm is not installed.
    def tqdm(iterable: Iterable, total: int | None = None):
        return iterable


# -----------------------------------------------------------------------------
# CONFIGURATION
# -----------------------------------------------------------------------------

# Users only need to change this output filename.
OUTPUT_CSV_NAME = "pixel_extraction_matches.csv"

# Optional: leave as None for automatic buoy CSV detection.
# If your folder contains multiple buoy-like CSV files, set this to one filename.
# Example: BUOY_CSV_NAME = "chesapeake_buoys.csv"
BUOY_CSV_NAME = None

# Read files from the same folder where this script lives.
WORKING_FOLDER = Path(__file__).resolve().parent

# Raster settings.
TIFF_EXTENSIONS = {".tif", ".tiff"}
RASTER_VALUES_ARE_KELVIN = True

# Pixel extraction settings.
# WINDOW_SIZE must be odd: 3 gives 3x3, 5 gives 5x5, etc.
WINDOW_SIZE = 3
REQUIRED_VALID_PIXELS = 9
MAX_CV = 0.15

# Invalid fill values to remove in addition to raster nodata and NaN.
EXTRA_INVALID_VALUES = {-9999.0, -99999.0, 9999.0}

# Required buoy CSV columns.
REQUIRED_COLUMNS = {
    "timestamp",
    "station_id",
    "latitude",
    "longitude",
    "water_temperature",
}

# Read only the required buoy columns.
LOAD_ONLY_REQUIRED_BUOY_COLUMNS = True

# Prefer a timestamp after "doy" when present, but also support any standalone
# 12-digit timestamp anywhere in the filename.
DOY_12_DIGIT_PATTERN = re.compile(r"doy(\d{12})", re.IGNORECASE)
ANY_12_DIGIT_PATTERN = re.compile(r"(?<!\d)(\d{12})(?!\d)")
ANY_12_DIGIT_PATTERN_TEXT = r"(?<!\d)(\d{12})(?!\d)"

# Text values that should be treated as missing after the fast string-based CSV load.
MISSING_TEXT_VALUES = {"", "nan", "none", "null", "na", "n/a", "<na>", "mm"}


# -----------------------------------------------------------------------------
# HELPERS
# -----------------------------------------------------------------------------

def ensure_csv_extension(filename: str) -> str:
    """Return filename with a .csv extension."""
    cleaned = filename.strip()
    if not cleaned:
        raise ValueError("OUTPUT_CSV_NAME cannot be blank.")

    if not cleaned.lower().endswith(".csv"):
        cleaned += ".csv"

    if Path(cleaned).name != cleaned:
        raise ValueError(
            "OUTPUT_CSV_NAME should be a filename only, not a folder path."
        )

    return cleaned


def is_valid_yyyymmddhhmm(timestamp_str: str) -> bool:
    """Validate a 12-digit YYYYMMDDHHMM timestamp."""
    try:
        pd.to_datetime(timestamp_str, format="%Y%m%d%H%M", errors="raise")
        return True
    except Exception:
        return False


def extract_12_digit_timestamp_from_filename(filename: str) -> str | None:
    """
    Extract the first valid 12-digit YYYYMMDDHHMM timestamp from a filename.

    First preference is a timestamp directly after "doy" because the binned
    ECOSTRESS files may retain the original ECOSTRESS naming structure:
        ECO_L2T_LSTE.002_LST_doy201807290100.tif

    If that is not found, the script falls back to any standalone 12-digit
    timestamp anywhere in the filename:
        201807281900_filtered.tif
    """
    candidates: list[str] = []
    candidates.extend(DOY_12_DIGIT_PATTERN.findall(filename))
    candidates.extend(ANY_12_DIGIT_PATTERN.findall(filename))

    # Deduplicate while preserving order.
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)

        if is_valid_yyyymmddhhmm(candidate):
            return candidate

    return None


def normalize_text_missing_values(series: pd.Series) -> pd.Series:
    """Strip text and convert common missing-value tokens to pandas NA."""
    text = series.astype("string").str.strip()
    lower_text = text.str.lower()
    return text.mask(lower_text.isin(MISSING_TEXT_VALUES), pd.NA)


def parse_datetime_series_fast(series: pd.Series) -> pd.Series:
    """
    Parse a text series as datetimes using the fastest available pandas path.

    Pandas 2.x supports format="mixed", which is helpful when a CSV contains
    multiple datetime string styles. Older pandas versions do not, so this
    function falls back cleanly.
    """
    try:
        return pd.to_datetime(series, errors="coerce", format="mixed")
    except Exception:
        return pd.to_datetime(series, errors="coerce")


def normalize_buoy_timestamps(series: pd.Series) -> pd.Series:
    """
    Vectorized normalization of buoy timestamps to YYYYMMDDHHMM.

    Matching priority:
    1. If a value contains a valid standalone 12-digit YYYYMMDDHHMM string,
       use that directly.
    2. Otherwise, parse it as a datetime and format to YYYYMMDDHHMM.

    This replaces the slower row-by-row timestamp parser from v2.
    """
    text = normalize_text_missing_values(series)
    result = pd.Series(pd.NA, index=series.index, dtype="string")

    # Fast path: already contains a 12-digit YYYYMMDDHHMM timestamp.
    candidates = text.str.extract(ANY_12_DIGIT_PATTERN_TEXT, expand=False)
    parsed_candidates = pd.to_datetime(
        candidates,
        format="%Y%m%d%H%M",
        errors="coerce",
    )
    valid_candidate_mask = parsed_candidates.notna()
    result.loc[valid_candidate_mask] = candidates.loc[valid_candidate_mask]

    # Fallback path: normal datetime strings.
    remaining_mask = result.isna() & text.notna()
    if remaining_mask.any():
        parsed_remaining = parse_datetime_series_fast(text.loc[remaining_mask])
        valid_remaining_mask = parsed_remaining.notna()
        if valid_remaining_mask.any():
            formatted = parsed_remaining.dt.strftime("%Y%m%d%H%M")
            valid_indices = formatted.index[valid_remaining_mask]
            result.loc[valid_indices] = formatted.loc[valid_indices]

    return result


def find_buoy_csv(output_csv_path: Path) -> Path:
    """
    Detect the buoy CSV in the working folder.

    If BUOY_CSV_NAME is set, that filename is used.
    Otherwise, the script scans CSV files in the working folder. If there is
    only one CSV, it is used. If there are multiple CSVs, it chooses the one
    whose header contains the required buoy columns.
    """
    if BUOY_CSV_NAME:
        csv_path = WORKING_FOLDER / BUOY_CSV_NAME
        if not csv_path.exists():
            raise FileNotFoundError(f"BUOY_CSV_NAME was set, but this file was not found: {csv_path.name}")
        return csv_path

    csv_paths = sorted(
        path
        for path in WORKING_FOLDER.iterdir()
        if path.is_file()
        and path.suffix.lower() == ".csv"
        and path.resolve() != output_csv_path.resolve()
    )

    if not csv_paths:
        raise FileNotFoundError(
            "No CSV file found in the working folder. Put the buoy CSV in the "
            "same folder as this script."
        )

    if len(csv_paths) == 1:
        return csv_paths[0]

    buoy_like_csvs: list[Path] = []
    for csv_path in csv_paths:
        try:
            columns = set(pd.read_csv(csv_path, nrows=0).columns)
            if REQUIRED_COLUMNS.issubset(columns):
                buoy_like_csvs.append(csv_path)
        except Exception:
            continue

    if len(buoy_like_csvs) == 1:
        return buoy_like_csvs[0]

    csv_names = ", ".join(path.name for path in csv_paths)
    if not buoy_like_csvs:
        raise ValueError(
            "Multiple CSV files were found, but none clearly match the buoy "
            f"CSV column requirements. Found: {csv_names}. Required columns: "
            f"{', '.join(sorted(REQUIRED_COLUMNS))}."
        )

    buoy_names = ", ".join(path.name for path in buoy_like_csvs)
    raise ValueError(
        "Multiple buoy-like CSV files were found. Set BUOY_CSV_NAME at the top "
        f"of the script to choose one. Candidates: {buoy_names}"
    )


def read_buoy_csv_fast(csv_path: Path) -> pd.DataFrame:
    """
    Fast buoy CSV reader that avoids DtypeWarning and unnecessary columns.

    The previous version used pd.read_csv(csv_path), which forced pandas to
    infer dtypes across every column in the file. Large buoy files often contain
    mixed text/numeric values, which can trigger DtypeWarning and slow loading.
    """
    header_columns = list(pd.read_csv(csv_path, nrows=0).columns)
    missing = REQUIRED_COLUMNS.difference(header_columns)
    if missing:
        raise ValueError(
            f"Buoy CSV is missing required columns: {', '.join(sorted(missing))}. "
            f"CSV file: {csv_path.name}"
        )

    if LOAD_ONLY_REQUIRED_BUOY_COLUMNS:
        usecols = sorted(REQUIRED_COLUMNS)
    else:
        usecols = None

    dtype_map = {column: "string" for column in REQUIRED_COLUMNS}

    df = pd.read_csv(
        csv_path,
        usecols=usecols,
        dtype=dtype_map,
        low_memory=False,
    )

    validate_buoy_columns(df, csv_path)

    # Normalize text columns after loading.
    for column in REQUIRED_COLUMNS:
        df[column] = normalize_text_missing_values(df[column])

    # Convert numeric columns once, vectorized.
    for numeric_column in ["latitude", "longitude", "water_temperature"]:
        df[numeric_column] = pd.to_numeric(df[numeric_column], errors="coerce")

    return df


def find_tiff_files() -> list[Path]:
    """Return all TIFF files in the working folder."""
    return sorted(
        path
        for path in WORKING_FOLDER.iterdir()
        if path.is_file() and path.suffix.lower() in TIFF_EXTENSIONS
    )


def index_tiffs_by_timestamp(tiff_paths: list[Path]) -> dict[str, list[Path]]:
    """Map YYYYMMDDHHMM timestamps to TIFF files."""
    timestamp_to_files: dict[str, list[Path]] = {}
    skipped = 0

    for tiff_path in tiff_paths:
        timestamp = extract_12_digit_timestamp_from_filename(tiff_path.name)
        if timestamp is None:
            print(f"[WARNING] Skipped TIFF, no valid 12-digit timestamp found: {tiff_path.name}")
            skipped += 1
            continue

        timestamp_to_files.setdefault(timestamp, []).append(tiff_path)

    print(f"[INFO] Indexed {sum(len(v) for v in timestamp_to_files.values())} TIFF file(s)")
    print(f"[INFO] Unique TIFF timestamps: {len(timestamp_to_files)}")
    if skipped:
        print(f"[INFO] TIFF files skipped for missing/invalid timestamps: {skipped}")

    return timestamp_to_files


def validate_config() -> None:
    """Validate user-editable settings."""
    if WINDOW_SIZE < 1 or WINDOW_SIZE % 2 == 0:
        raise ValueError("WINDOW_SIZE must be an odd positive integer, such as 3, 5, or 7.")

    max_pixels = WINDOW_SIZE * WINDOW_SIZE
    if REQUIRED_VALID_PIXELS < 1 or REQUIRED_VALID_PIXELS > max_pixels:
        raise ValueError(
            f"REQUIRED_VALID_PIXELS must be between 1 and {max_pixels} "
            f"for WINDOW_SIZE={WINDOW_SIZE}."
        )

    if MAX_CV < 0:
        raise ValueError("MAX_CV must be greater than or equal to 0.")


def validate_buoy_columns(df: pd.DataFrame, csv_path: Path) -> None:
    """Ensure the buoy CSV has the expected columns."""
    missing = REQUIRED_COLUMNS.difference(df.columns)
    if missing:
        raise ValueError(
            f"Buoy CSV is missing required columns: {', '.join(sorted(missing))}. "
            f"CSV file: {csv_path.name}"
        )


def buoy_lonlat_to_raster_xy(src: rasterio.io.DatasetReader, lon: float, lat: float) -> tuple[float, float]:
    """
    Convert buoy lon/lat into the raster's coordinate system.

    If the raster CRS is missing, the script assumes the raster already uses
    longitude/latitude coordinates.
    """
    if src.crs is None:
        return lon, lat

    epsg4326 = CRS.from_epsg(4326)
    if src.crs == epsg4326:
        return lon, lat

    x_vals, y_vals = transform_coords(epsg4326, src.crs, [lon], [lat])
    return x_vals[0], y_vals[0]


def read_centered_window(src: rasterio.io.DatasetReader, row_pix: int, col_pix: int) -> np.ndarray:
    """Read an NxN window centered on a raster pixel."""
    half = WINDOW_SIZE // 2
    window = Window(
        col_off=col_pix - half,
        row_off=row_pix - half,
        width=WINDOW_SIZE,
        height=WINDOW_SIZE,
    )

    return src.read(
        1,
        window=window,
        boundless=True,
        fill_value=np.nan,
        out_dtype="float64",
    )


def clean_lst_window(window: np.ndarray, nodata_value) -> np.ndarray:
    """Convert nodata/fill values to NaN and optionally convert Kelvin to Celsius."""
    window = window.astype(float)

    if nodata_value is not None and not np.isnan(nodata_value):
        window[np.isclose(window, nodata_value)] = np.nan

    for invalid_value in EXTRA_INVALID_VALUES:
        window[np.isclose(window, invalid_value)] = np.nan

    if RASTER_VALUES_ARE_KELVIN:
        window = window - 273.15

    return window


def build_empty_output() -> pd.DataFrame:
    """Return an empty output dataframe with stable column order."""
    return pd.DataFrame(
        columns=[
            "buoy_timestamp",
            "ecostress_timestamp",
            "ecostress_file",
            "station_id",
            "latitude",
            "longitude",
            "buoy_temp",
            "ecostress_lst",
            "difference",
        ]
    )


# -----------------------------------------------------------------------------
# MAIN SCRIPT
# -----------------------------------------------------------------------------

def main() -> None:
    validate_config()

    output_csv_name = ensure_csv_extension(OUTPUT_CSV_NAME)
    output_csv_path = WORKING_FOLDER / output_csv_name

    print(f"[INFO] Working folder: {WORKING_FOLDER}")
    print(f"[INFO] Output CSV:     {output_csv_path.name}")

    # 1. Index ECOSTRESS TIFF files by timestamp first. This lets the script
    #    filter buoy rows before doing any raster extraction work.
    print("[INFO] Indexing ECOSTRESS LST files...")
    tiff_paths = find_tiff_files()
    if not tiff_paths:
        raise FileNotFoundError("No .tif or .tiff files found in the working folder.")

    timestamp_to_files = index_tiffs_by_timestamp(tiff_paths)
    if not timestamp_to_files:
        raise ValueError("No TIFF files had a valid 12-digit YYYYMMDDHHMM timestamp.")

    # 2. Find and load buoy data.
    csv_path = find_buoy_csv(output_csv_path)
    if csv_path.resolve() == output_csv_path.resolve():
        raise ValueError("Output CSV name cannot be the same as the input buoy CSV name.")

    print(f"[INFO] Buoy CSV:       {csv_path.name}")
    print("[INFO] Loading buoy data with fast typed reader...")
    df = read_buoy_csv_fast(csv_path)

    print("[INFO] Normalizing buoy timestamps...")
    df["match_timestamp"] = normalize_buoy_timestamps(df["timestamp"])
    invalid_ts_count = int(df["match_timestamp"].isna().sum())
    if invalid_ts_count:
        print(f"[WARNING] Buoy rows with unparseable timestamps skipped: {invalid_ts_count}")

    df = df.dropna(subset=["match_timestamp"])
    print(f"[INFO] Loaded {len(df)} usable buoy row(s)")

    # Keep only buoy rows that actually have a matching TIFF timestamp.
    has_tiff_match_mask = df["match_timestamp"].isin(timestamp_to_files.keys())
    no_timestamp_match_count = int((~has_tiff_match_mask).sum())
    df_to_process = df.loc[has_tiff_match_mask].copy()
    print(f"[INFO] Buoy rows with matching TIFF timestamp: {len(df_to_process)}")

    # Drop invalid buoy coordinates or missing buoy temperatures before raster work.
    invalid_input_mask = df_to_process[["latitude", "longitude", "water_temperature"]].isna().any(axis=1)
    outside_or_invalid_count = int(invalid_input_mask.sum())
    if outside_or_invalid_count:
        print(
            "[WARNING] Buoy rows with invalid latitude/longitude/water_temperature "
            f"skipped: {outside_or_invalid_count}"
        )
    df_to_process = df_to_process.loc[~invalid_input_mask]

    # 3. Match buoy data to ECOSTRESS pixels.
    print(f"[INFO] Matching buoy records to {WINDOW_SIZE}x{WINDOW_SIZE} ECOSTRESS pixels...")
    output_rows: list[dict] = []
    homogeneity_reject_count = 0

    timestamp_group_count = int(df_to_process["match_timestamp"].nunique())
    grouped_rows = df_to_process.groupby("match_timestamp", sort=True)

    for buoy_ts, group in tqdm(grouped_rows, total=timestamp_group_count):
        matching_tiffs = timestamp_to_files.get(str(buoy_ts), [])
        if not matching_tiffs:
            # This should not happen because df_to_process is pre-filtered,
            # but keep the guard in place for safety.
            no_timestamp_match_count += len(group)
            continue

        for tiff_path in matching_tiffs:
            try:
                with rasterio.open(tiff_path) as src:
                    eco_ts = extract_12_digit_timestamp_from_filename(tiff_path.name)

                    for buoy_row in group.itertuples(index=False):
                        try:
                            lat = float(buoy_row.latitude)
                            lon = float(buoy_row.longitude)

                            x, y = buoy_lonlat_to_raster_xy(src, lon, lat)
                            row_pix, col_pix = rowcol(src.transform, x, y)

                            window = read_centered_window(src, row_pix, col_pix)
                            window = clean_lst_window(window, src.nodata)

                            valid_pixels = window[np.isfinite(window)]
                            valid_pixel_count = int(len(valid_pixels))
                            if valid_pixel_count < REQUIRED_VALID_PIXELS:
                                outside_or_invalid_count += 1
                                continue

                            mean = float(np.mean(valid_pixels))
                            std = float(np.std(valid_pixels))
                            cv = float(std / abs(mean)) if mean != 0 else np.inf

                            if cv > MAX_CV:
                                homogeneity_reject_count += 1
                                continue

                            buoy_temp_value = float(buoy_row.water_temperature)
                            difference = abs(mean - buoy_temp_value)

                            output_rows.append(
                                {
                                    "buoy_timestamp": str(buoy_ts),
                                    "ecostress_timestamp": eco_ts,
                                    "ecostress_file": tiff_path.name,
                                    "station_id": buoy_row.station_id,
                                    "latitude": lat,
                                    "longitude": lon,
                                    "buoy_temp": buoy_temp_value,
                                    "ecostress_lst": mean,
                                    "difference": difference,                                }
                            )

                        except Exception:
                            outside_or_invalid_count += 1
                            continue

            except Exception as exc:
                print(f"[WARNING] Could not process {tiff_path.name}: {exc}")
                continue

    # 4. Save matched data.
    print(f"[INFO] Writing {len(output_rows)} filtered match(es) to output CSV...")
    if output_rows:
        output_df = pd.DataFrame(output_rows)
    else:
        output_df = build_empty_output()

    output_df.to_csv(output_csv_path, index=False)

    print("-" * 72)
    print(f"[DONE] Output saved to: {output_csv_path}")
    print(f"[DONE] Matches written: {len(output_rows)}")
    print(f"[INFO] Buoy rows with no matching TIFF timestamp: {no_timestamp_match_count}")
    print(f"[INFO] Rejected for invalid/outside/too few pixels: {outside_or_invalid_count}")
    print(f"[INFO] Rejected by homogeneity CV filter: {homogeneity_reject_count}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[ERROR] {exc}")
        sys.exit(1)
