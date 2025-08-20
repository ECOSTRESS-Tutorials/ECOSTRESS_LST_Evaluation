#!/usr/bin/env python3
"""
ECOSTRESS LST Quality Control (QC) Filtering Script

This script processes ECOSTRESS LST and QC TIFF files, applies quality filtering
based on QC flags, masks data outside a given Area of Interest (AOI), and outputs
cleaned scenes plus summary maps (mean, variance, count).

How to use:
- Update all placeholder text marked as <<< REPLACE_THIS_TEXT >>> with your settings.
- Make sure you have ECOSTRESS LST and QC GeoTIFFS in your input folder as when as a shapefile defining your AOI (in ESPG:4326)
"""

# Import the Libraries Needed for QC Filtering
import os
import glob
import re
import numpy as np
import rasterio
from rasterio.warp import calculate_default_transform, reproject, Resampling
from rasterio.features import geometry_mask
import geopandas as gpd
from affine import Affine
from scipy.ndimage import binary_dilation
from collections import defaultdict

# 1. CONFIGURATION
# Path to folder containing ECOSTRESS LST/QC files
INPUT_FOLDER = '<<< REPLACE_THIS_TEXT_WITH_FOLDER_PATH >>>'

# Shapefile defining your AOI (Area of Interest)
AOI_PATH = os.path.join(INPUT_FOLDER, '<<< REPLACE_THIS_TEXT_WITH_SHAPEFILE_NAME.shp >>>')

# Output folder for cleaned files
OUTPUT_FOLDER = os.path.join(INPUT_FOLDER, '<<< REPLACE_THIS_TEXT_WITH_FOLDER_NAME >>>')

# Pixel neighborhood dilation for QC mask (0 = no dilation)
NEIGHBORHOOD = 0 # e.g., 1 expands mask to include adjacent pixels

# Fill value for nodata cells
FILL_VALUE = -9999.0

# Target coordinate reference system (CRS) for output
TARGET_CRS = 'EPSG:4326'

# Temperature thresholds in Kelvin
TEMP_MIN = <<< REPLACE_WITH_YOUR_TEMP_MIN >>>
TEMP_MAX = <<< REPLACE_WITH_YOUR_TEMP_MAX >>>

# 2. HELPER FUNCTIONS
def extract_timestamp_ecostress(filename):
    """Extracts the 13-digit ECOSTRESS timestamp from a filename (YYYYDOYhhmmss)"""
    match = re.search(r'doy(\d{13})', filename)
    return match.group(1) if match else None

def apply_qc_mask(qc_array):
    """ Returns a boolean mask where True = GOOD pixels, False = BAD pixels.
    Bits 14-15 of the ECOSTRESS QC layer define quality:
    0 = best quality, non-zero = lower quality. """
    return ((qc_array.astype(int) >> 14) & 0b11) != 0

