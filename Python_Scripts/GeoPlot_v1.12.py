#!/usr/bin/env python3
import os, sys
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.lines as mlines
from matplotlib.gridspec import GridSpec

def load_geo_csv(path: str) -> pd.DataFrame:
    """
    Supports two schemas:
      1) Old: header row with columns 'datetime','azimuth_deg','distance_m'
      2) New: no guaranteed headers; datetime is 2nd column (index 1) with
         format 'YYYY-MM-DD HH:MM:SS', azimuth_deg is 3rd (index 2),
         distance_m is 4th (index 3).
    """
    # Try headered first
    try:
        df = pd.read_csv(path)
        cols = {c.lower().strip(): c for c in df.columns}
        if {'datetime','azimuth_deg','distance_m'}.issubset(set(cols.keys())):
            # normalize column names
            df = df.rename(columns={
                cols['datetime']: 'datetime',
                cols['azimuth_deg']: 'azimuth_deg',
                cols['distance_m']: 'distance_m'
            })
            # parse datetime (robust)
            df['datetime'] = pd.to_datetime(
                df['datetime'], errors='coerce', infer_datetime_format=True
            )
            # if everything NaT, fall back to explicit format
            if df['datetime'].isna().all():
                df['datetime'] = pd.to_datetime(df['datetime'],
                                                format='%Y-%m-%d %H:%M:%S',
                                                errors='coerce')
            df['azimuth_deg'] = pd.to_numeric(df['azimuth_deg'], errors='coerce')
            df['distance_m'] = pd.to_numeric(df['distance_m'], errors='coerce')
            return df[['datetime','azimuth_deg','distance_m']]
    except Exception:
        pass

    # Fallback: position-based (new layout)
    # Read without header; keep at least 4 cols
    df = pd.read_csv(path, header=None)
    if df.shape[1] < 4:
        raise ValueError(
            "CSV appears to have fewer than 4 columns; cannot map to new layout."
        )
    out = pd.DataFrame({
        'datetime': pd.to_datetime(df.iloc[:, 1],
                                   format='%Y-%m-%d %H:%M:%S',
                                   errors='coerce'),
        'azimuth_deg': pd.to_numeric(df.iloc[:, 2], errors='coerce'),
        'distance_m': pd.to_numeric(df.iloc[:, 3], errors='coerce')
    })
    return out

