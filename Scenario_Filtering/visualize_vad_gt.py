#!/usr/bin/env python3
"""Generate and visualize VAD vector-map GT.

The primary mode reads corrected Bench2Drive annotations and a Town HD map
directly. It writes frame-level numeric vector GT as compressed NPZ files and
matching BEV QA images without creating an intermediate PKL.

Shows in BEV/LiDAR coordinates:
- current agent 3D boxes (BEV footprint)
- persistent actor IDs
- agent future trajectories (derived from gt_ids + npc2world)
- ego future trajectory (derived from world2lidar of future frames)
- VAD-B2D vector-map source elements inside the official ROI
- traffic-control objects stored in legacy info data, if present

The legacy ``--infos``/``--map-infos`` mode remains available for debugging.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import pickle
import re
from pathlib import Path
from typing import Any

import cv2
import numpy as np


MAP_ELEMENT_CLASS = {
    "Broken": 0,
    "Solid": 1,
    "SolidSolid": 2,
    "Center": 3,
    "TrafficLight": 4,
    "StopSign": 5,
}
DEFAULT_PC_RANGE = np.array(
    [-51.2, -51.2, -5.0, 51.2, 51.2, 3.0], dtype=float
)
DEFAULT_POLYLINE_POINTS = 20

LEFT2RIGHT = np.eye(4, dtype=float)
LEFT2RIGHT[1, 1] = -1.0
LEFTHAND_EGO_TO_LIDAR = np.array(
    [[0, 1, 0, 0], [1, 0, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
    dtype=float,
)

MAP_STYLE = {
    "Broken": {"dashed": True, "double": False, "color": (220, 120, 30)},
    "Solid": {"dashed": False, "double": False, "color": (0, 145, 255)},
    "SolidSolid": {"dashed": False, "double": True, "color": (40, 40, 220)},
    "Center": {"dashed": False, "double": False, "color": (45, 160, 45)},
    "TrafficLight": {"dashed": False, "double": False, "color": (180, 80, 220)},
    "StopSign": {"dashed": False, "double": False, "color": (150, 60, 140)},
}

def resample_polyline(points: np.ndarray, fixed_num: int = DEFAULT_POLYLINE_POINTS) -> np.ndarray:
    """Arc-length interpolation used only to visualize the fixed_num=20 target points."""
    pts = np.asarray(points, dtype=float)
    if len(pts) < 2:
        return pts.copy()
    seg = np.linalg.norm(pts[1:] - pts[:-1], axis=1)
    cumulative = np.concatenate([[0.0], np.cumsum(seg)])
    total = float(cumulative[-1])
    if total <= 1e-9:
        return np.repeat(pts[:1], fixed_num, axis=0)
    targets = np.linspace(0.0, total, fixed_num)
    result = np.empty((fixed_num, 2), dtype=float)
    result[:, 0] = np.interp(targets, cumulative, pts[:, 0])
    result[:, 1] = np.interp(targets, cumulative, pts[:, 1])
    return result


def contiguous_true_runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Return (start, end) index pairs (end exclusive) for each contiguous True run."""
    runs = []
    n = len(mask)
    i = 0
    while i < n:
        if mask[i]:
            j = i + 1
            while j < n and mask[j]:
                j += 1
            runs.append((i, j))
            i = j
        else:
            i += 1
    return runs


def load_pickle(path: Path) -> Any:
    with path.open("rb") as f:
        return pickle.load(f)


def load_annotation(path: Path) -> dict[str, Any]:
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as file:
        return json.load(file)


def frame_number(path: Path) -> int:
    match = re.search(r"(\d+)", path.name)
    if match is None:
        raise ValueError(f"frame number not found: {path.name}")
    return int(match.group(1))


def normalize_map_points(raw: Any, lane_mode: bool) -> np.ndarray:
    points = []
    if not isinstance(raw, (list, tuple, np.ndarray)):
        return np.zeros((0, 3), dtype=float)
    for item in raw:
        value = item
        if lane_mode and isinstance(value, (list, tuple, np.ndarray)) and len(value):
            if isinstance(value[0], (list, tuple, np.ndarray)):
                value = value[0]
        array = np.asarray(value, dtype=float).reshape(-1)
        if array.size >= 3:
            points.append(array[:3])
    return np.asarray(points, dtype=float).reshape(-1, 3)


def load_raw_map_info(map_file: Path, town: str) -> dict[str, dict[str, Any]]:
    raw_map = dict(np.load(map_file, allow_pickle=True)["arr"])
    lane_points, lane_samples, lane_types = [], [], []
    trigger_points, trigger_samples, trigger_types = [], [], []
    for road in raw_map.values():
        if not isinstance(road, dict):
            continue
        for lane_id, lane in road.items():
            if not isinstance(lane, (list, tuple, np.ndarray)):
                continue
            for item in lane:
                if not isinstance(item, dict):
                    continue
                is_trigger = lane_id == "Trigger_Volumes"
                points = normalize_map_points(
                    item.get("Points", []), lane_mode=not is_trigger)
                if len(points) == 0:
                    continue
                points[:, 1] *= -1.0
                typ = str(item.get("Type", "Unknown"))
                if is_trigger:
                    trigger_points.append(points)
                    trigger_samples.append(points.mean(axis=0))
                    trigger_types.append(typ)
                else:
                    lane_points.append(points)
                    indices = list(range(0, len(points), 50))
                    if not indices or indices[-1] != len(points) - 1:
                        indices.append(len(points) - 1)
                    lane_samples.append(points[indices])
                    lane_types.append(typ)

    return {town: {
        "lane_points": lane_points,
        "lane_sample_points": lane_samples,
        "lane_types": lane_types,
        "trigger_volumes_points": trigger_points,
        "trigger_volumes_sample_points": trigger_samples,
        "trigger_volumes_types": trigger_types,
    }}


