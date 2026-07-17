#!/usr/bin/env python3
"""
ECOSTRESS LST/QC mask tuning application.

Place this script in the folder that contains scene subfolders, then run:
    python QC.py

The application recursively finds matching LST and QC GeoTIFFs, chooses a
representative scene for interactive tuning, previews the masked LST in real
time, and writes the tuned mask to every matched LST scene.
"""

import gc
import glob
import math
import os
import re
import stat
import sys
import tempfile
import time
import traceback
from collections import defaultdict
from dataclasses import dataclass, replace

import numpy as np
import rasterio
from affine import Affine
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.warp import calculate_default_transform, reproject

# Let Qt and Matplotlib use the same high-DPI behavior as GeoViewer.
os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")
os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

from PyQt5 import QtCore, QtGui, QtWidgets

try:
    QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)
except Exception:
    pass

import matplotlib

try:
    matplotlib.use("Qt5Agg")
except Exception:
    matplotlib.use("QtAgg")

import matplotlib.pyplot as plt
from matplotlib import colors as mcolors
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

try:
    from scipy import ndimage as _scipy_ndimage
except Exception:
    _scipy_ndimage = None

try:
    import fiona
    from fiona.crs import CRS as FionaCRS
    from fiona.transform import transform_geom
except Exception:
    fiona = None
    FionaCRS = None
    transform_geom = None


SCRIPT_FOLDER = os.path.dirname(os.path.abspath(__file__))
# All missing raster values use IEEE NaN.
FILL_VALUE = np.nan
PREVIEW_MAX_SIZE = 1600
REPRESENTATIVE_SCAN_SIZE = 600
DEFAULT_APPEND_SUFFIX = "_qc"

# GUI temperature controls are expressed in degrees Celsius.
# ECOSTRESS LST rasters are assumed to be stored in Kelvin, so masks compare
# against a Celsius view of the data while output files preserve original LST units.
KELVIN_TO_CELSIUS_OFFSET = 273.15
TEMPERATURE_MASK_MIN_C = -10.0
TEMPERATURE_MASK_MAX_C = 50.0
DEFAULT_COLOR_MIN_C = TEMPERATURE_MASK_MIN_C
DEFAULT_COLOR_MAX_C = TEMPERATURE_MASK_MAX_C

QC_BIT_LABELS = ("00", "01", "10", "11")
QC_BIT_MEANINGS = ("best", "good", "fair", "poor")
COLORMAPS = (
    "magma",
    "inferno",
    "plasma",
    "viridis",
    "turbo",
    "gray",
    "cividis",
    "hot",
    "coolwarm",
)

NAN_COLOR_OPTIONS = (
    ("White", "#FFFFFF"),
    ("Black", "#000000"),
    ("Dark gray", "#202124"),
    ("Light gray", "#BFC5D0"),
    ("Bright cyan", "#00FFFF"),
    ("Magenta", "#FF00FF"),
    ("Yellow", "#FFFF00"),
    ("Red", "#FF3B30"),
    ("Blue", "#007AFF"),
    ("Green", "#34C759"),
)
DEFAULT_NAN_COLOR = NAN_COLOR_OPTIONS[0][1]
DEFAULT_LOW_COVER_SUFFIX = "_nan"
DEFAULT_MAX_MISSING_PERCENT = 100.0
DEFAULT_FILTERED_OUTPUT_FOLDER = "QC_output"
DEFAULT_LOW_COVER_OUTPUT_FOLDER = "nan_thresholded_scenes"
FIXED_OUTPUT_FOLDERS = (DEFAULT_FILTERED_OUTPUT_FOLDER, DEFAULT_LOW_COVER_OUTPUT_FOLDER)

# Post-batch CRS conversion. The suffix spelling follows the requested
# filename convention exactly.
DEFAULT_REPROJECT_SUFFIX = "_EPSG4326"
# Requested folder name intentionally keeps the user-provided spelling.
DEFAULT_REPROJECT_APPEND_FOLDER = "reprojected_EPSG4326"
REPROJECT_TARGET_CRS_LABEL = "EPSG:4326"
REPROJECT_BATCH_RETRIES = 15
REPROJECT_BATCH_RETRY_WAIT_SEC = 0.75


def _parse_reproject_threads(value):
    cpus = os.cpu_count() or 1
    try:
        text = str(value).strip().lower()
    except Exception:
        text = "-1"
    if text in ("all", "auto", "max"):
        return max(1, cpus)
    match = re.fullmatch(r"(all|max|auto)\s*-\s*(\d+)", text)
    if match:
        return max(1, cpus - int(match.group(2)))
    try:
        n = int(text)
        return max(1, cpus + n) if n < 0 else max(1, n)
    except Exception:
        return max(1, cpus - 1)


REPROJECT_THREADS = _parse_reproject_threads(os.environ.get("QC_REPROJECT_THREADS", "-1"))


@dataclass(frozen=True)
class ScenePair:
    lst_path: str
    qc_path: str
    timestamp: str = ""
    key: str = ""
    score: float = 0.0
    valid_fraction: float = 0.0
    nan_fraction: float = 1.0
    quality_counts: tuple = (0, 0, 0, 0)
    confidence_counts: tuple = (0, 0, 0, 0)


@dataclass(frozen=True)
class MaskParams:
    quality_max: int = 1
    confidence_max: int = 1
    qc_logic: str = "and"
    dilation_radius: int = 0
    temperature_enabled: bool = False
    temperature_min_c: float = TEMPERATURE_MASK_MIN_C
    temperature_max_c: float = TEMPERATURE_MASK_MAX_C


@dataclass(frozen=True)
class BatchOptions:
    mode: str = "append"
    suffix: str = DEFAULT_APPEND_SUFFIX
    root_folder: str = SCRIPT_FOLDER
    QC_output_folder: str = DEFAULT_FILTERED_OUTPUT_FOLDER
    max_missing_percent: float = DEFAULT_MAX_MISSING_PERCENT
    nan_threshold_action: str = "keep"
    nan_threshold_suffix: str = DEFAULT_LOW_COVER_SUFFIX
    nan_threshold_output_folder: str = DEFAULT_LOW_COVER_OUTPUT_FOLDER


def extract_timestamp_ecostress(filename):
    """Extract an ECOSTRESS timestamp from current and older filename styles."""
    name = os.path.basename(filename)

    match = re.search(r"_(\d{8}T\d{6})_", name)
    if match:
        return match.group(1)

    match = re.search(r"doy(\d{13})", name, flags=re.IGNORECASE)
    return match.group(1) if match else ""


def classify_ecostress_file(filename):
    """Return 'LST', 'QC', or None based on filename tokens."""
    stem = os.path.splitext(os.path.basename(filename))[0].upper()
    if stem.endswith("_LST") or re.search(r"(^|_)LST(?=_DOY)", stem):
        return "LST"
    if stem.endswith("_QC") or re.search(r"(^|_)QC(?=_DOY)", stem):
        return "QC"
    return None


def scene_pairing_key(filename):
    """Return a key that should match between one LST file and one QC file."""
    stem = os.path.splitext(os.path.basename(filename))[0]

    key = re.sub(r"_(LST|QC)$", "", stem, flags=re.IGNORECASE)
    key = re.sub(r"(^|_)(LST|QC)(?=_doy)", r"\1DATA", key, flags=re.IGNORECASE)
    return key.upper()


def extract_tile_id(filename):
    """Extract a tile ID such as 18SUG for fallback pairing."""
    name = os.path.basename(filename).upper()
    match = re.search(r"_(\d{2}[A-Z]{3})_", name)
    if match:
        return match.group(1)

    match = re.search(r"_(\d{2}N)(?:_|$)", name)
    return match.group(1) if match else ""


def _safe_common_path_score(path_a, path_b):
    try:
        common = os.path.commonpath([os.path.abspath(path_a), os.path.abspath(path_b)])
        return len(common)
    except Exception:
        return 0


def _is_inside_fixed_output_folder(path, root_folder=None):
    """Return True when path is already inside one of this app's output folders."""
    try:
        root = os.path.abspath(root_folder or SCRIPT_FOLDER)
        rel = os.path.relpath(os.path.abspath(path), root)
        parts = [part.lower() for part in rel.split(os.sep) if part and part != os.curdir]
    except Exception:
        parts = [part.lower() for part in os.path.abspath(path).split(os.sep)]
    fixed = {name.lower() for name in FIXED_OUTPUT_FOLDERS}
    return any(part in fixed for part in parts)


def _choose_best_qc_for_lst(lst_path, qc_candidates):
    if not qc_candidates:
        return None

    lst_dir = os.path.normcase(os.path.abspath(os.path.dirname(lst_path)))

    def score(qc_path):
        qc_dir = os.path.normcase(os.path.abspath(os.path.dirname(qc_path)))
        same_folder = 100000 if qc_dir == lst_dir else 0
        same_parent = 10000 if os.path.dirname(qc_dir) == os.path.dirname(lst_dir) else 0
        return same_folder + same_parent + _safe_common_path_score(lst_path, qc_path)

    return max(sorted(qc_candidates), key=score)


def discover_scene_pairs(root_folder):
    """Recursively find LST/QC GeoTIFF pairs under root_folder."""
    patterns = (
        os.path.join(root_folder, "**", "*.tif"),
        os.path.join(root_folder, "**", "*.tiff"),
    )
    all_files = []
    for pattern in patterns:
        all_files.extend(glob.glob(pattern, recursive=True))
    all_files = sorted(set(os.path.abspath(p) for p in all_files))
    # Do not re-ingest products already written by this GUI. This matters
    # especially in overwrite mode, where filtered scenes are moved into
    # QC outputs are moved into QC_output and NaN-thresholded scenes are moved into nan_thresholded_scenes.
    all_files = [p for p in all_files if not _is_inside_fixed_output_folder(p, root_folder)]

    lst_files = []
    qc_files = []
    for path in all_files:
        file_type = classify_ecostress_file(path)
        if file_type == "LST":
            lst_files.append(path)
        elif file_type == "QC":
            qc_files.append(path)

    qc_by_key = defaultdict(list)
    qc_by_timestamp_tile = defaultdict(list)
    qc_by_timestamp = defaultdict(list)
    qc_by_folder = defaultdict(list)
    for qc_path in qc_files:
        key = scene_pairing_key(qc_path)
        ts = extract_timestamp_ecostress(qc_path)
        tile = extract_tile_id(qc_path)
        qc_by_key[key].append(qc_path)
        if ts and tile:
            qc_by_timestamp_tile[(ts, tile)].append(qc_path)
        if ts:
            qc_by_timestamp[ts].append(qc_path)
        qc_by_folder[os.path.normcase(os.path.abspath(os.path.dirname(qc_path)))].append(qc_path)

    pairs = []
    seen = set()
    for lst_path in sorted(lst_files):
        key = scene_pairing_key(lst_path)
        ts = extract_timestamp_ecostress(lst_path)
        tile = extract_tile_id(lst_path)
        folder = os.path.normcase(os.path.abspath(os.path.dirname(lst_path)))

        candidates = list(qc_by_key.get(key, []))
        if not candidates and ts and tile:
            candidates = list(qc_by_timestamp_tile.get((ts, tile), []))
        if not candidates and ts:
            same_ts = list(qc_by_timestamp.get(ts, []))
            if len(same_ts) == 1:
                candidates = same_ts
        if not candidates:
            same_folder_qc = list(qc_by_folder.get(folder, []))
            same_folder_lst = [
                p for p in lst_files
                if os.path.normcase(os.path.abspath(os.path.dirname(p))) == folder
            ]
            if len(same_folder_qc) == 1 and len(same_folder_lst) == 1:
                candidates = same_folder_qc

        qc_path = _choose_best_qc_for_lst(lst_path, candidates)
        if not qc_path:
            continue

        pair_id = (os.path.normcase(lst_path), os.path.normcase(qc_path))
        if pair_id in seen:
            continue
        seen.add(pair_id)
        pairs.append(ScenePair(lst_path=lst_path, qc_path=qc_path, timestamp=ts, key=key))

    return pairs


def _finite_lst_array(array, nodata=None):
    arr = array.astype("float32", copy=True)
    if nodata is not None and np.isfinite(nodata):
        arr[arr == float(nodata)] = np.nan
    arr[~np.isfinite(arr)] = np.nan
    return arr


def lst_kelvin_to_celsius(array):
    """Return a Celsius view/copy of an LST array stored in Kelvin."""
    return array.astype("float32", copy=False) - np.float32(KELVIN_TO_CELSIUS_OFFSET)


def _valid_qc_array(array, nodata=None):
    values = np.asarray(array)
    missing = ~np.isfinite(values)
    if nodata is not None and np.isfinite(nodata):
        missing |= values == nodata
    qc = values.astype("float32", copy=True)
    qc[missing] = np.nan
    return qc


def _scaled_shape(width, height, max_size):
    longest = max(int(width), int(height), 1)
    scale = min(1.0, float(max_size) / float(longest))
    out_width = max(1, int(round(width * scale)))
    out_height = max(1, int(round(height * scale)))
    return out_height, out_width


def _transforms_match(transform_a, transform_b):
    try:
        return transform_a.almost_equals(transform_b)
    except Exception:
        return transform_a == transform_b


def _datasets_share_grid(src_a, src_b):
    if src_a.width != src_b.width or src_a.height != src_b.height:
        return False
    if src_a.crs and src_b.crs and src_a.crs != src_b.crs:
        return False
    return _transforms_match(src_a.transform, src_b.transform)


def _read_qc_to_grid(qc_src, out_shape, out_transform, out_crs, fallback_resampling=Resampling.nearest):
    out_height, out_width = out_shape
    qc_dst = np.full((out_height, out_width), np.nan, dtype="float32")
    src_nodata = qc_src.nodata if qc_src.nodata is not None else None

    can_reproject = qc_src.crs is not None and out_crs is not None
    if can_reproject:
        reproject(
            source=rasterio.band(qc_src, 1),
            destination=qc_dst,
            src_transform=qc_src.transform,
            src_crs=qc_src.crs,
            src_nodata=src_nodata,
            dst_transform=out_transform,
            dst_crs=out_crs,
            dst_nodata=np.nan,
            resampling=Resampling.nearest,
        )
        return _valid_qc_array(qc_dst, np.nan)

    # Last-resort fallback for rasters without CRS metadata.
    qc = qc_src.read(1, out_shape=out_shape, resampling=fallback_resampling)
    return _valid_qc_array(qc, qc_src.nodata)


def read_pair_preview(pair, max_size=PREVIEW_MAX_SIZE):
    """Read an LST preview and QC preview aligned to the preview LST grid."""
    with rasterio.open(pair.lst_path) as lst_src, rasterio.open(pair.qc_path) as qc_src:
        out_height, out_width = _scaled_shape(lst_src.width, lst_src.height, max_size)
        lst = lst_src.read(
            1,
            out_shape=(out_height, out_width),
            resampling=Resampling.nearest,
        )
        lst = _finite_lst_array(lst, lst_src.nodata)

        scale_x = float(lst_src.width) / float(out_width)
        scale_y = float(lst_src.height) / float(out_height)
        preview_transform = lst_src.transform * Affine.scale(scale_x, scale_y)

        if _datasets_share_grid(lst_src, qc_src):
            qc = qc_src.read(
                1,
                out_shape=(out_height, out_width),
                resampling=Resampling.nearest,
            )
            qc = _valid_qc_array(qc, qc_src.nodata)
        else:
            qc = _read_qc_to_grid(qc_src, (out_height, out_width), preview_transform, lst_src.crs)

        return lst, qc


def read_scene_full(pair):
    """Read full-resolution LST and QC aligned to the LST grid."""
    with rasterio.open(pair.lst_path) as lst_src, rasterio.open(pair.qc_path) as qc_src:
        lst = _finite_lst_array(lst_src.read(1), lst_src.nodata)

        if _datasets_share_grid(lst_src, qc_src):
            qc = _valid_qc_array(qc_src.read(1), qc_src.nodata)
        else:
            qc = _read_qc_to_grid(
                qc_src,
                (lst_src.height, lst_src.width),
                lst_src.transform,
                lst_src.crs,
            )

        profile = lst_src.profile.copy()
        profile.update(
            driver="GTiff",
            count=1,
            dtype="float32",
            nodata=np.nan,
            compress=profile.get("compress") or "deflate",
        )
        return lst, qc, profile


