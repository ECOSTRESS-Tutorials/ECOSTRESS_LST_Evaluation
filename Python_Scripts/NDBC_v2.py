#!/usr/bin/env python3
"""
NDBC Water Temperature Retrieval & Hourly Averaging

This script retrieves hourly averaged buoy water temperature data from the
National Data Buoy Center (NDBC) API for all stations inside a shapefile Area
of Interest (AOI).

Folder behavior:
- Put this script in the same folder as your AOI shapefile.
- Keep the full shapefile bundle together in that folder: .shp, .shx, .dbf,
  .prj, and any other companion files.
- The script automatically searches this folder for one file ending in .shp.
- The output CSV is written back into this same folder.

User settings to edit:
- LOCAL_TIMEZONE
- START_DATE
- END_DATE
- OUTPUT_CSV_NAME
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytz
from ndbc_api import NdbcApi
from shapely.geometry import Point


# =============================================================================
# 1. USER SETTINGS
# =============================================================================
# Timezone for your study area. Examples: "US/Eastern", "US/Pacific", "UTC".
# To see all valid names in Python, run: python -c "import pytz; print(pytz.all_timezones)"
LOCAL_TIMEZONE = "<<< REPLACE_THIS_TEXT_WITH_TIMEZONE >>>"

# Date range for retrieval. Use YYYY-MM-DD format. The range is inclusive.
START_DATE = "<<< REPLACE_THIS_TEXT_WITH_START_DATE (YYYY-MM-DD) >>>"
END_DATE = "<<< REPLACE_THIS_TEXT_WITH_END_DATE (YYYY-MM-DD) >>>"

# Output CSV filename. The file will be saved in the same folder as this script.
OUTPUT_CSV_NAME = "<<< REPLACE_THIS_TEXT_WITH_OUTPUT_FILENAME.csv >>>"

# NDBC data categories to try. These are the two common categories where buoys
# report water temperature and related measurements.
MODES_TO_TRY = ["ocean", "stdmet"]

# Possible names for water temperature columns in NDBC datasets.
WTMP_COLS_CANDIDATES = ["WTMP", "WTMP1", "WTMP2", "WTMP_M"]


# =============================================================================
# 2. HELPER FUNCTIONS
# =============================================================================
def get_working_folder() -> Path:
    """
    Return the folder where this script lives."""
    if "__file__" in globals():
        return Path(__file__).resolve().parent
    return Path.cwd().resolve()


def find_single_shapefile(folder: Path) -> Path:
    """
    Find exactly one .shp file in the working folder.

    The script stops if no shapefile is found or if multiple shapefiles are
    present."""
    shapefiles = sorted(folder.glob("*.shp"))

    if not shapefiles:
        raise FileNotFoundError(
            f"No .shp file found in working folder: {folder}\n"
            "Place this script in the same folder as your AOI shapefile bundle."
        )

    if len(shapefiles) > 1:
        shapefile_list = "\n".join(f"  - {path.name}" for path in shapefiles)
        raise RuntimeError(
            "Multiple .shp files were found in the working folder.\n"
            "Keep only the AOI shapefile in this folder before running the script, "
            "or move the extra shapefiles elsewhere.\n"
            f"Found:\n{shapefile_list}"
        )

    return shapefiles[0]


def validate_shapefile_bundle(shapefile_path: Path) -> None:
    """
    Warn if common shapefile companion files are missing.

    GeoPandas/Fiona/Pyogrio will do the final read validation. This helper gives
    a clearer early message for the most common folder setup mistake.
    """
    required_sidecars = [".shx", ".dbf"]
    missing = [
        shapefile_path.with_suffix(suffix).name
        for suffix in required_sidecars
        if not shapefile_path.with_suffix(suffix).exists()
    ]

    if missing:
        print(
            "[WARNING] The shapefile may be incomplete. Missing companion file(s): "
            + ", ".join(missing)
        )
        print("[WARNING] GeoPandas may fail to read the shapefile if these are required.")


def make_output_path(folder: Path, output_csv_name: str) -> Path:
    """Create a CSV output path inside the working folder."""
    output_name = output_csv_name.strip()
    if not output_name:
        raise ValueError("OUTPUT_CSV_NAME cannot be blank.")

    output_path = Path(output_name)
    if output_path.suffix.lower() != ".csv":
        output_path = output_path.with_suffix(".csv")

    if not output_path.is_absolute():
        output_path = folder / output_path

    output_path.parent.mkdir(parents=True, exist_ok=True)
    return output_path


def load_aoi(shapefile_path: Path) -> gpd.GeoDataFrame:
    """Load the AOI shapefile and convert it to WGS84/EPSG:4326."""
    print(f"[INFO] Loading shapefile: {shapefile_path.name}")
    aoi = gpd.read_file(shapefile_path)

    if aoi.empty:
        raise ValueError(f"The shapefile is empty: {shapefile_path}")

    print(f"[INFO] AOI original CRS: {aoi.crs}")

    if aoi.crs is None:
        raise ValueError(
            "The AOI shapefile has no CRS defined. "
            "Assign the correct CRS before running this script."
        )

    return aoi.to_crs("EPSG:4326")


def get_local_timezone(timezone_name: str):
    """Validate and return a pytz timezone object."""
    try:
        return pytz.timezone(timezone_name)
    except pytz.UnknownTimeZoneError as exc:
        raise ValueError(
            f"Unknown timezone: {timezone_name!r}. "
            "Use a valid pytz timezone such as 'US/Eastern', 'US/Pacific', or 'UTC'."
        ) from exc


def round_to_nearest_hour(ts: pd.Timestamp) -> pd.Timestamp:
    """Round a timestamp to the nearest hour."""
    if ts.minute < 30:
        return ts.replace(minute=0, second=0, microsecond=0)
    return (ts + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)


def extract_local_timestamps(data: pd.DataFrame, local_tz) -> pd.DatetimeIndex:
    """Extract UTC timestamps from an NDBC dataframe index and convert to local time."""
    if isinstance(data.index, pd.MultiIndex):
        timestamps = pd.to_datetime(data.index.get_level_values(0))
    else:
        timestamps = pd.to_datetime(data.index)

    if timestamps.tz is None:
        timestamps = timestamps.tz_localize("UTC")
    else:
        timestamps = timestamps.tz_convert("UTC")

    return timestamps.tz_convert(local_tz)


def hourly_average_records(
    data: pd.DataFrame,
    wtmp_col: str,
    station_id: str,
    lat: float,
    lon: float,
    local_tz,
) -> list[dict]:
    """Convert station data to hourly averaged water-temperature records."""
    timestamps = extract_local_timestamps(data, local_tz)

    df = pd.DataFrame(
        {
            "timestamp": timestamps,
            "water_temperature": pd.to_numeric(data[wtmp_col].values, errors="coerce"),
        }
    )

    df = df.dropna(subset=["water_temperature"])
    if df.empty:
        return []

    df["rounded_timestamp"] = df["timestamp"].apply(round_to_nearest_hour)

    records = []
    for ts, group in df.groupby("rounded_timestamp"):
        temps = group["water_temperature"]
        temp_std = temps.std(skipna=True)

        if len(temps) <= 2 or temp_std == 0 or pd.isna(temp_std):
            avg_temp = temps.mean()
        else:
            temp_mean = temps.mean()
            filtered = temps[
                (temps >= temp_mean - 2 * temp_std)
                & (temps <= temp_mean + 2 * temp_std)
            ]
            if filtered.empty:
                continue
            avg_temp = filtered.mean()

        records.append(
            {
                "timestamp": ts.strftime("%Y%m%d%H%M"),
                "water_temperature": avg_temp,
                "station_id": station_id,
                "latitude": lat,
                "longitude": lon,
            }
        )

    return records


# =============================================================================
# 3. MAIN WORKFLOW
# =============================================================================
def main() -> None:
    working_folder = get_working_folder()
    shapefile_path = find_single_shapefile(working_folder)
    output_csv_path = make_output_path(working_folder, OUTPUT_CSV_NAME)
    local_tz = get_local_timezone(LOCAL_TIMEZONE)

    print(f"[INFO] Working folder: {working_folder}")
    print(f"[INFO] Auto-detected AOI shapefile: {shapefile_path.name}")
    print(f"[INFO] Local timezone: {LOCAL_TIMEZONE}")
    print(f"[INFO] Date range: {START_DATE} to {END_DATE}")
    print(f"[INFO] Output CSV: {output_csv_path.name}")

    validate_shapefile_bundle(shapefile_path)
    aoi = load_aoi(shapefile_path)

    print("[INFO] Fetching NDBC stations list from API...")
    api = NdbcApi()
    stations_df = api.stations()

    required_station_cols = {"Station", "Lat", "Lon"}
    missing_cols = required_station_cols - set(stations_df.columns)
    if missing_cols:
        raise ValueError(
            "The NDBC stations table is missing expected column(s): "
            + ", ".join(sorted(missing_cols))
        )

    print("[INFO] Filtering stations by AOI boundary...")
    points = [Point(lon, lat) for lat, lon in zip(stations_df["Lat"], stations_df["Lon"])]
    stations_geo = gpd.GeoDataFrame(stations_df, geometry=points, crs="EPSG:4326")
    stations_in_aoi = gpd.sjoin(stations_geo, aoi, how="inner", predicate="intersects")
    stations_in_aoi = stations_in_aoi.drop_duplicates(subset=["Station"])

    if stations_in_aoi.empty:
        print("[ERROR] No NDBC stations found in AOI.")
        print("[DEBUG] Check the shapefile CRS and coordinates.")
        print(aoi.bounds)
        sys.exit(1)

    print(f"[INFO] Found {len(stations_in_aoi)} station(s) inside AOI.")
    print("[DEBUG] Station IDs and coordinates:")
    print(stations_in_aoi[["Station", "Lat", "Lon"]].to_string(index=False))

    print("[INFO] Retrieving buoy data...")
    all_records = []

    for _, row in stations_in_aoi.iterrows():
        station_id = row["Station"]
        lat = row["Lat"]
        lon = row["Lon"]
        station_data_collected = False

        for mode in MODES_TO_TRY:
            try:
                data = api.get_data(
                    station_id=station_id,
                    mode=mode,
                    start_time=START_DATE,
                    end_time=END_DATE,
                    as_df=True,
                )

                if isinstance(data, dict) or data.empty:
                    print(f"[INFO] No valid data for {station_id} mode {mode}.")
                    continue

                wtmp_col = next(
                    (col for col in WTMP_COLS_CANDIDATES if col in data.columns),
                    None,
                )
                if not wtmp_col:
                    print(
                        f"[INFO] No water temperature column found for "
                        f"{station_id} mode {mode}."
                    )
                    continue

                station_records = hourly_average_records(
                    data=data,
                    wtmp_col=wtmp_col,
                    station_id=station_id,
                    lat=lat,
                    lon=lon,
                    local_tz=local_tz,
                )

                if not station_records:
                    print(
                        f"[INFO] No usable water temperature records for "
                        f"{station_id} mode {mode}."
                    )
                    continue

                all_records.extend(station_records)
                print(f"[SUCCESS] Data collected for {station_id} mode {mode}")
                station_data_collected = True
                break

            except Exception as exc:
                print(f"[ERROR] Failed to fetch data for {station_id} mode {mode}: {exc}")

        if not station_data_collected:
            print(f"[WARNING] No water temperature data collected for {station_id}")

    if all_records:
        final_df = pd.DataFrame(all_records)
        final_df = final_df.sort_values(["station_id", "timestamp"]).reset_index(drop=True)
        print(f"[INFO] Writing output CSV to {output_csv_path}")
        final_df.to_csv(output_csv_path, index=False)
    else:
        print("[WARNING] No data collected from any stations.")

    print("[DONE]")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[FATAL] {exc}")
        sys.exit(1)