def load_map_infos_pkl(pkl_path: Path, town: str) -> dict[str, dict[str, Any]]:
    """Load a single Town entry from prepare_b2d_infos.py's map-PKL output.

    ``b2d_map_infos.pkl`` already stores per-town ``lane_points``/``lane_types``/
    ``trigger_volumes_points`` in the exact shape ``map_vectors`` expects, so no
    extra normalization is needed here -- unlike ``load_raw_map_info``, which
    still parses the raw ``Town*_HD_map.npz``.
    """
    with pkl_path.open("rb") as f:
        raw_map_infos = pickle.load(f)
    if town not in raw_map_infos:
        raise KeyError(f"town {town!r} not found in {pkl_path}")
    return {town: raw_map_infos[town]}


def detect_town(
    anno_dir: Path, map_file: Path | None, scenario_name: str | None
) -> str:
    candidates = []
    if map_file is not None:
        candidates.append(map_file.stem)
    if scenario_name:
        candidates.append(scenario_name)
    candidates.append(anno_dir.name)
    candidates.append(anno_dir.parent.name)
    for candidate in candidates:
        match = re.search(r"Town\d+", candidate, re.IGNORECASE)
        if match is not None:
            return "Town" + re.search(r"\d+", match.group(0)).group(0)
    raise ValueError(f"Town name not found near: {anno_dir}")


def raw_infos_from_annotations(
    anno_dir: Path,
    town: str,
    scenario_name: str,
) -> list[dict[str, Any]]:
    paths = sorted(
        (path for path in anno_dir.iterdir()
         if path.name.endswith(".json") or path.name.endswith(".json.gz")),
        key=frame_number,
    )
    infos = []
    for path in paths:
        annotation = load_annotation(path)
        raw_world2lidar = np.asarray(
            annotation["sensors"]["LIDAR_TOP"]["world2lidar"], dtype=float)
        world2lidar = LEFTHAND_EGO_TO_LIDAR @ raw_world2lidar @ LEFT2RIGHT
        infos.append({
            "folder": scenario_name,
            "scenario_name": scenario_name,
            "town_name": town,
            "frame_idx": frame_number(path),
            "command_near": annotation.get("command_near"),
            "sensors": {"LIDAR_TOP": {"world2lidar": world2lidar}},
            "gt_boxes": np.zeros((0, 9), dtype=float),
            "gt_ids": np.zeros((0,), dtype=np.int64),
            "num_points": np.zeros((0,), dtype=np.int64),
            "npc2world": np.zeros((0, 4, 4), dtype=float),
            "traffic_controls": {},
        })
    return infos


def save_vector_gt(
    info: dict[str, Any],
    map_infos: dict[str, Any],
    pc_range: np.ndarray,
    output: Path,
) -> int:
    vectors = map_vectors(info, map_infos, pc_range)
    points = []
    labels = []
    types = []
    closed = []
    for typ, xy, is_closed in vectors:
        sampled = resample_polyline(xy, DEFAULT_POLYLINE_POINTS)
        if sampled.shape != (DEFAULT_POLYLINE_POINTS, 2):
            continue
        points.append(sampled.astype(np.float32))
        labels.append(MAP_ELEMENT_CLASS[typ])
        types.append(typ)
        closed.append(is_closed)
    output.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        output,
        points=(np.stack(points) if points else
                np.zeros((0, DEFAULT_POLYLINE_POINTS, 2), dtype=np.float32)),
        labels=np.asarray(labels, dtype=np.int64),
        types=np.asarray(types, dtype="U32"),
        closed=np.asarray(closed, dtype=np.bool_),
        frame_idx=np.asarray(int(info["frame_idx"]), dtype=np.int64),
        town=np.asarray(str(info["town_name"])),
        pc_range=np.asarray(pc_range, dtype=np.float32),
    )
    return len(points)


def shift_permutation_variants(points: np.ndarray, closed: bool) -> np.ndarray:
    """Reconstruct MapTR/VAD permutation-equivalent GT variants from a saved instance.

    Mirrors Bench2DriveZoo's ``LiDARInstanceLines.shift_fixed_num_sampled_points_v2``
    (``map_gt_shift_pts_pattern='v2'`` in ``VAD_base_e2e_b2d.py``, the variant the
    official config actually trains with). A training-time Dataset should call
    this on ``points``/``closed`` loaded from the NPZ instead of baking every
    shift into the file.

    v2's open-line branch resamples to ``fixed_num`` first and only ever needs
    the forward and reversed order of those points, so it is exactly
    reproducible from the saved ``points`` array with no extra data.

    v2's closed-polygon branch instead rolls the *raw, pre-resample* polygon
    vertices and re-resamples each roll -- that is not reconstructible from
    ``points`` alone. This pipeline always emits ``closed=False`` now (trigger
    volumes are stored as open stop-line segments, not polygons), so that
    branch should never be hit; raise loudly instead of silently returning
    wrong variants if it ever is.
    """
    if closed:
        raise NotImplementedError(
            "closed=True has no saved raw vertices to reproduce v2's "
            "polygon-roll shift variants; this pipeline should not emit "
            "closed instances any more"
        )
    pts = np.asarray(points, dtype=np.float32)
    return np.stack([pts, pts[::-1]], axis=0)


