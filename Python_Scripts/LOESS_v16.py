#!/usr/bin/env python3
"""
ECOSTRESS LOESS baseline/outlier filtering GUI.

Put this script in a working folder, or launch it and browse to the folder that
contains your GeoTIFF scenes. The app scans for .tif/.tiff files, standardizes
them to one common raster grid, builds a LOESS baseline from random candidate
pixels, previews the outlier mask on a representative scene, then writes
filtered outputs into a fixed LOESS_filtered folder.

Default output behavior:
    Append mode:    LOESS_filtered/<relative path>/<stem>_LOESS_filtered.tif
    Overwrite mode: overwrite the source input TIFF in place with the same filename

Append mode keeps input scenes unchanged. Overwrite mode replaces the input
TIFF contents/profile at the original source path.
"""

import gc
import glob
import datetime
import math
import os
import random
import re
import stat
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, replace
from typing import List, Optional, Tuple

import numpy as np
import rasterio
from affine import Affine
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.transform import array_bounds
from rasterio.warp import calculate_default_transform, reproject, transform_bounds
from rasterio.windows import Window, from_bounds
from statsmodels.nonparametric.smoothers_lowess import lowess

try:
    import fiona
except Exception:
    fiona = None

# Match the high-DPI behavior used by the QC GUI.
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

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib import colors as mcolors
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

SCRIPT_FOLDER = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "ECOSTRESS LOESS Filter GUI"

KELVIN_TO_CELSIUS_OFFSET = 273.15
FILL_VALUE = -9999.0
DEFAULT_COLOR_MIN_C = -10.0
DEFAULT_COLOR_MAX_C = 50.0
PREVIEW_MAX_SIZE = 1400
REPRESENTATIVE_SCAN_SIZE = 700

DEFAULT_OUTPUT_FOLDER = "LOESS_filtered"
DEFAULT_TUNING_OUTPUT_FOLDER = "LOESS_tuning_parameters"
DEFAULT_APPEND_SUFFIX = "_LOESS_filtered"
DEFAULT_REPROJECT_SUFFIX = "_EPSG4326"
DEFAULT_REPROJECT_APPEND_FOLDER = "reprojected_EPSG4326"
REPROJECT_TARGET_CRS_LABEL = "EPSG:4326"
REPROJECT_BATCH_RETRIES = 15
REPROJECT_BATCH_RETRY_WAIT_SEC = 0.75

OUTPUT_FOLDERS_TO_SKIP = (
    DEFAULT_OUTPUT_FOLDER,
    DEFAULT_REPROJECT_APPEND_FOLDER,
    DEFAULT_TUNING_OUTPUT_FOLDER,
)

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

POINT_COLOR_OPTIONS = (
    ("Red", "#FF3B30"),
    ("Cyan", "#00FFFF"),
    ("Orange", "#FF9500"),
    ("Yellow", "#FFD60A"),
    ("Green", "#34C759"),
    ("Mint", "#00C7BE"),
    ("Blue", "#007AFF"),
    ("Purple", "#AF52DE"),
    ("Pink", "#FF2D55"),
    ("Magenta", "#FF00FF"),
    ("White", "#FFFFFF"),
    ("Black", "#000000"),
)
DEFAULT_SAMPLE_POINT_COLOR = POINT_COLOR_OPTIONS[0][1]


def theme_values(mode="dark"):
    """Return color tokens for the GUI and Matplotlib canvases."""
    mode = "light" if str(mode).lower() == "light" else "dark"
    if mode == "light":
        return {
            "mode": "light",
            "window": "#F4F6F8",
            "base": "#FFFFFF",
            "alternate": "#EEF1F5",
            "group": "#FFFFFF",
            "text": "#1F2328",
            "muted": "#5F6872",
            "button": "#E8EDF3",
            "button_hover": "#DDE5EE",
            "button_pressed": "#CCD8E5",
            "button_disabled": "#EEF1F5",
            "disabled_text": "#8A9199",
            "border": "#B7C0CC",
            "highlight": "#2F6FED",
            "highlight_text": "#FFFFFF",
            "figure": "#FFFFFF",
            "axes": "#FFFFFF",
            "grid": "#E1E6ED",
            "spine": "#B7C0CC",
            "title": "#111827",
            "outlier": "#D93025",
            "line": "#111827",
            "accent": "#2F6FED",
        }
    return {
        "mode": "dark",
        "window": "#202124",
        "base": "#151719",
        "alternate": "#26292D",
        "group": "#202124",
        "text": "#E8EAED",
        "muted": "#DADCE0",
        "button": "#30343A",
        "button_hover": "#3A3F47",
        "button_pressed": "#4A4F57",
        "button_disabled": "#26292D",
        "disabled_text": "#8A8F98",
        "border": "#3C4043",
        "highlight": "#4D8BF5",
        "highlight_text": "#FFFFFF",
        "figure": "#111111",
        "axes": "#111111",
        "grid": "#30343A",
        "spine": "#3C4043",
        "title": "#E8EAED",
        "outlier": "#FF453A",
        "line": "#E8EAED",
        "accent": "#8AB4F8",
    }


# Capacity preflight settings. These can be overridden for larger workflows with:
#     LOESS_CAPACITY_LIMIT_GB=16 LOESS_INPUT_LIMIT_GB=20 python LOESS_Filter_GUI.py
# The app reads one scene/window at a time and stores candidate-pixel time series.
DEFAULT_SAMPLE_SEED = 42
LOESS_CAPACITY_FRACTION_OF_TOTAL = 0.70
LOESS_CAPACITY_FRACTION_OF_AVAILABLE = 0.85
LOESS_MIN_CAPACITY_FRACTION_OF_TOTAL = 0.35
LOESS_WORK_OVERHEAD_FACTOR = 2.00
LOESS_FIXED_WORK_PAD_BYTES = 512 * 1024 * 1024
LOESS_DEFAULT_INPUT_SIZE_LIMIT_GB = 20.0
PREVIEW_MIN_VALID_FRACTION = 0.60
PREVIEW_MAX_MISSING_FRACTION = 0.40


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


REPROJECT_THREADS = _parse_reproject_threads(os.environ.get("LOESS_REPROJECT_THREADS", "-1"))


@dataclass(frozen=True)
class SceneInfo:
    path: str
    date: object
    date_label: str
    date_source: str
    order_index: int
    score: float = 0.0
    finite_fraction: float = 0.0
    missing_fraction: float = 1.0
    temp_std: float = 0.0
    outlier_fraction: float = 0.0


@dataclass(frozen=True)
class LoessParams:
    n_desired: int = 40
    depth_required: int = 10
    n_candidates_initial: int = 2000
    frac_val: float = 0.15
    it_val: int = 10
    threshold_cold: float = 1.75
    threshold_hot: float = 3.75


@dataclass(frozen=True)
class IntersectionInfo:
    row_off: int
    col_off: int
    height: int
    width: int
    bounds: tuple
    transform: object = None
    crs: object = None
    profile: dict = None


@dataclass(frozen=True)
class LoessModel:
    scenes: list
    params: LoessParams
    dates: list
    x_full: np.ndarray
    intersection: IntersectionInfo
    candidate_pixels: list
    candidate_values: np.ndarray
    valid_indices: np.ndarray
    selected_indices: np.ndarray
    selected_baselines: np.ndarray
    final_baseline: np.ndarray
    seed_label: str
    warnings: tuple = ()


@dataclass(frozen=True)
class BatchOptions:
    mode: str = "append"
    suffix: str = DEFAULT_APPEND_SUFFIX
    root_folder: str = SCRIPT_FOLDER
    output_folder: str = DEFAULT_OUTPUT_FOLDER


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------


def _safe_suffix(value, default_suffix):
    suffix = str(value or default_suffix).strip()
    if not suffix:
        suffix = default_suffix
    if not suffix.startswith("_"):
        suffix = "_" + suffix
    return suffix


def _safe_output_folder_name(value, default_folder=DEFAULT_OUTPUT_FOLDER):
    folder = str(value or default_folder).strip().strip("/\\")
    if not folder:
        folder = default_folder
    folder = os.path.normpath(folder)
    if folder in (".", "..") or os.path.isabs(folder) or folder.startswith(".." + os.sep):
        folder = os.path.basename(folder) or default_folder
    return folder


