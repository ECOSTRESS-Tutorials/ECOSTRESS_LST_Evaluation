#!/usr/bin/env python3
"""
NASA JPL Thermal Viewer — Semi‑Automated Georeferencer v1.12
  - We do not specifically use VRAM, but > 8GB is better so-as to keep CPU available.

Dependencies:
  pip install PyQt5 matplotlib rasterio geopandas scikit-image pyproj numpy shapely

Run examples:
  python GeoViewer.py  
  python GeoViewer_PyQt5.py --gdal-cache-mb 4096 --threads all

Notes:
- Backend is a Qt backend (Qt5Agg preferred, QtAgg fallback). No fallback to Agg.
- Optional overlay shapefile defaults to "YourShapefile.shp".
"""

import os, sys, glob, csv, time, textwrap, argparse
from pathlib import Path

# ── Lock UI to raw pixels (no per-monitor scaling) ──────────────────────────
os.environ["QT_AUTO_SCREEN_SCALE_FACTOR"] = "0"
os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "0"
os.environ["QT_SCALE_FACTOR"] = "1"

from PyQt5 import QtCore, QtGui, QtWidgets

QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_DisableHighDpiScaling, True)
# ── Lock UI to raw pixels (no per-monitor scaling) ──────────────────────────

# ---- Matplotlib: force a Qt backend BEFORE importing pyplot/figure canvas ---- #
import matplotlib
try:
    matplotlib.use("Qt5Agg")  # preferred on PyQt5
except Exception:
    matplotlib.use("QtAgg")   # unified name on newer Matplotlib

import matplotlib.pyplot as plt  # needed for style API
from matplotlib import colors as mcolors

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QDialog, QLabel, QPushButton,
    QMessageBox
)

import numpy as np
import rasterio
from rasterio.transform import Affine
from rasterio.warp import reproject, Resampling, transform as warp_transform
from rasterio.windows import bounds as window_bounds

import geopandas as gpd
from pyproj import Geod
from skimage.transform import estimate_transform, warp as skwarp, AffineTransform
from skimage.filters import sobel
from skimage.morphology import binary_dilation, binary_erosion, square
from matplotlib.figure import Figure
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.widgets import RectangleSelector, Button

# ---- Global style to match original dark look ---- #
try:
    plt.style.use("dark_background")
except Exception:
    pass
matplotlib.rcParams.update({
    "figure.facecolor": "black",
    "axes.facecolor": "black",
    "text.color": "#D3D3D3",
    "axes.titlecolor": "#D3D3D3",
    "axes.labelcolor": "#D3D3D3",
    "axes.edgecolor": "black",
    "xtick.color": "#D3D3D3",
    "ytick.color": "#D3D3D3",
    "font.family": "Lucida Console",
    "keymap.save": [],
})

# ---- Constants & performance defaults ---- #
LOG_FILE = "GeolocationLog.csv"
WARP_COLS = ["warp_a","warp_b","warp_c","warp_d","warp_e","warp_f"]
SHAPEFILE = "Hawaii_Oahu_07012025.shp"

# ---------------------------------------------------------------------------
# ---- Performance knobs (Large TIFF defaults) ----
# 12 GB cache unless overridden via env GEOVIEWER_GDAL_CACHE_MB
DEFAULT_GDAL_CACHE_MB = int(os.environ.get("GEOVIEWER_GDAL_CACHE_MB", str(12 * 1024)))

# Use all cores minus one unless overridden via env GEOVIEWER_REPROJECT_THREADS
# Accepts values like: "-1", "all-1", "all", "8", etc.
DEFAULT_THREADS_RAW = os.environ.get("GEOVIEWER_REPROJECT_THREADS", "-1")

def _parse_threads(val):
    """Return a safe int >= 1 for thread count.
    Rules:
      - 'all'/'auto'/'max' => os.cpu_count()
      - 'all-<n>' or 'max-<n>' => os.cpu_count() - n
      - negative ints (e.g., -1) => os.cpu_count() + val  (so -1 means cpu-1)
      - positive ints => that value
      - fallback => max(1, cpu-1)
    """
    import re
    cpus = os.cpu_count() or 1

    # String patterns
    if isinstance(val, str):
        v = val.strip().lower()

        # all / auto / max
        if v in ("all", "auto", "max"):
            return max(1, cpus)

        # all-<n> / max-<n> / auto-<n>
        m = re.fullmatch(r"(all|max|auto)\s*-\s*(\d+)", v)
        if m:
            return max(1, cpus - int(m.group(2)))

        # plain integer string (could be negative)
        try:
            n = int(v)
            return max(1, cpus + n) if n < 0 else max(1, n)
        except Exception:
            return max(1, cpus - 1)

    # Non-string (int-like)
    try:
        n = int(val)
        return max(1, cpus + n) if n < 0 else max(1, n)
    except Exception:
        return max(1, cpus - 1)

REPROJECT_THREADS = _parse_threads(DEFAULT_THREADS_RAW)

# Apply cache size to GDAL immediately so Rasterio picks it up.
# (You can also pass GDAL_CACHEMAX via rasterio.Env(...) if you prefer.)
os.environ["GDAL_CACHEMAX"] = str(DEFAULT_GDAL_CACHE_MB)

geod = Geod(ellps="WGS84")

# ---------------------------------------------------------------------------
# Utility functions (logic preserved from Tk version)
# ---------------------------------------------------------------------------

# ── User-driven datetime parsing support ────────────────────────────────────
# Globals set when the user provides a pattern in the post-splash dialog.
USER_DT_REGEX = None        # compiled re.Pattern that finds the dt substring
USER_DT_STRPTIME = None     # e.g., "%Y%d%mT%H%M%S" or "%Y%j%H%M%S"

def _userpattern_to_regex_and_strptime(pattern_text: str):
    """
    Convert a user-entered token string like:
      'yyyymmddThhmmss' or 'yyyydoyhhmmss'
    into (compiled_regex, strptime_format).
    'mm' before 'hh' => MONTH (%m); 'mm' after 'hh' => MINUTES (%M).
    """
    import re

    s = pattern_text.strip()
    i = 0
    tokens = []  # list of dicts to avoid tuple immutability issues

    while i < len(s):
        chunk = s[i:].lower()
        if chunk.startswith("yyyy"):
            tokens.append({"type": "yyyy"}); i += 4
        elif chunk.startswith("doy"):
            tokens.append({"type": "doy"}); i += 3
        elif chunk.startswith("dd"):
            tokens.append({"type": "dd"}); i += 2
        elif chunk.startswith("hh"):
            tokens.append({"type": "hh"}); i += 2
        elif chunk.startswith("mm"):
            tokens.append({"type": "mm"}); i += 2
        elif chunk.startswith("ss"):
            tokens.append({"type": "ss"}); i += 2
        else:
            tokens.append({"type": "lit", "val": s[i]}); i += 1

    # Decide which 'mm' is month vs minutes
    seen_hour = False
    for tok in tokens:
        if tok["type"] == "hh":
            seen_hour = True
        elif tok["type"] == "mm":
            tok["role"] = "min" if seen_hour else "mon"

    # Build strptime and regex
    strp_parts = []
    regex_parts = []
    for tok in tokens:
        t = tok["type"]
        if t == "yyyy":
            strp_parts.append("%Y"); regex_parts.append(r"\d{4}")
        elif t == "doy":
            strp_parts.append("%j"); regex_parts.append(r"\d{3}")
        elif t == "dd":
            strp_parts.append("%d"); regex_parts.append(r"\d{2}")
        elif t == "hh":
            strp_parts.append("%H"); regex_parts.append(r"\d{2}")
        elif t == "mm":
            role = tok.get("role", "mon")
            if role == "mon":
                strp_parts.append("%m"); regex_parts.append(r"\d{2}")
            else:
                strp_parts.append("%M"); regex_parts.append(r"\d{2}")
        elif t == "ss":
            strp_parts.append("%S"); regex_parts.append(r"\d{2}")
        else:  # literal
            lit = tok["val"]
            strp_parts.append(lit)
            regex_parts.append(re.escape(lit))

    compiled = re.compile("".join(regex_parts))
    return compiled, "".join(strp_parts)

class DatePatternDialog(QtWidgets.QDialog):
    """
    Modal dialog that:
      1) shows an example filename,
      2) asks user to paste the EXACT datetime substring from that filename,
      3) asks for a pattern like 'yyyyddmmhhmmss' or 'yyyydoyhhmmss',
         (ask them to include 'T' if present so spacing is preserved)
    and validates it by trying to parse the provided substring.
    """
    def __init__(self, example_filename: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Tell me how your datetimes look")
        self.setModal(True)
        self.resize(820, 280)
        self.setStyleSheet("background-color: black; color: #D3D3D3;")

        self.result_regex = None
        self.result_strptime = None

        hdr_font  = QtGui.QFont("Lucida Console", 18, QtGui.QFont.Bold)
        body_font = QtGui.QFont("Lucida Console", 13)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(18, 18, 18, 18)
        lay.setSpacing(10)

        lbl = QtWidgets.QLabel("Example filename:")
        lbl.setFont(body_font)
        lay.addWidget(lbl)

        ex = QtWidgets.QLabel(example_filename)
        ex.setFont(hdr_font)
        ex.setStyleSheet("color: white;")
        ex.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        lay.addWidget(ex)

        instr = QtWidgets.QLabel(
            "Paste the EXACT datetime portion from the filename above (include literal 'T' if present):"
        )
        instr.setFont(body_font)
        lay.addWidget(instr)

        self.subedit = QtWidgets.QLineEdit()
        self.subedit.setFont(body_font)
        self.subedit.setPlaceholderText("e.g., 20241029T154230  or  2024275T154230")
        lay.addWidget(self.subedit)

        fmtlbl = QtWidgets.QLabel(
            "Now type the matching pattern (e.g., yyyyddmmhhmmss or yyyydoyhhmmss). "
            "Include separators like 'T', '_' or '-'."
        )
        fmtlbl.setFont(body_font)
        lay.addWidget(fmtlbl)

        self.fmts = QtWidgets.QLineEdit()
        self.fmts.setFont(body_font)
        self.fmts.setPlaceholderText("e.g., yyyyddmmThhmmss  or  yyyydoyhhmmss")
        lay.addWidget(self.fmts)

        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        ok = QtWidgets.QPushButton("OK")
        ok.setFont(body_font)
        ok.setStyleSheet("background:#202020; color:#D3D3D3; padding:6px 16px;")
        ok.clicked.connect(self._on_ok)
        cancel = QtWidgets.QPushButton("Cancel")
        cancel.setFont(body_font)
        cancel.setStyleSheet("background:#202020; color:#D3D3D3; padding:6px 16px;")
        cancel.clicked.connect(self.reject)
        row.addWidget(ok); row.addWidget(cancel)
        lay.addLayout(row)

    def _on_ok(self):
        from datetime import datetime
        sub = (self.subedit.text() or "").strip()
        pat = (self.fmts.text() or "").strip()
        if not sub or not pat:
            QtWidgets.QMessageBox.warning(self, "Missing Input",
                "Please provide BOTH the datetime substring and its pattern.")
            return
        try:
            rx, sp = _userpattern_to_regex_and_strptime(pat)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Pattern Error",
                f"Could not interpret your pattern:\n{e}")
            return

        # Use the compiled pattern's .fullmatch()
        if rx.fullmatch(sub) is None:
            QtWidgets.QMessageBox.critical(
                self, "Pattern Mismatch",
                "Your pattern does not match the substring you entered.\n"
                "Check the number of digits and literals (include 'T' if present)."
            )
            return
        try:
            _ = datetime.strptime(sub, sp)
        except Exception as e:
            QtWidgets.QMessageBox.critical(
                self, "Parse Failed",
                f"Python could not parse the substring with that pattern:\n{e}"
            )
            return

        self.result_regex = rx
        self.result_strptime = sp
        self.accept()

