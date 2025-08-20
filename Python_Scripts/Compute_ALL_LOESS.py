#!/usr/bin/env python3
"""
Compute LOESS Baseline & Outlier Removal for ECOSTRESS Scenes

This script computes a LOESS-smoothed baseline temperature series from 
ECOSTRESS LST `_filtered_masked.tif` scenes and removes outliers 
relative to that baseline. Both anomalously cold and hot pixels 
are masked and new filtered TIFFs are written.

Inputs:
    • Folder containing ECOSTRESS LST files ending with "_filtered_masked.tif"
      (filenames must include a timestamp of the form YYYYDDDHHMMSS).
Outputs:
    • A subfolder containing new TIFFs where hot and cold outliers have been masked.
"""
# Import the Libraries Needed for Outlier Removal
import os
import re
import random
import datetime
import numpy as np
import rasterio
from rasterio.windows import from_bounds, Window, transform as window_transform
from statsmodels.nonparametric.smoothers_lowess import lowess

def main():
    # Folder where your ECOSTRESS LST Files are Stored
    INPUT_FOLDER = "<<< REPLACE_THIS_TEXT_WITH_FOLDER_PATH >>>"

    # Temperature thresholds for defining outliers relative to baeline
    threshold_cold = 1.5  # degrees below baseline, adjust for your data
    threshold_hot  = 3.5  # degrees above baseline, adjust for your data

    # 1. Gather all ECOSTRESS “_filtered.tif” files & parse timestamp (yyyydddHHMMSS)
    filtered_list = []
    for fname in os.listdir(INPUT_FOLDER):
        if fname.lower().endswith('_filtered_masked.tif'):
            match = re.search(r'(\d{13})_filtered_masked\.tif$', fname)
            if match:
                date_str = match.group(1)
                try:
                    # Parse YYYYDDDhhmmss using %Y%j%H%M%S - datetime.date
                    d_val = datetime.datetime.strptime(date_str, '%Y%j%H%M%S').date()
                except ValueError:
                    d_val = None
                if d_val:
                    filtered_list.append((fname, d_val))

    if not filtered_list:
        print("No valid '_filtered.tif' files with timestamps found.")
        return

    # Sort by acquisition date
    filtered_list.sort(key=lambda x: x[1])
    tif_paths = [os.path.join(INPUT_FOLDER, x[0]) for x in filtered_list]
    dates = [x[1] for x in filtered_list]
    n_scenes = len(tif_paths)
    print(f"Found {n_scenes} filtered scenes with valid timestamps.")

    # 2. Compute overlapping spatial intersection across all input TIFFs
    datasets = [rasterio.open(tp) for tp in tif_paths]
    ref_ds = datasets[0]
    transform_ref = ref_ds.transform
    crs_ref = ref_ds.crs
    width_full = ref_ds.width
    height_full = ref_ds.height

    # Start with reference bounds, shrink to intersection
    left, bottom, right, top = ref_ds.bounds
    for ds in datasets[1:]:
        l, b, r, t = ds.bounds
        left   = max(left,   l)
        bottom = max(bottom, b)
        right  = min(right,  r)
        top    = min(top,    t)

    # Validate overlap
    if left >= right or bottom >= top:
        print("No overlapping area found among TIFs.")
        for ds in datasets:
            ds.close()
        return

    # Compute raster window covering the overlap
    intersection_window = from_bounds(left, bottom, right, top, transform=transform_ref)
    intersection_window = intersection_window.round_offsets(op='floor').round_shape(op='floor')

    if intersection_window.height <= 0 or intersection_window.width <= 0:
        print("Intersection window has non-positive dimensions.")
        for ds in datasets:
            ds.close()
        return

    # Record offsets and dimensions of intersection
    intersection_transform = window_transform(intersection_window, transform_ref)
    row_off = int(intersection_window.row_off)
    col_off = int(intersection_window.col_off)
    height_window = int(intersection_window.height)
    width_window  = int(intersection_window.width)

    print(f"Intersection → row_off={row_off}, col_off={col_off}, "
          f"height={height_window}, width={width_window}")

    # 3. Randomly sample candidate pixels inside intersection and build their time series across
    # all scenes. We will select pixels that have at least ‘depth_required’ valid values.
    n_desired = 40
    depth_required = 10
    n_candidates_initial = 2000  # size of pool for sampling pixels (increase to improve chance of finding depth)
    rng = random.Random(41)  # reproducibility (change to get new set of randomized pixels)

    # Pick candidate pixel coordinates
    candidate_pixels = set()
    while len(candidate_pixels) < n_candidates_initial:
        rr = rng.randint(0, height_window - 1)
        cc = rng.randint(0, width_window - 1)
        candidate_pixels.add((rr, cc))
    candidate_pixels = list(candidate_pixels)

    candidate_array = np.zeros((n_scenes, len(candidate_pixels)), dtype=np.float32)

    # Extract time series for each candidate pixel
    for i, ds in enumerate(datasets):
        data_window = ds.read(
            1,
            window=Window(col_off, row_off, width_window, height_window),
            boundless=True,
            fill_value=np.nan
        )
        nodata = ds.nodata
        if nodata is not None:
            data_window = np.where(data_window == nodata, np.nan, data_window)
        else:
            data_window = data_window.astype(np.float32)

        for j, (r, c) in enumerate(candidate_pixels):
            candidate_array[i, j] = data_window[r, c]

    for ds in datasets:
        ds.close()

    # 4. Keep only pixels with enough valid (non-NaN) values
    valid_counts = np.sum(np.isfinite(candidate_array), axis=0)
    valid_indices = np.where(valid_counts >= depth_required)[0]
    print(f"Found {len(valid_indices)} candidate pixels with ≥ {depth_required} valid values.")

    if len(valid_indices) < n_desired:
        print(f"Not enough pixels with at least {depth_required} valid values. "
              f"Consider increasing n_candidates_initial.")
        return

    # Build time axis (days since first date)
    start_date = min(dates)
    x_data_full = np.array([(d - start_date).days for d in dates], dtype=float)

    # 5. Compute LOESS baseline for valid pixels and select those with lowest average baselines
    frac_val = 0.15 # smoothing fraction
    it_val = 10 # number of iterations

    valid_baselines = []  # tuples: (idx, baseline_full, avg_baseline)
    for idx in valid_indices:
        ts_j = candidate_array[:, idx]
        finite_mask = np.isfinite(ts_j)
        x_valid = x_data_full[finite_mask]
        y_valid = ts_j[finite_mask]

        # LOESS smoothing
        loess_out = lowess(y_valid, x_valid, frac=frac_val, it=it_val, return_sorted=True)
        baseline_full = np.interp(x_data_full, loess_out[:, 0], loess_out[:, 1])
        avg_baseline = np.mean(baseline_full)
        valid_baselines.append((idx, baseline_full, avg_baseline))

    # Sort by average baseline and take n_desired lowest
    valid_baselines.sort(key=lambda x: x[2])
    top_pixels = valid_baselines[:n_desired]
    print(f"Selected {n_desired} pixels with lowest average LOESS baselines.")

    # Stack and average to form final baseline
    baseline_stack = np.array([tb[1] for tb in top_pixels], dtype=np.float32)
    final_baseline = np.nanmean(baseline_stack, axis=0)

    # 6. Apply baseline to filter hot/cold outliers and write new TIFFs with outliers masked
    out_dir = os.path.join(INPUT_FOLDER, "<<< REPLACE_THIS_TEXT_WITH_OUTPUT_FILENAME.csv >>>")
    os.makedirs(out_dir, exist_ok=True)

    for i, (fname, date_val) in enumerate(filtered_list):
        src_path = os.path.join(INPUT_FOLDER, fname)
        with rasterio.open(src_path) as src:
            data_full = src.read(1).astype(np.float32)
            nodata = src.nodata
            if nodata is not None:
                data_full = np.where(data_full == nodata, np.nan, data_full)

            data_window = src.read(
                1,
                window=Window(col_off, row_off, width_window, height_window),
                boundless=True,
                fill_value=np.nan
            )
            if nodata is not None:
                data_window = np.where(data_window == nodata, np.nan, data_window)

            base_val = final_baseline[i]
            outlier_mask_window = (
                ((data_window < (base_val - threshold_cold)) |
                 (data_window > (base_val + threshold_hot))) &
                np.isfinite(data_window)
            )

            # Mask outliers in full array
            modified_full = data_full.copy()
            window_slice = modified_full[
                row_off:row_off + height_window,
                col_off:col_off + width_window
            ]
            window_slice[outlier_mask_window] = np.nan
            modified_full[
                row_off:row_off + height_window,
                col_off:col_off + width_window
            ] = window_slice

            # Update metadata
            profile = src.profile.copy()
            profile.update({
                'driver': 'GTiff',
                'dtype': 'float32',
                'count': 1,
                'compress': 'lzw',
                'nodata': np.nan
            })

        out_name = f"{os.path.splitext(fname)[0]}_no_outliers.tif"
        out_path = os.path.join(out_dir, out_name)
        with rasterio.open(out_path, 'w', **profile) as dst:
            dst.write(modified_full, 1)

        print(f"Wrote filtered TIFF: {out_path}")

    print("\nDone! All scenes written with hot/cold outliers removed.")

if __name__ == "__main__":
    main()