def choose_index(infos: list[dict[str, Any]], sample: int | None, folder: str | None, frame: int | None) -> int:
    if folder is not None or frame is not None:
        for i, info in enumerate(infos):
            ok_folder = folder is None or folder in str(info["folder"])
            ok_frame = frame is None or int(info["frame_idx"]) == frame
            if ok_folder and ok_frame:
                return i
        raise ValueError("requested folder/frame not found")
    if sample is None:
        sample = 0
    if not (0 <= sample < len(infos)):
        raise IndexError(f"sample out of range: {sample}, len={len(infos)}")
    return sample


def box_corners(box: np.ndarray) -> np.ndarray:
    """Return BEV corners using mmdet3d's LiDAR box yaw convention.

    ``gt_boxes`` is consumed as ``LiDARInstance3DBoxes``. Its positive yaw
    rotates clockwise when drawn on this (x right, y up) BEV plane, so the
    row-vector rotation below intentionally differs from the usual
    counter-clockwise Cartesian matrix.
    """
    x, y = float(box[0]), float(box[1])
    w, l = float(box[3]), float(box[4])
    yaw = float(box[6])
    local = np.array(
        [
            [ w / 2,  l / 2],
            [ w / 2, -l / 2],
            [-w / 2, -l / 2],
            [-w / 2,  l / 2],
            [ w / 2,  l / 2],
        ]
    )
    c, s = math.cos(yaw), math.sin(yaw)
    rot = np.array([[c, s], [-s, c]])
    return local @ rot.T + np.array([x, y])



def normalize_angle_deg(angle_deg: float) -> float:
    """Normalize degrees to [-180, 180)."""
    value = (angle_deg + 180.0) % 360.0 - 180.0
    return value


def compute_box_long_axis_heading(
    corners: np.ndarray,
    center_xy: np.ndarray,
    future: np.ndarray | None = None,
) -> np.ndarray:
    """Return a heading vector aligned with the bbox long axis.

    The returned direction is chosen to align with future motion if available.
    If no reliable future motion exists, a deterministic direction is chosen.
    """
    pts = np.asarray(corners[:4], dtype=float)

    # Use the longest edge as the vehicle longitudinal axis candidate.
    edge01 = pts[1] - pts[0]
    edge12 = pts[2] - pts[1]

    if np.linalg.norm(edge01) >= np.linalg.norm(edge12):
        axis = edge01
    else:
        axis = edge12

    norm = float(np.linalg.norm(axis))
    if norm < 1e-9:
        return np.array([1.0, 0.0], dtype=float)

    axis = axis / norm

    # Prefer the sign that matches actual future displacement.
    if future is not None and len(future) > 0:
        valid = np.isfinite(future).all(axis=1)
        valid_pts = future[valid]
        if len(valid_pts) > 0:
            motion = np.asarray(valid_pts[0], dtype=float) - np.asarray(center_xy, dtype=float)
            motion_norm = float(np.linalg.norm(motion))
            if motion_norm > 1e-6:
                if float(np.dot(axis, motion)) < 0:
                    axis = -axis
                return axis

    # Deterministic fallback for nearly static objects:
    # choose the direction with larger +y component, and if tied larger +x.
    cand1 = axis
    cand2 = -axis
    if (cand2[1] > cand1[1]) or (abs(cand2[1] - cand1[1]) < 1e-9 and cand2[0] > cand1[0]):
        axis = cand2
    return axis

def actor_future(
    infos: list[dict[str, Any]],
    idx: int,
    actor_id: int,
    sample_interval: int,
    future_frames: int,
) -> np.ndarray:
    cur = infos[idx]
    w2l_cur = np.asarray(cur["sensors"]["LIDAR_TOP"]["world2lidar"], dtype=float)
    points = []

    for step in range(future_frames + 1):
        j = idx + step * sample_interval
        if j >= len(infos) or infos[j]["folder"] != cur["folder"]:
            points.append([np.nan, np.nan])
            continue
        adj = infos[j]
        where = np.where(np.asarray(adj["gt_ids"]) == actor_id)[0]
        if len(where) != 1:
            points.append([np.nan, np.nan])
            continue
        box2world = np.asarray(adj["npc2world"][where[0]], dtype=float)
        box2cur = w2l_cur @ box2world
        points.append(box2cur[:2, 3])
    return np.asarray(points, dtype=float)


def ego_future(
    infos: list[dict[str, Any]],
    idx: int,
    sample_interval: int,
    future_frames: int,
) -> np.ndarray:
    cur = infos[idx]
    w2l_cur = np.asarray(cur["sensors"]["LIDAR_TOP"]["world2lidar"], dtype=float)
    points = [[0.0, 0.0]]

    for step in range(1, future_frames + 1):
        j = idx + step * sample_interval
        if j >= len(infos) or infos[j]["folder"] != cur["folder"]:
            points.append([np.nan, np.nan])
            continue
        w2l_adj = np.asarray(infos[j]["sensors"]["LIDAR_TOP"]["world2lidar"], dtype=float)
        adj_lidar_to_cur = w2l_cur @ np.linalg.inv(w2l_adj)
        points.append(adj_lidar_to_cur[:2, 3])
    return np.asarray(points, dtype=float)