def parse_datetime_from_filename(fname: str) -> str:
    """
    Return an ISO-like timestamp string for this filename (SPACE between date/time).
    If the user provided a filename pattern in the dialog, prefer that.
    Otherwise, try common patterns anywhere in the name before legacy fallbacks.
    """
    import os, re
    from datetime import datetime

    base = os.path.basename(fname)

    # 1) If the user gave us a pattern, use it
    global USER_DT_REGEX, USER_DT_STRPTIME
    if USER_DT_REGEX and USER_DT_STRPTIME:
        m = USER_DT_REGEX.search(base)
        if m:
            try:
                dt = datetime.strptime(m.group(0), USER_DT_STRPTIME)
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass  # fall through

    # 2) Robust regex search anywhere in the filename (handles prefixes like 'ECO_')
    #    Try YYYYMMDDTHHMMSS
    m = re.search(r'(\d{8}T\d{6})', base)
    if m:
        try:
            dt = datetime.strptime(m.group(1), "%Y%m%dT%H%M%S")
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    #    Try YYYYMMDDHHMMSS
    m = re.search(r'(\d{14})', base)
    if m:
        try:
            dt = datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    #    Try YYYYDOYHHMMSS
    m = re.search(r'(\d{4}\d{3}\d{6})', base)
    if m:
        try:
            dt = datetime.strptime(m.group(1), "%Y%j%H%M%S")
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass

    # 3) Legacy fallback — look only at the first '_' token (may be 'ECO', etc.)
    ts = base.split('_')[0]
    try:
        if 'T' in ts and len(ts) >= 15:
            maybe = datetime.strptime(ts[:15], "%Y%m%dT%H%M%S")
            return maybe.strftime("%Y-%m-%d %H:%M:%S")
        if len(ts) >= 14:
            maybe = datetime.strptime(ts[:14], "%Y%m%d%H%M%S")
            return maybe.strftime("%Y-%m-%d %H:%M:%S")
        if len(ts) >= 13:
            maybe = datetime.strptime(ts[:13], "%Y%j%H%M%S")
            return maybe.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass

    # 4) Give up
    return ts

def read_log():
    processed_dts = {}
    processed_files = set()
    log_entries = []

    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, newline='') as f:
            reader = csv.reader(f)
            header = next(reader, [])
            lower  = [h.lower() for h in header]

            has_fname     = (header and lower[0] == 'filename')
            has_warpcols  = all(c in lower for c in WARP_COLS)
            has_warpflag  = ('warped' in lower)  # old format

            for row in reader:
                if has_fname:
                    # filename-first logs
                    fname, dt = row[0], row[1]
                    az, dist  = float(row[2]), float(row[3])

                    if has_warpflag:
                        # old: explicit warped flag, then 6 params
                        warp_flag = (row[4] == '1')
                        vals = [float(v) for v in row[5:11]] if has_warpcols else [0]*6
                    else:
                        # new: no 'warped' column — infer from params
                        vals = [float(v) for v in row[4:10]] if has_warpcols else [0]*6
                        # infer: any non-zero means warped; also guard if identity ever gets logged
                        eps = 1e-12
                        is_all_zero = all(abs(v) < eps for v in vals)
                        is_identity = (len(vals) == 6 and
                                       abs(vals[0]-1) < eps and abs(vals[1]) < eps and abs(vals[2]) < eps and
                                       abs(vals[3]) < eps  and abs(vals[4]-1) < eps and abs(vals[5]) < eps)
                        warp_flag = (not is_all_zero) and (not is_identity)
                else:
                    # very old: datetime-first logs
                    dt       = row[0]
                    az, dist = float(row[1]), float(row[2])

                    if has_warpflag:
                        warp_flag = (row[3] == '1')
                        vals = [float(v) for v in row[4:10]] if has_warpcols else [0]*6
                    else:
                        vals = [float(v) for v in row[3:9]] if has_warpcols else [0]*6
                        eps = 1e-12
                        is_all_zero = all(abs(v) < eps for v in vals)
                        is_identity = (len(vals) == 6 and
                                       abs(vals[0]-1) < eps and abs(vals[1]) < eps and abs(vals[2]) < eps and
                                       abs(vals[3]) < eps  and abs(vals[4]-1) < eps and abs(vals[5]) < eps)
                        warp_flag = (not is_all_zero) and (not is_identity)
                    fname = None

                processed_dts[dt] = (az, dist, warp_flag, vals)
                if fname:
                    processed_files.add(fname)
                log_entries.append((fname, dt, az, dist, warp_flag, vals))
    return processed_dts, processed_files, log_entries

def write_log(log_entries):
    with open(LOG_FILE, 'w', newline='') as f:
        w = csv.writer(f)
        # NEW: drop the 'warped' column from disk; keep only the params
        w.writerow(['filename','datetime','azimuth_deg','distance_m'] + WARP_COLS)
        for fname, dt, az, dist, wf, vals in log_entries:
            w.writerow([fname, dt, f"{az:.6f}", f"{dist:.2f}"] +
                       [f"{v:.6f}" for v in (vals if len(vals)==6 else [0]*6)])

# ---- dtype & nodata helpers to support uint16/int ----
def _nodata_for_dtype(dtype, existing):
    """Choose a sensible nodata for the dtype, honoring an existing value."""
    if existing is not None:
        return existing
    import numpy as np
    if np.issubdtype(dtype, np.unsignedinteger):
        return 0
    if np.issubdtype(dtype, np.integer):
        return np.iinfo(dtype).min
    return np.nan

def auto_geocorrect(all_files, processed_dts, processed_files, log_entries):
    count = 0
    for path in all_files:
        dt = parse_datetime_from_filename(path)
        fname = os.path.basename(path)
        if dt in processed_dts and fname not in processed_files:
            az, dist, warp_flag, vals = processed_dts[dt]
            # pan‑shift apply

            if dist > 0:
                with rasterio.open(path) as src:
                    arr  = src.read()
                    meta = src.meta.copy()
                    L, R, B, T = src.bounds.left, src.bounds.right, src.bounds.bottom, src.bounds.top
                    lon0, lat0 = warp_transform(src.crs, 'EPSG:4326', [(L+R)/2], [(B+T)/2])
                dest_lon, dest_lat, _ = geod.fwd(lon0[0], lat0[0], az, dist)
                dest_x, dest_y = warp_transform('EPSG:4326', src.crs, [dest_lon], [dest_lat])
                dx = dest_x[0] - (L+R)/2
                dy = dest_y[0] - (B+T)/2
                with rasterio.open(path) as src:
                    oT = src.transform
                    crs = src.crs
                nT = Affine(oT.a, oT.b, oT.c+dx, oT.d, oT.e, oT.f+dy)

                src_dtype = arr.dtype
                dst_dtype = src_dtype
                nd        = _nodata_for_dtype(dst_dtype, meta.get('nodata'))

                if np.issubdtype(dst_dtype, np.integer):
                    dest_full = np.full_like(arr, nd, dtype=dst_dtype)
                    for b in range(arr.shape[0]):
                        reproject(
                            source=arr[b],
                            destination=dest_full[b],
                            src_transform=nT, src_crs=crs,
                            dst_transform=oT,  dst_crs=crs,
                            resampling=Resampling.nearest,
                            src_nodata=nd,     dst_nodata=nd,
                            num_threads=REPROJECT_THREADS
                        )
                    meta.update(transform=oT, dtype=dst_dtype, nodata=nd)
                else:
                    dest_full = np.full_like(arr, np.nan, dtype='float32')
                    for b in range(arr.shape[0]):
                        reproject(
                            source=arr[b],
                            destination=dest_full[b],
                            src_transform=nT, src_crs=crs,
                            dst_transform=oT,  dst_crs=crs,
                            resampling=Resampling.nearest,
                            src_nodata=meta.get('nodata'), dst_nodata=np.nan,
                            num_threads=REPROJECT_THREADS
                        )
                    meta.update(transform=oT, dtype='float32', nodata=np.nan)

                tmp = path + '.tmp'
                with rasterio.open(tmp, 'w', **meta) as dst:
                    dst.write(dest_full)
                os.replace(tmp, path)

            # warp apply
            if warp_flag:
                a, b, c, d, e, f = vals
                mat = np.array([[a, b, c], [d, e, f], [0, 0, 1]])
                tform = AffineTransform(matrix=mat)
                with rasterio.open(path) as src:
                    band = src.read(1)
                    m    = src.meta.copy()

                dst_dtype = band.dtype
                nd        = _nodata_for_dtype(dst_dtype, m.get('nodata'))

                warped = skwarp(band.astype(np.float32),
                                inverse_map=tform.inverse,
                                output_shape=band.shape,
                                cval=np.nan, preserve_range=True)

                if np.issubdtype(dst_dtype, np.integer):
                    info   = np.iinfo(dst_dtype)
                    warped = np.where(np.isnan(warped), nd, np.rint(warped))
                    warped = np.clip(warped, info.min, info.max).astype(dst_dtype)
                    m.update(dtype=dst_dtype, nodata=nd)
                else:
                    warped = warped.astype('float32')
                    m.update(dtype='float32', nodata=np.nan)

                tmpw = path + '.warp.tmp'
                with rasterio.open(tmpw, 'w', **m) as dst:
                    dst.write(warped, 1)
                os.replace(tmpw, path)

            log_entries.append((fname, dt, az, dist, warp_flag, vals))
            processed_files.add(fname)
            count += 1
    write_log(log_entries)
    return count

