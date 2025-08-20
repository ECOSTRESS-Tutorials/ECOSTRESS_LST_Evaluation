#!/usr/bin/env python3

"""
ECOSTRESS LST Timestamp Binning

This script reads ECOSTRESS LST TIFF filenames, extracts the timestamp (in format YYYYDOYhhmmss),
converts to local time, bins to 1 hour, and renames files with new timestamp.

Inputs:
- ECOSTRESS LST TIFF files (e.g., 2020009105259_filtered_masked.tif, ECO_L2T_LSTE.002_LST_doy2020004131603_aid0001_18N.tif)
Outputs:
- Renamed TIFF files with local timestamps saved to output_folder
"""

# Import the Libraries Needed for Binning
import os
import re
from datetime import datetime, timedelta
from shutil import copy2
import pytz
from collections import defaultdict

# CONFIGURATION
# Folder containing ECOSTRESS TIFF files
input_folder = "<<< REPLACE_THIS_TEXT_WITH_INPUT_FOLDER_PATH >>>"

# Folder to save renamed (binned) files within input_folder
output_folder = os.path.join(input_folder, "<<< REPLACE_THIS_TEXT_WITH_OUTPUT_FOLDER_NAME >>>")

# Create output folder
os.makedirs(output_folder, exist_ok=True)

# Convert UTC to the local timezone for your study area (example: US/Eastern)
utc = pytz.utc
local_tz = pytz.timezone("<<< REPLACE_THIS_TEXT_WITH_TIMEZONE >>>")

# DEFINE FILENAME PATTERN
# This pattern captures a 13-digit YYYYDOYhhmmss timestamp either at start or after 'doy', followed by suffix patterns.
pattern = re.compile(
    r"(?:^|doy)(\d{13})(?=_filtered_masked(?:_no_outliers)?\.tif|_|\.tif|$)"
)

# Track output counts to avoid overwriting files
filename_counts = defaultdict(int)

# LOOP OVER ALL FILES IN INPUT FOLDER
for filename in os.listdir(input_folder):
    # Search for the timestamp pattern in the filename
    match = pattern.search(filename)
    if match:
        timestamp_str = match.group(1)
        try:
            # Parse the UTC datetime from YYYYDOYhhmmss string
            dt_utc = datetime.strptime(timestamp_str, "%Y%j%H%M%S")
            dt_utc = utc.localize(dt_utc)

            # Convert from UTC to local time
            dt_local = dt_utc.astimezone(local_tz)

            # Round to nearest hour
            if dt_local.minute < 30:
                dt_binned = dt_local.replace(minute=0, second=0)
            else:
                dt_binned = (dt_local + timedelta(hours=1)).replace(minute=0, second=0)

            # Create new local timestamp string (YYYYMMDDhhmm)
            new_timestamp = dt_binned.strftime("%Y%m%d%H%M")

            # Build base filename without collision
            base_filename = f"{new_timestamp}_filtered_masked"
            filename_counts[base_filename] += 1
            suffix = f"_{filename_counts[base_filename]}" if filename_counts[base_filename] > 1 else ""
            final_filename = f"{base_filename}{suffix}.tif"

            # Copy and rename the file into the output folder
            src_path = os.path.join(input_folder, filename)
            dst_path = os.path.join(output_folder, final_filename)
            copy2(src_path, dst_path)

            print(f"Renamed: {filename} -> {final_filename}")

        except Exception as e:
            print(f"Error processing {filename}: {e}")
    else:
        print(f"No match for filename: {filename}")
