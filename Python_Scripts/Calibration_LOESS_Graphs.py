#!/usr/bin/env python3
"""
Calibration LOESS Graphs

This script plots time series for 5 randomly-selected pixels from ECOSTRESS *_filtered_masked.tif
scenes. Each pixel’s time series is LOESS-smoothed to establish a baseline, with outliers flagged
using two methods:  
  1. Against its individual LOESS baseline  
  2. Against the average baseline across all five pixels  

The script produces 10 subplots (2 per pixel: individual baseline and average baseline) 
with consistent y-axis limits for direct comparison.

Inputs:
- A folder containing multiple ECOSTRESS LST files ending with *_filtered_masked.tif
- Each filename must include a 13-digit timestamp (e.g., 2020205000000_filtered_masked.tif)

Outputs:
- A matplotlib figure with 10 subplots:
    Left column: LOESS fit + outliers for each individual pixel  
    Right column: LOESS fit + outliers against the average baseline
- Console printout with the number of discovered scenes
"""
# Import the Libraries Needed for LOESS Calibration
import os
import re
import random
import datetime
import numpy as np
import rasterio
from rasterio.windows import from_bounds, Window
from statsmodels.nonparametric.smoothers_lowess import lowess
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

def main():
    # Folder where your ECOSTRESS LST files are stored
    INPUT_FOLDER = "<<< REPLACE_THIS_TEXT_WITH_FOLDER_PATH >>>"
    
    # PARAMETERS
    n_desired            = 40     # (not used for plotting, only pixel selection target)
    depth_required       = 10     # minimum # of valid values per pixel
    n_candidates_initial = 2000   # how many random pixels to sample initially

    frac_val       = 0.15  # LOESS fraction
    it_val         = 10    # LOESS robustness iterations
    threshold_cold = 1.75   # outlier if below baseline - threshold_cold, adjust for your data
    threshold_hot  = 3.75   # outlier if above baseline + threshold_hot, adjust for your data

    # Global y-limits in °C
    y_min, y_max = 6.85, 26.85  # Adjust for your temperature limits

    # 1. DISCOVER ALL *_filtered.tif AND EXTRACT DATES
    filtered = []
    for fname in os.listdir(INPUT_FOLDER):
        if fname.lower().endswith('_filtered_masked.tif'):
            m = re.search(r'(\d{13})_filtered_masked\.tif$', fname)
            if m:
                stamp = m.group(1)
                try:
                    dt = datetime.datetime.strptime(stamp, '%Y%j%H%M%S').date()
                except ValueError:
                    continue
                filtered.append((fname, dt))

    if not filtered:
        print("No *_filtered.tif files with valid timestamps found; exiting.")
        return

    filtered.sort(key=lambda x: x[1])
    tif_paths = [os.path.join(INPUT_FOLDER, fn) for fn, _ in filtered]
    dates     = [dt for _, dt in filtered]
    n_scenes  = len(tif_paths)
    print(f"Found {n_scenes} scenes.")

    # 2. COMPUTE INTERSECTION WINDOW COMMON TO ALL SCENES
    datasets = [rasterio.open(p) for p in tif_paths]
    ref = datasets[0]
    left, bottom, right, top = ref.bounds
    for ds in datasets[1:]:
        l, b, r, t = ds.bounds
        left, bottom = max(left, l), max(bottom, b)
        right, top   = min(right, r), min(top, t)

    if left >= right or bottom >= top:
        print("No overlapping area across scenes; exiting.")
        for ds in datasets: ds.close()
        return

    win = from_bounds(left, bottom, right, top, ref.transform)
    win = win.round_offsets(op='floor').round_shape(op='floor')
    row_off, col_off = int(win.row_off), int(win.col_off)
    height, width    = int(win.height), int(win.width)

    # 3. SAMPLE CANDIDATE PIXELS ACROSS INTERSECTION WINDOW
    rng = random.Random(40)
    candidates = {(rng.randint(0, height - 1), rng.randint(0, width - 1))
                  for _ in range(n_candidates_initial)}
    candidate_pixels = list(candidates)

    # Read all candidate pixel values into [scene, pixel] array
    vals = np.full((n_scenes, len(candidate_pixels)), np.nan, dtype=np.float32)
    for i, ds in enumerate(datasets):
        arr = ds.read(1,
                      window=Window(col_off, row_off, width, height),
                      boundless=True, fill_value=np.nan)
        if ds.nodata is not None:
            arr[arr == ds.nodata] = np.nan
        for j, (r, c) in enumerate(candidate_pixels):
            vals[i, j] = arr[r, c]
    for ds in datasets: ds.close()

    # 4. KEEP PIXELS WITH ≥ depth_required FINITE VALUES
    good_idx = np.where(np.sum(np.isfinite(vals), axis=0) >= depth_required)[0]
    if len(good_idx) < 5:
        print("Fewer than five suitable pixels; exiting.")
        return
    plot_idx = good_idx[:5]

    # 5. PERFORM LOESS SMOOTHING AND IDENTIFY OUTLIERS
    start = min(dates)
    x_full = np.array([(d - start).days for d in dates], dtype=float)

    series       = []
    baselines    = []
    masks_ind    = []

    for idx in plot_idx:
        y = vals[:, idx]
        mask_finite = np.isfinite(y)

        # individual LOESS baseline
        lo = lowess(y[mask_finite], x_full[mask_finite],
                    frac=frac_val, it=it_val, return_sorted=True)
        baseline = np.interp(x_full, lo[:, 0], lo[:, 1])

        # outliers relative to individual baseline
        out_ind = (((y < (baseline - threshold_cold)) |
                    (y > (baseline + threshold_hot))) &
                   mask_finite)

        series.append(y)
        baselines.append(baseline)
        masks_ind.append(out_ind)

    baselines    = np.array(baselines)
    avg_baseline = np.nanmean(baselines, axis=0)

    # compute outliers relative to average baseline
    masks_avg = []
    for y in series:
        masks_avg.append(((y < (avg_baseline - threshold_cold)) |
                          (y > (avg_baseline + threshold_hot))) &
                         np.isfinite(y))

    # 6. PLOT RESULTS FOR 5 PIXELS (10 SUBPLOTS TOTAL)
    fig, axes = plt.subplots(5, 2, figsize=(12, 15), sharex='all')

    for row, idx in enumerate(plot_idx):
        y         = series[row] - 273.15  # Kelvin → °C
        b_ind     = baselines[row] - 273.15
        m_ind     = masks_ind[row]
        m_avg     = masks_avg[row]
        r, c      = candidate_pixels[idx]
        ax_l, ax_r = axes[row]

        # left: individual baseline + its outliers
        ax_l.plot(dates, y, 'o', ms=4, lw=1, alpha=0.7)
        ax_l.plot(dates, b_ind, '-',  lw=1, color='black')
        '''if m_ind.any():
            ax_l.scatter(np.array(dates)[m_ind], y[m_ind],
                         facecolors='none', edgecolors='red', s=80)'''
        ax_l.set_ylabel('Temp (°C)')
        ax_l.set_title(f'Pixel (r={r}, c={c})', fontsize=9)

        # right: average baseline + its outliers
        ax_r.plot(dates, y, 'o', ms=4, lw=1, alpha=0.7)
        ax_r.plot(dates, avg_baseline - 273.15, '--', lw=2, color='black')
        '''if m_avg.any():
            ax_r.scatter(np.array(dates)[m_avg], y[m_avg],
                         facecolors='none', edgecolors='red', s=80)'''
        ax_r.set_ylabel('Temp (°C)')
        ax_r.set_title(f'Pixel (r={r}, c={c}) — avg', fontsize=9)

    # Format x-axis ticks every 3 months
    for ax in fig.axes:
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
        ax.set_ylim(y_min, y_max)

    fig.autofmt_xdate()

    plt.tight_layout()
    plt.show()
    print("Done.")


if __name__ == '__main__':
    main()
