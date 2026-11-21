"""
CordInsight - Danio Rerio Tracking
Copyright (C) 2026 Riham Jameel, Ammar Alhajmee, Krys Strand

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, version 3.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.

A modern GUI application for tracking zebrafish (Danio rerio) movement in video footage.
Designed to be packaged as a standalone executable using PyInstaller.

Pipeline (per video):
    1. Select video(s)        - single or batch; auto-converts MTS/M2TS via ffmpeg
    2. Pick a frame, trace the fish to set its detection size
    3. Draw the arena (+ each side's real length to calibrate) and zones
    4. Choose measurements    - time/entries per zone, distance/speed
    5. Process & export       - Excel workbook + heatmap

Tracking is two-pass: an online detection pass records only raw detections, then an
offline pass bridges all bounded dropouts (interpolating between detections on either
side) so a subject seen before and after a gap stays present, with no teleport-smear.

Usage:
    python cordinsight.py

To build executable:
    pip install pyinstaller
    pyinstaller --onefile --windowed --name "CordInsight" cordinsight.py

Dependencies:
    pip install opencv-python numpy pandas openpyxl pillow imageio-ffmpeg
"""

import os
import sys
import math
import time
import threading
import queue
import subprocess
import tempfile
import atexit
from datetime import datetime
from dataclasses import dataclass, asdict, field
from typing import List, Tuple, Dict, Optional
from pathlib import Path

import imageio_ffmpeg

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import cv2
import numpy as np
import pandas as pd
from PIL import Image, ImageTk

# -----------------------------
# Configuration
# -----------------------------

APP_NAME = "CordInsight"
APP_SUBTITLE = "Danio Rerio Tracking"
APP_VERSION = "0.0.1"

# Colors (Light theme with Maroon, Gold, and Slate accents)
# Maroon: #6C1D45 | Gold: #EAAA00 | Slate: #333F48
COLORS = {
    "bg_dark": "#e8e8e8",       # Darker gray for sidebar
    "bg_medium": "#ffffff",     # Pure white
    "bg_light": "#fdfbfc",      # Almost white with subtle warm tint
    "accent": "#6C1D45",        # Maroon (PANTONE 222)
    "accent_hover": "#551534",  # Darker maroon on hover
    "gold": "#EAAA00",          # Gold (PANTONE 124)
    "gold_hover": "#C99200",    # Darker gold on hover
    "text": "#333F48",          # Slate (PANTONE 432)
    "text_dim": "#6b7780",      # Lighter slate for secondary text
    "success": "#EAAA00",       # Gold for success
    "success_hover": "#C99200", # Darker gold
    "warning": "#EAAA00",       # Gold
    "error": "#6C1D45",         # Maroon for errors
    "border": "#e5e7ea",        # Light gray border
}

# -----------------------------
# Video Format Converter
# -----------------------------

class VideoConverter:
    """
    Handles conversion of problematic video formats (like .MTS) to .mp4
    using ffmpeg for reliable OpenCV compatibility across all systems.
    """

    CONVERT_EXTENSIONS = {'.mts', '.m2ts', '.mxf', '.vob', '.ts', '.m2v'}

    def __init__(self):
        self._temp_files = []
        self._ffmpeg_path = None
        atexit.register(self._cleanup)

    def _get_ffmpeg(self):
        if self._ffmpeg_path is None:
            self._ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
        return self._ffmpeg_path

    def _cleanup(self):
        for path in self._temp_files:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        self._temp_files.clear()

    def needs_conversion(self, video_path: str) -> bool:
        ext = os.path.splitext(video_path)[1].lower()
        if ext in self.CONVERT_EXTENSIONS:
            return True
        cap = cv2.VideoCapture(video_path)
        opened = cap.isOpened()
        if opened:
            ok, _ = cap.read()
            cap.release()
            return not ok
        return True

    def convert(self, video_path: str, progress_callback=None) -> str:
        """
        Convert video to .mp4 format. Returns path to converted file,
        or original path if conversion not needed.
        """
        if not self.needs_conversion(video_path):
            return video_path

        base_name = os.path.splitext(os.path.basename(video_path))[0]
        # Reserve a unique path atomically. A timestamp alone collides when two
        # same-named clips are converted within one second (common in batches).
        fd, output_path = tempfile.mkstemp(
            prefix=f"cordinsight_{base_name[:40]}_", suffix=".mp4")
        os.close(fd)

        try:
            ffmpeg = self._get_ffmpeg()
            cmd = [
                ffmpeg,
                '-i', video_path,
                '-c:v', 'libx264',
                '-preset', 'veryfast',
                '-crf', '23',
                '-an',  # tracking ignores audio; dropping it speeds up conversion
                '-y',
                output_path
            ]
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=3600,  # safety net: a corrupt file must not hang forever
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                )
            except subprocess.TimeoutExpired:
                try:
                    os.remove(output_path)
                except OSError:
                    pass
                raise RuntimeError(
                    "FFmpeg conversion timed out after 1 hour — the file may be corrupt "
                    "or unusually large. Try converting it to MP4 manually first.")
            if result.returncode != 0:
                raise RuntimeError(f"FFmpeg conversion failed: {result.stderr}")

            self._temp_files.append(output_path)
            return output_path
        except FileNotFoundError:
            try:
                os.remove(output_path)
            except OSError:
                pass
            raise RuntimeError("FFmpeg not found. Please ensure imageio-ffmpeg is installed.")
        except Exception:
            try:
                os.remove(output_path)
            except OSError:
                pass
            raise


# Global converter instance
VIDEO_CONVERTER = VideoConverter()

# -----------------------------
# Utilities
# -----------------------------

def get_resource_path(relative_path: str) -> str:
    """Get absolute path to resource, works for dev and for PyInstaller."""
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)


# Real-world length of one unit, expressed in millimetres. Used to combine
# scale lines drawn in different units into a single comparable scale.
UNIT_TO_MM = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4}


def compute_scale_from_lines(lines, display_unit: Optional[str] = None) -> Optional[Dict]:
    """
    Turn one or more known-length "scale lines" into a single uniform image scale
    (real units per pixel).

    Each line is a dict {"p1": [x, y], "p2": [x, y], "length": float, "unit": str}:
    the user drew it across something whose real length they know (the tank's
    height, a ruler). For each line we get real_length / pixel_length; the lines
    are averaged for robustness, and their spread is reported so the user can see
    how well they agree (large disagreement => the camera is not perpendicular, or
    a length was mistyped). Calibration assumes a roughly perpendicular ("straight
    on") view, where the scale is uniform across the frame regardless of tank shape
    — so it works for trapezoidal novel tanks, round dishes, anything.

    Returns {"units_per_px", "unit", "n_lines", "spread_pct"} or None if no line
    yields a usable scale. display_unit picks the reporting unit (defaults to the
    unit of the first valid line).
    """
    if not lines:
        return None
    per_px_mm: List[float] = []
    for sl in lines:
        try:
            length = float(sl.get("length", 0) or 0)
        except (TypeError, ValueError):
            continue
        p1, p2, unit = sl.get("p1"), sl.get("p2"), sl.get("unit", "cm")
        if length <= 0 or not p1 or not p2 or unit not in UNIT_TO_MM:
            continue
        pixel_len = math.hypot(float(p2[0]) - float(p1[0]), float(p2[1]) - float(p1[1]))
        if pixel_len <= 0 or not math.isfinite(pixel_len):
            continue
        per_px_mm.append(length * UNIT_TO_MM[unit] / pixel_len)
    if not per_px_mm:
        return None
    if display_unit not in UNIT_TO_MM:
        display_unit = next((sl.get("unit") for sl in lines
                             if sl.get("unit") in UNIT_TO_MM), "cm")
    mean_mm = sum(per_px_mm) / len(per_px_mm)
    spread_pct = ((max(per_px_mm) - min(per_px_mm)) / mean_mm * 100.0) if mean_mm > 0 else 0.0
    return {
        "units_per_px": mean_mm / UNIT_TO_MM[display_unit],
        "unit": display_unit,
        "n_lines": len(per_px_mm),
        "spread_pct": spread_pct,
    }


def compute_scale(settings) -> Optional[Dict]:
    """Uniform image scale from the settings' scale lines (or None if uncalibrated)."""
    return compute_scale_from_lines(
        getattr(settings, "scale_lines", None) or [],
        getattr(settings, "calibration_unit", None))


def calibration_is_complete(settings) -> bool:
    """Calibration is complete once at least one usable scale line defines a scale."""
    return compute_scale(settings) is not None


def resolve_times(frame_ids: List[int], ts_ms: List[float], fps: float
                  ) -> Tuple[List[float], bool]:
    """
    Per-frame timestamps in seconds. Uses monotonic presentation timestamps whenever
    the decoder provides them; otherwise falls back to frame_index / nominal fps.
    Returns (times, used_real_time).
    """
    dt = 1.0 / fps if fps > 0 else 1.0 / 30.0
    nominal = [fid * dt for fid in frame_ids]
    valid = (len(ts_ms) == len(frame_ids) and len(ts_ms) >= 2
             and all(math.isfinite(t) for t in ts_ms)
             and ts_ms[-1] > ts_ms[0]
             and all(ts_ms[k] > ts_ms[k - 1] for k in range(1, len(ts_ms))))
    if not valid:
        return nominal, False
    real = [t / 1000.0 for t in ts_ms]
    return real, True


def frame_sample_durations(times: List[float], fallback_dt: float) -> List[float]:
    """Duration represented by each sampled frame, including a final-frame estimate."""
    n = len(times)
    if n == 0:
        return []
    fallback = fallback_dt if math.isfinite(fallback_dt) and fallback_dt > 0 else 1.0 / 30.0
    if n == 1:
        return [fallback]
    deltas = []
    for i in range(n - 1):
        delta = times[i + 1] - times[i]
        deltas.append(delta if math.isfinite(delta) and delta > 0 else fallback)
    ordered = sorted(deltas)
    mid = len(ordered) // 2
    final_delta = (ordered[mid] if len(ordered) % 2
                   else (ordered[mid - 1] + ordered[mid]) / 2.0)
    return deltas + [final_delta]


def bridge_track_gaps(det_positions: List[Optional[Tuple[float, float]]],
                      times: List[float]
                      ) -> List[Tuple[Optional[float], Optional[float], str]]:
    """
    Offline gap-filling over the whole sequence of raw detections. Returns one
    (x, y, status) per frame, where status is:
      detected     - a real measurement
      interpolated - any internal gap bounded by detections (linear interpolation)
      held         - leading/trailing gap, where only one positional anchor exists
      unresolved   - no detections exist anywhere, so no position can be reconstructed

    Presence is conserved: every bounded gap is interpolated regardless of length.
    """
    n = len(det_positions)
    det_idx = [i for i in range(n) if det_positions[i] is not None]
    if not det_idx:
        return [(None, None, "unresolved")] * n

    out: List[Optional[Tuple[Optional[float], Optional[float], str]]] = [None] * n
    first, last = det_idx[0], det_idx[-1]
    for i in range(0, first):                      # leading gap: hold first detection
        out[i] = (det_positions[first][0], det_positions[first][1], "held")
    for i in range(last + 1, n):                   # trailing gap: hold last detection
        out[i] = (det_positions[last][0], det_positions[last][1], "held")
    for i in det_idx:                              # real measurements
        out[i] = (det_positions[i][0], det_positions[i][1], "detected")
    for a, b in zip(det_idx, det_idx[1:]):         # interior gaps
        if b - a <= 1:
            continue
        ax, ay = det_positions[a]
        bx, by = det_positions[b]
        span = times[b] - times[a]
        for i in range(a + 1, b):
            frac = ((times[i] - times[a]) / span) if span > 0 else (i - a) / (b - a)
            out[i] = (ax + frac * (bx - ax), ay + frac * (by - ay), "interpolated")
    return out


def calculate_kinematics(
        filled: List[Tuple[Optional[float], Optional[float], str]],
        times: List[float], scale: Tuple[float, float], movement_threshold_px: float = 0.0,
        movement_reference: Optional[
            List[Tuple[Optional[float], Optional[float], str]]] = None
        ) -> Tuple[Dict[str, object], List[Optional[float]]]:
    """
    Calculate path distance and time-weighted speed.

    The reconstructed track is continuous whenever positional anchors exist; at
    least two direct detections are required for a defensible kinematic result.
    Distance is split into directly measured segments (both endpoints detected) and
    reconstructed segments (at least one interpolated/held endpoint) for auditability.
    """
    n = min(len(filled), len(times))
    speeds: List[Optional[float]] = [None] * n
    threshold = max(0.0, movement_threshold_px) if math.isfinite(movement_threshold_px) else 0.0
    reference = movement_reference if movement_reference is not None else filled
    direct = [i for i, p in enumerate(filled[:n]) if p[2] == "detected"]
    trajectory_resolved = len(direct) >= 2
    if not trajectory_resolved:
        return {
            "trajectory_resolved": False,
            "distance_traveled": 0.0,
            "measured_distance": 0.0,
            "reconstructed_distance": 0.0,
            "mean_speed": 0.0,
            "max_speed": 0.0,
            "kinematic_elapsed_s": 0.0,
            "movement_threshold_px": threshold,
        }, speeds
    total_distance = 0.0
    measured_distance = 0.0
    reconstructed_distance = 0.0
    elapsed = 0.0
    max_speed = 0.0

    # Leading/trailing holds establish presence and zone occupancy, but have only
    # one positional boundary. Restrict kinematics to the interval bounded by the
    # first and last direct detections so those unknowable edges cannot dilute speed
    # by silently assuming that the fish was stationary.
    first_direct, last_direct = direct[0], direct[-1]
    anchor = first_direct
    for i in range(first_direct + 1, last_direct + 1):
        x0, y0, _ = filled[i - 1]
        x1, y1, _ = filled[i]
        delta_t = times[i] - times[i - 1]
        if (x0 is None or y0 is None or x1 is None or y1 is None
                or not math.isfinite(delta_t) or delta_t <= 0):
            anchor = i if x1 is not None and y1 is not None else None
            continue
        elapsed += delta_t
        if anchor is None:
            anchor = i - 1
        ax, ay, _ = filled[anchor]
        rx1, ry1, _ = reference[i]
        rxa, rya, _ = reference[anchor]
        if rx1 is None or ry1 is None or rxa is None or rya is None:
            anchor = i
            continue
        px_step = math.hypot(rx1 - rxa, ry1 - rya)
        if px_step <= threshold:
            speeds[i] = 0.0
            continue
        movement_dt = times[i] - times[anchor]
        if not math.isfinite(movement_dt) or movement_dt <= 0:
            anchor = i
            continue
        dx = (x1 - ax) * scale[0]
        dy = (y1 - ay) * scale[1]
        step = math.hypot(dx, dy)
        speed = step / movement_dt
        speeds[i] = speed
        total_distance += step
        max_speed = max(max_speed, speed)
        if all(filled[j][2] == "detected" for j in range(anchor, i + 1)):
            measured_distance += step
        else:
            reconstructed_distance += step
        anchor = i

    metrics = {
        "trajectory_resolved": True,
        "distance_traveled": total_distance,
        "measured_distance": measured_distance,
        "reconstructed_distance": reconstructed_distance,
        "mean_speed": total_distance / elapsed if elapsed > 0 else 0.0,
        "max_speed": max_speed,
        "kinematic_elapsed_s": elapsed,
        "movement_threshold_px": threshold,
    }
    return metrics, speeds


def format_time(seconds: float) -> str:
    """Format seconds as MM:SS or HH:MM:SS."""
    if seconds < 3600:
        return f"{int(seconds // 60):02d}:{int(seconds % 60):02d}"
    return f"{int(seconds // 3600):02d}:{int((seconds % 3600) // 60):02d}:{int(seconds % 60):02d}"


def point_in_polygon(point: Tuple[float, float], polygon: np.ndarray) -> bool:
    """Check if point is inside polygon using OpenCV."""
    if polygon is None or len(polygon) < 3:
        return False
    return cv2.pointPolygonTest(polygon.astype(np.float32), (float(point[0]), float(point[1])), False) >= 0