# 3. MAIN PROCESSING
def main():
    os.makedirs(OUTPUT_FOLDER, exist_ok=True)

    # Load AOI shapefile
    if not os.path.exists(AOI_PATH):
        print(f"[ERROR] AOI shapefile not found: {AOI_PATH}")
        return
    gdf = gpd.read_file(AOI_PATH).to_crs(epsg=4326)
    geom = [feature["geometry"] for feature in gdf.__geo_interface__['features']]
    minx, miny, maxx, maxy = gdf.total_bounds

    # Find all LST and QC files in input folder
    all_files = glob.glob(os.path.join(INPUT_FOLDER, "*.tif"))
    lst_files_by_ts = defaultdict(list)
    qc_files_by_ts = defaultdict(list)

    for f in all_files:
        ts = extract_timestamp_ecostress(f)
        if ts:
            if 'LST_doy' in f:
                lst_files_by_ts[ts].append(f)
            elif 'QC_doy' in f:
                qc_files_by_ts[ts].append(f)

    # Match LST/QC pairs
    matched_pairs = []
    for ts in sorted(lst_files_by_ts.keys()):
        lst_group = sorted(lst_files_by_ts[ts])
        qc_group = sorted(qc_files_by_ts.get(ts, []))
        for lst_file in lst_group:
            tile_match = re.search(r'_(\d{2}N)', lst_file)
            tile_str = tile_match.group(1) if tile_match else ''
            qc_match = next((q for q in qc_group if tile_str in q), None)
            if qc_match:
                matched_pairs.append((ts, lst_file, qc_match))

    if not matched_pairs:
        print("[ERROR] No matching LST and QC file pairs found.")
        return

    data_list = []
    target_transform = target_width = target_height = pixel_width = pixel_height = None

    # Process each matched LST/QC pair
    for idx, (timestamp, lst_path, qc_path) in enumerate(matched_pairs, 1):
        print(f"[{idx}/{len(matched_pairs)}] Processing {os.path.basename(lst_path)}...")

        with rasterio.open(lst_path) as lst_src, rasterio.open(qc_path) as qc_src:
            if idx == 1:
                transform_full, width_full, height_full = calculate_default_transform(
                    lst_src.crs, TARGET_CRS, lst_src.width, lst_src.height, *lst_src.bounds
                )
                pixel_width = transform_full.a
                pixel_height = -transform_full.e
                target_transform = Affine(pixel_width, 0, minx, 0, -pixel_height, maxy)
                target_width = int(np.ceil((maxx - minx) / pixel_width))
                target_height = int(np.ceil((maxy - miny) / pixel_height))

            # Read LST and QC arrays
            lst = lst_src.read(1).astype('float32')
            qc = qc_src.read(1)

            if lst_src.nodata is not None:
                lst[lst == lst_src.nodata] = np.nan

            qc = np.where((qc == 0) | (qc == qc_src.nodata), FILL_VALUE, qc)

            # Reproject LST
            lst_reproj = np.full((target_height, target_width), FILL_VALUE, dtype='float32')
            reproject(
                source=lst,
                destination=lst_reproj,
                src_transform=lst_src.transform,
                src_crs=lst_src.crs,
                dst_transform=target_transform,
                dst_crs=TARGET_CRS,
                resampling=Resampling.nearest,
                src_nodata=FILL_VALUE,
                dst_nodata=FILL_VALUE
            )
            lst_reproj = np.where(lst_reproj == FILL_VALUE, np.nan, lst_reproj)

            # Reproject QC
            qc_reproj = np.full((target_height, target_width), FILL_VALUE, dtype='int16')
            reproject(
                source=qc,
                destination=qc_reproj,
                src_transform=qc_src.transform,
                src_crs=qc_src.crs,
                dst_transform=target_transform,
                dst_crs=TARGET_CRS,
                resampling=Resampling.nearest
            )

            # Apply QC mask and AOI mask
            qc_mask = apply_qc_mask(qc_reproj)
            aoi_mask = geometry_mask(geom, out_shape=(target_height, target_width),
                                     transform=target_transform, invert=False)
            combined_mask = qc_mask & ~aoi_mask & ~np.isnan(lst_reproj)

            # Optional neighborhood dilation
            if NEIGHBORHOOD > 0:
                struct = np.ones((2*NEIGHBORHOOD+1, 2*NEIGHBORHOOD+1), dtype=bool)
                invalid = binary_dilation(~combined_mask, structure=struct)
                final_mask = ~invalid
            else:
                final_mask = combined_mask

            # Apply masks and temperature limits
            lst_clean = np.where(final_mask, lst_reproj, np.nan)
            lst_clean[(lst_clean < TEMP_MIN) | (lst_clean > TEMP_MAX)] = np.nan

            # Skip low-coverage scenes
            valid_fraction = np.sum(~np.isnan(lst_clean)) / lst_clean.size
            if valid_fraction < 0.05:
                print(f"→ Skipped {timestamp}: only {valid_fraction:.2%} valid data")
                continue

            data_list.append(lst_clean)

            # Save cleaned scene
            out_path = os.path.join(OUTPUT_FOLDER, f"{timestamp}_filtered.tif")
            out_meta = lst_src.meta.copy()
            out_meta.update({
                'driver': 'GTiff',
                'height': target_height,
                'width': target_width,
                'transform': target_transform,
                'crs': TARGET_CRS,
                'dtype': 'float32',
                'nodata': FILL_VALUE
            })

            with rasterio.open(out_path, 'w', **out_meta) as dst:
                dst.write(np.where(np.isnan(lst_clean), FILL_VALUE, lst_clean), 1)
            print(f"→ Saved filtered file: {out_path}")

    if not data_list:
        print("No valid data to process after QC.")
        return

    # Summary maps
    stack = np.stack(data_list, axis=0)
    mean_arr = np.nanmean(stack, axis=0).astype('float32')
    var_arr  = np.nanvar(stack, axis=0).astype('float32')
    count_map = np.sum(~np.isnan(stack), axis=0).astype('int32')

    meta = {
        'driver': 'GTiff',
        'dtype': 'float32',
        'nodata': FILL_VALUE,
        'width': target_width,
        'height': target_height,
        'count': 1,
        'crs': TARGET_CRS,
        'transform': target_transform
    }

    for arr, name in [(mean_arr, 'mean_map.tif'), (var_arr, 'variance_map.tif')]:
        path = os.path.join(OUTPUT_FOLDER, name)
        with rasterio.open(path, 'w', **meta) as dst:
            dst.write(np.where(np.isnan(arr), FILL_VALUE, arr), 1)
        print(f"→ Saved {name[:-4]}: {path}")

    count_meta = meta.copy()
    count_meta.update(dtype='int32', nodata=None)
    count_path = os.path.join(OUTPUT_FOLDER, 'count_map.tif')
    with rasterio.open(count_path, 'w', **count_meta) as dst:
        dst.write(count_map, 1)
    print(f"→ Saved count map: {count_path}")

    print(f"[DONE] All outputs written to {OUTPUT_FOLDER}/")

if __name__ == '__main__':
    main()