# ---------------------------------------------------------------------------
# Splash dialog (Qt version of the Tk splash)
# ---------------------------------------------------------------------------
class SplashDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("NASA JPL Thermal Viewer")
        self.setModal(True)
        self.setFixedSize(1750, 1050)
        self.setStyleSheet("background-color: black; color: #D3D3D3;")

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)

        hdr_font = QtGui.QFont("Lucida Console", 29, QtGui.QFont.Bold)
        body_font = QtGui.QFont("Lucida Console", 16)
        ver_font  = QtGui.QFont("Lucida Console", 11)

        shapes1 = QLabel("▛▟ ▞▚ ▟▛ ▞▚")
        shapes1.setFont(hdr_font); shapes1.setAlignment(QtCore.Qt.AlignHCenter)
        shapes1.setStyleSheet("color: white;")
        layout.addWidget(shapes1)

        shapes2 = QLabel("▟ ▛ ▙")
        shapes2.setFont(hdr_font); shapes2.setAlignment(QtCore.Qt.AlignHCenter)
        shapes2.setStyleSheet("color: red;")
        layout.addWidget(shapes2)

        title = QLabel("NASA JPL THERMAL VIEWER")
        title.setFont(hdr_font); title.setAlignment(QtCore.Qt.AlignHCenter)
        layout.addWidget(title)

        subtitle = QLabel("SEMI‑AUTOMATED GEOREFERENCER")
        subtitle.setFont(body_font); subtitle.setAlignment(QtCore.Qt.AlignHCenter)
        layout.addWidget(subtitle)

        help_text = textwrap.dedent(
            """
                [SPACE]    Toggle Pan    |    [W/A/S/D]   Pan Image
                [1 – 3]     Pan Speed    |    [G/H/J]    Warp/Apply
                [8 – 9]     Pan Multi    |    [SHIFT]    Undo Warps
                [L drag]   Zoom Image    |    [O/K]    Gamma Adjust
                [R click]  Reset Zoom    |    [P/L] Contrast Adjust
                [ENTER]  Reset Colors    |    [X]  Skip/Defer Scene
                [KEEP]   Apply / Save    |    [F]  Toggle Full Scrn
                [REJECT]  Delete file    |    [E/R]  Edges/Colormap
            """
        )
        help_label = QLabel(help_text)
        help_label.setFont(body_font)
        help_label.setTextFormat(QtCore.Qt.PlainText)  # preserve spaces/newlines
        help_label.setAlignment(QtCore.Qt.AlignHCenter)
        help_label.setWordWrap(False)
        layout.addWidget(help_label)

        launch = QPushButton("  LAUNCH  ")
        launch.setFont(body_font)
        launch.setStyleSheet("background-color:#202020; color:#D3D3D3; padding:6px 12px;")
        launch.clicked.connect(self.accept)
        layout.addWidget(launch, alignment=QtCore.Qt.AlignHCenter)

        ver = QLabel("v1.12 | Longenecker et al. | MIT License 2025")
        ver.setFont(ver_font); ver.setAlignment(QtCore.Qt.AlignHCenter)
        layout.addWidget(ver)

# ---------------------------------------------------------------------------
# Main Viewer Window
# ---------------------------------------------------------------------------
# ── Shapefile picker UI (compact, readable, visible controls) ───────────── #
PICKER_COLORS = [
    "cyan", "magenta", "white", "black", "gray",
    "red", "orange", "yellow", "dodgerblue", "lime", "violet", "pink"
]

class ShapefilePickerDialog(QtWidgets.QDialog):
    """
    Choose:
      • one PRIMARY shapefile (for referencing) + its color
      • up to 5 additional overlay shapefiles + their colors

    This version improves readability (larger fonts), reduces empty space,
    and uses dark-but-visible widgets (combos, buttons, borders).
    """
    def __init__(self, shp_paths, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Choose Shapefiles")
        self.setModal(True)

        # ----- Readability: larger base font
        base_font = self.font()
        base_font.setPointSize(12)           # bump overall text size
        self.setFont(base_font)

        # ----- Compact & visible dark theme
        self.setStyleSheet("""
        QDialog { background-color: #111; color: #E6E6E6; }
        QLabel  { color: #E6E6E6; font-size: 12pt; }
        QGroupBox {
            color: #E6E6E6; font-weight: 600; font-size: 12pt;
            border: 1px solid #2a2a2a; border-radius: 8px;
            margin-top: 12px; padding: 10px 10px 8px 10px;
        }
        QGroupBox::title {
            subcontrol-origin: margin; left: 10px; top: 0px;
            padding: 0 4px; background-color: #111; color: #F0F0F0;
        }
        QComboBox {
            background-color: #1f1f1f; color: #EAEAEA;
            border: 1px solid #3a3a3a; border-radius: 6px;
            padding: 4px 8px; font-size: 12pt; min-height: 28px;
        }
        QComboBox:disabled {
            color: #999; background-color: #191919; border-color: #2b2b2b;
        }
        QComboBox QAbstractItemView {
            background-color: #1a1a1a; color: #F0F0F0;
            selection-background-color: #2d5fff; selection-color: #ffffff;
            border: 1px solid #3a3a3a;
        }
        QPushButton {
            background-color: #2a2a2a; color: #E6E6E6;
            border: 1px solid #3a3a3a; border-radius: 8px;
            padding: 6px 12px; font-size: 11pt;
        }
        QPushButton:hover  { background-color: #333; }
        QPushButton:pressed{ background-color: #3a3a3a; }
        QDialogButtonBox QPushButton { min-width: 90px; }
        """)

        # ----- Build label maps with EPSG lookup
        self._labels = []
        self._label_to_path = {}
        self._path_to_epsg = {}

        for p in sorted(shp_paths):
            name = os.path.basename(p)
            epsg_str, epsg_val = "Unknown", None
            try:
                _gdf = gpd.read_file(p)
                if _gdf.crs:
                    epsg_val = _gdf.crs.to_epsg()
                    epsg_str = f"EPSG:{epsg_val}" if epsg_val is not None else _gdf.crs.to_string()
            except Exception:
                epsg_str = "Unreadable"
            label = f"{name} — CRS: {epsg_str}"
            self._labels.append(label)
            self._label_to_path[label] = p
            self._path_to_epsg[p] = epsg_val

        # ----- Layout (tight margins/spacing)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)
        outer.setSizeConstraint(QtWidgets.QLayout.SetFixedSize)  # shrink-wrap to contents

        # Primary group (visually separated)
        grp_primary = QtWidgets.QGroupBox("Primary shapefile (used for referencing)", self)
        gl0 = QtWidgets.QGridLayout(grp_primary)
        gl0.setHorizontalSpacing(10)
        gl0.setVerticalSpacing(6)
        outer.addWidget(grp_primary)

        lab_shp = QtWidgets.QLabel("Shapefile:")
        lab_col = QtWidgets.QLabel("Color:")
        self.primary_combo = QtWidgets.QComboBox()
        self.primary_combo.addItems(self._labels)
        self.primary_combo.setMinimumWidth(420)  # show full filename+EPSG

        self.primary_color = QtWidgets.QComboBox()
        self.primary_color.addItems(PICKER_COLORS)
        self.primary_color.setCurrentText("cyan")
        self.primary_color.setMinimumWidth(140)

        gl0.addWidget(lab_shp,            0, 0)
        gl0.addWidget(self.primary_combo, 0, 1)
        gl0.addWidget(lab_col,            0, 2)
        gl0.addWidget(self.primary_color, 0, 3)
        gl0.setColumnStretch(1, 1)

        # Overlays group
        grp_ov = QtWidgets.QGroupBox("Additional overlays (optional, up to 5)")
        gl = QtWidgets.QGridLayout(grp_ov)
        gl.setHorizontalSpacing(10)
        gl.setVerticalSpacing(6)
        outer.addWidget(grp_ov)

        self.ov_shp, self.ov_col = [], []
        none_label = "— none —"

        for r in range(5):
            lab = QtWidgets.QLabel(f"Slot {r+1}:")
            shp_cb = QtWidgets.QComboBox()
            shp_cb.addItem(none_label)
            shp_cb.addItems(self._labels)
            shp_cb.setMinimumWidth(420)

            col_cb = QtWidgets.QComboBox()
            col_cb.addItems(PICKER_COLORS)
            col_cb.setCurrentText("dodgerblue" if r == 0 else "lime")
            col_cb.setMinimumWidth(140)

            # Disable color when 'none' is selected
            def _toggle_color(_idx, cb=col_cb, s=shp_cb):
                cb.setEnabled(s.currentText() != none_label)
            shp_cb.currentIndexChanged.connect(_toggle_color)
            _toggle_color(0)

            gl.addWidget(lab,    r, 0)
            gl.addWidget(shp_cb, r, 1)
            gl.addWidget(col_cb, r, 2)
            gl.setColumnStretch(1, 1)

            self.ov_shp.append(shp_cb)
            self.ov_col.append(col_cb)

        # OK/Cancel
        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        outer.addWidget(btns)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)

        # Results
        self.primary_path = None
        self.primary_epsg = None
        self.primary_color_sel = None
        self.overlay_selections = []  # list of (path, epsg, color)

    def accept(self):
        # Primary selection
        p_label = self.primary_combo.currentText()
        self.primary_path = self._label_to_path.get(p_label)
        self.primary_epsg = self._path_to_epsg.get(self.primary_path)
        self.primary_color_sel = self.primary_color.currentText()

        # Overlay selections (skip "— none —", prevent duplicates)
        seen = set([self.primary_path])
        overlays = []
        for shp_cb, col_cb in zip(self.ov_shp, self.ov_col):
            lbl = shp_cb.currentText()
            if lbl.startswith("—"):
                continue
            path = self._label_to_path.get(lbl)
            if not path or path in seen:
                continue
            seen.add(path)
            epsg = self._path_to_epsg.get(path)
            color = col_cb.currentText()
            overlays.append((path, epsg, color))

        self.overlay_selections = overlays
        super().accept()