def main():
    csv_file = sys.argv[1] if len(sys.argv) > 1 else 'GeolocationLog.csv'
    if not os.path.isfile(csv_file):
        print(f"Error: '{csv_file}' not found in current directory.")
        sys.exit(1)

    # ── Load, filter out nodata, classify ECOSTRESS vs LANDSAT ──────────────
    df = load_geo_csv(csv_file)

    # drop invalid rows
    df = df.dropna(subset=['datetime','azimuth_deg','distance_m'])
    # remove sentinel nodata if present
    df = df[(df['azimuth_deg'] != -99999) & (df['distance_m'] != -99999)]

    # classify: LANDSAT at 00:00:00, ECOSTRESS otherwise (same rule as before)
    midnight = pd.to_datetime('00:00:00').time()
    df['type'] = np.where(df['datetime'].dt.time == midnight, 'LANDSAT', 'ECOSTRESS')
    df_ls = df[df['type'] == 'LANDSAT']
    df_ec = df[df['type'] == 'ECOSTRESS']

    # ── Scene counts & average repeat intervals (days) ───────────────────────
    n_ec = len(df_ec)
    delta_ec = df_ec.sort_values('datetime')['datetime'].diff().dt.days.dropna()
    avg_repeat_ec = delta_ec.mean() if not delta_ec.empty else np.nan

    n_ls = len(df_ls)
    delta_ls = df_ls.sort_values('datetime')['datetime'].diff().dt.days.dropna()
    avg_repeat_ls = delta_ls.mean() if not delta_ls.empty else np.nan

    print(f"ECOSTRESS scenes: {n_ec}, average repeat time: {avg_repeat_ec:.2f} days")
    print(f"LANDSAT scenes:  {n_ls}, average repeat time: {avg_repeat_ls:.2f} days")

    # ── Figure & GridSpec setup ─────────────────────────────────────────────
    fig = plt.figure(figsize=(16, 6))
    gs = GridSpec(2, 2, figure=fig,
                  width_ratios=[1.4, 1], height_ratios=[1, 1],
                  wspace=0.2, hspace=0.3)

    ms_cp = 14
    ms_rad = 4
    max_dist_cp = 8000
    max_dist_rad = 5000

    # ── 1) Timeline (left, top) ─────────────────────────────────────────────
    ax_tl = fig.add_subplot(gs[0, 0])
    pos_ec = mdates.date2num(df_ec['datetime'])
    pos_ls = mdates.date2num(df_ls['datetime'])
    ax_tl.eventplot([pos_ec, pos_ls],
                    colors=['green','blue'],
                    lineoffsets=[1,0],
                    linelengths=0.6, linewidths=1)
    ax_tl.set_yticks([1, 0]); ax_tl.set_yticklabels(['ECOSTRESS','LANDSAT'])
    ax_tl.xaxis_date()
    ax_tl.xaxis.set_major_formatter(mdates.DateFormatter('%m-%Y'))

    # Start at earliest full-year boundary >= min date, but not before 2019-01-01
    start_date = max(pd.to_datetime('2019-01-01'),
                     pd.to_datetime(f"{df['datetime'].min().year}-01-01"))
    end_date = df['datetime'].max()
    ax_tl.set_xlim(start_date, end_date)

    years = sorted(df['datetime'].dt.year.dropna().unique())
    jan_ticks = [pd.to_datetime(f'{y}-01-01') for y in years
                 if pd.to_datetime(f'{y}-01-01') >= start_date]
    if jan_ticks:
        ax_tl.set_xticks(mdates.date2num(jan_ticks))
        for dt in jan_ticks:
            ax_tl.axvline(mdates.date2num(dt), color='gray', linewidth=0.5)

    ax_tl.text(0.02, 0.90, f"{n_ec} scenes, avg {avg_repeat_ec:.1f} days",
               transform=ax_tl.transAxes, fontsize=10, va='bottom',
               bbox=dict(facecolor='white', alpha=0.7, edgecolor='none'))
    ax_tl.text(0.02, 0.10, f"{n_ls} scenes, avg {avg_repeat_ls:.1f} days",
               transform=ax_tl.transAxes, fontsize=10, va='top',
               bbox=dict(facecolor='white', alpha=0.7, edgecolor='none'))
    ax_tl.set_title('Observation Timeline')
    plt.setp(ax_tl.get_xticklabels(), rotation=0, ha='center')

    # ── 2) Cross-plot Azimuth vs Distance (left, bottom) ────────────────────
    ax_cp = fig.add_subplot(gs[1, 0])
    for az, dist in zip(df_ec['azimuth_deg'], df_ec['distance_m']):
        ax_cp.vlines(az, 0, dist, color='green', linewidth=0.7, alpha=0.5)
    for az, dist in zip(df_ls['azimuth_deg'], df_ls['distance_m']):
        ax_cp.vlines(az, 0, dist, color='blue', linewidth=0.7, alpha=0.5)
    ax_cp.scatter(df_ec['azimuth_deg'], df_ec['distance_m'],
                  s=ms_cp, facecolors='none', edgecolors='green', label='ECOSTRESS')
    ax_cp.scatter(df_ls['azimuth_deg'], df_ls['distance_m'],
                  s=ms_cp, facecolors='none', edgecolors='blue', label='LANDSAT')
    ax_cp.set_xlabel('Azimuth (°)'); ax_cp.set_ylabel('Distance (m)')
    ax_cp.set_xlim(0, 360); ax_cp.set_ylim(0, max_dist_cp)
    xticks = list(range(0, 361, 45))
    ax_cp.set_xticks(xticks); ax_cp.set_xticklabels([f"{t}°" for t in xticks])
    ax_cp.grid(axis='x', linestyle='--', linewidth=0.5, alpha=0.7)
    ax_cp.legend(loc='upper left'); ax_cp.set_title('Azimuth vs Distance')

    # ── 3) Radial plot (right, spanning both rows) ─────────────────────────
    ax_rad = fig.add_subplot(gs[:, 1], projection='polar')
    ax_rad.set_theta_zero_location('N'); ax_rad.set_theta_direction(-1)
    ax_rad.set_ylim(0, max_dist_rad)

    all_ticks = list(range(0, max_dist_rad+1, 1000))
    ax_rad.set_yticks(all_ticks)
    labels = [str(t) if 3000 <= t <= 4000 else '' for t in all_ticks]
    ax_rad.set_yticklabels(labels)
    ax_rad.set_rlabel_position(90)
    ax_rad.tick_params(axis='y', labelrotation=-25)
    ax_rad.grid(True, linestyle='--', linewidth=0.5)

    angles_ec = np.deg2rad(df_ec['azimuth_deg'])
    radii_ec  = df_ec['distance_m']
    for th, r in zip(angles_ec, radii_ec):
        ax_rad.plot([th, th], [0, r], color='green', linewidth=1)
        ax_rad.plot(th, r, marker='o', markersize=ms_rad,
                    markerfacecolor='none', markeredgecolor='green')

    angles_ls = np.deg2rad(df_ls['azimuth_deg'])
    radii_ls  = df_ls['distance_m']
    for th, r in zip(angles_ls, radii_ls):
        ax_rad.plot([th, th], [0, r], color='blue', linewidth=1)
        ax_rad.plot(th, r, marker='o', markersize=ms_rad,
                    markerfacecolor='none', markeredgecolor='blue')

    eco_line = mlines.Line2D([], [], color='green', marker='o',
                             markerfacecolor='none', markersize=ms_rad, label='ECOSTRESS')
    ls_line  = mlines.Line2D([], [], color='blue',  marker='o',
                             markerfacecolor='none', markersize=ms_rad, label='LANDSAT')
    ax_rad.legend(handles=[eco_line, ls_line], loc='upper left', bbox_to_anchor=(0.0, 1.1))
    ax_rad.set_title('Azimuth & Distance Corrections (Radial)', pad=20)

    plt.tight_layout()
    plt.show()

if __name__ == '__main__':
    main()