def _segments_cross(p1, p2, p3, p4) -> bool:
    """True if open segment p1p2 properly crosses open segment p3p4."""
    def orient(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    d1, d2 = orient(p3, p4, p1), orient(p3, p4, p2)
    d3, d4 = orient(p1, p2, p3), orient(p1, p2, p4)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def suggest_fish_area_bounds(area_px2: float) -> Tuple[int, int]:
    """
    Turn one traced fish-silhouette area (px²) into sensible Min/Max detection
    bounds. The silhouette area changes with the fish's pose and distance, so the
    band is generous: Min well below (but above typical noise) and Max well above.
    """
    area = max(1.0, float(area_px2))
    lo = max(20, int(round(area * 0.3)))   # above speckle noise, below a curled pose
    hi = int(round(area * 4.0))            # above a stretched / closer pose
    if hi <= lo:
        hi = lo + 1
    return lo, hi


def polygon_invalid_reason(points) -> Optional[str]:
    """
    Return a short reason a polygon is unusable, or None if it is valid:
      - fewer than 3 points
      - (near) zero enclosed area  (degenerate / collinear → broken masks)
      - self-intersecting edges    (bow-tie → ambiguous inside/outside)
    """
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    n = len(pts)
    if n < 3:
        return "needs at least 3 points"
    if abs(cv2.contourArea(pts.astype(np.float32))) < 50.0:
        return "is too small or its points are nearly in a line"
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        for j in range(i + 1, n):
            # Skip edges that share a vertex (adjacent / wrap-around).
            if j == i or (j + 1) % n == i or (i + 1) % n == j:
                continue
            c, d = pts[j], pts[(j + 1) % n]
            if _segments_cross(a, b, c, d):
                return "has edges that cross themselves"
    return None


# Characters Excel forbids in a worksheet name.
_BAD_SHEET_CHARS = set('[]:*?/\\')


def safe_excel_sheet_name(name: str, used: set) -> str:
    """A valid, unique (case-insensitive) Excel sheet name ≤ 31 chars."""
    cleaned = "".join("-" if c in _BAD_SHEET_CHARS else c for c in str(name)).strip()
    cleaned = cleaned.strip("'")            # Excel forbids leading/trailing apostrophes
    cleaned = cleaned[:31] or "Sheet"
    candidate, i = cleaned, 1
    while candidate.lower() in used:
        suffix = f"~{i}"
        candidate = cleaned[:31 - len(suffix)] + suffix
        i += 1
    used.add(candidate.lower())
    return candidate


def excel_safe_text(value):
    """
    Neutralise spreadsheet formula injection: a cell whose text starts with
    = + - @ (or a control char) is treated as a formula by Excel. Prefix such
    user-controlled strings with an apostrophe so they stay literal text.
    """
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r", "\n"):
        return "'" + value
    return value


def guard_excel_frame(df: "pd.DataFrame") -> "pd.DataFrame":
    """Return a copy of df with formula-injection guards on its text columns.

    Guards every non-numeric/bool/datetime column (object, str, category, mixed),
    since pandas may report string columns as dtype 'str' rather than 'object';
    excel_safe_text leaves any stray non-string values unchanged.
    """
    from pandas.api.types import is_numeric_dtype, is_bool_dtype, is_datetime64_any_dtype
    safe = df.copy()
    for col in safe.columns:
        c = safe[col]
        if is_numeric_dtype(c) or is_bool_dtype(c) or is_datetime64_any_dtype(c):
            continue
        safe[col] = c.map(excel_safe_text)
    # Column headers are cells too — a header like "=BAD Time (s)" (built from a
    # user-supplied zone name) would otherwise execute as a formula.
    safe.columns = [excel_safe_text(str(col)) for col in safe.columns]
    return safe


def zones_for_point(point: Tuple[float, float],
                    zones: List[Tuple[str, np.ndarray]]) -> List[str]:
    """Return every zone containing point, preserving the configured zone order."""
    return [name for name, polygon in zones if point_in_polygon(point, polygon)]


def video_duration_seconds(path: str) -> Optional[float]:
    """Return a video's length in seconds, or None if it can't be determined."""
    try:
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            return None
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        cap.release()
        if fps > 0 and frames > 0:
            return frames / fps
        return None
    except Exception:
        return None


def video_frame_size(path: str) -> Optional[Tuple[int, int]]:
    """Return encoded (width, height), or None when the video cannot be inspected."""
    try:
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            return None
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        cap.release()
        return (width, height) if width > 0 and height > 0 else None
    except Exception:
        return None


def video_size_mismatches(paths: List[str]) -> List[Tuple[str, Tuple[int, int]]]:
    """Videos whose pixel dimensions differ from the first inspectable video."""
    sizes = [(path, video_frame_size(path)) for path in paths]
    reference = next((size for _, size in sizes if size is not None), None)
    if reference is None:
        return []
    return [(path, size) for path, size in sizes
            if size is not None and size != reference]


def videos_window_issues(paths: List[str], trial_start: float, trial_end: float
                         ) -> Tuple[List[Tuple[str, float]], List[Tuple[str, float]]]:
    """
    Compare each video's length to the requested [start, end] analysis window.
    Returns (empties, clamped):
      empties  - (path, duration) where the window starts at/after the video end,
                 so the run would produce no data.
      clamped  - (path, duration) where the window ends past the video, so analysis
                 will simply stop at the video's own end.
    Videos whose duration can't be determined are skipped (no false warning).
    """
    empties, clamped = [], []
    for p in paths:
        dur = video_duration_seconds(p)
        if dur is None or dur <= 0:
            continue
        if trial_start > 0 and trial_start >= dur:
            empties.append((p, dur))
        elif trial_end > 0 and trial_end > dur:
            clamped.append((p, dur))
    return empties, clamped


def polygon_inside_polygon(inner: np.ndarray, outer: np.ndarray) -> Tuple[bool, float]:
    """
    Check if inner polygon is inside outer polygon.
    Returns (fully_inside, percentage_inside).
    """
    if inner is None or outer is None or len(inner) < 3 or len(outer) < 3:
        return False, 0.0

    # Check what percentage of inner polygon points are inside outer polygon
    points_inside = 0
    for point in inner:
        if point_in_polygon(tuple(point), outer):
            points_inside += 1

    percentage = points_inside / len(inner) if len(inner) > 0 else 0.0
    fully_inside = points_inside == len(inner)

    return fully_inside, percentage


def polygons_overlap(poly1: np.ndarray, poly2: np.ndarray) -> Tuple[bool, float]:
    """
    Check if two polygons overlap.
    Returns (overlaps, overlap_percentage).
    Uses contour intersection to calculate overlap area.
    """
    if poly1 is None or poly2 is None or len(poly1) < 3 or len(poly2) < 3:
        return False, 0.0

    try:
        # Create masks for both polygons
        # Find bounding box that contains both polygons
        all_points = np.vstack([poly1, poly2])
        x_min, y_min = np.min(all_points, axis=0).astype(int)
        x_max, y_max = np.max(all_points, axis=0).astype(int)

        # Add padding
        x_min, y_min = max(0, x_min - 10), max(0, y_min - 10)
        x_max, y_max = x_max + 10, y_max + 10

        # Create offset polygons
        poly1_offset = (poly1 - [x_min, y_min]).astype(np.int32)
        poly2_offset = (poly2 - [x_min, y_min]).astype(np.int32)

        # Create masks
        h, w = y_max - y_min, x_max - x_min
        mask1 = np.zeros((h, w), dtype=np.uint8)
        mask2 = np.zeros((h, w), dtype=np.uint8)

        cv2.fillPoly(mask1, [poly1_offset], 255)
        cv2.fillPoly(mask2, [poly2_offset], 255)

        # Calculate intersection
        intersection = cv2.bitwise_and(mask1, mask2)
        intersection_area = np.sum(intersection > 0)

        # Calculate areas
        area1 = np.sum(mask1 > 0)
        area2 = np.sum(mask2 > 0)
        smaller_area = min(area1, area2)

        if smaller_area == 0:
            return False, 0.0

        overlap_percentage = intersection_area / smaller_area
        overlaps = intersection_area > 0

        return overlaps, overlap_percentage

    except Exception:
        return False, 0.0


# -----------------------------
# Tracking Settings
# -----------------------------

@dataclass
class TrackingSettings:
    # Tracking method: "refsub" — reference (static-background) subtraction: build
    # an image of the EMPTY tank and detect whatever differs from it. Detects a fish
    # whether it moves or not (like EthoVision); most robust for a fixed camera. This
    # is the only pipeline; the field is retained because it's recorded in exports.
    tracking_method: str = "refsub"
    min_area: int = 100          # min detected-blob area (px²) accepted as the fish
    max_area: int = 200000       # max detected-blob area (px²)
    refsub_threshold: int = 0    # reference-subtraction sensitivity (gray diff 0-255 from the empty
                                 # tank). 0 = auto-calibrate to the video; >0 = manual override.
    refsub_samples: int = 80     # frames sampled across the video to build the static background
    fish_polarity: str = "dark"  # fish is "dark" (darker than the tank) | "light" | "both".
                                 # "dark" ignores bright water reflections/glints (the usual setup).
    fish_bbox: Optional[list] = None    # [x, y, w, h] in image px — traced fish box (from Select Fish);
                                        # seeds the starting position and sets the detection size
    blur_size: int = 5           # Gaussian blur kernel (px) before detection
    morph_size: int = 5          # morphology kernel (px) for mask cleanup
    processing_speed: str = "fast"  # "normal" = 1x preview | "fast" = smooth ~6x sped-up preview
    trial_start: float = 0.0     # analysis window start in seconds (0 = from the beginning)
    trial_end: float = 0.0       # analysis window end in seconds (0 = until the end)

    # Calibration (optional): one or more "scale lines" — straight segments drawn
    # across something of known real length (the tank's height, a ruler). Each is
    # {"p1": [x, y], "p2": [x, y], "length": float, "unit": str} in image pixels.
    # They are averaged into one uniform real-units-per-pixel scale (see
    # compute_scale). Works for any tank shape under a roughly perpendicular view.
    calibration_unit: str = "cm"     # mm | cm | m | in (reporting unit)
    scale_lines: list = field(default_factory=list)

    # Measurement toggles: which metrics to compute and export.
    measure_time_per_zone: bool = True
    measure_zone_entries: bool = True
    measure_distance: bool = False   # requires calibration
    measure_speed: bool = False      # requires calibration

    movement_threshold_px: float = 2.0  # ignore centroid jitter up to this many px

    def to_dict(self) -> Dict:
        return asdict(self)


# -----------------------------
# Video Tracker Engine
# -----------------------------

class VideoTracker:
    # Preset zone colors — BGR of the SAME palette the setup canvas uses
    # (ZoneDrawingCanvas._get_zone_color), in the same order, so a zone shows the
    # identical colour in the live preview as it did when you drew it.
    ZONE_COLORS = [
        (60, 76, 231),    # #e74c3c red
        (219, 152, 52),   # #3498db blue
        (113, 204, 46),   # #2ecc71 green
        (182, 89, 155),   # #9b59b6 purple
        (18, 156, 243),   # #f39c12 orange
        (156, 188, 26),   # #1abc9c teal
        (99, 30, 233),    # #e91e63 pink
        (212, 188, 0),    # #00bcd4 cyan
    ]
    # Arena outline colour (BGR) = the setup canvas's cyan (#00ffff).
    ARENA_COLOR = (255, 255, 0)

    # Default color when not in any zone
    DEFAULT_COLOR = (128, 128, 128)  # Gray

    def __init__(self, settings: TrackingSettings):
        self.settings = settings
        self.cancelled = False
        self.show_preview = True
        # Tracking state for persistent tracking
        self.last_position = None  # Last known position (cx, cy)
        self.max_jump_distance = 150  # Max pixels the fish can move between frames
        self.frames_undetected = 0  # Frames since the last usable centroid
        self.max_frames_undetected = 10  # Then reset only the contour-association anchor

    def cancel(self):
        self.cancelled = True

    def get_zone_color(self, zone_index: int) -> Tuple[int, int, int]:
        """Get color for a zone by index."""
        return self.ZONE_COLORS[zone_index % len(self.ZONE_COLORS)]

    def _distance(self, p1, p2):
        """Calculate Euclidean distance between two points."""
        return ((p1[0] - p2[0])**2 + (p1[1] - p2[1])**2)**0.5

    def _pick_fish_blob(self, contours, last_pos):
        """Pick the fish for reference subtraction: the LARGEST blob in the arena
        above a small noise floor (a single fish is the biggest moving object), with a
        preference for one near the last position for continuity. Deliberately does NOT
        apply the circularity/aspect 'streak' filters or a large min-area gate — those
        reject small or elongated/darting fish, which is how the track froze."""
        floor = 8                                   # px²: ignore sub-fish noise specks
        cap_area = float(self.settings.max_area)    # ignore huge spurious blobs
        cands = []
        for c in contours:
            area = cv2.contourArea(c)
            if area < floor or area > cap_area:
                continue
            m = cv2.moments(c)
            if m["m00"] < 1e-6:
                continue
            cands.append((c, int(m["m10"] / m["m00"]), int(m["m01"] / m["m00"]), area))
        if not cands:
            return None, None, None
        if last_pos is not None:
            near = [k for k in cands
                    if self._distance((k[1], k[2]), last_pos) <= self.max_jump_distance]
            if near:
                # Stay on the SAME target: pick the blob nearest the last position, not
                # the biggest. With a fish + its glass reflection (two similar blobs),
                # "biggest" flips between them frame to frame; "nearest" stays locked.
                best = min(near, key=lambda k: self._distance((k[1], k[2]), last_pos))
                return best[0], best[1], best[2]
        # No usable anchor (first frame / after a long loss): take the largest blob.
        best = max(cands, key=lambda k: k[3])
        return best[0], best[1], best[2]

    def _reference_background(self, video_path, total_frames, progress_cb=None):
        """Build the EMPTY-tank reference image for reference subtraction and an
        auto-calibrated detection threshold.

        The background is the per-pixel median (grayscale, blurred) over frames sampled
        across the video — the fish moves, so at each pixel the median is the empty
        tank. A fish that holds still still differs from it and stays detected.

        The threshold is derived from how much the empty tank naturally varies (sensor
        noise / compression): we set it just above that noise floor, so a LOW-CONTRAST
        fish is still detected instead of being thresholded away by a fixed cutoff.

        Returns ``(background_uint8, auto_threshold_int)`` or ``(None, None)``.
        """
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return None, None
        try:
            total = total_frames or int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            n = max(8, int(getattr(self.settings, "refsub_samples", 80) or 80))
            bsz = self.settings.blur_size
            frames = []
            if total and total > 0:
                step = max(1, total // n)
                idxs = list(range(0, total, step))[:n]
                for k, fi in enumerate(idxs):
                    cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
                    ok, fr = cap.read()
                    if ok and fr is not None:
                        if bsz > 1:
                            fr = cv2.GaussianBlur(fr, (bsz, bsz), 0)
                        frames.append(cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY))
                    if progress_cb is not None:
                        progress_cb(k + 1, len(idxs))
            else:  # unknown length: sample the first n frames sequentially
                while len(frames) < n:
                    ok, fr = cap.read()
                    if not ok or fr is None:
                        break
                    if bsz > 1:
                        fr = cv2.GaussianBlur(fr, (bsz, bsz), 0)
                    frames.append(cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY))
            if not frames:
                return None, None
            stack = np.stack(frames, axis=0).astype(np.float32)
            bg = np.median(stack, axis=0)
            # Robust background-noise estimate: median + MAD of |sample - bg| over all
            # samples/pixels (the fish is a tiny fraction, so it barely affects these).
            absdiff = np.abs(stack - bg)
            med = float(np.median(absdiff))
            mad = float(np.median(np.abs(absdiff - med)))
            thr = int(round(med + 4.0 * 1.4826 * mad + 3))
            thr = max(6, min(60, thr))     # sane floor/ceiling
            return bg.astype(np.uint8), thr
        finally:
            cap.release()

    def track(self, video_path: str, arena_poly: np.ndarray, zones: List[Tuple[str, np.ndarray]],
              progress_callback=None, preview_callback=None) -> Tuple[Dict, pd.DataFrame]:
        """
        Track video and return summary + per-frame data.
        Uses persistent tracking to follow the same fish and not jump to other movements.
        """
        self.cancelled = False
        self.last_position = None
        # Seed the starting position from the Select-Fish box so detection locks onto
        # the fish the user marked rather than a similar blob (e.g. its glass
        # reflection); persistence then keeps it on that target.
        _bb = getattr(self.settings, "fish_bbox", None)
        if _bb:
            self.last_position = (_bb[0] + _bb[2] / 2.0, _bb[1] + _bb[3] / 2.0)
        self.frames_undetected = 0

        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            raise RuntimeError(f"Could not open video: {video_path}")

        # Build zone color mapping
        zone_colors = {name: self.get_zone_color(i) for i, (name, _) in enumerate(zones)}

        fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        dt = 1.0 / fps if fps > 0 else 1.0 / 30.0

        # Analysis window. The whole video is ALWAYS processed and tracked (so the
        # background model warms up and gap-bridging can use detections on both
        # sides of the window edges); only the frames inside [start_frame, end_frame)
        # are kept and exported. The trimming happens offline in PASS 2 — see the
        # window slice below. (start 0 = from the beginning, end 0 = until the end.)
        trial_start = max(0.0, getattr(self.settings, "trial_start", 0.0) or 0.0)
        trial_end = max(0.0, getattr(self.settings, "trial_end", 0.0) or 0.0)
        start_frame = int(round(trial_start * fps)) if trial_start > 0 else 0
        end_frame = int(round(trial_end * fps)) if trial_end > 0 else None
        # Progress is over the whole video (we scan all of it).
        effective_total = total_frames

        # Tracking method (recorded in exports). Reference subtraction is the only
        # pipeline, so this is always "refsub".
        method = getattr(self.settings, "tracking_method", "refsub")

        # Reference subtraction: build the empty-tank background up front (a quick
        # pre-pass that samples frames across the video). Detection is then "differs
        # from the empty tank", which catches a still fish too.
        bg_ref_mean = None   # arena-average background brightness (for exposure correction)
        manual_thr = int(getattr(self.settings, "refsub_threshold", 0) or 0)
        ref_thr = manual_thr if manual_thr > 0 else 22
        polarity = str(getattr(self.settings, "fish_polarity", "dark")).lower()

        def _bg_progress(done, total):
            if progress_callback and total:
                progress_callback(min(done / total, 1.0), done, total)
        ref_bg, auto_thr = self._reference_background(video_path, total_frames, _bg_progress)
        if ref_bg is None:
            raise RuntimeError("Could not build the reference background for this video.")
        # Manual sensitivity overrides; otherwise use the auto-calibrated value.
        if manual_thr <= 0 and auto_thr is not None:
            ref_thr = auto_thr

        # Uniform image scale (real units per pixel) from the scale lines, or
        # None for zone-only runs (no distance/speed).
        scale_info = compute_scale(self.settings)

        # PASS 1 collects only RAW detections + real timestamps per frame. All
        # position inference (gap-filling) happens offline in PASS 2 below, so no
        # forward-guessed position is ever written to the record.
        frame_ids = []
        ts_ms = []
        det_positions = []       # raw centroid (cx, cy) or None, per recorded frame
        preview_last_zone = None

        arena_mask = None
        # Use larger morphology kernel for better noise removal
        morph_size = self.settings.morph_size
        morph_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (morph_size, morph_size))

        # Process from the first frame (no seek): the whole video is scanned so the
        # background model warms up; the window is applied to the OUTPUT in PASS 2.
        frame_idx = -1

        # Speed mode. "normal" paces the preview to real time so tracking is
        # watchable at 1x; "fast" runs as quickly as the machine allows and only
        # refreshes the preview occasionally so the UI never throttles processing.
        speed_mode = str(getattr(self.settings, "processing_speed", "fast")).lower()
        realtime = (speed_mode == "normal")
        # Min seconds between preview EMISSIONS. Fast mode still analyzes EVERY
        # frame flat-out; this only bounds how often the newest frame is handed to
        # the UI, which coalesces + renders it at ~30fps with a reused PhotoImage.
        # ~30fps of steady, cheap redraws reads as smooth fast motion rather than a
        # chunky slideshow. (The UI drops any extra emissions, so the higher rate
        # doesn't pile up work.)
        preview_interval = 0.0 if realtime else 0.033
        last_preview_time = 0.0
        play_anchor = None   # (wall_time, frame_idx) reference for paced playback
        # Playback speed while the preview is visible. Normal = 1x (real time).
        # Fast = a CAPPED fast-forward, NOT "flat out": running unthrottled
        # saturates the CPU, starves the Tk thread, and the preview only redraws
        # every few seconds (each jump 20-30s of video). Capping to ~6x keeps a
        # 6-min video ~1 min while the sleeps hand the UI enough time to render a
        # smooth ~30fps sped-up video. If analysis can't reach 6x it just runs as
        # fast as it can (the cap never slows a slow machine).
        FAST_PREVIEW_SPEEDUP = 6.0
        preview_speedup = 1.0 if realtime else FAST_PREVIEW_SPEEDUP

        try:
            while not self.cancelled:
                ok, frame = cap.read()
                if not ok or frame is None:
                    break
                frame_idx += 1
                # Query after decoding so this timestamp belongs to the frame that
                # was actually returned. Invalid/non-monotonic backends fall back to
                # nominal FPS in resolve_times().
                ts_current = cap.get(cv2.CAP_PROP_POS_MSEC)

                h, w = frame.shape[:2]
                if arena_mask is None:
                    arena_mask = np.zeros((h, w), dtype=np.uint8)
                    cv2.fillPoly(arena_mask, [arena_poly.astype(np.int32)], 255)
                    # Set max jump distance based on frame size
                    self.max_jump_distance = max(w, h) // 5

                # Apply Gaussian blur to reduce noise
                blur_size = self.settings.blur_size
                if blur_size > 1:
                    frame_blurred = cv2.GaussianBlur(frame, (blur_size, blur_size), 0)
                else:
                    frame_blurred = frame

                # Reference subtraction: the fish is wherever the frame differs
                # from the empty-tank background — true whether it moves or not.
                # Threshold is auto-calibrated (low contrast still detected) and
                # morphology is gentle (open x1) so a SMALL fish survives.
                gray = cv2.cvtColor(frame_blurred, cv2.COLOR_BGR2GRAY)
                # Correct global brightness/exposure drift first: a backlit tank
                # flickers, and a frame that's uniformly a bit darker/brighter than
                # the background would otherwise flood the whole mask. Shift this
                # frame so its arena-average brightness matches the background's.
                if bg_ref_mean is None:
                    bg_ref_mean = cv2.mean(ref_bg, mask=arena_mask)[0]
                shift = bg_ref_mean - cv2.mean(gray, mask=arena_mask)[0]
                if abs(shift) >= 1.0:
                    gray = np.clip(gray.astype(np.int16) + int(round(shift)),
                                   0, 255).astype(np.uint8)
                # Polarity matters on reflective water: a "dark" fish is found by
                # what's DARKER than the empty tank, which ignores bright glints /
                # surface reflections (the usual noise). "light" is the opposite;
                # "both" uses the absolute difference.
                if polarity == "dark":
                    diff = cv2.subtract(ref_bg, gray)      # >0 only where the frame is darker
                elif polarity == "light":
                    diff = cv2.subtract(gray, ref_bg)      # >0 only where brighter
                else:
                    diff = cv2.absdiff(gray, ref_bg)
                _, fg = cv2.threshold(diff, ref_thr, 255, cv2.THRESH_BINARY)
                fg = cv2.bitwise_and(fg, arena_mask)
                fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, morph_kernel, iterations=1)
                fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, morph_kernel, iterations=2)
                cnts, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                # Largest blob in the arena (a single fish is the biggest mover) —
                # no strict shape/min-area gate that would drop a small/elongated fish.
                best, cx, cy = self._pick_fish_blob(cnts, self.last_position)
                detected = best is not None

                if detected:
                    self.frames_undetected = 0
                    self.last_position = (cx, cy)
                else:
                    cx = cy = None
                    self.frames_undetected += 1
                    if self.frames_undetected > self.max_frames_undetected:
                        self.last_position = None

                frame_ids.append(frame_idx)
                ts_ms.append(ts_current)
                det_positions.append((cx, cy) if detected else None)

                # Progress callback (over the whole video being scanned)
                if progress_callback and effective_total > 0:
                    done = frame_idx + 1
                    progress = min(done / effective_total, 1.0)
                    progress_callback(progress, done, effective_total)

                # Pace the scan while the preview is visible so it plays as a
                # smooth video — Normal at 1x, Fast at the capped speed-up. The
                # sleeps also yield the CPU so the UI redraws steadily instead of
                # being starved. With the preview hidden there's nothing to watch,
                # so we skip pacing and run flat out (max throughput for batches).
                if self.show_preview and preview_speedup > 0:
                    if play_anchor is None:
                        play_anchor = (time.time(), frame_idx)
                    target = play_anchor[0] + (frame_idx - play_anchor[1]) * dt / preview_speedup
                    delay = target - time.time()
                    if delay > 0:
                        time.sleep(min(delay, 0.25))

                # Live preview (ONLINE: shows the raw detection as the video plays).
                current_time = time.time()
                time_ok = (current_time - last_preview_time) >= preview_interval
                if preview_callback and self.show_preview and time_ok:
                    last_preview_time = current_time
                    pv_zone = None
                    if detected:
                        memberships = zones_for_point((cx, cy), zones)
                        pv_zone = memberships[0] if memberships else None
                        # A real detection outside all zones must display as outside.
                        preview_last_zone = pv_zone
                    else:
                        # During a missed detection only, retain the prior display
                        # zone until the offline interpolation can resolve the gap.
                        pv_zone = preview_last_zone
                    if pv_zone is not None:
                        tracker_color = zone_colors.get(pv_zone, self.DEFAULT_COLOR)
                        zone_idx = next((i for i, (n, _) in enumerate(zones) if n == pv_zone), -1)
                    else:
                        tracker_color = self.DEFAULT_COLOR
                        zone_idx = -1
                    # Elapsed time WITHIN THE VIDEO for this frame: use the decoder's
                    # real presentation timestamp when valid (correct on VFR media),
                    # else fall back to nominal frame/fps.
                    video_time_s = (ts_current / 1000.0
                                    if ts_current is not None and ts_current > 0
                                    else (frame_idx / fps if fps > 0 else 0.0))
                    try:
                        preview_callback(
                            frame=frame.copy(),
                            fg_mask=fg.copy(),
                            centroid=(cx, cy) if detected else None,
                            zone_name=pv_zone,
                            zone_index=zone_idx,
                            tracker_color=tracker_color,
                            arena_poly=arena_poly,
                            zones=zones,
                            zone_colors=zone_colors,
                            frame_idx=frame_idx,
                            fps=fps,
                            video_time_s=video_time_s,
                        )
                    except Exception:
                        # Preview failed, continue tracking without preview
                        pass
        finally:
            cap.release()

        if self.cancelled:
            return None, None

        # ---- PASS 2 (OFFLINE): bridge gaps using past AND future, then derive
        # zones / time / kinematics from the filled track. Because gaps are filled
        # by interpolating between the detections on either side (never by guessing
        # forward), a fish seen before and after a dropout stays present the whole
        # time, with no teleport-smear. ----
        # Bridge over the WHOLE scanned video first, so detections on either side of
        # the analysis-window edges inform the reconstruction (and a fish that just
        # sits still stays positioned via interpolation/hold).
        full_times, used_real_time = resolve_times(frame_ids, ts_ms, fps)
        full_filled = bridge_track_gaps(det_positions, full_times)
        full_durations = frame_sample_durations(full_times, dt)
        frames_scanned = len(frame_ids)

        # Now apply the analysis window to the OUTPUT: keep only the frames inside
        # [start_frame, end_frame). Everything below derives from these slices, so
        # the export reflects exactly the requested seconds — the full-video scan
        # stays "behind the scenes". A blank window keeps every frame.
        hi_frame = end_frame if end_frame is not None else (
            (frame_ids[-1] + 1) if frame_ids else 0)
        win = [k for k, fid in enumerate(frame_ids) if start_frame <= fid < hi_frame]
        frame_ids = [frame_ids[k] for k in win]
        times = [full_times[k] for k in win]
        filled = [full_filled[k] for k in win]
        frame_durations = [full_durations[k] for k in win]

        # With a uniform scale, interpolating in pixels and in real units are
        # identical (both linear), so the calibrated track is just the filled
        # pixel track multiplied by the scale — no perspective unwarp needed.
        if scale_info is not None:
            upp = scale_info["units_per_px"]
            physical_filled = [
                (x * upp if x is not None else None,
                 y * upp if y is not None else None, st)
                for x, y, st in filled]
        else:
            physical_filled = None

        duration_s = sum(frame_durations)
        if scale_info is not None:
            upp = scale_info["units_per_px"]
            kinematics, frame_speeds = calculate_kinematics(
                filled, times, (upp, upp),
                float(getattr(self.settings, "movement_threshold_px", 2.0) or 0.0),
                movement_reference=filled)
        else:
            kinematics, frame_speeds = {}, [None] * len(filled)

        seconds_per_zone = {name: 0.0 for name, _ in zones}
        entries_per_zone = {name: 0 for name, _ in zones}
        last_zones = set()
        status_counts = {"detected": 0, "interpolated": 0, "held": 0, "unresolved": 0}

        records = []
        n = len(frame_ids)
        for k in range(n):
            fx, fy, status = filled[k]
            status_counts[status] = status_counts.get(status, 0) + 1
            t_s = times[k]
            interval = frame_durations[k]

            # Zone comes from the reconstructed position, but only while that point
            # is inside the arena. Raw detections are arena-masked, yet a straight
            # interpolation across a concave arena can pass briefly outside it;
            # excluding those frames keeps zone time/entries consistent with the
            # arena-masked heatmap.
            zone_names = []
            if fx is not None and fy is not None and point_in_polygon((fx, fy), arena_poly):
                zone_names = zones_for_point((fx, fy), zones)
            current_zones = set(zone_names)
            for zone_name in zone_names:
                if zone_name not in last_zones:
                    entries_per_zone[zone_name] += 1
                seconds_per_zone[zone_name] += interval
            last_zones = current_zones

            inst_speed = frame_speeds[k] if k < len(frame_speeds) else None

            rec = {
                "frame": frame_ids[k],
                "time_s": round(t_s, 3),
                "sample_duration_s": round(interval, 6),
                "x": round(fx, 1) if fx is not None else "",
                "y": round(fy, 1) if fy is not None else "",
                "zone": "; ".join(zone_names),
                "status": status,
            }
            if physical_filled is not None:
                wx, wy, _ = physical_filled[k]
                rec["x_real"] = round(wx, 4) if wx is not None else ""
                rec["y_real"] = round(wy, 4) if wy is not None else ""
            if physical_filled is not None and self.settings.measure_speed:
                rec["speed"] = round(inst_speed, 3) if inst_speed is not None else ""
            records.append(rec)

        frame_columns = ["frame", "time_s", "sample_duration_s",
                         "x", "y", "zone", "status"]
        if physical_filled is not None:
            frame_columns.extend(["x_real", "y_real"])
        if physical_filled is not None and self.settings.measure_speed:
            frame_columns.append("speed")
        # Preserve a stable schema even for an empty analysis window. This keeps
        # heatmap/export code from failing on df["x"] / df["y"].
        df = pd.DataFrame.from_records(records, columns=frame_columns)
        processed = len(records)
        summary = {
            "video": os.path.basename(video_path),
            "fps": fps,
            "total_frames": processed,
            # Whole-video frames actually scanned (diagnostic; not exported). When a
            # trial window is set this exceeds total_frames — the rest was processed
            # "behind the scenes" to warm the model and bridge the window edges.
            "frames_scanned": frames_scanned,
            "duration_s": round(duration_s, 3),
            "analyzed_from_s": round(times[0], 3) if times else round(start_frame * dt, 3),
            "analyzed_to_s": round((times[-1] + frame_durations[-1]), 3)
                              if times else round(start_frame * dt, 3),
            "seconds_per_zone": {k: round(v, 2) for k, v in seconds_per_zone.items()},
            "entries_per_zone": entries_per_zone,
            "tracking_quality": {
                "detected": status_counts["detected"],
                "interpolated": status_counts["interpolated"],
                "held": status_counts["held"],
                "unresolved": status_counts["unresolved"],
                "time_base": "measured" if used_real_time else "nominal_fps",
                "reconstruction": "straight_line_between_detections",
                "tracking_method": method,
            },
        }

        # Calibration & kinematics (only when scale lines define a scale).
        if scale_info is not None:
            summary["calibration"] = {
                "unit": scale_info["unit"],
                "units_per_px": round(scale_info["units_per_px"], 6),
                "scale_lines": scale_info["n_lines"],
                "scale_agreement_pct": round(scale_info["spread_pct"], 2),
                "model": "scale_lines_uniform",
            }
            summary["kinematics"] = {
                key: value if isinstance(value, bool) else round(value, 3)
                for key, value in kinematics.items()
            }

        return summary, df


