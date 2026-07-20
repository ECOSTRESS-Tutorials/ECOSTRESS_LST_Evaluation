#!/usr/bin/env python3
"""
NASA JPL Thermal Viewer — Semi‑Automated Georeferencer v1.15
  - We do not specifically use VRAM, but > 8GB is better so-as to keep CPU available.

Run examples:
  python GeoViewer.py  
  python GeoViewer_PyQt5.py --gdal-cache-mb 4096 --threads all

Notes:
- Backend is a Qt backend (Qt5Agg preferred, QtAgg fallback). No fallback to Agg.
"""

import os, sys, glob, csv, time, textwrap, argparse, math, random, json, re, shutil, tempfile, stat, hashlib, gc
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

# Let Qt work in logical pixels so Windows/monitor DPI scaling stays consistent.
os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")
os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

from PyQt5 import QtCore, QtGui, QtWidgets

try:
    QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)
except Exception:
    pass

MIN_AUTO_UI_SCALE = 0.85
MAX_AUTO_UI_SCALE = 1.15
MIN_MANUAL_UI_SCALE = 0.30
MAX_MANUAL_UI_SCALE = 3.00
UI_SCALE_CHOICES = (
    ("Auto (Qt/OS)", "auto"),
    ("30%", 0.30),
    ("45%", 0.45),
    ("60%", 0.60),
    ("75%", 0.75),
    ("90%", 0.90),
    ("100%", 1.00),
    ("110%", 1.10),
    ("125%", 1.25),
    ("140%", 1.40),
    ("155%", 1.55),
    ("170%", 1.70),
    ("185%", 1.85),
    ("200%", 2.00),
    ("250%", 2.50),
    ("300%", 3.00),
)
GEOVIEWER_UI_SCALE_ENV_OVERRIDE = bool(str(os.environ.get("GEOVIEWER_UI_SCALE", "") or "").strip())
_UI_SCALE_REAPPLY_RATIO = 1.0
_UI_SCALE_REAPPLYING = False
_UI_SCALE_OVERRIDE = None
_UI_SCALE_HOOKS_INSTALLED = False
_UI_SCALED_FONT_KEYS = set()
_UI_SCALE_FORCE_100_PROPERTY = "geoviewer_force_ui_scale_100"
GEOVIEWER_CENTER_ON_SHOW_PROPERTY = "_geoviewer_center_on_show"
GEOVIEWER_NO_SCREEN_SCROLL_PROPERTY = "_geoviewer_no_screen_scroll"

def clamp_ui_scale(value, fallback=1.0):
    try:
        numeric = float(value)
    except Exception:
        numeric = float(fallback)
    if not math.isfinite(numeric):
        numeric = float(fallback)
    return max(MIN_MANUAL_UI_SCALE, min(MAX_MANUAL_UI_SCALE, numeric))

def _active_screen_for_ui_scale():
    app = QtWidgets.QApplication.instance()
    screen = None
    if app is not None:
        try:
            screen = app.screenAt(QtGui.QCursor.pos())
        except Exception:
            screen = None
        if screen is None:
            try:
                screen = app.primaryScreen()
            except Exception:
                screen = None
    if screen is None:
        try:
            screen = QtGui.QGuiApplication.primaryScreen()
        except Exception:
            screen = None
    return screen

def _available_geometry_for_widget_or_cursor(widget=None):
    screen = None
    if isinstance(widget, QtWidgets.QWidget):
        try:
            screen = widget.screen()
        except Exception:
            screen = None
        if screen is None:
            try:
                handle = widget.windowHandle()
                if handle is not None:
                    screen = handle.screen()
            except Exception:
                screen = None
        if screen is None:
            try:
                screen = QtGui.QGuiApplication.screenAt(widget.frameGeometry().center())
            except Exception:
                screen = None
    if screen is None:
        try:
            screen = QtGui.QGuiApplication.screenAt(QtGui.QCursor.pos())
        except Exception:
            screen = None
    if screen is None:
        try:
            screen = QtGui.QGuiApplication.primaryScreen()
        except Exception:
            screen = None
    if screen is not None:
        try:
            return screen.availableGeometry()
        except Exception:
            pass
    return QtCore.QRect(0, 0, 1400, 850)

def initial_main_window_geometry(widget=None, fraction=0.75):
    available = _available_geometry_for_widget_or_cursor(widget)
    try:
        fraction = max(0.10, min(1.0, float(fraction)))
    except Exception:
        fraction = 0.75
    width = max(1, int(round(float(available.width()) * fraction)))
    height = max(1, int(round(float(available.height()) * fraction)))
    width = min(width, int(available.width()))
    height = min(height, int(available.height()))
    x = int(available.left() + max(0, (available.width() - width) // 2))
    y = int(available.top() + max(0, (available.height() - height) // 2))
    return QtCore.QRect(x, y, width, height)

def center_top_level_widget_on_available_screen(widget, available=None):
    if not isinstance(widget, QtWidgets.QWidget):
        return
    if available is None:
        available = _available_geometry_for_widget_or_cursor(widget)
    try:
        frame = widget.frameGeometry()
        width = max(1, int(frame.width()))
        height = max(1, int(frame.height()))
        min_x = int(available.left())
        min_y = int(available.top())
        max_x = int(available.left() + max(0, available.width() - width))
        max_y = int(available.top() + max(0, available.height() - height))
        x = int(available.left() + max(0, (available.width() - width) // 2))
        y = int(available.top() + max(0, (available.height() - height) // 2))
        widget.move(min(max(x, min_x), max_x), min(max(y, min_y), max_y))
    except Exception:
        pass

def _compute_auto_ui_scale():
    raw_override = str(os.environ.get("GEOVIEWER_UI_SCALE", "") or "").strip()
    if raw_override:
        try:
            return clamp_ui_scale(float(raw_override), fallback=1.0)
        except Exception:
            pass

    screen = _active_screen_for_ui_scale()
    if screen is None:
        return 1.0

    try:
        dpi_scale = float(screen.logicalDotsPerInch()) / 96.0
    except Exception:
        dpi_scale = 1.0
    try:
        dpr = float(screen.devicePixelRatio())
    except Exception:
        dpr = 1.0

    if not math.isfinite(dpi_scale) or dpi_scale <= 0:
        dpi_scale = 1.0
    if not math.isfinite(dpr) or dpr <= 0:
        dpr = 1.0

    # Qt high-DPI mode already converts physical pixels into logical pixels.
    # Use DPI/DPR only as a small correction, not a resolution-based shrink.
    if dpr > 1.01 and dpi_scale > 1.01:
        candidate = dpi_scale / dpr
    elif dpr > 1.01:
        candidate = 1.0
    else:
        candidate = dpi_scale
    return max(MIN_AUTO_UI_SCALE, min(MAX_AUTO_UI_SCALE, candidate))

GEOVIEWER_UI_SCALE = _compute_auto_ui_scale()
GEOVIEWER_UI_SCALE_MANUAL = GEOVIEWER_UI_SCALE_ENV_OVERRIDE

def ui_scale():
    if _UI_SCALE_OVERRIDE is not None:
        try:
            return float(_UI_SCALE_OVERRIDE)
        except Exception:
            pass
    try:
        return float(GEOVIEWER_UI_SCALE)
    except Exception:
        return 1.0

def ui_scale_is_manual():
    return bool(GEOVIEWER_UI_SCALE_MANUAL)

def normalize_persisted_ui_scale(value, fallback="auto"):
    if value is None:
        return fallback
    text = str(value).strip().lower()
    if not text or text in ("auto", "default", "screen", "qt"):
        return "auto"
    try:
        return clamp_ui_scale(float(text), fallback=1.0)
    except Exception:
        return fallback

def ui_px(value, minimum=1):
    try:
        numeric = float(value)
    except Exception:
        return value
    if numeric == 0:
        return 0
    scaled = int(round(numeric * ui_scale()))
    if numeric > 0 and minimum is not None:
        scaled = max(int(minimum), scaled)
    return scaled

def ui_geometry_px(value, minimum=1):
    try:
        numeric = float(value)
    except Exception:
        return value
    if numeric == 0:
        return 0
    scale = min(ui_scale(), 1.0)
    scaled = int(round(numeric * scale))
    if numeric > 0 and minimum is not None:
        scaled = max(int(minimum), scaled)
    return scaled

def ui_pt(value, minimum=6.0):
    try:
        numeric = float(value)
    except Exception:
        return value
    if numeric <= 0:
        return numeric
    return max(float(minimum), numeric * ui_scale())

def _truthy_qt_property(obj, name):
    try:
        value = obj.property(name)
    except Exception:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off", "none")
    return bool(value)

def _object_or_parent_forces_ui_scale_100(obj):
    cur = obj
    seen = set()
    while isinstance(cur, QtCore.QObject) and id(cur) not in seen:
        seen.add(id(cur))
        if _truthy_qt_property(cur, _UI_SCALE_FORCE_100_PROPERTY):
            return True
        if isinstance(cur, QtWidgets.QLayout):
            try:
                parent_widget = cur.parentWidget()
            except Exception:
                parent_widget = None
            if parent_widget is not None and id(parent_widget) not in seen:
                cur = parent_widget
                continue
        try:
            parent = cur.parent()
        except Exception:
            parent = None
        cur = parent if isinstance(parent, QtCore.QObject) else None
    return False

def _with_ui_scale_override(scale, callback):
    global _UI_SCALE_OVERRIDE
    previous = _UI_SCALE_OVERRIDE
    _UI_SCALE_OVERRIDE = scale
    try:
        return callback()
    finally:
        _UI_SCALE_OVERRIDE = previous

def _with_object_ui_scale(obj, callback):
    if _object_or_parent_forces_ui_scale_100(obj):
        return _with_ui_scale_override(1.0, callback)
    return callback()

def force_widget_ui_scale_100(widget):
    if widget is not None:
        try:
            widget.setProperty(_UI_SCALE_FORCE_100_PROPERTY, True)
        except Exception:
            pass
    return widget

def set_widget_stylesheet_unscaled(widget, style_text):
    if widget is None:
        return
    force_widget_ui_scale_100(widget)
    return _with_ui_scale_override(1.0, lambda: widget.setStyleSheet(style_text))

def _preserve_qmainwindow_size_during_ui_scale(widget):
    return isinstance(widget, QtWidgets.QMainWindow)

def _is_private_qmainwindow_layout(layout):
    try:
        parent_widget = layout.parentWidget()
    except Exception:
        parent_widget = None
    return isinstance(parent_widget, QtWidgets.QMainWindow)

_MENU_QSS_SPACING_PROPERTIES = {
    "padding", "padding-left", "padding-top", "padding-right", "padding-bottom",
    "margin", "margin-left", "margin-top", "margin-right", "margin-bottom",
    "spacing", "min-height",
}

def _scale_qt_stylesheet(style_text):
    if not style_text or abs(ui_scale() - 1.0) < 1e-6:
        return style_text

    def scale_numbers(text):
        return re.sub(r"(?<![#A-Za-z0-9_.-])(\d+(?:\.\d+)?)(px|pt)\b", repl, str(text))

    def repl(match):
        number = float(match.group(1))
        unit = match.group(2)
        if number == 0:
            return f"0{unit}"
        scaled = number * ui_scale()
        if unit == "px":
            return f"{max(1, int(round(scaled)))}px"
        value = max(6.0, scaled)
        return f"{value:.3f}".rstrip("0").rstrip(".") + "pt"

    def scale_body(selector, body):
        pieces = []
        for decl in str(body).split(";"):
            if not decl:
                pieces.append(decl)
                continue
            if ":" not in decl:
                pieces.append(scale_numbers(decl))
                continue
            prop, value = decl.split(":", 1)
            prop_name = prop.strip().lower()
            if ui_scale() > 1.0 and prop_name in _MENU_QSS_SPACING_PROPERTIES:
                pieces.append(decl)
            else:
                pieces.append(f"{prop}:{scale_numbers(value)}")
        return ";".join(pieces)

    text = str(style_text)
    block_re = re.compile(r"([^{}]+)\{([^{}]*)\}", re.S)
    out = []
    last = 0
    for match in block_re.finditer(text):
        out.append(scale_numbers(text[last:match.start()]))
        selector = match.group(1)
        body = match.group(2)
        out.append(f"{selector}{{{scale_body(selector, body)}}}")
        last = match.end()
    out.append(scale_numbers(text[last:]))
    return "".join(out)

def _scale_qt_font(font):
    if font is None or abs(ui_scale() - 1.0) < 1e-6:
        return font
    try:
        if int(font.cacheKey()) in _UI_SCALED_FONT_KEYS:
            return font
    except Exception:
        pass

    scaled = QtGui.QFont(font)
    try:
        point_size = float(scaled.pointSizeF())
    except Exception:
        point_size = -1.0
    if point_size > 0:
        scaled.setPointSizeF(ui_pt(point_size))
    else:
        try:
            pixel_size = int(scaled.pixelSize())
        except Exception:
            pixel_size = -1
        if pixel_size > 0:
            scaled.setPixelSize(ui_px(pixel_size))

    try:
        _UI_SCALED_FONT_KEYS.add(int(scaled.cacheKey()))
    except Exception:
        pass
    return scaled

def _scale_qt_dimension(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if value == 0 or abs(float(value)) >= 100000:
            return int(value)
        return ui_geometry_px(value)
    return value

def _scale_qt_size_args(args):
    if len(args) == 1 and isinstance(args[0], QtCore.QSize):
        size = args[0]
        return (QtCore.QSize(ui_geometry_px(size.width()), ui_geometry_px(size.height())),)
    if len(args) == 2 and all(isinstance(v, (int, float)) for v in args):
        return (ui_geometry_px(args[0]), ui_geometry_px(args[1]))
    return args

def _scale_qt_margins_args(args):
    if len(args) == 1 and isinstance(args[0], QtCore.QMargins):
        margins = args[0]
        return (
            QtCore.QMargins(
                ui_geometry_px(margins.left(), minimum=0),
                ui_geometry_px(margins.top(), minimum=0),
                ui_geometry_px(margins.right(), minimum=0),
                ui_geometry_px(margins.bottom(), minimum=0),
            ),
        )
    if len(args) == 4 and all(isinstance(v, (int, float)) for v in args):
        return tuple(ui_geometry_px(v, minimum=0) for v in args)
    return args

def install_qt_ui_scale_hooks():
    global _UI_SCALE_HOOKS_INSTALLED
    if _UI_SCALE_HOOKS_INSTALLED:
        return
    _UI_SCALE_HOOKS_INSTALLED = True

    original_app_set_stylesheet = QtWidgets.QApplication.setStyleSheet
    original_app_set_font = QtWidgets.QApplication.setFont
    original_widget_set_stylesheet = QtWidgets.QWidget.setStyleSheet
    original_widget_set_font = QtWidgets.QWidget.setFont
    original_resize = QtWidgets.QWidget.resize
    original_set_minimum_width = QtWidgets.QWidget.setMinimumWidth
    original_set_minimum_height = QtWidgets.QWidget.setMinimumHeight
    original_set_minimum_size = QtWidgets.QWidget.setMinimumSize
    original_set_fixed_width = QtWidgets.QWidget.setFixedWidth
    original_set_fixed_height = QtWidgets.QWidget.setFixedHeight
    original_set_fixed_size = QtWidgets.QWidget.setFixedSize
    original_layout_set_contents_margins = QtWidgets.QLayout.setContentsMargins
    original_layout_set_spacing = QtWidgets.QLayout.setSpacing
    original_grid_set_horizontal_spacing = QtWidgets.QGridLayout.setHorizontalSpacing
    original_grid_set_vertical_spacing = QtWidgets.QGridLayout.setVerticalSpacing

    def scaled_app_set_stylesheet(self, style_text):
        if not _UI_SCALE_REAPPLYING:
            try:
                setattr(self, "_geoviewer_raw_stylesheet", str(style_text or ""))
            except Exception:
                pass
        return original_app_set_stylesheet(self, _scale_qt_stylesheet(style_text))

    def scaled_app_set_font(self_or_font, font=None, *args):
        if isinstance(self_or_font, QtGui.QFont):
            call_font = self_or_font
            call_args = (() if font is None else (font,)) + args
            app = QtWidgets.QApplication.instance()
        else:
            call_font = font
            call_args = args
            app = self_or_font if isinstance(self_or_font, QtWidgets.QApplication) else QtWidgets.QApplication.instance()
        if app is not None and call_font is not None and not _UI_SCALE_REAPPLYING:
            try:
                setattr(app, "_geoviewer_raw_font", QtGui.QFont(call_font))
            except Exception:
                pass
        return original_app_set_font(_scale_qt_font(call_font), *call_args)

    def scaled_widget_set_stylesheet(self, style_text):
        if not _UI_SCALE_REAPPLYING:
            try:
                setattr(self, "_geoviewer_raw_stylesheet", str(style_text or ""))
            except Exception:
                pass
        return _with_object_ui_scale(
            self,
            lambda: original_widget_set_stylesheet(self, _scale_qt_stylesheet(style_text)),
        )

    def scaled_widget_set_font(self, font):
        if font is not None and not _UI_SCALE_REAPPLYING:
            try:
                setattr(self, "_geoviewer_raw_font", QtGui.QFont(font))
            except Exception:
                pass
        return _with_object_ui_scale(
            self,
            lambda: original_widget_set_font(self, _scale_qt_font(font)),
        )

    def scaled_resize(self, *args):
        if bool(getattr(self, "_geoviewer_skip_scale_next_resize", False)):
            try:
                setattr(self, "_geoviewer_skip_scale_next_resize", False)
            except Exception:
                pass
            return original_resize(self, *args)
        if not _UI_SCALE_REAPPLYING:
            try:
                setattr(self, "_geoviewer_raw_resize_args", tuple(args))
            except Exception:
                pass
        return _with_object_ui_scale(
            self,
            lambda: original_resize(self, *_scale_qt_size_args(args)),
        )

    def scaled_set_minimum_width(self, width):
        if not _UI_SCALE_REAPPLYING:
            try:
                setattr(self, "_geoviewer_raw_minimum_width", width)
            except Exception:
                pass
        return _with_object_ui_scale(
            self,
            lambda: original_set_minimum_width(self, _scale_qt_dimension(width)),
        )

    def scaled_set_minimum_height(self, height):
        if not _UI_SCALE_REAPPLYING:
            try:
                setattr(self, "_geoviewer_raw_minimum_height", height)
            except Exception:
                pass
        return _with_object_ui_scale(
            self,
            lambda: original_set_minimum_height(self, _scale_qt_dimension(height)),
        )

    def scaled_set_minimum_size(self, *args):
        if not _UI_SCALE_REAPPLYING:
            try:
                setattr(self, "_geoviewer_raw_minimum_size_args", tuple(args))
            except Exception:
                pass
        return _with_object_ui_scale(
            self,
            lambda: original_set_minimum_size(self, *_scale_qt_size_args(args)),
        )

    def scaled_set_fixed_width(self, width):
        if not _UI_SCALE_REAPPLYING:
            try:
                setattr(self, "_geoviewer_raw_fixed_width", width)
            except Exception:
                pass
        return _with_object_ui_scale(
            self,
            lambda: original_set_fixed_width(self, _scale_qt_dimension(width)),
        )

    def scaled_set_fixed_height(self, height):
        if not _UI_SCALE_REAPPLYING:
            try:
                setattr(self, "_geoviewer_raw_fixed_height", height)
            except Exception:
                pass
        return _with_object_ui_scale(
            self,
            lambda: original_set_fixed_height(self, _scale_qt_dimension(height)),
        )

    def scaled_set_fixed_size(self, *args):
        if not _UI_SCALE_REAPPLYING:
            try:
                setattr(self, "_geoviewer_raw_fixed_size_args", tuple(args))
            except Exception:
                pass
        return _with_object_ui_scale(
            self,
            lambda: original_set_fixed_size(self, *_scale_qt_size_args(args)),
        )

    def scaled_layout_set_contents_margins(self, *args):
        if not _UI_SCALE_REAPPLYING:
            try:
                setattr(self, "_geoviewer_raw_contents_margins_args", tuple(args))
            except Exception:
                pass
        return _with_object_ui_scale(
            self,
            lambda: original_layout_set_contents_margins(self, *_scale_qt_margins_args(args)),
        )

    def scaled_layout_set_spacing(self, spacing):
        if not _UI_SCALE_REAPPLYING:
            try:
                setattr(self, "_geoviewer_raw_spacing", spacing)
            except Exception:
                pass
        return _with_object_ui_scale(
            self,
            lambda: original_layout_set_spacing(self, _scale_qt_dimension(spacing)),
        )

    def scaled_grid_set_horizontal_spacing(self, spacing):
        if not _UI_SCALE_REAPPLYING:
            try:
                setattr(self, "_geoviewer_raw_horizontal_spacing", spacing)
            except Exception:
                pass
        return _with_object_ui_scale(
            self,
            lambda: original_grid_set_horizontal_spacing(self, _scale_qt_dimension(spacing)),
        )

    def scaled_grid_set_vertical_spacing(self, spacing):
        if not _UI_SCALE_REAPPLYING:
            try:
                setattr(self, "_geoviewer_raw_vertical_spacing", spacing)
            except Exception:
                pass
        return _with_object_ui_scale(
            self,
            lambda: original_grid_set_vertical_spacing(self, _scale_qt_dimension(spacing)),
        )

    QtWidgets.QApplication.setStyleSheet = scaled_app_set_stylesheet
    QtWidgets.QApplication.setFont = scaled_app_set_font
    QtWidgets.QWidget.setStyleSheet = scaled_widget_set_stylesheet
    QtWidgets.QWidget.setFont = scaled_widget_set_font
    QtWidgets.QWidget.resize = scaled_resize
    QtWidgets.QWidget.setMinimumWidth = scaled_set_minimum_width
    QtWidgets.QWidget.setMinimumHeight = scaled_set_minimum_height
    QtWidgets.QWidget.setMinimumSize = scaled_set_minimum_size
    QtWidgets.QWidget.setFixedWidth = scaled_set_fixed_width
    QtWidgets.QWidget.setFixedHeight = scaled_set_fixed_height
    QtWidgets.QWidget.setFixedSize = scaled_set_fixed_size
    QtWidgets.QLayout.setContentsMargins = scaled_layout_set_contents_margins
    QtWidgets.QLayout.setSpacing = scaled_layout_set_spacing
    QtWidgets.QGridLayout.setHorizontalSpacing = scaled_grid_set_horizontal_spacing
    QtWidgets.QGridLayout.setVerticalSpacing = scaled_grid_set_vertical_spacing

def _reapply_scaled_layout(layout, seen=None):
    if layout is None:
        return
    if _is_private_qmainwindow_layout(layout):
        return
    if seen is None:
        seen = set()
    key = id(layout)
    if key in seen:
        return
    seen.add(key)

    for attr, method_name in (
        ("_geoviewer_raw_contents_margins_args", "setContentsMargins"),
        ("_geoviewer_raw_spacing", "setSpacing"),
        ("_geoviewer_raw_horizontal_spacing", "setHorizontalSpacing"),
        ("_geoviewer_raw_vertical_spacing", "setVerticalSpacing"),
    ):
        try:
            raw = getattr(layout, attr, None)
            if raw is None:
                continue
            if isinstance(raw, tuple):
                getattr(layout, method_name)(*raw)
            elif hasattr(layout, method_name):
                getattr(layout, method_name)(raw)
        except Exception:
            pass

    try:
        for idx in range(layout.count()):
            item = layout.itemAt(idx)
            child_layout = item.layout() if item is not None else None
            if child_layout is not None:
                _reapply_scaled_layout(child_layout, seen)
    except Exception:
        pass
    try:
        layout.invalidate()
        layout.activate()
    except Exception:
        pass

def _reapply_scaled_widget(widget):
    if widget is None:
        return
    preserve_window_size = _preserve_qmainwindow_size_during_ui_scale(widget)
    try:
        raw_font = getattr(widget, "_geoviewer_raw_font", None)
        if raw_font is not None:
            widget.setFont(raw_font)
    except Exception:
        pass
    try:
        raw_style = getattr(widget, "_geoviewer_raw_stylesheet", None)
        if raw_style is not None:
            widget.setStyleSheet(raw_style)
    except Exception:
        pass
    for attr, method_name in (
        ("_geoviewer_raw_minimum_size_args", "setMinimumSize"),
        ("_geoviewer_raw_fixed_size_args", "setFixedSize"),
        ("_geoviewer_raw_resize_args", "resize"),
    ):
        if preserve_window_size:
            continue
        try:
            raw = getattr(widget, attr, None)
            if raw is not None:
                getattr(widget, method_name)(*raw)
        except Exception:
            pass
    for attr, method_name in (
        ("_geoviewer_raw_minimum_width", "setMinimumWidth"),
        ("_geoviewer_raw_minimum_height", "setMinimumHeight"),
        ("_geoviewer_raw_fixed_width", "setFixedWidth"),
        ("_geoviewer_raw_fixed_height", "setFixedHeight"),
    ):
        if preserve_window_size:
            continue
        try:
            raw = getattr(widget, attr, None)
            if raw is not None:
                getattr(widget, method_name)(raw)
        except Exception:
            pass
    try:
        if not preserve_window_size:
            _reapply_scaled_layout(widget.layout())
        widget.updateGeometry()
        widget.update()
    except Exception:
        pass

def refresh_ui_scale_control_widgets():
    app = QtWidgets.QApplication.instance()
    if app is None:
        return
    try:
        for widget in app.topLevelWidgets():
            updater = getattr(widget, "_update_ui_scale_menu", None)
            if callable(updater):
                updater()
    except Exception:
        pass

def apply_current_ui_scale_to_qt_app(app=None):
    global _UI_SCALE_REAPPLYING
    app = app or QtWidgets.QApplication.instance()
    if app is None:
        return
    previous = _UI_SCALE_REAPPLYING
    _UI_SCALE_REAPPLYING = True
    try:
        try:
            app.setProperty("geoviewer_ui_scale", ui_scale())
        except Exception:
            pass
        try:
            raw_font = getattr(app, "_geoviewer_raw_font", None)
            if raw_font is not None:
                app.setFont(raw_font)
        except Exception:
            pass
        try:
            raw_style = getattr(app, "_geoviewer_raw_stylesheet", None)
            if raw_style is not None:
                app.setStyleSheet(raw_style)
        except Exception:
            pass
        try:
            widgets = list(app.allWidgets())
        except Exception:
            widgets = []
        for widget in widgets:
            _reapply_scaled_widget(widget)
        try:
            app.processEvents(QtCore.QEventLoop.ExcludeUserInputEvents)
        except Exception:
            pass
    finally:
        _UI_SCALE_REAPPLYING = previous

def set_ui_scale(value, reapply=True, manual=True):
    global GEOVIEWER_UI_SCALE, GEOVIEWER_UI_SCALE_MANUAL, _UI_SCALE_REAPPLY_RATIO
    old_scale = ui_scale()
    GEOVIEWER_UI_SCALE = clamp_ui_scale(value, fallback=old_scale)
    GEOVIEWER_UI_SCALE_MANUAL = bool(manual)
    try:
        _UI_SCALE_REAPPLY_RATIO = GEOVIEWER_UI_SCALE / max(old_scale, 1e-6)
    except Exception:
        _UI_SCALE_REAPPLY_RATIO = 1.0
    if manual:
        try:
            os.environ["GEOVIEWER_UI_SCALE"] = f"{GEOVIEWER_UI_SCALE:.3f}".rstrip("0").rstrip(".")
        except Exception:
            pass
    try:
        _UI_SCALED_FONT_KEYS.clear()
    except Exception:
        pass
    if reapply:
        apply_current_ui_scale_to_qt_app()
    refresh_ui_scale_control_widgets()
    return GEOVIEWER_UI_SCALE

def reset_ui_scale_to_auto(reapply=True):
    global GEOVIEWER_UI_SCALE, GEOVIEWER_UI_SCALE_MANUAL, _UI_SCALE_REAPPLY_RATIO
    old_scale = ui_scale()
    GEOVIEWER_UI_SCALE_MANUAL = False
    if not GEOVIEWER_UI_SCALE_ENV_OVERRIDE:
        try:
            os.environ.pop("GEOVIEWER_UI_SCALE", None)
        except Exception:
            pass
    GEOVIEWER_UI_SCALE = _compute_auto_ui_scale()
    try:
        _UI_SCALE_REAPPLY_RATIO = GEOVIEWER_UI_SCALE / max(old_scale, 1e-6)
    except Exception:
        _UI_SCALE_REAPPLY_RATIO = 1.0
    try:
        _UI_SCALED_FONT_KEYS.clear()
    except Exception:
        pass
    if reapply:
        apply_current_ui_scale_to_qt_app()
    refresh_ui_scale_control_widgets()
    return GEOVIEWER_UI_SCALE

def apply_persisted_ui_scale_setting(value, reapply=True, respect_manual_override=False):
    if GEOVIEWER_UI_SCALE_ENV_OVERRIDE:
        return ui_scale()
    if respect_manual_override and ui_scale_is_manual():
        return ui_scale()
    normalized = normalize_persisted_ui_scale(value, "auto")
    if normalized == "auto":
        return reset_ui_scale_to_auto(reapply=reapply)
    return set_ui_scale(normalized, reapply=reapply, manual=True)

def format_ui_scale_label(value=None):
    if value is None:
        return f"{int(round(ui_scale() * 100.0))}%"
    normalized = normalize_persisted_ui_scale(value, "auto")
    if normalized == "auto":
        return "Auto"
    return f"{int(round(float(normalized) * 100.0))}%"

install_qt_ui_scale_hooks()

# ---- Matplotlib: force a Qt backend BEFORE importing pyplot/figure canvas ---- #
import matplotlib
try:
    matplotlib.use("Qt5Agg")  # preferred on PyQt5
except Exception:
    matplotlib.use("QtAgg")   # unified name on newer Matplotlib

import matplotlib.pyplot as plt  # needed for style API
from matplotlib import colors as mcolors
import matplotlib.patheffects as path_effects
from matplotlib.font_manager import FontProperties
from matplotlib.path import Path as MplPath

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QDialog, QLabel, QPushButton,
    QMessageBox
)

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.transform import Affine
from rasterio.warp import calculate_default_transform, reproject, Resampling, transform as warp_transform
from rasterio.windows import bounds as window_bounds

import geopandas as gpd
try:
    import fiona
    from fiona.transform import transform_geom
    from fiona.crs import CRS as FionaCRS
except ImportError:
    fiona = None
    transform_geom = None
    FionaCRS = None
from pyproj import Geod
from skimage.transform import estimate_transform, warp as skwarp, AffineTransform
from skimage.filters import sobel
from skimage.morphology import binary_dilation, binary_erosion
try:
    from skimage.morphology import footprint_rectangle
except ImportError:
    try:
        # Older scikit-image used rectangle(nrows, ncols) instead of
        # footprint_rectangle((nrows, ncols)).
        from skimage.morphology import rectangle as _skimage_rectangle

        def footprint_rectangle(shape, *args, **kwargs):
            try:
                rows, cols = tuple(shape)
            except Exception:
                rows = cols = int(shape)
            return _skimage_rectangle(int(rows), int(cols))
    except ImportError:
        def footprint_rectangle(shape, *args, **kwargs):
            try:
                shape = tuple(int(v) for v in shape)
            except Exception:
                shape = (int(shape), int(shape))
            return np.ones(shape, dtype=bool)
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import FancyBboxPatch, Rectangle
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
BASEMAP_FOLDER_NAME = "Basemaps"
WARP_COLS = ["warp_a","warp_b","warp_c","warp_d","warp_e","warp_f"]
LOG_COMMENT_COL = "comment"
LOG_LOGGED_DATETIME_COL = "logged_datetime"
LOG_BASEMAP_FILENAME_COL = "Basemap filename"
LOG_BASEMAP_DELTA_DAYS_COL = "Delta Time (in days)"
LOG_SOURCE_CRS_COL = "source_crs"
LOG_SOURCE_WIDTH_COL = "source_width"
LOG_SOURCE_HEIGHT_COL = "source_height"
SOURCE_GRID_COLS = [
    "source_grid_a", "source_grid_b", "source_grid_c",
    "source_grid_d", "source_grid_e", "source_grid_f",
]
LOG_COMMENT_MAX_CHARS = 300
COMMENT_FLAG_MAX_CHARS = 40
COMMENT_FLAG_GRID_ROW_SIZES = (6, 6, 6, 6, 6)
COMMENT_FLAG_GRID_MAX_FLAGS = sum(COMMENT_FLAG_GRID_ROW_SIZES)
SPLIT_REGION_GRID_MAX_SIZE = 1400
SPLIT_TOPOLOGY_TOLERANCE_PIXELS = 0.75
STRIPES_RESTRUCTURED_COMMENT = "Stripes_Restructured"
ORIGINAL_COPY_COMMENT = "Original Copy"
SHAPEFILE = None  # Optional future fallback path to a shapefile
OUTPUT_ALIAS_AZIMUTH = 99998.0
OUTPUT_ALIAS_DISTANCE = 99998.0
REJECT_AZIMUTH = -99999.0
REJECT_DISTANCE = -99999.0
DEFAULT_COMMENT_FLAGS = (
    "Needs review",
    "Cloud",
    "Cloud shadow",
    "Haze",
    "Smoke",
    "Dust",
    "Plume",
    "Fire",
    "Hotspot",
    "Thermal anomaly",
    "Flooding",
    "Water",
    "Snow/Ice",
    "Vegetation",
    "Agriculture",
    "Urban",
    "Bare soil",
    "Terrain shadow",
    "Low contrast",
    "Sensor noise",
    "Striping",
    "Missing data",
    "Edge artifact",
    "Georef mismatch",
    "Partial scene",
)


@dataclass
class SplitLineSegment:
    x1: float
    y1: float
    x2: float
    y2: float
    solid: bool = False
    group_id: int = 0

    def as_tuple(self):
        return (self.x1, self.y1, self.x2, self.y2)

    def move_by(self, dx, dy, width, height):
        self.x1 = min(max(self.x1 + dx, 0.0), float(width))
        self.y1 = min(max(self.y1 + dy, 0.0), float(height))
        self.x2 = min(max(self.x2 + dx, 0.0), float(width))
        self.y2 = min(max(self.y2 + dy, 0.0), float(height))


@dataclass
class SplitRegionInfo:
    label: int
    preview_pixels: int
    centroid_x: float
    centroid_y: float
    vertices: list
    segment_indices: tuple


@dataclass
class SplitRegionAnalysis:
    labels: np.ndarray
    regions: list
    grid_width: int
    grid_height: int


def _split_scaled_shape(width, height, max_size):
    longest = max(int(width), int(height), 1)
    scale = min(1.0, float(max_size) / float(longest)) if max_size else 1.0
    out_width = max(1, int(round(width * scale)))
    out_height = max(1, int(round(height * scale)))
    return out_height, out_width


def _split_grid_shape_for(width, height, max_size):
    if max_size is None:
        return int(height), int(width)
    return _split_scaled_shape(width, height, max_size)


def _split_full_to_grid(x, y, width, height, grid_width, grid_height):
    gx = float(x) / max(float(width), 1.0) * max(float(grid_width - 1), 1.0)
    gy = float(y) / max(float(height), 1.0) * max(float(grid_height - 1), 1.0)
    return gx, gy


def _split_draw_line_on_barrier(barrier, x0, y0, x1, y1, radius=0):
    h, w = barrier.shape
    steps = int(max(abs(x1 - x0), abs(y1 - y0))) + 1
    steps = max(2, steps)
    xs = np.linspace(x0, x1, steps)
    ys = np.linspace(y0, y1, steps)
    cols = np.clip(np.rint(xs).astype(np.int64), 0, w - 1)
    rows = np.clip(np.rint(ys).astype(np.int64), 0, h - 1)
    if radius <= 0:
        barrier[rows, cols] = True
        return
    for dy in range(-radius, radius + 1):
        rr = rows + dy
        good_y = (rr >= 0) & (rr < h)
        for dx in range(-radius, radius + 1):
            if dx * dx + dy * dy > radius * radius:
                continue
            cc = cols + dx
            good = good_y & (cc >= 0) & (cc < w)
            barrier[rr[good], cc[good]] = True


def _split_polygon_area(vertices):
    if len(vertices) < 3:
        return 0.0
    area = 0.0
    for index, (x1, y1) in enumerate(vertices):
        x2, y2 = vertices[(index + 1) % len(vertices)]
        area += x1 * y2 - x2 * y1
    return 0.5 * area


def _split_polygon_centroid(vertices):
    area = _split_polygon_area(vertices)
    if abs(area) < 1e-9:
        xs = [p[0] for p in vertices]
        ys = [p[1] for p in vertices]
        return float(np.mean(xs)), float(np.mean(ys))
    cx = 0.0
    cy = 0.0
    for index, (x1, y1) in enumerate(vertices):
        x2, y2 = vertices[(index + 1) % len(vertices)]
        cross = x1 * y2 - x2 * y1
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    scale = 1.0 / (6.0 * area)
    return cx * scale, cy * scale


def _split_endpoint_node(nodes, point, tolerance=SPLIT_TOPOLOGY_TOLERANCE_PIXELS):
    x, y = point
    for index, node in enumerate(nodes):
        if math.hypot(node[0] - x, node[1] - y) <= tolerance:
            count = node[2] + 1
            node[0] = (node[0] * node[2] + x) / count
            node[1] = (node[1] * node[2] + y) / count
            node[2] = count
            return index
    nodes.append([float(x), float(y), 1])
    return len(nodes) - 1


def _split_cross(ax, ay, bx, by):
    return ax * by - ay * bx


def _split_segment_point(seg, t):
    return (
        seg.x1 + (seg.x2 - seg.x1) * float(t),
        seg.y1 + (seg.y2 - seg.y1) * float(t),
    )


def _split_project_t(seg, point):
    dx = seg.x2 - seg.x1
    dy = seg.y2 - seg.y1
    denom = dx * dx + dy * dy
    if denom <= 1e-12:
        return 0.0
    return ((point[0] - seg.x1) * dx + (point[1] - seg.y1) * dy) / denom


def _split_add_split(split_points, index, t):
    if index < 0 or index >= len(split_points):
        return
    if t < -1e-6 or t > 1.0 + 1e-6:
        return
    split_points[index].append(max(0.0, min(1.0, float(t))))


def _split_add_near_endpoint_splits(split_points, index_a, seg_a, index_b, seg_b):
    for t_a, endpoint in ((0.0, (seg_a.x1, seg_a.y1)), (1.0, (seg_a.x2, seg_a.y2))):
        t_b = _split_project_t(seg_b, endpoint)
        if t_b < -1e-6 or t_b > 1.0 + 1e-6:
            continue
        projected = _split_segment_point(seg_b, max(0.0, min(1.0, t_b)))
        if math.hypot(endpoint[0] - projected[0], endpoint[1] - projected[1]) <= SPLIT_TOPOLOGY_TOLERANCE_PIXELS:
            _split_add_split(split_points, index_a, t_a)
            _split_add_split(split_points, index_b, t_b)


def _split_add_pair_intersections(split_points, index_a, seg_a, index_b, seg_b):
    px, py = seg_a.x1, seg_a.y1
    rx, ry = seg_a.x2 - seg_a.x1, seg_a.y2 - seg_a.y1
    qx, qy = seg_b.x1, seg_b.y1
    sx, sy = seg_b.x2 - seg_b.x1, seg_b.y2 - seg_b.y1
    rxs = _split_cross(rx, ry, sx, sy)
    qpx = qx - px
    qpy = qy - py
    qpxr = _split_cross(qpx, qpy, rx, ry)

    if abs(rxs) > 1e-9:
        t = _split_cross(qpx, qpy, sx, sy) / rxs
        u = _split_cross(qpx, qpy, rx, ry) / rxs
        if -1e-6 <= t <= 1.0 + 1e-6 and -1e-6 <= u <= 1.0 + 1e-6:
            _split_add_split(split_points, index_a, t)
            _split_add_split(split_points, index_b, u)
        return

    if abs(qpxr) <= 1e-9:
        rr = rx * rx + ry * ry
        if rr <= 1e-12:
            return
        t0 = ((qx - px) * rx + (qy - py) * ry) / rr
        t1 = ((qx + sx - px) * rx + (qy + sy - py) * ry) / rr
        lo = max(0.0, min(t0, t1))
        hi = min(1.0, max(t0, t1))
        if lo <= hi + 1e-6:
            for t in (lo, hi):
                point = _split_segment_point(seg_a, t)
                _split_add_split(split_points, index_a, t)
                _split_add_split(split_points, index_b, _split_project_t(seg_b, point))
        return

    _split_add_near_endpoint_splits(split_points, index_a, seg_a, index_b, seg_b)
    _split_add_near_endpoint_splits(split_points, index_b, seg_b, index_a, seg_a)


def _split_linework_edges(segments):
    split_points = [[0.0, 1.0] for _ in segments]
    for index_a in range(len(segments)):
        for index_b in range(index_a + 1, len(segments)):
            _split_add_pair_intersections(split_points, index_a, segments[index_a], index_b, segments[index_b])
            _split_add_near_endpoint_splits(split_points, index_a, segments[index_a], index_b, segments[index_b])
            _split_add_near_endpoint_splits(split_points, index_b, segments[index_b], index_a, segments[index_a])

    nodes = []
    raw_edges = []
    for index, seg in enumerate(segments):
        values = sorted(split_points[index])
        merged = []
        for value in values:
            if not merged or abs(value - merged[-1]) > 1e-5:
                merged.append(value)
        for t0, t1 in zip(merged[:-1], merged[1:]):
            if abs(t1 - t0) <= 1e-5:
                continue
            p0 = _split_segment_point(seg, t0)
            p1 = _split_segment_point(seg, t1)
            if math.hypot(p1[0] - p0[0], p1[1] - p0[1]) < 1e-6:
                continue
            a = _split_endpoint_node(nodes, p0)
            b = _split_endpoint_node(nodes, p1)
            if a == b:
                continue
            raw_edges.append((a, b, index))

    merged = {}
    for a, b, index in raw_edges:
        key = (a, b) if a < b else (b, a)
        if key not in merged:
            merged[key] = [key[0], key[1], set()]
        merged[key][2].add(index)

    edges = []
    adjacency = defaultdict(list)
    for a, b, indices in merged.values():
        edge_id = len(edges)
        edges.append((a, b, tuple(sorted(indices))))
        adjacency[a].append((b, edge_id))
        adjacency[b].append((a, edge_id))
    return nodes, edges, adjacency


def _split_closed_cycles_from_segments(segments):
    nodes, edges, _adjacency = _split_linework_edges(segments)
    if not edges:
        return []

    outgoing = defaultdict(list)
    for edge_id, (a, b, _segment_index) in enumerate(edges):
        ax, ay = nodes[a][0], nodes[a][1]
        bx, by = nodes[b][0], nodes[b][1]
        outgoing[a].append((math.atan2(by - ay, bx - ax), b, edge_id))
        outgoing[b].append((math.atan2(ay - by, ax - bx), a, edge_id))

    positions = {}
    for node, entries in outgoing.items():
        entries.sort(key=lambda item: item[0])
        outgoing[node] = entries
        for idx, (_angle, to_node, edge_id) in enumerate(entries):
            positions[(node, to_node, edge_id)] = idx

    def next_half_edge(u, v, edge_id):
        entries = outgoing.get(v, [])
        if not entries:
            return None
        reverse_idx = positions.get((v, u, edge_id))
        if reverse_idx is None:
            return None
        _angle, next_node, next_edge_id = entries[(reverse_idx - 1) % len(entries)]
        return v, next_node, next_edge_id

    cycles = []
    visited = set()
    max_steps = max(8, len(edges) * 4)
    for edge_id, (a, b, _segment_index) in enumerate(edges):
        for start in ((a, b, edge_id), (b, a, edge_id)):
            if start in visited:
                continue
            current = start
            path_nodes = []
            path_edges = []
            for _step in range(max_steps):
                if current in visited:
                    break
                visited.add(current)
                u, v, current_edge_id = current
                path_nodes.append(u)
                path_edges.append(current_edge_id)
                current = next_half_edge(u, v, current_edge_id)
                if current is None:
                    break
                if current == start:
                    if len(path_edges) >= 3:
                        cycles.append((list(path_nodes), list(path_edges)))
                    break

    results = []
    for node_path, edge_ids in cycles:
        vertices = [(nodes[node][0], nodes[node][1]) for node in node_path]
        area = _split_polygon_area(vertices)
        if len(vertices) < 3 or area <= 4.0:
            continue
        segment_indices = tuple(
            sorted(
                {
                    segment_index
                    for edge_id in edge_ids
                    for segment_index in edges[edge_id][2]
                }
            )
        )
        results.append((vertices, segment_indices))

    results.sort(key=lambda item: abs(_split_polygon_area(item[0])))
    unique = []
    used_vertex_keys = set()
    for vertices, segment_indices in results:
        key = tuple(sorted((round(x, 3), round(y, 3)) for x, y in vertices))
        if key in used_vertex_keys:
            continue
        used_vertex_keys.add(key)
        unique.append((vertices, segment_indices))
    return unique


def _split_polygon_mask(width, height, vertices, max_size=None):
    grid_height, grid_width = _split_grid_shape_for(width, height, max_size)
    grid_width = max(2, int(grid_width))
    grid_height = max(2, int(grid_height))
    grid_vertices = [
        _split_full_to_grid(x, y, width, height, grid_width, grid_height)
        for x, y in vertices
    ]
    rows, cols = np.mgrid[0:grid_height, 0:grid_width]
    points = np.column_stack((cols.ravel() + 0.5, rows.ravel() + 0.5))
    mask = MplPath(grid_vertices).contains_points(points, radius=0.001).reshape((grid_height, grid_width))
    boundary = np.zeros((grid_height, grid_width), dtype=bool)
    for index, (x0, y0) in enumerate(grid_vertices):
        x1, y1 = grid_vertices[(index + 1) % len(grid_vertices)]
        _split_draw_line_on_barrier(boundary, x0, y0, x1, y1, radius=0)
    return mask | boundary


def _split_region_label_mask(width, height, regions, max_size=None):
    grid_height, grid_width = _split_grid_shape_for(width, height, max_size)
    grid_width = max(2, int(grid_width))
    grid_height = max(2, int(grid_height))
    labels = np.zeros((grid_height, grid_width), dtype=np.int32)
    for region in sorted(regions, key=lambda item: abs(_split_polygon_area(item.vertices)), reverse=True):
        if not region.vertices:
            continue
        labels[_split_polygon_mask(width, height, region.vertices, max_size=max_size)] = int(region.label)
    return labels


def _split_analyze_regions(width, height, segments, max_size=SPLIT_REGION_GRID_MAX_SIZE):
    grid_height, grid_width = _split_grid_shape_for(width, height, max_size)
    grid_width = max(2, int(grid_width))
    grid_height = max(2, int(grid_height))
    labels = np.zeros((grid_height, grid_width), dtype=np.int32)
    regions = []

    for seg in segments:
        seg.solid = False

    closed = sorted(
        _split_closed_cycles_from_segments(segments),
        key=lambda item: abs(_split_polygon_area(item[0])),
        reverse=True,
    )
    for label, (vertices, segment_indices) in enumerate(closed, start=1):
        mask = _split_polygon_mask(width, height, vertices, max_size=max_size)
        if not np.any(mask):
            continue
        for index in segment_indices:
            if 0 <= index < len(segments):
                segments[index].solid = True
        labels[mask] = label
        cx, cy = _split_polygon_centroid(vertices)
        regions.append(
            SplitRegionInfo(
                label=label,
                preview_pixels=int(np.count_nonzero(mask)),
                centroid_x=float(cx),
                centroid_y=float(cy),
                vertices=list(vertices),
                segment_indices=tuple(segment_indices),
            )
        )

    regions.sort(key=lambda item: (item.centroid_y, item.centroid_x))
    remap = {region.label: index + 1 for index, region in enumerate(regions)}
    if remap:
        new_labels = np.zeros_like(labels)
        for old_label, new_label in remap.items():
            new_labels[labels == old_label] = new_label
        labels = new_labels
        for region in regions:
            region.label = remap[region.label]
    for region in regions:
        region.preview_pixels = int(np.count_nonzero(labels == region.label))
    return SplitRegionAnalysis(labels=labels, regions=regions, grid_width=grid_width, grid_height=grid_height)


def _split_nearest_label_to_point(labels, x, y, radius=5):
    row = int(round(float(y)))
    col = int(round(float(x)))
    if 0 <= row < labels.shape[0] and 0 <= col < labels.shape[1]:
        value = int(labels[row, col])
        if value > 0:
            return value
    for rad in range(1, radius + 1):
        r0 = max(0, row - rad)
        r1 = min(labels.shape[0], row + rad + 1)
        c0 = max(0, col - rad)
        c1 = min(labels.shape[1], col + rad + 1)
        sub = labels[r0:r1, c0:c1]
        values = sub[sub > 0]
        if values.size:
            counts = np.bincount(values.ravel())
            return int(np.argmax(counts))
    return 0


NAN_TRANSPARENT_OPTIONS = [
    ("25% transparent", "transparent_25", 0.75),
    ("50% transparent", "transparent_50", 0.50),
    ("75% transparent", "transparent_75", 0.25),
    ("100% transparent", "transparent_100", 0.0),
]
THERMAL_BLEND_MODES = (
    "normal",
    "screen",
    "addition",
    "overlay",
    "soft light",
    "hard light",
    "difference",
    "subtract",
)
THERMAL_BLEND_MODE_LABELS = {
    "normal": "Normal",
    "screen": "Screen",
    "addition": "Addition",
    "overlay": "Overlay",
    "soft light": "Soft Light",
    "hard light": "Hard Light",
    "difference": "Difference",
    "subtract": "Subtract",
}
VISUAL_RESAMPLING_OPTIONS = (
    ("Nearest Neighbor", "nearest"),
    ("Bilinear (2x2 kernel)", "bilinear"),
    ("Cubic (4x4 kernel)", "cubic"),
    ("Cubic B-spline (4x4 kernel)", "cubic_spline"),
    ("Lanczos (6x6 kernel)", "lanczos"),
    ("Average", "average"),
    ("Mode", "mode"),
)
VISUAL_RESAMPLING_LABELS = {
    token: label for label, token in VISUAL_RESAMPLING_OPTIONS
}
VISUAL_RESAMPLING_RASTERIO = {
    "nearest": Resampling.nearest,
    "bilinear": Resampling.bilinear,
    "cubic": Resampling.cubic,
    "cubic_spline": Resampling.cubic_spline,
    "lanczos": Resampling.lanczos,
    "average": Resampling.average,
    "mode": Resampling.mode,
}
VISUAL_RESAMPLING_MPL_INTERPOLATION = {
    "nearest": "nearest",
    "bilinear": "bilinear",
    "cubic": "bicubic",
    "cubic_spline": "spline16",
    "lanczos": "lanczos",
    "average": "nearest",
    "mode": "nearest",
}
BASEMAP_RESOLUTION_OPTIONS = (
    ("Dynamic (fast)", "dynamic"),
    ("Full Source (slow)", "full_source"),
)
BASEMAP_RESOLUTION_LABELS = {
    token: label for label, token in BASEMAP_RESOLUTION_OPTIONS
}
BASEMAP_COLOR_SCALING_OPTIONS = (
    ("Normal", "normal"),
    ("Inverted", "inverted"),
)
BASEMAP_COLOR_SCALING_LABELS = {
    token: label for label, token in BASEMAP_COLOR_SCALING_OPTIONS
}
BASEMAP_MODE_OPTIONS = (
    ("Nearest by date within category", "nearest"),
    ("Single scene for all images", "single"),
)
BASEMAP_MODE_LABELS = {
    token: label for label, token in BASEMAP_MODE_OPTIONS
}
BASEMAP_CATEGORY_LABELS = {
    "refl": "Reflective color RGB (_refl)",
    "tir": "Thermal IR single band (_tir)",
    "OPERA_BWTR": "OPERA binary water (BWTR)",
    "ECOSTRESS_WATER": "ECOSTRESS water",
    "single": "Single band",
    "rgb": "RGB",
    "other": "Other",
}
BASEMAP_DELTA_LABEL_COLOR = "#FFFFFF"
BASEMAP_DELTA_LABEL_STROKE_COLOR = "#000000"
MIN_MAIN_PANEL_TEXT_SCALE = 0.50
MAX_MAIN_PANEL_TEXT_SCALE = 3.00
MAIN_PANEL_TEXT_SCALE_CHOICES = (
    ("50%", 0.50),
    ("80%", 0.80),
    ("100%", 1.00),
    ("120%", 1.20),
    ("150%", 1.50),
    ("200%", 2.00),
    ("250%", 2.50),
    ("300%", 3.00),
)

def normalize_main_panel_text_scale(value, fallback=1.0):
    try:
        numeric = float(value)
    except Exception:
        numeric = float(fallback)
    if not math.isfinite(numeric):
        numeric = float(fallback)
    return max(MIN_MAIN_PANEL_TEXT_SCALE, min(MAX_MAIN_PANEL_TEXT_SCALE, numeric))

def format_main_panel_text_scale_label(value):
    scale = normalize_main_panel_text_scale(value)
    return f"{int(round(scale * 100.0))}%"

def normalize_thermal_blend_mode(value, fallback="normal"):
    fallback = str(fallback or "normal").strip().lower()
    if fallback not in THERMAL_BLEND_MODES:
        fallback = "normal"
    text = str(value or "").strip().lower().replace("_", " ").replace("-", " ")
    text = re.sub(r"\s+", " ", text)
    aliases = {
        "add": "addition",
        "linear dodge": "addition",
        "softlight": "soft light",
        "hardlight": "hard light",
        "minus": "subtract",
        "subtraction": "subtract",
    }
    text = aliases.get(text, text)
    return text if text in THERMAL_BLEND_MODES else fallback

def thermal_blend_mode_display_label(value):
    mode = normalize_thermal_blend_mode(value)
    return THERMAL_BLEND_MODE_LABELS.get(mode, mode.title())

def normalize_visual_resampling(value, fallback="nearest"):
    fallback = str(fallback or "nearest").strip().lower().replace("-", "_").replace(" ", "_")
    if fallback not in VISUAL_RESAMPLING_LABELS:
        fallback = "nearest"
    text = str(value or "").strip().lower()
    text = text.replace("-", " ").replace("_", " ")
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    aliases = {
        "nn": "nearest",
        "nearest": "nearest",
        "nearest neighbor": "nearest",
        "nearest neighbour": "nearest",
        "bilinear": "bilinear",
        "linear": "bilinear",
        "cubic": "cubic",
        "bicubic": "cubic",
        "cubic b spline": "cubic_spline",
        "cubic bspline": "cubic_spline",
        "cubic spline": "cubic_spline",
        "bspline": "cubic_spline",
        "b spline": "cubic_spline",
        "lanczos": "lanczos",
        "average": "average",
        "averaging": "average",
        "mode": "mode",
    }
    token = aliases.get(text, text.replace(" ", "_"))
    return token if token in VISUAL_RESAMPLING_LABELS else fallback

def visual_resampling_display_label(value):
    token = normalize_visual_resampling(value)
    return VISUAL_RESAMPLING_LABELS.get(token, token.title())

def visual_resampling_rasterio(value):
    return VISUAL_RESAMPLING_RASTERIO.get(
        normalize_visual_resampling(value),
        Resampling.nearest,
    )

def visual_resampling_mpl_interpolation(value):
    return VISUAL_RESAMPLING_MPL_INTERPOLATION.get(
        normalize_visual_resampling(value),
        "nearest",
    )

def normalize_basemap_resolution_mode(value, fallback="dynamic"):
    fallback = str(fallback or "dynamic").strip().lower().replace("-", "_").replace(" ", "_")
    if fallback not in BASEMAP_RESOLUTION_LABELS:
        fallback = "dynamic"
    text = str(value or "").strip().lower()
    text = text.replace("-", " ").replace("_", " ")
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    aliases = {
        "auto": "dynamic",
        "dynamic": "dynamic",
        "dynamic variable": "dynamic",
        "dynamic fast": "dynamic",
        "variable": "dynamic",
        "match thermal": "dynamic",
        "thermal": "dynamic",
        "fast": "dynamic",
        "full": "full_source",
        "full source": "full_source",
        "source": "full_source",
        "slow": "full_source",
        "native": "full_source",
        "original": "full_source",
    }
    token = aliases.get(text, text.replace(" ", "_"))
    return token if token in BASEMAP_RESOLUTION_LABELS else fallback

def basemap_resolution_display_label(value):
    token = normalize_basemap_resolution_mode(value)
    return BASEMAP_RESOLUTION_LABELS.get(token, token.title())

def normalize_basemap_color_scaling(value, fallback="normal"):
    fallback = str(fallback or "normal").strip().lower().replace("-", "_").replace(" ", "_")
    if fallback not in BASEMAP_COLOR_SCALING_LABELS:
        fallback = "normal"
    text = str(value or "").strip().lower()
    text = text.replace("-", " ").replace("_", " ")
    text = re.sub(r"\s+", " ", text).strip()
    aliases = {
        "normal": "normal",
        "regular": "normal",
        "standard": "normal",
        "invert": "inverted",
        "inverted": "inverted",
        "reverse": "inverted",
        "reversed": "inverted",
    }
    token = aliases.get(text, text.replace(" ", "_"))
    return token if token in BASEMAP_COLOR_SCALING_LABELS else fallback

def normalize_basemap_cmap(value, fallback="gray"):
    text = str(value or "").strip()
    if not text:
        return fallback
    try:
        plt.get_cmap(text)
        return text
    except Exception:
        return fallback

def normalize_basemap_mode(value, fallback="nearest"):
    fallback = str(fallback or "nearest").strip().lower().replace("-", "_").replace(" ", "_")
    if fallback not in BASEMAP_MODE_LABELS:
        fallback = "nearest"
    text = str(value or "").strip().lower()
    text = text.replace("-", " ").replace("_", " ")
    text = re.sub(r"\s+", " ", text).strip()
    aliases = {
        "auto": "nearest",
        "nearest": "nearest",
        "nearest by date": "nearest",
        "nearest date": "nearest",
        "category": "nearest",
        "per scene": "nearest",
        "single": "single",
        "one": "single",
        "fixed": "single",
        "same": "single",
        "single scene": "single",
        "single scene for all images": "single",
    }
    token = aliases.get(text, text.replace(" ", "_"))
    return token if token in BASEMAP_MODE_LABELS else fallback

def basemap_mode_display_label(value):
    token = normalize_basemap_mode(value)
    return BASEMAP_MODE_LABELS.get(token, token.title())

def normalize_basemap_category(value):
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.lstrip("_")
    low = text.lower()
    if low in ("refl", "reflective", "reflectance", "rgb_color", "rgb"):
        return "refl" if low != "rgb" else "rgb"
    if low in ("tir", "thermal", "thermal_ir", "thermal infrared"):
        return "tir"
    hls_match = re.fullmatch(r"hls[._\-\s]*(b\d+[a-z0-9]*)", low)
    if hls_match:
        return f"HLS_{hls_match.group(1).upper()}"
    if low in ("opera", "opera_bwtr", "opera bwtr", "bwtr", "binary_water", "binary water"):
        return "OPERA_BWTR"
    if low in ("ecostress_water", "ecostress water", "eco_water", "eco water"):
        return "ECOSTRESS_WATER"
    m = re.fullmatch(r"b\d+[a-z0-9]*", low)
    if m:
        return low.upper()
    if low in ("single", "single_band", "other"):
        return low.replace("_band", "")
    return text

def basemap_category_for_path(path):
    stem = os.path.splitext(os.path.basename(str(path or "")))[0]
    low = stem.lower()
    if low.startswith("hls."):
        m = re.search(r"\.(b\d+[a-z0-9]*)$", low)
        if m:
            return f"HLS_{m.group(1).upper()}"
        return "hls"
    if low.startswith("opera_") and low.endswith("_bwtr"):
        return "OPERA_BWTR"
    if low.startswith("eco") and low.endswith("_water"):
        return "ECOSTRESS_WATER"
    if re.search(r"_refl$", low):
        return "refl"
    if re.search(r"_tir$", low):
        return "tir"
    m = re.search(r"_(b\d+[a-z0-9]*)$", low)
    if m:
        return m.group(1).upper()
    try:
        with rasterio.open(path) as src:
            if int(src.count) >= 3:
                return "rgb"
            if int(src.count) == 1:
                return "single"
    except Exception:
        pass
    return "other"

def basemap_category_label(category):
    token = normalize_basemap_category(category)
    if not token:
        return ""
    if token in BASEMAP_CATEGORY_LABELS:
        return BASEMAP_CATEGORY_LABELS[token]
    if re.fullmatch(r"HLS_B\d+[A-Z0-9]*", token):
        band = token.split("_", 1)[1]
        return f"HLS single band (.{band})"
    if re.fullmatch(r"B\d+[A-Z0-9]*", token):
        return f"Landsat single band (_{token})"
    return str(token)

def basemap_path_is_rgb(path):
    category = basemap_category_for_path(path)
    if category in ("refl", "rgb"):
        return True
    try:
        with rasterio.open(path) as src:
            return int(src.count) >= 3
    except Exception:
        return False

def parse_basemap_acquisition_date(path):
    from datetime import datetime
    base = os.path.basename(str(path or ""))
    upper = base.upper()
    if upper.startswith("HLS."):
        hls_match = re.search(r"(?<!\d)(\d{4})(\d{3})T\d{6}(?!\d)", base)
        if hls_match:
            try:
                return datetime.strptime("".join(hls_match.groups()), "%Y%j").date()
            except Exception:
                pass
    if upper.startswith("OPERA_") and "_BWTR" in upper:
        opera_match = re.search(r"_(\d{8})T\d{6}Z_", base)
        if opera_match:
            try:
                return datetime.strptime(opera_match.group(1), "%Y%m%d").date()
            except Exception:
                pass
    if upper.startswith("ECO") and (upper.endswith("_WATER.TIF") or upper.endswith("_WATER.TIFF")):
        eco_match = re.search(r"_(\d{8})T\d{6}(?:_|$)", base)
        if eco_match:
            try:
                return datetime.strptime(eco_match.group(1), "%Y%m%d").date()
            except Exception:
                pass
    matches = re.findall(r"(?<!\d)(\d{8})(?!\d)", base)
    for text in matches:
        try:
            return datetime.strptime(text, "%Y%m%d").date()
        except Exception:
            continue
    return None

def parse_landsat_basemap_acquisition_date(path):
    return parse_basemap_acquisition_date(path)

def basemap_product_nodata_values(path):
    category = normalize_basemap_category(basemap_category_for_path(path))
    values = [65535.0]
    if category == "tir":
        values.append(0.0)
    if re.fullmatch(r"B\d+[A-Z0-9]*", category):
        values.append(0.0)
    if category == "hls":
        values.append(-9999.0)
    if re.fullmatch(r"HLS_B\d+[A-Z0-9]*", category):
        values.append(-9999.0)
    if category == "OPERA_BWTR":
        values.append(255.0)
    return tuple(dict.fromkeys(values))

def apply_basemap_product_nodata_mask(arr, path):
    category = normalize_basemap_category(basemap_category_for_path(path))
    nodata_values = basemap_product_nodata_values(path)
    if not nodata_values:
        return arr
    try:
        data = np.ma.asarray(arr)
        raw = np.asarray(data.data)
        mask = np.ma.getmaskarray(data)
        extra_mask = np.zeros(raw.shape, dtype=bool)
        for value in nodata_values:
            if np.issubdtype(raw.dtype, np.floating):
                extra_mask |= np.isclose(raw, float(value), rtol=0.0, atol=1e-6)
            else:
                try:
                    extra_mask |= raw == np.array(value, dtype=raw.dtype).item()
                except Exception:
                    extra_mask |= raw.astype("float64", copy=False) == float(value)
        combined_mask = mask | extra_mask
        if category == "OPERA_BWTR":
            if np.issubdtype(raw.dtype, np.floating):
                is_water = np.isclose(raw, 1.0, rtol=0.0, atol=1e-6)
                out_dtype = raw.dtype
            else:
                try:
                    is_water = raw == np.array(1, dtype=raw.dtype).item()
                    out_dtype = raw.dtype if np.issubdtype(raw.dtype, np.number) else np.uint8
                except Exception:
                    is_water = raw.astype("float64", copy=False) == 1.0
                    out_dtype = np.uint8
            clamped = np.where(is_water & ~combined_mask, 1, 0).astype(out_dtype, copy=False)
            clamped = np.array(clamped, copy=True)
            try:
                nodata_value = np.array(255, dtype=clamped.dtype).item()
            except Exception:
                nodata_value = 255.0
            clamped[combined_mask] = nodata_value
            return np.ma.array(clamped, mask=combined_mask, copy=False)
        if extra_mask.any():
            return np.ma.array(raw, mask=combined_mask, copy=False)
    except Exception:
        pass
    return arr

def basemap_scalar_display_limits(arr, std_limit=5.0):
    try:
        data = np.asarray(arr, dtype="float64")
        finite = data[np.isfinite(data)]
        if finite.size <= 0:
            return 0.0, 1.0
        if finite.size >= 2:
            mean = float(np.mean(finite))
            std = float(np.std(finite))
            if np.isfinite(mean) and np.isfinite(std) and std > 0.0:
                threshold = mean + (float(std_limit) * std)
                filtered = finite[finite <= threshold]
                if filtered.size > 0:
                    finite = filtered
        vmin = float(np.min(finite))
        vmax = float(np.max(finite))
        if not np.isfinite(vmin) or not np.isfinite(vmax):
            return 0.0, 1.0
        if vmax <= vmin:
            pad = max(abs(vmin) * 1e-6, 1e-6)
            return vmin - pad, vmax + pad
        return vmin, vmax
    except Exception:
        return 0.0, 1.0

def acquisition_date_from_scene_path(path):
    from datetime import datetime
    try:
        dt_text = parse_datetime_from_filename(path)
    except Exception:
        dt_text = ""
    text = str(dt_text or "").strip()
    for fmt, width in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d", 10)):
        try:
            return datetime.strptime(text[:width], fmt).date()
        except Exception:
            pass
    auto_dt = auto_detect_datetime_from_filename(os.path.basename(str(path or "")))
    if auto_dt:
        try:
            return datetime.strptime(str(auto_dt), "%Y-%m-%d %H:%M:%S").date()
        except Exception:
            pass
    return None

def basemap_delta_days_for_scene(scene_path, basemap_path):
    scene_date = acquisition_date_from_scene_path(scene_path)
    basemap_date = parse_basemap_acquisition_date(basemap_path)
    if scene_date is None or basemap_date is None:
        return None
    return abs((scene_date - basemap_date).days)

def _basemap_category_sort_key(category):
    token = normalize_basemap_category(category)
    order = {"refl": 0, "tir": 1, "single": 998, "rgb": 999, "other": 1000}
    if re.fullmatch(r"B\d+[A-Z0-9]*", token):
        try:
            return 100 + int(re.search(r"\d+", token).group(0))
        except Exception:
            return 199
    if re.fullmatch(r"HLS_B\d+[A-Z0-9]*", token):
        try:
            return 300 + int(re.search(r"\d+", token).group(0))
        except Exception:
            return 399
    if token == "OPERA_BWTR":
        return 400
    if token == "ECOSTRESS_WATER":
        return 410
    return order.get(token, 500)

def basemap_categories_from_paths(paths):
    seen = set()
    categories = []
    for path in list(paths or []):
        category = normalize_basemap_category(basemap_category_for_path(path))
        if not category:
            continue
        key = category.casefold()
        if key in seen:
            continue
        seen.add(key)
        categories.append(category)
    return sorted(categories, key=lambda item: (_basemap_category_sort_key(item), basemap_category_label(item).lower()))

def basemap_paths_for_category(paths, category):
    target = normalize_basemap_category(category)
    out = []
    for path in list(paths or []):
        if normalize_basemap_category(basemap_category_for_path(path)) == target:
            out.append(path)
    return _unique_sorted_paths(out)

def nearest_basemap_for_scene(scene_path, basemap_paths):
    candidates = list(basemap_paths or [])
    if not candidates:
        return None, None
    ranked = []
    for path in candidates:
        delta = basemap_delta_days_for_scene(scene_path, path)
        ranked.append((
            delta if delta is not None else 10**9,
            parse_basemap_acquisition_date(path) or "",
            os.path.basename(str(path)).lower(),
            path,
            delta,
        ))
    ranked.sort(key=lambda item: item[:4])
    best = ranked[0]
    return best[3], best[4]

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

APP_THEME_MODE = "dark"

def get_app_theme_mode():
    app = QtWidgets.QApplication.instance()
    if app is not None:
        mode = app.property("geoviewer_theme_mode")
        if isinstance(mode, str) and mode.lower() in ("light", "dark"):
            return mode.lower()
    return "light" if str(APP_THEME_MODE).lower() == "light" else "dark"

def build_theme_palette(mode=None):
    mode = (mode or get_app_theme_mode()).lower()
    palettes = {
        "dark": {
            "window_bg": "#000000",
            "panel_bg": "#101010",
            "figure_bg": "#000000",
            "axes_bg": "#000000",
            "group_bg": "#111111",
            "input_bg": "#111111",
            "list_bg": "#1A1A1A",
            "text": "#D3D3D3",
            "muted": "#B0B0B0",
            "heading": "#FFFFFF",
            "title": "#D3D3D3",
            "empty": "#555555",
            "button_bg": "#202020",
            "button_hover": "#333333",
            "button_pressed": "#3A3A3A",
            "button_text": "#D3D3D3",
            "border": "#3A3A3A",
            "disabled_text": "#999999",
            "selection_bg": "#2D5FFF",
            "selection_text": "#FFFFFF",
            "link": "#7DB3FF",
            "splash_logo_fill": "#FFFFFF",
            "splash_logo_outline": None,
            "splash_red": "#FF3B30",
            "help_text": "#C7F7C1",
        },
        "light": {
            "window_bg": "#E8E8E8",
            "panel_bg": "#FFFFFF",
            "figure_bg": "#F4F4F4",
            "axes_bg": "#FFFFFF",
            "group_bg": "#EFEFEF",
            "input_bg": "#FFFFFF",
            "list_bg": "#FFFFFF",
            "text": "#202020",
            "muted": "#505050",
            "heading": "#101010",
            "title": "#202020",
            "empty": "#808080",
            "button_bg": "#E7E7E7",
            "button_hover": "#DADADA",
            "button_pressed": "#CECECE",
            "button_text": "#202020",
            "border": "#909090",
            "disabled_text": "#777777",
            "selection_bg": "#2D5FFF",
            "selection_text": "#FFFFFF",
            "link": "#0B57D0",
            "splash_logo_fill": "#FFFFFF",
            "splash_logo_outline": "#000000",
            "splash_red": "#C51F1A",
            "help_text": "#245C2A",
        },
    }
    return palettes["light" if mode == "light" else "dark"]

def build_app_stylesheet(mode=None):
    pal = build_theme_palette(mode)
    return f"""
    QWidget {{
        color: {pal['text']};
    }}
    QMainWindow, QDialog, QMessageBox {{
        background-color: {pal['window_bg']};
        color: {pal['text']};
    }}
    QLabel {{
        color: {pal['text']};
        background: transparent;
    }}
    QPushButton {{
        background-color: {pal['button_bg']};
        color: {pal['button_text']};
        border: 1px solid {pal['border']};
        border-radius: 8px;
        padding: 6px 12px;
    }}
    QPushButton:hover {{
        background-color: {pal['button_hover']};
    }}
    QPushButton:pressed {{
        background-color: {pal['button_pressed']};
    }}
    QMenuBar {{
        background-color: transparent;
        border: 0px;
        spacing: 6px;
        font-size: 11pt;
    }}
    QMenuBar::item {{
        background-color: transparent;
        color: {pal['text']};
        padding: 4px 10px;
        margin: 2px 4px;
        min-height: 22px;
    }}
    QMenuBar::item:selected {{
        background-color: transparent;
        color: {pal['text']};
        font-weight: 700;
    }}
    QMenuBar::item:pressed {{
        background-color: transparent;
        color: {pal['text']};
        font-weight: 700;
    }}
    QMenu {{
        background-color: {pal['panel_bg']};
        color: {pal['text']};
        border: 1px solid {pal['border']};
        padding: 0px;
        font-size: 11pt;
    }}
    QMenu::item {{
        background-color: transparent;
        color: {pal['text']};
        padding: 1px 8px 1px 6px;
        min-height: 0px;
    }}
    QMenu::item:selected {{
        background-color: {pal['button_hover']};
        color: {pal['text']};
    }}
    QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QComboBox, QListWidget, QListView {{
        background-color: {pal['input_bg']};
        color: {pal['text']};
        border: 1px solid {pal['border']};
        border-radius: 6px;
        padding: 4px 8px;
        selection-background-color: {pal['selection_bg']};
        selection-color: {pal['selection_text']};
    }}
    QComboBox:disabled, QSpinBox:disabled, QLineEdit:disabled {{
        color: {pal['disabled_text']};
    }}
    QComboBox QAbstractItemView, QListWidget, QListView {{
        background-color: {pal['list_bg']};
        color: {pal['text']};
        selection-background-color: {pal['selection_bg']};
        selection-color: {pal['selection_text']};
        border: 1px solid {pal['border']};
    }}
    QGroupBox {{
        color: {pal['text']};
        font-weight: 600;
        border: 1px solid {pal['border']};
        border-radius: 8px;
        margin-top: 12px;
        padding: 10px 10px 8px 10px;
        background-color: {pal['group_bg']};
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 10px;
        top: 0px;
        padding: 0 4px;
        background-color: {pal['window_bg']};
        color: {pal['heading']};
    }}
    """

KEEP_REJECT_BUTTON_COLOR_OPTIONS = [
    ("Forest Green", "#2E8B57"),
    ("Brick Red", "#C23B22"),
    ("Vermillion", "#D55E00"),
    ("Blue", "#0072B2"),
    ("Orange", "#E69F00"),
    ("Bluish Green", "#009E73"),
    ("Sky Blue", "#56B4E9"),
    ("Yellow", "#F0E442"),
    ("Reddish Purple", "#CC79A7"),
    ("Slate", "#4E79A7"),
    ("Gray", "#6C757D"),
]

KEEP_REJECT_BUTTON_PRESETS = [
    {"id": "standard", "label": "True-Trit (Green / Red)", "keep": "#2E8B57", "reject": "#C23B22"},
    {"id": "blue_orange", "label": "All (Blue / Orange)", "keep": "#0072B2", "reject": "#E69F00"},
    {"id": "teal_magenta", "label": "True-Trit (Teal / Magenta)", "keep": "#009E73", "reject": "#CC79A7"},
    {"id": "sky_vermillion", "label": "All (Sky Blue / Vermillion)", "keep": "#56B4E9", "reject": "#D55E00"},
]

CUSTOM_KEEP_REJECT_PRESET_ID = "custom"
DEFAULT_KEEP_REJECT_PRESET_ID = "standard"
KEEP_REJECT_BUTTON_PRESET_BY_ID = {
    item["id"]: dict(item) for item in KEEP_REJECT_BUTTON_PRESETS
}
DEFAULT_WARP_SOURCE_COLOR = KEEP_REJECT_BUTTON_PRESET_BY_ID[DEFAULT_KEEP_REJECT_PRESET_ID]["reject"]
DEFAULT_WARP_TARGET_COLOR = KEEP_REJECT_BUTTON_PRESET_BY_ID[DEFAULT_KEEP_REJECT_PRESET_ID]["keep"]

DEFAULT_PANEL_LAYOUT_SETTINGS = {
    "left": 0.010,
    "right": 0.99,
    "top": 0.96,
    "bottom": 0.03,
    "wspace": 0.035,
    "hspace": 0.1,
}
DEFAULT_KEEP_REJECT_BUTTON_LAYOUT_SETTINGS = {
    "size_scale": 1.0,
    "spacing_px": 18.0,
}
_PANEL_LAYOUT_BOUNDS = {
    "left": (0.0, 1.0),
    "right": (0.0, 1.0),
    "top": (0.0, 1.0),
    "bottom": (0.0, 1.0),
    "wspace": (0.0, 1.5),
    "hspace": (0.0, 1.5),
}
_KEEP_REJECT_BUTTON_LAYOUT_BOUNDS = {
    "size_scale": (0.5, 2.0),
    "spacing_px": (0.0, 80.0),
}
_PANEL_LAYOUT_MIN_SPAN = 0.05
_KEEP_REJECT_BUTTON_BASE_FONT_PT = 14.0
_KEEP_REJECT_BUTTON_BASE_PAD_X_PX = 22.0
_KEEP_REJECT_BUTTON_BASE_PAD_Y_PX = 12.0

def normalize_panel_layout_settings(settings=None):
    src = dict(settings or {}) if isinstance(settings, dict) else {}
    out = {}

    for key, fallback in DEFAULT_PANEL_LAYOUT_SETTINGS.items():
        try:
            value = float(src.get(key, fallback))
        except Exception:
            value = fallback
        min_val, max_val = _PANEL_LAYOUT_BOUNDS.get(key, (0.0, 1.0))
        out[key] = max(min_val, min(max_val, value))

    if out["right"] <= out["left"] + _PANEL_LAYOUT_MIN_SPAN:
        out["right"] = min(1.0, out["left"] + _PANEL_LAYOUT_MIN_SPAN)
        out["left"] = min(out["left"], max(0.0, out["right"] - _PANEL_LAYOUT_MIN_SPAN))

    if out["top"] <= out["bottom"] + _PANEL_LAYOUT_MIN_SPAN:
        out["top"] = min(1.0, out["bottom"] + _PANEL_LAYOUT_MIN_SPAN)
        out["bottom"] = min(out["bottom"], max(0.0, out["top"] - _PANEL_LAYOUT_MIN_SPAN))

    return out

def format_panel_layout_settings_summary(settings):
    vals = normalize_panel_layout_settings(settings)
    order = ("left", "right", "top", "bottom", "wspace", "hspace")
    return ", ".join(f"{key}={vals[key]:.3f}" for key in order)

def normalize_keep_reject_button_layout_settings(settings=None):
    src = dict(settings or {}) if isinstance(settings, dict) else {}
    out = {}

    for key, fallback in DEFAULT_KEEP_REJECT_BUTTON_LAYOUT_SETTINGS.items():
        try:
            value = float(src.get(key, fallback))
        except Exception:
            value = fallback
        min_val, max_val = _KEEP_REJECT_BUTTON_LAYOUT_BOUNDS.get(key, (0.0, 1.0))
        out[key] = max(min_val, min(max_val, value))

    return out

def keep_reject_button_layout_metrics(settings=None):
    vals = normalize_keep_reject_button_layout_settings(settings)
    size_scale = vals["size_scale"]
    return {
        "font_pt": _KEEP_REJECT_BUTTON_BASE_FONT_PT * size_scale,
        "pad_x_px": _KEEP_REJECT_BUTTON_BASE_PAD_X_PX * size_scale,
        "pad_y_px": _KEEP_REJECT_BUTTON_BASE_PAD_Y_PX * size_scale,
        "gap_px": vals["spacing_px"],
    }

def qt_font_metrics_text_width(metrics, text):
    try:
        return float(metrics.horizontalAdvance(text))
    except Exception:
        try:
            return float(metrics.width(text))
        except Exception:
            return float(len(str(text)) * 8)

def normalize_keep_reject_button_color(color, fallback="#6C757D"):
    try:
        return mcolors.to_hex(mcolors.to_rgb(color), keep_alpha=False)
    except Exception:
        return mcolors.to_hex(mcolors.to_rgb(fallback), keep_alpha=False)

def _mix_keep_reject_button_colors(color_a, color_b, amount):
    amount = max(0.0, min(1.0, float(amount)))
    rgb_a = np.array(mcolors.to_rgb(normalize_keep_reject_button_color(color_a, "#6C757D")), dtype=float)
    rgb_b = np.array(mcolors.to_rgb(normalize_keep_reject_button_color(color_b, "#6C757D")), dtype=float)
    mixed = rgb_a * (1.0 - amount) + rgb_b * amount
    return mcolors.to_hex(np.clip(mixed, 0.0, 1.0), keep_alpha=False)

def keep_reject_button_text_color(base_color):
    r, g, b = mcolors.to_rgb(normalize_keep_reject_button_color(base_color, "#6C757D"))
    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "#101010" if luminance >= 0.62 else "#FFFFFF"

def build_keep_reject_button_style(base_color):
    base = normalize_keep_reject_button_color(base_color, "#6C757D")
    text = keep_reject_button_text_color(base)
    if text == "#FFFFFF":
        hover = _mix_keep_reject_button_colors(base, "#FFFFFF", 0.18)
        edge = _mix_keep_reject_button_colors(base, "#000000", 0.38)
    else:
        hover = _mix_keep_reject_button_colors(base, "#000000", 0.10)
        edge = _mix_keep_reject_button_colors(base, "#000000", 0.28)
    return {"base": base, "hover": hover, "edge": edge, "text": text}

def infer_keep_reject_button_preset(keep_color, reject_color):
    keep_norm = normalize_keep_reject_button_color(
        keep_color,
        KEEP_REJECT_BUTTON_PRESET_BY_ID[DEFAULT_KEEP_REJECT_PRESET_ID]["keep"],
    )
    reject_norm = normalize_keep_reject_button_color(
        reject_color,
        KEEP_REJECT_BUTTON_PRESET_BY_ID[DEFAULT_KEEP_REJECT_PRESET_ID]["reject"],
    )
    for preset in KEEP_REJECT_BUTTON_PRESETS:
        if (
            normalize_keep_reject_button_color(preset["keep"], preset["keep"]) == keep_norm
            and normalize_keep_reject_button_color(preset["reject"], preset["reject"]) == reject_norm
        ):
            return preset["id"]
    return CUSTOM_KEEP_REJECT_PRESET_ID

def set_app_theme_mode(mode):
    global APP_THEME_MODE
    mode = "light" if str(mode).lower() == "light" else "dark"
    APP_THEME_MODE = mode
    app = QtWidgets.QApplication.instance()
    if app is not None:
        app.setProperty("geoviewer_theme_mode", mode)
        app.setStyleSheet(build_app_stylesheet(mode))
    return build_theme_palette(mode)

class OutlinedLabel(QtWidgets.QLabel):
    def __init__(self, text="", parent=None, fill_color="#FFFFFF", outline_color=None, outline_width=2.0):
        super().__init__(text, parent)
        self._fill_color = QtGui.QColor(fill_color)
        self._outline_color = QtGui.QColor(outline_color) if outline_color else None
        self._outline_width = float(outline_width)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)

    def set_outline_style(self, fill_color=None, outline_color=None, outline_width=None):
        if fill_color is not None:
            self._fill_color = QtGui.QColor(fill_color)
        self._outline_color = QtGui.QColor(outline_color) if outline_color else None
        if outline_width is not None:
            self._outline_width = float(outline_width)
        self.update()

    def paintEvent(self, event):
        text = self.text()
        if not text:
            return super().paintEvent(event)

        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setRenderHint(QtGui.QPainter.TextAntialiasing)

        fm = QtGui.QFontMetricsF(self.font())
        br = fm.boundingRect(text)
        x = (self.width() - br.width()) / 2.0 - br.left()
        y = (self.height() - br.height()) / 2.0 + fm.ascent()

        path = QtGui.QPainterPath()
        path.addText(x, y, self.font(), text)

        if self._outline_color is not None and self._outline_width > 0:
            pen = QtGui.QPen(
                self._outline_color, self._outline_width,
                QtCore.Qt.SolidLine, QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin
            )
            painter.setPen(pen)
            painter.drawPath(path)

        painter.fillPath(path, QtGui.QBrush(self._fill_color))


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

def validate_user_datetime_pattern(substring_text, pattern_text):
    from datetime import datetime

    substring = normalize_filename_datetime_substring(substring_text)
    pattern = normalize_filename_datetime_pattern(pattern_text)

    if not substring and not pattern:
        return None, None, None
    if not substring or not pattern:
        return None, None, "Please provide both the datetime substring and the matching pattern, or leave both blank."

    try:
        rx, sp = _userpattern_to_regex_and_strptime(pattern)
    except Exception as e:
        return None, None, f"Could not interpret the pattern: {e}"

    if rx.fullmatch(substring) is None:
        return None, None, "The pattern does not match the datetime substring. Include every digit and literal such as 'T', '_' or '-'."

    try:
        _ = datetime.strptime(substring, sp)
    except Exception as e:
        return None, None, f"Python could not parse the datetime substring with that pattern: {e}"

    return rx, sp, None

def apply_user_datetime_pattern_settings(substring_text, pattern_text):
    global USER_DT_REGEX, USER_DT_STRPTIME
    USER_DT_REGEX = None
    USER_DT_STRPTIME = None

    rx, sp, err = validate_user_datetime_pattern(substring_text, pattern_text)
    if err:
        return err

    if rx is not None and sp is not None:
        USER_DT_REGEX = rx
        USER_DT_STRPTIME = sp
    return None

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
        self.theme_mode = get_app_theme_mode()
        self.theme = build_theme_palette(self.theme_mode)
        self.setStyleSheet(build_app_stylesheet(self.theme_mode))

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
        ex.setStyleSheet(f"color: {self.theme['heading']};")
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
        ok.clicked.connect(self._on_ok)
        cancel = QtWidgets.QPushButton("Cancel")
        cancel.setFont(body_font)
        cancel.clicked.connect(self.reject)
        row.addWidget(ok); row.addWidget(cancel)
        lay.addLayout(row)

    def _on_ok(self):
        sub = (self.subedit.text() or "").strip()
        pat = (self.fmts.text() or "").strip()
        rx, sp, err = validate_user_datetime_pattern(sub, pat)
        if err:
            QtWidgets.QMessageBox.critical(self, "Filename Pattern", err)
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
    import os
    from datetime import datetime

    base = os.path.basename(fname)

    global USER_DT_REGEX, USER_DT_STRPTIME
    if USER_DT_REGEX and USER_DT_STRPTIME:
        m = USER_DT_REGEX.search(base)
        if m:
            try:
                dt = datetime.strptime(m.group(0), USER_DT_STRPTIME)
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass

    auto_dt = auto_detect_datetime_from_filename(base)
    if auto_dt is not None:
        return auto_dt

    return base.split('_')[0]

def auto_detect_datetime_from_filename(fname: str):
    import os, re
    from datetime import datetime

    base = os.path.basename(fname)

    m = re.search(r'(\d{8}T\d{6})', base)
    if m:
        try:
            dt = datetime.strptime(m.group(1), "%Y%m%dT%H%M%S")
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass

    m = re.search(r'(\d{14})', base)
    if m:
        try:
            dt = datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass

    m = re.search(r'(\d{4}\d{3}\d{6})', base)
    if m:
        try:
            dt = datetime.strptime(m.group(1), "%Y%j%H%M%S")
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass

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

    return None

def _clean_log_comment(value):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()[:LOG_COMMENT_MAX_CHARS]

def normalize_comment_flag(value):
    text = str(value or "")
    text = text.replace("[", " ").replace("]", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:COMMENT_FLAG_MAX_CHARS]

def _comment_flag_key(value):
    return normalize_comment_flag(value).casefold()

def normalize_comment_flags(flags):
    seen = set()
    out = []
    for raw in list(flags or []):
        flag = normalize_comment_flag(raw)
        if not flag:
            continue
        key = _comment_flag_key(flag)
        if key in seen:
            continue
        seen.add(key)
        out.append(flag)
    return out

def load_user_comment_flags():
    try:
        data = json.loads(USER_COMMENT_FLAGS_JSON)
    except Exception:
        data = []
    if isinstance(data, dict):
        data = data.get("flags", [])
    if not isinstance(data, list):
        data = []
    return normalize_comment_flags(data)

def all_comment_flags(user_flags=None):
    if user_flags is None:
        user_flags = load_user_comment_flags()
    user_flags = normalize_comment_flags(user_flags)
    if user_flags:
        return user_flags
    return normalize_comment_flags(DEFAULT_COMMENT_FLAGS)

def _comment_flag_token(flag):
    flag = normalize_comment_flag(flag)
    return f"[{flag}]" if flag else ""

def _comment_flags_prefix(flags):
    tokens = [_comment_flag_token(flag) for flag in normalize_comment_flags(flags)]
    return " ".join(token for token in tokens if token)

def split_comment_flags(comment):
    text = _clean_log_comment(comment)
    flags = []
    pos = 0
    pattern = r"\s*\[([^\[\]\r\n]{1,%d})\]" % COMMENT_FLAG_MAX_CHARS
    while pos < len(text):
        match = re.match(pattern, text[pos:])
        if not match:
            break
        flag = normalize_comment_flag(match.group(1))
        if not flag:
            break
        flags.append(flag)
        pos += match.end()
    return normalize_comment_flags(flags), text[pos:].strip()

def compose_comment(flags, text):
    prefix = _comment_flags_prefix(flags)
    body = re.sub(r"\s+", " ", str(text or "")).strip()
    if prefix and body:
        return _clean_log_comment(f"{prefix} {body}")
    return _clean_log_comment(prefix or body)

def comment_text_char_budget(flags):
    prefix = _comment_flags_prefix(flags)
    spacer = 1 if prefix else 0
    return max(0, LOG_COMMENT_MAX_CHARS - len(prefix) - spacer)

def _comment_from_log_row(row, lower):
    try:
        idx = lower.index(LOG_COMMENT_COL)
    except ValueError:
        return ""
    if idx < len(row):
        return _clean_log_comment(row[idx])
    return ""

def _clean_log_logged_datetime(value):
    return str(value if value is not None else "").strip()

def _clean_log_image_datetime(value):
    text = str(value if value is not None else "").strip()
    marker = "__OUTPUT_ALIAS__::"
    if marker in text:
        text = text.split(marker, 1)[0].strip()
    return text

def _current_log_logged_datetime():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

def _log_column_value(row, lower, column_name, default=""):
    key = str(column_name or "").strip().lower()
    try:
        idx = lower.index(key)
    except ValueError:
        return default
    return _log_row_value(row, idx, default)

def _log_row_value(row, idx, default=""):
    return row[idx] if idx < len(row) else default

def _log_float_or_none(value):
    text = str(value if value is not None else "").strip()
    if text == "":
        return None
    return float(text)

def _log_warp_values(row, start_idx):
    return [_log_float_or_none(_log_row_value(row, start_idx + i)) for i in range(6)]

def _log_int_or_blank(value):
    text = str(value if value is not None else "").strip()
    if text == "":
        return ""
    try:
        return str(int(round(float(text))))
    except Exception:
        return text

def _blank_source_grid_metadata():
    return {
        "crs": "",
        "width": "",
        "height": "",
        "transform": [None] * 6,
    }

def _normalize_source_grid_metadata(value=None):
    out = _blank_source_grid_metadata()
    if not isinstance(value, dict):
        return out
    out["crs"] = str(value.get("crs") or "").strip()
    out["width"] = _log_int_or_blank(value.get("width"))
    out["height"] = _log_int_or_blank(value.get("height"))
    vals = list(value.get("transform") or [])
    if len(vals) < 6:
        vals = vals + [None] * (6 - len(vals))
    out["transform"] = [
        _log_float_or_none(vals[i]) if vals[i] is not None else None
        for i in range(6)
    ]
    return out

def _source_grid_metadata_from_log_row(row, lower):
    source = _blank_source_grid_metadata()
    source["crs"] = _log_column_value(row, lower, LOG_SOURCE_CRS_COL, "")
    source["width"] = _log_column_value(row, lower, LOG_SOURCE_WIDTH_COL, "")
    source["height"] = _log_column_value(row, lower, LOG_SOURCE_HEIGHT_COL, "")
    if all(c in lower for c in SOURCE_GRID_COLS):
        start_idx = lower.index(SOURCE_GRID_COLS[0])
        source["transform"] = _log_warp_values(row, start_idx)
    return _normalize_source_grid_metadata(source)

def _source_crs_text(crs_obj):
    if not crs_obj:
        return ""
    try:
        epsg = crs_obj.to_epsg()
        if epsg:
            return f"EPSG:{int(epsg)}"
    except Exception:
        pass
    try:
        return str(crs_obj.to_string() or "").strip()
    except Exception:
        pass
    try:
        return str(crs_obj.to_wkt() or "").strip()
    except Exception:
        return str(crs_obj or "").strip()

def _source_grid_metadata_from_path(path):
    path = str(path or "").strip()
    if not path or not os.path.exists(path):
        return _blank_source_grid_metadata()
    try:
        with rasterio.open(path) as src:
            transform = src.transform
            return _normalize_source_grid_metadata({
                "crs": _source_crs_text(src.crs),
                "width": src.width,
                "height": src.height,
                "transform": [
                    transform.a, transform.b, transform.c,
                    transform.d, transform.e, transform.f,
                ],
            })
    except Exception:
        return _blank_source_grid_metadata()

def _source_grid_metadata_from_entry(entry):
    if len(entry) > 10 and isinstance(entry[10], dict):
        return _normalize_source_grid_metadata(entry[10])
    return _blank_source_grid_metadata()

def _format_source_grid_log_values(source_grid):
    source = _normalize_source_grid_metadata(source_grid)
    vals = list(source.get("transform") or [])
    if len(vals) < 6:
        vals = vals + [None] * (6 - len(vals))
    return [
        source.get("crs", ""),
        source.get("width", ""),
        source.get("height", ""),
    ] + [
        "" if vals[i] is None else _format_log_float(vals[i], ".12g")
        for i in range(6)
    ]

def _source_crs_label_token(source_grid):
    source = _normalize_source_grid_metadata(source_grid)
    crs_text = str(source.get("crs") or "").strip()
    if not crs_text:
        return "", None
    try:
        crs = CRS.from_string(crs_text)
        label = _reproject_crs_label(crs) if crs else crs_text
        token = crs.to_wkt() if crs else crs_text
        return label, token
    except Exception:
        return crs_text, crs_text

def _source_grid_transform(source_grid):
    source = _normalize_source_grid_metadata(source_grid)
    vals = list(source.get("transform") or [])
    if len(vals) < 6 or any(v is None for v in vals[:6]):
        return None
    try:
        return Affine(vals[0], vals[1], vals[2], vals[3], vals[4], vals[5])
    except Exception:
        return None

def _infer_warp_flag_from_vals(vals):
    if not vals or all(v is None for v in vals):
        return False
    numeric = [float(v) if v is not None else 0.0 for v in vals]
    eps = 1e-12
    is_all_zero = all(abs(v) < eps for v in numeric)
    is_identity = (len(numeric) == 6 and
                   abs(numeric[0]-1) < eps and abs(numeric[1]) < eps and abs(numeric[2]) < eps and
                   abs(numeric[3]) < eps  and abs(numeric[4]-1) < eps and abs(numeric[5]) < eps)
    return (not is_all_zero) and (not is_identity)

def _is_blank_log_value(value):
    return value is None or str(value).strip() == ""

def _is_original_copy_comment(comment):
    return _clean_log_comment(comment).casefold() == ORIGINAL_COPY_COMMENT.casefold()

def _unpack_log_entry(entry):
    fname, dt, az, dist, wf, vals = entry[:6]
    if len(entry) > 9:
        comment = entry[9]
    elif len(entry) > 8:
        comment = entry[8]
    else:
        comment = entry[6] if len(entry) > 6 else ""
    return fname, dt, az, dist, wf, vals, _clean_log_comment(comment)

def _unpack_log_entry_with_basemap(entry):
    fname, dt, az, dist, wf, vals = entry[:6]
    if len(entry) > 9:
        basemap_filename = _clean_log_comment(entry[6])
        basemap_delta_days = entry[7]
        logged_datetime = _clean_log_logged_datetime(entry[8])
        comment = entry[9]
    elif len(entry) > 8:
        basemap_filename = _clean_log_comment(entry[6])
        basemap_delta_days = entry[7]
        logged_datetime = ""
        comment = entry[8]
    else:
        basemap_filename = ""
        basemap_delta_days = ""
        logged_datetime = ""
        comment = entry[6] if len(entry) > 6 else ""
    return (
        fname, dt, az, dist, wf, vals,
        basemap_filename, basemap_delta_days,
        logged_datetime, _clean_log_comment(comment),
    )

def _is_original_copy_log_entry(entry):
    try:
        _, _, az, dist, _, _, comment = _unpack_log_entry(entry)
    except Exception:
        return False
    return (
        _is_original_copy_comment(comment)
        and _is_blank_log_value(az)
        and _is_blank_log_value(dist)
    )

def _is_restructured_log_entry(entry):
    try:
        _, _, _az, _dist, _wf, _vals, comment = _unpack_log_entry(entry)
    except Exception:
        return False
    return _clean_log_comment(comment).casefold() == STRIPES_RESTRUCTURED_COMMENT.casefold()

def _is_output_alias_log_entry(entry):
    try:
        _, dt, az, dist, _, _, _ = _unpack_log_entry(entry)
    except Exception:
        return False
    if _is_output_alias_datetime(dt):
        return True
    try:
        return float(az) == OUTPUT_ALIAS_AZIMUTH and float(dist) == OUTPUT_ALIAS_DISTANCE
    except Exception:
        return False

def _format_log_float(value, fmt, fallback=0.0):
    if _is_blank_log_value(value):
        value = fallback
    return format(float(value), fmt)

def _format_log_warp_values(vals):
    vals = list(vals or [])
    if len(vals) != 6:
        vals = [0] * 6
    return [_format_log_float(v, ".6f") for v in vals]

def _format_log_delta_days(value):
    text = str(value if value is not None else "").strip()
    if not text:
        return ""
    try:
        return str(int(round(float(text))))
    except Exception:
        return text

def read_log():
    processed_dts = {}
    processed_files = set()
    log_entries = []

    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, newline='') as f:
            reader = csv.reader(f)
            header = next(reader, [])
            lower  = [h.strip().lower() for h in header]

            has_fname     = (header and lower[0] == 'filename')
            has_warpcols  = all(c in lower for c in WARP_COLS)
            has_warpflag  = ('warped' in lower)  # old format

            for row in reader:
                comment = _comment_from_log_row(row, lower)
                logged_datetime = _log_column_value(row, lower, LOG_LOGGED_DATETIME_COL, "")
                basemap_filename = _log_column_value(row, lower, LOG_BASEMAP_FILENAME_COL, "")
                basemap_delta_days = _log_column_value(row, lower, LOG_BASEMAP_DELTA_DAYS_COL, "")
                source_grid = _source_grid_metadata_from_log_row(row, lower)
                if has_fname:
                    # filename-first logs
                    fname, dt = _log_row_value(row, 0), _log_row_value(row, 1)
                    az = _log_float_or_none(_log_row_value(row, 2))
                    dist = _log_float_or_none(_log_row_value(row, 3))

                    if has_warpflag:
                        # old: explicit warped flag, then 6 params
                        warp_flag = (_log_row_value(row, 4) == '1')
                        vals = _log_warp_values(row, 5) if has_warpcols else [0]*6
                    else:
                        # new: no 'warped' column — infer from params
                        vals = _log_warp_values(row, 4) if has_warpcols else [0]*6
                        warp_flag = _infer_warp_flag_from_vals(vals)
                else:
                    # very old: datetime-first logs
                    dt = _log_row_value(row, 0)
                    az = _log_float_or_none(_log_row_value(row, 1))
                    dist = _log_float_or_none(_log_row_value(row, 2))

                    if has_warpflag:
                        warp_flag = (_log_row_value(row, 3) == '1')
                        vals = _log_warp_values(row, 4) if has_warpcols else [0]*6
                    else:
                        vals = _log_warp_values(row, 3) if has_warpcols else [0]*6
                        warp_flag = _infer_warp_flag_from_vals(vals)
                    fname = None

                entry = (
                    fname, dt, az, dist, warp_flag, vals,
                    basemap_filename, basemap_delta_days,
                    logged_datetime, comment, source_grid,
                )
                if not _is_original_copy_log_entry(entry) and not _is_output_alias_log_entry(entry):
                    processed_dts[dt] = (az, dist, warp_flag, vals, comment)
                if fname:
                    processed_files.add(fname)
                log_entries.append(entry)
    return processed_dts, processed_files, log_entries

def write_log(log_entries):
    with open(LOG_FILE, 'w', newline='') as f:
        w = csv.writer(f)
        # NEW: drop the 'warped' column from disk; keep only the params
        w.writerow(['filename','datetime','azimuth_deg','distance_m'] + WARP_COLS +
                   [LOG_BASEMAP_FILENAME_COL, LOG_BASEMAP_DELTA_DAYS_COL,
                    LOG_LOGGED_DATETIME_COL, LOG_COMMENT_COL,
                    LOG_SOURCE_CRS_COL, LOG_SOURCE_WIDTH_COL, LOG_SOURCE_HEIGHT_COL] +
                   SOURCE_GRID_COLS)
        for entry in log_entries:
            (
                fname, dt, az, dist, wf, vals,
                basemap_filename, basemap_delta_days,
                logged_datetime, comment,
            ) = _unpack_log_entry_with_basemap(entry)
            dt = _clean_log_image_datetime(dt)
            logged_datetime = _clean_log_logged_datetime(logged_datetime) or _current_log_logged_datetime()
            source_grid = _source_grid_metadata_from_entry(entry)
            if not source_grid.get("crs") and all(v is None for v in source_grid.get("transform", [])):
                source_grid = _source_grid_metadata_from_path(
                    _resolve_logged_file_path(fname, log_path=LOG_FILE)
                )
            source_grid_values = _format_source_grid_log_values(source_grid)
            if _is_original_copy_log_entry(entry):
                w.writerow([fname, dt, "", ""] + [""] * 6 +
                           [basemap_filename, _format_log_delta_days(basemap_delta_days),
                            logged_datetime, ORIGINAL_COPY_COMMENT] +
                           source_grid_values)
                continue
            if _is_restructured_log_entry(entry):
                w.writerow([fname, dt, "", ""] + [""] * 6 +
                           [basemap_filename, _format_log_delta_days(basemap_delta_days),
                            logged_datetime, STRIPES_RESTRUCTURED_COMMENT] +
                           source_grid_values)
                continue
            w.writerow([fname, dt, _format_log_float(az, ".6f"), _format_log_float(dist, ".2f")] +
                       _format_log_warp_values(vals) +
                       [basemap_filename, _format_log_delta_days(basemap_delta_days),
                        logged_datetime, comment] +
                       source_grid_values)

def summarize_log_entries(log_entries):
    counts = {'processed': 0, 'as_is': 0, 'geo': 0, 'warp': 0, 'reject': 0}
    for entry in log_entries:
        if _is_original_copy_log_entry(entry) or _is_output_alias_log_entry(entry):
            continue
        _, _, az, dist, wf, _, _ = _unpack_log_entry(entry)
        counts['processed'] += 1
        if az == REJECT_AZIMUTH and dist == REJECT_DISTANCE:
            counts['reject'] += 1
        elif _is_restructured_log_entry(entry):
            counts['geo'] += 1
        else:
            try:
                dist_value = float(dist or 0.0)
            except Exception:
                dist_value = 0.0
            has_geo = (dist_value > 0)
            has_warp = bool(wf)
            if has_geo:
                counts['geo'] += 1
            if has_warp:
                counts['warp'] += 1
            if not has_geo and not has_warp:
                counts['as_is'] += 1
    return counts

def _format_file_size(num_bytes):
    try:
        size = float(num_bytes)
    except Exception:
        return "unavailable"
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"

def _short_error_text(exc, max_chars=120):
    text = re.sub(r"\s+", " ", str(exc or "")).strip()
    if len(text) > max_chars:
        text = text[:max_chars - 3].rstrip() + "..."
    return text or "unknown error"

def _is_output_alias_datetime(dt):
    return "__OUTPUT_ALIAS__::" in str(dt or "")

def _is_reject_log_values(az, dist):
    try:
        return float(az) == REJECT_AZIMUTH and float(dist) == REJECT_DISTANCE
    except Exception:
        return False

def _log_operation_label(az, dist, warp_flag):
    try:
        az = float(az)
        dist = float(dist)
    except Exception:
        return "Unreadable log row"
    if az == OUTPUT_ALIAS_AZIMUTH and dist == OUTPUT_ALIAS_DISTANCE:
        return "Output alias"
    if _is_reject_log_values(az, dist):
        return "Rejected"
    parts = []
    if dist > 0:
        parts.append("Transform")
    if bool(warp_flag):
        parts.append("Warp")
    return " + ".join(parts) if parts else "As-is"

def _json_safe_metadata(value):
    if isinstance(value, dict):
        return {str(k): _json_safe_metadata(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_metadata(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)

def _format_metadata_block(title, data):
    lines = [str(title)]
    if isinstance(data, dict) and data:
        lines.append(json.dumps(_json_safe_metadata(data), indent=2, sort_keys=True))
    elif isinstance(data, (list, tuple)) and data:
        lines.append(json.dumps(_json_safe_metadata(list(data)), indent=2, sort_keys=True))
    else:
        lines.append("(none)")
    return "\n".join(lines)

def _current_file_lookup_by_name(all_files):
    lookup = {}
    for path in list(all_files or []):
        fname = os.path.basename(str(path or ""))
        if fname and fname not in lookup:
            lookup[fname] = os.path.abspath(path)
    return lookup

def _resolve_logged_file_path(fname, file_by_name=None, log_path=None):
    fname = str(fname or "").strip()
    if not fname:
        return ""
    if os.path.isabs(fname) and os.path.exists(fname):
        return os.path.abspath(fname)
    file_by_name = dict(file_by_name or {})
    if fname in file_by_name:
        return os.path.abspath(file_by_name[fname])
    candidates = []
    if log_path:
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(log_path)), fname))
    candidates.append(os.path.abspath(fname))
    for cand in candidates:
        if cand and os.path.exists(cand):
            return os.path.abspath(cand)
    return ""

def _raster_crs_info_for_log_inventory(path, cache=None):
    abs_path = os.path.abspath(str(path or ""))
    if cache is not None and abs_path in cache:
        return cache[abs_path]
    try:
        with rasterio.open(abs_path) as src:
            if src.crs:
                label = _reproject_crs_label(src.crs)
                try:
                    token = src.crs.to_wkt()
                except Exception:
                    token = str(src.crs)
            else:
                label = "None"
                token = "__GEOVIEWER_NO_CRS__"
    except Exception as exc:
        label = f"Unreadable: {_short_error_text(exc)}"
        token = None
    result = (label, token)
    if cache is not None:
        cache[abs_path] = result
    return result

def _crs_tokens_match(left, right):
    return bool(left is not None and right is not None and str(left) == str(right))

def _is_epsg_4326_label(label):
    return str(label or "").strip().upper() == "EPSG:4326"

def _log_entries_by_datetime(log_entries, file_by_name=None, log_path=None, crs_cache=None):
    by_dt = {}
    for entry in list(log_entries or []):
        try:
            fname, dt, az, dist, warp_flag, vals, comment = _unpack_log_entry(entry)
        except Exception:
            continue
        if not dt or _is_output_alias_log_entry(entry) or _is_original_copy_log_entry(entry):
            continue
        path = _resolve_logged_file_path(fname, file_by_name, log_path)
        source_grid = _source_grid_metadata_from_entry(entry)
        if path:
            crs_label, crs_token = _raster_crs_info_for_log_inventory(path, crs_cache)
            if not source_grid.get("crs") and all(v is None for v in source_grid.get("transform", [])):
                source_grid = _source_grid_metadata_from_path(path)
        else:
            crs_label, crs_token = _source_crs_label_token(source_grid)
            if not crs_token:
                crs_label, crs_token = "Source file unavailable; using log values only", None
        rejected = _is_reject_log_values(az, dist)
        by_dt.setdefault(str(dt), []).append({
            "filename": fname or "",
            "path": path,
            "datetime": str(dt),
            "crs": crs_label,
            "crs_token": crs_token,
            "azimuth": az,
            "distance": dist,
            "warp_flag": bool(warp_flag),
            "vals": vals,
            "comment": comment,
            "source_grid": source_grid,
            "rejected": rejected,
            "operation": _log_operation_label(az, dist, warp_flag),
        })
    return by_dt

def _raster_crs_label_for_log_inventory(path):
    return _raster_crs_info_for_log_inventory(path)[0]

def _find_existing_log_match(dt, current_crs_token, by_dt):
    candidates = list(dict(by_dt or {}).get(str(dt or ""), []) or [])
    for candidate in candidates:
        if _crs_tokens_match(current_crs_token, candidate.get("crs_token")):
            return candidate
    if current_crs_token is None:
        return None
    unresolved_candidates = [
        candidate for candidate in candidates
        if candidate.get("crs_token") is None
    ]
    if not unresolved_candidates:
        return None
    # If the logged source TIFF is no longer present, the CRS cannot be checked
    # from that file. Fall back to the newest same-datetime log row and use the
    # stored translation/warp values directly.
    for candidate in reversed(unresolved_candidates):
        if bool(candidate.get("warp_flag")):
            return candidate
        try:
            if float(candidate.get("distance") or 0.0) > 0.0:
                return candidate
        except Exception:
            pass
    return unresolved_candidates[-1]

def _find_rejected_log_match(dt, by_dt):
    candidates = list(dict(by_dt or {}).get(str(dt or ""), []) or [])
    for candidate in candidates:
        if bool(candidate.get("rejected")):
            return candidate
    return None

def _format_existing_log_file_metadata(path):
    path = str(path or "").strip()
    if not path:
        return "No datetime-compatible log file is available for this row."
    abs_path = os.path.abspath(path)
    lines = [
        f"Path: {abs_path}",
        f"File name: {os.path.basename(abs_path)}",
    ]
    try:
        stat_info = os.stat(abs_path)
        lines.extend([
            f"File size: {_format_file_size(stat_info.st_size)}",
            f"Modified: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stat_info.st_mtime))}",
        ])
    except Exception as exc:
        lines.append(f"File stat error: {_short_error_text(exc)}")

    if not os.path.exists(abs_path):
        lines.append("File not found.")
        return "\n".join(lines)

    try:
        with rasterio.open(abs_path) as src:
            lines.extend([
                "",
                "Raster",
                f"Driver: {src.driver}",
                f"Width: {src.width}",
                f"Height: {src.height}",
                f"Band count: {src.count}",
                f"CRS: {_reproject_crs_label(src.crs) if src.crs else 'None'}",
                f"Transform: {src.transform}",
                f"Bounds: {src.bounds}",
                f"Resolution: {src.res}",
                f"Indexes: {list(src.indexes)}",
                f"Dtypes: {list(src.dtypes)}",
                f"Nodata values: {list(src.nodatavals)}",
                f"Descriptions: {list(src.descriptions or [])}",
                f"Units: {list(src.units or [])}",
                f"Scales: {list(src.scales or [])}",
                f"Offsets: {list(src.offsets or [])}",
                f"Color interpretation: {[str(item) for item in src.colorinterp]}",
                f"Block shapes: {list(src.block_shapes or [])}",
            ])
            try:
                lines.append(f"Subdatasets: {list(src.subdatasets or [])}")
            except Exception:
                pass
            if src.crs:
                try:
                    lines.extend(["", "CRS WKT:", src.crs.to_wkt()])
                except Exception:
                    pass
            lines.extend(["", _format_metadata_block("Profile:", src.profile)])
            lines.extend(["", _format_metadata_block("Dataset tags:", src.tags())])
            tag_namespaces = []
            try:
                tag_ns_fn = getattr(src, "tag_namespaces", None)
                tag_namespaces = list(tag_ns_fn() or []) if callable(tag_ns_fn) else []
            except Exception:
                tag_namespaces = []
            for ns in tag_namespaces:
                try:
                    lines.extend(["", _format_metadata_block(f"Dataset tags ({ns}):", src.tags(ns=ns))])
                except Exception:
                    pass
            for band_idx in src.indexes:
                try:
                    lines.extend(["", _format_metadata_block(f"Band {band_idx} tags:", src.tags(band_idx))])
                except Exception:
                    pass
                try:
                    mask_flags = [str(item) for item in src.mask_flag_enums[band_idx - 1]]
                    lines.append(f"Band {band_idx} mask flags: {mask_flags}")
                except Exception:
                    pass
    except Exception as exc:
        lines.extend(["", f"Raster metadata error: {_short_error_text(exc, max_chars=240)}"])
    return "\n".join(lines)

class ExistingLogMetadataDialog(QtWidgets.QDialog):
    """Two-panel metadata viewer for the selected current file and matched log file."""

    def __init__(self, row_data, theme_mode=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("File Metadata")
        self.setModal(True)
        self.resize(1120, 760)
        self.theme_mode = "light" if str(theme_mode or get_app_theme_mode()).lower() == "light" else "dark"
        self.theme = build_theme_palette(self.theme_mode)
        self.setStyleSheet(build_app_stylesheet(self.theme_mode) + f"""
        QPlainTextEdit {{
            background-color: {self.theme['input_bg']};
            color: {self.theme['text']};
            border: 1px solid {self.theme['border']};
            border-radius: 6px;
            padding: 8px;
            font-family: Lucida Console;
            font-size: 10pt;
        }}
        QSplitter::handle {{
            background-color: transparent;
        }}
        QSplitter::handle:horizontal {{
            margin-left: 4px;
            margin-right: 5px;
            background-color: {self.theme['border']};
        }}
        """)

        row_data = dict(row_data or {})
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal, self)
        splitter.setHandleWidth(10)
        layout.addWidget(splitter, 1)

        left_panel = self._metadata_panel(
            "  File name in folder",
            row_data.get("filename") or "",
            _format_existing_log_file_metadata(row_data.get("path")),
        )
        matched_metadata = _format_existing_log_file_metadata(row_data.get("matched_path"))
        if row_data.get("matched_filename") and not row_data.get("matched_path"):
            matched_metadata = (
                "Logged source file is not currently available.\n"
                "Auto-Geocorrect can still use the datetime, translation, and warp "
                "values stored in GeolocationLog.csv."
            )
        right_panel = self._metadata_panel(
            "  Matched log file",
            row_data.get("matched_filename") or "No datetime-compatible log file",
            matched_metadata,
        )
        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setSizes([1, 1])

        button_row = QtWidgets.QHBoxLayout()
        layout.addLayout(button_row)
        button_row.addStretch(1)
        self.close_btn = QtWidgets.QPushButton("Close", self)
        self.close_btn.setDefault(True)
        self.close_btn.setAutoDefault(True)
        self.close_btn.clicked.connect(self.accept)
        button_row.addWidget(self.close_btn)

        self.installEventFilter(self)
        for widget in self.findChildren(QtWidgets.QWidget):
            widget.installEventFilter(self)

    def _metadata_panel(self, label, filename, metadata_text):
        panel = QtWidgets.QWidget(self)
        lay = QtWidgets.QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        title = QtWidgets.QLabel(f"{label}: {filename}", panel)
        title.setStyleSheet(f"color: {self.theme['heading']}; font-weight: 700;")
        title.setWordWrap(True)
        text = QtWidgets.QPlainTextEdit(panel)
        text.setReadOnly(True)
        text.setPlainText(str(metadata_text or "No metadata available."))
        lay.addWidget(title)
        lay.addWidget(text, 1)
        return panel

    def eventFilter(self, obj, event):
        if event.type() == QtCore.QEvent.KeyPress and event.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
            self.accept()
            return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event):
        if event.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
            self.accept()
            return
        super().keyPressEvent(event)

def build_existing_log_inventory(all_files, processed_dts, processed_files, log_entries, log_path=None):
    log_path = os.path.abspath(log_path or LOG_FILE)
    try:
        log_size = os.path.getsize(log_path)
    except Exception:
        log_size = None

    processed_files = set(processed_files or [])
    processed_dts = dict(processed_dts or {})
    log_entries = list(log_entries or [])
    file_by_name = _current_file_lookup_by_name(all_files)
    crs_cache = {}
    by_dt = _log_entries_by_datetime(log_entries, file_by_name, log_path, crs_cache)
    rows = []

    for path in list(all_files or []):
        fname = os.path.basename(path)
        dt = str(parse_datetime_from_filename(path) or "").strip()
        recorded = fname in processed_files
        crs_label, crs_token = _raster_crs_info_for_log_inventory(path, crs_cache)
        datetime_candidates = list(by_dt.get(dt, []) or []) if dt else []
        rejected_match = _find_rejected_log_match(dt, by_dt) if dt else None
        reject_delete_candidate = bool((not recorded) and rejected_match)
        match = rejected_match if reject_delete_candidate else (_find_existing_log_match(dt, crs_token, by_dt) if dt else None)
        matched_operation = str((match or {}).get("operation") or "")
        auto_candidate = bool((not recorded) and match and not reject_delete_candidate)
        auto_datetime_match_only = bool(auto_candidate and matched_operation == "As-is")
        if reject_delete_candidate:
            auto_text = "No - Set for Deletion"
        elif auto_candidate:
            if auto_datetime_match_only:
                auto_text = "Yes - Preserved"
            else:
                operation_text = matched_operation or "Log match"
                if match.get("crs_token") is None:
                    operation_text = f"{operation_text} (log-only)"
                auto_text = f"Yes - {operation_text}"
        elif recorded:
            auto_text = "No - already recorded"
        elif datetime_candidates:
            auto_text = "No - matching date, incompatible CRS"
        else:
            auto_text = "No - no matching log date/compatible CRS"

        rows.append({
            "path": os.path.abspath(path),
            "filename": fname,
            "datetime": dt or "Unavailable",
            "crs": crs_label,
            "crs_is_4326": _is_epsg_4326_label(crs_label),
            "crs_token": crs_token,
            "recorded": bool(recorded),
            "status": "Rejected" if reject_delete_candidate else ("Recorded" if recorded else "Needs recording"),
            "auto_candidate": auto_candidate,
            "auto_delete_candidate": reject_delete_candidate,
            "auto_datetime_match_only": auto_datetime_match_only,
            "auto_text": auto_text,
            "matched_filename": str((match or {}).get("filename") or ""),
            "matched_path": str((match or {}).get("path") or ""),
            "matched_crs": str((match or {}).get("crs") or ""),
            "matched_operation": matched_operation,
        })

    recorded_count = sum(1 for row in rows if row["recorded"])
    auto_transform_count = sum(1 for row in rows if row["auto_candidate"])
    auto_delete_count = sum(1 for row in rows if row.get("auto_delete_candidate"))
    auto_count = auto_transform_count + auto_delete_count
    non_4326_crs_count = sum(1 for row in rows if not row["crs_is_4326"])
    real_log_rows = sum(
        1
        for entry in log_entries
        if not _is_output_alias_log_entry(entry)
    )
    return {
        "log_path": log_path,
        "log_size": log_size,
        "log_size_text": _format_file_size(log_size) if log_size is not None else "unavailable",
        "log_rows": real_log_rows,
        "raw_log_rows": len(log_entries),
        "current_files": len(rows),
        "recorded_files": recorded_count,
        "need_recording_files": max(0, len(rows) - recorded_count),
        "auto_candidate_files": auto_count,
        "auto_transform_candidate_files": auto_transform_count,
        "auto_delete_candidate_files": auto_delete_count,
        "non_4326_crs_files": non_4326_crs_count,
        "all_crs_4326": non_4326_crs_count == 0,
        "rows": rows,
    }

def _default_auto_geocorrect_behavior():
    return {
        "mode": "overwrite",
        "suffix": "_autogeo",
        "preserve_original": False,
    }

def _normalize_output_suffix(suffix_text, fallback):
    suffix = str(suffix_text or "").strip() or fallback
    for bad in ("/", "\\"):
        suffix = suffix.replace(bad, "_")
    suffix = suffix.replace(":", "_")
    return suffix or fallback

def _build_suffixed_output_path(path, suffix_text, fallback):
    suffix = _normalize_output_suffix(suffix_text, fallback)
    p = Path(path)
    return str(p.with_name(f"{p.stem}{suffix}{p.suffix}"))

def _make_unique_output_path(path):
    p = Path(path)
    if not p.exists():
        return str(p)
    parent = p.parent
    stem = p.stem
    suffix = p.suffix
    idx = 1
    while True:
        cand = parent / f"{stem}_{idx}{suffix}"
        if not cand.exists():
            return str(cand)
        idx += 1

def _coerce_auto_geocorrect_behavior(data=None):
    fallback = _default_auto_geocorrect_behavior()
    out = dict(fallback)
    if isinstance(data, dict):
        mode = str(data.get("mode", fallback["mode"]) or fallback["mode"]).strip().lower()
        out["mode"] = "suffix" if mode == "suffix" else "overwrite"
        out["suffix"] = _normalize_output_suffix(data.get("suffix", fallback["suffix"]), fallback["suffix"])
        out["preserve_original"] = bool(data.get("preserve_original", fallback["preserve_original"]))
    return out

def summarize_auto_geocorrect_behavior(behavior=None):
    vals = _coerce_auto_geocorrect_behavior(behavior)
    if vals["mode"] != "suffix":
        return "overwrite matching TIFF files in place"
    summary = f"save corrected outputs with suffix ({vals['suffix']})"
    if vals.get("preserve_original"):
        summary += "; keep original files"
    else:
        summary += "; remove originals after corrected outputs are written"
    return summary

def summarize_rejected_auto_file_behavior(behavior=None):
    vals = _coerce_auto_geocorrect_behavior(behavior)
    if vals["mode"] != "suffix":
        return "log rejected files with -99999 and delete originals"
    summary = f"save rejected files with suffix ({vals['suffix']}) and log the suffixed filenames with -99999"
    if vals.get("preserve_original"):
        summary += "; keep original files"
    else:
        summary += "; remove originals after suffixed files are written"
    return summary

def _output_alias_log_entry(output_path, dt):
    fname = os.path.basename(output_path)
    alias_dt = _clean_log_image_datetime(dt)
    return (fname, alias_dt, OUTPUT_ALIAS_AZIMUTH, OUTPUT_ALIAS_DISTANCE, False, [0, 0, 0, 0, 0, 0], "")

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

def _coerce_affine_transform(tform=None, vals=None):
    if tform is not None:
        mat = np.asarray(getattr(tform, "params", tform), dtype=float)
    elif vals is not None:
        if len(vals) < 6 or any(v is None for v in vals[:6]):
            return None
        a, b, c, d, e, f = vals[:6]
        mat = np.array([[a, b, c], [d, e, f], [0, 0, 1]], dtype=float)
    else:
        return None

    if mat.shape == (2, 3):
        mat = np.vstack([mat, [0, 0, 1]])
    if mat.shape != (3, 3) or not np.all(np.isfinite(mat)):
        return None
    return AffineTransform(matrix=mat)

def _affine_to_matrix(transform):
    if transform is None:
        return None
    if isinstance(transform, Affine):
        return np.array(
            [
                [transform.a, transform.b, transform.c],
                [transform.d, transform.e, transform.f],
                [0.0, 0.0, 1.0],
            ],
            dtype=float,
        )
    mat = np.asarray(transform, dtype=float)
    if mat.shape == (3, 3):
        return mat
    if mat.shape == (2, 3):
        return np.vstack([mat, [0.0, 0.0, 1.0]])
    return None

def _pixel_transform_from_bounds(left, bottom, right, top, width, height):
    width = max(1, int(width))
    height = max(1, int(height))
    return Affine(
        (float(right) - float(left)) / float(width),
        0.0,
        float(left),
        0.0,
        (float(bottom) - float(top)) / float(height),
        float(top),
    )

def _convert_pixel_tform_between_grids(tform, source_pixel_transform, target_pixel_transform):
    tform = _coerce_affine_transform(tform)
    src_mat = _affine_to_matrix(source_pixel_transform)
    dst_mat = _affine_to_matrix(target_pixel_transform)
    if tform is None or src_mat is None or dst_mat is None:
        return tform
    if src_mat.shape != (3, 3) or dst_mat.shape != (3, 3):
        return tform
    try:
        converted = np.linalg.inv(dst_mat) @ src_mat @ np.asarray(tform.params, dtype=float) @ np.linalg.inv(src_mat) @ dst_mat
    except Exception:
        return tform
    if converted.shape != (3, 3) or not np.all(np.isfinite(converted)):
        return tform
    return AffineTransform(matrix=converted)

def _convert_logged_tform_to_target_grid(tform, logged_source_path, target_path, logged_source_grid=None):
    tform = _coerce_affine_transform(tform)
    if tform is None:
        return None
    logged_source_path = str(logged_source_path or "").strip()
    try:
        if logged_source_path and os.path.exists(logged_source_path):
            with rasterio.open(logged_source_path) as source_src:
                source_transform = source_src.transform
        else:
            source_transform = _source_grid_transform(logged_source_grid)
            if source_transform is None:
                return tform
        with rasterio.open(target_path) as target_src:
            target_transform = target_src.transform
    except Exception:
        return tform
    return _convert_pixel_tform_between_grids(tform, source_transform, target_transform)

def _compose_affine_transforms(previous_tform, new_tform):
    prev = _coerce_affine_transform(previous_tform)
    new = _coerce_affine_transform(new_tform)
    if new is None:
        return prev
    if prev is None:
        return new
    return AffineTransform(matrix=np.asarray(new.params, dtype=float) @ np.asarray(prev.params, dtype=float))

def _categorical_raster_categories_for_path(path):
    name = os.path.basename(str(path or "")).lower()
    is_qc = "_qc" in name
    is_cloud = "_cloud" in name
    if not is_qc and not is_cloud:
        return None
    try:
        with rasterio.open(path) as src:
            if is_qc:
                expected_dtype = np.dtype("uint16")
            else:
                expected_dtype = np.dtype("uint8")
            if not src.dtypes or not all(np.dtype(dt) == expected_dtype for dt in src.dtypes):
                return None
            vals = np.unique(src.read())
            if is_qc and src.nodata is not None:
                vals = vals[vals != src.nodata]
    except Exception:
        return None
    if vals.size == 0:
        return None
    return np.unique(vals.astype(np.float64))

def _qc_uint16_categories_for_path(path):
    return _categorical_raster_categories_for_path(path)

def _is_cloud_byte_raster_for_path(path):
    if "_cloud" not in os.path.basename(str(path or "")).lower():
        return False
    try:
        with rasterio.open(path) as src:
            return bool(src.dtypes and all(np.dtype(dt) == np.dtype("uint8") for dt in src.dtypes))
    except Exception:
        return False

def _is_water_byte_raster_for_path(path):
    if "_water" not in os.path.basename(str(path or "")).lower():
        return False
    try:
        with rasterio.open(path) as src:
            return bool(src.dtypes and all(np.dtype(dt) == np.dtype("uint8") for dt in src.dtypes))
    except Exception:
        return False

def _snap_to_nearest_source_category(arr, categories, preserve_mask=None):
    if categories is None:
        return arr
    cats = np.asarray(categories, dtype=np.float64)
    cats = cats[np.isfinite(cats)]
    if cats.size == 0:
        return arr
    cats = np.unique(cats)

    src = np.asarray(arr)
    out = src.copy()
    valid = np.isfinite(src)
    if preserve_mask is not None:
        try:
            valid &= ~np.asarray(preserve_mask, dtype=bool)
        except Exception:
            pass
    if not np.any(valid):
        return out

    values = src[valid].astype(np.float64)
    right_idx = np.searchsorted(cats, values, side="left")
    right_idx = np.clip(right_idx, 0, cats.size - 1)
    left_idx = np.clip(right_idx - 1, 0, cats.size - 1)
    left_vals = cats[left_idx]
    right_vals = cats[right_idx]
    use_right = np.abs(right_vals - values) < np.abs(values - left_vals)
    out[valid] = np.where(use_right, right_vals, left_vals)
    return out

def _apply_raster_warp_to_path(target_path, tform, categorical_values=None):
    tform = _coerce_affine_transform(tform)
    if tform is None:
        return

    with rasterio.open(target_path) as src:
        band = src.read(1)
        meta = src.meta.copy()

    dst_dtype = band.dtype
    cloud_zero_is_class = _is_cloud_byte_raster_for_path(target_path)
    declared_nodata = None if cloud_zero_is_class else meta.get('nodata')
    nd = _nodata_for_dtype(dst_dtype, declared_nodata)

    warped = skwarp(
        band.astype(np.float32),
        inverse_map=tform.inverse,
        output_shape=band.shape,
        cval=np.nan,
        preserve_range=True,
        order=0 if categorical_values is not None else None,
    )

    if np.issubdtype(dst_dtype, np.integer):
        info = np.iinfo(dst_dtype)
        nan_mask = np.isnan(warped)
        if categorical_values is not None:
            if declared_nodata is not None:
                warped = _snap_to_nearest_source_category(warped, categorical_values, preserve_mask=nan_mask)
            else:
                warped = np.where(nan_mask, nd, warped)
                warped = _snap_to_nearest_source_category(warped, categorical_values)
                nan_mask = np.zeros_like(nan_mask, dtype=bool)
        else:
            warped = np.rint(warped)
        warped = np.where(nan_mask, nd, warped)
        warped = np.clip(warped, info.min, info.max).astype(dst_dtype)
        meta.update(dtype=dst_dtype, nodata=None if cloud_zero_is_class else nd)
    else:
        warped = warped.astype('float32')
        meta.update(dtype='float32', nodata=np.nan)

    tmp = target_path + '.warp.tmp'
    with rasterio.open(tmp, 'w', **meta) as dst:
        dst.write(warped, 1)
    os.replace(tmp, target_path)

def _apply_raster_translation_to_path(target_path, dx, dy, categorical_values=None):
    dx = float(dx or 0.0)
    dy = float(dy or 0.0)
    if dx == 0.0 and dy == 0.0:
        return

    with rasterio.open(target_path) as src:
        arr = src.read()
        meta = src.meta.copy()
        oT, crs = src.transform, src.crs

    nT = Affine(oT.a, oT.b, oT.c + dx, oT.d, oT.e, oT.f + dy)

    dst_dtype = arr.dtype
    cloud_zero_is_class = _is_cloud_byte_raster_for_path(target_path)
    declared_nodata = None if cloud_zero_is_class else meta.get('nodata')
    nd = _nodata_for_dtype(dst_dtype, declared_nodata)

    if np.issubdtype(dst_dtype, np.integer):
        dest = np.full_like(arr, nd, dtype=dst_dtype)
        src_nodata = None if cloud_zero_is_class else nd
        dst_nodata = None if cloud_zero_is_class else nd
        for b in range(arr.shape[0]):
            reproject(
                source=arr[b],
                destination=dest[b],
                src_transform=nT,
                src_crs=crs,
                dst_transform=oT,
                dst_crs=crs,
                resampling=Resampling.nearest,
                src_nodata=src_nodata,
                dst_nodata=dst_nodata,
                fill_value=nd,
                num_threads=REPROJECT_THREADS,
            )
        if categorical_values is not None:
            preserve_mask = (dest == nd) if declared_nodata is not None else None
            dest = _snap_to_nearest_source_category(
                dest,
                categorical_values,
                preserve_mask=preserve_mask,
            ).astype(dst_dtype)
        meta.update(transform=oT, dtype=dst_dtype, nodata=None if cloud_zero_is_class else nd)
    else:
        dest = np.full_like(arr, np.nan, dtype='float32')
        for b in range(arr.shape[0]):
            reproject(
                source=arr[b],
                destination=dest[b],
                src_transform=nT,
                src_crs=crs,
                dst_transform=oT,
                dst_crs=crs,
                resampling=Resampling.nearest,
                src_nodata=meta.get('nodata'),
                dst_nodata=np.nan,
                fill_value=np.nan,
                num_threads=REPROJECT_THREADS,
            )
        meta.update(transform=oT, dtype='float32', nodata=np.nan)

    tmp = target_path + '.tmp'
    with rasterio.open(tmp, 'w', **meta) as dst:
        dst.write(dest)
    os.replace(tmp, target_path)

def _apply_final_transform_to_path(target_path, dx=0.0, dy=0.0, tform=None, categorical_values=None):
    # The interactive view warps pixels first and applies pan as the image extent.
    # Persist in the same order so later translations are not lost.
    _apply_raster_warp_to_path(target_path, tform, categorical_values=categorical_values)
    _apply_raster_translation_to_path(target_path, dx, dy, categorical_values=categorical_values)

def auto_geocorrect(all_files, processed_dts, processed_files, log_entries, output_behavior=None):
    output_behavior = _coerce_auto_geocorrect_behavior(output_behavior)
    file_by_name = _current_file_lookup_by_name(all_files)
    crs_cache = {}
    by_dt = _log_entries_by_datetime(log_entries, file_by_name, LOG_FILE, crs_cache)
    count = 0
    for path in all_files:
        dt = parse_datetime_from_filename(path)
        fname = os.path.basename(path)
        current_crs_label, current_crs_token = _raster_crs_info_for_log_inventory(path, crs_cache)
        match = _find_existing_log_match(str(dt or ""), current_crs_token, by_dt)
        rejected_match = _find_rejected_log_match(str(dt or ""), by_dt)
        if rejected_match and fname not in processed_files:
            comment = rejected_match.get("comment", "")
            if output_behavior["mode"] == "suffix":
                target_path = _make_unique_output_path(
                    _build_suffixed_output_path(path, output_behavior.get("suffix"), "_autogeo")
                )
                if bool(output_behavior.get("preserve_original")):
                    shutil.copy2(path, target_path)
                    log_entries.append((
                        fname, dt, None, None, False, [None] * 6,
                        "", "", _current_log_logged_datetime(), ORIGINAL_COPY_COMMENT,
                    ))
                    processed_files.add(fname)
                else:
                    shutil.move(path, target_path)
                log_fname = os.path.basename(target_path)
            else:
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
                log_fname = fname

            log_entries.append((
                log_fname, dt, REJECT_AZIMUTH, REJECT_DISTANCE, False, [0] * 6,
                "", "", _current_log_logged_datetime(), comment,
            ))
            processed_files.add(log_fname)
            count += 1
            continue
        if match and fname not in processed_files:
            az = match.get("azimuth", 0.0)
            dist = match.get("distance", 0.0)
            warp_flag = bool(match.get("warp_flag"))
            vals = match.get("vals", [0] * 6)
            comment = match.get("comment", "")
            if _is_water_byte_raster_for_path(path):
                log_entries.append((
                    fname, dt, 0.0, 0.0, False, [0] * 6,
                    "", "", _current_log_logged_datetime(), ORIGINAL_COPY_COMMENT,
                ))
                processed_files.add(fname)
                count += 1
                continue
            categorical_values = _categorical_raster_categories_for_path(path)
            target_path = path
            created_output = None
            if output_behavior["mode"] == "suffix":
                target_path = _make_unique_output_path(
                    _build_suffixed_output_path(path, output_behavior.get("suffix"), "_autogeo")
                )
                shutil.copy2(path, target_path)
                created_output = target_path

            dx = 0.0
            dy = 0.0
            try:
                dist_for_shift = float(dist or 0.0)
            except Exception:
                dist_for_shift = 0.0

            if dist_for_shift > 0:
                with rasterio.open(target_path) as src:
                    crs = src.crs
                    L, R, B, T = src.bounds.left, src.bounds.right, src.bounds.bottom, src.bounds.top
                    lon0, lat0 = warp_transform(crs, 'EPSG:4326', [(L+R)/2], [(B+T)/2])
                dest_lon, dest_lat, _ = geod.fwd(lon0[0], lat0[0], float(az or 0.0), dist_for_shift)
                dest_x, dest_y = warp_transform('EPSG:4326', crs, [dest_lon], [dest_lat])
                dx = dest_x[0] - (L+R)/2
                dy = dest_y[0] - (B+T)/2

            tform = _coerce_affine_transform(vals=vals) if warp_flag else None
            if tform is not None:
                tform = _convert_logged_tform_to_target_grid(
                    tform,
                    match.get("path"),
                    target_path,
                    match.get("source_grid"),
                )
            _apply_final_transform_to_path(target_path, dx, dy, tform, categorical_values=categorical_values)

            if created_output and not bool(output_behavior.get("preserve_original")):
                os.remove(path)

            if created_output and bool(output_behavior.get("preserve_original")):
                log_entries.append((
                    fname, dt, None, None, False, [None] * 6,
                    "", "", _current_log_logged_datetime(), ORIGINAL_COPY_COMMENT,
                ))
                processed_files.add(fname)

            log_fname = os.path.basename(created_output) if created_output else fname
            log_entries.append((
                log_fname, dt, az, dist, warp_flag, vals,
                "", "", _current_log_logged_datetime(), comment,
            ))
            processed_files.add(log_fname)
            count += 1
    write_log(log_entries)
    return count

class ExistingLogDecisionDialog(QtWidgets.QDialog):
    """Review a found GeolocationLog.csv before manual append or auto-geocorrect."""

    def __init__(self, all_files, processed_dts, processed_files, log_entries, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Existing Log Detected")
        self.setModal(True)
        self.resize(1320, 880)
        self._decision = None
        self.inventory = build_existing_log_inventory(
            all_files,
            processed_dts,
            processed_files,
            log_entries,
            LOG_FILE,
        )
        self._summary_label_widgets = []

        self._apply_theme()

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(10)

        title = QtWidgets.QLabel("GeolocationLog.csv was found")
        title_font = QtGui.QFont("Lucida Console", 16, QtGui.QFont.Bold)
        title.setFont(title_font)
        title.setStyleSheet(f"color: {self.theme['heading']};")
        self.title_label = title
        outer.addWidget(title)

        intro = QtWidgets.QLabel(
            "Review records before choosing Manual Append or Auto-Geocorrect. "
            "<b>Auto-Geocorrect</b> runs on unrecorded TIFFs whose detected datetime matches a correction row in the log; "
            "Matched Pairs record datetime matched between the log file / folder. "
            "<b>Manual Append</b> enters full referencer."
        )
        intro.setTextFormat(QtCore.Qt.RichText)
        intro.setWordWrap(True)
        outer.addWidget(intro)

        summary_group = QtWidgets.QGroupBox("Detected Log")
        summary_lay = QtWidgets.QGridLayout(summary_group)
        summary_lay.setHorizontalSpacing(14)
        summary_lay.setVerticalSpacing(7)
        outer.addWidget(summary_group)

        self._add_summary_value(summary_lay, 0, 0, "Path", self.inventory["log_path"], span=5)
        self._add_summary_value(summary_lay, 1, 0, "Size", self.inventory["log_size_text"])
        self._add_summary_value(summary_lay, 1, 2, "Log file rows", str(self.inventory["log_rows"]))
        self._add_summary_value(summary_lay, 1, 4, "TIFFs in directory", str(self.inventory["current_files"]))
        self._add_summary_value(summary_lay, 2, 0, "Already recorded", str(self.inventory["recorded_files"]))
        self._add_summary_value(summary_lay, 2, 2, "Left to record", str(self.inventory["need_recording_files"]))
        self._add_summary_value(summary_lay, 2, 4, "Matched pairs (auto-geocorrectable)", str(self.inventory["auto_candidate_files"]))
        self.crs_status_label = QtWidgets.QLabel()
        self.crs_status_label.setTextFormat(QtCore.Qt.RichText)
        self.crs_status_label.setWordWrap(True)
        self.crs_status_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        summary_lay.addWidget(self.crs_status_label, 3, 0, 1, 6)
        self._sync_crs_status_label()

        table_group = QtWidgets.QGroupBox("Current Folder TIFF Inventory")
        table_lay = QtWidgets.QVBoxLayout(table_group)
        table_lay.setContentsMargins(10, 14, 10, 10)
        outer.addWidget(table_group, 1)

        self.table = QtWidgets.QTableWidget(len(self.inventory["rows"]), 6, table_group)
        self.table.setHorizontalHeaderLabels([
            "Status",
            "Auto-geocorrect",
            "File name in folder",
            "Detected date",
            "CRS",
            "Matched log file",
        ])
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setFocusPolicy(QtCore.Qt.NoFocus)
        self.table.verticalHeader().setVisible(False)
        self.table.setSortingEnabled(False)
        self.table.horizontalHeader().setSectionsClickable(False)
        self.table.horizontalHeader().setSortIndicatorShown(False)
        self.table.cellDoubleClicked.connect(self._show_metadata_for_table_row)
        self._table_rows = []
        self._populate_table()
        table_lay.addWidget(self.table)

        auto_group = QtWidgets.QGroupBox("Auto-Geocorrect File Handling")
        auto_lay = QtWidgets.QGridLayout(auto_group)
        auto_lay.setHorizontalSpacing(10)
        auto_lay.setVerticalSpacing(8)
        outer.addWidget(auto_group)

        auto_note = QtWidgets.QLabel(
            "Confirm how files detected for Auto-Geocorrect will be written before running the batch."
        )
        auto_note.setWordWrap(True)
        auto_note.setStyleSheet(f"color: {self.theme['muted']};")
        self.auto_note = auto_note
        auto_lay.addWidget(auto_note, 0, 0, 1, 4)

        self.auto_overwrite = QtWidgets.QRadioButton("Overwrite matching TIFFs in place")
        self.auto_suffix_mode = QtWidgets.QRadioButton("Save corrected TIFFs with suffix")
        self.auto_suffix_edit = QtWidgets.QLineEdit("_autogeo")
        self.auto_suffix_edit.setMinimumWidth(160)
        self.auto_preserve_original = QtWidgets.QCheckBox("Keep original TIFFs when using suffix")
        self.auto_preserve_original.setChecked(True)
        self.auto_summary_label = QtWidgets.QLabel()
        self.auto_summary_label.setWordWrap(True)
        self.auto_summary_label.setStyleSheet(f"color: {self.theme['heading']}; font-weight: 700;")

        self.auto_overwrite.setChecked(True)
        auto_lay.addWidget(self.auto_overwrite, 1, 0, 1, 2)
        auto_lay.addWidget(self.auto_suffix_mode, 1, 2)
        auto_lay.addWidget(self.auto_suffix_edit, 1, 3)
        auto_lay.addWidget(self.auto_preserve_original, 2, 2, 1, 2)
        auto_lay.addWidget(self.auto_summary_label, 3, 0, 1, 4)

        self.auto_overwrite.toggled.connect(self._sync_auto_controls)
        self.auto_suffix_mode.toggled.connect(self._sync_auto_controls)
        self.auto_suffix_edit.textChanged.connect(self._sync_auto_controls)
        self.auto_preserve_original.toggled.connect(self._sync_auto_controls)

        button_row = QtWidgets.QHBoxLayout()
        outer.addLayout(button_row)
        button_row.addStretch(1)
        self.cancel_btn = QtWidgets.QPushButton("Cancel")
        self.manual_btn = QtWidgets.QPushButton("Manual Append")
        self.auto_btn = QtWidgets.QPushButton("Run Auto-Geocorrect")
        for btn in (self.cancel_btn, self.manual_btn, self.auto_btn):
            btn.setDefault(False)
            btn.setAutoDefault(False)
        self.auto_btn.setEnabled(self.inventory["auto_candidate_files"] > 0)
        button_row.addWidget(self.cancel_btn)
        button_row.addWidget(self.manual_btn)
        button_row.addWidget(self.auto_btn)

        self.cancel_btn.clicked.connect(self.reject)
        self.manual_btn.clicked.connect(self._choose_manual)
        self.auto_btn.clicked.connect(self._choose_auto)
        self._sync_auto_controls()
        self._install_selection_clear_filters()
        self._apply_theme()

    def _add_summary_value(self, layout, row, col, label, value, span=1):
        label_widget = QtWidgets.QLabel(f"{label}:")
        label_widget.setStyleSheet(f"color: {self.theme['muted']}; font-weight: 700;")
        self._summary_label_widgets.append(label_widget)
        value_widget = QtWidgets.QLabel(str(value))
        value_widget.setWordWrap(True)
        value_widget.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        layout.addWidget(label_widget, row, col)
        layout.addWidget(value_widget, row, col + 1, 1, span)

    def _sync_crs_status_label(self):
        if not hasattr(self, "crs_status_label"):
            return
        if self.theme_mode == "dark":
            ok_color = "#70D98B"
            warning_color = "#FF8A8A"
        else:
            ok_color = "#1F8A4C"
            warning_color = "#C23B22"
        if bool(self.inventory.get("all_crs_4326")):
            self.crs_status_label.setText(f'<span style="color:{ok_color};">All CRS in 4326</span>')
        else:
            self.crs_status_label.setText(
                f'<span style="color:{warning_color};">All CRS not in 4326 - '
                '<b>Manual Append</b> to reproject.</span>'
            )

    def _table_theme_colors(self):
        if self.theme_mode == "dark":
            return "#242424", "#303030", "#FFFFFF", "#3F5F78"
        return self.theme["input_bg"], "#F1F1F1", self.theme["text"], "#BBD7F0"

    def _apply_theme(self):
        self.theme_mode = get_app_theme_mode()
        self.theme = build_theme_palette(self.theme_mode)
        table_bg, table_alt_bg, table_text, table_selection = self._table_theme_colors()
        self.setStyleSheet(build_app_stylesheet(self.theme_mode) + f"""
        QTableWidget {{
            gridline-color: {self.theme['border']};
            background-color: {table_bg};
            alternate-background-color: {table_alt_bg};
            selection-background-color: {table_selection};
            selection-color: {table_text};
            outline: 0;
        }}
        QTableWidget::item:selected {{
            background-color: {table_selection};
            color: {table_text};
        }}
        QHeaderView::section {{
            background-color: {self.theme['panel_bg']};
            color: {self.theme['heading']};
            border: 1px solid {self.theme['border']};
            border-bottom: 2px solid {self.theme['border']};
            padding: 5px 7px;
            font-weight: 700;
        }}
        QRadioButton, QCheckBox {{
            color: {self.theme['text']};
            spacing: 6px;
        }}
        """)
        if hasattr(self, "title_label"):
            self.title_label.setStyleSheet(f"color: {self.theme['heading']};")
        for label_widget in getattr(self, "_summary_label_widgets", []):
            label_widget.setStyleSheet(f"color: {self.theme['muted']}; font-weight: 700;")
        if hasattr(self, "crs_status_label"):
            self._sync_crs_status_label()
        if hasattr(self, "auto_note"):
            self.auto_note.setStyleSheet(f"color: {self.theme['muted']};")
        if hasattr(self, "auto_summary_label"):
            self.auto_summary_label.setStyleSheet(f"color: {self.theme['heading']}; font-weight: 700;")
        if hasattr(self, "table"):
            self._populate_table()

    def _toggle_theme(self):
        new_mode = "light" if get_app_theme_mode() == "dark" else "dark"
        set_app_theme_mode(new_mode)
        self._apply_theme()

    def _handle_theme_toggle_key(self, event):
        if event.type() != QtCore.QEvent.KeyPress:
            return False
        if event.key() != QtCore.Qt.Key_Slash:
            return False
        if event.modifiers() & (QtCore.Qt.ControlModifier | QtCore.Qt.AltModifier | QtCore.Qt.MetaModifier):
            return False
        text = event.text() if hasattr(event, "text") else ""
        if text and text != "/":
            return False
        self._toggle_theme()
        event.accept()
        return True

    def _status_sort_rank(self, row):
        if row.get("recorded"):
            return 0
        if row.get("auto_delete_candidate"):
            return 1
        if row.get("auto_datetime_match_only"):
            return 2
        return 3

    def _populate_table(self):
        self.table.setSortingEnabled(False)
        if self.theme_mode == "dark":
            recorded_brush = QtGui.QBrush(QtGui.QColor("#70D98B"))
            pending_brush = QtGui.QBrush(QtGui.QColor("#FF8A8A"))
            deletion_brush = QtGui.QBrush(QtGui.QColor("#FF69B4"))
            datetime_match_brush = QtGui.QBrush(QtGui.QColor("#FFD166"))
            auto_brush = QtGui.QBrush(QtGui.QColor("#7DB3FF"))
            crs_warning_brush = QtGui.QBrush(QtGui.QColor("#FF5A5A"))
        else:
            recorded_brush = QtGui.QBrush(QtGui.QColor("#70D98B"))
            pending_brush = QtGui.QBrush(QtGui.QColor("#D85C5C"))
            deletion_brush = QtGui.QBrush(QtGui.QColor("#D63384"))
            datetime_match_brush = QtGui.QBrush(QtGui.QColor("#D89B00"))
            auto_brush = QtGui.QBrush(QtGui.QColor("#2F6FA3"))
            crs_warning_brush = QtGui.QBrush(QtGui.QColor("#C23B22"))
        default_text = "#FFFFFF" if self.theme_mode == "dark" else self.theme["text"]
        default_brush = QtGui.QBrush(QtGui.QColor(default_text))
        rows = sorted(
            self.inventory["rows"],
            key=lambda item: (
                self._status_sort_rank(item),
                str(item.get("filename") or "").lower(),
            ),
        )
        self._table_rows = rows
        for row_idx, row in enumerate(rows):
            values = [
                row["status"],
                row["auto_text"],
                row["filename"],
                row["datetime"],
                row["crs"],
                row["matched_filename"],
            ]
            for col_idx, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(str(value))
                if col_idx == 0:
                    if row.get("auto_delete_candidate"):
                        item.setForeground(deletion_brush)
                    elif row["recorded"]:
                        item.setForeground(recorded_brush)
                    elif row.get("auto_datetime_match_only"):
                        item.setForeground(datetime_match_brush)
                    else:
                        item.setForeground(pending_brush)
                elif col_idx == 1 and row.get("auto_delete_candidate"):
                    item.setForeground(deletion_brush)
                elif col_idx == 1 and row["auto_candidate"]:
                    item.setForeground(auto_brush)
                elif col_idx == 4 and not row.get("crs_is_4326"):
                    item.setForeground(crs_warning_brush)
                else:
                    item.setForeground(default_brush)
                self.table.setItem(row_idx, col_idx, item)
        self.table.clearSelection()
        self.table.setCurrentIndex(QtCore.QModelIndex())
        self.table.resizeColumnsToContents()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QtWidgets.QHeaderView.Stretch)

    def _show_metadata_for_table_row(self, row_idx, _col_idx=None):
        rows = getattr(self, "_table_rows", [])
        if row_idx < 0 or row_idx >= len(rows):
            return
        self.table.selectRow(row_idx)
        self.table.setCurrentCell(row_idx, 0)
        dlg = ExistingLogMetadataDialog(rows[row_idx], self.theme_mode, self)
        dlg.exec_()

    def _install_selection_clear_filters(self):
        self.installEventFilter(self)
        for widget in self.findChildren(QtWidgets.QWidget):
            widget.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == QtCore.QEvent.KeyPress and hasattr(self, "table"):
            key = event.key()
            if self._handle_theme_toggle_key(event):
                return True
            if key in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
                self._pulse_decision_buttons()
                return True
            if key in (QtCore.Qt.Key_Left, QtCore.Qt.Key_Right):
                return True
            if key in (QtCore.Qt.Key_Up, QtCore.Qt.Key_Down) and self._move_selected_table_row(key):
                return True
        if event.type() == QtCore.QEvent.MouseButtonPress and hasattr(self, "table"):
            source = obj if isinstance(obj, QtWidgets.QWidget) else None
            inside_table = bool(
                source is self.table
                or source is self.table.viewport()
                or self.table.isAncestorOf(source)
            )
            if not inside_table:
                self.table.clearSelection()
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event):
        if self._handle_theme_toggle_key(event):
            return
        super().keyPressEvent(event)

    def _move_selected_table_row(self, key):
        selected = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not selected:
            return False
        current = self.table.currentIndex()
        row = current.row() if current.isValid() else selected[0].row()
        delta = -1 if key == QtCore.Qt.Key_Up else 1
        target = max(0, min(self.table.rowCount() - 1, row + delta))
        if target == row:
            return True
        self.table.selectRow(target)
        self.table.setCurrentCell(target, 0)
        item = self.table.item(target, 0)
        if item is not None:
            self.table.scrollToItem(item, QtWidgets.QAbstractItemView.PositionAtCenter)
        return True

    def _pulse_decision_buttons(self):
        if getattr(self, "_decision_button_pulse_active", False):
            return
        self._decision_button_pulse_active = True
        token = object()
        self._decision_button_pulse_token = token
        flash_style = (
            "QPushButton { border: 1px solid #FFFFFF; }"
            "QPushButton:disabled { border: 1px solid #FFFFFF; }"
        )

        buttons = [self.manual_btn, self.auto_btn]
        originals = {btn: str(btn.styleSheet() or "") for btn in buttons}

        def apply_state(on, final=False):
            if getattr(self, "_decision_button_pulse_token", None) is not token:
                return
            for btn in buttons:
                btn.setStyleSheet(flash_style if on else originals.get(btn, ""))
            if final:
                self._decision_button_pulse_active = False

        apply_state(True)
        QtCore.QTimer.singleShot(130, lambda: apply_state(False))
        QtCore.QTimer.singleShot(260, lambda: apply_state(True))
        QtCore.QTimer.singleShot(390, lambda: apply_state(False, True))

    def _sync_auto_controls(self, *_args):
        suffix_mode = self.auto_suffix_mode.isChecked()
        self.auto_suffix_edit.setEnabled(suffix_mode)
        self.auto_preserve_original.setEnabled(suffix_mode)
        behavior = self.auto_behavior()
        transform_count = int(self.inventory.get("auto_transform_candidate_files", 0) or 0)
        delete_count = int(self.inventory.get("auto_delete_candidate_files", 0) or 0)
        lines = []
        if transform_count:
            plural = "" if transform_count == 1 else "s"
            lines.append(
                f"{transform_count} file{plural} will be handled as: "
                f"{summarize_auto_geocorrect_behavior(behavior)}."
            )
        if delete_count:
            plural = "" if delete_count == 1 else "s"
            lines.append(
                f"{delete_count} rejected duplicate file{plural} will be handled as: "
                f"{summarize_rejected_auto_file_behavior(behavior)}."
            )
        if not lines:
            lines.append("No unrecorded TIFFs match usable correction rows in the log.")
        self.auto_summary_label.setText(" ".join(lines))

    def auto_behavior(self):
        return _coerce_auto_geocorrect_behavior({
            "mode": "suffix" if self.auto_suffix_mode.isChecked() else "overwrite",
            "suffix": self.auto_suffix_edit.text(),
            "preserve_original": bool(self.auto_suffix_mode.isChecked() and self.auto_preserve_original.isChecked()),
        })

    def decision(self):
        return self._decision

    def _choose_manual(self):
        self._decision = "manual"
        self.accept()

    def _choose_auto(self):
        if self.inventory["auto_candidate_files"] <= 0:
            QMessageBox.information(self, "Auto-Geocorrect", "No unrecorded TIFFs match usable correction rows in the log.")
            return
        if self.auto_suffix_mode.isChecked() and not (self.auto_suffix_edit.text() or "").strip():
            QMessageBox.warning(self, "Auto-Geocorrect Suffix", "Enter a suffix for corrected output files.")
            return
        self._decision = "auto"
        self.accept()

class AutoGeocorrectCompletionDialog(QtWidgets.QDialog):
    """Completion dialog shown after Auto-Geocorrect finishes."""

    def __init__(self, corrected_count, candidate_count, behavior, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Auto-Geocorrect Complete")
        self.setModal(True)
        self.restart_requested = False

        self.corrected_count = self._safe_int(corrected_count)
        self.candidate_count = max(1, self._safe_int(candidate_count), self.corrected_count)
        self.behavior = _coerce_auto_geocorrect_behavior(behavior)
        self.theme_mode = get_app_theme_mode()
        self.theme = build_theme_palette(self.theme_mode)

        self._apply_theme()

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 18)
        outer.setSpacing(12)

        title = QtWidgets.QLabel("Auto-Geocorrect Complete", self)
        title.setObjectName("AutoGeocorrectCompletionTitle")
        outer.addWidget(title)

        intro = QtWidgets.QLabel(self)
        intro.setWordWrap(True)
        intro.setText(
            "GeoViewer applied the matching georeferencing records from GeolocationLog.csv "
            "to the unrecorded TIFFs detected in this folder. Restart to reload the corrected "
            "file set immediately, or exit and reopen GeoViewer when you are ready."
        )
        outer.addWidget(intro)

        summary_group = QtWidgets.QGroupBox("Batch Summary", self)
        summary_lay = QtWidgets.QGridLayout(summary_group)
        summary_lay.setHorizontalSpacing(14)
        summary_lay.setVerticalSpacing(7)
        outer.addWidget(summary_group)

        self._add_summary_row(summary_lay, 0, "Detected Auto-Geocorrect candidates", str(self.candidate_count))
        self._add_summary_row(summary_lay, 1, "Corrected TIFFs", str(self.corrected_count))
        self._add_summary_row(summary_lay, 2, "File handling", summarize_auto_geocorrect_behavior(self.behavior))
        self._add_summary_row(summary_lay, 3, "Log status", "GeolocationLog.csv was updated with the applied corrections.")

        self.progress_bar = QtWidgets.QProgressBar(self)
        self.progress_bar.setRange(0, self.candidate_count)
        self.progress_bar.setValue(min(self.corrected_count, self.candidate_count))
        self.progress_bar.setFormat("%v / %m applied")
        outer.addWidget(self.progress_bar)

        status = QtWidgets.QLabel(self)
        status.setObjectName("AutoGeocorrectCompletionStatus")
        status.setWordWrap(True)
        plural = "" if self.corrected_count == 1 else "s"
        status.setText(
            f"Auto-Geocorrect applied to {self.corrected_count} file{plural}. "
            "Use Restart Application to relaunch in the current folder, or Exit to close GeoViewer."
        )
        outer.addWidget(status)

        button_row = QtWidgets.QHBoxLayout()
        button_row.addStretch(1)
        self.restart_btn = QtWidgets.QPushButton("Restart Application", self)
        self.exit_btn = QtWidgets.QPushButton("Exit", self)
        self.restart_btn.setAutoDefault(False)
        self.restart_btn.setDefault(False)
        self.exit_btn.setAutoDefault(True)
        self.exit_btn.setDefault(True)
        self.restart_btn.clicked.connect(self._restart)
        self.exit_btn.clicked.connect(self._exit)
        button_row.addWidget(self.restart_btn)
        button_row.addWidget(self.exit_btn)
        outer.addLayout(button_row)

        self.exit_btn.setFocus(QtCore.Qt.OtherFocusReason)
        self.resize(700, 390)

    @staticmethod
    def _safe_int(value):
        try:
            return max(0, int(value))
        except Exception:
            return 0

    def _apply_theme(self):
        self.setStyleSheet(build_app_stylesheet(self.theme_mode) + f"""
        QLabel#AutoGeocorrectCompletionTitle {{
            color: {self.theme['heading']};
            font-size: 16pt;
            font-weight: 700;
        }}
        QLabel#AutoGeocorrectCompletionStatus {{
            color: {self.theme['heading']};
            font-weight: 700;
        }}
        QProgressBar {{
            background-color: {self.theme['input_bg']};
            color: {self.theme['text']};
            border: 1px solid {self.theme['border']};
            border-radius: 6px;
            min-height: 28px;
            text-align: center;
        }}
        QProgressBar::chunk {{
            background-color: {self.theme['selection_bg']};
            border-radius: 5px;
        }}
        """)

    def _add_summary_row(self, layout, row, label, value):
        label_widget = QtWidgets.QLabel(f"{label}:", self)
        label_widget.setStyleSheet(f"color: {self.theme['muted']}; font-weight: 700;")
        value_widget = QtWidgets.QLabel(str(value), self)
        value_widget.setWordWrap(True)
        value_widget.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        layout.addWidget(label_widget, row, 0)
        layout.addWidget(value_widget, row, 1)

    def _restart(self):
        self.restart_requested = True
        self.accept()

    def _exit(self):
        self.restart_requested = False
        self.accept()

    def keyPressEvent(self, event):
        if event.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
            self._exit()
            event.accept()
            return
        super().keyPressEvent(event)

# ---------------------------------------------------------------------------
# Splash dialog (Qt version of the Tk splash)
# ---------------------------------------------------------------------------

class LinksDialog(QDialog):
    """
    Simple dark-mode dialog that shows clickable / copyable links.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Links")
        self.setModal(True)
        self.theme_mode = get_app_theme_mode()
        self.theme = build_theme_palette(self.theme_mode)
        self.setStyleSheet(build_app_stylesheet(self.theme_mode))

        header_font = QtGui.QFont("Lucida Console", 16, QtGui.QFont.Bold)
        body_font   = QtGui.QFont("Lucida Console", 12)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        # GitHub links
        links_html = (
            f'<span style="color:{self.theme["text"]}"><b>For updates see:</b></span><br>'
            f'<a style="color:{self.theme["link"]}" href="https://github.com/YOUR_USER/GeoViewer">NASA-JPL GitHub</a><br>'
            f'<a style="color:{self.theme["link"]}" href="https://github.com/YOUR_USER/AnotherRepo">Longenecker Github</a>'
        )

        links_label = QtWidgets.QLabel(links_html)
        links_label.setFont(body_font)
        links_label.setTextFormat(QtCore.Qt.RichText)
        links_label.setTextInteractionFlags(
            QtCore.Qt.TextBrowserInteraction | QtCore.Qt.TextSelectableByMouse
        )
        links_label.setOpenExternalLinks(True)
        layout.addWidget(links_label)

        layout.addStretch(1)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch(1)
        close_btn = QtWidgets.QPushButton("Close")
        close_btn.setFont(body_font)
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

class PanelLayoutPreview(QtWidgets.QFrame):
    """Live preview of the selected panel grid."""
    def __init__(self, theme, cols: int = 3, rows: int = 1, parent=None):
        super().__init__(parent)
        self.theme = dict(theme or {})
        self._cols = 1
        self._rows = 1

        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.setMinimumHeight(170)
        self.setStyleSheet(
            f"""
            QFrame {{
                background-color: {self.theme.get('panel_bg', '#101010')};
                border: 1px solid {self.theme.get('border', '#3A3A3A')};
                border-radius: 10px;
            }}
            """
        )
        self.set_grid(cols, rows)

    def sizeHint(self):
        return QtCore.QSize(180, 170)

    def set_grid(self, cols: int, rows: int):
        cols = int(max(1, min(7, cols)))
        rows = int(max(1, min(7, rows)))
        if self._cols == cols and self._rows == rows:
            return
        self._cols = cols
        self._rows = rows
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)

        rect = self.contentsRect().adjusted(16, 16, -16, -16)
        if rect.width() <= 0 or rect.height() <= 0:
            return

        cols = max(1, self._cols)
        rows = max(1, self._rows)
        gap = max(4, min(10, min(rect.width(), rect.height()) // 18))
        cell_w = (rect.width() - gap * (cols - 1)) / float(cols)
        cell_h = (rect.height() - gap * (rows - 1)) / float(rows)
        cell = max(1.0, min(cell_w, cell_h))

        grid_w = cell * cols + gap * (cols - 1)
        grid_h = cell * rows + gap * (rows - 1)
        start_x = rect.x() + (rect.width() - grid_w) / 2.0
        start_y = rect.y() + (rect.height() - grid_h) / 2.0

        fill_color = QtGui.QColor(self.theme.get("button_bg", "#202020"))
        fill_color.setAlpha(225)
        edge_color = QtGui.QColor(self.theme.get("selection_bg", "#2D5FFF"))
        painter.setPen(QtGui.QPen(edge_color, 2))
        painter.setBrush(fill_color)

        for row in range(rows):
            for col in range(cols):
                x = start_x + col * (cell + gap)
                y = start_y + row * (cell + gap)
                painter.drawRoundedRect(QtCore.QRectF(x, y, cell, cell), 4.0, 4.0)

class PanelLayoutAdvancedPreview(QtWidgets.QFrame):
    """Visualize subplot margins and inter-panel spacing."""
    def __init__(self, theme, layout_settings=None, parent=None):
        super().__init__(parent)
        self.theme = theme
        self._layout_settings = normalize_panel_layout_settings(layout_settings)
        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.setMinimumHeight(210)
        self.setStyleSheet(
            f"""
            QFrame {{
                background-color: {self.theme.get('panel_bg', '#101010')};
                border: 1px solid {self.theme.get('border', '#3A3A3A')};
                border-radius: 10px;
            }}
            """
        )

    def sizeHint(self):
        return QtCore.QSize(260, 210)

    def set_layout_settings(self, layout_settings):
        vals = normalize_panel_layout_settings(layout_settings)
        if vals == self._layout_settings:
            return
        self._layout_settings = vals
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)

        outer = QtCore.QRectF(self.contentsRect().adjusted(18, 18, -18, -18))
        if outer.width() <= 0 or outer.height() <= 0:
            return

        vals = normalize_panel_layout_settings(self._layout_settings)
        figure_rect = outer
        plot_rect = QtCore.QRectF(
            figure_rect.left() + figure_rect.width() * vals["left"],
            figure_rect.top() + figure_rect.height() * (1.0 - vals["top"]),
            figure_rect.width() * max(0.0, vals["right"] - vals["left"]),
            figure_rect.height() * max(0.0, vals["top"] - vals["bottom"]),
        )

        painter.setPen(QtGui.QPen(QtGui.QColor(self.theme.get("border", "#3A3A3A")), 1.4))
        painter.setBrush(QtGui.QColor(self.theme.get("button_bg", "#202020")))
        painter.drawRoundedRect(figure_rect, 10.0, 10.0)

        margin_fill = QtGui.QColor(self.theme.get("group_bg", "#111111"))
        margin_fill.setAlpha(180)
        painter.setBrush(margin_fill)
        painter.drawRoundedRect(plot_rect, 8.0, 8.0)

        plot_border = QtGui.QColor(self.theme.get("selection_bg", "#2D5FFF"))
        plot_border.setAlpha(220)
        painter.setPen(QtGui.QPen(plot_border, 2.0))
        painter.setBrush(QtGui.QColor(0, 0, 0, 0))
        painter.drawRoundedRect(plot_rect, 8.0, 8.0)

        cols = 2
        rows = 2
        cell_w = plot_rect.width() / max(1e-6, cols + vals["wspace"] * (cols - 1))
        cell_h = plot_rect.height() / max(1e-6, rows + vals["hspace"] * (rows - 1))
        gap_x = cell_w * vals["wspace"]
        gap_y = cell_h * vals["hspace"]

        panel_fill = QtGui.QColor(self.theme.get("selection_bg", "#2D5FFF"))
        panel_fill.setAlpha(120)
        panel_edge = QtGui.QColor(self.theme.get("selection_bg", "#2D5FFF"))
        panel_edge.setAlpha(255)
        painter.setPen(QtGui.QPen(panel_edge, 1.5))
        painter.setBrush(panel_fill)

        for row in range(rows):
            for col in range(cols):
                x = plot_rect.left() + col * (cell_w + gap_x)
                y = plot_rect.top() + row * (cell_h + gap_y)
                cell_rect = QtCore.QRectF(x, y, max(6.0, cell_w), max(6.0, cell_h))
                painter.drawRoundedRect(cell_rect, 6.0, 6.0)

class PanelLayoutAdvancedDialog(QtWidgets.QDialog):
    """Expose subplot spacing and margins with a live preview."""
    def __init__(
        self,
        layout_settings=None,
        sync_zoom_pan=False,
        scroll_wheel_pan_multi_enabled=True,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Advanced panel spacing")
        self.setModal(True)

        self.theme_mode = get_app_theme_mode()
        self.theme = build_theme_palette(self.theme_mode)
        self.setStyleSheet(build_app_stylesheet(self.theme_mode))
        self._spin_boxes = {}

        body_font = QtGui.QFont("Lucida Console", 11)
        hdr_font = QtGui.QFont("Lucida Console", 13, QtGui.QFont.Bold)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)

        title = QtWidgets.QLabel("Subplot margins and spacing")
        title.setFont(hdr_font)
        title.setStyleSheet(f"color: {self.theme['heading']};")
        lay.addWidget(title)

        note = QtWidgets.QLabel(
            "Adjust Matplotlib subplot margins and panel spacing. "
            "The preview uses a sample 2 x 2 grid so each change is visible."
        )
        note.setWordWrap(True)
        note.setFont(body_font)
        lay.addWidget(note)

        form = QtWidgets.QFormLayout()
        form.setLabelAlignment(QtCore.Qt.AlignRight)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        lay.addLayout(form)

        for key, minimum, maximum, step in (
            ("left", 0.0, 1.0, 0.005),
            ("right", 0.0, 1.0, 0.005),
            ("top", 0.0, 1.0, 0.005),
            ("bottom", 0.0, 1.0, 0.005),
            ("wspace", 0.0, 1.5, 0.005),
            ("hspace", 0.0, 1.5, 0.005),
        ):
            spin = QtWidgets.QDoubleSpinBox(self)
            spin.setDecimals(3)
            spin.setRange(minimum, maximum)
            spin.setSingleStep(step)
            spin.setFont(body_font)
            self._spin_boxes[key] = spin
            form.addRow(f"{key}:", spin)

        preview_title = QtWidgets.QLabel("Preview")
        preview_title.setFont(body_font)
        lay.addWidget(preview_title)

        self.preview = PanelLayoutAdvancedPreview(self.theme, layout_settings=layout_settings, parent=self)
        lay.addWidget(self.preview)

        self.preview_note = QtWidgets.QLabel("")
        self.preview_note.setWordWrap(True)
        self.preview_note.setFont(body_font)
        self.preview_note.setStyleSheet(f"color: {self.theme['muted']};")
        lay.addWidget(self.preview_note)
        self.preview_note.hide()

        self.sync_zoom_pan_check = QtWidgets.QCheckBox(self)
        self.sync_zoom_pan_check.setFont(body_font)
        self.sync_zoom_pan_check.setChecked(bool(sync_zoom_pan))
        self.sync_zoom_pan_check.stateChanged.connect(self._update_sync_zoom_pan_text)
        lay.addWidget(self.sync_zoom_pan_check, 0, QtCore.Qt.AlignLeft)

        self.scroll_wheel_pan_multi_check = QtWidgets.QCheckBox(self)
        self.scroll_wheel_pan_multi_check.setFont(body_font)
        self.scroll_wheel_pan_multi_check.setChecked(bool(scroll_wheel_pan_multi_enabled))
        self.scroll_wheel_pan_multi_check.stateChanged.connect(self._update_scroll_wheel_pan_multi_text)
        lay.addWidget(self.scroll_wheel_pan_multi_check, 0, QtCore.Qt.AlignLeft)

        btns = QtWidgets.QHBoxLayout()
        self.reset_button = QtWidgets.QPushButton("Reset to default")
        self.reset_button.setFont(body_font)
        self.reset_button.setAutoDefault(False)
        self.reset_button.setDefault(False)
        self.reset_button.clicked.connect(self._reset_to_defaults)
        btns.addWidget(self.reset_button)
        btns.addStretch(1)

        ok = QtWidgets.QPushButton("OK")
        ok.setFont(body_font)
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        btns.addWidget(ok)

        cancel = QtWidgets.QPushButton("Cancel")
        cancel.setFont(body_font)
        cancel.setAutoDefault(False)
        cancel.setDefault(False)
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        lay.addLayout(btns)

        for spin in self._spin_boxes.values():
            spin.valueChanged.connect(self._update_preview)

        self._apply_layout_settings(layout_settings)
        self._update_sync_zoom_pan_text()
        self._update_scroll_wheel_pan_multi_text()

    def _apply_layout_settings(self, layout_settings):
        vals = normalize_panel_layout_settings(layout_settings)
        for key, spin in self._spin_boxes.items():
            old = spin.blockSignals(True)
            spin.setValue(vals[key])
            spin.blockSignals(old)
        self._update_preview()

    def _current_layout_settings(self):
        return {
            key: float(spin.value())
            for key, spin in self._spin_boxes.items()
        }

    def _update_preview(self):
        vals = normalize_panel_layout_settings(self._current_layout_settings())
        self.preview.set_layout_settings(vals)

    def _reset_to_defaults(self):
        self._apply_layout_settings(DEFAULT_PANEL_LAYOUT_SETTINGS)

    def _update_sync_zoom_pan_text(self):
        label = "Sync Zoom/Pan: On" if self.sync_zoom_pan_check.isChecked() else "Sync Zoom/Pan: Off"
        self.sync_zoom_pan_check.setText(label)

    def _update_scroll_wheel_pan_multi_text(self):
        label = (
            "Scroll Wheel Pan Multi: On"
            if self.scroll_wheel_pan_multi_check.isChecked()
            else "Scroll Wheel Pan Multi: Off"
        )
        self.scroll_wheel_pan_multi_check.setText(label)

    def values(self):
        return {
            "panel_layout_settings": normalize_panel_layout_settings(self._current_layout_settings()),
            "sync_zoom_pan": bool(self.sync_zoom_pan_check.isChecked()),
            "scroll_wheel_pan_multi_enabled": bool(self.scroll_wheel_pan_multi_check.isChecked()),
        }

class PanelLayoutDialog(QDialog):
    """Dialog to choose the georeferencing panel grid (across × down)."""
    def __init__(
        self,
        cols: int = 3,
        rows: int = 1,
        panel_layout_settings=None,
        button_layout_settings=None,
        sync_zoom_pan: bool = False,
        scroll_wheel_pan_multi_enabled: bool = True,
        button_preset: str = DEFAULT_KEEP_REJECT_PRESET_ID,
        keep_color=None,
        reject_color=None,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Panel layout")
        self.setModal(True)
        self.setProperty(GEOVIEWER_CENTER_ON_SHOW_PROPERTY, True)
        self.theme_mode = get_app_theme_mode()
        self.theme = build_theme_palette(self.theme_mode)
        self.setStyleSheet(build_app_stylesheet(self.theme_mode))
        self._syncing_button_color_controls = False
        self.panel_layout_settings = normalize_panel_layout_settings(panel_layout_settings)
        self.button_layout_settings = normalize_keep_reject_button_layout_settings(button_layout_settings)
        self.sync_zoom_pan = bool(sync_zoom_pan)
        self.scroll_wheel_pan_multi_enabled = bool(scroll_wheel_pan_multi_enabled)

        fallback_preset = KEEP_REJECT_BUTTON_PRESET_BY_ID.get(
            button_preset,
            KEEP_REJECT_BUTTON_PRESET_BY_ID[DEFAULT_KEEP_REJECT_PRESET_ID],
        )
        keep_color = normalize_keep_reject_button_color(
            keep_color,
            fallback_preset["keep"],
        )
        reject_color = normalize_keep_reject_button_color(
            reject_color,
            fallback_preset["reject"],
        )

        body_font = QtGui.QFont("Lucida Console", 12)
        hdr_font  = QtGui.QFont("Lucida Console", 14, QtGui.QFont.Bold)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)

        title = QtWidgets.QLabel("Choose panel grid")
        title.setFont(hdr_font)
        title.setStyleSheet(f"color: {self.theme['heading']};")
        lay.addWidget(title)

        note = QtWidgets.QLabel("Across = columns, Down = rows. (1–5 each)")
        note.setFont(body_font)
        lay.addWidget(note)

        form = QtWidgets.QFormLayout()
        form.setLabelAlignment(QtCore.Qt.AlignRight)

        self.cols_spin = QtWidgets.QSpinBox()
        self.cols_spin.setRange(1, 7)
        self.cols_spin.setValue(int(max(1, min(7, cols))))
        self.cols_spin.setFont(body_font)

        self.rows_spin = QtWidgets.QSpinBox()
        self.rows_spin.setRange(1, 7)
        self.rows_spin.setValue(int(max(1, min(7, rows))))
        self.rows_spin.setFont(body_font)

        form.addRow("Across (columns):", self.cols_spin)
        form.addRow("Down (rows):", self.rows_spin)
        lay.addLayout(form)

        preview_title = QtWidgets.QLabel("Preview")
        preview_title.setFont(body_font)
        lay.addWidget(preview_title)

        self.preview = PanelLayoutPreview(self.theme, cols=cols, rows=rows, parent=self)
        lay.addWidget(self.preview)

        self.preview_value = QtWidgets.QLabel("")
        self.preview_value.setFont(body_font)
        self.preview_value.setAlignment(QtCore.Qt.AlignCenter)
        lay.addWidget(self.preview_value)

        advanced_row = QtWidgets.QHBoxLayout()
        self.advanced_button = QtWidgets.QPushButton("Advanced...")
        self.advanced_button.setFont(body_font)
        self.advanced_button.setAutoDefault(False)
        self.advanced_button.setDefault(False)
        self.advanced_button.clicked.connect(self._open_advanced_layout_dialog)
        advanced_row.addWidget(self.advanced_button, 0, QtCore.Qt.AlignLeft)
        advanced_row.addStretch(1)
        lay.addLayout(advanced_row)

        self.advanced_summary = QtWidgets.QLabel("")
        self.advanced_summary.setWordWrap(True)
        self.advanced_summary.setFont(body_font)
        self.advanced_summary.setStyleSheet(f"color: {self.theme['muted']};")
        lay.addWidget(self.advanced_summary)
        self.advanced_summary.hide()

        color_group = QtWidgets.QGroupBox("Keep / Reject button colors")
        color_group.setFont(body_font)
        color_form = QtWidgets.QFormLayout(color_group)
        color_form.setLabelAlignment(QtCore.Qt.AlignRight)
        color_form.setFormAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop)
        color_form.setHorizontalSpacing(12)
        color_form.setVerticalSpacing(8)
        lay.addWidget(color_group)

        color_note = QtWidgets.QLabel(
            "Colorblind safe combinations: Prot-Deut-Trit-Dtnm."
        )
        color_note.setWordWrap(True)
        color_note.setFont(body_font)
        color_note.setStyleSheet(f"color: {self.theme['muted']};")
        color_form.addRow(color_note)

        self.button_preset_combo = QtWidgets.QComboBox()
        self.button_preset_combo.setFont(body_font)
        for preset in KEEP_REJECT_BUTTON_PRESETS:
            self.button_preset_combo.addItem(preset["label"], preset["id"])
        self.button_preset_combo.addItem("Custom", CUSTOM_KEEP_REJECT_PRESET_ID)
        color_form.addRow("Preset:", self.button_preset_combo)

        self.keep_color_combo = QtWidgets.QComboBox()
        self.keep_color_combo.setFont(body_font)
        self.keep_color_combo.setMinimumWidth(220)
        self._populate_color_combo(self.keep_color_combo)
        color_form.addRow("Keep color:", self.keep_color_combo)

        self.reject_color_combo = QtWidgets.QComboBox()
        self.reject_color_combo.setFont(body_font)
        self.reject_color_combo.setMinimumWidth(220)
        self._populate_color_combo(self.reject_color_combo)
        color_form.addRow("Reject color:", self.reject_color_combo)

        preview_box = QtWidgets.QWidget(self)
        self.button_preview_row = QtWidgets.QHBoxLayout(preview_box)
        self.button_preview_row.setContentsMargins(0, 0, 0, 0)
        self.button_preview_row.setSpacing(8)

        self.keep_preview = QtWidgets.QPushButton("Keep")
        self.keep_preview.setFont(body_font)
        self.keep_preview.setMinimumWidth(110)
        self.keep_preview.setFocusPolicy(QtCore.Qt.NoFocus)
        self.keep_preview.clicked.connect(lambda: None)
        self.button_preview_row.addWidget(self.keep_preview)

        self.reject_preview = QtWidgets.QPushButton("Reject")
        self.reject_preview.setFont(body_font)
        self.reject_preview.setMinimumWidth(110)
        self.reject_preview.setFocusPolicy(QtCore.Qt.NoFocus)
        self.reject_preview.clicked.connect(lambda: None)
        self.button_preview_row.addWidget(self.reject_preview)
        self.button_preview_row.addStretch(1)

        color_form.addRow("Preview:", preview_box)

        button_layout_box = QtWidgets.QWidget(self)
        button_layout_row = QtWidgets.QHBoxLayout(button_layout_box)
        button_layout_row.setContentsMargins(0, 0, 0, 0)
        button_layout_row.setSpacing(8)

        button_size_label = QtWidgets.QLabel("Size:")
        button_size_label.setFont(body_font)
        button_layout_row.addWidget(button_size_label)

        self.button_size_spin = QtWidgets.QDoubleSpinBox(self)
        self.button_size_spin.setDecimals(0)
        self.button_size_spin.setRange(
            _KEEP_REJECT_BUTTON_LAYOUT_BOUNDS["size_scale"][0] * 100.0,
            _KEEP_REJECT_BUTTON_LAYOUT_BOUNDS["size_scale"][1] * 100.0,
        )
        self.button_size_spin.setSingleStep(5.0)
        self.button_size_spin.setSuffix("%")
        self.button_size_spin.setFont(body_font)
        self.button_size_spin.setValue(self.button_layout_settings["size_scale"] * 100.0)
        button_layout_row.addWidget(self.button_size_spin)

        button_layout_row.addSpacing(12)

        button_spacing_label = QtWidgets.QLabel("Spacing:")
        button_spacing_label.setFont(body_font)
        button_layout_row.addWidget(button_spacing_label)

        self.button_spacing_spin = QtWidgets.QDoubleSpinBox(self)
        self.button_spacing_spin.setDecimals(0)
        self.button_spacing_spin.setRange(*_KEEP_REJECT_BUTTON_LAYOUT_BOUNDS["spacing_px"])
        self.button_spacing_spin.setSingleStep(2.0)
        self.button_spacing_spin.setSuffix(" px")
        self.button_spacing_spin.setFont(body_font)
        self.button_spacing_spin.setValue(self.button_layout_settings["spacing_px"])
        button_layout_row.addWidget(self.button_spacing_spin)
        button_layout_row.addStretch(1)
        color_form.addRow("", button_layout_box)

        self.cols_spin.valueChanged.connect(self._update_preview)
        self.rows_spin.valueChanged.connect(self._update_preview)
        self.button_preset_combo.currentIndexChanged.connect(self._on_button_preset_changed)
        self.keep_color_combo.currentIndexChanged.connect(self._on_button_color_changed)
        self.reject_color_combo.currentIndexChanged.connect(self._on_button_color_changed)
        self.button_size_spin.valueChanged.connect(self._on_button_layout_changed)
        self.button_spacing_spin.valueChanged.connect(self._on_button_layout_changed)

        self._set_color_combo_value(self.keep_color_combo, keep_color)
        self._set_color_combo_value(self.reject_color_combo, reject_color)
        self._sync_preset_combo_from_colors()
        self._update_button_preview()
        self._update_preview()

        btns = QtWidgets.QHBoxLayout()
        btns.addStretch(1)

        ok = QtWidgets.QPushButton("OK")
        ok.setFont(body_font)
        ok.setDefault(True)
        ok.clicked.connect(self.accept)

        cancel = QtWidgets.QPushButton("Cancel")
        cancel.setFont(body_font)
        cancel.setAutoDefault(False)
        cancel.setDefault(False)
        cancel.clicked.connect(self.reject)

        btns.addWidget(ok)
        btns.addWidget(cancel)
        lay.addLayout(btns)

    def _update_preview(self):
        cols = int(self.cols_spin.value())
        rows = int(self.rows_spin.value())
        self.preview.set_grid(cols, rows)
        self.preview_value.setText(f"{cols} x {rows}")

    def _open_advanced_layout_dialog(self):
        dlg = PanelLayoutAdvancedDialog(
            self.panel_layout_settings,
            sync_zoom_pan=self.sync_zoom_pan,
            scroll_wheel_pan_multi_enabled=self.scroll_wheel_pan_multi_enabled,
            parent=self,
        )
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            vals = dlg.values()
            self.panel_layout_settings = normalize_panel_layout_settings(
                vals.get("panel_layout_settings")
            )
            self.sync_zoom_pan = bool(vals.get("sync_zoom_pan", self.sync_zoom_pan))
            self.scroll_wheel_pan_multi_enabled = bool(
                vals.get(
                    "scroll_wheel_pan_multi_enabled",
                    self.scroll_wheel_pan_multi_enabled,
                )
            )

    def _populate_color_combo(self, combo):
        for label, color in KEEP_REJECT_BUTTON_COLOR_OPTIONS:
            combo.addItem(f"{label} ({color.upper()})", normalize_keep_reject_button_color(color, color))

    def _set_color_combo_value(self, combo, color):
        target = normalize_keep_reject_button_color(color, "#6C757D")
        idx = combo.findData(target)
        if idx < 0:
            combo.addItem(f"Custom ({target.upper()})", target)
            idx = combo.count() - 1
        combo.setCurrentIndex(idx)

    def _set_preset_combo_value(self, preset_id):
        idx = self.button_preset_combo.findData(preset_id)
        if idx < 0:
            idx = self.button_preset_combo.findData(CUSTOM_KEEP_REJECT_PRESET_ID)
        old_flag = self._syncing_button_color_controls
        self._syncing_button_color_controls = True
        self.button_preset_combo.setCurrentIndex(max(0, idx))
        self._syncing_button_color_controls = old_flag

    def _sync_preset_combo_from_colors(self):
        preset_id = infer_keep_reject_button_preset(
            self.keep_color_combo.currentData(),
            self.reject_color_combo.currentData(),
        )
        self._set_preset_combo_value(preset_id)

    def _update_button_preview(self):
        keep_style = build_keep_reject_button_style(self.keep_color_combo.currentData())
        reject_style = build_keep_reject_button_style(self.reject_color_combo.currentData())

        self.keep_preview.setStyleSheet(
            f"background-color: {keep_style['base']}; color: {keep_style['text']}; "
            f"border: 1px solid {keep_style['edge']}; border-radius: 8px; padding: 6px 12px;"
        )
        self.reject_preview.setStyleSheet(
            f"background-color: {reject_style['base']}; color: {reject_style['text']}; "
            f"border: 1px solid {reject_style['edge']}; border-radius: 8px; padding: 6px 12px;"
        )
        for btn in (self.keep_preview, self.reject_preview):
            btn.setFont(QtGui.QFont("Lucida Console", 12))
            btn.setMinimumWidth(110)
            btn.setMaximumSize(16777215, 16777215)
        self.button_preview_row.setSpacing(8)

    def _on_button_preset_changed(self):
        if self._syncing_button_color_controls:
            return
        preset_id = self.button_preset_combo.currentData()
        if preset_id not in KEEP_REJECT_BUTTON_PRESET_BY_ID:
            self._update_button_preview()
            return

        preset = KEEP_REJECT_BUTTON_PRESET_BY_ID[preset_id]
        self._syncing_button_color_controls = True
        self._set_color_combo_value(self.keep_color_combo, preset["keep"])
        self._set_color_combo_value(self.reject_color_combo, preset["reject"])
        self._syncing_button_color_controls = False
        self._update_button_preview()

    def _on_button_color_changed(self):
        if self._syncing_button_color_controls:
            return
        self._sync_preset_combo_from_colors()
        self._update_button_preview()

    def _current_button_layout_settings(self):
        return normalize_keep_reject_button_layout_settings({
            "size_scale": float(self.button_size_spin.value()) / 100.0,
            "spacing_px": float(self.button_spacing_spin.value()),
        })

    def _on_button_layout_changed(self):
        self.button_layout_settings = self._current_button_layout_settings()
        self._update_button_preview()

    def values(self):
        return {
            "cols": int(self.cols_spin.value()),
            "rows": int(self.rows_spin.value()),
            "panel_layout_settings": dict(self.panel_layout_settings),
            "button_layout_settings": dict(self.button_layout_settings),
            "sync_zoom_pan": bool(self.sync_zoom_pan),
            "scroll_wheel_pan_multi_enabled": bool(self.scroll_wheel_pan_multi_enabled),
            "button_colors": {
                "preset": self.button_preset_combo.currentData(),
                "keep": self.keep_color_combo.currentData(),
                "reject": self.reject_color_combo.currentData(),
            },
        }

class ColormapPickerDialog(QtWidgets.QDialog):
    """
    Tab key: choose any Matplotlib colormap from a big list,
    and adjust panel, footer, and loaded vector styling.

    - Type in the filter box to narrow the list.
    - Double-click a colormap name to accept.
    """
    def __init__(
        self,
        cmap_names,
        current="gray",
        vector_items=None,
        available_shapefile_paths=None,
        available_basemap_paths=None,
        current_basemap_path="",
        current_basemap_mode="nearest",
        current_basemap_category="",
        current_basemap_resolution_mode="dynamic",
        current_basemap_cmap="gray",
        current_basemap_color_scaling="normal",
        max_overlay_slots=5,
        nan_color="black",
        use_theme_nan_color=True,
        shapefile_linewidth=1.2,
        summary_fontsize=11.0,
        warp_source_color=None,
        warp_target_color=None,
        thermal_visual_resampling="nearest",
        basemap_visual_resampling="nearest",
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Display options")
        self.setModal(True)
        self.setProperty(GEOVIEWER_CENTER_ON_SHOW_PROPERTY, True)

        self.theme_mode = get_app_theme_mode()
        self.theme = build_theme_palette(self.theme_mode)
        self.setStyleSheet(f"""
            QDialog {{ background-color: {self.theme['window_bg']}; color: {self.theme['text']}; }}
            QLabel  {{ color: {self.theme['text']}; }}
            QLineEdit {{
                background-color: {self.theme['input_bg']}; color: {self.theme['text']};
                border: 1px solid {self.theme['border']}; border-radius: 6px;
                padding: 6px 10px;
            }}
            QListWidget {{
                background-color: {self.theme['list_bg']}; color: {self.theme['text']};
                border: 1px solid {self.theme['border']}; border-radius: 6px;
            }}
            QListWidget::item:selected {{ background-color: {self.theme['selection_bg']}; color: {self.theme['selection_text']}; }}
            QComboBox {{
                background-color: {self.theme['input_bg']}; color: {self.theme['text']};
                border: 1px solid {self.theme['border']}; border-radius: 6px;
                padding: 4px 8px; min-height: 28px;
            }}
            QComboBox QAbstractItemView {{
                background-color: {self.theme['list_bg']}; color: {self.theme['text']};
                selection-background-color: {self.theme['selection_bg']}; selection-color: {self.theme['selection_text']};
                border: 1px solid {self.theme['border']};
            }}
            QDoubleSpinBox {{
                background-color: {self.theme['input_bg']}; color: {self.theme['text']};
                border: 1px solid {self.theme['border']}; border-radius: 6px;
                padding: 4px 8px; min-height: 28px;
            }}
            QCheckBox {{
                color: {self.theme['text']};
                spacing: 6px;
            }}
            QGroupBox {{
                color: {self.theme['text']}; font-weight: 600;
                border: 1px solid {self.theme['border']}; border-radius: 8px;
                margin-top: 12px; padding: 10px 10px 8px 10px;
                background-color: {self.theme['group_bg']};
            }}
            QGroupBox::title {{
                subcontrol-origin: margin; left: 10px; top: 0px;
                padding: 0 4px; background-color: {self.theme['window_bg']}; color: {self.theme['heading']};
            }}
            QPushButton {{
                background-color: {self.theme['button_bg']}; color: {self.theme['button_text']};
                border: 1px solid {self.theme['border']}; border-radius: 8px;
                padding: 6px 14px;
            }}
            QPushButton:hover  {{ background-color: {self.theme['button_hover']}; }}
            QPushButton:pressed{{ background-color: {self.theme['button_pressed']}; }}
        """)

        body_font = QtGui.QFont("Lucida Console", 12)
        hdr_font  = QtGui.QFont("Lucida Console", 14, QtGui.QFont.Bold)

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(10)

        title = QtWidgets.QLabel("Display options")
        title.setFont(hdr_font)
        title.setStyleSheet(f"color: {self.theme['heading']};")
        outer.addWidget(title)

        cmap_group = QtWidgets.QGroupBox("Colormap")
        cmap_group.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        cmap_layout = QtWidgets.QVBoxLayout(cmap_group)
        cmap_layout.setContentsMargins(10, 12, 10, 10)
        cmap_layout.setSpacing(8)
        outer.addWidget(cmap_group)

        self.filter_edit = QtWidgets.QLineEdit()
        self.filter_edit.setFont(body_font)
        self.filter_edit.setPlaceholderText("Filter colormaps… (type to search)")
        cmap_layout.addWidget(self.filter_edit)

        self.listw = QtWidgets.QListWidget()
        self.listw.setFont(body_font)
        self.listw.setMinimumWidth(520)
        self.listw.setMinimumHeight(120)
        self.listw.setMaximumHeight(280)
        self.listw.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Fixed,
        )
        cmap_layout.addWidget(self.listw)

        self._all_names = list(cmap_names) if cmap_names is not None else []
        for nm in self._all_names:
            self.listw.addItem(nm)

        try:
            matches = self.listw.findItems(str(current), QtCore.Qt.MatchExactly)
            if matches:
                self.listw.setCurrentItem(matches[0])
                self.listw.scrollToItem(matches[0])
            elif self.listw.count() > 0:
                self.listw.setCurrentRow(0)
        except Exception:
            pass

        self.filter_edit.textChanged.connect(self._apply_filter)
        self.listw.itemDoubleClicked.connect(lambda _item: self.accept())

        display_group = QtWidgets.QGroupBox("Panel + footer styling")
        display_group.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        display_layout = QtWidgets.QGridLayout(display_group)
        display_layout.setHorizontalSpacing(10)
        display_layout.setVerticalSpacing(6)
        outer.addWidget(display_group)

        self.nan_default_check = QtWidgets.QCheckBox("Use dark/light default")
        self.nan_default_check.setFont(body_font)
        self.nan_default_check.setChecked(bool(use_theme_nan_color))

        self.nan_combo = QtWidgets.QComboBox()
        self.nan_combo.setFont(body_font)
        nan_color = normalize_nan_override_value(nan_color)
        nan_choices = list(PICKER_COLORS)
        if nan_color not in nan_choices:
            nan_choices.append(nan_color)
        for choice in nan_choices:
            self.nan_combo.addItem(nan_override_display_label(choice), normalize_nan_override_value(choice))
        for label, token, _alpha in NAN_TRANSPARENT_OPTIONS:
            if self.nan_combo.findData(token) < 0:
                self.nan_combo.addItem(label, token)
        nan_idx = self.nan_combo.findData(nan_color)
        self.nan_combo.setCurrentIndex(nan_idx if nan_idx >= 0 else 0)
        self.nan_color_preview = QtWidgets.QLabel("")
        self.nan_color_preview.setFont(body_font)
        self.nan_color_preview.setAlignment(QtCore.Qt.AlignCenter)
        self.nan_color_preview.setFixedSize(38, 24)

        self.shp_width_spin = QtWidgets.QDoubleSpinBox()
        self.shp_width_spin.setFont(body_font)
        self.shp_width_spin.setDecimals(1)
        self.shp_width_spin.setRange(0.1, 12.0)
        self.shp_width_spin.setSingleStep(0.1)
        self.shp_width_spin.setValue(float(shapefile_linewidth))

        self.summary_font_spin = QtWidgets.QDoubleSpinBox()
        self.summary_font_spin.setFont(body_font)
        self.summary_font_spin.setDecimals(1)
        self.summary_font_spin.setRange(6.0, 36.0)
        self.summary_font_spin.setSingleStep(1.0)
        self.summary_font_spin.setValue(float(summary_fontsize))

        display_widgets = [
            ("NaN default", self.nan_default_check),
            ("NaN override", self.nan_combo),
            ("SHP line width", self.shp_width_spin),
            ("Footer font size", self.summary_font_spin),
        ]
        for row, (label_text, widget) in enumerate(display_widgets):
            lab = QtWidgets.QLabel(label_text)
            lab.setFont(body_font)
            display_layout.addWidget(lab, row, 0)
            display_layout.addWidget(widget, row, 1)
        display_layout.addWidget(self.nan_color_preview, 1, 2)

        self.nan_default_check.toggled.connect(self._sync_nan_override_enabled)
        self.nan_combo.currentIndexChanged.connect(self._update_nan_color_preview)
        self._update_nan_color_preview()
        self._sync_nan_override_enabled(self.nan_default_check.isChecked())

        warp_group = QtWidgets.QGroupBox("Warp point colors")
        warp_group.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        warp_layout = QtWidgets.QGridLayout(warp_group)
        warp_layout.setHorizontalSpacing(10)
        warp_layout.setVerticalSpacing(6)
        outer.addWidget(warp_group)

        self.warp_source_combo = QtWidgets.QComboBox()
        self.warp_source_combo.setFont(body_font)
        self._populate_warp_color_combo(
            self.warp_source_combo,
            warp_source_color,
            DEFAULT_WARP_SOURCE_COLOR,
        )

        self.warp_target_combo = QtWidgets.QComboBox()
        self.warp_target_combo.setFont(body_font)
        self._populate_warp_color_combo(
            self.warp_target_combo,
            warp_target_color,
            DEFAULT_WARP_TARGET_COLOR,
        )

        self.warp_source_preview = QtWidgets.QLabel("")
        self.warp_target_preview = QtWidgets.QLabel("")
        for preview in (self.warp_source_preview, self.warp_target_preview):
            preview.setFont(body_font)
            preview.setAlignment(QtCore.Qt.AlignCenter)
            preview.setFixedSize(38, 24)

        source_lab = QtWidgets.QLabel("Source")
        source_lab.setFont(body_font)
        target_lab = QtWidgets.QLabel("Target")
        target_lab.setFont(body_font)
        warp_layout.addWidget(source_lab, 0, 0)
        warp_layout.addWidget(self.warp_source_combo, 0, 1)
        warp_layout.addWidget(self.warp_source_preview, 0, 2)
        warp_layout.addWidget(target_lab, 1, 0)
        warp_layout.addWidget(self.warp_target_combo, 1, 1)
        warp_layout.addWidget(self.warp_target_preview, 1, 2)
        self.warp_source_combo.currentIndexChanged.connect(self._update_warp_preview)
        self.warp_target_combo.currentIndexChanged.connect(self._update_warp_preview)
        self._update_warp_preview()

        self._vector_body_font = body_font
        self._vector_items = list(vector_items or [])
        self._pending_shapefile_loads = []
        self._available_shapefile_paths = list(available_shapefile_paths or [])
        self._available_basemap_paths = list(available_basemap_paths or [])
        self._selected_basemap_path = str(current_basemap_path or "").strip()
        self._selected_basemap_mode = normalize_basemap_mode(
            current_basemap_mode,
            "single" if self._selected_basemap_path else "nearest",
        )
        self._selected_basemap_category = normalize_basemap_category(current_basemap_category)
        if not self._selected_basemap_category and self._selected_basemap_path:
            self._selected_basemap_category = normalize_basemap_category(
                basemap_category_for_path(self._selected_basemap_path)
            )
        if not self._selected_basemap_category:
            categories = basemap_categories_from_paths(self._available_basemap_paths)
            if categories:
                self._selected_basemap_category = categories[0]
        self._selected_basemap_resolution_mode = normalize_basemap_resolution_mode(
            current_basemap_resolution_mode,
            "dynamic",
        )
        self._selected_basemap_cmap = normalize_basemap_cmap(current_basemap_cmap, "gray")
        self._selected_basemap_color_scaling = normalize_basemap_color_scaling(
            current_basemap_color_scaling,
            "normal",
        )
        self._max_overlay_slots = max(0, int(max_overlay_slots))
        self._vector_status_message = ""
        self.vector_combos = []

        self.vector_group = QtWidgets.QGroupBox("Loaded vector colors")
        self.vector_group.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Fixed,
        )
        self.vector_layout = QtWidgets.QGridLayout(self.vector_group)
        self.vector_layout.setHorizontalSpacing(10)
        self.vector_layout.setVerticalSpacing(6)
        self.vector_layout.setColumnStretch(1, 1)
        self.vector_layout.setColumnStretch(3, 0)
        outer.addWidget(self.vector_group)

        resampling_group = QtWidgets.QGroupBox("Visual Resampling")
        resampling_group.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        resampling_layout = QtWidgets.QGridLayout(resampling_group)
        resampling_layout.setHorizontalSpacing(10)
        resampling_layout.setVerticalSpacing(6)
        outer.addWidget(resampling_group)

        self.thermal_resampling_combo = QtWidgets.QComboBox()
        self.thermal_resampling_combo.setFont(body_font)
        self._populate_visual_resampling_combo(
            self.thermal_resampling_combo,
            thermal_visual_resampling,
        )
        self.basemap_resampling_combo = QtWidgets.QComboBox()
        self.basemap_resampling_combo.setFont(body_font)
        self._populate_visual_resampling_combo(
            self.basemap_resampling_combo,
            basemap_visual_resampling,
        )

        thermal_resampling_lab = QtWidgets.QLabel("Thermal TIF")
        thermal_resampling_lab.setFont(body_font)
        basemap_resampling_lab = QtWidgets.QLabel("Basemap")
        basemap_resampling_lab.setFont(body_font)
        resampling_layout.addWidget(thermal_resampling_lab, 0, 0)
        resampling_layout.addWidget(self.thermal_resampling_combo, 0, 1)
        resampling_layout.addWidget(basemap_resampling_lab, 1, 0)
        resampling_layout.addWidget(self.basemap_resampling_combo, 1, 1)
        resampling_layout.setColumnStretch(1, 1)

        self.load_shapefiles_btn = QtWidgets.QPushButton("Load Shapefiles / Basemaps")
        self.load_shapefiles_btn.setFont(body_font)
        self.load_shapefiles_btn.setAutoDefault(False)
        self.load_shapefiles_btn.setDefault(False)
        self.load_shapefiles_btn.clicked.connect(self._open_shapefile_loader)
        outer.addWidget(self.load_shapefiles_btn, 0, QtCore.Qt.AlignLeft)

        self.load_status_label = QtWidgets.QLabel("")
        self.load_status_label.setFont(body_font)
        self.load_status_label.setWordWrap(True)
        self.load_status_label.setStyleSheet(f"color: {self.theme['muted']};")
        outer.addWidget(self.load_status_label)

        self._refresh_vector_controls()
        self._screen_limit_timer = QtCore.QTimer(self)
        self._screen_limit_timer.setSingleShot(True)
        self._screen_limit_timer.timeout.connect(self._limit_to_screen)

        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        use_button = btns.button(QtWidgets.QDialogButtonBox.Ok)
        use_button.setText("Use")
        use_button.setDefault(True)
        cancel_button = btns.button(QtWidgets.QDialogButtonBox.Cancel)
        cancel_button.setText("Cancel")
        cancel_button.setAutoDefault(False)
        cancel_button.setDefault(False)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        outer.addWidget(btns)

        self._use_enter_shortcuts = []
        for key in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
            shortcut = QtWidgets.QShortcut(QtGui.QKeySequence(key), self)
            shortcut.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(self._accept_from_enter)
            self._use_enter_shortcuts.append(shortcut)
        self._install_enter_accept_filter()

        self._limit_to_screen()
        self.filter_edit.setFocus()

    def _install_enter_accept_filter(self):
        self.installEventFilter(self)
        for child in self.findChildren(QtCore.QObject):
            try:
                child.installEventFilter(self)
            except Exception:
                pass

    def _accept_from_enter(self):
        self.accept()

    def eventFilter(self, obj, event):
        if (
            event.type() == QtCore.QEvent.KeyPress
            and event.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter)
        ):
            self._accept_from_enter()
            event.accept()
            return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event):
        if event.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
            self._accept_from_enter()
            event.accept()
            return
        super().keyPressEvent(event)

    def _combo_color_value(self, combo, fallback):
        value = combo.currentData()
        if value is None:
            value = combo.currentText()
        return normalize_keep_reject_button_color(value, fallback)

    def _populate_warp_color_combo(self, combo, current_color, fallback):
        target = normalize_keep_reject_button_color(current_color, fallback)
        for label, color in KEEP_REJECT_BUTTON_COLOR_OPTIONS:
            normalized = normalize_keep_reject_button_color(color, color)
            combo.addItem(f"{label} ({normalized.upper()})", normalized)
        if combo.findData(target) < 0:
            combo.addItem(target.upper(), target)
        idx = combo.findData(target)
        combo.setCurrentIndex(idx if idx >= 0 else 0)

    def _populate_visual_resampling_combo(self, combo, current_value):
        target = normalize_visual_resampling(current_value, "nearest")
        for label, token in VISUAL_RESAMPLING_OPTIONS:
            combo.addItem(label, token)
        idx = combo.findData(target)
        combo.setCurrentIndex(idx if idx >= 0 else 0)

    def _style_warp_preview_label(self, label, color):
        style = build_keep_reject_button_style(color)
        label.setText("")
        label.setStyleSheet(
            f"background-color: {style['base']}; color: {style['text']}; "
            f"border: 1px solid {style['edge']}; border-radius: 3px;"
        )

    def _update_nan_color_preview(self):
        value = normalize_nan_override_value(self.nan_combo.currentData() or self.nan_combo.currentText())
        alpha = _nan_transparency_values().get(value)
        if alpha is not None:
            pct = int(round((1.0 - float(alpha)) * 100.0))
            self.nan_color_preview.setText(f"{pct}%")
            self.nan_color_preview.setStyleSheet(
                f"background-color: rgba(128, 128, 128, {int(float(alpha) * 255)}); "
                f"color: {self.theme['text']}; border: 1px solid {self.theme['border']}; "
                "border-radius: 3px; font-size: 9pt;"
            )
            return
        self.nan_color_preview.setText("")
        self._style_warp_preview_label(self.nan_color_preview, normalize_picker_color_name(value, "black"))

    def _update_warp_preview(self):
        source_color = self._combo_color_value(self.warp_source_combo, DEFAULT_WARP_SOURCE_COLOR)
        target_color = self._combo_color_value(self.warp_target_combo, DEFAULT_WARP_TARGET_COLOR)
        self._style_warp_preview_label(self.warp_source_preview, source_color)
        self._style_warp_preview_label(self.warp_target_preview, target_color)

    def _apply_filter(self, txt):
        s = (txt or "").strip().lower()
        for i in range(self.listw.count()):
            it = self.listw.item(i)
            it.setHidden(bool(s) and (s not in it.text().lower()))

    def selected_cmap(self):
        it = self.listw.currentItem()
        return it.text().strip() if it is not None else ""

    def selected_vector_items(self):
        items = []
        for item in self.vector_combos:
            items.append({
                "label": item["label"],
                "color": normalize_picker_color_name(item["combo"].currentText(), item.get("color", "dodgerblue")),
                "pending": bool(item.get("pending")),
                "slot_kind": item.get("slot_kind", "overlay"),
                "path": item.get("path", ""),
                "name": item.get("name", ""),
                "source_id": item.get("source_id"),
            })
        return items

    def selected_vector_colors(self):
        return [
            (item["label"], item["color"])
            for item in self.selected_vector_items()
        ]

    def selected_shapefile_loads(self):
        loads = []
        for item in self.selected_vector_items():
            if not item.get("pending"):
                continue
            loads.append({
                "slot_kind": item.get("slot_kind", "overlay"),
                "path": item.get("path", ""),
                "name": item.get("name", ""),
                "color": item.get("color", "dodgerblue"),
            })
        return loads

    def selected_basemap_path(self):
        return str(getattr(self, "_selected_basemap_path", "") or "").strip()

    def selected_basemap_mode(self):
        return normalize_basemap_mode(
            getattr(self, "_selected_basemap_mode", "nearest"),
            "nearest",
        )

    def selected_basemap_category(self):
        return normalize_basemap_category(
            getattr(self, "_selected_basemap_category", "")
        )

    def selected_basemap_resolution_mode(self):
        return normalize_basemap_resolution_mode(
            getattr(self, "_selected_basemap_resolution_mode", "dynamic"),
            "dynamic",
        )

    def selected_basemap_color_scaling(self):
        return normalize_basemap_color_scaling(
            getattr(self, "_selected_basemap_color_scaling", "normal"),
            "normal",
        )

    def selected_basemap_cmap(self):
        return normalize_basemap_cmap(
            getattr(self, "_selected_basemap_cmap", "gray"),
            "gray",
        )

    def selected_nan_color(self):
        return normalize_nan_override_value(self.nan_combo.currentData() or self.nan_combo.currentText())

    def selected_use_theme_nan_color(self):
        return bool(self.nan_default_check.isChecked())

    def selected_shapefile_linewidth(self):
        return float(self.shp_width_spin.value())

    def selected_summary_fontsize(self):
        return float(self.summary_font_spin.value())

    def selected_warp_source_color(self):
        return self._combo_color_value(self.warp_source_combo, DEFAULT_WARP_SOURCE_COLOR)

    def selected_warp_target_color(self):
        return self._combo_color_value(self.warp_target_combo, DEFAULT_WARP_TARGET_COLOR)

    def selected_thermal_visual_resampling(self):
        return normalize_visual_resampling(
            self.thermal_resampling_combo.currentData() or self.thermal_resampling_combo.currentText(),
            "nearest",
        )

    def selected_basemap_visual_resampling(self):
        return normalize_visual_resampling(
            self.basemap_resampling_combo.currentData() or self.basemap_resampling_combo.currentText(),
            "nearest",
        )

    def _sync_nan_override_enabled(self, use_default):
        self.nan_combo.setEnabled(not bool(use_default))

    def showEvent(self, event):
        super().showEvent(event)
        self._limit_to_screen()

    def _clear_layout_widgets(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            child_layout = item.layout()
            widget = item.widget()
            if child_layout is not None:
                self._clear_layout_widgets(child_layout)
            if widget is not None:
                widget.deleteLater()

    def _combined_vector_items(self):
        return list(self._vector_items) + list(self._pending_shapefile_loads)

    def _used_shapefile_names(self):
        used = set()
        for item in self._combined_vector_items():
            name = normalize_persisted_shapefile_name(item.get("name"))
            if name:
                used.add(name.lower())
        return used

    def _available_shapefile_paths_for_loading(self):
        used_names = self._used_shapefile_names()
        available = []
        seen = set()
        for path in self._available_shapefile_paths:
            raw_path = str(path or "").strip()
            if not raw_path:
                continue
            norm_name = normalize_persisted_shapefile_name(raw_path)
            if norm_name and norm_name.lower() in used_names:
                continue
            path_key = os.path.abspath(raw_path).lower()
            if path_key in seen:
                continue
            seen.add(path_key)
            available.append(raw_path)
        return available

    def _available_shapefile_slots(self):
        current_items = self._combined_vector_items()
        has_primary = any(str(item.get("slot_kind") or "").strip().lower() == "primary" for item in current_items)
        overlay_count = sum(
            1 for item in current_items
            if str(item.get("slot_kind") or "").strip().lower() == "overlay"
        )

        slots = []
        if not has_primary:
            slots.append({
                "slot_kind": "primary",
                "slot_label": "Primary",
                "default_color": "cyan",
            })
        for slot_index in range(overlay_count + 1, self._max_overlay_slots + 1):
            slots.append({
                "slot_kind": "overlay",
                "slot_label": f"Overlay {slot_index}",
                "default_color": "dodgerblue" if slot_index == 1 else "lime",
            })
        return slots

    def _overlay_display_index(self, item, fallback):
        source_id = str(item.get("source_id") or "")
        m = re.fullmatch(r"overlay:(\d+)", source_id.strip().lower())
        if m:
            return max(0, int(m.group(1)))

        label = str(item.get("label") or "")
        m = re.search(r"\boverlay\s+(\d+)\b", label, flags=re.IGNORECASE)
        if m:
            return max(0, int(m.group(1)) - 1)

        return max(0, int(fallback))

    def _ordered_vector_records(self):
        records = []
        for idx, item in enumerate(self._combined_vector_items()):
            records.append({
                "item": item,
                "combined_index": idx,
                "original_order": idx,
            })

        def _sort_key(record):
            item = record["item"]
            slot_kind = str(item.get("slot_kind") or "overlay").strip().lower()
            if slot_kind == "primary":
                return (0, 0, record["original_order"])
            overlay_idx = self._overlay_display_index(item, record["original_order"])
            return (1, overlay_idx, record["original_order"])

        return sorted(records, key=_sort_key)

    def _refresh_vector_controls(self):
        self.vector_group.setMinimumHeight(0)
        self._clear_layout_widgets(self.vector_layout)
        self.vector_combos = []

        vector_records = self._ordered_vector_records()
        row = 0

        if vector_records:
            for row, record in enumerate(vector_records):
                item = record["item"]
                item_index = int(record["combined_index"])
                label_text = str(item.get("label", f"Vector {item_index + 1}"))
                color_now = str(item.get("color", "cyan") or "cyan")
                lab = QtWidgets.QLabel(label_text)
                lab.setFont(self._vector_body_font)
                combo = QtWidgets.QComboBox()
                combo.setFont(self._vector_body_font)
                combo.setMinimumWidth(170)
                color_choices = list(PICKER_COLORS)
                if color_now not in color_choices:
                    color_choices.append(color_now)
                combo.addItems(color_choices)
                combo.setCurrentText(color_now)
                color_swatch = QtWidgets.QLabel("")
                color_swatch.setFont(self._vector_body_font)
                color_swatch.setAlignment(QtCore.Qt.AlignCenter)
                color_swatch.setFixedSize(38, 24)
                color_swatch.setToolTip(label_text)
                self._style_warp_preview_label(color_swatch, color_now)
                combo.currentIndexChanged.connect(
                    lambda _idx, swatch=color_swatch, color_combo=combo:
                    self._style_warp_preview_label(swatch, color_combo.currentText())
                )
                remove_btn = QtWidgets.QPushButton("X")
                remove_btn.setFont(self._vector_body_font)
                remove_btn.setFixedWidth(32)
                remove_btn.setToolTip(f"Remove {label_text}")
                remove_btn.setStyleSheet(
                    "QPushButton { background-color: #8B2D2D; color: #FFF6F4; "
                    "border: 1px solid #C23B22; border-radius: 8px; padding: 4px 0px; font-weight: 700; }"
                    "QPushButton:hover { background-color: #A63636; }"
                )
                remove_btn.clicked.connect(lambda _checked=False, idx=item_index: self._remove_vector_item(idx))
                self.vector_layout.addWidget(lab, row, 0)
                self.vector_layout.addWidget(combo, row, 1)
                self.vector_layout.addWidget(color_swatch, row, 2)
                self.vector_layout.addWidget(remove_btn, row, 3)
                self.vector_combos.append({
                    "label": label_text,
                    "combo": combo,
                    "pending": bool(item.get("pending")),
                    "slot_kind": item.get("slot_kind", "overlay"),
                    "path": item.get("path", ""),
                    "name": item.get("name", ""),
                    "color": color_now,
                    "source_id": item.get("source_id"),
                })
            row = len(vector_records)
        else:
            empty_label = QtWidgets.QLabel("No shapefiles currently loaded.")
            empty_label.setWordWrap(True)
            empty_label.setFont(self._vector_body_font)
            empty_label.setStyleSheet(f"color: {self.theme['muted']};")
            self.vector_layout.addWidget(empty_label, row, 0, 1, 4)
            row += 1

        if self._pending_shapefile_loads:
            pending_label = QtWidgets.QLabel("Queued shapefiles will be imported when you press Use.")
            pending_label.setWordWrap(True)
            pending_label.setFont(self._vector_body_font)
            pending_label.setStyleSheet(f"color: {self.theme['muted']};")
            self.vector_layout.addWidget(pending_label, row, 0, 1, 4)
            row += 1

        available_paths = self._available_shapefile_paths_for_loading()
        slot_specs = self._available_shapefile_slots()
        has_basemap_choices = bool(self._available_basemap_paths) or bool(self._selected_basemap_path)
        if not available_paths and not has_basemap_choices:
            self.load_shapefiles_btn.setToolTip("No unused shapefiles or basemaps were found.")
        elif not slot_specs and not has_basemap_choices:
            self.load_shapefiles_btn.setToolTip("All shapefile slots are already filled.")
        else:
            self.load_shapefiles_btn.setToolTip("")
        self._update_load_status_label()

        self.vector_group.setMinimumHeight(self.vector_group.sizeHint().height())

        if self.isVisible():
            self._screen_limit_timer.start(0)

    def _limit_to_screen(self):
        screen = self.screen()
        if screen is None:
            try:
                screen = QtWidgets.QApplication.screenAt(self.frameGeometry().center())
            except Exception:
                screen = None
        if screen is None:
            screen = QtWidgets.QApplication.primaryScreen()
        if screen is None:
            return

        available = screen.availableGeometry()
        max_width = max(620, min(available.width() - 40, int(round(available.width() * 0.90))))
        max_height = max(240, min(available.height() - 40, int(round(available.height() * 0.90))))
        self.setMaximumSize(max_width, max_height)

        try:
            target = self.sizeHint()
        except Exception:
            target = self.size()

        try:
            minimum = self.minimumSizeHint()
        except Exception:
            minimum = target
        target_width = min(max(target.width(), minimum.width(), 620), max_width)
        target_height = min(max(target.height(), minimum.height()), max_height)
        _with_ui_scale_override(1.0, lambda: self.resize(target_width, target_height))

    def _update_load_status_label(self):
        text = str(self._vector_status_message or "").strip()
        if (
            not text
            and not self._available_shapefile_paths_for_loading()
            and not self._available_basemap_paths
            and not self._selected_basemap_path
        ):
            text = "No more SHPs in folder, and no basemaps were found."
        elif not text and self._selected_basemap_mode == "nearest" and self._selected_basemap_category:
            text = f"Basemap: nearest {basemap_category_label(self._selected_basemap_category)} by date"
        elif not text and self._selected_basemap_path:
            text = f"Basemap: {os.path.basename(self._selected_basemap_path)}"
        self.load_status_label.setText(text)
        self.load_status_label.setVisible(bool(text))

    def _remove_vector_item(self, index):
        combined = self._combined_vector_items()
        if index < 0 or index >= len(combined):
            return
        existing_count = len(self._vector_items)
        if index < existing_count:
            del self._vector_items[index]
        else:
            del self._pending_shapefile_loads[index - existing_count]
        self._vector_status_message = ""
        self._refresh_vector_controls()

    def _open_shapefile_loader(self):
        available_paths = self._available_shapefile_paths_for_loading()
        slot_specs = self._available_shapefile_slots()
        has_basemap_choices = bool(self._available_basemap_paths) or bool(self._selected_basemap_path)
        if not available_paths and not has_basemap_choices:
            self._vector_status_message = "No more SHPs in folder, and no basemaps were found."
            self._update_load_status_label()
            return
        if not slot_specs and not has_basemap_choices:
            self._vector_status_message = "All shapefile slots are already filled."
            self._update_load_status_label()
            return

        self._vector_status_message = ""
        self._update_load_status_label()
        dlg = LoadShapefilesDialog(
            available_paths,
            slot_specs,
            available_basemap_paths=self._available_basemap_paths,
            current_basemap_path=self._selected_basemap_path,
            current_basemap_mode=self._selected_basemap_mode,
            current_basemap_category=self._selected_basemap_category,
            current_basemap_resolution_mode=self._selected_basemap_resolution_mode,
            basemap_cmap_names=self._all_names,
            current_basemap_cmap=self._selected_basemap_cmap,
            current_basemap_color_scaling=self._selected_basemap_color_scaling,
            parent=self,
        )
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return

        self._selected_basemap_path = str(dlg.selected_basemap_path() or "").strip()
        self._selected_basemap_mode = dlg.selected_basemap_mode()
        self._selected_basemap_category = dlg.selected_basemap_category()
        self._selected_basemap_resolution_mode = dlg.selected_basemap_resolution_mode()
        self._selected_basemap_cmap = dlg.selected_basemap_cmap()
        self._selected_basemap_color_scaling = dlg.selected_basemap_color_scaling()

        for item in dlg.selected_shapefile_loads():
            name = normalize_persisted_shapefile_name(item.get("name"))
            path = str(item.get("path") or "").strip()
            if not name or not path:
                continue
            self._pending_shapefile_loads.append({
                "label": f"{item.get('slot_label', 'Overlay')}: {name}",
                "color": normalize_picker_color_name(item.get("color"), "dodgerblue"),
                "name": name,
                "slot_kind": str(item.get("slot_kind") or "overlay"),
                "path": path,
                "pending": True,
                "source_id": None,
            })
        self._vector_status_message = ""
        self._refresh_vector_controls()


class LoadShapefilesDialog(QtWidgets.QDialog):
    def __init__(
        self,
        shp_paths,
        slot_specs,
        available_basemap_paths=None,
        current_basemap_path="",
        current_basemap_mode="nearest",
        current_basemap_category="",
        current_basemap_resolution_mode="dynamic",
        basemap_cmap_names=None,
        current_basemap_cmap="gray",
        current_basemap_color_scaling="normal",
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Load Shapefiles / Basemaps")
        self.setModal(True)

        base_font = self.font()
        base_font.setPointSize(12)
        self.setFont(base_font)

        self.theme_mode = get_app_theme_mode()
        self.theme = build_theme_palette(self.theme_mode)
        self.setStyleSheet(f"""
            QDialog {{ background-color: {self.theme['window_bg']}; color: {self.theme['text']}; }}
            QLabel {{ color: {self.theme['text']}; font-size: 12pt; }}
            QGroupBox {{
                color: {self.theme['text']}; font-weight: 600; font-size: 12pt;
                border: 1px solid {self.theme['border']}; border-radius: 8px;
                margin-top: 12px; padding: 10px 10px 8px 10px;
                background-color: {self.theme['group_bg']};
            }}
            QGroupBox::title {{
                subcontrol-origin: margin; left: 10px; top: 0px;
                padding: 0 4px; background-color: {self.theme['window_bg']}; color: {self.theme['heading']};
            }}
            QComboBox {{
                background-color: {self.theme['input_bg']}; color: {self.theme['text']};
                border: 1px solid {self.theme['border']}; border-radius: 6px;
                padding: 4px 8px; font-size: 12pt; min-height: 28px;
            }}
            QComboBox:disabled {{
                color: {self.theme['disabled_text']}; background-color: {self.theme['group_bg']}; border-color: {self.theme['border']};
            }}
            QComboBox QAbstractItemView {{
                background-color: {self.theme['list_bg']}; color: {self.theme['text']};
                selection-background-color: {self.theme['selection_bg']}; selection-color: {self.theme['selection_text']};
                border: 1px solid {self.theme['border']};
            }}
            QPushButton {{
                background-color: {self.theme['button_bg']}; color: {self.theme['button_text']};
                border: 1px solid {self.theme['border']}; border-radius: 8px;
                padding: 6px 12px; font-size: 11pt;
            }}
            QPushButton:hover {{ background-color: {self.theme['button_hover']}; }}
            QPushButton:pressed {{ background-color: {self.theme['button_pressed']}; }}
            QDialogButtonBox QPushButton {{ min-width: 90px; }}
        """)

        self._none_label = "— keep empty —"
        self._slot_specs = [dict(item) for item in list(slot_specs or [])]
        self._all_paths = []
        self._path_labels = {}
        for path in list(shp_paths or []):
            raw_path = str(path or "").strip()
            if not raw_path:
                continue
            name = os.path.basename(raw_path)
            epsg_str = "Unknown"
            try:
                gdf = gpd.read_file(raw_path)
                if gdf.crs:
                    epsg_val = gdf.crs.to_epsg()
                    epsg_str = f"EPSG:{epsg_val}" if epsg_val is not None else gdf.crs.to_string()
            except Exception:
                epsg_str = "Unreadable"
            self._all_paths.append(raw_path)
            self._path_labels[raw_path] = f"{name} — CRS: {epsg_str}"

        self._all_basemap_paths = []
        self._basemap_path_labels = {}
        current_basemap_resolution_mode = normalize_basemap_resolution_mode(
            current_basemap_resolution_mode,
            "dynamic",
        )
        current_basemap_cmap = normalize_basemap_cmap(current_basemap_cmap, "gray")
        current_basemap_color_scaling = normalize_basemap_color_scaling(
            current_basemap_color_scaling,
            "normal",
        )
        self._basemap_cmap_names = list(basemap_cmap_names or [])
        if current_basemap_cmap not in self._basemap_cmap_names:
            self._basemap_cmap_names.insert(0, current_basemap_cmap)
        if "gray" not in self._basemap_cmap_names:
            self._basemap_cmap_names.insert(0, "gray")
        current_basemap_path = os.path.abspath(str(current_basemap_path or "").strip()) if current_basemap_path else ""
        seen_basemaps = set()
        for path in list(available_basemap_paths or []) + ([current_basemap_path] if current_basemap_path else []):
            raw_path = str(path or "").strip()
            if not raw_path:
                continue
            abs_path = os.path.abspath(raw_path)
            key = os.path.normcase(abs_path)
            if key in seen_basemaps or not os.path.isfile(abs_path):
                continue
            seen_basemaps.add(key)
            self._all_basemap_paths.append(abs_path)
            self._basemap_path_labels[abs_path] = raster_choice_label(abs_path)
        self._basemap_categories = basemap_categories_from_paths(self._all_basemap_paths)
        current_basemap_mode = normalize_basemap_mode(
            current_basemap_mode,
            "single" if current_basemap_path else "nearest",
        )
        current_basemap_category = normalize_basemap_category(current_basemap_category)
        if not current_basemap_category and current_basemap_path:
            current_basemap_category = normalize_basemap_category(
                basemap_category_for_path(current_basemap_path)
            )
        if not current_basemap_category and self._basemap_categories:
            current_basemap_category = self._basemap_categories[0]

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        note_text = "Fill any empty shapefile slots and choose one basemap from the Basemaps folder."
        note = QtWidgets.QLabel(note_text)
        note.setWordWrap(False)
        note.setStyleSheet(f"color: {self.theme['muted']};")
        outer.addWidget(note)
        self._native_content_width = max(
            ui_px(520),
            int(QtGui.QFontMetrics(note.font()).horizontalAdvance(note_text)),
        )

        def compact_combo(combo, min_chars=18):
            try:
                combo.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
                combo.setMinimumContentsLength(max(4, int(min_chars)))
            except Exception:
                pass
            combo.setMinimumWidth(0)
            combo.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)

        basemap_group = QtWidgets.QGroupBox("Basemap", self)
        basemap_layout = QtWidgets.QGridLayout(basemap_group)
        basemap_layout.setHorizontalSpacing(10)
        basemap_layout.setVerticalSpacing(6)
        outer.addWidget(basemap_group)

        self.basemap_mode_combo = QtWidgets.QComboBox(basemap_group)
        compact_combo(self.basemap_mode_combo, 24)
        for label, token in BASEMAP_MODE_OPTIONS:
            self.basemap_mode_combo.addItem(label, token)
        mode_idx = self.basemap_mode_combo.findData(current_basemap_mode)
        self.basemap_mode_combo.setCurrentIndex(mode_idx if mode_idx >= 0 else 0)

        self.basemap_category_combo = QtWidgets.QComboBox(basemap_group)
        compact_combo(self.basemap_category_combo, 24)
        for category in self._basemap_categories:
            paths = basemap_paths_for_category(self._all_basemap_paths, category)
            count_text = f"{len(paths)} file" if len(paths) == 1 else f"{len(paths)} files"
            self.basemap_category_combo.addItem(f"{basemap_category_label(category)} - {count_text}", category)
        if self.basemap_category_combo.count() <= 0:
            self.basemap_category_combo.addItem("No basemap categories found", "")
        category_idx = self.basemap_category_combo.findData(current_basemap_category)
        self.basemap_category_combo.setCurrentIndex(category_idx if category_idx >= 0 else 0)

        self.basemap_combo = QtWidgets.QComboBox(basemap_group)
        compact_combo(self.basemap_combo, 26)
        self.basemap_combo.addItem(self._none_label, "")
        for path in self._all_basemap_paths:
            self.basemap_combo.addItem(self._basemap_path_labels.get(path, os.path.basename(path)), path)
        if current_basemap_path:
            for item_idx in range(self.basemap_combo.count()):
                if str(self.basemap_combo.itemData(item_idx) or "").strip().lower() == current_basemap_path.lower():
                    self.basemap_combo.setCurrentIndex(item_idx)
                    break
        basemap_layout.addWidget(QtWidgets.QLabel("Mode:", basemap_group), 0, 0)
        basemap_layout.addWidget(self.basemap_mode_combo, 0, 1)
        basemap_layout.addWidget(QtWidgets.QLabel("Category:", basemap_group), 1, 0)
        basemap_layout.addWidget(self.basemap_category_combo, 1, 1)
        basemap_layout.addWidget(QtWidgets.QLabel("Single scene:", basemap_group), 2, 0)
        basemap_layout.addWidget(self.basemap_combo, 2, 1)
        self.basemap_resolution_combo = QtWidgets.QComboBox(basemap_group)
        compact_combo(self.basemap_resolution_combo, 18)
        for label, token in BASEMAP_RESOLUTION_OPTIONS:
            self.basemap_resolution_combo.addItem(label, token)
        resolution_idx = self.basemap_resolution_combo.findData(current_basemap_resolution_mode)
        self.basemap_resolution_combo.setCurrentIndex(resolution_idx if resolution_idx >= 0 else 0)
        basemap_layout.addWidget(QtWidgets.QLabel("Basemap Resolution:", basemap_group), 3, 0)
        basemap_layout.addWidget(self.basemap_resolution_combo, 3, 1)
        self.basemap_color_scaling_combo = QtWidgets.QComboBox(basemap_group)
        compact_combo(self.basemap_color_scaling_combo, 12)
        for label, token in BASEMAP_COLOR_SCALING_OPTIONS:
            self.basemap_color_scaling_combo.addItem(label, token)
        scaling_idx = self.basemap_color_scaling_combo.findData(current_basemap_color_scaling)
        self.basemap_color_scaling_combo.setCurrentIndex(scaling_idx if scaling_idx >= 0 else 0)
        basemap_layout.addWidget(QtWidgets.QLabel("Color Scaling:", basemap_group), 4, 0)
        basemap_layout.addWidget(self.basemap_color_scaling_combo, 4, 1)

        self.basemap_cmap_combo = QtWidgets.QComboBox(basemap_group)
        compact_combo(self.basemap_cmap_combo, 14)
        self.basemap_cmap_combo.setEditable(True)
        self.basemap_cmap_combo.addItems(self._basemap_cmap_names)
        self.basemap_cmap_combo.setCurrentText(current_basemap_cmap)
        try:
            completer = QtWidgets.QCompleter(self._basemap_cmap_names, self.basemap_cmap_combo)
            completer.setCaseSensitivity(QtCore.Qt.CaseInsensitive)
            completer.setFilterMode(QtCore.Qt.MatchContains)
            self.basemap_cmap_combo.setCompleter(completer)
        except Exception:
            pass
        basemap_layout.addWidget(QtWidgets.QLabel("Colormap:", basemap_group), 5, 0)
        basemap_layout.addWidget(self.basemap_cmap_combo, 5, 1)
        self.basemap_rgb_note = QtWidgets.QLabel("RGB basemaps render in true color; colormap controls are disabled.", basemap_group)
        self.basemap_rgb_note.setWordWrap(True)
        self.basemap_rgb_note.setStyleSheet(f"color: {self.theme['muted']}; font-size: 10pt;")
        basemap_layout.addWidget(self.basemap_rgb_note, 6, 0, 1, 2)

        slot_group = QtWidgets.QGroupBox("Empty slots to fill", self)
        slot_layout = QtWidgets.QGridLayout(slot_group)
        slot_layout.setHorizontalSpacing(10)
        slot_layout.setVerticalSpacing(6)
        outer.addWidget(slot_group)

        self.slot_combos = []
        self.color_combos = []
        if not self._slot_specs:
            empty_label = QtWidgets.QLabel("No empty shapefile slots are available.", slot_group)
            empty_label.setStyleSheet(f"color: {self.theme['muted']};")
            slot_layout.addWidget(empty_label, 0, 0, 1, 3)
        for row, spec in enumerate(self._slot_specs):
            label = QtWidgets.QLabel(f"{spec.get('slot_label', f'Slot {row + 1}')}:", slot_group)
            shp_combo = QtWidgets.QComboBox(slot_group)
            compact_combo(shp_combo, 24)
            color_combo = QtWidgets.QComboBox(slot_group)
            color_combo.addItems(list(PICKER_COLORS))
            color_combo.setCurrentText(
                normalize_picker_color_name(spec.get("default_color"), "dodgerblue")
            )
            compact_combo(color_combo, 12)

            slot_layout.addWidget(label, row, 0)
            slot_layout.addWidget(shp_combo, row, 1)
            slot_layout.addWidget(color_combo, row, 2)

            self.slot_combos.append(shp_combo)
            self.color_combos.append(color_combo)

        self._selected_loads = []
        self._selected_basemap_path = current_basemap_path
        self._selected_basemap_mode = current_basemap_mode
        self._selected_basemap_category = current_basemap_category
        self._selected_basemap_resolution_mode = current_basemap_resolution_mode
        self._selected_basemap_color_scaling = current_basemap_color_scaling
        self._selected_basemap_cmap = current_basemap_cmap
        self._refresh_slot_choices()
        self.basemap_combo.currentIndexChanged.connect(self._sync_basemap_cmap_controls)
        self.basemap_mode_combo.currentIndexChanged.connect(self._sync_basemap_cmap_controls)
        self.basemap_category_combo.currentIndexChanged.connect(self._sync_basemap_cmap_controls)
        self._sync_basemap_cmap_controls()
        for shp_combo, color_combo in zip(self.slot_combos, self.color_combos):
            shp_combo.currentIndexChanged.connect(self._refresh_slot_choices)
            shp_combo.currentIndexChanged.connect(
                lambda _idx, combo=shp_combo, color_widget=color_combo: color_widget.setEnabled(bool(combo.currentData()))
            )
            color_combo.setEnabled(bool(shp_combo.currentData()))

        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel,
            parent=self,
        )
        add_button = btns.button(QtWidgets.QDialogButtonBox.Ok)
        add_button.setText("Add")
        add_button.setDefault(True)
        cancel_button = btns.button(QtWidgets.QDialogButtonBox.Cancel)
        cancel_button.setAutoDefault(False)
        cancel_button.setDefault(False)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        outer.addWidget(btns)

        self._add_enter_shortcuts = []
        for key in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
            shortcut = QtWidgets.QShortcut(QtGui.QKeySequence(key), self)
            shortcut.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(self._accept_from_enter)
            self._add_enter_shortcuts.append(shortcut)
        self._install_enter_accept_filter()
        self._apply_native_width_from_note()

    def _apply_native_width_from_note(self):
        try:
            margins = self.layout().contentsMargins()
            target_w = int(self._native_content_width) + margins.left() + margins.right()
            target_w = max(target_w, int(self.minimumSizeHint().width()))
            _with_ui_scale_override(1.0, lambda: self.resize(target_w, self.sizeHint().height()))
        except Exception:
            pass

    def _install_enter_accept_filter(self):
        self.installEventFilter(self)
        for child in self.findChildren(QtCore.QObject):
            try:
                child.installEventFilter(self)
            except Exception:
                pass

    def _accept_from_enter(self):
        self.accept()

    def eventFilter(self, obj, event):
        if (
            event.type() == QtCore.QEvent.KeyPress
            and event.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter)
        ):
            self._accept_from_enter()
            event.accept()
            return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event):
        if event.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
            self._accept_from_enter()
            event.accept()
            return
        super().keyPressEvent(event)

    def _refresh_slot_choices(self, *_args):
        current_paths = [str(combo.currentData() or "").strip() for combo in self.slot_combos]
        for idx, combo in enumerate(self.slot_combos):
            current_path = current_paths[idx]
            other_paths = {
                path for j, path in enumerate(current_paths)
                if j != idx and path
            }
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(self._none_label, "")
            for path in self._all_paths:
                if path in other_paths:
                    continue
                combo.addItem(self._path_labels.get(path, os.path.basename(path)), path)

            restored = False
            if current_path:
                for item_idx in range(combo.count()):
                    if str(combo.itemData(item_idx) or "").strip() == current_path:
                        combo.setCurrentIndex(item_idx)
                        restored = True
                        break
            if not restored:
                combo.setCurrentIndex(0)
            combo.blockSignals(False)

        for combo, color_combo in zip(self.slot_combos, self.color_combos):
            color_combo.setEnabled(bool(combo.currentData()))

    def selected_shapefile_loads(self):
        return list(self._selected_loads)

    def selected_basemap_path(self):
        return str(getattr(self, "_selected_basemap_path", "") or "").strip()

    def selected_basemap_mode(self):
        return normalize_basemap_mode(
            getattr(self, "_selected_basemap_mode", "nearest"),
            "nearest",
        )

    def selected_basemap_category(self):
        return normalize_basemap_category(
            getattr(self, "_selected_basemap_category", "")
        )

    def selected_basemap_resolution_mode(self):
        return normalize_basemap_resolution_mode(
            getattr(self, "_selected_basemap_resolution_mode", "dynamic"),
            "dynamic",
        )

    def selected_basemap_color_scaling(self):
        return normalize_basemap_color_scaling(
            getattr(self, "_selected_basemap_color_scaling", "normal"),
            "normal",
        )

    def selected_basemap_cmap(self):
        return normalize_basemap_cmap(
            getattr(self, "_selected_basemap_cmap", "gray"),
            "gray",
        )

    def _sync_basemap_cmap_controls(self, *_args):
        mode = normalize_basemap_mode(
            self.basemap_mode_combo.currentData() or self.basemap_mode_combo.currentText(),
            "nearest",
        )
        self.basemap_category_combo.setEnabled(mode == "nearest" and len(self._basemap_categories) > 1)
        self.basemap_combo.setEnabled(mode == "single" and bool(self._all_basemap_paths))
        if mode == "single":
            path = str(self.basemap_combo.currentData() or "").strip()
            is_rgb = bool(path and basemap_path_is_rgb(path))
        else:
            category = normalize_basemap_category(self.basemap_category_combo.currentData() or "")
            is_rgb = category in ("refl", "rgb")
        self.basemap_color_scaling_combo.setEnabled(not is_rgb)
        self.basemap_cmap_combo.setEnabled(not is_rgb)
        self.basemap_rgb_note.setVisible(is_rgb)

    def accept(self):
        seen = set()
        loads = []
        self._selected_basemap_mode = normalize_basemap_mode(
            self.basemap_mode_combo.currentData() or self.basemap_mode_combo.currentText(),
            "nearest",
        )
        self._selected_basemap_category = normalize_basemap_category(
            self.basemap_category_combo.currentData() or ""
        )
        if not self._selected_basemap_category and self._basemap_categories:
            self._selected_basemap_category = self._basemap_categories[0]
        self._selected_basemap_path = (
            str(self.basemap_combo.currentData() or "").strip()
            if self._selected_basemap_mode == "single"
            else ""
        )
        if self._selected_basemap_path:
            self._selected_basemap_category = normalize_basemap_category(
                basemap_category_for_path(self._selected_basemap_path)
            )
        self._selected_basemap_resolution_mode = normalize_basemap_resolution_mode(
            self.basemap_resolution_combo.currentData() or self.basemap_resolution_combo.currentText(),
            "dynamic",
        )
        self._selected_basemap_color_scaling = normalize_basemap_color_scaling(
            self.basemap_color_scaling_combo.currentData() or self.basemap_color_scaling_combo.currentText(),
            "normal",
        )
        self._selected_basemap_cmap = normalize_basemap_cmap(
            self.basemap_cmap_combo.currentText(),
            "gray",
        )
        for spec, shp_combo, color_combo in zip(self._slot_specs, self.slot_combos, self.color_combos):
            path = str(shp_combo.currentData() or "").strip()
            if not path:
                continue
            path_key = os.path.abspath(path).lower()
            if path_key in seen:
                continue
            seen.add(path_key)
            loads.append({
                "slot_kind": str(spec.get("slot_kind") or "overlay"),
                "slot_label": str(spec.get("slot_label") or "Overlay"),
                "path": path,
                "name": os.path.basename(path),
                "color": normalize_picker_color_name(
                    color_combo.currentText(),
                    spec.get("default_color", "dodgerblue"),
                ),
            })
        self._selected_loads = loads
        super().accept()

class ActionBehaviorDialog(QtWidgets.QDialog):
    """Control key: choose how Keep and Reject should save files."""
    def __init__(self, keep_behavior=None, reject_behavior=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Keep / Reject behavior")
        self.setModal(True)

        self.theme_mode = get_app_theme_mode()
        self.theme = build_theme_palette(self.theme_mode)
        self.setStyleSheet(f"""
            QDialog {{ background-color: {self.theme['window_bg']}; color: {self.theme['text']}; }}
            QLabel  {{ color: {self.theme['text']}; }}
            QLineEdit {{
                background-color: {self.theme['input_bg']}; color: {self.theme['text']};
                border: 1px solid {self.theme['border']}; border-radius: 6px;
                padding: 6px 10px;
            }}
            QGroupBox {{
                color: {self.theme['text']}; font-weight: 600;
                border: 1px solid {self.theme['border']}; border-radius: 8px;
                margin-top: 12px; padding: 10px 10px 8px 10px;
                background-color: {self.theme['group_bg']};
            }}
            QGroupBox::title {{
                subcontrol-origin: margin; left: 10px; top: 0px;
                padding: 0 4px; background-color: {self.theme['window_bg']}; color: {self.theme['heading']};
            }}
            QRadioButton, QCheckBox {{ color: {self.theme['text']}; spacing: 6px; }}
            QPushButton {{
                background-color: {self.theme['button_bg']}; color: {self.theme['button_text']};
                border: 1px solid {self.theme['border']}; border-radius: 8px;
                padding: 6px 14px;
            }}
            QPushButton:hover  {{ background-color: {self.theme['button_hover']}; }}
            QPushButton:pressed{{ background-color: {self.theme['button_pressed']}; }}
        """)

        keep_behavior = dict(keep_behavior or {})
        reject_behavior = dict(reject_behavior or {})

        body_font = QtGui.QFont("Lucida Console", 12)
        hdr_font  = QtGui.QFont("Lucida Console", 14, QtGui.QFont.Bold)

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(10)

        title = QtWidgets.QLabel("Keep / Reject behavior")
        title.setFont(hdr_font)
        title.setStyleSheet(f"color: {self.theme['heading']};")
        outer.addWidget(title)

        note = QtWidgets.QLabel(
            "Change how GeoViewer handles your files. "
            "Delete and overwrite to keep clean, or append to write new files."
        )
        note.setWordWrap(True)
        note.setFont(body_font)
        outer.addWidget(note)

        self.keep_group = QtWidgets.QGroupBox("Keep")
        keep_layout = QtWidgets.QVBoxLayout(self.keep_group)
        keep_layout.setContentsMargins(10, 12, 10, 10)
        keep_layout.setSpacing(8)
        outer.addWidget(self.keep_group)

        self.keep_overwrite = QtWidgets.QRadioButton("Overwrite the current file (default)")
        self.keep_suffix_mode = QtWidgets.QRadioButton("Save to a suffixed file instead")
        self.keep_suffix_edit = QtWidgets.QLineEdit()
        self.keep_suffix_edit.setPlaceholderText("e.g. _keep or _adjusted")
        self.keep_keep_original = QtWidgets.QCheckBox("Also keep the original file")
        for w in (self.keep_overwrite, self.keep_suffix_mode, self.keep_suffix_edit, self.keep_keep_original):
            try:
                w.setFont(body_font)
            except Exception:
                pass
            keep_layout.addWidget(w)

        self.reject_group = QtWidgets.QGroupBox("Reject")
        reject_layout = QtWidgets.QVBoxLayout(self.reject_group)
        reject_layout.setContentsMargins(10, 12, 10, 10)
        reject_layout.setSpacing(8)
        outer.addWidget(self.reject_group)

        self.reject_delete = QtWidgets.QRadioButton("Delete the current file (default)")
        self.reject_suffix_mode = QtWidgets.QRadioButton("Save to a suffixed file instead")
        self.reject_suffix_edit = QtWidgets.QLineEdit()
        self.reject_suffix_edit.setPlaceholderText("e.g. _reject or _bad")
        self.reject_keep_original = QtWidgets.QCheckBox("Also keep the original file")
        for w in (self.reject_delete, self.reject_suffix_mode, self.reject_suffix_edit, self.reject_keep_original):
            try:
                w.setFont(body_font)
            except Exception:
                pass
            reject_layout.addWidget(w)

        self.keep_overwrite.toggled.connect(self._sync_enabled_state)
        self.keep_suffix_mode.toggled.connect(self._sync_enabled_state)
        self.reject_delete.toggled.connect(self._sync_enabled_state)
        self.reject_suffix_mode.toggled.connect(self._sync_enabled_state)

        keep_mode = str(keep_behavior.get("mode", "overwrite") or "overwrite").lower()
        keep_suffix = str(keep_behavior.get("suffix", "_keep") or "_keep")
        keep_original = bool(keep_behavior.get("preserve_original", False))
        if keep_mode == "suffix":
            self.keep_suffix_mode.setChecked(True)
        else:
            self.keep_overwrite.setChecked(True)
        self.keep_suffix_edit.setText(keep_suffix)
        self.keep_keep_original.setChecked(keep_original)

        reject_mode = str(reject_behavior.get("mode", "delete") or "delete").lower()
        reject_suffix = str(reject_behavior.get("suffix", "_reject") or "_reject")
        reject_original = bool(reject_behavior.get("preserve_original", False))
        if reject_mode == "suffix":
            self.reject_suffix_mode.setChecked(True)
        else:
            self.reject_delete.setChecked(True)
        self.reject_suffix_edit.setText(reject_suffix)
        self.reject_keep_original.setChecked(reject_original)

        self._sync_enabled_state()

        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        btns.button(QtWidgets.QDialogButtonBox.Ok).setText("Use")
        btns.button(QtWidgets.QDialogButtonBox.Cancel).setText("Cancel")
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        outer.addWidget(btns)

    def _sync_enabled_state(self):
        keep_suffix_mode = self.keep_suffix_mode.isChecked()
        self.keep_suffix_edit.setEnabled(keep_suffix_mode)
        self.keep_keep_original.setEnabled(keep_suffix_mode)
        if not keep_suffix_mode:
            self.keep_keep_original.setChecked(False)

        reject_suffix_mode = self.reject_suffix_mode.isChecked()
        self.reject_suffix_edit.setEnabled(reject_suffix_mode)
        self.reject_keep_original.setEnabled(reject_suffix_mode)
        if not reject_suffix_mode:
            self.reject_keep_original.setChecked(False)

    def _on_accept(self):
        if self.keep_suffix_mode.isChecked() and not (self.keep_suffix_edit.text() or "").strip():
            QtWidgets.QMessageBox.warning(self, "Keep suffix", "Please enter a suffix for Keep suffix mode.")
            return
        if self.reject_suffix_mode.isChecked() and not (self.reject_suffix_edit.text() or "").strip():
            QtWidgets.QMessageBox.warning(self, "Reject suffix", "Please enter a suffix for Reject suffix mode.")
            return
        self.accept()

    def values(self):
        return {
            "keep": {
                "mode": "suffix" if self.keep_suffix_mode.isChecked() else "overwrite",
                "suffix": (self.keep_suffix_edit.text() or "").strip(),
                "preserve_original": bool(self.keep_suffix_mode.isChecked() and self.keep_keep_original.isChecked()),
            },
            "reject": {
                "mode": "suffix" if self.reject_suffix_mode.isChecked() else "delete",
                "suffix": (self.reject_suffix_edit.text() or "").strip(),
                "preserve_original": bool(self.reject_suffix_mode.isChecked() and self.reject_keep_original.isChecked()),
            },
        }

class _SplashWhatsThisFilter(QtCore.QObject):
    """
    Intercepts the '?' (What’s This) help mode on the splash dialog and
    opens the LinksDialog instead of the default What's This behavior.
    """
    def __init__(self, parent_dialog):
        super().__init__(parent_dialog)
        self._dlg = parent_dialog

    def eventFilter(self, obj, event):
        if obj is self._dlg and event.type() == QtCore.QEvent.EnterWhatsThisMode:
            # Leave Qt's built-in What's This mode
            QtWidgets.QWhatsThis.leaveWhatsThisMode()

            # Open the links dialog centered over the splash
            links_dlg = LinksDialog(self._dlg)
            size = links_dlg.sizeHint()
            center_pt = self._dlg.frameGeometry().center() - QtCore.QPoint(
                size.width() // 2, size.height() // 2
            )
            links_dlg.move(center_pt)
            links_dlg.exec_()
            return True
        return False

class SplashDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("NASA JPL Thermal Viewer")
        self.setModal(True)

        # NEW: turn on the '?' help button in the titlebar
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowContextHelpButtonHint)

        # -----------------------------
        # Fixed physical size across monitors
        # -----------------------------
        base_w, base_h = 1025, 675  # reference size at 120 DPI

        screen = self.screen() or QtWidgets.QApplication.primaryScreen()
        if screen is not None:
            dpi = screen.logicalDotsPerInch() or 120.0
            scale = dpi / 120.0

            w = int(base_w * scale)
            h = int(base_h * scale)

            # Avoid going off-screen: cap at 95% of available geometry
            geo = screen.availableGeometry()
            w = min(w, int(geo.width() * 0.95))
            h = min(h, int(geo.height() * 0.95))
        else:
            # Fallback if screen detection fails
            w, h = base_w, base_h

        self.setFixedSize(w, h)
        self.theme_mode = get_app_theme_mode()
        self.theme = build_theme_palette(self.theme_mode)
        self.setStyleSheet(build_app_stylesheet(self.theme_mode))

        # -----------------------------
        # Layout & spacing control
        # -----------------------------
        layout = QtWidgets.QVBoxLayout(self)
        # outer padding to window border
        layout.setContentsMargins(10, 35, 10, 10)
        # all inter-widget spacing is manual via addSpacing
        layout.setSpacing(0)

        # Manual per-line spacing (pixels)
        TITLE_LINE_GAP = 20   # between shapes2 and title, title and subtitle
        BLOCK_GAP      = 12   # between subtitle and help, help and button
        BUTTON_GAP     = 12   # between button and version text

        def add_line(widget, spacing_after=0, alignment=QtCore.Qt.AlignHCenter):
            """Add a widget as a 'line' and optional vertical spacing."""
            layout.addWidget(widget, alignment=alignment)
            if spacing_after > 0:
                layout.addSpacing(spacing_after)

        # -----------------------------
        # Fonts
        # -----------------------------
        hdr_font = QtGui.QFont("Lucida Console", 29, QtGui.QFont.Bold)
        body_font = QtGui.QFont("Lucida Console", 16)
        ver_font  = QtGui.QFont("Lucida Console", 11)

        # -----------------------------
        # Header glyphs & title
        # -----------------------------
        self.shapes1 = OutlinedLabel(" ▛▟ ▞▚ ▟▛ ▞▚  ")
        self.shapes1.setFont(hdr_font)
        self.shapes1.setAlignment(QtCore.Qt.AlignHCenter)
        add_line(self.shapes1)  # no extra spacing after

        self.shapes2 = QLabel(" ▟ ▛ ▙ ")
        self.shapes2.setFont(hdr_font)
        self.shapes2.setAlignment(QtCore.Qt.AlignHCenter)
        add_line(self.shapes2, spacing_after=TITLE_LINE_GAP)

        self.title_label = QLabel("NASA JPL THERMAL VIEWER")
        self.title_label.setFont(hdr_font)
        self.title_label.setAlignment(QtCore.Qt.AlignHCenter)
        add_line(self.title_label, spacing_after=TITLE_LINE_GAP)

        self.subtitle_label = QLabel("SEMI-AUTOMATED GEOREFERENCER")
        self.subtitle_label.setFont(body_font)
        self.subtitle_label.setAlignment(QtCore.Qt.AlignHCenter)
        add_line(self.subtitle_label, spacing_after=BLOCK_GAP)

        # -----------------------------
        # Help text (no extra blank lines)
        # -----------------------------
        help_text = textwrap.dedent(
            """
            [SPACE]    Toggle Pan    |    [W/A/S/D]   Pan Image
            [SHIFT]   Reset Warps    |    [Wheel]     Pan Speed
            [ALT/TAB]  Formatting    |    [G/H/J]    Warp/Apply
            [L drag]   Zoom Image    |    [O/K]    Gamma Adjust
            [R click]  Reset Zoom    |    [P/L] Contrast Adjust
            [ENTER]  Reset Colors    |    [X]  Skip/Defer Scene
            [KEEP]   Apply / Save    |    [F]  Toggle Full Scrn
            [REJECT]  Delete file    |    [E/R]  Edges/Colormap
            ▼
            """
        ).strip("\n")

        self.help_label = QLabel(help_text)
        self.help_label.setFont(body_font)
        self.help_label.setTextFormat(QtCore.Qt.PlainText)  # preserve spaces/newlines
        self.help_label.setWordWrap(False)
        self.help_label.setAlignment(QtCore.Qt.AlignHCenter)
        add_line(self.help_label, spacing_after=BLOCK_GAP)

        # -----------------------------
        # Launch button
        # -----------------------------
        self.launch_btn = QPushButton("LAUNCH")
        self.launch_btn.setFont(body_font)
        # Button size just around its text (plus padding), no stretch
        self.launch_btn.setSizePolicy(
            QtWidgets.QSizePolicy.Fixed,  # horizontal
            QtWidgets.QSizePolicy.Fixed   # vertical
        )
        self.launch_btn.clicked.connect(self.accept)
        add_line(self.launch_btn, spacing_after=BUTTON_GAP)

        # -----------------------------
        # Version line
        # -----------------------------
        self.ver_label = QLabel("v1.15 | Longenecker et al. | MIT License 2025")
        self.ver_label.setFont(ver_font)
        self.ver_label.setAlignment(QtCore.Qt.AlignHCenter)
        add_line(self.ver_label)

        self._apply_theme()
        self._fit_to_scaled_contents()
        QtCore.QTimer.singleShot(0, self._fit_to_scaled_contents)

        # NEW: hook the '?' help button to open LinksDialog
        self._wt_filter = _SplashWhatsThisFilter(self)
        self.installEventFilter(self._wt_filter)

    def _fit_to_scaled_contents(self):
        try:
            for widget in (
                self.shapes1,
                self.shapes2,
                self.title_label,
                self.subtitle_label,
                self.help_label,
                self.launch_btn,
                self.ver_label,
            ):
                widget.ensurePolished()
                widget.updateGeometry()
            layout = self.layout()
            layout.invalidate()
            layout.activate()
            hint = layout.sizeHint()
            minimum = layout.minimumSize()
        except Exception:
            hint = self.sizeHint()
            minimum = self.minimumSizeHint()

        available = _available_geometry_for_widget_or_cursor(self)
        max_w = max(420, int(min(available.width() * 0.95, available.width() - 20)))
        max_h = max(320, int(min(available.height() * 0.95, available.height() - 20)))
        target_w = min(max_w, max(int(self.width()), int(hint.width()), int(minimum.width())))
        target_h = min(max_h, max(int(self.height()), int(hint.height()), int(minimum.height())))

        def apply_size():
            self.setMinimumSize(0, 0)
            self.setMaximumSize(16777215, 16777215)
            self.setFixedSize(target_w, target_h)

        _with_ui_scale_override(1.0, apply_size)

    def _apply_theme(self):
        self.theme_mode = get_app_theme_mode()
        self.theme = build_theme_palette(self.theme_mode)
        self.setStyleSheet(build_app_stylesheet(self.theme_mode))
        outline_width = 2.4 if self.theme_mode == "light" else 0.0
        self.shapes1.set_outline_style(
            fill_color=self.theme["splash_logo_fill"],
            outline_color=self.theme["splash_logo_outline"],
            outline_width=outline_width,
        )
        self.shapes2.setStyleSheet(f"color: {self.theme['splash_red']};")
        self.title_label.setStyleSheet(f"color: {self.theme['heading']};")
        self.subtitle_label.setStyleSheet(f"color: {self.theme['text']};")
        self.help_label.setStyleSheet(f"color: {self.theme['text']};")
        self.ver_label.setStyleSheet(f"color: {self.theme['muted']};")

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key_Slash:
            new_mode = "light" if get_app_theme_mode() == "dark" else "dark"
            set_app_theme_mode(new_mode)
            self._apply_theme()
            self._fit_to_scaled_contents()
            event.accept()
            return
        super().keyPressEvent(event)

# ---------------------------------------------------------------------------
# Main Viewer Window
# ---------------------------------------------------------------------------
# ── Shapefile picker UI (compact, readable, visible controls) ───────────── #
PICKER_COLORS = [
    "cyan", "magenta", "white", "black", "gray",
    "red", "orange", "yellow", "dodgerblue", "lime", "violet", "pink"
]

def normalize_picker_color_name(color, fallback="black"):
    val = str(color or "").strip()
    if not val:
        return fallback
    low = val.lower()
    if low in ("#000", "#000000"):
        return "black"
    if low in ("#fff", "#ffffff"):
        return "white"
    return val

def _nan_transparency_values():
    return {value: alpha for _label, value, alpha in NAN_TRANSPARENT_OPTIONS}

def _nan_transparency_labels():
    return {label.lower(): value for label, value, _alpha in NAN_TRANSPARENT_OPTIONS}

def normalize_nan_override_value(value, fallback="black"):
    val = str(value or "").strip()
    if not val:
        return fallback
    low = val.lower()
    transparency_values = _nan_transparency_values()
    if low in transparency_values:
        return low
    label_match = _nan_transparency_labels().get(low)
    if label_match:
        return label_match
    return normalize_picker_color_name(val, fallback)

def nan_override_display_label(value):
    normalized = normalize_nan_override_value(value)
    for label, token, _alpha in NAN_TRANSPARENT_OPTIONS:
        if normalized == token:
            return label
    return normalized

def nan_override_bad_color(value, fallback_color):
    normalized = normalize_nan_override_value(value)
    alpha = _nan_transparency_values().get(normalized)
    if alpha is not None:
        try:
            return mcolors.to_rgba(fallback_color, alpha=float(alpha))
        except Exception:
            return (0.0, 0.0, 0.0, float(alpha))
    return normalized

def normalize_persisted_shapefile_name(name):
    text = str(name or "").strip()
    return os.path.basename(text) if text else ""

def normalize_persisted_basemap_name(name):
    text = str(name or "").strip()
    return os.path.basename(text) if text else ""

def normalize_filename_datetime_substring(text):
    return str(text or "").strip()

def normalize_filename_datetime_pattern(text):
    return str(text or "").strip()

_COMMENT_FLAGS_START_MARKER = "# --- " + "GEOVIEWER_COMMENT_FLAGS_BEGIN" + " ---"
_COMMENT_FLAGS_END_MARKER = "# --- " + "GEOVIEWER_COMMENT_FLAGS_END" + " ---"
_PERSISTED_UI_SETTINGS_START_MARKER = "# --- " + "GEOVIEWER_PERSISTED_UI_SETTINGS_BEGIN" + " ---"
_PERSISTED_UI_SETTINGS_END_MARKER = "# --- " + "GEOVIEWER_PERSISTED_UI_SETTINGS_END" + " ---"
MAX_PERSISTED_UI_PROFILES = 10
SETTINGS_PROFILE_EXPORT_MAGIC = "GEOVIEWER_SETTINGS_PROFILE_EXPORT"
SETTINGS_PROFILE_EXPORT_VERSION = "1"
SETTINGS_PROFILE_EXPORT_PROFILE_PREFIX = "PROFILE_NAME_JSON="
SETTINGS_PROFILE_EXPORT_SHA256_PREFIX = "SETTINGS_SHA256="
SETTINGS_PROFILE_EXPORT_JSON_BEGIN = "SETTINGS_JSON_BEGIN"
SETTINGS_PROFILE_EXPORT_JSON_END = "SETTINGS_JSON_END"
SETTINGS_PROFILE_EXPORT_MIN_BYTES = 1200
SETTINGS_PROFILE_EXPORT_MAX_BYTES = 120000

# --- GEOVIEWER_COMMENT_FLAGS_BEGIN ---
USER_COMMENT_FLAGS_JSON = r'''
[
    "Cloud",
    "Dust",
    "Urban",
    "Hotspot",
    "Sensor noise",
    "Low contrast",
    "Fire",
    "Thermal anomaly",
    "Solar Panel",
    "Edge artifact",
    "Striping",
    "Needs review",
    "Cloud shadow",
    "Vegetation",
    "Haze",
    "Missing data",
    "Smoke",
    "Plume",
    "Flooding",
    "Water",
    "Snow/Ice",
    "Agriculture",
    "Terrain shadow",
    "Partial scene"
]
'''
# --- GEOVIEWER_COMMENT_FLAGS_END ---

# --- GEOVIEWER_PERSISTED_UI_SETTINGS_BEGIN ---
PERSISTED_UI_SETTINGS_JSON = r'''
{
    "default_profile": "Preset 2 - Red White & Blue",
    "last_main_panel_text_scale": 1.0,
    "last_ui_scale": 0.9,
    "profiles": [
        {
            "name": "Preset 1 - Grays",
            "settings": {
                "alt_cmap": "Grays",
                "base_multiplier": 1,
                "basemap_category": "",
                "basemap_cmap": "gray",
                "basemap_color_scaling": "normal",
                "basemap_mode": "nearest",
                "basemap_name": "",
                "basemap_resolution_mode": "dynamic",
                "basemap_visual_resampling": "nearest",
                "cmap_mode": "Grays",
                "filename_dt_pattern": "",
                "filename_dt_substring": "",
                "global_contrast_rel": [
                    0.0,
                    1.0000000000000002
                ],
                "global_edge_mode": false,
                "global_gamma": 1.0000000000000007,
                "grid_cols": 2,
                "grid_rows": 1,
                "keep_behavior": {
                    "mode": "overwrite",
                    "preserve_original": false,
                    "suffix": "_keep"
                },
                "keep_button_color": "#0072b2",
                "keep_reject_button_layout_settings": {
                    "size_scale": 1.0,
                    "spacing_px": 18.0
                },
                "keep_reject_button_preset": "blue_orange",
                "keyboard_shortcuts_lock_theme": false,
                "keyboard_shortcuts_theme_mode": "light",
                "nan_color": "transparent_100",
                "panel_layout_settings": {
                    "bottom": 0.03,
                    "hspace": 0.1,
                    "left": 0.01,
                    "right": 0.99,
                    "top": 0.96,
                    "wspace": 0.035
                },
                "reject_behavior": {
                    "mode": "delete",
                    "preserve_original": false,
                    "suffix": "_reject"
                },
                "reject_button_color": "#e69f00",
                "scale_modifier": 1.0,
                "scroll_wheel_pan_multi_enabled": true,
                "shp_linewidth": 1.0,
                "shp_overlay_colors": [],
                "shp_primary_color": "cyan",
                "shp_primary_name": "",
                "summary_fontsize": 20.0,
                "sync_zoom_pan": true,
                "theme_mode": "dark",
                "thermal_alpha": 1.0,
                "thermal_blend_mode": "normal",
                "thermal_visual_resampling": "nearest",
                "title_fontsize": 18.641306958139815,
                "use_theme_nan_color": true,
                "warp_source_color": "#c23b22",
                "warp_target_color": "#2e8b57",
                "window_size": [
                    2358,
                    1422
                ]
            }
        },
        {
            "name": "Preset 2 - Red White & Blue",
            "settings": {
                "alt_cmap": "RdBu",
                "base_multiplier": 1,
                "basemap_category": "HLS_B05",
                "basemap_cmap": "gray",
                "basemap_color_scaling": "normal",
                "basemap_mode": "nearest",
                "basemap_name": "",
                "basemap_resolution_mode": "dynamic",
                "basemap_visual_resampling": "nearest",
                "cmap_mode": "RdBu",
                "filename_dt_pattern": "",
                "filename_dt_substring": "",
                "global_contrast_rel": [
                    0.0,
                    1.0
                ],
                "global_edge_mode": false,
                "global_gamma": 1.0,
                "grid_cols": 2,
                "grid_rows": 1,
                "keep_behavior": {
                    "mode": "overwrite",
                    "preserve_original": false,
                    "suffix": "_keep"
                },
                "keep_button_color": "#0072b2",
                "keep_reject_button_layout_settings": {
                    "size_scale": 1.0,
                    "spacing_px": 18.0
                },
                "keep_reject_button_preset": "blue_orange",
                "keyboard_shortcuts_lock_theme": false,
                "keyboard_shortcuts_theme_mode": "light",
                "nan_color": "transparent_100",
                "panel_layout_settings": {
                    "bottom": 0.03,
                    "hspace": 0.1,
                    "left": 0.01,
                    "right": 0.99,
                    "top": 0.96,
                    "wspace": 0.035
                },
                "reject_behavior": {
                    "mode": "delete",
                    "preserve_original": false,
                    "suffix": "_reject"
                },
                "reject_button_color": "#e69f00",
                "scale_modifier": 4.0,
                "scroll_wheel_pan_multi_enabled": true,
                "shp_linewidth": 1.0,
                "shp_overlay_colors": [],
                "shp_primary_color": "lime",
                "shp_primary_name": "",
                "summary_fontsize": 15.0,
                "sync_zoom_pan": true,
                "theme_mode": "dark",
                "thermal_alpha": 1.0,
                "thermal_blend_mode": "difference",
                "thermal_visual_resampling": "nearest",
                "title_fontsize": 14.798979409234999,
                "use_theme_nan_color": false,
                "warp_source_color": "#c23b22",
                "warp_target_color": "#2e8b57",
                "window_size": [
                    1728,
                    1080
                ]
            }
        },
        {
            "name": "Preset 3 - Pastels",
            "settings": {
                "alt_cmap": "Set3_r",
                "base_multiplier": 1,
                "basemap_category": "",
                "basemap_cmap": "gray",
                "basemap_color_scaling": "normal",
                "basemap_mode": "nearest",
                "basemap_name": "",
                "basemap_resolution_mode": "dynamic",
                "basemap_visual_resampling": "nearest",
                "cmap_mode": "Set3_r",
                "filename_dt_pattern": "",
                "filename_dt_substring": "",
                "global_contrast_rel": [
                    0.0,
                    0.6983372960937498
                ],
                "global_edge_mode": false,
                "global_gamma": 1.4400000000000015,
                "grid_cols": 2,
                "grid_rows": 1,
                "keep_behavior": {
                    "mode": "overwrite",
                    "preserve_original": false,
                    "suffix": "_keep"
                },
                "keep_button_color": "#009e73",
                "keep_reject_button_layout_settings": {
                    "size_scale": 1.0,
                    "spacing_px": 18.0
                },
                "keep_reject_button_preset": "teal_magenta",
                "keyboard_shortcuts_lock_theme": false,
                "keyboard_shortcuts_theme_mode": "light",
                "nan_color": "transparent_100",
                "panel_layout_settings": {
                    "bottom": 0.03,
                    "hspace": 0.1,
                    "left": 0.01,
                    "right": 0.99,
                    "top": 0.96,
                    "wspace": 0.035
                },
                "reject_behavior": {
                    "mode": "delete",
                    "preserve_original": false,
                    "suffix": "_reject"
                },
                "reject_button_color": "#cc79a7",
                "scale_modifier": 1.0,
                "scroll_wheel_pan_multi_enabled": true,
                "shp_linewidth": 1.0,
                "shp_overlay_colors": [],
                "shp_primary_color": "cyan",
                "shp_primary_name": "",
                "summary_fontsize": 20.0,
                "sync_zoom_pan": true,
                "theme_mode": "dark",
                "thermal_alpha": 1.0,
                "thermal_blend_mode": "normal",
                "thermal_visual_resampling": "nearest",
                "title_fontsize": 18.641306958139815,
                "use_theme_nan_color": true,
                "warp_source_color": "#c23b22",
                "warp_target_color": "#2e8b57",
                "window_size": [
                    2358,
                    1422
                ]
            }
        },
        {
            "name": "Preset 4 - Spectral",
            "settings": {
                "alt_cmap": "Spectral",
                "base_multiplier": 1,
                "basemap_category": "",
                "basemap_cmap": "gray",
                "basemap_color_scaling": "normal",
                "basemap_mode": "nearest",
                "basemap_name": "",
                "basemap_resolution_mode": "dynamic",
                "basemap_visual_resampling": "nearest",
                "cmap_mode": "Spectral",
                "filename_dt_pattern": "",
                "filename_dt_substring": "",
                "global_contrast_rel": [
                    0.0,
                    0.6634204312890624
                ],
                "global_edge_mode": false,
                "global_gamma": 1.7280000000000018,
                "grid_cols": 2,
                "grid_rows": 1,
                "keep_behavior": {
                    "mode": "overwrite",
                    "preserve_original": false,
                    "suffix": "_keep"
                },
                "keep_button_color": "#2e8b57",
                "keep_reject_button_layout_settings": {
                    "size_scale": 1.0,
                    "spacing_px": 18.0
                },
                "keep_reject_button_preset": "standard",
                "keyboard_shortcuts_lock_theme": false,
                "keyboard_shortcuts_theme_mode": "light",
                "nan_color": "transparent_100",
                "panel_layout_settings": {
                    "bottom": 0.03,
                    "hspace": 0.1,
                    "left": 0.01,
                    "right": 0.99,
                    "top": 0.96,
                    "wspace": 0.035
                },
                "reject_behavior": {
                    "mode": "delete",
                    "preserve_original": false,
                    "suffix": "_reject"
                },
                "reject_button_color": "#c23b22",
                "scale_modifier": 1.0,
                "scroll_wheel_pan_multi_enabled": true,
                "shp_linewidth": 1.0,
                "shp_overlay_colors": [],
                "shp_primary_color": "cyan",
                "shp_primary_name": "",
                "summary_fontsize": 20.0,
                "sync_zoom_pan": true,
                "theme_mode": "dark",
                "thermal_alpha": 1.0,
                "thermal_blend_mode": "normal",
                "thermal_visual_resampling": "nearest",
                "title_fontsize": 18.641306958139815,
                "use_theme_nan_color": true,
                "warp_source_color": "#c23b22",
                "warp_target_color": "#2e8b57",
                "window_size": [
                    2358,
                    1422
                ]
            }
        },
        {
            "name": "Preset 5 - Ice",
            "settings": {
                "alt_cmap": "berlin",
                "base_multiplier": 1,
                "basemap_category": "",
                "basemap_cmap": "gray",
                "basemap_color_scaling": "normal",
                "basemap_mode": "nearest",
                "basemap_name": "",
                "basemap_resolution_mode": "dynamic",
                "basemap_visual_resampling": "nearest",
                "cmap_mode": "berlin",
                "filename_dt_pattern": "",
                "filename_dt_substring": "",
                "global_contrast_rel": [
                    0.0,
                    0.7350918906249999
                ],
                "global_edge_mode": false,
                "global_gamma": 2.4883200000000025,
                "grid_cols": 2,
                "grid_rows": 1,
                "keep_behavior": {
                    "mode": "overwrite",
                    "preserve_original": false,
                    "suffix": "_keep"
                },
                "keep_button_color": "#009e73",
                "keep_reject_button_layout_settings": {
                    "size_scale": 1.0,
                    "spacing_px": 18.0
                },
                "keep_reject_button_preset": "teal_magenta",
                "keyboard_shortcuts_lock_theme": false,
                "keyboard_shortcuts_theme_mode": "light",
                "nan_color": "transparent_100",
                "panel_layout_settings": {
                    "bottom": 0.03,
                    "hspace": 0.1,
                    "left": 0.01,
                    "right": 0.99,
                    "top": 0.96,
                    "wspace": 0.035
                },
                "reject_behavior": {
                    "mode": "delete",
                    "preserve_original": false,
                    "suffix": "_reject"
                },
                "reject_button_color": "#cc79a7",
                "scale_modifier": 1.0,
                "scroll_wheel_pan_multi_enabled": true,
                "shp_linewidth": 1.0,
                "shp_overlay_colors": [],
                "shp_primary_color": "cyan",
                "shp_primary_name": "",
                "summary_fontsize": 20.0,
                "sync_zoom_pan": true,
                "theme_mode": "dark",
                "thermal_alpha": 1.0,
                "thermal_blend_mode": "normal",
                "thermal_visual_resampling": "nearest",
                "title_fontsize": 18.641306958139815,
                "use_theme_nan_color": true,
                "warp_source_color": "#c23b22",
                "warp_target_color": "#2e8b57",
                "window_size": [
                    2358,
                    1422
                ]
            }
        },
        {
            "name": "Preset 6 - Bone",
            "settings": {
                "alt_cmap": "bone",
                "base_multiplier": 1,
                "basemap_category": "",
                "basemap_cmap": "gray",
                "basemap_color_scaling": "normal",
                "basemap_mode": "nearest",
                "basemap_name": "",
                "basemap_resolution_mode": "dynamic",
                "basemap_visual_resampling": "nearest",
                "cmap_mode": "bone",
                "filename_dt_pattern": "",
                "filename_dt_substring": "",
                "global_contrast_rel": [
                    0.0,
                    0.7350918906249999
                ],
                "global_edge_mode": false,
                "global_gamma": 2.4883200000000025,
                "grid_cols": 2,
                "grid_rows": 1,
                "keep_behavior": {
                    "mode": "overwrite",
                    "preserve_original": false,
                    "suffix": "_keep"
                },
                "keep_button_color": "#009e73",
                "keep_reject_button_layout_settings": {
                    "size_scale": 1.0,
                    "spacing_px": 18.0
                },
                "keep_reject_button_preset": "teal_magenta",
                "keyboard_shortcuts_lock_theme": false,
                "keyboard_shortcuts_theme_mode": "light",
                "nan_color": "transparent_100",
                "panel_layout_settings": {
                    "bottom": 0.03,
                    "hspace": 0.1,
                    "left": 0.01,
                    "right": 0.99,
                    "top": 0.96,
                    "wspace": 0.035
                },
                "reject_behavior": {
                    "mode": "delete",
                    "preserve_original": false,
                    "suffix": "_reject"
                },
                "reject_button_color": "#cc79a7",
                "scale_modifier": 1.0,
                "scroll_wheel_pan_multi_enabled": true,
                "shp_linewidth": 1.0,
                "shp_overlay_colors": [],
                "shp_primary_color": "cyan",
                "shp_primary_name": "",
                "summary_fontsize": 20.0,
                "sync_zoom_pan": true,
                "theme_mode": "dark",
                "thermal_alpha": 1.0,
                "thermal_blend_mode": "normal",
                "thermal_visual_resampling": "nearest",
                "title_fontsize": 18.641306958139815,
                "use_theme_nan_color": true,
                "warp_source_color": "#c23b22",
                "warp_target_color": "#2e8b57",
                "window_size": [
                    2358,
                    1422
                ]
            }
        },
        {
            "name": "Preset 7 - Miami Lights",
            "settings": {
                "alt_cmap": "cool",
                "base_multiplier": 1,
                "basemap_category": "",
                "basemap_cmap": "gray",
                "basemap_color_scaling": "normal",
                "basemap_mode": "nearest",
                "basemap_name": "",
                "basemap_resolution_mode": "dynamic",
                "basemap_visual_resampling": "nearest",
                "cmap_mode": "cool",
                "filename_dt_pattern": "",
                "filename_dt_substring": "",
                "global_contrast_rel": [
                    0.0,
                    0.6983372960937498
                ],
                "global_edge_mode": false,
                "global_gamma": 6.191736422400005,
                "grid_cols": 2,
                "grid_rows": 1,
                "keep_behavior": {
                    "mode": "overwrite",
                    "preserve_original": false,
                    "suffix": "_keep"
                },
                "keep_button_color": "#009e73",
                "keep_reject_button_layout_settings": {
                    "size_scale": 1.0,
                    "spacing_px": 18.0
                },
                "keep_reject_button_preset": "teal_magenta",
                "keyboard_shortcuts_lock_theme": false,
                "keyboard_shortcuts_theme_mode": "light",
                "nan_color": "transparent_100",
                "panel_layout_settings": {
                    "bottom": 0.03,
                    "hspace": 0.1,
                    "left": 0.01,
                    "right": 0.99,
                    "top": 0.96,
                    "wspace": 0.035
                },
                "reject_behavior": {
                    "mode": "delete",
                    "preserve_original": false,
                    "suffix": "_reject"
                },
                "reject_button_color": "#cc79a7",
                "scale_modifier": 1.0,
                "scroll_wheel_pan_multi_enabled": true,
                "shp_linewidth": 1.0,
                "shp_overlay_colors": [],
                "shp_primary_color": "cyan",
                "shp_primary_name": "",
                "summary_fontsize": 20.0,
                "sync_zoom_pan": true,
                "theme_mode": "dark",
                "thermal_alpha": 1.0,
                "thermal_blend_mode": "normal",
                "thermal_visual_resampling": "nearest",
                "title_fontsize": 18.641306958139815,
                "use_theme_nan_color": true,
                "warp_source_color": "#c23b22",
                "warp_target_color": "#2e8b57",
                "window_size": [
                    2358,
                    1422
                ]
            }
        },
        {
            "name": "Preset 8 - Magma",
            "settings": {
                "alt_cmap": "magma",
                "base_multiplier": 1,
                "basemap_category": "",
                "basemap_cmap": "gray",
                "basemap_color_scaling": "normal",
                "basemap_mode": "nearest",
                "basemap_name": "",
                "basemap_resolution_mode": "dynamic",
                "basemap_visual_resampling": "nearest",
                "cmap_mode": "magma",
                "filename_dt_pattern": "",
                "filename_dt_substring": "",
                "global_contrast_rel": [
                    0.0,
                    0.8145062500000002
                ],
                "global_edge_mode": false,
                "global_gamma": 1.7280000000000018,
                "grid_cols": 2,
                "grid_rows": 1,
                "keep_behavior": {
                    "mode": "overwrite",
                    "preserve_original": false,
                    "suffix": "_keep"
                },
                "keep_button_color": "#009e73",
                "keep_reject_button_layout_settings": {
                    "size_scale": 1.0,
                    "spacing_px": 18.0
                },
                "keep_reject_button_preset": "teal_magenta",
                "keyboard_shortcuts_lock_theme": false,
                "keyboard_shortcuts_theme_mode": "light",
                "nan_color": "black",
                "panel_layout_settings": {
                    "bottom": 0.03,
                    "hspace": 0.1,
                    "left": 0.01,
                    "right": 0.99,
                    "top": 0.96,
                    "wspace": 0.035
                },
                "reject_behavior": {
                    "mode": "delete",
                    "preserve_original": false,
                    "suffix": "_reject"
                },
                "reject_button_color": "#cc79a7",
                "scale_modifier": 1.0,
                "scroll_wheel_pan_multi_enabled": true,
                "shp_linewidth": 1.0,
                "shp_overlay_colors": [],
                "shp_primary_color": "cyan",
                "shp_primary_name": "",
                "summary_fontsize": 20.0,
                "sync_zoom_pan": true,
                "theme_mode": "dark",
                "thermal_alpha": 1.0,
                "thermal_blend_mode": "normal",
                "thermal_visual_resampling": "nearest",
                "title_fontsize": 18.641306958139815,
                "use_theme_nan_color": true,
                "warp_source_color": "#c23b22",
                "warp_target_color": "#2e8b57",
                "window_size": [
                    2358,
                    1422
                ]
            }
        },
        {
            "name": "Preset 9 - Ocean",
            "settings": {
                "alt_cmap": "ocean",
                "base_multiplier": 1,
                "basemap_category": "",
                "basemap_cmap": "gray",
                "basemap_color_scaling": "normal",
                "basemap_mode": "nearest",
                "basemap_name": "",
                "basemap_resolution_mode": "dynamic",
                "basemap_visual_resampling": "nearest",
                "cmap_mode": "ocean",
                "filename_dt_pattern": "",
                "filename_dt_substring": "",
                "global_contrast_rel": [
                    0.0,
                    0.8145062500000002
                ],
                "global_edge_mode": false,
                "global_gamma": 1.7280000000000018,
                "grid_cols": 2,
                "grid_rows": 1,
                "keep_behavior": {
                    "mode": "overwrite",
                    "preserve_original": false,
                    "suffix": "_keep"
                },
                "keep_button_color": "#2e8b57",
                "keep_reject_button_layout_settings": {
                    "size_scale": 1.0,
                    "spacing_px": 18.0
                },
                "keep_reject_button_preset": "standard",
                "keyboard_shortcuts_lock_theme": false,
                "keyboard_shortcuts_theme_mode": "light",
                "nan_color": "black",
                "panel_layout_settings": {
                    "bottom": 0.03,
                    "hspace": 0.1,
                    "left": 0.01,
                    "right": 0.99,
                    "top": 0.96,
                    "wspace": 0.035
                },
                "reject_behavior": {
                    "mode": "delete",
                    "preserve_original": false,
                    "suffix": "_reject"
                },
                "reject_button_color": "#c23b22",
                "scale_modifier": 1.0,
                "scroll_wheel_pan_multi_enabled": true,
                "shp_linewidth": 1.0,
                "shp_overlay_colors": [],
                "shp_primary_color": "cyan",
                "shp_primary_name": "",
                "summary_fontsize": 20.0,
                "sync_zoom_pan": true,
                "theme_mode": "dark",
                "thermal_alpha": 1.0,
                "thermal_blend_mode": "normal",
                "thermal_visual_resampling": "nearest",
                "title_fontsize": 18.641306958139815,
                "use_theme_nan_color": true,
                "warp_source_color": "#c23b22",
                "warp_target_color": "#2e8b57",
                "window_size": [
                    2358,
                    1422
                ]
            }
        },
        {
            "name": "Preset 10 - Earth",
            "settings": {
                "alt_cmap": "gist_earth_r",
                "base_multiplier": 1,
                "basemap_category": "",
                "basemap_cmap": "gray",
                "basemap_color_scaling": "normal",
                "basemap_mode": "nearest",
                "basemap_name": "",
                "basemap_resolution_mode": "dynamic",
                "basemap_visual_resampling": "nearest",
                "cmap_mode": "gist_earth_r",
                "filename_dt_pattern": "",
                "filename_dt_substring": "",
                "global_contrast_rel": [
                    0.0,
                    0.7737809375000002
                ],
                "global_edge_mode": false,
                "global_gamma": 2.488320000000004,
                "grid_cols": 2,
                "grid_rows": 1,
                "keep_behavior": {
                    "mode": "overwrite",
                    "preserve_original": false,
                    "suffix": "_keep"
                },
                "keep_button_color": "#56b4e9",
                "keep_reject_button_layout_settings": {
                    "size_scale": 1.0,
                    "spacing_px": 18.0
                },
                "keep_reject_button_preset": "sky_vermillion",
                "keyboard_shortcuts_lock_theme": false,
                "keyboard_shortcuts_theme_mode": "light",
                "nan_color": "black",
                "panel_layout_settings": {
                    "bottom": 0.03,
                    "hspace": 0.1,
                    "left": 0.01,
                    "right": 0.99,
                    "top": 0.96,
                    "wspace": 0.035
                },
                "reject_behavior": {
                    "mode": "delete",
                    "preserve_original": false,
                    "suffix": "_reject"
                },
                "reject_button_color": "#d55e00",
                "scale_modifier": 1.0,
                "scroll_wheel_pan_multi_enabled": true,
                "shp_linewidth": 1.0,
                "shp_overlay_colors": [],
                "shp_primary_color": "cyan",
                "shp_primary_name": "",
                "summary_fontsize": 20.0,
                "sync_zoom_pan": true,
                "theme_mode": "dark",
                "thermal_alpha": 1.0,
                "thermal_blend_mode": "normal",
                "thermal_visual_resampling": "nearest",
                "title_fontsize": 18.641306958139815,
                "use_theme_nan_color": true,
                "warp_source_color": "#c23b22",
                "warp_target_color": "#2e8b57",
                "window_size": [
                    2358,
                    1422
                ]
            }
        }
    ]
}
'''
# --- GEOVIEWER_PERSISTED_UI_SETTINGS_END ---

def _default_persisted_ui_settings():
    default_button_preset = KEEP_REJECT_BUTTON_PRESET_BY_ID[DEFAULT_KEEP_REJECT_PRESET_ID]
    return {
        "theme_mode": "dark",
        "window_size": [1400, 850],
        "keyboard_shortcuts_lock_theme": False,
        "keyboard_shortcuts_theme_mode": "light",
        "grid_cols": 3,
        "grid_rows": 1,
        "panel_layout_settings": normalize_panel_layout_settings(DEFAULT_PANEL_LAYOUT_SETTINGS),
        "sync_zoom_pan": False,
        "scroll_wheel_pan_multi_enabled": True,
        "use_theme_nan_color": True,
        "nan_color": "black",
        "thermal_alpha": 1.0,
        "thermal_blend_mode": "normal",
        "thermal_visual_resampling": "nearest",
        "basemap_visual_resampling": "nearest",
        "basemap_resolution_mode": "dynamic",
        "basemap_color_scaling": "normal",
        "basemap_cmap": "gray",
        "basemap_mode": "nearest",
        "basemap_category": "",
        "shp_linewidth": 1.2,
        "summary_fontsize": 11.0,
        "title_fontsize": 18.0,
        "warp_source_color": DEFAULT_WARP_SOURCE_COLOR,
        "warp_target_color": DEFAULT_WARP_TARGET_COLOR,
        "cmap_mode": "gray",
        "alt_cmap": "magma",
        "global_edge_mode": False,
        "global_contrast_rel": [0.0, 1.0],
        "global_gamma": 1.0,
        "base_multiplier": 1,
        "scale_modifier": 1.0,
        "keep_reject_button_preset": default_button_preset["id"],
        "keep_button_color": normalize_keep_reject_button_color(
            default_button_preset["keep"],
            default_button_preset["keep"],
        ),
        "reject_button_color": normalize_keep_reject_button_color(
            default_button_preset["reject"],
            default_button_preset["reject"],
        ),
        "keep_reject_button_layout_settings": normalize_keep_reject_button_layout_settings(
            DEFAULT_KEEP_REJECT_BUTTON_LAYOUT_SETTINGS
        ),
        "keep_behavior": {
            "mode": "overwrite",
            "suffix": "_keep",
            "preserve_original": False,
        },
        "filename_dt_substring": "",
        "filename_dt_pattern": "",
        "reject_behavior": {
            "mode": "delete",
            "suffix": "_reject",
            "preserve_original": False,
        },
        "basemap_name": "",
        "shp_primary_color": "lime",
        "shp_primary_name": "",
        "shp_overlay_colors": [],
    }

def _default_persisted_ui_store():
    return {
        "default_profile": "Default",
        "last_ui_scale": "auto",
        "last_main_panel_text_scale": 1.0,
        "profiles": [
            {
                "name": "Default",
                "settings": _default_persisted_ui_settings(),
            }
        ],
    }

def _coerce_persisted_keep_reject_behavior(data, fallback, action):
    out = dict(fallback)
    if isinstance(data, dict):
        mode = str(data.get("mode", fallback["mode"]) or fallback["mode"]).strip().lower()
        if action == "keep":
            out["mode"] = "suffix" if mode == "suffix" else "overwrite"
            out["suffix"] = str(data.get("suffix", fallback["suffix"]) or fallback["suffix"]).strip() or fallback["suffix"]
        else:
            out["mode"] = "suffix" if mode == "suffix" else "delete"
            out["suffix"] = str(data.get("suffix", fallback["suffix"]) or fallback["suffix"]).strip() or fallback["suffix"]
        out["preserve_original"] = bool(data.get("preserve_original", fallback["preserve_original"]))
    return out

def summarize_keep_reject_behavior(action, behavior):
    action = "reject" if str(action or "").strip().lower() == "reject" else "keep"
    fallback = {
        "mode": "delete" if action == "reject" else "overwrite",
        "suffix": "_reject" if action == "reject" else "_keep",
        "preserve_original": False,
    }
    vals = _coerce_persisted_keep_reject_behavior(behavior, fallback, action)
    mode = vals["mode"]
    suffix = str(vals.get("suffix") or fallback["suffix"]).strip() or fallback["suffix"]
    if mode == "suffix":
        summary = f"save to suffixed file ({suffix})"
    elif action == "reject":
        summary = "delete current file"
    else:
        summary = "overwrite current file"
    if bool(vals.get("preserve_original")):
        summary += "; preserve original"
    return summary

def available_matplotlib_colormaps():
    try:
        return sorted(list(plt.colormaps()))
    except Exception:
        try:
            from matplotlib import cm as _cm
            return sorted(list(getattr(_cm, "cmap_d", {}).keys()))
        except Exception:
            return ["gray", "magma"]

def discover_startup_shapefiles():
    shp_paths = sorted(glob.glob("*.shp"))
    if not shp_paths and SHAPEFILE and os.path.exists(SHAPEFILE):
        shp_paths = [SHAPEFILE]
    return shp_paths

def discover_basemap_paths(folder=None):
    folder = os.path.abspath(folder or os.getcwd())
    basemap_dir = os.path.join(folder, BASEMAP_FOLDER_NAME)
    return _unique_sorted_paths(
        glob.glob(os.path.join(basemap_dir, "*.tif"))
        + glob.glob(os.path.join(basemap_dir, "*.tiff"))
    )

def raster_choice_label(path):
    name = os.path.basename(str(path or ""))
    try:
        with rasterio.open(path) as src:
            crs_label = _reproject_crs_label(src.crs) if src.crs else "None"
            band_text = "1 band" if int(src.count) == 1 else f"{int(src.count)} bands"
            category = basemap_category_label(basemap_category_for_path(path))
            date = parse_basemap_acquisition_date(path)
            date_text = date.isoformat() if date is not None else "date unavailable"
            return f"{name} - {category} - {band_text} - {date_text} - CRS: {crs_label}"
    except Exception:
        return f"{name} - unreadable"

REPROJECT_BATCH_RETRIES = 15
REPROJECT_BATCH_RETRY_WAIT_SEC = 0.75
REPROJECT_TARGET_CRS_LABEL = "EPSG:4326"

def _unique_sorted_paths(paths):
    seen = set()
    out = []
    for path in paths:
        abs_path = os.path.abspath(path)
        key = os.path.normcase(abs_path)
        if key in seen or not os.path.isfile(abs_path):
            continue
        seen.add(key)
        out.append(abs_path)
    return sorted(out, key=lambda p: os.path.basename(p).lower())

def discover_reproject_file_paths(folder=None):
    folder = os.path.abspath(folder or os.getcwd())
    raster_paths = _unique_sorted_paths(
        glob.glob(os.path.join(folder, "*.tif"))
        + glob.glob(os.path.join(folder, "*.tiff"))
        + glob.glob(os.path.join(folder, BASEMAP_FOLDER_NAME, "*.tif"))
        + glob.glob(os.path.join(folder, BASEMAP_FOLDER_NAME, "*.tiff"))
    )
    shapefile_paths = _unique_sorted_paths(glob.glob(os.path.join(folder, "*.shp")))
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

def reproject_raster_to_4326_inplace(src_path):
    dst_crs = CRS.from_epsg(4326)
    tmp_path = None
    try:
        with rasterio.Env():
            with rasterio.open(src_path, sharing=False) as src:
                if src.crs is None:
                    return "Skipped: no CRS found."

                src_crs = CRS.from_user_input(src.crs)
                if src_crs == dst_crs:
                    return "Skipped: already EPSG:4326."

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

                nodata = src.nodata
                if nodata is not None:
                    profile["nodata"] = nodata

                fd, tmp_path = tempfile.mkstemp(
                    suffix=".tif",
                    prefix="reproj_",
                    dir=os.path.dirname(src_path) or ".",
                )
                os.close(fd)

                with rasterio.open(tmp_path, "w", **profile) as dst:
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

        _replace_with_reproject_retries(tmp_path, src_path)
        tmp_path = None
        return "Converted."
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

def reproject_shapefile_to_4326_inplace(src_path):
    if fiona is None or transform_geom is None:
        return "Skipped: Fiona is not installed."

    dst_crs_rio = CRS.from_epsg(4326)
    tmp_shp = None
    try:
        with fiona.Env():
            with fiona.open(src_path, "r") as src:
                src_crs_raw = src.crs or getattr(src, "crs_wkt", None)
                if not src_crs_raw:
                    return "Skipped: no CRS found."

                src_crs_rio = CRS.from_user_input(src_crs_raw)
                if src_crs_rio == dst_crs_rio:
                    return "Skipped: already EPSG:4326."

                meta = src.meta.copy()
                if FionaCRS is not None:
                    meta["crs"] = FionaCRS.from_epsg(4326)
                else:
                    meta["crs"] = REPROJECT_TARGET_CRS_LABEL
                meta.pop("crs_wkt", None)

                fd, tmp_shp = tempfile.mkstemp(
                    suffix=".shp",
                    prefix="reproj_",
                    dir=os.path.dirname(src_path) or ".",
                )
                os.close(fd)
                os.remove(tmp_shp)

                with fiona.open(tmp_shp, "w", **meta) as dst:
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

        _replace_shapefile_group(tmp_shp, src_path)
        tmp_shp = None
        return "Converted."
    finally:
        if tmp_shp:
            tmp_dir = os.path.dirname(tmp_shp) or "."
            tmp_root = os.path.splitext(os.path.basename(tmp_shp))[0]
            for leftover in glob.glob(os.path.join(tmp_dir, tmp_root + ".*")):
                try:
                    os.remove(leftover)
                except OSError:
                    pass

def batch_reproject_targets_to_4326(targets, progress_callback=None):
    convert_items = [item for item in list(targets or []) if item.get("will_convert")]
    total = len(convert_items)
    results = []
    _release_reproject_file_handles()
    for index, item in enumerate(convert_items, start=1):
        if progress_callback:
            progress_callback(index - 1, total, item, "Converting")
        try:
            if item.get("kind") == "raster":
                message = reproject_raster_to_4326_inplace(item["path"])
            else:
                message = reproject_shapefile_to_4326_inplace(item["path"])
            status = "converted" if str(message).lower().startswith("converted") else "skipped"
            results.append(dict(item, result_status=status, result_message=message))
        except Exception as exc:
            results.append(dict(item, result_status="error", result_message=str(exc)))
        if progress_callback:
            progress_callback(index, total, item, results[-1]["result_message"])
    return results

def restart_geoviewer_application(parent=None):
    args = list(sys.argv)
    if not args or not str(args[0] or "").strip():
        args = [os.path.abspath(__file__)]
    try:
        ok = QtCore.QProcess.startDetached(sys.executable, args, os.getcwd())
    except Exception:
        ok = False
    if not ok:
        try:
            QMessageBox.warning(
                parent,
                "Restart Application",
                "GeoViewer could not restart automatically. Close this window and run the application again.",
            )
        except Exception:
            pass
        return False
    app = QtWidgets.QApplication.instance()
    if app is not None:
        app.exit(0)
    return True

def normalize_persisted_ui_settings(settings):
    defaults = _default_persisted_ui_settings()
    src = dict(settings or {})
    out = dict(defaults)

    out["theme_mode"] = "light" if str(src.get("theme_mode", defaults["theme_mode"])).lower() == "light" else "dark"
    out["keyboard_shortcuts_lock_theme"] = bool(
        src.get("keyboard_shortcuts_lock_theme", defaults["keyboard_shortcuts_lock_theme"])
    )
    out["keyboard_shortcuts_theme_mode"] = (
        "light"
        if str(
            src.get("keyboard_shortcuts_theme_mode", defaults["keyboard_shortcuts_theme_mode"])
        ).lower() == "light"
        else "dark"
    )

    try:
        size = src.get("window_size", defaults["window_size"])
        if isinstance(size, (list, tuple)) and len(size) == 2:
            width = max(900, int(size[0]))
            height = max(650, int(size[1]))
            out["window_size"] = [width, height]
    except Exception:
        out["window_size"] = list(defaults["window_size"])

    try:
        out["grid_cols"] = max(1, min(7, int(src.get("grid_cols", defaults["grid_cols"]))))
    except Exception:
        out["grid_cols"] = defaults["grid_cols"]
    try:
        out["grid_rows"] = max(1, min(7, int(src.get("grid_rows", defaults["grid_rows"]))))
    except Exception:
        out["grid_rows"] = defaults["grid_rows"]
    out["panel_layout_settings"] = normalize_panel_layout_settings(
        src.get("panel_layout_settings")
    )
    out["sync_zoom_pan"] = bool(src.get("sync_zoom_pan", defaults["sync_zoom_pan"]))
    out["scroll_wheel_pan_multi_enabled"] = bool(
        src.get(
            "scroll_wheel_pan_multi_enabled",
            defaults["scroll_wheel_pan_multi_enabled"],
        )
    )

    out["use_theme_nan_color"] = bool(src.get("use_theme_nan_color", defaults["use_theme_nan_color"]))
    out["nan_color"] = normalize_nan_override_value(src.get("nan_color"), defaults["nan_color"])
    try:
        out["thermal_alpha"] = max(0.0, min(1.0, float(src.get("thermal_alpha", defaults["thermal_alpha"]))))
    except Exception:
        out["thermal_alpha"] = defaults["thermal_alpha"]
    out["thermal_blend_mode"] = normalize_thermal_blend_mode(
        src.get("thermal_blend_mode"),
        defaults["thermal_blend_mode"],
    )
    out["thermal_visual_resampling"] = normalize_visual_resampling(
        src.get("thermal_visual_resampling"),
        defaults["thermal_visual_resampling"],
    )
    out["basemap_visual_resampling"] = normalize_visual_resampling(
        src.get("basemap_visual_resampling"),
        defaults["basemap_visual_resampling"],
    )
    out["basemap_resolution_mode"] = normalize_basemap_resolution_mode(
        src.get("basemap_resolution_mode"),
        defaults["basemap_resolution_mode"],
    )
    out["basemap_color_scaling"] = normalize_basemap_color_scaling(
        src.get("basemap_color_scaling"),
        defaults["basemap_color_scaling"],
    )
    out["basemap_cmap"] = normalize_basemap_cmap(
        src.get("basemap_cmap"),
        defaults["basemap_cmap"],
    )
    saved_basemap_name = normalize_persisted_basemap_name(src.get("basemap_name"))
    out["basemap_mode"] = normalize_basemap_mode(
        src.get("basemap_mode"),
        "single" if saved_basemap_name else defaults["basemap_mode"],
    )
    out["basemap_category"] = normalize_basemap_category(
        src.get("basemap_category", defaults["basemap_category"])
    )

    try:
        out["shp_linewidth"] = max(0.1, float(src.get("shp_linewidth", defaults["shp_linewidth"])))
    except Exception:
        out["shp_linewidth"] = defaults["shp_linewidth"]
    try:
        out["summary_fontsize"] = max(6.0, min(40.0, float(src.get("summary_fontsize", defaults["summary_fontsize"]))))
    except Exception:
        out["summary_fontsize"] = defaults["summary_fontsize"]
    try:
        out["title_fontsize"] = max(8.0, min(40.0, float(src.get("title_fontsize", defaults["title_fontsize"]))))
    except Exception:
        out["title_fontsize"] = defaults["title_fontsize"]

    out["cmap_mode"] = str(src.get("cmap_mode", defaults["cmap_mode"]) or defaults["cmap_mode"])
    out["alt_cmap"] = str(src.get("alt_cmap", defaults["alt_cmap"]) or defaults["alt_cmap"])
    out["global_edge_mode"] = bool(src.get("global_edge_mode", defaults["global_edge_mode"]))

    contrast = src.get("global_contrast_rel", defaults["global_contrast_rel"])
    try:
        if isinstance(contrast, (list, tuple)) and len(contrast) == 2:
            center_rel = float(contrast[0])
            half_rel = max(1e-12, float(contrast[1]))
            out["global_contrast_rel"] = [center_rel, half_rel]
        else:
            out["global_contrast_rel"] = list(defaults["global_contrast_rel"])
    except Exception:
        out["global_contrast_rel"] = list(defaults["global_contrast_rel"])

    try:
        out["global_gamma"] = max(1e-12, float(src.get("global_gamma", defaults["global_gamma"])))
    except Exception:
        out["global_gamma"] = defaults["global_gamma"]

    try:
        out["base_multiplier"] = max(1, int(src.get("base_multiplier", defaults["base_multiplier"])))
    except Exception:
        out["base_multiplier"] = defaults["base_multiplier"]
    try:
        out["scale_modifier"] = max(1e-3, float(src.get("scale_modifier", defaults["scale_modifier"])))
    except Exception:
        out["scale_modifier"] = defaults["scale_modifier"]

    keep_color = normalize_keep_reject_button_color(
        src.get("keep_button_color"),
        defaults["keep_button_color"],
    )
    reject_color = normalize_keep_reject_button_color(
        src.get("reject_button_color"),
        defaults["reject_button_color"],
    )
    out["keep_button_color"] = keep_color
    out["reject_button_color"] = reject_color
    out["keep_reject_button_preset"] = infer_keep_reject_button_preset(keep_color, reject_color)
    out["keep_reject_button_layout_settings"] = normalize_keep_reject_button_layout_settings(
        src.get("keep_reject_button_layout_settings")
    )
    out["warp_source_color"] = normalize_keep_reject_button_color(
        src.get("warp_source_color"),
        defaults["warp_source_color"],
    )
    out["warp_target_color"] = normalize_keep_reject_button_color(
        src.get("warp_target_color"),
        defaults["warp_target_color"],
    )

    out["keep_behavior"] = _coerce_persisted_keep_reject_behavior(
        src.get("keep_behavior"),
        defaults["keep_behavior"],
        "keep",
    )
    out["reject_behavior"] = _coerce_persisted_keep_reject_behavior(
        src.get("reject_behavior"),
        defaults["reject_behavior"],
        "reject",
    )
    out["filename_dt_substring"] = normalize_filename_datetime_substring(
        src.get("filename_dt_substring")
    )
    out["filename_dt_pattern"] = normalize_filename_datetime_pattern(
        src.get("filename_dt_pattern")
    )

    out["basemap_name"] = saved_basemap_name

    out["shp_primary_color"] = normalize_picker_color_name(
        src.get("shp_primary_color"),
        defaults["shp_primary_color"],
    )
    out["shp_primary_name"] = normalize_persisted_shapefile_name(
        src.get("shp_primary_name")
    )

    overlay_colors = []
    for item in list(src.get("shp_overlay_colors", []) or []):
        if not isinstance(item, dict):
            continue
        name = normalize_persisted_shapefile_name(item.get("name"))
        color = normalize_picker_color_name(item.get("color"), "lime")
        if not name:
            continue
        overlay_colors.append({"name": name, "color": color})
    out["shp_overlay_colors"] = overlay_colors

    return out

def _normalize_persisted_ui_profile_name(name, fallback="Profile"):
    text = str(name or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:60] if text else fallback

def _find_persisted_ui_profile(store, profile_name=None):
    normalized_store = normalize_persisted_ui_store(store)
    target_name = str(profile_name or normalized_store.get("default_profile") or "").strip().lower()

    for profile in normalized_store.get("profiles", []):
        if str(profile.get("name") or "").strip().lower() == target_name:
            return {
                "name": profile["name"],
                "settings": normalize_persisted_ui_settings(profile.get("settings")),
            }

    fallback = normalized_store["profiles"][0]
    return {
        "name": fallback["name"],
        "settings": normalize_persisted_ui_settings(fallback.get("settings")),
    }

def normalize_persisted_ui_store(data):
    defaults = _default_persisted_ui_store()
    default_profile_name = defaults["default_profile"]
    raw_profiles = []
    raw_default_name = default_profile_name
    raw_last_ui_scale = None
    raw_last_main_panel_text_scale = None

    if isinstance(data, dict) and isinstance(data.get("profiles"), list):
        raw_profiles = list(data.get("profiles") or [])
        raw_default_name = str(data.get("default_profile") or default_profile_name).strip() or default_profile_name
        raw_last_ui_scale = data.get("last_ui_scale", data.get("ui_scale"))
        raw_last_main_panel_text_scale = data.get(
            "last_main_panel_text_scale",
            data.get("main_panel_text_scale"),
        )
    else:
        raw_profiles = [{"name": default_profile_name, "settings": data if isinstance(data, dict) else {}}]
        if isinstance(data, dict):
            raw_last_ui_scale = data.get("last_ui_scale", data.get("ui_scale"))
            raw_last_main_panel_text_scale = data.get(
                "last_main_panel_text_scale",
                data.get("main_panel_text_scale"),
            )

    def legacy_profile_setting(key):
        candidates = []
        for item in raw_profiles:
            if not isinstance(item, dict):
                continue
            if str(item.get("name") or "").strip().lower() == raw_default_name.lower():
                candidates.insert(0, item)
            else:
                candidates.append(item)
        for item in candidates:
            settings = item.get("settings") if isinstance(item, dict) else None
            if isinstance(settings, dict) and key in settings:
                return settings.get(key)
        return None

    legacy_current_settings = data.get("settings") if isinstance(data, dict) else None
    if isinstance(legacy_current_settings, dict):
        if raw_last_ui_scale is None:
            raw_last_ui_scale = legacy_current_settings.get("ui_scale")
        if raw_last_main_panel_text_scale is None:
            raw_last_main_panel_text_scale = legacy_current_settings.get("main_panel_text_scale")

    if raw_last_ui_scale is None:
        raw_last_ui_scale = legacy_profile_setting("ui_scale")
    if raw_last_main_panel_text_scale is None:
        raw_last_main_panel_text_scale = legacy_profile_setting("main_panel_text_scale")

    last_ui_scale = normalize_persisted_ui_scale(
        raw_last_ui_scale,
        defaults["last_ui_scale"],
    )
    last_main_panel_text_scale = normalize_main_panel_text_scale(
        raw_last_main_panel_text_scale,
        defaults["last_main_panel_text_scale"],
    )

    profiles = []
    used_names = set()
    for idx, item in enumerate(raw_profiles):
        if not isinstance(item, dict):
            continue
        base_name = _normalize_persisted_ui_profile_name(item.get("name"), f"Profile {idx + 1}")
        name = base_name
        suffix_idx = 2
        while name.lower() in used_names:
            name = f"{base_name} ({suffix_idx})"
            suffix_idx += 1
        used_names.add(name.lower())
        profiles.append({
            "name": name,
            "settings": normalize_persisted_ui_settings(item.get("settings")),
        })
        if len(profiles) >= MAX_PERSISTED_UI_PROFILES:
            break

    if not profiles:
        profiles = list(defaults["profiles"])

    default_profile = None
    for profile in profiles:
        if profile["name"].lower() == raw_default_name.lower():
            default_profile = profile["name"]
            break
    if default_profile is None:
        default_profile = profiles[0]["name"]

    return {
        "default_profile": default_profile,
        "last_ui_scale": last_ui_scale,
        "last_main_panel_text_scale": last_main_panel_text_scale,
        "profiles": profiles,
    }

def update_persisted_scale_state(store, ui_scale_value=None, main_panel_text_scale=None):
    updated = normalize_persisted_ui_store(store)
    if ui_scale_value is not None:
        updated["last_ui_scale"] = normalize_persisted_ui_scale(
            ui_scale_value,
            updated.get("last_ui_scale", "auto"),
        )
    if main_panel_text_scale is not None:
        updated["last_main_panel_text_scale"] = normalize_main_panel_text_scale(
            main_panel_text_scale,
            updated.get("last_main_panel_text_scale", 1.0),
        )
    return normalize_persisted_ui_store(updated)

def load_persisted_ui_store():
    try:
        data = json.loads(PERSISTED_UI_SETTINGS_JSON)
    except Exception:
        data = {}
    return normalize_persisted_ui_store(data)

def load_persisted_ui_settings(store=None, profile_name=None):
    selected = _find_persisted_ui_profile(
        load_persisted_ui_store() if store is None else store,
        profile_name=profile_name,
    )
    return dict(selected["settings"])

def _sanitize_settings_profile_export_filename(name):
    text = _normalize_persisted_ui_profile_name(name, "GeoViewer_Settings_Profile")
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", text)
    text = re.sub(r"\s+", " ", text).strip(" ._")
    return (text or "GeoViewer_Settings_Profile")[:80]

def _settings_profile_export_payload(profile_name, settings):
    name = _normalize_persisted_ui_profile_name(profile_name, "Profile")
    return {
        "profile": {
            "name": name,
            "settings": normalize_persisted_ui_settings(settings),
        }
    }

def settings_profile_settings_sha256(settings):
    canonical = json.dumps(
        settings,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

def build_settings_profile_export_text(profile_name, settings, newline="\n"):
    payload = _settings_profile_export_payload(profile_name, settings)
    profile_name_json = json.dumps(payload["profile"]["name"])
    checksum = settings_profile_settings_sha256(payload["profile"]["settings"])
    json_payload = json.dumps(payload, indent=4, sort_keys=True)
    text = "\n".join([
        SETTINGS_PROFILE_EXPORT_MAGIC,
        f"FORMAT_VERSION={SETTINGS_PROFILE_EXPORT_VERSION}",
        f"{SETTINGS_PROFILE_EXPORT_PROFILE_PREFIX}{profile_name_json}",
        f"{SETTINGS_PROFILE_EXPORT_SHA256_PREFIX}{checksum}",
        SETTINGS_PROFILE_EXPORT_JSON_BEGIN,
        json_payload,
        SETTINGS_PROFILE_EXPORT_JSON_END,
        "",
    ])
    return text.replace("\n", newline)

def _check_export_settings_key_shape(settings):
    errors = []
    if not isinstance(settings, dict):
        return ["Settings payload is not a JSON object."]

    expected_keys = set(_default_persisted_ui_settings().keys())
    actual_keys = set(settings.keys())
    optional_missing_keys = {"scroll_wheel_pan_multi_enabled"}
    legacy_extra_keys = {"ui_scale", "main_panel_text_scale"}
    missing = sorted((expected_keys - actual_keys) - optional_missing_keys)
    extra = sorted((actual_keys - expected_keys) - legacy_extra_keys)
    if missing:
        errors.append("Missing settings keys: " + ", ".join(missing[:8]) + ("..." if len(missing) > 8 else ""))
    if extra:
        errors.append("Unexpected settings keys: " + ", ".join(extra[:8]) + ("..." if len(extra) > 8 else ""))

    nested_expectations = {
        "panel_layout_settings": set(DEFAULT_PANEL_LAYOUT_SETTINGS.keys()),
        "keep_reject_button_layout_settings": set(DEFAULT_KEEP_REJECT_BUTTON_LAYOUT_SETTINGS.keys()),
        "keep_behavior": {"mode", "suffix", "preserve_original"},
        "reject_behavior": {"mode", "suffix", "preserve_original"},
    }
    for key, expected in nested_expectations.items():
        value = settings.get(key)
        if not isinstance(value, dict):
            errors.append(f"{key} must be a JSON object.")
            continue
        actual = set(value.keys())
        missing_nested = sorted(expected - actual)
        extra_nested = sorted(actual - expected)
        if missing_nested:
            errors.append(f"{key} is missing: " + ", ".join(missing_nested))
        if extra_nested:
            errors.append(f"{key} has unexpected keys: " + ", ".join(extra_nested))

    overlay_colors = settings.get("shp_overlay_colors", [])
    if not isinstance(overlay_colors, list):
        errors.append("shp_overlay_colors must be a JSON list.")
    else:
        for idx, item in enumerate(overlay_colors):
            if not isinstance(item, dict) or set(item.keys()) != {"name", "color"}:
                errors.append(f"shp_overlay_colors item {idx + 1} must contain only name and color.")
                break

    return errors

def find_settings_profile_missing_local_references(settings, folder=None):
    missing = []
    if not isinstance(settings, dict):
        return missing
    try:
        base_folder = os.path.abspath(folder or os.getcwd())
    except Exception:
        base_folder = os.path.abspath(".")

    basemap_name = normalize_persisted_basemap_name(settings.get("basemap_name"))
    if basemap_name:
        basemap_path = os.path.join(base_folder, BASEMAP_FOLDER_NAME, basemap_name)
        if not os.path.isfile(basemap_path):
            missing.append(f"Basemap not found in {BASEMAP_FOLDER_NAME}: {basemap_name}")

    primary_name = normalize_persisted_shapefile_name(settings.get("shp_primary_name"))
    seen_shp_names = set()
    if primary_name:
        seen_shp_names.add(primary_name.lower())
        primary_path = os.path.join(base_folder, primary_name)
        if not os.path.isfile(primary_path):
            missing.append(f"Primary shapefile not found in main folder: {primary_name}")

    for item in list(settings.get("shp_overlay_colors", []) or []):
        if not isinstance(item, dict):
            continue
        overlay_name = normalize_persisted_shapefile_name(item.get("name"))
        if not overlay_name:
            continue
        overlay_key = overlay_name.lower()
        if overlay_key in seen_shp_names:
            continue
        seen_shp_names.add(overlay_key)
        overlay_path = os.path.join(base_folder, overlay_name)
        if not os.path.isfile(overlay_path):
            missing.append(f"Overlay shapefile not found in main folder: {overlay_name}")

    return missing

def validate_settings_profile_export_text(text):
    result = {
        "valid": False,
        "profile_name": "",
        "settings": None,
        "errors": [],
        "warnings": [],
        "missing_references": [],
        "checksum_ok": False,
        "checksum_expected": "",
        "checksum_actual": "",
        "size_bytes": 0,
    }
    if not isinstance(text, str):
        text = str(text or "")
    text = text.lstrip("\ufeff")
    result["size_bytes"] = len(text.encode("utf-8"))
    errors = result["errors"]

    if result["size_bytes"] < SETTINGS_PROFILE_EXPORT_MIN_BYTES:
        errors.append("File is smaller than a normal GeoViewer settings export.")
    if result["size_bytes"] > SETTINGS_PROFILE_EXPORT_MAX_BYTES:
        errors.append("File is larger than a normal GeoViewer settings export.")

    lines = text.splitlines()
    if len(lines) < 8:
        errors.append("File does not contain the expected export line layout.")
        return result

    expected_format = f"FORMAT_VERSION={SETTINGS_PROFILE_EXPORT_VERSION}"
    if lines[0].strip() != SETTINGS_PROFILE_EXPORT_MAGIC:
        errors.append("Line 1 is not the GeoViewer settings export marker.")
    if len(lines) <= 1 or lines[1].strip() != expected_format:
        errors.append("Line 2 is not the expected export format version.")
    if len(lines) <= 2 or not lines[2].startswith(SETTINGS_PROFILE_EXPORT_PROFILE_PREFIX):
        errors.append("Line 3 is not the expected profile-name line.")
    json_start_idx = 4
    if len(lines) <= 3 or not lines[3].startswith(SETTINGS_PROFILE_EXPORT_SHA256_PREFIX):
        errors.append("Line 4 is not the expected SHA-256 checksum line.")
        if len(lines) > 3 and lines[3].strip() == SETTINGS_PROFILE_EXPORT_JSON_BEGIN:
            json_start_idx = 3
    else:
        result["checksum_expected"] = lines[3][len(SETTINGS_PROFILE_EXPORT_SHA256_PREFIX):].strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", result["checksum_expected"]):
            errors.append("Line 4 does not contain a valid SHA-256 checksum.")
    if len(lines) <= json_start_idx or lines[json_start_idx].strip() != SETTINGS_PROFILE_EXPORT_JSON_BEGIN:
        errors.append(f"Line {json_start_idx + 1} is not the expected JSON begin marker.")

    tail_idx = len(lines) - 1
    while tail_idx >= 0 and not lines[tail_idx].strip():
        tail_idx -= 1
    if tail_idx < 0 or lines[tail_idx].strip() != SETTINGS_PROFILE_EXPORT_JSON_END:
        errors.append("The final non-empty line is not the expected JSON end marker.")
        tail_idx = len(lines)

    header_name = ""
    if len(lines) > 2 and lines[2].startswith(SETTINGS_PROFILE_EXPORT_PROFILE_PREFIX):
        raw_name = lines[2][len(SETTINGS_PROFILE_EXPORT_PROFILE_PREFIX):].strip()
        try:
            header_name = _normalize_persisted_ui_profile_name(json.loads(raw_name), "")
        except Exception:
            errors.append("The profile-name line is not valid JSON text.")

    json_lines = lines[json_start_idx + 1:tail_idx]
    if not json_lines or json_lines[0].strip() != "{":
        errors.append("The exported JSON does not start at the expected line.")
    try:
        payload = json.loads("\n".join(json_lines))
    except Exception as e:
        errors.append(f"The settings JSON could not be parsed: {e}")
        return result

    if not isinstance(payload, dict) or set(payload.keys()) != {"profile"}:
        errors.append("The top-level JSON object does not match a GeoViewer profile export.")
        return result
    profile = payload.get("profile")
    if not isinstance(profile, dict) or set(profile.keys()) != {"name", "settings"}:
        errors.append("The profile JSON object does not contain the expected name/settings fields.")
        return result

    profile_name = _normalize_persisted_ui_profile_name(profile.get("name"), "")
    if not profile_name:
        errors.append("The exported profile name is blank.")
    elif header_name and profile_name.lower() != header_name.lower():
        errors.append("The header profile name does not match the JSON profile name.")

    settings = profile.get("settings")
    errors.extend(_check_export_settings_key_shape(settings))
    if isinstance(settings, dict):
        try:
            result["checksum_actual"] = settings_profile_settings_sha256(settings)
            if result["checksum_expected"] and result["checksum_actual"] == result["checksum_expected"]:
                result["checksum_ok"] = True
            elif result["checksum_expected"]:
                errors.append("SHA-256 checksum does not match the exported settings JSON.")
        except Exception as e:
            errors.append(f"Could not calculate SHA-256 checksum: {e}")

        normalized_settings = normalize_persisted_ui_settings(settings)
        expected_size = len(build_settings_profile_export_text(profile_name, normalized_settings).encode("utf-8"))
        lower_size = int(expected_size * 0.65)
        upper_size = int(expected_size * 1.35)
        if result["size_bytes"] < lower_size or result["size_bytes"] > upper_size:
            errors.append("File size is not close to the expected GeoViewer export size.")
        result["settings"] = normalized_settings
        result["profile_name"] = profile_name

    result["valid"] = not errors
    return result

def validate_settings_profile_export_file(path, main_folder=None):
    result = {
        "valid": False,
        "profile_name": "",
        "settings": None,
        "errors": [],
        "warnings": [],
        "missing_references": [],
        "checksum_ok": False,
        "checksum_expected": "",
        "checksum_actual": "",
        "size_bytes": 0,
        "file_path": os.path.abspath(str(path)),
    }
    try:
        result["size_bytes"] = int(os.path.getsize(path))
    except Exception as e:
        result["errors"].append(f"Could not read file size: {e}")
        return result
    if result["size_bytes"] > SETTINGS_PROFILE_EXPORT_MAX_BYTES * 4:
        result["errors"].append("File is far larger than a GeoViewer settings export.")
        return result
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except Exception as e:
        result["errors"].append(f"Could not read text file: {e}")
        return result

    result.update(validate_settings_profile_export_text(text))
    result["file_path"] = os.path.abspath(str(path))
    if result.get("valid"):
        missing = find_settings_profile_missing_local_references(
            result.get("settings"),
            folder=main_folder or os.path.dirname(os.path.abspath(str(path))),
        )
        result["missing_references"] = missing
        if missing:
            result.setdefault("warnings", []).extend(missing)
    return result

def _build_user_comment_flags_block(flags, newline):
    payload = json.dumps(normalize_comment_flags(flags), indent=4)
    block = (
        f"{_COMMENT_FLAGS_START_MARKER}\n"
        "USER_COMMENT_FLAGS_JSON = r'''\n"
        f"{payload}\n"
        "'''\n"
        f"{_COMMENT_FLAGS_END_MARKER}"
    )
    return block.replace("\n", newline)

def save_user_comment_flags_to_script(script_path, flags):
    script_path = os.path.abspath(str(script_path))
    normalized = normalize_comment_flags(flags)

    with open(script_path, "r", encoding="utf-8") as fh:
        source = fh.read()

    start_idx = source.find(_COMMENT_FLAGS_START_MARKER)
    end_idx = source.find(_COMMENT_FLAGS_END_MARKER)
    if start_idx < 0 or end_idx < 0 or end_idx < start_idx:
        raise RuntimeError("Could not find the comment flags block in the script.")

    end_idx += len(_COMMENT_FLAGS_END_MARKER)
    newline = "\r\n" if "\r\n" in source else "\n"
    updated_source = (
        source[:start_idx]
        + _build_user_comment_flags_block(normalized, newline)
        + source[end_idx:]
    )

    tmp_path = script_path + ".comment_flags.tmp"
    with open(tmp_path, "w", encoding="utf-8", newline="") as fh:
        fh.write(updated_source)
    os.replace(tmp_path, script_path)
    globals()["USER_COMMENT_FLAGS_JSON"] = json.dumps(normalized, indent=4)
    return normalized

def _build_persisted_ui_settings_block(store, newline):
    payload = json.dumps(normalize_persisted_ui_store(store), indent=4, sort_keys=True)
    block = (
        f"{_PERSISTED_UI_SETTINGS_START_MARKER}\n"
        "PERSISTED_UI_SETTINGS_JSON = r'''\n"
        f"{payload}\n"
        "'''\n"
        f"{_PERSISTED_UI_SETTINGS_END_MARKER}"
    )
    return block.replace("\n", newline)

def save_persisted_ui_store_to_script(script_path, store):
    script_path = os.path.abspath(str(script_path))
    normalized = normalize_persisted_ui_store(store)

    with open(script_path, "r", encoding="utf-8") as fh:
        source = fh.read()

    start_idx = source.find(_PERSISTED_UI_SETTINGS_START_MARKER)
    end_idx = source.find(_PERSISTED_UI_SETTINGS_END_MARKER)
    if start_idx < 0 or end_idx < 0 or end_idx < start_idx:
        raise RuntimeError("Could not find the persisted settings block in the script.")

    end_idx += len(_PERSISTED_UI_SETTINGS_END_MARKER)
    newline = "\r\n" if "\r\n" in source else "\n"
    updated_source = (
        source[:start_idx]
        + _build_persisted_ui_settings_block(normalized, newline)
        + source[end_idx:]
    )

    tmp_path = script_path + ".settings.tmp"
    with open(tmp_path, "w", encoding="utf-8", newline="") as fh:
        fh.write(updated_source)
    os.replace(tmp_path, script_path)
    globals()["PERSISTED_UI_SETTINGS_JSON"] = json.dumps(normalized, indent=4, sort_keys=True)
    return normalized

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

        self.theme_mode = get_app_theme_mode()
        self.theme = build_theme_palette(self.theme_mode)
        self.setStyleSheet(f"""
        QDialog {{ background-color: {self.theme['window_bg']}; color: {self.theme['text']}; }}
        QLabel  {{ color: {self.theme['text']}; font-size: 12pt; }}
        QGroupBox {{
            color: {self.theme['text']}; font-weight: 600; font-size: 12pt;
            border: 1px solid {self.theme['border']}; border-radius: 8px;
            margin-top: 12px; padding: 10px 10px 8px 10px;
            background-color: {self.theme['group_bg']};
        }}
        QGroupBox::title {{
            subcontrol-origin: margin; left: 10px; top: 0px;
            padding: 0 4px; background-color: {self.theme['window_bg']}; color: {self.theme['heading']};
        }}
        QComboBox {{
            background-color: {self.theme['input_bg']}; color: {self.theme['text']};
            border: 1px solid {self.theme['border']}; border-radius: 6px;
            padding: 4px 8px; font-size: 12pt; min-height: 28px;
        }}
        QComboBox:disabled {{
            color: {self.theme['disabled_text']}; background-color: {self.theme['group_bg']}; border-color: {self.theme['border']};
        }}
        QComboBox QAbstractItemView {{
            background-color: {self.theme['list_bg']}; color: {self.theme['text']};
            selection-background-color: {self.theme['selection_bg']}; selection-color: {self.theme['selection_text']};
            border: 1px solid {self.theme['border']};
        }}
        QCheckBox {{
            color: {self.theme['text']}; font-size: 12pt; spacing: 6px;
        }}
        QPushButton {{
            background-color: {self.theme['button_bg']}; color: {self.theme['button_text']};
            border: 1px solid {self.theme['border']}; border-radius: 8px;
            padding: 6px 12px; font-size: 11pt;
        }}
        QPushButton:hover  {{ background-color: {self.theme['button_hover']}; }}
        QPushButton:pressed{{ background-color: {self.theme['button_pressed']}; }}
        QDialogButtonBox QPushButton {{ min-width: 90px; }}
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

class ReprojectCountGraphic(QtWidgets.QFrame):
    """Small startup graphic showing how many files will be reprojected."""

    def __init__(self, theme, parent=None):
        super().__init__(parent)
        self.theme = dict(theme or {})
        self.counts = summarize_reproject_targets([])
        self.setMinimumHeight(126)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        self.apply_theme(theme)

    def apply_theme(self, theme):
        self.theme = dict(theme or {})
        self.setStyleSheet(
            f"""
            QFrame {{
                background-color: {self.theme.get('panel_bg', '#101010')};
                border: 1px solid {self.theme.get('border', '#3A3A3A')};
                border-radius: 8px;
            }}
            """
        )
        self.update()

    def sizeHint(self):
        return QtCore.QSize(420, 126)

    def set_counts(self, counts):
        self.counts = dict(counts or summarize_reproject_targets([]))
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)

        rect = QtCore.QRectF(self.contentsRect().adjusted(14, 12, -14, -12))
        if rect.width() <= 0 or rect.height() <= 0:
            return

        text_color = QtGui.QColor(self.theme.get("text", "#D3D3D3"))
        muted_color = QtGui.QColor(self.theme.get("muted", "#B0B0B0"))
        raster_color = QtGui.QColor("#56B4E9")
        shp_color = QtGui.QColor("#2E8B57")
        total_color = QtGui.QColor(self.theme.get("selection_bg", "#2D5FFF"))

        total = int(self.counts.get("convert_total", 0))
        raster_count = int(self.counts.get("raster_convert", 0))
        shp_count = int(self.counts.get("shapefile_convert", 0))
        max_count = max(1, raster_count, shp_count)

        left_w = min(186.0, rect.width() * 0.42)
        total_rect = QtCore.QRectF(rect.left(), rect.top(), left_w, rect.height())
        bar_rect = QtCore.QRectF(rect.left() + left_w + 16.0, rect.top(), rect.width() - left_w - 16.0, rect.height())

        painter.setPen(QtGui.QPen(total_color, 2.0))
        painter.setBrush(QtGui.QColor(total_color.red(), total_color.green(), total_color.blue(), 42))
        circle_d = min(total_rect.width(), total_rect.height()) * 0.95
        circle = QtCore.QRectF(
            total_rect.center().x() - circle_d / 2.0,
            total_rect.center().y() - circle_d / 2.0 - 4.0,
            circle_d,
            circle_d,
        )
        painter.drawEllipse(circle)

        count_text = str(total)
        count_font = QtGui.QFont("Lucida Console", 26, QtGui.QFont.Bold)
        if len(count_text) > 3:
            max_text_w = circle.width() * 0.84
            max_text_h = circle.height() * 0.62
            for point_size in range(25, 11, -1):
                count_font.setPointSize(point_size)
                metrics = QtGui.QFontMetricsF(count_font)
                text_bounds = metrics.boundingRect(count_text)
                if text_bounds.width() <= max_text_w and text_bounds.height() <= max_text_h:
                    break
        painter.setFont(count_font)
        painter.setPen(text_color)
        painter.drawText(circle, QtCore.Qt.AlignCenter, count_text)

        label_font = QtGui.QFont("Lucida Console", 8, QtGui.QFont.Bold)
        painter.setFont(label_font)
        painter.setPen(muted_color)
        label_rect = QtCore.QRectF(total_rect.left() - 6.0, circle.bottom() - 2.0, total_rect.width() + 12.0, 24.0)
        painter.drawText(label_rect, QtCore.Qt.AlignCenter, "FILES TO CONVERT")

        row_h = min(34.0, (bar_rect.height() - 16.0) / 2.0)
        rows = [
            ("TIFF", raster_count, raster_color, bar_rect.top() + 10.0),
            ("SHP", shp_count, shp_color, bar_rect.top() + 10.0 + row_h + 14.0),
        ]
        name_w = min(86.0, bar_rect.width() * 0.34)
        value_w = 58.0
        scaled_max = max(math.log(max_count + 1.0, 1000.0), 1e-9)
        for label, value, color, y in rows:
            label_area = QtCore.QRectF(bar_rect.left(), y, name_w, row_h)
            painter.setFont(QtGui.QFont("Lucida Console", 10, QtGui.QFont.Bold))
            painter.setPen(text_color)
            painter.drawText(label_area, QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft, label)

            track = QtCore.QRectF(
                bar_rect.left() + name_w,
                y + row_h * 0.28,
                max(1.0, bar_rect.width() - name_w - value_w - 8.0),
                row_h * 0.44,
            )
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(QtGui.QColor(self.theme.get("button_bg", "#202020")))
            painter.drawRoundedRect(track, 4.0, 4.0)
            scaled_value = math.log(value + 1.0, 1000.0) if value > 0 else 0.0
            fill_w = track.width() * min(1.0, scaled_value / scaled_max)
            if value > 0:
                painter.setBrush(color)
                painter.drawRoundedRect(QtCore.QRectF(track.left(), track.top(), fill_w, track.height()), 4.0, 4.0)

            value_area = QtCore.QRectF(track.right() + 8.0, y, value_w, row_h)
            painter.setFont(QtGui.QFont("Lucida Console", 12, QtGui.QFont.Bold))
            painter.setPen(text_color)
            painter.drawText(value_area, QtCore.Qt.AlignVCenter | QtCore.Qt.AlignRight, str(value))

class ReprojectWorkflowDialog(QtWidgets.QDialog):
    """Single reprojection dialog for confirmation, progress, results, and details."""

    def __init__(self, counts, convert_items, folder, theme, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Reproject To EPSG:4326")
        self.setModal(True)

        self.counts = dict(counts or {})
        self.convert_items = list(convert_items or [])
        self.folder = str(folder or os.getcwd())
        self.theme = dict(theme or build_theme_palette())
        self.results = None
        self.restart_requested = False
        self._running = False

        base_font = self.font()
        base_font.setPointSize(11)
        self.setFont(base_font)

        self.setStyleSheet(f"""
        QDialog {{ background-color: {self.theme['window_bg']}; color: {self.theme['text']}; }}
        QLabel {{ color: {self.theme['text']}; font-size: 11pt; }}
        QLabel#ReprojectWorkflowTitle {{
            color: {self.theme['heading']}; font-size: 15pt; font-weight: 700;
        }}
        QLabel#ReprojectWorkflowStatus {{
            color: {self.theme['heading']}; font-size: 11pt; font-weight: 600;
        }}
        QLabel#ReprojectWorkflowWarningIcon {{
            background-color: #C23B22;
            color: #FFFFFF;
            border-radius: 17px;
            font-size: 19pt;
            font-weight: 900;
        }}
        QLabel#ReprojectWorkflowWarningText {{
            color: #C23B22;
            font-size: 11pt;
            font-weight: 700;
        }}
        QPlainTextEdit {{
            background-color: {self.theme['input_bg']}; color: {self.theme['text']};
            border: 1px solid {self.theme['border']}; border-radius: 6px;
            padding: 8px; font-size: 10pt;
        }}
        QProgressBar {{
            background-color: {self.theme['input_bg']};
            color: {self.theme['text']};
            border: 1px solid {self.theme['border']};
            border-radius: 6px;
            min-height: 28px;
            text-align: center;
        }}
        QProgressBar::chunk {{
            background-color: {self.theme['selection_bg']};
            border-radius: 5px;
        }}
        QPushButton {{
            background-color: {self.theme['button_bg']}; color: {self.theme['button_text']};
            border: 1px solid {self.theme['border']}; border-radius: 8px;
            padding: 8px 16px; font-size: 11pt; font-weight: 600;
        }}
        QPushButton:hover {{ background-color: {self.theme['button_hover']}; }}
        QPushButton:pressed {{ background-color: {self.theme['button_pressed']}; }}
        QPushButton:disabled {{ color: {self.theme['disabled_text']}; }}
        """)

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        title = QtWidgets.QLabel("Reproject To EPSG:4326", self)
        title.setObjectName("ReprojectWorkflowTitle")
        outer.addWidget(title)

        intro = QtWidgets.QLabel(self)
        intro.setWordWrap(True)
        intro.setText(
            "This will automatically reproject all TIFF, Basemap TIFF, and SHP files to EPSG:4326.\n\n"
            f"Folder:\n{self.folder}\n\n"
            f"Files converted in place: {self.counts.get('convert_total', len(self.convert_items))} "
            f"({self.counts.get('raster_convert', 0)} TIFF, {self.counts.get('shapefile_convert', 0)} SHP)."
        )
        outer.addWidget(intro)

        warning_row = QtWidgets.QHBoxLayout()
        warning_row.setSpacing(10)
        warning_row.setContentsMargins(0, 2, 0, 2)
        warning_icon = QtWidgets.QLabel("!", self)
        warning_icon.setObjectName("ReprojectWorkflowWarningIcon")
        warning_icon.setFixedSize(34, 34)
        warning_icon.setAlignment(QtCore.Qt.AlignCenter)
        warning_text = QtWidgets.QLabel(
            "The original files will be overwritten in place.\n"
            "Make sure you have a backup before continuing.",
            self,
        )
        warning_text.setObjectName("ReprojectWorkflowWarningText")
        warning_text.setWordWrap(True)
        warning_row.addWidget(warning_icon, 0, QtCore.Qt.AlignTop)
        warning_row.addWidget(warning_text, 1)
        outer.addLayout(warning_row)

        self.status_label = QtWidgets.QLabel("Ready to reproject.", self)
        self.status_label.setObjectName("ReprojectWorkflowStatus")
        self.status_label.setWordWrap(True)
        outer.addWidget(self.status_label)

        self.progress_bar = QtWidgets.QProgressBar(self)
        self.progress_bar.setRange(0, max(1, len(self.convert_items)))
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%v / %m")
        outer.addWidget(self.progress_bar)

        details_label = QtWidgets.QLabel("Details", self)
        details_label.setStyleSheet(f"color: {self.theme['heading']}; font-weight: 700;")
        outer.addWidget(details_label)

        self.details_edit = QtWidgets.QPlainTextEdit(self)
        self.details_edit.setReadOnly(True)
        self.details_edit.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        self.details_edit.setMinimumHeight(220)
        pending_lines = self._pending_detail_lines()
        self.details_edit.setPlainText("\n".join(pending_lines))
        outer.addWidget(self.details_edit, 1)

        button_row = QtWidgets.QHBoxLayout()
        button_row.addStretch(1)
        self.start_btn = QtWidgets.QPushButton("Start Reprojection", self)
        self.close_btn = QtWidgets.QPushButton("Cancel", self)
        self.restart_btn = QtWidgets.QPushButton("Restart Application", self)
        self.restart_btn.hide()
        self.start_btn.clicked.connect(self._run_reprojection)
        self.close_btn.clicked.connect(self.reject)
        self.restart_btn.clicked.connect(self._restart)
        button_row.addWidget(self.start_btn)
        button_row.addWidget(self.close_btn)
        button_row.addWidget(self.restart_btn)
        outer.addLayout(button_row)
        self._resize_for_detail_lines(pending_lines)

    def _pending_detail_lines(self):
        lines = []
        for item in self.convert_items:
            name = item.get("name") or os.path.basename(item.get("path", "file"))
            detail = str(item.get("detail") or item.get("source_crs") or "Pending")
            lines.append(f"{name}: {detail}")
        return lines

    def _resize_for_detail_lines(self, lines):
        try:
            metrics = QtGui.QFontMetrics(self.details_edit.font())
            longest_px = max([metrics.horizontalAdvance(str(line)) for line in list(lines or [])] or [0])
            target_width = max(874, int((longest_px + 130) * 1.15))
            screen = QtWidgets.QApplication.primaryScreen()
            if screen is not None:
                available_width = screen.availableGeometry().width()
                target_width = min(target_width, max(760, available_width - 80))
            self.setMinimumWidth(target_width)
            self.resize(target_width, 620)
        except Exception:
            self.resize(900, 620)

    def _set_running(self, running):
        self._running = bool(running)
        self.start_btn.setEnabled(not self._running)
        self.close_btn.setEnabled(not self._running)

    def _run_reprojection(self):
        if self._running:
            return

        self._set_running(True)
        self.start_btn.setText("Reprojecting...")
        self.details_edit.clear()
        self.status_label.setText("Preparing reprojection...")
        self.progress_bar.setValue(0)
        QtWidgets.QApplication.processEvents(QtCore.QEventLoop.ExcludeUserInputEvents)

        def update_progress(done, total, item, message):
            done = max(0, min(int(done), int(total)))
            name = item.get("name") or os.path.basename(item.get("path", ""))
            if str(message) == "Converting":
                line1 = f"Converting {min(done + 1, total)} of {total}"
            else:
                line1 = f"Finished {done} of {total}"
            self.status_label.setText(f"{line1}: {name}\n{message}")
            self.progress_bar.setValue(done)
            QtWidgets.QApplication.processEvents(QtCore.QEventLoop.ExcludeUserInputEvents)

        try:
            self.results = batch_reproject_targets_to_4326(self.convert_items, progress_callback=update_progress)
        finally:
            self._set_running(False)

        self._show_completed()

    def _show_completed(self):
        results = list(self.results or [])
        converted = sum(1 for item in results if item.get("result_status") == "converted")
        skipped = sum(1 for item in results if item.get("result_status") == "skipped")
        errors = [item for item in results if item.get("result_status") == "error"]

        self.progress_bar.setValue(len(self.convert_items))
        self.status_label.setText(
            f"Reprojection complete. Converted {converted} file(s)."
            + (f" Skipped {skipped} file(s)." if skipped else "")
            + (f" {len(errors)} file(s) failed." if errors else "")
        )

        detail_lines = []
        for item in results:
            name = item.get("name") or os.path.basename(item.get("path", "file"))
            detail_lines.append(f"{name}: {item.get('result_message', '')}")
        self.details_edit.setPlainText("\n".join(detail_lines))
        self._resize_for_detail_lines(detail_lines)

        self.start_btn.hide()
        self.close_btn.setText("Close")
        self.close_btn.setEnabled(True)
        self.restart_btn.show()
        self.restart_btn.setDefault(True)
        self.restart_btn.setFocus(QtCore.Qt.OtherFocusReason)

    def _restart(self):
        self.restart_requested = True
        self.accept()

    def reject(self):
        if self._running:
            return
        super().reject()

    def closeEvent(self, event):
        if self._running:
            event.ignore()
            return
        super().closeEvent(event)

class StartupSettingsDialog(QtWidgets.QDialog):
    """Simplified startup menu for saved profile, SHPs, file saving, and datetime."""

    def __init__(self, persisted_ui_store, shp_paths, basemap_paths=None, example_filename="", log_exists=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Startup Settings")
        self.setModal(True)
        self.setProperty(GEOVIEWER_NO_SCREEN_SCROLL_PROPERTY, True)
        self._startup_default_size = QtCore.QSize(1040, 1320)

        base_font = self.font()
        base_font.setPointSize(12)
        self.setFont(base_font)

        self._loading_profile = False
        self._missing_primary_name = ""
        self._missing_overlay_names = []
        self._missing_basemap_name = ""
        self._none_label = "— none —"
        self._keep_behavior = {
            "mode": "overwrite",
            "suffix": "_keep",
            "preserve_original": False,
        }
        self._reject_behavior = {
            "mode": "delete",
            "suffix": "_reject",
            "preserve_original": False,
        }
        self.example_filename = os.path.basename(str(example_filename or "").strip())
        self.log_exists = bool(log_exists)

        self.store = normalize_persisted_ui_store(persisted_ui_store)
        self.default_profile_name = str(self.store.get("default_profile") or "Default").strip() or "Default"
        self.profile_names = [
            str(item.get("name") or "").strip()
            for item in self.store.get("profiles", [])
            if str(item.get("name") or "").strip()
        ]
        self.profile_settings_by_name = {
            name: normalize_persisted_ui_settings(_find_persisted_ui_profile(self.store, name).get("settings"))
            for name in self.profile_names
        }

        self.theme_mode = get_app_theme_mode()
        self.theme = build_theme_palette(self.theme_mode)
        self._apply_theme()

        self._shp_paths = list(shp_paths or [])
        self._basemap_paths = list(basemap_paths or [])
        self._path_by_name = {}
        self._build_shapefile_lookup()
        self._basemap_path_by_name = {}
        self._basemap_categories = []
        self._build_basemap_lookup()
        self._reproject_targets = discover_reproject_targets()

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(14, 40, 14, 14)
        outer.setSpacing(10)

        self._startup_scroll_area = QtWidgets.QScrollArea(self)
        self._startup_scroll_area.setObjectName("StartupSettingsScrollArea")
        self._startup_scroll_area.setWidgetResizable(True)
        self._startup_scroll_area.setFrameShape(QtWidgets.QFrame.NoFrame)
        self._startup_scroll_area.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self._startup_scroll_area.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self._startup_scroll_area.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self._startup_scroll_area.setMinimumSize(0, 0)
        self._startup_scroll_area.setStyleSheet(
            "QScrollArea#StartupSettingsScrollArea { border: 0px; background: transparent; }"
            "QScrollArea#StartupSettingsScrollArea > QWidget > QWidget { background: transparent; }"
        )
        outer.addWidget(self._startup_scroll_area, 1)

        body = QtWidgets.QWidget()
        body.setObjectName("StartupSettingsContent")
        self._startup_scroll_area.setWidget(body)
        body_lay = QtWidgets.QVBoxLayout(body)
        body_lay.setContentsMargins(0, 12, 0, 0)
        body_lay.setSpacing(10)

        self.reproject_group = self._build_reproject_group(body)
        body_lay.addWidget(self.reproject_group)

        profile_group = QtWidgets.QGroupBox("Load Saved Profile", body)
        profile_lay = QtWidgets.QGridLayout(profile_group)
        profile_lay.setHorizontalSpacing(12)
        profile_lay.setVerticalSpacing(8)
        profile_lay.setContentsMargins(12, 18, 12, 10)
        body_lay.addWidget(profile_group)

        self.default_profile_label = QtWidgets.QLabel(profile_group)
        self.default_profile_label.setStyleSheet(f"color: {self.theme['heading']}; font-size: 12pt; font-weight: 600;")
        self.profile_combo = QtWidgets.QComboBox(profile_group)
        self.profile_combo.addItems(self.profile_names)
        self.profile_combo.setMinimumWidth(280)
        self.ui_scale_combo = QtWidgets.QComboBox(profile_group)
        for label, value in UI_SCALE_CHOICES:
            self.ui_scale_combo.addItem(label, value)
        self._set_ui_scale_combo_value(self.store.get("last_ui_scale", "auto"))

        profile_lay.addWidget(QtWidgets.QLabel("Auto-Load Default:", profile_group), 0, 0)
        profile_lay.addWidget(self.default_profile_label, 0, 1)
        profile_lay.addWidget(QtWidgets.QLabel("Load Alternate:", profile_group), 1, 0)
        profile_lay.addWidget(self.profile_combo, 1, 1)
        profile_lay.addWidget(QtWidgets.QLabel("UI Scale:", profile_group), 2, 0)
        profile_lay.addWidget(self.ui_scale_combo, 2, 1)

        vector_group = QtWidgets.QGroupBox("Load Shapefiles", body)
        vector_lay = QtWidgets.QGridLayout(vector_group)
        vector_lay.setHorizontalSpacing(12)
        vector_lay.setVerticalSpacing(8)
        vector_lay.setContentsMargins(12, 16, 12, 8)
        body_lay.addWidget(vector_group)

        self.vector_note_label = QtWidgets.QLabel(vector_group)
        self.vector_note_label.setWordWrap(True)
        self.vector_note_label.setStyleSheet(f"color: {self.theme['muted']}; font-size: 11pt;")
        vector_lay.addWidget(self.vector_note_label, 0, 0, 1, 4)

        self.vector_lay = vector_lay
        self.vector_group = vector_group
        self.primary_combo = QtWidgets.QComboBox(vector_group)
        self.primary_combo.setMinimumWidth(460)
        self.primary_color_combo = QtWidgets.QComboBox(vector_group)
        self.primary_color_combo.addItems(list(PICKER_COLORS))
        self.primary_color_combo.setCurrentText("lime")
        self._populate_shapefile_combo(self.primary_combo)

        vector_lay.addWidget(QtWidgets.QLabel("Primary:", vector_group), 1, 0)
        vector_lay.addWidget(self.primary_combo, 1, 1, 1, 2)
        vector_lay.addWidget(QtWidgets.QLabel("Color:", vector_group), 1, 3)
        vector_lay.addWidget(self.primary_color_combo, 1, 4)

        self.overlay_shp_combos = []
        self.overlay_color_combos = []
        self.overlay_row_widgets = []
        for row in range(5):
            label = QtWidgets.QLabel(f"Overlay {row + 1}:", vector_group)
            shp_combo = QtWidgets.QComboBox(vector_group)
            shp_combo.setMinimumWidth(460)
            self._populate_shapefile_combo(shp_combo)
            shp_combo.currentIndexChanged.connect(self._on_vector_selection_changed)
            color_combo = QtWidgets.QComboBox(vector_group)
            color_combo.addItems(list(PICKER_COLORS))
            color_combo.setCurrentText("lime")
            color_combo.currentIndexChanged.connect(self._on_any_control_changed)
            color_label = QtWidgets.QLabel("Color:", vector_group)
            vector_lay.addWidget(label, row + 2, 0)
            vector_lay.addWidget(shp_combo, row + 2, 1, 1, 2)
            vector_lay.addWidget(color_label, row + 2, 3)
            vector_lay.addWidget(color_combo, row + 2, 4)
            self.overlay_shp_combos.append(shp_combo)
            self.overlay_color_combos.append(color_combo)
            self.overlay_row_widgets.append((label, shp_combo, color_label, color_combo))

        self.add_overlay_btn = QtWidgets.QPushButton("Add Overlay", vector_group)
        self.add_overlay_btn.setAutoDefault(False)
        self.add_overlay_btn.clicked.connect(self._add_startup_overlay_row)
        vector_lay.addWidget(self.add_overlay_btn, 7, 1, 1, 2, QtCore.Qt.AlignLeft)
        self._set_visible_overlay_count(1)

        basemap_group = QtWidgets.QGroupBox("Load Basemap", body)
        basemap_lay = QtWidgets.QGridLayout(basemap_group)
        basemap_lay.setHorizontalSpacing(12)
        basemap_lay.setVerticalSpacing(8)
        body_lay.addWidget(basemap_group)

        self.basemap_note_label = QtWidgets.QLabel(basemap_group)
        self.basemap_note_label.setWordWrap(True)
        self.basemap_note_label.setStyleSheet(f"color: {self.theme['muted']}; font-size: 11pt;")
        self.basemap_mode_combo = QtWidgets.QComboBox(basemap_group)
        for label, token in BASEMAP_MODE_OPTIONS:
            self.basemap_mode_combo.addItem(label, token)
        self.basemap_category_combo = QtWidgets.QComboBox(basemap_group)
        self._populate_basemap_category_combo(self.basemap_category_combo)
        self.basemap_combo = QtWidgets.QComboBox(basemap_group)
        self.basemap_combo.setMinimumWidth(620)
        self._populate_basemap_combo(self.basemap_combo)

        basemap_lay.addWidget(self.basemap_note_label, 0, 0, 1, 2)
        basemap_lay.addWidget(QtWidgets.QLabel("Mode:", basemap_group), 1, 0)
        basemap_lay.addWidget(self.basemap_mode_combo, 1, 1)
        basemap_lay.addWidget(QtWidgets.QLabel("Category:", basemap_group), 2, 0)
        basemap_lay.addWidget(self.basemap_category_combo, 2, 1)
        basemap_lay.addWidget(QtWidgets.QLabel("Single scene:", basemap_group), 3, 0)
        basemap_lay.addWidget(self.basemap_combo, 3, 1)

        saving_group = QtWidgets.QGroupBox("File Saving Behavior", body)
        saving_lay = QtWidgets.QGridLayout(saving_group)
        saving_lay.setHorizontalSpacing(12)
        saving_lay.setVerticalSpacing(8)
        body_lay.addWidget(saving_group)

        self.keep_summary_label = QtWidgets.QLabel(saving_group)
        self.keep_summary_label.setWordWrap(True)
        self.keep_summary_label.setStyleSheet("font-weight: 700; color: #2E8B57;")
        self.reject_summary_label = QtWidgets.QLabel(saving_group)
        self.reject_summary_label.setWordWrap(True)
        self.reject_summary_label.setStyleSheet("font-weight: 700; color: #C23B22;")
        self.edit_save_btn = QtWidgets.QPushButton("Edit File Saving", saving_group)

        saving_lay.addWidget(QtWidgets.QLabel("Keep:", saving_group), 0, 0)
        saving_lay.addWidget(self.keep_summary_label, 0, 1)
        saving_lay.addWidget(QtWidgets.QLabel("Reject:", saving_group), 1, 0)
        saving_lay.addWidget(self.reject_summary_label, 1, 1)
        saving_lay.addWidget(self.edit_save_btn, 0, 2, 2, 1)

        filename_group = QtWidgets.QGroupBox("Datetime", body)
        filename_lay = QtWidgets.QGridLayout(filename_group)
        filename_lay.setHorizontalSpacing(12)
        filename_lay.setVerticalSpacing(8)
        body_lay.addWidget(filename_group)

        self.filename_status_banner = QtWidgets.QLabel(filename_group)
        self.filename_status_banner.setAlignment(QtCore.Qt.AlignCenter)
        self.filename_status_banner.setMinimumHeight(48)
        self.filename_status_banner.setStyleSheet("font-size: 14pt; font-weight: 700; border-radius: 8px; padding: 8px;")
        self.filename_example_label = QtWidgets.QLabel(filename_group)
        self.filename_example_label.setWordWrap(True)
        self.filename_detail_label = QtWidgets.QLabel(filename_group)
        self.filename_detail_label.setWordWrap(True)
        self.filename_detail_label.setStyleSheet(f"color: {self.theme['muted']}; font-size: 12pt;")
        self.filename_override_check = QtWidgets.QCheckBox("Allow substring / pattern override", filename_group)
        self.filename_override_check.setStyleSheet(f"color: {self.theme['text']}; font-size: 12pt; font-weight: 600;")
        self.filename_subedit = QtWidgets.QLineEdit(filename_group)
        self.filename_subedit.setPlaceholderText("Datetime substring")
        self.filename_pattern_edit = QtWidgets.QLineEdit(filename_group)
        self.filename_pattern_edit.setPlaceholderText("Pattern")
        substring_hint = "Example substring: 20250314T133738"
        pattern_hint = "Matching pattern: yyyymmddThhmmss"
        self.filename_subedit.setToolTip(substring_hint)
        self.filename_pattern_edit.setToolTip(pattern_hint)

        filename_lay.addWidget(self.filename_status_banner, 0, 0, 1, 4)
        filename_lay.addWidget(QtWidgets.QLabel("Example TIFF:", filename_group), 1, 0)
        filename_lay.addWidget(self.filename_example_label, 1, 1, 1, 3)
        filename_lay.addWidget(self.filename_detail_label, 2, 0, 1, 4)
        filename_lay.addWidget(self.filename_override_check, 3, 0, 1, 4)
        filename_lay.addWidget(QtWidgets.QLabel("Substring:", filename_group), 4, 0)
        filename_lay.addWidget(self.filename_subedit, 4, 1, 1, 3)
        filename_lay.addWidget(QtWidgets.QLabel("Pattern:", filename_group), 5, 0)
        filename_lay.addWidget(self.filename_pattern_edit, 5, 1, 1, 3)

        button_row = QtWidgets.QHBoxLayout()
        outer.addLayout(button_row)
        button_row.addStretch(1)
        self.enter_btn = QtWidgets.QPushButton("Enter", self)
        self.cancel_btn = QtWidgets.QPushButton("Cancel", self)
        self.enter_btn.setDefault(True)
        self.enter_btn.setMinimumWidth(160)
        self.cancel_btn.setMinimumWidth(160)
        button_row.addWidget(self.enter_btn)
        button_row.addWidget(self.cancel_btn)

        enter_style = build_keep_reject_button_style("#2E8B57")
        cancel_style = build_keep_reject_button_style("#C23B22")
        self.enter_btn.setStyleSheet(
            f"QPushButton {{ background-color: {enter_style['base']}; color: {enter_style['text']}; "
            f"border: 1px solid {enter_style['edge']}; border-radius: 8px; padding: 10px 18px; font-size: 12pt; font-weight: 700; }}"
            f"QPushButton:hover {{ background-color: {enter_style['hover']}; }}"
        )
        self.cancel_btn.setStyleSheet(
            f"QPushButton {{ background-color: {cancel_style['base']}; color: {cancel_style['text']}; "
            f"border: 1px solid {cancel_style['edge']}; border-radius: 8px; padding: 10px 18px; font-size: 12pt; font-weight: 700; }}"
            f"QPushButton:hover {{ background-color: {cancel_style['hover']}; }}"
        )

        self.profile_combo.currentTextChanged.connect(self._on_profile_changed)
        self.ui_scale_combo.currentIndexChanged.connect(self._on_ui_scale_changed)
        self.edit_save_btn.clicked.connect(self._edit_save_rules)
        self.enter_btn.clicked.connect(self._accept_reviewed)
        self.cancel_btn.clicked.connect(self.reject)
        self.filename_override_check.toggled.connect(self._on_any_control_changed)
        self.primary_color_combo.currentIndexChanged.connect(self._on_any_control_changed)
        self.filename_subedit.textChanged.connect(self._on_any_control_changed)
        self.filename_pattern_edit.textChanged.connect(self._on_any_control_changed)
        self.primary_combo.currentIndexChanged.connect(self._on_vector_selection_changed)
        self.basemap_combo.currentIndexChanged.connect(self._on_basemap_selection_changed)
        self.basemap_mode_combo.currentIndexChanged.connect(self._on_basemap_selection_changed)
        self.basemap_category_combo.currentIndexChanged.connect(self._on_basemap_selection_changed)

        self.default_profile_label.setText(self.default_profile_name)
        self.filename_example_label.setText(self.example_filename or "No TIFF example found.")
        self._set_profile(self.default_profile_name)
        self._apply_theme()
        self._install_theme_toggle_filter()
        self._refresh_startup_overlay_layout()
        self._fit_startup_dialog_to_screen()
        self._queue_startup_dialog_fit()

    def _queue_startup_dialog_fit(self):
        try:
            QtCore.QTimer.singleShot(0, self._fit_startup_dialog_to_screen)
        except Exception:
            pass

    def _fit_startup_dialog_to_screen(self):
        try:
            self.ensurePolished()
            layout = self.layout()
            if layout is not None:
                layout.invalidate()
                layout.activate()
            scroll = getattr(self, "_startup_scroll_area", None)
            if isinstance(scroll, QtWidgets.QScrollArea):
                scroll.setMinimumSize(0, 0)
                scroll.setMaximumSize(16777215, 16777215)

            available = _available_geometry_for_widget_or_cursor(self)
            margin = 16
            max_w = max(420, int(available.width()) - margin * 2)
            max_h = max(
                360,
                min(
                    int(available.height()) - margin * 2,
                    int(round(float(available.height()) * 0.90)),
                ),
            )
            default_size = getattr(self, "_startup_default_size", QtCore.QSize(1040, 1320))
            hint = self.sizeHint()
            minimum = self.minimumSizeHint()
            target_w = min(
                max_w,
                max(int(default_size.width()), int(hint.width()), int(minimum.width())),
            )
            target_h = max_h
            target_w = max(420, int(target_w))
            target_h = max(360, int(target_h))
            self._resize_startup_dialog(target_w, target_h)
            self._clamp_startup_dialog_to_screen(available)
        except Exception:
            pass

    def _resize_startup_dialog(self, width, height):
        try:
            scale = min(float(ui_scale()), 1.0)
        except Exception:
            scale = 1.0
        if not math.isfinite(scale) or scale <= 0:
            scale = 1.0
        try:
            raw_height = int(round(float(height) / scale)) if scale < 1.0 else int(height)
            self.resize(int(width), raw_height)
        except Exception:
            try:
                self.resize(int(width), int(height))
            except Exception:
                pass

    def _clamp_startup_dialog_to_screen(self, available):
        try:
            frame = self.frameGeometry()
            width = max(1, int(frame.width()))
            height = max(1, int(frame.height()))
            min_x = int(available.left())
            min_y = int(available.top())
            max_x = int(available.left() + max(0, available.width() - width))
            max_y = int(available.top() + max(0, available.height() - height))
            x = min(max(int(frame.x()), min_x), max_x)
            y = min(max(int(frame.y()), min_y), max_y)
            if x != int(frame.x()) or y != int(frame.y()):
                self.move(x, y)
        except Exception:
            pass

    def _apply_theme(self):
        self.theme_mode = get_app_theme_mode()
        self.theme = build_theme_palette(self.theme_mode)
        self.setStyleSheet(f"""
        QDialog {{ background-color: {self.theme['window_bg']}; color: {self.theme['text']}; }}
        QLabel {{ color: {self.theme['text']}; font-size: 12pt; }}
        QGroupBox {{
            color: {self.theme['heading']}; font-weight: 600; font-size: 13pt;
            border: 1px solid {self.theme['border']}; border-radius: 8px;
            margin-top: 12px; padding: 12px 12px 10px 12px;
            background-color: {self.theme['group_bg']};
        }}
        QGroupBox::title {{
            subcontrol-origin: margin; left: 10px; top: 0px;
            padding: 0 4px; background-color: {self.theme['window_bg']}; color: {self.theme['heading']};
        }}
        QComboBox, QLineEdit, QPlainTextEdit {{
            background-color: {self.theme['input_bg']}; color: {self.theme['text']};
            border: 1px solid {self.theme['border']}; border-radius: 6px;
            padding: 6px 10px; font-size: 12pt; min-height: 30px;
        }}
        QComboBox QAbstractItemView {{
            background-color: {self.theme['list_bg']}; color: {self.theme['text']};
            selection-background-color: {self.theme['selection_bg']}; selection-color: {self.theme['selection_text']};
            border: 1px solid {self.theme['border']};
        }}
        QPushButton {{
            background-color: {self.theme['button_bg']}; color: {self.theme['button_text']};
            border: 1px solid {self.theme['border']}; border-radius: 8px;
            padding: 8px 16px; font-size: 12pt; font-weight: 600;
        }}
        QPushButton:hover {{ background-color: {self.theme['button_hover']}; }}
        QPushButton:pressed {{ background-color: {self.theme['button_pressed']}; }}
        """)
        if hasattr(self, "default_profile_label"):
            self.default_profile_label.setStyleSheet(
                f"color: {self.theme['heading']}; font-size: 12pt; font-weight: 600;"
            )
        if hasattr(self, "vector_note_label"):
            self.vector_note_label.setStyleSheet(f"color: {self.theme['muted']}; font-size: 11pt;")
        if hasattr(self, "basemap_note_label"):
            self.basemap_note_label.setStyleSheet(f"color: {self.theme['muted']}; font-size: 11pt;")
        if hasattr(self, "filename_detail_label"):
            self.filename_detail_label.setStyleSheet(f"color: {self.theme['muted']}; font-size: 12pt;")
        if hasattr(self, "filename_override_check"):
            self.filename_override_check.setStyleSheet(
                f"color: {self.theme['text']}; font-size: 12pt; font-weight: 600;"
            )
        if hasattr(self, "reproject_prompt_label"):
            self.reproject_prompt_label.setStyleSheet(
                f"color: {self.theme['heading']}; font-size: 14pt; font-weight: 700;"
            )
        if hasattr(self, "reproject_detail_label"):
            self.reproject_detail_label.setStyleSheet(f"color: {self.theme['muted']}; font-size: 11pt;")
        if hasattr(self, "reproject_summary_label"):
            self.reproject_summary_label.setStyleSheet(f"color: {self.theme['text']}; font-size: 12pt;")
        if hasattr(self, "reproject_count_graphic"):
            self.reproject_count_graphic.apply_theme(self.theme)

    def _toggle_theme(self):
        new_mode = "light" if get_app_theme_mode() == "dark" else "dark"
        set_app_theme_mode(new_mode)
        self._apply_theme()

    def _handle_theme_toggle_key(self, event):
        if event.type() != QtCore.QEvent.KeyPress:
            return False
        if event.key() != QtCore.Qt.Key_Slash:
            return False
        if event.modifiers() & (QtCore.Qt.ControlModifier | QtCore.Qt.AltModifier | QtCore.Qt.MetaModifier):
            return False
        text = event.text() if hasattr(event, "text") else ""
        if text and text != "/":
            return False
        self._toggle_theme()
        event.accept()
        return True

    def _install_theme_toggle_filter(self):
        self.installEventFilter(self)
        for widget in self.findChildren(QtWidgets.QWidget):
            widget.installEventFilter(self)

    def eventFilter(self, obj, event):
        if self._handle_theme_toggle_key(event):
            return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event):
        if self._handle_theme_toggle_key(event):
            return
        super().keyPressEvent(event)

    def _build_reproject_group(self, parent):
        group = QtWidgets.QGroupBox("Reproject To EPSG:4326", parent)
        lay = QtWidgets.QGridLayout(group)
        lay.setHorizontalSpacing(12)
        lay.setVerticalSpacing(8)
        lay.setContentsMargins(12, 18, 12, 10)

        prompt = QtWidgets.QLabel("Need to reproject TIFs, Basemaps, or SHPs to 4326?", group)
        prompt.setStyleSheet(f"color: {self.theme['heading']}; font-size: 14pt; font-weight: 700;")
        prompt.setWordWrap(True)
        self.reproject_prompt_label = prompt

        detail = QtWidgets.QLabel(
            "This checks the current working folder plus the Basemaps folder and only converts files that are not already EPSG:4326.",
            group,
        )
        detail.setWordWrap(True)
        detail.setStyleSheet(f"color: {self.theme['muted']}; font-size: 11pt;")
        self.reproject_detail_label = detail

        self.reproject_count_graphic = ReprojectCountGraphic(self.theme, group)
        self.reproject_summary_label = QtWidgets.QLabel(group)
        self.reproject_summary_label.setWordWrap(True)
        self.reproject_summary_label.setStyleSheet(f"color: {self.theme['text']}; font-size: 12pt;")

        self.reproject_btn = QtWidgets.QPushButton("Reproject TIFs / Basemaps / SHPs to 4326", group)
        self.reproject_btn.setMinimumWidth(300)
        self.reproject_btn.clicked.connect(self._run_reproject_workflow)

        lay.addWidget(prompt, 0, 0, 1, 2)
        lay.addWidget(detail, 1, 0, 1, 2)
        lay.addWidget(self.reproject_count_graphic, 2, 0, 2, 1)
        lay.addWidget(self.reproject_summary_label, 2, 1)
        lay.addWidget(self.reproject_btn, 3, 1, 1, 1, QtCore.Qt.AlignLeft)

        self._refresh_reproject_preview()
        return group

    def _format_reproject_summary(self, counts):
        total = int(counts.get("total", 0))
        convert_total = int(counts.get("convert_total", 0))
        raster_convert = int(counts.get("raster_convert", 0))
        shapefile_convert = int(counts.get("shapefile_convert", 0))
        already_total = int(counts.get("already_total", 0))
        issue_total = int(counts.get("issue_total", 0))

        if total <= 0:
            return "No TIFF, Basemap TIFF, or SHP files were found."
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

    def _refresh_reproject_preview(self):
        counts = summarize_reproject_targets(self._reproject_targets)
        self.reproject_count_graphic.set_counts(counts)
        self.reproject_summary_label.setText(self._format_reproject_summary(counts))
        can_convert = int(counts.get("convert_total", 0)) > 0
        self.reproject_btn.setEnabled(can_convert)
        if can_convert:
            self.reproject_btn.setToolTip("Convert all non-EPSG:4326 TIFF, Basemap TIFF, and SHP files.")
        else:
            self.reproject_btn.setToolTip("No files need reprojection.")

    def _run_reproject_workflow(self):
        self.setCursor(QtCore.Qt.WaitCursor)
        QtWidgets.QApplication.processEvents()
        try:
            self._reproject_targets = discover_reproject_targets()
        finally:
            self.unsetCursor()
        self._refresh_reproject_preview()

        counts = summarize_reproject_targets(self._reproject_targets)
        convert_items = [item for item in self._reproject_targets if item.get("will_convert")]
        if not convert_items:
            QMessageBox.information(self, "Reproject To EPSG:4326", self._format_reproject_summary(counts))
            return

        dlg = ReprojectWorkflowDialog(counts, convert_items, os.getcwd(), self.theme, self)
        dlg.exec_()

        if dlg.results is not None:
            self.setCursor(QtCore.Qt.WaitCursor)
            QtWidgets.QApplication.processEvents()
            try:
                self._reproject_targets = discover_reproject_targets()
            finally:
                self.unsetCursor()
            self._refresh_reproject_preview()

        if dlg.restart_requested and restart_geoviewer_application(self):
            self.reject()

    def _build_shapefile_lookup(self):
        self._path_by_name = {}
        self._shapefile_items = []
        for path in list(self._shp_paths):
            name = os.path.basename(path)
            epsg_str = "Unknown"
            try:
                gdf = gpd.read_file(path)
                if gdf.crs:
                    epsg_val = gdf.crs.to_epsg()
                    epsg_str = f"EPSG:{epsg_val}" if epsg_val is not None else gdf.crs.to_string()
            except Exception:
                epsg_str = "Unreadable"
            label = f"{name} — CRS: {epsg_str}"
            self._path_by_name.setdefault(name, path)
            self._shapefile_items.append((label, path))

    def _build_basemap_lookup(self):
        self._basemap_path_by_name = {}
        self._basemap_items = []
        self._basemap_categories = basemap_categories_from_paths(self._basemap_paths)
        for path in list(self._basemap_paths):
            name = os.path.basename(path)
            label = raster_choice_label(path)
            self._basemap_path_by_name.setdefault(name, path)
            self._basemap_items.append((label, path))

    def _populate_shapefile_combo(self, combo):
        combo.clear()
        combo.addItem(self._none_label, "")
        for label, path in self._shapefile_items:
            combo.addItem(label, path)

    def _populate_basemap_combo(self, combo):
        combo.clear()
        combo.addItem(self._none_label, "")
        for label, path in self._basemap_items:
            combo.addItem(label, path)

    def _populate_basemap_category_combo(self, combo):
        combo.clear()
        for category in self._basemap_categories:
            paths = basemap_paths_for_category(self._basemap_paths, category)
            count_text = f"{len(paths)} file" if len(paths) == 1 else f"{len(paths)} files"
            combo.addItem(f"{basemap_category_label(category)} - {count_text}", category)
        if combo.count() <= 0:
            combo.addItem("No basemap categories found", "")

    def _set_overlay_row_visible(self, idx, visible):
        try:
            widgets = self.overlay_row_widgets[idx]
        except Exception:
            return
        for widget in widgets:
            try:
                widget.setVisible(bool(visible))
            except Exception:
                pass

    def _activate_widget_layout_tree(self, widget):
        seen = set()
        current = widget if isinstance(widget, QtWidgets.QWidget) else None
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            try:
                layout = current.layout()
            except Exception:
                layout = None
            if layout is not None:
                try:
                    layout.invalidate()
                    layout.activate()
                except Exception:
                    pass
            try:
                current.updateGeometry()
                current.update()
            except Exception:
                pass
            try:
                current = current.parentWidget()
            except Exception:
                current = None

    def _refresh_startup_overlay_layout(self, queue_followup=True):
        try:
            self.vector_lay.setVerticalSpacing(8)
        except Exception:
            pass
        try:
            visible_count = int(getattr(self, "_visible_overlay_count", 1) or 1)
            combo_heights = [
                self.primary_combo.sizeHint().height(),
                self.primary_color_combo.sizeHint().height(),
            ]
            for idx in range(max(0, min(visible_count, len(self.overlay_shp_combos)))):
                combo_heights.append(self.overlay_shp_combos[idx].sizeHint().height())
                combo_heights.append(self.overlay_color_combos[idx].sizeHint().height())
            row_height = max(combo_heights or [0]) + ui_px(8, minimum=6)
            self.vector_lay.setRowMinimumHeight(1, row_height)
            for idx in range(len(getattr(self, "overlay_row_widgets", []) or [])):
                self.vector_lay.setRowMinimumHeight(idx + 2, row_height if idx < visible_count else 0)
        except Exception:
            pass
        try:
            self.vector_lay.invalidate()
            self.vector_lay.activate()
        except Exception:
            pass
        self._activate_widget_layout_tree(getattr(self, "vector_group", None))

        content = getattr(self, "_geoviewer_screen_scroll_content", None)
        if isinstance(content, QtWidgets.QWidget):
            try:
                content.layout().invalidate()
                content.layout().activate()
            except Exception:
                pass
            try:
                hint = content.sizeHint()
                minimum = content.minimumSizeHint()
                content.setMinimumSize(0, max(0, int(hint.height()), int(minimum.height())))
                content.updateGeometry()
            except Exception:
                pass
        scroll = getattr(self, "_geoviewer_screen_scroll_area", None)
        if isinstance(scroll, QtWidgets.QScrollArea):
            try:
                scroll.updateGeometry()
                scroll.viewport().update()
            except Exception:
                pass

        try:
            self.layout().invalidate()
            self.layout().activate()
            self.updateGeometry()
            self.update()
        except Exception:
            pass
        try:
            QtWidgets.QApplication.sendPostedEvents(None, QtCore.QEvent.LayoutRequest)
        except Exception:
            pass
        if queue_followup:
            try:
                QtCore.QTimer.singleShot(0, lambda: self._refresh_startup_overlay_layout(queue_followup=False))
            except Exception:
                pass

    def _set_visible_overlay_count(self, count, reset_hidden=False):
        try:
            max_count = len(self.overlay_row_widgets)
        except Exception:
            max_count = 0
        visible_count = max(1, min(max_count, int(count))) if max_count else 0
        self._visible_overlay_count = visible_count
        for idx in range(max_count):
            visible = idx < visible_count
            self._set_overlay_row_visible(idx, visible)
            if reset_hidden and not visible:
                try:
                    self.overlay_shp_combos[idx].blockSignals(True)
                    self.overlay_shp_combos[idx].setCurrentIndex(0)
                finally:
                    try:
                        self.overlay_shp_combos[idx].blockSignals(False)
                    except Exception:
                        pass
                try:
                    self.overlay_color_combos[idx].blockSignals(True)
                    self.overlay_color_combos[idx].setCurrentText("lime")
                finally:
                    try:
                        self.overlay_color_combos[idx].blockSignals(False)
                    except Exception:
                        pass
        self._sync_overlay_add_button()
        self._refresh_startup_overlay_layout()

    def _sync_overlay_add_button(self):
        button = getattr(self, "add_overlay_btn", None)
        if button is None:
            return
        max_count = len(getattr(self, "overlay_row_widgets", []) or [])
        visible_count = int(getattr(self, "_visible_overlay_count", 0) or 0)
        button.setEnabled(visible_count < max_count)
        button.setVisible(max_count > 1)
        button.setText("Add Overlay" if visible_count < max_count else "All Overlays Added")

    def _add_startup_overlay_row(self):
        visible_count = int(getattr(self, "_visible_overlay_count", 1) or 1)
        if visible_count >= len(getattr(self, "overlay_row_widgets", []) or []):
            self._sync_overlay_add_button()
            return
        self._set_visible_overlay_count(visible_count + 1)
        try:
            self.overlay_color_combos[visible_count].setCurrentText("lime")
            self.overlay_shp_combos[visible_count].setFocus()
        except Exception:
            pass
        self._refresh_startup_overlay_layout()
        self._refresh_vector_note()
        self._refresh_summary()

    def _set_combo_data(self, combo, target_data):
        for idx in range(combo.count()):
            if combo.itemData(idx) == target_data:
                combo.setCurrentIndex(idx)
                return True
        return False

    def _set_ui_scale_combo_value(self, value):
        target = normalize_persisted_ui_scale(value, "auto")
        combo = getattr(self, "ui_scale_combo", None)
        if combo is None:
            return False
        for idx in range(combo.count()):
            candidate = normalize_persisted_ui_scale(combo.itemData(idx), "auto")
            if candidate == "auto" and target == "auto":
                combo.setCurrentIndex(idx)
                return True
            if candidate != "auto" and target != "auto":
                try:
                    if abs(float(candidate) - float(target)) < 1e-6:
                        combo.setCurrentIndex(idx)
                        return True
                except Exception:
                    pass
        combo.setCurrentIndex(0)
        return False

    def _selected_ui_scale_setting(self):
        combo = getattr(self, "ui_scale_combo", None)
        data = combo.currentData() if combo is not None else "auto"
        return normalize_persisted_ui_scale(data, "auto")

    def _set_shapefile_combo_by_name(self, combo, shp_name):
        target = normalize_persisted_shapefile_name(shp_name)
        if not target:
            combo.setCurrentIndex(0)
            return True
        path = self._path_by_name.get(target)
        if path:
            return self._set_combo_data(combo, path)
        combo.setCurrentIndex(0)
        return False

    def _set_basemap_combo_by_name(self, combo, basemap_name):
        target = normalize_persisted_basemap_name(basemap_name)
        if not target:
            combo.setCurrentIndex(0)
            return True
        path = self._basemap_path_by_name.get(target)
        if path:
            return self._set_combo_data(combo, path)
        combo.setCurrentIndex(0)
        return False

    def _set_basemap_mode(self, mode):
        target = normalize_basemap_mode(mode, "nearest")
        idx = self.basemap_mode_combo.findData(target)
        self.basemap_mode_combo.setCurrentIndex(idx if idx >= 0 else 0)

    def _set_basemap_category(self, category):
        target = normalize_basemap_category(category)
        if not target and self._basemap_categories:
            target = self._basemap_categories[0]
        idx = self.basemap_category_combo.findData(target)
        self.basemap_category_combo.setCurrentIndex(idx if idx >= 0 else 0)

    def _selected_basemap_mode(self):
        return normalize_basemap_mode(
            self.basemap_mode_combo.currentData() or self.basemap_mode_combo.currentText(),
            "nearest",
        )

    def _selected_basemap_category(self):
        data = self.basemap_category_combo.currentData()
        return normalize_basemap_category(data if data is not None else "")

    def _sync_basemap_controls(self):
        mode = self._selected_basemap_mode()
        has_categories = bool(self._basemap_categories)
        category_count = len(self._basemap_categories)
        self.basemap_mode_combo.setEnabled(bool(self._basemap_paths))
        self.basemap_category_combo.setEnabled(mode == "nearest" and category_count > 1)
        self.basemap_combo.setEnabled(mode == "single" and bool(self._basemap_paths))

    def _selected_shapefile_name(self, combo):
        path = str(combo.currentData() or "").strip()
        return normalize_persisted_shapefile_name(path)

    def _selected_basemap_name(self):
        path = str(self.basemap_combo.currentData() or "").strip()
        return normalize_persisted_basemap_name(path)

    def _current_profile_name(self):
        return str(self.profile_combo.currentText() or self.default_profile_name).strip() or self.default_profile_name

    def _set_profile(self, profile_name):
        target_name = str(profile_name or self.default_profile_name).strip() or self.default_profile_name
        if target_name not in self.profile_settings_by_name and self.profile_names:
            target_name = self.profile_names[0]
        if self.profile_combo.currentText() != target_name:
            self.profile_combo.setCurrentText(target_name)
            return
        self._apply_profile_settings(target_name)

    def _apply_profile_settings(self, profile_name):
        settings = dict(self.profile_settings_by_name.get(profile_name, _default_persisted_ui_settings()))
        self._loading_profile = True
        self._missing_primary_name = ""
        self._missing_overlay_names = []
        self._missing_basemap_name = ""

        primary_name = normalize_persisted_shapefile_name(settings.get("shp_primary_name"))
        if primary_name and not self._set_shapefile_combo_by_name(self.primary_combo, primary_name):
            self._missing_primary_name = primary_name
        else:
            self._set_shapefile_combo_by_name(self.primary_combo, primary_name)
        self.primary_color_combo.setCurrentText(
            normalize_picker_color_name(settings.get("shp_primary_color") if primary_name else None, "lime")
        )

        overlays = list(settings.get("shp_overlay_colors", []) or [])
        self._set_visible_overlay_count(max(1, min(5, len(overlays))), reset_hidden=True)
        for idx, shp_combo in enumerate(self.overlay_shp_combos):
            color_combo = self.overlay_color_combos[idx]
            shp_combo.setCurrentIndex(0)
            color_combo.setCurrentText("lime")
            if idx >= len(overlays):
                continue
            item = overlays[idx] if isinstance(overlays[idx], dict) else {}
            overlay_name = normalize_persisted_shapefile_name(item.get("name"))
            if overlay_name and not self._set_shapefile_combo_by_name(shp_combo, overlay_name):
                self._missing_overlay_names.append(overlay_name)
            else:
                self._set_shapefile_combo_by_name(shp_combo, overlay_name)
            color_combo.setCurrentText(
                normalize_picker_color_name(item.get("color") if overlay_name else None, "lime")
            )

        basemap_name = normalize_persisted_basemap_name(settings.get("basemap_name"))
        basemap_mode = normalize_basemap_mode(
            settings.get("basemap_mode"),
            "single" if basemap_name else "nearest",
        )
        basemap_category = normalize_basemap_category(settings.get("basemap_category"))
        if not basemap_category and basemap_name:
            saved_path = self._basemap_path_by_name.get(basemap_name)
            if saved_path:
                basemap_category = basemap_category_for_path(saved_path)
        self._set_basemap_mode(basemap_mode)
        self._set_basemap_category(basemap_category)
        if basemap_name and not self._set_basemap_combo_by_name(self.basemap_combo, basemap_name):
            self._missing_basemap_name = basemap_name
        else:
            self._set_basemap_combo_by_name(self.basemap_combo, basemap_name)
        self._sync_basemap_controls()

        self._keep_behavior = dict(settings.get("keep_behavior", {}))
        self._reject_behavior = dict(settings.get("reject_behavior", {}))
        saved_substring = normalize_filename_datetime_substring(settings.get("filename_dt_substring"))
        saved_pattern = normalize_filename_datetime_pattern(settings.get("filename_dt_pattern"))
        self.filename_override_check.setChecked(bool(saved_substring and saved_pattern))
        self.filename_subedit.setText(saved_substring)
        self.filename_pattern_edit.setText(saved_pattern)

        self._loading_profile = False
        self._refresh_vector_note()
        self._refresh_basemap_note()
        self._refresh_save_rule_summary()
        self._refresh_datetime_status()
        self._refresh_summary()

    def _refresh_vector_note(self):
        notes = []
        if not self._shp_paths:
            notes.append("No SHP files found in this folder.")
        if self._missing_primary_name:
            notes.append(f"Missing primary: {self._missing_primary_name}")
        if self._missing_overlay_names:
            notes.append("Missing overlays: " + ", ".join(self._missing_overlay_names))
        if not notes:
            notes.append("Auto-filled from the selected profile.")
        self.vector_note_label.setText("  ".join(notes))

    def _refresh_basemap_note(self):
        notes = []
        if not self._basemap_paths:
            notes.append(f"No TIFF basemaps found in the {BASEMAP_FOLDER_NAME} folder.")
        elif self._selected_basemap_mode() == "nearest":
            category = self._selected_basemap_category()
            if len(self._basemap_categories) > 1:
                notes.append("Choose which Landsat basemap category should be matched by nearest date.")
            if category:
                notes.append(f"Nearest-date category: {basemap_category_label(category)}.")
        else:
            notes.append("Single-scene mode uses the selected basemap for every ECOSTRESS image.")
        if self._missing_basemap_name:
            notes.append(f"Missing basemap: {self._missing_basemap_name}")
        if not notes:
            notes.append("Select an optional basemap.")
        self.basemap_note_label.setText("  ".join(notes))

    def _refresh_save_rule_summary(self):
        keep_text = summarize_keep_reject_behavior("keep", self._keep_behavior)
        reject_text = summarize_keep_reject_behavior("reject", self._reject_behavior)
        self.keep_summary_label.setText(keep_text)
        self.reject_summary_label.setText(reject_text)

    def _edit_save_rules(self):
        dlg = ActionBehaviorDialog(
            keep_behavior=self._keep_behavior,
            reject_behavior=self._reject_behavior,
            parent=self,
        )
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return
        vals = dlg.values()
        self._keep_behavior = dict(vals.get("keep", {}))
        self._reject_behavior = dict(vals.get("reject", {}))
        self._refresh_save_rule_summary()
        self._refresh_summary()

    def _current_filename_pattern_inputs(self):
        if not self._manual_datetime_override_enabled():
            return "", ""
        return self._raw_filename_pattern_inputs()

    def _raw_filename_pattern_inputs(self):
        return (
            normalize_filename_datetime_substring(self.filename_subedit.text()),
            normalize_filename_datetime_pattern(self.filename_pattern_edit.text()),
        )

    def _auto_datetime_source_payload(self):
        auto_dt = auto_detect_datetime_from_filename(self.example_filename) if self.example_filename else None
        if self.log_exists:
            if auto_dt is None:
                try:
                    _, _, log_entries = read_log()
                    if log_entries:
                        auto_dt = str(log_entries[0][1] or "").strip() or None
                except Exception:
                    auto_dt = None
            return {
                "detail": f"Auto-detected as: {auto_dt}" if auto_dt else "Auto-detected as: unavailable",
                "summary": "Auto-detected from GeolocationLog.csv",
            }

        if auto_dt is not None:
            return {
                "detail": f"Auto-detected as: {auto_dt}",
                "summary": "Auto-detected from example TIFF",
            }

        return None

    def _manual_datetime_override_enabled(self):
        auto_payload = self._auto_datetime_source_payload()
        if auto_payload is None:
            return True
        return bool(self.filename_override_check.isChecked())

    def _sync_datetime_override_controls(self):
        auto_payload = self._auto_datetime_source_payload()
        has_auto = auto_payload is not None
        self.filename_override_check.setVisible(has_auto)
        self.filename_override_check.setEnabled(has_auto)
        allow_override = self._manual_datetime_override_enabled()
        self.filename_subedit.setEnabled(allow_override)
        self.filename_pattern_edit.setEnabled(allow_override)

    def _datetime_status_payload(self):
        auto_payload = self._auto_datetime_source_payload()
        raw_substring, raw_pattern = self._raw_filename_pattern_inputs()
        if auto_payload is not None and not self.filename_override_check.isChecked():
            return {
                "kind": "auto",
                "title": "DATETIME AUTO-DETECTED",
                "detail": auto_payload["detail"],
                "summary": auto_payload["summary"],
            }

        _, _, err = validate_user_datetime_pattern(raw_substring, raw_pattern)
        if err:
            return {
                "kind": "needed",
                "title": "DATETIME INPUT NEEDED",
                "detail": err,
                "summary": "Datetime input needed",
            }

        if raw_substring and raw_pattern:
            return {
                "kind": "custom",
                "title": "CUSTOM DATETIME OVERRIDE READY",
                "detail": f"Substring: {raw_substring}    Pattern: {raw_pattern}",
                "summary": f"Custom override ({raw_pattern})",
            }

        if auto_payload is not None and self.filename_override_check.isChecked():
            return {
                "kind": "needed",
                "title": "DATETIME INPUT NEEDED",
                "detail": "Override is on. Enter both substring and pattern.",
                "summary": "Datetime override incomplete",
            }

        return {
            "kind": "needed",
            "title": "DATETIME INPUT NEEDED",
            "detail": "Enter both substring and pattern.",
            "summary": "Datetime input needed",
        }

    def _refresh_datetime_status(self):
        payload = self._datetime_status_payload()
        self._sync_datetime_override_controls()
        if payload["kind"] == "auto":
            bg = "#173d2a"
            edge = "#2E8B57"
            text = "#F4FFF7"
        elif payload["kind"] == "custom":
            bg = "#163247"
            edge = "#56B4E9"
            text = "#F5FBFF"
        else:
            bg = "#4a1d1d"
            edge = "#C23B22"
            text = "#FFF6F4"

        self.filename_status_banner.setText(payload["title"])
        self.filename_status_banner.setStyleSheet(
            f"font-size: 14pt; font-weight: 700; border-radius: 8px; padding: 8px; "
            f"background-color: {bg}; border: 2px solid {edge}; color: {text};"
        )
        self.filename_detail_label.setText(payload["detail"])

    def _clear_missing_vector_notes(self):
        if self._loading_profile:
            return
        self._missing_primary_name = ""
        self._missing_overlay_names = []

    def _clear_missing_basemap_note(self):
        if self._loading_profile:
            return
        self._missing_basemap_name = ""

    def _on_any_control_changed(self, *_args):
        if self._loading_profile:
            return
        self._refresh_datetime_status()
        self._refresh_summary()

    def _apply_startup_ui_scale_selection(self, selected):
        try:
            apply_persisted_ui_scale_setting(selected, reapply=True)
        except Exception:
            pass
        try:
            self.store = update_persisted_scale_state(self.store, ui_scale_value=selected)
            self.store = save_persisted_ui_store_to_script(os.path.abspath(__file__), self.store)
        except Exception:
            pass
        self._apply_theme()
        self._refresh_summary()
        self._queue_startup_dialog_fit()

    def _on_ui_scale_changed(self, *_args):
        if self._loading_profile:
            return
        selected = self._selected_ui_scale_setting()
        self._apply_startup_ui_scale_selection(selected)

    def _on_vector_selection_changed(self, *_args):
        self._clear_missing_vector_notes()
        if self._loading_profile:
            return
        self._refresh_vector_note()
        self._refresh_summary()

    def _on_basemap_selection_changed(self, *_args):
        self._clear_missing_basemap_note()
        if self._loading_profile:
            return
        self._sync_basemap_controls()
        self._refresh_basemap_note()
        self._refresh_summary()

    def _on_profile_changed(self, text):
        if self._loading_profile:
            return
        self._apply_profile_settings(text)

    def _format_vector_summary(self):
        primary_name = self._selected_shapefile_name(self.primary_combo)
        primary_color = str(self.primary_color_combo.currentText() or "lime")
        primary_line = f"{primary_name} ({primary_color})" if primary_name else "none"

        overlays = []
        seen = {primary_name.lower()} if primary_name else set()
        visible_count = int(getattr(self, "_visible_overlay_count", 1) or 1)
        for idx, (shp_combo, color_combo) in enumerate(zip(self.overlay_shp_combos, self.overlay_color_combos)):
            if idx >= visible_count:
                continue
            overlay_name = self._selected_shapefile_name(shp_combo)
            if not overlay_name:
                continue
            key = overlay_name.lower()
            if key in seen:
                continue
            seen.add(key)
            overlays.append(f"{overlay_name} ({color_combo.currentText()})")

        missing = []
        if self._missing_primary_name:
            missing.append(self._missing_primary_name)
        missing.extend(self._missing_overlay_names)
        return primary_line, overlays, missing

    def _refresh_summary(self):
        return

    def _build_settings_from_controls(self):
        base_settings = dict(
            self.profile_settings_by_name.get(self._current_profile_name(), _default_persisted_ui_settings())
        )
        primary_name = self._selected_shapefile_name(self.primary_combo)
        dt_substring, dt_pattern = self._current_filename_pattern_inputs()

        overlay_colors = []
        seen = {primary_name.lower()} if primary_name else set()
        visible_count = int(getattr(self, "_visible_overlay_count", 1) or 1)
        for idx, (shp_combo, color_combo) in enumerate(zip(self.overlay_shp_combos, self.overlay_color_combos)):
            if idx >= visible_count:
                continue
            overlay_name = self._selected_shapefile_name(shp_combo)
            if not overlay_name:
                continue
            key = overlay_name.lower()
            if key in seen:
                continue
            seen.add(key)
            overlay_colors.append({
                "name": overlay_name,
                "color": normalize_picker_color_name(color_combo.currentText(), "lime"),
            })

        base_settings.update({
            "keep_behavior": dict(self._keep_behavior),
            "reject_behavior": dict(self._reject_behavior),
            "filename_dt_substring": dt_substring,
            "filename_dt_pattern": dt_pattern,
            "basemap_mode": self._selected_basemap_mode(),
            "basemap_category": self._selected_basemap_category(),
            "basemap_name": self._selected_basemap_name() if self._selected_basemap_mode() == "single" else "",
            "shp_primary_name": primary_name,
            "shp_primary_color": normalize_picker_color_name(self.primary_color_combo.currentText(), "lime"),
            "shp_overlay_colors": overlay_colors,
        })
        return normalize_persisted_ui_settings(base_settings)

    def _accept_reviewed(self):
        payload = self._datetime_status_payload()
        if payload["kind"] == "needed":
            QMessageBox.warning(self, "Datetime", payload["detail"])
            return
        self.accept()

    def selected_startup_config(self):
        return {
            "profile_name": self._current_profile_name(),
            "settings": self._build_settings_from_controls(),
            "persisted_ui_store": normalize_persisted_ui_store(self.store),
        }


SPLIT_SHORTCUT_COLOR = "#C5E86C"

KEYBOARD_SHORTCUT_ACTIONS = {
    "M": ("Split", SPLIT_SHORTCUT_COLOR),
    "N": ("Rect", SPLIT_SHORTCUT_COLOR),
    "B": ("Undo", SPLIT_SHORTCUT_COLOR),
    "V": ("Line", SPLIT_SHORTCUT_COLOR),
    "C": ("Select", SPLIT_SHORTCUT_COLOR),
    "`": ("Comment 🗨", "#D4D4D4"),
    "W": ("Pan ↑", "#4FA8FF"),
    "A": ("Pan ←", "#4FA8FF"),
    "S": ("Pan ↓", "#4FA8FF"),
    "D": ("Pan →", "#4FA8FF"),
    "1": ("Pan Multi -2x", "#FFD447"),
    "2": ("Pan Multi +2x", "#FFD447"),
    "3": ("Pan Reset", "#FFD447"),
    "Scroll": ("±", "#FFD447"),
    "SPACE": ("Toggle Pan", "#45C07A"),
    "SHIFT": ("Reset Pan / Warp & View", "#3DD2D2"),
    "ENTER": ("Reset Color Adjustments", "#6699CC"),
    "R": ("Toggle Cmap", "#B4A7D6"),
    "F": ("Fullscreen", "#FFAA5D"),
    "ALT": ("Magnify 🔎︎", "#D9C6B6"),
    "/": ("Toggle ☯", "#D4D4D4"),
    "G": ("Warp / Apply", "#93E26E"),
    "H": ("Source Warp Pt", "#93E26E"),
    "J": ("Target Warp Pt", "#93E26E"),
    "BACKSPACE": ("Undo Warp Pt", "#FF6760"),
    "E": ("Edge Detect", "#B28DFF"),
    "X": ("Skip Scene", "#A1887F"),
    "P": ("Contrast +5%", "#FFCCBC"),
    "L": ("Contrast -5%", "#FFCCBC"),
    "[": ("Opacity +5%", "#AEE6FF"),
    "]": ("Blend Next", "#6CCAFF"),
    ";": ("Opacity -5%", "#AEE6FF"),
    "'": ("Blend Prev", "#6CCAFF"),
    "O": ("Gamma +20%", "#FFB1C8"),
    "K": ("Gamma -20%", "#FFB1C8"),
    "-": ("Filename -10%", "#D9C6B6"),
    "=": ("Filename +10%", "#D9C6B6"),
    "\\": ("Window Layout", "#D9C6B6"),
    "TAB": ("Change Alt Cmap", "#D9C6B6"),
    "CTRL": ("File / Save", "#D4D4D4"),
}

KEYBOARD_SHORTCUT_ROWS = [
    [("`", 1), ("1", 1), ("2", 1), ("3", 1), ("4", 1), ("5", 1), ("6", 1),
     ("7", 1), ("8", 1), ("9", 1), ("0", 1), ("-", 1), ("=", 1), ("BACKSPACE", 2)],
    [("TAB", 1.5), ("Q", 1), ("W", 1), ("E", 1), ("R", 1), ("T", 1), ("Y", 1),
     ("U", 1), ("I", 1), ("O", 1), ("P", 1), ("[", 1), ("]", 1), ("\\", 1.5)],
    [("CAPS", 1.75), ("A", 1), ("S", 1), ("D", 1), ("F", 1), ("G", 1), ("H", 1),
     ("J", 1), ("K", 1), ("L", 1), (";", 1), ("'", 1), ("ENTER", 2.25)],
    [("SHIFT", 2.25), ("Z", 1), ("X", 1), ("C", 1), ("V", 1), ("B", 1), ("N", 1),
     ("M", 1), (",", 1), (".", 1), ("/", 1), ("SHIFT", 2.75)],
    [("CTRL", 1.25), ("WIN", 1.25), ("ALT", 1.25),
     ("SPACE", 6.25), ("ALT", 1.25), ("WIN", 1.25), ("MENU", 1.25), ("CTRL", 1.25)],
]

LEFT_BTN_COL = "#FFB86C"
RIGHT_BTN_COL = "#6CCAFF"

UNIT = 1.0
KEYBOARD_SHORTCUT_H = 1.0
KEYBOARD_SHORTCUT_VPAD = 0.25
KEYBOARD_SHORTCUT_FONT_KEY_SIZE = 6
KEYBOARD_SHORTCUT_FONT_ACT_SIZE = 4
KEYBOARD_SHORTCUT_SCALE = 1.25

KEYBOARD_SHORTCUT_H_S = KEYBOARD_SHORTCUT_H * KEYBOARD_SHORTCUT_SCALE
KEYBOARD_SHORTCUT_V_PAD_S = KEYBOARD_SHORTCUT_VPAD * KEYBOARD_SHORTCUT_SCALE
KEYBOARD_SHORTCUT_MOUSE_W = 3.0
KEYBOARD_SHORTCUT_MOUSE_H = 5.0
KEYBOARD_SHORTCUT_MOUSE_W_S = KEYBOARD_SHORTCUT_MOUSE_W * KEYBOARD_SHORTCUT_SCALE
KEYBOARD_SHORTCUT_MOUSE_H_S = KEYBOARD_SHORTCUT_MOUSE_H * KEYBOARD_SHORTCUT_SCALE
KEYBOARD_SHORTCUT_FONT_KEY = dict(
    size=max(1, int(round(KEYBOARD_SHORTCUT_FONT_KEY_SIZE * KEYBOARD_SHORTCUT_SCALE))),
    weight="bold",
    va="center",
    ha="center",
)
KEYBOARD_SHORTCUT_FONT_ACT = dict(
    size=max(1, int(round(KEYBOARD_SHORTCUT_FONT_ACT_SIZE * KEYBOARD_SHORTCUT_SCALE))),
    va="top",
    ha="center",
)

KEYBOARD_SHORTCUT_KEY_LEFT_PAD = 0.08
KEYBOARD_SHORTCUT_GAP_KB_MOUSE = 0.5
KEYBOARD_SHORTCUT_LEFT_MARGIN = 0.05
KEYBOARD_SHORTCUT_RIGHT_MARGIN = 0.26
KEYBOARD_SHORTCUT_TOP_MARGIN = 1.28
KEYBOARD_SHORTCUT_BOTTOM_MARGIN = 1.0
KEYBOARD_SHORTCUT_GROUP_SHIFT_X = 0.0
KEYBOARD_SHORTCUT_PNG_DIR = os.path.join(str(Path.home()), "OneDrive", "Documents", "New project")
KEYBOARD_SHORTCUT_PNG_DPI = 600
KEYBOARD_SHORTCUT_DIALOG_MIN_W = 760
KEYBOARD_SHORTCUT_DIALOG_MIN_H = 420
KEYBOARD_SHORTCUT_DIALOG_TARGET_W = 1420
KEYBOARD_SHORTCUT_BASE_FONT = FontProperties(family=["DejaVu Sans"])
KEYBOARD_SHORTCUT_EMOJI_FONT = FontProperties(family=["DejaVu Sans", "Segoe UI Emoji"])
KEYBOARD_SHORTCUT_SYMBOL_FONT = FontProperties(family=["Segoe UI Symbol", "Segoe UI Emoji", "DejaVu Sans"])


def normalize_keyboard_shortcuts_theme_mode(mode, fallback="light"):
    fallback_mode = "light" if str(fallback).lower() == "light" else "dark"
    value = str(mode or fallback_mode).strip().lower()
    if value not in ("light", "dark"):
        value = fallback_mode
    return "light" if value == "light" else "dark"


def keyboard_shortcuts_surface_background(mode):
    return "#FFFFFF" if normalize_keyboard_shortcuts_theme_mode(mode, "light") == "light" else "#000000"


def keyboard_shortcuts_figure_geometry():
    kb_w_units = max(sum(width for _, width in row) for row in KEYBOARD_SHORTCUT_ROWS)
    kb_w = kb_w_units * KEYBOARD_SHORTCUT_SCALE
    n_rows = len(KEYBOARD_SHORTCUT_ROWS)
    kb_h = n_rows * KEYBOARD_SHORTCUT_H_S + (n_rows - 1) * KEYBOARD_SHORTCUT_V_PAD_S

    content_w = (
        KEYBOARD_SHORTCUT_KEY_LEFT_PAD
        + kb_w
        + KEYBOARD_SHORTCUT_GAP_KB_MOUSE
        + KEYBOARD_SHORTCUT_MOUSE_W_S
    )
    content_h = max(kb_h, KEYBOARD_SHORTCUT_MOUSE_H_S)

    fig_w = content_w + KEYBOARD_SHORTCUT_LEFT_MARGIN + KEYBOARD_SHORTCUT_RIGHT_MARGIN
    fig_h = content_h + KEYBOARD_SHORTCUT_TOP_MARGIN + KEYBOARD_SHORTCUT_BOTTOM_MARGIN
    return fig_w, fig_h, fig_w * 0.5, fig_h * 0.5


def keyboard_shortcuts_png_path(mode):
    mode = normalize_keyboard_shortcuts_theme_mode(mode, "light")
    return os.path.join(KEYBOARD_SHORTCUT_PNG_DIR, f"GeoViewer_Keyboard_{mode}.png")


def render_keyboard_shortcuts_figure(fig, mode="light"):
    mode = normalize_keyboard_shortcuts_theme_mode(mode, "light")
    if mode == "dark":
        fig_bg = "#000000"
        title_color = "#D3D3D3"
        caption_color = "#D3D3D3"
    else:
        fig_bg = "#FFFFFF"
        title_color = "#000000"
        caption_color = "#000000"

    fig_w, fig_h, _, _ = keyboard_shortcuts_figure_geometry()
    kb_w_units = max(sum(width for _, width in row) for row in KEYBOARD_SHORTCUT_ROWS)
    kb_w = kb_w_units * KEYBOARD_SHORTCUT_SCALE
    n_rows = len(KEYBOARD_SHORTCUT_ROWS)
    kb_h = n_rows * KEYBOARD_SHORTCUT_H_S + (n_rows - 1) * KEYBOARD_SHORTCUT_V_PAD_S
    content_h = max(kb_h, KEYBOARD_SHORTCUT_MOUSE_H_S)

    fig.clear()
    fig.set_facecolor(fig_bg)
    ax = fig.add_axes([0.0, 0.0, 1.0, 1.0])
    ax.set_facecolor(fig_bg)
    ax.set_xlim(0, fig_w)
    ax.set_ylim(0, fig_h)
    ax.axis("off")

    keyboard_x0 = (
        KEYBOARD_SHORTCUT_LEFT_MARGIN
        + KEYBOARD_SHORTCUT_KEY_LEFT_PAD
        - KEYBOARD_SHORTCUT_GROUP_SHIFT_X
    )
    mouse_x0 = keyboard_x0 + kb_w + KEYBOARD_SHORTCUT_GAP_KB_MOUSE
    y = (
        KEYBOARD_SHORTCUT_BOTTOM_MARGIN
        + content_h / 2
        - KEYBOARD_SHORTCUT_H_S / 2
        + (n_rows - 1) * (KEYBOARD_SHORTCUT_H_S + KEYBOARD_SHORTCUT_V_PAD_S) / 2
    )
    mouse_y0 = KEYBOARD_SHORTCUT_BOTTOM_MARGIN + content_h / 2 - KEYBOARD_SHORTCUT_MOUSE_H_S / 2

    for row in KEYBOARD_SHORTCUT_ROWS:
        x = keyboard_x0
        for key, width in row:
            width_scaled = width * KEYBOARD_SHORTCUT_SCALE
            key_name = str(key).upper()
            face_color = KEYBOARD_SHORTCUT_ACTIONS.get(key_name, (None, "#F0F0F0"))[1]
            rect = Rectangle(
                (x, y),
                width_scaled * UNIT,
                KEYBOARD_SHORTCUT_H_S,
                linewidth=0.6,
                facecolor=face_color,
                edgecolor="#555555",
                joinstyle="round",
                zorder=2,
            )
            ax.add_patch(rect)

            display_key = key_name if key_name != "SPACE" else "␣"
            ax.text(
                x + (width_scaled * UNIT) / 2,
                y + KEYBOARD_SHORTCUT_H_S * 0.60,
                display_key,
                **KEYBOARD_SHORTCUT_FONT_KEY,
                fontproperties=KEYBOARD_SHORTCUT_BASE_FONT,
                color="#000000",
                zorder=4,
            )

            if key_name in KEYBOARD_SHORTCUT_ACTIONS:
                action = KEYBOARD_SHORTCUT_ACTIONS[key_name][0]
                action_x = x + (width_scaled * UNIT) / 2
                action_y = y + KEYBOARD_SHORTCUT_H_S * 0.18
                if "🔎" in action or "🗨" in action:
                    action_font = (
                        KEYBOARD_SHORTCUT_SYMBOL_FONT
                        if "🗨" in action
                        else KEYBOARD_SHORTCUT_EMOJI_FONT
                    )
                    ax.text(
                        action_x,
                        action_y,
                        action,
                        **KEYBOARD_SHORTCUT_FONT_ACT,
                        color="#000000",
                        fontproperties=action_font,
                    )
                else:
                    ax.text(
                        action_x,
                        action_y,
                        action,
                        **KEYBOARD_SHORTCUT_FONT_ACT,
                        fontproperties=KEYBOARD_SHORTCUT_BASE_FONT,
                        color="#000000",
                    )

            x += width_scaled * UNIT
        y -= (KEYBOARD_SHORTCUT_H_S + KEYBOARD_SHORTCUT_V_PAD_S)

    mouse_body = FancyBboxPatch(
        (mouse_x0, mouse_y0),
        KEYBOARD_SHORTCUT_MOUSE_W_S,
        KEYBOARD_SHORTCUT_MOUSE_H_S,
        boxstyle="round,pad=0.2,rounding_size=0.3",
        linewidth=0.6,
        edgecolor="#555555",
        facecolor="#F0F0F0",
        zorder=1,
    )
    ax.add_patch(mouse_body)

    button_y = mouse_y0 + KEYBOARD_SHORTCUT_MOUSE_H_S * 0.6
    button_h = KEYBOARD_SHORTCUT_MOUSE_H_S * 0.4 - (0.2 * KEYBOARD_SHORTCUT_SCALE)
    wheel_gap = KEYBOARD_SHORTCUT_MOUSE_W_S * 0.26
    side_button_w = (KEYBOARD_SHORTCUT_MOUSE_W_S - wheel_gap) / 2 - (0.12 * KEYBOARD_SHORTCUT_SCALE)
    oval_w = side_button_w * 0.95
    oval_h = max(0.0, button_h * 0.9)
    left_center_x = mouse_x0 + (KEYBOARD_SHORTCUT_MOUSE_W_S - wheel_gap) / 4
    right_center_x = mouse_x0 + KEYBOARD_SHORTCUT_MOUSE_W_S - (KEYBOARD_SHORTCUT_MOUSE_W_S - wheel_gap) / 4
    wheel_w = KEYBOARD_SHORTCUT_MOUSE_W_S * 0.16
    wheel_h = button_h * 0.72
    wheel_x = mouse_x0 + KEYBOARD_SHORTCUT_MOUSE_W_S / 2 - wheel_w / 2
    wheel_y = button_y + (button_h - wheel_h) / 2
    wheel_color = KEYBOARD_SHORTCUT_ACTIONS["Scroll"][1]
    wheel_label = KEYBOARD_SHORTCUT_ACTIONS["Scroll"][0]

    ax.add_patch(
        FancyBboxPatch(
            (left_center_x - oval_w / 2, button_y + (button_h - oval_h) / 2),
            oval_w,
            oval_h,
            boxstyle="round,pad=0.02,rounding_size=0.12",
            facecolor=LEFT_BTN_COL,
            edgecolor="none",
            zorder=2,
        )
    )
    ax.add_patch(
        FancyBboxPatch(
            (right_center_x - oval_w / 2, button_y + (button_h - oval_h) / 2),
            oval_w,
            oval_h,
            boxstyle="round,pad=0.02,rounding_size=0.12",
            facecolor=RIGHT_BTN_COL,
            edgecolor="none",
            zorder=2,
        )
    )
    ax.add_patch(
        FancyBboxPatch(
            (wheel_x, wheel_y),
            wheel_w,
            wheel_h,
            boxstyle="round,pad=0.02,rounding_size=0.08",
            facecolor=wheel_color,
            edgecolor="#666666",
            linewidth=0.6,
            zorder=4,
        )
    )

    ax.add_line(
        Line2D(
            [mouse_x0 + KEYBOARD_SHORTCUT_MOUSE_W_S / 2, mouse_x0 + KEYBOARD_SHORTCUT_MOUSE_W_S / 2],
            [mouse_y0 + KEYBOARD_SHORTCUT_MOUSE_H_S * 0.4, mouse_y0 + KEYBOARD_SHORTCUT_MOUSE_H_S * 0.6],
            linewidth=0.6,
            color="#AAAAAA",
            zorder=3,
        )
    )
    for frac in (0.25, 0.75):
        y_tick = wheel_y + wheel_h * frac
        ax.add_line(
            Line2D(
                [wheel_x + wheel_w * 0.2, wheel_x + wheel_w * 0.8],
                [y_tick, y_tick],
                linewidth=0.5,
                color="#AA9933",
                zorder=5,
            )
        )

    ax.text(left_center_x, mouse_y0 + KEYBOARD_SHORTCUT_MOUSE_H_S * 0.85, "L",
            **KEYBOARD_SHORTCUT_FONT_KEY, fontproperties=KEYBOARD_SHORTCUT_BASE_FONT, color="#000000", zorder=5)
    ax.text(right_center_x, mouse_y0 + KEYBOARD_SHORTCUT_MOUSE_H_S * 0.85, "R",
            **KEYBOARD_SHORTCUT_FONT_KEY, fontproperties=KEYBOARD_SHORTCUT_BASE_FONT, color="#000000", zorder=5)
    ax.text(
        mouse_x0 + KEYBOARD_SHORTCUT_MOUSE_W_S / 2,
        wheel_y + wheel_h / 2,
        wheel_label,
        ha="center",
        va="center",
        fontsize=max(1, int(round(6 * KEYBOARD_SHORTCUT_SCALE))),
        fontweight="bold",
        fontproperties=KEYBOARD_SHORTCUT_BASE_FONT,
        color="#000000",
        zorder=6,
    )

    ax.text(
        left_center_x,
        mouse_y0 + KEYBOARD_SHORTCUT_MOUSE_H_S * 0.8,
        "Left-drag:\nZoom",
        ha="center",
        va="top",
        fontsize=max(1, int(round(5 * KEYBOARD_SHORTCUT_SCALE))),
        fontproperties=KEYBOARD_SHORTCUT_BASE_FONT,
        color="#000000",
        zorder=5,
    )
    ax.text(
        right_center_x,
        mouse_y0 + KEYBOARD_SHORTCUT_MOUSE_H_S * 0.8,
        "Right-click:\nReset View",
        ha="center",
        va="top",
        fontsize=max(1, int(round(5 * KEYBOARD_SHORTCUT_SCALE))),
        fontproperties=KEYBOARD_SHORTCUT_BASE_FONT,
        color="#000000",
        zorder=5,
    )

    fig.text(
        0.5,
        0.95,
        "NASA JPL THERMAL VIEWER",
        ha="center",
        va="bottom",
        color=title_color,
        fontsize=int(round(12 * KEYBOARD_SHORTCUT_SCALE)),
        fontweight="bold",
        fontproperties=KEYBOARD_SHORTCUT_BASE_FONT,
    )
    fig.text(
        0.5,
        0.085,
        "Split Mode: C Highlight | V Line | B Undo | N Rect | M Toggle | W/A/S/D Move | G/H/J Warp | Delete Remove",
        ha="center",
        va="center",
        color="#000000",
        fontsize=int(round(6.5 * KEYBOARD_SHORTCUT_SCALE)),
        fontproperties=KEYBOARD_SHORTCUT_BASE_FONT,
        bbox=dict(boxstyle="round,pad=0.25", facecolor=SPLIT_SHORTCUT_COLOR, edgecolor="#555555", linewidth=0.6),
    )
    fig.text(
        0.5,
        0.03,
        "GeoViewer v1.15 | Longenecker et al. | MIT License 2025",
        ha="center",
        va="top",
        color=caption_color,
        fontsize=int(round(8 * KEYBOARD_SHORTCUT_SCALE)),
        fontproperties=KEYBOARD_SHORTCUT_BASE_FONT,
    )


def save_keyboard_shortcuts_png(mode, outfile):
    mode = normalize_keyboard_shortcuts_theme_mode(mode, "light")
    _, _, fig_w_inches, fig_h_inches = keyboard_shortcuts_figure_geometry()
    fig = Figure(figsize=(fig_w_inches, fig_h_inches), dpi=KEYBOARD_SHORTCUT_PNG_DPI)
    render_keyboard_shortcuts_figure(fig, mode)
    fig.patch.set_facecolor("#000000" if mode == "dark" else "#FFFFFF")
    fig.savefig(
        outfile,
        bbox_inches=None,
        dpi=KEYBOARD_SHORTCUT_PNG_DPI,
        pad_inches=0,
        facecolor=fig.get_facecolor(),
    )
    plt.close(fig)


def ensure_keyboard_shortcuts_pngs():
    os.makedirs(KEYBOARD_SHORTCUT_PNG_DIR, exist_ok=True)
    paths = {}
    for mode in ("light", "dark"):
        path = keyboard_shortcuts_png_path(mode)
        try:
            needs_render = not os.path.isfile(path) or os.path.getsize(path) <= 0
            if not needs_render:
                try:
                    needs_render = os.path.getmtime(path) < os.path.getmtime(__file__)
                except Exception:
                    pass
        except Exception:
            needs_render = True
        if needs_render:
            save_keyboard_shortcuts_png(mode, path)
        paths[mode] = path
    return paths


def geoviewer_info_html(theme_mode=None):
    palette = build_theme_palette(theme_mode)
    accent = "#2D5FFF" if str(theme_mode or get_app_theme_mode()).lower() == "light" else "#7DB3FF"
    warning_bg = "#FFF3CD" if str(theme_mode or get_app_theme_mode()).lower() == "light" else "#2E2410"
    warning_border = "#D39E00" if str(theme_mode or get_app_theme_mode()).lower() == "light" else "#B98B21"
    info_bg = "#EEF4FF" if str(theme_mode or get_app_theme_mode()).lower() == "light" else "#101A2E"
    info_border = "#8FB3FF" if str(theme_mode or get_app_theme_mode()).lower() == "light" else "#2D5FFF"
    code_bg = "#F3F3F3" if str(theme_mode or get_app_theme_mode()).lower() == "light" else "#1D1D1D"
    return f"""
    <html>
    <head>
    <style>
        body {{
            background: {palette['input_bg']};
            color: {palette['text']};
            font-family: "Segoe UI", Arial, sans-serif;
            font-size: 11pt;
            line-height: 1.42;
            margin: 0;
        }}
        h1 {{
            color: {palette['heading']};
            font-size: 22pt;
            margin: 2px 0 4px 0;
            font-weight: 750;
        }}
        h2 {{
            color: {palette['heading']};
            font-size: 14pt;
            margin: 18px 0 7px 0;
            font-weight: 700;
            border-bottom: 1px solid {palette['border']};
            padding-bottom: 4px;
        }}
        p {{
            margin: 7px 0;
        }}
        ol, ul {{
            margin-top: 6px;
            margin-bottom: 8px;
            padding-left: 24px;
        }}
        li {{
            margin: 5px 0;
        }}
        .subtitle {{
            color: {palette['muted']};
            font-size: 11pt;
            margin-bottom: 12px;
        }}
        .callout {{
            border: 1px solid {info_border};
            background: {info_bg};
            border-radius: 8px;
            padding: 10px 12px;
            margin: 10px 0;
        }}
        .warning {{
            border-color: {warning_border};
            background: {warning_bg};
        }}
        .label {{
            color: {accent};
            font-weight: 700;
        }}
        code {{
            background: {code_bg};
            color: {palette['heading']};
            border: 1px solid {palette['border']};
            border-radius: 4px;
            padding: 1px 4px;
            font-family: "Lucida Console", Consolas, monospace;
            font-size: 9.5pt;
        }}
    </style>
    </head>
    <body>
        <h1>GeoViewer</h1>
        <p class="subtitle"><b>Thermal scene QA/QC and interactive georeferencing</b></p>

        <div class="callout warning">
            <span class="label">Important:</span>
            Do not keep your <code>TIF</code> files or <code>GeolocationLog.csv</code> open in other software while using GeoViewer. Output writing can fail unpredictably if another program has those files locked.
        </div>

        <h2>Introduction</h2>
        <p>
            GeoViewer is a lightweight, single-file Python application for visually inspecting and interactively georeferencing thermal satellite scenes against vector shapefile data. Its primary use case is quality assurance and control for thermal products such as <b>ECOSTRESS</b> or <b>Landsat LST/SST</b> before they enter an automated pipeline.
        </p>
        <p>
            The tool was written for the <b>NASA Jet Propulsion Laboratory</b> and is released under the permissive <b>MIT license</b> so it can be adapted freely. Please cite when using or modifying it.
        </p>

        <h2>What the Program Does</h2>
        <ol>
            <li>Reads every <code>.tif</code> in the working directory and displays scenes side by side.</li>
            <li>Overlays a user-supplied shapefile, such as a shoreline or AOI boundary.</li>
            <li>Imports a reference basemap, such as a Landsat L8/9 single-band scene.</li>
            <li>Allows users to pan, zoom, and nudge each scene with keyboard control.</li>
            <li>Writes each accepted image back to disk with an updated affine transform or warp.</li>
            <li>Logs the true bearing and meter shift for every correction to <code>GeolocationLog.csv</code>.</li>
        </ol>

        <h2>Installation &amp; Setup</h2>
        <ol>
            <li>Clone or download <code>GeoViewer.py</code> into the folder that holds your TIF scenes and SHP files.</li>
            <li>Download <b>Python &ge; 3.12</b> from the Microsoft Store, or activate a clean environment.</li>
            <li>Install the required Python packages listed in System Requirements.</li>
            <li>Ensure imagery and SHPs are present, and that TIF filenames contain a datetime.</li>
            <li>Run <code>python GeoViewer.py</code> from the folder containing your SHPs and TIFs, or set the path when prompted.</li>
        </ol>

        <h2>Nomenclature &amp; Datetimes</h2>
        <p>
            On startup, if <code>GeolocationLog.csv</code> is not found in the working folder, GeoViewer prompts you to describe how your filenames are structured so it can extract datetimes correctly.
        </p>
        <div class="callout">
            <span class="label">Best practice:</span>
            Check the output log file after startup to confirm datetime detection and logging. Accurate datetime parsing is essential for matching scenes, resuming work, and Auto-Geocorrect.
        </div>
        <p>
            <b>NASA Earthdata</b> and <b>AppEEARS</b> naming schemes are automatically recognized when they use datetime strings such as <code>yyyydoyThhmmss</code> or <code>yyyydoyhhmmss</code>. Fallback logic searches for common combinations of year, month, day, day of year, hour, minute, and second. If you use an in-house filename format, manual specification is recommended.
        </p>

        <h2>Processing Logic</h2>
        <p>
            Start GeoViewer from the working folder that contains the main script, thermal TIFF files, and corresponding shapefiles. Basemap TIFFs should be placed in a subfolder named <code>Basemaps</code>.
        </p>
        <ul>
            <li>If no log exists, GeoViewer creates <code>GeolocationLog.csv</code> and prompts for datetime rules when needed.</li>
            <li>The program asks for the primary ground-truth shapefile, optional ancillary overlays, and any available basemap.</li>
            <li>It compares logged datetimes with datetimes detected from TIFF filenames, then resumes at the next unprocessed datetime.</li>
            <li>Corrections are stored during use and written to <code>GeolocationLog.csv</code> when the completion screen closes or when the program exits.</li>
            <li>The companion <code>GeoPlot.py</code> script can be run from the same folder to visualize referencing statistics saved in the log.</li>
        </ul>

        <h2>Basemaps &amp; Auto-Geocorrect</h2>
        <p>
            Initial geolocation correction should be performed only on the thermal TIFF files. For this first pass, keep the main script, shapefiles, and thermal TIFFs in the working folder, with basemaps stored separately in <code>Basemaps</code>.
        </p>
        <p><b>Supported basemap examples include:</b></p>
        <ul>
            <li>Landsat tri-band <code>_refl</code> and single-band <code>_tir</code> files.</li>
            <li>Landsat single-band TIFs ending in suffixes such as <code>_B1</code>, <code>_B5</code>, or <code>_B10</code>.</li>
            <li>HLS single-band basemaps beginning with <code>HLS</code> and ending in a band suffix such as <code>.B04</code>.</li>
            <li>OPERA binary water basemaps beginning with <code>OPERA_</code> and ending in <code>_BWTR</code>.</li>
            <li>ECOSTRESS water basemaps beginning with <code>ECO</code> and ending in <code>_water</code>.</li>
        </ul>
        <p>
            After thermal files have been corrected and logged, additional matching TIFF products may be added for Auto-Geocorrect. These files must use the same filename datetime format and match a datetime already recorded in <code>GeolocationLog.csv</code>.
        </p>
        <p>
            Eligible products may include <b>cloud</b>, <b>EmisWB</b>, <b>height</b>, <b>error</b>, <b>QC</b>, and <b>view-zenith</b> files. GeoViewer applies logged translation and/or warp parameters to matching files and records the results. For ECOSTRESS, the water TIFF is logged but not auto-corrected because it is derived from separate geolocation data and does not require this correction step.
        </p>
    </body>
    </html>
    """


class GeoViewerInfoDialog(QtWidgets.QDialog):
    def __init__(self, theme_mode=None, parent=None):
        super().__init__(parent)
        self.setObjectName("GeoViewerInfoDialogRoot")
        self.setWindowTitle("Info")
        self.setModal(False)
        self.setWindowModality(QtCore.Qt.NonModal)
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)
        self.setWindowFlag(QtCore.Qt.Window, True)
        self.setWindowFlag(QtCore.Qt.WindowMinimizeButtonHint, True)
        self.setWindowFlag(QtCore.Qt.WindowMaximizeButtonHint, True)
        self.setWindowFlag(QtCore.Qt.WindowCloseButtonHint, True)
        self.setSizeGripEnabled(True)

        self._theme_mode = "light" if str(theme_mode or get_app_theme_mode()).lower() == "light" else "dark"

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        self._browser = QtWidgets.QTextBrowser(self)
        self._browser.setOpenExternalLinks(True)
        self._browser.setTextInteractionFlags(
            QtCore.Qt.TextBrowserInteraction | QtCore.Qt.TextSelectableByMouse
        )
        self._browser.setHtml(geoviewer_info_html(self._theme_mode))
        root.addWidget(self._browser, 1)

        button_row = QtWidgets.QHBoxLayout()
        button_row.addStretch(1)
        close_btn = QtWidgets.QPushButton("Close", self)
        close_btn.setDefault(True)
        close_btn.setAutoDefault(True)
        close_btn.clicked.connect(self.close)
        button_row.addWidget(close_btn)
        root.addLayout(button_row)

        self.resize(920, 680)
        self._apply_theme()

    def update_dialog_state(self, theme_mode=None):
        if theme_mode is not None:
            self._theme_mode = "light" if str(theme_mode).lower() == "light" else "dark"
        self._apply_theme()

    def _apply_theme(self):
        palette = build_theme_palette(self._theme_mode)
        self.setStyleSheet(
            build_app_stylesheet(self._theme_mode)
            + f"""
            QDialog#GeoViewerInfoDialogRoot {{
                background-color: {palette['window_bg']};
                color: {palette['text']};
            }}
            QTextBrowser {{
                background-color: {palette['input_bg']};
                color: {palette['text']};
                border: 1px solid {palette['border']};
                border-radius: 6px;
                padding: 10px;
                font-family: Segoe UI;
                font-size: 11pt;
            }}
            """
        )
        if hasattr(self, "_browser"):
            self._browser.setHtml(geoviewer_info_html(self._theme_mode))


class KeyboardShortcutsDialog(QtWidgets.QDialog):
    preferencesChanged = QtCore.pyqtSignal(str, bool)

    def __init__(self, image_paths, initial_theme_mode="light", lock_theme=False, parent=None):
        super().__init__(parent)
        self.setObjectName("KeyboardShortcutsDialogRoot")
        self.setWindowTitle("Keyboard Shortcuts")
        self.setModal(False)
        self.setWindowModality(QtCore.Qt.NonModal)
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)
        self.setWindowFlag(QtCore.Qt.Window, True)
        self.setWindowFlag(QtCore.Qt.WindowMinimizeButtonHint, True)
        self.setWindowFlag(QtCore.Qt.WindowMaximizeButtonHint, True)
        self.setWindowFlag(QtCore.Qt.WindowCloseButtonHint, True)
        self.setSizeGripEnabled(True)
        force_widget_ui_scale_100(self)
        self.setFont(QtGui.QFont("Lucida Console", 11))
        try:
            self.setProperty(AppWideScreenScrollController._NO_SCROLL_PROP, True)
        except Exception:
            pass

        image_paths = image_paths if isinstance(image_paths, dict) else {}
        self._image_paths = {
            "light": str(image_paths.get("light") or ""),
            "dark": str(image_paths.get("dark") or ""),
        }
        self._pixmaps = {
            mode: QtGui.QPixmap(path)
            for mode, path in self._image_paths.items()
        }
        self._theme_mode = normalize_keyboard_shortcuts_theme_mode(initial_theme_mode, "light")
        self._lock_theme = bool(lock_theme)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        header = QtWidgets.QHBoxLayout()
        header.setSpacing(10)
        header.addStretch(1)

        self._toggle_button = QtWidgets.QPushButton(self)
        self._toggle_button.setAutoDefault(False)
        self._toggle_button.clicked.connect(self._toggle_theme)
        self._lock_checkbox = QtWidgets.QCheckBox(
            "Always use the selected light/dark theme for this shortcut viewer",
            self,
        )
        self._lock_checkbox.setChecked(self._lock_theme)
        self._lock_checkbox.toggled.connect(self._on_lock_toggled)
        header.addWidget(self._lock_checkbox, 0)
        header.addWidget(self._toggle_button, 0)
        root.addLayout(header)

        self._scroll = QtWidgets.QScrollArea(self)
        self._scroll.setWidgetResizable(False)
        self._scroll.setAlignment(QtCore.Qt.AlignCenter)
        self._scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        root.addWidget(self._scroll, 1)

        self._image_label = QtWidgets.QLabel(self)
        self._image_label.setAlignment(QtCore.Qt.AlignCenter)
        self._image_label.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Ignored)
        self._image_label.setText("Loading keyboard shortcuts image...")
        self._scroll.setWidget(self._image_label)

        self._set_initial_size()
        self._refresh_view()

    def _emit_preferences(self):
        self.preferencesChanged.emit(self._theme_mode, bool(self._lock_checkbox.isChecked()))

    def _status_text(self):
        if self._lock_checkbox.isChecked():
            return f"Shortcut viewer theme: {self._theme_mode.title()} (locked for future opens)"
        return (
            f"Shortcut viewer theme: {self._theme_mode.title()} "
            f"(follows the current GeoViewer theme unless locked)"
        )

    def _current_pixmap(self):
        return self._pixmaps.get(self._theme_mode, QtGui.QPixmap())

    def _available_geometry(self):
        screen = None
        try:
            screen = self.screen()
        except Exception:
            screen = None
        if screen is None:
            try:
                screen = QtWidgets.QApplication.screenAt(QtGui.QCursor.pos())
            except Exception:
                screen = None
        if screen is None:
            try:
                screen = QtWidgets.QApplication.primaryScreen()
            except Exception:
                screen = None
        if screen is not None:
            try:
                return screen.availableGeometry()
            except Exception:
                pass
        return QtCore.QRect(0, 0, 1280, 720)

    def _set_initial_size(self):
        available = self._available_geometry()
        max_w = max(
            KEYBOARD_SHORTCUT_DIALOG_MIN_W,
            min(int(available.width()) - 40, int(round(float(available.width()) * 0.94))),
        )
        max_h = max(
            KEYBOARD_SHORTCUT_DIALOG_MIN_H,
            min(int(available.height()) - 40, int(round(float(available.height()) * 0.88))),
        )
        pixmap = self._current_pixmap()
        image_aspect = 2.4
        if not pixmap.isNull() and pixmap.height() > 0:
            image_aspect = max(0.5, float(pixmap.width()) / float(pixmap.height()))
        chrome_h = 96
        target_w = min(max_w, KEYBOARD_SHORTCUT_DIALOG_TARGET_W)
        target_h = min(
            max_h,
            max(KEYBOARD_SHORTCUT_DIALOG_MIN_H, int(round(target_w / image_aspect)) + chrome_h),
        )

        def apply_size_limits():
            self.setMinimumSize(KEYBOARD_SHORTCUT_DIALOG_MIN_W, KEYBOARD_SHORTCUT_DIALOG_MIN_H)
            self.setMaximumSize(max_w, max_h)
            self.resize(target_w, target_h)

        _with_ui_scale_override(1.0, apply_size_limits)

    def update_dialog_state(self, image_paths=None, theme_mode=None, lock_theme=None):
        if isinstance(image_paths, dict):
            self._image_paths = {
                "light": str(image_paths.get("light") or self._image_paths.get("light") or ""),
                "dark": str(image_paths.get("dark") or self._image_paths.get("dark") or ""),
            }
            self._pixmaps = {
                mode: QtGui.QPixmap(path)
                for mode, path in self._image_paths.items()
            }
        if theme_mode is not None:
            self._theme_mode = normalize_keyboard_shortcuts_theme_mode(theme_mode, self._theme_mode)
        if lock_theme is not None:
            self._lock_theme = bool(lock_theme)
            self._lock_checkbox.blockSignals(True)
            self._lock_checkbox.setChecked(self._lock_theme)
            self._lock_checkbox.blockSignals(False)

        self._refresh_view()

    def _update_image_display(self, use_base_scale=False):
        palette = build_theme_palette(self._theme_mode)
        surface_bg = keyboard_shortcuts_surface_background(self._theme_mode)
        pixmap = self._current_pixmap()
        if pixmap.isNull():
            image_path = str(self._image_paths.get(self._theme_mode) or "")
            self._image_label.setPixmap(QtGui.QPixmap())
            self._image_label.setText(f"Could not load keyboard shortcuts image:\n{image_path}")
            self._image_label.setStyleSheet(
                f"color: {palette['text']}; background-color: {surface_bg};"
            )
            return

        viewport = self._scroll.viewport().size()
        avail_w = max(1, int(viewport.width()) - 8)
        avail_h = max(1, int(viewport.height()) - 8)
        try:
            dpr = float(self.devicePixelRatioF())
        except Exception:
            dpr = 1.0
        if not math.isfinite(dpr) or dpr <= 0:
            dpr = 1.0
        scaled = pixmap.scaled(
            max(1, int(round(avail_w * dpr))),
            max(1, int(round(avail_h * dpr))),
            QtCore.Qt.KeepAspectRatio,
            QtCore.Qt.SmoothTransformation,
        )
        try:
            scaled.setDevicePixelRatio(dpr)
        except Exception:
            dpr = 1.0
        self._image_label.setText("")
        self._image_label.setPixmap(scaled)
        logical_size = QtCore.QSize(
            max(1, int(math.ceil(float(scaled.width()) / float(dpr)))),
            max(1, int(math.ceil(float(scaled.height()) / float(dpr)))),
        )
        _with_ui_scale_override(1.0, lambda: self._image_label.resize(logical_size))
        self._image_label.setStyleSheet(f"background-color: {surface_bg};")

    def _refresh_view(self):
        surface_bg = keyboard_shortcuts_surface_background(self._theme_mode)
        self.setStyleSheet(
            build_app_stylesheet(self._theme_mode)
            + "\n"
            + f"QDialog#KeyboardShortcutsDialogRoot {{ background-color: {surface_bg}; }}"
        )
        self._toggle_button.setText("Switch to Dark" if self._theme_mode == "light" else "Switch to Light")
        palette = build_theme_palette(self._theme_mode)
        self._scroll.viewport().setStyleSheet(f"background-color: {surface_bg};")
        self._scroll.setStyleSheet(f"background-color: {surface_bg};")
        self._lock_checkbox.setStyleSheet(
            "QCheckBox {"
            f" color: {palette['text']};"
            f" background-color: {surface_bg};"
            " padding: 2px 4px;"
            " }"
        )
        self._update_image_display()

    def _toggle_theme(self):
        self._theme_mode = "light" if self._theme_mode == "dark" else "dark"
        self._refresh_view()
        self._emit_preferences()

    def _on_lock_toggled(self, checked):
        self._lock_theme = bool(checked)
        self._refresh_view()
        self._emit_preferences()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_image_display()

    def showEvent(self, event):
        super().showEvent(event)
        QtCore.QTimer.singleShot(0, self._update_image_display)

ROCKET_TRIGGER_PHRASE = "it's rocket science!"

class RocketLaunchOverlay(QtWidgets.QWidget):
    """Transparent top-layer overlay for the rocket easter egg."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(QtCore.Qt.WA_NoSystemBackground, True)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setFocusPolicy(QtCore.Qt.NoFocus)

        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._on_tick)

        self._active = False
        self._duration_s = 9.0
        self._start_ts = 0.0
        self._snapshot = QtGui.QPixmap()
        self._confetti = []
        self._spray = []
        self._embers = []
        self._red_rain = []
        self._sparks = []
        self._stars = []
        self.hide()

    def is_active(self):
        return bool(self._active)

    def start_animation(self):
        host = self.parentWidget()
        if host is None:
            return

        self._duration_s = 9.0
        self._active = True
        self.hide()
        self.setGeometry(host.rect())
        QtWidgets.QApplication.processEvents(QtCore.QEventLoop.ExcludeUserInputEvents)
        self._snapshot = host.grab()
        self._build_scene()
        self._start_ts = time.monotonic()
        self.raise_()
        self.show()
        self._timer.start()
        self.update()

    def stop_animation(self):
        self._timer.stop()
        self._active = False
        self._snapshot = QtGui.QPixmap()
        self._confetti = []
        self._spray = []
        self._embers = []
        self._red_rain = []
        self._sparks = []
        self._stars = []
        self.hide()
        self.update()

    def _on_tick(self):
        if not self._active:
            return
        if (time.monotonic() - self._start_ts) >= self._duration_s:
            self.stop_animation()
            return
        self.update()

    def _build_scene(self):
        w = max(1, self.width())
        h = max(1, self.height())
        water_colors = [
            QtGui.QColor('#e8fcff'), QtGui.QColor('#ffffff'), QtGui.QColor('#b8f1ff'),
            QtGui.QColor('#7bdff6'), QtGui.QColor('#58c7ff'), QtGui.QColor('#1ea7ff')
        ]
        water_burst_colors = [
            QtGui.QColor('#ffffff'), QtGui.QColor('#dffbff'), QtGui.QColor('#b8f1ff'),
            QtGui.QColor('#7bdff6'), QtGui.QColor('#58c7ff'), QtGui.QColor('#1ea7ff')
        ]

        self._stars = [
            (random.uniform(0, w), random.uniform(0, h * 0.52), random.uniform(1.0, 3.2))
            for _ in range(max(26, int(w / 34)))
        ]

        # Secondary water burst (replaces confetti, but keeps the earlier feel)
        self._confetti = []
        confetti_count = max(520, int((w * h) / 1700.0))
        for _ in range(confetti_count):
            launch_side = random.choice(('left', 'right', 'center'))
            if launch_side == 'left':
                x0 = random.uniform(-0.05 * w, 0.16 * w)
                y0 = random.uniform(0.66 * h, 0.96 * h)
                vx = random.uniform(120.0, 340.0)
            elif launch_side == 'right':
                x0 = random.uniform(0.84 * w, 1.05 * w)
                y0 = random.uniform(0.66 * h, 0.96 * h)
                vx = random.uniform(-340.0, -120.0)
            else:
                x0 = random.uniform(0.30 * w, 0.70 * w)
                y0 = random.uniform(0.74 * h, 1.04 * h)
                vx = random.uniform(-150.0, 150.0)
            self._confetti.append({
                'x0': x0,
                'y0': y0,
                'vx': vx,
                'vy': random.uniform(-760.0, -280.0),
                'spin': random.uniform(-90.0, 90.0),
                'angle0': random.uniform(-12.0, 12.0),
                'size': random.uniform(4.0, 11.0),
                'delay': random.uniform(0.0, 0.92),
                'life': random.uniform(3.8, 5.8),
                'shape': random.choice(('drop', 'drop', 'drop', 'mist', 'stream')),
                'color': random.choice(water_burst_colors),
            })

        self._spray = []
        spray_count = max(1100, int((w * h) / 780.0))
        nozzles = [
            (0.24 * w, 0.96 * h, (-1.20, -0.30)),
            (0.50 * w, 1.02 * h, (-0.35, 0.35)),
            (0.76 * w, 0.96 * h, (0.30, 1.20)),
        ]
        for _ in range(spray_count):
            nx, ny, ang_range = random.choice(nozzles)
            ang = random.uniform(*ang_range)
            speed = random.uniform(240.0, 880.0)
            self._spray.append({
                'x0': nx + random.uniform(-0.05 * w, 0.05 * w),
                'y0': ny + random.uniform(-0.03 * h, 0.04 * h),
                'vx': math.sin(ang) * speed,
                'vy': -abs(math.cos(ang)) * speed * random.uniform(0.88, 1.08),
                'radius': random.uniform(1.8, 7.4),
                'stretch': random.uniform(1.8, 4.8),
                'delay': random.uniform(0.0, 1.35),
                'life': random.uniform(1.9, 3.8),
                'color': random.choice(water_colors),
            })

        self._embers = []
        self._red_rain = []
        rain_count = max(240, int((w * h) / 4300.0))
        for _ in range(rain_count):
            side = random.choice(('left', 'right', 'center'))
            if side == 'left':
                x0 = random.uniform(-0.04 * w, 0.18 * w)
                vx = random.uniform(90.0, 290.0)
            elif side == 'right':
                x0 = random.uniform(0.82 * w, 1.04 * w)
                vx = random.uniform(-290.0, -90.0)
            else:
                x0 = random.uniform(0.25 * w, 0.75 * w)
                vx = random.uniform(-140.0, 140.0)
            self._red_rain.append({
                'x0': x0,
                'y0': random.uniform(0.90 * h, 1.03 * h),
                'vx': vx,
                'vy': random.uniform(-540.0, -180.0),
                'spin': random.uniform(-220.0, 220.0),
                'angle0': random.uniform(-24.0, 24.0),
                'size': random.uniform(5.0, 12.0),
                'delay': random.uniform(0.82, 7.7),
                'life': random.uniform(2.0, 4.8),
                'shape': random.choice(('rect', 'rect', 'rect', 'tri', 'strip')),
                'color': random.choice([
                    QtGui.QColor(255, 40, 0), QtGui.QColor(230, 0, 0),
                    QtGui.QColor(188, 0, 0), QtGui.QColor(255, 78, 25)
                ]),
            })

        self._sparks = []
        spark_count = max(140, int((w * h) / 6200.0))
        for _ in range(spark_count):
            origin_x = random.uniform(0.40 * w, 0.60 * w)
            origin_y = random.uniform(0.95 * h, 1.02 * h)
            ang = random.uniform(-1.30, 1.30)
            speed = random.uniform(260.0, 760.0)
            self._sparks.append({
                'x0': origin_x,
                'y0': origin_y,
                'vx': math.sin(ang) * speed,
                'vy': -abs(math.cos(ang)) * speed * random.uniform(1.05, 1.35),
                'size': random.uniform(1.6, 4.4),
                'delay': random.uniform(0.78, 7.1),
                'life': random.uniform(0.55, 1.35),
                'color': random.choice([
                    QtGui.QColor(255, 245, 210), QtGui.QColor(255, 215, 120),
                    QtGui.QColor(255, 178, 72), QtGui.QColor(255, 120, 36)
                ]),
            })

    def _elapsed(self):
        if not self._active:
            return 0.0
        return max(0.0, time.monotonic() - self._start_ts)

    @staticmethod
    def _ease_in_out_cubic(x):
        x = max(0.0, min(1.0, float(x)))
        if x < 0.5:
            return 4.0 * x * x * x
        return 1.0 - ((-2.0 * x + 2.0) ** 3) / 2.0

    @staticmethod
    def _ease_out_quart(x):
        x = max(0.0, min(1.0, float(x)))
        return 1.0 - ((1.0 - x) ** 4)

    def _draw_confetti(self, painter, elapsed, w, h):
        gravity = 560.0
        for part in self._confetti:
            age = elapsed - part['delay']
            if age < 0.0 or age > part['life']:
                continue
            fade = max(0.0, 1.0 - (age / max(part['life'], 1e-6)))
            color = QtGui.QColor(part['color'])
            color.setAlpha(max(0, min(255, int(220 * fade))))
            painter.save()
            x = part['x0'] + part['vx'] * age
            y = part['y0'] + part['vy'] * age + 0.5 * gravity * age * age
            vel_y = part['vy'] + gravity * age
            angle = math.degrees(math.atan2(vel_y, part['vx'])) + 90.0
            painter.translate(x, y)
            painter.rotate(angle + part['angle0'] + part['spin'] * age)
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(color)
            size = part['size']
            if part['shape'] == 'stream':
                painter.drawRoundedRect(QtCore.QRectF(-size * 0.20, -size * 2.2, size * 0.40, size * 4.4), 2.2, 2.2)
            elif part['shape'] == 'mist':
                painter.drawEllipse(QtCore.QRectF(-size * 1.10, -size * 1.10, size * 2.2, size * 2.2))
            else:
                drop = QtGui.QPainterPath()
                drop.moveTo(0.0, -size * 1.25)
                drop.cubicTo(size * 0.86, -size * 0.62, size * 0.78, size * 0.40, 0.0, size * 1.12)
                drop.cubicTo(-size * 0.78, size * 0.40, -size * 0.86, -size * 0.62, 0.0, -size * 1.25)
                painter.drawPath(drop)
            painter.restore()

    def _draw_water_spray(self, painter, elapsed):
        gravity = 760.0
        for part in self._spray:
            age = elapsed - part['delay']
            if age < 0.0 or age > part['life']:
                continue
            fade = max(0.0, 1.0 - (age / max(part['life'], 1e-6)))
            color = QtGui.QColor(part['color'])
            color.setAlpha(max(0, min(255, int(215 * fade))))
            painter.save()
            x = part['x0'] + part['vx'] * age
            y = part['y0'] + part['vy'] * age + 0.5 * gravity * age * age
            angle = math.degrees(math.atan2(part['vy'] + gravity * age, part['vx'])) + 90.0
            painter.translate(x, y)
            painter.rotate(angle)
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(color)
            rx = part['radius']
            ry = rx * part['stretch']
            painter.drawEllipse(QtCore.QRectF(-rx, -ry * 0.5, rx * 2.0, ry))
            painter.restore()

    def _draw_stars(self, painter, elapsed, w, h, launch_progress):
        if launch_progress <= 0.0:
            return
        alpha = int(235 * launch_progress)
        for x, y, radius in self._stars:
            twinkle = 0.72 + 0.28 * math.sin(elapsed * 5.0 + x * 0.018)
            color = QtGui.QColor(252, 253, 255, int(alpha * twinkle))
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(QtCore.QPointF(x, y), radius * 1.12, radius * 1.12)

    def _draw_flames(self, painter, elapsed, w, h, launch_progress):
        flame_progress = max(0.0, min(1.0, (elapsed - 0.82) / 2.4))
        if flame_progress <= 0.0:
            return

        center_x = 0.5 * w
        base_y = h * 0.992
        plume_len = h * (0.52 + 1.02 * flame_progress + 0.22 * launch_progress)
        outer_w = w * (0.11 + 0.10 * flame_progress + 0.03 * launch_progress)
        inner_w = outer_w * 0.44
        sway = 4.0 * math.sin(elapsed * 3.2)

        outer = QtGui.QPainterPath()
        outer.moveTo(center_x, base_y)
        outer.cubicTo(
            center_x - outer_w * 0.85 + sway, base_y + plume_len * 0.18,
            center_x - outer_w * 1.18 + sway * 0.6, base_y + plume_len * 0.76,
            center_x, base_y + plume_len,
        )
        outer.cubicTo(
            center_x + outer_w * 1.18 - sway * 0.6, base_y + plume_len * 0.76,
            center_x + outer_w * 0.85 - sway, base_y + plume_len * 0.18,
            center_x, base_y,
        )

        inner = QtGui.QPainterPath()
        inner.moveTo(center_x, base_y)
        inner.cubicTo(
            center_x - inner_w * 0.78 + sway * 0.35, base_y + plume_len * 0.16,
            center_x - inner_w * 1.05 + sway * 0.2, base_y + plume_len * 0.60,
            center_x, base_y + plume_len * 0.88,
        )
        inner.cubicTo(
            center_x + inner_w * 1.05 - sway * 0.2, base_y + plume_len * 0.60,
            center_x + inner_w * 0.78 - sway * 0.35, base_y + plume_len * 0.16,
            center_x, base_y,
        )

        outer_grad = QtGui.QLinearGradient(center_x, base_y, center_x, base_y + plume_len)
        outer_grad.setColorAt(0.00, QtGui.QColor(255, 248, 236, 246))
        outer_grad.setColorAt(0.12, QtGui.QColor(255, 122, 68, 236))
        outer_grad.setColorAt(0.42, QtGui.QColor(242, 18, 0, 224))
        outer_grad.setColorAt(0.84, QtGui.QColor(150, 0, 0, 150))
        outer_grad.setColorAt(1.00, QtGui.QColor(110, 0, 0, 0))
        painter.fillPath(outer, outer_grad)

        inner_grad = QtGui.QLinearGradient(center_x, base_y, center_x, base_y + plume_len * 0.82)
        inner_grad.setColorAt(0.00, QtGui.QColor(255, 255, 255, 255))
        inner_grad.setColorAt(0.20, QtGui.QColor(255, 224, 196, 238))
        inner_grad.setColorAt(0.70, QtGui.QColor(255, 112, 78, 120))
        inner_grad.setColorAt(1.00, QtGui.QColor(220, 52, 40, 0))
        painter.fillPath(inner, inner_grad)

        glow = QtGui.QRadialGradient(QtCore.QPointF(center_x, base_y + plume_len * 0.36), max(w, h) * 0.92)
        glow.setColorAt(0.00, QtGui.QColor(255, 96, 58, 176))
        glow.setColorAt(0.26, QtGui.QColor(228, 18, 0, 118))
        glow.setColorAt(0.60, QtGui.QColor(150, 0, 0, 70))
        glow.setColorAt(1.00, QtGui.QColor(110, 0, 0, 0))
        painter.fillRect(self.rect(), glow)

    def _draw_rocket_screen(self, painter, elapsed, w, h, launch_progress):
        if self._snapshot.isNull():
            return

        ease = self._ease_in_out_cubic(launch_progress)
        draw_w = max(1, self.width())
        draw_h = max(1, self.height())
        full_exit_lift = draw_h + h * 0.10
        y = -ease * full_exit_lift

        painter.drawPixmap(0, int(round(y)), self._snapshot)

    def _draw_red_rain(self, painter, elapsed):
        gravity = 420.0
        for streak in self._red_rain:
            age = elapsed - streak['delay']
            if age < 0.0 or age > streak['life']:
                continue
            fade = 1.0 - (age / max(streak['life'], 1e-6))
            x = streak['x0'] + streak['vx'] * age
            y = streak['y0'] + streak['vy'] * age + 0.5 * gravity * age * age
            vel_y = streak['vy'] + gravity * age
            angle = math.degrees(math.atan2(vel_y, streak['vx'])) + 90.0
            color = QtGui.QColor(streak['color'])
            color.setAlpha(max(0, min(255, int(182 * fade))))
            painter.save()
            painter.translate(x, y)
            painter.rotate(angle + streak['angle0'] + streak['spin'] * age)
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(color)
            size = streak['size']
            if streak['shape'] == 'strip':
                painter.drawRoundedRect(QtCore.QRectF(-size * 0.22, -size * 2.4, size * 0.44, size * 4.8), 2.2, 2.2)
            elif streak['shape'] == 'tri':
                tri = QtGui.QPolygonF([
                    QtCore.QPointF(0.0, -size * 1.28),
                    QtCore.QPointF(size * 0.96, size * 0.92),
                    QtCore.QPointF(-size * 0.96, size * 0.92),
                ])
                painter.drawPolygon(tri)
            else:
                painter.drawRoundedRect(QtCore.QRectF(-size * 0.70, -size * 0.38, size * 1.40, size * 0.76), 1.8, 1.8)
            painter.restore()

    def _draw_sparks(self, painter, elapsed):
        gravity = 520.0
        for spark in self._sparks:
            age = elapsed - spark['delay']
            if age < 0.0 or age > spark['life']:
                continue
            fade = 1.0 - (age / max(spark['life'], 1e-6))
            x = spark['x0'] + spark['vx'] * age
            y = spark['y0'] + spark['vy'] * age + 0.5 * gravity * age * age
            vel_y = spark['vy'] + gravity * age
            angle = math.degrees(math.atan2(vel_y, spark['vx']))
            color = QtGui.QColor(spark['color'])
            color.setAlpha(max(0, min(255, int(235 * fade))))
            painter.save()
            painter.translate(x, y)
            painter.rotate(angle)
            painter.setPen(QtGui.QPen(color, max(1.0, spark['size'] * 0.55), QtCore.Qt.SolidLine, QtCore.Qt.RoundCap))
            painter.drawLine(QtCore.QPointF(-spark['size'] * 1.4, 0.0), QtCore.QPointF(spark['size'] * 1.9, 0.0))
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(QtGui.QColor(255, 250, 225, max(0, min(255, int(180 * fade)))))
            painter.drawEllipse(QtCore.QPointF(0.0, 0.0), spark['size'] * 0.52, spark['size'] * 0.52)
            painter.restore()

    def paintEvent(self, event):
        if not self._active:
            return

        elapsed = self._elapsed()
        progress = max(0.0, min(1.0, elapsed / max(self._duration_s, 1e-6)))
        w = float(max(1, self.width()))
        h = float(max(1, self.height()))

        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        painter.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)

        launch_progress = max(0.0, min(1.0, (elapsed - 0.70) / 8.0))
        space_alpha = int(210 * self._ease_in_out_cubic(launch_progress))
        painter.fillRect(self.rect(), QtGui.QColor(6, 9, 22, space_alpha))

        self._draw_stars(painter, elapsed, w, h, launch_progress)
        self._draw_water_spray(painter, elapsed)
        self._draw_flames(painter, elapsed, w, h, launch_progress)
        self._draw_rocket_screen(painter, elapsed, w, h, launch_progress)
        self._draw_confetti(painter, elapsed, w, h)
        self._draw_red_rain(painter, elapsed)
        self._draw_sparks(painter, elapsed)

        vignette_alpha = int(34 + 74 * progress)
        painter.fillRect(self.rect(), QtGui.QColor(0, 0, 0, vignette_alpha // 7))
        painter.end()


class AltMagnifierOverlay(QtWidgets.QWidget):
    """Input-transparent magnifier lens shown while Alt is held."""

    REFRESH_INTERVAL_MS = 12

    def __init__(self, parent=None):
        flags = QtCore.Qt.Tool | QtCore.Qt.FramelessWindowHint | QtCore.Qt.WindowStaysOnTopHint
        flags |= getattr(QtCore.Qt, "NoDropShadowWindowHint", 0)
        super().__init__(parent, flags)
        force_widget_ui_scale_100(self)

        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(QtCore.Qt.WA_NoSystemBackground, True)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(QtCore.Qt.NoFocus)
        self.setCursor(QtGui.QCursor(QtCore.Qt.BlankCursor))

        try:
            self.setWindowFlag(QtCore.Qt.WindowDoesNotAcceptFocus, True)
        except Exception:
            pass
        try:
            self.setWindowFlag(QtCore.Qt.WindowTransparentForInput, True)
        except Exception:
            pass

        self.zoom_factor = 3.0
        self.sample_size = 284
        self.lens_size = int(round(self.sample_size * self.zoom_factor))
        self.frame_margin = 10
        self.shadow_padding = 18
        self.shadow_offset = 10
        total = self.lens_size + 2 * (self.frame_margin + self.shadow_padding)
        self.resize(total, total)

        self._active = False
        self._pixmap = QtGui.QPixmap()
        self._last_source_widget = None

        self._timer = QtCore.QTimer(self)
        self._timer.setTimerType(QtCore.Qt.PreciseTimer)
        self._timer.setInterval(self.REFRESH_INTERVAL_MS)
        self._timer.timeout.connect(self.refresh_around_cursor)
        self.hide()

    def is_active(self):
        return bool(self._active)

    def start(self, source_widget=None):
        was_active = self._active
        self._active = True
        if source_widget is not None:
            self._last_source_widget = source_widget
        if not self._timer.isActive():
            self._timer.start()
        if was_active and not self._pixmap.isNull():
            self.track_cursor(source_widget)
        else:
            self.refresh_around_cursor(source_widget)

    def stop(self):
        self._active = False
        self._timer.stop()
        self._pixmap = QtGui.QPixmap()
        self.hide()
        self.update()

    def refresh_around_cursor(self, source_widget=None):
        if not self._active:
            return

        if source_widget is not None:
            self._last_source_widget = source_widget

        host = self._resolve_capture_host(self._last_source_widget)
        if host is None:
            self.hide()
            return

        global_pos = QtGui.QCursor.pos()
        self._move_centered_on_cursor(global_pos)
        sample = self._grab_sample(host, global_pos)
        if sample.isNull():
            self.hide()
            return

        self._pixmap = sample.scaled(
            self.lens_size,
            self.lens_size,
            QtCore.Qt.IgnoreAspectRatio,
            QtCore.Qt.FastTransformation,
        )
        if not self.isVisible():
            self.show()
        self.raise_()
        self.update()

    def track_cursor(self, source_widget=None):
        if not self._active:
            return
        if source_widget is not None:
            self._last_source_widget = source_widget
        if self._pixmap.isNull():
            self.refresh_around_cursor(source_widget)
            return
        self._move_centered_on_cursor(QtGui.QCursor.pos())
        if not self.isVisible():
            self.show()
        self.raise_()
        self.update()

    def _resolve_capture_host(self, source_widget=None):
        candidates = []
        if isinstance(source_widget, QtWidgets.QWidget):
            candidates.append(source_widget)

        popup = QtWidgets.QApplication.activePopupWidget()
        if isinstance(popup, QtWidgets.QWidget):
            candidates.append(popup)

        modal = QtWidgets.QApplication.activeModalWidget()
        if isinstance(modal, QtWidgets.QWidget):
            candidates.append(modal)

        active = QtWidgets.QApplication.activeWindow()
        if isinstance(active, QtWidgets.QWidget):
            candidates.append(active)

        widget_at_cursor = QtWidgets.QApplication.widgetAt(QtGui.QCursor.pos())
        if isinstance(widget_at_cursor, QtWidgets.QWidget):
            candidates.append(widget_at_cursor)

        for widget in candidates:
            if widget is None or widget is self or not widget.isVisible():
                continue
            host = widget.window() or widget
            if host is None or host is self or not host.isVisible():
                continue
            return host
        return None

    def _grab_sample(self, host, global_pos):
        half = self.sample_size // 2
        local_center = host.mapFromGlobal(global_pos)
        requested = QtCore.QRect(
            local_center.x() - half,
            local_center.y() - half,
            self.sample_size,
            self.sample_size,
        )

        sample = QtGui.QPixmap(self.sample_size, self.sample_size)
        sample.fill(QtCore.Qt.transparent)

        visible = requested.intersected(host.rect())
        if visible.isEmpty():
            return sample

        screen = None
        try:
            window_handle = host.windowHandle()
            if window_handle is not None:
                screen = window_handle.screen()
        except Exception:
            screen = None
        if screen is None:
            screen = QtGui.QGuiApplication.screenAt(global_pos)
        if screen is None:
            screen = QtGui.QGuiApplication.primaryScreen()
        if screen is None:
            return sample

        try:
            win_id = int(host.winId())
        except Exception:
            return sample

        grabbed = screen.grabWindow(
            win_id,
            int(visible.x()),
            int(visible.y()),
            int(visible.width()),
            int(visible.height()),
        )
        painter = QtGui.QPainter(sample)
        painter.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)
        painter.drawPixmap(visible.topLeft() - requested.topLeft(), grabbed)
        painter.end()
        return sample

    def _move_centered_on_cursor(self, global_pos):
        self.move(
            int(global_pos.x() - (self.width() / 2.0)),
            int(global_pos.y() - (self.height() / 2.0)),
        )

    def paintEvent(self, event):
        if self._pixmap.isNull():
            return

        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        painter.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)

        inset = self.shadow_padding + self.frame_margin
        lens_rect = QtCore.QRectF(
            inset,
            inset,
            self.lens_size,
            self.lens_size,
        )
        shadow_rect = lens_rect.translated(self.shadow_offset, self.shadow_offset)

        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(QtGui.QColor(0, 0, 0, 100))
        painter.drawEllipse(shadow_rect)

        clip = QtGui.QPainterPath()
        clip.addEllipse(lens_rect)
        painter.setClipPath(clip)
        painter.fillRect(lens_rect, QtGui.QColor(0, 0, 0, 180))
        painter.drawPixmap(lens_rect.toRect(), self._pixmap)
        painter.setClipping(False)

        painter.setPen(QtGui.QPen(QtGui.QColor(245, 245, 245, 235), 3.5))
        painter.setBrush(QtCore.Qt.NoBrush)
        painter.drawEllipse(lens_rect)

        center = lens_rect.center()
        cross_arm = 36.0
        painter.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 235), 3.0))
        painter.drawLine(
            QtCore.QPointF(center.x() - cross_arm, center.y() - cross_arm),
            QtCore.QPointF(center.x() + cross_arm, center.y() + cross_arm),
        )
        painter.drawLine(
            QtCore.QPointF(center.x() - cross_arm, center.y() + cross_arm),
            QtCore.QPointF(center.x() + cross_arm, center.y() - cross_arm),
        )
        painter.end()


class ScrollableMenuProxyStyle(QtWidgets.QProxyStyle):
    """Keep popup menus reachable and give menu rows stable logical sizing."""

    _MENU_ROW_PAD_Y = 0
    _MENU_SEPARATOR_H = 4
    _MENU_TEXT_PAD_X = 14

    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QtWidgets.QStyle.SH_Menu_Scrollable:
            return 1
        return super().styleHint(hint, option, widget, returnData)

    def sizeFromContents(self, contents_type, option, size, widget=None):
        result = super().sizeFromContents(contents_type, option, size, widget)
        try:
            if contents_type == QtWidgets.QStyle.CT_MenuItem:
                font = widget.font() if isinstance(widget, QtWidgets.QWidget) else QtWidgets.QApplication.font()
                metrics = QtGui.QFontMetrics(font)
                separator_type = getattr(QtWidgets.QStyleOptionMenuItem, "Separator", None)
                is_separator = (
                    separator_type is not None
                    and getattr(option, "menuItemType", None) == separator_type
                )
                if is_separator:
                    row_h = self._MENU_SEPARATOR_H
                else:
                    row_h = int(metrics.height()) + (self._MENU_ROW_PAD_Y * 2)
                result.setHeight(max(1, int(row_h)))
                text = str(getattr(option, "text", "") or "").replace("&", "")
                if text:
                    shortcut = ""
                    if "\t" in text:
                        text, shortcut = text.split("\t", 1)
                    try:
                        text_w = int(metrics.horizontalAdvance(text))
                    except Exception:
                        text_w = len(text) * max(6, int(metrics.averageCharWidth()))
                    try:
                        shortcut_w = int(metrics.horizontalAdvance(shortcut)) if shortcut else 0
                    except Exception:
                        shortcut_w = len(shortcut) * max(6, int(metrics.averageCharWidth()))
                    shortcut_gap = max(8, int(metrics.averageCharWidth() * 2)) if shortcut_w else 0
                    try:
                        arrow_w = int(self.pixelMetric(QtWidgets.QStyle.PM_MenuButtonIndicator, option, widget))
                    except Exception:
                        arrow_w = max(10, int(metrics.height()))
                    try:
                        icon_w = int(self.pixelMetric(QtWidgets.QStyle.PM_SmallIconSize, option, widget))
                    except Exception:
                        icon_w = max(12, int(metrics.height()))
                    try:
                        check_w = int(self.pixelMetric(QtWidgets.QStyle.PM_IndicatorWidth, option, widget))
                    except Exception:
                        check_w = max(10, int(metrics.height() * 0.75))
                    extra_w = self._MENU_TEXT_PAD_X + arrow_w + icon_w + check_w
                    result.setWidth(max(1, text_w + shortcut_gap + shortcut_w + extra_w))
            elif contents_type == QtWidgets.QStyle.CT_MenuBarItem:
                font = widget.font() if isinstance(widget, QtWidgets.QWidget) else QtWidgets.QApplication.font()
                row_h = QtGui.QFontMetrics(font).height() + 4
                result.setHeight(max(int(result.height()), int(row_h), 20))
                result.setWidth(int(result.width()) + 3)
        except Exception:
            pass
        return result


class AppWideScreenScrollController(QtCore.QObject):
    """Keeps menus/dialogs reachable on smaller monitors without changing the main window."""

    _WRAPPED_PROP = "_geoviewer_screen_scroll_wrapped"
    _PENDING_PROP = "_geoviewer_screen_scroll_pending"
    _LIMITING_PROP = "_geoviewer_screen_scroll_limiting"
    _NO_SCROLL_PROP = GEOVIEWER_NO_SCREEN_SCROLL_PROPERTY
    _CENTER_ON_SHOW_PROP = GEOVIEWER_CENTER_ON_SHOW_PROPERTY
    _CENTERED_ON_SHOW_PROP = "_geoviewer_centered_on_show"
    _MAX_QT_SIZE = 16777215

    def __init__(self, parent=None):
        super().__init__(parent)
        self._screen_margin = 16

    def eventFilter(self, obj, event):
        try:
            etype = event.type()
            if etype not in (
                QtCore.QEvent.Show,
                QtCore.QEvent.LayoutRequest,
                QtCore.QEvent.Wheel,
            ):
                return False

            if etype == QtCore.QEvent.Wheel:
                return self._handle_wrapped_dialog_wheel(obj, event)
            if isinstance(obj, QtWidgets.QDialog):
                self._queue_dialog_fit(obj)
            elif etype == QtCore.QEvent.Show and (
                isinstance(obj, QtWidgets.QMenu) or self._is_popup_container(obj)
            ):
                self._queue_popup_fit(obj)
        except Exception:
            pass
        return False

    def _is_popup_container(self, widget):
        if not isinstance(widget, QtWidgets.QWidget):
            return False
        if isinstance(widget, (QtWidgets.QDialog, QtWidgets.QMainWindow)):
            return False
        try:
            if not (widget.windowFlags() & QtCore.Qt.Popup):
                return False
        except Exception:
            return False
        return bool(widget.findChild(QtWidgets.QAbstractItemView))

    def _queue_dialog_fit(self, dlg):
        if not isinstance(dlg, QtWidgets.QDialog):
            return
        try:
            if bool(dlg.property(self._NO_SCROLL_PROP)):
                return
            if bool(dlg.property(self._PENDING_PROP)) or bool(dlg.property(self._LIMITING_PROP)):
                return
            dlg.setProperty(self._PENDING_PROP, True)
            if dlg.isVisible():
                self._fit_dialog_to_screen(dlg)
                QtCore.QTimer.singleShot(0, lambda d=dlg: self._fit_dialog_to_screen(d))
            else:
                QtCore.QTimer.singleShot(0, lambda d=dlg: self._fit_dialog_to_screen(d))
        except Exception:
            pass

    def _queue_popup_fit(self, popup):
        if not isinstance(popup, QtWidgets.QWidget):
            return
        try:
            if bool(popup.property(self._PENDING_PROP)) or bool(popup.property(self._LIMITING_PROP)):
                return
            popup.setProperty(self._PENDING_PROP, True)
            if popup.isVisible():
                self._fit_popup_to_screen(popup)
                QtCore.QTimer.singleShot(0, lambda p=popup: self._fit_popup_to_screen(p))
            else:
                QtCore.QTimer.singleShot(0, lambda p=popup: self._fit_popup_to_screen(p))
        except Exception:
            pass

    def _available_geometry(self, widget):
        screen = None
        try:
            screen = widget.screen()
        except Exception:
            screen = None
        if screen is None:
            try:
                handle = widget.windowHandle()
                if handle is not None:
                    screen = handle.screen()
            except Exception:
                screen = None
        if screen is None:
            try:
                center = widget.frameGeometry().center()
                screen = QtGui.QGuiApplication.screenAt(center)
            except Exception:
                screen = None
        if screen is None:
            try:
                screen = QtGui.QGuiApplication.screenAt(QtGui.QCursor.pos())
            except Exception:
                screen = None
        if screen is None:
            screen = QtGui.QGuiApplication.primaryScreen()
        if screen is not None:
            return screen.availableGeometry()
        return QtCore.QRect(0, 0, 1280, 720)

    def _screen_limits(self, widget):
        available = self._available_geometry(widget)
        margin = max(0, int(self._screen_margin))
        max_w = max(320, int(available.width()) - margin * 2)
        max_h = max(
            240,
            min(
                int(available.height()) - margin * 2,
                int(round(float(available.height()) * 0.90)),
            ),
        )
        return available, max_w, max_h

    def _popup_screen_limits(self, widget):
        available = self._available_geometry(widget)
        max_w = max(1, int(round(float(available.width()) * 0.90)))
        max_h = max(1, int(round(float(available.height()) * 0.90)))
        return available, max_w, max_h

    def _menu_action_content_size(self, menu):
        if not isinstance(menu, QtWidgets.QMenu):
            return None
        try:
            menu.ensurePolished()
        except Exception:
            pass
        try:
            font = menu.font()
        except Exception:
            font = QtWidgets.QApplication.font()
        metrics = QtGui.QFontMetrics(font)
        try:
            frame_w = int(menu.style().pixelMetric(QtWidgets.QStyle.PM_MenuPanelWidth, None, menu))
        except Exception:
            frame_w = 1
        try:
            arrow_w = int(menu.style().pixelMetric(QtWidgets.QStyle.PM_MenuButtonIndicator, None, menu))
        except Exception:
            arrow_w = max(10, metrics.height())
        try:
            icon_w = int(menu.style().pixelMetric(QtWidgets.QStyle.PM_SmallIconSize, None, menu))
        except Exception:
            icon_w = max(12, metrics.height())

        try:
            check_w = int(menu.style().pixelMetric(QtWidgets.QStyle.PM_IndicatorWidth, None, menu))
        except Exception:
            check_w = max(10, int(metrics.height() * 0.75))

        row_pad_y = 0
        sep_h = 4
        row_h = int(metrics.height()) + row_pad_y * 2
        width_extra = 14 + arrow_w + icon_w + check_w + frame_w * 2
        height = frame_w * 2
        width = 0
        for action in list(menu.actions() or []):
            try:
                if not action.isVisible():
                    continue
            except Exception:
                pass
            if action.isSeparator():
                height += sep_h
                continue
            text = str(action.text() or "").replace("&", "")
            shortcut = ""
            if "\t" in text:
                text, shortcut = text.split("\t", 1)
            try:
                action_shortcut = action.shortcut()
                action_shortcut_text = action_shortcut.toString(QtGui.QKeySequence.NativeText)
            except Exception:
                action_shortcut_text = ""
            if action_shortcut_text:
                shortcut = action_shortcut_text
            try:
                text_w = int(metrics.horizontalAdvance(text))
            except Exception:
                text_w = len(text) * max(6, int(metrics.averageCharWidth()))
            try:
                shortcut_w = int(metrics.horizontalAdvance(shortcut)) if shortcut else 0
            except Exception:
                shortcut_w = len(shortcut) * max(6, int(metrics.averageCharWidth()))
            shortcut_gap = max(8, int(metrics.averageCharWidth() * 2)) if shortcut_w else 0
            action_extra = width_extra
            try:
                has_submenu = action.menu() is not None
            except Exception:
                has_submenu = False
            if not has_submenu:
                action_extra -= max(0, arrow_w - 4)
            width = max(width, text_w + shortcut_gap + shortcut_w + action_extra)
            height += row_h
        return QtCore.QSize(max(1, width), max(1, height))

    def _combo_matches_choices(self, combo, choices, normalizer, fallback):
        if not isinstance(combo, QtWidgets.QComboBox):
            return False
        try:
            expected = [normalizer(value, fallback) for _label, value in choices]
            if combo.count() != len(expected):
                return False
            actual = [
                normalizer(combo.itemData(idx), fallback)
                for idx in range(combo.count())
            ]
            return actual == expected
        except Exception:
            return False

    def _dialog_needs_scroll(self, dlg, max_w, max_h):
        try:
            size = dlg.size()
            hint = dlg.sizeHint()
            minimum_hint = dlg.minimumSizeHint()
        except Exception:
            return False
        needed_w = max(int(size.width()), int(hint.width()), int(minimum_hint.width()))
        needed_h = max(int(size.height()), int(hint.height()), int(minimum_hint.height()))
        return needed_w > max_w or needed_h > max_h

    def _resize_unscaled(self, widget, width, height):
        try:
            _with_ui_scale_override(
                1.0,
                lambda: widget.resize(max(1, int(width)), max(1, int(height))),
            )
        except Exception:
            try:
                widget.setProperty("_geoviewer_skip_scale_next_resize", True)
                widget.resize(max(1, int(width)), max(1, int(height)))
            except Exception:
                pass

    def _combo_is_scale_selector(self, combo):
        return (
            self._combo_matches_choices(combo, UI_SCALE_CHOICES, normalize_persisted_ui_scale, "auto")
            or self._combo_matches_choices(combo, MAIN_PANEL_TEXT_SCALE_CHOICES, normalize_main_panel_text_scale, 1.0)
        )

    def _item_view_content_size(self, view, max_w=None, max_h=None):
        if not isinstance(view, QtWidgets.QAbstractItemView):
            return None
        try:
            model = view.model()
            root = view.rootIndex()
            rows = int(model.rowCount(root)) if model is not None else 0
        except Exception:
            rows = 0
        if rows <= 0:
            return None

        try:
            frame = int(view.frameWidth()) * 2
        except Exception:
            frame = 2
        try:
            metrics_h = QtGui.QFontMetrics(view.font()).height()
        except Exception:
            metrics_h = 18
        fallback_row_h = max(18, int(metrics_h) + 2)

        total_h = frame
        sample_rows = min(rows, 200)
        sampled_heights = []
        for row in range(sample_rows):
            try:
                row_h = int(view.sizeHintForRow(row))
            except Exception:
                row_h = -1
            if row_h <= 0:
                row_h = fallback_row_h
            sampled_heights.append(row_h)
            total_h += row_h
        if rows > sample_rows:
            total_h += (rows - sample_rows) * max(sampled_heights or [fallback_row_h])

        try:
            content_w = int(view.sizeHintForColumn(0)) + frame + 12
        except Exception:
            content_w = int(view.sizeHint().width())
        try:
            min_w = int(view.minimumSizeHint().width())
        except Exception:
            min_w = 1
        content_w = max(1, content_w, min_w)

        if max_h is not None and total_h > int(max_h):
            try:
                content_w += int(view.style().pixelMetric(QtWidgets.QStyle.PM_ScrollBarExtent, None, view))
            except Exception:
                content_w += 18
        if max_w is not None:
            content_w = min(int(max_w), content_w)
        return QtCore.QSize(max(1, content_w), max(1, int(total_h)))

    def _wrap_dialog_layout(self, dlg):
        if bool(dlg.property(self._WRAPPED_PROP)):
            return True

        layout = dlg.layout()
        if not isinstance(layout, QtWidgets.QBoxLayout):
            return False

        margins = layout.contentsMargins()
        spacing = layout.spacing()
        direction = layout.direction()
        content = QtWidgets.QWidget()
        content.setObjectName("GeoViewerScreenScrollContent")
        content_layout = QtWidgets.QBoxLayout(direction, content)
        content_layout.setContentsMargins(
            margins.left(), margins.top(), margins.right(), margins.bottom()
        )
        if spacing >= 0:
            content_layout.setSpacing(spacing)
        try:
            content_layout.setSizeConstraint(layout.sizeConstraint())
        except Exception:
            pass

        while layout.count():
            try:
                stretch = int(layout.stretch(0))
            except Exception:
                stretch = 0
            item = layout.takeAt(0)
            if item is None:
                continue
            try:
                alignment = item.alignment()
            except Exception:
                alignment = QtCore.Qt.Alignment()
            widget = item.widget()
            child_layout = item.layout()
            if widget is not None:
                content_layout.addWidget(widget, stretch, alignment)
            elif child_layout is not None:
                content_layout.addLayout(child_layout, stretch)
            else:
                content_layout.addItem(item)
            if stretch and widget is None and child_layout is None:
                try:
                    content_layout.setStretch(content_layout.count() - 1, stretch)
                except Exception:
                    pass

        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        scroll = QtWidgets.QScrollArea(dlg)
        scroll.setObjectName("GeoViewerDialogScreenScrollArea")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        scroll.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        scroll.setStyleSheet(
            "QScrollArea#GeoViewerDialogScreenScrollArea { border: 0px; background: transparent; }"
            "QScrollArea#GeoViewerDialogScreenScrollArea > QWidget > QWidget { background: transparent; }"
        )
        scroll.setWidget(content)
        layout.addWidget(scroll)
        try:
            layout.setStretch(layout.count() - 1, 1)
        except Exception:
            pass

        dlg.setProperty(self._WRAPPED_PROP, True)
        dlg._geoviewer_screen_scroll_area = scroll
        dlg._geoviewer_screen_scroll_content = content
        return True

    def _refresh_wrapped_content_minimum(self, dlg):
        content = getattr(dlg, "_geoviewer_screen_scroll_content", None)
        if not isinstance(content, QtWidgets.QWidget):
            return
        try:
            hint = content.sizeHint()
            minimum = content.minimumSizeHint()
            natural_h = max(0, int(hint.height()), int(minimum.height()))
            content.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
            content.setMinimumSize(
                0,
                natural_h,
            )
            content.setMaximumHeight(natural_h)
        except Exception:
            pass

    def _limit_combo_popups(self, widget, max_h):
        row_height = 30
        try:
            row_height = max(row_height, QtGui.QFontMetrics(widget.font()).height() + 12)
        except Exception:
            pass
        for combo in widget.findChildren(QtWidgets.QComboBox):
            try:
                count = max(0, int(combo.count()))
                rows_fit = max(1, int(max(120, max_h - 80) / max(1, row_height)))
                if self._combo_is_scale_selector(combo):
                    visible = max(1, count)
                elif count > 0:
                    visible = max(1, min(count, rows_fit))
                else:
                    visible = max(5, min(30, rows_fit))
                combo.setMaxVisibleItems(visible)
                view = combo.view()
                if view is not None:
                    view.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
                    view.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
            except Exception:
                pass

    def _release_fixed_size_if_needed(self, widget):
        try:
            widget.setMinimumSize(0, 0)
            widget.setMaximumSize(self._MAX_QT_SIZE, self._MAX_QT_SIZE)
        except Exception:
            pass

    def _fit_dialog_to_screen(self, dlg):
        try:
            dlg.setProperty(self._PENDING_PROP, False)
            if not isinstance(dlg, QtWidgets.QDialog) or not dlg.isVisible():
                return
            if bool(dlg.property(self._NO_SCROLL_PROP)):
                return

            available, max_w, max_h = self._screen_limits(dlg)
            needs_scroll = self._dialog_needs_scroll(dlg, max_w, max_h)
            try:
                original_size = dlg.size()
                original_hint = dlg.sizeHint()
                original_minimum = dlg.minimumSizeHint()
                desired_w = max(
                    int(original_size.width()),
                    int(original_hint.width()),
                    int(original_minimum.width()),
                )
                desired_h = max(
                    int(original_size.height()),
                    int(original_hint.height()),
                    int(original_minimum.height()),
                )
            except Exception:
                original_size = dlg.size()
                desired_w = int(original_size.width())
                desired_h = int(original_size.height())
            if needs_scroll:
                self._wrap_dialog_layout(dlg)

            if bool(dlg.property(self._WRAPPED_PROP)):
                self._refresh_wrapped_content_minimum(dlg)
                self._release_fixed_size_if_needed(dlg)
                dlg.setMaximumSize(max_w, max_h)
                desired_h = max_h

            self._limit_combo_popups(dlg, max_h)

            size = dlg.size()
            target_w = min(max(1, int(size.width()), desired_w), max_w)
            target_h = min(max(1, int(size.height()), desired_h), max_h)
            if int(size.width()) != target_w or int(size.height()) != target_h:
                dlg.setProperty(self._LIMITING_PROP, True)
                try:
                    self._resize_unscaled(dlg, target_w, target_h)
                finally:
                    dlg.setProperty(self._LIMITING_PROP, False)

            if (
                bool(dlg.property(self._CENTER_ON_SHOW_PROP))
                and not bool(dlg.property(self._CENTERED_ON_SHOW_PROP))
            ):
                center_top_level_widget_on_available_screen(dlg, available)
                dlg.setProperty(self._CENTERED_ON_SHOW_PROP, True)
            else:
                self._clamp_widget_to_available(dlg, available)
        except Exception:
            try:
                dlg.setProperty(self._LIMITING_PROP, False)
            except Exception:
                pass

    def _fit_popup_to_screen(self, popup):
        try:
            popup.setProperty(self._PENDING_PROP, False)
            if not isinstance(popup, QtWidgets.QWidget) or not popup.isVisible():
                return

            available, max_w, max_h = self._popup_screen_limits(popup)
            is_menu_popup = isinstance(popup, QtWidgets.QMenu)
            current_popup_w = max(1, int(popup.width()), int(popup.frameGeometry().width()))
            popup.setProperty(self._LIMITING_PROP, True)
            try:
                self._release_fixed_size_if_needed(popup)
                hint = popup.sizeHint()
                minimum_hint = popup.minimumSizeHint()
                content_w = max(1, int(hint.width()), int(minimum_hint.width()))
                content_h = max(1, int(hint.height()), int(minimum_hint.height()))
                menu_size = self._menu_action_content_size(popup)
                if menu_size is not None:
                    content_w = max(1, int(menu_size.width()))
                    content_h = max(1, int(menu_size.height()))
                view_sizes = []
                for view in popup.findChildren(QtWidgets.QAbstractItemView):
                    view_size = self._item_view_content_size(view, max_w=max_w, max_h=max_h)
                    if view_size is not None:
                        view_sizes.append((view, view_size))
                if self._is_popup_container(popup) and view_sizes:
                    content_w = max(int(size.width()) for _view, size in view_sizes)
                    content_h = max(int(size.height()) for _view, size in view_sizes)
                target_h = min(content_h, max_h)
                if is_menu_popup:
                    target_w = min(max(1, content_w), max_w)
                else:
                    target_w = min(max(content_w, current_popup_w), max_w)
                if content_h > max_h:
                    try:
                        target_w = min(
                            max_w,
                            target_w + popup.style().pixelMetric(QtWidgets.QStyle.PM_ScrollBarExtent, None, popup),
                        )
                    except Exception:
                        pass
                popup.setMaximumSize(max_w, max_h)
                _with_ui_scale_override(1.0, lambda: popup.setMinimumWidth(target_w))
                self._resize_unscaled(popup, target_w, target_h)
            finally:
                popup.setProperty(self._LIMITING_PROP, False)

            popup_target_w = max(1, int(popup.width()))
            for view in popup.findChildren(QtWidgets.QAbstractItemView):
                try:
                    view.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
                    view.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
                    view.setMaximumSize(max_w, max_h)
                    if view.isVisible():
                        current_view_w = max(1, int(view.width()))
                        item_size = self._item_view_content_size(view, max_w=max_w, max_h=max_h)
                        if item_size is not None:
                            target_w = min(max_w, max(current_view_w, popup_target_w, int(item_size.width())))
                            target_h = min(max_h, max(1, int(item_size.height())))
                        else:
                            hint = view.sizeHint()
                            target_w = min(
                                max_w,
                                max(
                                    current_view_w,
                                    popup_target_w,
                                    int(hint.width()),
                                    int(view.minimumSizeHint().width()),
                                ),
                            )
                            target_h = min(max_h, max(1, int(hint.height()), int(view.minimumSizeHint().height())))
                            if int(hint.height()) > max_h:
                                target_h = max_h
                        _with_ui_scale_override(1.0, lambda w=target_w: view.setMinimumWidth(w))
                        self._resize_unscaled(view, target_w, target_h)
                except Exception:
                    pass

            self._clamp_widget_to_available(popup, available)
        except Exception:
            try:
                popup.setProperty(self._LIMITING_PROP, False)
            except Exception:
                pass

    def _wrapped_dialog_for_obj(self, obj):
        cur = obj
        seen = set()
        while isinstance(cur, QtCore.QObject) and id(cur) not in seen:
            seen.add(id(cur))
            if (
                isinstance(cur, QtWidgets.QDialog)
                and bool(cur.property(self._WRAPPED_PROP))
            ):
                return cur
            parent = cur.parent()
            cur = parent if isinstance(parent, QtCore.QObject) else None

        if isinstance(obj, QtWidgets.QWidget):
            window = obj.window()
            if (
                isinstance(window, QtWidgets.QDialog)
                and bool(window.property(self._WRAPPED_PROP))
            ):
                return window
        return None

    def _wheel_should_stay_with_inner_scroll_area(self, obj, outer_scroll):
        cur = obj if isinstance(obj, QtWidgets.QWidget) else None
        seen = set()
        while isinstance(cur, QtWidgets.QWidget) and id(cur) not in seen:
            seen.add(id(cur))
            if isinstance(cur, QtWidgets.QAbstractScrollArea) and cur is not outer_scroll:
                try:
                    bar = cur.verticalScrollBar()
                    if bar is not None and bar.maximum() > bar.minimum():
                        return True
                except Exception:
                    pass
            cur = cur.parentWidget()
        return False

    def _handle_wrapped_dialog_wheel(self, obj, event):
        dlg = self._wrapped_dialog_for_obj(obj)
        if dlg is None:
            return False

        scroll = getattr(dlg, "_geoviewer_screen_scroll_area", None)
        if not isinstance(scroll, QtWidgets.QScrollArea):
            return False
        if self._wheel_should_stay_with_inner_scroll_area(obj, scroll):
            return False

        bar = scroll.verticalScrollBar()
        if bar is None or bar.maximum() <= bar.minimum():
            return False

        delta = 0
        try:
            pixel_delta = event.pixelDelta()
            if not pixel_delta.isNull():
                delta = int(pixel_delta.y())
        except Exception:
            delta = 0
        if not delta:
            try:
                angle_delta = event.angleDelta()
                delta = int((angle_delta.y() / 120.0) * max(24, bar.singleStep() * 3))
            except Exception:
                delta = 0
        if not delta:
            return False

        old_value = bar.value()
        new_value = max(bar.minimum(), min(bar.maximum(), old_value - delta))
        if new_value == old_value:
            return False
        bar.setValue(new_value)
        event.accept()
        return True

    def _clamp_widget_to_available(self, widget, available):
        try:
            frame = widget.frameGeometry()
            width = max(1, int(frame.width()))
            height = max(1, int(frame.height()))
            min_x = int(available.left())
            min_y = int(available.top())
            max_x = int(available.left() + max(0, available.width() - width))
            max_y = int(available.top() + max(0, available.height() - height))
            x = min(max(int(frame.x()), min_x), max_x)
            y = min(max(int(frame.y()), min_y), max_y)
            if x != int(frame.x()) or y != int(frame.y()):
                widget.move(x, y)
        except Exception:
            pass


class AppWideAltMagnifierController(QtCore.QObject):
    """Tracks Alt globally for the QApplication and updates the overlay."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._alt_is_down = False
        self._last_widget = None
        self._overlay = AltMagnifierOverlay()
        self._cursor_hidden = False

    def cleanup(self):
        try:
            self._overlay.stop()
            self._overlay.close()
        except Exception:
            pass
        if self._cursor_hidden:
            try:
                QtWidgets.QApplication.restoreOverrideCursor()
            except Exception:
                pass
            self._cursor_hidden = False

    def _event_widget(self, obj):
        cur = obj
        seen = set()
        while isinstance(cur, QtCore.QObject) and id(cur) not in seen:
            seen.add(id(cur))
            if isinstance(cur, QtWidgets.QWidget):
                return cur
            parent = cur.parent()
            cur = parent if isinstance(parent, QtCore.QObject) else None
        return None

    def _image_comment_dialog_active(self, widget=None):
        candidates = [widget]
        try:
            candidates.append(QtWidgets.QApplication.activeModalWidget())
        except Exception:
            pass
        try:
            candidates.append(QtWidgets.QApplication.focusWidget())
        except Exception:
            pass

        for candidate in candidates:
            cur = candidate
            seen = set()
            while isinstance(cur, QtCore.QObject) and id(cur) not in seen:
                seen.add(id(cur))
                try:
                    if bool(cur.property("geoviewer_image_comment_dialog")):
                        return True
                except Exception:
                    pass
                parent = cur.parent()
                cur = parent if isinstance(parent, QtCore.QObject) else None
        return False

    def _stop_for_comment_entry(self):
        self._alt_is_down = False
        try:
            self._overlay.stop()
        except Exception:
            pass
        if self._cursor_hidden:
            try:
                QtWidgets.QApplication.restoreOverrideCursor()
            except Exception:
                pass
            self._cursor_hidden = False

    def _sync_overlay(self, widget=None, force_active=None, reposition_only=False):
        if isinstance(widget, QtWidgets.QWidget) and widget is not self._overlay:
            self._last_widget = widget

        alt_down = bool(
            self._alt_is_down
            or (QtWidgets.QApplication.keyboardModifiers() & QtCore.Qt.AltModifier)
        )
        if force_active is not None:
            alt_down = bool(force_active)

        if alt_down:
            if not self._cursor_hidden:
                try:
                    QtWidgets.QApplication.setOverrideCursor(QtGui.QCursor(QtCore.Qt.BlankCursor))
                    self._cursor_hidden = True
                except Exception:
                    pass
            if reposition_only and self._overlay.is_active():
                self._overlay.track_cursor(self._last_widget)
            else:
                self._overlay.start(self._last_widget)
        else:
            self._overlay.stop()
            if self._cursor_hidden:
                try:
                    QtWidgets.QApplication.restoreOverrideCursor()
                except Exception:
                    pass
                self._cursor_hidden = False

    def eventFilter(self, obj, event):
        try:
            widget = self._event_widget(obj)
            if widget is self._overlay:
                return False
            etype = event.type()

            if self._image_comment_dialog_active(widget):
                self._stop_for_comment_entry()
                return False

            if etype in (QtCore.QEvent.MouseMove, QtCore.QEvent.HoverMove):
                self._sync_overlay(widget, reposition_only=True)

            elif etype in (
                QtCore.QEvent.MouseButtonPress,
                QtCore.QEvent.MouseButtonRelease,
                QtCore.QEvent.Wheel,
                QtCore.QEvent.Show,
                QtCore.QEvent.Move,
                QtCore.QEvent.Enter,
                QtCore.QEvent.WindowActivate,
            ):
                self._sync_overlay(widget)

            if etype == QtCore.QEvent.ShortcutOverride:
                if event.key() == QtCore.Qt.Key_Alt and not event.isAutoRepeat():
                    self._alt_is_down = True
                    self._sync_overlay(widget, force_active=True)
                    event.accept()
                    return True

            elif etype == QtCore.QEvent.KeyPress:
                if event.key() == QtCore.Qt.Key_Alt and not event.isAutoRepeat():
                    self._alt_is_down = True
                    self._sync_overlay(widget, force_active=True)
                    return True

            elif etype == QtCore.QEvent.KeyRelease:
                if event.key() == QtCore.Qt.Key_Alt and not event.isAutoRepeat():
                    self._alt_is_down = False
                    self._sync_overlay(widget, force_active=False)
                    return True

            elif etype in (QtCore.QEvent.Hide, QtCore.QEvent.Close):
                if widget is not None and widget is self._last_widget:
                    self._last_widget = None
                    if self._alt_is_down:
                        self._sync_overlay(force_active=True)

            elif etype == QtCore.QEvent.ApplicationDeactivate:
                self._alt_is_down = False
                self._sync_overlay(widget, force_active=False)
        except Exception:
            pass
        return False


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

    def _read_for_display(self, src, win=None, max_dim=1100, resampling=None):
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

        read_resampling = visual_resampling_rasterio(
            resampling if resampling is not None else getattr(self, "thermal_visual_resampling", "nearest")
        )
        arr = src.read(
            1,
            window=win,
            out_shape=(out_h, out_w),          # (H, W) for a single band
            resampling=read_resampling,
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

    def _theme_palette(self, mode=None):
        mode = (mode or getattr(self, "theme_mode", "dark")).lower()
        palettes = {
            "dark": {
                "window_bg": "#000000",
                "figure_bg": "#000000",
                "axes_bg": "#000000",
                "text": "#D3D3D3",
                "title": "#D3D3D3",
                "empty": "#555555",
            },
            "light": {
                "window_bg": "#F4F4F4",
                "figure_bg": "#F4F4F4",
                "axes_bg": "#FFFFFF",
                "text": "#202020",
                "title": "#202020",
                "empty": "#808080",
            },
        }
        return palettes["light" if mode == "light" else "dark"]

    def _panel_facecolor(self):
        return str(getattr(self, "theme", {}).get("axes_bg", "black"))

    def _nan_bad_color(self):
        if bool(getattr(self, "use_theme_nan_color", True)):
            return self._panel_facecolor()
        return nan_override_bad_color(
            getattr(self, "nan_color", self._panel_facecolor()),
            self._panel_facecolor(),
        )

    def _basemap_cmap(self):
        cmap_name = normalize_basemap_cmap(getattr(self, "basemap_cmap", "gray"), "gray")
        try:
            base = plt.get_cmap(cmap_name)
        except Exception:
            base = plt.get_cmap("gray")
        if normalize_basemap_color_scaling(getattr(self, "basemap_color_scaling", "normal")) == "inverted":
            try:
                base = base.reversed()
            except Exception:
                pass
        try:
            return base.with_extremes(bad=(0.0, 0.0, 0.0, 0.0))
        except Exception:
            lut = base(np.linspace(0, 1, getattr(base, "N", 256)))
            cmap = mcolors.ListedColormap(lut, name=f"{cmap_name}_basemap")
            try:
                cmap.set_bad(color=(0.0, 0.0, 0.0, 0.0))
            except Exception:
                pass
            return cmap

    def _display_cmap(self, cmap_name=None):
        cmap_name = str(cmap_name or getattr(self, "cmap_mode", "gray") or "gray")
        bad_color = self._nan_bad_color()
        try:
            base = plt.get_cmap(cmap_name)
        except Exception:
            base = plt.get_cmap("gray")

        try:
            return base.with_extremes(bad=bad_color)
        except Exception:
            lut = base(np.linspace(0, 1, getattr(base, "N", 256)))
            cmap = mcolors.ListedColormap(lut, name=f"{cmap_name}_display")
            try:
                cmap.set_bad(color=bad_color)
            except Exception:
                pass
            return cmap

    def _active_basemap_paths(self):
        try:
            paths = discover_basemap_paths()
        except Exception:
            paths = []
        mode = normalize_basemap_mode(getattr(self, "basemap_mode", "nearest"))
        if mode == "single":
            path = str(getattr(self, "basemap_path", "") or "").strip()
            return [path] if path and os.path.isfile(path) else []
        category = normalize_basemap_category(getattr(self, "basemap_category", ""))
        categories = basemap_categories_from_paths(paths)
        if not category and categories:
            category = categories[0]
            self.basemap_category = category
        return basemap_paths_for_category(paths, category) if category else []

    def _basemap_path_for_panel(self, idx=None):
        mode = normalize_basemap_mode(getattr(self, "basemap_mode", "nearest"))
        if mode == "nearest" and idx is not None:
            path = str(getattr(self, "panel_basemap_paths", {}).get(idx, "") or "").strip()
            return path if path and os.path.isfile(path) else ""
        path = str(getattr(self, "basemap_path", "") or "").strip()
        return path if path and os.path.isfile(path) else ""

    def _basemap_loaded(self, idx=None):
        if idx is not None:
            return bool(self._basemap_path_for_panel(idx))
        mode = normalize_basemap_mode(getattr(self, "basemap_mode", "nearest"))
        if mode == "nearest":
            return bool(self._active_basemap_paths())
        return bool(self._basemap_path_for_panel(None))

    def _select_basemap_for_panel(self, idx, scene_path):
        if not hasattr(self, "panel_basemap_paths"):
            self.panel_basemap_paths = {}
        if not hasattr(self, "panel_basemap_delta_days"):
            self.panel_basemap_delta_days = {}
        self.panel_basemap_paths.pop(idx, None)
        self.panel_basemap_delta_days.pop(idx, None)

        if not scene_path:
            return "", None

        mode = normalize_basemap_mode(getattr(self, "basemap_mode", "nearest"))
        if mode == "single":
            path = self._basemap_path_for_panel(None)
            delta = basemap_delta_days_for_scene(scene_path, path) if path else None
        else:
            category_paths = self._active_basemap_paths()
            path, delta = nearest_basemap_for_scene(scene_path, category_paths)

        if path and os.path.isfile(path):
            self.panel_basemap_paths[idx] = path
            self.panel_basemap_delta_days[idx] = delta
            return path, delta
        return "", None

    def _basemap_log_values_for_panel(self, idx):
        path = self._basemap_path_for_panel(idx)
        if not path:
            return "", ""
        delta = getattr(self, "panel_basemap_delta_days", {}).get(idx, "")
        return os.path.basename(path), _format_log_delta_days(delta)

    def _basemap_delta_label_for_panel(self, idx):
        path = self._basemap_path_for_panel(idx)
        if not path:
            return ""
        delta = getattr(self, "panel_basemap_delta_days", {}).get(idx, None)
        delta_text = _format_log_delta_days(delta)
        if not delta_text:
            return ""
        suffix = "day" if delta_text == "1" else "days"
        return f"Delta Time: {delta_text} {suffix}"

    def _thermal_blend_mode(self):
        return normalize_thermal_blend_mode(getattr(self, "thermal_blend_mode", "normal"))

    def _thermal_blend_mode_active(self, idx=None):
        return self._basemap_loaded(idx) and self._thermal_blend_mode() != "normal"

    def _relative_clim(self, data_min, data_max):
        if data_min is None or data_max is None:
            return None
        try:
            data_min = float(data_min)
            data_max = float(data_max)
        except Exception:
            return None
        if not np.isfinite(data_min) or not np.isfinite(data_max):
            return None
        d_center = 0.5 * (data_min + data_max)
        d_half = max(0.5 * (data_max - data_min), 1e-12)
        try:
            center_rel, half_rel = self.global_contrast_rel
        except Exception:
            center_rel, half_rel = (0.0, 1.0)
        center = d_center + float(center_rel) * d_half
        half = max(float(half_rel) * d_half, 1e-12)
        return center - half, center + half

    def _display_range_from_data(self, display, data_range=None):
        if isinstance(data_range, (tuple, list)) and len(data_range) == 2:
            try:
                dmin = float(data_range[0])
                dmax = float(data_range[1])
                if np.isfinite(dmin) and np.isfinite(dmax):
                    return dmin, dmax
            except Exception:
                pass
        try:
            arr = np.asarray(display)
            if np.isfinite(arr).any():
                return float(np.nanmin(arr)), float(np.nanmax(arr))
        except Exception:
            pass
        return None, None

    def _thermal_rgba_for_display_values(self, display, data_min, data_max):
        arr = np.asarray(display, dtype="float32")
        rgba = np.zeros(arr.shape + (4,), dtype="float32")
        finite = np.isfinite(arr)
        bad_rgba = np.array(mcolors.to_rgba(self._nan_bad_color()), dtype="float32")
        rgba[...] = bad_rgba
        clim = self._relative_clim(data_min, data_max)
        if clim is None:
            dmin, dmax = self._display_range_from_data(arr)
            clim = self._relative_clim(dmin, dmax)
        if clim is None:
            return rgba
        vmin, vmax = clim
        if not np.isfinite(vmin) or not np.isfinite(vmax) or vmax <= vmin:
            vmax = vmin + 1e-12
        scaled = np.zeros(arr.shape, dtype="float32")
        scaled[finite] = (arr[finite] - float(vmin)) / float(vmax - vmin)
        scaled = np.clip(scaled, 0.0, 1.0)
        gamma = max(1e-12, float(getattr(self, "global_gamma", 1.0)))
        if abs(gamma - 1.0) > 1e-12:
            scaled[finite] = np.power(scaled[finite], gamma)
        cmap = self._display_cmap()
        rgba = np.asarray(cmap(scaled), dtype="float32")
        rgba[~finite] = bad_rgba
        return rgba

    def _sample_basemap_rgb_for_extent(self, idx, shape, extent):
        basemap_path = self._basemap_path_for_panel(idx)
        if not basemap_path or extent is None:
            return None
        try:
            h, w = int(shape[0]), int(shape[1])
        except Exception:
            return None
        if h <= 0 or w <= 0:
            return None
        try:
            xmin, xmax, ymin, ymax = [float(v) for v in extent]
            sampled, _bounds, vmin, vmax = self._read_basemap_for_display(
                basemap_path,
                window_extent=(xmin, ymin, xmax, ymax),
                out_shape=(h, w),
            )
        except Exception:
            return None

        if getattr(sampled, "ndim", 0) == 3 and sampled.shape[-1] in (3, 4):
            rgb = np.asarray(sampled[..., :3], dtype="float32")
            valid = np.isfinite(rgb).all(axis=2)
            if sampled.shape[-1] == 4:
                valid &= np.asarray(sampled[..., 3]) > 0
            rgb[~valid] = np.nan
            return np.clip(rgb, 0.0, 1.0)

        denom = float(vmax) - float(vmin)
        if not np.isfinite(denom) or denom <= 0:
            denom = 1.0
        gray = (np.asarray(sampled, dtype="float32") - float(vmin)) / denom
        gray = np.clip(gray, 0.0, 1.0).astype("float32", copy=False)
        rgb = np.asarray(self._basemap_cmap()(gray)[..., :3], dtype="float32")
        rgb[~np.isfinite(gray)] = np.nan
        return rgb

    def _blend_rgb_arrays(self, base_rgb, thermal_rgb, mode):
        mode = normalize_thermal_blend_mode(mode)
        base = np.clip(base_rgb, 0.0, 1.0)
        top = np.clip(thermal_rgb, 0.0, 1.0)
        if mode == "screen":
            out = 1.0 - (1.0 - base) * (1.0 - top)
        elif mode == "addition":
            out = base + top
        elif mode == "overlay":
            out = np.where(base <= 0.5, 2.0 * base * top, 1.0 - 2.0 * (1.0 - base) * (1.0 - top))
        elif mode == "soft light":
            out = (1.0 - 2.0 * top) * base * base + 2.0 * top * base
        elif mode == "hard light":
            out = np.where(top <= 0.5, 2.0 * base * top, 1.0 - 2.0 * (1.0 - base) * (1.0 - top))
        elif mode == "difference":
            out = np.abs(base - top)
        elif mode == "subtract":
            out = base - top
        else:
            out = top
        return np.clip(out, 0.0, 1.0)

    def _render_blended_thermal_rgba(self, idx, display, data_range, extent):
        arr = np.asarray(display, dtype="float32")
        thermal_rgba = self._thermal_rgba_for_display_values(arr, *(data_range or (None, None)))
        alpha = max(0.0, min(1.0, float(getattr(self, "thermal_alpha", 1.0))))
        thermal_rgb = np.clip(thermal_rgba[..., :3], 0.0, 1.0)
        thermal_alpha = np.clip(thermal_rgba[..., 3] * alpha, 0.0, 1.0)

        sampled_rgb = self._sample_basemap_rgb_for_extent(idx, arr.shape, extent)
        try:
            face_rgb = np.array(mcolors.to_rgb(self._panel_facecolor()), dtype="float32")
        except Exception:
            face_rgb = np.array((0.0, 0.0, 0.0), dtype="float32")
        if sampled_rgb is None:
            base_rgb = np.zeros(arr.shape + (3,), dtype="float32")
            base_rgb[...] = face_rgb
        else:
            base_rgb = np.asarray(sampled_rgb, dtype="float32")
            base_rgb[~np.isfinite(base_rgb).all(axis=2)] = face_rgb

        finite = np.isfinite(arr)
        blended_rgb = self._blend_rgb_arrays(base_rgb, thermal_rgb, self._thermal_blend_mode())
        source_rgb = np.where(finite[..., np.newaxis], blended_rgb, thermal_rgb)
        out = np.ones(arr.shape + (4,), dtype="float32")
        out[..., :3] = np.clip(source_rgb, 0.0, 1.0)
        out[..., 3] = thermal_alpha
        return out

    def _panel_thermal_extent(self, idx, img_obj=None):
        if img_obj is not None:
            try:
                ext = list(img_obj.get_extent())
                if len(ext) == 4:
                    return ext
            except Exception:
                pass
        try:
            L, R, B, T = self.bases[idx]
            dx, dy = self.offsets[idx]
            return [L + dx, R + dx, B + dy, T + dy]
        except Exception:
            return None

    def _refresh_panel_thermal_display(self, idx, display=None, data_range=None, extent=None):
        try:
            img_obj = self.images.get(idx) if hasattr(self.images, "get") else self.images[idx]
        except Exception:
            img_obj = None
        if img_obj is None:
            return

        if display is None:
            try:
                display = self.current_display_data.get(idx)
            except Exception:
                display = None
        if display is None:
            try:
                display = self.images_data.get(idx)
            except Exception:
                display = None
        if display is None:
            return

        if data_range is None:
            try:
                data_range = self.current_display_ranges.get(idx)
            except Exception:
                data_range = None
        dmin, dmax = self._display_range_from_data(display, data_range)
        if extent is None:
            extent = self._panel_thermal_extent(idx, img_obj)

        try:
            self.current_display_data[idx] = display
            self.current_display_ranges[idx] = (dmin, dmax)
        except Exception:
            pass

        if extent is not None:
            try:
                img_obj.set_extent(extent)
            except Exception:
                pass

        if self._thermal_blend_mode_active(idx):
            rgba = self._render_blended_thermal_rgba(idx, display, (dmin, dmax), extent)
            try:
                img_obj.set_data(rgba)
                img_obj.set_alpha(1.0)
                img_obj.set_interpolation(visual_resampling_mpl_interpolation(
                    getattr(self, "thermal_visual_resampling", "nearest")
                ))
            except Exception:
                pass
            try:
                self._refresh_split_artists(idx, draw=False)
            except Exception:
                pass
            return

        try:
            img_obj.set_data(display)
            img_obj.set_interpolation(visual_resampling_mpl_interpolation(
                getattr(self, "thermal_visual_resampling", "nearest")
            ))
        except Exception:
            pass
        try:
            img_obj.set_cmap(self._display_cmap())
        except Exception:
            pass
        clim = self._relative_clim(dmin, dmax)
        if clim is not None:
            vmin, vmax = clim
            if vmax <= vmin:
                vmax = vmin + 1e-12
            try:
                img_obj.set_norm(mcolors.PowerNorm(
                    gamma=max(1e-12, float(getattr(self, "global_gamma", 1.0))),
                    vmin=vmin,
                    vmax=vmax,
                ))
            except Exception:
                pass
        try:
            img_obj.set_alpha(max(0.0, min(1.0, float(getattr(self, "thermal_alpha", 1.0)))))
        except Exception:
            pass
        try:
            self._refresh_split_artists(idx, draw=False)
        except Exception:
            pass

    def _refresh_all_thermal_displays(self, draw=False):
        for idx in range(getattr(self, "n_pan", 0)):
            if not self._panel_has_image(idx):
                continue
            self._refresh_panel_thermal_display(idx)
        self._update_blend_mode_status_label()
        if draw:
            try:
                self.canvas.draw_idle()
            except Exception:
                pass

    def _init_blend_mode_status_label(self):
        if getattr(self, "_blend_mode_status_label", None) is not None:
            return
        try:
            status_bar = self.statusBar()
            try:
                existing_style = str(status_bar.styleSheet() or "")
                item_rule = "QStatusBar::item { border: 0px solid transparent; }"
                if "QStatusBar::item" not in existing_style:
                    status_bar.setStyleSheet((existing_style + "\n" + item_rule).strip())
            except Exception:
                pass
            label = QtWidgets.QLabel("", status_bar)
            label.setObjectName("blendModeStatusLabel")
            label.setTextFormat(QtCore.Qt.PlainText)
            status_bar.addWidget(label, 0)
            self._blend_mode_status_label = label
            self._update_blend_mode_status_label()
        except Exception:
            self._blend_mode_status_label = None

    def _update_blend_mode_status_label(self):
        label = getattr(self, "_blend_mode_status_label", None)
        if label is None:
            return
        try:
            try:
                gamma = max(1e-12, float(getattr(self, "global_gamma", 1.0)))
            except Exception:
                gamma = 1.0
            try:
                _center_rel, half_rel = getattr(self, "global_contrast_rel", (0.0, 1.0))
                half_rel = max(1e-12, float(half_rel))
                contrast_pct = int(round(100.0 / half_rel))
            except Exception:
                contrast_pct = 100
            try:
                alpha = max(0.0, min(1.0, float(getattr(self, "thermal_alpha", 1.0))))
                opacity_pct = int(round(alpha * 100.0))
            except Exception:
                opacity_pct = 100

            has_basemap = self._basemap_loaded()
            parts = [
                f"Gamma: {gamma:.2f}x",
                f"Contrast: {contrast_pct}%",
            ]
            if has_basemap:
                parts.append(f"Opacity: {opacity_pct}%")
                parts.append(f"Blend: {thermal_blend_mode_display_label(self._thermal_blend_mode())}")
            label.setText(" | ".join(parts))
            label.setVisible(True)
            set_widget_stylesheet_unscaled(label, self._blend_mode_status_stylesheet())
        except Exception:
            pass

    def _cycle_thermal_blend_mode(self, direction):
        if not self._basemap_loaded():
            return
        modes = list(THERMAL_BLEND_MODES)
        current = self._thermal_blend_mode()
        try:
            idx = modes.index(current)
        except ValueError:
            idx = 0
        self.thermal_blend_mode = modes[(idx + int(direction)) % len(modes)]
        self._update_blend_mode_status_label()
        self._refresh_all_thermal_displays(draw=True)
        try:
            self.statusBar().clearMessage()
        except Exception:
            pass

    def _build_edge_display(self, base):
        valid = np.isfinite(base)

        # Suppress the halo at NaN boundaries, but keep the NaN mask itself
        # so the display colormap can still render those pixels with set_bad().
        dil = binary_dilation(valid, footprint_rectangle((3, 3)))
        ero = binary_erosion(valid, footprint_rectangle((3, 3)))
        boundary = np.logical_xor(dil, ero)

        safe = np.nan_to_num(base, nan=0.0)
        edges = sobel(safe).astype("float32", copy=False)
        edges[boundary] = 0.0
        edges[~valid] = np.nan

        if np.isfinite(edges).any():
            e_min = float(np.nanmin(edges))
            e_max = float(np.nanmax(edges))
        else:
            e_min, e_max = 0.0, 0.0

        if e_max > e_min:
            edges = (edges - e_min) / (e_max - e_min)
        else:
            edges = np.zeros_like(edges, dtype="float32")
            edges[~valid] = np.nan

        return edges.astype("float32", copy=False), (0.0, 1.0)

    def _main_panel_text_scale(self):
        return normalize_main_panel_text_scale(getattr(self, "main_panel_text_scale", 1.0))

    def _main_panel_ui_scale_for_layout(self):
        return normalize_main_panel_text_scale(
            getattr(self, "_main_panel_ui_effective_scale", self._main_panel_text_scale())
        )

    def _scaled_main_panel_fontsize(self, base_fontsize, minimum=5.0, maximum=64.0):
        try:
            base = float(base_fontsize)
        except Exception:
            base = 12.0
        return max(float(minimum), min(float(maximum), base))

    def _main_panel_ui_font_pt(self, base_fontsize=10.0, minimum=6.0, maximum=28.0):
        try:
            base = float(base_fontsize)
        except Exception:
            base = 10.0
        return max(float(minimum), min(float(maximum), base * self._main_panel_ui_scale_for_layout()))

    def _main_panel_ui_px(self, value, minimum=0, maximum=80):
        try:
            numeric = float(value)
        except Exception:
            numeric = 0.0
        scaled = int(round(numeric * self._main_panel_ui_scale_for_layout()))
        return max(int(minimum), min(int(maximum), scaled))

    def _estimate_main_panel_menu_width(self, scale):
        labels = []
        try:
            labels.extend(str(action.text() or "").replace("&", "") for action in self.menuBar().actions())
        except Exception:
            pass
        corner_bar = getattr(self, "_keyboard_shortcuts_corner_bar", None)
        if corner_bar is not None:
            try:
                labels.extend(str(action.text() or "").replace("&", "") for action in corner_bar.actions())
            except Exception:
                pass
        labels = [label for label in labels if label]
        if not labels:
            return 0

        try:
            font = QtGui.QFont(self.menuBar().font())
        except Exception:
            font = QtGui.QFont()
        font.setPointSizeF(max(6.0, min(26.0, 10.0 * float(scale))))
        metrics = QtGui.QFontMetricsF(font)
        pad_x = min(6, max(2, int(round(6.0 * float(scale)))))
        extra = min(12, max(8, int(round(12.0 * float(scale)))))
        width = 0
        for label in labels:
            try:
                text_width = int(math.ceil(metrics.horizontalAdvance(label)))
            except Exception:
                text_width = len(label) * int(max(6, round(8 * float(scale))))
            width += text_width + 2 * pad_x + extra
        return width

    def _fitted_main_panel_ui_scale(self):
        desired = self._main_panel_text_scale()
        try:
            available = int(self.menuBar().width())
        except Exception:
            available = 0
        if available <= 0:
            try:
                available = int(self.width())
            except Exception:
                available = 0
        if available <= 0:
            return desired

        if self._estimate_main_panel_menu_width(desired) <= available:
            return desired

        low = MIN_MAIN_PANEL_TEXT_SCALE
        high = desired
        if self._estimate_main_panel_menu_width(low) > available:
            return low
        for _ in range(18):
            mid = (low + high) * 0.5
            if self._estimate_main_panel_menu_width(mid) <= available:
                low = mid
            else:
                high = mid
        return normalize_main_panel_text_scale(low)

    def _main_panel_menu_stylesheet(self):
        font_pt = self._main_panel_ui_font_pt(10.0, minimum=6.0, maximum=26.0)
        item_pad_y = min(2, self._main_panel_ui_px(2, minimum=0, maximum=6))
        item_pad_x = min(6, self._main_panel_ui_px(6, minimum=2, maximum=16))
        menu_pad_y = 0
        menu_pad_x = min(8, self._main_panel_ui_px(8, minimum=4, maximum=14))
        return (
            f"QMenuBar {{ font-size: {font_pt:.3f}pt; }}"
            f"QMenuBar::item {{ padding: {item_pad_y}px {item_pad_x}px; margin: 0px 2px; }}"
            f"QMenu {{ font-size: {font_pt:.3f}pt; padding: 0px; }}"
            f"QMenu::item {{ padding: {menu_pad_y}px {menu_pad_x}px; min-height: 0px; }}"
            f"QMenu::separator {{ height: 4px; margin: 0px; }}"
        )

    def _blend_mode_status_stylesheet(self):
        color = str(getattr(self, "theme", {}).get("text", "#D3D3D3"))
        font_pt = self._main_panel_ui_font_pt(9.0, minimum=6.0, maximum=24.0)
        pad_left = self._main_panel_ui_px(4, minimum=1, maximum=18)
        pad_y = self._main_panel_ui_px(1, minimum=0, maximum=8)
        return (
            f"color: {color}; font-size: {font_pt:.3f}pt; "
            f"padding: {pad_y}px 0px {pad_y}px {pad_left}px; border: 0px;"
        )

    def _relayout_main_panel_ui_scale_controls(self, defer=True):
        try:
            menu_bar = self.menuBar()
        except Exception:
            menu_bar = None
        corner_bar = getattr(self, "_keyboard_shortcuts_corner_bar", None)
        if corner_bar is not None:
            try:
                corner_bar.ensurePolished()
            except Exception:
                pass
            try:
                hint = corner_bar.sizeHint()
                width = max(1, int(hint.width()))
                if menu_bar is not None and int(menu_bar.width()) > 0:
                    left_edge = 0
                    for action in list(menu_bar.actions() or []):
                        try:
                            action_rect = menu_bar.actionGeometry(action)
                        except Exception:
                            continue
                        if action_rect is not None and not action_rect.isNull():
                            left_edge = max(left_edge, int(action_rect.right()) + 1)
                    available_width = int(menu_bar.width()) - left_edge
                    if available_width > 0:
                        width = min(width, available_width)
                height = max(1, int(hint.height()))

                def apply_corner_geometry():
                    corner_bar.setFixedWidth(width)
                    corner_bar.setMinimumHeight(height)
                    corner_bar.resize(width, height)

                _with_ui_scale_override(1.0, apply_corner_geometry)
            except Exception:
                pass
            try:
                corner_bar.updateGeometry()
                corner_bar.update()
            except Exception:
                pass

        if menu_bar is not None:
            try:
                if corner_bar is not None:
                    menu_bar.setCornerWidget(corner_bar, QtCore.Qt.TopRightCorner)
            except Exception:
                pass
            try:
                menu_bar.updateGeometry()
                menu_bar.adjustSize()
                menu_bar.update()
            except Exception:
                pass
            if corner_bar is not None:
                try:
                    geom = corner_bar.geometry()
                    max_x = max(0, int(menu_bar.width()) - int(geom.width()))
                    if int(geom.x()) > max_x:
                        corner_bar.move(max_x, int(geom.y()))
                except Exception:
                    pass

        if defer:
            try:
                QtCore.QTimer.singleShot(0, lambda: self._relayout_main_panel_ui_scale_controls(defer=False))
            except Exception:
                pass

    def _apply_main_panel_ui_scale(self):
        self._update_main_panel_text_scale_controls()
        self._main_panel_ui_effective_scale = self._fitted_main_panel_ui_scale()
        style = self._main_panel_menu_stylesheet()
        menu_bar = None
        try:
            menu_bar = self.menuBar()
        except Exception:
            menu_bar = None
        if menu_bar is not None:
            try:
                set_widget_stylesheet_unscaled(menu_bar, style)
            except Exception:
                pass
        for menu in (
            getattr(self, "_settings_profiles_menu", None),
            getattr(self, "_view_menu", None),
            getattr(self, "_ui_scale_menu", None),
        ):
            if menu is None:
                continue
            try:
                set_widget_stylesheet_unscaled(menu, style)
            except Exception:
                pass

        corner_bar = getattr(self, "_keyboard_shortcuts_corner_bar", None)
        if corner_bar is not None:
            try:
                set_widget_stylesheet_unscaled(corner_bar, style)
            except Exception:
                pass

        try:
            status_bar = self.statusBar()
            force_widget_ui_scale_100(status_bar)
            status_font_pt = self._main_panel_ui_font_pt(9.0, minimum=6.0, maximum=24.0)
            set_widget_stylesheet_unscaled(
                status_bar,
                f"QStatusBar {{ font-size: {status_font_pt:.3f}pt; }}"
                "QStatusBar::item { border: 0px solid transparent; }",
            )
        except Exception:
            pass

        self._update_blend_mode_status_label()
        self._update_main_panel_text_scale_controls()
        self._relayout_main_panel_ui_scale_controls(defer=True)

    def _set_panel_text_artist_fontsize(self, artist, base_fontsize, minimum=5.0, maximum=64.0):
        if artist is None:
            return
        try:
            base = float(base_fontsize)
        except Exception:
            return
        try:
            setattr(artist, "_geoviewer_panel_text_base_fontsize", base)
        except Exception:
            pass
        try:
            artist.set_fontsize(self._scaled_main_panel_fontsize(base, minimum=minimum, maximum=maximum))
        except Exception:
            pass

    def _panel_title_fontsize(self):
        try:
            value = float(getattr(self, "title_fontsize", 18.0))
        except Exception:
            value = 18.0
        return max(8.0, min(40.0, value))

    def _panel_delta_base_fontsize(self):
        return max(6.0, min(10.0, float(getattr(self, "title_fontsize", 18.0)) * 0.55))

    def _panel_delta_fontsize(self):
        return self._panel_delta_base_fontsize()

    def _base_fontsize_for_panel_text_value(self, value):
        text = str(value or "")
        if text == "NO MORE IMAGES":
            return 16.0
        if text in ("EMPTY", "Warping..."):
            return 14.0 if text == "EMPTY" else 12.0
        return None

    def _refresh_main_panel_text_scale(self, draw=True):
        axes = list(getattr(self, "axes", []) or [])
        for ax in axes:
            try:
                label = getattr(ax, "_panel_title_text", "")
                if label:
                    self._set_panel_title(ax, label)
                delta_label = getattr(ax, "_basemap_delta_text", "")
                if delta_label:
                    self._set_basemap_delta_label(ax, delta_label)

                comment_marker = getattr(ax, "_comment_marker_artist", None)
                if comment_marker is not None:
                    self._set_panel_text_artist_fontsize(comment_marker, 18.0, minimum=6.0, maximum=48.0)

                delta_artist = getattr(ax, "_basemap_delta_artist", None)
                for text_artist in list(getattr(ax, "texts", []) or []):
                    if text_artist is delta_artist or text_artist is comment_marker:
                        continue
                    base = getattr(text_artist, "_geoviewer_panel_text_base_fontsize", None)
                    if base is None:
                        base = self._base_fontsize_for_panel_text_value(text_artist.get_text())
                    if base is not None:
                        self._set_panel_text_artist_fontsize(text_artist, base, minimum=5.0, maximum=64.0)
            except Exception:
                pass

        warp_data = getattr(self, "warp_data", {}) or {}
        warp_items = warp_data.values() if isinstance(warp_data, dict) else warp_data
        for data in warp_items:
            if not isinstance(data, dict):
                continue
            banner = data.get("banner")
            if banner is not None:
                self._set_panel_text_artist_fontsize(banner, 12.0, minimum=5.0, maximum=48.0)
            for label in list(data.get("labels", []) or []):
                self._set_panel_text_artist_fontsize(label, 8.0, minimum=4.0, maximum=32.0)

        self._update_main_panel_text_scale_controls()
        if draw and hasattr(self, "canvas"):
            try:
                self.canvas.draw_idle()
            except Exception:
                pass

    def _set_main_panel_text_scale(self, value, draw=True):
        self.main_panel_text_scale = normalize_main_panel_text_scale(value, getattr(self, "main_panel_text_scale", 1.0))
        self._apply_main_panel_ui_scale()
        return self.main_panel_text_scale

    def _set_main_panel_text_scale_from_menu(self, value):
        scale = self._set_main_panel_text_scale(value, draw=True)
        self._persist_script_level_scale_state(main_panel_text_scale=scale)
        self._update_main_panel_text_scale_controls()
        try:
            self.statusBar().showMessage(
                f"Panel Text scale set to {format_main_panel_text_scale_label(scale)}",
                2500,
            )
        except Exception:
            pass

    def _adjust_main_panel_text_scale(self, factor):
        current = self._main_panel_text_scale()
        try:
            factor = float(factor)
        except Exception:
            factor = 1.0
        scale = self._set_main_panel_text_scale(current * factor, draw=True)
        try:
            self.statusBar().showMessage(
                f"Main panel UI scale set to {format_main_panel_text_scale_label(scale)}",
                2500,
            )
        except Exception:
            pass

    def _reset_main_panel_text_scale(self):
        scale = self._set_main_panel_text_scale(1.0, draw=True)
        try:
            self.statusBar().showMessage(
                f"Main panel UI scale set to {format_main_panel_text_scale_label(scale)}",
                2500,
            )
        except Exception:
            pass

    def _apply_footer_style(self):
        prog = getattr(self, "prog", None)
        if prog is not None:
            try:
                prog.set_color(self.theme['text'])
                prog.set_fontsize(float(getattr(self, "summary_fontsize", 11.0)))
            except Exception:
                pass
        footer_frame = getattr(self, "footer_frame", None)
        if footer_frame is not None:
            try:
                footer_frame.setStyleSheet(f"background-color: {self.theme['window_bg']};")
            except Exception:
                pass
        footer_label = getattr(self, "footer_label", None)
        if footer_label is not None:
            try:
                font = QtGui.QFont('DejaVu Sans Mono')
                font.setPointSizeF(float(getattr(self, "summary_fontsize", 11.0)))
                footer_label.setFont(font)
                footer_label.setAlignment(QtCore.Qt.AlignHCenter | QtCore.Qt.AlignVCenter)
                footer_label.setStyleSheet(
                    f"background-color: {self.theme['window_bg']};"
                    f"color: {self.theme['text']};"
                    "padding: 2px 0px 6px 0px;"
                )
            except Exception:
                pass

    def _style_panel_axes(self, ax):
        ax.set_facecolor(self._panel_facecolor())
        # Keep panel boxes fixed during per-panel zoom changes; the view-fit
        # helper below preserves the on-screen ratio without Matplotlib's
        # aspect engine resizing axes or emitting limit/aspect warnings.
        ax.set_aspect('auto')
        ax.set_anchor('C')
        ax.set_xticks([])
        ax.set_yticks([])
        ax.tick_params(left=False, bottom=False, labelleft=False, labelbottom=False)
        for spine in ax.spines.values():
            spine.set_visible(False)

    def _clear_panel_title(self, ax):
        artist = getattr(ax, "_panel_title_artist", None)
        if artist is not None:
            try:
                artist.remove()
            except Exception:
                pass
        try:
            ax.set_title("")
        except Exception:
            pass
        ax._panel_title_artist = None
        ax._panel_title_text = ""

    def _set_panel_title(self, ax, text):
        label = str(text or "")
        if not label:
            self._clear_panel_title(ax)
            return

        pos = ax.get_position()
        x = float(pos.x0 + pos.width * 0.5)
        y = min(0.985, float(pos.y1 + 0.008))
        fontsize = self._panel_title_fontsize()

        artist = getattr(ax, "_panel_title_artist", None)
        if artist is None or getattr(artist, "figure", None) is not self.fig:
            if artist is not None:
                try:
                    artist.remove()
                except Exception:
                    pass
            artist = self.fig.text(
                x, y, label,
                ha='center', va='bottom',
                fontsize=fontsize,
                color=self.theme['title'],
                zorder=7,
                clip_on=False,
            )
            ax._panel_title_artist = artist
        else:
            artist.set_text(label)
            artist.set_position((x, y))
            artist.set_fontsize(fontsize)
            artist.set_color(self.theme['title'])

        ax._panel_title_text = label
        try:
            ax.set_title("")
        except Exception:
            pass

    def _clear_basemap_delta_label(self, ax):
        artist = getattr(ax, "_basemap_delta_artist", None)
        if artist is not None:
            try:
                artist.remove()
            except Exception:
                pass
        ax._basemap_delta_artist = None
        ax._basemap_delta_text = ""

    def _set_basemap_delta_label(self, ax, text):
        label = str(text or "").strip()
        if not label:
            self._clear_basemap_delta_label(ax)
            return
        artist = getattr(ax, "_basemap_delta_artist", None)
        fontsize = self._panel_delta_fontsize()
        if artist is None or getattr(artist, "axes", None) is not ax:
            if artist is not None:
                try:
                    artist.remove()
                except Exception:
                    pass
            artist = ax.text(
                0.01, 0.99, label,
                ha="left", va="top",
                transform=ax.transAxes,
                fontsize=fontsize,
                color=BASEMAP_DELTA_LABEL_COLOR,
                zorder=8,
                clip_on=False,
                path_effects=[path_effects.withStroke(linewidth=2.5, foreground=BASEMAP_DELTA_LABEL_STROKE_COLOR)],
            )
            ax._basemap_delta_artist = artist
        else:
            artist.set_text(label)
            artist.set_fontsize(fontsize)
            artist.set_color(BASEMAP_DELTA_LABEL_COLOR)
            artist.set_path_effects([
                path_effects.withStroke(linewidth=2.5, foreground=BASEMAP_DELTA_LABEL_STROKE_COLOR)
            ])
        ax._basemap_delta_text = label

    def _ensure_panel_geometry_timer(self):
        timer = getattr(self, "_panel_geometry_timer", None)
        if timer is None:
            timer = QtCore.QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._sync_panel_geometry)
            self._panel_geometry_timer = timer
        return timer

    def _schedule_panel_geometry_sync(self, delay_ms: int = 35):
        if not hasattr(self, "canvas"):
            return
        try:
            timer = self._ensure_panel_geometry_timer()
            timer.start(max(0, int(delay_ms)))
        except Exception:
            pass

    def _sync_panel_geometry(self):
        if getattr(self, "_syncing_panel_geometry", False):
            return
        if not hasattr(self, "canvas") or not hasattr(self, "axes"):
            return

        self._syncing_panel_geometry = True
        startup_paint_blocked = bool(getattr(self, "_startup_panel_paint_blocked", False))
        if startup_paint_blocked:
            try:
                self.canvas.setUpdatesEnabled(False)
            except Exception:
                pass
        try:
            pending_layout_autofit = bool(getattr(self, "_pending_layout_autofit", False))
            try:
                self.canvas.draw()
            except Exception:
                pass

            for idx, ax in enumerate(getattr(self, "axes", [])):
                if idx >= getattr(self, "n_pan", 0):
                    continue
                if self._panel_has_loaded_image(idx):
                    if pending_layout_autofit:
                        self._fit_panel_to_current_scene(idx)
                    else:
                        view = getattr(self, "panel_views", {}).get(idx)
                        if view is None:
                            try:
                                view = (ax.get_xlim(), ax.get_ylim())
                            except Exception:
                                view = None
                        if view is not None:
                            self._set_panel_view(ax, view[0], view[1], remember=True, sync=False)
                            self._refresh_dynamic_basemap_underlay(idx, force=True)

                label = getattr(ax, "_panel_title_text", "")
                if label:
                    self._set_panel_title(ax, label)
                delta_label = getattr(ax, "_basemap_delta_text", "")
                if delta_label:
                    self._set_basemap_delta_label(ax, delta_label)

            self.add_keep_reject_buttons()
            self._sync_button_visibility()
            if startup_paint_blocked:
                try:
                    self.canvas.setUpdatesEnabled(True)
                except Exception:
                    pass
                self._startup_panel_paint_blocked = False
            try:
                self.canvas.draw()
            except Exception:
                self.canvas.draw_idle()
        finally:
            if bool(getattr(self, "_startup_panel_paint_blocked", False)):
                try:
                    self.canvas.setUpdatesEnabled(True)
                except Exception:
                    pass
                self._startup_panel_paint_blocked = False
            self._pending_layout_autofit = False
            self._syncing_panel_geometry = False

    def _fit_panel_to_current_scene(self, idx: int, remember: bool = True, sync: bool = False):
        if not self._panel_has_loaded_image(idx):
            return
        try:
            L, R, B, T = self.bases[idx]
        except Exception:
            return
        dx, dy = self.offsets.get(idx, [0, 0])
        self._set_panel_view(
            self.axes[idx],
            (L + dx, R + dx),
            (B + dy, T + dy),
            remember=remember,
            sync=sync,
        )

    def _panel_display_pixel_size(self, idx: int):
        if not self._panel_has_loaded_image(idx):
            return None
        try:
            L, R, B, T = self.bases[idx]
            display = self.images_data.get(idx) if hasattr(self.images_data, "get") else self.images_data[idx]
            h, w = display.shape[:2]
            pixel_w = abs(float(R) - float(L)) / max(1, int(w))
            pixel_h = abs(float(T) - float(B)) / max(1, int(h))
        except Exception:
            return None
        if not np.isfinite([pixel_w, pixel_h]).all() or pixel_w <= 0 or pixel_h <= 0:
            return None
        return pixel_w, pixel_h

    def _view_with_panel_pixel_floor(self, idx: int, xlim, ylim, min_pixels: float = 3.0):
        try:
            x0, x1 = float(xlim[0]), float(xlim[1])
            y0, y1 = float(ylim[0]), float(ylim[1])
        except Exception:
            return None
        if not np.isfinite([x0, x1, y0, y1]).all():
            return None

        span_x = abs(x1 - x0)
        span_y = abs(y1 - y0)
        if span_x <= 1e-12 or span_y <= 1e-12:
            return None

        pixel_size = self._panel_display_pixel_size(idx)
        if pixel_size is not None:
            pixel_w, pixel_h = pixel_size
            min_span_x = pixel_w * max(1.0, float(min_pixels))
            min_span_y = pixel_h * max(1.0, float(min_pixels))

            if span_x < min_span_x:
                cx = 0.5 * (x0 + x1)
                half = 0.5 * min_span_x
                if x1 >= x0:
                    x0, x1 = cx - half, cx + half
                else:
                    x0, x1 = cx + half, cx - half

            if span_y < min_span_y:
                cy = 0.5 * (y0 + y1)
                half = 0.5 * min_span_y
                if y1 >= y0:
                    y0, y1 = cy - half, cy + half
                else:
                    y0, y1 = cy + half, cy - half

        if not np.isfinite([x0, x1, y0, y1]).all():
            return None
        if abs(x1 - x0) <= 1e-12 or abs(y1 - y0) <= 1e-12:
            return None
        return (x0, x1), (y0, y1)

    def _set_panel_view_exact(self, ax, xlim, ylim, remember: bool = True, idx: int = None):
        """Restore axis limits exactly, without aspect expansion or sync broadcast."""
        panel_idx = idx
        if panel_idx is None and hasattr(self, "axes") and ax in getattr(self, "axes", []):
            panel_idx = self.axes.index(ax)

        view = self._view_with_panel_pixel_floor(panel_idx, xlim, ylim) if panel_idx is not None else None
        if view is None:
            return False
        (x0, x1), (y0, y1) = view

        try:
            ax.set_xlim(x0, x1)
            ax.set_ylim(y0, y1)
        except Exception:
            return False

        if remember and panel_idx is not None:
            if not hasattr(self, "panel_views"):
                self.panel_views = {}
            self.panel_views[panel_idx] = ((x0, x1), (y0, y1))
            self._refresh_dynamic_basemap_underlay(panel_idx)
        return True

    def _panel_scene_extent(self, idx: int):
        if not self._panel_has_loaded_image(idx):
            return None
        try:
            L, R, B, T = self.bases[idx]
            dx, dy = self.offsets.get(idx, [0, 0])
            sx0, sx1 = sorted((float(L) + float(dx), float(R) + float(dx)))
            sy0, sy1 = sorted((float(B) + float(dy), float(T) + float(dy)))
        except Exception:
            return None
        vals = [sx0, sx1, sy0, sy1]
        if not np.isfinite(vals).all() or sx1 <= sx0 or sy1 <= sy0:
            return None
        return sx0, sx1, sy0, sy1

    def _view_overlaps_current_scene(self, idx: int, xlim, ylim, previous_scene_extent=None) -> bool:
        scene_extent = self._panel_scene_extent(idx)
        if scene_extent is None:
            return False
        try:
            vx0, vx1 = sorted((float(xlim[0]), float(xlim[1])))
            vy0, vy1 = sorted((float(ylim[0]), float(ylim[1])))
            sx0, sx1, sy0, sy1 = scene_extent
        except Exception:
            return False
        vals = [vx0, vx1, vy0, vy1, sx0, sx1, sy0, sy1]
        if not np.isfinite(vals).all() or vx1 <= vx0 or vy1 <= vy0:
            return False

        view_w = vx1 - vx0
        view_h = vy1 - vy0
        scene_w = sx1 - sx0
        scene_h = sy1 - sy0
        if view_w <= 0 or view_h <= 0 or scene_w <= 0 or scene_h <= 0:
            return False

        if previous_scene_extent is not None:
            try:
                px0, px1, py0, py1 = previous_scene_extent
                prev_w = abs(float(px1) - float(px0))
                prev_h = abs(float(py1) - float(py0))
                if prev_w > 0 and prev_h > 0 and np.isfinite([prev_w, prev_h]).all():
                    scene_scale_jump = max(
                        scene_w / prev_w,
                        prev_w / scene_w,
                        scene_h / prev_h,
                        prev_h / scene_h,
                    )
                    if scene_scale_jump > 4.0:
                        return False
            except Exception:
                pass

        if view_w > scene_w * 4.0 or view_h > scene_h * 4.0:
            return False
        if view_w < scene_w * 1e-5 or view_h < scene_h * 1e-5:
            return False

        overlap_w = min(vx1, sx1) - max(vx0, sx0)
        overlap_h = min(vy1, sy1) - max(vy0, sy0)
        if overlap_w <= 0 or overlap_h <= 0:
            return False

        view_overlap = (overlap_w * overlap_h) / max(view_w * view_h, 1e-12)
        scene_overlap = (overlap_w * overlap_h) / max(scene_w * scene_h, 1e-12)
        vcx = 0.5 * (vx0 + vx1)
        vcy = 0.5 * (vy0 + vy1)
        center_inside_scene = sx0 <= vcx <= sx1 and sy0 <= vcy <= sy1

        return center_inside_scene or view_overlap >= 0.5 or scene_overlap >= 0.5

    def _sync_zoom_pan_enabled(self):
        return bool(getattr(self, "sync_zoom_pan", False))

    def _sync_panel_view_from_limits(self, source_idx, view):
        if not self._sync_zoom_pan_enabled() or getattr(self, "_syncing_panel_view", False):
            return
        try:
            source_idx = int(source_idx)
        except Exception:
            return
        if not self._panel_has_loaded_image(source_idx):
            return
        axes = list(getattr(self, "axes", []) or [])
        if not axes or view is None:
            return

        self._syncing_panel_view = True
        try:
            for idx, other_ax in enumerate(axes):
                if idx == source_idx:
                    continue
                if idx >= getattr(self, "n_pan", 0) or not self._panel_has_loaded_image(idx):
                    continue
                if not self._set_panel_view_exact(other_ax, view[0], view[1], remember=True, idx=idx):
                    self._set_panel_view(other_ax, view[0], view[1], remember=True, sync=False)
        finally:
            self._syncing_panel_view = False

    def _sync_panel_view_from_index(self, source_idx=None):
        if not self._sync_zoom_pan_enabled():
            return
        axes = list(getattr(self, "axes", []) or [])
        if not axes:
            return
        candidates = []
        try:
            candidates.append(int(source_idx))
        except Exception:
            pass
        try:
            candidates.append(int(getattr(self, "active_idx", 0)))
        except Exception:
            pass
        candidates.extend(range(len(axes)))

        chosen_idx = None
        for idx in candidates:
            if 0 <= idx < len(axes) and idx < getattr(self, "n_pan", 0) and self._panel_has_loaded_image(idx):
                chosen_idx = idx
                break
        if chosen_idx is None:
            return

        try:
            view = (axes[chosen_idx].get_xlim(), axes[chosen_idx].get_ylim())
        except Exception:
            return
        self._sync_panel_view_from_limits(chosen_idx, view)

    def _set_panel_view(self, ax, xlim, ylim, remember: bool = True, sync: bool = True):
        """Apply a per-panel view without allowing subplot boxes to resize."""
        try:
            x0, x1 = sorted((float(xlim[0]), float(xlim[1])))
            y0, y1 = sorted((float(ylim[0]), float(ylim[1])))
        except Exception:
            return

        if not np.isfinite([x0, x1, y0, y1]).all():
            return

        panel_idx = None
        if hasattr(self, "axes") and ax in getattr(self, "axes", []):
            panel_idx = self.axes.index(ax)

        if panel_idx is not None:
            floored_view = self._view_with_panel_pixel_floor(panel_idx, (x0, x1), (y0, y1))
            if floored_view is None:
                return
            (x0, x1), (y0, y1) = floored_view

        width = max(x1 - x0, 1e-12)
        height = max(y1 - y0, 1e-12)

        try:
            pos = ax.get_position()
            fig_w = max(float(self.fig.bbox.width), 1.0)
            fig_h = max(float(self.fig.bbox.height), 1.0)
            target_ratio = (float(pos.width) * fig_w) / max(float(pos.height) * fig_h, 1e-12)
        except Exception:
            target_ratio = None

        if target_ratio and np.isfinite(target_ratio) and target_ratio > 0:
            cx = 0.5 * (x0 + x1)
            cy = 0.5 * (y0 + y1)
            current_ratio = width / height
            if current_ratio > target_ratio:
                height = width / target_ratio
            else:
                width = height * target_ratio
            x0, x1 = cx - width * 0.5, cx + width * 0.5
            y0, y1 = cy - height * 0.5, cy + height * 0.5

        ax.set_xlim(x0, x1)
        ax.set_ylim(y0, y1)

        applied_view = ((x0, x1), (y0, y1))

        if remember and panel_idx is not None:
            if not hasattr(self, "panel_views"):
                self.panel_views = {}
            self.panel_views[panel_idx] = applied_view
            if sync and self._sync_zoom_pan_enabled():
                self._sync_panel_view_from_limits(panel_idx, applied_view)
            self._refresh_dynamic_basemap_underlay(panel_idx)

    def _drag_is_bottom_up(self, ax, press_event, release_event):
        drag = getattr(self, "_rectangle_drag_state", None)
        if isinstance(drag, dict) and drag.get("axes") is ax:
            try:
                press_y = float(drag.get("press_y"))
                current_y = float(drag.get("current_y"))
                if abs(current_y - press_y) > 1e-6:
                    return current_y > press_y
                return False
            except Exception:
                pass

        try:
            return float(release_event.y) > float(press_event.y)
        except Exception:
            try:
                return float(release_event.ydata) > float(press_event.ydata)
            except Exception:
                return False

    def _zoom_out_from_drag_box(self, ax, press_event, release_event):
        try:
            cur_x0, cur_x1 = sorted(float(v) for v in ax.get_xlim())
            cur_y0, cur_y1 = sorted(float(v) for v in ax.get_ylim())
            box_x0, box_x1 = sorted((float(press_event.xdata), float(release_event.xdata)))
            box_y0, box_y1 = sorted((float(press_event.ydata), float(release_event.ydata)))
        except Exception:
            return

        vals = [cur_x0, cur_x1, cur_y0, cur_y1, box_x0, box_x1, box_y0, box_y1]
        if not np.isfinite(vals).all():
            return

        cur_w = max(cur_x1 - cur_x0, 1e-12)
        cur_h = max(cur_y1 - cur_y0, 1e-12)
        box_w = box_x1 - box_x0
        box_h = box_y1 - box_y0
        if box_w <= 1e-12 or box_h <= 1e-12:
            return

        rel_x0 = (box_x0 - cur_x0) / cur_w
        rel_x1 = (box_x1 - cur_x0) / cur_w
        rel_y0 = (box_y0 - cur_y0) / cur_h
        rel_y1 = (box_y1 - cur_y0) / cur_h

        rel_w = max(rel_x1 - rel_x0, 1e-12)
        rel_h = max(rel_y1 - rel_y0, 1e-12)
        new_w = cur_w / rel_w
        new_h = cur_h / rel_h
        new_x0 = cur_x0 - rel_x0 * new_w
        new_y0 = cur_y0 - rel_y0 * new_h

        self._set_panel_view(
            ax,
            (new_x0, new_x0 + new_w),
            (new_y0, new_y0 + new_h),
        )

    def _handle_rectangle_zoom(self, ax, press_event, release_event):
        try:
            idx = self.axes.index(ax)
        except Exception:
            return
        if not self._panel_has_loaded_image(idx):
            return
        if self._drag_is_bottom_up(ax, press_event, release_event):
            self._zoom_out_from_drag_box(ax, press_event, release_event)
        else:
            self._set_panel_view(
                ax,
                (press_event.xdata, release_event.xdata),
                (press_event.ydata, release_event.ydata),
            )
        self.canvas.draw_idle()

    def _keep_reject_button_styles(self):
        return {
            "keep": build_keep_reject_button_style(
                getattr(
                    self,
                    "keep_button_color",
                    KEEP_REJECT_BUTTON_PRESET_BY_ID[DEFAULT_KEEP_REJECT_PRESET_ID]["keep"],
                )
            ),
            "reject": build_keep_reject_button_style(
                getattr(
                    self,
                    "reject_button_color",
                    KEEP_REJECT_BUTTON_PRESET_BY_ID[DEFAULT_KEEP_REJECT_PRESET_ID]["reject"],
                )
            ),
        }

    def _apply_keep_reject_button_styles(self):
        button_styles = self._keep_reject_button_styles()
        for btn_idx, btn in enumerate(getattr(self, "buttons", [])):
            role = "keep" if (btn_idx % 2) == 0 else "reject"
            style = button_styles[role]
            try:
                btn.color = style["base"]
                btn.hovercolor = style["hover"]
            except Exception:
                pass
            try:
                btn.ax.set_facecolor(style["base"])
                btn.ax.patch.set_facecolor(style["base"])
                btn.ax.patch.set_edgecolor(style["edge"])
                btn.ax.patch.set_linewidth(1.25)
            except Exception:
                pass
            try:
                btn.label.set_color(style["text"])
                btn.label.set_fontweight("bold")
            except Exception:
                pass

    def _set_keep_reject_button_colors(self, button_colors):
        if not button_colors:
            return

        current_keep = getattr(
            self,
            "keep_button_color",
            KEEP_REJECT_BUTTON_PRESET_BY_ID[DEFAULT_KEEP_REJECT_PRESET_ID]["keep"],
        )
        current_reject = getattr(
            self,
            "reject_button_color",
            KEEP_REJECT_BUTTON_PRESET_BY_ID[DEFAULT_KEEP_REJECT_PRESET_ID]["reject"],
        )
        preset_id = str(button_colors.get("preset") or "").strip()
        preset = KEEP_REJECT_BUTTON_PRESET_BY_ID.get(preset_id)

        self.keep_button_color = normalize_keep_reject_button_color(
            button_colors.get("keep"),
            preset["keep"] if preset is not None else current_keep,
        )
        self.reject_button_color = normalize_keep_reject_button_color(
            button_colors.get("reject"),
            preset["reject"] if preset is not None else current_reject,
        )
        self.keep_reject_button_preset = infer_keep_reject_button_preset(
            self.keep_button_color,
            self.reject_button_color,
        )
        self._apply_keep_reject_button_styles()

    def _apply_theme(self):
        set_app_theme_mode(self.theme_mode)
        self.theme = self._theme_palette()

        self.setStyleSheet(
            f"background-color: {self.theme['window_bg']}; color: {self.theme['text']};"
        )
        central = self.centralWidget()
        if central is not None:
            central.setStyleSheet(
                f"background-color: {self.theme['window_bg']}; color: {self.theme['text']};"
            )

        try:
            self.fig.patch.set_facecolor(self.theme['figure_bg'])
        except Exception:
            pass
        try:
            self.canvas.setStyleSheet(f"background-color: {self.theme['figure_bg']};")
        except Exception:
            pass

        for ax in getattr(self, "axes", []):
            try:
                self._style_panel_axes(ax)
                label = getattr(ax, "_panel_title_text", "")
                if label:
                    self._set_panel_title(ax, label)
                delta_label = getattr(ax, "_basemap_delta_text", "")
                if delta_label:
                    self._set_basemap_delta_label(ax, delta_label)
                for txt in ax.texts:
                    val = txt.get_text()
                    if val in ("EMPTY", "NO MORE IMAGES", "Warping..."):
                        txt.set_color(
                            self.theme['empty'] if val in ("EMPTY", "NO MORE IMAGES") else self.theme['text']
                        )
                        base_fontsize = self._base_fontsize_for_panel_text_value(val)
                        if base_fontsize is not None:
                            self._set_panel_text_artist_fontsize(txt, base_fontsize, minimum=5.0, maximum=64.0)
            except Exception:
                pass

        try:
            self._refresh_all_thermal_displays()
        except Exception:
            for img in getattr(self, "images", {}).values():
                try:
                    if img is not None:
                        img.set_cmap(self._display_cmap())
                        img.set_alpha(float(getattr(self, "thermal_alpha", 1.0)))
                except Exception:
                    pass

        for img in getattr(self, "basemap_images", {}).values():
            try:
                if img is not None:
                    arr = img.get_array()
                    if not (getattr(arr, "ndim", 0) == 3 and arr.shape[-1] in (3, 4)):
                        img.set_cmap(self._basemap_cmap())
            except Exception:
                pass

        self._apply_footer_style()
        self._apply_keep_reject_button_styles()
        self._update_keyboard_shortcuts_button_state()
        self._update_ui_scale_menu()
        self._apply_main_panel_ui_scale()
        self._update_blend_mode_status_label()

        try:
            self.canvas.draw()
        except Exception:
            self.canvas.draw_idle()

    def _toggle_theme(self):
        self.theme_mode = "light" if getattr(self, "theme_mode", "dark") == "dark" else "dark"
        self._apply_theme()

    def _init_rocket_easter_egg(self):
        self._rocket_phrase = ROCKET_TRIGGER_PHRASE
        self._rocket_buffer = ""
        self._rocket_last_key_ts = 0.0
        self._rocket_swallow_after = 2
        self._rocket_overlay = RocketLaunchOverlay(self)
        self._rocket_overlay.setGeometry(self.rect())
        self._pending_shift_reset = False
        self._shift_candidate = False
        self._shift_is_down = False
        self._shift_started_in_window = False

        app = QApplication.instance()
        if app is not None:
            try:
                app.installEventFilter(self)
            except Exception:
                pass

    def _init_alt_magnifier(self):
        self._alt_is_down = False
        self._alt_magnifier_widget = None
        self._alt_magnifier = AltMagnifierOverlay(self)

    def _event_widget(self, obj):
        cur = obj
        seen = set()
        while isinstance(cur, QtCore.QObject) and id(cur) not in seen:
            seen.add(id(cur))
            if isinstance(cur, QtWidgets.QWidget):
                return cur
            parent = cur.parent()
            cur = parent if isinstance(parent, QtCore.QObject) else None
        return None

    def _object_belongs_to_viewer(self, obj):
        cur = obj
        seen = set()
        while isinstance(cur, QtCore.QObject) and id(cur) not in seen:
            seen.add(id(cur))
            if cur is self:
                return True
            if isinstance(cur, QtWidgets.QWidget):
                if cur is self.menuBar() or cur.window() is self or self.isAncestorOf(cur):
                    return True
            parent = cur.parent()
            cur = parent if isinstance(parent, QtCore.QObject) else None
        return False

    def _widget_is_image_comment_widget(self, obj):
        cur = obj
        seen = set()
        while isinstance(cur, QtCore.QObject) and id(cur) not in seen:
            seen.add(id(cur))
            try:
                if bool(cur.property("geoviewer_image_comment_dialog")):
                    return True
            except Exception:
                pass
            parent = cur.parent()
            cur = parent if isinstance(parent, QtCore.QObject) else None
        return False

    def _image_comment_dialog_active(self, obj=None):
        if bool(getattr(self, "_comment_dialog_active", False)):
            return True

        candidates = [obj, getattr(self, "_active_comment_dialog", None)]
        app = QtWidgets.QApplication.instance()
        if app is not None:
            for getter in (app.activeModalWidget, app.focusWidget, app.activePopupWidget):
                try:
                    candidates.append(getter())
                except Exception:
                    pass

        return any(self._widget_is_image_comment_widget(candidate) for candidate in candidates)

    def _release_image_shortcut_state_for_text_entry(self):
        self._cancel_pending_shift_reset()
        self._shift_is_down = False
        self._shift_started_in_window = False
        self._control_is_down = False
        self._control_candidate = False
        self._control_started_in_window = False
        self._release_all_thermal_transparency_keys()
        self._reset_rocket_buffer()

    def _sync_alt_magnifier(self, source_widget=None, force_active=None):
        overlay = getattr(self, "_alt_magnifier", None)
        if overlay is None:
            return
        if source_widget is not None:
            self._alt_magnifier_widget = source_widget

        alt_down = bool(
            getattr(self, "_alt_is_down", False)
            or (QtWidgets.QApplication.keyboardModifiers() & QtCore.Qt.AltModifier)
        )
        if force_active is not None:
            alt_down = bool(force_active)
        if alt_down:
            overlay.start(self._alt_magnifier_widget)
        else:
            overlay.stop()

    def _reset_rocket_buffer(self):
        self._rocket_buffer = ""
        self._rocket_last_key_ts = 0.0

    def _normalize_rocket_text(self, text):
        if not text:
            return ""
        return (text
                .replace('’', "'")
                .replace('`', "'")
                .replace('“', '"')
                .replace('”', '"')
                .lower())

    def _trigger_rocket_launch(self):
        overlay = getattr(self, '_rocket_overlay', None)
        if overlay is None:
            return
        self._cancel_pending_shift_reset()
        overlay.start_animation()
        self._reset_rocket_buffer()

    def _init_action_behavior_controls(self):
        self.keep_behavior = {
            "mode": "overwrite",
            "suffix": "_keep",
            "preserve_original": False,
        }
        self.reject_behavior = {
            "mode": "delete",
            "suffix": "_reject",
            "preserve_original": False,
        }
        self._control_is_down = False
        self._control_candidate = False
        self._control_started_in_window = False
        self._action_behavior_dialog = None

    def _active_popup_or_modal_widget(self):
        app = QtWidgets.QApplication.instance()
        if app is None:
            return None
        for getter_name in ("activePopupWidget", "activeModalWidget"):
            try:
                getter = getattr(app, getter_name)
                widget = getter()
            except Exception:
                widget = None
            if widget is not None and widget is not self:
                return widget
        return None

    def _open_action_behavior_dialog(self):
        existing = getattr(self, "_action_behavior_dialog", None)
        if existing is not None:
            try:
                if existing.isVisible():
                    existing.raise_()
                    existing.activateWindow()
                    return
            except Exception:
                pass

        if self._active_popup_or_modal_widget() is not None:
            return

        dlg = ActionBehaviorDialog(
            keep_behavior=getattr(self, 'keep_behavior', None),
            reject_behavior=getattr(self, 'reject_behavior', None),
            parent=self
        )
        self._action_behavior_dialog = dlg
        try:
            if dlg.exec_() != QtWidgets.QDialog.Accepted:
                return
            vals = dlg.values()
            self.keep_behavior = dict(vals.get("keep", {}))
            self.reject_behavior = dict(vals.get("reject", {}))
        finally:
            self._action_behavior_dialog = None

    def _normalize_suffix_text(self, suffix_text, fallback):
        suffix = (suffix_text or "").strip()
        if not suffix:
            suffix = fallback
        for bad in ('/', '\\'):
            suffix = suffix.replace(bad, '_')
        suffix = suffix.replace(':', '_')
        return suffix

    def _build_suffixed_output_path(self, path, suffix_text, fallback):
        suffix = self._normalize_suffix_text(suffix_text, fallback)
        p = Path(path)
        return str(p.with_name(f"{p.stem}{suffix}{p.suffix}"))

    def _make_unique_output_path(self, path):
        p = Path(path)
        if not p.exists():
            return str(p)
        stem = p.stem
        suffix = p.suffix
        parent = p.parent
        i = 1
        while True:
            cand = parent / f"{stem}_{i}{suffix}"
            if not cand.exists():
                return str(cand)
            i += 1

    def _register_output_alias(self, output_path, dt):
        fname = os.path.basename(output_path)
        vals = [0, 0, 0, 0, 0, 0]
        alias_dt = _clean_log_image_datetime(dt)
        self.log_entries.append((
            fname, alias_dt, OUTPUT_ALIAS_AZIMUTH, OUTPUT_ALIAS_DISTANCE, False, vals,
            "", "", _current_log_logged_datetime(), "",
        ))
        self.processed_files.add(fname)

    def _split_display_to_source_tform(self, idx, src, display_shape=None):
        if display_shape is None:
            display_shape = self._split_display_shape(idx)
        if display_shape is None:
            return None
        try:
            h, w = display_shape[:2]
            L, R, B, T = self.bases[idx]
        except Exception:
            return None
        h = max(1, int(h))
        w = max(1, int(w))

        def _display_world(col, row):
            x = float(L) + (float(col) / max(float(w), 1.0)) * (float(R) - float(L))
            y = float(T) - (float(row) / max(float(h), 1.0)) * (float(T) - float(B))
            return x, y

        src_pts = np.asarray([[0.0, 0.0], [float(w), 0.0], [0.0, float(h)]], dtype=float)
        dst_pts = []
        try:
            inv_transform = ~src.transform
            for col, row in src_pts:
                xw, yw = _display_world(col, row)
                scol, srow = inv_transform * (xw, yw)
                dst_pts.append((float(scol), float(srow)))
            return estimate_transform('affine', src_pts, np.asarray(dst_pts, dtype=float))
        except Exception:
            return None

    def _split_transform_vertices(self, vertices, tform):
        try:
            pts = np.asarray(vertices, dtype=float)
            if pts.ndim != 2 or pts.shape[1] != 2 or pts.shape[0] < 3:
                return []
            out = np.asarray(tform(pts), dtype=float)
            return [(float(col), float(row)) for col, row in out if np.isfinite([col, row]).all()]
        except Exception:
            return []

    def _split_mask_from_vertices_for_shape(self, height, width, vertices):
        height = max(1, int(height))
        width = max(1, int(width))
        mask = np.zeros((height, width), dtype=bool)
        pts = np.asarray(vertices, dtype=float)
        if pts.ndim != 2 or pts.shape[1] != 2 or pts.shape[0] < 3:
            return mask
        finite = np.isfinite(pts).all(axis=1)
        pts = pts[finite]
        if pts.shape[0] < 3:
            return mask
        c0 = max(0, int(math.floor(float(np.nanmin(pts[:, 0])))) - 2)
        c1 = min(width, int(math.ceil(float(np.nanmax(pts[:, 0])))) + 2)
        r0 = max(0, int(math.floor(float(np.nanmin(pts[:, 1])))) - 2)
        r1 = min(height, int(math.ceil(float(np.nanmax(pts[:, 1])))) + 2)
        if c1 <= c0 or r1 <= r0:
            return mask
        local = pts - np.asarray([float(c0), float(r0)], dtype=float)
        box_h = int(r1 - r0)
        box_w = int(c1 - c0)
        rows, cols = np.mgrid[0:box_h, 0:box_w]
        points = np.column_stack((cols.ravel() + 0.5, rows.ravel() + 0.5))
        try:
            inside = MplPath(local).contains_points(points, radius=0.001).reshape((box_h, box_w))
        except Exception:
            return mask
        boundary = np.zeros((box_h, box_w), dtype=bool)
        for index, (x0, y0) in enumerate(local):
            x1, y1 = local[(index + 1) % len(local)]
            _split_draw_line_on_barrier(boundary, x0, y0, x1, y1, radius=0)
        mask[r0:r1, c0:c1] = inside | boundary
        return mask

    def _split_display_tform_to_source_tform(self, display_tform, display_to_source_tform):
        display_tform = _coerce_affine_transform(display_tform)
        display_to_source_tform = _coerce_affine_transform(display_to_source_tform)
        if display_tform is None or display_to_source_tform is None:
            return display_tform
        try:
            bridge = np.asarray(display_to_source_tform.params, dtype=float)
            converted = bridge @ np.asarray(display_tform.params, dtype=float) @ np.linalg.inv(bridge)
            return AffineTransform(matrix=converted)
        except Exception:
            return display_tform

    def _split_apply_restructure_to_path(self, target_path, idx):
        state = self._split_state(idx)
        analysis = state.get("analysis")
        if not isinstance(analysis, SplitRegionAnalysis) or not analysis.regions:
            return

        with rasterio.open(target_path) as src:
            arr = src.read()
            meta = src.meta.copy()
            height, width = int(src.height), int(src.width)
            display_to_source = self._split_display_to_source_tform(idx, src)

        display_to_source = _coerce_affine_transform(display_to_source)
        if display_to_source is None:
            raise RuntimeError("Could not map split panel coordinates back to source raster pixels.")

        masks = {}
        combined_all = np.zeros((height, width), dtype=bool)
        for region in analysis.regions:
            source_vertices = self._split_transform_vertices(region.vertices, display_to_source)
            mask = self._split_mask_from_vertices_for_shape(height, width, source_vertices)
            if mask.shape != (height, width) or not np.any(mask):
                continue
            masks[int(region.label)] = mask
            combined_all |= mask
        if not masks:
            return

        categorical_values = _categorical_raster_categories_for_path(target_path)
        categorical = categorical_values is not None
        order = 0 if categorical else 1
        deleted = set(state.get("deleted_labels", set()) or set())
        source_dtype = arr.dtype
        out = np.full(arr.shape, np.nan, dtype=np.float32)
        nodata = meta.get("nodata")

        for band_index in range(arr.shape[0]):
            source = arr[band_index].astype(np.float32, copy=False)
            valid_source = np.isfinite(source)
            if nodata is not None:
                try:
                    valid_source &= source != float(nodata)
                except Exception:
                    pass

            accum = np.zeros((height, width), dtype=np.float64)
            count = np.zeros((height, width), dtype=np.float32)

            remainder_valid = (~combined_all) & valid_source
            if np.any(remainder_valid):
                accum[remainder_valid] += source[remainder_valid].astype(np.float64)
                count[remainder_valid] += 1.0

            for region in analysis.regions:
                label = int(region.label)
                if label in deleted:
                    continue
                mask = masks.get(label)
                if mask is None or not np.any(mask):
                    continue
                piece = np.full((height, width), np.nan, dtype=np.float32)
                piece_valid = mask & valid_source
                if not np.any(piece_valid):
                    continue
                piece[piece_valid] = source[piece_valid]
                display_tform = self._split_piece_tform(state, label, idx=idx)
                source_tform = self._split_display_tform_to_source_tform(display_tform, display_to_source)
                try:
                    warped = skwarp(
                        piece,
                        inverse_map=source_tform.inverse,
                        output_shape=(height, width),
                        cval=np.nan,
                        preserve_range=True,
                        order=order,
                    )
                    warped_mask = skwarp(
                        mask.astype(np.float32),
                        inverse_map=source_tform.inverse,
                        output_shape=(height, width),
                        cval=0.0,
                        preserve_range=True,
                        order=0,
                    ) > 0.5
                except Exception:
                    warped = piece
                    warped_mask = mask
                valid_warped = warped_mask & np.isfinite(warped)
                if np.any(valid_warped):
                    accum[valid_warped] += np.asarray(warped, dtype=np.float64)[valid_warped]
                    count[valid_warped] += 1.0

            valid_out = count > 0
            if np.any(valid_out):
                out[band_index, valid_out] = (accum[valid_out] / np.maximum(count[valid_out], 1.0)).astype(np.float32)

        if categorical and np.issubdtype(source_dtype, np.integer):
            cloud_zero_is_class = _is_cloud_byte_raster_for_path(target_path)
            declared_nodata = None if cloud_zero_is_class else meta.get('nodata')
            nd = _nodata_for_dtype(source_dtype, declared_nodata)
            info = np.iinfo(source_dtype)
            write_arr = np.empty(arr.shape, dtype=source_dtype)
            for band_index in range(out.shape[0]):
                band = out[band_index]
                nan_mask = ~np.isfinite(band)
                snapped = _snap_to_nearest_source_category(band, categorical_values, preserve_mask=nan_mask)
                snapped = np.where(nan_mask, nd, snapped)
                write_arr[band_index] = np.clip(np.rint(snapped), info.min, info.max).astype(source_dtype)
            meta.update(dtype=str(np.dtype(source_dtype)), nodata=None if cloud_zero_is_class else nd)
        else:
            write_arr = out.astype("float32", copy=False)
            meta.update(dtype="float32", nodata=np.nan)

        tmp = str(target_path) + ".split.tmp"
        with rasterio.open(tmp, "w", **meta) as dst:
            dst.write(write_arr)
        os.replace(tmp, target_path)

    def _apply_keep_transforms_to_path(self, target_path, dx, dy, warp_flag, data, idx=None):
        if idx is not None and self._split_has_restructure(idx):
            self._split_apply_restructure_to_path(target_path, idx)
            return
        tform = data.get('tform') if warp_flag and isinstance(data, dict) else None
        categorical_values = _categorical_raster_categories_for_path(target_path)
        _apply_final_transform_to_path(
            target_path,
            dx,
            dy,
            tform,
            categorical_values=categorical_values,
        )

    def _cancel_pending_shift_reset(self):
        self._pending_shift_reset = False
        self._shift_candidate = False

    def _schedule_shift_reset(self):
        self._pending_shift_reset = True
        self._shift_candidate = True

    def _execute_pending_shift_reset(self):
        if not getattr(self, '_pending_shift_reset', False):
            return
        self._pending_shift_reset = False
        self._shift_candidate = False
        idx = getattr(self, 'active_idx', 0)
        self._do_full_reset(idx)

    def _maybe_consume_rocket_key(self, event):
        phrase = getattr(self, '_rocket_phrase', '')
        if not phrase:
            return False

        if event.isAutoRepeat():
            return False

        blocked_mods = QtCore.Qt.ControlModifier | QtCore.Qt.AltModifier | QtCore.Qt.MetaModifier
        if event.modifiers() & blocked_mods:
            self._reset_rocket_buffer()
            return False

        key = event.key()
        if key in (QtCore.Qt.Key_Backspace, QtCore.Qt.Key_Delete, QtCore.Qt.Key_Escape,
                   QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter, QtCore.Qt.Key_Tab):
            self._reset_rocket_buffer()
            return False

        now = time.monotonic()
        if self._rocket_buffer and (now - self._rocket_last_key_ts) > 2.0:
            self._reset_rocket_buffer()

        text = self._normalize_rocket_text(event.text())
        if not text or not all(ch.isprintable() for ch in text):
            return False

        consumed = False
        swallow_after = int(getattr(self, '_rocket_swallow_after', 2))
        for ch in text:
            if phrase.startswith(self._rocket_buffer + ch):
                self._rocket_buffer = self._rocket_buffer + ch
                consume_this = len(self._rocket_buffer) > swallow_after
            elif phrase.startswith(ch):
                self._rocket_buffer = ch
                consume_this = False
            else:
                self._reset_rocket_buffer()
                consume_this = False

            if self._rocket_buffer == phrase:
                self._trigger_rocket_launch()
                return True

            consumed = consumed or consume_this

        if self._rocket_buffer:
            self._rocket_last_key_ts = now
        return consumed

    def eventFilter(self, obj, event):
        try:
            if self.isVisible():
                same_window = self._object_belongs_to_viewer(obj)
                if same_window:
                    etype = event.type()
                    if etype == QtCore.QEvent.Resize:
                        if obj is self or obj is getattr(self, "canvas", None):
                            self._schedule_panel_geometry_sync(delay_ms=0)

                    if (
                        etype in (
                            QtCore.QEvent.KeyPress,
                            QtCore.QEvent.KeyRelease,
                            QtCore.QEvent.ShortcutOverride,
                        )
                        and self._image_comment_dialog_active(obj)
                    ):
                        self._release_image_shortcut_state_for_text_entry()
                        return False

                    if etype == QtCore.QEvent.KeyPress:
                        key = event.key()
                        blocked_mods = QtCore.Qt.ControlModifier | QtCore.Qt.AltModifier | QtCore.Qt.MetaModifier

                        if key == QtCore.Qt.Key_Shift and not event.isAutoRepeat():
                            self._shift_is_down = True
                            self._shift_started_in_window = True
                            self._shift_candidate = True
                            self._pending_shift_reset = True
                            return True

                        if key == QtCore.Qt.Key_Control and not event.isAutoRepeat():
                            self._control_is_down = True
                            self._control_started_in_window = True
                            self._control_candidate = True
                            return True

                        elif not event.isAutoRepeat():
                            modifier_only = key in (
                                QtCore.Qt.Key_Control, QtCore.Qt.Key_Alt, QtCore.Qt.Key_Meta,
                                QtCore.Qt.Key_CapsLock, QtCore.Qt.Key_NumLock, QtCore.Qt.Key_ScrollLock
                            )
                            text = self._normalize_rocket_text(getattr(event, 'text', lambda: '')())
                            if self._shift_is_down and (text or not modifier_only):
                                self._cancel_pending_shift_reset()
                            if self._control_is_down and (text or not modifier_only):
                                self._control_candidate = False
                            if text and not (event.modifiers() & blocked_mods):
                                if self._maybe_consume_rocket_key(event):
                                    return True
                    elif etype == QtCore.QEvent.KeyRelease:
                        key = event.key()
                        if key == QtCore.Qt.Key_Shift and not event.isAutoRepeat():
                            should_reset = bool(getattr(self, '_shift_started_in_window', False) and getattr(self, '_shift_candidate', False))
                            self._shift_is_down = False
                            self._shift_started_in_window = False
                            self._pending_shift_reset = False
                            self._shift_candidate = False
                            if should_reset:
                                idx = getattr(self, 'active_idx', 0)
                                self._do_full_reset(idx)
                            return True
                        if key == QtCore.Qt.Key_Control and not event.isAutoRepeat():
                            should_open = bool(getattr(self, '_control_started_in_window', False) and getattr(self, '_control_candidate', False))
                            self._control_is_down = False
                            self._control_started_in_window = False
                            self._control_candidate = False
                            if should_open:
                                self._open_action_behavior_dialog()
                            return True
        except Exception:
            pass
        return super().eventFilter(obj, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        overlay = getattr(self, '_rocket_overlay', None)
        if overlay is not None:
            overlay.setGeometry(self.rect())
            if overlay.is_active():
                overlay.raise_()
        try:
            self._apply_main_panel_ui_scale()
        except Exception:
            pass
        self._schedule_panel_geometry_sync(delay_ms=0)

    def __init__(
        self,
        all_files,
        processed_files,
        log_entries,
        persisted_ui_store=None,
        startup_profile_name=None,
        startup_ui_settings=None,
        parent=None,
    ):
        super().__init__(parent)
        self._script_path = os.path.abspath(__file__)
        self.persisted_ui_store = normalize_persisted_ui_store(
            persisted_ui_store if persisted_ui_store is not None else load_persisted_ui_store()
        )
        selected_profile = _find_persisted_ui_profile(self.persisted_ui_store, startup_profile_name)
        self.current_settings_profile_name = str(startup_profile_name or selected_profile["name"]).strip() or selected_profile["name"]
        self.persisted_ui_settings = normalize_persisted_ui_settings(
            startup_ui_settings if startup_ui_settings is not None else selected_profile["settings"]
        )
        self.setWindowTitle("NASA JPL Thermal Viewer")
        startup_geometry = initial_main_window_geometry(self, fraction=0.75)
        self.resize(startup_geometry.size())
        self.move(startup_geometry.topLeft())

        self.theme_mode = str(self.persisted_ui_settings.get("theme_mode", get_app_theme_mode()) or get_app_theme_mode())
        self.theme = self._theme_palette(self.theme_mode)
        self.keyboard_shortcuts_lock_theme = bool(
            self.persisted_ui_settings.get("keyboard_shortcuts_lock_theme", False)
        )
        self.keyboard_shortcuts_theme_mode = normalize_keyboard_shortcuts_theme_mode(
            self.persisted_ui_settings.get("keyboard_shortcuts_theme_mode"),
            "light",
        )
        self.use_theme_nan_color = bool(self.persisted_ui_settings.get("use_theme_nan_color", True))
        self.nan_color = normalize_nan_override_value(
            self.persisted_ui_settings.get("nan_color"),
            normalize_nan_override_value(self._panel_facecolor()),
        )
        self.thermal_alpha = max(0.0, min(1.0, float(self.persisted_ui_settings.get("thermal_alpha", 1.0))))
        self.thermal_blend_mode = normalize_thermal_blend_mode(
            self.persisted_ui_settings.get("thermal_blend_mode"),
            "normal",
        )
        self.thermal_visual_resampling = normalize_visual_resampling(
            self.persisted_ui_settings.get("thermal_visual_resampling"),
            "nearest",
        )
        self.basemap_visual_resampling = normalize_visual_resampling(
            self.persisted_ui_settings.get("basemap_visual_resampling"),
            "nearest",
        )
        self.basemap_resolution_mode = normalize_basemap_resolution_mode(
            self.persisted_ui_settings.get("basemap_resolution_mode"),
            "dynamic",
        )
        self.basemap_color_scaling = normalize_basemap_color_scaling(
            self.persisted_ui_settings.get("basemap_color_scaling"),
            "normal",
        )
        self.basemap_cmap = normalize_basemap_cmap(
            self.persisted_ui_settings.get("basemap_cmap"),
            "gray",
        )
        self.basemap_mode = normalize_basemap_mode(
            self.persisted_ui_settings.get("basemap_mode"),
            "nearest",
        )
        self.basemap_category = normalize_basemap_category(
            self.persisted_ui_settings.get("basemap_category")
        )
        self.shp_linewidth = float(self.persisted_ui_settings.get("shp_linewidth", 1.2))
        self.summary_fontsize = float(self.persisted_ui_settings.get("summary_fontsize", 11.0))
        self.main_panel_text_scale = normalize_main_panel_text_scale(
            self.persisted_ui_store.get("last_main_panel_text_scale", 1.0)
        )
        default_button_preset = KEEP_REJECT_BUTTON_PRESET_BY_ID[DEFAULT_KEEP_REJECT_PRESET_ID]
        self.keep_button_color = normalize_keep_reject_button_color(
            self.persisted_ui_settings.get("keep_button_color"),
            default_button_preset["keep"],
        )
        self.reject_button_color = normalize_keep_reject_button_color(
            self.persisted_ui_settings.get("reject_button_color"),
            default_button_preset["reject"],
        )
        self.keep_reject_button_preset = infer_keep_reject_button_preset(
            self.keep_button_color,
            self.reject_button_color,
        )
        self.keep_reject_button_layout_settings = normalize_keep_reject_button_layout_settings(
            self.persisted_ui_settings.get("keep_reject_button_layout_settings")
        )
        self.sync_zoom_pan = bool(self.persisted_ui_settings.get("sync_zoom_pan", False))
        self.scroll_wheel_pan_multi_enabled = bool(
            self.persisted_ui_settings.get("scroll_wheel_pan_multi_enabled", True)
        )
        self.warp_source_color = normalize_keep_reject_button_color(
            self.persisted_ui_settings.get("warp_source_color"),
            DEFAULT_WARP_SOURCE_COLOR,
        )
        self.warp_target_color = normalize_keep_reject_button_color(
            self.persisted_ui_settings.get("warp_target_color"),
            DEFAULT_WARP_TARGET_COLOR,
        )

        # Central widget and layout
        central = QWidget(self)
        self.setCentralWidget(central)
        self.vbox = QVBoxLayout(central)
        self.vbox.setContentsMargins(0, 0, 0, 0)
        self.vbox.setSpacing(0)
        self._init_menu_bar()

        # Matplotlib Figure/Canvas
        self.fig = Figure(figsize=(23, 8.5), dpi=100)
        self.fig.patch.set_facecolor(self.theme['figure_bg'])
        self.fig.set_constrained_layout(False)
        self.canvas = FigureCanvas(self.fig)
        self.canvas.setStyleSheet(f"background-color: {self.theme['figure_bg']};")
        self.vbox.addWidget(self.canvas, 1)

        self.footer_frame = QtWidgets.QFrame(central)
        force_widget_ui_scale_100(self.footer_frame)
        self.footer_frame.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.footer_frame.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Fixed)
        # Reserve a stable footer band so changing stats font size does not
        # change the canvas height and shrink the panels.
        self.footer_frame.setFixedHeight(84)
        self.footer_layout = QtWidgets.QVBoxLayout(self.footer_frame)
        self.footer_layout.setContentsMargins(12, 2, 12, 4)
        self.footer_layout.setSpacing(0)
        self.footer_label = QtWidgets.QLabel("", self.footer_frame)
        force_widget_ui_scale_100(self.footer_label)
        self.footer_label.setTextFormat(QtCore.Qt.PlainText)
        self.footer_label.setWordWrap(False)
        self.footer_label.setAlignment(QtCore.Qt.AlignHCenter | QtCore.Qt.AlignVCenter)
        self.footer_label.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Fixed)
        self.footer_layout.addWidget(self.footer_label)
        self.vbox.addWidget(self.footer_frame, 0)

        # ---- Make sure the canvas receives keyboard focus ---- #
        try:
            self.canvas.setFocusPolicy(QtCore.Qt.StrongFocus)
        except Exception:
            # Qt6 name
            self.canvas.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.canvas.setFocus()

        self._init_action_behavior_controls()
        self.keep_behavior = dict(self.persisted_ui_settings.get("keep_behavior", self.keep_behavior))
        self.reject_behavior = dict(self.persisted_ui_settings.get("reject_behavior", self.reject_behavior))
        self.filename_dt_substring = normalize_filename_datetime_substring(
            self.persisted_ui_settings.get("filename_dt_substring")
        )
        self.filename_dt_pattern = normalize_filename_datetime_pattern(
            self.persisted_ui_settings.get("filename_dt_pattern")
        )

        self._comment_dialog_active = False
        self._active_comment_dialog = None

        # Hidden typed-phrase easter egg (independent visual overlay)
        self._init_rocket_easter_egg()

        # Data prepared from constructor args
        self.all_files = all_files
        self.processed_files = set(processed_files)
        self.log_entries = list(log_entries)

        # Determine files to process
        self.to_process = [f for f in self.all_files if os.path.basename(f) not in self.processed_files]
        self.total = len(self.to_process)
        self.overall_target = summarize_log_entries(self.log_entries)['processed'] + self.total
        if self.total == 0:
            QtWidgets.QMessageBox.information(self, "Info", "No new files to process.")
            QtCore.QTimer.singleShot(0, self.close)
            return

        # session counters
        self.session = {"as_is":0, "geo":0, "warp":0, "reject":0}
        self.session_processed = 0
        self.pending_comments = {}
        self.user_comment_flags = load_user_comment_flags()

        # Load + color-select shapefile(s) (no CSV writes)
        self.shp_primary = None
        self.shp_primary_name = None
        self.shp_primary_color = normalize_picker_color_name(
            self.persisted_ui_settings.get("shp_primary_color"),
            "cyan",
        )
        self.shp_overlays = []   # list of {"name": str, "gdf": GeoDataFrame, "color": str}
        self.basemap_path = None
        self.basemap_name = ""
        self.basemap_images = {}
        self._basemap_display_cache = {}
        self._dynamic_basemap_view_keys = {}
        self._basemap_warned_paths = set()
        self.panel_basemap_paths = {}
        self.panel_basemap_delta_days = {}

        self._load_startup_basemap_from_settings()
        self._load_startup_shapefiles_from_settings()

        self._apply_saved_vector_colors()

        # Panel grid layout (\ to change)
        self.grid_cols = int(self.persisted_ui_settings.get("grid_cols", 3))
        self.grid_rows = int(self.persisted_ui_settings.get("grid_rows", 1))
        self.panel_layout_settings = normalize_panel_layout_settings(
            self.persisted_ui_settings.get("panel_layout_settings")
        )
        self.n_pan = int(self.grid_cols * self.grid_rows)  # number of panel slots (1..25)

        # Fill panel slots; pad with None if fewer files remain
        initial = self.to_process[:self.n_pan]
        self.current = list(initial) + [None] * (self.n_pan - len(initial))
        self.queue = self.to_process[len(initial):]

        # Build axes grid
        self._build_axes_grid(clear_fig=False)

        # Track last/active axes index for key events
        self.active_idx = 0

        # Progress footer lives below the canvas so panel layout is unaffected.
        self.prog = None
        self._update_progress_footer()

        # State containers
        self.bases = {}

        self.offsets = {i: [0, 0] for i in range(self.n_pan)}
        self.images = {i: None for i in range(self.n_pan)}
        self.basemap_images = {i: None for i in range(self.n_pan)}
        self._dynamic_basemap_view_keys = {}
        self.panel_basemap_paths = {}
        self.panel_basemap_delta_days = {}
        self.images_data = {i: None for i in range(self.n_pan)}
        self.current_display_data = {i: None for i in range(self.n_pan)}
        self.current_display_ranges = {i: (None, None) for i in range(self.n_pan)}
        self.panel_views = {}
        self.split_mode = False
        self._split_drag_state = None
        self.split_data = {i: self._new_split_state(self.current[i]) for i in range(self.n_pan)}

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
        self.base_multiplier = max(1, int(self.persisted_ui_settings.get("base_multiplier", 1)))
        self.scale_modifier = max(1e-3, float(self.persisted_ui_settings.get("scale_modifier", 1.0)))
        self.pan_factor = 0.001 * self.base_multiplier * self.scale_modifier
        self.aoi_bounds = None  # (minx, miny, maxx, maxy) in world coords
        self.cmap_mode = str(self.persisted_ui_settings.get("cmap_mode", "gray") or "gray")
        self.alt_cmap = str(self.persisted_ui_settings.get("alt_cmap", "magma") or "magma")  # used by [R] toggle (gray <-> alt)
        self._all_cmaps = None     # cached list for [Tab] display-options picker
        self.edge_cache = {}                                      # i -> {"data": ndarray, "range": (vmin, vmax)}
        self.data_ranges = {}                                     # i -> (vmin, vmax) for base image
        self.global_edge_mode = bool(self.persisted_ui_settings.get("global_edge_mode", False))
        contrast_rel = list(self.persisted_ui_settings.get("global_contrast_rel", [0.0, 1.0]))
        self.global_contrast_rel = (
            float(contrast_rel[0]),
            max(1e-12, float(contrast_rel[1])),
        )
        self.global_gamma = max(1e-12, float(self.persisted_ui_settings.get("global_gamma", 1.0)))
        self.title_fontsize = max(8.0, min(40.0, float(self.persisted_ui_settings.get("title_fontsize", 18.0))))
        self._startup_panel_paint_blocked = bool(self._basemap_loaded())
        self._pending_layout_autofit = True
        self._thermal_transparency_keys_down = set()
        self._thermal_alpha_hold_hidden = False
        self._thermal_alpha_hold_restore = None
        self._thermal_transparency_suppress_until_clear = False
        if self._startup_panel_paint_blocked:
            try:
                self.canvas.setUpdatesEnabled(False)
            except Exception:
                pass

        # Draw initial panels
        for i in range(self.n_pan):
            self.draw(i)

        # Mouse + keyboard + motion events
        self.canvas.mpl_connect('button_press_event', self.on_button_press)
        self.canvas.mpl_connect('button_release_event', self.on_button_release)
        self.canvas.mpl_connect('key_press_event', self.on_key)
        self.canvas.mpl_connect('key_release_event', self.on_key_release)
        self.canvas.mpl_connect('motion_notify_event', self.on_motion)
        self.canvas.mpl_connect('scroll_event', self.on_scroll)

        # SHIFT reset is handled from the app-wide event filter on key release
        self._shortcut_shift_reset = None

        # Global TAB shortcut: open display options
        self._shortcut_tab_layout = QtWidgets.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Tab), self)
        self._shortcut_tab_layout.setContext(QtCore.Qt.ApplicationShortcut)
        self._shortcut_tab_layout.setAutoRepeat(False)
        self._shortcut_tab_layout.activated.connect(self._open_colormap_dialog)

        # Global backslash shortcut: open the panel layout dialog
        self._shortcut_backslash_layout = QtWidgets.QShortcut(QtGui.QKeySequence('\\'), self)
        self._shortcut_backslash_layout.setContext(QtCore.Qt.ApplicationShortcut)
        self._shortcut_backslash_layout.setAutoRepeat(False)
        self._shortcut_backslash_layout.activated.connect(self._open_layout_dialog)

        # Build Keep/Reject buttons under each panel
        self.add_keep_reject_buttons()

        # Disable any toolbar pan if present (we didn't add a toolbar, but be safe)
        self.kill_toolbar_pan_only()
        self._apply_theme()

        # Ensure log is flushed when app quits (belt‑and‑suspenders)
        app = QApplication.instance()
        if app is not None:
            try:
                app.aboutToQuit.connect(self.flush_log)
            except Exception:
                pass

        # Repaint
        self.fig.set_constrained_layout(False)
        self.canvas.draw_idle()
        self._schedule_panel_geometry_sync(delay_ms=0)

    def _load_startup_shapefiles_from_settings(self):
        self.shp_primary = None
        self.shp_primary_name = None
        self.shp_overlays = []

        try:
            shp_paths = discover_startup_shapefiles()
            if not shp_paths:
                return

            path_by_name = {}
            for path in shp_paths:
                path_by_name.setdefault(os.path.basename(path), path)

            primary_name = normalize_persisted_shapefile_name(
                self.persisted_ui_settings.get("shp_primary_name")
            )
            primary_path = path_by_name.get(primary_name) if primary_name else None

            if primary_name and primary_path is None:
                print(f"[WARN] Saved primary shapefile not found in current folder: {primary_name}")
            elif primary_path:
                try:
                    self.shp_primary = gpd.read_file(primary_path).to_crs(epsg=4326)
                    self.shp_primary_name = os.path.basename(primary_path)
                    self.shp_primary_color = normalize_picker_color_name(
                        self.persisted_ui_settings.get("shp_primary_color"),
                        "cyan",
                    )
                except Exception as e:
                    print(f"[WARN] Could not read primary shapefile '{primary_name}': {e}")
                    self.shp_primary = None
                    self.shp_primary_name = None

            seen_names = {primary_name.lower()} if primary_name else set()
            for item in list(self.persisted_ui_settings.get("shp_overlay_colors", []) or []):
                if not isinstance(item, dict):
                    continue
                overlay_name = normalize_persisted_shapefile_name(item.get("name"))
                if not overlay_name:
                    continue
                overlay_key = overlay_name.lower()
                if overlay_key in seen_names:
                    continue
                seen_names.add(overlay_key)

                overlay_path = path_by_name.get(overlay_name)
                if overlay_path is None:
                    print(f"[WARN] Saved overlay shapefile not found in current folder: {overlay_name}")
                    continue

                try:
                    gdf = gpd.read_file(overlay_path).to_crs(epsg=4326)
                    self.shp_overlays.append({
                        "name": os.path.basename(overlay_path),
                        "gdf": gdf,
                        "color": normalize_picker_color_name(item.get("color"), "dodgerblue"),
                    })
                except Exception as e:
                    print(f"[WARN] Could not read overlay shapefile '{overlay_name}': {e}")
        except Exception as e:
            print(f"[WARN] Startup shapefile loading failed: {e}")
            self.shp_primary = None
            self.shp_primary_name = None
            self.shp_overlays = []

    def _discover_runtime_basemap_paths(self):
        try:
            return discover_basemap_paths()
        except Exception:
            return []

    def _set_basemap_path(self, path):
        old_path = os.path.abspath(str(getattr(self, "basemap_path", "") or "")) if getattr(self, "basemap_path", None) else ""
        raw_path = str(path or "").strip()
        if not raw_path:
            self.basemap_path = None
            self.basemap_name = ""
        else:
            abs_path = os.path.abspath(raw_path)
            if not os.path.isfile(abs_path):
                self.basemap_path = None
                self.basemap_name = ""
            else:
                self.basemap_path = abs_path
                self.basemap_name = os.path.basename(abs_path)
        new_path = os.path.abspath(str(getattr(self, "basemap_path", "") or "")) if getattr(self, "basemap_path", None) else ""
        changed = old_path != new_path
        if changed:
            try:
                self._basemap_display_cache.clear()
            except Exception:
                self._basemap_display_cache = {}
            try:
                self._dynamic_basemap_view_keys.clear()
            except Exception:
                self._dynamic_basemap_view_keys = {}
            if not new_path:
                self.thermal_alpha = 1.0
                self.thermal_blend_mode = "normal"
                try:
                    self._apply_thermal_alpha()
                except Exception:
                    pass
            self._update_blend_mode_status_label()
        return changed

    def _load_startup_basemap_from_settings(self):
        self.basemap_path = None
        self.basemap_name = ""
        try:
            self.basemap_mode = normalize_basemap_mode(
                self.persisted_ui_settings.get("basemap_mode"),
                "nearest",
            )
            self.basemap_category = normalize_basemap_category(
                self.persisted_ui_settings.get("basemap_category")
            )
            basemap_paths = discover_basemap_paths()
            categories = basemap_categories_from_paths(basemap_paths)
            if not self.basemap_category and categories:
                self.basemap_category = categories[0]
            if self.basemap_mode == "nearest":
                if not self.basemap_category and categories:
                    self.basemap_category = categories[0]
                if self.basemap_category and not basemap_paths_for_category(basemap_paths, self.basemap_category):
                    print(f"[WARN] Saved basemap category not found in {BASEMAP_FOLDER_NAME}: {self.basemap_category}")
                    self.basemap_category = categories[0] if categories else ""
                return

            basemap_name = normalize_persisted_basemap_name(
                self.persisted_ui_settings.get("basemap_name")
            )
            if not basemap_name:
                return
            path_by_name = {}
            for path in basemap_paths:
                path_by_name.setdefault(os.path.basename(path), path)
            basemap_path = path_by_name.get(basemap_name)
            if basemap_path is None:
                print(f"[WARN] Saved basemap not found in {BASEMAP_FOLDER_NAME}: {basemap_name}")
                return
            self._set_basemap_path(basemap_path)
        except Exception as e:
            print(f"[WARN] Startup basemap loading failed: {e}")
            self.basemap_path = None
            self.basemap_name = ""

    def _basemap_resolution_mode(self):
        return normalize_basemap_resolution_mode(
            getattr(self, "basemap_resolution_mode", "dynamic"),
            "dynamic",
        )

    def _panel_pixel_shape(self, idx, fallback=(900, 600)):
        try:
            ax = self.axes[idx]
            pos = ax.get_position()
            fig_w = max(float(self.fig.bbox.width), 1.0)
            fig_h = max(float(self.fig.bbox.height), 1.0)
            width = max(1, int(round(float(pos.width) * fig_w)))
            height = max(1, int(round(float(pos.height) * fig_h)))
            return height, width
        except Exception:
            try:
                width, height = fallback
                return max(1, int(height)), max(1, int(width))
            except Exception:
                return 600, 900

    def _current_basemap_view_extent(self, ax):
        try:
            x0, x1 = sorted(float(v) for v in ax.get_xlim())
            y0, y1 = sorted(float(v) for v in ax.get_ylim())
        except Exception:
            return None
        if not np.isfinite([x0, x1, y0, y1]).all():
            return None
        if abs(x1 - x0) <= 1e-12 or abs(y1 - y0) <= 1e-12:
            return None
        return x0, y0, x1, y1

    def _dynamic_basemap_request_key(self, idx, ax):
        extent = self._current_basemap_view_extent(ax)
        if extent is None:
            return None
        shape = self._panel_pixel_shape(idx)
        try:
            return (
                os.path.abspath(self._basemap_path_for_panel(idx) or ""),
                tuple(round(float(v), 10) for v in extent),
                (int(shape[0]), int(shape[1])),
                normalize_visual_resampling(getattr(self, "basemap_visual_resampling", "nearest")),
            )
        except Exception:
            return None

    def _read_basemap_for_display(self, path, max_dim=None, window_extent=None, out_shape=None):
        abs_path = os.path.abspath(str(path))
        try:
            mtime = os.path.getmtime(abs_path)
        except Exception:
            mtime = 0.0
        resampling_name = normalize_visual_resampling(
            getattr(self, "basemap_visual_resampling", "nearest")
        )
        rgb_basemap = basemap_path_is_rgb(abs_path)
        max_dim_key = 0 if max_dim is None else int(max_dim)
        if window_extent is not None:
            try:
                window_key = tuple(round(float(v), 10) for v in window_extent)
            except Exception:
                window_key = None
        else:
            window_key = None
        if out_shape is not None:
            try:
                shape_key = (int(out_shape[0]), int(out_shape[1]))
            except Exception:
                shape_key = None
        else:
            shape_key = None
        key = (abs_path, max_dim_key, resampling_name, window_key, shape_key, bool(rgb_basemap), float(mtime))
        cache = getattr(self, "_basemap_display_cache", None)
        if cache is None:
            cache = {}
            self._basemap_display_cache = cache
        if key in cache:
            return cache[key]

        with rasterio.Env(GDAL_NUM_THREADS="ALL_CPUS"):
            with rasterio.open(abs_path, sharing=False) as src:
                read_indexes = [1, 2, 3] if rgb_basemap and int(src.count) >= 3 else 1
                if window_extent is not None:
                    left, bottom, right, top = [float(v) for v in window_extent]
                    if out_shape is None:
                        out_h, out_w = self._panel_pixel_shape(getattr(self, "active_idx", 0))
                    else:
                        out_h = max(1, int(out_shape[0]))
                        out_w = max(1, int(out_shape[1]))
                    win = src.window(left, bottom, right, top)
                    read_shape = (3, out_h, out_w) if isinstance(read_indexes, list) else (out_h, out_w)
                    arr = src.read(
                        read_indexes,
                        window=win,
                        out_shape=read_shape,
                        resampling=visual_resampling_rasterio(resampling_name),
                        masked=True,
                        boundless=True,
                    )
                    bounds = (left, bottom, right, top)
                elif max_dim is None or int(max_dim) <= 0:
                    arr = src.read(read_indexes, masked=True)
                    bounds = (src.bounds.left, src.bounds.bottom, src.bounds.right, src.bounds.top)
                else:
                    scale = max(1, int(np.ceil(max(src.width, src.height) / float(max_dim))))
                    out_w = max(1, int(src.width) // scale)
                    out_h = max(1, int(src.height) // scale)
                    read_shape = (3, out_h, out_w) if isinstance(read_indexes, list) else (out_h, out_w)
                    arr = src.read(
                        read_indexes,
                        out_shape=read_shape,
                        resampling=visual_resampling_rasterio(resampling_name),
                        masked=True,
                    )
                    bounds = (src.bounds.left, src.bounds.bottom, src.bounds.right, src.bounds.top)

        arr = apply_basemap_product_nodata_mask(arr, abs_path)

        if rgb_basemap and getattr(arr, "ndim", 0) == 3:
            mask = np.ma.getmaskarray(arr) if np.ma.isMaskedArray(arr) else None
            data = arr.astype("float32", copy=False).filled(0.0) if np.ma.isMaskedArray(arr) else arr.astype("float32", copy=False)
            data = np.moveaxis(data[:3], 0, -1)
            finite = np.isfinite(data).all(axis=2)
            if mask is not None:
                valid = ~np.any(np.moveaxis(mask[:3], 0, -1), axis=2)
            else:
                valid = finite
            max_val = float(np.nanmax(data)) if np.isfinite(data).any() else 1.0
            if max_val > 1.0:
                scale = 255.0 if max_val <= 255.0 else max_val
                data = data / scale
            data = np.clip(data, 0.0, 1.0)
            rgba = np.ones(data.shape[:2] + (4,), dtype="float32")
            rgba[..., :3] = data
            rgba[..., 3] = np.where(valid & finite, 1.0, 0.0).astype("float32")
            arr = rgba
            vmin, vmax = 0.0, 1.0
        elif np.ma.isMaskedArray(arr):
            arr = arr.astype("float32", copy=False).filled(np.nan)
            vmin, vmax = basemap_scalar_display_limits(arr)
        else:
            arr = arr.astype("float32", copy=False)
            vmin, vmax = basemap_scalar_display_limits(arr)

        result = (arr, bounds, vmin, vmax)
        cache.clear()
        cache[key] = result
        return result

    def _draw_basemap(self, idx, ax):
        basemap_path = self._basemap_path_for_panel(idx)
        if not basemap_path:
            return
        try:
            try:
                saved_xlim = ax.get_xlim()
                saved_ylim = ax.get_ylim()
                autoscale_x = bool(ax.get_autoscalex_on())
                autoscale_y = bool(ax.get_autoscaley_on())
                ax.set_autoscalex_on(False)
                ax.set_autoscaley_on(False)
            except Exception:
                saved_xlim = saved_ylim = None
                autoscale_x = autoscale_y = None

            try:
                saved_datalim = ax.dataLim.frozen()
            except Exception:
                saved_datalim = None

            resolution_mode = self._basemap_resolution_mode()
            read_kwargs = {}
            dynamic_key = None
            if resolution_mode == "dynamic":
                extent = self._current_basemap_view_extent(ax)
                if extent is not None:
                    read_kwargs["window_extent"] = extent
                    read_kwargs["out_shape"] = self._panel_pixel_shape(idx)
                    dynamic_key = self._dynamic_basemap_request_key(idx, ax)

            arr, (left, bottom, right, top), vmin, vmax = self._read_basemap_for_display(
                basemap_path,
                **read_kwargs,
            )
            image_kwargs = {
                "extent": [left, right, bottom, top],
                "origin": "upper",
                "zorder": 0,
                "interpolation": visual_resampling_mpl_interpolation(
                    getattr(self, "basemap_visual_resampling", "nearest")
                ),
                "aspect": "auto",
                "clip_on": True,
            }
            if not (getattr(arr, "ndim", 0) == 3 and arr.shape[-1] in (3, 4)):
                image_kwargs["cmap"] = self._basemap_cmap()
                image_kwargs["norm"] = mcolors.Normalize(vmin=vmin, vmax=vmax)
            img = ax.imshow(arr, **image_kwargs)
            try:
                img.set_gid("basemap_image")
            except Exception:
                pass
            if hasattr(self, "basemap_images"):
                self.basemap_images[idx] = img
            if resolution_mode == "dynamic" and dynamic_key is not None:
                if not hasattr(self, "_dynamic_basemap_view_keys"):
                    self._dynamic_basemap_view_keys = {}
                self._dynamic_basemap_view_keys[idx] = dynamic_key
            else:
                try:
                    getattr(self, "_dynamic_basemap_view_keys", {}).pop(idx, None)
                except Exception:
                    pass
            if saved_datalim is not None:
                try:
                    ax.dataLim.set_points(saved_datalim.get_points())
                except Exception:
                    pass
            if saved_xlim is not None and saved_ylim is not None:
                try:
                    ax.set_xlim(*saved_xlim)
                    ax.set_ylim(*saved_ylim)
                except Exception:
                    pass
            if autoscale_x is not None and autoscale_y is not None:
                try:
                    ax.set_autoscalex_on(autoscale_x)
                    ax.set_autoscaley_on(autoscale_y)
                except Exception:
                    pass
        except Exception as e:
            warned = getattr(self, "_basemap_warned_paths", set())
            path_key = str(getattr(self, "basemap_path", ""))
            if idx is not None:
                path_key = self._basemap_path_for_panel(idx) or path_key
            if path_key not in warned:
                print(f"[WARN] Could not draw basemap '{os.path.basename(path_key)}': {e}")
                warned.add(path_key)
                self._basemap_warned_paths = warned

    def _clear_basemap_underlay(self, idx, ax):
        for img in list(getattr(ax, "images", [])):
            try:
                if img.get_gid() == "basemap_image":
                    img.remove()
            except Exception:
                pass
        if hasattr(self, "basemap_images"):
            self.basemap_images[idx] = None
        try:
            getattr(self, "_dynamic_basemap_view_keys", {}).pop(idx, None)
        except Exception:
            pass

    def _refresh_dynamic_basemap_underlay(self, idx, draw=False, force=False):
        if self._basemap_resolution_mode() != "dynamic":
            return
        if getattr(self, "_refreshing_dynamic_basemap", False):
            return
        if not self._basemap_loaded(idx) or not self._panel_has_image(idx):
            return
        try:
            ax = self.axes[idx]
        except Exception:
            return
        if getattr(self, "basemap_images", {}).get(idx) is None:
            return
        request_key = self._dynamic_basemap_request_key(idx, ax)
        if request_key is not None and not bool(force):
            try:
                if getattr(self, "_dynamic_basemap_view_keys", {}).get(idx) == request_key:
                    return
            except Exception:
                pass

        self._refreshing_dynamic_basemap = True
        try:
            self._clear_basemap_underlay(idx, ax)
            self._draw_basemap(idx, ax)
            if self._thermal_blend_mode_active(idx):
                self._refresh_panel_thermal_display(idx)
            if draw:
                try:
                    self.canvas.draw_idle()
                except Exception:
                    pass
        finally:
            self._refreshing_dynamic_basemap = False

    def _refresh_basemap_underlays_all(self, refresh_thermal=True):
        for idx, ax in enumerate(getattr(self, "axes", [])):
            if idx >= getattr(self, "n_pan", 0) or not self._panel_has_image(idx):
                continue
            try:
                saved_xlim = ax.get_xlim()
                saved_ylim = ax.get_ylim()
            except Exception:
                saved_xlim = saved_ylim = None
            try:
                saved_pos = ax.get_position().frozen()
            except Exception:
                saved_pos = None
            try:
                autoscale_x = bool(ax.get_autoscalex_on())
                autoscale_y = bool(ax.get_autoscaley_on())
                ax.set_autoscalex_on(False)
                ax.set_autoscaley_on(False)
            except Exception:
                autoscale_x = autoscale_y = None

            self._clear_basemap_underlay(idx, ax)
            self._select_basemap_for_panel(idx, self.current[idx])
            self._draw_basemap(idx, ax)
            if refresh_thermal:
                self._refresh_panel_thermal_display(idx)
            self._set_basemap_delta_label(ax, self._basemap_delta_label_for_panel(idx))

            if saved_pos is not None:
                try:
                    ax.set_position(saved_pos)
                except Exception:
                    pass
            if saved_xlim is not None and saved_ylim is not None:
                try:
                    ax.set_xlim(*saved_xlim)
                    ax.set_ylim(*saved_ylim)
                except Exception:
                    pass
            if autoscale_x is not None and autoscale_y is not None:
                try:
                    ax.set_autoscalex_on(autoscale_x)
                    ax.set_autoscaley_on(autoscale_y)
                except Exception:
                    pass
        self._update_blend_mode_status_label()
        self.canvas.draw_idle()

    def _apply_visual_resampling_to_current_artists(self, thermal=True, basemap=True, draw=False):
        if thermal:
            interpolation = visual_resampling_mpl_interpolation(
                getattr(self, "thermal_visual_resampling", "nearest")
            )
            for img in list(getattr(self, "images", {}).values()):
                try:
                    if img is not None:
                        img.set_interpolation(interpolation)
                except Exception:
                    pass

        if basemap:
            interpolation = visual_resampling_mpl_interpolation(
                getattr(self, "basemap_visual_resampling", "nearest")
            )
            for img in list(getattr(self, "basemap_images", {}).values()):
                try:
                    if img is not None:
                        img.set_interpolation(interpolation)
                except Exception:
                    pass

        if draw:
            try:
                self.canvas.draw_idle()
            except Exception:
                pass

    def _capture_panel_views(self):
        views = {}
        for idx, ax in enumerate(getattr(self, "axes", [])):
            if idx >= getattr(self, "n_pan", 0) or not self._panel_has_image(idx):
                continue
            try:
                xlim = tuple(float(v) for v in ax.get_xlim())
                ylim = tuple(float(v) for v in ax.get_ylim())
            except Exception:
                continue
            if len(xlim) != 2 or len(ylim) != 2:
                continue
            if not np.isfinite([xlim[0], xlim[1], ylim[0], ylim[1]]).all():
                continue
            views[idx] = (xlim, ylim)
        return views

    def _restore_panel_views(self, views):
        if not views:
            return
        for idx, view in dict(views).items():
            if idx >= getattr(self, "n_pan", 0) or not self._panel_has_image(idx):
                continue
            try:
                ax = self.axes[idx]
                xlim, ylim = view
                if not self._set_panel_view_exact(ax, xlim, ylim, idx=idx):
                    self._set_panel_view(ax, xlim, ylim, remember=True, sync=False)
            except Exception:
                pass

    def _redraw_panels_preserving_views(self):
        preserved_views = self._capture_panel_views()
        for idx in range(getattr(self, "n_pan", 0)):
            self.draw(idx, refresh_selectors=False)
            self._restore_panel_views({idx: preserved_views[idx]} if idx in preserved_views else {})
        self.ensure_selectors()
        self.kill_toolbar_tools()
        self.canvas.draw_idle()

    def _apply_thermal_alpha(self):
        alpha = max(0.0, min(1.0, float(getattr(self, "thermal_alpha", 1.0))))
        self.thermal_alpha = alpha
        if self._thermal_blend_mode_active():
            self._refresh_all_thermal_displays()
            return
        for img in getattr(self, "images", {}).values():
            try:
                if img is not None:
                    img.set_alpha(alpha)
            except Exception:
                pass
        self._update_blend_mode_status_label()

    def _adjust_thermal_transparency(self, delta_transparency):
        alpha = max(0.0, min(1.0, float(getattr(self, "thermal_alpha", 1.0)) - float(delta_transparency)))
        self.thermal_alpha = alpha
        self._apply_thermal_alpha()
        try:
            self.statusBar().clearMessage()
        except Exception:
            pass
        self.canvas.draw_idle()

    def _thermal_transparency_key_token(self, key):
        text = str(key or "").strip().lower()
        if text in ('[', 'bracketleft', 'leftbracket'):
            return "opacity_plus"
        if text in (';', 'semicolon'):
            return "opacity_minus"
        return None

    def _hide_thermal_layer_while_transparency_keys_held(self):
        if bool(getattr(self, "_thermal_alpha_hold_hidden", False)):
            return
        restore_alpha = getattr(self, "_thermal_alpha_hold_restore", None)
        if restore_alpha is None:
            restore_alpha = getattr(self, "thermal_alpha", 1.0)
        self._thermal_alpha_hold_restore = max(0.0, min(1.0, float(restore_alpha)))
        self._thermal_alpha_hold_hidden = True
        self.thermal_alpha = 0.0
        self._apply_thermal_alpha()
        try:
            self.statusBar().clearMessage()
        except Exception:
            pass
        self.canvas.draw_idle()

    def _restore_thermal_layer_after_transparency_key_hold(self):
        if not bool(getattr(self, "_thermal_alpha_hold_hidden", False)):
            self._thermal_alpha_hold_restore = None
            return
        restore_alpha = getattr(self, "_thermal_alpha_hold_restore", None)
        try:
            restore_alpha = float(restore_alpha)
        except Exception:
            restore_alpha = 1.0
        self._thermal_alpha_hold_hidden = False
        self._thermal_alpha_hold_restore = None
        self._thermal_transparency_suppress_until_clear = True
        self.thermal_alpha = max(0.0, min(1.0, restore_alpha))
        self._apply_thermal_alpha()
        self.canvas.draw_idle()

    def _handle_thermal_transparency_key_press(self, key, delta_transparency):
        token = self._thermal_transparency_key_token(key)
        if token is None:
            return False
        keys_down = getattr(self, "_thermal_transparency_keys_down", None)
        if not isinstance(keys_down, set):
            keys_down = set()
            self._thermal_transparency_keys_down = keys_down
        if not keys_down:
            self._thermal_alpha_hold_restore = max(
                0.0,
                min(1.0, float(getattr(self, "thermal_alpha", 1.0))),
            )
        keys_down.add(token)
        if {"opacity_plus", "opacity_minus"}.issubset(keys_down):
            self._thermal_transparency_suppress_until_clear = False
            self._hide_thermal_layer_while_transparency_keys_held()
            return True
        if bool(getattr(self, "_thermal_transparency_suppress_until_clear", False)):
            return True
        self._adjust_thermal_transparency(delta_transparency)
        return True

    def _release_thermal_transparency_key(self, key):
        token = self._thermal_transparency_key_token(key)
        if token is None:
            return
        keys_down = getattr(self, "_thermal_transparency_keys_down", None)
        if isinstance(keys_down, set):
            keys_down.discard(token)
            if not keys_down:
                self._thermal_transparency_suppress_until_clear = False
        if not {"opacity_plus", "opacity_minus"}.issubset(keys_down or set()):
            self._restore_thermal_layer_after_transparency_key_hold()

    def _release_all_thermal_transparency_keys(self):
        keys_down = getattr(self, "_thermal_transparency_keys_down", None)
        if isinstance(keys_down, set):
            keys_down.clear()
        self._restore_thermal_layer_after_transparency_key_hold()
        self._thermal_transparency_suppress_until_clear = False

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
        app = QApplication.instance()
        if app is not None:
            try:
                app.removeEventFilter(self)
            except Exception:
                pass
        super().closeEvent(event)

    def _init_menu_bar(self):
        menu_bar = self.menuBar()
        try:
            menu_bar.setNativeMenuBar(False)
        except Exception:
            pass
        force_widget_ui_scale_100(menu_bar)

        self._settings_profiles_menu = menu_bar.addMenu("Save/Load Settings")
        force_widget_ui_scale_100(self._settings_profiles_menu)

        self._save_settings_profile_action = QtWidgets.QAction("Save Current Settings...", self)
        self._save_settings_profile_action.setShortcut(QtGui.QKeySequence("Ctrl+Shift+S"))
        self._save_settings_profile_action.setStatusTip("Save the current viewer settings as a named profile in this Python file.")
        self._save_settings_profile_action.triggered.connect(self._save_settings_profile_interactive)
        self._settings_profiles_menu.addAction(self._save_settings_profile_action)

        self._load_settings_profile_action = QtWidgets.QAction("Load Saved Settings...", self)
        self._load_settings_profile_action.setStatusTip("Load one of the saved named settings profiles.")
        self._load_settings_profile_action.triggered.connect(self._load_settings_profile_interactive)
        self._settings_profiles_menu.addAction(self._load_settings_profile_action)

        self._import_export_settings_profile_action = QtWidgets.QAction("Import/Export Settings...", self)
        self._import_export_settings_profile_action.setStatusTip("Share a saved settings profile through a GeoViewer settings text file.")
        self._import_export_settings_profile_action.triggered.connect(self._import_export_settings_profile_interactive)
        self._settings_profiles_menu.addAction(self._import_export_settings_profile_action)

        self._default_settings_profile_action = QtWidgets.QAction("Choose Default Settings...", self)
        self._default_settings_profile_action.setStatusTip("Choose which saved profile should load automatically next time.")
        self._default_settings_profile_action.triggered.connect(self._choose_default_settings_profile_interactive)
        self._settings_profiles_menu.addAction(self._default_settings_profile_action)

        self._init_ui_scale_menu(menu_bar)

        self._keyboard_shortcuts_corner_bar = QtWidgets.QMenuBar(menu_bar)
        try:
            self._keyboard_shortcuts_corner_bar.setNativeMenuBar(False)
        except Exception:
            pass
        force_widget_ui_scale_100(self._keyboard_shortcuts_corner_bar)
        self._keyboard_shortcuts_corner_bar.setFont(menu_bar.font())
        self._geoviewer_info_action = self._keyboard_shortcuts_corner_bar.addAction("Info")
        self._geoviewer_info_action.setStatusTip("Open GeoViewer overview and workflow information.")
        self._geoviewer_info_action.triggered.connect(self._open_geoviewer_info_dialog)
        self._keyboard_shortcuts_action = self._keyboard_shortcuts_corner_bar.addAction("Keyboard Shortcuts")
        self._keyboard_shortcuts_action.triggered.connect(self._open_keyboard_shortcuts_dialog)
        menu_bar.setCornerWidget(self._keyboard_shortcuts_corner_bar, QtCore.Qt.TopRightCorner)
        self._update_keyboard_shortcuts_button_state()
        self._update_main_panel_text_scale_controls()

        self.statusBar().showMessage("Ready", 3000)
        self._init_blend_mode_status_label()
        self._apply_main_panel_ui_scale()

    def _init_ui_scale_menu(self, menu_bar):
        self._view_menu = menu_bar.addMenu("View")
        force_widget_ui_scale_100(self._view_menu)
        self._ui_scale_menu = self._view_menu.addMenu("UI Scale")
        force_widget_ui_scale_100(self._ui_scale_menu)
        self._ui_scale_action_group = QtWidgets.QActionGroup(self)
        self._ui_scale_action_group.setExclusive(True)
        self._ui_scale_actions = []
        for label, value in UI_SCALE_CHOICES:
            action = QtWidgets.QAction(label, self)
            action.setCheckable(True)
            action.setData(value)
            action.setStatusTip("Change GeoViewer UI text, menu, and control scale.")
            action.triggered.connect(lambda checked=False, v=value: self._set_ui_scale_from_menu(v))
            self._ui_scale_action_group.addAction(action)
            self._ui_scale_menu.addAction(action)
            self._ui_scale_actions.append(action)
        self._update_ui_scale_menu()
        self._init_main_panel_text_scale_actions()

    def _init_main_panel_text_scale_actions(self):
        self._view_menu.addSeparator()
        self._main_panel_text_scale_label_action = QtWidgets.QAction("Panel Text Scale", self)
        self._main_panel_text_scale_label_action.setEnabled(False)
        self._view_menu.addAction(self._main_panel_text_scale_label_action)

        self._main_panel_text_scale_action_group = QtWidgets.QActionGroup(self)
        self._main_panel_text_scale_action_group.setExclusive(True)
        self._main_panel_text_scale_actions = []
        for label, value in MAIN_PANEL_TEXT_SCALE_CHOICES:
            action = QtWidgets.QAction(label, self)
            action.setCheckable(True)
            action.setData(float(value))
            action.setStatusTip("Change top menu and bottom status text scale without changing title or Delta Time text.")
            action.triggered.connect(lambda checked=False, v=value: self._set_main_panel_text_scale_from_menu(v))
            self._main_panel_text_scale_action_group.addAction(action)
            self._view_menu.addAction(action)
            self._main_panel_text_scale_actions.append(action)
        self._update_main_panel_text_scale_controls()

    def _set_ui_scale_from_menu(self, value):
        normalized = normalize_persisted_ui_scale(value, "auto")
        if normalized == "auto":
            apply_persisted_ui_scale_setting("auto", reapply=True)
            persisted = "auto"
        else:
            set_ui_scale(float(normalized), reapply=True, manual=True)
            persisted = float(ui_scale())
        self._update_ui_scale_menu()
        self._apply_main_panel_ui_scale()
        self._persist_script_level_scale_state(ui_scale_value=persisted)
        try:
            self.statusBar().showMessage(f"UI scale set to {format_ui_scale_label(persisted)}", 2500)
        except Exception:
            pass

    def _update_ui_scale_menu(self):
        actions = list(getattr(self, "_ui_scale_actions", []) or [])
        if not actions:
            return
        current = "auto" if not ui_scale_is_manual() else float(ui_scale())
        for action in actions:
            value = normalize_persisted_ui_scale(action.data(), "auto")
            checked = False
            if current == "auto":
                checked = value == "auto"
            elif value != "auto":
                try:
                    checked = abs(float(value) - float(current)) < 0.005
                except Exception:
                    checked = False
            try:
                action.setChecked(checked)
            except Exception:
                pass

    def _update_main_panel_text_scale_controls(self):
        scale = self._main_panel_text_scale() if hasattr(self, "_main_panel_text_scale") else 1.0
        actions = list(getattr(self, "_main_panel_text_scale_actions", []) or [])
        for action in actions:
            try:
                value = normalize_main_panel_text_scale(action.data(), 1.0)
                action.setChecked(abs(float(value) - float(scale)) < 0.005)
            except Exception:
                pass
            try:
                action.setStatusTip("Change top menu and bottom status text scale without changing title or Delta Time text.")
            except Exception:
                pass
        label_action = getattr(self, "_main_panel_text_scale_label_action", None)
        if label_action is not None:
            try:
                label_action.setText(f"Panel Text Scale ({format_main_panel_text_scale_label(scale)})")
            except Exception:
                pass

    def _resolve_keyboard_shortcuts_theme_mode(self):
        if bool(getattr(self, "keyboard_shortcuts_lock_theme", False)):
            return normalize_keyboard_shortcuts_theme_mode(
                getattr(self, "keyboard_shortcuts_theme_mode", "light"),
                "light",
            )

        current_mode = str(getattr(self, "theme_mode", "") or "").strip().lower()
        if current_mode not in ("light", "dark"):
            current_mode = get_app_theme_mode()
        return normalize_keyboard_shortcuts_theme_mode(current_mode, "light")

    def _set_keyboard_shortcuts_preferences(self, theme_mode, lock_theme):
        self.keyboard_shortcuts_theme_mode = normalize_keyboard_shortcuts_theme_mode(theme_mode, "light")
        self.keyboard_shortcuts_lock_theme = bool(lock_theme)
        self._update_keyboard_shortcuts_button_state()

    def _update_keyboard_shortcuts_button_state(self):
        corner_bar = getattr(self, "_keyboard_shortcuts_corner_bar", None)
        action = getattr(self, "_keyboard_shortcuts_action", None)
        if corner_bar is None or action is None:
            return
        parent_menu_bar = self.menuBar()
        if parent_menu_bar is not None:
            corner_bar.setFont(parent_menu_bar.font())
            try:
                set_widget_stylesheet_unscaled(corner_bar, parent_menu_bar.styleSheet())
            except Exception:
                pass
        action.setToolTip("")
        self._update_main_panel_text_scale_controls()
        self._relayout_main_panel_ui_scale_controls(defer=True)

    def _open_geoviewer_info_dialog(self):
        theme_mode = str(getattr(self, "theme_mode", "") or get_app_theme_mode()).lower()
        if theme_mode not in ("light", "dark"):
            theme_mode = get_app_theme_mode()

        existing = getattr(self, "_geoviewer_info_dialog", None)
        if existing is not None:
            try:
                if existing.isVisible():
                    existing.update_dialog_state(theme_mode=theme_mode)
                    existing.show()
                    existing.raise_()
                    existing.activateWindow()
                    return
            except Exception:
                self._geoviewer_info_dialog = None

        dlg = GeoViewerInfoDialog(
            theme_mode=theme_mode,
            parent=self,
        )
        dlg.destroyed.connect(lambda *_: setattr(self, "_geoviewer_info_dialog", None))
        self._geoviewer_info_dialog = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _open_keyboard_shortcuts_dialog(self):
        try:
            image_paths = ensure_keyboard_shortcuts_pngs()
        except Exception as e:
            QMessageBox.warning(
                self,
                "Keyboard Shortcuts",
                "Could not prepare the keyboard shortcut image files:\n"
                f"{e}",
            )
            return

        existing = getattr(self, "_keyboard_shortcuts_dialog", None)
        if existing is not None:
            try:
                if existing.isVisible():
                    existing.update_dialog_state(
                        image_paths=image_paths,
                        theme_mode=self._resolve_keyboard_shortcuts_theme_mode(),
                        lock_theme=bool(getattr(self, "keyboard_shortcuts_lock_theme", False)),
                    )
                    existing.show()
                    existing.raise_()
                    existing.activateWindow()
                    return
            except Exception:
                self._keyboard_shortcuts_dialog = None

        dlg = KeyboardShortcutsDialog(
            image_paths=image_paths,
            initial_theme_mode=self._resolve_keyboard_shortcuts_theme_mode(),
            lock_theme=bool(getattr(self, "keyboard_shortcuts_lock_theme", False)),
            parent=self,
        )
        dlg.preferencesChanged.connect(self._set_keyboard_shortcuts_preferences)
        dlg.destroyed.connect(lambda *_: setattr(self, "_keyboard_shortcuts_dialog", None))
        self._keyboard_shortcuts_dialog = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _apply_saved_vector_colors(self):
        if getattr(self, "shp_primary", None) is not None:
            self.shp_primary_color = normalize_picker_color_name(
                self.persisted_ui_settings.get("shp_primary_color"),
                getattr(self, "shp_primary_color", "cyan"),
            )

        overlay_color_map = {}
        for item in list(self.persisted_ui_settings.get("shp_overlay_colors", []) or []):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            color = normalize_picker_color_name(item.get("color"), "dodgerblue")
            if name:
                overlay_color_map[name] = color

        for ov in getattr(self, "shp_overlays", []):
            if not isinstance(ov, dict):
                continue
            name = str(ov.get("name") or "").strip()
            if name and name in overlay_color_map:
                ov["color"] = overlay_color_map[name]

    def _discover_runtime_shapefile_paths(self):
        try:
            shp_paths = discover_startup_shapefiles()
        except Exception:
            shp_paths = []

        discovered = []
        seen = set()
        for path in list(shp_paths or []):
            raw_path = str(path or "").strip()
            if not raw_path:
                continue
            abs_path = os.path.abspath(raw_path)
            key = abs_path.lower()
            if key in seen:
                continue
            seen.add(key)
            discovered.append(abs_path)
        return discovered

    def _load_runtime_shapefile_selections(self, load_items):
        loaded_labels = []
        failed_items = []

        used_names = set()
        primary_name = normalize_persisted_shapefile_name(getattr(self, "shp_primary_name", ""))
        if primary_name:
            used_names.add(primary_name.lower())
        for ov in getattr(self, "shp_overlays", []):
            if not isinstance(ov, dict):
                continue
            overlay_name = normalize_persisted_shapefile_name(ov.get("name"))
            if overlay_name:
                used_names.add(overlay_name.lower())

        for item in list(load_items or []):
            if not isinstance(item, dict):
                continue
            slot_kind = str(item.get("slot_kind") or "overlay").strip().lower()
            raw_path = str(item.get("path") or "").strip()
            shp_name = normalize_persisted_shapefile_name(item.get("name") or raw_path)
            if slot_kind not in ("primary", "overlay") or not raw_path or not shp_name:
                continue

            name_key = shp_name.lower()
            if name_key in used_names:
                continue

            try:
                gdf = gpd.read_file(raw_path).to_crs(epsg=4326)
            except Exception as e:
                failed_items.append(f"{shp_name}: {e}")
                continue

            if slot_kind == "primary":
                if getattr(self, "shp_primary", None) is not None:
                    failed_items.append(f"{shp_name}: primary slot is already filled")
                    continue
                self.shp_primary = gdf
                self.shp_primary_name = os.path.basename(raw_path)
                self.shp_primary_color = normalize_picker_color_name(item.get("color"), "cyan")
                loaded_labels.append(f"Primary: {self.shp_primary_name}")
            else:
                if len(getattr(self, "shp_overlays", [])) >= 5:
                    failed_items.append(f"{shp_name}: all overlay slots are already filled")
                    continue
                overlay_name = os.path.basename(raw_path)
                self.shp_overlays.append({
                    "name": overlay_name,
                    "gdf": gdf,
                    "color": normalize_picker_color_name(item.get("color"), "dodgerblue"),
                })
                loaded_labels.append(f"Overlay {len(self.shp_overlays)}: {overlay_name}")

            used_names.add(name_key)

        return loaded_labels, failed_items

    def _collect_current_ui_settings(self):
        overlay_colors = []
        for ov in getattr(self, "shp_overlays", []):
            if isinstance(ov, dict):
                name = str(ov.get("name") or "").strip()
                color = normalize_picker_color_name(ov.get("color"), "dodgerblue")
            else:
                name = ""
                color = "dodgerblue"
            if name:
                overlay_colors.append({"name": name, "color": color})

        return normalize_persisted_ui_settings({
            "theme_mode": getattr(self, "theme_mode", get_app_theme_mode()),
            "window_size": [int(max(900, self.width())), int(max(650, self.height()))],
            "keyboard_shortcuts_lock_theme": bool(
                getattr(self, "keyboard_shortcuts_lock_theme", False)
            ),
            "keyboard_shortcuts_theme_mode": normalize_keyboard_shortcuts_theme_mode(
                getattr(self, "keyboard_shortcuts_theme_mode", "light"),
                "light",
            ),
            "grid_cols": int(getattr(self, "grid_cols", 3)),
            "grid_rows": int(getattr(self, "grid_rows", 1)),
            "panel_layout_settings": normalize_panel_layout_settings(
                getattr(self, "panel_layout_settings", DEFAULT_PANEL_LAYOUT_SETTINGS)
            ),
            "sync_zoom_pan": bool(getattr(self, "sync_zoom_pan", False)),
            "scroll_wheel_pan_multi_enabled": bool(
                getattr(self, "scroll_wheel_pan_multi_enabled", True)
            ),
            "use_theme_nan_color": bool(getattr(self, "use_theme_nan_color", True)),
            "nan_color": normalize_nan_override_value(getattr(self, "nan_color", self._panel_facecolor())),
            "thermal_alpha": max(0.0, min(1.0, float(getattr(self, "thermal_alpha", 1.0)))),
            "thermal_blend_mode": normalize_thermal_blend_mode(
                getattr(self, "thermal_blend_mode", "normal")
            ),
            "thermal_visual_resampling": normalize_visual_resampling(
                getattr(self, "thermal_visual_resampling", "nearest")
            ),
            "basemap_visual_resampling": normalize_visual_resampling(
                getattr(self, "basemap_visual_resampling", "nearest")
            ),
            "basemap_resolution_mode": normalize_basemap_resolution_mode(
                getattr(self, "basemap_resolution_mode", "dynamic")
            ),
            "basemap_color_scaling": normalize_basemap_color_scaling(
                getattr(self, "basemap_color_scaling", "normal")
            ),
            "basemap_cmap": normalize_basemap_cmap(
                getattr(self, "basemap_cmap", "gray")
            ),
            "basemap_mode": normalize_basemap_mode(
                getattr(self, "basemap_mode", "nearest")
            ),
            "basemap_category": normalize_basemap_category(
                getattr(self, "basemap_category", "")
            ),
            "shp_linewidth": float(getattr(self, "shp_linewidth", 1.2)),
            "summary_fontsize": float(getattr(self, "summary_fontsize", 11.0)),
            "title_fontsize": float(getattr(self, "title_fontsize", 18.0)),
            "cmap_mode": str(getattr(self, "cmap_mode", "gray") or "gray"),
            "alt_cmap": str(getattr(self, "alt_cmap", "magma") or "magma"),
            "global_edge_mode": bool(getattr(self, "global_edge_mode", False)),
            "global_contrast_rel": [
                float(getattr(self, "global_contrast_rel", (0.0, 1.0))[0]),
                float(getattr(self, "global_contrast_rel", (0.0, 1.0))[1]),
            ],
            "global_gamma": float(getattr(self, "global_gamma", 1.0)),
            "base_multiplier": int(getattr(self, "base_multiplier", 1)),
            "scale_modifier": float(getattr(self, "scale_modifier", 1.0)),
            "keep_reject_button_preset": str(
                getattr(self, "keep_reject_button_preset", DEFAULT_KEEP_REJECT_PRESET_ID)
                or DEFAULT_KEEP_REJECT_PRESET_ID
            ),
            "keep_button_color": normalize_keep_reject_button_color(
                getattr(self, "keep_button_color", KEEP_REJECT_BUTTON_PRESET_BY_ID[DEFAULT_KEEP_REJECT_PRESET_ID]["keep"]),
                KEEP_REJECT_BUTTON_PRESET_BY_ID[DEFAULT_KEEP_REJECT_PRESET_ID]["keep"],
            ),
            "reject_button_color": normalize_keep_reject_button_color(
                getattr(self, "reject_button_color", KEEP_REJECT_BUTTON_PRESET_BY_ID[DEFAULT_KEEP_REJECT_PRESET_ID]["reject"]),
                KEEP_REJECT_BUTTON_PRESET_BY_ID[DEFAULT_KEEP_REJECT_PRESET_ID]["reject"],
            ),
            "keep_reject_button_layout_settings": normalize_keep_reject_button_layout_settings(
                getattr(self, "keep_reject_button_layout_settings", DEFAULT_KEEP_REJECT_BUTTON_LAYOUT_SETTINGS)
            ),
            "warp_source_color": normalize_keep_reject_button_color(
                getattr(self, "warp_source_color", DEFAULT_WARP_SOURCE_COLOR),
                DEFAULT_WARP_SOURCE_COLOR,
            ),
            "warp_target_color": normalize_keep_reject_button_color(
                getattr(self, "warp_target_color", DEFAULT_WARP_TARGET_COLOR),
                DEFAULT_WARP_TARGET_COLOR,
            ),
            "keep_behavior": dict(getattr(self, "keep_behavior", {}) or {}),
            "reject_behavior": dict(getattr(self, "reject_behavior", {}) or {}),
            "filename_dt_substring": normalize_filename_datetime_substring(
                getattr(self, "filename_dt_substring", self.persisted_ui_settings.get("filename_dt_substring", ""))
            ),
            "filename_dt_pattern": normalize_filename_datetime_pattern(
                getattr(self, "filename_dt_pattern", self.persisted_ui_settings.get("filename_dt_pattern", ""))
            ),
            "basemap_name": normalize_persisted_basemap_name(
                getattr(self, "basemap_name", "") if normalize_basemap_mode(getattr(self, "basemap_mode", "nearest")) == "single" else ""
            ),
            "shp_primary_color": normalize_picker_color_name(
                getattr(self, "shp_primary_color", "cyan"),
                "cyan",
            ),
            "shp_primary_name": normalize_persisted_shapefile_name(
                getattr(self, "shp_primary_name", "")
            ),
            "shp_overlay_colors": overlay_colors,
        })

    def _saved_settings_profile_names(self):
        return [str(item.get("name") or "").strip() for item in self.persisted_ui_store.get("profiles", []) if str(item.get("name") or "").strip()]

    def _show_wide_text_prompt(self, title, label, text=""):
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setModal(True)
        dlg.setMinimumWidth(560)
        dlg.setStyleSheet(build_app_stylesheet(get_app_theme_mode()))

        lay = QtWidgets.QVBoxLayout(dlg)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)

        label_widget = QtWidgets.QLabel(label, dlg)
        label_widget.setWordWrap(True)
        lay.addWidget(label_widget)

        text_edit = QtWidgets.QLineEdit(dlg)
        text_edit.setText(str(text or ""))
        text_edit.selectAll()
        lay.addWidget(text_edit)

        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel,
            parent=dlg,
        )
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)

        text_edit.setFocus()
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            return text_edit.text(), True
        return text_edit.text(), False

    def _show_wide_choice_prompt(self, title, label, items, current_index=0):
        choices = [str(item) for item in list(items or [])]
        if not choices:
            return "", False

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setModal(True)
        dlg.setMinimumWidth(560)
        dlg.setStyleSheet(build_app_stylesheet(get_app_theme_mode()))

        lay = QtWidgets.QVBoxLayout(dlg)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)

        label_widget = QtWidgets.QLabel(label, dlg)
        label_widget.setWordWrap(True)
        lay.addWidget(label_widget)

        combo = QtWidgets.QComboBox(dlg)
        combo.setMinimumWidth(520)
        combo.addItems(choices)
        combo.setCurrentIndex(max(0, min(len(choices) - 1, int(current_index))))
        lay.addWidget(combo)

        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel,
            parent=dlg,
        )
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)

        combo.setFocus()
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            return combo.currentText(), True
        return combo.currentText(), False

    def _persist_store_to_script(self, success_message):
        self.persisted_ui_store = save_persisted_ui_store_to_script(self._script_path, self.persisted_ui_store)
        self.statusBar().showMessage(success_message, 6000)

    def _persist_script_level_scale_state(self, ui_scale_value=None, main_panel_text_scale=None):
        try:
            self.persisted_ui_store = update_persisted_scale_state(
                self.persisted_ui_store,
                ui_scale_value=ui_scale_value,
                main_panel_text_scale=main_panel_text_scale,
            )
            self.persisted_ui_store = save_persisted_ui_store_to_script(
                self._script_path,
                self.persisted_ui_store,
            )
        except Exception as e:
            try:
                self.statusBar().showMessage(f"Could not save scale preference: {e}", 6000)
            except Exception:
                pass

    def _store_current_profile_settings_snapshot(self):
        current_name = str(getattr(self, "current_settings_profile_name", "") or "").strip()
        if not current_name:
            return
        current_settings = self._collect_current_ui_settings()
        updated_profiles = []
        found = False
        for profile in self.persisted_ui_store.get("profiles", []):
            if str(profile.get("name") or "").strip().lower() == current_name.lower():
                updated_profiles.append({"name": current_name, "settings": current_settings})
                found = True
            else:
                updated_profiles.append(profile)
        if found:
            self.persisted_ui_store["profiles"] = updated_profiles
            self.persisted_ui_settings = dict(current_settings)

    def _save_named_settings_profile(self, profile_name, settings, overwrite_name=None):
        profile_name = _normalize_persisted_ui_profile_name(profile_name, "Profile")
        normalized_settings = normalize_persisted_ui_settings(settings)
        store = normalize_persisted_ui_store(self.persisted_ui_store)
        profiles = list(store.get("profiles", []))

        existing_idx = None
        for idx, profile in enumerate(profiles):
            if str(profile.get("name") or "").strip().lower() == profile_name.lower():
                existing_idx = idx
                break

        replaced_name = None
        if existing_idx is not None:
            profiles[existing_idx] = {"name": profile_name, "settings": normalized_settings}
        elif overwrite_name:
            overwrite_idx = None
            for idx, profile in enumerate(profiles):
                if str(profile.get("name") or "").strip().lower() == str(overwrite_name).strip().lower():
                    overwrite_idx = idx
                    break
            if overwrite_idx is None:
                raise RuntimeError("Could not find the saved settings profile to overwrite.")
            replaced_name = str(profiles[overwrite_idx].get("name") or "").strip()
            profiles[overwrite_idx] = {"name": profile_name, "settings": normalized_settings}
            if str(store.get("default_profile") or "").strip().lower() == replaced_name.lower():
                store["default_profile"] = profile_name
        else:
            if len(profiles) >= MAX_PERSISTED_UI_PROFILES:
                raise RuntimeError("The maximum number of saved settings profiles has already been reached.")
            profiles.append({"name": profile_name, "settings": normalized_settings})

        store["profiles"] = profiles
        self.persisted_ui_store = normalize_persisted_ui_store(store)
        self.persisted_ui_settings = dict(normalized_settings)
        self.current_settings_profile_name = profile_name
        self._persist_store_to_script(f"Saved settings profile '{profile_name}'.")
        return replaced_name

    def _save_settings_profile_interactive(self):
        suggested_name = str(getattr(self, "current_settings_profile_name", "") or "").strip()
        text, ok = self._show_wide_text_prompt(
            "Save Settings Profile",
            f"Profile name (up to {MAX_PERSISTED_UI_PROFILES} saved):",
            text=suggested_name,
        )
        if not ok:
            return

        profile_name = _normalize_persisted_ui_profile_name(text, "")
        if not profile_name:
            QMessageBox.warning(self, "Save Settings", "Please enter a name for the saved settings profile.")
            return

        existing_names = self._saved_settings_profile_names()
        overwrite_name = None
        if all(name.lower() != profile_name.lower() for name in existing_names) and len(existing_names) >= MAX_PERSISTED_UI_PROFILES:
            overwrite_name, ok = self._show_wide_choice_prompt(
                "Overwrite Saved Settings",
                "Ten saved setting groups already exist. Choose which one to overwrite:",
                existing_names,
                current_index=0,
            )
            if not ok or not overwrite_name:
                return

        try:
            replaced_name = self._save_named_settings_profile(
                profile_name,
                self._collect_current_ui_settings(),
                overwrite_name=overwrite_name,
            )
            message = f"Saved settings profile '{profile_name}' inside this Python file."
            if replaced_name and replaced_name.lower() != profile_name.lower():
                message = f"Saved '{profile_name}' and overwrote '{replaced_name}' inside this Python file."
            QMessageBox.information(self, "Settings Saved", message)
        except Exception as e:
            self.statusBar().showMessage("Could not save settings profile.", 6000)
            QMessageBox.warning(self, "Save Settings Failed", f"Could not save settings profile:\n{e}")

    def _settings_profile_share_start_dir(self):
        path = str(getattr(self, "_last_settings_profile_share_dir", "") or "").strip()
        if path and os.path.isdir(path):
            return path
        script_dir = os.path.dirname(os.path.abspath(getattr(self, "_script_path", __file__)))
        return script_dir if os.path.isdir(script_dir) else os.getcwd()

    def _settings_profile_main_folder(self):
        try:
            folder = os.path.abspath(os.getcwd())
        except Exception:
            folder = ""
        if folder and os.path.isdir(folder):
            return folder
        script_dir = os.path.dirname(os.path.abspath(getattr(self, "_script_path", __file__)))
        return script_dir if os.path.isdir(script_dir) else "."

    def _import_export_settings_profile_interactive(self):
        choice, ok = self._show_wide_choice_prompt(
            "Import/Export Settings",
            "Choose a settings profile sharing action:",
            ["Export Settings Profile...", "Import Settings Profile..."],
            current_index=0,
        )
        if not ok or not choice:
            return
        if str(choice).lower().startswith("export"):
            self._export_settings_profile_interactive()
        else:
            self._import_settings_profile_interactive()

    def _export_settings_profile_interactive(self):
        names = self._saved_settings_profile_names()
        if not names:
            QMessageBox.information(self, "Export Settings", "No saved settings profiles were found in this Python file.")
            return

        current_name = str(getattr(self, "current_settings_profile_name", "") or "").strip()
        default_idx = names.index(current_name) if current_name in names else 0
        selected_name, ok = self._show_wide_choice_prompt(
            "Export Settings Profile",
            "Choose a saved settings profile to export:",
            names,
            current_index=default_idx,
        )
        if not ok or not selected_name:
            return

        profile = _find_persisted_ui_profile(self.persisted_ui_store, selected_name)
        suggested_file = _sanitize_settings_profile_export_filename(profile["name"]) + ".txt"
        file_name, ok = self._show_wide_text_prompt(
            "Export Settings Profile",
            "File name for this exported settings profile:",
            text=suggested_file,
        )
        if not ok:
            return

        clean_file_name = _sanitize_settings_profile_export_filename(file_name)
        if not clean_file_name:
            QMessageBox.warning(self, "Export Settings", "Please enter a file name for the exported settings profile.")
            return
        if not clean_file_name.lower().endswith(".txt"):
            clean_file_name += ".txt"

        folder = self._settings_profile_main_folder()
        file_path = os.path.join(folder, clean_file_name)
        if os.path.exists(file_path):
            answer = QMessageBox.question(
                self,
                "Replace Export File",
                f"'{clean_file_name}' already exists in the main folder. Replace it?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return

        try:
            export_text = build_settings_profile_export_text(
                profile["name"],
                profile["settings"],
                newline="\r\n",
            )
            with open(file_path, "w", encoding="utf-8", newline="") as fh:
                fh.write(export_text)
            QMessageBox.information(
                self,
                "Settings Exported",
                f"Exported settings profile '{profile['name']}' to:\n{file_path}",
            )
        except Exception as e:
            self.statusBar().showMessage("Could not export settings profile.", 6000)
            QMessageBox.warning(self, "Export Settings Failed", f"Could not export settings profile:\n{e}")

    def _show_settings_import_file_prompt(self, folder, validations):
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Import Settings Profile")
        dlg.setModal(True)
        dlg.setMinimumWidth(760)
        dlg.setMinimumHeight(460)
        dlg.setStyleSheet(build_app_stylesheet(get_app_theme_mode()))

        lay = QtWidgets.QVBoxLayout(dlg)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)

        title = QtWidgets.QLabel(f"Text files in:\n{folder}", dlg)
        title.setWordWrap(True)
        lay.addWidget(title)

        listw = QtWidgets.QListWidget(dlg)
        listw.setMinimumHeight(260)
        listw.setUniformItemSizes(False)
        lay.addWidget(listw, 1)

        detail = QtWidgets.QLabel("", dlg)
        detail.setWordWrap(True)
        lay.addWidget(detail)

        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel,
            parent=dlg,
        )
        import_button = btns.button(QtWidgets.QDialogButtonBox.Ok)
        import_button.setText("Import")
        import_button.setEnabled(False)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)

        ok_color = QtGui.QColor("#2E8B57")
        warn_color = QtGui.QColor("#E69F00")
        bad_color = QtGui.QColor("#C23B22")
        first_valid_row = -1
        for idx, item in enumerate(validations):
            file_name = os.path.basename(item.get("file_path", ""))
            if item.get("valid"):
                profile_name = item.get("profile_name") or "Profile"
                if item.get("warnings"):
                    label = f"{file_name}    WARNING    SHA-256 passed    profile: {profile_name}"
                    color = warn_color
                else:
                    label = f"{file_name}    OK    SHA-256 passed    profile: {profile_name}"
                    color = ok_color
                if first_valid_row < 0:
                    first_valid_row = idx
            else:
                reason = "; ".join(item.get("errors", [])[:2]) or "Invalid GeoViewer settings export."
                label = f"{file_name}    INVALID    {reason}"
                color = bad_color
            row = QtWidgets.QListWidgetItem(label)
            row.setForeground(color)
            notes = []
            if item.get("errors"):
                notes.extend(item.get("errors", []))
            if item.get("warnings"):
                notes.extend(item.get("warnings", []))
            if not notes and item.get("checksum_ok"):
                notes.append("Valid GeoViewer settings export. SHA-256 checksum passed.")
            row.setToolTip("\n".join(notes) if notes else "Valid GeoViewer settings export.")
            row.setData(QtCore.Qt.UserRole, idx)
            listw.addItem(row)

        def _update_detail():
            selected = listw.currentItem()
            if selected is None:
                detail.setText("Select a green or yellow file to import.")
                import_button.setEnabled(False)
                return
            item = validations[int(selected.data(QtCore.Qt.UserRole))]
            if item.get("valid"):
                detail_lines = [
                    "Valid GeoViewer export. SHA-256 checksum passed.",
                    f"Profile: {item.get('profile_name', 'Profile')} ({int(item.get('size_bytes', 0))} bytes).",
                ]
                if item.get("warnings"):
                    detail_lines.append("Safe to import, but these referenced local files are not present:")
                    detail_lines.extend(item.get("warnings", [])[:8])
                    if len(item.get("warnings", [])) > 8:
                        detail_lines.append("...")
                detail.setText("\n".join(detail_lines))
                import_button.setEnabled(True)
            else:
                detail.setText("This file failed import checks:\n" + "\n".join(item.get("errors", [])))
                import_button.setEnabled(False)

        listw.currentItemChanged.connect(lambda _current, _previous: _update_detail())
        if first_valid_row >= 0:
            listw.setCurrentRow(first_valid_row)
        elif listw.count() > 0:
            listw.setCurrentRow(0)
        _update_detail()

        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return None
        selected = listw.currentItem()
        if selected is None:
            return None
        item = validations[int(selected.data(QtCore.Qt.UserRole))]
        return item if item.get("valid") else None

    def _import_settings_profile_interactive(self):
        folder = self._settings_profile_main_folder()

        txt_files = sorted(glob.glob(os.path.join(folder, "*.txt")), key=lambda p: os.path.basename(p).lower())
        if not txt_files:
            QMessageBox.information(
                self,
                "Import Settings",
                f"No .txt files were found in the main folder:\n{folder}",
            )
            return

        validations = [validate_settings_profile_export_file(path, main_folder=folder) for path in txt_files]
        validations.sort(
            key=lambda item: (
                0 if item.get("valid") and not item.get("warnings") else 1 if item.get("valid") else 2,
                os.path.basename(item.get("file_path", "")).lower(),
            )
        )
        selected = self._show_settings_import_file_prompt(folder, validations)
        if not selected:
            return

        suggested_name = selected.get("profile_name") or "Imported Profile"
        text, ok = self._show_wide_text_prompt(
            "Import Settings Profile",
            "Name for this imported settings profile:",
            text=suggested_name,
        )
        if not ok:
            return

        profile_name = _normalize_persisted_ui_profile_name(text, "")
        if not profile_name:
            QMessageBox.warning(self, "Import Settings", "Please enter a name for the imported settings profile.")
            return

        existing_names = self._saved_settings_profile_names()
        overwrite_name = None
        matching_name = next((name for name in existing_names if name.lower() == profile_name.lower()), None)
        if matching_name:
            answer = QMessageBox.question(
                self,
                "Replace Settings Profile",
                f"A settings profile named '{matching_name}' already exists. Replace it with the imported profile?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        elif len(existing_names) >= MAX_PERSISTED_UI_PROFILES:
            overwrite_name, ok = self._show_wide_choice_prompt(
                "Overwrite Saved Settings",
                "Ten saved setting groups already exist. Choose which one to overwrite:",
                existing_names,
                current_index=0,
            )
            if not ok or not overwrite_name:
                return

        try:
            replaced_name = self._save_named_settings_profile(
                profile_name,
                selected["settings"],
                overwrite_name=overwrite_name,
            )
            self._apply_persisted_ui_settings_to_viewer(selected["settings"], profile_name=profile_name)
            message = f"Imported and loaded settings profile '{profile_name}'.\nSHA-256 checksum passed."
            if replaced_name and replaced_name.lower() != profile_name.lower():
                message = f"Imported and loaded '{profile_name}', replacing '{replaced_name}'.\nSHA-256 checksum passed."
            if selected.get("warnings"):
                message += "\n\nReferenced local files not found:\n" + "\n".join(selected.get("warnings", [])[:8])
                if len(selected.get("warnings", [])) > 8:
                    message += "\n..."
            QMessageBox.information(self, "Settings Imported", message)
        except Exception as e:
            self.statusBar().showMessage("Could not import settings profile.", 6000)
            QMessageBox.warning(self, "Import Settings Failed", f"Could not import settings profile:\n{e}")

    def _apply_persisted_ui_settings_to_viewer(self, settings, profile_name=None):
        settings = normalize_persisted_ui_settings(settings)
        try:
            window_size = settings.get("window_size", [self.width(), self.height()])
            if isinstance(window_size, (list, tuple)) and len(window_size) == 2:
                self.resize(int(window_size[0]), int(window_size[1]))
        except Exception:
            pass

        new_cols = int(settings.get("grid_cols", 3))
        new_rows = int(settings.get("grid_rows", 1))
        new_panel_layout_settings = normalize_panel_layout_settings(
            settings.get("panel_layout_settings")
        )
        new_button_layout_settings = normalize_keep_reject_button_layout_settings(
            settings.get("keep_reject_button_layout_settings")
        )
        layout_changed = (
            new_cols != int(getattr(self, "grid_cols", 3))
            or new_rows != int(getattr(self, "grid_rows", 1))
        )
        panel_layout_changed = (
            new_panel_layout_settings
            != normalize_panel_layout_settings(
                getattr(self, "panel_layout_settings", DEFAULT_PANEL_LAYOUT_SETTINGS)
            )
        )
        button_layout_changed = (
            new_button_layout_settings
            != normalize_keep_reject_button_layout_settings(
                getattr(self, "keep_reject_button_layout_settings", DEFAULT_KEEP_REJECT_BUTTON_LAYOUT_SETTINGS)
            )
        )

        preserved_views = {}
        if not layout_changed:
            for idx, ax in enumerate(getattr(self, "axes", [])):
                if idx >= getattr(self, "n_pan", 0):
                    continue
                if not self._panel_has_image(idx):
                    continue
                try:
                    preserved_views[idx] = (ax.get_xlim(), ax.get_ylim())
                except Exception:
                    pass

        self.panel_layout_settings = dict(new_panel_layout_settings)
        if layout_changed:
            self.set_panel_layout(new_cols, new_rows)
        elif panel_layout_changed:
            self._build_axes_grid(clear_fig=True)

        self.persisted_ui_settings = dict(settings)
        self.current_settings_profile_name = str(profile_name or getattr(self, "current_settings_profile_name", "") or "").strip() or None

        self.theme_mode = str(settings.get("theme_mode", get_app_theme_mode()) or get_app_theme_mode())
        self.keyboard_shortcuts_lock_theme = bool(settings.get("keyboard_shortcuts_lock_theme", False))
        self.keyboard_shortcuts_theme_mode = normalize_keyboard_shortcuts_theme_mode(
            settings.get("keyboard_shortcuts_theme_mode"),
            "light",
        )
        self.sync_zoom_pan = bool(settings.get("sync_zoom_pan", False))
        self.scroll_wheel_pan_multi_enabled = bool(
            settings.get("scroll_wheel_pan_multi_enabled", True)
        )
        self.use_theme_nan_color = bool(settings.get("use_theme_nan_color", True))
        self.nan_color = normalize_nan_override_value(settings.get("nan_color"), normalize_nan_override_value(self._panel_facecolor()))
        self.thermal_alpha = max(0.0, min(1.0, float(settings.get("thermal_alpha", 1.0))))
        self.thermal_blend_mode = normalize_thermal_blend_mode(
            settings.get("thermal_blend_mode"),
            "normal",
        )
        self.thermal_visual_resampling = normalize_visual_resampling(
            settings.get("thermal_visual_resampling"),
            "nearest",
        )
        self.basemap_visual_resampling = normalize_visual_resampling(
            settings.get("basemap_visual_resampling"),
            "nearest",
        )
        self.basemap_resolution_mode = normalize_basemap_resolution_mode(
            settings.get("basemap_resolution_mode"),
            "dynamic",
        )
        self.basemap_color_scaling = normalize_basemap_color_scaling(
            settings.get("basemap_color_scaling"),
            "normal",
        )
        self.basemap_cmap = normalize_basemap_cmap(
            settings.get("basemap_cmap"),
            "gray",
        )
        self.basemap_mode = normalize_basemap_mode(
            settings.get("basemap_mode"),
            "nearest",
        )
        self.basemap_category = normalize_basemap_category(
            settings.get("basemap_category")
        )
        self.shp_linewidth = float(settings.get("shp_linewidth", 1.2))
        self.summary_fontsize = float(settings.get("summary_fontsize", 11.0))
        self.title_fontsize = float(settings.get("title_fontsize", 18.0))
        self.cmap_mode = str(settings.get("cmap_mode", "gray") or "gray")
        self.alt_cmap = str(settings.get("alt_cmap", "magma") or "magma")
        self.global_edge_mode = bool(settings.get("global_edge_mode", False))
        contrast_rel = list(settings.get("global_contrast_rel", [0.0, 1.0]))
        self.global_contrast_rel = (float(contrast_rel[0]), max(1e-12, float(contrast_rel[1])))
        self.global_gamma = max(1e-12, float(settings.get("global_gamma", 1.0)))
        self.base_multiplier = max(1, int(settings.get("base_multiplier", 1)))
        self.scale_modifier = max(1e-3, float(settings.get("scale_modifier", 1.0)))
        self.pan_factor = 0.001 * self.base_multiplier * self.scale_modifier
        self.keep_button_color = normalize_keep_reject_button_color(
            settings.get("keep_button_color"),
            KEEP_REJECT_BUTTON_PRESET_BY_ID[DEFAULT_KEEP_REJECT_PRESET_ID]["keep"],
        )
        self.reject_button_color = normalize_keep_reject_button_color(
            settings.get("reject_button_color"),
            KEEP_REJECT_BUTTON_PRESET_BY_ID[DEFAULT_KEEP_REJECT_PRESET_ID]["reject"],
        )
        self.keep_reject_button_preset = infer_keep_reject_button_preset(
            self.keep_button_color,
            self.reject_button_color,
        )
        self.warp_source_color = normalize_keep_reject_button_color(
            settings.get("warp_source_color"),
            DEFAULT_WARP_SOURCE_COLOR,
        )
        self.warp_target_color = normalize_keep_reject_button_color(
            settings.get("warp_target_color"),
            DEFAULT_WARP_TARGET_COLOR,
        )
        self.keep_reject_button_layout_settings = dict(new_button_layout_settings)
        self.keep_behavior = dict(settings.get("keep_behavior", getattr(self, "keep_behavior", {})) or {})
        self.reject_behavior = dict(settings.get("reject_behavior", getattr(self, "reject_behavior", {})) or {})
        self.filename_dt_substring = normalize_filename_datetime_substring(
            settings.get("filename_dt_substring")
        )
        self.filename_dt_pattern = normalize_filename_datetime_pattern(
            settings.get("filename_dt_pattern")
        )
        apply_user_datetime_pattern_settings(
            self.filename_dt_substring,
            self.filename_dt_pattern,
        )
        self._load_startup_basemap_from_settings()
        self._load_startup_shapefiles_from_settings()

        self._apply_saved_vector_colors()
        self._apply_theme()
        self._update_progress_footer()

        for idx in range(getattr(self, "n_pan", 0)):
            self.draw(idx, refresh_selectors=False)
            if idx in preserved_views:
                try:
                    xlim, ylim = preserved_views[idx]
                    if not self._set_panel_view_exact(self.axes[idx], xlim, ylim, idx=idx):
                        self._set_panel_view(self.axes[idx], xlim, ylim, sync=False)
                except Exception:
                    pass

        self.ensure_selectors()
        if layout_changed or panel_layout_changed or button_layout_changed:
            self.add_keep_reject_buttons()
        self._sync_button_visibility()
        self.kill_toolbar_tools()
        try:
            self.canvas.draw()
        except Exception:
            self.canvas.draw_idle()
        self._schedule_panel_geometry_sync(delay_ms=0)

    def _load_settings_profile_interactive(self):
        names = self._saved_settings_profile_names()
        if not names:
            QMessageBox.information(self, "Load Saved Settings", "No saved settings profiles were found in this Python file.")
            return

        current_name = str(getattr(self, "current_settings_profile_name", "") or "").strip()
        default_idx = names.index(current_name) if current_name in names else 0
        selected_name, ok = self._show_wide_choice_prompt(
            "Load Saved Settings",
            "Choose a saved settings profile to load:",
            names,
            current_index=default_idx,
        )
        if not ok or not selected_name:
            return

        profile = _find_persisted_ui_profile(self.persisted_ui_store, selected_name)
        self._apply_persisted_ui_settings_to_viewer(profile["settings"], profile_name=profile["name"])
        self.statusBar().showMessage(f"Loaded settings profile '{profile['name']}'.", 6000)

    def _choose_default_settings_profile_interactive(self):
        names = self._saved_settings_profile_names()
        if not names:
            QMessageBox.information(self, "Choose Default Settings", "No saved settings profiles were found in this Python file.")
            return

        current_default = str(self.persisted_ui_store.get("default_profile") or "").strip()
        default_idx = names.index(current_default) if current_default in names else 0
        selected_name, ok = self._show_wide_choice_prompt(
            "Choose Default Settings",
            "Choose which saved settings profile should load by default next time:",
            names,
            current_index=default_idx,
        )
        if not ok or not selected_name:
            return

        self.persisted_ui_store["default_profile"] = selected_name
        try:
            self._persist_store_to_script(f"Default settings profile set to '{selected_name}'.")
            QMessageBox.information(
                self,
                "Default Settings Saved",
                f"'{selected_name}' will be the settings profile that loads by default next time.",
            )
        except Exception as e:
            self.statusBar().showMessage("Could not set default settings profile.", 6000)
            QMessageBox.warning(self, "Save Default Failed", f"Could not save the default settings profile:\n{e}")

    def _pct(self, count, total):
        return (count / total * 100.0) if total else 0.0

    def _summary_stat_text(self, label, count, total):
        return f"{label} {count:>4} ({self._pct(count, total):>4.1f}%)"

    def _summary_header_text(self, label, headline, width):
        return f"{label}: {headline:<{width}}"

    def _summary_breakdown_text(self, counts, total):
        parts = [
            self._summary_stat_text("Preserved:", counts['as_is'], total),
            self._summary_stat_text("Transformed:", counts['geo'], total),
            self._summary_stat_text("Warped:", counts['warp'], total),
            self._summary_stat_text("Rejected:", counts['reject'], total),
        ]
        return " | ".join(parts)

    def _update_progress_footer(self):
        proc = int(getattr(self, "session_processed", 0))
        overall = summarize_log_entries(self.log_entries)
        overall_total = overall['processed']
        session_head = f"{proc}/{self.total} files ({self._pct(proc, self.total):.1f}%)"
        overall_target = max(getattr(self, "overall_target", overall_total), overall_total)
        overall_head = f"{overall_total}/{overall_target} files ({self._pct(overall_total, overall_target):.1f}%)"
        head_width = max(len(session_head), len(overall_head))
        text = (
            f"{self._summary_header_text('Session', session_head, head_width)} | {self._summary_breakdown_text(self.session, proc)}\n"
            f"{self._summary_header_text('Overall', overall_head, head_width)} | {self._summary_breakdown_text(overall, overall_total)}"
        )
        prog = getattr(self, "prog", None)
        if prog is not None:
            try:
                prog.set_text(text)
            except Exception:
                pass
        footer_label = getattr(self, "footer_label", None)
        if footer_label is not None:
            try:
                footer_label.setText(text)
            except Exception:
                pass
        self._apply_footer_style()

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

    def _panel_has_image(self, idx: int) -> bool:
        return 0 <= idx < len(getattr(self, "current", [])) and bool(self.current[idx])

    def _panel_has_loaded_image(self, idx: int) -> bool:
        if not self._panel_has_image(idx):
            return False
        try:
            if idx not in getattr(self, "bases", {}):
                return False
        except Exception:
            return False
        try:
            display = self.images_data.get(idx) if hasattr(self.images_data, "get") else self.images_data[idx]
        except Exception:
            display = None
        if display is None:
            return False
        try:
            img_obj = self.images.get(idx) if hasattr(self.images, "get") else self.images[idx]
        except Exception:
            img_obj = None
        return img_obj is not None

    def _first_loaded_panel_index(self):
        for idx in range(min(getattr(self, "n_pan", 0), len(getattr(self, "current", [])))):
            if self._panel_has_loaded_image(idx):
                return idx
        return None

    def _active_loaded_panel_index(self, preferred=None):
        candidates = []
        try:
            candidates.append(int(preferred))
        except Exception:
            pass
        try:
            candidates.append(int(getattr(self, "active_idx", 0)))
        except Exception:
            pass
        for idx in candidates:
            if self._panel_has_loaded_image(idx):
                return idx
        return self._first_loaded_panel_index()

    def _set_panel_buttons_visible(self, idx: int, visible: bool):
        buttons = getattr(self, "buttons", [])
        for btn_idx in (2 * idx, 2 * idx + 1):
            if 0 <= btn_idx < len(buttons):
                try:
                    buttons[btn_idx].ax.set_visible(bool(visible))
                except Exception:
                    pass

    def _sync_button_visibility(self, idx: int = None):
        if idx is None:
            indices = range(min(getattr(self, "n_pan", 0), len(getattr(self, "current", []))))
        else:
            if idx < 0 or idx >= getattr(self, "n_pan", 0):
                return
            indices = [idx]

        hide_all = bool(getattr(self, "pan_mode", False))
        hide_all = hide_all or any(
            bool((panel_data or {}).get("collecting", False))
            for panel_data in getattr(self, "warp_data", {}).values()
        )
        hide_all = hide_all or bool(getattr(self, "split_mode", False))

        for panel_idx in indices:
            visible = self._panel_has_image(panel_idx) and not hide_all
            self._set_panel_buttons_visible(panel_idx, visible)

    def _new_split_state(self, path=""):
        return {
            "path": str(path or ""),
            "segments": [],
            "next_group_id": 1,
            "analysis": None,
            "selected_labels": set(),
            "piece_offsets": {},
            "piece_tforms": {},
            "deleted_labels": set(),
            "tool": "line",
            "artists": [],
            "temp_artist": None,
            "cursor": None,
            "keyboard_anchor": None,
            "keyboard_anchor_tool": None,
            "warp": {
                "collecting": False,
                "src_pix": [],
                "dst_pix": [],
                "point_order": [],
                "markers": [],
                "labels": [],
                "banner": None,
            },
        }

    def _ensure_split_data(self):
        if not hasattr(self, "split_data") or not isinstance(getattr(self, "split_data", None), dict):
            self.split_data = {}
        for panel_idx in range(getattr(self, "n_pan", 0)):
            if panel_idx not in self.split_data or not isinstance(self.split_data.get(panel_idx), dict):
                path = ""
                try:
                    path = self.current[panel_idx]
                except Exception:
                    pass
                self.split_data[panel_idx] = self._new_split_state(path)
        return self.split_data

    def _split_state(self, idx):
        self._ensure_split_data()
        state = self.split_data.get(idx)
        if not isinstance(state, dict):
            state = self._new_split_state(self.current[idx] if idx < len(getattr(self, "current", [])) else "")
            self.split_data[idx] = state
        return state

    def _split_status(self, message, timeout=5000):
        try:
            self.statusBar().showMessage(str(message), int(timeout))
        except Exception:
            pass

    def _clear_split_artists(self, idx):
        state = self._split_state(idx)
        for artist in list(state.get("artists", []) or []):
            try:
                artist.remove()
            except Exception:
                pass
        state["artists"] = []
        temp = state.get("temp_artist")
        if temp is not None:
            try:
                temp.remove()
            except Exception:
                pass
        state["temp_artist"] = None

    def _reset_split_state(self, idx, draw=False):
        if idx < 0 or idx >= getattr(self, "n_pan", 0):
            return
        try:
            self._split_clear_warp_artifacts(idx)
        except Exception:
            pass
        self._clear_split_artists(idx)
        path = ""
        try:
            path = self.current[idx]
        except Exception:
            pass
        self.split_data[idx] = self._new_split_state(path)
        try:
            img_obj = self.images.get(idx) if hasattr(self.images, "get") else self.images[idx]
            if img_obj is not None:
                img_obj.set_visible(True)
        except Exception:
            pass
        if draw:
            try:
                self.canvas.draw_idle()
            except Exception:
                pass

    def _split_panel_extent(self, idx):
        extent = self._panel_thermal_extent(idx)
        if extent is None:
            try:
                img_obj = self.images.get(idx) if hasattr(self.images, "get") else self.images[idx]
                extent = img_obj.get_extent() if img_obj is not None else None
            except Exception:
                extent = None
        if extent is None or len(extent) != 4:
            return None
        try:
            left, right, bottom, top = [float(v) for v in extent]
        except Exception:
            return None
        if not np.isfinite([left, right, bottom, top]).all():
            return None
        if abs(right - left) <= 1e-12 or abs(top - bottom) <= 1e-12:
            return None
        return left, right, bottom, top

    def _split_display_array(self, idx):
        display = None
        try:
            display = self.current_display_data.get(idx)
        except Exception:
            display = None
        if display is None:
            try:
                display = self.images_data.get(idx)
            except Exception:
                display = None
        if display is None:
            return None
        arr = np.asarray(display)
        if arr.ndim < 2:
            return None
        return arr

    def _split_display_shape(self, idx):
        arr = self._split_display_array(idx)
        if arr is None:
            return None
        h, w = arr.shape[:2]
        if h <= 0 or w <= 0:
            return None
        return int(h), int(w)

    def _split_event_to_pixel(self, idx, ev):
        shape = self._split_display_shape(idx)
        extent = self._split_panel_extent(idx)
        if shape is None or extent is None:
            return None
        if getattr(ev, "xdata", None) is None or getattr(ev, "ydata", None) is None:
            return None
        h, w = shape
        left, right, bottom, top = extent
        try:
            col = (float(ev.xdata) - left) / (right - left) * float(w)
            row = (top - float(ev.ydata)) / (top - bottom) * float(h)
        except Exception:
            return None
        if not np.isfinite([col, row]).all():
            return None
        return (
            min(max(float(col), 0.0), float(w)),
            min(max(float(row), 0.0), float(h)),
        )

    def _split_pixel_to_world(self, idx, col, row, offset=(0.0, 0.0)):
        shape = self._split_display_shape(idx)
        extent = self._split_panel_extent(idx)
        if shape is None or extent is None:
            return None
        h, w = shape
        left, right, bottom, top = extent
        dx, dy = offset
        x = left + (float(col) / max(float(w), 1.0)) * (right - left) + float(dx)
        y = top - (float(row) / max(float(h), 1.0)) * (top - bottom) + float(dy)
        return x, y

    def _split_vertices_to_world(self, idx, vertices, offset=(0.0, 0.0)):
        out = []
        for col, row in vertices:
            point = self._split_pixel_to_world(idx, col, row, offset=offset)
            if point is not None:
                out.append(point)
        return out

    def _split_identity_tform(self):
        return AffineTransform(matrix=np.eye(3, dtype=float))

    def _split_piece_tform(self, state, label, idx=None):
        tform = dict(state.get("piece_tforms", {}) or {}).get(label)
        if tform is not None:
            return tform
        dx_world, dy_world = dict(state.get("piece_offsets", {}) or {}).get(label, (0.0, 0.0))
        return self._split_translation_tform_from_world_delta(label, dx_world, dy_world, idx=idx)

    def _split_translation_tform_from_world_delta(self, label, dx_world, dy_world, idx=None):
        if idx is None:
            try:
                idx = int(getattr(self, "active_idx", 0))
            except Exception:
                idx = 0
        shape = self._split_display_shape(idx)
        extent = self._split_panel_extent(idx)
        if shape is None or extent is None:
            return self._split_identity_tform()
        h, w = shape
        left, right, bottom, top = extent
        pixel_w = abs(right - left) / max(float(w), 1.0)
        pixel_h = abs(top - bottom) / max(float(h), 1.0)
        tx = float(dx_world) / max(pixel_w, 1e-12)
        ty = -float(dy_world) / max(pixel_h, 1e-12)
        return AffineTransform(translation=(tx, ty))

    def _split_world_delta_to_pixel_delta(self, idx, dx_world, dy_world):
        shape = self._split_display_shape(idx)
        extent = self._split_panel_extent(idx)
        if shape is None or extent is None:
            return 0.0, 0.0
        h, w = shape
        left, right, bottom, top = extent
        pixel_w = abs(right - left) / max(float(w), 1.0)
        pixel_h = abs(top - bottom) / max(float(h), 1.0)
        return (
            float(dx_world) / max(pixel_w, 1e-12),
            -float(dy_world) / max(pixel_h, 1e-12),
        )

    def _split_apply_tform_to_point(self, tform, col, row):
        try:
            point = np.asarray(tform(np.asarray([[float(col), float(row)]], dtype=float)))[0]
            return float(point[0]), float(point[1])
        except Exception:
            return float(col), float(row)

    def _split_transformed_vertices_to_world(self, idx, region, tform=None):
        state = self._split_state(idx)
        if tform is None:
            tform = self._split_piece_tform(state, region.label, idx=idx)
        out = []
        for col, row in region.vertices:
            tcol, trow = self._split_apply_tform_to_point(tform, col, row)
            point = self._split_pixel_to_world(idx, tcol, trow)
            if point is not None:
                out.append(point)
        return out

    def _split_selected_labels(self, idx, fallback_all=False):
        state = self._split_state(idx)
        analysis = state.get("analysis")
        if not isinstance(analysis, SplitRegionAnalysis):
            return set()
        valid = {int(region.label) for region in analysis.regions}
        deleted = set(state.get("deleted_labels", set()) or set())
        selected = {int(label) for label in (state.get("selected_labels", set()) or set()) if int(label) in valid and int(label) not in deleted}
        if selected or not fallback_all:
            return selected
        return valid - deleted

    def _split_has_restructure(self, idx):
        state = self._split_state(idx)
        analysis = state.get("analysis")
        if not isinstance(analysis, SplitRegionAnalysis) or not analysis.regions:
            return False
        if state.get("deleted_labels"):
            return True
        identity = np.eye(3, dtype=float)
        for tform in dict(state.get("piece_tforms", {}) or {}).values():
            try:
                mat = np.asarray(_coerce_affine_transform(tform).params, dtype=float)
            except Exception:
                continue
            if mat.shape == (3, 3) and not np.allclose(mat, identity, atol=1e-9):
                return True
        for dx, dy in dict(state.get("piece_offsets", {}) or {}).values():
            try:
                if abs(float(dx)) > 1e-12 or abs(float(dy)) > 1e-12:
                    return True
            except Exception:
                continue
        return False

    def _split_next_group_id(self, state):
        group_id = int(state.get("next_group_id", 1) or 1)
        state["next_group_id"] = group_id + 1
        return group_id

    def _split_analysis_segments(self, idx):
        state = self._split_state(idx)
        shape = self._split_display_shape(idx)
        if shape is None:
            return [], 0, 0
        h, w = shape
        source_segments = list(state.get("segments", []) or [])
        segments = [
            SplitLineSegment(seg.x1, seg.y1, seg.x2, seg.y2, solid=bool(seg.solid), group_id=seg.group_id)
            for seg in source_segments
        ]
        user_count = len(segments)
        if user_count:
            segments.extend(
                [
                    SplitLineSegment(0.0, 0.0, float(w), 0.0, group_id=0),
                    SplitLineSegment(float(w), 0.0, float(w), float(h), group_id=0),
                    SplitLineSegment(float(w), float(h), 0.0, float(h), group_id=0),
                    SplitLineSegment(0.0, float(h), 0.0, 0.0, group_id=0),
                ]
            )
        return segments, user_count, (w, h)

    def _split_relabel_analysis(self, width, height, regions, selected_keys, offsets_by_key, tforms_by_key, deleted_keys):
        regions = sorted(regions, key=lambda item: (item.centroid_y, item.centroid_x))
        for new_label, region in enumerate(regions, start=1):
            region.label = new_label
        labels = _split_region_label_mask(
            width,
            height,
            regions,
            max_size=SPLIT_REGION_GRID_MAX_SIZE,
        ) if regions else np.zeros(_split_grid_shape_for(width, height, SPLIT_REGION_GRID_MAX_SIZE), dtype=np.int32)
        for region in regions:
            region.preview_pixels = int(np.count_nonzero(labels == region.label))
        selected = set()
        offsets = {}
        tforms = {}
        deleted = set()
        for region in regions:
            key = tuple(region.segment_indices)
            if key in selected_keys:
                selected.add(region.label)
            if key in deleted_keys:
                deleted.add(region.label)
            offsets[region.label] = offsets_by_key.get(key, (0.0, 0.0))
            if key in tforms_by_key:
                tforms[region.label] = tforms_by_key[key]
        analysis = SplitRegionAnalysis(
            labels=labels,
            regions=regions,
            grid_width=int(labels.shape[1]) if labels.ndim == 2 else 0,
            grid_height=int(labels.shape[0]) if labels.ndim == 2 else 0,
        )
        return analysis, selected, offsets, tforms, deleted

    def _split_rebuild_regions(self, idx):
        state = self._split_state(idx)
        shape = self._split_display_shape(idx)
        if shape is None:
            state["analysis"] = None
            return None
        h, w = shape
        old_analysis = state.get("analysis")
        old_selected = set(state.get("selected_labels", set()) or set())
        old_offsets = dict(state.get("piece_offsets", {}) or {})
        old_tforms = dict(state.get("piece_tforms", {}) or {})
        old_deleted = set(state.get("deleted_labels", set()) or set())
        selected_keys = set()
        offsets_by_key = {}
        tforms_by_key = {}
        deleted_keys = set()
        if isinstance(old_analysis, SplitRegionAnalysis):
            for region in old_analysis.regions:
                key = tuple(region.segment_indices)
                if region.label in old_selected:
                    selected_keys.add(key)
                if region.label in old_deleted:
                    deleted_keys.add(key)
                offsets_by_key[key] = old_offsets.get(region.label, (0.0, 0.0))
                if region.label in old_tforms:
                    tforms_by_key[key] = old_tforms.get(region.label)

        analysis_segments, user_count, _dims = self._split_analysis_segments(idx)
        if not analysis_segments or user_count <= 0:
            for seg in state.get("segments", []) or []:
                seg.solid = False
            state["analysis"] = None
            state["selected_labels"] = set()
            state["piece_offsets"] = {}
            state["piece_tforms"] = {}
            state["deleted_labels"] = set()
            return None

        raw_analysis = _split_analyze_regions(float(w), float(h), analysis_segments)
        for source_idx, source_seg in enumerate(state.get("segments", []) or []):
            if source_idx < len(analysis_segments):
                source_seg.solid = bool(analysis_segments[source_idx].solid)

        regions = [
            region for region in raw_analysis.regions
            if any(0 <= int(seg_idx) < user_count for seg_idx in region.segment_indices)
        ]
        analysis, selected, offsets, tforms, deleted = self._split_relabel_analysis(
            float(w),
            float(h),
            regions,
            selected_keys,
            offsets_by_key,
            tforms_by_key,
            deleted_keys,
        )
        state["analysis"] = analysis if analysis.regions else None
        state["selected_labels"] = selected if analysis.regions else set()
        state["piece_offsets"] = offsets if analysis.regions else {}
        state["piece_tforms"] = tforms if analysis.regions else {}
        state["deleted_labels"] = deleted if analysis.regions else set()
        return state["analysis"]

    def _split_add_line(self, idx, start, end):
        state = self._split_state(idx)
        x1, y1 = start
        x2, y2 = end
        if math.hypot(x2 - x1, y2 - y1) < 2.0:
            return False
        state["segments"].append(
            SplitLineSegment(float(x1), float(y1), float(x2), float(y2), group_id=self._split_next_group_id(state))
        )
        self._split_rebuild_regions(idx)
        return True

    def _split_add_rectangle(self, idx, start, end):
        state = self._split_state(idx)
        x1, y1 = start
        x2, y2 = end
        if abs(x2 - x1) < 2.0 or abs(y2 - y1) < 2.0:
            return False
        left, right = sorted((float(x1), float(x2)))
        top, bottom = sorted((float(y1), float(y2)))
        group_id = self._split_next_group_id(state)
        state["segments"].extend(
            [
                SplitLineSegment(left, top, right, top, group_id=group_id),
                SplitLineSegment(right, top, right, bottom, group_id=group_id),
                SplitLineSegment(right, bottom, left, bottom, group_id=group_id),
                SplitLineSegment(left, bottom, left, top, group_id=group_id),
            ]
        )
        self._split_rebuild_regions(idx)
        return True

    def _split_set_tool(self, idx, tool):
        state = self._split_state(idx)
        state["tool"] = "rect" if str(tool).lower().startswith("rect") else "line"
        label = "rectangle" if state["tool"] == "rect" else "line"
        self._split_status(f"Split mode: {label} cut tool active.")

    def _toggle_split_mode(self, idx=None):
        idx = self._active_loaded_panel_index(idx)
        if idx is None:
            self._split_status("Load a TIFF before entering split mode.")
            return True
        self.active_idx = idx
        self._ensure_split_data()
        state = self._split_state(idx)
        state["path"] = str(self.current[idx] or "")
        self.split_mode = not bool(getattr(self, "split_mode", False))
        if self.split_mode:
            self._split_drag_state = None
            self.kill_toolbar_tools()
            self.ensure_selectors()
            self._sync_button_visibility()
            self._refresh_split_artists(idx, draw=True)
            self._split_status("Split mode on: C=highlight, V=line, N=rectangle, W/A/S/D=move highlighted, G/H/J=warp highlighted, Delete=remove.")
        else:
            self._split_cancel_keyboard_draw(idx)
            self.ensure_selectors()
            self._sync_button_visibility()
            self._refresh_split_artists(idx, draw=True)
            self._split_status("Split mode off.")
        return True

    def _split_clear_temp(self, idx):
        state = self._split_state(idx)
        temp = state.get("temp_artist")
        if temp is not None:
            try:
                temp.remove()
            except Exception:
                pass
        state["temp_artist"] = None

    def _split_draw_temp(self, idx, start, end, tool):
        self._split_clear_temp(idx)
        state = self._split_state(idx)
        ax = self.axes[idx]
        if tool == "rect":
            sx, sy = start
            ex, ey = end
            corners = [(sx, sy), (ex, sy), (ex, ey), (sx, ey), (sx, sy)]
            world = [self._split_pixel_to_world(idx, x, y) for x, y in corners]
        else:
            world = [self._split_pixel_to_world(idx, start[0], start[1]), self._split_pixel_to_world(idx, end[0], end[1])]
        world = [point for point in world if point is not None]
        if len(world) < 2:
            return
        xs = [point[0] for point in world]
        ys = [point[1] for point in world]
        line = Line2D(xs, ys, color="#FFD60A", linewidth=1.6, linestyle="--", zorder=8)
        ax.add_line(line)
        state["temp_artist"] = line
        self.canvas.draw_idle()

    def _split_cancel_keyboard_draw(self, idx):
        state = self._split_state(idx)
        state["keyboard_anchor"] = None
        state["keyboard_anchor_tool"] = None
        self._split_clear_temp(idx)

    def _split_keyboard_draw_key(self, idx, tool):
        state = self._split_state(idx)
        point = state.get("cursor")
        if point is None:
            self._split_status("Move the cursor over the panel before pressing a split draw key.")
            return True
        tool = "rect" if str(tool).lower().startswith("rect") else "line"
        if state.get("keyboard_anchor") is None or state.get("keyboard_anchor_tool") != tool:
            state["keyboard_anchor"] = point
            state["keyboard_anchor_tool"] = tool
            self._split_draw_temp(idx, point, point, tool)
            label = "rectangle" if tool == "rect" else "line"
            self._split_status(f"Split {label}: first point set. Move cursor and press {('N' if tool == 'rect' else 'V')} again.")
            return True
        start = state.get("keyboard_anchor")
        self._split_cancel_keyboard_draw(idx)
        changed = self._split_add_rectangle(idx, start, point) if tool == "rect" else self._split_add_line(idx, start, point)
        if changed:
            analysis = state.get("analysis")
            count = len(analysis.regions) if isinstance(analysis, SplitRegionAnalysis) else 0
            self._split_status(f"Split linework updated: {count} section(s).")
        self._refresh_split_artists(idx, draw=True)
        return True

    def _handle_split_button_press(self, ev, idx):
        # Split drawing is keyboard-only so the mouse remains available for zoom.
        return False

    def _handle_split_motion(self, ev):
        if not bool(getattr(self, "split_mode", False)):
            return False
        if ev.inaxes not in getattr(self, "axes", []):
            return False
        idx = self.axes.index(ev.inaxes)
        if not self._panel_has_loaded_image(idx):
            return False
        point = self._split_event_to_pixel(idx, ev)
        if point is None:
            return False
        state = self._split_state(idx)
        state["cursor"] = point
        self.active_idx = idx
        anchor = state.get("keyboard_anchor")
        anchor_tool = state.get("keyboard_anchor_tool")
        if anchor is not None and anchor_tool in ("line", "rect"):
            self._split_draw_temp(idx, anchor, point, anchor_tool)
            return False
        drag = getattr(self, "_split_drag_state", None)
        if isinstance(drag, dict) and drag.get("idx") == idx:
            drag["current"] = point
            self._split_draw_temp(idx, drag["start"], point, drag.get("tool", "line"))
            return True
        return False

    def _handle_split_button_release(self, ev):
        drag = getattr(self, "_split_drag_state", None)
        if not isinstance(drag, dict):
            return False
        idx = int(drag.get("idx", getattr(self, "active_idx", 0)))
        if idx < 0 or idx >= getattr(self, "n_pan", 0):
            self._split_drag_state = None
            return True
        state = self._split_state(idx)
        point = self._split_event_to_pixel(idx, ev) if ev.inaxes in getattr(self, "axes", []) else None
        if point is None:
            point = state.get("cursor") or drag.get("current") or drag.get("start")
        self._split_clear_temp(idx)
        start = drag.get("start")
        tool = drag.get("tool", state.get("tool", "line"))
        self._split_drag_state = None
        if start is None or point is None:
            return True
        try:
            sx, sy = drag.get("screen_start", (0.0, 0.0))
            screen_dist = math.hypot(float(getattr(ev, "x", sx) or sx) - sx, float(getattr(ev, "y", sy) or sy) - sy)
        except Exception:
            screen_dist = math.hypot(point[0] - start[0], point[1] - start[1])
        if screen_dist < 4.0 and math.hypot(point[0] - start[0], point[1] - start[1]) < 2.0:
            self._split_select_at_pixel(idx, point)
            self._refresh_split_artists(idx, draw=True)
            return True
        changed = self._split_add_rectangle(idx, start, point) if tool == "rect" else self._split_add_line(idx, start, point)
        if changed:
            analysis = state.get("analysis")
            count = len(analysis.regions) if isinstance(analysis, SplitRegionAnalysis) else 0
            self._split_status(f"Split linework updated: {count} piece(s).")
        self._refresh_split_artists(idx, draw=True)
        return True

    def on_button_release(self, ev):
        if self._image_comment_dialog_active():
            return
        if self._handle_split_button_release(ev):
            return

    def _split_select_at_pixel(self, idx, point):
        state = self._split_state(idx)
        analysis = state.get("analysis")
        if not isinstance(analysis, SplitRegionAnalysis) or not analysis.regions:
            state["selected_labels"] = set()
            self._split_status("No split piece to highlight yet.")
            return 0
        x, y = point
        candidates = []
        for region in analysis.regions:
            try:
                contains = MplPath(region.vertices).contains_point((x, y), radius=0.5)
            except Exception:
                contains = False
            if contains:
                candidates.append(region)
        if candidates:
            label = min(candidates, key=lambda region: abs(_split_polygon_area(region.vertices))).label
        else:
            gx, gy = _split_full_to_grid(x, y, self._split_display_shape(idx)[1], self._split_display_shape(idx)[0], analysis.grid_width, analysis.grid_height)
            label = _split_nearest_label_to_point(analysis.labels, gx, gy, radius=5)
        if label:
            label = int(label)
            selected = set(state.get("selected_labels", set()) or set())
            if label in selected:
                selected.remove(label)
                self._split_status(f"Unhighlighted split piece {label}.")
            else:
                selected.add(label)
                self._split_status(f"Highlighted split piece {label}.")
            state["selected_labels"] = selected
            return int(label)
        state["selected_labels"] = set()
        self._split_status("No split piece under cursor.")
        return 0

    def _split_nudge_selected(self, idx, direction, key_text=""):
        state = self._split_state(idx)
        analysis = state.get("analysis")
        if not isinstance(analysis, SplitRegionAnalysis) or not analysis.regions:
            self._split_status("Draw split lines before nudging pieces.")
            return True
        shape = self._split_display_shape(idx)
        extent = self._split_panel_extent(idx)
        if shape is None or extent is None:
            return True
        h, w = shape
        left, right, bottom, top = extent
        step_x = abs(right - left) / max(float(w), 1.0)
        step_y = abs(top - bottom) / max(float(h), 1.0)
        lowered = str(key_text or "").lower()
        multiplier = 1.0
        if "shift" in lowered:
            multiplier = 10.0
        elif "ctrl" in lowered or "control" in lowered:
            multiplier = 0.25
        labels = set(state.get("selected_labels", set()) or set())
        if not labels:
            labels = {int(region.label) for region in analysis.regions}
        offsets = dict(state.get("piece_offsets", {}) or {})
        for label in labels:
            dx, dy = offsets.get(label, (0.0, 0.0))
            if direction == "left":
                dx -= step_x * multiplier
            elif direction == "right":
                dx += step_x * multiplier
            elif direction == "up":
                dy += step_y * multiplier
            elif direction == "down":
                dy -= step_y * multiplier
            offsets[label] = (float(dx), float(dy))
        state["piece_offsets"] = offsets
        noun = "selected piece" if state.get("selected_labels") else "all pieces"
        self._refresh_split_artists(idx, draw=True)
        self._split_status(f"Nudged {noun} {direction}.")
        return True

    def _split_translate_selected(self, idx, dx_world, dy_world):
        labels = self._split_selected_labels(idx, fallback_all=False)
        if not labels:
            self._split_status("Highlight split section(s) before using W/A/S/D to move them.")
            return True
        state = self._split_state(idx)
        tx, ty = self._split_world_delta_to_pixel_delta(idx, dx_world, dy_world)
        delta_tform = AffineTransform(translation=(tx, ty))
        tforms = dict(state.get("piece_tforms", {}) or {})
        offsets = dict(state.get("piece_offsets", {}) or {})
        for label in labels:
            current = self._split_piece_tform(state, label, idx=idx)
            tforms[label] = _compose_affine_transforms(current, delta_tform)
            offsets.pop(label, None)
        state["piece_tforms"] = tforms
        state["piece_offsets"] = offsets
        self._refresh_split_artists(idx, draw=True)
        return True

    def _split_undo_last(self, idx):
        state = self._split_state(idx)
        segments = list(state.get("segments", []) or [])
        if not segments:
            self._split_status("No split linework to undo.")
            return True
        group_id = segments[-1].group_id
        state["segments"] = [seg for seg in segments if seg.group_id != group_id]
        self._split_rebuild_regions(idx)
        self._refresh_split_artists(idx, draw=True)
        self._split_status("Undid last split cut.")
        return True

    def _split_clear_panel(self, idx):
        self._reset_split_state(idx, draw=True)
        self._split_status("Cleared split linework for this panel.")
        return True

    def _split_delete_selected(self, idx):
        state = self._split_state(idx)
        labels = self._split_selected_labels(idx, fallback_all=False)
        if not labels:
            self._split_status("Highlight a split section before pressing Delete.")
            return True
        deleted = set(state.get("deleted_labels", set()) or set())
        deleted.update(labels)
        state["deleted_labels"] = deleted
        state["selected_labels"] = set()
        self._refresh_split_artists(idx, draw=True)
        count = len(labels)
        suffix = "" if count == 1 else "s"
        self._split_status(f"Deleted {count} highlighted split section{suffix}.")
        return True

    def _split_warp_state(self, idx):
        state = self._split_state(idx)
        warp = state.get("warp")
        if not isinstance(warp, dict):
            warp = {}
            state["warp"] = warp
        list_keys = ("src_pix", "dst_pix", "point_order", "markers", "labels", "labels_to_warp")
        for key in list_keys:
            if not isinstance(warp.get(key), list):
                warp[key] = []
        warp["collecting"] = bool(warp.get("collecting", False))
        if "banner" not in warp:
            warp["banner"] = None
        if "absolute_label" not in warp:
            warp["absolute_label"] = None
        return warp

    def _split_clear_warp_artifacts(self, idx, keep_collecting=False):
        warp = self._split_warp_state(idx)
        for marker in list(warp.get("markers", []) or []):
            try:
                marker.remove()
            except Exception:
                pass
        for label in list(warp.get("labels", []) or []):
            try:
                label.remove()
            except Exception:
                pass
        banner = warp.get("banner")
        if banner is not None:
            try:
                banner.remove()
            except Exception:
                pass
        warp["markers"] = []
        warp["labels"] = []
        warp["src_pix"] = []
        warp["dst_pix"] = []
        warp["point_order"] = []
        warp["labels_to_warp"] = []
        warp["absolute_label"] = None
        warp["banner"] = None
        if not keep_collecting:
            warp["collecting"] = False

    def _split_warp_cursor_pixel(self, idx, ev):
        point = None
        if ev is not None and getattr(ev, "inaxes", None) in getattr(self, "axes", []):
            try:
                event_idx = self.axes.index(ev.inaxes)
            except Exception:
                event_idx = idx
            if event_idx == idx:
                point = self._split_event_to_pixel(idx, ev)
        if point is None:
            point = self._split_state(idx).get("cursor")
        return point

    def _split_add_warp_marker(self, idx, point, marker_color, point_number):
        world = self._split_pixel_to_world(idx, point[0], point[1])
        if world is None:
            return
        ax = self.axes[idx]
        warp = self._split_warp_state(idx)
        mark, = ax.plot(world[0], world[1], 'o', color=marker_color, markersize=6, zorder=8)
        warp["markers"].append(mark)
        lbl = ax.text(
            world[0],
            world[1],
            str(point_number),
            color='white',
            fontsize=self._scaled_main_panel_fontsize(8.0, minimum=4.0, maximum=32.0),
            zorder=8.2,
        )
        self._set_panel_text_artist_fontsize(lbl, 8.0, minimum=4.0, maximum=32.0)
        warp["labels"].append(lbl)

    def _split_handle_warp_key(self, ev, idx, key):
        if not bool(getattr(self, "split_mode", False)):
            return False
        base_key = str(key or "").lower().split("+")[-1]
        if base_key not in ("g", "h", "j", "backspace"):
            return False
        if not self._panel_has_loaded_image(idx):
            return True
        state = self._split_state(idx)
        warp = self._split_warp_state(idx)

        if base_key == "g":
            if not warp.get("collecting", False):
                labels = sorted(self._split_selected_labels(idx, fallback_all=False))
                if not labels:
                    self._split_status("Highlight split section(s) before pressing G to warp them.")
                    return True
                self._split_clear_warp_artifacts(idx)
                warp = self._split_warp_state(idx)
                warp["collecting"] = True
                warp["labels_to_warp"] = labels
                warp["absolute_label"] = labels[0] if len(labels) == 1 else None
                banner = self.axes[idx].text(
                    0.5,
                    0.95,
                    "Warping split section...",
                    ha='center',
                    transform=self.axes[idx].transAxes,
                    color=self.theme['text'],
                    fontsize=self._scaled_main_panel_fontsize(12.0, minimum=5.0, maximum=48.0),
                    zorder=8,
                )
                self._set_panel_text_artist_fontsize(banner, 12.0, minimum=5.0, maximum=48.0)
                warp["banner"] = banner
                self._sync_button_visibility()
                self.canvas.draw_idle()
                self._split_status("Split warp: press H on source points, J on target points, then G to apply.")
                return True

            labels = sorted(int(label) for label in (warp.get("labels_to_warp") or self._split_selected_labels(idx, fallback_all=False)))
            pair_count = min(len(warp.get("src_pix", [])), len(warp.get("dst_pix", [])))
            if pair_count >= 3 and labels:
                src_pts = np.asarray(warp["src_pix"][:pair_count], dtype=float)
                dst_pts = np.asarray(warp["dst_pix"][:pair_count], dtype=float)
                try:
                    tform = estimate_transform('affine', src_pts, dst_pts)
                except Exception:
                    tform = None
                if tform is not None and np.all(np.isfinite(np.asarray(tform.params, dtype=float))):
                    tforms = dict(state.get("piece_tforms", {}) or {})
                    offsets = dict(state.get("piece_offsets", {}) or {})
                    absolute_label = warp.get("absolute_label")
                    for label in labels:
                        current = self._split_piece_tform(state, label, idx=idx)
                        if absolute_label == label:
                            tforms[label] = tform
                        else:
                            tforms[label] = _compose_affine_transforms(current, tform)
                        offsets.pop(label, None)
                    state["piece_tforms"] = tforms
                    state["piece_offsets"] = offsets
                    self._split_status(f"Applied split warp to {len(labels)} highlighted section(s).")
                else:
                    self._split_status("Split warp could not estimate a transform from those points.")
            else:
                self._split_status("Split warp canceled: add at least 3 source and 3 target points.")
            self._split_clear_warp_artifacts(idx)
            self._sync_button_visibility()
            self._refresh_split_artists(idx, draw=True)
            return True

        if base_key in ("h", "j"):
            if not warp.get("collecting", False):
                self._split_status("Press G first to start warping highlighted split section(s).")
                return True
            point = self._split_warp_cursor_pixel(idx, ev)
            if point is None:
                self._split_status("Move the cursor over the panel before adding split warp points.")
                return True
            visible_point = (float(point[0]), float(point[1]))
            if base_key == "h":
                source_point = visible_point
                absolute_label = warp.get("absolute_label")
                if absolute_label is not None:
                    current = self._split_piece_tform(state, int(absolute_label), idx=idx)
                    try:
                        source_point = tuple(np.asarray(current.inverse(np.asarray([visible_point], dtype=float)))[0])
                    except Exception:
                        source_point = visible_point
                warp["src_pix"].append((float(source_point[0]), float(source_point[1])))
                warp["point_order"].append("src")
                marker_color = normalize_keep_reject_button_color(
                    getattr(self, "warp_source_color", DEFAULT_WARP_SOURCE_COLOR),
                    DEFAULT_WARP_SOURCE_COLOR,
                )
                point_number = len(warp["src_pix"])
            else:
                warp["dst_pix"].append(visible_point)
                warp["point_order"].append("dst")
                marker_color = normalize_keep_reject_button_color(
                    getattr(self, "warp_target_color", DEFAULT_WARP_TARGET_COLOR),
                    DEFAULT_WARP_TARGET_COLOR,
                )
                point_number = len(warp["dst_pix"])
            self._split_add_warp_marker(idx, visible_point, marker_color, point_number)
            self.canvas.draw_idle()
            return True

        if base_key == "backspace" and warp.get("collecting", False):
            if warp.get("markers"):
                try:
                    warp["markers"].pop().remove()
                except Exception:
                    pass
            if warp.get("labels"):
                try:
                    warp["labels"].pop().remove()
                except Exception:
                    pass
            point_kind = warp["point_order"].pop() if warp.get("point_order") else None
            if point_kind == "dst":
                if warp.get("dst_pix"):
                    warp["dst_pix"].pop()
            else:
                if warp.get("src_pix"):
                    warp["src_pix"].pop()
            self.canvas.draw_idle()
            return True
        return base_key == "backspace"

    def _handle_split_key(self, ev, idx, key):
        raw_key = str(key or "").lower()
        base_key = raw_key.split("+")[-1]
        if base_key == "m":
            return self._toggle_split_mode(idx)
        if not bool(getattr(self, "split_mode", False)):
            return False
        if base_key in ("escape", "esc"):
            return self._toggle_split_mode(idx)
        if not self._panel_has_loaded_image(idx):
            return True
        state = self._split_state(idx)
        if base_key == "v":
            return self._split_keyboard_draw_key(idx, "line")
        if base_key == "n":
            return self._split_keyboard_draw_key(idx, "rect")
        if base_key == "c":
            point = None
            if ev.inaxes in getattr(self, "axes", []):
                point = self._split_event_to_pixel(idx, ev)
            if point is None:
                point = state.get("cursor")
            if point is None:
                self._split_status("Move the cursor over a split piece before pressing C.")
            else:
                self._split_select_at_pixel(idx, point)
                self._refresh_split_artists(idx, draw=True)
            return True
        if base_key in ("b", "z"):
            return self._split_undo_last(idx)
        if base_key in ("delete", "del"):
            return self._split_delete_selected(idx)
        return False

    def _split_transparent_cmap(self):
        try:
            cmap = self._display_cmap().copy()
        except Exception:
            base = self._display_cmap()
            cmap = mcolors.ListedColormap(base(np.linspace(0, 1, getattr(base, "N", 256))))
        try:
            cmap.set_bad((0.0, 0.0, 0.0, 0.0))
        except Exception:
            pass
        return cmap

    def _split_norm_for_display(self, idx, display):
        try:
            data_range = self.current_display_ranges.get(idx)
        except Exception:
            data_range = None
        dmin, dmax = self._display_range_from_data(display, data_range)
        clim = self._relative_clim(dmin, dmax)
        if clim is None:
            if dmin is None or dmax is None:
                return None
            clim = (dmin, dmax)
        vmin, vmax = clim
        if vmin is None or vmax is None:
            return None
        try:
            vmin = float(vmin)
            vmax = float(vmax)
        except Exception:
            return None
        if not np.isfinite([vmin, vmax]).all():
            return None
        if vmax <= vmin:
            vmax = vmin + 1e-12
        return mcolors.PowerNorm(
            gamma=max(1e-12, float(getattr(self, "global_gamma", 1.0))),
            vmin=vmin,
            vmax=vmax,
        )

    def _split_region_mask(self, idx, region):
        shape = self._split_display_shape(idx)
        if shape is None:
            return None
        h, w = shape
        try:
            return _split_polygon_mask(float(w), float(h), region.vertices, max_size=None)
        except Exception:
            return None

    def _split_build_display_composite(self, idx, display, regions, masks):
        shape = self._split_display_shape(idx)
        if shape is None:
            return None
        h, w = shape
        if np.asarray(display).ndim != 2:
            return display
        state = self._split_state(idx)
        deleted = set(state.get("deleted_labels", set()) or set())
        accum = np.zeros((h, w), dtype=np.float64)
        count = np.zeros((h, w), dtype=np.float32)
        combined_all = np.zeros((h, w), dtype=bool)
        finite = np.isfinite(display)

        for region in regions:
            mask = masks.get(region.label)
            if mask is not None:
                combined_all |= mask

        remainder_valid = (~combined_all) & finite
        if np.any(remainder_valid):
            accum[remainder_valid] += np.asarray(display, dtype=np.float64)[remainder_valid]
            count[remainder_valid] += 1.0

        for region in regions:
            if region.label in deleted:
                continue
            mask = masks.get(region.label)
            if mask is None or not np.any(mask):
                continue
            tform = self._split_piece_tform(state, region.label, idx=idx)
            piece = np.full((h, w), np.nan, dtype=np.float32)
            valid_source = mask & finite
            piece[valid_source] = np.asarray(display, dtype=np.float32)[valid_source]
            try:
                warped = skwarp(
                    piece,
                    inverse_map=tform.inverse,
                    output_shape=(h, w),
                    cval=np.nan,
                    preserve_range=True,
                    order=0,
                )
                warped_mask = skwarp(
                    mask.astype(np.float32),
                    inverse_map=tform.inverse,
                    output_shape=(h, w),
                    cval=0.0,
                    preserve_range=True,
                    order=0,
                ) > 0.5
            except Exception:
                warped = piece
                warped_mask = mask
            valid = warped_mask & np.isfinite(warped)
            if np.any(valid):
                accum[valid] += np.asarray(warped, dtype=np.float64)[valid]
                count[valid] += 1.0

        out = np.full((h, w), np.nan, dtype=np.float32)
        valid_out = count > 0
        out[valid_out] = (accum[valid_out] / np.maximum(count[valid_out], 1.0)).astype(np.float32)
        return out

    def _refresh_split_artists(self, idx, draw=False):
        if idx < 0 or idx >= getattr(self, "n_pan", 0):
            return
        self._ensure_split_data()
        state = self._split_state(idx)
        current_path = str(self.current[idx] or "") if idx < len(getattr(self, "current", [])) else ""
        if state.get("path") and state.get("path") != current_path:
            self._reset_split_state(idx, draw=False)
            state = self._split_state(idx)
        state["path"] = current_path
        self._clear_split_artists(idx)

        if not current_path or not self._panel_has_image(idx):
            return
        ax = self.axes[idx]
        display = self._split_display_array(idx)
        shape = self._split_display_shape(idx)
        extent = self._split_panel_extent(idx)
        if display is None or shape is None or extent is None:
            return
        h, w = shape
        left, right, bottom, top = extent
        analysis = state.get("analysis")
        regions = list(analysis.regions) if isinstance(analysis, SplitRegionAnalysis) else []

        try:
            img_obj = self.images.get(idx) if hasattr(self.images, "get") else self.images[idx]
        except Exception:
            img_obj = None

        if regions:
            if img_obj is not None:
                try:
                    img_obj.set_visible(False)
                except Exception:
                    pass
            cmap = self._split_transparent_cmap()
            norm = self._split_norm_for_display(idx, display)
            interpolation = visual_resampling_mpl_interpolation(
                getattr(self, "thermal_visual_resampling", "nearest")
            )
            image_kwargs = {
                "origin": "upper",
                "cmap": cmap,
                "interpolation": interpolation,
                "aspect": "auto",
                "alpha": max(0.0, min(1.0, float(getattr(self, "thermal_alpha", 1.0)))),
            }
            if norm is not None:
                image_kwargs["norm"] = norm

            masks = {}
            for region in regions:
                mask = self._split_region_mask(idx, region)
                if mask is None or mask.shape != (h, w):
                    continue
                masks[region.label] = mask
            composite = self._split_build_display_composite(idx, display, regions, masks)
            if composite is not None:
                artist = ax.imshow(
                    np.ma.masked_invalid(composite),
                    extent=[left, right, bottom, top],
                    zorder=1.18,
                    **image_kwargs,
                )
                try:
                    artist.set_gid("split_composite")
                except Exception:
                    pass
                state["artists"].append(artist)
        else:
            if img_obj is not None:
                try:
                    img_obj.set_visible(True)
                except Exception:
                    pass

        draw_linework = bool(getattr(self, "split_mode", False))
        selected = set(state.get("selected_labels", set()) or set())
        deleted = set(state.get("deleted_labels", set()) or set())
        if draw_linework:
            for seg in list(state.get("segments", []) or []):
                p1 = self._split_pixel_to_world(idx, seg.x1, seg.y1)
                p2 = self._split_pixel_to_world(idx, seg.x2, seg.y2)
                if p1 is None or p2 is None:
                    continue
                line = Line2D(
                    [p1[0], p2[0]],
                    [p1[1], p2[1]],
                    color="#FFD60A",
                    linewidth=1.4 if seg.solid else 1.0,
                    linestyle="-" if seg.solid else (0, (2, 3)),
                    zorder=6,
                )
                ax.add_line(line)
                state["artists"].append(line)

        if regions and (draw_linework or selected or deleted):
            for region in regions:
                world = self._split_transformed_vertices_to_world(idx, region)
                if len(world) < 3:
                    continue
                xs = [p[0] for p in world] + [world[0][0]]
                ys = [p[1] for p in world] + [world[0][1]]
                is_selected = region.label in selected
                is_deleted = region.label in deleted
                if is_deleted:
                    fill = ax.fill(xs, ys, color="#FF3B30", alpha=0.20, zorder=5.4)
                    state["artists"].extend(fill)
                elif is_selected:
                    fill = ax.fill(xs, ys, color="#00E5FF", alpha=0.18, zorder=5.5)
                    state["artists"].extend(fill)
                outline = Line2D(
                    xs,
                    ys,
                    color="#FF3B30" if is_deleted else ("#00E5FF" if is_selected else "#34C759"),
                    linewidth=2.2 if (is_selected or is_deleted) else 0.9,
                    alpha=0.95 if (is_selected or is_deleted) else 0.65,
                    linestyle=(0, (4, 3)) if is_deleted else "-",
                    zorder=7 if is_selected else (6.8 if is_deleted else 5.8),
                )
                ax.add_line(outline)
                state["artists"].append(outline)

        if draw:
            try:
                self.canvas.draw_idle()
            except Exception:
                pass

    def _clear_panel_slot(self, idx: int, message: str = 'EMPTY', fontsize: int = 14):
        if idx < 0 or idx >= getattr(self, "n_pan", 0):
            return

        self.current[idx] = None
        self.offsets[idx] = [0, 0]
        self._reset_split_state(idx, draw=False)

        data = self.warp_data.get(idx, {})
        for marker in data.get('markers', []):
            try:
                marker.remove()
            except Exception:
                pass
        for label in data.get('labels', []):
            try:
                label.remove()
            except Exception:
                pass
        self.warp_data[idx] = {
            "src_world": [], "dst_world": [],
            "src_pix": [],   "dst_pix": [],
            "tform": None,   "applied": False,
            "collecting": False, "markers": [], "labels": []
        }

        self.images[idx] = None
        if hasattr(self, "basemap_images"):
            self.basemap_images[idx] = None
        try:
            getattr(self, "_dynamic_basemap_view_keys", {}).pop(idx, None)
        except Exception:
            pass
        self.images_data[idx] = None
        if hasattr(self, "current_display_data"):
            self.current_display_data[idx] = None
        if hasattr(self, "current_display_ranges"):
            self.current_display_ranges[idx] = (None, None)
        self.bases.pop(idx, None)
        self.srccrs.pop(idx, None)
        self.srctrans.pop(idx, None)
        self.edge_cache.pop(idx, None)
        self.data_ranges.pop(idx, None)
        self.panel_views.pop(idx, None)

        ax = self.axes[idx]
        ax.clear()
        self._style_panel_axes(ax)
        self._clear_panel_title(ax)
        self._clear_basemap_delta_label(ax)
        ax.set_axis_off()
        base_fontsize = float(fontsize)
        message_artist = ax.text(
            0.5, 0.5, message,
            ha='center', va='center',
            transform=ax.transAxes,
            color=self.theme['empty'],
            fontsize=self._scaled_main_panel_fontsize(base_fontsize, minimum=5.0, maximum=64.0)
        )
        self._set_panel_text_artist_fontsize(message_artist, base_fontsize, minimum=5.0, maximum=64.0)
        if getattr(self, "active_idx", None) == idx:
            replacement_idx = self._first_loaded_panel_index()
            if replacement_idx is not None:
                self.active_idx = replacement_idx
        try:
            self.ensure_selectors()
        except Exception:
            pass
        self._sync_button_visibility(idx)

    def _do_full_reset(self, idx: int):
        """Reset zoom to full TIF AOI, clear pan and any in-memory warp."""
        if not self._panel_has_image(idx):
            self._sync_button_visibility(idx)
            return

        # Full scene
        self.aoi_bounds = None
        self.offsets[idx] = [0, 0]
        self._reset_split_state(idx, draw=False)

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
        self._set_panel_view(ax, (L, R), (B, T))

        # Make sure no lingering toolbar tool is active
        self.kill_toolbar_tools()
        self.canvas.draw_idle()
        self._ensure_focus()

    def _apply_rel_contrast_state(self, idx, img_obj, data_min, data_max):
        """Reapply saved relative-contrast (center_rel, half_rel) globally."""
        clim = self._relative_clim(data_min, data_max)
        if clim is None:
            return
        vmin_new, vmax_new = clim
        try:
            img_obj.set_clim(vmin_new, vmax_new)  # unclamped; "blow out" allowed
        except Exception:
            pass

    def _clear_vector_overlays(self, ax):
        for coll in list(getattr(ax, "collections", [])):
            try:
                if coll.get_gid() == "vector_overlay":
                    coll.remove()
            except Exception:
                pass

    def _plot_vector_overlays(self, ax):
        try:
            try:
                saved_xlim = ax.get_xlim()
                saved_ylim = ax.get_ylim()
            except Exception:
                saved_xlim = saved_ylim = None
            try:
                saved_pos = ax.get_position().frozen()
            except Exception:
                saved_pos = None
            try:
                autoscale_x = bool(ax.get_autoscalex_on())
                autoscale_y = bool(ax.get_autoscaley_on())
            except Exception:
                autoscale_x = autoscale_y = True
            try:
                ax.set_autoscalex_on(False)
                ax.set_autoscaley_on(False)
            except Exception:
                pass

            linewidth = max(0.1, float(getattr(self, "shp_linewidth", 1.2)))
            if getattr(self, "shp_primary", None) is not None:
                before = len(ax.collections)
                self.shp_primary.plot(
                    ax=ax, facecolor='none', edgecolor=self.shp_primary_color,
                    linewidth=linewidth, zorder=2
                )
                for coll in list(ax.collections)[before:]:
                    try:
                        coll.set_gid("vector_overlay")
                    except Exception:
                        pass

            for ov in getattr(self, "shp_overlays", []):
                if isinstance(ov, dict):
                    gdf = ov.get("gdf")
                    color = ov.get("color", "dodgerblue")
                else:
                    gdf, color = ov
                if gdf is None:
                    continue
                before = len(ax.collections)
                gdf.plot(
                    ax=ax, facecolor='none', edgecolor=color,
                    linewidth=linewidth, zorder=2
                )
                for coll in list(ax.collections)[before:]:
                    try:
                        coll.set_gid("vector_overlay")
                    except Exception:
                        pass

            if saved_pos is not None:
                try:
                    ax.set_position(saved_pos)
                except Exception:
                    pass
            try:
                self._style_panel_axes(ax)
            except Exception:
                pass
            if saved_xlim is not None and saved_ylim is not None:
                try:
                    ax.set_xlim(*saved_xlim)
                    ax.set_ylim(*saved_ylim)
                except Exception:
                    pass
            try:
                ax.set_autoscalex_on(autoscale_x)
                ax.set_autoscaley_on(autoscale_y)
            except Exception:
                pass
        except Exception:
            pass

    def _refresh_vector_overlays_all(self):
        for i, ax in enumerate(getattr(self, "axes", [])):
            if i >= getattr(self, "n_pan", 0):
                continue
            if not getattr(self, "current", [None] * (i + 1))[i]:
                continue
            view = getattr(self, "panel_views", {}).get(i)
            if view is None:
                try:
                    view = (ax.get_xlim(), ax.get_ylim())
                except Exception:
                    view = None
            self._clear_vector_overlays(ax)
            self._plot_vector_overlays(ax)
            if view is not None:
                if not self._set_panel_view_exact(ax, view[0], view[1], remember=False, idx=i):
                    self._set_panel_view(ax, view[0], view[1], remember=False)
        self.canvas.draw_idle()

    # ----- focus / shortcut plumbing (lazy so you don't edit __init__) -----
    def _ensure_shortcuts_once(self):
        if getattr(self, "_shortcuts_ready", False):
            return
        self._shortcuts_ready = True

    def _on_shift_shortcut(self):
        return

    def _ensure_focus(self):
        try:
            # Make sure the window and canvas are willing to take key focus
            self.setFocusPolicy(QtCore.Qt.StrongFocus)
            self.canvas.setFocusPolicy(QtCore.Qt.StrongFocus)
            self.canvas.setFocus()
        except Exception:
            pass

    def _build_axes_grid(self, clear_fig: bool = False):
        """(Re)build the Matplotlib axes grid from self.grid_rows/self.grid_cols."""
        if clear_fig:
            self.fig.clear()
            self.fig.set_facecolor(self.theme['figure_bg'])
            try:
                self.fig.set_constrained_layout(False)
            except Exception:
                pass

        axs = self.fig.subplots(self.grid_rows, self.grid_cols)
        try:
            self.panel_layout_settings = normalize_panel_layout_settings(
                getattr(self, "panel_layout_settings", DEFAULT_PANEL_LAYOUT_SETTINGS)
            )
            self.fig.subplots_adjust(**self.panel_layout_settings)
        except Exception:
            pass
        if isinstance(axs, np.ndarray):
            axs_list = axs.ravel().tolist()
        else:
            axs_list = [axs]
        self.axes = axs_list
        for ax in self.axes:
            self._style_panel_axes(ax)

    def _refresh_current_panel_layout(self, fit_to_scene: bool = False):
        preserved_views = {}
        if not fit_to_scene:
            for idx, ax in enumerate(getattr(self, "axes", [])):
                if idx >= getattr(self, "n_pan", 0):
                    continue
                if not self._panel_has_image(idx):
                    continue
                try:
                    preserved_views[idx] = (ax.get_xlim(), ax.get_ylim())
                except Exception:
                    pass

        self._build_axes_grid(clear_fig=True)
        for idx in range(getattr(self, "n_pan", 0)):
            self.draw(idx, refresh_selectors=False)
            if fit_to_scene:
                self._fit_panel_to_current_scene(idx)
            elif idx in preserved_views:
                try:
                    xlim, ylim = preserved_views[idx]
                    if not self._set_panel_view_exact(self.axes[idx], xlim, ylim, idx=idx):
                        self._set_panel_view(self.axes[idx], xlim, ylim, sync=False)
                except Exception:
                    pass

        self.ensure_selectors()
        self.add_keep_reject_buttons()
        self._sync_button_visibility()
        self.kill_toolbar_tools()
        self._pending_layout_autofit = bool(fit_to_scene)
        try:
            self.canvas.draw()
        except Exception:
            self.canvas.draw_idle()
        self._schedule_panel_geometry_sync(delay_ms=0)

    def _open_layout_dialog(self):
        """Backslash: open panel grid dialog (across × down)."""
        try:
            dlg = PanelLayoutDialog(
                cols=self.grid_cols,
                rows=self.grid_rows,
                panel_layout_settings=getattr(self, "panel_layout_settings", DEFAULT_PANEL_LAYOUT_SETTINGS),
                button_layout_settings=getattr(
                    self,
                    "keep_reject_button_layout_settings",
                    DEFAULT_KEEP_REJECT_BUTTON_LAYOUT_SETTINGS,
                ),
                sync_zoom_pan=bool(getattr(self, "sync_zoom_pan", False)),
                scroll_wheel_pan_multi_enabled=bool(
                    getattr(self, "scroll_wheel_pan_multi_enabled", True)
                ),
                button_preset=getattr(self, "keep_reject_button_preset", DEFAULT_KEEP_REJECT_PRESET_ID),
                keep_color=getattr(
                    self,
                    "keep_button_color",
                    KEEP_REJECT_BUTTON_PRESET_BY_ID[DEFAULT_KEEP_REJECT_PRESET_ID]["keep"],
                ),
                reject_color=getattr(
                    self,
                    "reject_button_color",
                    KEEP_REJECT_BUTTON_PRESET_BY_ID[DEFAULT_KEEP_REJECT_PRESET_ID]["reject"],
                ),
                parent=self,
            )
            if dlg.exec_() == QDialog.Accepted:
                vals = dlg.values()
                new_cols = int(vals.get("cols", self.grid_cols))
                new_rows = int(vals.get("rows", self.grid_rows))
                grid_changed = (
                    new_cols != int(getattr(self, "grid_cols", 3))
                    or new_rows != int(getattr(self, "grid_rows", 1))
                )
                new_panel_layout_settings = normalize_panel_layout_settings(
                    vals.get("panel_layout_settings")
                )
                panel_layout_changed = (
                    new_panel_layout_settings
                    != normalize_panel_layout_settings(
                        getattr(self, "panel_layout_settings", DEFAULT_PANEL_LAYOUT_SETTINGS)
                    )
                )
                new_button_layout_settings = normalize_keep_reject_button_layout_settings(
                    vals.get("button_layout_settings")
                )
                new_sync_zoom_pan = bool(vals.get("sync_zoom_pan", getattr(self, "sync_zoom_pan", False)))
                sync_zoom_pan_changed = (
                    new_sync_zoom_pan != bool(getattr(self, "sync_zoom_pan", False))
                )
                new_scroll_wheel_pan_multi_enabled = bool(
                    vals.get(
                        "scroll_wheel_pan_multi_enabled",
                        getattr(self, "scroll_wheel_pan_multi_enabled", True),
                    )
                )
                scroll_wheel_pan_multi_changed = (
                    new_scroll_wheel_pan_multi_enabled
                    != bool(getattr(self, "scroll_wheel_pan_multi_enabled", True))
                )
                button_layout_changed = (
                    new_button_layout_settings
                    != normalize_keep_reject_button_layout_settings(
                        getattr(
                            self,
                            "keep_reject_button_layout_settings",
                            DEFAULT_KEEP_REJECT_BUTTON_LAYOUT_SETTINGS,
                        )
                    )
                )
                self._set_keep_reject_button_colors(vals.get("button_colors"))
                self.panel_layout_settings = dict(new_panel_layout_settings)
                self.keep_reject_button_layout_settings = dict(new_button_layout_settings)
                self.sync_zoom_pan = new_sync_zoom_pan
                self.scroll_wheel_pan_multi_enabled = new_scroll_wheel_pan_multi_enabled
                self._pending_layout_autofit = bool(grid_changed or panel_layout_changed)
                if grid_changed:
                    self.set_panel_layout(cols=new_cols, rows=new_rows)
                elif panel_layout_changed:
                    self._refresh_current_panel_layout(fit_to_scene=True)
                elif button_layout_changed:
                    self.add_keep_reject_buttons()
                if sync_zoom_pan_changed and self.sync_zoom_pan:
                    self._sync_panel_view_from_index(getattr(self, "active_idx", 0))
                    try:
                        self.statusBar().showMessage("Sync Zoom/Pan enabled.", 4000)
                    except Exception:
                        pass
                elif sync_zoom_pan_changed:
                    try:
                        self.statusBar().showMessage("Sync Zoom/Pan disabled.", 4000)
                    except Exception:
                        pass
                if scroll_wheel_pan_multi_changed:
                    try:
                        state = "enabled" if self.scroll_wheel_pan_multi_enabled else "disabled"
                        self.statusBar().showMessage(f"Scroll wheel pan multiplier {state}.", 4000)
                    except Exception:
                        pass
                self._sync_button_visibility()
                try:
                    self.canvas.draw()
                except Exception:
                    self.canvas.draw_idle()
        except Exception as e:
            try:
                QMessageBox.warning(self, "Layout", f"Could not open layout dialog: {e}")
            except Exception:
                pass

    def _refresh_warp_marker_colors(self):
        source_color = normalize_keep_reject_button_color(
            getattr(self, "warp_source_color", DEFAULT_WARP_SOURCE_COLOR),
            DEFAULT_WARP_SOURCE_COLOR,
        )
        target_color = normalize_keep_reject_button_color(
            getattr(self, "warp_target_color", DEFAULT_WARP_TARGET_COLOR),
            DEFAULT_WARP_TARGET_COLOR,
        )
        warp_data = getattr(self, "warp_data", {}) or {}
        if isinstance(warp_data, dict):
            warp_items = warp_data.values()
        else:
            warp_items = warp_data
        for data in warp_items:
            if not isinstance(data, dict):
                continue
            markers = list(data.get("markers", []) or [])
            point_order = list(data.get("point_order", []) or [])
            for marker, point_kind in zip(markers, point_order):
                try:
                    marker.set_color(source_color if point_kind == "src" else target_color)
                except Exception:
                    pass

    def _open_colormap_dialog(self):
        if getattr(self, "_colormap_dialog_opening", False):
            try:
                active = QtWidgets.QApplication.activeModalWidget()
                if isinstance(active, ColormapPickerDialog):
                    active.raise_()
                    active.activateWindow()
            except Exception:
                pass
            return

        self._colormap_dialog_opening = True
        try:
            return self._open_colormap_dialog_impl()
        finally:
            self._colormap_dialog_opening = False

    def _open_colormap_dialog_impl(self):
        """Open display options for imagery colormap and loaded vector colors (Tab key).

        Selecting a colormap:
          - applies it immediately to all panels
          - sets it as the alternate used by the [R] toggle (gray <-> alt)
        Vector color, NaN, line-width, and footer changes update in place.
        """
        # Cache the list once (it's large)
        try:
            if getattr(self, "_all_cmaps", None) is None:
                self._all_cmaps = sorted(list(plt.colormaps()))
        except Exception:
            try:
                from matplotlib import cm as _cm
                self._all_cmaps = sorted(list(getattr(_cm, "cmap_d", {}).keys()))
            except Exception:
                self._all_cmaps = ["gray", "magma"]

        vector_items = []
        if getattr(self, "shp_primary", None) is not None:
            vector_items.append({
                "label": f"Primary: {getattr(self, 'shp_primary_name', 'shapefile') or 'shapefile'}",
                "color": getattr(self, "shp_primary_color", "cyan"),
                "name": getattr(self, "shp_primary_name", ""),
                "slot_kind": "primary",
                "source_id": "primary",
            })
        for j, ov in enumerate(getattr(self, "shp_overlays", []), start=1):
            if isinstance(ov, dict):
                vector_items.append({
                    "label": f"Overlay {j}: {ov.get('name', f'shapefile {j}')}",
                    "color": ov.get("color", "dodgerblue"),
                    "name": ov.get("name", f"shapefile {j}"),
                    "slot_kind": "overlay",
                    "source_id": f"overlay:{j - 1}",
                })
            else:
                vector_items.append({
                    "label": f"Overlay {j}",
                    "color": ov[1] if len(ov) > 1 else "dodgerblue",
                    "name": "",
                    "slot_kind": "overlay",
                    "source_id": f"overlay:{j - 1}",
                })

        current = getattr(self, "cmap_mode", "gray")
        dlg = ColormapPickerDialog(
            self._all_cmaps,
            current=current,
            vector_items=vector_items,
            available_shapefile_paths=self._discover_runtime_shapefile_paths(),
            available_basemap_paths=self._discover_runtime_basemap_paths(),
            current_basemap_path=getattr(self, "basemap_path", "") or "",
            current_basemap_mode=getattr(self, "basemap_mode", "nearest"),
            current_basemap_category=getattr(self, "basemap_category", ""),
            current_basemap_resolution_mode=getattr(self, "basemap_resolution_mode", "dynamic"),
            current_basemap_cmap=getattr(self, "basemap_cmap", "gray"),
            current_basemap_color_scaling=getattr(self, "basemap_color_scaling", "normal"),
            max_overlay_slots=5,
            nan_color=getattr(self, "nan_color", self._panel_facecolor()),
            use_theme_nan_color=bool(getattr(self, "use_theme_nan_color", True)),
            shapefile_linewidth=float(getattr(self, "shp_linewidth", 1.2)),
            summary_fontsize=float(getattr(self, "summary_fontsize", 11.0)),
            warp_source_color=getattr(self, "warp_source_color", DEFAULT_WARP_SOURCE_COLOR),
            warp_target_color=getattr(self, "warp_target_color", DEFAULT_WARP_TARGET_COLOR),
            thermal_visual_resampling=getattr(self, "thermal_visual_resampling", "nearest"),
            basemap_visual_resampling=getattr(self, "basemap_visual_resampling", "nearest"),
            parent=self,
        )
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return

        def _path_key(path):
            text = str(path or "").strip()
            return os.path.normcase(os.path.abspath(text)) if text else ""

        def _vector_item_signature(items):
            signature = []
            for item in list(items or []):
                signature.append((
                    str(item.get("slot_kind") or "overlay").strip().lower(),
                    str(item.get("source_id") or "").strip(),
                    normalize_persisted_shapefile_name(item.get("name")),
                    _path_key(item.get("path")),
                    normalize_picker_color_name(item.get("color"), "dodgerblue"),
                    bool(item.get("pending")),
                ))
            return tuple(signature)

        selected_use_theme_nan_color = bool(dlg.selected_use_theme_nan_color())
        selected_nan_color = dlg.selected_nan_color()
        selected_shp_linewidth = float(dlg.selected_shapefile_linewidth())
        selected_summary_fontsize = float(dlg.selected_summary_fontsize())
        selected_warp_source_color = dlg.selected_warp_source_color()
        selected_warp_target_color = dlg.selected_warp_target_color()
        selected_thermal_resampling = dlg.selected_thermal_visual_resampling()
        selected_basemap_resampling = dlg.selected_basemap_visual_resampling()
        selected_basemap_resolution_mode = dlg.selected_basemap_resolution_mode()
        selected_basemap_color_scaling = dlg.selected_basemap_color_scaling()
        selected_basemap_cmap = dlg.selected_basemap_cmap()
        selected_basemap_mode = dlg.selected_basemap_mode()
        selected_basemap_category = dlg.selected_basemap_category()
        selected_basemap_path = str(dlg.selected_basemap_path() or "").strip()
        if selected_basemap_mode == "single" and selected_basemap_path:
            selected_basemap_category = normalize_basemap_category(basemap_category_for_path(selected_basemap_path))
        elif selected_basemap_mode != "single":
            selected_basemap_path = ""
        selected_cmap_name = str(dlg.selected_cmap() or "").strip()
        selected_vector_items = dlg.selected_vector_items()

        old_thermal_resampling = normalize_visual_resampling(
            getattr(self, "thermal_visual_resampling", "nearest")
        )
        old_basemap_resampling = normalize_visual_resampling(
            getattr(self, "basemap_visual_resampling", "nearest")
        )
        old_basemap_resolution_mode = normalize_basemap_resolution_mode(
            getattr(self, "basemap_resolution_mode", "dynamic")
        )
        old_basemap_color_scaling = normalize_basemap_color_scaling(
            getattr(self, "basemap_color_scaling", "normal")
        )
        old_basemap_cmap = normalize_basemap_cmap(
            getattr(self, "basemap_cmap", "gray")
        )
        old_basemap_mode = normalize_basemap_mode(
            getattr(self, "basemap_mode", "nearest")
        )
        old_basemap_category = normalize_basemap_category(
            getattr(self, "basemap_category", "")
        )
        thermal_resampling_changed = selected_thermal_resampling != old_thermal_resampling
        basemap_resampling_changed = selected_basemap_resampling != old_basemap_resampling
        basemap_resolution_changed = selected_basemap_resolution_mode != old_basemap_resolution_mode
        basemap_color_scaling_changed = selected_basemap_color_scaling != old_basemap_color_scaling
        basemap_cmap_changed = selected_basemap_cmap != old_basemap_cmap
        basemap_mode_changed = selected_basemap_mode != old_basemap_mode
        basemap_category_changed = selected_basemap_category != old_basemap_category
        basemap_path_changed = _path_key(selected_basemap_path) != _path_key(getattr(self, "basemap_path", ""))
        cmap_changed = bool(selected_cmap_name) and selected_cmap_name != str(getattr(self, "cmap_mode", "") or "")
        vector_items_changed = _vector_item_signature(selected_vector_items) != _vector_item_signature(vector_items)
        non_resampling_changed = any((
            selected_use_theme_nan_color != bool(getattr(self, "use_theme_nan_color", True)),
            normalize_nan_override_value(selected_nan_color) != normalize_nan_override_value(getattr(self, "nan_color", self._panel_facecolor())),
            abs(selected_shp_linewidth - float(getattr(self, "shp_linewidth", 1.2))) > 1e-9,
            abs(selected_summary_fontsize - float(getattr(self, "summary_fontsize", 11.0))) > 1e-9,
            selected_warp_source_color != normalize_keep_reject_button_color(getattr(self, "warp_source_color", DEFAULT_WARP_SOURCE_COLOR), DEFAULT_WARP_SOURCE_COLOR),
            selected_warp_target_color != normalize_keep_reject_button_color(getattr(self, "warp_target_color", DEFAULT_WARP_TARGET_COLOR), DEFAULT_WARP_TARGET_COLOR),
            basemap_resolution_changed,
            basemap_color_scaling_changed,
            basemap_cmap_changed,
            basemap_mode_changed,
            basemap_category_changed,
            basemap_path_changed,
            cmap_changed,
            vector_items_changed,
        ))
        if (thermal_resampling_changed or basemap_resampling_changed) and not non_resampling_changed:
            self.thermal_visual_resampling = selected_thermal_resampling
            self.basemap_visual_resampling = selected_basemap_resampling
            self._apply_visual_resampling_to_current_artists(
                thermal=thermal_resampling_changed,
                basemap=False,
                draw=False,
            )
            if basemap_resampling_changed:
                self._refresh_basemap_underlays_all(refresh_thermal=False)
            else:
                try:
                    self.canvas.draw_idle()
                except Exception:
                    pass
            return

        preserved_display_views = self._capture_panel_views()

        self.use_theme_nan_color = selected_use_theme_nan_color
        self.nan_color = selected_nan_color
        self.shp_linewidth = selected_shp_linewidth
        self.summary_fontsize = selected_summary_fontsize
        self.warp_source_color = selected_warp_source_color
        self.warp_target_color = selected_warp_target_color
        self._refresh_warp_marker_colors()
        self.thermal_visual_resampling = selected_thermal_resampling
        self.basemap_visual_resampling = selected_basemap_resampling
        self.basemap_resolution_mode = selected_basemap_resolution_mode
        self.basemap_color_scaling = selected_basemap_color_scaling
        self.basemap_cmap = selected_basemap_cmap
        self.basemap_mode = selected_basemap_mode
        self.basemap_category = selected_basemap_category
        if (
            basemap_resolution_changed
            or basemap_color_scaling_changed
            or basemap_cmap_changed
            or basemap_mode_changed
            or basemap_category_changed
        ):
            try:
                self._basemap_display_cache.clear()
            except Exception:
                self._basemap_display_cache = {}
            try:
                self._dynamic_basemap_view_keys.clear()
            except Exception:
                self._dynamic_basemap_view_keys = {}
        basemap_changed = self._set_basemap_path(selected_basemap_path)

        chosen = selected_cmap_name
        if chosen:
            self.alt_cmap = chosen
            self.cmap_mode = chosen

            self._refresh_all_thermal_displays()

        existing_primary = None
        primary_name = normalize_persisted_shapefile_name(getattr(self, "shp_primary_name", ""))
        if getattr(self, "shp_primary", None) is not None:
            existing_primary = {
                "source_id": "primary",
                "name": primary_name,
                "gdf": self.shp_primary,
            }

        existing_overlays = {}
        for overlay_idx, ov in enumerate(getattr(self, "shp_overlays", [])):
            source_id = f"overlay:{overlay_idx}"
            if isinstance(ov, dict):
                overlay_name = normalize_persisted_shapefile_name(ov.get("name"))
                existing_overlays[source_id] = {
                    "name": overlay_name,
                    "gdf": ov.get("gdf"),
                }
            else:
                existing_overlays[source_id] = {
                    "name": "",
                    "gdf": ov[0] if len(ov) > 0 else None,
                }

        prior_primary_color = normalize_picker_color_name(
            getattr(self, "shp_primary_color", "cyan"),
            "cyan",
        )
        self.shp_primary = None
        self.shp_primary_name = None
        self.shp_primary_color = prior_primary_color
        self.shp_overlays = []

        pending_vector_items = []
        for item in selected_vector_items:
            if item.get("pending"):
                pending_vector_items.append(item)
                continue

            slot_kind = str(item.get("slot_kind") or "overlay").strip().lower()
            source_id = item.get("source_id")
            default_color = "cyan" if slot_kind == "primary" else "dodgerblue"
            color = normalize_picker_color_name(item.get("color"), default_color)

            if slot_kind == "primary":
                if existing_primary is None or source_id != existing_primary.get("source_id"):
                    continue
                if existing_primary.get("gdf") is None:
                    continue
                self.shp_primary = existing_primary["gdf"]
                self.shp_primary_name = existing_primary.get("name") or normalize_persisted_shapefile_name(item.get("name"))
                self.shp_primary_color = color
                continue

            overlay_item = existing_overlays.get(source_id)
            if overlay_item is None or overlay_item.get("gdf") is None:
                continue
            overlay_name = (
                overlay_item.get("name")
                or normalize_persisted_shapefile_name(item.get("name"))
                or f"shapefile {len(self.shp_overlays) + 1}"
            )
            self.shp_overlays.append({
                "name": overlay_name,
                "gdf": overlay_item["gdf"],
                "color": color,
            })

        newly_loaded, failed_loads = self._load_runtime_shapefile_selections(
            pending_vector_items
        )
        if newly_loaded:
            self.statusBar().showMessage(
                "Loaded shapefiles: " + ", ".join(newly_loaded),
                6000,
            )
        if failed_loads:
            QtWidgets.QMessageBox.warning(
                self,
                "Load Shapefiles / Basemaps",
                "Some shapefiles could not be loaded:\n" + "\n".join(failed_loads),
            )

        for ax in getattr(self, "axes", []):
            try:
                self._style_panel_axes(ax)
            except Exception:
                pass
        self._apply_footer_style()
        self._refresh_all_thermal_displays()
        basemap_display_changed = (
            basemap_changed
            or basemap_resolution_changed
            or basemap_color_scaling_changed
            or basemap_cmap_changed
            or basemap_mode_changed
            or basemap_category_changed
        )
        if basemap_display_changed:
            self._refresh_basemap_underlays_all()
            self.ensure_selectors()
            self.kill_toolbar_tools()
        elif basemap_resampling_changed:
            self._refresh_basemap_underlays_all(refresh_thermal=False)
        if not basemap_display_changed:
            self.ensure_selectors()
            self.kill_toolbar_tools()

        # SHP loads and color changes must be applied even when this Use action
        # also takes a basemap or thermal-refresh path.
        self._refresh_vector_overlays_all()
        self._restore_panel_views(preserved_display_views)
        self._schedule_panel_geometry_sync(delay_ms=0)

    def set_panel_layout(self, cols: int, rows: int):
        """Apply a new grid layout and redraw panels."""
        cols = int(max(1, min(7, cols)))
        rows = int(max(1, min(7, rows)))
        if getattr(self, "grid_cols", 3) == cols and getattr(self, "grid_rows", 1) == rows:
            return

        # Preserve remaining work order (current panels left-to-right, then queue)
        remaining = [p for p in self.current if p] + list(self.queue)

        # Preserve global display state (avoid breaking P/L/O/K/E workflows)
        old_contrast = getattr(self, "global_contrast_rel", (0.0, 1.0))
        if not (isinstance(old_contrast, (tuple, list)) and len(old_contrast) == 2):
            old_contrast = (0.0, 1.0)
        try:
            old_gamma = float(getattr(self, "global_gamma", 1.0))
        except Exception:
            old_gamma = 1.0
        old_edge_mode = bool(getattr(self, "global_edge_mode", False))
        old_cmap = getattr(self, "cmap_mode", "gray")
        old_alt_cmap = getattr(self, "alt_cmap", "magma")
        old_title_fs = float(getattr(self, "title_fontsize", 18.0))
        old_use_theme_nan_color = bool(getattr(self, "use_theme_nan_color", True))
        old_nan_color = normalize_nan_override_value(getattr(self, "nan_color", self._panel_facecolor()))
        old_thermal_alpha = max(0.0, min(1.0, float(getattr(self, "thermal_alpha", 1.0))))
        old_thermal_blend_mode = normalize_thermal_blend_mode(
            getattr(self, "thermal_blend_mode", "normal")
        )
        old_thermal_visual_resampling = normalize_visual_resampling(
            getattr(self, "thermal_visual_resampling", "nearest")
        )
        old_basemap_visual_resampling = normalize_visual_resampling(
            getattr(self, "basemap_visual_resampling", "nearest")
        )
        old_basemap_resolution_mode = normalize_basemap_resolution_mode(
            getattr(self, "basemap_resolution_mode", "dynamic")
        )
        old_basemap_color_scaling = normalize_basemap_color_scaling(
            getattr(self, "basemap_color_scaling", "normal")
        )
        old_basemap_cmap = normalize_basemap_cmap(
            getattr(self, "basemap_cmap", "gray")
        )
        old_basemap_mode = normalize_basemap_mode(
            getattr(self, "basemap_mode", "nearest")
        )
        old_basemap_category = normalize_basemap_category(
            getattr(self, "basemap_category", "")
        )
        old_shp_linewidth = float(getattr(self, "shp_linewidth", 1.2))
        old_summary_fontsize = float(getattr(self, "summary_fontsize", 11.0))
        old_main_panel_text_scale = normalize_main_panel_text_scale(
            getattr(self, "main_panel_text_scale", 1.0)
        )

        self.grid_cols = cols
        self.grid_rows = rows
        self.n_pan = int(cols * rows)

        # Refill current slots (pad with None for empties)
        new_current = remaining[:self.n_pan]
        self.current = list(new_current) + [None] * (self.n_pan - len(new_current))
        self.queue = remaining[self.n_pan:]

        # Reset per-panel state (safe + predictable)
        self.bases = {}
        self.offsets = {i: [0, 0] for i in range(self.n_pan)}
        self.images = {i: None for i in range(self.n_pan)}
        self.basemap_images = {i: None for i in range(self.n_pan)}
        self.panel_basemap_paths = {}
        self.panel_basemap_delta_days = {}
        self.images_data = {i: None for i in range(self.n_pan)}
        self.current_display_data = {i: None for i in range(self.n_pan)}
        self.current_display_ranges = {i: (None, None) for i in range(self.n_pan)}
        self.panel_views = {}
        self.split_mode = False
        self._split_drag_state = None
        self.split_data = {i: self._new_split_state(self.current[i]) for i in range(self.n_pan)}
        self._ovr_done = getattr(self, "_ovr_done", set())
        self.srccrs = {}
        self.srctrans = {}
        self.data_ranges = {}
        self.edge_cache = {}
        self.aoi_bounds = None

        # Restore global display state (IMPORTANT: keep tuple + float types)
        self.global_contrast_rel = (float(old_contrast[0]), float(old_contrast[1]))
        self.global_gamma = old_gamma if old_gamma > 0 else 1.0
        self.global_edge_mode = old_edge_mode
        self.cmap_mode = old_cmap
        self.alt_cmap = old_alt_cmap
        self.title_fontsize = old_title_fs
        self.use_theme_nan_color = old_use_theme_nan_color
        self.nan_color = normalize_nan_override_value(old_nan_color)
        self.thermal_alpha = old_thermal_alpha
        self.thermal_blend_mode = old_thermal_blend_mode
        self.thermal_visual_resampling = old_thermal_visual_resampling
        self.basemap_visual_resampling = old_basemap_visual_resampling
        self.basemap_resolution_mode = old_basemap_resolution_mode
        self.basemap_color_scaling = old_basemap_color_scaling
        self.basemap_cmap = old_basemap_cmap
        self.basemap_mode = old_basemap_mode
        self.basemap_category = old_basemap_category
        self.shp_linewidth = old_shp_linewidth
        self.summary_fontsize = old_summary_fontsize
        self.main_panel_text_scale = old_main_panel_text_scale

        self.warp_data = {
            i: {"src_world": [], "dst_world": [],
                "src_pix": [],   "dst_pix": [],
                "tform": None,   "applied": False,
                "collecting": False, "markers": [], "labels": []}
            for i in range(self.n_pan)
        }

        # Rebuild axes + footer + UI controls
        self._build_axes_grid(clear_fig=True)
        self.active_idx = min(getattr(self, "active_idx", 0), max(0, self.n_pan - 1))

        # Progress footer lives below the canvas so panel layout is unaffected.
        self.prog = None
        self._update_progress_footer()

        # Redraw panels (batch selectors)
        for i in range(self.n_pan):
            self.draw(i, refresh_selectors=False)
        self.ensure_selectors()
        self.add_keep_reject_buttons()
        self._sync_button_visibility()

        self.canvas.draw_idle()
        self._schedule_panel_geometry_sync(delay_ms=0)

# ------------------------------ Drawing -------------------------------- #
    def draw(self, i: int, refresh_selectors: bool = True):
        path = self.current[i]
        ax = self.axes[i]
        ax.clear()
        self._style_panel_axes(ax)
        self._clear_panel_title(ax)
        self._clear_basemap_delta_label(ax)

        # Invalidate any cached AxesImage that was removed by ax.clear()
        self.images[i] = None
        if hasattr(self, "basemap_images"):
            self.basemap_images[i] = None
        if hasattr(self, "panel_basemap_paths"):
            self.panel_basemap_paths.pop(i, None)
        if hasattr(self, "panel_basemap_delta_days"):
            self.panel_basemap_delta_days.pop(i, None)
        self.images_data[i] = None
        if hasattr(self, "current_display_data"):
            self.current_display_data[i] = None
        if hasattr(self, "current_display_ranges"):
            self.current_display_ranges[i] = (None, None)

        # Empty slot: just blank the panel
        if not path:
            self._clear_panel_slot(i, message='EMPTY', fontsize=14)
            if refresh_selectors:
                self.ensure_selectors()
            return

        # Build overview pyramids once for faster, lower-res reads
        self._ensure_overviews_once(path)
        self._select_basemap_for_panel(i, path)

        # Read decimated pixels suited to panel size
        with rasterio.Env(GDAL_NUM_THREADS="ALL_CPUS"):
            with rasterio.open(path, sharing=False) as src:
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
            edges, e_range = self._build_edge_display(im)
            self.edge_cache[i] = {"data": edges, "range": e_range}
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

        if reuse_ok:
            self.images[i] = img
        else:
            self.images[i] = ax.imshow(
                display,
                cmap=self._display_cmap(),
                norm=mcolors.PowerNorm(gamma=self.global_gamma, vmin=rmin, vmax=rmax),
                extent=[xmin, xmax, ymin, ymax],
                origin='upper', zorder=1,
                interpolation=visual_resampling_mpl_interpolation(
                    getattr(self, "thermal_visual_resampling", "nearest")
                ),
                aspect='auto',
                alpha=float(getattr(self, "thermal_alpha", 1.0)),
            )
        self._refresh_panel_thermal_display(
            i,
            display=display,
            data_range=(rmin, rmax),
            extent=[xmin, xmax, ymin, ymax],
        )
        self._set_panel_view(ax, (xmin, xmax), (ymin, ymax), sync=False)
        try:
            ax.set_autoscalex_on(False)
            ax.set_autoscaley_on(False)
        except Exception:
            pass
        self._draw_basemap(i, ax)

        # Overlays: primary first (if any), then additional overlays
        self._plot_vector_overlays(ax)

        ax.set_aspect('auto')
        self._set_panel_view(ax, (xmin, xmax), (ymin, ymax), sync=False)
        ax.set_xticks([]); ax.set_yticks([])
        self._set_panel_title(ax, os.path.basename(path))
        self._set_basemap_delta_label(ax, self._basemap_delta_label_for_panel(i))
        self._refresh_comment_marker(i)
        self._refresh_split_artists(i, draw=False)

        # Build/refresh rectangle selector (non-interactive after draw)
        if refresh_selectors:
            self.ensure_selectors()
        self._sync_button_visibility(i)

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

        # rebuild selectors only for axes that currently have a loaded raster
        for idx, ax in enumerate(self.axes):
            if idx >= getattr(self, "n_pan", 0) or not self._panel_has_loaded_image(idx):
                continue
            sel = RectangleSelector(
                ax,
                lambda e1, e2, ax=ax: self._handle_rectangle_zoom(ax, e1, e2),
                useblit=True,
                button=[1],
                spancoords='data',
                props=dict(facecolor='lightcoral', edgecolor='none', alpha=0.3),
                interactive=False  # prevent post-draw drag/resize
            )
            self.selectors.append(sel)

    # --------------------------- UI Button Row ----------------------------- #
    def add_keep_reject_buttons(self):
        # Make sure axes positions + a renderer exist (needed to measure text)
        try:
            self.canvas.draw()
            renderer = self.canvas.get_renderer()
        except Exception:
            try:
                self.fig.canvas.draw()
                renderer = self.fig.canvas.get_renderer()
            except Exception:
                renderer = None

        # Remove any existing button axes (important when layout changes)
        if not hasattr(self, "_button_axes"):
            self._button_axes = []
        for bax in list(self._button_axes):
            try:
                self.fig.delaxes(bax)
            except Exception:
                pass
        self._button_axes = []
        self.buttons = []

        label_keep = "Keep"
        label_rej  = "Reject"
        button_styles = self._keep_reject_button_styles()
        button_layout_settings = normalize_keep_reject_button_layout_settings(
            getattr(
                self,
                "keep_reject_button_layout_settings",
                DEFAULT_KEEP_REJECT_BUTTON_LAYOUT_SETTINGS,
            )
        )
        button_layout_metrics = keep_reject_button_layout_metrics(button_layout_settings)

        # ---Button text size ---
        font_pt = float(button_layout_metrics["font_pt"])
        # -----------------------------
        # Size buttons based on *measured text* (no monitor-DPI heuristics)
        # -----------------------------
        pad_x_px = float(button_layout_metrics["pad_x_px"])
        pad_y_px = float(button_layout_metrics["pad_y_px"])
        gap_px   = float(button_layout_metrics["gap_px"])

        if renderer is not None:
            # Measure actual rendered label sizes
            def _measure_px(s: str):
                t = self.fig.text(0, 0, s, fontsize=font_pt, alpha=0.0)
                try:
                    bb = t.get_window_extent(renderer=renderer)
                    return float(bb.width), float(bb.height)
                finally:
                    try:
                        t.remove()
                    except Exception:
                        pass

            w1, h1 = _measure_px(label_keep)
            w2, h2 = _measure_px(label_rej)
            text_w_px = max(w1, w2)
            text_h_px = max(h1, h2)

            # Convert pixels -> figure-fraction using the figure bbox (stable)
            fig_w_px = float(self.fig.bbox.width) if hasattr(self.fig, "bbox") else 1.0
            fig_h_px = float(self.fig.bbox.height) if hasattr(self.fig, "bbox") else 1.0

            bw = (text_w_px + pad_x_px) / max(fig_w_px, 1.0)
            bh = (text_h_px + pad_y_px) / max(fig_h_px, 1.0)
            gap = gap_px / max(fig_w_px, 1.0)
        else:
            # Fallback if renderer isn't available (rare)
            max_len = max(len(label_keep), len(label_rej))
            size_scale = float(button_layout_settings["size_scale"])
            bw = (0.018 * max_len + 0.03) * size_scale
            bh = 0.055 * size_scale
            default_gap_px = max(1.0, float(DEFAULT_KEEP_REJECT_BUTTON_LAYOUT_SETTINGS["spacing_px"]))
            gap = 0.018 * (gap_px / default_gap_px)

        # Fit inside smallest panel (esp. 5-across), keeping SAME size everywhere
        try:
            min_panel_w = min(ax.get_position().width for ax in self.axes[:self.n_pan])
            min_panel_h = min(ax.get_position().height for ax in self.axes[:self.n_pan])
        except Exception:
            min_panel_w, min_panel_h = 0.2, 0.2

        total_w = 2 * bw + gap
        max_total_w = max(0.001, min_panel_w * 0.92)
        if total_w > max_total_w:
            s = max_total_w / total_w
            bw *= s
            gap *= s

        # Allow a bit more vertical real estate now that buttons are bigger
        bh = min(bh, max(0.001, min_panel_h * 0.18))

        # -----------------------------
        # Create buttons for each panel (CENTERED), overlaid near the bottom
        # -----------------------------
        for i in range(self.n_pan):
            pos = self.axes[i].get_position()

            cx = pos.x0 + pos.width * 0.5
            left_x  = cx - (gap * 0.5 + bw)
            right_x = cx + (gap * 0.5)

            y0 = pos.y0 + pos.height * 0.025

            ka = self.fig.add_axes([left_x,  y0, bw, bh], facecolor=self.theme['figure_bg'])
            kb = self.fig.add_axes([right_x, y0, bw, bh], facecolor=self.theme['figure_bg'])
            self._button_axes.extend([ka, kb])

            # Ensure button axes never show 0..1 ticks/spines
            for bax in (ka, kb):
                try:
                    bax.set_in_layout(False)
                except Exception:
                    pass
                bax.set_xticks([]); bax.set_yticks([])
                bax.set_xticklabels([]); bax.set_yticklabels([])
                bax.tick_params(left=False, bottom=False, labelleft=False, labelbottom=False)
                for sp in bax.spines.values():
                    sp.set_visible(False)
                try:
                    bax.set_navigate(False)
                except Exception:
                    pass

            b1 = Button(
                ka,
                label_keep,
                color=button_styles["keep"]["base"],
                hovercolor=button_styles["keep"]["hover"],
            )
            b2 = Button(
                kb,
                label_rej,
                color=button_styles["reject"]["base"],
                hovercolor=button_styles["reject"]["hover"],
            )

            # Keep label size consistent
            try:
                b1.label.set_fontsize(font_pt)
                b2.label.set_fontsize(font_pt)
            except Exception:
                pass

            b1.on_clicked(self.make_cb(i, 'keep'))
            b2.on_clicked(self.make_cb(i, 'reject'))
            self.buttons.extend([b1, b2])

        self._apply_keep_reject_button_styles()
        self._sync_button_visibility()

    def _comment_key_for_path(self, path):
        if not path:
            return ""
        return os.path.basename(path)

    def _pending_comment_for_path(self, path):
        key = self._comment_key_for_path(path)
        if not key:
            return ""
        return _clean_log_comment(getattr(self, "pending_comments", {}).get(key, ""))

    def _refresh_comment_marker(self, idx):
        try:
            path = self.current[idx]
            ax = self.axes[idx]
        except Exception:
            return

        artist = getattr(ax, "_comment_marker_artist", None)
        has_comment = bool(path and self._pending_comment_for_path(path))
        if not has_comment:
            if artist is not None:
                try:
                    artist.remove()
                except Exception:
                    pass
                ax._comment_marker_artist = None
            return

        if artist is None or getattr(artist, "axes", None) is not ax:
            if artist is not None:
                try:
                    artist.remove()
                except Exception:
                    pass
            artist = ax.text(
                0.018, 0.975, "*",
                transform=ax.transAxes,
                ha="left", va="top",
                fontsize=self._scaled_main_panel_fontsize(18.0, minimum=6.0, maximum=48.0),
                fontweight="bold",
                color="white",
                zorder=12,
                path_effects=[
                    path_effects.withStroke(linewidth=1.1, foreground="black")
                ],
            )
            self._set_panel_text_artist_fontsize(artist, 18.0, minimum=6.0, maximum=48.0)
            ax._comment_marker_artist = artist
        else:
            artist.set_visible(True)
            artist.set_text("*")
            artist.set_color("white")
            self._set_panel_text_artist_fontsize(artist, 18.0, minimum=6.0, maximum=48.0)

    def _refresh_comment_markers_for_path(self, path):
        key = self._comment_key_for_path(path)
        if not key:
            return
        for idx, current_path in enumerate(getattr(self, "current", []) or []):
            if self._comment_key_for_path(current_path) == key:
                self._refresh_comment_marker(idx)
        try:
            self.canvas.draw_idle()
        except Exception:
            pass

    def _set_pending_comment_for_path(self, path, comment):
        key = self._comment_key_for_path(path)
        if not key:
            return
        comment = _clean_log_comment(comment)
        if not hasattr(self, "pending_comments"):
            self.pending_comments = {}
        if comment:
            self.pending_comments[key] = comment
        else:
            self.pending_comments.pop(key, None)
        self._refresh_comment_markers_for_path(path)

    def _discard_pending_comment_for_path(self, path):
        key = self._comment_key_for_path(path)
        if key and hasattr(self, "pending_comments"):
            self.pending_comments.pop(key, None)
        self._refresh_comment_markers_for_path(path)

    def _take_pending_comment_for_path(self, path):
        comment = self._pending_comment_for_path(path)
        self._discard_pending_comment_for_path(path)
        return comment

    def _move_dialog_to_screen_center(self, dlg):
        try:
            dlg.adjustSize()
            hint = dlg.sizeHint()
            dlg.resize(max(460, int(hint.width())), int(hint.height()))
        except Exception:
            pass

        try:
            screen = self.screen() or QtWidgets.QApplication.primaryScreen()
            if screen is not None:
                avail = screen.availableGeometry()
                x = avail.center().x() - dlg.width() // 2
                y = avail.center().y() - dlg.height() // 2
                x = min(max(x, avail.left()), avail.right() - dlg.width())
                y = min(max(y, avail.top()), avail.bottom() - dlg.height())
            else:
                main_geo = self.frameGeometry()
                x = main_geo.center().x() - dlg.width() // 2
                y = main_geo.center().y() - dlg.height() // 2
            dlg.move(x, y)
        except Exception:
            pass

    def _all_comment_flags(self):
        return all_comment_flags(getattr(self, "user_comment_flags", []))

    def _add_user_comment_flag_to_script(self, flag):
        flag = normalize_comment_flag(flag)
        if not flag:
            return ""

        existing_flags = self._all_comment_flags()
        key = _comment_flag_key(flag)
        for existing in existing_flags:
            if _comment_flag_key(existing) == key:
                return existing

        user_flags = normalize_comment_flags(self._all_comment_flags() + [flag])
        self.user_comment_flags = save_user_comment_flags_to_script(
            self._script_path,
            user_flags,
        )
        return flag

    def _open_image_comment_dialog(self, idx):
        if idx < 0 or idx >= len(getattr(self, "current", [])):
            return
        path = self.current[idx]
        if not path or not self._panel_has_image(idx):
            return

        fname = os.path.basename(path)
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Image Comment")
        dlg.setModal(True)
        dlg.setProperty("geoviewer_image_comment_dialog", True)
        dlg.setWindowFlags(dlg.windowFlags() & ~QtCore.Qt.WindowContextHelpButtonHint)
        dlg.setStyleSheet(build_app_stylesheet(get_app_theme_mode()))

        lay = QtWidgets.QVBoxLayout(dlg)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        title = QtWidgets.QLabel(fname, dlg)
        title.setWordWrap(True)
        title.setStyleSheet(f"font-size: 13pt; font-weight: 700; color: {self.theme['text']};")
        lay.addWidget(title)

        existing_flags, existing_text = split_comment_flags(self._pending_comment_for_path(path))
        flag_names = normalize_comment_flags(self._all_comment_flags() + existing_flags)
        selected_flags_state = {"flags": normalize_comment_flags(existing_flags)}
        flag_buttons_by_key = {}
        programmatic_edit = {"active": False}

        class _CommentEnterFilter(QtCore.QObject):
            def __init__(self, dialog):
                super().__init__(dialog)
                self._dialog = dialog

            def eventFilter(self, obj, event):
                if (
                    event.type() == QtCore.QEvent.KeyPress
                    and event.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter)
                    and not (event.modifiers() & QtCore.Qt.ShiftModifier)
                ):
                    self._dialog.accept()
                    return True
                return False

        class _CommentCounterOverlayFilter(QtCore.QObject):
            def __init__(self, label, parent=None):
                super().__init__(parent)
                self._label = label

            def eventFilter(self, obj, event):
                if event.type() in (QtCore.QEvent.Resize, QtCore.QEvent.Show):
                    QtCore.QTimer.singleShot(0, self.position_label)
                return False

            def position_label(self):
                label = self._label
                parent = label.parentWidget()
                if parent is None:
                    return
                label.adjustSize()
                margin = 6
                rect = parent.rect()
                label.move(
                    max(margin, rect.width() - label.width() - margin),
                    max(margin, rect.height() - label.height() - margin),
                )

        class _InlineFlagEdit(QtWidgets.QLineEdit):
            def __init__(self, confirm_cb, cancel_cb, parent=None):
                super().__init__(parent)
                self._confirm_cb = confirm_cb
                self._cancel_cb = cancel_cb

            def keyPressEvent(self, event):
                if event.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
                    self._confirm_cb(self.text())
                    return
                if event.key() == QtCore.Qt.Key_Escape:
                    self._cancel_cb()
                    return
                super().keyPressEvent(event)

        flag_drag_mime = "application/x-geoviewer-comment-flag"

        class _FlagButtonFilter(QtCore.QObject):
            def eventFilter(self, obj, event):
                etype = event.type()
                if etype == QtCore.QEvent.MouseButtonPress:
                    if event.button() == QtCore.Qt.RightButton:
                        _show_flag_context(obj, event.globalPos())
                        return True
                    if event.button() == QtCore.Qt.LeftButton:
                        obj._flag_drag_start_pos = event.pos()
                elif etype == QtCore.QEvent.MouseMove:
                    if not (event.buttons() & QtCore.Qt.LeftButton):
                        return False
                    start_pos = getattr(obj, "_flag_drag_start_pos", None)
                    if start_pos is None:
                        return False
                    if (event.pos() - start_pos).manhattanLength() < QtWidgets.QApplication.startDragDistance():
                        return False
                    key = str(obj.property("flag_key") or "")
                    if not key:
                        return False
                    drag = QtGui.QDrag(obj)
                    mime = QtCore.QMimeData()
                    mime.setData(flag_drag_mime, key.encode("utf-8"))
                    drag.setMimeData(mime)
                    drag.exec_(QtCore.Qt.MoveAction)
                    return True
                elif etype in (QtCore.QEvent.DragEnter, QtCore.QEvent.DragMove):
                    if event.mimeData().hasFormat(flag_drag_mime):
                        event.acceptProposedAction()
                        return True
                elif etype == QtCore.QEvent.Drop:
                    if not event.mimeData().hasFormat(flag_drag_mime):
                        return False
                    try:
                        source_key = bytes(event.mimeData().data(flag_drag_mime)).decode("utf-8")
                    except Exception:
                        source_key = ""
                    target_key = str(obj.property("flag_key") or "")
                    if source_key and target_key:
                        _move_flag(source_key, target_key, after=(event.pos().x() > obj.width() / 2))
                    event.acceptProposedAction()
                    return True
                return False

        edit = QtWidgets.QPlainTextEdit(dlg)
        edit.setPlainText(compose_comment(existing_flags, existing_text))
        edit.setPlaceholderText("")
        edit.setMinimumWidth(680)
        edit.setLineWrapMode(QtWidgets.QPlainTextEdit.WidgetWidth)
        edit.setWordWrapMode(QtGui.QTextOption.WrapAtWordBoundaryOrAnywhere)
        edit_font = edit.font()
        edit_font.setPointSize(max(edit_font.pointSize(), 13))
        edit.setFont(edit_font)
        edit.setMinimumHeight(QtGui.QFontMetrics(edit_font).lineSpacing() * 5 + 18)
        dlg._comment_enter_filter = _CommentEnterFilter(dlg)
        edit.installEventFilter(dlg._comment_enter_filter)
        lay.addWidget(edit)

        flag_grid_frame = QtWidgets.QFrame(dlg)
        flag_grid_lay = QtWidgets.QGridLayout(flag_grid_frame)
        flag_grid_lay.setContentsMargins(0, 0, 0, 0)
        flag_grid_lay.setHorizontalSpacing(5)
        flag_grid_lay.setVerticalSpacing(5)
        lay.addWidget(flag_grid_frame)

        counter = QtWidgets.QLabel(edit.viewport())
        counter.setAlignment(QtCore.Qt.AlignRight)
        counter_color_normal = self.theme.get("muted", self.theme.get("text", "#D3D3D3"))
        counter_color_amber = "#E69F00"
        counter_color_red = "#D55E00"
        editor_bg = self.theme.get("input_bg", self.theme.get("panel_bg", "#111111"))
        counter.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        counter.raise_()
        dlg._comment_counter_overlay_filter = _CommentCounterOverlayFilter(counter, dlg)
        edit.viewport().installEventFilter(dlg._comment_counter_overlay_filter)
        dlg._flag_button_filter = _FlagButtonFilter(dlg)
        inline_add_state = {"index": None, "editor": None}

        def _selected_comment_flags():
            return normalize_comment_flags(selected_flags_state.get("flags", []))

        def _body_from_edit():
            _flags, body = split_comment_flags(edit.toPlainText())
            return body

        def _flags_and_body_from_edit():
            flags, body = split_comment_flags(edit.toPlainText())
            return normalize_comment_flags(flags), body

        def _persist_comment_flags(new_flags):
            nonlocal flag_names
            normalized = normalize_comment_flags(new_flags)[:COMMENT_FLAG_GRID_MAX_FLAGS]
            if not normalized:
                return False
            try:
                self.user_comment_flags = save_user_comment_flags_to_script(
                    self._script_path,
                    normalized,
                )
            except Exception as e:
                QtWidgets.QMessageBox.warning(
                    dlg,
                    "Flags Not Saved",
                    f"Could not save comment flags to the script:\n{e}",
                )
                return False
            flag_names = self._all_comment_flags()
            return True

        def _button_style(checked=False):
            if checked:
                return (
                    "QPushButton { background-color: #2E8B57; color: #FFFFFF; "
                    "border: 1px solid #8FE3B0; border-radius: 5px; padding: 3px 5px; }\n"
                    "QPushButton:hover { background-color: #36A468; }"
                )
            return (
                f"QPushButton {{ background-color: {self.theme.get('button_bg', '#2A2A2A')}; "
                f"color: {self.theme.get('text', '#D3D3D3')}; "
                f"border: 1px solid {self.theme.get('border', '#4A4A4A')}; "
                "border-radius: 5px; padding: 3px 5px; }\n"
                f"QPushButton:hover {{ background-color: {self.theme.get('button_hover', '#3A3A3A')}; }}"
            )

        def _update_flag_button_states():
            selected_keys = {_comment_flag_key(flag) for flag in _selected_comment_flags()}
            for key, button in list(flag_buttons_by_key.items()):
                checked = key in selected_keys
                if button.isChecked() != checked:
                    button.blockSignals(True)
                    button.setChecked(checked)
                    button.blockSignals(False)
                button.setStyleSheet(_button_style(checked))

        def _update_comment_counter(text=None):
            if not programmatic_edit.get("active"):
                parsed_flags, _body = split_comment_flags(edit.toPlainText())
                if parsed_flags or not edit.toPlainText().lstrip().startswith("["):
                    selected_flags_state["flags"] = parsed_flags

            text = edit.toPlainText()
            if len(text) > LOG_COMMENT_MAX_CHARS:
                cursor = edit.textCursor()
                position = min(cursor.position(), LOG_COMMENT_MAX_CHARS)
                edit.blockSignals(True)
                edit.setPlainText(text[:LOG_COMMENT_MAX_CHARS])
                cursor = edit.textCursor()
                cursor.setPosition(position)
                edit.setTextCursor(cursor)
                edit.blockSignals(False)
                text = edit.toPlainText()

            ratio = len(text) / max(1, LOG_COMMENT_MAX_CHARS)
            counter_color = counter_color_normal
            if ratio >= 0.90:
                counter_color = counter_color_red
            elif ratio >= 0.70:
                counter_color = counter_color_amber
            counter.setStyleSheet(
                f"font-size: 10pt; color: {counter_color}; "
                f"background-color: {editor_bg}; padding: 1px 4px;"
            )
            counter.setText(f"{len(text)} / {LOG_COMMENT_MAX_CHARS}")
            dlg._comment_counter_overlay_filter.position_label()
            _update_flag_button_states()

        def _set_edit_comment(flags, body):
            flags = normalize_comment_flags(flags)
            comment = compose_comment(flags, body)
            if len(_comment_flags_prefix(flags)) > LOG_COMMENT_MAX_CHARS:
                return False
            if len(comment) > LOG_COMMENT_MAX_CHARS:
                body = str(body or "")[:comment_text_char_budget(flags)]
                comment = compose_comment(flags, body)

            selected_flags_state["flags"] = flags
            cursor_pos = len(comment)
            programmatic_edit["active"] = True
            try:
                edit.blockSignals(True)
                edit.setPlainText(comment)
                cursor = edit.textCursor()
                cursor.setPosition(cursor_pos)
                edit.setTextCursor(cursor)
            finally:
                edit.blockSignals(False)
                programmatic_edit["active"] = False
            _update_comment_counter()
            return True

        def _toggle_comment_flag(flag):
            flag = normalize_comment_flag(flag)
            if not flag:
                return
            selected, body = _flags_and_body_from_edit()
            key = _comment_flag_key(flag)
            if key in {_comment_flag_key(item) for item in selected}:
                selected = [item for item in selected if _comment_flag_key(item) != key]
            else:
                selected.append(flag)

            if len(_comment_flags_prefix(selected)) > LOG_COMMENT_MAX_CHARS:
                _update_flag_button_states()
                return
            _set_edit_comment(selected, body)

        def _delete_flag(flag):
            flag = normalize_comment_flag(flag)
            key = _comment_flag_key(flag)
            new_flags = [item for item in flag_names if _comment_flag_key(item) != key]
            if not new_flags:
                return
            if not _persist_comment_flags(new_flags):
                return
            selected = [item for item in _selected_comment_flags() if _comment_flag_key(item) != key]
            _set_edit_comment(selected, _body_from_edit())
            _refresh_flag_grid()

        def _move_flag(source_key, target_key, after=False):
            if not source_key or not target_key or source_key == target_key:
                return
            flags = list(flag_names)
            source_flag = None
            for item in flags:
                if _comment_flag_key(item) == source_key:
                    source_flag = item
                    break
            if source_flag is None:
                return
            flags = [item for item in flags if _comment_flag_key(item) != source_key]
            target_idx = None
            for idx_, item in enumerate(flags):
                if _comment_flag_key(item) == target_key:
                    target_idx = idx_
                    break
            if target_idx is None:
                return
            insert_idx = target_idx + (1 if after else 0)
            flags.insert(insert_idx, source_flag)
            if _persist_comment_flags(flags):
                _refresh_flag_grid()

        def _cancel_inline_add():
            inline_add_state["index"] = None
            inline_add_state["editor"] = None
            _refresh_flag_grid()

        def _confirm_inline_add(text):
            flag = normalize_comment_flag(text)
            if not flag:
                _cancel_inline_add()
                return
            target_key = _comment_flag_key(flag)
            if any(_comment_flag_key(item) == target_key for item in flag_names):
                _cancel_inline_add()
                return
            insert_idx = inline_add_state.get("index")
            if insert_idx is None:
                insert_idx = len(flag_names)
            insert_idx = max(0, min(int(insert_idx), len(flag_names)))
            new_flags = list(flag_names)
            new_flags.insert(insert_idx, flag)
            inline_add_state["index"] = None
            inline_add_state["editor"] = None
            if _persist_comment_flags(new_flags):
                _refresh_flag_grid()

        def _start_inline_add_after(flag):
            if len(flag_names) >= COMMENT_FLAG_GRID_MAX_FLAGS:
                return
            key = _comment_flag_key(flag)
            insert_idx = len(flag_names)
            for idx_, item in enumerate(flag_names):
                if _comment_flag_key(item) == key:
                    insert_idx = idx_ + 1
                    break
            inline_add_state["index"] = insert_idx
            _refresh_flag_grid()

        def _show_flag_context(button, global_pos):
            flag = normalize_comment_flag(button.property("flag_name"))
            if not flag:
                return
            menu = QtWidgets.QMenu(dlg)
            add_action = menu.addAction("Add New Flag")
            add_action.setEnabled(len(flag_names) < COMMENT_FLAG_GRID_MAX_FLAGS)
            delete_action = menu.addAction("Delete Flag")
            delete_action.setEnabled(len(flag_names) > 1)
            action = menu.exec_(global_pos)
            if action == add_action:
                _start_inline_add_after(flag)
            elif action == delete_action:
                _delete_flag(flag)

        footer_note = QtWidgets.QLabel(
            "To save the comment, keep or reject the corresponding image.",
            dlg,
        )
        footer_note.setWordWrap(False)
        footer_note.setStyleSheet(
            f"font-size: 11pt; color: {self.theme.get('muted', self.theme.get('text', '#D3D3D3'))};"
        )
        inline_add_hint = QtWidgets.QLabel("Hit ENTER to commit new flag.", dlg)
        inline_add_hint.setWordWrap(False)
        inline_add_hint.setStyleSheet("font-size: 11pt; color: #E69F00; font-weight: 700;")
        inline_add_hint.hide()

        def _clear_flag_grid():
            while flag_grid_lay.count():
                item = flag_grid_lay.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.setParent(None)

        def _refresh_flag_grid():
            nonlocal flag_names
            _clear_flag_grid()
            flag_buttons_by_key.clear()
            entries = list(flag_names)
            insert_idx = inline_add_state.get("index")
            if insert_idx is not None:
                insert_idx = max(0, min(int(insert_idx), len(entries)))
                entries.insert(insert_idx, None)
            entries = entries[:COMMENT_FLAG_GRID_MAX_FLAGS]
            for entry_idx, flag in enumerate(entries):
                row = entry_idx // 6
                col = entry_idx % 6
                if flag is None:
                    editor = _InlineFlagEdit(_confirm_inline_add, _cancel_inline_add, flag_grid_frame)
                    editor.setMinimumHeight(24)
                    editor.setMaximumHeight(28)
                    font = editor.font()
                    font.setPointSize(max(7, min(font.pointSize(), 9)))
                    editor.setFont(font)
                    flag_grid_lay.addWidget(editor, row, col)
                    inline_add_state["editor"] = editor
                    QtCore.QTimer.singleShot(0, editor.setFocus)
                    continue
                flag = normalize_comment_flag(flag)
                if flag:
                    button = QtWidgets.QPushButton(flag, flag_grid_frame)
                    button.setCheckable(True)
                    button.setAcceptDrops(True)
                    key = _comment_flag_key(flag)
                    button.setProperty("flag_key", key)
                    button.setProperty("flag_name", flag)
                    button.setMinimumHeight(24)
                    button.setMaximumHeight(28)
                    button.setSizePolicy(
                        QtWidgets.QSizePolicy.Expanding,
                        QtWidgets.QSizePolicy.Fixed,
                    )
                    font = button.font()
                    font.setPointSize(max(7, min(font.pointSize(), 9)))
                    button.setFont(font)
                    button.installEventFilter(dlg._flag_button_filter)
                    button.clicked.connect(lambda _checked=False, f=flag: _toggle_comment_flag(f))
                    flag_grid_lay.addWidget(button, row, col)
                    flag_buttons_by_key[key] = button

            for col in range(6):
                flag_grid_lay.setColumnStretch(col, 1)
            inline_add_hint.setVisible(inline_add_state.get("index") is not None)
            _update_comment_counter()

        edit.textChanged.connect(_update_comment_counter)
        _refresh_flag_grid()

        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel,
            parent=dlg,
        )
        ok_button = btns.button(QtWidgets.QDialogButtonBox.Ok)
        if ok_button is not None:
            ok_button.setText("Okay")
            ok_button.setDefault(True)
            ok_button.setAutoDefault(True)
        cancel_button = btns.button(QtWidgets.QDialogButtonBox.Cancel)
        if cancel_button is not None:
            cancel_button.setAutoDefault(False)
            cancel_button.setDefault(False)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)

        footer = QtWidgets.QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(10)
        footer.addWidget(footer_note, 0, QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        footer.addWidget(inline_add_hint, 0, QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        footer.addStretch(1)
        footer.addWidget(btns, 0, QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        lay.addLayout(footer)

        self._move_dialog_to_screen_center(dlg)
        edit.setFocus(QtCore.Qt.OtherFocusReason)
        cursor = edit.textCursor()
        cursor.movePosition(QtGui.QTextCursor.End)
        edit.setTextCursor(cursor)

        self._active_comment_dialog = dlg
        self._comment_dialog_active = True
        try:
            accepted = dlg.exec_() == QtWidgets.QDialog.Accepted
        finally:
            self._comment_dialog_active = False
            self._active_comment_dialog = None

        if accepted:
            final_flags, final_body = _flags_and_body_from_edit()
            final_comment = compose_comment(final_flags, final_body)
            self._set_pending_comment_for_path(path, final_comment)
            if self._pending_comment_for_path(path):
                try:
                    self.statusBar().showMessage(
                        f"Comment pending for {fname}. Keep or reject the image to save it to {LOG_FILE}.",
                        6000,
                    )
                except Exception:
                    pass
        self._ensure_focus()

    # ----------------------------- Events --------------------------------- #
    def idx_from_event(self, ev):
        if ev.inaxes in self.axes:
            event_idx = self.axes.index(ev.inaxes)
            if self._panel_has_loaded_image(event_idx):
                return event_idx
        active_idx = self._active_loaded_panel_index()
        if active_idx is not None:
            return active_idx
        return getattr(self, 'active_idx', 0)

    def on_motion(self, ev):
        if self._image_comment_dialog_active():
            return

        # Track which panel we're over (used by keyboard-only shortcuts)
        if ev.inaxes in self.axes:
            hover_idx = self.axes.index(ev.inaxes)
            if self._panel_has_loaded_image(hover_idx):
                self.active_idx = hover_idx
        if self._handle_split_motion(ev):
            return
        drag = getattr(self, "_rectangle_drag_state", None)
        if isinstance(drag, dict) and drag.get("axes") is ev.inaxes:
            try:
                drag["current_y"] = float(ev.y)
            except Exception:
                pass

    def on_button_press(self, ev):
        if self._image_comment_dialog_active():
            return

        # ensure focus & shortcuts are ready
        self._ensure_shortcuts_once()
        self._ensure_focus()

        if ev.inaxes not in self.axes:
            return

        idx = self.axes.index(ev.inaxes)
        if not self._panel_has_loaded_image(idx):
            self._sync_button_visibility(idx)
            return

        self.active_idx = idx
        ax = self.axes[idx]

        button_name = str(getattr(ev, "button", "") or "").lower()
        if getattr(ev, "button", None) == 1 or button_name in ("1", "left", "mousebutton.left"):
            try:
                y_px = float(ev.y)
            except Exception:
                y_px = None
            self._rectangle_drag_state = {
                "axes": ax,
                "press_y": y_px,
                "current_y": y_px,
            }

        # SHIFT + click anywhere inside a panel => full reset (zoom + pan + warp)
        keystr = str(getattr(ev, "key", "") or "").lower()
        if "shift" in keystr:
            self._do_full_reset(idx)
            return

        # Right-click => reset to full-scene zoom, preserving current pan & any warp
        if ev.button == 3:
            L, R, B, T = self.bases[idx]
            dx, dy = self.offsets[idx]
            self._set_panel_view(ax, (L + dx, R + dx), (B + dy, T + dy))
            self.kill_toolbar_pan_only()
            self.canvas.draw_idle()
            return
        # left/middle clicks: just set active axis

    def _step_pan_multiplier(self, factor):
        self.scale_modifier = max(1e-3, float(self.scale_modifier) * float(factor))
        self.pan_factor = 0.001 * self.base_multiplier * self.scale_modifier

    def _reset_pan_multiplier(self):
        self.scale_modifier = 1.0
        self.pan_factor = 0.001 * self.base_multiplier * self.scale_modifier

    def on_scroll(self, ev):
        if self._image_comment_dialog_active():
            return

        # Mouse wheel mirrors the pan multiplier hotkeys.
        self._ensure_shortcuts_once()
        self._ensure_focus()
        if not bool(getattr(self, "scroll_wheel_pan_multi_enabled", True)):
            return

        if ev.inaxes in self.axes and not self._panel_has_loaded_image(self.axes.index(ev.inaxes)):
            return

        idx = self.idx_from_event(ev)
        if not self.current[idx]:
            return
        if ev.inaxes in self.axes:
            self.active_idx = self.axes.index(ev.inaxes)

        step = getattr(ev, 'step', 0)
        button = str(getattr(ev, 'button', '') or '').lower()
        if step > 0 or button == 'up':
            self._step_pan_multiplier(2.0)
            return
        if step < 0 or button == 'down':
            self._step_pan_multiplier(0.5)
            return

    def on_key(self, ev):
        if self._image_comment_dialog_active():
            self._release_all_thermal_transparency_keys()
            return

        # ensure focus & shortcuts are ready
        self._ensure_shortcuts_once()
        self._ensure_focus()

        key = (ev.key or '').lower()
        early_split_key = key.split("+")[-1]
        if early_split_key == "m" or (bool(getattr(self, "split_mode", False)) and early_split_key in ("escape", "esc")):
            split_idx = self._active_loaded_panel_index(getattr(self, "active_idx", 0))
            if split_idx is not None:
                self._handle_split_key(ev, split_idx, key)
            else:
                self._split_status("Load a TIFF before entering split mode.")
            return
        if bool(getattr(self, "split_mode", False)) and early_split_key in (
            "v", "b", "n", "c", "z", "delete", "del"
        ):
            split_idx = self._active_loaded_panel_index(getattr(self, "active_idx", 0))
            if split_idx is not None:
                self._handle_split_key(ev, split_idx, key)
            return

        if ev.inaxes in self.axes:
            event_idx = self.axes.index(ev.inaxes)
            if not self._panel_has_loaded_image(event_idx):
                if key == 'tab':
                    self._open_colormap_dialog()
                elif key in ('\\', 'backslash'):
                    self._open_layout_dialog()
                elif key in ('/', 'slash'):
                    self._toggle_theme()
                return

        idx = self.idx_from_event(ev)
        ax = self.axes[idx]
        data = self.warp_data[idx]
        data.setdefault('point_order', [])

        if self._split_handle_warp_key(ev, idx, key):
            return

        if self._handle_split_key(ev, idx, key):
            return

        # ` — add/edit a pending comment for the image under the mouse.
        if key in ('`', 'backquote', 'quoteleft', 'grave', 'graveaccent'):
            if ev.inaxes in self.axes:
                comment_idx = self.axes.index(ev.inaxes)
                if self.current[comment_idx] and self._panel_has_image(comment_idx):
                    self.active_idx = comment_idx
                    self._open_image_comment_dialog(comment_idx)
            return

        # TAB — open display options (colormap + vector colors)
        if key == 'tab':
            self._open_colormap_dialog()
            return

        # \ — open panel layout dialog
        if key in ('\\', 'backslash'):
            self._open_layout_dialog()
            return

        # / — toggle light / dark mode
        if key in ('/', 'slash'):
            self._toggle_theme()
            return

        basemap_loaded = self._basemap_loaded(idx)
        basemap_shortcut_keys = (
            '[', ']', 'bracketleft', 'leftbracket', 'bracketright', 'rightbracket',
            ';', 'semicolon', "'", 'apostrophe', 'quote',
        )

        # If this slot is empty, ignore panel-specific commands
        if not self.current[idx] and key not in ('f', 'tab') + basemap_shortcut_keys:
            return

        # [ / ; - adjust thermal opacity over the basemap.
        if key in ('[', 'bracketleft', 'leftbracket'):
            if not basemap_loaded or not self.current[idx]:
                return
            self._handle_thermal_transparency_key_press(key, -0.05)
            return

        if key in (';', 'semicolon'):
            if not basemap_loaded or not self.current[idx]:
                return
            self._handle_thermal_transparency_key_press(key, 0.05)
            return

        # ] / ' - cycle thermal blending mode over the basemap.
        if key in (']', 'bracketright', 'rightbracket'):
            if not basemap_loaded:
                return
            self._cycle_thermal_blend_mode(1)
            return

        if key in ("'", "apostrophe", "quote"):
            if not basemap_loaded:
                return
            self._cycle_thermal_blend_mode(-1)
            return

        # - / =  - adjust filename title and Delta Time text only.
        if key in ('-', 'minus'):
            new_size = self.title_fontsize * 0.9
            self.title_fontsize = max(8.0, min(40.0, new_size))
            try:
                self.persisted_ui_settings["title_fontsize"] = self.title_fontsize
            except Exception:
                pass
            for ax_ in self.axes:
                label = getattr(ax_, "_panel_title_text", "")
                if label:
                    self._set_panel_title(ax_, label)
                delta_label = getattr(ax_, "_basemap_delta_text", "")
                if delta_label:
                    self._set_basemap_delta_label(ax_, delta_label)
            self.canvas.draw_idle()
            return

        if key in ('=', 'equal', '+'):
            new_size = self.title_fontsize * 1.1
            self.title_fontsize = max(8.0, min(40.0, new_size))
            try:
                self.persisted_ui_settings["title_fontsize"] = self.title_fontsize
            except Exception:
                pass
            for ax_ in self.axes:
                label = getattr(ax_, "_panel_title_text", "")
                if label:
                    self._set_panel_title(ax_, label)
                delta_label = getattr(ax_, "_basemap_delta_text", "")
                if delta_label:
                    self._set_basemap_delta_label(ax_, delta_label)
            self.canvas.draw_idle()
            return

        # R — toggle colormap (apply to all panels)
        if key == 'r':
            alt = getattr(self, "alt_cmap", "magma")
            self.cmap_mode = alt if self.cmap_mode == 'gray' else 'gray'
            self._refresh_all_thermal_displays()
            self.ensure_selectors()
            self.kill_toolbar_tools()
            self.canvas.draw_idle()
            return

        # E — toggle Sobel edges globally, preserving global contrast state
        if key == 'e':
            self.global_edge_mode = not self.global_edge_mode

            for i in range(self.n_pan):
                # --- INSERT (#8/#9): skip empty panel slots / missing images ---
                if not self.current[i]:
                    continue
                img_obj = self.images.get(i) if hasattr(self.images, "get") else self.images[i]
                base    = self.images_data.get(i) if hasattr(self.images_data, "get") else self.images_data[i]
                if img_obj is None or base is None:
                    continue
                # -------------------------------------------------------------

                if self.global_edge_mode:
                    # compute & cache edges for panel i if missing or stale
                    edges, e_range = self._build_edge_display(base)
                    self.edge_cache[i] = {"data": edges, "range": e_range}
                    self._refresh_panel_thermal_display(
                        i,
                        display=self.edge_cache[i]["data"],
                        data_range=self.edge_cache[i]["range"],
                    )
                else:
                    # restore base
                    dmin, dmax = self.data_ranges.get(i, (None, None))
                    self._refresh_panel_thermal_display(i, display=base, data_range=(dmin, dmax))

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
                # --- INSERT (#8/#9): skip empty panel slots / missing images ---
                if not self.current[i]:
                    continue
                img_obj = self.images.get(i) if hasattr(self.images, "get") else self.images[i]
                if img_obj is None:
                    continue
                # -------------------------------------------------------------

                self._refresh_panel_thermal_display(i)

            self._update_blend_mode_status_label()
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
                # --- INSERT (#8/#9): skip empty panel slots / missing images ---
                if not self.current[i]:
                    continue
                img_obj = self.images.get(i) if hasattr(self.images, "get") else self.images[i]
                if img_obj is None:
                    continue
                # -------------------------------------------------------------

                self._refresh_panel_thermal_display(i)

            self._update_blend_mode_status_label()
            self.canvas.draw_idle()
            return

        # X — skip/defer current scene on active panel
        if key == 'x':
            if self.queue and self.current[idx]:
                cur = self.current[idx]
                self._discard_pending_comment_for_path(cur)
                self.queue.append(cur)
                self.current[idx] = self.queue.pop(0)
                # clear state for this panel before drawing the new file
                self._reset_split_state(idx, draw=False)
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
            self._sync_button_visibility()
            self.canvas.draw_idle()
            return

        # 1/2/3 — halve/double/reset the pan multiplier
        if key == '1':
            self._step_pan_multiplier(0.5)
            return
        if key == '2':
            self._step_pan_multiplier(2.0)
            return
        if key == '3':
            self._reset_pan_multiplier()
            return

        # WASD — apply custom pan when in pan_mode (adjusts extent, not zoom)
        if self.pan_mode and key in 'wasd':
            if not self.current[idx]:
                return
            L, R, B, T = self.bases[idx]
            dx, dy = self.offsets[idx]
            sx = (R - L) * self.pan_factor
            sy = (T - B) * self.pan_factor
            if bool(getattr(self, "split_mode", False)):
                move_dx = 0.0
                move_dy = 0.0
                if key == 'w': move_dy += sy
                if key == 's': move_dy -= sy
                if key == 'a': move_dx -= sx
                if key == 'd': move_dx += sx
                if self._split_translate_selected(idx, move_dx, move_dy):
                    self.canvas.draw_idle()
                    return
            if key == 'w': dy += sy
            if key == 's': dy -= sy
            if key == 'a': dx -= sx
            if key == 'd': dx += sx
            self.offsets[idx] = [dx, dy]
            try:
                img_obj = self.images.get(idx) if hasattr(self.images, "get") else self.images[idx]
                if img_obj is not None:
                    img_obj.set_extent([L + dx, R + dx, B + dy, T + dy])
                    if self._thermal_blend_mode_active(idx):
                        self._refresh_panel_thermal_display(idx, extent=[L + dx, R + dx, B + dy, T + dy])
                    self._refresh_split_artists(idx, draw=False)
            except Exception:
                pass
            self.canvas.draw_idle()
            return

        # G — toggle/apply warp collection & application
        if key == 'g':
            if not self.current[idx]:
                return
            if not data['collecting']:
                data['collecting'] = True
                self._sync_button_visibility()
                banner = self.axes[idx].text(
                    0.5, 0.95, "Warping...", ha='center',
                    transform=self.axes[idx].transAxes, color=self.theme['text'],
                    fontsize=self._scaled_main_panel_fontsize(12.0, minimum=5.0, maximum=48.0), zorder=6
                )
                self._set_panel_text_artist_fontsize(banner, 12.0, minimum=5.0, maximum=48.0)
                data['banner'] = banner
                self.canvas.draw_idle()
            else:
                data['collecting'] = False
                if 'banner' in data:
                    try: data['banner'].remove()
                    except Exception: pass
                self._sync_button_visibility()
                if len(data['src_pix']) >= 3 and len(data['dst_pix']) >= 3:
                    src_pts = np.array(data['src_pix'])
                    dst_pts = np.array(data['dst_pix'])
                    tform = estimate_transform('affine', src_pts, dst_pts)
                    combined_tform = _compose_affine_transforms(data.get('tform'), tform)
                    data['tform'] = combined_tform
                    display_tform = combined_tform
                    try:
                        h_disp, w_disp = self.images_data[idx].shape[:2]
                        L, R, B, T = self.bases[idx]
                        display_grid = _pixel_transform_from_bounds(L, B, R, T, w_disp, h_disp)
                        display_tform = _convert_pixel_tform_between_grids(
                            combined_tform,
                            self.srctrans[idx],
                            display_grid,
                        )
                    except Exception:
                        display_tform = combined_tform
                    categorical_preview = _categorical_raster_categories_for_path(self.current[idx]) is not None
                    warped = skwarp(
                        self.images_data[idx],
                        inverse_map=display_tform.inverse,
                        output_shape=self.images_data[idx].shape,
                        cval=np.nan, preserve_range=True,
                        order=0 if categorical_preview else None,
                    )
                    try:
                        img_obj = self.images.get(idx) if hasattr(self.images, "get") else self.images[idx]
                        if img_obj is not None:
                            if np.isfinite(warped).any():
                                warped_range = (float(np.nanmin(warped)), float(np.nanmax(warped)))
                            else:
                                warped_range = (None, None)
                            self._refresh_panel_thermal_display(
                                idx,
                                display=warped,
                                data_range=warped_range,
                            )
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
                    data['point_order'].clear()

                    # mark warp as applied before any optional overlay redraw
                    data['applied'] = True

                    # re-plot overlays (primary first)
                    self._clear_vector_overlays(self.axes[idx])
                    self._plot_vector_overlays(self.axes[idx])

                    self._set_panel_title(self.axes[idx], os.path.basename(self.current[idx]))
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
                    data['point_order'].clear()
                    self.canvas.draw_idle()
            return

        # H / J — record source / target warp control points at the cursor location
        if data['collecting'] and key in ('h', 'j') and ev.inaxes in self.axes:
            xw, yw = ev.xdata, ev.ydata
            if xw is None or yw is None:
                return

            dx, dy = self.offsets[idx]
            x0, y0 = xw - dx, yw - dy
            col, row = ~self.srctrans[idx] * (x0, y0)

            if key == 'h':
                data['src_world'].append((x0, y0))
                data['src_pix'].append((col, row))
                marker_color = normalize_keep_reject_button_color(
                    getattr(self, "warp_source_color", DEFAULT_WARP_SOURCE_COLOR),
                    DEFAULT_WARP_SOURCE_COLOR,
                )
                point_number = len(data['src_pix'])
                data['point_order'].append('src')
            else:
                data['dst_world'].append((x0, y0))
                data['dst_pix'].append((col, row))
                marker_color = normalize_keep_reject_button_color(
                    getattr(self, "warp_target_color", DEFAULT_WARP_TARGET_COLOR),
                    DEFAULT_WARP_TARGET_COLOR,
                )
                point_number = len(data['dst_pix'])
                data['point_order'].append('dst')

            mark, = ev.inaxes.plot(xw, yw, 'o', color=marker_color, markersize=6, zorder=5)
            data['markers'].append(mark)
            lbl = ev.inaxes.text(
                xw, yw, str(point_number),
                color='white',
                fontsize=self._scaled_main_panel_fontsize(8.0, minimum=4.0, maximum=32.0),
                zorder=6
            )
            self._set_panel_text_artist_fontsize(lbl, 8.0, minimum=4.0, maximum=32.0)
            data['labels'].append(lbl)
            self.canvas.draw_idle()
            return

        # BACKSPACE — undo the most recently added warp control point
        if key == 'backspace' and data['collecting']:
            if data['markers']:
                try:
                    data['markers'].pop().remove()
                except Exception:
                    pass
            if data['labels']:
                try:
                    data['labels'].pop().remove()
                except Exception:
                    pass

            point_kind = data['point_order'].pop() if data['point_order'] else None
            if point_kind == 'dst':
                if data['dst_pix']:
                    data['dst_pix'].pop()
                if data['dst_world']:
                    data['dst_world'].pop()
            else:
                if data['src_pix']:
                    data['src_pix'].pop()
                if data['src_world']:
                    data['src_world'].pop()

            self.canvas.draw_idle()
            return

        # ENTER / RETURN — reset contrast & gamma to original, and turn OFF edges
        if key in ('enter', 'return'):
            # reset global state
            self.global_contrast_rel = (0.0, 1.0)   # full range
            self.global_gamma = 1.0                 # linear
            self.global_edge_mode = False           # base imagery
            self.thermal_alpha = 1.0                # 0% transparent
            self.thermal_blend_mode = "normal"      # default basemap blend
            try:
                self.edge_cache.clear()             # optional: free cached edges
            except Exception:
                pass

            # push to all panels
            for i in range(self.n_pan):
                # --- INSERT (#8): skip empty panel slots / missing images ---
                if not self.current[i]:
                    continue
                img_obj = self.images.get(i) if hasattr(self.images, "get") else self.images[i]
                base    = self.images_data.get(i) if hasattr(self.images_data, "get") else self.images_data[i]
                if img_obj is None or base is None:
                    continue
                # ------------------------------------------------------------

                # reset contrast to the panel's native data range
                dmin, dmax = self.data_ranges.get(i, (None, None))
                self._refresh_panel_thermal_display(i, display=base, data_range=(dmin, dmax))

            self._apply_thermal_alpha()
            self.canvas.draw_idle()
            return

    # ------------------------ Keep/Reject callbacks ------------------------ #
    def make_cb(self, idx, act):
        def _cb(_event):
            path = self.current[idx]

            # ignore empty slots ---
            if not path:
                return

            ax = self.axes[idx]
            # Save current view limits
            last_xlim = ax.get_xlim(); last_ylim = ax.get_ylim()
            fname = os.path.basename(path)
            dt = parse_datetime_from_filename(path)
            basemap_fname, basemap_delta_days = self._basemap_log_values_for_panel(idx)
            dx, dy = self.offsets[idx]
            data = self.warp_data[idx]
            warp_flag = bool(data.get('tform')) or data.get('applied', False)
            split_restructured = self._split_has_restructure(idx)

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
            if dx == 0 and dy == 0:
                azimuth = 0.0
            else:
                azimuth = azimuth % 360

            if warp_flag and data.get('tform') is not None:
                p = data['tform'].params
                vals = [p[0,0], p[0,1], p[0,2], p[1,0], p[1,1], p[1,2]]
            else:
                vals = [0,0,0,0,0,0]

            if act == 'reject':
                behavior = dict(getattr(self, 'reject_behavior', {}) or {})
                mode = str(behavior.get('mode', 'delete') or 'delete').lower()
                rejected_suffix_fname = ""
                reject_original_preserved = False
                if mode == 'suffix':
                    target = self._make_unique_output_path(
                        self._build_suffixed_output_path(path, behavior.get('suffix', '_reject'), '_reject')
                    )
                    try:
                        reject_original_preserved = bool(behavior.get('preserve_original', False))
                        if reject_original_preserved:
                            shutil.copy2(path, target)
                        else:
                            shutil.move(path, target)
                        rejected_suffix_fname = os.path.basename(target)
                    except Exception as e:
                        QMessageBox.warning(self, 'Reject Save Failed', f'Could not save rejected file for {fname}:\n{e}')
                        return
                else:
                    try:
                        os.remove(path)
                    except Exception as e:
                        try:
                            QMessageBox.warning(None, 'Delete Failed',
                                                f'Could not delete {fname}:\n{e}')
                        except Exception:
                            pass
                        return

                comment = self._take_pending_comment_for_path(path)
                if mode != 'suffix':
                    self.log_entries.append((
                        fname, dt, REJECT_AZIMUTH, REJECT_DISTANCE, warp_flag, vals,
                        basemap_fname, basemap_delta_days, _current_log_logged_datetime(), comment,
                    ))
                elif reject_original_preserved:
                    self.log_entries.append((
                        fname, dt, None, None, False, [None] * 6,
                        basemap_fname, basemap_delta_days, _current_log_logged_datetime(), ORIGINAL_COPY_COMMENT,
                    ))
                if rejected_suffix_fname:
                    self.log_entries.append((
                        rejected_suffix_fname, dt, REJECT_AZIMUTH, REJECT_DISTANCE, warp_flag, vals,
                        basemap_fname, basemap_delta_days, _current_log_logged_datetime(), comment,
                    ))
                    self.processed_files.add(rejected_suffix_fname)
                self.session['reject'] += 1
                self.session_processed += 1

            else:
                behavior = dict(getattr(self, 'keep_behavior', {}) or {})
                mode = str(behavior.get('mode', 'overwrite') or 'overwrite').lower()
                target_path = path
                created_output = None

                if mode == 'suffix':
                    target_path = self._make_unique_output_path(
                        self._build_suffixed_output_path(path, behavior.get('suffix', '_keep'), '_keep')
                    )
                    try:
                        shutil.copy2(path, target_path)
                    except Exception as e:
                        QMessageBox.warning(self, 'Keep Save Failed', f'Could not prepare output file for {fname}:\n{e}')
                        return
                    created_output = target_path

                try:
                    self._apply_keep_transforms_to_path(target_path, dx, dy, warp_flag, data, idx=idx)
                except Exception as e:
                    if created_output and os.path.exists(created_output):
                        try:
                            os.remove(created_output)
                        except Exception:
                            pass
                    QMessageBox.warning(self, 'Keep Failed', f'Could not save Keep output for {fname}:\n{e}')
                    return

                if mode == 'suffix':
                    original_preserved = bool(behavior.get('preserve_original', False))
                    if original_preserved:
                        self.log_entries.append((
                            fname, dt, None, None, False, [None] * 6,
                            basemap_fname, basemap_delta_days, _current_log_logged_datetime(), ORIGINAL_COPY_COMMENT,
                        ))
                    else:
                        try:
                            os.remove(path)
                        except Exception as e:
                            try:
                                QMessageBox.warning(self, 'Original Not Removed',
                                                    f'Created {os.path.basename(target_path)}, but could not remove the original {fname}:\n{e}')
                            except Exception:
                                pass

                comment = self._take_pending_comment_for_path(path)
                if split_restructured:
                    azimuth = None
                    dist = None
                    warp_flag = False
                    vals = [None] * 6
                    comment = STRIPES_RESTRUCTURED_COMMENT
                log_fname = os.path.basename(target_path) if mode == 'suffix' else fname
                self.log_entries.append((
                    log_fname, dt, azimuth, dist, warp_flag, vals,
                    basemap_fname, basemap_delta_days, _current_log_logged_datetime(), comment,
                ))
                self.processed_files.add(log_fname)
                if split_restructured:
                    self.session['geo'] += 1
                elif dist > 0:
                    self.session['geo'] += 1
                if warp_flag:
                    self.session['warp'] += 1
                if not split_restructured and not warp_flag and dist <= 0:
                    self.session['as_is'] += 1
                self.session_processed += 1

            self.processed_files.add(fname)

            # After handling this scene, clear in-memory warp state so the next image starts fresh
            self.warp_data[idx] = {
                "src_world": [], "dst_world": [],
                "src_pix": [],   "dst_pix": [],
                "tform": None,   "applied": False,
                "collecting": False, "markers": [], "labels": []
            }
            self._reset_split_state(idx, draw=False)

            # Persist log immediately
            self.flush_log()

            # Update UI and advance queue
            self._update_progress_footer()

            if self.queue:
                self.current[idx] = self.queue.pop(0)
                self.draw(idx)
                if not self._set_panel_view_exact(ax, last_xlim, last_ylim, idx=idx):
                    self._fit_panel_to_current_scene(idx, sync=False)
                self.canvas.draw_idle()
            else:
                self._clear_panel_slot(idx, message='NO MORE IMAGES', fontsize=16)
                self.canvas.draw_idle()

            if self.session_processed == self.total:
                self.flush_log()
                self.show_final_summary()

        return _cb

    def on_key_release(self, ev):
        key = (getattr(ev, 'key', '') or '').lower()
        self._release_thermal_transparency_key(key)

    # -------------------------- Final Summary ------------------------------ #
    def show_final_summary(self):
        # Recompute totals across entire log_entries
        counts = summarize_log_entries(self.log_entries)
        total_log = counts['processed']

        def pct(k):
            return (counts[k] / total_log * 100) if total_log else 0.0

        # -----------------------------
        # Modal dialog (DPI-aware size)
        # -----------------------------
        dlg = QDialog(self)
        dlg.setWindowTitle("Final Summary")
        dlg.setModal(True)

        # Fixed physical size across monitors (similar to SplashDialog)
        base_w, base_h = 470, 775  # reference size at 120 DPI

        screen = dlg.screen() or QtWidgets.QApplication.primaryScreen()
        if screen is not None:
            dpi = screen.logicalDotsPerInch() or 120.0
            scale = dpi / 120.0

            w = int(base_w * scale)
            h = int(base_h * scale)

            # Avoid going off-screen: cap at 95% of available geometry
            geo = screen.availableGeometry()
            w = min(w, int(geo.width() * 0.95))
            h = min(h, int(geo.height() * 0.95))
        else:
            w, h = base_w, base_h

        dlg.setFixedSize(w, h)
        dlg.setWindowFlags(dlg.windowFlags() | QtCore.Qt.WindowContextHelpButtonHint)

        lay = QVBoxLayout(dlg)

        # -----------------------------
        # Matplotlib figure
        # -----------------------------
        sum_theme = build_theme_palette(get_app_theme_mode())
        dlg.setStyleSheet(build_app_stylesheet(get_app_theme_mode()))
        fig = Figure(dpi=120)
        fig.patch.set_facecolor(sum_theme['window_bg'])
        cvs = FigureCanvas(fig)
        cvs.setStyleSheet(f"background-color: {sum_theme['window_bg']};")
        lay.addWidget(cvs)

        # -----------------------------
        # Text styling (smaller overall)
        # -----------------------------
        shapes1 = "▛▟ ▞▚ ▟▛ ▞▚"
        shapes2 = "▟ ▛ ▙"

        # Header glyphs & title
        # Use native Qt text here: it supports the glyph fallback needed for the
        # logo and QFont stretch widens the glyphs horizontally as requested.
        logo_font = QtGui.QFont("Lucida Console", 24, QtGui.QFont.Bold)
        logo_font.setStretch(85)

        def _make_logo_label(text, color):
            lbl = QtWidgets.QLabel(text, cvs)
            lbl.setFont(logo_font)
            lbl.setAlignment(QtCore.Qt.AlignHCenter | QtCore.Qt.AlignVCenter)
            lbl.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
            lbl.setStyleSheet(f"color: {color}; background: transparent;")
            lbl.show()
            return lbl

        logo1 = _make_logo_label(shapes1, sum_theme['heading'])
        logo2 = _make_logo_label(shapes2, sum_theme['splash_red'])
        final_summary_text_scale = 0.75

        def _position_logo_labels():
            rect = cvs.rect()
            h = QtGui.QFontMetrics(logo_font).height() + 6
            for lbl, y_frac in ((logo1, 0.92), (logo2, 0.86)):
                top = max(0, int(rect.height() * (1.0 - y_frac) - h / 2))
                lbl.setGeometry(0, top, rect.width(), h)
                lbl.raise_()
        fig.text(
            0.5, 0.74, 'NASA JPL Thermal Viewer',
            ha='center', fontsize=18 * final_summary_text_scale, color=sum_theme['heading']
        )
        fig.text(
            0.5, 0.68, 'FINAL SUMMARY',
            ha='center', fontsize=16 * final_summary_text_scale, color=sum_theme['text']
        )

        # Counts
        from matplotlib import patheffects as pe
        count_stroke = [pe.withStroke(linewidth=0.10, foreground=sum_theme['text'])]
        y = 0.56
        for key in ['as_is', 'geo', 'warp', 'reject']:
            label = {'as_is': 'Preserved', 'geo': 'Transformed', 'warp': 'Warped', 'reject': 'Rejected'}[key]
            txt = fig.text(
                0.5, y,
                f"{label}: {counts[key]} ({pct(key):.1f}%)",
                ha='center',
                fontsize=14 * final_summary_text_scale,
                color=sum_theme['text'],
                fontfamily='DejaVu Sans Mono',
                fontweight='regular'
            )
            txt.set_path_effects(count_stroke)
            y -= 0.07

        # Footer text
        fig.text(
            0.32, 0.12,
            'Close screen to \n  save and exit.',
            ha='center',
            fontsize=13 * final_summary_text_scale,
            color=sum_theme['text'],
            fontfamily='DejaVu Sans Mono',
            linespacing=1.5
        )

        # ASCII art (small)
        art = r"""

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

        """
        fig.text(
            0.65, 0.32, art,
            ha='center',
            va='top',
            family='DejaVu Sans Mono',
            fontsize=7 * final_summary_text_scale,
            color=sum_theme['heading']
        )

        cvs.draw()
        QtCore.QTimer.singleShot(0, _position_logo_labels)

        # --- Custom help dialog with blinking cursor, 5s suspense, growth animation ---
        # --- Simple, fixed-size Help dialog (fits full text; 5s -> 3s pause; +0.5s after line 1) ---
        class HelpDialog(QtWidgets.QDialog):
            def __init__(self, parent=None):
                super().__init__(parent)
                self.setWindowTitle("Help")
                self.setModal(True)

                # Big enough to show all lines without geometry animations
                self.resize(350, 425)
                self.setMinimumSize(370, 390)
                self.theme_mode = get_app_theme_mode()
                self.theme = build_theme_palette(self.theme_mode)
                self.setStyleSheet(build_app_stylesheet(self.theme_mode))

                frame = QtWidgets.QFrame(self)
                frame.setStyleSheet(
                    f"QFrame {{ border: 2px solid {self.theme['border']}; border-radius: 10px; "
                    f"background-color: {self.theme['panel_bg']}; }}"
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
                    f"font-family: 'Lucida Console','DejaVu Sans Mono'; font-size: 14pt; color: {self.theme['help_text']};"
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
                   help="Override GDAL block cache size in MB (RAM).")
    p.add_argument("--threads", default=None,
                   help="Number of threads for rasterio.reproject (int) or 'all'.")
    p.add_argument("--theme", choices=["light", "dark"], default=None,
                   help="Startup theme. Defaults to the saved viewer setting; '/' toggles after launch.")
    p.add_argument("--ui-scale", default=None,
                   help="Startup UI scale override: 'auto' or a multiplier such as 0.9, 1.1, or 1.25.")
    return p

def prompt_startup_settings(persisted_ui_store, example_filename="", log_exists=False, parent=None):
    dlg = StartupSettingsDialog(
        persisted_ui_store,
        discover_startup_shapefiles(),
        discover_basemap_paths(),
        example_filename=example_filename,
        log_exists=log_exists,
        parent=parent,
    )
    if dlg.exec_() != QtWidgets.QDialog.Accepted:
        return None
    return dlg.selected_startup_config()

def main():
    persisted_ui_store = load_persisted_ui_store()
    persisted_ui_settings = load_persisted_ui_settings(persisted_ui_store)
    parser = _build_arg_parser()
    known, _ = parser.parse_known_args()

    if known.gdal_cache_mb is not None:
        os.environ["GDAL_CACHEMAX"] = str(int(known.gdal_cache_mb))
    if known.threads is not None:
        global REPROJECT_THREADS
        REPROJECT_THREADS = _parse_threads(known.threads)

    app = QApplication(sys.argv)
    if known.ui_scale is not None:
        apply_persisted_ui_scale_setting(known.ui_scale, reapply=False)
    else:
        apply_persisted_ui_scale_setting(
            persisted_ui_store.get("last_ui_scale", "auto"),
            reapply=False,
        )
    try:
        app.setFont(QtGui.QFont("Lucida Console", 11))
        app.setProperty("geoviewer_ui_scale", ui_scale())
    except Exception:
        pass
    try:
        app._scrollable_menu_style = ScrollableMenuProxyStyle(app.style())
        app.setStyle(app._scrollable_menu_style)
    except Exception:
        pass
    app._screen_scroll_controller = AppWideScreenScrollController(app)
    app.installEventFilter(app._screen_scroll_controller)
    app._alt_magnifier_controller = AppWideAltMagnifierController(app)
    app.installEventFilter(app._alt_magnifier_controller)
    try:
        app.aboutToQuit.connect(app._alt_magnifier_controller.cleanup)
    except Exception:
        pass
    startup_theme = known.theme if known.theme else persisted_ui_settings.get("theme_mode", "dark")
    set_app_theme_mode(startup_theme)

    # Splash
    splash = SplashDialog()
    splash.exec_()

    # Gather TIFFs
    all_files = sorted(
        {path for path in (glob.glob("*.tif") + glob.glob("*.tiff")) if os.path.isfile(path)},
        key=lambda p: os.path.basename(p).lower(),
    )
    if not all_files:
        QMessageBox.information(None, "Info", "No TIFFs found in working directory. \nAdd a TIFF, e.g. *_LST.tif to this folder.")
        return 0

    processed_dts, processed_files, log_entries = read_log()

    auto_mode = False
    auto_behavior = _default_auto_geocorrect_behavior()
    auto_candidate_count = 0
    if processed_dts:
        log_dlg = ExistingLogDecisionDialog(
            all_files,
            processed_dts,
            processed_files,
            log_entries,
        )
        if log_dlg.exec_() != QtWidgets.QDialog.Accepted:
            return 0
        auto_mode = (log_dlg.decision() == "auto")
        auto_behavior = log_dlg.auto_behavior()
        auto_candidate_count = int(log_dlg.inventory.get("auto_candidate_files", 0) or 0)

    if auto_mode:
        try:
            count = auto_geocorrect(
                all_files,
                processed_dts,
                processed_files,
                log_entries,
                output_behavior=auto_behavior,
            )
        except Exception as exc:
            QMessageBox.critical(None, "Auto-Geocorrect Failed", f"Auto-geocorrect could not finish:\n{exc}")
            return 1
        completion_dlg = AutoGeocorrectCompletionDialog(
            count,
            auto_candidate_count,
            auto_behavior,
        )
        completion_dlg.exec_()
        if completion_dlg.restart_requested:
            restart_geoviewer_application(None)
        return 0

    startup_config = None
    example_filename = os.path.basename(all_files[0]) if all_files else ""
    log_exists = os.path.exists(LOG_FILE)
    while True:
        startup_config = prompt_startup_settings(
            persisted_ui_store,
            example_filename=example_filename,
            log_exists=log_exists,
        )
        if startup_config is None:
            return 0

        dt_error = apply_user_datetime_pattern_settings(
            startup_config["settings"].get("filename_dt_substring"),
            startup_config["settings"].get("filename_dt_pattern"),
        )
        if not dt_error:
            break
        QMessageBox.warning(None, "Filename Pattern", dt_error)

    persisted_ui_store = normalize_persisted_ui_store(
        startup_config.get("persisted_ui_store", persisted_ui_store)
    )
    set_app_theme_mode(startup_config["settings"].get("theme_mode", startup_theme))

    win = ThermalViewerQt(
        all_files,
        processed_files,
        log_entries,
        persisted_ui_store=persisted_ui_store,
        startup_profile_name=startup_config["profile_name"],
        startup_ui_settings=startup_config["settings"],
    )
    win.show()

    return app.exec_()

if __name__ == "__main__":
    sys.exit(main())