def map_vectors(info: dict[str, Any], map_infos: dict[str, Any], pc_range: np.ndarray):
    """Reproduce B2D_VAD_Dataset.get_map_info() exactly (team_code/data/b2d_vad_dataset.py).

    Candidate lane prefilter distance (50 m) is hardcoded there regardless of
    pc_range, boundary masks are strict (``>``/``<``, not ``>=``/``<=``), lane
    points outside the mask are dropped (not clipped) before connecting the
    remainder into one LineString, and trigger volumes (TrafficLight/StopSign)
    are kept as closed polygons (all 4 corners must be in range) rather than
    reduced to a stop-line segment.
    """
    town = info["town_name"]
    if town not in map_infos:
        return []

    m = map_infos[town]
    w2l = np.asarray(info["sensors"]["LIDAR_TOP"]["world2lidar"], dtype=float)
    ego_xy_world = np.linalg.inv(w2l)[:2, 3]
    max_distance = 50.0
    vectors = []

    lane_points = m["lane_points"]
    lane_sample_points = m["lane_sample_points"]
    lane_types = m["lane_types"]
    for idx in range(len(lane_sample_points)):
        samples = np.asarray(lane_sample_points[idx], dtype=float)
        if np.min(np.linalg.norm(samples[:, :2] - ego_xy_world, axis=-1)) >= max_distance:
            continue
        typ = lane_types[idx]
        if typ not in MAP_ELEMENT_CLASS:
            continue
        pts = np.asarray(lane_points[idx], dtype=float)
        hom = np.concatenate([pts, np.ones((len(pts), 1))], axis=1)
        local = (w2l @ hom.T).T
        mask = (
            (local[:, 0] > pc_range[0]) & (local[:, 0] < pc_range[3])
            & (local[:, 1] > pc_range[1]) & (local[:, 1] < pc_range[4])
        )
        local_in_range = local[mask, 0:2]
        if len(local_in_range) > 1:
            vectors.append((typ, local_in_range, False))

    trigger_points = m.get("trigger_volumes_points", [])
    trigger_types = m.get("trigger_volumes_types", [])
    for pts, typ in zip(trigger_points, trigger_types):
        if typ not in MAP_ELEMENT_CLASS:
            continue
        pts = np.asarray(pts, dtype=float)
        hom = np.concatenate([pts, np.ones((len(pts), 1))], axis=1)
        local = (w2l @ hom.T).T
        mask = (
            (local[:, 0] > pc_range[0]) & (local[:, 0] < pc_range[3])
            & (local[:, 1] > pc_range[1]) & (local[:, 1] < pc_range[4])
        )
        local_in_range = local[mask, 0:2]
        if mask.all():
            closed = np.concatenate([local_in_range, local_in_range[0:1]], axis=0)
            vectors.append((typ, closed, False))
    return vectors


