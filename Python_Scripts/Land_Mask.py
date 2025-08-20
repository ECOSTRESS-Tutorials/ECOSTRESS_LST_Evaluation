#!/usr/bin/env python3

"""
ECOSTRESS LST Labd Masking

This script applies a land mask to filtered ECOSTRESS LST TIFF files, setting land pixels
to a fill value.

Inputs:
- Filtered LST TIFF files (ending with "_filtered.tif")
- Land shapefile (AOI) defining land areas
Outputs:
- _filtered_masked.tif (land masked LST files saves to OUTPUT_FOLDER)
"""

# Import the Libraries Needed for Masking
import os
import glob
import re
import numpy as np
import rasterio
from rasterio.mask import mask
import geopandas as gpd
from collections import defaultdict

# CONFIGURATION
# Path to the folder containing your filtered LST files and shapefile
INPUT_FOLDER = "<<< REPLACE_THIS_TEXT_WITH_FOLDER_PATH >>>"

# Land shapefile to use for masking
AOI_SHP = os.path.join(INPUT_FOLDER, '<<< REPLACE_THIS_TEXT_WITH_AOI_NAME.shp >>>')

# Folder to save masked outputs
OUTPUT_FOLDER = os.path.join(INPUT_FOLDER, '<<< REPLACE_THIS_TEXT_WITH_FOLDER_NAME >>>')

# Constants
FILL_VALUE = -9999.0 # Value to fill masked pixels

def extract_timestamp_ecostress(filename):
    """Extract the yyyydddhhmmss pattern used to identify unique scenes."""
    match = re.search(r'doy(\d{13})', filename)
    return match.group(1) if match else None

def main():
    # Create output folder
    os.makedirs(OUTPUT_FOLDER, exist_ok=True)
    # Gather filtered LST TIFF files
    all_files = glob.glob(os.path.join(INPUT_FOLDER, "*.tif"))
    lst_files_by_ts = defaultdict(list)

    for f in all_files:
        fname = os.path.basename(f)
        # Only use filtered LST files (skip already masked ones)
        if fname.endswith("_filtered.tif") and "_masked" not in fname:
            ts = fname.split("_")[0]  # Get the timestamp prefix before the first "_"
            lst_files_by_ts[ts].append(f)

    if not lst_files_by_ts:
        print("No filtered LST files found to apply land mask.")
        return

    # Load land shapefile
    land_gdf = gpd.read_file(AOI_SHP)
    land_shapes = land_gdf.geometry.values

    ts_counter = defaultdict(int)

    #Apply land mask to each LST file
    for idx, ts in enumerate(sorted(lst_files_by_ts.keys()), start=1):
        for lst_path in sorted(lst_files_by_ts[ts]):
            ts_counter[ts] += 1
            label = f"{ts}_{ts_counter[ts]:02d}"
            print(f"[{idx}] Masking land from {label}...")

            with rasterio.open(lst_path) as src:
                # Reproject shapefile to match raster CRS
                if land_gdf.crs != src.crs:
                    land_gdf = land_gdf.to_crs(src.crs)
                    land_shapes = land_gdf.geometry.values

                # Apply land mask (invert=True means mask OUT land)
                out_image, out_transform = mask(
                    dataset=src,
                    shapes=land_shapes,
                    invert=True,
                    crop=False,
                    nodata=FILL_VALUE
                )
                # Update metadata for output file
                out_meta = src.meta.copy()
                out_meta.update({
                    'driver': 'GTiff',
                    'height': out_image.shape[1],
                    'width': out_image.shape[2],
                    'transform': out_transform,
                    'nodata': FILL_VALUE
                })

                # Save masked output
                out_name = os.path.basename(lst_path).replace("_filtered.tif", "_filtered_masked.tif")
                out_path = os.path.join(OUTPUT_FOLDER, out_name)

                with rasterio.open(out_path, 'w', **out_meta) as dst:
                    dst.write(out_image)

                print(f"→ Saved land-masked file: {out_path}")

    print(f"\nAll masked outputs written to: {OUTPUT_FOLDER}/")

if __name__ == '__main__':
    main()