def _format_bytes(num_bytes):
    value = float(max(0, num_bytes or 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            if unit in ("B", "KB"):
                return f"{value:.0f} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{value:.2f} TB"


def _format_duration(seconds):
    seconds = max(1, int(round(float(seconds or 1))))
    if seconds < 60:
        return f"about {seconds} second{'s' if seconds != 1 else ''}"
    minutes, rem = divmod(seconds, 60)
    if minutes < 60:
        if rem >= 30:
            minutes += 1
        return f"about {minutes} minute{'s' if minutes != 1 else ''}"
    hours, minutes = divmod(minutes, 60)
    if minutes >= 30:
        hours += 1
    return f"about {hours} hour{'s' if hours != 1 else ''}"


def _system_capacity_snapshot():
    """Return (total_bytes, available_bytes, source_label)."""
    try:
        import psutil  # optional dependency
        vm = getattr(psutil, "virtual_" + "me" + "mory")()
        return int(vm.total), int(vm.available), "psutil"
    except Exception:
        pass

    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        phys_pages = os.sysconf("SC_PHYS_PAGES")
        total = int(page_size * phys_pages)
        return total, total, "os.sysconf"
    except Exception:
        fallback = 8 * 1024 * 1024 * 1024
        return fallback, fallback, "fallback 8 GB"


def _loess_capacity_budget_bytes():
    """Return a practical LOESS capacity budget."""
    env_gb = os.environ.get("LOESS_CAPACITY_LIMIT_GB", "").strip()
    total, available, source = _system_capacity_snapshot()
    if env_gb:
        try:
            budget = int(float(env_gb) * 1024 ** 3)
            return max(512 * 1024 * 1024, budget), total, available, f"LOESS_CAPACITY_LIMIT_GB={env_gb}"
        except Exception:
            pass

    total_based = int(total * LOESS_CAPACITY_FRACTION_OF_TOTAL)
    available_based = int(available * LOESS_CAPACITY_FRACTION_OF_AVAILABLE)
    minimum_practical = int(total * LOESS_MIN_CAPACITY_FRACTION_OF_TOTAL)
    budget = min(total_based, max(available_based, minimum_practical))
    budget = max(512 * 1024 * 1024, budget)
    return budget, total, available, source


def _loess_input_size_limit_bytes():
    """Return the optional total-input-size limit.

    This is a practical input data-volume estimate. It is a practical data-volume gate for users who
    want to keep the workflow below a known reliable dataset size. The default
    is intentionally around the user's observed workflow scale, and it can be
    disabled with LOESS_INPUT_LIMIT_GB=0.
    """
    env_gb = os.environ.get("LOESS_INPUT_LIMIT_GB", "").strip()
    try:
        value_gb = float(env_gb) if env_gb else float(LOESS_DEFAULT_INPUT_SIZE_LIMIT_GB)
    except Exception:
        value_gb = float(LOESS_DEFAULT_INPUT_SIZE_LIMIT_GB)
    if value_gb <= 0:
        return None, "disabled"
    return int(value_gb * 1024 ** 3), f"LOESS_INPUT_LIMIT_GB={value_gb:g}"


def _dtype_nbytes(dtype_name, fallback=4):
    try:
        return int(np.dtype(dtype_name).itemsize)
    except Exception:
        return int(fallback)


def inspect_raster_capacity_geometry(scenes):
    """Inspect rasters just enough to estimate the LOESS capacity envelope."""
    if not scenes:
        return {
            "max_pixels": 1,
            "overlap_pixels": 1,
            "overlap_width": 1,
            "overlap_height": 1,
            "same_crs": True,
            "total_file_bytes": 0,
            "avg_file_bytes": 0,
            "max_file_bytes": 0,
            "max_uncompressed_band_bytes": 4,
            "max_uncompressed_scene_bytes": 4,
            "warning": "No scenes available for capacity inspection.",
        }

    max_pixels = 1
    max_uncompressed_band_bytes = 4
    max_uncompressed_scene_bytes = 4
    total_file_bytes = 0
    max_file_bytes = 0
    inspected_count = 0
    same_crs = True
    warning_parts = []
    ref_crs = None
    ref_res_x = None
    ref_res_y = None
    left = bottom = right = top = None

    for index, scene in enumerate(scenes):
        try:
            try:
                file_bytes = int(os.path.getsize(scene.path))
            except Exception:
                file_bytes = 0
            total_file_bytes += file_bytes
            max_file_bytes = max(max_file_bytes, file_bytes)

            with rasterio.open(scene.path) as src:
                inspected_count += 1
                pixels = int(src.width) * int(src.height)
                max_pixels = max(max_pixels, pixels)
                band_bytes = pixels * _dtype_nbytes(src.dtypes[0] if src.dtypes else "float32", 4)
                scene_bytes = band_bytes * max(1, int(getattr(src, "count", 1) or 1))
                max_uncompressed_band_bytes = max(max_uncompressed_band_bytes, int(band_bytes))
                max_uncompressed_scene_bytes = max(max_uncompressed_scene_bytes, int(scene_bytes))

                if left is None:
                    ref_crs = src.crs
                    try:
                        ref_res_x = abs(float(src.res[0]))
                        ref_res_y = abs(float(src.res[1]))
                    except Exception:
                        ref_res_x = abs(float(src.transform.a))
                        ref_res_y = abs(float(src.transform.e))
                    left, bottom, right, top = src.bounds
                    continue

                if ref_crs is not None and src.crs is not None and src.crs != ref_crs:
                    try:
                        l, b, r, t = transform_bounds(src.crs, ref_crs, *src.bounds, densify_pts=21)
                    except Exception:
                        same_crs = False
                        l, b, r, t = src.bounds
                elif ref_crs is not None and src.crs is None:
                    same_crs = False
                    l, b, r, t = src.bounds
                else:
                    l, b, r, t = src.bounds
                left = min(left, l)
                bottom = min(bottom, b)
                right = max(right, r)
                top = max(top, t)
        except Exception as exc:
            warning_parts.append(f"Could not inspect {os.path.basename(scene.path)}: {exc}")

    overlap_width = overlap_height = 1
    overlap_pixels = max_pixels
    if ref_res_x and ref_res_y and left is not None and left < right and bottom < top:
        try:
            overlap_width = max(1, int(math.ceil((right - left) / ref_res_x)))
            overlap_height = max(1, int(math.ceil((top - bottom) / ref_res_y)))
            overlap_pixels = max(1, overlap_width * overlap_height)
        except Exception as exc:
            warning_parts.append(f"Could not compute standardized-grid estimate, using largest scene size: {exc}")
    elif not same_crs:
        warning_parts.append("Some scenes could not be transformed into the reference CRS. Capacity check used largest scene size.")
    else:
        warning_parts.append("No standardized grid could be estimated. Capacity check used largest scene size.")

    avg_file_bytes = int(total_file_bytes / max(1, len(scenes)))
    warning = " ".join(warning_parts[:3])
    if len(warning_parts) > 3:
        warning += f" Additional inspection warnings: {len(warning_parts) - 3}."

    return {
        "max_pixels": int(max_pixels),
        "overlap_pixels": int(overlap_pixels),
        "overlap_width": int(overlap_width),
        "overlap_height": int(overlap_height),
        "same_crs": bool(same_crs),
        "total_file_bytes": int(total_file_bytes),
        "avg_file_bytes": int(avg_file_bytes),
        "max_file_bytes": int(max_file_bytes),
        "max_uncompressed_band_bytes": int(max_uncompressed_band_bytes),
        "max_uncompressed_scene_bytes": int(max_uncompressed_scene_bytes),
        "inspected_count": int(inspected_count),
        "warning": warning,
    }


def estimate_loess_capacity_report(scenes, params: LoessParams, geometry=None):
    """Estimate whether the current scene set is safe for this LOESS workflow."""
    scene_count = int(len(scenes or []))
    geometry = geometry or inspect_raster_capacity_geometry(scenes)
    budget, total_capacity_value, available_capacity_value, capacity_source = _loess_capacity_budget_bytes()
    input_limit, input_limit_source = _loess_input_size_limit_bytes()

    overlap_pixels = max(1, int(geometry.get("overlap_pixels", 1)))
    max_uncompressed_band_bytes = max(4, int(geometry.get("max_uncompressed_band_bytes", overlap_pixels * 4)))
    max_uncompressed_scene_bytes = max(4, int(geometry.get("max_uncompressed_scene_bytes", max_uncompressed_band_bytes)))
    avg_file_bytes = max(1, int(geometry.get("avg_file_bytes", 0) or 1))
    total_file_bytes = max(0, int(geometry.get("total_file_bytes", 0) or 0))

    n_candidates = max(1, int(params.n_candidates_initial))
    n_desired = max(1, int(params.n_desired))

    # Stored arrays that scale with number of scenes.
    candidate_matrix_per_scene = n_candidates * 4
    baseline_matrix_per_scene = (n_desired + 4) * 4
    per_scene_model_bytes = int(
        (candidate_matrix_per_scene + baseline_matrix_per_scene) * LOESS_WORK_OVERHEAD_FACTOR
    )

    # Peak one-scene/window workspace. This stays roughly constant as scene
    # count grows because rasters are read one-at-a-time.
    read_window_bytes = overlap_pixels * 4
    preview_bytes = min(overlap_pixels, REPRESENTATIVE_SCAN_SIZE * REPRESENTATIVE_SCAN_SIZE) * 4 * 4
    graph_bytes = max(5, min(10, n_desired)) * scene_count * 8 * 8
    batch_one_scene_bytes = max_uncompressed_band_bytes * 4 + read_window_bytes * 2
    fixed_peak_bytes = int(
        (
            LOESS_FIXED_WORK_PAD_BYTES
            + read_window_bytes * 3
            + preview_bytes
            + graph_bytes
            + batch_one_scene_bytes
            + max_uncompressed_scene_bytes
        ) * LOESS_WORK_OVERHEAD_FACTOR
    )

    estimated_peak_bytes = int(fixed_peak_bytes + per_scene_model_bytes * scene_count)
    if per_scene_model_bytes <= 0:
        working_capacity = scene_count
    else:
        working_capacity = int((budget - fixed_peak_bytes) // per_scene_model_bytes)
    # Never show a nonsensical zero capacity unless even one scene truly exceeds
    # the estimated peak budget.
    if working_capacity < 1 and estimated_peak_bytes <= budget:
        working_capacity = max(1, scene_count)
    working_capacity = max(0, int(working_capacity))

    if input_limit is None:
        input_capacity = 10 ** 9
        input_percent = 0.0
        within_input_limit = True
    else:
        input_capacity = max(1, int(input_limit // avg_file_bytes))
        input_percent = 100.0 * float(total_file_bytes) / float(max(1, input_limit))
        within_input_limit = bool(total_file_bytes <= input_limit)

    working_percent = 100.0 * float(estimated_peak_bytes) / float(max(1, budget))
    max_scenes = max(0, min(working_capacity, input_capacity))
    remove_count = max(0, scene_count - max_scenes)
    within_working_limit = bool(estimated_peak_bytes <= budget)
    within_limit = bool(within_working_limit and within_input_limit and remove_count <= 0)

    if input_limit is not None and input_capacity <= working_capacity:
        limiting_factor = "input file-size limit"
        percent_full = input_percent
    else:
        limiting_factor = "peak working capacity"
        percent_full = working_percent

    return {
        "scene_count": scene_count,
        "estimated_bytes": int(estimated_peak_bytes),
        "estimated_peak_bytes": int(estimated_peak_bytes),
        "budget_bytes": int(budget),
        "total_capacity_bytes": int(total_capacity_value),
        "available_capacity_bytes": int(available_capacity_value),
        "capacity_source": capacity_source,
        "percent_full": float(percent_full),
        "working_percent_full": float(working_percent),
        "input_percent_full": float(input_percent),
        "max_scenes": int(max_scenes),
        "working_capacity": int(working_capacity),
        "input_capacity": int(input_capacity),
        "remove_count": int(remove_count),
        "within_limit": within_limit,
        "within_working_limit": within_working_limit,
        "within_input_limit": within_input_limit,
        "geometry": geometry,
        "per_scene_bytes": int(per_scene_model_bytes),
        "fixed_bytes": int(fixed_peak_bytes),
        "total_file_bytes": int(total_file_bytes),
        "input_limit_bytes": None if input_limit is None else int(input_limit),
        "input_limit_source": input_limit_source,
        "limiting_factor": limiting_factor,
    }


def loess_capacity_message(report):
    n = int(report.get("scene_count", 0))
    pct = float(report.get("percent_full", 0.0))
    max_scenes = int(report.get("max_scenes", 0))
    return (
        f"{n} scenes detected, {pct:.1f}% full.\n\n"
        f"Estimated safe scene capacity: {max_scenes} scenes."
    )


def estimate_loess_runtime_seconds(scene_count, params: LoessParams, geometry=None, last_seconds=None, last_work_units=None):
    geometry = geometry or {}
    overlap_pixels = max(1, int(geometry.get("overlap_pixels", 1)))
    overlap_mb = (float(overlap_pixels) * 4.0 * float(max(1, scene_count))) / (1024.0 ** 2)
    io_seconds = overlap_mb / 120.0
    lowess_seconds = (float(max(1, scene_count)) * float(max(1, params.n_candidates_initial))) / 12000.0
    setup_seconds = 2.0 + float(params.n_desired) / 80.0
    estimate = max(3.0, io_seconds + lowess_seconds + setup_seconds)

    work_units = float(max(1, scene_count)) * float(max(1, params.n_candidates_initial)) + overlap_mb * 1000.0
    if last_seconds and last_work_units and last_work_units > 0:
        calibrated = float(last_seconds) * (work_units / float(last_work_units))
        estimate = 0.55 * estimate + 0.45 * calibrated
    return max(1.0, estimate), work_units


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

def _versioned_output_folder(root_folder, base_folder_name):
    """Return base_folder_name, base_folder_name_1, base_folder_name_2, ..."""
    root_folder = os.path.abspath(root_folder or SCRIPT_FOLDER)
    base_folder_name = _safe_output_folder_name(base_folder_name, DEFAULT_TUNING_OUTPUT_FOLDER)
    first = os.path.join(root_folder, base_folder_name)
    if not os.path.exists(first):
        return first
    counter = 1
    while True:
        candidate = os.path.join(root_folder, f"{base_folder_name}_{counter}")
        if not os.path.exists(candidate):
            return candidate
        counter += 1


def _is_inside_output_folder(path, root_folder=None):
    try:
        root = os.path.abspath(root_folder or SCRIPT_FOLDER)
        rel = os.path.relpath(os.path.abspath(path), root)
        parts = [part.lower() for part in rel.split(os.sep) if part and part != os.curdir]
    except Exception:
        parts = [part.lower() for part in os.path.abspath(path).split(os.sep)]
    fixed = {name.lower() for name in OUTPUT_FOLDERS_TO_SKIP}
    tuning_base = DEFAULT_TUNING_OUTPUT_FOLDER.lower()
    for part in parts:
        if part in fixed:
            return True
        if part == tuning_base or part.startswith(tuning_base + "_"):
            return True
    return False


def _relative_output_subfolder(src_path, root_folder):
    root_folder = os.path.abspath(root_folder or SCRIPT_FOLDER)
    src_dir = os.path.dirname(os.path.abspath(src_path))
    try:
        rel_dir = os.path.relpath(src_dir, root_folder)
    except Exception:
        return ""
    if rel_dir in (".", os.curdir):
        return ""
    if rel_dir.startswith("..") or os.path.isabs(rel_dir):
        return ""
    parts = [part for part in rel_dir.split(os.sep) if part]
    fixed = {name.lower() for name in OUTPUT_FOLDERS_TO_SKIP}
    if parts and parts[0].lower() in fixed:
        parts = parts[1:]
    return os.path.join(*parts) if parts else ""


def output_path_for_scene(src_path, options: BatchOptions):
    """Return the output path for a LOESS-filtered scene.

    Append mode writes a new suffixed copy into LOESS_filtered and leaves the
    source TIFF untouched.

    Overwrite mode writes directly back to the source TIFF path. The filename
    and folder stay the same, but the raster contents/profile are replaced by
    the standardized LOESS-filtered output.
    """
    src_path = os.path.abspath(src_path)
    if str(options.mode).lower() == "overwrite":
        return src_path

    root_folder = os.path.abspath(options.root_folder or SCRIPT_FOLDER)
    output_folder = _safe_output_folder_name(options.output_folder, DEFAULT_OUTPUT_FOLDER)
    rel_dir = _relative_output_subfolder(src_path, root_folder)
    out_dir = os.path.join(root_folder, output_folder, rel_dir)
    os.makedirs(out_dir, exist_ok=True)
    stem, ext = os.path.splitext(os.path.basename(src_path))
    suffix = _safe_suffix(options.suffix, DEFAULT_APPEND_SUFFIX)
    return _unique_output_path(os.path.join(out_dir, stem + suffix + ext))


def _write_array_to_tif_path(out_path, output, profile):
    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    profile = dict(profile)
    profile.update(dtype="float32", count=1, nodata=FILL_VALUE)
    output = np.asarray(output, dtype="float32")
    output = np.where(np.isfinite(output), output, FILL_VALUE).astype("float32", copy=False)
    stem, ext = os.path.splitext(os.path.basename(out_path))
    fd, tmp_path = tempfile.mkstemp(
        suffix=ext or ".tif",
        prefix=stem + ".loess_tmp_",
        dir=os.path.dirname(out_path) or ".",
    )
    os.close(fd)
    try:
        with rasterio.open(tmp_path, "w", **profile) as dst:
            dst.write(output, 1)
        try:
            if os.path.exists(out_path):
                mode = os.stat(out_path).st_mode
                if not (mode & stat.S_IWRITE):
                    os.chmod(out_path, mode | stat.S_IWRITE)
        except Exception:
            pass
        os.replace(tmp_path, out_path)
        tmp_path = None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    return out_path


def file_display_name(scene_or_path, root_folder=None):
    path = scene_or_path.path if isinstance(scene_or_path, SceneInfo) else str(scene_or_path)
    if root_folder:
        try:
            rel = os.path.relpath(path, root_folder)
            if not rel.startswith(".."):
                return rel
        except Exception:
            pass
    parent = os.path.basename(os.path.dirname(path))
    name = os.path.basename(path)
    return os.path.join(parent, name) if parent else name


def pct_text(value):
    return f"{100.0 * float(value):.1f}%"


def _finite_lst_array(array, nodata=None):
    arr = array.astype("float32", copy=True)
    if nodata is not None:
        try:
            if np.isfinite(nodata):
                arr[arr == float(nodata)] = np.nan
        except Exception:
            pass
    arr[arr == FILL_VALUE] = np.nan
    arr[arr <= FILL_VALUE + 1] = np.nan
    return arr


def looks_kelvin(array):
    finite = np.asarray(array)[np.isfinite(array)]
    if finite.size == 0:
        return True
    med = float(np.nanmedian(finite))
    return med > 150.0


def to_celsius_for_display(array, assume_kelvin=None):
    if assume_kelvin is None:
        assume_kelvin = looks_kelvin(array)
    if assume_kelvin:
        return array.astype("float32", copy=False) - np.float32(KELVIN_TO_CELSIUS_OFFSET)
    return array.astype("float32", copy=False)


def _scaled_shape(width, height, max_size):
    longest = max(int(width), int(height), 1)
    scale = min(1.0, float(max_size) / float(longest))
    out_width = max(1, int(round(width * scale)))
    out_height = max(1, int(round(height * scale)))
    return out_height, out_width


# ---------------------------------------------------------------------------
# LST discovery and date parsing
# ---------------------------------------------------------------------------


def parse_scene_datetime(path):
    """Return (date_or_datetime, label, source). Falls back to file order later."""
    name = os.path.basename(path)

    # ECOSTRESS L2T style: 20250122T221736
    match = re.search(r"(\d{8}T\d{6})", name)
    if match:
        stamp = match.group(1)
        try:
            dt = datetime.datetime.strptime(stamp, "%Y%m%dT%H%M%S")
            return dt, dt.strftime("%Y-%m-%d %H:%M"), "filename YYYYMMDDTHHMMSS"
        except ValueError:
            pass

    # Older DOY style: YYYYDDDHHMMSS, for example 2020205000000.
    match = re.search(r"(?<!\d)(\d{13})(?!\d)", name)
    if match:
        stamp = match.group(1)
        try:
            dt = datetime.datetime.strptime(stamp, "%Y%j%H%M%S")
            return dt, dt.strftime("%Y-%m-%d %H:%M"), "filename YYYYDDDHHMMSS"
        except ValueError:
            pass

    # Date only fallback.
    match = re.search(r"(?<!\d)(20\d{6})(?!\d)", name)
    if match:
        stamp = match.group(1)
        try:
            dt = datetime.datetime.strptime(stamp, "%Y%m%d")
            return dt, dt.strftime("%Y-%m-%d"), "filename YYYYMMDD"
        except ValueError:
            pass

    # Metadata fallback.
    try:
        with rasterio.open(path) as src:
            tags = src.tags()
        candidates = [
            tags.get("TIFFTAG_DATETIME"),
            tags.get("datetime"),
            tags.get("acquisition_time"),
            tags.get("time_coverage_start"),
        ]
        for text in candidates:
            if not text:
                continue
            cleaned = str(text).strip().replace("Z", "")
            for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                try:
                    dt = datetime.datetime.strptime(cleaned[:19], fmt)
                    return dt, dt.strftime("%Y-%m-%d %H:%M"), "metadata"
                except ValueError:
                    continue
    except Exception:
        pass

    return None, "file order", "file order"


def discover_lST_tifs(root_folder):
    root_folder = os.path.abspath(root_folder or SCRIPT_FOLDER)
    patterns = [
        os.path.join(root_folder, "**", "*.tif"),
        os.path.join(root_folder, "**", "*.tiff"),
        os.path.join(root_folder, "**", "*.TIF"),
        os.path.join(root_folder, "**", "*.TIFF"),
    ]
    paths = []
    for pattern in patterns:
        paths.extend(glob.glob(pattern, recursive=True))
    paths = sorted(set(os.path.abspath(p) for p in paths if os.path.isfile(p)))
    paths = [p for p in paths if not _is_inside_output_folder(p, root_folder)]

    chosen = paths
    discovery_note = "Using all .tif/.tiff files found in the working folder, excluding LOESS output folders."

    scenes = []
    parsed = []
    for path in chosen:
        dt, label, source = parse_scene_datetime(path)
        parsed.append((path, dt, label, source))

    # If all dates are missing, use sorted file order with synthetic daily spacing.
    any_real_date = any(item[1] is not None for item in parsed)
    base = datetime.datetime(2000, 1, 1)
    for order_index, (path, dt, label, source) in enumerate(parsed):
        if dt is None:
            dt = base + datetime.timedelta(days=order_index)
            label = f"#{order_index + 1}"
            source = "file order"
        scenes.append(SceneInfo(path=path, date=dt, date_label=label, date_source=source, order_index=order_index))

    if any_real_date:
        scenes.sort(key=lambda s: (s.date, os.path.basename(s.path)))
    else:
        scenes.sort(key=lambda s: os.path.basename(s.path).lower())
        scenes = [replace(s, order_index=i) for i, s in enumerate(scenes)]

    return scenes, discovery_note


# ---------------------------------------------------------------------------
# LOESS baseline engine
# ---------------------------------------------------------------------------


def compute_intersection(scenes):
    datasets = []
    try:
        for scene in scenes:
            datasets.append(rasterio.open(scene.path))
        if not datasets:
            raise ValueError("No readable scenes were found.")

        ref = datasets[0]
        ref_crs = ref.crs
        if ref_crs is None:
            for ds in datasets[1:]:
                if ds.crs is not None:
                    raise ValueError(
                        "Cannot standardize scenes because some rasters have a CRS and others do not."
                    )

        try:
            res_x = abs(float(ref.res[0]))
            res_y = abs(float(ref.res[1]))
        except Exception:
            res_x = abs(float(ref.transform.a))
            res_y = abs(float(ref.transform.e))
        if not np.isfinite(res_x) or res_x <= 0 or not np.isfinite(res_y) or res_y <= 0:
            raise ValueError("Could not determine a valid raster resolution for the standardized grid.")

        left = bottom = right = top = None

        def _bounds_in_reference_crs(ds):
            l, b, r, t = ds.bounds
            if ref_crs is not None and ds.crs is not None and ds.crs != ref_crs:
                return transform_bounds(ds.crs, ref_crs, l, b, r, t, densify_pts=21)
            if ref_crs is not None and ds.crs is None:
                raise ValueError(
                    f"{os.path.basename(ds.name)} has no CRS. Add a CRS or reproject before running LOESS."
                )
            return l, b, r, t

        for ds in datasets:
            l, b, r, t = ds.bounds
            l, b, r, t = _bounds_in_reference_crs(ds)
            if left is None:
                left, bottom, right, top = l, b, r, t
            else:
                left = min(left, l)
                bottom = min(bottom, b)
                right = max(right, r)
                top = max(top, t)

        if left is None or left >= right or bottom >= top:
            raise ValueError("Could not build a standardized grid from the selected scenes.")

        width = int(math.ceil((right - left) / res_x))
        height = int(math.ceil((top - bottom) / res_y))
        if height <= 0 or width <= 0:
            raise ValueError("The standardized grid has non-positive dimensions.")

        right = left + width * res_x
        bottom = top - height * res_y
        transform = Affine(res_x, 0.0, left, 0.0, -res_y, top)
        profile = ref.profile.copy()
        profile.update(
            driver="GTiff",
            height=height,
            width=width,
            count=1,
            dtype="float32",
            crs=ref_crs,
            transform=transform,
            nodata=FILL_VALUE,
            compress="deflate",
            predictor=3,
            zlevel=6,
            bigtiff="IF_SAFER",
        )
        return IntersectionInfo(
            row_off=0,
            col_off=0,
            height=height,
            width=width,
            bounds=(left, bottom, right, top),
            transform=transform,
            crs=ref_crs,
            profile=profile,
        )
    finally:
        for ds in datasets:
            try:
                ds.close()
            except Exception:
                pass


def read_scene_on_common_grid(scene: SceneInfo, grid: IntersectionInfo, resampling=Resampling.nearest):
    """Read one scene into the standardized LOESS grid, returning NaN for missing data."""
    dest = np.full((int(grid.height), int(grid.width)), FILL_VALUE, dtype="float32")
    with rasterio.open(scene.path) as src:
        src_crs = src.crs or grid.crs
        dst_crs = grid.crs or src.crs
        if src_crs is None or dst_crs is None:
            if src.width == grid.width and src.height == grid.height:
                arr = src.read(1).astype("float32", copy=False)
                return _finite_lst_array(arr, src.nodata)
            raise ValueError(
                f"{os.path.basename(scene.path)} has no CRS and cannot be standardized to the common grid."
            )
        src_nodata = src.nodata if src.nodata is not None else FILL_VALUE
        reproject(
            source=rasterio.band(src, 1),
            destination=dest,
            src_transform=src.transform,
            src_crs=src_crs,
            src_nodata=src_nodata,
            dst_transform=grid.transform,
            dst_crs=dst_crs,
            dst_nodata=FILL_VALUE,
            resampling=resampling,
        )
    return _finite_lst_array(dest, FILL_VALUE)


def _downsample_array_nearest(arr, max_size):
    arr = np.asarray(arr)
    out_height, out_width = _scaled_shape(arr.shape[1], arr.shape[0], max_size)
    if out_height == arr.shape[0] and out_width == arr.shape[1]:
        return arr
    row_idx = np.linspace(0, arr.shape[0] - 1, out_height).round().astype(np.int64)
    col_idx = np.linspace(0, arr.shape[1] - 1, out_width).round().astype(np.int64)
    return arr[np.ix_(row_idx, col_idx)]


def sample_candidate_pixels(height, width, n_candidates, rng=None):
    rng = rng or random.Random()
    total_pixels = int(height) * int(width)
    target = int(max(1, min(int(n_candidates), total_pixels)))
    candidates = set()
    while len(candidates) < target:
        rr = rng.randint(0, int(height) - 1)
        cc = rng.randint(0, int(width) - 1)
        candidates.add((rr, cc))
    return list(candidates)


def read_candidate_values(scenes, intersection, candidate_pixels, progress_callback=None):
    n_scenes = len(scenes)
    n_pixels = len(candidate_pixels)
    vals = np.full((n_scenes, n_pixels), np.nan, dtype=np.float32)
    rows = np.asarray([p[0] for p in candidate_pixels], dtype=np.int64)
    cols = np.asarray([p[1] for p in candidate_pixels], dtype=np.int64)

    for i, scene in enumerate(scenes):
        if progress_callback:
            progress_callback(
                i + 1,
                n_scenes,
                f"Reading scene {i + 1} of {n_scenes}: {os.path.basename(scene.path)}",
            )
        arr = read_scene_on_common_grid(scene, intersection, Resampling.nearest)
        vals[i, :] = arr[rows, cols]
    return vals


def build_time_axis(scenes):
    dates = [scene.date for scene in scenes]
    start = min(dates)
    x_full = np.array([(d - start).total_seconds() / 86400.0 for d in dates], dtype=float)
    if not np.any(np.diff(np.sort(x_full))):
        x_full = np.arange(len(scenes), dtype=float)
    return dates, x_full


def _fit_loess_baseline(y, x_full, frac_val, it_val):
    finite_mask = np.isfinite(y) & np.isfinite(x_full)
    if np.count_nonzero(finite_mask) < 3:
        return None
    x = x_full[finite_mask]
    vals = y[finite_mask]
    order = np.argsort(x)
    x = x[order]
    vals = vals[order]

    # LOWESS can struggle when x has duplicates. Collapse duplicate x values.
    uniq_x, inverse = np.unique(x, return_inverse=True)
    if uniq_x.size < 3:
        return None
    uniq_y = np.zeros_like(uniq_x, dtype=float)
    for k in range(uniq_x.size):
        uniq_y[k] = float(np.nanmean(vals[inverse == k]))

    frac = float(max(0.02, min(1.0, frac_val)))
    it = int(max(0, min(30, it_val)))
    loess_out = lowess(uniq_y, uniq_x, frac=frac, it=it, return_sorted=True)
    baseline = np.interp(x_full, loess_out[:, 0], loess_out[:, 1])
    return baseline.astype("float32")


def _normalize_score(values, default=0.0):
    """Normalize a numeric vector to 0-1 while handling constants and NaNs."""
    arr = np.asarray(values, dtype=float)
    out = np.full(arr.shape, float(default), dtype=float)
    finite = np.isfinite(arr)
    if not np.any(finite):
        return out
    vmin = float(np.nanmin(arr[finite]))
    vmax = float(np.nanmax(arr[finite]))
    if vmax <= vmin:
        out[finite] = 1.0
    else:
        out[finite] = (arr[finite] - vmin) / (vmax - vmin)
    return out


def _variance_bin_edges(variances, max_bins):
    """Return quantile bin edges for variance-stratified pixel selection."""
    finite = np.asarray(variances, dtype=float)
    finite = finite[np.isfinite(finite)]
    if finite.size == 0:
        return None
    unique = np.unique(finite)
    if unique.size <= 1:
        return None
    n_bins = int(max(1, min(max_bins, unique.size, finite.size)))
    if n_bins <= 1:
        return None
    edges = np.nanquantile(finite, np.linspace(0.0, 1.0, n_bins + 1))
    edges = np.unique(edges)
    if edges.size <= 2:
        return None
    # Internal edges only. The first/last bounds are implicit.
    return edges[1:-1]


def select_representative_baseline_pixels(valid_baselines, vals, candidate_pixels, n_desired):
    """Select baseline pixels using data completeness and variance diversity.

    Earlier versions selected the final baseline pixels only after applying a
    valid-observation depth threshold and then ranking by mean baseline value.
    That can over-focus the baseline on one narrow type of pixel. This selector
    first keeps only candidates that can be smoothed by LOESS, then deliberately
    samples across the distribution of temporal variance so low-, medium-, and
    high-variability parts of the scene can contribute. Within each variance
    group, pixels with more valid observations are preferred. A small spatial
    spread term discourages all selected pixels from clustering in one corner of
    the standardized grid.
    """
    records = []
    n_scenes = int(vals.shape[0]) if getattr(vals, "shape", None) is not None else 0
    if n_scenes <= 0:
        n_scenes = 1

    for idx, baseline, avg_baseline in valid_baselines:
        idx = int(idx)
        y = vals[:, idx]
        finite = np.isfinite(y)
        valid_count = int(np.count_nonzero(finite))
        if valid_count <= 0:
            continue
        with np.errstate(invalid="ignore"):
            variance = float(np.nanvar(y.astype(float)))
        if not np.isfinite(variance):
            variance = 0.0
        try:
            row, col = candidate_pixels[idx]
        except Exception:
            row, col = 0, 0
        records.append({
            "idx": idx,
            "baseline": baseline,
            "avg_baseline": float(avg_baseline),
            "valid_count": valid_count,
            "completeness": float(valid_count) / float(max(1, n_scenes)),
            "variance": variance,
            "row": float(row),
            "col": float(col),
        })

    if not records:
        return [], {}

    n_take = int(max(1, min(int(n_desired), len(records))))
    variances = np.asarray([rec["variance"] for rec in records], dtype=float)
    completeness = np.asarray([rec["completeness"] for rec in records], dtype=float)

    # Use up to 8 variance groups. This keeps the selection balanced without
    # forcing too many tiny bins when n_desired is small.
    max_bins = max(1, min(8, n_take, len(records)))
    edges = _variance_bin_edges(variances, max_bins=max_bins)
    if edges is None:
        for rec in records:
            rec["variance_bin"] = 0
        n_bins = 1
    else:
        bin_ids = np.searchsorted(edges, variances, side="right")
        for rec, bin_id in zip(records, bin_ids):
            rec["variance_bin"] = int(bin_id)
        n_bins = int(np.max(bin_ids)) + 1 if bin_ids.size else 1

    rows = np.asarray([rec["row"] for rec in records], dtype=float)
    cols = np.asarray([rec["col"] for rec in records], dtype=float)
    row_span = max(1.0, float(np.nanmax(rows) - np.nanmin(rows)) if rows.size else 1.0)
    col_span = max(1.0, float(np.nanmax(cols) - np.nanmin(cols)) if cols.size else 1.0)
    spatial_diag = math.hypot(row_span, col_span) or 1.0

    # Base score is mostly completeness. Variance is not used as "higher is
    # better" here because the goal is representation across variance groups,
    # not selecting only highly variable pixels.
    completeness_score = _normalize_score(completeness, default=1.0)
    for rec, comp_score in zip(records, completeness_score):
        rec["base_score"] = float(comp_score)

    remaining = list(records)
    selected = []

    def pick_from(pool):
        if not pool:
            return None
        if not selected:
            return max(pool, key=lambda rec: (rec["base_score"], rec["valid_count"], -rec["variance"]))
        selected_xy = np.asarray([(rec["row"], rec["col"]) for rec in selected], dtype=float)
        best = None
        best_score = None
        for rec in pool:
            d = np.sqrt((selected_xy[:, 0] - rec["row"]) ** 2 + (selected_xy[:, 1] - rec["col"]) ** 2)
            distance_score = float(np.nanmin(d)) / spatial_diag if d.size else 1.0
            # Completeness drives the choice, while distance prevents clusters.
            score = 0.78 * rec["base_score"] + 0.22 * min(1.0, max(0.0, distance_score))
            key = (score, rec["valid_count"], -abs(rec["variance"] - float(np.nanmedian(variances))))
            if best_score is None or key > best_score:
                best = rec
                best_score = key
        return best

    # Round-robin through variance bins so the chosen pixels cover the variance
    # distribution rather than only one part of it.
    bin_order = list(range(max(1, n_bins)))
    while remaining and len(selected) < n_take:
        made_pick = False
        for bin_id in bin_order:
            if len(selected) >= n_take:
                break
            pool = [rec for rec in remaining if rec.get("variance_bin", 0) == bin_id]
            pick = pick_from(pool)
            if pick is None:
                continue
            selected.append(pick)
            remaining.remove(pick)
            made_pick = True
        if not made_pick:
            pick = pick_from(remaining)
            if pick is None:
                break
            selected.append(pick)
            remaining.remove(pick)

    # Final order follows variance bins for easy interpretation in logs/exports.
    selected.sort(key=lambda rec: (rec.get("variance_bin", 0), -rec["valid_count"], rec["variance"]))
    selected_indices = [rec["idx"] for rec in selected]
    selection_info = {
        "selection_method": "valid_observation_and_variance_stratified",
        "variance_bin_count": int(max(1, n_bins)),
        "candidate_count": int(len(records)),
        "selected_count": int(len(selected)),
        "selected_valid_count_min": int(min(rec["valid_count"] for rec in selected)) if selected else 0,
        "selected_valid_count_max": int(max(rec["valid_count"] for rec in selected)) if selected else 0,
        "selected_variance_min": float(min(rec["variance"] for rec in selected)) if selected else 0.0,
        "selected_variance_max": float(max(rec["variance"] for rec in selected)) if selected else 0.0,
    }
    return selected_indices, selection_info


def compute_loess_model(scenes, params: LoessParams, seed=None, progress_callback=None):
    if len(scenes) < 3:
        raise ValueError("LOESS needs at least 3 scenes to build a useful time series.")
    params = LoessParams(
        n_desired=max(1, int(params.n_desired)),
        depth_required=max(3, int(params.depth_required)),
        n_candidates_initial=max(10, int(params.n_candidates_initial)),
        frac_val=float(params.frac_val),
        it_val=max(0, int(params.it_val)),
        threshold_cold=float(params.threshold_cold),
        threshold_hot=float(params.threshold_hot),
    )
    if params.depth_required > len(scenes):
        params = replace(params, depth_required=len(scenes))

    warnings = []
    scene_total = len(scenes)
    if progress_callback:
        progress_callback(0, scene_total, "Building standardized raster grid")
    intersection = compute_intersection(scenes)

    if seed is None:
        rng = random.Random()
        seed_label = "random"
    else:
        rng = random.Random(seed)
        seed_label = str(seed)

    if progress_callback:
        progress_callback(0, scene_total, "Sampling candidate pixels")
    candidate_pixels = sample_candidate_pixels(
        intersection.height,
        intersection.width,
        params.n_candidates_initial,
        rng=rng,
    )

    if progress_callback:
        progress_callback(0, scene_total, "Reading candidate time series")
    vals = read_candidate_values(
        scenes,
        intersection,
        candidate_pixels,
        progress_callback=progress_callback,
    )

    valid_counts = np.sum(np.isfinite(vals), axis=0)
    valid_indices = np.where(valid_counts >= params.depth_required)[0]
    if len(valid_indices) < max(1, min(5, params.n_desired)):
        raise ValueError(
            f"Only {len(valid_indices)} candidate pixels had at least {params.depth_required} valid values. "
            "Increase n_candidates_initial or lower depth_required."
        )

    dates, x_full = build_time_axis(scenes)
    if progress_callback:
        progress_callback(scene_total, scene_total, "Fitting LOESS baselines")

    valid_baselines = []
    for idx in valid_indices:
        baseline = _fit_loess_baseline(vals[:, idx], x_full, params.frac_val, params.it_val)
        if baseline is None or not np.any(np.isfinite(baseline)):
            continue
        avg_baseline = float(np.nanmean(baseline))
        valid_baselines.append((int(idx), baseline, avg_baseline))

    if len(valid_baselines) < max(1, min(5, params.n_desired)):
        raise ValueError(
            "Not enough candidate pixels could be smoothed by LOESS. Try increasing the candidate pool."
        )

    selected_idx_list, selection_info = select_representative_baseline_pixels(
        valid_baselines,
        vals,
        candidate_pixels,
        params.n_desired,
    )
    n_take = len(selected_idx_list)
    if n_take < params.n_desired:
        warnings.append(f"Selected {n_take} pixels because only {len(valid_baselines)} candidates were usable.")
    if selection_info:
        warnings.append(
            "Baseline pixels selected using valid-observation coverage and "
            f"variance-stratified sampling across {selection_info.get('variance_bin_count', 1)} variance group(s)."
        )
    baseline_by_idx = {int(idx): baseline for idx, baseline, _avg in valid_baselines}
    selected_indices = np.array(selected_idx_list, dtype=np.int64)
    selected_baselines = np.array([baseline_by_idx[int(idx)] for idx in selected_indices], dtype=np.float32)
    final_baseline = np.nanmean(selected_baselines, axis=0).astype("float32")

    if progress_callback:
        progress_callback(scene_total, scene_total, "LOESS model ready")

    return LoessModel(
        scenes=scenes,
        params=params,
        dates=dates,
        x_full=x_full,
        intersection=intersection,
        candidate_pixels=candidate_pixels,
        candidate_values=vals,
        valid_indices=valid_indices,
        selected_indices=selected_indices,
        selected_baselines=selected_baselines,
        final_baseline=final_baseline,
        seed_label=seed_label,
        warnings=tuple(warnings),
    )


def build_outlier_mask(array_window, base_val, params: LoessParams):
    finite = np.isfinite(array_window)
    return (
        ((array_window < (base_val - params.threshold_cold)) |
         (array_window > (base_val + params.threshold_hot))) & finite
    )


def apply_loess_to_scene(scene: SceneInfo, scene_index: int, model: LoessModel, options: BatchOptions):
    params = model.params
    base_val = float(model.final_baseline[scene_index])
    inter = model.intersection
    data_full = read_scene_on_common_grid(scene, inter, Resampling.nearest)
    outlier_mask = build_outlier_mask(data_full, base_val, params)
    modified_full = data_full.copy()
    modified_full[outlier_mask] = np.nan
    profile = dict(inter.profile or {})

    out_path = output_path_for_scene(scene.path, options)
    _write_array_to_tif_path(out_path, modified_full.astype("float32", copy=False), profile)
    total_valid = int(np.count_nonzero(np.isfinite(data_full)))
    removed = int(np.count_nonzero(outlier_mask))
    removed_fraction = removed / float(total_valid) if total_valid else 0.0
    return {
        "out_path": out_path,
        "removed": removed,
        "removed_fraction": removed_fraction,
        "base_val": base_val,
    }


def read_scene_preview(scene: SceneInfo, max_size=PREVIEW_MAX_SIZE):
    with rasterio.open(scene.path) as src:
        out_height, out_width = _scaled_shape(src.width, src.height, max_size)
        arr = src.read(1, out_shape=(out_height, out_width), resampling=Resampling.nearest)
        arr = _finite_lst_array(arr, src.nodata)
        scale_x = float(src.width) / float(out_width)
        scale_y = float(src.height) / float(out_height)
        preview_transform = src.transform * Affine.scale(scale_x, scale_y)
        return arr, preview_transform, src.crs


def read_intersection_preview(scene: SceneInfo, model: LoessModel, max_size=REPRESENTATIVE_SCAN_SIZE):
    arr = read_scene_on_common_grid(scene, model.intersection, Resampling.nearest)
    return _downsample_array_nearest(arr, max_size)



def read_full_scene_preview_for_scoring(scene: SceneInfo, max_size=REPRESENTATIVE_SCAN_SIZE):
    """Read a downsampled full-scene preview for choosing visible example scenes."""
    with rasterio.open(scene.path) as src:
        out_height, out_width = _scaled_shape(src.width, src.height, max_size)
        arr = src.read(1, out_shape=(out_height, out_width), resampling=Resampling.nearest)
        arr = _finite_lst_array(arr, src.nodata)
        return arr


def score_representative_scene(scene: SceneInfo, scene_index: int, model: LoessModel):
    """Score preview examples without hard rejecting partially masked scenes.

    The previous version used a strict 60% valid / 40% NaN gate inside the shared
    scene area. That was too brittle because the standardized grid can be clouded
    or already masked even when the full scene is useful for visual review. This
    selector now prioritizes scenes with the most full-scene data available, then
    uses LOESS outlier fraction and temperature variability only as tie-breakers.
    """
    try:
        full_arr = read_full_scene_preview_for_scoring(scene, REPRESENTATIVE_SCAN_SIZE)
        finite = np.isfinite(full_arr)
        total = int(full_arr.size)
        finite_count = int(np.count_nonzero(finite))
        if total <= 0 or finite_count <= 0:
            return replace(
                scene,
                score=-1.0,
                finite_fraction=0.0,
                missing_fraction=1.0,
                temp_std=0.0,
                outlier_fraction=0.0,
            )

        finite_fraction = finite_count / float(total)
        missing_fraction = 1.0 - finite_fraction
        temp_std = float(np.nanstd(full_arr[finite])) if finite_count else 0.0

        outlier_fraction = 0.0
        try:
            inter_arr = read_intersection_preview(scene, model, REPRESENTATIVE_SCAN_SIZE)
            inter_finite = np.isfinite(inter_arr)
            inter_count = int(np.count_nonzero(inter_finite))
            if inter_count > 0:
                base = float(model.final_baseline[scene_index])
                outlier = build_outlier_mask(inter_arr, base, model.params)
                outlier_fraction = float(np.count_nonzero(outlier)) / float(inter_count)
        except Exception:
            outlier_fraction = 0.0

        # Main goal: pick full-looking scenes users can visually inspect.
        # Tie-breakers favor scenes where LOESS actually removes visible pixels.
        valid_score = 10.0 * finite_fraction
        filtering_score = 5.0 * min(0.25, outlier_fraction) / 0.25
        variability_score = 1.5 * min(1.0, temp_std / 8.0)
        score = valid_score + filtering_score + variability_score

        return replace(
            scene,
            score=float(score),
            finite_fraction=finite_fraction,
            missing_fraction=missing_fraction,
            temp_std=temp_std,
            outlier_fraction=outlier_fraction,
        )
    except Exception:
        return replace(scene, score=-1.0, finite_fraction=0.0, missing_fraction=1.0)


def eligible_preview_scenes(scored_scenes):
    """Return preview scenes users can cycle through.

    Prefer scenes with any real data. If every scene looks empty, keep the full
    list anyway so the GUI can show a diagnostic blank scene instead of failing.
    """
    scenes = list(scored_scenes or [])
    usable = [scene for scene in scenes if scene.finite_fraction > 0.0 and scene.score >= 0.0]
    if usable:
        return sorted(usable, key=lambda s: (s.score, s.finite_fraction), reverse=True)
    return sorted(scenes, key=lambda s: (s.score, s.finite_fraction), reverse=True)


def choose_representative_scene(model: LoessModel, progress_callback=None):
    scored = []
    total = len(model.scenes)
    for i, scene in enumerate(model.scenes):
        if progress_callback:
            progress_callback(i, total, f"Selecting example scene: {os.path.basename(scene.path)}")
        scored.append(score_representative_scene(scene, i, model))
    scored.sort(key=lambda s: (s.score, s.finite_fraction), reverse=True)
    usable = eligible_preview_scenes(scored)
    if progress_callback:
        progress_callback(total, total, "Example scene selected")
    return usable[0] if usable else None, scored

def make_scene_preview_arrays(scene: SceneInfo, model: LoessModel):
    idx = next((i for i, s in enumerate(model.scenes) if s.path == scene.path), None)
    if idx is None:
        raise ValueError("Preview scene is not part of the current LOESS model.")

    full_res = read_scene_on_common_grid(scene, model.intersection, Resampling.nearest)
    full = _downsample_array_nearest(full_res, PREVIEW_MAX_SIZE).astype("float32", copy=False)
    modified = full.copy()
    base = float(model.final_baseline[idx])
    outlier = build_outlier_mask(modified, base, model.params)
    modified[outlier] = np.nan
    r0, c0, r1, c1 = 0, 0, int(full.shape[0]), int(full.shape[1])

    return full, modified, outlier, (r0, c0, r1, c1), base



def make_scene_full_arrays_for_save(scene: SceneInfo, model: LoessModel):
    """Return full-resolution original and LOESS-filtered arrays for snapshot export."""
    idx = next((i for i, s in enumerate(model.scenes) if s.path == scene.path), None)
    if idx is None:
        raise ValueError("Preview scene is not part of the current LOESS model.")

    inter = model.intersection
    base = float(model.final_baseline[idx])

    original = read_scene_on_common_grid(scene, inter, Resampling.nearest)
    filtered = original.copy()
    outlier_mask = build_outlier_mask(filtered, base, model.params)
    filtered[outlier_mask] = np.nan
    profile = dict(inter.profile or {})
    return original.astype("float32", copy=False), filtered.astype("float32", copy=False), profile, base, int(np.count_nonzero(outlier_mask))


def selected_sample_pixels(model: LoessModel):
    """Return the selected LOESS baseline pixels in full-scene pixel coordinates."""
    if model is None:
        return []
    inter = model.intersection
    out = []
    for sample_id, cand_idx in enumerate(np.asarray(model.selected_indices, dtype=np.int64), start=1):
        try:
            local_row, local_col = model.candidate_pixels[int(cand_idx)]
        except Exception:
            continue
        full_row = int(inter.row_off + int(local_row))
        full_col = int(inter.col_off + int(local_col))
        out.append({
            "sample_id": int(sample_id),
            "candidate_index": int(cand_idx),
            "local_row": int(local_row),
            "local_col": int(local_col),
            "row": int(full_row),
            "col": int(full_col),
        })
    return out


def selected_sample_preview_points(scene: SceneInfo, model: LoessModel, preview_shape):
    """Return selected baseline pixel locations as preview-image x/y coordinates."""
    if scene is None or model is None or preview_shape is None:
        return []
    try:
        preview_h, preview_w = int(preview_shape[0]), int(preview_shape[1])
        if preview_h <= 0 or preview_w <= 0:
            return []
        scale_x = float(model.intersection.width) / float(preview_w)
        scale_y = float(model.intersection.height) / float(preview_h)
    except Exception:
        return []
    pts = []
    for item in selected_sample_pixels(model):
        # Coordinates are in Matplotlib image pixel space. The +0.5/-0.5 keeps
        # the marker centered on the downsampled preview pixel center.
        x = (float(item["col"]) + 0.5) / scale_x - 0.5
        y = (float(item["row"]) + 0.5) / scale_y - 0.5
        if np.isfinite(x) and np.isfinite(y):
            pts.append((x, y))
    return pts


def _contrast_edge_color(fill_color):
    try:
        r, g, b = mcolors.to_rgb(fill_color)
        luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
        return "#000000" if luminance > 0.58 else "#FFFFFF"
    except Exception:
        return "#000000"


def write_sample_pixels_shapefile(out_path, scene: SceneInfo, model: LoessModel):
    """Write selected LOESS baseline pixels as a point shapefile."""
    if fiona is None:
        raise RuntimeError("Fiona is required to write sample_pixels.shp. Install it with: pip install fiona")
    pixels = selected_sample_pixels(model)
    if not pixels:
        raise ValueError("No selected LOESS sample pixels are available to save.")

    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    # Clean up shapefile sidecars if a previous partial write exists.
    root, _ = os.path.splitext(out_path)
    for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
        try:
            os.remove(root + ext)
        except FileNotFoundError:
            pass
        except Exception:
            pass

    schema = {
        "geometry": "Point",
        "properties": {
            "sample_id": "int",
            "cand_idx": "int",
            "row": "int",
            "col": "int",
            "loc_row": "int",
            "loc_col": "int",
        },
    }

    crs_obj = model.intersection.crs
    crs_wkt = crs_obj.to_wkt() if crs_obj is not None else None
    transform = model.intersection.transform
    features = []
    for item in pixels:
        x, y = transform * (float(item["col"]) + 0.5, float(item["row"]) + 0.5)
        features.append({
            "geometry": {"type": "Point", "coordinates": (float(x), float(y))},
            "properties": {
                "sample_id": int(item["sample_id"]),
                "cand_idx": int(item["candidate_index"]),
                "row": int(item["row"]),
                "col": int(item["col"]),
                "loc_row": int(item["local_row"]),
                "loc_col": int(item["local_col"]),
            },
        })

    open_kwargs = {"driver": "ESRI Shapefile", "schema": schema}
    if crs_wkt:
        open_kwargs["crs_wkt"] = crs_wkt
    with fiona.open(out_path, "w", **open_kwargs) as dst:
        for feat in features:
            dst.write(feat)
    return out_path


# ---------------------------------------------------------------------------
# EPSG:4326 reprojection helpers for LOESS outputs
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
        if name.startswith("reproj_") or ".loess_tmp" in name:
            continue
        seen.add(key)
        out.append(abs_path)
    return sorted(out, key=lambda p: (os.path.dirname(p).lower(), os.path.basename(p).lower()))


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


def _reproject_item(path, status, will_convert=False, source_crs="", detail=""):
    return {
        "path": os.path.abspath(path),
        "name": os.path.basename(path),
        "kind": "raster",
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
                return _reproject_item(path, "missing_crs", detail="No CRS found.")
            try:
                src_crs = CRS.from_user_input(src.crs)
            except Exception as exc:
                return _reproject_item(
                    path,
                    "unreadable_crs",
                    source_crs=_reproject_crs_label(src.crs),
                    detail=f"Could not parse CRS: {exc}",
                )
            if src_crs == dst_crs:
                return _reproject_item(
                    path,
                    "already_4326",
                    source_crs=_reproject_crs_label(src_crs),
                    detail="Already EPSG:4326.",
                )
            return _reproject_item(
                path,
                "convert",
                will_convert=True,
                source_crs=_reproject_crs_label(src_crs),
                detail=f"{_reproject_crs_label(src_crs)} to {REPROJECT_TARGET_CRS_LABEL}",
            )
    except Exception as exc:
        return _reproject_item(path, "error", detail=str(exc))


def discover_reproject_targets_in_loess_outputs(root_folder=None):
    root = os.path.abspath(root_folder or SCRIPT_FOLDER)
    folder = os.path.join(root, DEFAULT_OUTPUT_FOLDER)
    if not os.path.isdir(folder):
        return []
    paths = _unique_sorted_paths(
        glob.glob(os.path.join(folder, "**", "*.tif"), recursive=True)
        + glob.glob(os.path.join(folder, "**", "*.tiff"), recursive=True)
    )
    paths = [p for p in paths if DEFAULT_REPROJECT_APPEND_FOLDER.lower() not in [part.lower() for part in p.split(os.sep)]]
    return [_scan_reproject_raster(path) for path in paths]


def summarize_reproject_targets(items):
    total = len(items or [])
    convert_total = sum(1 for item in items or [] if item.get("will_convert"))
    already_total = sum(1 for item in items or [] if item.get("status") == "already_4326")
    issue_total = total - convert_total - already_total
    return {
        "total": total,
        "convert_total": convert_total,
        "already_total": already_total,
        "issue_total": issue_total,
    }


def _format_reproject_summary(counts):
    total = int(counts.get("total", 0))
    convert_total = int(counts.get("convert_total", 0))
    already_total = int(counts.get("already_total", 0))
    issue_total = int(counts.get("issue_total", 0))
    if total <= 0:
        return "No LOESS output TIFF files were found."
    if convert_total <= 0:
        if issue_total:
            return (
                f"No files can be converted. {already_total} file(s) are already EPSG:4326; "
                f"{issue_total} file(s) are missing CRS information or unreadable."
            )
        return f"No reprojection is needed. {already_total} file(s) are already EPSG:4326."
    summary = f"{convert_total} LOESS output TIFF file(s) will be converted to EPSG:4326."
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


def _loess_output_base_for_path(src_path):
    abs_path = os.path.abspath(src_path)
    parts = abs_path.split(os.sep)
    for index, part in enumerate(parts):
        if part.lower() == DEFAULT_OUTPUT_FOLDER.lower():
            return os.sep.join(parts[:index + 1]) or os.sep
    return None


def _reproject_append_output_path(src_path, suffix=DEFAULT_REPROJECT_SUFFIX):
    src_path = os.path.abspath(src_path)
    base_folder = _loess_output_base_for_path(src_path)
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


def _reproject_output_path(src_path, output_mode="append", suffix=DEFAULT_REPROJECT_SUFFIX):
    if str(output_mode).lower() == "overwrite":
        return os.path.abspath(src_path)
    return _reproject_append_output_path(src_path, suffix)


def reproject_raster_to_4326(src_path, output_mode="append", suffix=DEFAULT_REPROJECT_SUFFIX):
    dst_crs = CRS.from_epsg(4326)
    out_path = _reproject_output_path(src_path, output_mode, suffix)
    tmp_path = None
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
                profile.update(
                    crs=dst_crs,
                    transform=transform,
                    width=width,
                    height=height,
                    compress="deflate",
                    predictor=3 if str(profile.get("dtype", "")).startswith("float") else 2,
                    zlevel=6,
                    bigtiff="IF_SAFER",
                )
                profile = _sanitize_reproject_raster_profile(profile, src)
                nodata = src.nodata
                if nodata is not None:
                    profile["nodata"] = nodata

                fd, tmp_path = tempfile.mkstemp(
                    suffix=os.path.splitext(out_path)[1] or ".tif",
                    prefix="reproj_",
                    dir=os.path.dirname(out_path) or ".",
                )
                os.close(fd)
                with rasterio.open(tmp_path, "w", **profile) as dst:
                    try:
                        dst.update_tags(**src.tags())
                    except Exception:
                        pass
                    for bidx in range(1, src.count + 1):
                        reproject(
                            source=rasterio.band(src, bidx),
                            destination=rasterio.band(dst, bidx),
                            src_transform=src.transform,
                            src_crs=src.crs,
                            dst_transform=transform,
                            dst_crs=dst_crs,
                            resampling=Resampling.nearest,
                            src_nodata=nodata,
                            dst_nodata=nodata,
                            num_threads=REPROJECT_THREADS,
                        )
                        try:
                            dst.update_tags(bidx, **src.tags(bidx))
                        except Exception:
                            pass
        _replace_with_reproject_retries(tmp_path, out_path)
        tmp_path = None
        action = "Overwritten" if str(output_mode).lower() == "overwrite" else "Appended"
        return f"{action}: {os.path.basename(out_path)}", out_path
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def batch_reproject_targets_to_4326(targets, output_mode="append", suffix=DEFAULT_REPROJECT_SUFFIX, progress_callback=None):
    convert_items = [item for item in list(targets or []) if item.get("will_convert")]
    total = len(convert_items)
    results = []
    _release_reproject_file_handles()
    for index, item in enumerate(convert_items, start=1):
        if progress_callback:
            progress_callback(index - 1, total, item, "Converting")
        try:
            message, output_path = reproject_raster_to_4326(item["path"], output_mode, suffix)
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


# ---------------------------------------------------------------------------
# Qt widgets
# ---------------------------------------------------------------------------


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
        self.value_edit = QtWidgets.QLineEdit()
        self.value_edit.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        self.value_edit.setFixedWidth(82)
        self.value_edit.setToolTip("Type an exact value, then press Enter or click away.")
        self.value_edit.editingFinished.connect(self._edit_finished)
        self.value_edit.returnPressed.connect(self._edit_finished)
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
        raw = 0 if self._maximum <= self._minimum else int(round((value - self._minimum) / (self._maximum - self._minimum) * 1000.0))
        raw = max(0, min(1000, raw))
        old = self._block_emit
        self._block_emit = True
        self.slider.setValue(raw)
        self._block_emit = old
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
            return
        self._block_edit_update = True
        self.value_edit.setText(self._format_value(value))
        self._block_edit_update = False

    def _parse_edit_value(self):
        text = self.value_edit.text().strip()
        if not text:
            return self.value()
        for token in ("%", "°C", "°", "C", "c"):
            text = text.replace(token, "")
        try:
            return float(text.strip())
        except Exception:
            return self.value()

    def _edit_finished(self):
        if self._block_edit_update:
            return
        self.set_value(self._parse_edit_value(), emit=True)

    def _slider_changed(self):
        value = self.value()
        self._update_edit(value)
        if not self._block_emit:
            self.valueChanged.emit(value)


class IntSlider(FloatSlider):
    valueChangedInt = QtCore.pyqtSignal(int)

    def __init__(self, title, minimum=0, maximum=100, value=0, parent=None):
        super().__init__(title, minimum, maximum, value, decimals=0, parent=parent)
        self.valueChanged.connect(lambda v: self.valueChangedInt.emit(self.value()))

    def value(self):
        return int(round(super().value()))

    def set_value(self, value, emit=True):
        super().set_value(int(round(float(value))), emit=emit)


class LoessGraphCanvas(FigureCanvas):
    def __init__(self, parent=None):
        self.theme_mode = "dark"
        self.theme = theme_values(self.theme_mode)
        self.figure = Figure(figsize=(9, 10), dpi=100, facecolor=self.theme["figure"])
        super().__init__(self.figure)
        self.setParent(parent)
        self.clear_message("Scan a folder, then build LOESS graphs.")

    def set_theme(self, mode="dark"):
        self.theme_mode = "light" if str(mode).lower() == "light" else "dark"
        self.theme = theme_values(self.theme_mode)
        try:
            self.figure.set_facecolor(self.theme["figure"])
            for ax in self.figure.axes:
                ax.set_facecolor(self.theme["axes"])
                ax.tick_params(colors=self.theme["muted"], labelsize=8)
                for spine in ax.spines.values():
                    spine.set_color(self.theme["spine"])
        except Exception:
            pass
        self.draw_idle()

    def clear_message(self, message):
        self.figure.clear()
        ax = self.figure.add_subplot(111)
        ax.set_facecolor(self.theme["axes"])
        ax.text(0.5, 0.5, message, ha="center", va="center", color=self.theme["text"], fontsize=12, wrap=True)
        ax.set_axis_off()
        self.draw_idle()

    def plot_model(self, model: LoessModel, max_pixels=5):
        self.figure.clear()
        self.figure.set_facecolor(self.theme["figure"])
        n_show = min(max_pixels, len(model.selected_indices))
        if n_show <= 0:
            self.clear_message("No selected LOESS pixels to plot.")
            return

        axes = self.figure.subplots(n_show, 2, sharex=True)
        if n_show == 1:
            axes = np.array([axes])

        dates = np.array(model.dates)
        x_dates_ok = model.scenes[0].date_source != "file order"
        model_is_kelvin = looks_kelvin(model.candidate_values)
        final_c = to_celsius_for_display(model.final_baseline, assume_kelvin=model_is_kelvin)
        all_y_for_limits = []

        for row in range(n_show):
            idx = int(model.selected_indices[row])
            y_raw = model.candidate_values[:, idx]
            b_raw = model.selected_baselines[row]
            y = to_celsius_for_display(y_raw)
            b = to_celsius_for_display(b_raw, assume_kelvin=looks_kelvin(y_raw))
            all_y_for_limits.extend(list(y[np.isfinite(y)]))
            all_y_for_limits.extend(list(b[np.isfinite(b)]))
            finite = np.isfinite(y_raw)
            out_ind = (((y_raw < (b_raw - model.params.threshold_cold)) |
                        (y_raw > (b_raw + model.params.threshold_hot))) & finite)
            out_avg = (((y_raw < (model.final_baseline - model.params.threshold_cold)) |
                        (y_raw > (model.final_baseline + model.params.threshold_hot))) & finite)
            rr, cc = model.candidate_pixels[idx]
            ax_l, ax_r = axes[row]
            for ax in (ax_l, ax_r):
                ax.set_facecolor(self.theme["axes"])
                ax.tick_params(colors=self.theme["muted"], labelsize=8)
                for spine in ax.spines.values():
                    spine.set_color(self.theme["spine"])
                ax.grid(True, color=self.theme["grid"], linewidth=0.5, alpha=0.8)

            x = dates if x_dates_ok else np.arange(len(y))
            ax_l.plot(x, y, "o", ms=4, lw=1, alpha=0.75)
            ax_l.plot(x, b, "--", lw=1.4, color=self.theme["line"])
            if np.any(out_ind):
                ax_l.scatter(np.asarray(x)[out_ind], y[out_ind], facecolors="none", edgecolors=self.theme["outlier"], s=70, linewidths=1.4)
            ax_l.set_ylabel("Temp (°C)", color=self.theme["muted"], fontsize=8)
            ax_l.set_title(f"Pixel r={rr}, c={cc} individual", color=self.theme["title"], fontsize=9)

            ax_r.plot(x, y, "o", ms=4, lw=1, alpha=0.75)
            ax_r.plot(x, final_c, "-", lw=1.4, color=self.theme["line"])
            if np.any(out_avg):
                ax_r.scatter(np.asarray(x)[out_avg], y[out_avg], facecolors="none", edgecolors=self.theme["outlier"], s=70, linewidths=1.4)
            ax_r.set_ylabel("Temp (°C)", color=self.theme["muted"], fontsize=8)
            ax_r.set_title("Against averaged baseline", color=self.theme["title"], fontsize=9)

        if all_y_for_limits:
            low, high = np.nanpercentile(np.asarray(all_y_for_limits, dtype=float), [1, 99])
            pad = max(1.0, 0.08 * (high - low if high > low else 1.0))
            for ax in self.figure.axes:
                ax.set_ylim(low - pad, high + pad)

        if x_dates_ok:
            for ax in self.figure.axes:
                ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=8))
                ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(ax.xaxis.get_major_locator()))
        else:
            for ax in self.figure.axes:
                ax.set_xlabel("Scene order", color=self.theme["muted"], fontsize=8)

        self.figure.tight_layout(pad=1.0)
        self.draw_idle()



class SceneCompareCanvas(FigureCanvas):
    viewChanged = QtCore.pyqtSignal()
    pixelClicked = QtCore.pyqtSignal(float, float, int)

    def __init__(self, parent=None):
        self.theme_mode = "dark"
        self.theme = theme_values(self.theme_mode)
        self.figure = Figure(figsize=(9, 5), dpi=100, facecolor=self.theme["figure"])
        super().__init__(self.figure)
        self.setParent(parent)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setFocus()

        self.original = None
        self.filtered = None
        self.sample_points = []
        self.sample_color = DEFAULT_SAMPLE_POINT_COLOR
        self.sample_point_artists = []
        self.reverse_scroll = False
        self.axes = []
        self.images = []
        self.sample_point_artists = []
        self.colorbar = None
        self._last_shape = None
        self._home_xlim = None
        self._home_ylim = None
        self._pan_start = None
        self._click_start = None
        self._pending_xlim = None
        self._pending_ylim = None
        self._is_dragging = False
        self._click_pixel_tolerance = 5.0

        # Same mouse-pan strategy as the QC viewer: store the initial view and
        # translate it using screen-pixel deltas. A 16 ms timer coalesces high
        # frequency trackpad events so drag stays smooth.
        self._pan_draw_timer = QtCore.QTimer(self)
        self._pan_draw_timer.setSingleShot(True)
        self._pan_draw_timer.setInterval(16)
        self._pan_draw_timer.timeout.connect(self._flush_pan_draw)

        self.mpl_connect("scroll_event", self._on_scroll)
        self.mpl_connect("button_press_event", self._on_button_press)
        self.mpl_connect("button_release_event", self._on_button_release)
        self.mpl_connect("motion_notify_event", self._on_motion)
        self.clear_message("Build a LOESS model, then click Preview Example Scene.")

    def set_reverse_scroll(self, enabled=False):
        self.reverse_scroll = bool(enabled)

    def set_theme(self, mode="dark"):
        self.theme_mode = "light" if str(mode).lower() == "light" else "dark"
        self.theme = theme_values(self.theme_mode)
        try:
            self.figure.set_facecolor(self.theme["figure"])
            for ax in self.figure.axes:
                ax.set_facecolor(self.theme["axes"])
                ax.tick_params(colors=self.theme["muted"], labelsize=8)
                for spine in ax.spines.values():
                    spine.set_color(self.theme["spine"])
            if self.colorbar is not None:
                self.colorbar.ax.tick_params(colors=self.theme["muted"], labelsize=8)
                self.colorbar.set_label("LST (°C)", color=self.theme["muted"], fontsize=8)
        except Exception:
            pass
        self.draw_idle()

    def _style_axis(self, ax):
        ax.set_facecolor(self.theme["axes"])
        ax.set_xticks([])
        ax.set_yticks([])
        ax.set_aspect("equal", adjustable="box", anchor="C")
        ax.set_autoscale_on(False)
        for spine in ax.spines.values():
            spine.set_visible(False)

    def clear_message(self, message):
        self._pan_draw_timer.stop()
        self.figure.clear()
        self.axes = []
        self.images = []
        self.colorbar = None
        self._last_shape = None
        self._home_xlim = None
        self._home_ylim = None
        self._pan_start = None
        self._click_start = None
        self._pending_xlim = None
        self._pending_ylim = None
        ax = self.figure.add_subplot(111)
        ax.set_facecolor(self.theme["axes"])
        ax.text(0.5, 0.5, message, ha="center", va="center", color=self.theme["text"], fontsize=12, wrap=True)
        ax.set_axis_off()
        self.draw_idle()

    def _set_home_view(self, shape):
        height, width = shape
        self._home_xlim = (-0.5, max(0.5, width - 0.5))
        self._home_ylim = (max(0.5, height - 0.5), -0.5)

    def _apply_limits(self, xlim, ylim, draw=True):
        for ax in list(self.axes or []):
            try:
                ax.set_xlim(*xlim)
                ax.set_ylim(*ylim)
                ax.set_aspect("equal", adjustable="box", anchor="C")
                ax.set_autoscale_on(False)
            except Exception:
                pass
        if draw:
            self.draw_idle()
            self.viewChanged.emit()

    def reset_view(self):
        if not self.axes or self._home_xlim is None or self._home_ylim is None:
            return
        self._pan_draw_timer.stop()
        self._pending_xlim = None
        self._pending_ylim = None
        self._pan_start = None
        self._is_dragging = False
        self._apply_limits(self._home_xlim, self._home_ylim, draw=True)

    def _current_limits(self):
        if not self.axes:
            return None
        try:
            return self.axes[0].get_xlim(), self.axes[0].get_ylim()
        except Exception:
            return None

    def plot_scene(self, original, filtered, title, cmap_name="magma", nan_color=DEFAULT_NAN_COLOR, vmin=None, vmax=None, reset_view=False, sample_points=None, sample_color=DEFAULT_SAMPLE_POINT_COLOR):
        self.original = original
        self.figure.set_facecolor(self.theme["figure"])
        self.filtered = filtered
        self.sample_points = list(sample_points or [])
        self.sample_color = str(sample_color or DEFAULT_SAMPLE_POINT_COLOR)
        orig_c = to_celsius_for_display(original)
        filt_c = to_celsius_for_display(filtered, assume_kelvin=looks_kelvin(original))
        finite = np.concatenate([orig_c[np.isfinite(orig_c)], filt_c[np.isfinite(filt_c)]])
        if finite.size == 0:
            vmin, vmax = 0.0, 1.0
        elif vmin is None or vmax is None or not np.isfinite(vmin) or not np.isfinite(vmax) or vmax <= vmin:
            vmin, vmax = np.nanpercentile(finite, [2, 98])
            if vmax <= vmin:
                vmin = float(np.nanmin(finite))
                vmax = float(np.nanmax(finite))
            if vmax <= vmin:
                vmax = vmin + 1.0

        old_limits = self._current_limits()
        shape_changed = self._last_shape != orig_c.shape
        cmap = plt.get_cmap(cmap_name).copy()
        cmap.set_bad(str(nan_color or DEFAULT_NAN_COLOR))
        norm = mcolors.Normalize(vmin=float(vmin), vmax=float(vmax))

        self.figure.clear()
        ax1, ax2 = self.figure.subplots(1, 2)
        self.axes = [ax1, ax2]
        self.images = []
        self.sample_point_artists = []
        for ax, data, name in ((ax1, orig_c, "Original"), (ax2, filt_c, "LOESS filtered")):
            self._style_axis(ax)
            im = ax.imshow(np.ma.masked_invalid(data), cmap=cmap, norm=norm, interpolation="nearest", origin="upper")
            im.set_clip_on(True)
            ax.set_title(name, color=self.theme["text"], fontsize=10)
            self.images.append(im)

        if self.sample_points:
            pts = np.asarray(self.sample_points, dtype=float)
            if pts.ndim == 2 and pts.shape[1] >= 2:
                edge_color = _contrast_edge_color(self.sample_color)
                for ax in self.axes:
                    artist = ax.scatter(
                        pts[:, 0], pts[:, 1],
                        s=34, marker="o",
                        facecolors=self.sample_color,
                        edgecolors=edge_color,
                        linewidths=0.8,
                        alpha=0.95,
                        zorder=20,
                        clip_on=True,
                    )
                    self.sample_point_artists.append(artist)

        self.colorbar = self.figure.colorbar(self.images[-1], ax=self.axes, fraction=0.025, pad=0.02)
        self.colorbar.ax.tick_params(colors=self.theme["muted"], labelsize=8)
        self.colorbar.set_label("LST (°C)", color=self.theme["muted"], fontsize=8)
        self.figure.suptitle(title, color=self.theme["text"], fontsize=11)
        try:
            self.figure.subplots_adjust(left=0.02, right=0.925, bottom=0.035, top=0.90, wspace=0.035)
        except Exception:
            pass

        if reset_view or shape_changed or self._home_xlim is None or self._home_ylim is None:
            self._set_home_view(orig_c.shape)
            self._apply_limits(self._home_xlim, self._home_ylim, draw=False)
        elif old_limits is not None:
            self._apply_limits(old_limits[0], old_limits[1], draw=False)
        self._last_shape = orig_c.shape
        self.draw_idle()

    def _event_axis(self, event):
        if event is None:
            return None
        if event.inaxes in self.axes:
            return event.inaxes
        return None

    def _event_to_data(self, event, ax=None):
        ax = ax or self._event_axis(event)
        if ax is None or event is None or event.x is None or event.y is None:
            return None
        try:
            xdata, ydata = ax.transData.inverted().transform((event.x, event.y))
            if not np.isfinite(xdata) or not np.isfinite(ydata):
                return None
            return float(xdata), float(ydata)
        except Exception:
            return None

    def _axes_pixel_size(self, ax):
        try:
            bbox = ax.bbox
            return float(max(1.0, bbox.width)), float(max(1.0, bbox.height))
        except Exception:
            return float(max(1, self.width())), float(max(1, self.height()))

    def _schedule_pan_draw(self):
        if not self._pan_draw_timer.isActive():
            self._pan_draw_timer.start()

    def _flush_pan_draw(self):
        if self._pending_xlim is None or self._pending_ylim is None:
            return
        xlim = self._pending_xlim
        ylim = self._pending_ylim
        self._pending_xlim = None
        self._pending_ylim = None
        self._apply_limits(xlim, ylim, draw=True)

    def _on_scroll(self, event):
        ax = self._event_axis(event)
        if ax is None or not self.axes:
            return
        point = self._event_to_data(event, ax=ax)
        if point is None:
            return
        xdata, ydata = point
        base_scale = 1.2
        zoom_in = event.button == "up"
        if bool(getattr(self, "reverse_scroll", False)):
            zoom_in = not zoom_in
        scale = 1.0 / base_scale if zoom_in else base_scale
        cur_xlim = ax.get_xlim()
        cur_ylim = ax.get_ylim()
        width = cur_xlim[1] - cur_xlim[0]
        height = cur_ylim[1] - cur_ylim[0]
        if width == 0 or height == 0:
            return
        new_width = width * scale
        new_height = height * scale
        relx = (cur_xlim[1] - xdata) / width
        rely = (cur_ylim[1] - ydata) / height
        new_xlim = (xdata - new_width * (1.0 - relx), xdata + new_width * relx)
        new_ylim = (ydata - new_height * (1.0 - rely), ydata + new_height * rely)
        self._apply_limits(new_xlim, new_ylim, draw=True)

    def _on_button_press(self, event):
        ax = self._event_axis(event)
        if ax is not None and event.button == 3:
            self.reset_view()
            return
        if ax is None or event.button != 1 or event.x is None or event.y is None:
            return
        axes_w, axes_h = self._axes_pixel_size(ax)
        try:
            ax_index = self.axes.index(ax)
        except Exception:
            ax_index = 0
        self._click_start = {
            "px": float(event.x),
            "py": float(event.y),
            "ax_index": int(ax_index),
        }
        self._pan_start = {
            "px": float(event.x),
            "py": float(event.y),
            "xlim": ax.get_xlim(),
            "ylim": ax.get_ylim(),
            "axes_w": axes_w,
            "axes_h": axes_h,
        }
        self._pending_xlim = None
        self._pending_ylim = None
        self._is_dragging = True

    def _on_motion(self, event):
        if self._pan_start is None or event is None or event.x is None or event.y is None:
            return
        x0, x1 = self._pan_start["xlim"]
        y0, y1 = self._pan_start["ylim"]
        axes_w = max(1.0, float(self._pan_start.get("axes_w", 1.0)))
        axes_h = max(1.0, float(self._pan_start.get("axes_h", 1.0)))
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
        click_start = self._click_start
        if self._pan_start is not None and event is not None and event.x is not None and event.y is not None:
            try:
                dx_px = float(event.x) - float(self._pan_start.get("px", event.x))
                dy_px = float(event.y) - float(self._pan_start.get("py", event.y))
                click_candidate = math.hypot(dx_px, dy_px) <= self._click_pixel_tolerance
            except Exception:
                click_candidate = False

        if self._pan_start is not None:
            self._pan_draw_timer.stop()
            if click_candidate:
                self._pending_xlim = None
                self._pending_ylim = None
            else:
                self._flush_pan_draw()

        if click_candidate and click_start is not None and event is not None and event.button == 1 and event.x is not None and event.y is not None:
            ax = self._event_axis(event)
            point = self._event_to_data(event, ax=ax)
            if point is not None:
                try:
                    ax_index = self.axes.index(ax) if ax in self.axes else int(click_start.get("ax_index", 0))
                except Exception:
                    ax_index = int(click_start.get("ax_index", 0))
                self.pixelClicked.emit(float(point[0]), float(point[1]), int(ax_index))

        self._click_start = None
        self._pan_start = None
        self._is_dragging = False


class LoessComputeWorker(QtCore.QObject):
    progress = QtCore.pyqtSignal(int, int, str)
    finished = QtCore.pyqtSignal(object, str)

    def __init__(self, scenes, params, seed):
        super().__init__()
        self.scenes = scenes
        self.params = params
        self.seed = seed

    @QtCore.pyqtSlot()
    def run(self):
        try:
            model = compute_loess_model(self.scenes, self.params, seed=self.seed, progress_callback=self.progress.emit)
            self.finished.emit(model, "")
        except Exception:
            self.finished.emit(None, traceback.format_exc())


class PreviewWorker(QtCore.QObject):
    progress = QtCore.pyqtSignal(int, int, str)
    finished = QtCore.pyqtSignal(object, object, str)

    def __init__(self, model):
        super().__init__()
        self.model = model

    @QtCore.pyqtSlot()
    def run(self):
        try:
            best, scored = choose_representative_scene(self.model, progress_callback=self.progress.emit)
            if best is None:
                raise ValueError("No preview scene could be loaded. Check that the input LST scenes contain valid finite data.")
            original, filtered, outlier, window_box, base = make_scene_preview_arrays(best, self.model)
            payload = {
                "scene": best,
                "scored": scored,
                "original": original,
                "filtered": filtered,
                "outlier": outlier,
                "window_box": window_box,
                "base": base,
            }
            self.finished.emit(payload, scored, "")
        except Exception:
            self.finished.emit(None, None, traceback.format_exc())


class BatchWorker(QtCore.QObject):
    progress = QtCore.pyqtSignal(int, int, str)
    message = QtCore.pyqtSignal(str)
    finished = QtCore.pyqtSignal(int, int, list)

    def __init__(self, model, options):
        super().__init__()
        self.model = model
        self.options = options

    @QtCore.pyqtSlot()
    def run(self):
        written = 0
        failed = 0
        errors = []
        total = len(self.model.scenes)
        for index, scene in enumerate(self.model.scenes, 1):
            name = os.path.basename(scene.path)
            self.progress.emit(index - 1, total, f"Processing {name}")
            try:
                result = apply_loess_to_scene(scene, index - 1, self.model, self.options)
                written += 1
                self.message.emit(
                    f"[OK] {os.path.basename(result['out_path'])} removed {result['removed']:,} outlier pixels "
                    f"({pct_text(result['removed_fraction'])} of valid input pixels)."
                )
            except Exception as exc:
                failed += 1
                detail = f"{name}: {exc}"
                errors.append(detail)
                self.message.emit(f"[ERROR] {detail}")
            self.progress.emit(index, total, name)
        self.finished.emit(written, failed, errors)


class ReprojectTo4326Dialog(QtWidgets.QDialog):
    def __init__(self, counts, convert_items, folder, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Reproject LOESS Outputs To EPSG:4326")
        self.setModal(True)
        self.counts = dict(counts or {})
        self.convert_items = list(convert_items or [])
        self.folder = os.path.abspath(folder or SCRIPT_FOLDER)
        self.results = None
        self.resize(820, 560)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        title = QtWidgets.QLabel("Reproject LOESS outputs to EPSG:4326")
        font = QtGui.QFont()
        font.setPointSize(14)
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)

        intro = QtWidgets.QLabel(
            "Only files inside LOESS_filtered are queued. Append mode keeps the LOESS outputs "
            "and writes converted copies into reprojected_EPSG4326 with the EPSG4326 suffix."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        summary = QtWidgets.QLabel(_format_reproject_summary(self.counts) + f"\n\nFolder:\n{self.folder}")
        summary.setWordWrap(True)
        layout.addWidget(summary)

        mode_group = QtWidgets.QGroupBox("Output mode")
        mode_layout = QtWidgets.QGridLayout(mode_group)
        self.append_radio = QtWidgets.QRadioButton("Append converted files into reprojected_EPSG4326 with suffix (keep original LOESS outputs)")
        self.overwrite_radio = QtWidgets.QRadioButton("Overwrite LOESS outputs in place")
        self.append_radio.setChecked(True)
        mode_layout.addWidget(self.append_radio, 0, 0, 1, 2)
        mode_layout.addWidget(self.overwrite_radio, 1, 0, 1, 2)
        mode_layout.addWidget(QtWidgets.QLabel("Suffix"), 2, 0)
        self.suffix_edit = QtWidgets.QLineEdit(DEFAULT_REPROJECT_SUFFIX)
        mode_layout.addWidget(self.suffix_edit, 2, 1)
        self.append_radio.toggled.connect(lambda checked: self.suffix_edit.setEnabled(bool(checked)))
        layout.addWidget(mode_group)

        self.status_label = QtWidgets.QLabel("Ready to reproject.")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setRange(0, max(1, len(self.convert_items)))
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%v / %m")
        layout.addWidget(self.progress_bar)

        self.details_edit = QtWidgets.QPlainTextEdit()
        self.details_edit.setReadOnly(True)
        self.details_edit.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        pending_lines = []
        for item in self.convert_items:
            rel = os.path.relpath(item.get("path", ""), self.folder)
            pending_lines.append(f"[PENDING] {rel} ({item.get('detail', '')})")
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

    def _append_detail(self, text):
        self.details_edit.appendPlainText(str(text))
        try:
            bar = self.details_edit.verticalScrollBar()
            bar.setValue(bar.maximum())
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
        if mode == "overwrite":
            reply = QtWidgets.QMessageBox.warning(
                self,
                "Overwrite LOESS outputs in EPSG:4326?",
                "This will replace every queued non-EPSG:4326 LOESS output in place. Continue?",
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
        self._append_detail(f"\n[RUN] Mode: {mode}. Suffix: {suffix}.")
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
            QtWidgets.QMessageBox.warning(self, "Reprojection finished with errors", f"{errors} file(s) failed. See details.")
        else:
            QtWidgets.QMessageBox.information(self, "Reprojection complete", f"{converted} file(s) converted to EPSG:4326.")


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1480, 900)
        self.theme_mode = "dark"
        self._graphs_need_update = True
        self._preview_needs_update = False

        self.scenes = []
        self.model = None
        self.last_seed = DEFAULT_SAMPLE_SEED
        self.representative_scene = None
        self.scored_preview_scenes = []
        self.preview_example_index = -1
        self._preview_color_initialized = False
        self.capacity_report = None
        self.scene_capacity_ok = True
        self._last_build_seconds = None
        self._last_build_work_units = None
        self._build_started_at = None
        self._pending_work_units = None
        self._compute_thread = None
        self._compute_worker = None
        self._preview_thread = None
        self._preview_worker = None
        self._batch_thread = None
        self._batch_worker = None
        self._params_dirty = False
        self._last_batch_options = None
        self._loading_color_controls = False
        self._preview_color_initialized = False

        self._build_ui()
        self._apply_theme()
        self.folder_edit.setText(SCRIPT_FOLDER)
        QtCore.QTimer.singleShot(150, self.scan_folder)

    def _build_ui(self):
        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.setCentralWidget(splitter)

        left_scroll = QtWidgets.QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setMinimumWidth(430)
        left_scroll.setMaximumWidth(560)
        left_widget = QtWidgets.QWidget()
        self.controls = QtWidgets.QVBoxLayout(left_widget)
        self.controls.setContentsMargins(14, 14, 14, 14)
        self.controls.setSpacing(12)
        left_scroll.setWidget(left_widget)
        splitter.addWidget(left_scroll)

        right_widget = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right_widget)
        right_layout.setContentsMargins(8, 8, 8, 8)
        self.tabs = QtWidgets.QTabWidget()
        self.graph_canvas = LoessGraphCanvas()
        self.scene_canvas = SceneCompareCanvas()
        self.scene_canvas.pixelClicked.connect(self._show_preview_temperature_at_pixel)

        viewer_top_row = QtWidgets.QHBoxLayout()
        viewer_top_row.setContentsMargins(0, 0, 0, 0)
        viewer_top_row.addStretch(1)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setFixedWidth(280)
        self.progress.setFormat("%v / %m")
        viewer_top_row.addWidget(self.progress)
        self.viewer_reset_btn = QtWidgets.QPushButton("Reset Original View")
        self.viewer_reset_btn.setToolTip("Reset the preview panels to the original full-scene view. You can also right-click over the scene.")
        self.viewer_reset_btn.clicked.connect(self.scene_canvas.reset_view)
        self.viewer_reset_btn.setEnabled(False)
        viewer_top_row.addWidget(self.viewer_reset_btn)
        right_layout.addLayout(viewer_top_row)
        self.reset_preview_view_btn = self.viewer_reset_btn

        self.tabs.addTab(self.graph_canvas, "LOESS baseline graphs")
        self.tabs.addTab(self.scene_canvas, "Scene preview")
        right_layout.addWidget(self.tabs, 1)
        splitter.addWidget(right_widget)
        splitter.setStretchFactor(1, 1)

        self._build_folder_group()
        self._build_appearance_group()
        self._build_parameter_group()
        self._build_baseline_group()
        self._build_preview_group()
        self._build_display_group()
        self._build_output_group()
        self.controls.addStretch(1)
        self.statusBar().showMessage("Ready")

    def _build_folder_group(self):
        group = QtWidgets.QGroupBox("Working folder")
        layout = QtWidgets.QGridLayout(group)
        layout.setColumnStretch(0, 1)
        self.folder_edit = QtWidgets.QLineEdit()
        self.folder_edit.setPlaceholderText("Folder containing LST GeoTIFFs")
        browse_btn = QtWidgets.QPushButton("Browse")
        browse_btn.clicked.connect(self.browse_folder)
        self.scan_btn = QtWidgets.QPushButton("Scan")
        self.scan_btn.clicked.connect(self.scan_folder)
        layout.addWidget(self.folder_edit, 0, 0, 1, 2)
        layout.addWidget(browse_btn, 1, 0)
        layout.addWidget(self.scan_btn, 1, 1)
        self.scan_summary = QtWidgets.QLabel("No scan yet")
        self.scan_summary.setWordWrap(True)
        layout.addWidget(self.scan_summary, 2, 0, 1, 2)
        self.controls.addWidget(group)

    def _build_appearance_group(self):
        group = QtWidgets.QGroupBox("Appearance")
        layout = QtWidgets.QVBoxLayout(group)
        row = QtWidgets.QHBoxLayout()
        self.light_mode_toggle = QtWidgets.QCheckBox("Light mode")
        self.light_mode_toggle.setChecked(False)
        self.light_mode_toggle.setToolTip("Switch between dark and light interface themes.")
        self.light_mode_toggle.stateChanged.connect(self._theme_toggle_changed)
        self.reverse_scroll_toggle = QtWidgets.QCheckBox("Reverse scrolling")
        self.reverse_scroll_toggle.setChecked(False)
        self.reverse_scroll_toggle.setToolTip("Flip the mouse-wheel/trackpad zoom direction in the scene preview.")
        self.reverse_scroll_toggle.stateChanged.connect(self._reverse_scroll_toggle_changed)
        row.addWidget(self.light_mode_toggle)
        row.addWidget(self.reverse_scroll_toggle)
        row.addStretch(1)
        layout.addLayout(row)
        self.controls.addWidget(group)

    def _theme_toggle_changed(self, state):
        self.theme_mode = "light" if state == QtCore.Qt.Checked else "dark"
        self._apply_theme()

    def _reverse_scroll_toggle_changed(self, state):
        if hasattr(self, "scene_canvas"):
            self.scene_canvas.set_reverse_scroll(state == QtCore.Qt.Checked)

    def _build_parameter_group(self):
        group = QtWidgets.QGroupBox("LOESS parameters")
        layout = QtWidgets.QVBoxLayout(group)
        self.n_desired_slider = IntSlider("n_desired selected baseline pixels", 5, 250, 40)
        self.depth_slider = IntSlider("depth_required valid observations", 3, 50, 10)
        self.candidates_slider = IntSlider("n_candidates_initial random candidates", 100, 20000, 2000)
        self.frac_slider = FloatSlider("frac_val LOESS smoothing", 0.02, 0.80, 0.4, decimals=2)
        self.it_slider = IntSlider("it_val robust iterations", 0, 20, 10)
        self.threshold_cold_slider = FloatSlider("threshold_cold below baseline", 0.0, 15.0, 3.0, decimals=2)
        self.threshold_hot_slider = FloatSlider("threshold_hot above baseline", 0.0, 20.0, 10.0, decimals=2)

        parameter_tooltips = {
            self.n_desired_slider: "Number of pixels used to build the LOESS baseline",
            self.depth_slider: "Minimum number of valid observations required per pixel",
            self.candidates_slider: "Number of random pixels to sample initially when looking for good calibration candidates",
            self.frac_slider: "LOESS fraction controls the smoothing window size (higher values = smoother; lower values = noisy)",
            self.it_slider: "LOESS robustness iterations (higher values: more aggressive suppression of outliers; lower values: baseline less robust to noisy points)",
            self.threshold_cold_slider: "Outlier threshold below baseline",
            self.threshold_hot_slider: "Outlier threshold above baseline",
        }
        for widget, tip in parameter_tooltips.items():
            widget.setToolTip(tip)
            for child_name in ("title_label", "value_edit", "slider"):
                child = getattr(widget, child_name, None)
                if child is not None:
                    child.setToolTip(tip)

        for widget in (
            self.n_desired_slider,
            self.depth_slider,
            self.candidates_slider,
            self.frac_slider,
            self.it_slider,
            self.threshold_cold_slider,
            self.threshold_hot_slider,
        ):
            layout.addWidget(widget)
            if isinstance(widget, IntSlider):
                widget.valueChangedInt.connect(self.parameters_changed)
            else:
                widget.valueChanged.connect(self.parameters_changed)

        self.controls.addWidget(group)

    def _build_baseline_group(self):
        group = QtWidgets.QGroupBox("Baseline graph controls")
        layout = QtWidgets.QVBoxLayout(group)
        self.baseline_status = QtWidgets.QLabel("Build a baseline after scanning scenes.")
        self.baseline_status.setWordWrap(True)
        layout.addWidget(self.baseline_status)

        seed_row = QtWidgets.QHBoxLayout()
        seed_row.addWidget(QtWidgets.QLabel("Sample seed"))
        self.seed_spin = QtWidgets.QSpinBox()
        self.seed_spin.setRange(0, 2_147_483_647)
        self.seed_spin.setValue(DEFAULT_SAMPLE_SEED)
        self.seed_spin.setToolTip("Type the same number later to return to the same candidate-pixel sample.")
        self.seed_spin.valueChanged.connect(self.seed_changed)
        self.random_seed_btn = QtWidgets.QPushButton("Random Seed")
        self.random_seed_btn.clicked.connect(self.randomize_seed)
        seed_row.addWidget(self.seed_spin, 1)
        seed_row.addWidget(self.random_seed_btn)
        layout.addLayout(seed_row)

        row = QtWidgets.QHBoxLayout()
        self.rebuild_btn = QtWidgets.QPushButton("Build LOESS Graphs")
        self.rebuild_btn.clicked.connect(self.rebuild_loess_graphs)
        row.addWidget(self.rebuild_btn)
        layout.addLayout(row)
        self.controls.addWidget(group)

    def _build_preview_group(self):
        group = QtWidgets.QGroupBox("Example scene preview")
        layout = QtWidgets.QVBoxLayout(group)
        self.preview_btn = QtWidgets.QPushButton("Preview Example Scene With Current LOESS Filters")
        self.preview_btn.clicked.connect(self.preview_representative_scene)
        layout.addWidget(self.preview_btn)

        nav_row = QtWidgets.QHBoxLayout()
        self.prev_example_btn = QtWidgets.QPushButton("← Previous Example")
        self.prev_example_btn.clicked.connect(self.generate_previous_example_scene)
        self.prev_example_btn.setEnabled(False)
        self.next_example_btn = QtWidgets.QPushButton("Next Example →")
        self.next_example_btn.clicked.connect(self.generate_new_example_scene)
        self.next_example_btn.setEnabled(False)
        nav_row.addWidget(self.prev_example_btn)
        nav_row.addWidget(self.next_example_btn)
        layout.addLayout(nav_row)

        self.save_params_btn = QtWidgets.QPushButton("Save Current Parameters")
        self.save_params_btn.clicked.connect(self.save_current_parameters)
        self.save_params_btn.setEnabled(False)
        layout.addWidget(self.save_params_btn)

        self.preview_summary = QtWidgets.QLabel("No preview yet.")
        self.preview_summary.setWordWrap(True)
        layout.addWidget(self.preview_summary)
        self.controls.addWidget(group)

    def _build_display_group(self):
        group = QtWidgets.QGroupBox("Scene display")
        layout = QtWidgets.QVBoxLayout(group)
        cmap_row = QtWidgets.QHBoxLayout()
        cmap_row.addWidget(QtWidgets.QLabel("Colormap"))
        self.cmap_combo = QtWidgets.QComboBox()
        self.cmap_combo.addItems(COLORMAPS)
        self.cmap_combo.setCurrentText("magma")
        cmap_row.addWidget(self.cmap_combo, 1)
        layout.addLayout(cmap_row)

        nan_row = QtWidgets.QHBoxLayout()
        nan_row.addWidget(QtWidgets.QLabel("NaN color"))
        self.nan_color_combo = QtWidgets.QComboBox()
        for label, color in NAN_COLOR_OPTIONS:
            self.nan_color_combo.addItem(label, color)
        nan_row.addWidget(self.nan_color_combo, 1)
        layout.addLayout(nan_row)

        point_row = QtWidgets.QHBoxLayout()
        point_row.addWidget(QtWidgets.QLabel("Sample pixel color"))
        self.sample_point_color_combo = QtWidgets.QComboBox()
        for label, color in POINT_COLOR_OPTIONS:
            self.sample_point_color_combo.addItem(label, color)
        self.sample_point_color_combo.setCurrentIndex(0)
        point_row.addWidget(self.sample_point_color_combo, 1)
        layout.addLayout(point_row)

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

        self.auto_color_btn = QtWidgets.QPushButton("Auto color")
        self.auto_color_btn.clicked.connect(self.auto_color_from_preview)
        layout.addWidget(self.auto_color_btn)

        self.cmap_combo.currentTextChanged.connect(self.refresh_preview_display)
        self.nan_color_combo.currentIndexChanged.connect(self.refresh_preview_display)
        self.sample_point_color_combo.currentIndexChanged.connect(self.refresh_preview_display)
        self.controls.addWidget(group)

    def _build_output_group(self):
        group = QtWidgets.QGroupBox("Apply to all scenes")
        layout = QtWidgets.QVBoxLayout(group)
        self.append_radio = QtWidgets.QRadioButton("Append copies into LOESS_filtered with suffix (keep source scenes)")
        self.overwrite_radio = QtWidgets.QRadioButton("Overwrite input TIFF files in place using the same filenames")
        self.append_radio.setChecked(True)
        layout.addWidget(self.append_radio)
        layout.addWidget(self.overwrite_radio)

        suffix_row = QtWidgets.QHBoxLayout()
        suffix_row.addWidget(QtWidgets.QLabel("Append suffix"))
        self.suffix_edit = QtWidgets.QLineEdit(DEFAULT_APPEND_SUFFIX)
        suffix_row.addWidget(self.suffix_edit, 1)
        layout.addLayout(suffix_row)
        self.append_radio.toggled.connect(lambda checked: self.suffix_edit.setEnabled(bool(checked)))

        note = QtWidgets.QLabel(
            "Append mode writes new files into LOESS_filtered and keeps the input TIFFs unchanged. "
            "Overwrite mode replaces the input TIFFs in place using the same filenames."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        self.apply_btn = QtWidgets.QPushButton("Apply Current LOESS Filter to All TIFFs")
        self.apply_btn.clicked.connect(self.apply_to_all)
        layout.addWidget(self.apply_btn)

        self.reproject_btn = QtWidgets.QPushButton("Reproject LOESS Outputs to EPSG:4326")
        self.reproject_btn.clicked.connect(self.offer_reproject_to_4326)
        layout.addWidget(self.reproject_btn)

        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setMinimumHeight(180)
        layout.addWidget(self.log)
        self.controls.addWidget(group)

    def _apply_theme(self):
        app = QtWidgets.QApplication.instance()
        t = theme_values(getattr(self, "theme_mode", "dark"))
        palette = QtGui.QPalette()
        palette.setColor(QtGui.QPalette.Window, QtGui.QColor(t["window"]))
        palette.setColor(QtGui.QPalette.WindowText, QtGui.QColor(t["text"]))
        palette.setColor(QtGui.QPalette.Base, QtGui.QColor(t["base"]))
        palette.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor(t["alternate"]))
        palette.setColor(QtGui.QPalette.Text, QtGui.QColor(t["text"]))
        palette.setColor(QtGui.QPalette.Button, QtGui.QColor(t["button"]))
        palette.setColor(QtGui.QPalette.ButtonText, QtGui.QColor(t["text"]))
        palette.setColor(QtGui.QPalette.Highlight, QtGui.QColor(t["highlight"]))
        palette.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor(t["highlight_text"]))
        if app:
            app.setPalette(palette)
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{ font-size: 10pt; color: {t['text']}; background-color: {t['window']}; }}
            QGroupBox {{
                border: 1px solid {t['border']};
                border-radius: 6px;
                margin-top: 10px;
                padding-top: 10px;
                font-weight: 700;
                background-color: {t['window']};
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 4px;
                background-color: {t['window']};
                color: {t['text']};
            }}
            QPushButton {{
                background-color: {t['button']};
                color: {t['text']};
                border: 1px solid {t['border']};
                border-radius: 5px;
                padding: 6px 10px;
            }}
            QPushButton:hover {{ background-color: {t['button_hover']}; }}
            QPushButton:pressed {{ background-color: {t['button_pressed']}; }}
            QPushButton:disabled {{ color: {t['disabled_text']}; background-color: {t['button_disabled']}; }}
            QLineEdit, QComboBox, QPlainTextEdit, QTabWidget::pane {{
                background-color: {t['base']};
                color: {t['text']};
                border: 1px solid {t['border']};
                border-radius: 4px;
                padding: 4px;
            }}
            QComboBox QAbstractItemView {{
                background-color: {t['base']};
                color: {t['text']};
                selection-background-color: {t['highlight']};
                selection-color: {t['highlight_text']};
            }}
            QTabBar::tab {{
                background: {t['button']};
                color: {t['text']};
                border: 1px solid {t['border']};
                padding: 6px 10px;
            }}
            QTabBar::tab:selected {{ background: {t['base']}; }}
            QSlider::groove:horizontal {{
                height: 6px;
                background: {t['border']};
                border-radius: 3px;
            }}
            QSlider::handle:horizontal {{
                background: {t['accent']};
                border: 1px solid {t['highlight']};
                width: 16px;
                margin: -6px 0;
                border-radius: 8px;
            }}
            QProgressBar {{
                border: 1px solid {t['border']};
                border-radius: 4px;
                text-align: center;
                background: {t['base']};
                color: {t['text']};
            }}
            QProgressBar::chunk {{ background-color: {t['highlight']}; }}
        """)
        try:
            self.graph_canvas.set_theme(self.theme_mode)
            self.scene_canvas.set_theme(self.theme_mode)
            if self.model is not None:
                self.graph_canvas.plot_model(self.model)
            if self.representative_scene is not None:
                self.refresh_preview_display()
        except Exception:
            pass
        self._refresh_action_button_highlights()

    def _attention_button_style(self):
        return """
            QPushButton {
                background-color: #2E8B57;
                color: #FFFFFF;
                border: 1px solid #1F6F43;
                border-radius: 5px;
                padding: 6px 10px;
                font-weight: 700;
            }
            QPushButton:hover { background-color: #36A269; }
            QPushButton:pressed { background-color: #267247; }
            QPushButton:disabled {
                background-color: #8EBF9D;
                color: #FFFFFF;
                border: 1px solid #6BA879;
            }
        """

    def _set_button_attention(self, button, enabled):
        if button is None:
            return
        try:
            button.setStyleSheet(self._attention_button_style() if enabled else "")
        except Exception:
            pass

    def _refresh_action_button_highlights(self):
        self._set_button_attention(getattr(self, "rebuild_btn", None), bool(getattr(self, "_graphs_need_update", False)))
        self._set_button_attention(getattr(self, "preview_btn", None), bool(getattr(self, "_preview_needs_update", False)))

    def browse_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Select folder containing LST GeoTIFFs",
            self.folder_edit.text().strip() or SCRIPT_FOLDER,
        )
        if folder:
            self.folder_edit.setText(folder)
            self.scan_folder()

    def log_message(self, message):
        self.log.appendPlainText(str(message))

    def scan_folder(self):
        root = self.folder_edit.text().strip() or SCRIPT_FOLDER
        if not os.path.isdir(root):
            QtWidgets.QMessageBox.warning(self, "Folder not found", root)
            return
        self.model = None
        self.last_seed = self.seed_spin.value() if hasattr(self, "seed_spin") else DEFAULT_SAMPLE_SEED
        self.representative_scene = None
        self.scored_preview_scenes = []
        self.preview_example_index = -1
        self._preview_color_initialized = False
        self.capacity_report = None
        self.scene_capacity_ok = True
        self.graph_canvas.clear_message("Build LOESS graphs after scanning scenes.")
        self.scene_canvas.clear_message("Build a LOESS model, then preview a representative scene.")
        self.preview_summary.setText("No preview yet.")
        self.scan_btn.setEnabled(False)
        self.rebuild_btn.setEnabled(False)
        if hasattr(self, "new_candidates_btn"):
            self.new_candidates_btn.setEnabled(False)
        self.apply_btn.setEnabled(False)
        self.preview_btn.setEnabled(False)
        if hasattr(self, "prev_example_btn"):
            self.prev_example_btn.setEnabled(False)
        if hasattr(self, "next_example_btn"):
            self.next_example_btn.setEnabled(False)
        if hasattr(self, "reset_preview_view_btn"):
            self.reset_preview_view_btn.setEnabled(False)
        if hasattr(self, "save_params_btn"):
            self.save_params_btn.setEnabled(False)
        self.statusBar().showMessage("Scanning folder...")
        self.log_message(f"[SCAN] {root}")
        try:
            self.scenes, note = discover_lST_tifs(root)
            if not self.scenes:
                self.scan_summary.setText("No GeoTIFFs found. The app looks for every .tif/.tiff file outside LOESS output folders.")
                self.statusBar().showMessage("No TIFFs found")
                return

            date_sources = sorted(set(scene.date_source for scene in self.scenes))
            self.depth_slider.set_range(3, max(3, len(self.scenes)))
            self.depth_slider.set_value(min(self.depth_slider.value(), len(self.scenes)), emit=False)
            self.progress.setRange(0, max(1, len(self.scenes)))
            self.progress.setValue(0)

            self.capacity_report = estimate_loess_capacity_report(self.scenes, self.current_params())
            self.scene_capacity_ok = bool(self.capacity_report.get("within_limit", False))
            capacity_line = (
                f"{self.capacity_report['percent_full']:.1f}% full; "
                f"safe capacity = {self.capacity_report['max_scenes']} scenes."
            )
            self.scan_summary.setText(
                f"Found {len(self.scenes)} TIFF scene(s). "
                f"{capacity_line} Date source(s): {', '.join(date_sources)}"
            )

            if self.scene_capacity_ok:
                QtWidgets.QMessageBox.information(
                    self,
                    "LOESS Capacity Check",
                    loess_capacity_message(self.capacity_report),
                )
            else:
                QtWidgets.QMessageBox.critical(
                    self,
                    "LOESS Capacity Limit Exceeded",
                    loess_capacity_message(self.capacity_report),
                )

            self.rebuild_btn.setEnabled(self.scene_capacity_ok)
            if hasattr(self, "new_candidates_btn"):
                self.new_candidates_btn.setEnabled(self.scene_capacity_ok)
            self.apply_btn.setEnabled(False)
            self.preview_btn.setEnabled(False)
            if hasattr(self, "next_example_btn"):
                self.next_example_btn.setEnabled(False)
            if not self.scene_capacity_ok:
                self.baseline_status.setText(
                    f"Scene limit exceeded. Remove at least {self.capacity_report['remove_count']} scene(s), then rescan."
                )
            else:
                self.baseline_status.setText("Scan complete. Build LOESS graphs when ready.")
            self.log_message(f"[SCAN] Found {len(self.scenes)} TIFF scene(s). {note}")
            self.log_message(f"[CAPACITY] {capacity_line}")
            self.statusBar().showMessage("Scan complete" if self.scene_capacity_ok else "Capacity limit exceeded")
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Scan failed", str(exc))
            self.log_message("[ERROR] Scan failed:\n" + traceback.format_exc())
            self.statusBar().showMessage("Scan failed")
        finally:
            self.scan_btn.setEnabled(True)
            self._refresh_action_button_highlights()

    def current_params(self):
        return LoessParams(
            n_desired=int(self.n_desired_slider.value()),
            depth_required=int(self.depth_slider.value()),
            n_candidates_initial=int(self.candidates_slider.value()),
            frac_val=float(self.frac_slider.value()),
            it_val=int(self.it_slider.value()),
            threshold_cold=float(self.threshold_cold_slider.value()),
            threshold_hot=float(self.threshold_hot_slider.value()),
        )

    def parameters_changed(self, *args):
        self._params_dirty = True
        self._graphs_need_update = True
        self._preview_needs_update = False
        self.baseline_status.setText("Parameters changed. Click Build LOESS Graphs to refresh the baseline.")
        self._refresh_action_button_highlights()
        if self.scenes:
            try:
                geometry = self.capacity_report.get("geometry") if isinstance(self.capacity_report, dict) else None
                self.capacity_report = estimate_loess_capacity_report(self.scenes, self.current_params(), geometry=geometry)
                self.scene_capacity_ok = bool(self.capacity_report.get("within_limit", False))
                if not self.scene_capacity_ok:
                    self.baseline_status.setText(
                        f"Current settings exceed the capacity limit. Remove {self.capacity_report['remove_count']} scene(s) or lower capacity-heavy settings."
                    )
            except Exception:
                pass

    def seed_changed(self, value):
        self.last_seed = int(value)
        self._params_dirty = True
        self._graphs_need_update = True
        self._preview_needs_update = False
        self.baseline_status.setText("Sample seed changed. Click Build LOESS Graphs to use that candidate sample.")
        self._refresh_action_button_highlights()

    def randomize_seed(self):
        value = random.randrange(1, 2_147_483_647)
        self.seed_spin.setValue(value)

    def _set_buttons_enabled(self, enabled):
        can_scan = bool(enabled)
        can_build = bool(enabled and self.scenes and self.scene_capacity_ok)
        self.scan_btn.setEnabled(can_scan)
        self.rebuild_btn.setEnabled(can_build)
        if hasattr(self, "new_candidates_btn"):
            self.new_candidates_btn.setEnabled(can_build)
        if hasattr(self, "random_seed_btn"):
            self.random_seed_btn.setEnabled(bool(enabled))
        if hasattr(self, "seed_spin"):
            self.seed_spin.setEnabled(bool(enabled))
        self.preview_btn.setEnabled(bool(enabled and self.model is not None))
        usable = eligible_preview_scenes(self.scored_preview_scenes)
        has_preview = bool(self.representative_scene is not None and self.model is not None)
        if hasattr(self, "prev_example_btn"):
            self.prev_example_btn.setEnabled(bool(enabled and self.model is not None and len(usable) > 1))
        if hasattr(self, "next_example_btn"):
            self.next_example_btn.setEnabled(bool(enabled and self.model is not None and len(usable) > 1))
        if hasattr(self, "reset_preview_view_btn"):
            self.reset_preview_view_btn.setEnabled(bool(enabled and getattr(self.scene_canvas, "original", None) is not None))
        if hasattr(self, "save_params_btn"):
            self.save_params_btn.setEnabled(bool(enabled and has_preview))
        self.apply_btn.setEnabled(bool(enabled and self.model is not None))
        self.reproject_btn.setEnabled(bool(enabled))
        self._refresh_action_button_highlights()

    def _confirm_loess_build(self, seed):
        params = self.current_params()
        geometry = self.capacity_report.get("geometry") if isinstance(self.capacity_report, dict) else None
        self.capacity_report = estimate_loess_capacity_report(self.scenes, params, geometry=geometry)
        self.scene_capacity_ok = bool(self.capacity_report.get("within_limit", False))
        if not self.scene_capacity_ok:
            QtWidgets.QMessageBox.critical(
                self,
                "LOESS Capacity Limit Exceeded",
                loess_capacity_message(self.capacity_report),
            )
            self.baseline_status.setText(
                f"Scene limit exceeded. Remove at least {self.capacity_report['remove_count']} scene(s), then rescan."
            )
            return False

        estimate_seconds, work_units = estimate_loess_runtime_seconds(
            len(self.scenes),
            params,
            geometry=self.capacity_report.get("geometry"),
            last_seconds=self._last_build_seconds,
            last_work_units=self._last_build_work_units,
        )
        message = (
            "Are you sure you want to continue?\n\n"
            f"Scenes: {len(self.scenes)}\n"
            f"Sample seed: {seed}\n"
            f"Estimated graph generation time: {_format_duration(estimate_seconds)}.\n\n"
            "The GUI will stay busy while the LOESS candidate pixels and graphs are rebuilt."
        )
        reply = QtWidgets.QMessageBox.question(
            self,
            "Build LOESS graphs?",
            message,
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if reply != QtWidgets.QMessageBox.Yes:
            return False
        self._pending_work_units = work_units
        return True

    def rebuild_loess_graphs(self):
        if not self.scenes:
            QtWidgets.QMessageBox.information(self, "No scenes", "Scan a folder with LST GeoTIFFs first.")
            return
        seed = int(self.seed_spin.value()) if hasattr(self, "seed_spin") else int(self.last_seed or DEFAULT_SAMPLE_SEED)
        self.last_seed = seed
        if self._confirm_loess_build(seed):
            self._start_compute(seed=seed)

    def generate_new_candidates(self):
        if not self.scenes:
            QtWidgets.QMessageBox.information(self, "No scenes", "Scan a folder with LST GeoTIFFs first.")
            return
        seed = int(self.seed_spin.value()) if hasattr(self, "seed_spin") else int(self.last_seed or DEFAULT_SAMPLE_SEED)
        self.last_seed = seed
        if self._confirm_loess_build(seed):
            self._start_compute(seed=seed)

    def _start_compute(self, seed=None):
        params = self.current_params()
        self._set_buttons_enabled(False)
        self.progress.setRange(0, len(self.scenes))
        self.progress.setValue(0)
        self.baseline_status.setText("Computing LOESS baseline...")
        self.statusBar().showMessage("Computing LOESS baseline...")
        self._build_started_at = time.perf_counter()
        self.representative_scene = None
        self.scored_preview_scenes = []
        self.preview_example_index = -1
        self._preview_needs_update = False
        self.scene_canvas.clear_message("Build a LOESS model, then preview a representative scene.")
        self.preview_summary.setText("No preview yet.")
        if hasattr(self, "prev_example_btn"):
            self.prev_example_btn.setEnabled(False)
        if hasattr(self, "next_example_btn"):
            self.next_example_btn.setEnabled(False)
        if hasattr(self, "reset_preview_view_btn"):
            self.reset_preview_view_btn.setEnabled(False)
        if hasattr(self, "save_params_btn"):
            self.save_params_btn.setEnabled(False)
        self.log_message(
            "[LOESS] Building baseline with "
            f"sample_seed={seed}, "
            f"n_desired={params.n_desired}, depth_required={params.depth_required}, "
            f"n_candidates_initial={params.n_candidates_initial}, frac_val={params.frac_val:.3f}, "
            f"it_val={params.it_val}, threshold_cold={params.threshold_cold:.2f}, threshold_hot={params.threshold_hot:.2f}."
        )
        self._compute_thread = QtCore.QThread(self)
        self._compute_worker = LoessComputeWorker(self.scenes, params, seed)
        self._compute_worker.moveToThread(self._compute_thread)
        self._compute_thread.started.connect(self._compute_worker.run)
        self._compute_worker.progress.connect(self._compute_progress)
        self._compute_worker.finished.connect(self._compute_finished)
        self._compute_worker.finished.connect(self._compute_thread.quit)
        self._compute_worker.finished.connect(self._compute_worker.deleteLater)
        self._compute_thread.finished.connect(self._compute_thread.deleteLater)
        self._compute_thread.start()

    def _compute_progress(self, index, total, message):
        total = max(1, int(total or 0))
        index = max(0, min(int(index or 0), total))
        self.progress.setRange(0, total)
        self.progress.setValue(index)
        # The total shown here is the actual number of input TIFF scenes.
        # Earlier versions added setup/fitting steps to this number, which made
        # the status bar look like extra scenes were being read.
        self.statusBar().showMessage(f"{index}/{total} {message}")

    def _compute_finished(self, model, error_text):
        self._set_buttons_enabled(True)
        if error_text:
            self.model = None
            self.apply_btn.setEnabled(False)
            self.preview_btn.setEnabled(False)
            self.baseline_status.setText("LOESS build failed. See log for details.")
            self.graph_canvas.clear_message("LOESS build failed. Adjust parameters and try again.")
            self.log_message("[ERROR] LOESS build failed:\n" + error_text)
            QtWidgets.QMessageBox.critical(self, "LOESS build failed", error_text.splitlines()[-1] if error_text.splitlines() else error_text)
            self.statusBar().showMessage("LOESS build failed")
            return

        self.model = model
        self._preview_color_initialized = False
        self._params_dirty = False
        self._graphs_need_update = False
        self._preview_needs_update = True
        if self._build_started_at is not None:
            try:
                self._last_build_seconds = max(0.1, time.perf_counter() - self._build_started_at)
                self._last_build_work_units = getattr(self, "_pending_work_units", None)
            except Exception:
                pass
        self.progress.setValue(self.progress.maximum())
        self.graph_canvas.plot_model(model)
        self.tabs.setCurrentWidget(self.graph_canvas)
        self.preview_btn.setEnabled(True)
        self.apply_btn.setEnabled(True)
        if hasattr(self, "prev_example_btn"):
            self.prev_example_btn.setEnabled(False)
        if hasattr(self, "next_example_btn"):
            self.next_example_btn.setEnabled(False)
        if hasattr(self, "reset_preview_view_btn"):
            self.reset_preview_view_btn.setEnabled(False)
        if hasattr(self, "save_params_btn"):
            self.save_params_btn.setEnabled(False)
        inter = model.intersection
        warn = "\n" + "\n".join(model.warnings) if model.warnings else ""
        self.baseline_status.setText(
            f"LOESS ready. Selected {len(model.selected_indices)} baseline pixels from "
            f"{len(model.valid_indices)} valid candidates. Standardized grid: "
            f"{inter.width} x {inter.height} px. Candidate seed: {model.seed_label}.{warn}"
        )
        self.log_message(
            f"[LOESS] Ready. Selected {len(model.selected_indices)} baseline pixels from "
            f"{len(model.valid_indices)} valid candidates. Standardized grid {inter.width} x {inter.height}."
        )
        self.statusBar().showMessage("LOESS baseline ready")
        self._refresh_action_button_highlights()

    def preview_representative_scene(self):
        if self.model is None:
            QtWidgets.QMessageBox.information(self, "No LOESS model", "Build the LOESS graphs first.")
            return
        self._set_buttons_enabled(False)
        self.progress.setRange(0, len(self.model.scenes))
        self.progress.setValue(0)
        self.preview_summary.setText(
            "Selecting an example scene with the most visible data so users can inspect the LOESS filtering..."
        )
        self.statusBar().showMessage("Selecting example scene...")
        self._preview_thread = QtCore.QThread(self)
        self._preview_worker = PreviewWorker(self.model)
        self._preview_worker.moveToThread(self._preview_thread)
        self._preview_thread.started.connect(self._preview_worker.run)
        self._preview_worker.progress.connect(self._preview_progress)
        self._preview_worker.finished.connect(self._preview_finished)
        self._preview_worker.finished.connect(self._preview_thread.quit)
        self._preview_worker.finished.connect(self._preview_worker.deleteLater)
        self._preview_thread.finished.connect(self._preview_thread.deleteLater)
        self._preview_thread.start()

    def _preview_progress(self, index, total, message):
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(max(0, min(index, total)))
        self.statusBar().showMessage(f"{index}/{total} {message}")

    def _display_preview_scene(self, scene, example_index=None, reset_view=True, auto_color=None):
        if scene is None or self.model is None:
            return
        original, filtered, outlier, window_box, base = make_scene_preview_arrays(scene, self.model)
        self.representative_scene = scene
        removed_preview = int(np.count_nonzero(np.isfinite(original) & ~np.isfinite(filtered)))
        valid_preview = int(np.count_nonzero(np.isfinite(original)))
        removed_fraction = removed_preview / float(valid_preview) if valid_preview else 0.0
        title = os.path.basename(scene.path)

        if auto_color is None:
            auto_color = not bool(getattr(self, "_preview_color_initialized", False))
        if auto_color:
            self._auto_color_from_arrays(original, filtered, emit=False)
            self._preview_color_initialized = True

        sample_points = selected_sample_preview_points(scene, self.model, original.shape)
        sample_color = self.sample_point_color_combo.currentData() if hasattr(self, "sample_point_color_combo") else DEFAULT_SAMPLE_POINT_COLOR
        self.scene_canvas.plot_scene(
            original,
            filtered,
            title,
            cmap_name=self.cmap_combo.currentText() or "magma",
            nan_color=self.nan_color_combo.currentData() or DEFAULT_NAN_COLOR,
            vmin=self.color_min_slider.value(),
            vmax=self.color_max_slider.value(),
            reset_view=bool(reset_view),
            sample_points=sample_points,
            sample_color=sample_color or DEFAULT_SAMPLE_POINT_COLOR,
        )
        self.tabs.setCurrentWidget(self.scene_canvas)
        usable = eligible_preview_scenes(self.scored_preview_scenes)
        if example_index is None:
            example_index = self.preview_example_index
        position_text = ""
        if usable and example_index is not None and example_index >= 0:
            position_text = f" Example {example_index + 1} of {len(usable)}."
        self.preview_summary.setText(
            f"Example scene: {file_display_name(scene, self.folder_edit.text().strip())}\n"
            f"Score {scene.score:.3f}; preview removed {removed_preview:,} pixels "
            f"({pct_text(removed_fraction)} of valid preview pixels). "
            f"Data available: {pct_text(scene.finite_fraction)}; NaN/missing: {pct_text(scene.missing_fraction)}; "
            f"outlier fraction in standardized grid: {pct_text(scene.outlier_fraction)}.{position_text}"
        )
        if hasattr(self, "prev_example_btn"):
            self.prev_example_btn.setEnabled(len(usable) > 1)
        if hasattr(self, "next_example_btn"):
            self.next_example_btn.setEnabled(len(usable) > 1)
        if hasattr(self, "reset_preview_view_btn"):
            self.reset_preview_view_btn.setEnabled(True)
        if hasattr(self, "save_params_btn"):
            self.save_params_btn.setEnabled(True)
        self.statusBar().showMessage("Example preview ready")

    def _preview_finished(self, payload, scored, error_text):
        self._set_buttons_enabled(True)
        if error_text:
            self.preview_summary.setText("Preview failed. See log for details.")
            self.scene_canvas.clear_message("Preview failed.")
            self.log_message("[ERROR] Preview failed:\n" + error_text)
            QtWidgets.QMessageBox.critical(self, "Preview failed", error_text.splitlines()[-1] if error_text.splitlines() else error_text)
            self.statusBar().showMessage("Preview failed")
            return
        self.scored_preview_scenes = scored or []
        usable = eligible_preview_scenes(self.scored_preview_scenes)
        self.preview_example_index = 0 if usable else -1
        if not usable:
            self.representative_scene = None
            self.scene_canvas.clear_message("No preview scene could be loaded.")
            self.preview_summary.setText("No preview scene could be loaded. Check that the input LST scenes contain valid finite data.")
            if hasattr(self, "prev_example_btn"):
                self.prev_example_btn.setEnabled(False)
            if hasattr(self, "next_example_btn"):
                self.next_example_btn.setEnabled(False)
            if hasattr(self, "reset_preview_view_btn"):
                self.reset_preview_view_btn.setEnabled(False)
            if hasattr(self, "save_params_btn"):
                self.save_params_btn.setEnabled(False)
            return
        self._preview_needs_update = False
        self._display_preview_scene(usable[0], self.preview_example_index, reset_view=True, auto_color=True)
        self._refresh_action_button_highlights()

    def generate_previous_example_scene(self):
        if self.model is None:
            QtWidgets.QMessageBox.information(self, "No LOESS model", "Build the LOESS graphs first.")
            return
        usable = eligible_preview_scenes(self.scored_preview_scenes)
        if not usable:
            QtWidgets.QMessageBox.information(
                self,
                "No examples loaded",
                "Click Preview Example Scene first. The GUI will cycle through scenes ranked by available data and visible LOESS filtering.",
            )
            return
        self.preview_example_index = (self.preview_example_index - 1) % len(usable)
        try:
            QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
            self._display_preview_scene(usable[self.preview_example_index], self.preview_example_index, reset_view=True, auto_color=True)
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

    def generate_new_example_scene(self):
        if self.model is None:
            QtWidgets.QMessageBox.information(self, "No LOESS model", "Build the LOESS graphs first.")
            return
        usable = eligible_preview_scenes(self.scored_preview_scenes)
        if not usable:
            QtWidgets.QMessageBox.information(
                self,
                "No examples loaded",
                "Click Preview Example Scene first. The GUI will cycle through scenes ranked by available data and visible LOESS filtering.",
            )
            return
        self.preview_example_index = (self.preview_example_index + 1) % len(usable)
        try:
            QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
            self._display_preview_scene(usable[self.preview_example_index], self.preview_example_index, reset_view=True, auto_color=True)
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()


    def _preview_color_values(self, original=None, filtered=None):
        if original is None:
            original = getattr(self.scene_canvas, "original", None)
        if filtered is None:
            filtered = getattr(self.scene_canvas, "filtered", None)
        arrays = []
        if original is not None:
            arrays.append(to_celsius_for_display(original))
        if filtered is not None:
            arrays.append(to_celsius_for_display(filtered, assume_kelvin=looks_kelvin(original) if original is not None else None))
        finite_parts = [arr[np.isfinite(arr)] for arr in arrays if arr is not None and np.any(np.isfinite(arr))]
        if not finite_parts:
            return np.array([], dtype="float32")
        return np.concatenate(finite_parts).astype("float32", copy=False)

    def _auto_color_from_arrays(self, original=None, filtered=None, emit=True):
        finite = self._preview_color_values(original, filtered)
        if finite.size == 0:
            low, high = DEFAULT_COLOR_MIN_C, DEFAULT_COLOR_MAX_C
            range_low, range_high = DEFAULT_COLOR_MIN_C, DEFAULT_COLOR_MAX_C
        else:
            low, high = np.nanpercentile(finite, [2.0, 98.0])
            if high <= low:
                low = float(np.nanmin(finite))
                high = float(np.nanmax(finite))
            if high <= low:
                high = low + 1.0
            range_low, range_high = np.nanpercentile(finite, [0.1, 99.9])
            if range_high <= range_low:
                range_low, range_high = float(np.nanmin(finite)), float(np.nanmax(finite))
            if range_high <= range_low:
                range_high = range_low + 1.0
            range_low = min(float(range_low), DEFAULT_COLOR_MIN_C, float(low))
            range_high = max(float(range_high), DEFAULT_COLOR_MAX_C, float(high))

        self._loading_color_controls = True
        try:
            self.color_min_slider.set_range(range_low, range_high)
            self.color_max_slider.set_range(range_low, range_high)
            self.color_min_slider.set_value(low, emit=False)
            self.color_max_slider.set_value(high, emit=False)
        finally:
            self._loading_color_controls = False
        if emit:
            self.refresh_preview_display()

    def auto_color_from_preview(self, checked=False):
        self._auto_color_from_arrays(emit=True)

    def _color_min_changed(self, value):
        if self._loading_color_controls:
            return
        if value >= self.color_max_slider.value():
            self.color_max_slider.set_value(value + 0.01, emit=False)
        self.refresh_preview_display()

    def _color_max_changed(self, value):
        if self._loading_color_controls:
            return
        if value <= self.color_min_slider.value():
            self.color_min_slider.set_value(value - 0.01, emit=False)
        self.refresh_preview_display()

    def refresh_preview_display(self):
        if self.representative_scene is None or self.model is None:
            return
        try:
            self._display_preview_scene(self.representative_scene, self.preview_example_index, reset_view=False, auto_color=False)
        except Exception:
            pass


    def _show_preview_temperature_at_pixel(self, xdata, ydata, panel_index):
        arrays = [getattr(self.scene_canvas, "original", None), getattr(self.scene_canvas, "filtered", None)]
        panel_index = int(max(0, min(1, panel_index)))
        arr = arrays[panel_index]
        if arr is None:
            return
        row = int(round(float(ydata)))
        col = int(round(float(xdata)))
        if row < 0 or col < 0 or row >= arr.shape[0] or col >= arr.shape[1]:
            return

        value = arr[row, col]
        if np.isfinite(value):
            temp_c = float(to_celsius_for_display(np.array([value], dtype="float32"))[0])
            temp_text = f"{temp_c:.2f} °C"
        else:
            temp_text = "NaN / masked"

        popup_text = f"Temperature: {temp_text}"
        try:
            QtWidgets.QToolTip.showText(QtGui.QCursor.pos(), popup_text, self.scene_canvas)
        except Exception:
            QtWidgets.QMessageBox.information(self, "Pixel info", popup_text)


    def current_batch_options(self):
        mode = "overwrite" if self.overwrite_radio.isChecked() else "append"
        return BatchOptions(
            mode=mode,
            suffix=self.suffix_edit.text().strip() or DEFAULT_APPEND_SUFFIX,
            root_folder=self.folder_edit.text().strip() or SCRIPT_FOLDER,
            output_folder=DEFAULT_OUTPUT_FOLDER,
        )

    def save_current_parameters(self):
        if self.model is None:
            QtWidgets.QMessageBox.information(self, "No LOESS model", "Build the LOESS graphs first.")
            return
        if self.representative_scene is None:
            QtWidgets.QMessageBox.information(self, "No preview scene", "Preview an example scene first.")
            return

        root = self.folder_edit.text().strip() or SCRIPT_FOLDER
        out_dir = _versioned_output_folder(os.path.abspath(root), DEFAULT_TUNING_OUTPUT_FOLDER)
        scene_stem = os.path.splitext(os.path.basename(self.representative_scene.path))[0]
        safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", scene_stem).strip("_") or "scene"

        try:
            QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
            os.makedirs(out_dir, exist_ok=False)

            png_path = os.path.join(out_dir, f"{safe_stem}_baseline_graphs.png")
            eps_path = os.path.join(out_dir, f"{safe_stem}_baseline_graphs.eps")
            self.graph_canvas.figure.savefig(png_path, dpi=200, bbox_inches="tight", facecolor=self.graph_canvas.figure.get_facecolor())
            self.graph_canvas.figure.savefig(eps_path, format="eps", bbox_inches="tight", facecolor=self.graph_canvas.figure.get_facecolor())

            original, filtered, profile, base_val, removed_full = make_scene_full_arrays_for_save(self.representative_scene, self.model)
            original_path = os.path.join(out_dir, f"{safe_stem}_original.tif")
            filtered_path = os.path.join(out_dir, f"{safe_stem}_LOESSfiltered.tif")
            _write_array_to_tif_path(original_path, original, profile)
            _write_array_to_tif_path(filtered_path, filtered, profile)

            sample_pixels_path = os.path.join(out_dir, "sample_pixels.shp")
            write_sample_pixels_shapefile(sample_pixels_path, self.representative_scene, self.model)

            params = self.model.params
            txt_path = os.path.join(out_dir, f"{safe_stem}_parameters_utf-8.txt")
            base_c = to_celsius_for_display(np.array([base_val], dtype="float32"))[0]
            lines = [
                "ECOSTRESS LOESS tuning parameters",
                "==================================",
                f"Created: {datetime.datetime.now().isoformat(timespec='seconds')}",
                f"Working folder: {os.path.abspath(root)}",
                f"Tuning folder: {out_dir}",
                f"Preview scene: {self.representative_scene.path}",
                f"Sample seed: {self.model.seed_label}",
                "",
                "LOESS parameters",
                f"n_desired: {params.n_desired}",
                f"depth_required: {params.depth_required}",
                f"n_candidates_initial: {params.n_candidates_initial}",
                f"frac_val: {params.frac_val}",
                f"it_val: {params.it_val}",
                f"threshold_cold: {params.threshold_cold}",
                f"threshold_hot: {params.threshold_hot}",
                "",
                "Model info",
                f"scene_count: {len(self.model.scenes)}",
                f"selected_baseline_pixels: {len(self.model.selected_indices)}",
                "baseline_pixel_selection: valid_observation_and_variance_stratified",
                f"valid_candidate_pixels: {len(self.model.valid_indices)}",
                f"standardized_grid_width_px: {self.model.intersection.width}",
                f"standardized_grid_height_px: {self.model.intersection.height}",
                f"preview_baseline_value_raw: {base_val}",
                f"preview_baseline_value_celsius: {base_c}",
                f"full_resolution_removed_pixels_in_preview_scene: {removed_full}",
                "",
                "Display settings",
                f"theme_mode: {self.theme_mode}",
                f"colormap: {self.cmap_combo.currentText()}",
                f"nan_color: {self.nan_color_combo.currentText()} ({self.nan_color_combo.currentData()})",
                f"color_minimum_celsius: {self.color_min_slider.value()}",
                f"color_maximum_celsius: {self.color_max_slider.value()}",
                f"sample_pixel_color: {self.sample_point_color_combo.currentText()} ({self.sample_point_color_combo.currentData()})",
                "",
                "Sample pixels",
                f"sample_pixel_count: {len(selected_sample_pixels(self.model))}",
                f"sample_pixels_shapefile: {sample_pixels_path}",
                "",
                "Output files",
                f"baseline_graph_png: {png_path}",
                f"baseline_graph_eps: {eps_path}",
                f"original_scene_tif: {original_path}",
                f"loess_filtered_scene_tif: {filtered_path}",
                f"sample_pixels_shapefile: {sample_pixels_path}",
                f"parameters_utf8_text: {txt_path}",
            ]
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except Exception as exc:
            self.log_message("[ERROR] Save current parameters failed:\n" + traceback.format_exc())
            QtWidgets.QMessageBox.critical(self, "Save failed", str(exc))
            return
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

        self.log_message(f"[SAVE] LOESS tuning parameters saved to {out_dir}")
        QtWidgets.QMessageBox.information(
            self,
            "Parameters saved",
            "Saved LOESS tuning snapshot to:\n" + out_dir,
        )

    def apply_to_all(self):
        if self.model is None:
            QtWidgets.QMessageBox.information(self, "No LOESS model", "Build the LOESS graphs first.")
            return
        if self._params_dirty:
            reply = QtWidgets.QMessageBox.question(
                self,
                "Parameters changed",
                "The sliders changed after the last LOESS build. Apply the last built baseline anyway?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if reply != QtWidgets.QMessageBox.Yes:
                return
        options = self.current_batch_options()
        self._last_batch_options = options
        if options.mode == "overwrite":
            reply = QtWidgets.QMessageBox.warning(
                self,
                "Overwrite input TIFF files?",
                "This will replace the original input TIFF files in place using the same filenames. Continue?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if reply != QtWidgets.QMessageBox.Yes:
                return

        self._set_buttons_enabled(False)
        self.progress.setRange(0, len(self.model.scenes))
        self.progress.setValue(0)
        output_note = (
            f"output folder={DEFAULT_OUTPUT_FOLDER}; suffix={options.suffix}"
            if options.mode == "append"
            else "overwriting input TIFFs in place"
        )
        self.log_message(
            f"[RUN] Applying LOESS filter to {len(self.model.scenes)} scene(s). "
            f"Mode={options.mode}; {output_note}."
        )
        self._batch_thread = QtCore.QThread(self)
        self._batch_worker = BatchWorker(self.model, options)
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
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(max(0, min(index, total)))
        self.statusBar().showMessage(f"{index}/{total} {message}")

    def _batch_finished(self, written, failed, errors):
        self._set_buttons_enabled(True)
        self.progress.setValue(self.progress.maximum())
        options = getattr(self, "_last_batch_options", None)
        mode = getattr(options, "mode", "append")
        self.log_message(f"[DONE] {written} LOESS-filtered scene(s) written, {failed} failed.")
        self.statusBar().showMessage(f"Finished: {written} written, {failed} failed")
        if failed:
            QtWidgets.QMessageBox.warning(self, "Batch finished with errors", f"{written} files written, {failed} failed. See the log.")
        else:
            if mode == "overwrite":
                QtWidgets.QMessageBox.information(self, "Batch complete", f"{written} input TIFF file(s) were overwritten in place.")
            else:
                QtWidgets.QMessageBox.information(self, "Batch complete", f"{written} LOESS-filtered files written into {DEFAULT_OUTPUT_FOLDER}.")
        if mode == "append":
            QtCore.QTimer.singleShot(0, self.offer_reproject_to_4326)

    def offer_reproject_to_4326(self):
        folder = self.folder_edit.text().strip() or SCRIPT_FOLDER
        if not os.path.isdir(folder):
            self.log_message(f"[REPROJECT] Skipped: folder not found: {folder}")
            return
        self.log_message("[REPROJECT] Scanning LOESS_filtered for non-EPSG:4326 TIFF files...")
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            targets = discover_reproject_targets_in_loess_outputs(folder)
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


def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("ECOSTRESS")
    window = MainWindow()
    window.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