def plot_sample(
    infos: list[dict[str, Any]],
    map_infos: dict[str, Any],
    idx: int,
    output: Path,
    sample_interval: int,
    future_frames: int,
    pc_range: np.ndarray,
    show_ids: bool,
    show_training_points: bool,
    show_heading: bool,
    show_yaw: bool,
    highlight_actor_id: int | None,
) -> None:
    info = infos[idx]
    width, height, margin, header = 900, 1200, 70, 105
    image = np.full((height, width, 3), 248, dtype=np.uint8)
    plot_left, plot_right = margin, width - margin
    plot_top, plot_bottom = header + 25, height - margin

    def to_pixel(points: np.ndarray) -> np.ndarray:
        pts = np.asarray(points, dtype=float).reshape(-1, 2)
        x = plot_left + (pts[:, 0] - pc_range[0]) / (
            pc_range[3] - pc_range[0]
        ) * (plot_right - plot_left)
        y = plot_bottom - (pts[:, 1] - pc_range[1]) / (
            pc_range[4] - pc_range[1]
        ) * (plot_bottom - plot_top)
        return np.rint(np.column_stack([x, y])).astype(np.int32)

    def draw_polyline(points: np.ndarray, color, thickness=2, dashed=False, double=False):
        pixels = to_pixel(points)
        if len(pixels) < 2:
            return
        if double:
            # SolidSolid (double solid line): offset two parallel solid
            # lines perpendicular to each segment instead of a single line.
            offset = 2.0
            for i in range(len(pixels) - 1):
                p1 = pixels[i].astype(np.float64)
                p2 = pixels[i + 1].astype(np.float64)
                seg = p2 - p1
                seg_norm = float(np.linalg.norm(seg))
                if seg_norm < 1e-6:
                    continue
                perp = np.array([-seg[1], seg[0]]) / seg_norm * offset
                for sign in (-1.0, 1.0):
                    a = tuple(np.rint(p1 + sign * perp).astype(np.int32))
                    b = tuple(np.rint(p2 + sign * perp).astype(np.int32))
                    cv2.line(image, a, b, color, thickness, cv2.LINE_AA)
        elif dashed:
            # Dash by cumulative pixel arc-length, not by point index: source
            # lane points can be a few cm apart (dense HD-map sampling), far
            # below one pixel, so alternating raw segments would depend on
            # rounding luck instead of drawing an actual visible dash.
            dash_on, dash_off = 6.0, 4.0
            pts = pixels.astype(np.float64)
            seg_len = np.linalg.norm(np.diff(pts, axis=0), axis=1)
            cumulative = np.concatenate([[0.0], np.cumsum(seg_len)])
            total = float(cumulative[-1])
            if total < 1e-6:
                return
            pos = 0.0
            while pos < total:
                end = min(pos + dash_on, total)
                a = np.column_stack([
                    np.interp(pos, cumulative, pts[:, 0]),
                    np.interp(pos, cumulative, pts[:, 1]),
                ])[0]
                b = np.column_stack([
                    np.interp(end, cumulative, pts[:, 0]),
                    np.interp(end, cumulative, pts[:, 1]),
                ])[0]
                cv2.line(image, tuple(np.rint(a).astype(np.int32)),
                         tuple(np.rint(b).astype(np.int32)),
                         color, thickness, cv2.LINE_AA)
                pos += dash_on + dash_off
        else:
            cv2.polylines(image, [pixels], False, color, thickness, cv2.LINE_AA)

    # Metric grid in the official point-cloud ROI.
    for x in np.arange(math.ceil(pc_range[0] / 5) * 5, pc_range[3] + 0.1, 5):
        p = to_pixel(np.array([[x, pc_range[1]], [x, pc_range[4]]]))
        cv2.line(image, tuple(p[0]), tuple(p[1]), (225, 225, 225), 1)
    for y in np.arange(math.ceil(pc_range[1] / 5) * 5, pc_range[4] + 0.1, 5):
        p = to_pixel(np.array([[pc_range[0], y], [pc_range[3], y]]))
        cv2.line(image, tuple(p[0]), tuple(p[1]), (225, 225, 225), 1)
    cv2.rectangle(image, (plot_left, plot_top), (plot_right, plot_bottom),
                  (110, 110, 110), 2)

    seen_labels = set()
    for typ, xy, _ in map_vectors(info, map_infos, pc_range):
        style = MAP_STYLE.get(typ, {"dashed": False, "double": False, "color": (30, 30, 30)})
        draw_polyline(xy, style["color"], 1, style["dashed"], style.get("double", False))
        if show_training_points:
            for point in to_pixel(resample_polyline(xy, DEFAULT_POLYLINE_POINTS)):
                cv2.circle(image, tuple(point), 3, style["color"], -1, cv2.LINE_AA)
        seen_labels.add(typ)

    boxes = np.asarray(info["gt_boxes"])
    ids = np.asarray(info["gt_ids"])
    num_points = np.asarray(info.get("num_points", np.ones(len(boxes), dtype=int)))

    def point_in_roi(x: float, y: float) -> bool:
        return (
            pc_range[0] <= x <= pc_range[3]
            and pc_range[1] <= y <= pc_range[4]
        )

    visible_agent_count = 0
    highlighted_present = False

    for box, actor_id, lidar_points in zip(boxes, ids, num_points):
        # Official B2D_VAD_Dataset.get_ann_info(): mask = (num_points != 0)
        if int(lidar_points) == 0:
            continue

        cx, cy = float(box[0]), float(box[1])

        # Debug view에서는 ROI 안의 현재 agent만 표시한다.
        if not point_in_roi(cx, cy):
            continue

        visible_agent_count += 1
        actor_id_int = int(actor_id)
        is_highlight = (
            highlight_actor_id is not None
            and actor_id_int == int(highlight_actor_id)
        )
        if is_highlight:
            highlighted_present = True

        corners = box_corners(box)

        agent_color = (15, 15, 15) if is_highlight else (70, 70, 70)
        draw_polyline(corners, agent_color, 4 if is_highlight else 2)

        yaw = float(box[6])

        future = actor_future(
            infos,
            idx,
            actor_id_int,
            sample_interval,
            future_frames,
        )

        # Heading arrow는 yaw를 단순 (cos, sin)으로 쓰지 않고,
        # 실제 bbox의 긴 축(long axis)을 따라 그린다.
        # 그리고 가능하면 future trajectory 진행방향과 같은 쪽(sign)으로 맞춘다.
        if show_heading:
            heading_unit = compute_box_long_axis_heading(
                corners=corners,
                center_xy=np.array([cx, cy], dtype=float),
                future=future,
            )
            arrow_length = max(2.0, min(5.0, float(max(box[3], box[4])) * 0.9))
            dx = arrow_length * float(heading_unit[0])
            dy = arrow_length * float(heading_unit[1])
            arrow = to_pixel(np.array([[cx, cy], [cx + dx, cy + dy]]))
            cv2.arrowedLine(image, tuple(arrow[0]), tuple(arrow[1]), agent_color,
                            3 if is_highlight else 2, cv2.LINE_AA, tipLength=0.25)
        valid = np.isfinite(future).all(axis=1)
        if valid.any():
            roi_valid = valid.copy()
            roi_valid &= (
                (future[:, 0] >= pc_range[0])
                & (future[:, 0] <= pc_range[3])
                & (future[:, 1] >= pc_range[1])
                & (future[:, 1] <= pc_range[4])
            )
            if roi_valid.any():
                draw_polyline(future[roi_valid], agent_color,
                              3 if is_highlight else 1)
                for point in to_pixel(future[roi_valid]):
                    cv2.circle(image, tuple(point), 3, agent_color, -1, cv2.LINE_AA)

        if show_ids or show_yaw:
            parts = []
            if show_ids:
                parts.append(str(actor_id_int))
            if show_yaw:
                parts.append(f"yaw={normalize_angle_deg(math.degrees(yaw)):.1f}")
            center = to_pixel(np.array([[cx, cy]]))[0]
            for line_no, label_text in enumerate(parts):
                cv2.putText(image, label_text,
                            (int(center[0]) + 5, int(center[1]) - 5 + line_no * 15),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.38, agent_color, 1,
                            cv2.LINE_AA)

    ego = ego_future(infos, idx, sample_interval, future_frames)
    valid = np.isfinite(ego).all(axis=1)
    ego_pixel = to_pixel(np.array([[0.0, 0.0]]))[0]
    triangle = np.array([
        [ego_pixel[0], ego_pixel[1] - 10],
        [ego_pixel[0] - 8, ego_pixel[1] + 8],
        [ego_pixel[0] + 8, ego_pixel[1] + 8],
    ], dtype=np.int32)
    cv2.fillConvexPoly(image, triangle, (0, 0, 0), cv2.LINE_AA)
    if valid.any():
        draw_polyline(ego[valid], (0, 0, 0), 3)
        for point in to_pixel(ego[valid]):
            cv2.circle(image, tuple(point), 4, (0, 0, 0), -1, cv2.LINE_AA)

    # Optional traffic-control centers from the extended metadata.
    tc = info.get("traffic_controls", {})
    for kind, items in (
        ("TL affects_ego", tc.get("traffic_lights", [])),
        ("Stop affects_ego", tc.get("stop_signs", [])),
    ):
        first = True
        for item in items:
            if not item.get("affects_ego", False):
                continue
            loc_world = item.get("trigger_volume_location")
            if loc_world is None:
                loc_world = item.get("location")
            if loc_world is None:
                continue
            p = np.ones(4)
            p[:3] = np.asarray(loc_world, dtype=float)[:3]
            local = np.asarray(info["sensors"]["LIDAR_TOP"]["world2lidar"]) @ p
            if not (
                pc_range[0] <= local[0] <= pc_range[3]
                and pc_range[1] <= local[1] <= pc_range[4]
            ):
                continue
            point = to_pixel(np.array([[local[0], local[1]]]))[0]
            color = (0, 0, 230) if kind.startswith("TL") else (170, 40, 170)
            cv2.drawMarker(image, tuple(point), color, cv2.MARKER_TILTED_CROSS,
                           18, 3, cv2.LINE_AA)
            first = False

    scenario_name = Path(str(info["folder"])).name or "scenario"
    title = f"{scenario_name}  frame {int(info['frame_idx']):05d}"
    subtitle = (
        f"VAD GT ROI | agents={visible_agent_count} | cmd={info.get('command_near')}"
        + (f" | track={highlight_actor_id}" if highlight_actor_id is not None else "")
    )
    cv2.putText(image, title, (margin, 38), cv2.FONT_HERSHEY_SIMPLEX,
                0.72, (25, 25, 25), 2, cv2.LINE_AA)
    cv2.putText(image, subtitle, (margin, 68), cv2.FONT_HERSHEY_SIMPLEX,
                0.52, (70, 70, 70), 1, cv2.LINE_AA)
    legend_x, legend_y = margin, 96
    for typ in MAP_ELEMENT_CLASS:
        if typ not in seen_labels:
            continue
        style = MAP_STYLE[typ]
        if style.get("double"):
            cv2.line(image, (legend_x, legend_y - 2), (legend_x + 22, legend_y - 2),
                     style["color"], 2, cv2.LINE_AA)
            cv2.line(image, (legend_x, legend_y + 2), (legend_x + 22, legend_y + 2),
                     style["color"], 2, cv2.LINE_AA)
        else:
            cv2.line(image, (legend_x, legend_y), (legend_x + 22, legend_y),
                     style["color"], 3, cv2.LINE_AA)
        cv2.putText(image, typ, (legend_x + 27, legend_y + 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.36, (60, 60, 60), 1,
                    cv2.LINE_AA)
        legend_x += 125

    cv2.putText(image, "LiDAR x: lateral | LiDAR y: forward | dots: VAD fixed_num=20",
                (margin, height - 25), cv2.FONT_HERSHEY_SIMPLEX, 0.43,
                (80, 80, 80), 1, cv2.LINE_AA)
    output.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(output), image):
        raise OSError(f"VAD GT visualization write failed: {output}")