# -----------------------------
# Zone Drawing Canvas
# -----------------------------

class ZoneDrawingCanvas(tk.Canvas):
    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)
        self.image = None
        self.photo = None
        self.scale = 1.0
        self.offset_x = 0
        self.offset_y = 0

        # Drawing state
        self.current_polygon = []
        self.polygons = []  # List of (name, points, color)
        self.arena_polygon = None
        self.drawing_mode = None  # "arena" | "zone" | "fish"
        self.current_zone_name = ""
        self.selected_zone = None  # index of the zone highlighted for editing
        # Calibration: the real length of each arena side (aligned to its edges,
        # "" if unknown). Each side with a length is a scale reference; they are
        # averaged into one uniform scale. scale_unit is the shared reporting unit.
        self.arena_side_lengths = []
        self.scale_unit = "cm"
        self.fish_bbox = None   # [x, y, w, h] of the last traced fish (seeds the start position)
        self.show_guides = False   # optional alignment overlay: arena center "+" + fine grid

        # Bind events
        self.bind("<Button-1>", self._on_left_click)
        self.bind("<Button-3>", self._on_right_click)
        self.bind("<Double-Button-1>", self._on_double_click)
        self.bind("<Motion>", self._on_motion)
        self.bind("<Configure>", self._on_resize)
        self.bind("<Return>", self._on_enter_key)
        self.bind("<Escape>", self._on_escape_key)

        self.on_polygon_complete = None
        self.on_drawing_status_change = None
        self.on_arena_complete = None
        self.on_invalid_polygon = None   # (kind, reason) -> warn the user
        self.on_fish_selected = None     # (area_px2) -> set fish-size bounds

    def set_image(self, cv_image: np.ndarray):
        """Set the background image (BGR format from OpenCV)."""
        self.cv_image = cv_image
        self._update_display()

    def _update_display(self):
        if not hasattr(self, 'cv_image') or self.cv_image is None:
            return

        canvas_w = self.winfo_width()
        canvas_h = self.winfo_height()
        if canvas_w <= 1 or canvas_h <= 1:
            return

        img_h, img_w = self.cv_image.shape[:2]

        # Calculate scale to fit
        scale_w = canvas_w / img_w
        scale_h = canvas_h / img_h
        self.scale = min(scale_w, scale_h)

        new_w = int(img_w * self.scale)
        new_h = int(img_h * self.scale)

        self.offset_x = (canvas_w - new_w) // 2
        self.offset_y = (canvas_h - new_h) // 2

        # Convert and resize
        rgb = cv2.cvtColor(self.cv_image, cv2.COLOR_BGR2RGB)
        resized = cv2.resize(rgb, (new_w, new_h), interpolation=cv2.INTER_AREA)
        self.image = Image.fromarray(resized)
        self.photo = ImageTk.PhotoImage(self.image)

        self._redraw()

    def _redraw(self):
        self.delete("all")

        # Crosshair cursor whenever a drawing mode is active, so it's visually
        # obvious the app is waiting for you to click on the frame.
        try:
            self.config(cursor="crosshair" if self.drawing_mode in ("arena", "zone", "fish") else "")
        except tk.TclError:
            pass

        if self.photo:
            self.create_image(self.offset_x, self.offset_y, anchor="nw", image=self.photo)

        # Optional alignment guides (under the arena/zone overlays).
        if self.show_guides:
            self._draw_guides()

        # Arena: the tracking area, drawn as a free-form polygon (any shape). Each
        # side is numbered; sides with a real length show it (those are the scale).
        if self.arena_polygon is not None and len(self.arena_polygon) >= 3:
            self._draw_polygon(self.arena_polygon, "#00ffff", "", filled=False)
            for i, (a, b) in enumerate(self._arena_sides()):
                sa, sb = self._to_screen(a), self._to_screen(b)
                mid = ((sa[0] + sb[0]) // 2, (sa[1] + sb[1]) // 2)
                v = self.arena_side_lengths[i] if i < len(self.arena_side_lengths) else ""
                if v:
                    self._dim_chip(mid[0], mid[1], f"{i+1}: {v} {self.scale_unit}", "#ffd400")
                else:
                    self._dim_chip(mid[0], mid[1], str(i + 1), "#00ffff")

        for i, (name, points, color) in enumerate(self.polygons):
            self._draw_polygon(points, color, name, filled=True)
            # Highlight the zone currently selected for editing.
            if i == self.selected_zone and len(points) >= 3:
                screen = [self._to_screen(p) for p in points]
                flat = [c for pt in screen for c in pt]
                self.create_polygon(*flat, fill="", outline="#ffffff", width=4, dash=(6, 4))

        # The traced fish box (orange) — confirms the size was set and seeds the
        # starting position. Drawn only when not actively tracing.
        if self.fish_bbox is not None and self.drawing_mode != "fish":
            bx, by, bw, bh = self.fish_bbox
            a = self._to_screen((bx, by))
            b = self._to_screen((bx + bw, by + bh))
            self.create_rectangle(a[0], a[1], b[0], b[1], outline="#ffa500", width=2, dash=(4, 3))
            self._dim_chip(a[0] + (b[0] - a[0]) // 2, a[1] - 9, "Fish", "#ffa500")

        # In-progress drawing (arena, zone, or fish). The prompt appears the MOMENT
        # the mode starts — before any click — so it's obvious the app is waiting
        # for you to draw on the frame (clicking the button alone showed nothing).
        if self.drawing_mode in ("arena", "zone", "fish"):
            screen_pts = [self._to_screen(p) for p in self.current_polygon]
            line_color = {"arena": "#00ffff", "fish": "#ffa500"}.get(
                self.drawing_mode, COLORS["accent"])

            for i, pt in enumerate(screen_pts):
                color = "#00ff00" if (i == 0 and len(screen_pts) >= 3) else line_color
                size = 8 if i == 0 else 5
                self.create_oval(pt[0]-size, pt[1]-size, pt[0]+size, pt[1]+size,
                               fill=color, outline="white", width=2)

            if len(screen_pts) >= 2:
                for i in range(len(screen_pts)):
                    p1 = screen_pts[i]
                    p2 = screen_pts[(i + 1) % len(screen_pts)]
                    if i == len(screen_pts) - 1 and len(screen_pts) >= 3:
                        self.create_line(p1[0], p1[1], p2[0], p2[1],
                                       fill=line_color, width=2, dash=(4, 4))
                    else:
                        self.create_line(p1[0], p1[1], p2[0], p2[1], fill=line_color, width=2)

            what = {"arena": "Draw the ARENA", "fish": "Trace the FISH"}.get(
                self.drawing_mode, "Draw the ZONE")
            target = {"arena": "tank/area", "fish": "fish"}.get(self.drawing_mode, "zone area")
            if not screen_pts:
                instruction = f"{what}: click around the {target} on the frame  |  ESC to cancel"
            else:
                instruction = (f"{what}: {len(screen_pts)} point(s) | click the green dot, "
                               "double-click, or press ENTER to finish  |  ESC to cancel")
            # Always-visible dark banner (legible over any frame).
            self.create_rectangle(5, 5, 670, 34, fill="#101418", outline=line_color, width=3)
            self.create_text(14, 19, text=instruction, fill="#ffffff", anchor="w",
                             font=("Arial", 11, "bold"))

            # Until the first click, an unmissable hint sits over the image itself,
            # so the user knows to click on the frame rather than re-clicking the button.
            if not screen_pts and hasattr(self, "cv_image") and self.cv_image is not None:
                ih, iw = self.cv_image.shape[:2]
                cx = self.offset_x + int(iw * self.scale / 2)
                cy = self.offset_y + int(ih * self.scale / 2)
                msg = ("Click around the FISH on the image to trace it"
                       if self.drawing_mode == "fish"
                       else f"Click on the image to draw the {target}")
                half = 9 + len(msg) * 5
                self.create_rectangle(cx - half, cy - 20, cx + half, cy + 20,
                                      fill="#101418", outline=line_color, width=3)
                self.create_text(cx, cy, text=msg, fill="#ffffff", font=("Arial", 13, "bold"))

    def set_show_guides(self, on: bool):
        """Toggle the optional alignment overlay (arena-center cross + fine grid)."""
        self.show_guides = bool(on)
        self._redraw()

    def _arena_spans(self, level, axis):
        """Where a grid line crosses the INSIDE of the arena polygon. For a vertical
        line at x=level (axis 0) returns the y-spans inside the arena; for a horizontal
        line at y=level (axis 1) returns the x-spans. Even-odd scanline, so it works
        for any arena shape (rectangle, trapezoid, free-form)."""
        pts = self.arena_polygon
        xs = [float(p[0]) for p in pts]
        ys = [float(p[1]) for p in pts]
        n = len(pts)
        hits = []
        for i in range(n):
            if axis == 0:                       # vertical line at x=level -> y hits
                a, b, va, vb = xs[i], xs[(i + 1) % n], ys[i], ys[(i + 1) % n]
            else:                               # horizontal line at y=level -> x hits
                a, b, va, vb = ys[i], ys[(i + 1) % n], xs[i], xs[(i + 1) % n]
            if (a <= level < b) or (b <= level < a):
                hits.append(va + (level - a) / (b - a) * (vb - va))
        hits.sort()
        return [(hits[k], hits[k + 1]) for k in range(0, len(hits) - 1, 2)]

    def _draw_guides(self):
        """Draw a faint micro-grid INSIDE the arena (clipped to its shape) as an
        alignment aid. Shown only once the arena exists; purely visual, does not
        affect tracking."""
        if self.arena_polygon is None or len(self.arena_polygon) < 3:
            return
        xs = [float(p[0]) for p in self.arena_polygon]
        ys = [float(p[1]) for p in self.arena_polygon]
        minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
        step = max(10.0, (maxx - minx) / 24.0)   # ~24 cells across the arena width
        grid = "#5a6b7a"

        def _seg(level, axis):
            for lo, hi in self._arena_spans(level, axis):
                p = self._to_screen((level, lo) if axis == 0 else (lo, level))
                q = self._to_screen((level, hi) if axis == 0 else (hi, level))
                self.create_line(p[0], p[1], q[0], q[1], fill=grid, width=1)

        v = minx + step
        while v < maxx:
            _seg(v, 0)
            v += step
        h = miny + step
        while h < maxy:
            _seg(h, 1)
            h += step

    def _draw_polygon(self, points, color, name, filled=False):
        if len(points) < 3:
            return
        screen_pts = [self._to_screen(p) for p in points]
        flat = [coord for pt in screen_pts for coord in pt]

        if filled:
            self.create_polygon(*flat, fill="", outline=color, width=3)
            # Semi-transparent fill using stipple
            self.create_polygon(*flat, fill=color, outline="", stipple="gray25")
        else:
            self.create_polygon(*flat, fill="", outline=color, width=3)

        # Label (skip when empty)
        if name:
            cx = sum(p[0] for p in screen_pts) // len(screen_pts)
            cy = sum(p[1] for p in screen_pts) // len(screen_pts)
            self.create_text(cx, cy, text=name, fill="white", font=("Arial", 12, "bold"))

    def _dim_chip(self, x, y, text, color):
        """A bold high-contrast label chip (sized to its text) for scale-line lengths."""
        half_w = 8 + len(str(text)) * 5
        self.create_rectangle(x - half_w, y - 12, x + half_w, y + 12,
                              fill="#000000", outline=color, width=2)
        self.create_text(x, y, text=text, fill=color, font=("Arial", 12, "bold"))

    def _to_screen(self, point):
        return (int(point[0] * self.scale + self.offset_x),
                int(point[1] * self.scale + self.offset_y))

    def _from_screen(self, x, y):
        return ((x - self.offset_x) / self.scale,
                (y - self.offset_y) / self.scale)

    def _on_left_click(self, event):
        if self.drawing_mode is None:
            return

        img_x, img_y = self._from_screen(event.x, event.y)

        # Check if clicking near first point to close polygon (if we have 3+ points)
        if len(self.current_polygon) >= 3:
            first_pt = self._to_screen(self.current_polygon[0])
            dist = ((event.x - first_pt[0])**2 + (event.y - first_pt[1])**2)**0.5
            if dist < 15:  # Within 15 pixels of first point
                self._finish_polygon()
                return

        # Check bounds
        if hasattr(self, 'cv_image') and self.cv_image is not None:
            h, w = self.cv_image.shape[:2]
            if 0 <= img_x <= w and 0 <= img_y <= h:
                self.current_polygon.append((img_x, img_y))
                self._redraw()
                self.focus_set()  # Take focus for keyboard events

    def _on_right_click(self, event):
        self._finish_polygon()

    def _on_double_click(self, event):
        # Double-click to finish polygon
        self._finish_polygon()

    def _on_enter_key(self, event):
        self._finish_polygon()

    def _on_escape_key(self, event):
        if self.drawing_mode:
            self.current_polygon = []
            self.drawing_mode = None
            self._redraw()
            if self.on_drawing_status_change:
                self.on_drawing_status_change(False)

    def _finish_polygon(self):
        # Arena, zones, and the fish-size outline are all free-form polygons.
        if self.drawing_mode not in ("arena", "zone", "fish") or len(self.current_polygon) < 3:
            return
        mode = self.drawing_mode
        reason = polygon_invalid_reason(self.current_polygon)
        if reason is not None:
            # Discard the bad shape and let the user redraw it.
            kind = {"arena": "Arena", "zone": "Zone", "fish": "Fish outline"}[mode]
            self.current_polygon = []
            self.drawing_mode = None
            self._redraw()
            if self.on_invalid_polygon:
                self.on_invalid_polygon(kind, reason)
            return
        points = np.array(self.current_polygon, dtype=np.float32)

        # The fish outline measures the silhouette (its area sets the detection
        # size bounds; its bounding box seeds the starting position).
        if mode == "fish":
            area = float(abs(cv2.contourArea(points)))
            x, y, bw, bh = cv2.boundingRect(points.astype(np.int32))
            self.fish_bbox = [int(x), int(y), int(bw), int(bh)]
            self.current_polygon = []
            self.drawing_mode = None
            self._redraw()
            if self.on_fish_selected:
                self.on_fish_selected(area)
            return

        if mode == "arena":
            self.arena_polygon = points
            self.arena_side_lengths = [""] * len(points)   # one blank per side
        else:
            color = self._get_zone_color(len(self.polygons))
            self.polygons.append((self.current_zone_name, points, color))

        self.current_polygon = []
        self.drawing_mode = None
        self._redraw()

        if mode == "arena" and self.on_arena_complete:
            self.on_arena_complete()      # app then asks for each side's length
        elif self.on_polygon_complete:
            self.on_polygon_complete()

    def _on_motion(self, event):
        if self.drawing_mode in ("arena", "zone", "fish") and self.current_polygon:
            self._redraw()
            last_screen = self._to_screen(self.current_polygon[-1])
            color = {"arena": "#00ffff", "fish": "#ffa500"}.get(self.drawing_mode, COLORS["accent"])
            self.create_line(last_screen[0], last_screen[1], event.x, event.y,
                           fill=color, width=2, dash=(4, 4))

    def _on_resize(self, event):
        self._update_display()

    def _get_zone_color(self, index):
        # Vibrant colors that work well on light backgrounds
        colors = ["#e74c3c", "#3498db", "#2ecc71", "#9b59b6", "#f39c12", "#1abc9c", "#e91e63", "#00bcd4"]
        return colors[index % len(colors)]

    def start_arena_drawing(self):
        self.current_polygon = []
        self.drawing_mode = "arena"
        self.arena_polygon = None
        self.arena_side_lengths = []
        self._redraw()

    def start_zone_drawing(self, zone_name: str):
        self.current_polygon = []
        self.drawing_mode = "zone"
        self.current_zone_name = zone_name
        self._redraw()

    def start_fish_drawing(self):
        """Trace the fish once to measure its silhouette area (sets size bounds)."""
        self.current_polygon = []
        self.drawing_mode = "fish"
        self._redraw()

    # ----- Calibration: real length of each arena side -----

    def _arena_sides(self):
        """List of (cornerA, cornerB) pixel pairs for each arena edge."""
        if self.arena_polygon is None or len(self.arena_polygon) < 3:
            return []
        pts = self.arena_polygon
        n = len(pts)
        return [(pts[i], pts[(i + 1) % n]) for i in range(n)]

    def num_arena_sides(self) -> int:
        return len(self._arena_sides())

    def set_arena_side_lengths(self, values, unit: str):
        """Store the real length typed for each arena side ('' = unknown)."""
        n = self.num_arena_sides()
        vals = list(values) if values else []
        vals = (vals + [""] * n)[:n]
        self.arena_side_lengths = [str(v).strip() for v in vals]
        self.scale_unit = unit or "cm"
        self._redraw()

    def get_arena_side_lengths(self) -> List[str]:
        return list(self.arena_side_lengths)

    def get_scale_unit(self) -> str:
        return self.scale_unit

    def get_fish_bbox(self) -> Optional[list]:
        """[x, y, w, h] of the last traced fish (seeds the start position), or None."""
        return list(self.fish_bbox) if self.fish_bbox is not None else None

    def get_scale_lines(self) -> List[Dict]:
        """Each arena side with a positive real length becomes a scale reference."""
        out = []
        for i, (a, b) in enumerate(self._arena_sides()):
            v = self.arena_side_lengths[i] if i < len(self.arena_side_lengths) else ""
            try:
                length = float(v)
            except (TypeError, ValueError):
                continue
            if length > 0:
                out.append({"p1": [float(a[0]), float(a[1])],
                            "p2": [float(b[0]), float(b[1])],
                            "length": length, "unit": self.scale_unit})
        return out

    def cancel_drawing(self):
        self.current_polygon = []
        self.drawing_mode = None
        self._redraw()

    def clear_zones(self):
        self.polygons = []
        self.selected_zone = None
        self._redraw()

    def clear_all(self):
        self.arena_polygon = None
        self.arena_side_lengths = []
        self.polygons = []
        self.current_polygon = []
        self.fish_bbox = None
        self.selected_zone = None
        self.drawing_mode = None
        self._redraw()

    def get_arena(self) -> Optional[np.ndarray]:
        return self.arena_polygon

    def get_zones(self) -> List[Tuple[str, np.ndarray]]:
        return [(name, pts) for name, pts, _ in self.polygons]

    def get_zone_names(self) -> List[str]:
        return [name for name, _, _ in self.polygons]

    def set_selected_zone(self, index):
        """Highlight a zone (by index) for editing, or None to clear."""
        self.selected_zone = index if (index is not None and 0 <= index < len(self.polygons)) else None
        self._redraw()

    def remove_zone(self, index) -> bool:
        """Delete a single zone by index. Other zones keep their names/colors."""
        if 0 <= index < len(self.polygons):
            del self.polygons[index]
            self.selected_zone = None
            self._redraw()
            return True
        return False

    def rename_zone(self, index, new_name: str) -> bool:
        """Rename a single zone by index (keeps its shape and color)."""
        if 0 <= index < len(self.polygons) and new_name:
            _, pts, color = self.polygons[index]
            self.polygons[index] = (new_name, pts, color)
            self._redraw()
            return True
        return False


# -----------------------------
# Live Preview Window
# -----------------------------

class LivePreviewWindow(tk.Toplevel):
    """Window showing real-time tracking preview with zone-colored tracker and B/W mask."""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("CordInsight - Live Preview")
        self.configure(bg=COLORS["bg_dark"])

        # Calculate size based on screen (70% of screen width, 50% height)
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        win_w = int(screen_w * 0.7)
        win_h = int(screen_h * 0.5)

        # Center on screen
        x = (screen_w - win_w) // 2
        y = (screen_h - win_h) // 2
        self.geometry(f"{win_w}x{win_h}+{x}+{y}")
        self.minsize(800, 400)

        # Make window stay on top
        self.attributes("-topmost", True)

        self._build_ui()
        self.photo_main = None
        self.photo_mask = None

        # Track trail (last N positions) - short for fast fade
        self.trail = []
        self.max_trail_length = 12
        self.last_contour = None     # last drawn fish outline (held during stills)
        self._contour_hold = 0       # consecutive frames the outline has been held

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.closed = False

    def _build_ui(self):
        # Title bar
        title_frame = tk.Frame(self, bg=COLORS["bg_dark"])
        title_frame.pack(fill="x", pady=5)

        tk.Label(title_frame, text="Live Tracking Preview",
                font=("Segoe UI", 14, "bold"), bg=COLORS["bg_dark"], fg=COLORS["text"]
                ).pack(side="left", padx=10)

        self.info_label = tk.Label(title_frame, text="Frame: 0 | Zone: -",
                                   font=("Segoe UI", 10), bg=COLORS["bg_dark"], fg=COLORS["text_dim"])
        self.info_label.pack(side="right", padx=10)

        # Video name label (for batch mode)
        self.video_name_label = tk.Label(self, text="",
                                         font=("Segoe UI", 11, "bold"), bg=COLORS["bg_dark"], fg=COLORS["gold"])
        self.video_name_label.pack(fill="x", padx=10)

        # Main content - two panels side by side
        content = tk.Frame(self, bg=COLORS["bg_dark"])
        content.pack(fill="both", expand=True, padx=10, pady=10)

        # Left panel - Video with tracking overlay
        left_frame = tk.Frame(content, bg=COLORS["bg_medium"])
        left_frame.pack(side="left", fill="both", expand=True, padx=(0, 5))

        tk.Label(left_frame, text="Tracking View (Zone-Colored)",
                font=("Segoe UI", 10, "bold"), bg=COLORS["bg_medium"], fg=COLORS["text"]
                ).pack(pady=5)

        self.canvas_main = tk.Canvas(left_frame, bg="black", highlightthickness=0)
        self.canvas_main.pack(fill="both", expand=True, padx=5, pady=5)

        # Right panel - Black/White foreground mask
        right_frame = tk.Frame(content, bg=COLORS["bg_medium"])
        right_frame.pack(side="right", fill="both", expand=True, padx=(5, 0))

        tk.Label(right_frame, text="Foreground Mask (Fish = White)",
                font=("Segoe UI", 10, "bold"), bg=COLORS["bg_medium"], fg=COLORS["text"]
                ).pack(pady=5)

        self.canvas_mask = tk.Canvas(right_frame, bg="black", highlightthickness=0)
        self.canvas_mask.pack(fill="both", expand=True, padx=5, pady=5)

        # Legend
        legend_frame = tk.Frame(self, bg=COLORS["bg_dark"])
        legend_frame.pack(fill="x", pady=5, padx=10)

        tk.Label(legend_frame, text="Zone Colors: ",
                font=("Segoe UI", 9), bg=COLORS["bg_dark"], fg=COLORS["text"]
                ).pack(side="left")

        self.legend_labels = []

    def set_current_video(self, video_name: str, video_num: int = 0, total_videos: int = 0):
        """Update the current video being processed (for batch mode display)."""
        # New video: clear the trail and any held outline from the previous one.
        self.trail = []
        self.last_contour = None
        self._contour_hold = 0
        if total_videos > 1:
            self.video_name_label.config(text=f"Now Processing ({video_num}/{total_videos}): {video_name}")
        elif video_name:
            self.video_name_label.config(text=f"Now Processing: {video_name}")
        else:
            self.video_name_label.config(text="")

    def update_legend(self, zones: List[Tuple[str, np.ndarray]], zone_colors: Dict):
        """Update the zone color legend."""
        # Clear old labels
        for label in self.legend_labels:
            label.destroy()
        self.legend_labels = []

        legend_frame = self.winfo_children()[-1]  # Last frame is legend

        for name, _ in zones:
            color_bgr = zone_colors.get(name, (128, 128, 128))
            # Convert BGR to hex
            color_hex = f"#{color_bgr[2]:02x}{color_bgr[1]:02x}{color_bgr[0]:02x}"

            label = tk.Label(legend_frame, text=f"  {name}  ",
                           font=("Segoe UI", 9, "bold"),
                           bg=color_hex, fg="black" if sum(color_bgr) > 380 else "white")
            label.pack(side="left", padx=2)
            self.legend_labels.append(label)

    def update_preview(self, frame: np.ndarray, fg_mask: np.ndarray,
                      centroid: Optional[Tuple[int, int]], zone_name: Optional[str],
                      zone_index: int, tracker_color: Tuple[int, int, int],
                      arena_poly: np.ndarray, zones: List[Tuple[str, np.ndarray]],
                      zone_colors: Dict, frame_idx: int, fps: float,
                      video_time_s: float = 0.0):
        """Update the preview with new frame data."""
        if self.closed:
            return

        try:
            # Update trail
            if centroid is not None:
                self.trail.append((centroid, tracker_color))
                if len(self.trail) > self.max_trail_length:
                    self.trail.pop(0)

            # Draw main frame with overlay
            display = frame.copy()

            def _draw_regions(canvas_img):
                # Arena (cyan) + zones (each zone's colour), matching the setup
                # canvas exactly. Drawn on both the colour and the B/W mask view.
                if arena_poly is not None:
                    cv2.polylines(canvas_img, [arena_poly.astype(np.int32)], True,
                                  VideoTracker.ARENA_COLOR, 2)
                for name, zpoly in zones:
                    color = zone_colors.get(name, (128, 128, 128))
                    cv2.polylines(canvas_img, [zpoly.astype(np.int32)], True, color, 2)
                    if len(zpoly) > 0:
                        zx = int(np.mean(zpoly[:, 0]))
                        zy = int(np.mean(zpoly[:, 1]))
                        cv2.putText(canvas_img, name, (zx - 20, zy),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2, cv2.LINE_AA)

            _draw_regions(display)

            # Draw trail as fading dots (small, like a dotted line)
            for i, (pos, color) in enumerate(self.trail):
                alpha = (i + 1) / len(self.trail)  # Fade effect
                radius = max(2, int(3 * alpha))
                cv2.circle(display, pos, radius, color, -1, cv2.LINE_AA)

            # EthoVision-style: outline the auto-detected fish boundary on the frame.
            # fg_mask is this frame's detection; its largest contour is the fish
            # silhouette. When the fish is momentarily still/undetected we HOLD the
            # last outline (dimmer) so the boundary keeps following it continuously
            # instead of blinking off.
            fish_cnt = None
            cnts, _ = cv2.findContours(fg_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            # Only outline the fish when this frame actually produced a RECORDED
            # detection (centroid is not None). This keeps the overlay honest — it
            # never shows a highlight while nothing is being tracked — and the hold is
            # brief (a few frames) just to ride out momentary single-frame misses.
            if centroid is not None and cnts:
                c = max(cnts, key=cv2.contourArea)
                if cv2.contourArea(c) >= 1:
                    fish_cnt = c
            if fish_cnt is not None:
                self.last_contour = fish_cnt
                self._contour_hold = 0
                cv2.drawContours(display, [fish_cnt], -1, tracker_color, 2, cv2.LINE_AA)
            else:
                held = getattr(self, "last_contour", None)
                self._contour_hold = getattr(self, "_contour_hold", 0) + 1
                if held is not None and self._contour_hold <= 8:
                    cv2.drawContours(display, [held], -1, tracker_color, 1, cv2.LINE_AA)

            # Draw current centroid with zone color
            if centroid is not None:
                cx, cy = centroid
                # Outer ring
                cv2.circle(display, (cx, cy), 15, tracker_color, 3)
                # Inner filled circle
                cv2.circle(display, (cx, cy), 8, tracker_color, -1)
                # White center dot
                cv2.circle(display, (cx, cy), 3, (255, 255, 255), -1)

            # Info overlay — lead with the video timestamp (same in Normal & Fast;
            # in Fast it just advances quickly, like a sped-up video).
            info_text = f"Time {format_time(video_time_s)}  |  Frame {frame_idx}  |  Zone: {zone_name or 'None'}"
            cv2.rectangle(display, (5, 5), (470, 35), (0, 0, 0), -1)
            cv2.putText(display, info_text, (10, 27),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)

            # Update main canvas
            self._update_canvas(self.canvas_main, display, "photo_main")

            # Update mask canvas (B/W view) — show the same arena + zone outlines
            # as the colour view so the two panels are spatially consistent.
            mask_display = cv2.cvtColor(fg_mask, cv2.COLOR_GRAY2BGR)
            _draw_regions(mask_display)

            # Draw green trail on mask
            for i, (pos, _) in enumerate(self.trail):
                alpha = (i + 1) / len(self.trail)
                radius = max(2, int(3 * alpha))
                cv2.circle(mask_display, pos, radius, (0, 255, 0), -1, cv2.LINE_AA)

            # Draw centroid on mask too
            if centroid is not None:
                cv2.circle(mask_display, centroid, 10, (0, 255, 0), 2)

            self._update_canvas(self.canvas_mask, mask_display, "photo_mask")

            # Update info label
            self.info_label.config(
                text=f"Time: {format_time(video_time_s)} | Frame: {frame_idx} | FPS: {fps:.1f} | Zone: {zone_name or 'None'}")

        except Exception:
            # Window was closed or other error
            self.closed = True

    def _update_canvas(self, canvas, cv_image, photo_attr):
        """Update a canvas with an OpenCV image."""
        try:
            canvas_w = canvas.winfo_width()
            canvas_h = canvas.winfo_height()

            if canvas_w <= 1 or canvas_h <= 1:
                return
        except tk.TclError:
            # Window was destroyed
            return

        item_attr = photo_attr + "_item"
        size_attr = photo_attr + "_size"
        try:
            h, w = cv_image.shape[:2]
            scale = min(canvas_w / w, canvas_h / h)
            new_w, new_h = max(1, int(w * scale)), max(1, int(h * scale))

            resized = cv2.resize(cv_image, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
            rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(rgb)

            photo = getattr(self, photo_attr, None)
            item_id = getattr(self, item_attr, None)
            prev_size = getattr(self, size_attr, None)

            if photo is None or prev_size != (new_w, new_h) or item_id is None:
                # First frame or the display size changed: (re)create the photo and
                # its canvas item once, then keep reusing them.
                photo = ImageTk.PhotoImage(img)
                setattr(self, photo_attr, photo)
                setattr(self, size_attr, (new_w, new_h))
                if item_id is not None:
                    canvas.delete(item_id)
                item_id = canvas.create_image(canvas_w // 2, canvas_h // 2,
                                              anchor="center", image=photo)
                setattr(self, item_attr, item_id)
            else:
                # Same size: paste new pixels into the existing PhotoImage in place
                # and recentre the existing item — no new allocation, no rebuild.
                photo.paste(img)
                canvas.coords(item_id, canvas_w // 2, canvas_h // 2)
        except (tk.TclError, cv2.error):
            # Window was destroyed or image processing error
            pass

    def _on_close(self):
        self.closed = True
        self.destroy()


# -----------------------------
# Heatmap Results Window
# -----------------------------

class HeatmapWindow(tk.Toplevel):
    """Window showing heatmap visualization of tracking results."""

    def __init__(self, parent, results_list, arena, zones):
        """
        Args:
            parent: Parent window
            results_list: List of (summary, df, video_name) tuples
            arena: Arena polygon points
            zones: List of (zone_name, zone_points) tuples
        """
        super().__init__(parent)
        self.title("CordInsight - Tracking Heatmaps")
        self.configure(bg=COLORS["bg_dark"])

        # Calculate window size
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        win_w = int(screen_w * 0.8)
        win_h = int(screen_h * 0.8)
        x = (screen_w - win_w) // 2
        y = (screen_h - win_h) // 2
        self.geometry(f"{win_w}x{win_h}+{x}+{y}")

        self.results_list = results_list
        self.arena = arena
        self.zones = zones
        self.heatmap_images = []  # Keep references to prevent garbage collection

        self._build_ui()

    def _build_ui(self):
        # Title
        title_frame = tk.Frame(self, bg=COLORS["bg_dark"])
        title_frame.pack(fill="x", pady=10)
        tk.Label(title_frame, text="Tracking Heatmaps",
                font=("Segoe UI", 16, "bold"), bg=COLORS["bg_dark"], fg=COLORS["text"]
                ).pack(side="left", padx=20)
        tk.Label(title_frame, text="Shows where the subject spent most time (red = more time)",
                font=("Segoe UI", 10), bg=COLORS["bg_dark"], fg=COLORS["text_dim"]
                ).pack(side="left", padx=10)

        # Close button (will be connected to _on_close after canvas is created)
        self.close_btn = tk.Button(title_frame, text="Close", command=self._close_window,
                             bg=COLORS["accent"], fg="white", font=("Segoe UI", 10),
                             padx=15, pady=5, relief="flat", cursor="hand2")
        self.close_btn.pack(side="right", padx=20)

        # Scrollable content area
        container = tk.Frame(self, bg=COLORS["bg_dark"])
        container.pack(fill="both", expand=True, padx=10, pady=10)

        # Canvas with scrollbar for multiple heatmaps
        canvas = tk.Canvas(container, bg=COLORS["bg_dark"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        self.scrollable_frame = tk.Frame(canvas, bg=COLORS["bg_dark"])

        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )

        canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        # Store canvas reference for cleanup
        self._canvas = canvas

        # Enable mousewheel scrolling (cross-platform)
        def _on_mousewheel(event):
            if event.delta:
                canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
            elif event.num == 4:
                canvas.yview_scroll(-1, "units")
            elif event.num == 5:
                canvas.yview_scroll(1, "units")

        # Bind for Windows/Mac
        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        # Bind for Linux
        canvas.bind_all("<Button-4>", _on_mousewheel)
        canvas.bind_all("<Button-5>", _on_mousewheel)

        # Set window close protocol
        self.protocol("WM_DELETE_WINDOW", self._close_window)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Generate heatmaps for each video
        for i, (summary, df, video_name) in enumerate(self.results_list):
            self._add_heatmap_panel(i, video_name, df, summary)

    def _close_window(self):
        """Clean up bindings and close window."""
        try:
            self._canvas.unbind_all("<MouseWheel>")
            self._canvas.unbind_all("<Button-4>")
            self._canvas.unbind_all("<Button-5>")
        except Exception:
            pass
        self.destroy()

    def _add_heatmap_panel(self, index, video_name, df, summary):
        """Add a heatmap panel for one video."""
        panel = tk.Frame(self.scrollable_frame, bg=COLORS["bg_medium"], relief="solid", bd=1)
        panel.pack(fill="x", pady=10, padx=10)

        # Video name header
        header = tk.Frame(panel, bg=COLORS["accent"])
        header.pack(fill="x")
        tk.Label(header, text=f"Video {index + 1}: {video_name}",
                font=("Segoe UI", 12, "bold"), bg=COLORS["accent"], fg="white"
                ).pack(side="left", padx=10, pady=8)

        # Stats
        duration = summary.get("duration_s", 0)
        frames = summary.get("total_frames", 0)
        tk.Label(header, text=f"{duration:.1f}s | {frames} frames",
                font=("Segoe UI", 10), bg=COLORS["accent"], fg="white"
                ).pack(side="right", padx=10, pady=8)

        # Content frame
        content = tk.Frame(panel, bg=COLORS["bg_medium"])
        content.pack(fill="both", expand=True, padx=10, pady=10)

        # Generate and display heatmap
        heatmap_img = self._generate_heatmap(df)
        if heatmap_img is not None:
            # Convert to PhotoImage
            photo = ImageTk.PhotoImage(heatmap_img)
            self.heatmap_images.append(photo)  # Keep reference

            img_label = tk.Label(content, image=photo, bg=COLORS["bg_medium"])
            img_label.pack(pady=5)

        # Zone time summary
        zone_frame = tk.Frame(content, bg=COLORS["bg_medium"])
        zone_frame.pack(fill="x", pady=5)

        tk.Label(zone_frame, text="Time per zone:",
                font=("Segoe UI", 10, "bold"), bg=COLORS["bg_medium"], fg=COLORS["text"]
                ).pack(side="left", padx=5)

        for zone_name, seconds in summary.get("seconds_per_zone", {}).items():
            pct = (seconds / duration * 100) if duration > 0 else 0
            zone_colors = {name: VideoTracker.ZONE_COLORS[i % len(VideoTracker.ZONE_COLORS)]
                          for i, (name, _) in enumerate(self.zones)}
            color_bgr = zone_colors.get(zone_name, (128, 128, 128))
            color_hex = f"#{color_bgr[2]:02x}{color_bgr[1]:02x}{color_bgr[0]:02x}"

            zone_label = tk.Label(zone_frame, text=f"  {zone_name}: {seconds:.1f}s ({pct:.1f}%)  ",
                                 font=("Segoe UI", 9, "bold"), bg=color_hex,
                                 fg="black" if sum(color_bgr) > 380 else "white")
            zone_label.pack(side="left", padx=3)

    def _generate_heatmap(self, df):
        """Generate a heatmap image from tracking data."""
        if df is None or "x" not in df.columns or "y" not in df.columns:
            return None
        # Get valid positions (convert to numeric, coerce errors to NaN)
        x_series = pd.to_numeric(df["x"], errors="coerce").dropna()
        y_series = pd.to_numeric(df["y"], errors="coerce").dropna()

        # Get common valid indices
        valid_idx = x_series.index.intersection(y_series.index)
        x_data = x_series.loc[valid_idx].values.astype(float)
        y_data = y_series.loc[valid_idx].values.astype(float)

        if "sample_duration_s" in df.columns:
            duration_series = pd.to_numeric(
                df.loc[valid_idx, "sample_duration_s"], errors="coerce").fillna(0.0)
            weights = duration_series.values.astype(float)
        else:
            weights = np.ones(len(valid_idx), dtype=float)

        # Reconstructed coordinates are normally inside a convex arena, but an
        # arbitrary/concave ROI can be crossed by a straight interpolation. Never
        # let such samples contribute heat outside the experimental arena.
        inside = np.array([
            point_in_polygon((x, y), self.arena) for x, y in zip(x_data, y_data)
        ], dtype=bool)
        x_data, y_data, weights = x_data[inside], y_data[inside], weights[inside]

        if len(x_data) == 0 or len(y_data) == 0:
            return None

        # Get arena bounds
        arena_pts = self.arena.astype(np.int32)
        x_min, y_min = arena_pts.min(axis=0)
        x_max, y_max = arena_pts.max(axis=0)

        # Add padding
        padding = 20
        x_min = max(0, x_min - padding)
        y_min = max(0, y_min - padding)
        x_max = x_max + padding
        y_max = y_max + padding

        img_w = x_max - x_min
        img_h = y_max - y_min

        # Create heatmap using 2D histogram
        # Translate coordinates to image space
        x_translated = x_data - x_min
        y_translated = y_data - y_min

        # Create histogram
        bins = 50
        heatmap, _, _ = np.histogram2d(
            x_translated, y_translated,
            bins=bins, range=[[0, img_w], [0, img_h]], weights=weights
        )

        # Apply Gaussian blur for smoothness (using OpenCV instead of scipy)
        heatmap = heatmap.T.astype(np.float32)
        heatmap = cv2.GaussianBlur(heatmap, (15, 15), 0)

        # Normalize to 0-255
        if heatmap.max() > 0:
            heatmap = (heatmap / heatmap.max() * 255).astype(np.uint8)
        else:
            heatmap = np.zeros((bins, bins), dtype=np.uint8)

        # Resize heatmap to image size
        heatmap_resized = cv2.resize(heatmap, (img_w, img_h), interpolation=cv2.INTER_LINEAR)

        # Apply the arena polygon after Gaussian blur. Masking only the input points
        # is insufficient because the blur itself spreads density across boundaries.
        arena_translated = arena_pts - np.array([x_min, y_min])
        arena_mask = np.zeros((img_h, img_w), dtype=np.uint8)
        cv2.fillPoly(arena_mask, [arena_translated], 255)
        heatmap_resized[arena_mask == 0] = 0

        # Apply colormap (blue to red)
        heatmap_colored = cv2.applyColorMap(heatmap_resized, cv2.COLORMAP_JET)

        # Create base image (white background)
        img = np.ones((img_h, img_w, 3), dtype=np.uint8) * 255

        # Blend heatmap with base (only where there's heat)
        mask = (heatmap_resized > 10) & (arena_mask > 0)
        mask_3ch = np.stack([mask, mask, mask], axis=-1)
        img = np.where(mask_3ch, heatmap_colored, img)

        # Draw arena outline (translated)
        cv2.polylines(img, [arena_translated], True, (0, 0, 0), 2)

        # Draw zones (translated)
        zone_colors = {name: VideoTracker.ZONE_COLORS[i % len(VideoTracker.ZONE_COLORS)]
                      for i, (name, _) in enumerate(self.zones)}

        for zone_name, zone_pts in self.zones:
            zone_translated = zone_pts.astype(np.int32) - np.array([x_min, y_min])
            color = zone_colors.get(zone_name, (128, 128, 128))
            cv2.polylines(img, [zone_translated], True, color, 2)

            # Draw zone label
            centroid = zone_translated.mean(axis=0).astype(int)
            cv2.putText(img, zone_name, tuple(centroid), cv2.FONT_HERSHEY_SIMPLEX,
                       0.6, (0, 0, 0), 2, cv2.LINE_AA)
            cv2.putText(img, zone_name, tuple(centroid), cv2.FONT_HERSHEY_SIMPLEX,
                       0.6, (255, 255, 255), 1, cv2.LINE_AA)

        # Scale down if too large
        max_display_size = 600
        scale = min(max_display_size / img_w, max_display_size / img_h, 1.0)
        if scale < 1.0:
            new_w = int(img_w * scale)
            new_h = int(img_h * scale)
            img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)

        # Convert BGR to RGB for PIL
        img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        return Image.fromarray(img_rgb)


# -----------------------------
# Modern Styled Widgets
# -----------------------------

class ModernButton(tk.Button):
    def __init__(self, parent, text, command=None, style="primary", **kwargs):
        # Button styles: (background, hover, text_color)
        colors = {
            "primary": (COLORS["accent"], COLORS["accent_hover"], "#ffffff"),  # Maroon
            "secondary": ("#ffffff", "#f0f0f0", COLORS["accent"]),  # White with maroon text
            "success": (COLORS["gold"], COLORS["gold_hover"], COLORS["accent"]),  # Gold with maroon text
        }
        bg, hover, fg = colors.get(style, colors["primary"])

        super().__init__(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=hover,
            activeforeground=fg,
            disabledforeground=fg,  # Keep same text color when disabled
            relief="flat",
            cursor="hand2",
            font=("Segoe UI", 10, "bold"),
            padx=15,
            pady=6,
            bd=0,
            highlightthickness=0,
            **kwargs
        )
        self.bind("<Enter>", lambda e: self.config(bg=hover))
        self.bind("<Leave>", lambda e: self.config(bg=bg))


class ModernLabel(tk.Label):
    def __init__(self, parent, text="", style="normal", **kwargs):
        fonts = {
            "title": ("Segoe UI", 24, "bold"),
            "heading": ("Segoe UI", 14, "bold"),
            "normal": ("Segoe UI", 11),
            "small": ("Segoe UI", 9),
        }
        # Allow overriding defaults via kwargs
        bg = kwargs.pop("bg", COLORS["bg_dark"])
        fg = kwargs.pop("fg", COLORS["text"])
        font = kwargs.pop("font", fonts.get(style, fonts["normal"]))
        super().__init__(
            parent,
            text=text,
            bg=bg,
            fg=fg,
            font=font,
            **kwargs
        )


class ModernFrame(tk.Frame):
    def __init__(self, parent, **kwargs):
        super().__init__(parent, bg=COLORS["bg_dark"], **kwargs)


class ModernEntry(tk.Entry):
    def __init__(self, parent, **kwargs):
        super().__init__(
            parent,
            bg=COLORS["bg_medium"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            relief="solid",
            font=("Segoe UI", 11),
            bd=1,
            highlightthickness=1,
            highlightcolor=COLORS["accent"],
            highlightbackground=COLORS["border"],
            **kwargs
        )


def add_tooltip(widget, text: str):
    """Show `text` in a small popup while the pointer is over `widget`.

    Keeps per-field explanations off the always-visible panel — hover to learn
    what a setting does instead of reading a paragraph next to it.
    """
    state = {"tip": None}

    def show(_event=None):
        if state["tip"] is not None:
            return
        try:
            x = widget.winfo_rootx() + 20
            y = widget.winfo_rooty() + widget.winfo_height() + 4
        except tk.TclError:
            return
        tip = tk.Toplevel(widget)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{x}+{y}")
        tk.Label(tip, text=text, justify="left", bg="#101418", fg="#ffffff",
                 font=("Segoe UI", 9), padx=8, pady=5, wraplength=260,
                 relief="solid", bd=1).pack()
        state["tip"] = tip

    def hide(_event=None):
        if state["tip"] is not None:
            try:
                state["tip"].destroy()
            except tk.TclError:
                pass
            state["tip"] = None

    widget.bind("<Enter>", show)
    widget.bind("<Leave>", hide)
    widget.bind("<Destroy>", hide)


# -----------------------------
# Main Application
# -----------------------------

class CordInsightApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"{APP_NAME} v{APP_VERSION}")
        self.root.configure(bg=COLORS["bg_dark"])

        # Dynamic window sizing based on screen
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        win_w = min(1400, int(screen_w * 0.85))
        win_h = min(900, int(screen_h * 0.85))
        x = (screen_w - win_w) // 2
        y = (screen_h - win_h) // 2
        self.root.geometry(f"{win_w}x{win_h}+{x}+{y}")
        self.root.minsize(1000, 750)

        # State
        self.video_path: Optional[str] = None
        self.video_path_original: Optional[str] = None  # Original path before conversion
        self.video_paths: List[str] = []  # For batch mode (may be converted paths)
        self.video_paths_original: List[str] = []  # Original paths for display
        self.batch_mode = False
        self.video_frame: Optional[np.ndarray] = None
        self.tracker: Optional[VideoTracker] = None
        self.processing = False
        self.results_summary: Optional[Dict] = None
        self.results_df: Optional[pd.DataFrame] = None
        self.batch_results: List[Tuple[Dict, pd.DataFrame, str]] = []  # For batch mode: (summary, df, filename)
        self.preview_window: Optional[LivePreviewWindow] = None
        self.logo_photo: Optional[ImageTk.PhotoImage] = None  # Keep reference to prevent garbage collection

        # Settings
        self.settings = TrackingSettings()

        # Default scale unit for new arenas.
        self._scale_unit = "cm"

        # The video frame currently shown on the canvas (where the fish is traced).
        self.reference_frame_index = 0

        # Message queue for thread communication
        self.message_queue = queue.Queue()   # control messages (progress, completion)
        # Previews use a dedicated single-slot handoff, NOT the queue: routing them
        # through message_queue let per-frame progress messages flood the queue and
        # drop almost every preview (the "one frame every 15s" bug). The tracker
        # overwrites this slot each emission; the UI renders the latest on its tick.
        self._preview_lock = threading.Lock()
        self._latest_preview = None

        self._build_ui()
        self._setup_keyboard_shortcuts()
        self._check_messages()

    def _load_logo_image(self, max_width: int = 280) -> Optional[ImageTk.PhotoImage]:
        """Load the logo image for display in the GUI."""
        try:
            # Try to load logo from various locations
            logo_paths = [
                Path(__file__).parent / "assets" / "Logo-CI-Transparent.png",
                Path(get_resource_path("assets/Logo-CI-Transparent.png")),
                Path.cwd() / "assets" / "Logo-CI-Transparent.png",
            ]

            logo_path = None
            for p in logo_paths:
                if p.exists():
                    logo_path = p
                    break

            if logo_path:
                img = Image.open(logo_path)
                # Calculate height to maintain aspect ratio
                aspect_ratio = img.height / img.width
                new_height = int(max_width * aspect_ratio)
                img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)
                return ImageTk.PhotoImage(img)
        except Exception as e:
            print(f"Could not load logo: {e}")
        return None

    def _build_ui(self):
        # Shortcuts bar at bottom of window
        shortcuts_bar = tk.Frame(self.root, bg=COLORS["bg_medium"], height=30)
        shortcuts_bar.pack(side="bottom", fill="x")
        shortcuts_bar.pack_propagate(False)

        shortcuts_text = (
            "Shortcuts:   Ctrl+O: Open Video   |   Ctrl+F: Pick Frame   |   "
            "Ctrl+D: Draw Arena   |   Ctrl+N: Add Zone   |   "
            "Ctrl+R: Run/Stop   |   Ctrl+E: Export   |   Esc: Cancel   |   Del: Clear Zones"
        )
        tk.Label(shortcuts_bar, text=shortcuts_text, bg=COLORS["bg_medium"],
                fg=COLORS["text_dim"], font=("Segoe UI", 9)).pack(side="left", padx=15, pady=5)

        # Main container
        main_container = ModernFrame(self.root)
        main_container.pack(fill="both", expand=True, padx=10, pady=10)

        # Left panel (two-column controls) — wide enough that all six steps fit
        # in order; the scrollbar reveals any steps below the fold.
        left_container = ModernFrame(main_container, width=348)
        left_container.pack(side="left", fill="y", padx=(0, 10))
        left_container.pack_propagate(False)

        left_canvas = tk.Canvas(left_container, bg=COLORS["bg_dark"],
                                highlightthickness=0, width=330)
        left_scrollbar = ttk.Scrollbar(left_container, orient="vertical",
                                       command=left_canvas.yview)
        left_panel = ModernFrame(left_canvas)
        left_panel.bind(
            "<Configure>",
            lambda e: left_canvas.configure(scrollregion=left_canvas.bbox("all")))
        left_canvas.create_window((0, 0), window=left_panel, anchor="nw", width=330)
        left_canvas.configure(yscrollcommand=left_scrollbar.set)
        left_canvas.pack(side="left", fill="both", expand=True)
        left_scrollbar.pack(side="right", fill="y")

        # Mouse-wheel scrolling, bound only while the pointer is over the panel.
        def _left_wheel(event):
            if event.delta:
                left_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
            elif getattr(event, "num", None) == 4:
                left_canvas.yview_scroll(-1, "units")
            elif getattr(event, "num", None) == 5:
                left_canvas.yview_scroll(1, "units")

        def _bind_left_wheel(_):
            left_canvas.bind_all("<MouseWheel>", _left_wheel)
            left_canvas.bind_all("<Button-4>", _left_wheel)
            left_canvas.bind_all("<Button-5>", _left_wheel)

        def _unbind_left_wheel(_):
            left_canvas.unbind_all("<MouseWheel>")
            left_canvas.unbind_all("<Button-4>")
            left_canvas.unbind_all("<Button-5>")

        left_canvas.bind("<Enter>", _bind_left_wheel)
        left_canvas.bind("<Leave>", _unbind_left_wheel)

        # Right panel (canvas)
        right_panel = ModernFrame(main_container)
        right_panel.pack(side="right", fill="both", expand=True)

        self._build_left_panel(left_panel)
        self._build_right_panel(right_panel)

    def _build_left_panel(self, parent):
        # Logo
        self.logo_photo = self._load_logo_image(max_width=200)
        if self.logo_photo:
            tk.Label(parent, image=self.logo_photo, bg=COLORS["bg_dark"]).pack(pady=(5, 6))
        else:
            ModernLabel(parent, text=APP_NAME, style="title", fg=COLORS["accent"]).pack(pady=(5, 2))
            ModernLabel(parent, text=APP_SUBTITLE, style="heading", fg=COLORS["gold"]).pack(pady=(0, 6))

        # Welcome / status line at the top, just under the logo.
        status_frame = ModernFrame(parent)
        status_frame.pack(side="top", fill="x", pady=(0, 4), padx=10)
        self.status_label = ModernLabel(status_frame, text="Welcome! Load a video to begin.",
                                         style="small", fg=COLORS["text_dim"], wraplength=300)
        self.status_label.pack(anchor="w")

        body = ModernFrame(parent)
        body.pack(fill="both", expand=True)

        def _step(title):
            ttk.Separator(body, orient="horizontal").pack(fill="x", pady=(6, 2))
            f = ModernFrame(body)
            f.pack(fill="x", padx=8)
            ModernLabel(f, text=title, style="heading").pack(anchor="w")
            return f

        def _hint(container, text):
            ModernLabel(container, text=text, style="small", fg=COLORS["text_dim"],
                        wraplength=300, justify="left").pack(anchor="w", pady=(0, 2))

        def _row(container):
            r = ModernFrame(container)
            r.pack(fill="x", pady=3)
            return r

        # 1 - Video & fish: choose the clip(s) and the frame everything is drawn on,
        # then optionally trace the fish so the tracker learns its size. There is no
        # tracking-method picker — CordInsight uses one automatic pipeline (reference
        # background subtraction + persistent contour tracking + gap reconstruction).
        s1 = _step("1 · Video & fish")
        self.batch_var = tk.BooleanVar(value=False)
        self.batch_check = tk.Checkbutton(s1, text="Batch mode (multiple videos)",
                                          variable=self.batch_var, command=self._toggle_batch_mode,
                                          bg=COLORS["bg_dark"], fg=COLORS["text"],
                                          selectcolor=COLORS["bg_dark"], activebackground=COLORS["bg_dark"])
        self.batch_check.pack(anchor="w")
        r = _row(s1)
        self.browse_btn = ModernButton(r, "Browse Video", self._select_video, style="secondary")
        self.browse_btn.pack(side="left")
        ModernButton(r, "Pick Frame", self._pick_frame, style="secondary").pack(side="left", padx=5)
        self.video_label = ModernLabel(s1, text="No video selected", style="small",
                                        fg=COLORS["text_dim"], wraplength=300)
        self.video_label.pack(anchor="w", pady=2)
        # Manual size fields live under Advanced; create their vars here.
        self.min_area_var = tk.StringVar(value="100")
        self.max_area_var = tk.StringVar(value="50000")
        _hint(s1, "Optional: trace the fish so CordInsight learns its size (sharpens detection).")
        self.fish_btn = ModernButton(s1, "Select Fish — trace it", self._start_fish_selection,
                                     style="secondary")
        self.fish_btn.pack(anchor="w", pady=2)
        self.fish_size_label = ModernLabel(s1, text="Fish size: not set", style="small",
                                           fg=COLORS["text_dim"], wraplength=300)
        self.fish_size_label.pack(anchor="w", pady=2)

        # 2 - Arena
        s3 = _step("2 · Arena")
        _hint(s3, "Draw the tank outline; you'll be asked each side's real length (for distance/speed).")
        r = _row(s3)
        ModernButton(r, "Draw Arena", self._start_arena_drawing, style="secondary").pack(side="left")
        self.edit_dims_btn = ModernButton(r, "Edit Dimensions", self._edit_dimensions, style="secondary")
        self.edit_dims_btn.pack(side="left", padx=5)
        self.region_status = ModernLabel(s3, text="Arena: Not set | Zones: 0", style="small",
                                          fg=COLORS["text_dim"], wraplength=300)
        self.region_status.pack(anchor="w", pady=2)
        self.cal_status = ModernLabel(s3, text="Calibration: not set", style="small",
                                      fg=COLORS["text_dim"], wraplength=300)
        self.cal_status.pack(anchor="w", pady=2)
        self.guides_var = tk.BooleanVar(value=False)
        self.guides_check = tk.Checkbutton(
            s3, text="Show alignment grid", variable=self.guides_var,
            command=self._toggle_guides, bg=COLORS["bg_dark"], fg=COLORS["text"],
            selectcolor=COLORS["bg_dark"], activebackground=COLORS["bg_dark"],
            activeforeground=COLORS["text"], anchor="w")
        self.guides_check.pack(anchor="w", pady=2)

        # 4 - Zones
        s4 = _step("3 · Zones")
        r = _row(s4)
        self.add_zone_btn = ModernButton(r, "Add Zone", self._start_zone_drawing, style="secondary")
        self.add_zone_btn.pack(side="left")
        ModernButton(r, "Clear Zones", self._clear_zones, style="secondary").pack(side="left", padx=5)
        self.zone_listbox = tk.Listbox(s4, height=4, exportselection=False,
                                       bg=COLORS["bg_medium"], fg=COLORS["text"],
                                       selectbackground=COLORS["accent"], selectforeground="#ffffff",
                                       highlightthickness=1, highlightbackground=COLORS["border"],
                                       relief="solid", bd=1, font=("Segoe UI", 9))
        self.zone_listbox.pack(fill="x", pady=2)
        self.zone_listbox.bind("<<ListboxSelect>>", self._on_zone_select)
        r = _row(s4)
        ModernButton(r, "Rename Zone", self._rename_selected_zone, style="secondary").pack(side="left")
        ModernButton(r, "Delete Zone", self._delete_selected_zone, style="secondary").pack(side="left", padx=5)
        ModernButton(s4, "Clear All", self._clear_all, style="secondary").pack(anchor="w", pady=3)

        # 5 - Measure
        s5 = _step("4 · Measure")
        self.measure_vars = {}

        def _add_measure(key, label, default):
            var = tk.BooleanVar(value=default)
            tk.Checkbutton(s5, text=label, variable=var,
                           bg=COLORS["bg_dark"], fg=COLORS["text"], selectcolor=COLORS["bg_dark"],
                           activebackground=COLORS["bg_dark"], activeforeground=COLORS["text"],
                           anchor="w", wraplength=300, justify="left").pack(anchor="w")
            self.measure_vars[key] = var

        _add_measure("time_per_zone", "Time per zone", True)
        _add_measure("zone_entries", "Zone entries", True)
        _add_measure("distance", "Distance traveled  (needs calibration)", False)
        _add_measure("speed", "Speed: mean & max  (needs calibration)", False)

        # 6 - Run
        s6 = _step("5 · Run")
        # Trial window: label on its own line, then a clean "From [ ] To [ ] sec" row
        # with two equal, fully-visible boxes.
        ModernLabel(s6, text="Trial window (optional)", style="small").pack(anchor="w")
        tw = _row(s6)
        ModernLabel(tw, text="From", style="small").pack(side="left")
        self.trial_start_var = tk.StringVar(value="")
        ModernEntry(tw, textvariable=self.trial_start_var, width=6).pack(side="left", padx=(3, 10))
        ModernLabel(tw, text="To", style="small").pack(side="left")
        self.trial_end_var = tk.StringVar(value="")
        ModernEntry(tw, textvariable=self.trial_end_var, width=6).pack(side="left", padx=3)
        ModernLabel(tw, text="sec", style="small", fg=COLORS["text_dim"]).pack(side="left", padx=(3, 0))
        # Show the loaded video's length so the window can be set without guessing.
        # Blank = whole video. The whole clip is always tracked; only this window is
        # kept in the results.
        self.trial_hint = ModernLabel(
            s6, text="Leave blank to analyze the whole video.", style="small",
            fg=COLORS["text_dim"], wraplength=300, justify="left")
        self.trial_hint.pack(anchor="w", pady=(0, 2))
        self.process_btn = ModernButton(s6, "Start Processing", self._start_processing, style="primary")
        self.process_btn.pack(fill="x", pady=5)
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(s6, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill="x", pady=3)
        self.progress_label = ModernLabel(s6, text="Ready", style="small",
                                          fg=COLORS["text_dim"], wraplength=300)
        self.progress_label.pack(anchor="w")
        self.export_btn = ModernButton(s6, "Export to Excel", self._export_results, style="success")
        self.export_btn.pack(fill="x", pady=5)
        self.export_btn.config(state="disabled")

        # Advanced (collapsible) - detection knobs, hidden by default. Hover any
        # field for a plain explanation instead of on-screen paragraphs.
        ttk.Separator(body, orient="horizontal").pack(fill="x", pady=(6, 2))
        self._advanced_open = False
        self.advanced_btn = ModernButton(body, "▸ Advanced (fine-tuning)",
                                         self._toggle_advanced, style="secondary")
        self.advanced_btn.pack(anchor="w", padx=8, pady=2)
        self.advanced_frame = ModernFrame(body)
        adv = self.advanced_frame

        # Advanced holds the fine-tuning detection knobs only.
        a = _row(adv)
        ModernLabel(a, text="Fish size px²  Min", style="small").pack(side="left")
        e_min = ModernEntry(a, textvariable=self.min_area_var, width=7)
        e_min.pack(side="left", padx=3)
        ModernLabel(a, text="Max", style="small").pack(side="left")
        e_max = ModernEntry(a, textvariable=self.max_area_var, width=7)
        e_max.pack(side="left", padx=3)
        add_tooltip(e_min, "Smallest detected blob area (pixels²) counted as the fish. Raise to ignore specks. 'Select Fish' fills this for you.")
        add_tooltip(e_max, "Largest detected blob area (pixels²) counted as the fish. Lower to ignore large objects.")

        a = _row(adv)
        ModernLabel(a, text="Noise filter", style="small").pack(side="left")
        self.blur_var = tk.StringVar(value="5")
        e_blur = ModernEntry(a, textvariable=self.blur_var, width=4)
        e_blur.pack(side="left", padx=3)
        add_tooltip(e_blur, "Blur strength in pixels (odd, 1–15) applied before detection. Higher = smoother image, fewer false specks.")

        a = _row(adv)
        ModernLabel(a, text="Movement floor (px)", style="small").pack(side="left")
        self.movement_threshold_var = tk.StringVar(value="2.0")
        e_move = ModernEntry(a, textvariable=self.movement_threshold_var, width=5)
        e_move.pack(side="left", padx=3)
        add_tooltip(e_move, "Movement smaller than this many pixels counts as none, so a still fish doesn't accumulate fake distance.")

        a = _row(adv)
        ModernLabel(a, text="Fish is", style="small").pack(side="left")
        self.polarity_var = tk.StringVar(value="Darker than tank")
        cb_pol = ttk.Combobox(a, textvariable=self.polarity_var,
                              values=["Darker than tank", "Lighter than tank"],
                              width=16, state="readonly")
        cb_pol.pack(side="left", padx=3)
        add_tooltip(cb_pol, "Whether the fish is darker or lighter than the tank background. "
                            "'Darker than tank' (the usual case) ignores bright water reflections and "
                            "glints, the main cause of a noisy mask. Select Fish sets this automatically.")

        a = _row(adv)
        ModernLabel(a, text="Speed", style="small").pack(side="left")
        self.speed_var = tk.StringVar(value="Fast")
        cb = ttk.Combobox(a, textvariable=self.speed_var,
                          values=["Normal", "Fast"], width=8, state="readonly")
        cb.pack(side="left", padx=3)
        add_tooltip(cb, "Normal plays the preview at real time (1x) so you can watch tracking closely. "
                        "Fast plays a smooth ~6x sped-up preview (a 6-min video in about a minute). "
                        "Either way every frame is analyzed.")

        # Gate steps whose prerequisites aren't met yet (no arena -> no zones).
        self._refresh_step_gating()

    def _refresh_step_gating(self):
        """Disable controls whose hard prerequisite isn't met yet, so the flow reads
        as one logical step after another instead of failing with a late error: zones
        need an arena to bound them, so 'Add Zone' / 'Edit Dimensions' stay disabled
        until the arena exists."""
        has_arena = (getattr(self, "canvas", None) is not None
                     and self.canvas.get_arena() is not None)
        arena_state = "normal" if has_arena else "disabled"
        for attr in ("add_zone_btn", "edit_dims_btn"):
            btn = getattr(self, attr, None)
            if btn is not None:
                try:
                    btn.config(state=arena_state)
                except tk.TclError:
                    pass

    def _compute_backend(self):
        """Compute backend for the run (shown to the user). The single automatic
        pipeline — reference subtraction — runs on the CPU."""
        return "CPU (reference subtraction)"

    def _update_trial_hint(self):
        """Show the loaded video's length next to the trial window so the user can
        set From/To without guessing (and knows blank = whole clip)."""
        if not hasattr(self, "trial_hint"):
            return
        path = self.video_path if getattr(self, "video_path", None) else None
        dur = video_duration_seconds(path) if path else None
        if dur:
            self.trial_hint.config(
                text=f"Video is {dur:.1f} s. Blank = whole clip; the whole clip is always "
                     f"tracked, only this window is saved.")
        else:
            self.trial_hint.config(text="Leave blank to analyze the whole video.")

    def _toggle_guides(self):
        """Show/hide the alignment overlay (arena-center cross + fine grid) on the canvas."""
        if hasattr(self, "canvas"):
            self.canvas.set_show_guides(bool(self.guides_var.get()))

    def _toggle_advanced(self):
        self._advanced_open = not getattr(self, "_advanced_open", False)
        if self._advanced_open:
            self.advanced_frame.pack(fill="x", padx=8, pady=2)
            self.advanced_btn.config(text="▾ Advanced (fine-tuning)")
        else:
            self.advanced_frame.pack_forget()
            self.advanced_btn.config(text="▸ Advanced (fine-tuning)")

    def _build_right_panel(self, parent):
        # Canvas for video frame and zone drawing. A dark background so the video's
        # letterbox margins read as a neutral frame rather than glaring white.
        self.canvas = ZoneDrawingCanvas(parent, bg="#222a30", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.on_polygon_complete = self._on_polygon_complete
        self.canvas.on_arena_complete = self._on_arena_complete
        self.canvas.on_invalid_polygon = self._on_invalid_polygon
        self.canvas.on_fish_selected = self._on_fish_selected

        # Instructions overlay
        self.instructions_label = ModernLabel(parent, text="Load a video and select a frame to begin drawing zones",
                                               style="normal", fg=COLORS["text_dim"])
        self.instructions_label.place(relx=0.5, rely=0.5, anchor="center")

    def _setup_keyboard_shortcuts(self):
        """Set up keyboard shortcuts for common actions."""
        # Ctrl+O: Open/Browse Video
        self.root.bind("<Control-o>", lambda e: self._select_video())
        self.root.bind("<Control-O>", lambda e: self._select_video())

        # Ctrl+F: Pick Frame
        self.root.bind("<Control-f>", lambda e: self._pick_frame())
        self.root.bind("<Control-F>", lambda e: self._pick_frame())

        # Ctrl+D: Draw Arena
        self.root.bind("<Control-d>", lambda e: self._start_arena_drawing())
        self.root.bind("<Control-D>", lambda e: self._start_arena_drawing())

        # Ctrl+N: Add Zone (New zone)
        self.root.bind("<Control-n>", lambda e: self._start_zone_drawing())
        self.root.bind("<Control-N>", lambda e: self._start_zone_drawing())

        # Ctrl+R: Run/Stop Processing
        self.root.bind("<Control-r>", lambda e: self._start_processing())
        self.root.bind("<Control-R>", lambda e: self._start_processing())

        # Ctrl+E: Export to Excel
        self.root.bind("<Control-e>", lambda e: self._export_results())
        self.root.bind("<Control-E>", lambda e: self._export_results())

        # Escape: Cancel current drawing or processing
        self.root.bind("<Escape>", lambda e: self._handle_escape())

        # Delete: Clear all zones
        self.root.bind("<Delete>", lambda e: self._clear_zones())

    def _handle_escape(self):
        """Handle Escape key - cancel current operation."""
        # If drawing, cancel the drawing
        if hasattr(self, 'canvas') and self.canvas.drawing_mode:
            self.canvas.cancel_drawing()
            self._set_status("Drawing cancelled.")
        # If processing, cancel processing
        elif self.processing and self.tracker:
            self.tracker.cancel()
            self._set_status("Cancelling processing...")

    def _toggle_batch_mode(self):
        """Toggle between single and batch video mode."""
        self.batch_mode = self.batch_var.get()
        if self.batch_mode:
            self.browse_btn.config(text="Browse Videos")
            self.video_label.config(text="No videos selected")
            self.video_paths = []
            self.video_paths_original = []
            self.video_path = None
            self.video_path_original = None
        else:
            self.browse_btn.config(text="Browse Video")
            self.video_label.config(text="No video selected")
            self.video_paths = []
            self.video_paths_original = []
            self.video_path = None
            self.video_path_original = None
        self._reset_video_setup()

    def _invalidate_results(self):
        """Drop any previous run's results so stale data can't be exported after
        the setup changes (new video, or switching single/batch mode)."""
        self.results_summary = None
        self.results_df = None
        self.batch_results = []
        if hasattr(self, "export_btn"):
            self.export_btn.config(state="disabled")
        if hasattr(self, "progress_label"):
            self.progress_label.config(text="Ready")
        if hasattr(self, "progress_var"):
            self.progress_var.set(0)

    def _reset_video_setup(self):
        """Clear image-coordinate geometry before loading a different video set."""
        self.video_frame = None
        if hasattr(self, "canvas"):
            self.canvas.clear_all()
        if hasattr(self, "region_status"):
            self.region_status.config(text="Arena: Not set | Zones: 0")
        if hasattr(self, "cal_status"):
            self._update_cal_status()
        self._refresh_step_gating()   # arena cleared -> zones disabled again
        self._update_trial_hint()     # show the new video's length
        self._invalidate_results()

    def _convert_videos_with_progress(self, paths: List[str]) -> List[str]:
        """
        Convert any incompatible videos (e.g. .MTS) to MP4 off the UI thread,
        showing a modal progress dialog so the app stays responsive instead of
        appearing frozen. Returns converted paths in the same order; on failure
        the original path is kept (and OpenCV will report the open error later).
        """
        paths = list(paths)
        needing = [p for p in paths if VIDEO_CONVERTER.needs_conversion(p)]
        if not needing:
            return paths

        # Modal progress dialog. ffmpeg gives no easy progress %, so it's indeterminate.
        dialog = tk.Toplevel(self.root)
        dialog.title("Converting Video(s)")
        dialog.configure(bg=COLORS["bg_dark"])
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.protocol("WM_DELETE_WINDOW", lambda: None)  # block close mid-conversion

        ModernLabel(dialog, text="Preparing incompatible video(s) for tracking",
                    style="heading").pack(padx=24, pady=(18, 4))
        status_lbl = ModernLabel(dialog, text="Starting...", style="small",
                                 fg=COLORS["text_dim"])
        status_lbl.pack(padx=24, pady=2)
        ModernLabel(dialog, text="Large files can take a minute. Please wait.",
                    style="small", fg=COLORS["text_dim"]).pack(padx=24, pady=2)
        pb = ttk.Progressbar(dialog, mode="indeterminate", length=340)
        pb.pack(padx=24, pady=(10, 18))
        pb.start(12)

        # Center the dialog over the main window
        dialog.update_idletasks()
        rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
        rw, rh = self.root.winfo_width(), self.root.winfo_height()
        dw, dh = dialog.winfo_width(), dialog.winfo_height()
        dialog.geometry(f"+{rx + (rw - dw) // 2}+{ry + (rh - dh) // 2}")
        dialog.grab_set()

        shared = {"status": "Starting...", "done": False, "error": None, "results": {}}

        def worker():
            # Runs off the UI thread; must not touch any Tk widgets directly.
            try:
                total = len(needing)
                idx = 0
                for p in paths:
                    if VIDEO_CONVERTER.needs_conversion(p):
                        idx += 1
                        shared["status"] = f"Converting {os.path.basename(p)} ({idx}/{total})..."
                        shared["results"][p] = VIDEO_CONVERTER.convert(p)
                    else:
                        shared["results"][p] = p
            except Exception as e:
                shared["error"] = e
            finally:
                shared["done"] = True

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        # Keep the UI painting/animating while the worker runs.
        while not shared["done"]:
            status_lbl.config(text=shared["status"])
            dialog.update()
            time.sleep(0.03)

        pb.stop()
        dialog.grab_release()
        dialog.destroy()

        if shared["error"] is not None:
            messagebox.showerror(
                "Conversion Error",
                f"Could not convert video for compatibility:\n{shared['error']}")
            return paths

        return [shared["results"].get(p, p) for p in paths]

    def _select_video(self):
        # Check batch_var directly for reliability
        if self.batch_var.get():
            # Multiple file selection
            paths = filedialog.askopenfilenames(
                title="Select Video Files",
                filetypes=[
                    ("Video files", "*.mp4 *.avi *.mov *.mkv *.wmv *.webm *.mts *.m2ts *.ts"),
                    ("All files", "*.*")
                ]
            )
            if paths:
                self.video_paths_original = list(paths)
                self.video_paths = self._convert_videos_with_progress(list(paths))
                self.video_path = self.video_paths[0]  # Use first for frame/arena setup
                self._reset_video_setup()
                if len(self.video_paths) == 1:
                    self.video_label.config(text=os.path.basename(self.video_paths_original[0]))
                else:
                    self.video_label.config(text=f"{len(self.video_paths)} videos selected")
                self._set_status(f"Loaded {len(self.video_paths)} video(s). Using first for setup.")
                self._extract_frame(0)
        else:
            # Single file selection
            path = filedialog.askopenfilename(
                title="Select Video File",
                filetypes=[
                    ("Video files", "*.mp4 *.avi *.mov *.mkv *.wmv *.webm *.mts *.m2ts *.ts"),
                    ("All files", "*.*")
                ]
            )
            if path:
                self.video_path_original = path
                self.video_path = self._convert_videos_with_progress([path])[0]
                self.video_paths = [self.video_path]
                self.video_paths_original = [path]
                self._reset_video_setup()
                self.video_label.config(text=os.path.basename(path))
                self._set_status(f"Video loaded: {os.path.basename(path)}")
                self._extract_frame(0)

    def _extract_frame(self, frame_idx: int):
        if not self.video_path:
            return

        cap = cv2.VideoCapture(self.video_path)
        if not cap.isOpened():
            messagebox.showerror("Error", "Could not open video file")
            return

        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ok, frame = cap.read()
        cap.release()

        if ok and frame is not None:
            self.video_frame = frame
            self.reference_frame_index = int(frame_idx)
            self.canvas.set_image(frame)
            self.instructions_label.place_forget()
            self._set_status(f"Frame {frame_idx} loaded. Draw arena and zones.")

    def _pick_frame(self):
        if not self.video_path:
            messagebox.showwarning("No Video", "Please select a video first.")
            return

        # Open frame picker dialog
        picker = FramePickerDialog(self.root, self.video_path)
        self.root.wait_window(picker)

        if picker.selected_frame is not None:
            self.video_frame = picker.selected_frame
            self.reference_frame_index = int(picker.selected_frame_idx)
            self.canvas.set_image(picker.selected_frame)
            self.instructions_label.place_forget()
            self._set_status(f"Frame {picker.selected_frame_idx} selected. Draw arena and zones.")

    def _start_arena_drawing(self):
        if self.video_frame is None:
            messagebox.showwarning("No Frame", "Please load a video and select a frame first.")
            return
        self.canvas.start_arena_drawing()
        self._set_status(
            "Drawing arena: click points around the tracking area (any shape), "
            "then double-click or press ENTER to finish")

    def _start_zone_drawing(self):
        if self.video_frame is None:
            messagebox.showwarning("No Frame", "Please load a video and select a frame first.")
            return
        if self.canvas.get_arena() is None:
            messagebox.showwarning("No Arena", "Please draw the arena first.")
            return

        # Ask for zone name
        dialog = ZoneNameDialog(self.root, len(self.canvas.get_zones()) + 1)
        self.root.wait_window(dialog)

        if dialog.zone_name:
            if dialog.zone_name.strip().lower() in [n.strip().lower() for n in self.canvas.get_zone_names()]:
                messagebox.showerror("Duplicate Zone Name",
                                     f"A zone named '{dialog.zone_name}' already exists.\n\n"
                                     "Zone results are keyed by name, so each zone needs a unique name.")
                return
            self.canvas.start_zone_drawing(dialog.zone_name)
            self._set_status(f"Drawing zone '{dialog.zone_name}': Left-click to add points, right-click to finish")

    def _edit_dimensions(self):
        """Open the side-length dialog for the current arena to set/change/clear lengths.

        Editing and clearing both happen here: change the numbers, or blank every
        field and click OK to remove calibration entirely.
        """
        if self.canvas.get_arena() is None:
            messagebox.showwarning("No Arena", "Please draw the arena first, then edit its side lengths.")
            return
        self._ask_side_lengths()

    def _on_invalid_polygon(self, kind, reason):
        """The canvas rejected a degenerate / self-intersecting shape; tell the user."""
        messagebox.showwarning(
            f"Invalid {kind}",
            f"That {kind.lower()} {reason}.\n\nPlease draw it again with a clear, "
            "non-crossing outline.")
        self._set_status(f"{kind} discarded — {reason}. Draw it again.")

    def _start_fish_selection(self):
        """Trace the fish on the current frame to set the detection size (optional)."""
        if self.video_frame is None:
            messagebox.showwarning("No Frame",
                                   "Load a video and pick a frame where the fish is clearly "
                                   "visible first, then trace the fish.")
            return
        self.canvas.start_fish_drawing()
        self._set_status("Trace the fish: click points around its outline, then double-click "
                         "or press ENTER to finish. CordInsight will set the size for you.")

    def _on_fish_selected(self, area_px2):
        """Set Min/Max fish-size bounds from the traced silhouette area, and auto-set
        the fish polarity (darker vs lighter than the tank) from the traced region."""
        lo, hi = suggest_fish_area_bounds(area_px2)
        self.min_area_var.set(str(lo))
        self.max_area_var.set(str(hi))
        area = int(round(area_px2))
        pol_note = ""
        bbox = self.canvas.get_fish_bbox()
        if bbox is not None and self.video_frame is not None:
            pol = self._fish_polarity_from_bbox(bbox)
            if pol == "light":
                self.polarity_var.set("Lighter than tank"); pol_note = "  • lighter than tank"
            elif pol == "dark":
                self.polarity_var.set("Darker than tank"); pol_note = "  • darker than tank"
        if hasattr(self, "fish_size_label"):
            self.fish_size_label.config(text=f"Size: ~{area} px² measured ✓{pol_note}")
        self._set_status(f"Fish measured: ~{area} px² (detection size + contrast set automatically).")

    def _fish_polarity_from_bbox(self, bbox):
        """Decide whether the traced fish is darker or lighter than its surroundings,
        by comparing the mean brightness inside the box to a ring around it."""
        try:
            x, y, w, h = (int(v) for v in bbox)
            gray = cv2.cvtColor(self.video_frame, cv2.COLOR_BGR2GRAY)
            H, W = gray.shape[:2]
            x0, y0 = max(0, x), max(0, y)
            x1, y1 = min(W, x + w), min(H, y + h)
            if x1 <= x0 or y1 <= y0:
                return "dark"
            inside = float(gray[y0:y1, x0:x1].mean())
            m = max(8, (w + h) // 4)              # surrounding ring
            rx0, ry0 = max(0, x0 - m), max(0, y0 - m)
            rx1, ry1 = min(W, x1 + m), min(H, y1 + m)
            ring = gray[ry0:ry1, rx0:rx1].copy()
            ring[(y0 - ry0):(y1 - ry0), (x0 - rx0):(x1 - rx0)] = 0  # blank the fish box
            vals = ring[ring > 0]
            if vals.size == 0:
                return "dark"
            outside = float(vals.mean())
            return "dark" if inside < outside else "light"
        except Exception:
            return "dark"

    def _on_polygon_complete(self):
        arena = self.canvas.get_arena()
        zones = self.canvas.get_zones()
        arena_status = "Set" if arena is not None else "Not set"
        self.region_status.config(text=f"Arena: {arena_status} | Zones: {len(zones)}")
        self._refresh_zone_list()
        self._update_cal_status()
        self._refresh_step_gating()   # arena may have just appeared/disappeared
        self._set_status("Region defined. Continue adding zones or start processing.")

        # Validate zones if we have both arena and zones
        if arena is not None and len(zones) > 0:
            self._validate_zones(arena, zones)

    def _validate_zones(self, arena: np.ndarray, zones: List[Tuple[str, np.ndarray]]):
        """Validate zones and show warnings for issues."""
        warnings = []

        # Check each zone
        for i, (name, poly) in enumerate(zones):
            # Check if zone is inside arena
            fully_inside, pct_inside = polygon_inside_polygon(poly, arena)

            if pct_inside < 0.5:
                warnings.append(f"Zone '{name}' is mostly OUTSIDE the arena ({pct_inside*100:.0f}% inside). "
                               f"Tracking will not detect the animal in areas outside the arena.")
            elif not fully_inside:
                warnings.append(f"Zone '{name}' is partially outside the arena ({pct_inside*100:.0f}% inside). "
                               f"Only the portion inside the arena will be tracked.")

            # Check for overlaps with other zones
            for j, (other_name, other_poly) in enumerate(zones):
                if j <= i:  # Only check each pair once
                    continue

                overlaps, overlap_pct = polygons_overlap(poly, other_poly)
                if overlaps and overlap_pct > 0.1:  # More than 10% overlap
                    warnings.append(f"Zones '{name}' and '{other_name}' overlap ({overlap_pct*100:.0f}%). "
                                   "Time and entries in the overlapping area will count toward both zones.")

        # Show warnings if any
        if warnings:
            warning_text = "Zone Warnings:\n\n" + "\n\n".join(f"• {w}" for w in warnings)
            messagebox.showwarning("Zone Configuration Warnings", warning_text)

    def _clear_zones(self):
        self.canvas.clear_zones()
        self._on_polygon_complete()

    def _clear_all(self):
        self.canvas.clear_all()
        self._on_polygon_complete()

    # ----- Per-zone editing (rename / delete individual zones) -----

    def _refresh_zone_list(self):
        """Sync the zone listbox with the canvas's current zones."""
        if not hasattr(self, "zone_listbox"):
            return
        self.zone_listbox.delete(0, "end")
        for i, name in enumerate(self.canvas.get_zone_names()):
            self.zone_listbox.insert("end", f"{i + 1}. {name}")
        self.canvas.set_selected_zone(None)

    def _on_zone_select(self, event=None):
        sel = self.zone_listbox.curselection()
        self.canvas.set_selected_zone(sel[0] if sel else None)

    def _rename_selected_zone(self):
        sel = self.zone_listbox.curselection()
        if not sel:
            messagebox.showinfo("No Zone Selected", "Select a zone in the list first.")
            return
        idx = sel[0]
        names = self.canvas.get_zone_names()
        dialog = ZoneNameDialog(self.root, idx + 1, initial_name=names[idx])
        self.root.wait_window(dialog)
        if dialog.zone_name:
            others = [n.strip().lower() for j, n in enumerate(names) if j != idx]
            if dialog.zone_name.strip().lower() in others:
                messagebox.showerror("Duplicate Zone Name",
                                     f"A zone named '{dialog.zone_name}' already exists. Use a unique name.")
                return
            self.canvas.rename_zone(idx, dialog.zone_name)
            self._refresh_zone_list()
            self.zone_listbox.selection_set(idx)
            self.canvas.set_selected_zone(idx)
            self._set_status(f"Zone renamed to '{dialog.zone_name}'.")

    def _delete_selected_zone(self):
        sel = self.zone_listbox.curselection()
        if not sel:
            messagebox.showinfo("No Zone Selected", "Select a zone in the list first.")
            return
        idx = sel[0]
        names = self.canvas.get_zone_names()
        if not messagebox.askyesno("Delete Zone", f"Delete zone '{names[idx]}'?"):
            return
        self.canvas.remove_zone(idx)
        self._refresh_zone_list()
        zones = self.canvas.get_zones()
        arena_status = "Set" if self.canvas.get_arena() is not None else "Not set"
        self.region_status.config(text=f"Arena: {arena_status} | Zones: {len(zones)}")
        self._set_status("Zone deleted.")

    def _start_processing(self):
        if self.processing:
            # Cancel
            if self.tracker:
                self.tracker.cancel()
            return

        # Validate
        if not self.video_path and not self.video_paths:
            messagebox.showwarning("No Video", "Please select a video first.")
            return

        arena = self.canvas.get_arena()
        if arena is None:
            messagebox.showwarning("No Arena", "Please draw the arena (the tracking area) first.")
            return

        zones = self.canvas.get_zones()
        if not zones:
            messagebox.showwarning("No Zones", "Please add at least one zone.")
            return

        # Update settings
        try:
            self.settings.min_area = int(self.min_area_var.get())
            self.settings.max_area = int(self.max_area_var.get())
            if not (1 <= self.settings.min_area < self.settings.max_area):
                messagebox.showerror(
                    "Invalid Settings",
                    "Fish size (px² area): Min must be at least 1 and smaller than Max.")
                return
            blur = int(self.blur_var.get())
            movement_threshold = float(self.movement_threshold_var.get().strip() or "0")
            if not math.isfinite(movement_threshold) or movement_threshold < 0:
                messagebox.showerror("Invalid Settings",
                                     "Movement floor must be a finite, non-negative pixel value.")
                return
            self.settings.movement_threshold_px = movement_threshold
            # Blur must be odd number
            self.settings.blur_size = max(1, min(15, blur)) | 1
            # Speed mode: "Normal" = real-time watchable preview, "Fast" = max speed.
            self.settings.processing_speed = (
                "normal" if self.speed_var.get().strip().lower().startswith("normal")
                else "fast")
            # Trial window (blank From = from start, blank To = until end)
            start_str = self.trial_start_var.get().strip()
            end_str = self.trial_end_var.get().strip()
            trial_start = float(start_str) if start_str else 0.0
            trial_end = float(end_str) if end_str else 0.0
            if not (math.isfinite(trial_start) and math.isfinite(trial_end)):
                messagebox.showerror("Invalid Settings", "Trial window times must be finite numbers.")
                return
            if trial_start < 0 or trial_end < 0:
                messagebox.showerror("Invalid Settings", "Trial window times cannot be negative.")
                return
            if trial_end > 0 and trial_end <= trial_start:
                messagebox.showerror("Invalid Settings",
                                     "Trial window 'To' must be greater than 'From'.")
                return
            self.settings.trial_start = trial_start
            self.settings.trial_end = trial_end
        except ValueError:
            messagebox.showerror("Invalid Settings", "Settings must be valid numbers.")
            return

        # One automatic pipeline: reference (static-background) subtraction. The
        # optional Select-Fish trace sets the detection size; no method choice.
        self.settings.tracking_method = "refsub"
        self.settings.fish_bbox = self.canvas.get_fish_bbox()
        self.settings.fish_polarity = {"Darker than tank": "dark",
                                       "Lighter than tank": "light"}.get(
            self.polarity_var.get(), "dark")

        # Calibration (optional): the real length of each arena side (Step 2),
        # averaged into a uniform real-units-per-pixel scale. All sides share one
        # unit, so distance/speed report in that unit.
        self.settings.scale_lines = self.canvas.get_scale_lines()
        self.settings.calibration_unit = self.canvas.get_scale_unit() or "cm"

        # Measurements — which metrics to compute/export.
        self.settings.measure_time_per_zone = bool(self.measure_vars["time_per_zone"].get())
        self.settings.measure_zone_entries = bool(self.measure_vars["zone_entries"].get())
        self.settings.measure_distance = bool(self.measure_vars["distance"].get())
        self.settings.measure_speed = bool(self.measure_vars["speed"].get())

        if not any(v.get() for v in self.measure_vars.values()):
            messagebox.showwarning("No Measurements",
                                   "Please enable at least one measurement in Step 4.")
            return

        # Distance / Speed require a complete calibration (Step 3).
        if (self.settings.measure_distance or self.settings.measure_speed
                ) and not calibration_is_complete(self.settings):
            messagebox.showerror(
                "Calibration Required",
                "Distance and Speed need calibration.\n\n"
                "In Step 2 (Arena) draw the arena and enter at least one side's real length "
                "(it asks automatically, or use 'Edit Dimensions') — or turn those "
                "measurements off in Step 4.")
            return

        check_videos = self.video_paths if self.video_paths else [self.video_path]
        size_mismatches = video_size_mismatches(check_videos)
        if size_mismatches:
            details = "\n".join(
                f"• {os.path.basename(path)}: {size[0]}×{size[1]}"
                for path, size in size_mismatches[:6])
            messagebox.showerror(
                "Incompatible Batch Dimensions",
                "Calibration, arena, and zones use source-image pixels, so every batch "
                "video must have identical encoded dimensions.\n\nMismatches:\n" + details)
            return

        # Warn if the analysis window doesn't fit the video length(s).
        if self.settings.trial_start > 0 or self.settings.trial_end > 0:
            n = len(check_videos)
            empties, clamped = videos_window_issues(
                check_videos, self.settings.trial_start, self.settings.trial_end)
            if empties:
                start = self.settings.trial_start
                if n > 1:
                    msg = (f"{len(empties)} of {n} selected videos are shorter than the window "
                           f"start ({start:g} s) and would produce no data.\n\nContinue anyway?")
                else:
                    dur = empties[0][1]
                    msg = (f"This video is only {dur:.1f} s long, but the window starts at "
                           f"{start:g} s — there is nothing to analyze.\n\nContinue anyway?")
                if not messagebox.askyesno("Window Starts After Video Ends", msg):
                    return
            elif clamped:
                end = self.settings.trial_end
                if n > 1:
                    msg = (f"{len(clamped)} of {n} selected videos are shorter than the window "
                           f"end ({end:g} s) and will stop at their own end.\n\nContinue anyway?")
                else:
                    dur = clamped[0][1]
                    msg = (f"This video is only {dur:.1f} s long, but the window ends at {end:g} s.\n\n"
                           f"Analysis will stop at the end of the video ({dur:.1f} s).\n\n"
                           f"Continue anyway?")
                if not messagebox.askyesno("Window Extends Past Video", msg):
                    return

        # Start processing. Clear ALL prior results up front so a new run can never
        # export stale data from a previous run (e.g. 30 videos, then a different 30).
        self.processing = True
        self.process_btn.config(text="Cancel Processing")
        self.export_btn.config(state="disabled")
        self.progress_var.set(0)
        self.batch_results = []
        self.results_summary = None
        self.results_df = None

        # Store arena and zones for heatmap generation
        self.current_arena = arena
        self.current_zones = zones

        # Create live preview window
        with self._preview_lock:
            self._latest_preview = None
        self.preview_window = LivePreviewWindow(self.root)
        self.preview_window.update_legend(zones,
            {name: VideoTracker.ZONE_COLORS[i % len(VideoTracker.ZONE_COLORS)]
             for i, (name, _) in enumerate(zones)})

        # Determine videos to process (converted paths for processing, original for display)
        videos_to_process = self.video_paths if self.video_paths else [self.video_path]
        videos_original = self.video_paths_original if self.video_paths_original else [self.video_path_original or self.video_path]

        if self.batch_mode and len(videos_to_process) > 1:
            self.progress_label.config(text=f"Processing video 1/{len(videos_to_process)}...")
        else:
            self.progress_label.config(text="Initializing...")
        # Tell the user which compute backend this run uses.
        self._compute_backend_text = self._compute_backend()
        self._set_status(f"Processing on {self._compute_backend_text}.")

        # Run in thread
        self.tracker = VideoTracker(self.settings)
        thread = threading.Thread(
            target=self._process_video_thread,
            args=(videos_to_process, videos_original, arena, zones),
            daemon=True
        )
        thread.start()

    def _process_video_thread(self, video_paths, video_paths_original, arena, zones):
        """Process one or multiple videos sequentially."""
        total_videos = len(video_paths)

        all_results = []
        failures = []   # (display_name, error_message) for videos that could not be processed

        for video_idx, video_path in enumerate(video_paths):
            if self.tracker.cancelled:
                break

            # Get original path for display (use converted path as fallback)
            original_path = video_paths_original[video_idx] if video_idx < len(video_paths_original) else video_path

            # Create fresh tracker for each video (to reset background model)
            self.tracker = VideoTracker(self.settings)

            # Use closure to capture video_idx correctly
            vid_idx = video_idx
            vid_total = total_videos
            progress_state = {"last": 0.0}

            def progress_callback(progress, current, total, vi=vid_idx, vt=vid_total):
                # Coalesce to ~20/s so per-frame progress can't flood the queue
                # (which was starving previews) or make Cancel unresponsive.
                now = time.monotonic()
                if progress < 1.0 and now - progress_state["last"] < 0.05:
                    return
                progress_state["last"] = now
                if vt > 1:
                    self.message_queue.put(("batch_progress", vi + 1, vt, progress * 100, current, total))
                else:
                    self.message_queue.put(("progress", progress * 100, current, total))

            def preview_callback(**kwargs):
                # Overwrite the single latest-frame slot (bounded to one frame, no
                # queue, no drop). The UI renders whatever is here on its tick.
                with self._preview_lock:
                    self._latest_preview = kwargs

            # Notify UI of current video being processed (use original name for display)
            video_name = os.path.basename(original_path)
            self.message_queue.put(("set_video", video_name, video_idx + 1, total_videos))

            try:
                summary, df = self.tracker.track(video_path, arena, zones, progress_callback, preview_callback)
                if summary is not None:
                    all_results.append((summary, df, os.path.basename(original_path)))
            except Exception as e:
                # Record and keep going; the completion handler reports failures so
                # they are never silently dropped from a "successful" batch.
                failures.append((os.path.basename(original_path), str(e)))
                continue

        # Send completion message
        if self.tracker.cancelled:
            self.message_queue.put(("cancelled",))
        elif total_videos > 1:
            # Batch: always report via batch_complete so partial failures surface.
            self.message_queue.put(("batch_complete", all_results, failures))
        elif all_results:
            self.message_queue.put(("complete", all_results[0][0], all_results[0][1]))
        else:
            reason = failures[0][1] if failures else "No videos were processed successfully."
            self.message_queue.put(("error", f"Processing failed: {reason}"))

    def _check_messages(self):
        try:
            while True:
                msg = self.message_queue.get_nowait()
                if msg[0] == "progress":
                    self.progress_var.set(msg[1])
                    self.progress_label.config(text=f"Processing: {msg[2]}/{msg[3]} frames ({msg[1]:.1f}%)")
                elif msg[0] == "batch_progress":
                    # msg = ("batch_progress", video_num, total_videos, progress, current_frame, total_frames)
                    self.progress_var.set(msg[3])
                    self.progress_label.config(
                        text=f"Video {msg[1]}/{msg[2]}: {msg[4]}/{msg[5]} frames ({msg[3]:.1f}%)")
                elif msg[0] == "set_video":
                    # Update preview window with current video name (batch mode)
                    if hasattr(self, 'preview_window') and self.preview_window and not self.preview_window.closed:
                        self.preview_window.set_current_video(msg[1], msg[2], msg[3])
                elif msg[0] == "complete":
                    self._on_processing_complete(msg[1], msg[2])
                elif msg[0] == "batch_complete":
                    self._on_batch_complete(msg[1], msg[2])
                elif msg[0] == "cancelled":
                    self._on_processing_cancelled()
                elif msg[0] == "error":
                    self._on_processing_error(msg[1])
        except queue.Empty:
            pass

        # Render the newest preview frame from the single-slot handoff (never
        # starved by progress traffic, which lives in the queue above).
        preview_kwargs = None
        with self._preview_lock:
            if self._latest_preview is not None:
                preview_kwargs = self._latest_preview
                self._latest_preview = None
        if (preview_kwargs is not None and hasattr(self, 'preview_window')
                and self.preview_window and not self.preview_window.closed):
            self.preview_window.update_preview(**preview_kwargs)

        self.root.after(33, self._check_messages)  # ~30fps UI updates (smoother preview)

    def _close_preview_window(self):
        """Close the preview window if it exists."""
        with self._preview_lock:
            self._latest_preview = None
        if hasattr(self, 'preview_window') and self.preview_window:
            try:
                self.preview_window.destroy()
            except tk.TclError:
                # Window already destroyed
                pass
            self.preview_window = None

    def _on_processing_complete(self, summary, df):
        self.processing = False
        self.process_btn.config(text="Start Processing")
        self.progress_var.set(100)
        self.results_summary = summary
        self.results_df = df
        self.export_btn.config(state="normal")

        # Close preview window
        self._close_preview_window()

        # Show summary
        zones_info = ", ".join([f"{k}: {v:.1f}s" for k, v in summary["seconds_per_zone"].items()])
        self.progress_label.config(text=f"Complete! Duration: {summary['duration_s']}s")
        self._set_status(f"Processing complete. Time per zone: {zones_info}")

        messagebox.showinfo("Processing Complete",
                           f"Video analyzed successfully!\n\n"
                           f"Duration: {summary['duration_s']} seconds\n"
                           f"Frames: {summary['total_frames']}\n"
                           f"Computed on: {getattr(self, '_compute_backend_text', self._compute_backend())}\n"
                           f"{self._kinematics_summary_text(summary)}\n"
                           f"Click 'Export to Excel' to save results.")

        # Show heatmap window
        if hasattr(self, 'current_arena') and hasattr(self, 'current_zones'):
            video_name = os.path.basename(self.video_path) if self.video_path else "Video"
            results_list = [(summary, df, video_name)]
            HeatmapWindow(self.root, results_list, self.current_arena, self.current_zones)

    def _on_batch_complete(self, all_results, failures=None):
        """Handle completion of batch processing, reporting any per-video failures."""
        failures = failures or []
        self.processing = False
        self.process_btn.config(text="Start Processing")
        self.progress_var.set(100)
        self._close_preview_window()

        fail_lines = "\n".join(f"• {name}: {err}" for name, err in failures[:8])
        if len(failures) > 8:
            fail_lines += f"\n• …and {len(failures) - 8} more"

        if not all_results:
            # Every video failed — nothing to export.
            self.batch_results = []
            self.export_btn.config(state="disabled")
            self.progress_label.config(text=f"All {len(failures)} videos failed")
            self._set_status("Batch failed: no videos could be processed.")
            messagebox.showerror(
                "Batch Processing Failed",
                f"None of the {len(failures)} videos could be processed.\n\n{fail_lines}")
            return

        self.batch_results = all_results
        self.export_btn.config(state="normal")

        total_duration = sum(r[0]["duration_s"] for r in all_results)
        total_frames = sum(r[0]["total_frames"] for r in all_results)
        succeeded = len(all_results)

        if failures:
            self.progress_label.config(text=f"Done: {succeeded} ok, {len(failures)} failed")
            self._set_status(f"Batch finished with errors: {succeeded} succeeded, {len(failures)} failed.")
            messagebox.showwarning(
                "Batch Complete — With Errors",
                f"{succeeded} video(s) succeeded; {len(failures)} FAILED.\n\n"
                f"Failed:\n{fail_lines}\n\n"
                f"The export and heatmap below include only the {succeeded} successful video(s).")
        else:
            self.progress_label.config(text=f"Complete! {succeeded} videos processed")
            self._set_status(f"Batch complete: {succeeded} videos, {total_duration:.1f}s total")
            messagebox.showinfo(
                "Batch Processing Complete",
                f"All videos analyzed successfully!\n\n"
                f"Videos processed: {succeeded}\n"
                f"Total duration: {total_duration:.1f} seconds\n"
                f"Total frames: {total_frames}\n"
                f"Computed on: {getattr(self, '_compute_backend_text', self._compute_backend())}\n\n"
                f"Click 'Export to Excel' to save results.")

        # Show heatmap window with the successful videos
        if hasattr(self, 'current_arena') and hasattr(self, 'current_zones'):
            HeatmapWindow(self.root, all_results, self.current_arena, self.current_zones)

    def _on_processing_cancelled(self):
        self.processing = False
        self.process_btn.config(text="Start Processing")
        self.progress_label.config(text="Cancelled")
        self._set_status("Processing cancelled.")
        self._close_preview_window()

    def _on_processing_error(self, error_msg):
        self.processing = False
        self.process_btn.config(text="Start Processing")
        self.progress_label.config(text="Error occurred")
        self._set_status(f"Error: {error_msg}")
        self._close_preview_window()
        messagebox.showerror("Processing Error", f"An error occurred:\n{error_msg}")

    def _export_results(self):
        # Check if we have results (single or batch)
        has_single = self.results_summary is not None and self.results_df is not None
        has_batch = len(self.batch_results) > 0

        if not has_single and not has_batch:
            messagebox.showwarning("No Results", "Please process a video first.")
            return

        # Get save path
        if has_batch:
            default_name = f"batch_tracking_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        else:
            default_name = f"tracking_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        path = filedialog.asksaveasfilename(
            title="Save Results",
            defaultextension=".xlsx",
            initialfile=default_name,
            filetypes=[("Excel files", "*.xlsx"), ("All files", "*.*")]
        )

        if not path:
            return

        try:
            self._write_excel(path)
            self._set_status(f"Results exported to {os.path.basename(path)}")
            messagebox.showinfo("Export Complete", f"Results saved to:\n{path}")
        except Exception as e:
            messagebox.showerror("Export Error", f"Failed to save results:\n{e}")

    def _write_excel(self, path: str):
        """Write formatted Excel file with results (single or batch).

        Any batch results route to the batch writer — including a batch where only
        one video succeeded (results_summary is None in that case, so the single
        writer would crash). Single-video runs leave batch_results empty.

        Written atomically: the workbook is built in a temp file in the same folder
        and renamed into place only on success, so a crash or error mid-write can
        never leave a half-written (corrupt) .xlsx — or clobber an existing one.
        """
        is_batch = len(self.batch_results) > 0
        directory = os.path.dirname(os.path.abspath(path))
        fd, tmp_path = tempfile.mkstemp(suffix=".xlsx", dir=directory)
        os.close(fd)
        try:
            with pd.ExcelWriter(tmp_path, engine="openpyxl") as writer:
                if is_batch:
                    self._write_batch_excel(writer)
                else:
                    self._write_single_excel(writer)

                # Format all worksheets
                workbook = writer.book
                for sheet_name in workbook.sheetnames:
                    worksheet = workbook[sheet_name]
                    for column in worksheet.columns:
                        max_length = 0
                        column_letter = column[0].column_letter
                        for cell in column:
                            try:
                                if len(str(cell.value)) > max_length:
                                    max_length = len(str(cell.value))
                            except (TypeError, AttributeError):
                                pass
                        adjusted_width = min(max_length + 2, 50)
                        worksheet.column_dimensions[column_letter].width = adjusted_width
            os.replace(tmp_path, path)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    def _write_single_excel(self, writer):
        """Write Excel for single video results."""
        summary = self.results_summary
        df = self.results_df

        s = self.settings

        # Summary sheet
        summary_data = {
            "Metric": ["Video File", "FPS", "Total Frames", "Duration (s)",
                       "Analyzed From (s)", "Analyzed To (s)"],
            "Value": [excel_safe_text(summary["video"]), summary["fps"], summary["total_frames"],
                      summary["duration_s"], summary.get("analyzed_from_s", 0),
                      summary.get("analyzed_to_s", summary["duration_s"])]
        }
        q = summary.get("tracking_quality")
        if q:
            summary_data["Metric"] += ["Tracking Method", "Frames Detected", "Frames Interpolated",
                                       "Frames Held", "Frames Unresolved", "Time Base",
                                       "Reconstruction"]
            summary_data["Value"] += [q.get("tracking_method", "refsub"),
                                      q.get("detected", 0), q.get("interpolated", 0),
                                      q.get("held", 0), q.get("unresolved", 0),
                                      q.get("time_base", "nominal_fps"),
                                      q.get("reconstruction", "straight_line_between_detections")]
        for zone_name in summary["seconds_per_zone"]:
            if s.measure_time_per_zone:
                summary_data["Metric"].append(f"Time in {zone_name} (s)")
                summary_data["Value"].append(summary["seconds_per_zone"][zone_name])
            if s.measure_zone_entries:
                summary_data["Metric"].append(f"Entries to {zone_name}")
                summary_data["Value"].append(summary["entries_per_zone"][zone_name])

        self._append_kinematics_rows(summary_data["Metric"], summary_data["Value"], summary)

        pd.DataFrame(summary_data).to_excel(writer, sheet_name="Summary", index=False)
        guard_excel_frame(df).to_excel(writer, sheet_name="Frame Data", index=False)

        # Zone statistics (only if a zone-level measurement is enabled)
        if s.measure_time_per_zone or s.measure_zone_entries:
            zone_stats = []
            for zone_name in summary["seconds_per_zone"]:
                row = {"Zone": excel_safe_text(zone_name)}
                if s.measure_time_per_zone:
                    row["Time (s)"] = summary["seconds_per_zone"][zone_name]
                    row["Time (%)"] = round(summary["seconds_per_zone"][zone_name]
                                            / summary["duration_s"] * 100, 2) if summary["duration_s"] > 0 else 0
                if s.measure_zone_entries:
                    row["Entries"] = summary["entries_per_zone"][zone_name]
                zone_stats.append(row)
            pd.DataFrame(zone_stats).to_excel(writer, sheet_name="Zone Statistics", index=False)

    def _append_kinematics_rows(self, metrics, values, summary):
        """Append calibration + kinematic metrics (distance/speed) to a
        Metric/Value summary, honoring the measurement toggles. No-op if the run
        wasn't calibrated."""
        s = self.settings
        cal = summary.get("calibration")
        kin = summary.get("kinematics")
        if not cal or not kin:
            return
        unit = cal.get("unit", "cm")
        metrics.extend([f"Scale ({unit}/px)", "Scale lines",
                        "Scale agreement (%)", "Calibration model"])
        values.extend([cal.get("units_per_px", ""), cal.get("scale_lines", ""),
                       cal.get("scale_agreement_pct", ""),
                       cal.get("model", "scale_lines_uniform")])
        metrics.append("Kinematics Resolved")
        values.append(kin.get("trajectory_resolved", True))
        if not kin.get("trajectory_resolved", True):
            return
        if s.measure_distance:
            metrics.extend([f"Distance Traveled ({unit})",
                            f"Direct-detection Segment Distance ({unit})",
                            f"Reconstructed Segment Distance ({unit})"])
            values.extend([kin["distance_traveled"], kin["measured_distance"],
                           kin["reconstructed_distance"]])
        if s.measure_speed:
            metrics.extend([f"Mean Speed ({unit}/s)", f"Max Frame Speed ({unit}/s)",
                            "Kinematic Elapsed Time (s)"])
            values.extend([kin["mean_speed"], kin["max_speed"], kin["kinematic_elapsed_s"]])

    def _kinematics_summary_text(self, summary) -> str:
        """One-line-per-metric kinematics summary for the completion dialog ('' if none)."""
        s = self.settings
        cal = summary.get("calibration")
        kin = summary.get("kinematics")
        if not cal or not kin:
            return ""
        if not kin.get("trajectory_resolved", True):
            return "Kinematics unavailable: fewer than two direct detections.\n"
        unit = cal.get("unit", "cm")
        parts = []
        if s.measure_distance:
            parts.append(f"Distance: {kin['distance_traveled']:.1f} {unit}")
        if s.measure_speed:
            parts.append(f"Mean speed: {kin['mean_speed']:.2f} {unit}/s "
                         f"(max {kin['max_speed']:.2f})")
        return ("\n".join(parts) + "\n") if parts else ""

    def _write_batch_excel(self, writer):
        """Write Excel for batch video results with aggregated data."""
        all_results = self.batch_results

        s = self.settings

        # 1. Aggregated Summary sheet
        agg_data = []
        for summary, df, filename in all_results:
            row = {"Video": excel_safe_text(filename), "FPS": summary["fps"],
                   "Frames": summary["total_frames"], "Duration (s)": summary["duration_s"]}
            for zone_name in summary["seconds_per_zone"]:
                if s.measure_time_per_zone:
                    row[f"{zone_name} Time (s)"] = summary["seconds_per_zone"][zone_name]
                if s.measure_zone_entries:
                    row[f"{zone_name} Entries"] = summary["entries_per_zone"][zone_name]
            cal = summary.get("calibration")
            kin = summary.get("kinematics")
            if cal and kin:
                unit = cal.get("unit", "cm")
                row["Kinematics Resolved"] = kin.get("trajectory_resolved", True)
                if not kin.get("trajectory_resolved", True):
                    agg_data.append(row)
                    continue
                if s.measure_distance:
                    row[f"Distance ({unit})"] = kin["distance_traveled"]
                    row[f"Direct Segment Distance ({unit})"] = kin["measured_distance"]
                    row[f"Reconstructed Segment Distance ({unit})"] = kin["reconstructed_distance"]
                if s.measure_speed:
                    row[f"Mean Speed ({unit}/s)"] = kin["mean_speed"]
                    row[f"Max Frame Speed ({unit}/s)"] = kin["max_speed"]
            agg_data.append(row)

        df_agg = pd.DataFrame(agg_data)
        guard_excel_frame(df_agg).to_excel(writer, sheet_name="All Videos Summary", index=False)

        # 2. Aggregated Zone Statistics
        zone_names = list(all_results[0][0]["seconds_per_zone"].keys())
        zone_totals = {z: {"time": 0, "entries": 0} for z in zone_names}
        total_duration = 0

        for summary, df, filename in all_results:
            total_duration += summary["duration_s"]
            for zone_name in zone_names:
                zone_totals[zone_name]["time"] += summary["seconds_per_zone"].get(zone_name, 0)
                zone_totals[zone_name]["entries"] += summary["entries_per_zone"].get(zone_name, 0)

        if s.measure_time_per_zone or s.measure_zone_entries:
            zone_stats = []
            for zone_name in zone_names:
                row = {"Zone": excel_safe_text(zone_name)}
                if s.measure_time_per_zone:
                    row["Total Time (s)"] = round(zone_totals[zone_name]["time"], 2)
                    row["Avg Time (s)"] = round(zone_totals[zone_name]["time"] / len(all_results), 2)
                    row["Time (%)"] = round(zone_totals[zone_name]["time"] / total_duration * 100, 2) if total_duration > 0 else 0
                if s.measure_zone_entries:
                    row["Total Entries"] = zone_totals[zone_name]["entries"]
                    row["Avg Entries"] = round(zone_totals[zone_name]["entries"] / len(all_results), 2)
                zone_stats.append(row)
            pd.DataFrame(zone_stats).to_excel(writer, sheet_name="Aggregated Zone Stats", index=False)

        # 3. Combined Frame Data (all videos)
        all_frames = []
        for summary, df, filename in all_results:
            df_copy = df.copy()
            df_copy.insert(0, "video", filename)
            all_frames.append(df_copy)
        guard_excel_frame(pd.concat(all_frames, ignore_index=True)).to_excel(
            writer, sheet_name="All Frame Data", index=False)

        # 4. Individual sheets for each video (valid, unique, ≤31-char sheet names)
        used_sheets = {"all videos summary", "aggregated zone stats", "all frame data"}
        for summary, df, filename in all_results:
            sheet_name = safe_excel_sheet_name(filename, used_sheets)
            guard_excel_frame(df).to_excel(writer, sheet_name=sheet_name, index=False)

    def _set_status(self, message: str):
        self.status_label.config(text=message)

    # ----- Calibration / measurement profile (persistent across runs) -----

    def _on_arena_complete(self):
        """After the arena is drawn, refresh status then ask for each side's length."""
        self._on_polygon_complete()   # region status + zone list + cal status
        self._ask_side_lengths()

    def _ask_side_lengths(self):
        """Pop the per-side length dialog for the current arena, numbered to match."""
        n = self.canvas.num_arena_sides()
        if n == 0:
            return
        init = self.canvas.get_arena_side_lengths()
        dlg = SideLengthsDialog(self.root, n, init, self.canvas.get_scale_unit())
        self.root.wait_window(dlg)
        if dlg.confirmed:
            self._scale_unit = dlg.unit or "cm"
            self.canvas.set_arena_side_lengths(dlg.values, self._scale_unit)
        self._update_cal_status()

    def _update_cal_status(self):
        """Refresh the calibration status line under the Arena step from the arena sides."""
        if not hasattr(self, "canvas"):
            return
        if self.canvas.get_arena() is None:
            self.cal_status.config(text="Calibration: draw the arena, then set its side lengths")
            return
        n_sides = self.canvas.num_arena_sides()
        lines = self.canvas.get_scale_lines()
        if not lines:
            self.cal_status.config(
                text="Calibration: set side lengths to enable distance/speed (Edit Dimensions)")
            return
        info = compute_scale_from_lines(lines)
        agree = "" if info["n_lines"] < 2 else f", ±{info['spread_pct']:.0f}%"
        self.cal_status.config(
            text=(f"Calibration: {info['n_lines']}/{n_sides} sides → "
                  f"{info['units_per_px']:.4f} {info['unit']}/px{agree}"))


# -----------------------------
# Frame Picker Dialog
# -----------------------------

class FramePickerDialog(tk.Toplevel):
    def __init__(self, parent, video_path: str):
        super().__init__(parent)
        self.title("Select Frame")
        self.configure(bg=COLORS["bg_dark"])

        self.video_path = video_path
        self.selected_frame = None
        self.selected_frame_idx = 0
        self.current_frame = None  # Initialize to prevent AttributeError

        self.cap = cv2.VideoCapture(video_path)
        self.total_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT) or 1)
        self.fps = float(self.cap.get(cv2.CAP_PROP_FPS) or 30.0)

        # Get video dimensions for smart sizing
        video_w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 640)
        video_h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 480)

        # Calculate window size based on screen and video aspect ratio
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        max_w = int(screen_w * 0.7)
        max_h = int(screen_h * 0.7)

        # Scale video to fit, maintaining aspect ratio
        scale = min(max_w / video_w, (max_h - 150) / video_h)  # Reserve 150px for controls
        win_w = max(640, int(video_w * scale))
        win_h = max(480, int(video_h * scale) + 150)

        # Center on screen
        x = (screen_w - win_w) // 2
        y = (screen_h - win_h) // 2
        self.geometry(f"{win_w}x{win_h}+{x}+{y}")
        self.minsize(640, 480)

        self._build_ui()
        self._show_frame(0)

        self.transient(parent)
        self.grab_set()

    def _build_ui(self):
        # Canvas
        self.canvas = tk.Canvas(self, bg=COLORS["bg_medium"], highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=10, pady=10)

        # Controls
        control_frame = ModernFrame(self)
        control_frame.pack(fill="x", padx=10, pady=10)

        # Frame slider
        self.frame_var = tk.IntVar(value=0)
        self.slider = ttk.Scale(control_frame, from_=0, to=max(1, self.total_frames - 1),
                                variable=self.frame_var, orient="horizontal",
                                command=self._on_slider_change)
        self.slider.pack(fill="x", pady=5)

        # Info and buttons
        info_frame = ModernFrame(control_frame)
        info_frame.pack(fill="x", pady=5)

        self.info_label = ModernLabel(info_frame, text="Frame: 0 | Time: 0:00", style="normal")
        self.info_label.pack(side="left")

        ModernButton(info_frame, "Select This Frame", self._select_frame, style="primary").pack(side="right")
        ModernButton(info_frame, "Cancel", self.destroy, style="secondary").pack(side="right", padx=5)

        # Navigation buttons
        nav_frame = ModernFrame(control_frame)
        nav_frame.pack(fill="x", pady=5)

        for text, delta in [("<<", -100), ("<", -10), (">", 10), (">>", 100)]:
            ModernButton(nav_frame, text, lambda d=delta: self._navigate(d), style="secondary").pack(side="left", padx=2)

    def _on_slider_change(self, value):
        frame_idx = int(float(value))
        self._show_frame(frame_idx)

    def _navigate(self, delta):
        new_idx = max(0, min(self.total_frames - 1, self.frame_var.get() + delta))
        self.frame_var.set(new_idx)
        self._show_frame(new_idx)

    def _show_frame(self, frame_idx: int):
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ok, frame = self.cap.read()

        if ok and frame is not None:
            self.current_frame = frame
            self.selected_frame_idx = frame_idx

            # Display
            canvas_w = self.canvas.winfo_width()
            canvas_h = self.canvas.winfo_height()
            if canvas_w > 1 and canvas_h > 1:
                h, w = frame.shape[:2]
                scale = min(canvas_w / w, canvas_h / h)
                new_w, new_h = int(w * scale), int(h * scale)

                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                resized = cv2.resize(rgb, (new_w, new_h))
                self.photo = ImageTk.PhotoImage(Image.fromarray(resized))

                self.canvas.delete("all")
                self.canvas.create_image(canvas_w // 2, canvas_h // 2, anchor="center", image=self.photo)

            # Update info
            time_s = frame_idx / self.fps
            self.info_label.config(text=f"Frame: {frame_idx}/{self.total_frames} | Time: {format_time(time_s)}")

    def _select_frame(self):
        if self.current_frame is None:
            # No valid frame was loaded, cannot select
            return
        self.selected_frame = self.current_frame
        self.cap.release()
        self.destroy()

    def destroy(self):
        if hasattr(self, 'cap') and self.cap.isOpened():
            self.cap.release()
        super().destroy()


# -----------------------------
# Zone Name Dialog
# -----------------------------

class ZoneNameDialog(tk.Toplevel):
    def __init__(self, parent, zone_number: int, initial_name: str = None):
        super().__init__(parent)
        self.title("Zone Name")
        self.configure(bg=COLORS["bg_dark"])
        self.resizable(False, False)

        self.zone_name = None

        ModernLabel(self, text="Enter Zone Name", style="heading").pack(pady=15)

        self.entry = ModernEntry(self, width=30)
        self.entry.insert(0, initial_name if initial_name else f"Zone {zone_number}")
        self.entry.pack(fill="x", padx=20, pady=10)
        self.entry.select_range(0, "end")
        self.entry.focus()

        btn_frame = ModernFrame(self)
        btn_frame.pack(pady=15)

        ModernButton(btn_frame, "OK", self._confirm, style="primary").pack(side="left", padx=5)
        ModernButton(btn_frame, "Cancel", self.destroy, style="secondary").pack(side="left", padx=5)

        self.bind("<Return>", lambda e: self._confirm())
        self.bind("<Escape>", lambda e: self.destroy())

        self.transient(parent)
        self.grab_set()

        # Auto-size to fit content and center on parent
        self.update_idletasks()
        self.minsize(self.winfo_width(), self.winfo_height())
        x = parent.winfo_x() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")

    def _confirm(self):
        name = self.entry.get().strip()
        if name:
            self.zone_name = name
        self.destroy()


class SideLengthsDialog(tk.Toplevel):
    """Asked after the arena is drawn: the real length of each numbered side."""

    def __init__(self, parent, n_sides, init_values=None, init_unit="cm"):
        super().__init__(parent)
        self.title("Arena Side Lengths")
        self.configure(bg=COLORS["bg_dark"])
        self.resizable(False, False)

        self.confirmed = False
        self.values = list(init_values) if init_values else [""] * n_sides
        self.unit = init_unit or "cm"

        ModernLabel(self, text="Real length of each side", style="heading").pack(pady=(15, 4), padx=20)
        ModernLabel(self, text=(f"You drew {n_sides} sides — they're numbered on the video. Type the "
                                "REAL length of the sides you know (what a ruler would read). Leave a "
                                "side blank if you don't know it; the ones you fill in are averaged "
                                "into the scale. Skip all for zone-only (time / entries)."),
                    style="small", fg=COLORS["text_dim"], wraplength=360, justify="left").pack(padx=20)

        grid = ModernFrame(self)
        grid.pack(pady=12, padx=20)
        self.entries = []
        ncols = 1 if n_sides <= 6 else 2
        nrows = math.ceil(n_sides / ncols)
        for i in range(n_sides):
            r, c = i % nrows, i // nrows
            ModernLabel(grid, text=f"Side {i+1}:", style="small").grid(
                row=r, column=c * 2, sticky="e", padx=(10, 3), pady=3)
            e = ModernEntry(grid, width=8)
            if i < len(self.values) and self.values[i]:
                e.insert(0, str(self.values[i]))
            e.grid(row=r, column=c * 2 + 1, sticky="w", padx=(0, 12), pady=3)
            self.entries.append(e)

        unit_row = ModernFrame(self)
        unit_row.pack(pady=(0, 6), padx=20)
        ModernLabel(unit_row, text="Unit (all sides):", style="small").pack(side="left")
        self.unit_var = tk.StringVar(value=self.unit)
        ttk.Combobox(unit_row, textvariable=self.unit_var, values=["mm", "cm", "m", "in"],
                     width=4, state="readonly").pack(side="left", padx=6)

        btn = ModernFrame(self)
        btn.pack(pady=(4, 15))
        ModernButton(btn, "OK", self._confirm, style="primary").pack(side="left", padx=5)
        ModernButton(btn, "Skip", self.destroy, style="secondary").pack(side="left", padx=5)

        if self.entries:
            self.entries[0].focus()
        self.bind("<Return>", lambda e: self._confirm())
        self.bind("<Escape>", lambda e: self.destroy())
        self.transient(parent)
        self.grab_set()

        self.update_idletasks()
        self.minsize(self.winfo_width(), self.winfo_height())
        x = parent.winfo_x() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")

    def _confirm(self):
        self.values = [e.get().strip() for e in self.entries]
        self.unit = self.unit_var.get() or "cm"
        self.confirmed = True
        self.destroy()


# -----------------------------
# Main Entry Point
# -----------------------------

def set_window_icon(root):
    """Set the application window icon for taskbar and title bar."""
    try:
        # Try to load icon.png from various locations
        icon_paths = [
            Path(__file__).parent / "assets" / "icon.png",  # source layout
            Path(get_resource_path("assets/icon.png")),     # PyInstaller bundle
            Path.cwd() / "assets" / "icon.png",             # current working dir
        ]

        icon_path = None
        for p in icon_paths:
            if p.exists():
                icon_path = p
                break

        if icon_path:
            # Load icon using PIL and convert to PhotoImage
            icon_image = Image.open(icon_path)
            # Create multiple sizes for better display
            icon_sizes = []
            for size in [16, 32, 48, 64, 128, 256]:
                resized = icon_image.resize((size, size), Image.Resampling.LANCZOS)
                icon_sizes.append(ImageTk.PhotoImage(resized))

            # Set window icon (pass multiple sizes)
            root.iconphoto(True, *icon_sizes)
    except Exception as e:
        # If icon loading fails, continue without custom icon
        print(f"Could not load icon: {e}")


def main():
    # Configure ttk style
    root = tk.Tk()

    # Set window icon for taskbar and title bar
    set_window_icon(root)

    style = ttk.Style()
    style.theme_use("clam")

    # Configure colors for ttk widgets (light theme)
    style.configure("TScale", background=COLORS["bg_dark"], troughcolor=COLORS["border"])
    style.configure("TProgressbar", background=COLORS["accent"], troughcolor=COLORS["border"])
    style.configure("TCombobox", fieldbackground=COLORS["bg_medium"], background=COLORS["bg_light"])
    style.configure("TSeparator", background=COLORS["border"])
    style.configure("TFrame", background=COLORS["bg_dark"])

    root._cordinsight_app = CordInsightApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