def qc_bit_arrays(qc_array):
    valid_qc = np.isfinite(qc_array)
    qc_int = np.where(valid_qc, qc_array, 0).astype("int32", copy=False)
    quality_bits = qc_int & 0b11
    confidence_bits = (qc_int >> 14) & 0b11
    return quality_bits, confidence_bits, valid_qc


def binary_dilate(mask, radius):
    """Dilate a boolean invalid-pixel mask by a square pixel neighborhood."""
    radius = int(max(0, radius))
    if radius <= 0 or not np.any(mask):
        return mask

    if _scipy_ndimage is not None:
        structure = np.ones((2 * radius + 1, 2 * radius + 1), dtype=bool)
        return _scipy_ndimage.binary_dilation(mask, structure=structure)

    result = np.zeros(mask.shape, dtype=bool)
    height, width = mask.shape
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            src_y0 = max(0, -dy)
            src_y1 = min(height, height - dy)
            dst_y0 = max(0, dy)
            dst_y1 = min(height, height + dy)
            src_x0 = max(0, -dx)
            src_x1 = min(width, width - dx)
            dst_x0 = max(0, dx)
            dst_x1 = min(width, width + dx)
            if src_y0 < src_y1 and src_x0 < src_x1:
                result[dst_y0:dst_y1, dst_x0:dst_x1] |= mask[src_y0:src_y1, src_x0:src_x1]
    return result


def build_keep_mask(lst_array, qc_array, params):
    """Return True for pixels retained by QC, dilation, and temperature rules.

    Dilation is applied to all invalid pixels from the pre-temperature mask,
    including old/pre-existing NaNs from the input LST and newly rejected QC
    pixels. This matches the old-script idea of dilating around ~combined_mask,
    rather than only dilating pixels newly removed by the GUI bit filter.
    """
    quality_bits, confidence_bits, valid_qc = qc_bit_arrays(qc_array)
    valid_lst = np.isfinite(lst_array)

    quality_ok = quality_bits <= int(params.quality_max)
    confidence_ok = confidence_bits <= int(params.confidence_max)
    if str(params.qc_logic).lower() == "or":
        qc_ok = quality_ok | confidence_ok
    else:
        qc_ok = quality_ok & confidence_ok

    # Pre-temperature keep mask. Any False pixel here is an invalid seed for
    # dilation, including old cloud-mask NaNs already present in the LST raster.
    keep = valid_lst & valid_qc & qc_ok

    if int(params.dilation_radius) > 0:
        invalid_seed = ~keep
        dilated_invalid = binary_dilate(invalid_seed, int(params.dilation_radius))
        keep = keep & ~dilated_invalid

    # Temperature limits are applied after QC/dilation, following the old script.
    if params.temperature_enabled:
        lst_c = lst_kelvin_to_celsius(lst_array)
        keep &= lst_c >= float(params.temperature_min_c)
        keep &= lst_c <= float(params.temperature_max_c)

    return keep


def apply_mask_to_lst(lst_array, qc_array, params):
    keep = build_keep_mask(lst_array, qc_array, params)
    return np.where(keep, lst_array, np.nan).astype("float32", copy=False), keep


def _normalized_entropy(counts):
    total = float(np.sum(counts))
    if total <= 0:
        return 0.0
    probs = np.asarray(counts, dtype=float) / total
    probs = probs[probs > 0]
    if probs.size <= 1:
        return 0.0
    return float(-(probs * np.log(probs)).sum() / math.log(4.0))


def representative_score(pair):
    """Score a scene by QC class breadth and valid LST coverage."""
    try:
        lst, qc = read_pair_preview(pair, max_size=REPRESENTATIVE_SCAN_SIZE)
        quality_bits, confidence_bits, valid_qc = qc_bit_arrays(qc)
        valid_lst = np.isfinite(lst)
        valid = valid_lst & valid_qc
        total_pixels = int(lst.size)
        valid_count = int(np.count_nonzero(valid))
        if total_pixels <= 0 or valid_count <= 0:
            return replace(pair, score=-1.0)

        q_counts = np.bincount(quality_bits[valid].ravel(), minlength=4)[:4]
        c_counts = np.bincount(confidence_bits[valid].ravel(), minlength=4)[:4]
        q_present = int(np.count_nonzero(q_counts >= max(5, valid_count * 0.001)))
        c_present = int(np.count_nonzero(c_counts >= max(5, valid_count * 0.001)))
        q_entropy = _normalized_entropy(q_counts)
        c_entropy = _normalized_entropy(c_counts)
        valid_fraction = valid_count / float(total_pixels)
        nan_fraction = 1.0 - valid_fraction

        # Favor images with all four bit classes present, balanced good/bad QC,
        # and enough valid LST pixels to make visual tuning meaningful.
        q_bad = float(q_counts[2] + q_counts[3]) / float(valid_count)
        c_bad = float(c_counts[2] + c_counts[3]) / float(valid_count)
        q_bad_balance = 1.0 - min(1.0, abs(q_bad - 0.35) / 0.35)
        c_bad_balance = 1.0 - min(1.0, abs(c_bad - 0.35) / 0.35)
        breadth = (q_present + c_present) + 2.0 * (q_entropy + c_entropy)
        balance = q_bad_balance + c_bad_balance
        score = breadth + balance + 2.5 * valid_fraction

        return replace(
            pair,
            score=float(score),
            valid_fraction=float(valid_fraction),
            nan_fraction=float(nan_fraction),
            quality_counts=tuple(int(v) for v in q_counts),
            confidence_counts=tuple(int(v) for v in c_counts),
        )
    except Exception:
        return replace(pair, score=-1.0)


def score_representative_pairs(pairs, progress_callback=None):
    scored = []
    total = len(pairs)
    for index, pair in enumerate(pairs, 1):
        scored_pair = representative_score(pair)
        scored.append(scored_pair)
        if progress_callback:
            progress_callback(index, total, scored_pair)
    scored.sort(key=lambda p: p.score, reverse=True)
    return scored


def bit_threshold_text(value):
    value = int(max(0, min(3, value)))
    labels = ", ".join(QC_BIT_LABELS[:value + 1])
    return f"keep {labels}"


def pct_text(value):
    return f"{value * 100.0:.1f}%"


def file_display_name(pair):
    rel = os.path.basename(pair.lst_path)
    parent = os.path.basename(os.path.dirname(pair.lst_path))
    if parent:
        rel = os.path.join(parent, rel)
    return rel


class FloatSlider(QtWidgets.QWidget):
    valueChanged = QtCore.pyqtSignal(float)

    def __init__(self, title, minimum=0.0, maximum=1.0, value=0.0, decimals=2, parent=None):
        super().__init__(parent)
        self._minimum = float(minimum)
        self._maximum = float(maximum)
        self._decimals = int(decimals)
        self._block_emit = False
        self._block_edit_update = False

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        row = QtWidgets.QHBoxLayout()
        self.title_label = QtWidgets.QLabel(title)

        # Editable numeric value box. This replaces the old read-only label so
        # users can type exact values such as 90 for the NaN-thresholded threshold.
        self.value_edit = QtWidgets.QLineEdit()
        self.value_edit.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        self.value_edit.setFixedWidth(82)
        self.value_edit.setToolTip("Type an exact value, then press Enter or click away.")
        self.value_edit.editingFinished.connect(self._edit_finished)
        self.value_edit.returnPressed.connect(self._edit_finished)
        self.value_label = self.value_edit  # Backward-compatible name used by older code.

        row.addWidget(self.title_label, 1)
        row.addWidget(self.value_edit)
        layout.addLayout(row)

        self.slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.valueChanged.connect(self._slider_changed)
        layout.addWidget(self.slider)
        self.set_value(value, emit=False)

    def set_range(self, minimum, maximum):
        minimum = float(minimum)
        maximum = float(maximum)
        if not math.isfinite(minimum):
            minimum = 0.0
        if not math.isfinite(maximum):
            maximum = minimum + 1.0
        if maximum <= minimum:
            maximum = minimum + 1.0
        current = self.value()
        self._minimum = minimum
        self._maximum = maximum
        self.set_value(min(max(current, minimum), maximum), emit=False)

    def set_value(self, value, emit=True):
        value = float(value)
        if not math.isfinite(value):
            value = self._minimum
        value = min(max(value, self._minimum), self._maximum)
        if self._maximum <= self._minimum:
            raw = 0
        else:
            raw = int(round((value - self._minimum) / (self._maximum - self._minimum) * 1000.0))
        raw = max(0, min(1000, raw))

        # Block slider callbacks while synchronizing from typed input or code,
        # then emit exactly once if requested.
        old_block = self._block_emit
        self._block_emit = True
        self.slider.setValue(raw)
        self._block_emit = old_block
        self._update_edit(value)
        if emit and not self._block_emit:
            self.valueChanged.emit(value)

    def value(self):
        raw = self.slider.value() / 1000.0
        return self._minimum + raw * (self._maximum - self._minimum)

    def _format_value(self, value):
        return f"{float(value):.{self._decimals}f}"

    def _update_edit(self, value=None):
        if value is None:
            value = self.value()
        if self.value_edit.hasFocus() and not self._block_edit_update:
            # Do not fight the user's cursor while they are typing.
            return
        self._block_edit_update = True
        self.value_edit.setText(self._format_value(value))
        self._block_edit_update = False

    def _update_label(self, value=None):
        # Compatibility wrapper for older code paths.
        self._update_edit(value)

    def _parse_edit_value(self):
        text = self.value_edit.text().strip()
        if not text:
            return self.value()
        # Be forgiving if a user types a percent sign or Celsius marker.
        for token in ("%", "°C", "°", "C", "c"):
            text = text.replace(token, "")
        text = text.strip()
        try:
            return float(text)
        except Exception:
            return self.value()

    def _edit_finished(self):
        if self._block_edit_update:
            return
        value = self._parse_edit_value()
        self.set_value(value, emit=True)

    def _slider_changed(self):
        value = self.value()
        self._update_edit(value)
        if not self._block_emit:
            self.valueChanged.emit(value)