def find_actor_sample_indices(
    infos: list[dict[str, Any]],
    actor_id: int,
    folder_filter: str | None = None,
    start_frame: int | None = None,
    end_frame: int | None = None,
    frame_step: int = 1,
) -> list[int]:
    """persistent actor ID가 실제 training GT에 존재하는 sample index를 반환한다."""
    matched: list[int] = []

    for idx, info in enumerate(infos):
        folder = str(info["folder"])
        if folder_filter is not None and folder_filter not in folder:
            continue

        frame_idx = int(info["frame_idx"])
        if start_frame is not None and frame_idx < start_frame:
            continue
        if end_frame is not None and frame_idx > end_frame:
            continue

        ids = np.asarray(info["gt_ids"])
        num_points = np.asarray(
            info.get("num_points", np.ones(len(ids), dtype=int))
        )
        where = np.where(ids == int(actor_id))[0]
        if len(where) != 1:
            continue

        pos = int(where[0])
        if int(num_points[pos]) == 0:
            continue

        matched.append(idx)

    if not matched:
        return []

    if frame_step <= 1:
        return matched

    first_frame = int(infos[matched[0]]["frame_idx"])
    selected = [
        idx
        for idx in matched
        if (int(infos[idx]["frame_idx"]) - first_frame) % frame_step == 0
    ]
    return selected


