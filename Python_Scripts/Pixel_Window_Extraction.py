#!/usr/bin/env python3
"""
ECOSTRESS 3x3 Pixel Extraction

This script matches buoy water temperature data with 3x3 pixel windows from ECOSTRESS LST raster files.
For each buoy location and timestamp, it extracts a 3x3 grid of LST pixels centered on the buoy's location,
filters out invalid pixels, and calculates the average LST in Celsius.
The window size can be changed.

Inputs:
- Buoy CSV file containing timestamp, station_id, latitude, longitude, and water temperature (from NDBC retrieval step)
- ECOSTRESS LST TIFF files with timestamps in YYYYMMDDhhmm format
Output:
- CSV file containing matched buoy records with corresponding ECOSTRESS LST values
"""

# Import the Libraries Needed for Pixel Extraction
import os
import pandas as pd
import rasterio
import numpy as np
from rasterio.transform import rowcol
from tqdm import tqdm
import re
from datetime import datetime
import pytz

# CONFIGURATION
# Folder containing both ECOSTRESS LST files and buoy CSV
INPUT_FOLDER = '<<< REPLACE_THIS_TEXT_WITH_FOLDER_PATH >>>'

# Path to buoy CSV file within INPUT_FOLDER
CSV_PATH = os.path.join(INPUT_FOLDER, '<<< REPLACE_THIS_TEXT_WITH_BUOY_CSV_FILENAME.csv >>>')

# Path to output matched CSV file within INPUT_FOLDER
OUTPUT_CSV = os.path.join(INPUT_FOLDER, '<<< REPLACE_THIS_TEXT_WITH_OUTPUT_CSV_FILENAME.csv >>>')

# 1. LOAD BUOY DATA
print("[INFO] Loading buoy data...")
df = pd.read_csv(CSV_PATH)
df['timestamp'] = df['timestamp'].astype(str)
print(f"[INFO] Loaded {len(df)} buoy data rows")

# 2. Index ECOSTRESS LST files by timestamp
print("[INFO] Indexing ECOSTRESS LST files...")
LST_FILES = [f for f in os.listdir(INPUT_FOLDER) if f.endswith('.tif')]

# Create dictionary mapping timestamp to list of filenames
timestamp_to_files = {}
for fname in LST_FILES:
    match = re.match(r'(\d{12})_.*\.tif$', fname) # First 12 characters (YYYYMMDDhhmm)
    if match:
        ts = match.group(1)
        timestamp_to_files.setdefault(ts, []).append(fname)

# 3. MATCH BUOY DATA TO LST PIXELS
print("[INFO] Matching buoy records to 3x3 ECOSTRESS pixels...")
output_rows = []

# Iterate over buoy records
for _, row in tqdm(df.iterrows(), total=len(df)):
    buoy_ts = row['timestamp']
    lat = row['latitude']
    lon = row['longitude']
    buoy_temp = row['water_temperature']
    station = row['station_id']

    # Skip if no matching ECOSTRESS files for this timestamp
    if buoy_ts not in timestamp_to_files:
        continue

    # Process each matching ECOSTRESS file for this timestamp
    for lst_file in timestamp_to_files[buoy_ts]:
        lst_path = os.path.join(folder, lst_file)
        try:
            with rasterio.open(lst_path) as src:
                # Convert buoy lon/lat to raster pixel coordinates
                row_pix, col_pix = rowcol(src.transform, lon, lat)

                # Read a 3x3 pixel window centered on the buoy location
                '''NOTE: The ranges here define the top/bottom and left/right pixel bounds:
                (row_start, row_end), (col_start, col_end)
                For a 3x3 window → subtract 1 and add 2 from the center pixel index.
                Example: (row_pix - 1, row_pix + 2), (col_pix - 1, col_pix + 2)
                
                To use a larger window, adjust these offsets:
                - 5x5 window → (row_pix - 2, row_pix + 3), (col_pix - 2, col_pix + 3)
                - 7x7 window → (row_pix - 3, row_pix + 4), (col_pix - 3, col_pix + 4)
                In general: subtract N//2 and add (N//2 + 1) for an NxN window.'''
                window = src.read(1, window=((row_pix - 1, row_pix + 2), (col_pix - 1, col_pix + 2)))
                window = window.astype(float)

                # Mask out nodata values
                window[window == src.nodata] = np.nan

                # Convert Kelvin to Celsius
                window = window - 273.15

                # Flatten and filter out NaNs
                valid_pixels = window.flatten()
                valid_pixels = valid_pixels[~np.isnan(valid_pixels)]

                # Decide how many pixels need to have data
                ''' For a 3x3 window, there are 9 pixels total, so requiring len(valid_pixels) >= 9
                means **all pixels** must have data.
                
                This requirement is up to the user:
                - You may choose to keep the point even if some pixels are missing.
                - Example: For a 5x5 window (25 pixels total), you might accept the match
                  if at least 20 pixels have valid data.
                
                General rule: Required pixels = your chosen threshold, up to N * N where N is the window size.'''
                if len(valid_pixels) < 9:
                    continue

                # Compute statistics
                mean = np.mean(valid_pixels)
                std = np.std(valid_pixels)
                cv = std / mean if mean != 0 else np.inf

                # Apply a homogeneity test (CV must be ≤ 0.15)
                if cv > 0.15:
                    continue

                # Store matched results
                eco_ts = lst_file[:12] # ECOSTRESS timestamp from filename
                output_rows.append({
                    'buoy_timestamp': buoy_ts,
                    'ecostress_timestamp': eco_ts,
                    'station_id': station,
                    'latitude': lat,
                    'longitude': lon,
                    'buoy_temp': buoy_temp,
                    'ecostress_lst': mean
                })

        except Exception as e:
            print(f"[WARNING] Could not process {lst_file}: {e}")
            continue

# 4. SAVE MATCHED DATA TO CSV
print(f"[INFO] Writing {len(output_rows)} filtered matches to output CSV...")
output_df = pd.DataFrame(output_rows)
output_df.to_csv(OUTPUT_CSV, index=False)
print(f"[DONE] Output saved to: {OUTPUT_CSV}")