class ThermalViewerQt(QMainWindow):

    def _ensure_overviews_once(self, path: str) -> None:
        """Build overview pyramids once per file (fast subsequent reads)."""
        if not hasattr(self, "_ovr_done"):
            self._ovr_done = set()
        if path in self._ovr_done:
            return
        try:
            # r+ lets GDAL create a sidecar .ovr if needed (no rewrite of the base tif)
            with rasterio.open(path, "r+") as ds:
                ovs = ds.overviews(1) or []
                # Ensure we have reasonably deep pyramids
                if not ovs or ovs[-1] < 32:
                    ds.build_overviews([2, 4, 8, 16, 32], Resampling.average)
                    ds.update_tags(ns="rio_overview", resampling="average")
        except Exception as e:
            print(f"[WARN] Overviews not built for {os.path.basename(path)}: {e}")
        finally:
            self._ovr_done.add(path)

    def _read_for_display(self, src, win=None, max_dim=1100):
        """
        Read a decimated view matching UI needs.
        max_dim ~ width of one panel in pixels (≈ figure_width/3).
        Returns (arr_float32_with_nan, (xmin, ymin, xmax, ymax)).
        """
        if win is not None:
            # window pixel size
            w_px = int(np.ceil(win.width))
            h_px = int(np.ceil(win.height))
            left, bottom, right, top = window_bounds(win, src.transform)
        else:
            w_px, h_px = src.width, src.height
            left, bottom, right, top = src.bounds.left, src.bounds.bottom, src.bounds.right, src.bounds.top

        # Choose a decimation so the longer side ≲ max_dim
        scale = max(1, int(np.ceil(max(w_px, h_px) / float(max_dim))))
        out_w = max(1, w_px // scale)
        out_h = max(1, h_px // scale)

        arr = src.read(
            1,
            window=win,
            out_shape=(out_h, out_w),          # (H, W) for a single band
            resampling=Resampling.nearest,     # fastest for preview
            masked=True,
            boundless=True
        )
        # Fill mask with NaN and keep memory light
        # Cast first, then fill; only call .filled on MaskedArray
        if np.ma.isMaskedArray(arr):
            arr = arr.astype("float32", copy=False).filled(np.nan)
        else:
            arr = arr.astype("float32", copy=False)

        return arr, (left, bottom, right, top)

    def __init__(self, all_files, processed_files, log_entries, parent=None):
        super().__init__(parent)
        self.setWindowTitle("NASA JPL Thermal Viewer — PyQt5 Edition")
        self.resize(1400, 850)

        # Central widget and layout
        central = QWidget(self)
        self.setCentralWidget(central)
        self.vbox = QVBoxLayout(central)
        self.vbox.setContentsMargins(0, 0, 0, 0)
        self.vbox.setSpacing(0)

        # Matplotlib Figure/Canvas
        self.fig = Figure(figsize=(23, 8.5), dpi=100)
        self.fig.patch.set_facecolor('black')
        self.canvas = FigureCanvas(self.fig)
        self.vbox.addWidget(self.canvas)

        # ---- Make sure the canvas receives keyboard focus ---- #
        try:
            self.canvas.setFocusPolicy(QtCore.Qt.StrongFocus)
        except Exception:
            # Qt6 name
            self.canvas.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.canvas.setFocus()

        # Data prepared from constructor args
        self.all_files = all_files
        self.processed_files = set(processed_files)
        self.log_entries = list(log_entries)

        # Determine files to process
        self.to_process = [f for f in self.all_files if os.path.basename(f) not in self.processed_files]
        self.total = len(self.to_process)
        if self.total == 0:
            QtWidgets.QMessageBox.information(self, "Info", "No new files to process.")
            QtCore.QTimer.singleShot(0, self.close)
            return

        # session counters
        self.session = {"as_is":0, "geo":0, "warp":0, "reject":0}

        # Load + color-select shapefile(s) (no CSV writes)
        self.shp_primary = None
        self.shp_primary_color = "cyan"
        self.shp_overlays = []   # list of (GeoDataFrame, color)

        try:
            shp_paths = sorted(glob.glob("*.shp"))
            # If none present, optionally fall back to a constant if your code defines SHAPEFILE
            if not shp_paths and 'SHAPEFILE' in globals() and os.path.exists(SHAPEFILE):
                shp_paths = [SHAPEFILE]

            if shp_paths:
                picker = ShapefilePickerDialog(shp_paths, parent=self)
                if picker.exec_() == QtWidgets.QDialog.Accepted:
                    # Primary
                    if picker.primary_path:
                        try:
                            self.shp_primary = gpd.read_file(picker.primary_path).to_crs(epsg=4326)
                            self.shp_primary_color = picker.primary_color_sel or "cyan"
                        except Exception as e:
                            print(f"[WARN] Could not read primary shapefile: {e}")
                            self.shp_primary = None

                    # Overlays
                    for pth, epsg_val, color in picker.overlay_selections:
                        try:
                            gdf = gpd.read_file(pth).to_crs(epsg=4326)
                            self.shp_overlays.append((gdf, color))
                        except Exception as e:
                            print(f"[WARN] Could not read overlay {os.path.basename(pth)}: {e}")
            # else: no shapefiles found — fine, just skip overlays
        except Exception as e:
            print(f"[WARN] Shapefile selection failed: {e}")
            self.shp_primary = None
            self.shp_overlays = []

        # Determine number of panels (<=3)
        self.n_pan = min(3, self.total)
        self.current = self.to_process[:self.n_pan]
        self.queue = self.to_process[self.n_pan:]

        # Build axes row
        self.axes = self.fig.subplots(1, self.n_pan)
        if self.n_pan == 1:
            self.axes = [self.axes]
        else:
            self.axes = list(self.axes)

        # Track last/active axes index for key events
        self.active_idx = 0

        # Progress footer text
        self.prog = self.fig.text(0.5, 0.02, f"Session: 0/{self.total} files (0.0%)",
                                  ha="center", color="#D3D3D3", fontsize=14)

        # State containers
        self.bases = {}
        self.offsets = {}
        self.images = {}
        self.images_data = {}
        self._ovr_done = set()
        self.srccrs = {}
        self.srctrans = {}
        self.buttons = []
        self.selectors = []
        self.warp_data = {
            i: {"src_world": [], "dst_world": [],
                "src_pix": [],   "dst_pix": [],
                "tform": None,   "applied": False,
                "collecting": False, "markers": [], "labels": []}
            for i in range(self.n_pan)
        }

        self.pan_mode = False
        self.base_multiplier = 1
        self.scale_modifier = 1.0
        self.pan_factor = 0.001 * self.base_multiplier * self.scale_modifier
        self.aoi_bounds = None  # (minx, miny, maxx, maxy) in world coords
        self.cmap_mode = 'gray'
        self.edge_cache = {}                                      # i -> {"data": ndarray, "range": (vmin, vmax)}
        self.data_ranges = {}                                     # i -> (vmin, vmax) for base image
        self.global_edge_mode = False          # False = base image, True = Sobel edges (all panels)
        self.global_contrast_rel = (0.0, 1.0)  # (center_rel, half_rel), relative to each panel's data range
        self.global_gamma = 1.0                # PowerNorm gamma applied to all panels

        # Draw initial panels
        for i in range(self.n_pan):
            self.draw(i)

        # Mouse + keyboard + motion events
        self.canvas.mpl_connect('button_press_event', self.on_button_press)
        self.canvas.mpl_connect('key_press_event', self.on_key)
        self.canvas.mpl_connect('motion_notify_event', self.on_motion)

        # Global SHIFT shortcut (works even when toolbar/buttons have focus)
        self._shortcut_shift_reset = QtWidgets.QShortcut(QtGui.QKeySequence("Shift"), self)
        self._shortcut_shift_reset.setContext(QtCore.Qt.ApplicationShortcut)
        self._shortcut_shift_reset.activated.connect(lambda: self._do_full_reset(self.active_idx))

        # Build Keep/Reject buttons under each panel
        self.add_keep_reject_buttons()

        # Disable any toolbar pan if present (we didn't add a toolbar, but be safe)
        self.kill_toolbar_pan_only()

        # Ensure log is flushed when app quits (belt‑and‑suspenders)
        app = QApplication.instance()
        if app is not None:
            try:
                app.aboutToQuit.connect(self.flush_log)
            except Exception:
                pass

        # Repaint
        self.fig.set_constrained_layout(True)
        self.canvas.draw_idle()

    # ------------------ Log flush hooks ------------------ #
    def flush_log(self):
        try:
            write_log(self.log_entries)
        except Exception as e:
            # Show a warning but do not crash the app
            try:
                QMessageBox.warning(self, "Log Write Error", f"Could not write {LOG_FILE}: {e}")
            except Exception:
                pass

    def closeEvent(self, event):
        # Always persist log on close
        self.flush_log()
        super().closeEvent(event)

    # ------------------ Helpers to manage toolbars/cursors ------------------ #
    def kill_toolbar_pan_only(self):
        tb = getattr(self.canvas, "toolbar", None)
        if tb:
            try:
                mode = (getattr(tb, "mode", "") or "")
                if "pan" in mode.lower() and hasattr(tb, "pan"):
                    tb.pan()  # toggle pan OFF
            except Exception:
                pass

        # reset cursor to standard pointer & keep focus on canvas
        try:
            self.canvas.setCursor(QtGui.QCursor(QtCore.Qt.ArrowCursor))
            self.canvas.setFocus()
        except Exception:
            pass

    # Back-compat alias to match original code path after 'R'
    def kill_toolbar_tools(self):
        tb = getattr(self.canvas, "toolbar", None)
        if tb:
            try:
                mode = (getattr(tb, "mode", "") or "")
                m = mode.lower()
                # toggle OFF whatever is active
                if "pan" in m and hasattr(tb, "pan"):
                    tb.pan()
                if "zoom" in m and hasattr(tb, "zoom"):
                    tb.zoom()
            except Exception:
                pass

        # always restore pointer & focus
        try:
            self.canvas.setCursor(QtGui.QCursor(QtCore.Qt.ArrowCursor))
            self.canvas.setFocus()
        except Exception:
            pass

    def _do_full_reset(self, idx: int):
        """Reset zoom to full TIF AOI, clear pan and any in-memory warp."""
        # Full scene
        self.aoi_bounds = None
        self.offsets[idx] = [0, 0]

        # Clear warp state & artifacts
        data = self.warp_data[idx]
        for m in data.get('markers', []):
            try: m.remove()
            except Exception: pass
        for l in data.get('labels', []):
            try: l.remove()
            except Exception: pass
        self.warp_data[idx] = {
            "src_world": [], "dst_world": [],
            "src_pix": [],   "dst_pix": [],
            "tform": None,   "applied": False,
            "collecting": False, "markers": [], "labels": []
        }

        # Redraw base (unwarped) & set full extent
        self.draw(idx)
        L, R, B, T = self.bases[idx]
        ax = self.axes[idx]
        ax.set_xlim(L, R); ax.set_ylim(B, T)

        # Make sure no lingering toolbar tool is active
        self.kill_toolbar_tools()
        self.canvas.draw_idle()
        self._ensure_focus()

    def _apply_rel_contrast_state(self, idx, img_obj, data_min, data_max):
        """Reapply saved relative-contrast (center_rel, half_rel) globally."""
        center_rel, half_rel = self.global_contrast_rel
        if data_min is None or data_max is None:
            return
        d_center = 0.5 * (data_min + data_max)
        d_half   = max(0.5 * (data_max - data_min), 1e-12)
        center   = d_center + center_rel * d_half
        half     = max(half_rel * d_half, 1e-12)
        vmin_new = center - half
        vmax_new = center + half
        try:
            img_obj.set_clim(vmin_new, vmax_new)  # unclamped; "blow out" allowed
        except Exception:
            pass

    # ----- focus / shortcut plumbing (lazy so you don't edit __init__) -----
    def _ensure_shortcuts_once(self):
        if getattr(self, "_shortcuts_ready", False):
            return
        # A plain Shift key often doesn't emit a MPL key_press reliably; use Qt too.
        try:
            self._sc_shift = QtWidgets.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Shift), self)
            self._sc_shift.setContext(QtCore.Qt.ApplicationShortcut)
            self._sc_shift.activated.connect(self._on_shift_shortcut)
            self._shortcuts_ready = True
        except Exception:
            self._shortcuts_ready = True  # don't retry forever if something odd happens

    def _on_shift_shortcut(self):
        # Use whichever panel the mouse last hovered
        idx = getattr(self, "active_idx", 0)
        self._do_full_reset(idx)

    def _ensure_focus(self):
        try:
            # Make sure the window and canvas are willing to take key focus
            self.setFocusPolicy(QtCore.Qt.StrongFocus)
            self.canvas.setFocusPolicy(QtCore.Qt.StrongFocus)
            self.canvas.setFocus()
        except Exception:
            pass

    # ------------------------------ Drawing -------------------------------- #
    def draw(self, i: int):
        path = self.current[i]
        ax = self.axes[i]
        ax.clear()
        ax.set_facecolor('black')

        # Invalidate any cached AxesImage that was removed by ax.clear()
        try:
            if i in self.images:
                self.images[i] = None
        except Exception:
            self.images[i] = None

        # Build overview pyramids once for faster, lower-res reads
        self._ensure_overviews_once(path)

        # Read decimated pixels suited to panel size
        with rasterio.Env(GDAL_NUM_THREADS="ALL_CPUS"):
            with rasterio.open(path, sharing=True) as src:
                if self.aoi_bounds:
                    win = src.window(*self.aoi_bounds)
                    im, (xmin, ymin, xmax, ymax) = self._read_for_display(
                        src, win, max_dim=1100
                    )
                else:
                    im, (xmin, ymin, xmax, ymax) = self._read_for_display(
                        src, None, max_dim=1100
                    )

                self.srccrs[i]  = src.crs
                self.srctrans[i] = src.transform

        # cache extents & base image
        self.bases[i]      = (xmin, xmax, ymin, ymax)
        self.offsets[i]    = [0, 0]
        self.images_data[i] = im

        # compute & store base data range (for contrast math)
        vmin = np.nanmin(im) if np.isfinite(im).any() else None
        vmax = np.nanmax(im) if np.isfinite(im).any() else None
        self.data_ranges[i] = (vmin, vmax)

        # Decide what to display (base or edges), recomputing edges if global edge mode is ON
        if self.global_edge_mode:
            base = im
            valid = np.isfinite(base)

            # suppress NaN boundary edges (3x3 ring)
            dil = binary_dilation(valid, square(3))
            ero = binary_erosion(valid, square(3))
            boundary = np.logical_xor(dil, ero)

            safe  = np.nan_to_num(base, nan=0.0)
            edges = sobel(safe)
            edges[boundary] = 0.0

            if np.isfinite(edges).any():
                e_min = float(np.nanmin(edges))
                e_max = float(np.nanmax(edges))
            else:
                e_min, e_max = 0.0, 0.0

            if e_max > e_min:
                edges = (edges - e_min) / (e_max - e_min)
                e_range = (0.0, 1.0)
            else:
                edges   = np.zeros_like(edges, dtype='float32')
                e_range = (0.0, 1.0)

            self.edge_cache[i] = {"data": edges.astype('float32', copy=False),
                                  "range": e_range}
            display = self.edge_cache[i]["data"]
            rmin, rmax = self.edge_cache[i]["range"]
        else:
            display = im
            rmin, rmax = vmin, vmax

        # Reuse the image only if it is still attached to this axes
        reuse_ok = False
        if i in self.images and self.images[i] is not None:
            img = self.images[i]
            try:
                reuse_ok = (getattr(img, "axes", None) is ax)
            except Exception:
                reuse_ok = False

        g = self.global_gamma

        if reuse_ok:
            img.set_data(display)
            img.set_extent([xmin, xmax, ymin, ymax])
            img.set_cmap(self.cmap_mode)
            img.set_norm(mcolors.PowerNorm(gamma=g, vmin=rmin, vmax=rmax))
            # reapply saved relative-contrast for this panel & mode
            self._apply_rel_contrast_state(i, img, rmin, rmax)
            self.images[i] = img
        else:
            self.images[i] = ax.imshow(
                display,
                cmap=self.cmap_mode,
                norm=mcolors.PowerNorm(gamma=g, vmin=rmin, vmax=rmax),
                extent=[xmin, xmax, ymin, ymax],
                origin='upper', zorder=1,
                interpolation='nearest'
            )
            # reapply saved relative-contrast for this panel & mode
            self._apply_rel_contrast_state(i, self.images[i], rmin, rmax)

        # Overlays: primary first (if any), then additional overlays
        try:
            if getattr(self, "shp_primary", None) is not None:
                self.shp_primary.plot(
                    ax=ax, facecolor='none', edgecolor=self.shp_primary_color,
                    linewidth=1.2, zorder=2
                )
            for gdf, color in getattr(self, "shp_overlays", []):
                gdf.plot(
                    ax=ax, facecolor='none', edgecolor=color,
                    linewidth=1.0, zorder=2
                )
        except Exception:
            pass

        ax.set_xlim(xmin, xmax)
        ax.set_ylim(ymin, ymax)
        ax.set_xticks([]); ax.set_yticks([])
        ax.set_title(os.path.basename(path), pad=2, y=1.02, fontsize=18, color="#D3D3D3")

        # Build/refresh rectangle selector (non-interactive after draw)
        self.ensure_selectors()

    def ensure_selectors(self):
        # make sure the container exists
        if not hasattr(self, "selectors"):
            self.selectors = []

        # remove old selectors
        for sel in self.selectors:
            try:
                sel.disconnect_events()
            except Exception:
                pass
        self.selectors = []

        # rebuild selectors for all axes
        for ax in self.axes:
            sel = RectangleSelector(
                ax,
                lambda e1, e2, ax=ax: (
                    ax.set_xlim(*sorted([e1.xdata, e2.xdata])),
                    ax.set_ylim(*sorted([e1.ydata, e2.ydata])),
                    self.canvas.draw_idle()
                ),
                useblit=True,
                button=[1],
                spancoords='data',
                props=dict(facecolor='lightcoral', edgecolor='none', alpha=0.3),
                interactive=False  # prevent post-draw drag/resize
            )
            self.selectors.append(sel)

    # --------------------------- UI Button Row ----------------------------- #
    def add_keep_reject_buttons(self):
        # remove any existing button axes
        self.buttons.clear()
        for i in range(self.n_pan):
            pos = self.axes[i].get_position()
            bw = pos.width * 0.15
            gap = pos.width * 0.05
            sx = pos.x0 + (pos.width - (2*bw + gap)) / 2
            y0 = pos.y0 - 0.08
            ka = self.fig.add_axes([sx, y0, bw, 0.04], facecolor='black')
            kb = self.fig.add_axes([sx + bw + gap, y0, bw, 0.04], facecolor='black')
            b1 = Button(ka, 'Keep', color='green', hovercolor='limegreen')
            b2 = Button(kb, 'Reject', color='red', hovercolor='salmon')
            b1.on_clicked(self.make_cb(i, 'keep'))
            b2.on_clicked(self.make_cb(i, 'reject'))
            self.buttons.extend([b1, b2])

    # ----------------------------- Events --------------------------------- #
    def idx_from_event(self, ev):
        if ev.inaxes in self.axes:
            return self.axes.index(ev.inaxes)
        return getattr(self, 'active_idx', 0)

    def on_motion(self, ev):
        # Track which panel we're over (used by keyboard-only shortcuts)
        if ev.inaxes in self.axes:
            self.active_idx = self.axes.index(ev.inaxes)

    def on_button_press(self, ev):
        # ensure focus & shortcuts are ready
        self._ensure_shortcuts_once()
        self._ensure_focus()

        if ev.inaxes not in self.axes:
            return

        idx = self.axes.index(ev.inaxes)
        self.active_idx = idx
        ax = self.axes[idx]

        # SHIFT + click anywhere inside a panel => full reset (zoom + pan + warp)
        keystr = str(getattr(ev, "key", "") or "").lower()
        if "shift" in keystr:
            self._do_full_reset(idx)
            return

        # Right-click => reset to full-scene zoom, preserving current pan & any warp
        if ev.button == 3:
            L, R, B, T = self.bases[idx]
            dx, dy = self.offsets[idx]
            ax.set_xlim(L + dx, R + dx)
            ax.set_ylim(B + dy, T + dy)
            self.kill_toolbar_pan_only()
            self.canvas.draw_idle()
            return
        # left/middle clicks: just set active axis

    def on_key(self, ev):
        # ensure focus & shortcuts are ready
        self._ensure_shortcuts_once()
        self._ensure_focus()

        key = (ev.key or '').lower()
        idx = self.idx_from_event(ev)
        ax = self.axes[idx]
        data = self.warp_data[idx]

        # SHIFT — keyboard-only fallback (acts same as Shift+click)
        if key == 'shift':
            self._do_full_reset(idx)
            return

        # R — toggle colormap (apply to all panels)
        if key == 'r':
            self.cmap_mode = 'magma' if self.cmap_mode == 'gray' else 'gray'
            for img in getattr(self, "images", {}).values():
                try:
                    if img is not None:
                        img.set_cmap(self.cmap_mode)
                except Exception:
                    pass
            self.ensure_selectors()
            self.kill_toolbar_tools()
            self.canvas.draw_idle()
            return

        # E — toggle Sobel edges globally, preserving global contrast state
        if key == 'e':
            self.global_edge_mode = not self.global_edge_mode

            for i in range(self.n_pan):
                if self.global_edge_mode:
                    # compute & cache edges for panel i if missing or stale
                    base = self.images_data[i]
                    valid = np.isfinite(base)
                    dil = binary_dilation(valid, square(3))
                    ero = binary_erosion(valid, square(3))
                    boundary = np.logical_xor(dil, ero)
                    safe = np.nan_to_num(base, nan=0.0)
                    edges = sobel(safe)
                    edges[boundary] = 0.0

                    if np.isfinite(edges).any():
                        e_min = float(np.nanmin(edges)); e_max = float(np.nanmax(edges))
                    else:
                        e_min, e_max = 0.0, 0.0

                    if e_max > e_min:
                        edges = (edges - e_min) / (e_max - e_min)
                        e_range = (0.0, 1.0)
                    else:
                        edges = np.zeros_like(edges, dtype='float32')
                        e_range = (0.0, 1.0)

                    self.edge_cache[i] = {"data": edges.astype('float32', copy=False),
                                          "range": e_range}

                    try:
                        self.images[i].set_data(self.edge_cache[i]["data"])
                    except Exception:
                        pass
                    dmin, dmax = self.edge_cache[i]["range"]
                    self._apply_rel_contrast_state(i, self.images[i], dmin, dmax)
                else:
                    # restore base
                    try:
                        self.images[i].set_data(self.images_data[i])
                    except Exception:
                        pass
                    dmin, dmax = self.data_ranges.get(i, (None, None))
                    self._apply_rel_contrast_state(i, self.images[i], dmin, dmax)

                # keep current gamma and cmap
                try:
                    vmin, vmax = self.images[i].get_clim()
                    self.images[i].set_norm(mcolors.PowerNorm(gamma=self.global_gamma, vmin=vmin, vmax=vmax))
                    self.images[i].set_cmap(self.cmap_mode)
                except Exception:
                    pass

            self.canvas.draw_idle()
            return

        # P / L — global contrast +5% / -5%, store as relative state and apply to all
        if key in ('p', 'l'):
            c_rel, h_rel = self.global_contrast_rel
            # adjust half-width only (contrast), keep center fixed
            if key == 'p':   # increase contrast
                h_rel *= 0.95
            else:            # decrease contrast
                h_rel /= 0.95
            self.global_contrast_rel = (float(c_rel), float(h_rel))

            # reapply to all visible panels relative to their current mode's range
            for i in range(self.n_pan):
                if self.global_edge_mode and i in self.edge_cache:
                    dmin, dmax = self.edge_cache[i]["range"]
                else:
                    dmin, dmax = self.data_ranges.get(i, (None, None))
                self._apply_rel_contrast_state(i, self.images[i], dmin, dmax)

            self.canvas.draw_idle()
            return

        # O / K — global gamma -20% / +20% (unbounded; tiny floor to avoid 0)
        if key in ('o', 'k'):
            step = 1.2
            self.global_gamma = (self.global_gamma * step) if key == 'k' else (self.global_gamma / step)
            if self.global_gamma <= 0:
                self.global_gamma = 1e-12

            # preserve each panel's current clim while updating gamma
            for i in range(self.n_pan):
                img = self.images[i]
                try:
                    vmin, vmax = img.get_clim()
                except Exception:
                    if self.global_edge_mode and i in self.edge_cache:
                        vmin, vmax = self.edge_cache[i]["range"]
                    else:
                        vmin, vmax = self.data_ranges.get(i, (None, None))
                if vmin is None or vmax is None:
                    continue
                try:
                    img.set_norm(mcolors.PowerNorm(gamma=self.global_gamma, vmin=vmin, vmax=vmax))
                except Exception:
                    pass

            self.canvas.draw_idle()
            return

        # X — skip/defer current scene on active panel
        if key == 'x':
            if self.queue:
                cur = self.current[idx]
                self.queue.append(cur)
                self.current[idx] = self.queue.pop(0)
                # clear state for this panel before drawing the new file
                self.warp_data[idx] = {
                    "src_world": [], "dst_world": [],
                    "src_pix": [],   "dst_pix": [],
                    "tform": None,   "applied": False,
                    "collecting": False, "markers": [], "labels": []
                }
                self.offsets[idx] = [0, 0]
                self.aoi_bounds = None
                self.draw(idx)
                self.canvas.draw_idle()
            return

        # F — toggle fullscreen for the main window
        if key == 'f':
            if self.isFullScreen():
                self.showNormal()
            else:
                self.showFullScreen()
            self._ensure_focus()
            return

        # SPACE — toggle custom pan mode (hides/shows Keep/Reject buttons)
        if key == ' ':
            self.pan_mode = not self.pan_mode
            for b in self.buttons:
                try:
                    b.ax.set_visible(not self.pan_mode)
                except Exception:
                    pass
            self.canvas.draw_idle()
            return

        # 1/2/3 — change base pan speed multiplier
        if key in ('1', '2', '3'):
            self.base_multiplier = {'1': 1, '2': 3, '3': 9}[key]
            self.pan_factor = 0.001 * self.base_multiplier * self.scale_modifier
            return

        # 8/9 — scale modifier for pan speed
        if key == '8':
            self.scale_modifier = max(1e-3, self.scale_modifier / 2)
            self.pan_factor = 0.001 * self.base_multiplier * self.scale_modifier
            return
        if key == '9':
            self.scale_modifier = self.scale_modifier * 2
            self.pan_factor = 0.001 * self.base_multiplier * self.scale_modifier
            return

        # WASD — apply custom pan when in pan_mode (adjusts extent, not zoom)
        if self.pan_mode and key in 'wasd':
            L, R, B, T = self.bases[idx]
            dx, dy = self.offsets[idx]
            sx = (R - L) * self.pan_factor
            sy = (T - B) * self.pan_factor
            if key == 'w': dy += sy
            if key == 's': dy -= sy
            if key == 'a': dx -= sx
            if key == 'd': dx += sx
            self.offsets[idx] = [dx, dy]
            try:
                self.images[idx].set_extent([L + dx, R + dx, B + dy, T + dy])
            except Exception:
                pass
            self.canvas.draw_idle()
            return

        # G — toggle/apply warp collection & application
        if key == 'g':
            if not data['collecting']:
                data['collecting'] = True
                for b in self.buttons:
                    try: b.ax.set_visible(False)
                    except Exception: pass
                banner = self.axes[idx].text(
                    0.5, 0.95, "Warping...", ha='center',
                    transform=self.axes[idx].transAxes, color='white',
                    fontsize=12, zorder=6
                )
                data['banner'] = banner
                self.canvas.draw_idle()
            else:
                data['collecting'] = False
                if 'banner' in data:
                    try: data['banner'].remove()
                    except Exception: pass
                for b in self.buttons:
                    try: b.ax.set_visible(True)
                    except Exception: pass
                if len(data['src_pix']) >= 3 and len(data['dst_pix']) >= 3:
                    src_pts = np.array(data['src_pix'])
                    dst_pts = np.array(data['dst_pix'])
                    tform = estimate_transform('affine', src_pts, dst_pts)
                    data['tform'] = tform
                    warped = skwarp(
                        self.images_data[idx],
                        inverse_map=tform.inverse,
                        output_shape=self.images_data[idx].shape,
                        cval=np.nan, preserve_range=True
                    )
                    try:
                        self.images[idx].set_data(warped)
                    except Exception:
                        pass
                    # clear control-point artifacts
                    for m in data['markers']:
                        try: m.remove()
                        except Exception: pass
                    for l in data['labels']:
                        try: l.remove()
                        except Exception: pass
                    data['markers'].clear(); data['labels'].clear()
                    data['src_world'].clear(); data['dst_world'].clear()
                    data['src_pix'].clear();   data['dst_pix'].clear()
                    # re-plot shapefile overlay (if present)

                    # mark warp as applied before any optional overlay redraw
                    data['applied'] = True

                    # re-plot overlays (primary first), all wrapped safely
                    try:
                        axr = self.axes[idx]
                        if getattr(self, "shp_primary", None) is not None:
                            self.shp_primary.plot(
                                ax=axr, facecolor='none',
                                edgecolor=self.shp_primary_color, linewidth=1.2, zorder=2
                            )
                        for gdf, color in getattr(self, "shp_overlays", []):
                            gdf.plot(
                                ax=axr, facecolor='none',
                                edgecolor=color, linewidth=1.0, zorder=2
                            )
                    except Exception:
                        pass

                    self.axes[idx].set_title(
                        os.path.basename(self.current[idx]),
                        pad=2, y=1.02, fontsize=18
                    )
                    self.canvas.draw_idle()

                else:
                    for m in data['markers']:
                        try: m.remove()
                        except Exception: pass
                    for l in data['labels']:
                        try: l.remove()
                        except Exception: pass
                    data['markers'].clear(); data['labels'].clear()
                    data['src_world'].clear(); data['dst_world'].clear()
                    data['src_pix'].clear();   data['dst_pix'].clear()
                    self.canvas.draw_idle()
            return

        # Record source points (H)
        if data['collecting'] and key == 'h' and ev.inaxes in self.axes:
            xw, yw = ev.xdata, ev.ydata
            if xw is None or yw is None:
                return
            data['src_world'].append((xw, yw))
            col, row = ~self.srctrans[idx] * (xw, yw)
            data['src_pix'].append((col, row))
            mark, = ev.inaxes.plot(xw, yw, 'o', color='red', markersize=6, zorder=5)
            data['markers'].append(mark)
            lbl = ev.inaxes.text(xw, yw, str(len(data['src_pix'])), color='white', fontsize=8, zorder=6)
            data['labels'].append(lbl)
            self.canvas.draw_idle()
            return

        # Record destination points (J)
        if data['collecting'] and key == 'j' and ev.inaxes in self.axes:
            xw, yw = ev.xdata, ev.ydata
            if xw is None or yw is None:
                return
            data['dst_world'].append((xw, yw))
            col, row = ~self.srctrans[idx] * (xw, yw)
            data['dst_pix'].append((col, row))
            mark, = ev.inaxes.plot(xw, yw, 'o', color='green', markersize=6, zorder=5)
            data['markers'].append(mark)
            lbl = ev.inaxes.text(xw, yw, str(len(data['dst_pix'])), color='white', fontsize=8, zorder=6)
            data['labels'].append(lbl)
            self.canvas.draw_idle()
            return

        # BACKSPACE — undo last warp control point
        if key == 'backspace' and data['collecting']:
            if data['markers']:
                try: data['markers'].pop().remove()
                except Exception: pass
            if data['labels']:
                try: data['labels'].pop().remove()
                except Exception: pass
            if len(data['dst_pix']) >= len(data['src_pix']) and data['dst_pix']:
                data['dst_pix'].pop(); data['dst_world'].pop()
            elif data['src_pix']:
                data['src_pix'].pop(); data['src_world'].pop()
            self.canvas.draw_idle()
            return

        # ENTER / RETURN — reset contrast & gamma to original, and turn OFF edges
        if key in ('enter', 'return'):
            # reset global state
            self.global_contrast_rel = (0.0, 1.0)   # full range
            self.global_gamma = 1.0                 # linear
            self.global_edge_mode = False           # base imagery
            try:
                self.edge_cache.clear()             # optional: free cached edges
            except Exception:
                pass

            # push to all panels
            for i in range(self.n_pan):
                # restore base image data (not edges)
                try:
                    self.images[i].set_data(self.images_data[i])
                except Exception:
                    pass

                # reset contrast to the panel's native data range
                dmin, dmax = self.data_ranges.get(i, (None, None))
                self._apply_rel_contrast_state(i, self.images[i], dmin, dmax)

                # reset gamma while preserving the just-applied clim
                try:
                    vmin, vmax = self.images[i].get_clim()
                    self.images[i].set_norm(mcolors.PowerNorm(gamma=self.global_gamma, vmin=vmin, vmax=vmax))
                except Exception:
                    pass

                # keep current colormap (no change requested), just reapply it
                try:
                    self.images[i].set_cmap(self.cmap_mode)
                except Exception:
                    pass

            self.canvas.draw_idle()
            return

    # ------------------------ Keep/Reject callbacks ------------------------ #
    def make_cb(self, idx, act):
        def cb(_event):
            path = self.current[idx]
            ax = self.axes[idx]
            # Save current view limits
            last_xlim = ax.get_xlim(); last_ylim = ax.get_ylim()
            fname = os.path.basename(path)
            dt = parse_datetime_from_filename(path)
            dx, dy = self.offsets[idx]
            data = self.warp_data[idx]
            warp_flag = bool(data.get('tform')) or data.get('applied', False)

            # Compute geodetic azimuth & distance
            L, R, B, T = self.bases[idx]
            midx = (L+R)/2 + dx
            midy = (B+T)/2 + dy
            try:
                lon0, lat0 = warp_transform(self.srccrs[idx], 'EPSG:4326', [(L+R)/2], [(B+T)/2])
                lon1, lat1 = warp_transform(self.srccrs[idx], 'EPSG:4326', [midx], [midy])
                azimuth, _, dist = geod.inv(lon0[0], lat0[0], lon1[0], lat1[0])
            except Exception:
                azimuth, dist = 0.0, 0.0
            # if no movement, force azimuth to 0
            if dx == 0 and dy == 0:
                azimuth = 0.0
            else:
                azimuth = azimuth % 360

            # --- REJECT path: delete file and log -99999, do NOT modify file ---
            if act == 'reject':
                # Log reject first
                vals = [0,0,0,0,0,0]
                if warp_flag and data.get('tform') is not None:
                    p = data['tform'].params
                    vals = [p[0,0], p[0,1], p[0,2], p[1,0], p[1,1], p[1,2]]
                self.log_entries.append((fname, dt, -99999.0, -99999.0, warp_flag, vals))
                self.session['reject'] += 1

                # Try to remove file from disk
                try:
                    os.remove(path)
                except Exception as e:
                    try:
                        QMessageBox.warning(None, "Delete Failed", f"Could not delete {fname}: {e}")
                    except Exception:
                        pass

            else:
                # --- KEEP path: apply pan/warp as needed and log ---
                # Apply pan shift to file (dtype- and nodata-aware; supports uint16)
                if dx or dy:
                    with rasterio.open(path) as src:
                        arr  = src.read()
                        meta = src.meta.copy()
                        oT, crs = src.transform, src.crs
                    nT = Affine(oT.a, oT.b, oT.c+dx, oT.d, oT.e, oT.f+dy)

                    dst_dtype = arr.dtype
                    nd        = _nodata_for_dtype(dst_dtype, meta.get('nodata'))

                    if np.issubdtype(dst_dtype, np.integer):
                        dest = np.full_like(arr, nd, dtype=dst_dtype)
                        for b in range(arr.shape[0]):
                            reproject(
                                source=arr[b],
                                destination=dest[b],
                                src_transform=nT, src_crs=crs,
                                dst_transform=oT,  dst_crs=crs,
                                resampling=Resampling.nearest,
                                src_nodata=nd,     dst_nodata=nd,
                                fill_value=nd,
                                num_threads=REPROJECT_THREADS
                            )
                        meta.update(transform=oT, dtype=dst_dtype, nodata=nd)
                    else:
                        dest = np.full_like(arr, np.nan, dtype='float32')
                        for b in range(arr.shape[0]):
                            reproject(
                                source=arr[b],
                                destination=dest[b],
                                src_transform=nT, src_crs=crs,
                                dst_transform=oT,  dst_crs=crs,
                                resampling=Resampling.nearest,
                                src_nodata=meta.get('nodata'), dst_nodata=np.nan,
                                fill_value=np.nan,
                                num_threads=REPROJECT_THREADS
                            )
                        meta.update(transform=oT, dtype='float32', nodata=np.nan)

                    tmp = path + '.tmp'
                    with rasterio.open(tmp, 'w', **meta) as dst:
                        dst.write(dest)
                    os.replace(tmp, path)

                # Apply warp to file (dtype- and nodata-aware; supports uint16)
                if warp_flag and data.get('tform') is not None:
                    with rasterio.open(path) as src:
                        band1 = src.read(1)
                        m1    = src.meta.copy()

                    dst_dtype = band1.dtype
                    nd        = _nodata_for_dtype(dst_dtype, m1.get('nodata'))

                    warped_band = skwarp(band1.astype(np.float32),
                                         inverse_map=data['tform'].inverse,
                                         output_shape=band1.shape,
                                         cval=np.nan, preserve_range=True)

                    if np.issubdtype(dst_dtype, np.integer):
                        info         = np.iinfo(dst_dtype)
                        warped_band  = np.where(np.isnan(warped_band), nd, np.rint(warped_band))
                        warped_band  = np.clip(warped_band, info.min, info.max).astype(dst_dtype)
                        m1.update(dtype=dst_dtype, nodata=nd)
                    else:
                        warped_band = warped_band.astype('float32')
                        m1.update(dtype='float32', nodata=np.nan)

                    tmp2 = path + '.warp.tmp'
                    with rasterio.open(tmp2, 'w', **m1) as dst2:
                        dst2.write(warped_band, 1)
                    os.replace(tmp2, path)

                # Collect warp parameters for logging
                if warp_flag and data.get('tform') is not None:
                    p = data['tform'].params
                    vals = [p[0,0], p[0,1], p[0,2], p[1,0], p[1,1], p[1,2]]
                else:
                    vals = [0,0,0,0,0,0]

                self.log_entries.append((fname, dt, azimuth, dist, warp_flag, vals))
                if warp_flag:
                    self.session['warp'] += 1
                elif dist > 0:
                    self.session['geo'] += 1
                else:
                    self.session['as_is'] += 1

            # Persist log immediately (durable even if user closes right away)
            self.flush_log()

            # Update UI and advance queue
            proc = sum(self.session.values())
            self.prog.set_text(
                f"Session: {proc}/{self.total} ({proc/self.total*100:.1f}%)\n"
                f"As-Is: {self.session['as_is']} ({(self.session['as_is']/proc*100 if proc else 0):.1f}%)   "
                f"Transform:    {self.session['geo']} ({(self.session['geo']/proc*100 if proc else 0):.1f}%)   "
                f"Warp:   {self.session['warp']} ({(self.session['warp']/proc*100 if proc else 0):.1f}%)   "
                f"Reject: {self.session['reject']} ({(self.session['reject']/proc*100 if proc else 0):.1f}%)"
            )

            if self.queue:
                self.current[idx] = self.queue.pop(0)
                self.draw(idx)
                ax.set_xlim(*last_xlim)
                ax.set_ylim(*last_ylim)
                self.canvas.draw_idle()
            else:
                # blank this axis
                ax.clear(); ax.set_facecolor('black'); ax.set_axis_off()
                ax.text(0.5, 0.5, 'NO MORE IMAGES', ha='center', va='center',
                        transform=ax.transAxes, color='#555555', fontsize=16)
                try:
                    self.buttons[2*idx].ax.set_visible(False)
                    self.buttons[2*idx+1].ax.set_visible(False)
                except Exception:
                    pass
                self.canvas.draw_idle()

            if proc == self.total:
                self.flush_log()
                self.show_final_summary()
        return cb

    # -------------------------- Final Summary ------------------------------ #
    def show_final_summary(self):
        # recompute totals across entire log_entries
        counts = {'as_is': 0, 'geo': 0, 'warp': 0, 'reject': 0}
        for _, _, az, dist, wf, _ in self.log_entries:
            if az == -99999.0 and dist == -99999.0:
                counts['reject'] += 1
            elif wf:
                counts['warp'] += 1
            elif dist > 0:
                counts['geo'] += 1
            else:
                counts['as_is'] += 1
        total_log = sum(counts.values())

        def pct(k):
            return (counts[k] / total_log * 100) if total_log else 0.0

        # Modal dialog with embedded Matplotlib figure (to mirror original summary look)
        dlg = QDialog(self)
        dlg.setWindowTitle("Final Summary")

        try:
            from PyQt5 import QtWidgets, QtCore, QtGui
        except Exception:
            pass
        dlg.setWindowFlags(dlg.windowFlags() | QtCore.Qt.WindowContextHelpButtonHint)

        dlg.resize(900, 1600)
        lay = QVBoxLayout(dlg)

        fig = Figure(dpi=150)
        fig.patch.set_facecolor('black')
        cvs = FigureCanvas(fig)
        lay.addWidget(cvs)

        shapes1 = "▛▟ ▞▚ ▟▛ ▞▚"
        shapes2 = "▟ ▛ ▙"
        fig.text(0.5, 0.88, shapes1, ha='center', color='white',
                 fontsize=24, fontfamily='DejaVu Sans Mono', fontweight='bold')
        fig.text(0.5, 0.83, shapes2, ha='center', color='red',
                 fontsize=24, fontfamily='DejaVu Sans Mono', fontweight='bold')
        fig.text(0.5, 0.75, 'NASA JPL Thermal Viewer', ha='center', fontsize=24, color='white')
        fig.text(0.5, 0.70, 'FINAL SUMMARY',           ha='center', fontsize=20, color='#D3D3D3')

        y = 0.60
        for key in ['as_is', 'geo', 'warp', 'reject']:
            label = key.replace('_', ' ').title()
            fig.text(0.5, y, f"{label}: {counts[key]} ({pct(key):.1f}%)",
                     ha='center', fontsize=18, color='#D3D3D3')
            y -= 0.06

        fig.text(0.5, 0.25, 'Close this window to finish.', ha='center', fontsize=16, color='#D3D3D3')

        art = r'''

                      .'.
                      |o|
                     .'o'.
                     |.-.|
                     '   '
                      ( )
                       )
                      ( )

                  ____
             .-'"p 8o"'-. 
          .-'8888P'Y.`Y[ ' `-. 
        ,']88888b.J8oo_      '`. 

        '''
        fig.text(0.5, 0.20, art, ha='center', va='top',
                 family='DejaVu Sans Mono', fontsize=8, color='white')

        cvs.draw()

        # --- Custom help dialog with blinking cursor, 5s suspense, growth animation ---
        # --- Simple, fixed-size Help dialog (fits full text; 5s -> 3s pause; +0.5s after line 1) ---
        class HelpDialog(QtWidgets.QDialog):
            def __init__(self, parent=None):
                super().__init__(parent)
                self.setWindowTitle("Help")
                self.setModal(True)

                # Big enough to show all lines without geometry animations
                self.resize(370, 280)
                self.setMinimumSize(370, 280)
                self.setStyleSheet("background-color: black;")

                frame = QtWidgets.QFrame(self)
                frame.setStyleSheet(
                    "QFrame { border: 2px solid #3a3a3a; border-radius: 10px; "
                    "background-color: #101010; }"
                )
                outer = QtWidgets.QVBoxLayout(self)
                outer.setContentsMargins(14, 14, 14, 14)
                outer.addWidget(frame)

                inner = QtWidgets.QVBoxLayout(frame)
                inner.setContentsMargins(16, 16, 12, 12)

                self.label = QtWidgets.QLabel("", self)
                self.label.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop)
                self.label.setWordWrap(True)
                self.label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
                self.label.setStyleSheet(
                    "font-family: 'Lucida Console','DejaVu Sans Mono'; font-size: 14pt; color: #C7F7C1;"
                )
                sp = self.label.sizePolicy()
                sp.setVerticalStretch(1)
                self.label.setSizePolicy(sp)
                inner.addWidget(self.label, 1)

                btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close, parent=self)
                btns.rejected.connect(self.close)
                inner.addWidget(btns, 0)

                # Script
                self.messages = [
                    "Hello there.",
                    "Inquisitive you are...",
                    "",  # blank line
                    (
                        ".-.. .. ...- . / .-.. --- -. --. /.- -. -.."
                        "/ .--. .-. --- ... .--. . .-. / .- -.. / .- "
                        "... - .-. .- /.--. . .-. / .- ... .--. . .-. .-"
                    )
                ]
                self.display_text = ""
                self.current_line = 0
                self.current_char = 0

                # Blinking cursor for 3s first
                self.cursor_on = True
                self.cursor_char = "▌"
                self._render_with_cursor()

                self.blink_timer = QtCore.QTimer(self)
                self.blink_timer.setInterval(400)
                self.blink_timer.timeout.connect(self._blink)
                self.blink_timer.start()

                # after 3 seconds, start typewriter
                QtCore.QTimer.singleShot(3000, self._start_typewriter)

                self._pause_for_surprise = False
                self._paused_after_first_line = False  # NEW: 0.5s pause after first line

            # ----- cursor & typing -----
            def _blink(self):
                self.cursor_on = not self.cursor_on
                self._render_with_cursor()

            def _render_with_cursor(self):
                cursor = self.cursor_char if self.cursor_on else " "
                self.label.setText(self.display_text + cursor)

            def _start_typewriter(self):
                self.blink_timer.stop()
                self.cursor_on = True
                self.type_timer = QtCore.QTimer(self)
                self.type_timer.setInterval(45)  # typing speed (ms per char)
                self.type_timer.timeout.connect(self._type_step)
                self.type_timer.start()

            def _type_step(self):
                # Pause before line index 3 ("morse") 3s
                if self.current_line == 3 and not self._pause_for_surprise:
                    self._pause_for_surprise = True
                    self.type_timer.stop()
                    self.blink_timer.start()
                    QtCore.QTimer.singleShot(3000, self._resume_after_surprise)
                    return

                if self.current_line >= len(self.messages):
                    # finished typing; resume blink
                    self.type_timer.stop()
                    self.cursor_on = True
                    self.blink_timer.start()
                    return

                line = self.messages[self.current_line]
                if self.current_char < len(line):
                    self.display_text += line[self.current_char]
                    self.current_char += 1
                    self._render_with_cursor()
                else:
                    # end of line: add newline and advance
                    self.display_text += "\n"
                    self.current_line += 1
                    self.current_char = 0
                    self._render_with_cursor()

                    # NEW: 0.5s pause after completing the first line (index 0)
                    if self.current_line == 1 and not self._paused_after_first_line:
                        self._paused_after_first_line = True
                        self.type_timer.stop()
                        self.blink_timer.start()
                        QtCore.QTimer.singleShot(500, self._resume_after_first_line)
                        return

            def _resume_after_first_line(self):
                self.blink_timer.stop()
                self.cursor_on = True
                self.type_timer.start()

            def _resume_after_surprise(self):
                self.blink_timer.stop()
                self.cursor_on = True
                self.type_timer.start()

        # Make the '?' open the HelpDialog and keep it open until closed
        class _WhatsThisFilter(QtCore.QObject):
            def __init__(self, parent_dialog):
                super().__init__(parent_dialog)
                self._dlg = parent_dialog

            def eventFilter(self, obj, event):
                if obj is self._dlg and event.type() == QtCore.QEvent.EnterWhatsThisMode:
                    QtWidgets.QWhatsThis.leaveWhatsThisMode()  # exit what's-this mode first
                    help_dlg = HelpDialog(self._dlg)
                    # center over parent dialog
                    size = help_dlg.sizeHint()
                    center_pt = self._dlg.frameGeometry().center() - QtCore.QPoint(size.width() // 2,
                                                                                   size.height() // 2)
                    help_dlg.move(center_pt)
                    help_dlg.exec_()  # stays open until user closes
                    return True
                return False

        wt = _WhatsThisFilter(dlg)
        dlg.installEventFilter(wt)

        dlg.exec_()
        self.close()  # close main after summary

# ---------------------------------------------------------------------------
# Entry point: splash, mode selection, and app launch
# ---------------------------------------------------------------------------

def _build_arg_parser():
    p = argparse.ArgumentParser(description="NASA JPL Thermal Viewer (PyQt5)")
    p.add_argument("--gdal-cache-mb", type=int, default=None,
                   help="Override GDAL block cache size in MB (RAM). Default via GEOVIEWER_GDAL_CACHE_MB or 1024.")
    p.add_argument("--threads", default=None,
                   help="Number of threads for rasterio.reproject (int) or 'all'. Default via GEOVIEWER_REPROJECT_THREADS or 'all'.")
    return p

def main():
    # Parse perf flags early so we can create the GDAL env before opening rasters
    parser = _build_arg_parser()
    # Allow running inside interactive environments that inject args
    known, _ = parser.parse_known_args()

    cache_mb = known.gdal_cache_mb if known.gdal_cache_mb is not None else DEFAULT_GDAL_CACHE_MB

    global REPROJE
    self.close()  # close main after summary

# ---------------------------------------------------------------------------
# Entry point: splash, mode selection, and app launch
# ---------------------------------------------------------------------------

def main():
    app = QApplication(sys.argv)

    # Splash
    splash = SplashDialog()
    splash.exec_()

    # Gather TIFFs
    all_files = sorted(glob.glob("*.tif"))
    if not all_files:
        QMessageBox.information(None, "Info", "No TIFFs in working directory.")
        return 0

    # ── NEW: If no log yet, ask the user to define the datetime substring + format
    # (We use the first filename as the example; they can paste the exact substring.)
    if not os.path.exists(LOG_FILE):
        example = os.path.basename(all_files[0])
        dlg = DatePatternDialog(example)
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            # Store results globally for parse_datetime_from_filename()
            global USER_DT_REGEX, USER_DT_STRPTIME
            USER_DT_REGEX = dlg.result_regex
            USER_DT_STRPTIME = dlg.result_strptime
        else:
            QMessageBox.warning(
                None, "Proceeding without a pattern",
                "No geolocation log found and no pattern provided.\n"
                "I'll attempt to infer datetimes from the start of each filename."
            )

    # Read log and ask for mode if log exists
    processed_dts, processed_files, log_entries = read_log()

    auto_mode = False
    if processed_dts:
        mb = QMessageBox()
        mb.setIcon(QMessageBox.Question)
        mb.setWindowTitle("Existing Log Detected")
        mb.setText("GeolocationLog.csv found.\n\nYes = Manual append\nNo  = Auto-geocorrect then exit")
        yes = mb.addButton("Yes (Manual)", QMessageBox.YesRole)
        no  = mb.addButton("No (Auto)",    QMessageBox.NoRole)
        mb.exec_()
        auto_mode = (mb.clickedButton() == no)

    if auto_mode:
        count = auto_geocorrect(all_files, processed_dts, processed_files, log_entries)
        QMessageBox.information(None, "Auto-Geocorrect", f"Auto-geocorrect applied to {count} files. Exiting.")
        return 0

    # Manual session
    win = ThermalViewerQt(all_files, processed_files, log_entries)
    win.show()

    return app.exec_()

if __name__ == "__main__":
    sys.exit(main())