#!/usr/bin/env python3
"""
ECOSTRESS LST Timestamp Binning

Run this script from the same folder as your ECOSTRESS TIFF files.
It scans all .tif and .tiff files in that folder, extracts a UTC timestamp
from filenames, converts the timestamp to local time of specified study site,
bins it to the nearest hour, and writes the output using the selected
OUTPUT_MODE.

Expected input timestamp format somewhere in the filename:
    YYYYDOYHHMMSS

The output filename preserves the original filename structure and replaces
only the detected 13-digit timestamp with the new binned local timestamp:
    YYYYMMDDHHMM

Examples this script can read and rename:
    ECO_L2T_LSTE.002_LST_doy2018219184952_aid0001_18N.tif
        -> ECO_L2T_LSTE.002_LST_doy201808071500_aid0001_18N.tif

    2018219184952_filtered.tif
        -> 201808071500_filtered.tif

Modes:
    append
        Copies renamed TIFFs into ./Binned/.
        Original TIFFs in the working folder are untouched.

    overwrite
        Renames the original TIFFs in the working folder.
        No ./Binned/ output is created for this mode.

Notes:
    - Raster values are not changed. This script only copies/renames files.
    - The timestamp detection is filename-flexible: it looks for a valid
      standalone string of 13 digits in YYYYDOYHHMMSS format.
    - Name collisions are protected with _2, _3, etc.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from shutil import copy2
from zoneinfo import ZoneInfo


# -----------------------------------------------------------------------------
# CONFIGURATION
# -----------------------------------------------------------------------------

# Choose one:
#   "append"    -> copy renamed TIFFs to ./Binned/ and keep originals
#   "overwrite" -> rename original TIFFs in the working folder
OUTPUT_MODE = "overwrite"

# Read TIFFs from the same folder where this script lives.
WORKING_FOLDER = Path(__file__).resolve().parent

# Append mode output folder.
OUTPUT_FOLDER = WORKING_FOLDER / "Binned"

# Convert UTC to the local timezone for your study area (example: US/Eastern)
LOCAL_TIMEZONE = ZoneInfo("<<< REPLACE_THIS_TEXT_WITH_TIMEZONE >>>")

# Input timestamps in ECOSTRESS filenames are treated as UTC.
UTC = timezone.utc

# First preference: timestamps directly after "doy".
DOY_TIMESTAMP_PATTERN = re.compile(r"doy(\d{13})", re.IGNORECASE)

# Fallback: any standalone 13-digit timestamp in the filename.
ANY_TIMESTAMP_PATTERN = re.compile(r"(?<!\d)(\d{13})(?!\d)")

# Valid raster extensions to process.
TIFF_EXTENSIONS = {".tif", ".tiff"}


# -----------------------------------------------------------------------------
# DATA STRUCTURES
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class BinnedFilenameInfo:
    """Information needed to copy or rename one binned TIFF."""

    original_timestamp: str
    binned_timestamp: str
    output_filename: str
    dt_utc: datetime
    dt_local: datetime
    dt_binned_local: datetime


# -----------------------------------------------------------------------------
# HELPERS
# -----------------------------------------------------------------------------

def parse_ecostress_timestamp(timestamp_str: str) -> datetime | None:
    """Parse YYYYDOYHHMMSS as a timezone-aware UTC datetime."""
    try:
        dt_utc = datetime.strptime(timestamp_str, "%Y%j%H%M%S")
        return dt_utc.replace(tzinfo=UTC)
    except ValueError:
        return None


def iter_timestamp_candidates(filename: str):
    """
    Yield possible timestamp strings and their exact spans in the filename.

    The script first checks for timestamps immediately after "doy" because
    that is the common ECOSTRESS style. It then falls back to any standalone
    13-digit string, which allows names like 2018219184952_filtered.tif.
    """
    seen_spans: set[tuple[int, int]] = set()

    for match in DOY_TIMESTAMP_PATTERN.finditer(filename):
        span = match.span(1)
        if span not in seen_spans:
            seen_spans.add(span)
            yield match.group(1), span

    for match in ANY_TIMESTAMP_PATTERN.finditer(filename):
        span = match.span(1)
        if span not in seen_spans:
            seen_spans.add(span)
            yield match.group(1), span


def find_valid_timestamp_in_filename(filename: str) -> tuple[str, tuple[int, int], datetime] | None:
    """
    Return the first valid YYYYDOYHHMMSS timestamp found in the filename.

    Returns:
        (timestamp_string, timestamp_span, datetime_utc)

    Returns None when the filename does not contain a valid timestamp.
    """
    for timestamp_str, span in iter_timestamp_candidates(filename):
        dt_utc = parse_ecostress_timestamp(timestamp_str)
        if dt_utc is not None:
            return timestamp_str, span, dt_utc

    return None


def bin_to_nearest_hour(dt_local: datetime) -> datetime:
    """Round a local datetime to the nearest hour."""
    if dt_local.minute < 30:
        return dt_local.replace(minute=0, second=0, microsecond=0)

    return (dt_local + timedelta(hours=1)).replace(
        minute=0,
        second=0,
        microsecond=0,
    )


def build_binned_output_filename(src_name: str) -> BinnedFilenameInfo | None:
    """
    Replace only the detected 13-digit timestamp with the binned local timestamp.

    Example:
        ECO_L2T_LSTE.002_LST_doy2018219184952_aid0001_18N_1.tif
        becomes
        ECO_L2T_LSTE.002_LST_doy201808071500_aid0001_18N_1.tif
    """
    found = find_valid_timestamp_in_filename(src_name)
    if found is None:
        return None

    original_timestamp, span, dt_utc = found
    dt_local = dt_utc.astimezone(LOCAL_TIMEZONE)
    dt_binned = bin_to_nearest_hour(dt_local)
    binned_timestamp = dt_binned.strftime("%Y%m%d%H%M")

    start, end = span
    output_filename = f"{src_name[:start]}{binned_timestamp}{src_name[end:]}"

    return BinnedFilenameInfo(
        original_timestamp=original_timestamp,
        binned_timestamp=binned_timestamp,
        output_filename=output_filename,
        dt_utc=dt_utc,
        dt_local=dt_local,
        dt_binned_local=dt_binned,
    )


def get_unique_path(folder: Path, filename: str, source_path: Path | None = None) -> Path:
    """
    Return a path that will not overwrite an existing file.

    If source_path is supplied and the target path is the same file, the source
    path is returned unchanged.

    Example collision handling:
        ECO_L2T_LSTE.002_LST_doy201808071500_aid0001_18N.tif
        ECO_L2T_LSTE.002_LST_doy201808071500_aid0001_18N_2.tif
        ECO_L2T_LSTE.002_LST_doy201808071500_aid0001_18N_3.tif
    """
    candidate = folder / filename

    if source_path is not None:
        try:
            if candidate.resolve() == source_path.resolve():
                return candidate
        except FileNotFoundError:
            # Source may have been renamed in overwrite mode. Fall through.
            pass

    if not candidate.exists():
        return candidate

    path_obj = Path(filename)
    stem = path_obj.stem
    suffix = path_obj.suffix

    counter = 2
    while True:
        candidate = folder / f"{stem}_{counter}{suffix}"

        if source_path is not None:
            try:
                if candidate.resolve() == source_path.resolve():
                    return candidate
            except FileNotFoundError:
                pass

        if not candidate.exists():
            return candidate

        counter += 1


def validate_output_mode() -> str:
    """Validate and normalize OUTPUT_MODE."""
    mode = OUTPUT_MODE.strip().lower()
    valid_modes = {"append", "overwrite"}

    if mode not in valid_modes:
        raise ValueError(
            f'Invalid OUTPUT_MODE: "{OUTPUT_MODE}". '
            'Use either "append" or "overwrite".'
        )

    return mode


def format_time_for_log(dt: datetime) -> str:
    """Format a timezone-aware datetime for clear terminal output."""
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z")


# -----------------------------------------------------------------------------
# MAIN SCRIPT
# -----------------------------------------------------------------------------

def main() -> None:
    mode = validate_output_mode()

    if mode == "append":
        OUTPUT_FOLDER.mkdir(exist_ok=True)
        destination_folder = OUTPUT_FOLDER
    else:
        destination_folder = WORKING_FOLDER

    processed_count = 0
    skipped_count = 0
    error_count = 0

    print(f"Working folder: {WORKING_FOLDER}")
    print(f"Output mode:    {mode}")
    if mode == "append":
        print(f"Output folder:  {OUTPUT_FOLDER}")
    else:
        print("Output folder:  working folder, in-place rename")
    print(f"Local timezone: {LOCAL_TIMEZONE.key}")
    print("-" * 72)

    # Snapshot the input list before any overwrite-mode renaming happens.
    input_paths = [
        path
        for path in sorted(WORKING_FOLDER.iterdir())
        if path.is_file() and path.suffix.lower() in TIFF_EXTENSIONS
    ]

    for src_path in input_paths:
        info = build_binned_output_filename(src_path.name)
        if info is None:
            print(f"Skipped, no valid YYYYDOYHHMMSS timestamp found: {src_path.name}")
            skipped_count += 1
            continue

        try:
            if mode == "append":
                dst_path = get_unique_path(destination_folder, info.output_filename)
                copy2(src_path, dst_path)
                processed_count += 1
                print(
                    f"Copied:  {src_path.name} -> Binned/{dst_path.name} | "
                    f"UTC {info.original_timestamp} -> local bin {info.binned_timestamp} "
                    f"({format_time_for_log(info.dt_binned_local)})"
                )

            else:
                dst_path = get_unique_path(
                    destination_folder,
                    info.output_filename,
                    source_path=src_path,
                )

                # If the source already has the target name, leave it alone.
                if src_path.resolve() == dst_path.resolve():
                    processed_count += 1
                    print(f"Already named correctly: {src_path.name}")
                    continue

                src_path.rename(dst_path)
                processed_count += 1
                print(
                    f"Renamed: {src_path.name} -> {dst_path.name} | "
                    f"UTC {info.original_timestamp} -> local bin {info.binned_timestamp} "
                    f"({format_time_for_log(info.dt_binned_local)})"
                )

        except Exception as exc:
            print(f"Error processing {src_path.name}: {exc}")
            error_count += 1

    print("-" * 72)
    print(f"Done. Processed: {processed_count} | Skipped: {skipped_count} | Errors: {error_count}")


if __name__ == "__main__":
    main()