class BitSnapSlider(QtWidgets.QWidget):
    """A four-stop QC bit slider with ticks and labels exactly on each snap point."""

    valueChanged = QtCore.pyqtSignal(int)

    def __init__(self, value=1, parent=None):
        super().__init__(parent)
        self._value = int(max(0, min(3, value)))
        self._dragging = False
        self._margin = 36
        self.setMinimumHeight(66)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setMouseTracking(True)

    def value(self):
        return int(self._value)

    def set_value(self, value, emit=True):
        value = int(max(0, min(3, round(float(value)))))
        if value == self._value:
            self.update()
            return
        self._value = value
        self.update()
        if emit:
            self.valueChanged.emit(self._value)

    def _usable_margin(self):
        # Keep enough room for the edge labels while still allowing the leftmost
        # and rightmost snap points to read visually as the slider endpoints.
        return min(self._margin, max(18, self.width() // 7))

    def _track_geometry(self):
        margin = self._usable_margin()
        left = float(margin)
        right = float(max(margin + 1, self.width() - margin))
        y = 17.0
        return left, right, y

    def _x_for_value(self, value):
        left, right, _ = self._track_geometry()
        return left + (right - left) * (float(value) / 3.0)

    def _value_for_x(self, x):
        left, right, _ = self._track_geometry()
        if right <= left:
            return 0
        ratio = (float(x) - left) / (right - left)
        return int(max(0, min(3, round(ratio * 3.0))))

    def _theme_colors(self):
        pal = self.palette()
        base = pal.color(QtGui.QPalette.Window).name()
        text = pal.color(QtGui.QPalette.WindowText).name()
        highlight = pal.color(QtGui.QPalette.Highlight).name()
        # Use a soft neutral groove for both dark and light modes.
        groove = "#CBD5E1" if base.upper() in ("#F8F9FA", "#FFFFFF") else "#3C4043"
        handle_border = "#1D4ED8" if base.upper() in ("#F8F9FA", "#FFFFFF") else "#AECBF9"
        handle = highlight
        return groove, text, highlight, handle, handle_border

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)

        left, right, y = self._track_geometry()
        span = right - left
        if span <= 0:
            return

        groove, label_color, active_color, handle_color, handle_border = self._theme_colors()
        groove_rect = QtCore.QRectF(left, y - 3.0, span, 6.0)
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(QtGui.QColor(groove))
        painter.drawRoundedRect(groove_rect, 3.0, 3.0)

        active_rect = QtCore.QRectF(left, y - 3.0, self._x_for_value(self._value) - left, 6.0)
        painter.setBrush(QtGui.QColor(active_color))
        painter.drawRoundedRect(active_rect, 3.0, 3.0)

        tick_pen = QtGui.QPen(QtGui.QColor(label_color))
        tick_pen.setWidth(1)
        painter.setPen(tick_pen)
        for i, (bit_label, meaning) in enumerate(zip(QC_BIT_LABELS, QC_BIT_MEANINGS)):
            x = self._x_for_value(i)
            painter.drawLine(QtCore.QPointF(x, y + 8.0), QtCore.QPointF(x, y + 15.0))

            label_rect = QtCore.QRectF(x - 35.0, y + 18.0, 70.0, 36.0)
            # Edge labels are still tied to the tick x-position, but nudged inside
            # the widget bounds so the text does not get clipped.
            if label_rect.left() < 0:
                label_rect.moveLeft(0)
            if label_rect.right() > self.width():
                label_rect.moveRight(self.width())

            font = painter.font()
            font.setPointSize(8)
            painter.setFont(font)
            painter.setPen(QtGui.QColor(label_color))
            painter.drawText(label_rect, QtCore.Qt.AlignHCenter | QtCore.Qt.AlignTop, f'{bit_label}\n{meaning}')

        handle_x = self._x_for_value(self._value)
        painter.setPen(QtGui.QPen(QtGui.QColor(handle_border), 1))
        painter.setBrush(QtGui.QColor(handle_color))
        painter.drawEllipse(QtCore.QPointF(handle_x, y), 8.5, 8.5)

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self._dragging = True
            self.setFocus(QtCore.Qt.MouseFocusReason)
            self.set_value(self._value_for_x(event.x()))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging:
            self.set_value(self._value_for_x(event.x()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton and self._dragging:
            self._dragging = False
            self.set_value(self._value_for_x(event.x()))
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (QtCore.Qt.Key_Left, QtCore.Qt.Key_Down):
            self.set_value(self._value - 1)
            event.accept()
            return
        if event.key() in (QtCore.Qt.Key_Right, QtCore.Qt.Key_Up):
            self.set_value(self._value + 1)
            event.accept()
            return
        if event.key() == QtCore.Qt.Key_Home:
            self.set_value(0)
            event.accept()
            return
        if event.key() == QtCore.Qt.Key_End:
            self.set_value(3)
            event.accept()
            return
        super().keyPressEvent(event)


class DiscreteBitSlider(QtWidgets.QWidget):
    valueChanged = QtCore.pyqtSignal(int)

    def __init__(self, title, value=1, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        row = QtWidgets.QHBoxLayout()
        self.title_label = QtWidgets.QLabel(title)
        self.value_label = QtWidgets.QLabel("")
        self.value_label.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        row.addWidget(self.title_label, 1)
        row.addWidget(self.value_label)
        layout.addLayout(row)

        self.slider = BitSnapSlider(value=value)
        self.slider.valueChanged.connect(self._changed)
        layout.addWidget(self.slider)
        self._changed(self.slider.value())

    def value(self):
        return int(self.slider.value())

    def set_value(self, value):
        self.slider.set_value(int(max(0, min(3, value))))

    def _changed(self, value):
        self.value_label.setText(bit_threshold_text(value))
        self.valueChanged.emit(int(value))



class LstCanvas(FigureCanvas):
    viewChanged = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        self.figure = Figure(figsize=(7, 6), dpi=100, facecolor="#111111")
        self.ax = self.figure.add_subplot(111)
        super().__init__(self.figure)
        self.setParent(parent)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setFocus()

        self.image = None
        self.colorbar = None
        self._last_shape = None
        self._home_xlim = None
        self._home_ylim = None

        # Pan state. Store mouse coordinates in screen pixels and derive the
        # camera move from the original x/y limits. This avoids the jitter caused
        # by repeatedly converting mouse locations through a data transform that
        # is changing during the drag.
        self._pan_start = None
        self._pending_xlim = None
        self._pending_ylim = None
        self._is_dragging = False

        # Coalesce mouse-move events to roughly 60 FPS. Trackpads and high-refresh
        # monitors can fire hundreds of motion events per second; redrawing the
        # whole Matplotlib canvas for every event makes dragging feel glitchy.
        self._pan_draw_timer = QtCore.QTimer(self)
        self._pan_draw_timer.setSingleShot(True)
        self._pan_draw_timer.setInterval(16)
        self._pan_draw_timer.timeout.connect(self._flush_pan_draw)

        self._light_mode = False
        self._canvas_bg = "#111111"
        self._viewer_fg = "#DDDDDD"
        self._reverse_scroll_zoom = False

        # Pixel-inspection state. These arrays are refreshed from the current
        # preview so a click can report the displayed Celsius value plus the
        # original QC quality/confidence classes.
        self._pixel_image_c = None
        self._pixel_qc = None
        self._click_pixel_tolerance = 5.0

        self._style_axes()
        self.mpl_connect("scroll_event", self._on_scroll)
        self.mpl_connect("button_press_event", self._on_button_press)
        self.mpl_connect("button_release_event", self._on_button_release)
        self.mpl_connect("motion_notify_event", self._on_motion)

    def _style_axes(self):
        try:
            self.figure.set_facecolor(self._canvas_bg)
        except Exception:
            pass
        self.ax.set_facecolor(self._canvas_bg)
        self.ax.set_xticks([])
        self.ax.set_yticks([])
        # Fill the full viewer panel while preserving square raster pixels.
        # adjustable="datalim" expands the data/camera limits instead of shrinking
        # the axes box to the TIFF aspect ratio. This keeps pan/zoom active across
        # the whole viewer panel without stretching the TIFF image.
        self.ax.set_aspect("equal", adjustable="datalim", anchor="C")
        self.ax.set_autoscale_on(False)
        for spine in self.ax.spines.values():
            spine.set_visible(False)

    def set_theme(self, light_mode=False):
        """Switch the Matplotlib viewer between dark and light GUI themes."""
        self._light_mode = bool(light_mode)
        if self._light_mode:
            self._canvas_bg = "#F8F9FA"
            self._viewer_fg = "#202124"
        else:
            self._canvas_bg = "#111111"
            self._viewer_fg = "#DDDDDD"

        try:
            self.figure.set_facecolor(self._canvas_bg)
            self.ax.set_facecolor(self._canvas_bg)
            if self.colorbar is not None:
                self.colorbar.ax.tick_params(colors=self._viewer_fg, labelsize=8)
                self.colorbar.set_label("LST (°C)", color=self._viewer_fg, fontsize=8)
                self.colorbar.ax.set_facecolor(self._canvas_bg)
        except Exception:
            pass
        self.draw_idle()

    def set_reverse_scroll_zoom(self, enabled=False):
        """If enabled, invert scroll-wheel zoom direction for user preference."""
        self._reverse_scroll_zoom = bool(enabled)

    def set_pixel_lookup(self, image_c_array=None, qc_array=None):
        """Store arrays used by click-to-inspect pixel popups."""
        self._pixel_image_c = image_c_array
        self._pixel_qc = qc_array

    def _apply_full_panel_layout(self):
        """Make the image axes occupy the full viewer area, leaving room for colorbar."""
        try:
            # Axes uses nearly the entire canvas; colorbar gets a slim fixed strip.
            self.ax.set_position([0.0, 0.0, 0.905, 1.0])
            if self.colorbar is not None:
                self.colorbar.ax.set_position([0.925, 0.035, 0.025, 0.93])
        except Exception:
            pass

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_full_panel_layout()
        self.draw_idle()

    def clear_scene(self):
        self._pan_draw_timer.stop()
        self.figure.clear()
        self.ax = self.figure.add_subplot(111)
        self._style_axes()
        self.image = None
        self.colorbar = None
        self._last_shape = None
        self._home_xlim = None
        self._home_ylim = None
        self._pan_start = None
        self._pending_xlim = None
        self._pending_ylim = None
        self._is_dragging = False
        self.draw_idle()

    def set_scene(self, image_array, cmap_name, vmin, vmax, nan_color=DEFAULT_NAN_COLOR, reset_view=False, qc_array=None):
        self.set_pixel_lookup(image_array, qc_array)
        cmap = plt.get_cmap(cmap_name).copy()
        cmap.set_bad(str(nan_color or DEFAULT_NAN_COLOR))
        masked = np.ma.masked_invalid(image_array)
        if not np.isfinite(vmin) or not np.isfinite(vmax) or vmax <= vmin:
            finite = image_array[np.isfinite(image_array)]
            if finite.size:
                vmin, vmax = np.nanpercentile(finite, [2.0, 98.0])
                if vmax <= vmin:
                    vmax = vmin + 1.0
            else:
                vmin, vmax = 0.0, 1.0
        norm = mcolors.PowerNorm(gamma=1.0, vmin=float(vmin), vmax=float(vmax))

        needs_new_image = self.image is None or self._last_shape != image_array.shape
        if needs_new_image:
            self._pan_draw_timer.stop()
            self.figure.clear()
            self.ax = self.figure.add_subplot(111)
            self._style_axes()
            self.image = self.ax.imshow(
                masked,
                cmap=cmap,
                norm=norm,
                interpolation="nearest",
                origin="upper",
            )
            self.image.set_clip_on(True)
            self.ax.set_aspect("equal", adjustable="datalim", anchor="C")
            self.ax.set_autoscale_on(False)
            self._last_shape = image_array.shape
            self.colorbar = self.figure.colorbar(self.image, ax=self.ax, fraction=0.035, pad=0.02)
            self.colorbar.ax.tick_params(colors=self._viewer_fg, labelsize=8)
            self.colorbar.set_label("LST (°C)", color=self._viewer_fg, fontsize=8)
            # Manual full-panel layout. The axes no longer letterboxes to the TIFF
            # aspect ratio, so mouse pan/zoom works throughout the viewer panel.
            self._apply_full_panel_layout()
        else:
            self.image.set_data(masked)
            self.image.set_cmap(cmap)
            self.image.set_norm(norm)
            self.ax.set_aspect("equal", adjustable="datalim", anchor="C")
            self.ax.set_autoscale_on(False)
            if self.colorbar is not None:
                self.colorbar.update_normal(self.image)
                self.colorbar.ax.tick_params(colors=self._viewer_fg, labelsize=8)
                self.colorbar.set_label("LST (°C)", color=self._viewer_fg, fontsize=8)
            self._apply_full_panel_layout()

        if reset_view or self._home_xlim is None or self._home_ylim is None:
            self._set_home_view(image_array.shape)
            self._apply_home_view()

        self.draw_idle()

    def _set_home_view(self, shape):
        height, width = shape
        self._home_xlim = (-0.5, max(0.5, width - 0.5))
        self._home_ylim = (max(0.5, height - 0.5), -0.5)

    def _apply_home_view(self):
        if self._home_xlim is None or self._home_ylim is None:
            return
        self.ax.set_xlim(*self._home_xlim)
        self.ax.set_ylim(*self._home_ylim)

    def _event_to_data(self, event):
        """Convert any canvas mouse location to data coordinates.

        Matplotlib normally only reports xdata/ydata inside the axes. For this
        viewer, panning and zooming should continue to work in blank canvas space
        around the image, so we use the axes transform directly and do not clamp
        the result to the TIFF extent.
        """
        if event is None or event.x is None or event.y is None:
            return None
        if self.colorbar is not None and event.inaxes is self.colorbar.ax:
            return None
        try:
            xdata, ydata = self.ax.transData.inverted().transform((event.x, event.y))
            if not np.isfinite(xdata) or not np.isfinite(ydata):
                return None
            return float(xdata), float(ydata)
        except Exception:
            return None

    def _axes_pixel_size(self):
        try:
            bbox = self.ax.bbox
            width = float(max(1.0, bbox.width))
            height = float(max(1.0, bbox.height))
            return width, height
        except Exception:
            return float(max(1, self.width())), float(max(1, self.height()))

    def _schedule_pan_draw(self):
        if not self._pan_draw_timer.isActive():
            self._pan_draw_timer.start()

    def _flush_pan_draw(self):
        if self._pending_xlim is None or self._pending_ylim is None:
            return
        self.ax.set_xlim(*self._pending_xlim)
        self.ax.set_ylim(*self._pending_ylim)
        self._pending_xlim = None
        self._pending_ylim = None
        self.draw_idle()
        self.viewChanged.emit()

    def _on_scroll(self, event):
        if self.image is None:
            return
        point = self._event_to_data(event)
        if point is None:
            return
        xdata, ydata = point

        base_scale = 1.2
        zoom_in = event.button == "up"
        if self._reverse_scroll_zoom:
            zoom_in = not zoom_in
        scale = 1.0 / base_scale if zoom_in else base_scale

        cur_xlim = self.ax.get_xlim()
        cur_ylim = self.ax.get_ylim()
        width = cur_xlim[1] - cur_xlim[0]
        height = cur_ylim[1] - cur_ylim[0]
        if width == 0 or height == 0:
            return

        new_width = width * scale
        new_height = height * scale
        relx = (cur_xlim[1] - xdata) / width
        rely = (cur_ylim[1] - ydata) / height

        # No boundary clamp: the camera may move into blank space around the TIFF.
        self.ax.set_xlim(xdata - new_width * (1.0 - relx), xdata + new_width * relx)
        self.ax.set_ylim(ydata - new_height * (1.0 - rely), ydata + new_height * rely)
        self.draw_idle()
        self.viewChanged.emit()

    def _on_button_press(self, event):
        if self.image is None:
            return
        if event is None or event.x is None or event.y is None:
            return
        if self.colorbar is not None and event.inaxes is self.colorbar.ax:
            return
        if event.button == 3:
            self.reset_view()
            return
        if event.button != 1:
            return

        axes_w, axes_h = self._axes_pixel_size()
        self._pan_start = {
            "px": float(event.x),
            "py": float(event.y),
            "xlim": self.ax.get_xlim(),
            "ylim": self.ax.get_ylim(),
            "axes_w": axes_w,
            "axes_h": axes_h,
        }
        self._pending_xlim = None
        self._pending_ylim = None
        self._is_dragging = True

    def _on_motion(self, event):
        if self.image is None or self._pan_start is None:
            return
        if event is None or event.x is None or event.y is None:
            return

        x0, x1 = self._pan_start["xlim"]
        y0, y1 = self._pan_start["ylim"]
        axes_w = max(1.0, float(self._pan_start.get("axes_w", 1.0)))
        axes_h = max(1.0, float(self._pan_start.get("axes_h", 1.0)))

        # Pixel-space deltas are stable throughout a drag. This avoids using the
        # current data transform, which changes every time the limits change and
        # can cause visible jitter or rubber-band motion.
        dx_px = float(event.x) - float(self._pan_start["px"])
        dy_px = float(event.y) - float(self._pan_start["py"])
        if math.hypot(dx_px, dy_px) <= self._click_pixel_tolerance:
            self._pending_xlim = None
            self._pending_ylim = None
            return
        dx_data = dx_px * ((x1 - x0) / axes_w)
        dy_data = dy_px * ((y1 - y0) / axes_h)

        self._pending_xlim = (x0 - dx_data, x1 - dx_data)
        self._pending_ylim = (y0 - dy_data, y1 - dy_data)
        self._schedule_pan_draw()

    def _on_button_release(self, event):
        click_candidate = False
        if self._pan_start is not None and event is not None and event.x is not None and event.y is not None:
            dx_px = float(event.x) - float(self._pan_start.get("px", event.x))
            dy_px = float(event.y) - float(self._pan_start.get("py", event.y))
            click_candidate = math.hypot(dx_px, dy_px) <= self._click_pixel_tolerance

        if self._pan_start is not None:
            self._pan_draw_timer.stop()
            if click_candidate:
                self._pending_xlim = None
                self._pending_ylim = None
                self._show_pixel_popup(event)
            else:
                self._flush_pan_draw()
        self._pan_start = None
        self._is_dragging = False

    def _format_qc_bits_for_popup(self, qc_value, shift=0):
        if not np.isfinite(qc_value):
            return "No QC value"
        try:
            qc_int = int(qc_value)
        except Exception:
            return "No QC value"
        bit_value = (qc_int >> int(shift)) & 0b11
        return f"{QC_BIT_LABELS[bit_value]} ({QC_BIT_MEANINGS[bit_value]})"

    def _show_pixel_popup(self, event):
        point = self._event_to_data(event)
        if point is None or self._pixel_image_c is None:
            return
        xdata, ydata = point
        col = int(round(xdata))
        row = int(round(ydata))
        image = self._pixel_image_c
        if row < 0 or col < 0 or row >= image.shape[0] or col >= image.shape[1]:
            return

        temp_value = image[row, col]
        if np.isfinite(temp_value):
            temp_text = f"{float(temp_value):.2f} °C"
        else:
            temp_text = "NaN / masked"

        quality_text = "No QC value"
        confidence_text = "No QC value"
        qc = self._pixel_qc
        if qc is not None and getattr(qc, "shape", None) == image.shape:
            qc_value = qc[row, col]
            quality_text = self._format_qc_bits_for_popup(qc_value, shift=0)
            confidence_text = self._format_qc_bits_for_popup(qc_value, shift=14)

        popup_text = (
            f"Temperature: {temp_text}\n"
            f"Quality: {quality_text}\n"
            f"Confidence: {confidence_text}"
        )
        try:
            QtWidgets.QToolTip.showText(QtGui.QCursor.pos(), popup_text, self)
        except Exception:
            QtWidgets.QMessageBox.information(self, "Pixel info", popup_text)

    def reset_view(self):
        if self.image is None:
            return
        self._pan_draw_timer.stop()
        self._pending_xlim = None
        self._pending_ylim = None
        self._pan_start = None
        self._is_dragging = False
        if self._home_xlim is None or self._home_ylim is None:
            array = self.image.get_array()
            self._set_home_view(array.shape)
        self._apply_home_view()
        self._apply_full_panel_layout()
        self.draw_idle()
        self.viewChanged.emit()


class PairScoringWorker(QtCore.QObject):
    progress = QtCore.pyqtSignal(int, int, object)
    finished = QtCore.pyqtSignal(list)

    def __init__(self, pairs):
        super().__init__()
        self.pairs = pairs

    @QtCore.pyqtSlot()
    def run(self):
        scored = []
        total = len(self.pairs)
        for index, pair in enumerate(self.pairs, 1):
            scored_pair = representative_score(pair)
            scored.append(scored_pair)
            self.progress.emit(index, total, scored_pair)
        scored.sort(key=lambda p: p.score, reverse=True)
        self.finished.emit(scored)


def unique_appended_path(lst_path, suffix):
    folder = os.path.dirname(lst_path)
    stem, ext = os.path.splitext(os.path.basename(lst_path))
    suffix = str(suffix or DEFAULT_APPEND_SUFFIX).strip()
    if not suffix:
        suffix = DEFAULT_APPEND_SUFFIX
    if not suffix.startswith("_"):
        suffix = "_" + suffix
    base = os.path.join(folder, stem + suffix)
    candidate = base + ext
    counter = 2
    while os.path.exists(candidate):
        candidate = f"{base}_{counter}{ext}"
        counter += 1
    return candidate


def _safe_output_folder_name(value, default_folder):
    """Return a safe relative output folder name/path for appended products."""
    folder = str(value or default_folder).strip().strip("/\\")
    if not folder:
        folder = default_folder
    folder = os.path.normpath(folder)
    if folder in (".", "..") or os.path.isabs(folder) or folder.startswith(".." + os.sep):
        folder = os.path.basename(folder) or default_folder
    return folder


def _relative_output_subfolder(src_path, root_folder):
    """Preserve the input folder layout beneath root_folder inside output folders."""
    root_folder = os.path.abspath(root_folder or SCRIPT_FOLDER)
    src_dir = os.path.dirname(os.path.abspath(src_path))
    try:
        rel_dir = os.path.relpath(src_dir, root_folder)
    except Exception:
        rel_dir = ""
    if rel_dir in (".", os.curdir):
        return ""
    if rel_dir.startswith("..") or os.path.isabs(rel_dir):
        return ""
    # Avoid nesting output folders inside themselves if a user manually selects
    # an output folder as the scan root.
    parts = [p for p in rel_dir.split(os.sep) if p]
    fixed = {name.lower() for name in FIXED_OUTPUT_FOLDERS}
    if parts and parts[0].lower() in fixed:
        parts = parts[1:]
    return os.path.join(*parts) if parts else ""


def output_path_in_fixed_folder(
    src_path,
    root_folder,
    output_folder_name,
    suffix="",
    unique=False,
    default_suffix=DEFAULT_APPEND_SUFFIX,
):
    """Build an output path in a fixed product folder.

    Append mode uses a suffix and unique naming. Overwrite/move mode uses the
    original filename in the fixed output folder and replaces any previous file
    of the same name there.
    """
    root_folder = os.path.abspath(root_folder or SCRIPT_FOLDER)
    src_path = os.path.abspath(src_path)
    output_folder_name = _safe_output_folder_name(output_folder_name, output_folder_name)
    rel_dir = _relative_output_subfolder(src_path, root_folder)
    out_dir = os.path.join(root_folder, output_folder_name, rel_dir)
    os.makedirs(out_dir, exist_ok=True)

    stem, ext = os.path.splitext(os.path.basename(src_path))
    suffix = str(suffix or "").strip()
    if suffix:
        suffix = _safe_suffix(suffix, default_suffix)
    candidate = os.path.join(out_dir, stem + suffix + ext)
    if not unique:
        return candidate

    base = os.path.splitext(candidate)[0]
    counter = 2
    while os.path.exists(candidate):
        candidate = f"{base}_{counter}{ext}"
        counter += 1
    return candidate


def unique_appended_path_in_output_folder(
    lst_path,
    suffix,
    root_folder,
    output_folder_name,
    default_output_folder=DEFAULT_FILTERED_OUTPUT_FOLDER,
):
    """Backward-compatible wrapper for append-mode output paths."""
    return output_path_in_fixed_folder(
        lst_path,
        root_folder,
        _safe_output_folder_name(output_folder_name, default_output_folder),
        suffix=suffix,
        unique=True,
        default_suffix=DEFAULT_APPEND_SUFFIX,
    )


def _safe_suffix(value, default_suffix):
    suffix = str(value or default_suffix).strip()
    if not suffix:
        suffix = default_suffix
    if not suffix.startswith("_"):
        suffix = "_" + suffix
    return suffix


def _delete_file_if_exists(path):
    if path and os.path.exists(path):
        os.remove(path)


def _write_array_to_tif_path(out_path, output, profile):
    """Write raster output atomically to out_path."""
    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    stem, ext = os.path.splitext(os.path.basename(out_path))
    fd, tmp_path = tempfile.mkstemp(
        suffix=ext or ".tif",
        prefix=stem + ".qcfilter_tmp_",
        dir=os.path.dirname(out_path) or ".",
    )
    os.close(fd)
    try:
        with rasterio.open(tmp_path, "w", **profile) as dst:
            dst.write(output, 1)
        os.replace(tmp_path, out_path)
        tmp_path = None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
    return out_path


def _remove_original_after_move(src_path, out_path):
    """Remove the source LST after writing its moved replacement elsewhere."""
    try:
        same = os.path.normcase(os.path.abspath(src_path)) == os.path.normcase(os.path.abspath(out_path))
    except Exception:
        same = False
    if not same:
        _delete_file_if_exists(src_path)


def write_masked_scene(pair, params, options):
    """Write, skip, delete, or move one scene based on mask and coverage.

    Normal kept scenes always go to QC_output.
    NaN-thresholded kept scenes always go to nan_thresholded_scenes.

    Append mode keeps the original source file and writes a suffixed new file.
    Overwrite mode writes the masked file using the original filename in the
    fixed output folder, then removes the original LST from its source folder.
    """
    lst, qc, profile = read_scene_full(pair)
    masked, keep = apply_mask_to_lst(lst, qc, params)
    output = np.where(np.isfinite(masked), masked, np.nan).astype("float32", copy=False)

    valid_fraction = float(np.count_nonzero(keep)) / float(keep.size) if keep.size else 0.0
    missing_percent = 100.0 * (1.0 - valid_fraction)
    max_missing = float(max(0.0, min(100.0, options.max_missing_percent)))
    is_nan_thresholded = missing_percent > max_missing + 1e-9

    source_removed = False

    if is_nan_thresholded:
        if str(options.nan_threshold_action).lower() == "delete":
            if options.mode == "overwrite":
                # The application writes/deletes LST products only. QC source files
                # are left in place so the user does not lose metadata/QA inputs.
                _delete_file_if_exists(pair.lst_path)
                return {
                    "status": "low_deleted",
                    "out_path": pair.lst_path,
                    "valid_fraction": valid_fraction,
                    "missing_percent": missing_percent,
                    "nan_thresholded": True,
                    "source_removed": True,
                }
            return {
                "status": "low_skipped",
                "out_path": None,
                "valid_fraction": valid_fraction,
                "missing_percent": missing_percent,
                "nan_thresholded": True,
                "source_removed": False,
            }

        if options.mode == "overwrite":
            out_path = output_path_in_fixed_folder(
                pair.lst_path,
                options.root_folder,
                DEFAULT_LOW_COVER_OUTPUT_FOLDER,
                suffix="",
                unique=False,
            )
            _write_array_to_tif_path(out_path, output, profile)
            _remove_original_after_move(pair.lst_path, out_path)
            source_removed = True
        else:
            out_path = output_path_in_fixed_folder(
                pair.lst_path,
                options.root_folder,
                DEFAULT_LOW_COVER_OUTPUT_FOLDER,
                suffix=_safe_suffix(options.nan_threshold_suffix, DEFAULT_LOW_COVER_SUFFIX),
                unique=True,
                default_suffix=DEFAULT_LOW_COVER_SUFFIX,
            )
            _write_array_to_tif_path(out_path, output, profile)

        return {
            "status": "low_kept",
            "out_path": out_path,
            "valid_fraction": valid_fraction,
            "missing_percent": missing_percent,
            "nan_thresholded": True,
            "source_removed": source_removed,
        }

    if options.mode == "overwrite":
        out_path = output_path_in_fixed_folder(
            pair.lst_path,
            options.root_folder,
            DEFAULT_FILTERED_OUTPUT_FOLDER,
            suffix="",
            unique=False,
        )
        _write_array_to_tif_path(out_path, output, profile)
        _remove_original_after_move(pair.lst_path, out_path)
        source_removed = True
    else:
        out_path = output_path_in_fixed_folder(
            pair.lst_path,
            options.root_folder,
            DEFAULT_FILTERED_OUTPUT_FOLDER,
            suffix=_safe_suffix(options.suffix, DEFAULT_APPEND_SUFFIX),
            unique=True,
            default_suffix=DEFAULT_APPEND_SUFFIX,
        )
        _write_array_to_tif_path(out_path, output, profile)

    return {
        "status": "written",
        "out_path": out_path,
        "valid_fraction": valid_fraction,
        "missing_percent": missing_percent,
        "nan_thresholded": False,
        "source_removed": source_removed,
    }

# ---------------------------------------------------------------------------
# EPSG:4326 reprojection helpers
# ---------------------------------------------------------------------------

def _unique_sorted_paths(paths):
    seen = set()
    out = []
    for path in paths:
        abs_path = os.path.abspath(str(path or ""))
        key = os.path.normcase(abs_path)
        if key in seen or not os.path.isfile(abs_path):
            continue
        name = os.path.basename(abs_path).lower()
        if name.startswith("reproj_") or ".qcfilter_tmp" in name:
            continue
        seen.add(key)
        out.append(abs_path)
    return sorted(out, key=lambda p: (os.path.dirname(p).lower(), os.path.basename(p).lower()))


def discover_reproject_file_paths(folder=None):
    folder = os.path.abspath(folder or SCRIPT_FOLDER)
    raster_paths = _unique_sorted_paths(
        glob.glob(os.path.join(folder, "**", "*.tif"), recursive=True)
        + glob.glob(os.path.join(folder, "**", "*.tiff"), recursive=True)
    )
    shapefile_paths = _unique_sorted_paths(
        glob.glob(os.path.join(folder, "**", "*.shp"), recursive=True)
    )
    return raster_paths, shapefile_paths


def _reproject_crs_label(crs_obj):
    if crs_obj is None:
        return "None"
    try:
        crs = CRS.from_user_input(crs_obj)
    except Exception:
        return str(crs_obj)
    auth = crs.to_authority()
    if auth:
        return f"{auth[0]}:{auth[1]}"
    return crs.to_string()


def _reproject_item(path, kind, status, will_convert=False, source_crs="", detail=""):
    return {
        "path": os.path.abspath(path),
        "name": os.path.basename(path),
        "kind": kind,
        "status": status,
        "will_convert": bool(will_convert),
        "source_crs": source_crs,
        "detail": detail,
    }


def _scan_reproject_raster(path):
    dst_crs = CRS.from_epsg(4326)
    try:
        with rasterio.open(path) as src:
            if src.crs is None:
                return _reproject_item(path, "raster", "missing_crs", detail="No CRS found.")
            try:
                src_crs = CRS.from_user_input(src.crs)
            except Exception as exc:
                return _reproject_item(
                    path,
                    "raster",
                    "unreadable_crs",
                    source_crs=_reproject_crs_label(src.crs),
                    detail=f"Could not parse CRS: {exc}",
                )
            if src_crs == dst_crs:
                return _reproject_item(
                    path,
                    "raster",
                    "already_4326",
                    source_crs=_reproject_crs_label(src_crs),
                    detail="Already EPSG:4326.",
                )
            return _reproject_item(
                path,
                "raster",
                "convert",
                will_convert=True,
                source_crs=_reproject_crs_label(src_crs),
                detail=f"{_reproject_crs_label(src_crs)} to {REPROJECT_TARGET_CRS_LABEL}",
            )
    except Exception as exc:
        return _reproject_item(path, "raster", "error", detail=str(exc))


def _scan_reproject_shapefile(path):
    if fiona is None or transform_geom is None:
        return _reproject_item(
            path,
            "shapefile",
            "unsupported",
            detail="Fiona is not installed, so shapefiles cannot be reprojected.",
        )

    dst_crs = CRS.from_epsg(4326)
    try:
        with fiona.Env():
            with fiona.open(path, "r") as src:
                src_crs_raw = src.crs or getattr(src, "crs_wkt", None)
                if not src_crs_raw:
                    return _reproject_item(path, "shapefile", "missing_crs", detail="No CRS found.")
                try:
                    src_crs = CRS.from_user_input(src_crs_raw)
                except Exception as exc:
                    return _reproject_item(
                        path,
                        "shapefile",
                        "unreadable_crs",
                        source_crs=_reproject_crs_label(src_crs_raw),
                        detail=f"Could not parse CRS: {exc}",
                    )
                if src_crs == dst_crs:
                    return _reproject_item(
                        path,
                        "shapefile",
                        "already_4326",
                        source_crs=_reproject_crs_label(src_crs),
                        detail="Already EPSG:4326.",
                    )
                return _reproject_item(
                    path,
                    "shapefile",
                    "convert",
                    will_convert=True,
                    source_crs=_reproject_crs_label(src_crs),
                    detail=f"{_reproject_crs_label(src_crs)} to {REPROJECT_TARGET_CRS_LABEL}",
                )
    except Exception as exc:
        return _reproject_item(path, "shapefile", "error", detail=str(exc))


def discover_reproject_targets(folder=None):
    raster_paths, shapefile_paths = discover_reproject_file_paths(folder)
    items = []
    for path in raster_paths:
        items.append(_scan_reproject_raster(path))
    for path in shapefile_paths:
        items.append(_scan_reproject_shapefile(path))
    return items


def discover_reproject_targets_in_output_folders(root_folder=None):
    """Scan only QC product folders for EPSG:4326 conversion targets.

    The post-batch reprojection step must never touch original input scenes.
    Only files already written into QC_output or nan_thresholded_scenes are
    eligible for append/overwrite reprojection.
    """
    root = os.path.abspath(root_folder or SCRIPT_FOLDER)
    raster_paths = []
    shapefile_paths = []

    for output_folder in FIXED_OUTPUT_FOLDERS:
        folder_path = os.path.join(root, output_folder)
        if not os.path.isdir(folder_path):
            continue
        rasters, shapefiles = discover_reproject_file_paths(folder_path)
        rasters = [p for p in rasters if not _is_inside_reproject_append_folder(p)]
        shapefiles = [p for p in shapefiles if not _is_inside_reproject_append_folder(p)]
        raster_paths.extend(rasters)
        shapefile_paths.extend(shapefiles)

    items = []
    for path in _unique_sorted_paths(raster_paths):
        items.append(_scan_reproject_raster(path))
    for path in _unique_sorted_paths(shapefile_paths):
        items.append(_scan_reproject_shapefile(path))
    return items


def summarize_reproject_targets(items):
    counts = {
        "total": 0,
        "raster_total": 0,
        "shapefile_total": 0,
        "convert_total": 0,
        "raster_convert": 0,
        "shapefile_convert": 0,
        "already_total": 0,
        "issue_total": 0,
    }
    for item in list(items or []):
        kind = item.get("kind")
        counts["total"] += 1
        if kind == "raster":
            counts["raster_total"] += 1
        elif kind == "shapefile":
            counts["shapefile_total"] += 1
        if item.get("will_convert"):
            counts["convert_total"] += 1
            if kind == "raster":
                counts["raster_convert"] += 1
            elif kind == "shapefile":
                counts["shapefile_convert"] += 1
        elif item.get("status") == "already_4326":
            counts["already_total"] += 1
        else:
            counts["issue_total"] += 1
    return counts


def _format_reproject_summary(counts):
    total = int(counts.get("total", 0))
    convert_total = int(counts.get("convert_total", 0))
    raster_convert = int(counts.get("raster_convert", 0))
    shapefile_convert = int(counts.get("shapefile_convert", 0))
    already_total = int(counts.get("already_total", 0))
    issue_total = int(counts.get("issue_total", 0))

    if total <= 0:
        return "No TIFF or SHP files were found."
    if convert_total <= 0:
        if issue_total > 0:
            return (
                f"No files can be converted. {already_total} file(s) are already EPSG:4326; "
                f"{issue_total} file(s) are missing CRS information, unreadable, or unsupported."
            )
        return f"No reprojection is needed. {already_total} file(s) are already EPSG:4326."

    summary = (
        f"{convert_total} file(s) will be converted: "
        f"{raster_convert} TIFF and {shapefile_convert} SHP."
    )
    if already_total:
        summary += f" {already_total} file(s) are already EPSG:4326."
    if issue_total:
        summary += f" {issue_total} file(s) cannot be converted and will be skipped."
    return summary


def _ensure_reproject_writable(path):
    try:
        mode = os.stat(path).st_mode
        if not (mode & stat.S_IWRITE):
            os.chmod(path, mode | stat.S_IWRITE)
    except FileNotFoundError:
        pass


def _release_reproject_file_handles():
    try:
        gc.collect()
    except Exception:
        pass


def _replace_with_reproject_retries(tmp_path, dst_path):
    _ensure_reproject_writable(dst_path)
    for attempt in range(1, REPROJECT_BATCH_RETRIES + 1):
        try:
            _release_reproject_file_handles()
            os.replace(tmp_path, dst_path)
            return
        except PermissionError:
            if attempt == REPROJECT_BATCH_RETRIES:
                raise
            time.sleep(min(REPROJECT_BATCH_RETRY_WAIT_SEC * attempt, 3.0))


def _sanitize_reproject_raster_profile(profile, src):
    """Remove source GTiff creation options that conflict with reprojection output."""
    profile = dict(profile or {})
    compress = str(profile.get("compress", "") or "").strip().lower()
    photometric = str(profile.get("photometric", "") or "").strip().lower()

    if photometric == "ycbcr" and "jpeg" not in compress:
        try:
            count = int(profile.get("count", getattr(src, "count", 0)) or 0)
        except Exception:
            count = 0
        if count >= 3:
            profile["photometric"] = "RGB"
            profile["interleave"] = "pixel"
        else:
            profile.pop("photometric", None)

    for key in ("jpeg_quality", "jpegtablesmode"):
        profile.pop(key, None)

    return profile


def _reproject_nodata_values(src):
    """Return source nodata and the correct destination nodata value."""
    source_nodata = src.nodata
    try:
        is_floating = np.issubdtype(np.dtype(src.dtypes[0]), np.floating)
    except (IndexError, TypeError, ValueError):
        is_floating = False

    # All floating-point products use actual IEEE NaN as output nodata.
    output_nodata = np.nan if is_floating else source_nodata
    return source_nodata, output_nodata, is_floating


def _prepare_float_band_for_reprojection(src, bidx, source_nodata):
    """Convert declared floating-point nodata pixels to NaN."""
    source = src.read(bidx).astype("float32", copy=False)
    missing = ~np.isfinite(source)

    if source_nodata is not None:
        try:
            nodata_value = float(source_nodata)
            if np.isfinite(nodata_value):
                missing |= source == nodata_value
        except (TypeError, ValueError):
            pass

    if np.any(missing):
        source = source.copy()
        source[missing] = np.nan
    return source


def _reproject_output_tags(tags, is_floating=False):
    """Remove stale statistics and numeric fill tags from converted outputs."""
    output = {}
    floating_fill_tags = {"_FILLVALUE", "MISSING_VALUE", "NODATA", "NODATA_VALUE"}
    for key, value in dict(tags or {}).items():
        upper_key = str(key).upper()
        if upper_key.startswith("STATISTICS_"):
            continue
        if is_floating and upper_key in floating_fill_tags:
            continue
        output[key] = value
    return output


def _write_exact_float_statistics(raster_path):
    """Calculate and store exact per-band statistics using finite pixels only."""
    band_statistics = []
    with rasterio.open(raster_path, "r+", sharing=False) as dataset:
        total_pixels = int(dataset.width) * int(dataset.height)
        for bidx in range(1, dataset.count + 1):
            valid_count = 0
            value_sum = 0.0
            value_sum_squares = 0.0
            minimum = math.inf
            maximum = -math.inf

            for _, window in dataset.block_windows(bidx):
                values = dataset.read(bidx, window=window)
                finite_values = values[np.isfinite(values)]
                if finite_values.size == 0:
                    continue

                finite64 = finite_values.astype("float64", copy=False)
                valid_count += int(finite64.size)
                value_sum += float(np.sum(finite64, dtype="float64"))
                value_sum_squares += float(np.sum(finite64 * finite64, dtype="float64"))
                minimum = min(minimum, float(np.min(finite64)))
                maximum = max(maximum, float(np.max(finite64)))

            if valid_count == 0:
                band_statistics.append(None)
                continue

            mean = value_sum / valid_count
            variance = max(0.0, (value_sum_squares / valid_count) - (mean * mean))
            stddev = math.sqrt(variance)
            valid_percent = 100.0 * valid_count / total_pixels if total_pixels else 0.0
            statistics = {
                "minimum": minimum,
                "maximum": maximum,
                "mean": mean,
                "stddev": stddev,
                "valid_percent": valid_percent,
                "valid_count": valid_count,
            }
            dataset.update_tags(
                bidx,
                STATISTICS_MINIMUM=format(minimum, ".15g"),
                STATISTICS_MAXIMUM=format(maximum, ".15g"),
                STATISTICS_MEAN=format(mean, ".15g"),
                STATISTICS_STDDEV=format(stddev, ".15g"),
                STATISTICS_VALID_PERCENT=format(valid_percent, ".15g"),
            )
            band_statistics.append(statistics)

    return band_statistics


def _unique_output_path(path):
    p = os.path.abspath(path)
    if not os.path.exists(p):
        return p
    folder = os.path.dirname(p)
    stem, ext = os.path.splitext(os.path.basename(p))
    counter = 2
    while True:
        candidate = os.path.join(folder, f"{stem}_{counter}{ext}")
        if not os.path.exists(candidate):
            return candidate
        counter += 1


def _fixed_output_folder_base_for_path(src_path):
    """Return the QC_output/nan_thresholded_scenes folder containing src_path."""
    abs_path = os.path.abspath(src_path)
    parts = abs_path.split(os.sep)
    fixed = {name.lower(): name for name in FIXED_OUTPUT_FOLDERS}
    for index, part in enumerate(parts):
        if part.lower() in fixed:
            return os.sep.join(parts[:index + 1]) or os.sep
    return None


def _is_inside_reproject_append_folder(path):
    try:
        parts = [part.lower() for part in os.path.abspath(path).split(os.sep)]
    except Exception:
        return False
    return DEFAULT_REPROJECT_APPEND_FOLDER.lower() in parts


def _reproject_append_output_path(src_path, suffix=DEFAULT_REPROJECT_SUFFIX):
    """Append reprojection products into reprojected_EPSG4326 folders.

    Example:
        QC_output/subdir/A_LST.tif
        -> QC_output/reprojected_EPSG4326/subdir/A_LST_EPSG4326.tif

    The same rule applies under nan_thresholded_scenes.
    """
    src_path = os.path.abspath(src_path)
    base_folder = _fixed_output_folder_base_for_path(src_path)
    src_dir = os.path.dirname(src_path)
    if base_folder:
        try:
            rel_dir = os.path.relpath(src_dir, base_folder)
        except Exception:
            rel_dir = ""
        if rel_dir in (".", os.curdir):
            rel_dir = ""
        rel_parts = [part for part in rel_dir.split(os.sep) if part]
        if rel_parts and rel_parts[0].lower() == DEFAULT_REPROJECT_APPEND_FOLDER.lower():
            rel_parts = rel_parts[1:]
        out_dir = os.path.join(base_folder, DEFAULT_REPROJECT_APPEND_FOLDER, *rel_parts)
    else:
        out_dir = os.path.join(src_dir, DEFAULT_REPROJECT_APPEND_FOLDER)

    os.makedirs(out_dir, exist_ok=True)
    stem, ext = os.path.splitext(os.path.basename(src_path))
    suffix = _safe_suffix(suffix, DEFAULT_REPROJECT_SUFFIX)
    return _unique_output_path(os.path.join(out_dir, stem + suffix + ext))


def _reproject_output_path(src_path, output_mode="overwrite", suffix=DEFAULT_REPROJECT_SUFFIX):
    if str(output_mode).lower() == "overwrite":
        return os.path.abspath(src_path)
    return _reproject_append_output_path(src_path, suffix)


def reproject_raster_to_4326(src_path, output_mode="overwrite", suffix=DEFAULT_REPROJECT_SUFFIX):
    dst_crs = CRS.from_epsg(4326)
    out_path = _reproject_output_path(src_path, output_mode, suffix)
    tmp_path = None
    band_statistics = []
    try:
        with rasterio.Env():
            with rasterio.open(src_path, sharing=False) as src:
                if src.crs is None:
                    return "Skipped: no CRS found.", None

                src_crs = CRS.from_user_input(src.crs)
                if src_crs == dst_crs:
                    return "Skipped: already EPSG:4326.", None

                transform, width, height = calculate_default_transform(
                    src.crs, dst_crs, src.width, src.height, *src.bounds
                )

                profile = src.profile.copy()
                profile.update({
                    "crs": dst_crs,
                    "transform": transform,
                    "width": width,
                    "height": height,
                    "compress": "LZW",
                    "tiled": True,
                    "blockxsize": 256,
                    "blockysize": 256,
                    "bigtiff": "IF_SAFER",
                })
                profile = _sanitize_reproject_raster_profile(profile, src)

                source_nodata, output_nodata, is_floating = _reproject_nodata_values(src)
                if output_nodata is not None:
                    profile["nodata"] = output_nodata
                else:
                    profile.pop("nodata", None)

                fd, tmp_path = tempfile.mkstemp(
                    suffix=os.path.splitext(out_path)[1] or ".tif",
                    prefix="reproj_",
                    dir=os.path.dirname(out_path) or ".",
                )
                os.close(fd)

                with rasterio.open(tmp_path, "w", **profile) as dst:
                    try:
                        output_tags = _reproject_output_tags(src.tags(), is_floating)
                        if output_tags:
                            dst.update_tags(**output_tags)
                    except Exception:
                        pass
                    for bidx in range(1, src.count + 1):
                        if is_floating:
                            source = _prepare_float_band_for_reprojection(src, bidx, source_nodata)
                            warp_source_nodata = np.nan
                        else:
                            source = rasterio.band(src, bidx)
                            warp_source_nodata = source_nodata
                        reproject(
                            source=source,
                            destination=rasterio.band(dst, bidx),
                            src_transform=src.transform,
                            src_crs=src.crs,
                            dst_transform=transform,
                            dst_crs=dst_crs,
                            resampling=Resampling.nearest,
                            src_nodata=warp_source_nodata,
                            dst_nodata=output_nodata,
                            num_threads=REPROJECT_THREADS,
                        )
                        try:
                            band_tags = _reproject_output_tags(src.tags(bidx), is_floating)
                            if band_tags:
                                dst.update_tags(bidx, **band_tags)
                        except Exception:
                            pass

                if is_floating:
                    band_statistics = _write_exact_float_statistics(tmp_path)

        _replace_with_reproject_retries(tmp_path, out_path)
        tmp_path = None
        action = "Overwritten" if str(output_mode).lower() == "overwrite" else "Appended"
        range_text = ""
        if band_statistics and band_statistics[0] is not None:
            first_band = band_statistics[0]
            range_text = (
                f"; valid range {first_band['minimum']:.6g} to "
                f"{first_band['maximum']:.6g}"
            )
        return f"{action}: {os.path.basename(out_path)}{range_text}", out_path
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _replace_shapefile_group(tmp_shp_path, dst_shp_path):
    tmp_dir = os.path.dirname(tmp_shp_path) or "."
    dst_dir = os.path.dirname(dst_shp_path) or "."
    tmp_root = os.path.splitext(os.path.basename(tmp_shp_path))[0]
    dst_root = os.path.splitext(os.path.basename(dst_shp_path))[0]

    for ext in [".shp", ".shx", ".dbf", ".prj", ".cpg"]:
        tmp_file = os.path.join(tmp_dir, tmp_root + ext)
        if not os.path.exists(tmp_file):
            continue
        dst_file = os.path.join(dst_dir, dst_root + ext)
        _replace_with_reproject_retries(tmp_file, dst_file)

    for leftover in glob.glob(os.path.join(tmp_dir, tmp_root + ".*")):
        if os.path.exists(leftover):
            try:
                os.remove(leftover)
            except OSError:
                pass


def _cleanup_shapefile_group(shp_path):
    if not shp_path:
        return
    folder = os.path.dirname(shp_path) or "."
    root = os.path.splitext(os.path.basename(shp_path))[0]
    for leftover in glob.glob(os.path.join(folder, root + ".*")):
        try:
            os.remove(leftover)
        except OSError:
            pass


def reproject_shapefile_to_4326(src_path, output_mode="overwrite", suffix=DEFAULT_REPROJECT_SUFFIX):
    if fiona is None or transform_geom is None:
        return "Skipped: Fiona is not installed.", None

    dst_crs_rio = CRS.from_epsg(4326)
    out_path = _reproject_output_path(src_path, output_mode, suffix)
    tmp_shp = None
    write_path = None
    try:
        with fiona.Env():
            with fiona.open(src_path, "r") as src:
                src_crs_raw = src.crs or getattr(src, "crs_wkt", None)
                if not src_crs_raw:
                    return "Skipped: no CRS found.", None

                src_crs_rio = CRS.from_user_input(src_crs_raw)
                if src_crs_rio == dst_crs_rio:
                    return "Skipped: already EPSG:4326.", None

                meta = src.meta.copy()
                if FionaCRS is not None:
                    meta["crs"] = FionaCRS.from_epsg(4326)
                else:
                    meta["crs"] = REPROJECT_TARGET_CRS_LABEL
                meta.pop("crs_wkt", None)

                if str(output_mode).lower() == "overwrite":
                    fd, tmp_shp = tempfile.mkstemp(
                        suffix=".shp",
                        prefix="reproj_",
                        dir=os.path.dirname(src_path) or ".",
                    )
                    os.close(fd)
                    os.remove(tmp_shp)
                    write_path = tmp_shp
                else:
                    write_path = out_path
                    _cleanup_shapefile_group(write_path)

                with fiona.open(write_path, "w", **meta) as dst:
                    for feat in src:
                        geom = feat["geometry"]
                        if geom is not None:
                            geom = transform_geom(
                                src_crs_rio.to_string(),
                                REPROJECT_TARGET_CRS_LABEL,
                                geom,
                            )
                        new_feat = {
                            "type": "Feature",
                            "properties": dict(feat["properties"]),
                            "geometry": geom,
                        }
                        if "id" in feat:
                            new_feat["id"] = feat["id"]
                        dst.write(new_feat)

        if str(output_mode).lower() == "overwrite":
            _replace_shapefile_group(tmp_shp, src_path)
            tmp_shp = None
            action = "Overwritten"
            out_path = src_path
        else:
            action = "Appended"
        return f"{action}: {os.path.basename(out_path)}", os.path.abspath(out_path)
    finally:
        if tmp_shp:
            _cleanup_shapefile_group(tmp_shp)


def batch_reproject_targets_to_4326(targets, output_mode="overwrite", suffix=DEFAULT_REPROJECT_SUFFIX, progress_callback=None):
    convert_items = [item for item in list(targets or []) if item.get("will_convert")]
    total = len(convert_items)
    results = []
    _release_reproject_file_handles()
    for index, item in enumerate(convert_items, start=1):
        if progress_callback:
            progress_callback(index - 1, total, item, "Converting")
        try:
            if item.get("kind") == "raster":
                message, output_path = reproject_raster_to_4326(item["path"], output_mode, suffix)
            else:
                message, output_path = reproject_shapefile_to_4326(item["path"], output_mode, suffix)
            lower = str(message).lower()
            status = "converted" if lower.startswith(("overwritten", "appended")) else "skipped"
            results.append(dict(item, result_status=status, result_message=message, output_path=output_path or ""))
        except Exception as exc:
            results.append(dict(item, result_status="error", result_message=str(exc), output_path=""))
        if progress_callback:
            progress_callback(index, total, item, results[-1]["result_message"])
    return results


def summarize_reproject_results(results):
    converted = sum(1 for item in list(results or []) if item.get("result_status") == "converted")
    skipped = sum(1 for item in list(results or []) if item.get("result_status") == "skipped")
    errors = sum(1 for item in list(results or []) if item.get("result_status") == "error")
    return converted, skipped, errors


class ReprojectTo4326Dialog(QtWidgets.QDialog):
    def __init__(self, counts, convert_items, folder, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Reproject To EPSG:4326")
        self.setModal(True)
        self.counts = dict(counts or {})
        self.convert_items = list(convert_items or [])
        self.folder = os.path.abspath(folder or SCRIPT_FOLDER)
        self.results = None

        self.resize(820, 560)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        title = QtWidgets.QLabel("Reproject files to EPSG:4326")
        title_font = QtGui.QFont()
        title_font.setPointSize(14)
        title_font.setBold(True)
        title.setFont(title_font)
        layout.addWidget(title)

        intro = QtWidgets.QLabel(
            "The mask batch is complete. You can now convert non-EPSG:4326 "
            "TIFF and SHP files that were written into QC_output or "
            "nan_thresholded_scenes. Original input-folder scenes are not queued. "
            "Append mode keeps the original files and writes converted copies into reprojected_EPSG4326."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        summary = QtWidgets.QLabel(_format_reproject_summary(self.counts) + f"\n\nFolder:\n{self.folder}")
        summary.setWordWrap(True)
        layout.addWidget(summary)

        mode_group = QtWidgets.QGroupBox("Output mode")
        mode_layout = QtWidgets.QGridLayout(mode_group)
        self.append_radio = QtWidgets.QRadioButton("Append converted files into reprojected_EPSG4326 with suffix (keep original files)")
        self.overwrite_radio = QtWidgets.QRadioButton("Overwrite existing files in place")
        self.append_radio.setChecked(True)
        mode_layout.addWidget(self.append_radio, 0, 0, 1, 2)
        mode_layout.addWidget(self.overwrite_radio, 1, 0, 1, 2)
        mode_layout.addWidget(QtWidgets.QLabel("Suffix"), 2, 0)
        self.suffix_edit = QtWidgets.QLineEdit(DEFAULT_REPROJECT_SUFFIX)
        self.suffix_edit.setMinimumWidth(180)
        mode_layout.addWidget(self.suffix_edit, 2, 1)
        self.append_radio.toggled.connect(self._sync_mode_controls)
        layout.addWidget(mode_group)

        self.status_label = QtWidgets.QLabel("Ready to reproject.")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setRange(0, max(1, len(self.convert_items)))
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%v / %m")
        layout.addWidget(self.progress_bar)

        details_label = QtWidgets.QLabel("Files queued")
        details_label.setStyleSheet("font-weight: 700;")
        layout.addWidget(details_label)

        self.details_edit = QtWidgets.QPlainTextEdit()
        self.details_edit.setReadOnly(True)
        self.details_edit.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        pending_lines = []
        for item in self.convert_items:
            rel = os.path.relpath(item.get("path", ""), self.folder)
            pending_lines.append(f"[PENDING] {item.get('kind', '')}: {rel} ({item.get('detail', '')})")
        self.details_edit.setPlainText("\n".join(pending_lines))
        layout.addWidget(self.details_edit, 1)

        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        self.skip_btn = QtWidgets.QPushButton("Skip")
        self.run_btn = QtWidgets.QPushButton("Run Reprojection")
        self.close_btn = QtWidgets.QPushButton("Close")
        self.close_btn.hide()
        self.skip_btn.clicked.connect(self.reject)
        self.run_btn.clicked.connect(self._run_reprojection)
        self.close_btn.clicked.connect(self.accept)
        row.addWidget(self.skip_btn)
        row.addWidget(self.run_btn)
        row.addWidget(self.close_btn)
        layout.addLayout(row)
        self._sync_mode_controls()

    def _sync_mode_controls(self):
        suffix_mode = self.append_radio.isChecked()
        self.suffix_edit.setEnabled(suffix_mode)

    def _append_detail(self, text):
        self.details_edit.appendPlainText(str(text))
        try:
            scrollbar = self.details_edit.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())
        except Exception:
            pass

    def _progress_callback(self, index, total, item, message):
        total = max(1, int(total or 0))
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(max(0, min(total, int(index or 0))))
        name = os.path.relpath(item.get("path", ""), self.folder) if item else ""
        self.status_label.setText(f"{index}/{total} {name}: {message}")
        if message and str(message) != "Converting":
            self._append_detail(f"[{index}/{total}] {name}: {message}")
        QtWidgets.QApplication.processEvents(QtCore.QEventLoop.ExcludeUserInputEvents)

    def _run_reprojection(self):
        mode = "overwrite" if self.overwrite_radio.isChecked() else "append"
        suffix = _safe_suffix(self.suffix_edit.text(), DEFAULT_REPROJECT_SUFFIX)
        if mode == "append" and not suffix:
            QtWidgets.QMessageBox.warning(self, "Suffix required", "Enter a suffix for appended EPSG:4326 files.")
            return
        if mode == "overwrite":
            reply = QtWidgets.QMessageBox.warning(
                self,
                "Overwrite files in EPSG:4326?",
                "This will replace every queued non-EPSG:4326 TIFF/SHP file in place. "
                "Make sure you have backups before continuing.",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if reply != QtWidgets.QMessageBox.Yes:
                return

        self.append_radio.setEnabled(False)
        self.overwrite_radio.setEnabled(False)
        self.suffix_edit.setEnabled(False)
        self.run_btn.setEnabled(False)
        self.skip_btn.setEnabled(False)
        self.status_label.setText("Starting reprojection...")
        self._append_detail(
            f"\n[RUN] Mode: {mode}. "
            + (
                f"Output folder: {DEFAULT_REPROJECT_APPEND_FOLDER}. Suffix: {suffix}."
                if mode == "append"
                else "Overwriting queued files in place."
            )
        )
        QtWidgets.QApplication.processEvents()

        self.results = batch_reproject_targets_to_4326(
            self.convert_items,
            output_mode=mode,
            suffix=suffix,
            progress_callback=self._progress_callback,
        )
        converted, skipped, errors = summarize_reproject_results(self.results)
        self.status_label.setText(f"Finished: {converted} converted, {skipped} skipped, {errors} failed.")
        self._append_detail(f"[DONE] {converted} converted, {skipped} skipped, {errors} failed.")
        self.progress_bar.setValue(self.progress_bar.maximum())
        self.close_btn.show()
        self.close_btn.setDefault(True)
        if errors:
            QtWidgets.QMessageBox.warning(
                self,
                "Reprojection finished with errors",
                f"{converted} files converted, {skipped} skipped, and {errors} failed. See details for file-level messages.",
            )
        else:
            QtWidgets.QMessageBox.information(
                self,
                "Reprojection complete",
                f"{converted} files converted to EPSG:4326. {skipped} skipped.",
            )


class BatchWorker(QtCore.QObject):
    progress = QtCore.pyqtSignal(int, int, str)
    message = QtCore.pyqtSignal(str)
    finished = QtCore.pyqtSignal(object, int, list)

    def __init__(self, pairs, params, options):
        super().__init__()
        self.pairs = pairs
        self.params = params
        self.options = options

    @QtCore.pyqtSlot()
    def run(self):
        counts = {
            "written": 0,
            "low_kept": 0,
            "low_skipped": 0,
            "low_deleted": 0,
        }
        failed = 0
        errors = []
        total = len(self.pairs)
        for index, pair in enumerate(self.pairs, 1):
            name = file_display_name(pair)
            self.progress.emit(index - 1, total, f"Processing {name}")
            try:
                result = write_masked_scene(pair, self.params, self.options)
                status = result.get("status", "written")
                counts[status] = counts.get(status, 0) + 1
                valid_txt = pct_text(float(result.get("valid_fraction", 0.0)))
                missing_txt = pct_text(float(result.get("missing_percent", 0.0)) / 100.0)
                out_path = result.get("out_path")

                source_removed = bool(result.get("source_removed", False))

                if status == "written":
                    tag = "[OK moved]" if source_removed else "[OK appended]"
                    extra = "; original LST moved" if source_removed else ""
                    self.message.emit(
                        f"{tag} {os.path.basename(out_path)} "
                        f"({valid_txt} valid, {missing_txt} missing{extra})"
                    )
                elif status == "low_kept":
                    tag = "[NAN-THRESH moved]" if source_removed else "[NAN-THRESH appended]"
                    extra = "; original LST moved" if source_removed else ""
                    self.message.emit(
                        f"{tag} {os.path.basename(out_path)} "
                        f"({valid_txt} valid, {missing_txt} missing{extra})"
                    )
                elif status == "low_skipped":
                    self.message.emit(
                        f"[NAN-THRESH skipped] {name} "
                        f"({valid_txt} valid, {missing_txt} missing; no appended output written)"
                    )
                elif status == "low_deleted":
                    self.message.emit(
                        f"[NAN-THRESH deleted] {name} "
                        f"({valid_txt} valid, {missing_txt} missing; original LST removed)"
                    )
                else:
                    self.message.emit(f"[OK] {name} ({valid_txt} valid, {missing_txt} missing)")
            except Exception as exc:
                failed += 1
                detail = f"{name}: {exc}"
                errors.append(detail)
                self.message.emit(f"[ERROR] {detail}")
            self.progress.emit(index, total, name)
        self.finished.emit(counts, failed, errors)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ECOSTRESS LST QC Tuner v22")
        self.resize(1380, 860)

        self.pairs = []
        self.scored_pairs = []
        self.preview_pair = None
        self.preview_lst = None
        self.preview_qc = None
        self.preview_keep = None
        self._loading_controls = False
        self._scoring_thread = None
        self._scoring_worker = None
        self._batch_thread = None
        self._batch_worker = None
        self._light_mode = False

        self.preview_timer = QtCore.QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(90)
        self.preview_timer.timeout.connect(self.update_preview)

        self._build_ui()
        self._apply_theme()
        self.folder_edit.setText(SCRIPT_FOLDER)
        QtCore.QTimer.singleShot(100, self.scan_folder)

    def _build_ui(self):
        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.main_splitter = splitter
        self.setCentralWidget(splitter)

        left_scroll = QtWidgets.QScrollArea()
        self.left_scroll = left_scroll
        left_scroll.setWidgetResizable(True)
        left_scroll.setMinimumWidth(410)
        left_scroll.setMaximumWidth(520)
        left_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        left_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        left_widget = QtWidgets.QWidget()
        self.left_widget = left_widget
        left_widget.setMinimumWidth(0)
        left_widget.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Expanding)
        self.controls = QtWidgets.QVBoxLayout(left_widget)
        self.controls.setContentsMargins(14, 14, 14, 14)
        self.controls.setSpacing(12)
        left_scroll.setWidget(left_widget)
        splitter.addWidget(left_scroll)

        right_widget = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right_widget)
        right_layout.setContentsMargins(8, 8, 8, 8)
        right_layout.setSpacing(6)
        viewer_top_row = QtWidgets.QHBoxLayout()
        viewer_top_row.setContentsMargins(0, 0, 0, 0)
        viewer_top_row.addStretch(1)

        self.canvas = LstCanvas()
        self.viewer_reset_btn = QtWidgets.QPushButton("Reset Original View")
        self.viewer_reset_btn.setToolTip("Undo all viewer zooming and dragging.")
        self.viewer_reset_btn.clicked.connect(self.canvas.reset_view)
        viewer_top_row.addWidget(self.viewer_reset_btn)

        right_layout.addLayout(viewer_top_row)
        right_layout.addWidget(self.canvas, 1)
        splitter.addWidget(right_widget)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([430, 950])
        self._last_splitter_sizes = splitter.sizes()
        splitter.splitterMoved.connect(self._splitter_moved)

        self._build_appearance_group()
        self._build_folder_group()
        self._build_scene_group()
        self._build_qc_group()
        self._build_temperature_group()
        self._build_display_group()
        self._build_output_group()
        self.controls.addStretch(1)

        self.statusBar().showMessage("Ready")

    def _splitter_moved(self, pos, index):
        try:
            self._last_splitter_sizes = list(self.main_splitter.sizes())
        except Exception:
            pass

    def _restore_splitter_sizes(self, sizes=None):
        try:
            if sizes is None:
                sizes = self._last_splitter_sizes
            if not sizes or len(sizes) < 2:
                return
            # Reapply the exact splitter sizes after a theme change so color-only
            # toggles do not let Qt recompute a wider left control panel.
            self.main_splitter.setSizes([int(sizes[0]), int(sizes[1])])
            self._last_splitter_sizes = list(self.main_splitter.sizes())
        except Exception:
            pass

    def _build_appearance_group(self):
        group = QtWidgets.QGroupBox("Appearance")
        layout = QtWidgets.QVBoxLayout(group)
        row = QtWidgets.QHBoxLayout()

        self.light_mode_toggle = QtWidgets.QCheckBox("Light mode")
        self.light_mode_toggle.setChecked(False)
        self.light_mode_toggle.setToolTip("Switch between dark and light interface themes.")
        self.light_mode_toggle.stateChanged.connect(self._theme_toggle_changed)
        row.addWidget(self.light_mode_toggle)

        self.reverse_scroll_toggle = QtWidgets.QCheckBox("Reverse scrolling")
        self.reverse_scroll_toggle.setChecked(False)
        self.reverse_scroll_toggle.setToolTip("Invert mouse-wheel/trackpad zoom direction in the viewer.")
        self.reverse_scroll_toggle.stateChanged.connect(self._reverse_scroll_toggle_changed)
        row.addWidget(self.reverse_scroll_toggle)
        row.addStretch(1)

        layout.addLayout(row)
        self.controls.addWidget(group)

    def _theme_toggle_changed(self, state):
        try:
            saved_sizes = list(self.main_splitter.sizes())
        except Exception:
            saved_sizes = list(self._last_splitter_sizes or [])

        self._light_mode = bool(state == QtCore.Qt.Checked)
        self._apply_theme()
        try:
            self.canvas.set_theme(self._light_mode)
        except Exception:
            pass

        # Theme changes should only repaint colors. Restore splitter geometry now
        # and once more after Qt has processed stylesheet-driven polish events.
        self._restore_splitter_sizes(saved_sizes)
        QtCore.QTimer.singleShot(0, lambda: self._restore_splitter_sizes(saved_sizes))
        QtCore.QTimer.singleShot(50, lambda: self._restore_splitter_sizes(saved_sizes))
        self.schedule_preview_update()

    def _reverse_scroll_toggle_changed(self, state):
        enabled = bool(state == QtCore.Qt.Checked)
        try:
            self.canvas.set_reverse_scroll_zoom(enabled)
        except Exception:
            pass

    def _build_folder_group(self):
        group = QtWidgets.QGroupBox("Folder")
        layout = QtWidgets.QGridLayout(group)
        layout.setColumnStretch(0, 1)

        self.folder_edit = QtWidgets.QLineEdit()
        self.folder_edit.setPlaceholderText("Folder containing LST/QC subfolders")
        browse_btn = QtWidgets.QPushButton("Browse")
        browse_btn.clicked.connect(self.browse_folder)
        self.scan_btn = QtWidgets.QPushButton("Scan")
        self.scan_btn.clicked.connect(self.scan_folder)

        layout.addWidget(self.folder_edit, 0, 0, 1, 2)
        layout.addWidget(browse_btn, 1, 0)
        layout.addWidget(self.scan_btn, 1, 1)

        self.controls.addWidget(group)

    def _build_scene_group(self):
        group = QtWidgets.QGroupBox("Representative Scene")
        layout = QtWidgets.QVBoxLayout(group)

        self.scan_summary = QtWidgets.QLabel("No scan yet")
        self.scan_summary.setWordWrap(True)
        layout.addWidget(self.scan_summary)

        self.scene_combo = QtWidgets.QComboBox()
        self.scene_combo.currentIndexChanged.connect(self.scene_selection_changed)
        layout.addWidget(self.scene_combo)

        self.scene_stats = QtWidgets.QLabel("")
        self.scene_stats.setWordWrap(True)
        layout.addWidget(self.scene_stats)

        self.controls.addWidget(group)

    def _build_qc_group(self):
        group = QtWidgets.QGroupBox("QC Mask")
        layout = QtWidgets.QVBoxLayout(group)

        self.quality_slider = DiscreteBitSlider("Quality bits", 1)
        self.confidence_slider = DiscreteBitSlider("Confidence bits", 1)

        logic_row = QtWidgets.QHBoxLayout()
        logic_row.addWidget(QtWidgets.QLabel("Quality/confidence logic"))
        self.qc_logic_combo = QtWidgets.QComboBox()
        self.qc_logic_combo.addItem("Quality AND confidence must pass", "and")
        self.qc_logic_combo.addItem("Quality OR confidence may pass", "or")
        self.qc_logic_combo.setCurrentIndex(0)
        self.qc_logic_combo.currentIndexChanged.connect(self.schedule_preview_update)
        logic_row.addWidget(self.qc_logic_combo, 1)

        self.dilation_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.dilation_slider.setRange(0, 20)
        self.dilation_slider.setTickPosition(QtWidgets.QSlider.TicksBelow)
        self.dilation_slider.setTickInterval(2)
        self.dilation_label = QtWidgets.QLabel("Dilation: 0 px")
        self.dilation_slider.valueChanged.connect(self._dilation_changed)

        layout.addWidget(self.quality_slider)
        layout.addWidget(self.confidence_slider)
        layout.addLayout(logic_row)
        layout.addWidget(self.dilation_label)
        layout.addWidget(self.dilation_slider)

        self.quality_slider.valueChanged.connect(self.schedule_preview_update)
        self.confidence_slider.valueChanged.connect(self.schedule_preview_update)
        self.controls.addWidget(group)

    def _build_temperature_group(self):
        group = QtWidgets.QGroupBox("Temperature Mask")
        layout = QtWidgets.QVBoxLayout(group)

        self.temp_enable = QtWidgets.QCheckBox("Enable temperature min/max (°C)")
        self.temp_enable.stateChanged.connect(self._temperature_enabled_changed)
        self.temp_min_slider = FloatSlider(
            "Minimum LST (°C)",
            TEMPERATURE_MASK_MIN_C,
            TEMPERATURE_MASK_MAX_C,
            TEMPERATURE_MASK_MIN_C,
            decimals=2,
        )
        self.temp_max_slider = FloatSlider(
            "Maximum LST (°C)",
            TEMPERATURE_MASK_MIN_C,
            TEMPERATURE_MASK_MAX_C,
            TEMPERATURE_MASK_MAX_C,
            decimals=2,
        )
        self.temp_min_slider.setEnabled(False)
        self.temp_max_slider.setEnabled(False)
        self.temp_reset_btn = QtWidgets.QPushButton("Reset to -10 to 50 °C")
        self.temp_reset_btn.clicked.connect(self.reset_temperature_range)

        self.temp_min_slider.valueChanged.connect(self._temperature_min_changed)
        self.temp_max_slider.valueChanged.connect(self._temperature_max_changed)

        layout.addWidget(self.temp_enable)
        layout.addWidget(self.temp_min_slider)
        layout.addWidget(self.temp_max_slider)
        layout.addWidget(self.temp_reset_btn)
        self.controls.addWidget(group)

    def _build_display_group(self):
        group = QtWidgets.QGroupBox("Display")
        layout = QtWidgets.QVBoxLayout(group)

        cmap_row = QtWidgets.QHBoxLayout()
        cmap_row.addWidget(QtWidgets.QLabel("Colormap"))
        self.cmap_combo = QtWidgets.QComboBox()
        self.cmap_combo.addItems(COLORMAPS)
        self.cmap_combo.setCurrentText("magma")
        self.cmap_combo.currentTextChanged.connect(self.schedule_preview_update)
        cmap_row.addWidget(self.cmap_combo, 1)
        layout.addLayout(cmap_row)

        nan_row = QtWidgets.QHBoxLayout()
        nan_row.addWidget(QtWidgets.QLabel("NaN color"))
        self.nan_color_combo = QtWidgets.QComboBox()
        for label, color in NAN_COLOR_OPTIONS:
            self.nan_color_combo.addItem(label, color)
        self.nan_color_combo.setCurrentIndex(0)
        self.nan_color_combo.currentIndexChanged.connect(self.schedule_preview_update)
        nan_row.addWidget(self.nan_color_combo, 1)
        layout.addLayout(nan_row)

        self.color_min_slider = FloatSlider(
            "Color minimum (°C)",
            DEFAULT_COLOR_MIN_C,
            DEFAULT_COLOR_MAX_C,
            DEFAULT_COLOR_MIN_C,
            decimals=2,
        )
        self.color_max_slider = FloatSlider(
            "Color maximum (°C)",
            DEFAULT_COLOR_MIN_C,
            DEFAULT_COLOR_MAX_C,
            DEFAULT_COLOR_MAX_C,
            decimals=2,
        )
        self.color_min_slider.valueChanged.connect(self._color_min_changed)
        self.color_max_slider.valueChanged.connect(self._color_max_changed)
        layout.addWidget(self.color_min_slider)
        layout.addWidget(self.color_max_slider)

        row = QtWidgets.QHBoxLayout()
        self.auto_color_btn = QtWidgets.QPushButton("Auto color")
        self.auto_color_btn.clicked.connect(self.auto_color_from_preview)
        row.addWidget(self.auto_color_btn)
        layout.addLayout(row)

        self.preview_stats = QtWidgets.QLabel("")
        self.preview_stats.setWordWrap(True)
        layout.addWidget(self.preview_stats)

        self.controls.addWidget(group)

    def _build_output_group(self):
        group = QtWidgets.QGroupBox("Apply to All")
        layout = QtWidgets.QVBoxLayout(group)

        self.append_radio = QtWidgets.QRadioButton("Append new LST files with suffix (keep original LST files)")
        self.overwrite_radio = QtWidgets.QRadioButton("Overwrite original LST files")
        self.append_radio.setChecked(True)
        layout.addWidget(self.append_radio)
        layout.addWidget(self.overwrite_radio)

        suffix_row = QtWidgets.QHBoxLayout()
        suffix_row.addWidget(QtWidgets.QLabel("Suffix"))
        self.suffix_edit = QtWidgets.QLineEdit(DEFAULT_APPEND_SUFFIX)
        suffix_row.addWidget(self.suffix_edit, 1)
        layout.addLayout(suffix_row)

        fixed_folder_note = QtWidgets.QLabel(
            "QC-filtered kept scenes are always written to: QC_output\n"
            "NaN-thresholded kept scenes are always written to: nan_thresholded_scenes\n"
            "Append mode keeps the original LST files and writes new suffixed copies."
        )
        fixed_folder_note.setWordWrap(True)
        layout.addWidget(fixed_folder_note)

        self.nan_threshold_slider = FloatSlider(
            "NaN threshold: maximum NaN/missing data (%)",
            0.0,
            100.0,
            DEFAULT_MAX_MISSING_PERCENT,
            decimals=1,
        )
        self.nan_threshold_slider.valueChanged.connect(self._nan_threshold_threshold_changed)
        layout.addWidget(self.nan_threshold_slider)

        coverage_scale = QtWidgets.QHBoxLayout()
        left_label = QtWidgets.QLabel("0% NaN/missing = full scene")
        right_label = QtWidgets.QLabel("100% NaN/missing = empty scene")
        right_label.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        coverage_scale.addWidget(left_label)
        coverage_scale.addWidget(right_label)
        layout.addLayout(coverage_scale)

        self.nan_threshold_help = QtWidgets.QLabel("")
        self.nan_threshold_help.setWordWrap(True)
        layout.addWidget(self.nan_threshold_help)

        low_action_row = QtWidgets.QHBoxLayout()
        low_action_row.addWidget(QtWidgets.QLabel("NaN-thresholded scenes"))
        self.nan_threshold_action_combo = QtWidgets.QComboBox()
        self.nan_threshold_action_combo.addItem("Keep with NaN suffix", "keep")
        self.nan_threshold_action_combo.addItem("Delete / skip NaN-thresholded scenes", "delete")
        self.nan_threshold_action_combo.setCurrentIndex(0)
        low_action_row.addWidget(self.nan_threshold_action_combo, 1)
        layout.addLayout(low_action_row)

        low_suffix_row = QtWidgets.QHBoxLayout()
        low_suffix_row.addWidget(QtWidgets.QLabel("NaN suffix"))
        self.nan_threshold_suffix_edit = QtWidgets.QLineEdit(DEFAULT_LOW_COVER_SUFFIX)
        low_suffix_row.addWidget(self.nan_threshold_suffix_edit, 1)
        layout.addLayout(low_suffix_row)

        self._nan_threshold_threshold_changed(self.nan_threshold_slider.value())

        self.apply_btn = QtWidgets.QPushButton("Apply Current Mask to All Images")
        self.apply_btn.clicked.connect(self.apply_to_all)
        layout.addWidget(self.apply_btn)

        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(300)
        self.log.setMinimumHeight(150)
        layout.addWidget(self.log)

        self.controls.addWidget(group)

    def _nan_threshold_threshold_changed(self, value):
        available_percent = max(0.0, 100.0 - float(value))
        self.nan_threshold_help.setText(
            f"Scenes with more than {float(value):.1f}% NaN/missing data are NaN-thresholded. "
            f"Normal QC outputs are kept only when at least {available_percent:.1f}% of pixels remain available."
        )

    def _apply_theme(self):
        light = bool(getattr(self, "_light_mode", False))
        palette = QtGui.QPalette()

        if light:
            palette.setColor(QtGui.QPalette.Window, QtGui.QColor("#F8F9FA"))
            palette.setColor(QtGui.QPalette.WindowText, QtGui.QColor("#202124"))
            palette.setColor(QtGui.QPalette.Base, QtGui.QColor("#FFFFFF"))
            palette.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor("#EEF1F5"))
            palette.setColor(QtGui.QPalette.Text, QtGui.QColor("#202124"))
            palette.setColor(QtGui.QPalette.Button, QtGui.QColor("#FFFFFF"))
            palette.setColor(QtGui.QPalette.ButtonText, QtGui.QColor("#202124"))
            palette.setColor(QtGui.QPalette.Highlight, QtGui.QColor("#2563EB"))
            palette.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor("#FFFFFF"))
            QtWidgets.QApplication.instance().setPalette(palette)

            self.setStyleSheet("""
                QMainWindow, QWidget { font-size: 10pt; }
                QGroupBox {
                    border: 1px solid #CBD5E1;
                    border-radius: 6px;
                    margin-top: 10px;
                    padding-top: 10px;
                    font-weight: 700;
                    background-color: #F8F9FA;
                    color: #202124;
                }
                QGroupBox::title {
                    subcontrol-origin: margin;
                    left: 10px;
                    padding: 0 4px;
                    color: #202124;
                }
                QPushButton {
                    background-color: #FFFFFF;
                    color: #202124;
                    border: 1px solid #CBD5E1;
                    border-radius: 5px;
                    padding: 6px 10px;
                }
                QPushButton:hover { background-color: #EEF1F5; }
                QPushButton:pressed { background-color: #E2E8F0; }
                QPushButton:disabled { color: #94A3B8; background-color: #F1F5F9; }
                QLineEdit, QComboBox, QPlainTextEdit {
                    background-color: #FFFFFF;
                    color: #202124;
                    border: 1px solid #CBD5E1;
                    border-radius: 4px;
                    padding: 4px;
                }
                QScrollArea { background-color: #F8F9FA; border: none; }
                QScrollBar:horizontal { height: 0px; }
                QSlider::groove:horizontal {
                    height: 6px;
                    background: #CBD5E1;
                    border-radius: 3px;
                }
                QSlider::handle:horizontal {
                    background: #2563EB;
                    border: 1px solid #1D4ED8;
                    width: 16px;
                    margin: -6px 0;
                    border-radius: 8px;
                }
                QProgressBar {
                    border: 1px solid #CBD5E1;
                    border-radius: 4px;
                    text-align: center;
                    background: #FFFFFF;
                    color: #202124;
                }
                QProgressBar::chunk { background-color: #2563EB; }
            """)
        else:
            palette.setColor(QtGui.QPalette.Window, QtGui.QColor("#202124"))
            palette.setColor(QtGui.QPalette.WindowText, QtGui.QColor("#E8EAED"))
            palette.setColor(QtGui.QPalette.Base, QtGui.QColor("#151719"))
            palette.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor("#26292D"))
            palette.setColor(QtGui.QPalette.Text, QtGui.QColor("#E8EAED"))
            palette.setColor(QtGui.QPalette.Button, QtGui.QColor("#30343A"))
            palette.setColor(QtGui.QPalette.ButtonText, QtGui.QColor("#E8EAED"))
            palette.setColor(QtGui.QPalette.Highlight, QtGui.QColor("#4D8BF5"))
            palette.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor("#FFFFFF"))
            QtWidgets.QApplication.instance().setPalette(palette)

            self.setStyleSheet("""
                QMainWindow, QWidget { font-size: 10pt; }
                QGroupBox {
                    border: 1px solid #3C4043;
                    border-radius: 6px;
                    margin-top: 10px;
                    padding-top: 10px;
                    font-weight: 700;
                }
                QGroupBox::title {
                    subcontrol-origin: margin;
                    left: 10px;
                    padding: 0 4px;
                }
                QPushButton {
                    background-color: #30343A;
                    border: 1px solid #4A4F57;
                    border-radius: 5px;
                    padding: 6px 10px;
                }
                QPushButton:hover { background-color: #3A3F47; }
                QPushButton:pressed { background-color: #4A4F57; }
                QPushButton:disabled { color: #8A8F98; background-color: #26292D; }
                QLineEdit, QComboBox, QPlainTextEdit {
                    background-color: #151719;
                    color: #E8EAED;
                    border: 1px solid #3C4043;
                    border-radius: 4px;
                    padding: 4px;
                }
                QScrollArea { background-color: #202124; border: none; }
                QScrollBar:horizontal { height: 0px; }
                QSlider::groove:horizontal {
                    height: 6px;
                    background: #3C4043;
                    border-radius: 3px;
                }
                QSlider::handle:horizontal {
                    background: #8AB4F8;
                    border: 1px solid #AECBF9;
                    width: 16px;
                    margin: -6px 0;
                    border-radius: 8px;
                }
                QProgressBar {
                    border: 1px solid #3C4043;
                    border-radius: 4px;
                    text-align: center;
                    background: #151719;
                }
                QProgressBar::chunk { background-color: #4D8BF5; }
            """)


    def browse_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Select folder containing LST/QC subfolders",
            self.folder_edit.text().strip() or SCRIPT_FOLDER,
        )
        if folder:
            self.folder_edit.setText(folder)
            self.scan_folder()

    def scan_folder(self):
        root = self.folder_edit.text().strip() or SCRIPT_FOLDER
        if not os.path.isdir(root):
            QtWidgets.QMessageBox.warning(self, "Folder not found", root)
            return

        self.scan_btn.setEnabled(False)
        self.apply_btn.setEnabled(False)
        self.scene_combo.clear()
        self.preview_pair = None
        self.preview_lst = None
        self.preview_qc = None
        self.canvas.clear_scene()
        self.log_message(f"[SCAN] {root}")
        self.statusBar().showMessage("Finding LST/QC pairs...")

        self.pairs = discover_scene_pairs(root)
        if not self.pairs:
            self.scan_summary.setText("No matching LST/QC pairs found.")
            self.scan_btn.setEnabled(True)
            self.apply_btn.setEnabled(False)
            self.progress.setRange(0, 1)
            self.progress.setValue(0)
            self.statusBar().showMessage("No matching pairs found")
            return

        self.progress.setRange(0, len(self.pairs))
        self.progress.setValue(0)
        self.scan_summary.setText(f"Found {len(self.pairs)} matching LST/QC pairs. Scoring scenes...")
        self.statusBar().showMessage("Scoring representative scenes...")

        self._scoring_thread = QtCore.QThread(self)
        self._scoring_worker = PairScoringWorker(self.pairs)
        self._scoring_worker.moveToThread(self._scoring_thread)
        self._scoring_thread.started.connect(self._scoring_worker.run)
        self._scoring_worker.progress.connect(self._scoring_progress)
        self._scoring_worker.finished.connect(self._scoring_finished)
        self._scoring_worker.finished.connect(self._scoring_thread.quit)
        self._scoring_worker.finished.connect(self._scoring_worker.deleteLater)
        self._scoring_thread.finished.connect(self._scoring_thread.deleteLater)
        self._scoring_thread.start()

    def _scoring_progress(self, index, total, pair):
        self.progress.setRange(0, total)
        self.progress.setValue(index)
        self.statusBar().showMessage(f"Scoring representative scenes {index}/{total}")

    def _scoring_finished(self, scored_pairs):
        self.scored_pairs = scored_pairs
        self.scene_combo.blockSignals(True)
        self.scene_combo.clear()
        for rank, pair in enumerate(scored_pairs, 1):
            label = f"{rank}. {file_display_name(pair)}  score {pair.score:.2f}"
            self.scene_combo.addItem(label, pair)
        self.scene_combo.blockSignals(False)

        usable = [p for p in scored_pairs if p.score >= 0]
        best = usable[0] if usable else None
        self.scan_summary.setText(
            f"Found {len(self.pairs)} LST/QC pairs. "
            f"{len(usable)} scenes were usable for representative scoring."
        )
        self.scan_btn.setEnabled(True)
        self.apply_btn.setEnabled(bool(self.pairs))
        self.progress.setValue(0)
        if best:
            self.scene_combo.setCurrentIndex(0)
            self.load_preview_pair(best)
        else:
            self.scene_stats.setText("No scene could be loaded for preview scoring.")
        self.statusBar().showMessage("Scan complete")
        self.log_message(f"[SCAN] Found {len(self.pairs)} matched LST/QC pairs.")

    def scene_selection_changed(self, index):
        if index < 0:
            return
        pair = self.scene_combo.itemData(index)
        if isinstance(pair, ScenePair):
            self.load_preview_pair(pair)

    def load_preview_pair(self, pair):
        self.preview_pair = pair
        self.statusBar().showMessage(f"Loading preview: {file_display_name(pair)}")
        try:
            QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
            self.preview_lst, self.preview_qc = read_pair_preview(pair, PREVIEW_MAX_SIZE)
            self._set_temperature_slider_range()
            self.auto_color_from_preview(emit=False)
            self._update_scene_stats(pair)
            self.update_preview(reset_view=True)
            self.statusBar().showMessage(f"Preview loaded: {file_display_name(pair)}")
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Preview failed", str(exc))
            self.log_message("[ERROR] Preview failed:\n" + traceback.format_exc())
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

    def _update_scene_stats(self, pair):
        q = pair.quality_counts
        c = pair.confidence_counts
        text = (
            f"Valid sample: {pct_text(pair.valid_fraction)} | NaN/QC missing: {pct_text(pair.nan_fraction)}\n"
            f"Quality 00/01/10/11: {q[0]}, {q[1]}, {q[2]}, {q[3]}\n"
            f"Confidence 00/01/10/11: {c[0]}, {c[1]}, {c[2]}, {c[3]}"
        )
        self.scene_stats.setText(text)

    def _set_temperature_slider_range(self):
        # Temperature filter sliders intentionally use a fixed dataset-friendly
        # Celsius range instead of the representative scene min/max.
        low, high = TEMPERATURE_MASK_MIN_C, TEMPERATURE_MASK_MAX_C
        self._loading_controls = True
        self.temp_min_slider.set_range(low, high)
        self.temp_max_slider.set_range(low, high)
        self.temp_min_slider.set_value(low, emit=False)
        self.temp_max_slider.set_value(high, emit=False)
        self._loading_controls = False

    def reset_temperature_range(self):
        self._set_temperature_slider_range()
        self.schedule_preview_update()

    def _temperature_enabled_changed(self):
        enabled = self.temp_enable.isChecked()
        self.temp_min_slider.setEnabled(enabled)
        self.temp_max_slider.setEnabled(enabled)
        self.schedule_preview_update()

    def _temperature_min_changed(self, value):
        if self._loading_controls:
            return
        if value > self.temp_max_slider.value():
            self.temp_max_slider.set_value(value, emit=False)
        self.schedule_preview_update()

    def _temperature_max_changed(self, value):
        if self._loading_controls:
            return
        if value < self.temp_min_slider.value():
            self.temp_min_slider.set_value(value, emit=False)
        self.schedule_preview_update()

    def _dilation_changed(self, value):
        self.dilation_label.setText(f"Dilation: {int(value)} px")
        self.schedule_preview_update()

    def _color_min_changed(self, value):
        if self._loading_controls:
            return
        if value >= self.color_max_slider.value():
            self.color_max_slider.set_value(value + 0.01, emit=False)
        self.schedule_preview_update()

    def _color_max_changed(self, value):
        if self._loading_controls:
            return
        if value <= self.color_min_slider.value():
            self.color_min_slider.set_value(value - 0.01, emit=False)
        self.schedule_preview_update()

    def auto_color_from_preview(self, checked=False, emit=True):
        if self.preview_lst is None:
            return
        if self.preview_qc is not None:
            params = self.current_params()
            masked_k, _ = apply_mask_to_lst(self.preview_lst, self.preview_qc, params)
            finite = lst_kelvin_to_celsius(masked_k)
            finite = finite[np.isfinite(finite)]
        else:
            full_c = lst_kelvin_to_celsius(self.preview_lst)
            finite = full_c[np.isfinite(full_c)]
        if finite.size == 0:
            low, high = DEFAULT_COLOR_MIN_C, DEFAULT_COLOR_MAX_C
        else:
            low, high = np.nanpercentile(finite, [2.0, 98.0])
            if high <= low:
                low = float(np.nanmin(finite))
                high = float(np.nanmax(finite))
            if high <= low:
                high = low + 1.0

        full_c = lst_kelvin_to_celsius(self.preview_lst)
        full = full_c[np.isfinite(full_c)]
        if full.size:
            range_low, range_high = np.nanpercentile(full, [0.1, 99.9])
            if range_high <= range_low:
                range_low, range_high = float(np.nanmin(full)), float(np.nanmax(full))
            if range_high <= range_low:
                range_high = range_low + 1.0
        else:
            range_low, range_high = DEFAULT_COLOR_MIN_C, DEFAULT_COLOR_MAX_C

        # Keep the color ramp expressed in Celsius. Expand only when the example
        # scene falls outside the default -10 to 50 °C display range.
        range_low = min(float(range_low), DEFAULT_COLOR_MIN_C)
        range_high = max(float(range_high), DEFAULT_COLOR_MAX_C)

        self._loading_controls = True
        self.color_min_slider.set_range(range_low, range_high)
        self.color_max_slider.set_range(range_low, range_high)
        self.color_min_slider.set_value(low, emit=False)
        self.color_max_slider.set_value(high, emit=False)
        self._loading_controls = False
        if emit:
            self.schedule_preview_update()

    def current_params(self):
        qc_logic = self.qc_logic_combo.currentData() if hasattr(self, "qc_logic_combo") else "and"
        if qc_logic not in ("and", "or"):
            qc_logic = "and"
        return MaskParams(
            quality_max=self.quality_slider.value(),
            confidence_max=self.confidence_slider.value(),
            qc_logic=str(qc_logic),
            dilation_radius=int(self.dilation_slider.value()),
            temperature_enabled=bool(self.temp_enable.isChecked()),
            temperature_min_c=float(self.temp_min_slider.value()),
            temperature_max_c=float(self.temp_max_slider.value()),
        )

    def schedule_preview_update(self, *args):
        if self.preview_lst is None or self.preview_qc is None:
            return
        self.preview_timer.start()

    def update_preview(self, reset_view=False):
        if self.preview_lst is None or self.preview_qc is None:
            return
        params = self.current_params()
        masked_k, keep = apply_mask_to_lst(self.preview_lst, self.preview_qc, params)
        masked_c = lst_kelvin_to_celsius(masked_k)
        self.preview_keep = keep
        nan_color = self.nan_color_combo.currentData() if hasattr(self, "nan_color_combo") else DEFAULT_NAN_COLOR
        self.canvas.set_scene(
            masked_c,
            self.cmap_combo.currentText() or "magma",
            self.color_min_slider.value(),
            self.color_max_slider.value(),
            nan_color=nan_color,
            reset_view=bool(reset_view),
            qc_array=self.preview_qc,
        )

        valid_lst_count = int(np.count_nonzero(np.isfinite(self.preview_lst)))
        kept_count = int(np.count_nonzero(keep))
        kept_fraction = kept_count / float(valid_lst_count) if valid_lst_count else 0.0
        total_fraction = kept_count / float(keep.size) if keep.size else 0.0
        temp_text = "off"
        if params.temperature_enabled:
            temp_text = f"{params.temperature_min_c:.2f} to {params.temperature_max_c:.2f} °C"
        qc_logic_text = "AND" if params.qc_logic == "and" else "OR"
        self.preview_stats.setText(
            f"Preview kept {kept_count:,} of {valid_lst_count:,} valid LST pixels "
            f"({pct_text(kept_fraction)}; {pct_text(total_fraction)} of scene). "
            f"QC logic: {qc_logic_text}. Temp mask: {temp_text}."
        )

    def current_batch_options(self):
        mode = "overwrite" if self.overwrite_radio.isChecked() else "append"
        low_action = self.nan_threshold_action_combo.currentData() if hasattr(self, "nan_threshold_action_combo") else "keep"
        if low_action not in ("keep", "delete"):
            low_action = "keep"
        return BatchOptions(
            mode=mode,
            suffix=self.suffix_edit.text().strip() or DEFAULT_APPEND_SUFFIX,
            root_folder=self.folder_edit.text().strip() or SCRIPT_FOLDER,
            QC_output_folder=DEFAULT_FILTERED_OUTPUT_FOLDER,
            max_missing_percent=float(self.nan_threshold_slider.value()) if hasattr(self, "nan_threshold_slider") else DEFAULT_MAX_MISSING_PERCENT,
            nan_threshold_action=str(low_action),
            nan_threshold_suffix=self.nan_threshold_suffix_edit.text().strip() or DEFAULT_LOW_COVER_SUFFIX,
            nan_threshold_output_folder=DEFAULT_LOW_COVER_OUTPUT_FOLDER,
        )

    def apply_to_all(self):
        if not self.pairs:
            QtWidgets.QMessageBox.information(self, "No pairs", "Scan a folder with LST/QC pairs first.")
            return

        params = self.current_params()
        options = self.current_batch_options()

        if options.mode == "overwrite":
            reply = QtWidgets.QMessageBox.warning(
                self,
                "Overwrite original LST files?",
                "This will write non-NaN-thresholded masked LST files into the QC_output folder and remove the original LST files from their source folders. NaN-thresholded kept scenes will move to nan_thresholded_scenes. Continue?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if reply != QtWidgets.QMessageBox.Yes:
                return

        if options.mode == "overwrite" and options.nan_threshold_action == "delete" and options.max_missing_percent < 100.0:
            reply = QtWidgets.QMessageBox.warning(
                self,
                "Delete NaN-thresholded original LST files?",
                "NaN-thresholded scenes will be deleted from the original LST dataset when their missing-data percentage exceeds the selected threshold. Continue?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if reply != QtWidgets.QMessageBox.Yes:
                return

        self.apply_btn.setEnabled(False)
        self.scan_btn.setEnabled(False)
        self.progress.setRange(0, len(self.pairs))
        self.progress.setValue(0)
        temp_log = "off"
        if params.temperature_enabled:
            temp_log = f"{params.temperature_min_c:.2f} to {params.temperature_max_c:.2f} °C"
        low_action_text = "keep with suffix" if options.nan_threshold_action == "keep" else "delete/skip"
        self.log_message(
            "[RUN] Applying mask: "
            f"quality {bit_threshold_text(params.quality_max)}, "
            f"confidence {bit_threshold_text(params.confidence_max)}, "
            f"QC logic {params.qc_logic.upper()}, "
            f"dilation {params.dilation_radius}px, "
            f"temperature {temp_log}, "
            f"normal kept scenes => {DEFAULT_FILTERED_OUTPUT_FOLDER}, "
            f"NaN threshold > {options.max_missing_percent:.1f}% NaN/missing => {low_action_text} "
            f"in {DEFAULT_LOW_COVER_OUTPUT_FOLDER}."
        )

        self._batch_thread = QtCore.QThread(self)
        self._batch_worker = BatchWorker(self.pairs, params, options)
        self._batch_worker.moveToThread(self._batch_thread)
        self._batch_thread.started.connect(self._batch_worker.run)
        self._batch_worker.progress.connect(self._batch_progress)
        self._batch_worker.message.connect(self.log_message)
        self._batch_worker.finished.connect(self._batch_finished)
        self._batch_worker.finished.connect(self._batch_thread.quit)
        self._batch_worker.finished.connect(self._batch_worker.deleteLater)
        self._batch_thread.finished.connect(self._batch_thread.deleteLater)
        self._batch_thread.start()


    def _batch_progress(self, index, total, message):
        self.progress.setRange(0, total)
        self.progress.setValue(index)
        self.statusBar().showMessage(f"{index}/{total} {message}")

    def _batch_finished(self, counts, failed, errors):
        self.apply_btn.setEnabled(True)
        self.scan_btn.setEnabled(True)
        self.progress.setValue(self.progress.maximum())
        written = int(counts.get("written", 0))
        low_kept = int(counts.get("low_kept", 0))
        low_skipped = int(counts.get("low_skipped", 0))
        low_deleted = int(counts.get("low_deleted", 0))
        written_total = written + low_kept
        self.statusBar().showMessage(
            f"Finished: {written_total} written, {low_skipped} skipped, {low_deleted} deleted, {failed} failed"
        )
        self.log_message(
            f"[DONE] {written} normal written, {low_kept} NaN-thresholded written, "
            f"{low_skipped} NaN-thresholded skipped, {low_deleted} NaN-thresholded deleted, {failed} failed."
        )
        if failed:
            QtWidgets.QMessageBox.warning(
                self,
                "Batch finished with errors",
                f"{written_total} files were written, {low_skipped} were skipped, "
                f"{low_deleted} were deleted, and {failed} failed. See the log for details.",
            )
        else:
            QtWidgets.QMessageBox.information(
                self,
                "Batch complete",
                f"{written_total} masked LST files written. "
                f"{low_skipped} NaN-thresholded scenes skipped. {low_deleted} NaN-thresholded scenes deleted.",
            )

        QtCore.QTimer.singleShot(0, self._offer_reproject_to_4326_after_batch)

    def _offer_reproject_to_4326_after_batch(self):
        folder = self.folder_edit.text().strip() or SCRIPT_FOLDER
        if not os.path.isdir(folder):
            self.log_message(f"[REPROJECT] Skipped: folder not found: {folder}")
            return

        self.log_message(
            "[REPROJECT] Scanning only QC_output and nan_thresholded_scenes "
            "for non-EPSG:4326 TIFF/SHP files..."
        )
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            targets = discover_reproject_targets_in_output_folders(folder)
            counts = summarize_reproject_targets(targets)
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

        convert_items = [item for item in targets if item.get("will_convert")]
        summary = _format_reproject_summary(counts)
        self.log_message(f"[REPROJECT] {summary}")
        if not convert_items:
            QtWidgets.QMessageBox.information(self, "Reproject To EPSG:4326", summary)
            return

        dlg = ReprojectTo4326Dialog(counts, convert_items, folder, self)
        dlg.exec_()
        if dlg.results is None:
            self.log_message("[REPROJECT] User skipped EPSG:4326 conversion.")
            return

        converted, skipped, errors = summarize_reproject_results(dlg.results)
        for item in dlg.results:
            rel = os.path.relpath(item.get("path", ""), folder)
            message = item.get("result_message", "")
            status = item.get("result_status", "")
            self.log_message(f"[REPROJECT {status.upper()}] {rel}: {message}")
        self.statusBar().showMessage(f"Reprojection finished: {converted} converted, {skipped} skipped, {errors} failed")


    def log_message(self, message):
        self.log.appendPlainText(str(message))


def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("ECOSTRESS LST QC Tuner v22")
    app.setOrganizationName("ECOSTRESS")
    window = MainWindow()
    window.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