def print_actor_summary(
    infos: list[dict[str, Any]],
    indices: list[int],
    actor_id: int,
) -> None:
    if not indices:
        print(f"actor {actor_id}: not found")
        return

    rows = []
    for idx in indices:
        info = infos[idx]
        ids = np.asarray(info["gt_ids"])
        pos = int(np.where(ids == int(actor_id))[0][0])
        box = np.asarray(info["gt_boxes"][pos], dtype=float)
        rows.append(
            (
                int(info["frame_idx"]),
                float(box[0]),
                float(box[1]),
                math.degrees(float(box[6])),
            )
        )

    print(
        f"actor {actor_id}: samples={len(rows)}, "
        f"frame={rows[0][0]}~{rows[-1][0]}"
    )
    print("frame,x,y,yaw_deg")
    for frame, x, y, yaw_deg in rows:
        print(f"{frame},{x:.3f},{y:.3f},{yaw_deg:.2f}")


def main() -> int:
    p = argparse.ArgumentParser(
        description=(
            "Generate frame-level VAD vector-map GT and visualize it in BEV."
        )
    )
    p.add_argument("--anno-dir", type=Path)
    p.add_argument("--map-file", type=Path, help="Town*_HD_map.npz")
    p.add_argument(
        "--map-infos-pkl",
        type=Path,
        help=(
            "prepare_b2d_infos.py 결과 b2d_map_infos.pkl 경로. 지정하면 "
            "--map-file(npz) 대신 이 PKL에서 town map(lane/trigger_volumes)을 읽는다."
        ),
    )
    p.add_argument("--scenario-name")
    p.add_argument(
        "--vectors-dir",
        type=Path,
        help="raw annotation mode에서 프레임별 vector GT NPZ를 저장할 폴더",
    )
    p.add_argument(
        "--all-frames",
        action="store_true",
        help="선택한 시작점부터 남은 모든 프레임을 처리합니다.",
    )
    p.add_argument("--infos", type=Path, help="legacy VAD info PKL")
    p.add_argument("--map-infos", type=Path, help="legacy map info PKL")
    p.add_argument("--output-dir", type=Path, default=Path("outputs/vad_gt_visualization"))
    p.add_argument("--sample", type=int, default=None)
    p.add_argument("--folder", default=None)
    p.add_argument("--frame", type=int, default=None)
    p.add_argument("--count", type=int, default=1)
    p.add_argument("--stride", type=int, default=1)
    p.add_argument("--sample-interval", type=int, default=5)
    p.add_argument("--future-frames", type=int, default=6)
    p.add_argument("--show-ids", action="store_true")
    p.add_argument(
        "--show-heading",
        action="store_true",
        help="각 agent bbox 중심에 heading arrow를 표시합니다.",
    )
    p.add_argument(
        "--show-yaw",
        action="store_true",
        help="각 agent 옆에 bbox yaw(deg)를 표시합니다.",
    )
    p.add_argument(
        "--track-agent",
        type=int,
        default=None,
        help="지정한 persistent actor ID를 강조 표시합니다.",
    )
    p.add_argument(
        "--track-sequence",
        action="store_true",
        help="--track-agent와 함께 사용하여 해당 actor가 보이는 연속 frame들을 생성합니다.",
    )
    p.add_argument(
        "--track-start-frame",
        type=int,
        default=None,
        help="tracking 출력 시작 frame. 생략하면 actor가 처음 보이는 frame부터 시작합니다.",
    )
    p.add_argument(
        "--track-end-frame",
        type=int,
        default=None,
        help="tracking 출력 종료 frame. 생략하면 actor가 마지막으로 보이는 frame까지 처리합니다.",
    )
    p.add_argument(
        "--track-frame-step",
        type=int,
        default=1,
        help="tracking 시 실제 frame_idx 기준 출력 간격 (기본: 1).",
    )
    p.add_argument(
        "--show-training-points",
        action="store_true",
        help="VAD fixed_num=20에 대응하는 map sample point를 함께 표시합니다.",
    )
    p.add_argument(
        "--one-per-scenario",
        action="store_true",
        help="각 scenario에서 대표 frame 1장씩 자동 생성합니다.",
    )
    p.add_argument(
        "--representative",
        choices=("first", "middle", "last"),
        default="middle",
        help="--one-per-scenario 사용 시 대표 frame 선택 방식 (기본: middle)",
    )
    p.add_argument(
        "--point-cloud-range",
        nargs=6,
        type=float,
        default=DEFAULT_PC_RANGE.tolist(),
    )
    args = p.parse_args()

    raw_mode = (
        args.anno_dir is not None
        or args.map_file is not None
        or args.map_infos_pkl is not None
    )
    if raw_mode:
        if args.anno_dir is None:
            p.error("raw mode requires --anno-dir")
        if args.map_file is None and args.map_infos_pkl is None:
            p.error("raw mode requires --map-file or --map-infos-pkl")
        if args.map_file is not None and args.map_infos_pkl is not None:
            p.error("use only one of --map-file or --map-infos-pkl")
        if args.infos is not None or args.map_infos is not None:
            p.error("do not mix raw annotation and legacy PKL inputs")
        if args.vectors_dir is None:
            p.error("raw mode requires --vectors-dir")
        if args.track_sequence or args.one_per_scenario:
            p.error("tracking modes are only available with legacy PKL inputs")

        anno_dir = args.anno_dir.expanduser().resolve()
        if not anno_dir.is_dir():
            p.error(f"annotation folder not found: {anno_dir}")
        scenario_name = args.scenario_name or anno_dir.parent.name

        if args.map_infos_pkl is not None:
            map_infos_pkl = args.map_infos_pkl.expanduser().resolve()
            if not map_infos_pkl.is_file():
                p.error(f"map infos pkl not found: {map_infos_pkl}")
            town = detect_town(anno_dir, None, scenario_name)
            map_infos = load_map_infos_pkl(map_infos_pkl, town)
        else:
            map_file = args.map_file.expanduser().resolve()
            if not map_file.is_file():
                p.error(f"map file not found: {map_file}")
            town = detect_town(anno_dir, map_file, scenario_name)
            map_infos = load_raw_map_info(map_file, town)

        infos = raw_infos_from_annotations(anno_dir, town, scenario_name)
        if not infos:
            p.error(f"annotation files not found: {anno_dir}")
    else:
        if args.infos is None or args.map_infos is None:
            p.error(
                "provide --anno-dir/--map-file or legacy --infos/--map-infos"
            )
        infos = load_pickle(args.infos.expanduser().resolve())
        map_infos = load_pickle(args.map_infos.expanduser().resolve())
        if not isinstance(infos, list):
            raise TypeError("infos PKL must contain a list")

    if args.count < 1:
        p.error("--count must be >= 1")
    if args.stride < 1:
        p.error("--stride must be >= 1")

    pc_range = np.asarray(args.point_cloud_range, dtype=float)
    output_dir = args.output_dir.expanduser().resolve()
    vectors_dir = (
        args.vectors_dir.expanduser().resolve()
        if args.vectors_dir is not None else None
    )

    if args.track_sequence:
        if args.track_agent is None:
            raise ValueError("--track-sequence requires --track-agent")
        if args.track_frame_step < 1:
            raise ValueError("--track-frame-step must be >= 1")

        indices = find_actor_sample_indices(
            infos=infos,
            actor_id=args.track_agent,
            folder_filter=args.folder,
            start_frame=args.track_start_frame,
            end_frame=args.track_end_frame,
            frame_step=args.track_frame_step,
        )
        if not indices:
            raise ValueError(
                f"actor id {args.track_agent} not found in requested range"
            )

        print_actor_summary(infos, indices, args.track_agent)

        generated = 0
        for seq_no, idx in enumerate(indices, 1):
            info = infos[idx]
            scenario_name = Path(str(info["folder"])).name or "scenario"
            name = (
                f"{scenario_name}_actor{args.track_agent}_"
                f"{int(info['frame_idx']):05d}.png"
            )
            output = output_dir / name
            plot_sample(
                infos,
                map_infos,
                idx,
                output,
                args.sample_interval,
                args.future_frames,
                pc_range,
                args.show_ids,
                args.show_training_points,
                args.show_heading,
                args.show_yaw,
                args.track_agent,
            )
            print(
                f"[{seq_no}/{len(indices)}] "
                f"{info['folder']} frame={int(info['frame_idx'])} -> {output}"
            )
            generated += 1

        print(f"generated={generated}")
        return 0

    if args.one_per_scenario:
        # PKL에서 folder별 sample index를 모은 뒤 각 scenario의 대표 index를 고른다.
        grouped: dict[str, list[int]] = {}
        for i, info in enumerate(infos):
            folder = str(info["folder"])
            grouped.setdefault(folder, []).append(i)

        selected_indices: list[int] = []
        for folder, indices in grouped.items():
            if args.representative == "first":
                chosen = indices[0]
            elif args.representative == "last":
                chosen = indices[-1]
            else:
                chosen = indices[len(indices) // 2]
            selected_indices.append(chosen)

        print(f"scenarios={len(grouped)} representative={args.representative}")
        generated = 0
        for scenario_no, idx in enumerate(selected_indices, 1):
            info = infos[idx]
            scenario_name = Path(str(info["folder"])).name or "scenario"
            name = f"{scenario_no:02d}_{scenario_name}_{int(info['frame_idx']):05d}.png"
            output = output_dir / name
            plot_sample(
                infos,
                map_infos,
                idx,
                output,
                args.sample_interval,
                args.future_frames,
                pc_range,
                args.show_ids,
                args.show_training_points,
                args.show_heading,
                args.show_yaw,
                args.track_agent,
            )
            print(
                f"[{scenario_no}/{len(selected_indices)}] "
                f"{info['folder']} frame={int(info['frame_idx'])} -> {output}"
            )
            generated += 1

        print(f"generated={generated}")
        return 0

    start = choose_index(infos, args.sample, args.folder, args.frame)
    count = args.count
    if args.all_frames:
        count = (len(infos) - start + args.stride - 1) // args.stride
    generated = 0
    vector_instances = 0
    for n in range(count):
        idx = start + n * args.stride
        if idx >= len(infos):
            break
        info = infos[idx]
        scenario_name = Path(str(info["folder"])).name or "scenario"
        frame_idx = int(info["frame_idx"])
        name = f"{scenario_name}_{frame_idx:05d}.png"
        output = output_dir / name
        if vectors_dir is not None:
            vector_instances += save_vector_gt(
                info,
                map_infos,
                pc_range,
                vectors_dir / f"{frame_idx:05d}.npz",
            )
        plot_sample(
            infos,
            map_infos,
            idx,
            output,
            args.sample_interval,
            args.future_frames,
            pc_range,
            args.show_ids,
            args.show_training_points,
            args.show_heading,
            args.show_yaw,
            args.track_agent,
        )
        print(output)
        generated += 1

    print(f"generated={generated} vector_instances={vector_instances}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
