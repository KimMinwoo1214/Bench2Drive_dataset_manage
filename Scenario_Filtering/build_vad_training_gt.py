#!/usr/bin/env python3
"""
Build Bench2DriveZoo VAD-B2D training infos from raw Bench2Drive/CARLA clips.

This script intentionally follows the data contract used by:
  Bench2DriveZoo / uniad/vad / mmcv/datasets/prepare_B2D.py
  Bench2DriveZoo / uniad/vad / mmcv/datasets/B2D_vad_dataset.py

Important:
- Raw Bench2Drive annotations are left-handed.
- The generated info PKL uses the same right-handed conversion as the official
  Bench2DriveZoo VAD preprocessing.
- Agent future trajectories, ego future trajectories and per-frame vector-map
  GT are NOT materialized into the info PKL. The official B2D_VAD_Dataset
  derives them at load time using gt_ids/npc2world/world2lidar/map_infos.
- Extra traffic-control metadata is stored under "traffic_controls". The
  stock B2D_VAD_Dataset ignores this extra key, so a custom traffic-control
  head must explicitly read it.

Default VAD-B2D temporal settings (from the official dataset/config):
  sample_interval = 5 frames at 10 Hz
  past_frames     = 2
  future_frames   = 6
  map points      = 20
  point cloud ROI = [-15, -30, -2, 15, 30, 2]

Typical usage:
  python3 build_vad_training_gt.py \
      /path/to/bench2drive_data \
      --maps-root /path/to/maps \
      --output-dir /path/to/data/infos \
      --visibility-filter off

For official visibility filtering:
  python3 build_vad_training_gt.py \
      /path/to/bench2drive_data \
      --maps-root /path/to/maps \
      --output-dir /path/to/data/infos \
      --visibility-filter official \
      --bench2drive-zoo-root /path/to/Bench2DriveZoo

If the input path is omitted, the script directory is used as data_root.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import importlib.util
import json
import math
import pickle
import random
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

import numpy as np

try:
    import cv2  # only needed for official visibility filtering
except Exception:
    cv2 = None


CAMERAS = [
    "CAM_FRONT",
    "CAM_FRONT_LEFT",
    "CAM_FRONT_RIGHT",
    "CAM_BACK",
    "CAM_BACK_LEFT",
    "CAM_BACK_RIGHT",
]

CAMERA_TO_FOLDER = {
    "CAM_FRONT": "rgb_front",
    "CAM_FRONT_LEFT": "rgb_front_left",
    "CAM_FRONT_RIGHT": "rgb_front_right",
    "CAM_BACK": "rgb_back",
    "CAM_BACK_LEFT": "rgb_back_left",
    "CAM_BACK_RIGHT": "rgb_back_right",
}

# Bench2DriveZoo VAD-B2D map classes.
MAP_ELEMENT_CLASS = {
    "Broken": 0,
    "Solid": 1,
    "SolidSolid": 2,
    "Center": 3,
    "TrafficLight": 4,
    "StopSign": 5,
}

DEFAULT_PC_RANGE = np.array([-15.0, -30.0, -2.0, 15.0, 30.0, 2.0], dtype=np.float64)
DEFAULT_SAMPLE_INTERVAL = 5
DEFAULT_PAST_FRAMES = 2
DEFAULT_FUTURE_FRAMES = 6
DEFAULT_POLYLINE_POINTS = 20

MAX_DISTANCE_DEFAULT = 75.0
FILTER_Z_THRESHOLD_DEFAULT = 10.0

STAND_TO_UE4_ROTATE = np.array(
    [
        [0, 0, 1, 0],
        [1, 0, 0, 0],
        [0, -1, 0, 0],
        [0, 0, 0, 1],
    ],
    dtype=np.float64,
)

LIDAR_TO_RIGHTHAND_EGO = np.array(
    [
        [0, 1, 0, 0],
        [-1, 0, 0, 0],
        [0, 0, 1, 0],
        [0, 0, 0, 1],
    ],
    dtype=np.float64,
)

LEFTHAND_EGO_TO_LIDAR = np.array(
    [
        [0, 1, 0, 0],
        [1, 0, 0, 0],
        [0, 0, 1, 0],
        [0, 0, 0, 1],
    ],
    dtype=np.float64,
)

LEFT2RIGHT = np.eye(4, dtype=np.float64)
LEFT2RIGHT[1, 1] = -1.0

# Think2Drive discrete expert action table used by official prepare_B2D.py.
# Values: throttle, steer, brake.
DISCRETE_ACTIONS = {
    0: (0.0, 0.0, 1.0),
    1: (0.7, -0.5, 0.0), 2: (0.7, -0.3, 0.0),
    3: (0.7, -0.2, 0.0), 4: (0.7, -0.1, 0.0),
    5: (0.7, 0.0, 0.0), 6: (0.7, 0.1, 0.0),
    7: (0.7, 0.2, 0.0), 8: (0.7, 0.3, 0.0),
    9: (0.7, 0.5, 0.0),
    10: (0.3, -0.7, 0.0), 11: (0.3, -0.5, 0.0),
    12: (0.3, -0.3, 0.0), 13: (0.3, -0.2, 0.0),
    14: (0.3, -0.1, 0.0), 15: (0.3, 0.0, 0.0),
    16: (0.3, 0.1, 0.0), 17: (0.3, 0.2, 0.0),
    18: (0.3, 0.3, 0.0), 19: (0.3, 0.5, 0.0),
    20: (0.3, 0.7, 0.0),
    21: (0.0, -1.0, 0.0), 22: (0.0, -0.6, 0.0),
    23: (0.0, -0.3, 0.0), 24: (0.0, -0.1, 0.0),
    25: (1.0, 0.0, 0.0),
    26: (0.0, 0.1, 0.0), 27: (0.0, 0.3, 0.0),
    28: (0.0, 0.6, 0.0), 29: (0.0, 1.0, 0.0),
    30: (0.5, -0.5, 0.0), 31: (0.5, -0.3, 0.0),
    32: (0.5, -0.2, 0.0), 33: (0.5, -0.1, 0.0),
    34: (0.5, 0.0, 0.0), 35: (0.5, 0.1, 0.0),
    36: (0.5, 0.2, 0.0), 37: (0.5, 0.3, 0.0),
    38: (0.5, 0.5, 0.0),
}


@dataclass(frozen=True)
class Scenario:
    root: Path
    anno_dir: Path
    rel_folder: str
    town: str


@dataclass
class BuildStats:
    frames_seen: int = 0
    frames_written: int = 0
    frames_skipped_empty_gt: int = 0
    frames_failed: int = 0
    boxes_seen: int = 0
    boxes_written: int = 0
    boxes_filtered_distance: int = 0
    boxes_filtered_z: int = 0
    boxes_filtered_visibility: int = 0


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                keys.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def save_pickle(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        pickle.dump(obj, f, protocol=pickle.HIGHEST_PROTOCOL)


def load_json_gz(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def frame_number(path: Path) -> int:
    m = re.search(r"(\d+)", path.name)
    if not m:
        raise ValueError(f"cannot parse frame number: {path.name}")
    return int(m.group(1))


def as_matrix(value: Any, name: str) -> np.ndarray:
    arr = np.asarray(value, dtype=np.float64)
    if arr.shape != (4, 4):
        raise ValueError(f"{name} must be 4x4, got {arr.shape}")
    return arr


def z_rotation(theta: float) -> np.ndarray:
    c = math.cos(theta)
    s = math.sin(theta)
    mat = np.eye(4, dtype=np.float64)
    mat[:2, :2] = [[c, -s], [s, c]]
    return mat


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except Exception:
        return default


def vec3(value: Any, default: float = 0.0) -> np.ndarray:
    if not isinstance(value, (list, tuple, np.ndarray)):
        return np.array([default, default, default], dtype=np.float64)
    data = list(value) + [default, default, default]
    return np.array(
        [safe_float(data[0], default), safe_float(data[1], default), safe_float(data[2], default)],
        dtype=np.float64,
    )


def apply_transform(point_xyz: np.ndarray, transform: np.ndarray) -> np.ndarray:
    p = np.ones(4, dtype=np.float64)
    p[:3] = np.asarray(point_xyz, dtype=np.float64)[:3]
    return (transform @ p)[:3]


def get_npc2world(npc: dict[str, Any]) -> np.ndarray:
    """
    Raw actor frame -> right-handed CARLA world.

    This mirrors the transform correction used by Bench2DriveZoo. If an actor
    transform matrix is present it is preferred; otherwise location/rotation is
    used as fallback.
    """
    raw_rotation = npc.get("rotation", [0.0, 0.0, 0.0])
    yaw_raw = math.radians(safe_float(list(raw_rotation)[-1] if raw_rotation else 0.0))

    for key in ("world2vehicle", "world2ego", "world2sign", "world2ped"):
        if key not in npc:
            continue
        try:
            npc2world_lh = np.linalg.inv(as_matrix(npc[key], key))
        except Exception:
            continue

        yaw_from_matrix = math.atan2(npc2world_lh[1, 0], npc2world_lh[0, 0])
        if abs(yaw_raw - yaw_from_matrix) > 0.01:
            fixed = z_rotation(yaw_raw)
            fixed[:3, 3] = npc2world_lh[:3, 3]
            npc2world_lh = fixed

        return LEFT2RIGHT @ npc2world_lh @ LEFT2RIGHT

    loc = vec3(npc.get("location"))
    npc2world_lh = z_rotation(yaw_raw)
    npc2world_lh[:3, 3] = loc
    return LEFT2RIGHT @ npc2world_lh @ LEFT2RIGHT


def cube_vertices(center: np.ndarray, extent: np.ndarray) -> np.ndarray:
    cx, cy, cz = center
    ex, ey, ez = extent
    return np.array(
        [
            [cx + ex, cy + ey, cz + ez],
            [cx + ex, cy - ey, cz + ez],
            [cx - ex, cy - ey, cz + ez],
            [cx - ex, cy + ey, cz + ez],
            [cx + ex, cy + ey, cz - ez],
            [cx + ex, cy - ey, cz - ez],
            [cx - ex, cy - ey, cz - ez],
            [cx - ex, cy + ey, cz - ez],
        ],
        dtype=np.float64,
    )


def project_point(point_xyz: np.ndarray, intrinsic: np.ndarray, lidar2cam: np.ndarray) -> tuple[np.ndarray, float]:
    p = np.ones(4, dtype=np.float64)
    p[:3] = point_xyz
    camera = (lidar2cam @ p)[:3]
    depth = float(camera[2])
    if abs(depth) < 1e-9:
        return np.array([np.inf, np.inf]), depth
    image = intrinsic @ camera
    return image[:2] / image[2], depth


def find_vis_utils(zoo_root: Optional[Path]) -> Any | None:
    candidates: list[Path] = []
    if zoo_root is not None:
        candidates.append(zoo_root / "mmcv" / "datasets" / "vis_utils.py")

    here = Path.cwd().resolve()
    for base in [here, *here.parents]:
        candidates.append(base / "mmcv" / "datasets" / "vis_utils.py")
        candidates.append(base / "Bench2DriveZoo" / "mmcv" / "datasets" / "vis_utils.py")

    for path in candidates:
        if not path.is_file():
            continue
        spec = importlib.util.spec_from_file_location("b2d_vis_utils", path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    return None


def parse_town(text: str, anno: Optional[dict[str, Any]] = None) -> str:
    match = re.search(r"(Town\d+)", text, flags=re.IGNORECASE)
    if match:
        raw = match.group(1)
        return "Town" + re.sub(r"\D", "", raw)
    if anno is not None:
        for key in ("town_name", "town", "map"):
            value = anno.get(key)
            if isinstance(value, str):
                match = re.search(r"(Town\d+)", value, flags=re.IGNORECASE)
                if match:
                    return "Town" + re.sub(r"\D", "", match.group(1))
    raise ValueError(f"TownXX not found in scenario name/path: {text}")


def discover_scenarios(data_root: Path, anno_dir_name: str) -> list[Scenario]:
    data_root = data_root.resolve()
    anno_dirs: list[Path] = []

    if data_root.name == anno_dir_name and data_root.is_dir():
        anno_dirs = [data_root]
        scenario_root_base = data_root.parent
        data_root_for_rel = scenario_root_base
    else:
        direct = data_root / anno_dir_name
        if direct.is_dir():
            anno_dirs.append(direct)
        anno_dirs.extend(p for p in data_root.rglob(anno_dir_name) if p.is_dir())
        data_root_for_rel = data_root

    unique = sorted({p.resolve() for p in anno_dirs})
    scenarios: list[Scenario] = []

    for anno_dir in unique:
        files = list(anno_dir.glob("*.json.gz"))
        if not files:
            continue
        root = anno_dir.parent

        # A valid training clip should normally own camera data. Do not reject
        # clips without it here; report missing camera paths later.
        try:
            rel = root.relative_to(data_root_for_rel).as_posix()
        except ValueError:
            rel = root.name

        # If data_root itself is a clip, folder="." keeps data_path resolvable
        # when B2D_VAD_Dataset joins data_root + data_path.
        rel = rel if rel else "."
        sample_anno = None
        try:
            sample_anno = load_json_gz(sorted(files, key=frame_number)[0])
        except Exception:
            pass
        town = parse_town(root.name, sample_anno)
        scenarios.append(Scenario(root=root, anno_dir=anno_dir, rel_folder=rel, town=town))

    # Avoid duplicates from direct + rglob.
    by_root = {s.root: s for s in scenarios}
    return sorted(by_root.values(), key=lambda s: s.rel_folder)


def choose_maps_root(data_root: Path, requested: Optional[Path]) -> Path:
    if requested is not None:
        path = requested.expanduser().resolve()
        if not path.is_dir():
            raise FileNotFoundError(f"maps root not found: {path}")
        return path

    script_dir = Path(__file__).resolve().parent
    candidates = [
        data_root / "maps",
        data_root.parent / "maps",
        script_dir / "maps",
        script_dir.parent / "maps",
    ]
    for path in candidates:
        if path.is_dir() and list(path.glob("Town*_HD_map.npz")):
            return path.resolve()
    raise FileNotFoundError("maps root not found; pass --maps-root")


def normalize_points(raw: Any, lane_mode: bool) -> np.ndarray:
    """
    Return Nx3 points from the object-heavy CARLA HD-map NPZ structure.
    Lane points are often stored as [[x,y,z]], trigger points as [x,y,z].
    """
    out: list[list[float]] = []
    if not isinstance(raw, (list, tuple, np.ndarray)):
        return np.zeros((0, 3), dtype=np.float64)
    for item in raw:
        value = item
        if lane_mode and isinstance(value, (list, tuple, np.ndarray)) and len(value) > 0:
            first = value[0]
            if isinstance(first, (list, tuple, np.ndarray)):
                value = first
        arr = np.asarray(value, dtype=np.float64).reshape(-1)
        if arr.size >= 3:
            out.append([float(arr[0]), float(arr[1]), float(arr[2])])
    return np.asarray(out, dtype=np.float64).reshape(-1, 3)


def build_map_infos(
    maps_root: Path,
    errors: list[dict[str, Any]],
    required_towns: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """
    Build b2d_map_infos.pkl source data exactly in the shape expected by
    B2D_VAD_Dataset.get_map_info().

    Memory note:
    The previous version loaded every Town map under maps_root even when only
    one scenario was requested. HD-map NPZ files are object-heavy and can use
    substantial RAM while being converted, so this version loads only towns
    that are actually required by the discovered scenarios.
    """
    map_infos: dict[str, dict[str, Any]] = {}

    if required_towns:
        map_paths: list[Path] = []
        for town in sorted(required_towns):
            exact = maps_root / f"{town}_HD_map.npz"
            if exact.is_file():
                map_paths.append(exact)
                continue

            alternatives = sorted(maps_root.glob(f"{town}*.npz"))
            if alternatives:
                map_paths.append(alternatives[0])
            else:
                errors.append(
                    {
                        "stage": "map_discovery",
                        "source": str(maps_root),
                        "town": town,
                        "error": f"map file not found for {town}",
                    }
                )
    else:
        map_paths = sorted(maps_root.glob("Town*.npz"))

    for npz_path in map_paths:
        town = npz_path.name.split("_")[0]
        if not re.fullmatch(r"Town\d+", town, flags=re.IGNORECASE):
            continue
        town = "Town" + re.sub(r"\D", "", town)

        if required_towns and town not in required_towns:
            continue

        print(f"[map] loading {town}: {npz_path.name}")

        try:
            raw_map = dict(np.load(npz_path, allow_pickle=True)["arr"])
        except Exception as exc:
            errors.append({"stage": "map_load", "source": str(npz_path), "error": repr(exc)})
            continue

        lane_points: list[np.ndarray] = []
        lane_types: list[str] = []
        lane_sample_points: list[np.ndarray] = []
        trigger_points: list[np.ndarray] = []
        trigger_types: list[str] = []
        trigger_samples: list[np.ndarray] = []

        for _, road in raw_map.items():
            if not isinstance(road, dict):
                continue
            for lane_id, lane in road.items():
                if lane_id == "Trigger_Volumes":
                    if not isinstance(lane, (list, tuple, np.ndarray)):
                        continue
                    for item in lane:
                        if not isinstance(item, dict):
                            continue
                        pts = normalize_points(item.get("Points", []), lane_mode=False)
                        if len(pts) == 0:
                            continue
                        pts[:, 1] *= -1.0
                        trigger_points.append(pts)
                        trigger_samples.append(pts.mean(axis=0))
                        trigger_types.append(str(item.get("Type", "Unknown")))
                else:
                    if not isinstance(lane, (list, tuple, np.ndarray)):
                        continue
                    for item in lane:
                        if not isinstance(item, dict):
                            continue
                        pts = normalize_points(item.get("Points", []), lane_mode=True)
                        if len(pts) == 0:
                            continue
                        pts[:, 1] *= -1.0
                        lane_points.append(pts)
                        lane_types.append(str(item.get("Type", "Unknown")))

                        indices = list(range(0, len(pts), 50))
                        if not indices or indices[-1] != len(pts) - 1:
                            indices.append(len(pts) - 1)
                        lane_sample_points.append(pts[indices])

        map_infos[town] = {
            "lane_points": lane_points,
            "lane_sample_points": lane_sample_points,
            "lane_types": lane_types,
            "trigger_volumes_points": trigger_points,
            "trigger_volumes_sample_points": trigger_samples,
            "trigger_volumes_types": trigger_types,
        }

        # Drop the original object-heavy map structure before loading another
        # town to reduce peak memory usage.
        del raw_map

    return map_infos


def infer_action(scenario_root: Path, frame_idx: int, anno: dict[str, Any]) -> tuple[float, float, float, str]:
    expert_dir = scenario_root / "expert_assessment"
    candidate = expert_dir / ("-0001.npz" if frame_idx == 0 else f"{frame_idx - 1:05d}.npz")
    if candidate.is_file():
        try:
            data = np.load(candidate, allow_pickle=True)["arr_0"]
            action_id = int(data[-1])
            if action_id in DISCRETE_ACTIONS:
                t, s, b = DISCRETE_ACTIONS[action_id]
                return t, s, b, "expert_assessment"
        except Exception:
            pass

    # Fallback for custom data collection.
    control = anno.get("control") if isinstance(anno.get("control"), dict) else {}
    throttle = anno.get("throttle", control.get("throttle", 0.0))
    steer = anno.get("steer", control.get("steer", 0.0))
    brake = anno.get("brake", control.get("brake", 0.0))
    source = "annotation" if any(k in anno or k in control for k in ("throttle", "steer", "brake")) else "default_zero"
    return safe_float(throttle), safe_float(steer), safe_float(brake), source


def build_sensor_infos(
    anno: dict[str, Any],
    rel_folder: str,
    frame_idx: int,
) -> dict[str, dict[str, Any]]:
    sensors_raw = anno.get("sensors")
    if not isinstance(sensors_raw, dict):
        raise KeyError("sensors missing")

    result: dict[str, dict[str, Any]] = {}

    for cam in CAMERAS:
        if cam not in sensors_raw:
            raise KeyError(f"sensor missing: {cam}")
        raw = sensors_raw[cam]
        cam2ego = LEFT2RIGHT @ as_matrix(raw["cam2ego"], f"{cam}.cam2ego") @ STAND_TO_UE4_ROTATE
        intrinsic = np.asarray(raw["intrinsic"], dtype=np.float64)
        world2cam = (
            np.linalg.inv(STAND_TO_UE4_ROTATE)
            @ as_matrix(raw["world2cam"], f"{cam}.world2cam")
            @ LEFT2RIGHT
        )

        relative_image = Path(rel_folder) / "camera" / CAMERA_TO_FOLDER[cam] / f"{frame_idx:05d}.jpg"
        result[cam] = {
            "cam2ego": cam2ego,
            "intrinsic": intrinsic,
            "world2cam": world2cam,
            "data_path": relative_image.as_posix(),
        }

    if "LIDAR_TOP" not in sensors_raw:
        raise KeyError("sensor missing: LIDAR_TOP")

    lidar_raw = sensors_raw["LIDAR_TOP"]
    lidar2ego = (
        LEFT2RIGHT
        @ as_matrix(lidar_raw["lidar2ego"], "LIDAR_TOP.lidar2ego")
        @ LEFT2RIGHT
        @ LIDAR_TO_RIGHTHAND_EGO
    )
    world2lidar = (
        LEFTHAND_EGO_TO_LIDAR
        @ as_matrix(lidar_raw["world2lidar"], "LIDAR_TOP.world2lidar")
        @ LEFT2RIGHT
    )
    result["LIDAR_TOP"] = {
        "lidar2ego": lidar2ego,
        "world2lidar": world2lidar,
    }
    return result


def depth_path(data_root: Path, image_rel: str) -> Path:
    path = data_root / image_rel
    name = path.parent.name
    if name.startswith("rgb_"):
        path = path.parent.parent / ("depth_" + name[4:]) / path.name
    return path.with_suffix(".png")


def load_depths(
    data_root: Path,
    sensors: dict[str, dict[str, Any]],
    visibility_mode: str,
) -> dict[str, np.ndarray]:
    if visibility_mode == "off":
        return {}
    if cv2 is None:
        raise RuntimeError("opencv-python is required for visibility filtering")

    depths: dict[str, np.ndarray] = {}
    missing = []
    for cam in CAMERAS:
        path = depth_path(data_root, sensors[cam]["data_path"])
        image = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        if image is None:
            missing.append(path)
            continue
        if image.ndim == 3:
            image = image[:, :, 0]
        depths[cam] = image

    if visibility_mode == "official" and missing:
        raise FileNotFoundError(f"depth images missing; first: {missing[0]}")
    return depths


def visibility_ok(
    local_center: np.ndarray,
    extent: np.ndarray,
    yaw_local: float,
    sensors: dict[str, dict[str, Any]],
    depth_images: dict[str, np.ndarray],
    vis_utils: Any,
    max_distance: float,
) -> bool:
    if not depth_images:
        return True

    box2lidar = z_rotation(yaw_local)
    box2lidar[:3, 3] = local_center
    lidar2box = np.linalg.inv(box2lidar)

    # Same construction used by official preprocessing: start from an
    # axis-aligned cube, rotate it into the actor frame and translate back.
    verts = []
    for raw_vert in cube_vertices(local_center, extent):
        tmp = lidar2box @ np.array([raw_vert[0], raw_vert[1], raw_vert[2], 1.0])
        tmp[:3] += local_center
        verts.append(tmp[:3])
    verts = np.asarray(verts)

    for cam in CAMERAS:
        if cam not in depth_images:
            continue
        lidar2cam = np.linalg.inv(sensors[cam]["cam2ego"]) @ sensors["LIDAR_TOP"]["lidar2ego"]
        pts: list[np.ndarray] = []
        dep: list[float] = []

        for vert in verts:
            p, d = project_point(vert, sensors[cam]["intrinsic"], lidar2cam)
            if d > 0 and np.isfinite(p).all():
                pts.append(p)
                dep.append(d)

        if not pts:
            continue

        visible, _, outside, _ = vis_utils.calculate_occlusion_stats(
            np.asarray(pts),
            np.asarray(dep),
            depth_images[cam],
            max_render_depth=max_distance,
        )
        if visible > 1 and outside < 7:
            return True
    return False


def extract_traffic_controls(anno: dict[str, Any]) -> dict[str, Any]:
    lights = []
    stops = []
    for obj in anno.get("bounding_boxes", []):
        if not isinstance(obj, dict):
            continue
        cls = str(obj.get("class", ""))
        type_id = str(obj.get("type_id", ""))
        is_light = cls == "traffic_light" or type_id == "traffic.traffic_light"
        is_stop = cls == "stop_sign" or type_id == "traffic.stop"

        if not (is_light or is_stop):
            continue

        loc_lh = vec3(obj.get("location"))
        loc_rh = np.array([loc_lh[0], -loc_lh[1], loc_lh[2]], dtype=np.float64)

        trig = obj.get("trigger_volume_location")
        trig_rh = None
        if isinstance(trig, (list, tuple, np.ndarray)) and len(trig) >= 3:
            t = vec3(trig)
            trig_rh = np.array([t[0], -t[1], t[2]], dtype=np.float64)

        row = {
            "id": obj.get("id"),
            "type_id": type_id,
            "state": obj.get("state"),
            "affects_ego": bool(obj.get("affects_ego", False)),
            "distance": obj.get("distance"),
            "location": loc_rh,
            "trigger_volume_location": trig_rh,
        }
        if is_light:
            lights.append(row)
        if is_stop:
            stops.append(row)

    return {
        "traffic_lights": lights,
        "stop_signs": stops,
        "relevant_traffic_light_ids": [x["id"] for x in lights if x["affects_ego"]],
        "relevant_stop_sign_ids": [x["id"] for x in stops if x["affects_ego"]],
    }


def process_scenario(
    scenario: Scenario,
    data_root: Path,
    visibility_mode: str,
    vis_utils: Any,
    max_distance: float,
    z_threshold: float,
    errors: list[dict[str, Any]],
    frame_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], BuildStats]:
    infos: list[dict[str, Any]] = []
    stats = BuildStats()
    previous_positions: dict[Any, np.ndarray] = {}

    anno_paths = sorted(scenario.anno_dir.glob("*.json.gz"), key=frame_number)

    for anno_path in anno_paths:
        stats.frames_seen += 1
        idx = frame_number(anno_path)

        try:
            anno = load_json_gz(anno_path)
            boxes = anno.get("bounding_boxes")
            if not isinstance(boxes, list) or not boxes:
                raise KeyError("bounding_boxes missing/empty")

            ego = next(
                (x for x in boxes if isinstance(x, dict) and x.get("class") == "ego_vehicle"),
                boxes[0] if isinstance(boxes[0], dict) else None,
            )
            if ego is None:
                raise KeyError("ego_vehicle missing")

            sensors = build_sensor_infos(anno, scenario.rel_folder, idx)
            world2lidar = sensors["LIDAR_TOP"]["world2lidar"]

            theta = anno.get("theta", math.pi)
            theta_f = safe_float(theta, math.pi)
            ego_yaw = -theta_f + math.pi / 2.0

            accel = vec3(anno.get("acceleration"))
            angular = vec3(anno.get("angular_velocity"))
            ego_extent = vec3(ego.get("extent"), 0.0)

            throttle, steer, brake, action_source = infer_action(scenario.root, idx, anno)

            frame: dict[str, Any] = {
                "folder": scenario.rel_folder,
                "town_name": scenario.town,
                "command_far_xy": np.array(
                    [safe_float(anno.get("x_command_far")), -safe_float(anno.get("y_command_far"))],
                    dtype=np.float64,
                ),
                "command_far": int(anno.get("command_far", -1)),
                "command_near_xy": np.array(
                    [safe_float(anno.get("x_command_near")), -safe_float(anno.get("y_command_near"))],
                    dtype=np.float64,
                ),
                "command_near": int(anno.get("command_near", -1)),
                "frame_idx": idx,
                "ego_yaw": ego_yaw,
                "ego_translation": np.array(
                    [safe_float(anno.get("x")), -safe_float(anno.get("y")), 0.0],
                    dtype=np.float64,
                ),
                "ego_vel": np.array([safe_float(anno.get("speed")), 0.0, 0.0], dtype=np.float64),
                "ego_accel": np.array([accel[0], -accel[1], accel[2]], dtype=np.float64),
                "ego_rotation_rate": -angular,
                "ego_size": np.array([ego_extent[1], ego_extent[0], ego_extent[2]], dtype=np.float64) * 2.0,
                "world2ego": LEFT2RIGHT @ as_matrix(ego["world2ego"], "ego.world2ego") @ LEFT2RIGHT,
                "brake": brake,
                "throttle": throttle,
                "steer": steer,
                "sensors": sensors,
                # Extension: stock B2D_VAD_Dataset ignores this key.
                "traffic_controls": extract_traffic_controls(anno),
            }

            depth_images = load_depths(data_root, sensors, visibility_mode)

            gt_boxes: list[np.ndarray] = []
            gt_names: list[str] = []
            gt_ids: list[int] = []
            num_points_list: list[int] = []
            npc2world_list: list[np.ndarray] = []
            current_positions: dict[Any, np.ndarray] = {}

            ego_loc = vec3(ego.get("location"))

            for npc in boxes:
                if not isinstance(npc, dict) or npc.get("class") == "ego_vehicle":
                    continue

                stats.boxes_seen += 1
                if safe_float(npc.get("distance"), 0.0) > max_distance:
                    stats.boxes_filtered_distance += 1
                    continue

                npc_loc = vec3(npc.get("location"))
                if abs(npc_loc[2] - ego_loc[2]) > z_threshold:
                    stats.boxes_filtered_z += 1
                    continue

                if "center" not in npc or "extent" not in npc:
                    continue

                center_lh = vec3(npc["center"])
                center = np.array([center_lh[0], -center_lh[1], center_lh[2]], dtype=np.float64)

                extent_lh = vec3(npc["extent"])
                extent = np.array([extent_lh[1], extent_lh[0], extent_lh[2]], dtype=np.float64)

                actor_id = npc.get("id")
                current_positions[actor_id] = center
                local_center = apply_transform(center, world2lidar)
                size = extent * 2.0

                if "world2vehicle" in npc:
                    world2vehicle = LEFT2RIGHT @ as_matrix(npc["world2vehicle"], "npc.world2vehicle") @ LEFT2RIGHT
                    vehicle2lidar = world2lidar @ np.linalg.inv(world2vehicle)
                    yaw_local = math.atan2(vehicle2lidar[1, 0], vehicle2lidar[0, 0])
                else:
                    raw_rot = npc.get("rotation", [0.0, 0.0, 0.0])
                    raw_yaw_deg = safe_float(list(raw_rot)[-1] if raw_rot else 0.0)
                    yaw_local = -math.radians(raw_yaw_deg) - ego_yaw + math.pi / 2.0

                yaw_box = -yaw_local - math.pi / 2.0

                if "speed" in npc and "vehicle" in str(npc.get("class", "")):
                    speed = safe_float(npc.get("speed"))
                elif actor_id in previous_positions:
                    speed = float(np.linalg.norm((center - previous_positions[actor_id])[:2]) * 10.0)
                else:
                    speed = 0.0

                speed_x = speed * math.cos(yaw_local)
                speed_y = speed * math.sin(yaw_local)
                num_points = int(npc.get("num_points", -1))

                valid = True
                if visibility_mode in ("official", "auto") and depth_images:
                    valid = visibility_ok(
                        local_center=local_center,
                        extent=extent,
                        yaw_local=yaw_local,
                        sensors=sensors,
                        depth_images=depth_images,
                        vis_utils=vis_utils,
                        max_distance=max_distance,
                    )
                if not valid:
                    stats.boxes_filtered_visibility += 1
                    continue

                npc2world = get_npc2world(npc)
                gt_boxes.append(
                    np.concatenate(
                        [
                            local_center,
                            size,
                            np.array([yaw_box, speed_x, speed_y], dtype=np.float64),
                        ]
                    )
                )
                gt_names.append(str(npc.get("type_id", npc.get("class", "unknown"))))
                try:
                    gt_ids.append(int(actor_id))
                except Exception:
                    # Persistent IDs are mandatory for VAD motion GT.
                    raise ValueError(f"actor has non-integer/missing persistent id: {actor_id!r}")
                num_points_list.append(num_points)
                npc2world_list.append(npc2world)
                stats.boxes_written += 1

            if not gt_boxes:
                stats.frames_skipped_empty_gt += 1
                frame_rows.append(
                    {
                        "split": "",
                        "folder": scenario.rel_folder,
                        "frame_idx": idx,
                        "status": "SKIP_EMPTY_GT",
                        "gt_count": 0,
                        "action_source": action_source,
                    }
                )
                # Official preprocessing skips this frame. Do not update the
                # previous-position cache to stay compatible with that behavior.
                continue

            frame["gt_ids"] = np.asarray(gt_ids, dtype=np.int64)
            frame["gt_boxes"] = np.stack(gt_boxes).astype(np.float64)
            frame["gt_names"] = np.asarray(gt_names)
            frame["num_points"] = np.asarray(num_points_list, dtype=np.int64)
            frame["npc2world"] = np.stack(npc2world_list).astype(np.float64)

            infos.append(frame)
            stats.frames_written += 1
            previous_positions = current_positions.copy()

            tc = frame["traffic_controls"]
            frame_rows.append(
                {
                    "split": "",
                    "folder": scenario.rel_folder,
                    "frame_idx": idx,
                    "status": "OK",
                    "gt_count": len(gt_boxes),
                    "traffic_lights": len(tc["traffic_lights"]),
                    "affecting_traffic_lights": len(tc["relevant_traffic_light_ids"]),
                    "stop_signs": len(tc["stop_signs"]),
                    "affecting_stop_signs": len(tc["relevant_stop_sign_ids"]),
                    "action_source": action_source,
                }
            )

        except Exception as exc:
            stats.frames_failed += 1
            errors.append(
                {
                    "stage": "frame",
                    "scenario": scenario.rel_folder,
                    "frame": idx,
                    "source": str(anno_path),
                    "error": repr(exc),
                }
            )
            frame_rows.append(
                {
                    "split": "",
                    "folder": scenario.rel_folder,
                    "frame_idx": idx,
                    "status": "ERROR",
                    "gt_count": "",
                    "error": repr(exc),
                }
            )

    return infos, stats


def scenario_key_match(rel_folder: str, values: set[str]) -> bool:
    normalized = rel_folder.strip("./")
    name = Path(normalized).name
    return rel_folder in values or normalized in values or name in values


def load_val_names(path: Path) -> set[str]:
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("val", [])
        if not isinstance(data, list):
            raise ValueError("val JSON must be a list or {'val': [...]}")
        return {str(x).strip() for x in data}
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def split_scenarios(
    scenarios: list[Scenario],
    val_ratio: float,
    val_list: Optional[Path],
    seed: int,
) -> tuple[list[Scenario], list[Scenario]]:
    if val_list is not None:
        names = load_val_names(val_list)
        val = [s for s in scenarios if scenario_key_match(s.rel_folder, names)]
        train = [s for s in scenarios if s not in val]
        unmatched = [x for x in names if not any(scenario_key_match(s.rel_folder, {x}) for s in scenarios)]
        if unmatched:
            print(f"[warning] {len(unmatched)} val entries did not match any scenario", file=sys.stderr)
        return train, val

    if not (0.0 <= val_ratio < 1.0):
        raise ValueError("--val-ratio must be in [0,1)")
    if val_ratio <= 0.0 or len(scenarios) <= 1:
        return scenarios, []

    rng = random.Random(seed)
    shuffled = scenarios.copy()
    rng.shuffle(shuffled)
    n_val = max(1, round(len(shuffled) * val_ratio))
    val_set = {s.root for s in shuffled[:n_val]}
    val = [s for s in scenarios if s.root in val_set]
    train = [s for s in scenarios if s.root not in val_set]
    return train, val


def potential_map_count(info: dict[str, Any], map_infos: dict[str, dict[str, Any]], pc_range: np.ndarray) -> Counter:
    counts = Counter()
    town = info["town_name"]
    if town not in map_infos:
        return counts

    m = map_infos[town]
    w2l = np.asarray(info["sensors"]["LIDAR_TOP"]["world2lidar"], dtype=np.float64)
    ego_xy = np.linalg.inv(w2l)[:2, 3]

    for pts, samples, typ in zip(m["lane_points"], m["lane_sample_points"], m["lane_types"]):
        if typ not in MAP_ELEMENT_CLASS:
            continue
        if np.min(np.linalg.norm(samples[:, :2] - ego_xy, axis=-1)) >= 50:
            continue
        h = np.concatenate([pts, np.ones((len(pts), 1))], axis=1)
        local = (w2l @ h.T).T
        mask = (
            (local[:, 0] > pc_range[0])
            & (local[:, 0] < pc_range[3])
            & (local[:, 1] > pc_range[1])
            & (local[:, 1] < pc_range[4])
        )
        if int(mask.sum()) > 1:
            counts[typ] += 1

    for pts, typ in zip(m["trigger_volumes_points"], m["trigger_volumes_types"]):
        if typ not in MAP_ELEMENT_CLASS:
            continue
        h = np.concatenate([pts, np.ones((len(pts), 1))], axis=1)
        local = (w2l @ h.T).T
        mask = (
            (local[:, 0] > pc_range[0])
            & (local[:, 0] < pc_range[3])
            & (local[:, 1] > pc_range[1])
            & (local[:, 1] < pc_range[4])
        )
        if bool(mask.all()):
            counts[typ] += 1
    return counts


def temporal_validity(
    infos: list[dict[str, Any]],
    sample_interval: int,
    future_frames: int,
) -> dict[int, tuple[float, float]]:
    """
    Return per-list-index:
      ego future valid ratio,
      agent future ID valid ratio.

    This mirrors the list-index based lookup used by B2D_VAD_Dataset.
    """
    result: dict[int, tuple[float, float]] = {}

    for idx, cur in enumerate(infos):
        ego_valid = 0
        agent_total = len(cur["gt_ids"]) * future_frames
        agent_valid = 0

        cur_ids = list(cur["gt_ids"])
        for step in range(1, future_frames + 1):
            adj_idx = idx + step * sample_interval
            if adj_idx >= len(infos):
                continue
            adj = infos[adj_idx]
            if adj["folder"] != cur["folder"]:
                continue

            ego_valid += 1
            ids = set(int(x) for x in adj["gt_ids"])
            agent_valid += sum(1 for actor_id in cur_ids if int(actor_id) in ids)

        ego_ratio = ego_valid / future_frames if future_frames else 1.0
        agent_ratio = agent_valid / agent_total if agent_total else 1.0
        result[idx] = (ego_ratio, agent_ratio)

    return result


def validate_infos(
    split: str,
    infos: list[dict[str, Any]],
    map_infos: dict[str, dict[str, Any]],
    pc_range: np.ndarray,
    sample_interval: int,
    future_frames: int,
    rows_by_key: dict[tuple[str, int], dict[str, Any]],
    errors: list[dict[str, Any]],
) -> dict[str, Any]:
    validity = temporal_validity(infos, sample_interval, future_frames)
    nan_inf_samples = 0
    missing_maps = 0
    temporal_index_warnings = 0

    by_folder_last_frame: dict[str, int] = {}

    for i, info in enumerate(infos):
        key = (info["folder"], int(info["frame_idx"]))
        row = rows_by_key.get(key)
        if row is None:
            continue
        row["split"] = split

        map_counts = potential_map_count(info, map_infos, pc_range)
        row["map_vectors_total"] = sum(map_counts.values())
        for typ in MAP_ELEMENT_CLASS:
            row[f"map_{typ}"] = map_counts.get(typ, 0)

        ego_valid, agent_valid = validity[i]
        row["ego_future_valid_ratio"] = round(ego_valid, 6)
        row["agent_future_valid_ratio"] = round(agent_valid, 6)

        numeric_items = [
            info["gt_boxes"],
            info["npc2world"],
            info["ego_translation"],
            info["ego_vel"],
            info["ego_accel"],
            info["ego_rotation_rate"],
            info["sensors"]["LIDAR_TOP"]["world2lidar"],
        ]
        finite = all(np.isfinite(np.asarray(x, dtype=np.float64)).all() for x in numeric_items)
        row["finite_numeric"] = finite
        if not finite:
            nan_inf_samples += 1
            errors.append(
                {
                    "stage": "validate",
                    "scenario": info["folder"],
                    "frame": info["frame_idx"],
                    "error": "NaN/Inf detected",
                }
            )

        if info["town_name"] not in map_infos:
            missing_maps += 1
            row["map_status"] = "MISSING"
        else:
            row["map_status"] = "OK"

        previous = by_folder_last_frame.get(info["folder"])
        if previous is not None and int(info["frame_idx"]) != previous + 1:
            temporal_index_warnings += 1
            row["frame_gap_from_previous"] = int(info["frame_idx"]) - previous
        else:
            row["frame_gap_from_previous"] = 1 if previous is not None else 0
        by_folder_last_frame[info["folder"]] = int(info["frame_idx"])

    return {
        "split": split,
        "samples": len(infos),
        "scenarios": len({x["folder"] for x in infos}),
        "nan_inf_samples": nan_inf_samples,
        "missing_map_samples": missing_maps,
        "frame_gap_samples": temporal_index_warnings,
        "mean_ego_future_valid_ratio": float(np.mean([x[0] for x in validity.values()])) if validity else 0.0,
        "mean_agent_future_valid_ratio": float(np.mean([x[1] for x in validity.values()])) if validity else 0.0,
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Build official-schema Bench2DriveZoo VAD-B2D info PKLs from raw clips."
    )
    p.add_argument(
        "data_root",
        nargs="?",
        type=Path,
        default=None,
        help="Dataset root containing one or more clip/anno folders. Default: script directory.",
    )
    p.add_argument("--maps-root", type=Path, default=None)
    p.add_argument("--output-dir", type=Path, default=Path("outputs/vad_infos"))
    p.add_argument("--anno-dir-name", default="anno")
    p.add_argument("--val-ratio", type=float, default=0.0)
    p.add_argument("--val-list", type=Path, default=None, help="TXT or JSON val scenario list.")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument(
        "--visibility-filter",
        choices=("off", "auto", "official"),
        default="off",
        help=(
            "official: require depth images + Bench2DriveZoo vis_utils; "
            "auto: use them when available; off: do not filter by depth visibility."
        ),
    )
    p.add_argument("--bench2drive-zoo-root", type=Path, default=None)
    p.add_argument("--max-distance", type=float, default=MAX_DISTANCE_DEFAULT)
    p.add_argument("--z-threshold", type=float, default=FILTER_Z_THRESHOLD_DEFAULT)
    p.add_argument("--sample-interval", type=int, default=DEFAULT_SAMPLE_INTERVAL)
    p.add_argument("--past-frames", type=int, default=DEFAULT_PAST_FRAMES)
    p.add_argument("--future-frames", type=int, default=DEFAULT_FUTURE_FRAMES)
    p.add_argument(
        "--point-cloud-range",
        nargs=6,
        type=float,
        default=DEFAULT_PC_RANGE.tolist(),
        metavar=("XMIN", "YMIN", "ZMIN", "XMAX", "YMAX", "ZMAX"),
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    script_dir = Path(__file__).resolve().parent
    data_root = (args.data_root or script_dir).expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()

    if not data_root.is_dir():
        print(f"[ERROR] data root not found: {data_root}", file=sys.stderr)
        return 1

    try:
        maps_root = choose_maps_root(data_root, args.maps_root)
        scenarios = discover_scenarios(data_root, args.anno_dir_name)
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    if not scenarios:
        print(f"[ERROR] no *{args.anno_dir_name}/*.json.gz scenarios found under {data_root}", file=sys.stderr)
        return 1

    vis_utils = None
    visibility_mode = args.visibility_filter
    if visibility_mode in ("auto", "official"):
        vis_utils = find_vis_utils(
            args.bench2drive_zoo_root.expanduser().resolve()
            if args.bench2drive_zoo_root is not None
            else None
        )
        if vis_utils is None:
            if visibility_mode == "official":
                print(
                    "[ERROR] official visibility mode requires Bench2DriveZoo/mmcv/datasets/vis_utils.py",
                    file=sys.stderr,
                )
                return 1
            print("[warning] vis_utils not found; visibility filtering disabled", file=sys.stderr)
            visibility_mode = "off"

    try:
        train_scenarios, val_scenarios = split_scenarios(
            scenarios, args.val_ratio, args.val_list, args.seed
        )
    except Exception as exc:
        print(f"[ERROR] split failed: {exc}", file=sys.stderr)
        return 1

    output_dir.mkdir(parents=True, exist_ok=True)
    errors: list[dict[str, Any]] = []
    frame_rows: list[dict[str, Any]] = []
    scenario_rows: list[dict[str, Any]] = []

    print(f"[data_root] {data_root}")
    print(f"[maps_root] {maps_root}")
    print(f"[output]    {output_dir}")
    print(f"[scenarios] train={len(train_scenarios)}, val={len(val_scenarios)}")
    print(f"[visibility] {visibility_mode}")

    required_towns = {s.town for s in scenarios}
    print(f"[map] required towns: {', '.join(sorted(required_towns))}")
    map_infos = build_map_infos(
        maps_root,
        errors,
        required_towns=required_towns,
    )

    missing_towns = sorted(required_towns - set(map_infos))
    if missing_towns:
        print(
            f"[ERROR] required map(s) could not be built: {', '.join(missing_towns)}",
            file=sys.stderr,
        )
        write_csv(output_dir / "debug" / "errors.csv", errors)
        return 1

    save_pickle(output_dir / "b2d_map_infos.pkl", map_infos)
    print(f"[map] towns={len(map_infos)} -> b2d_map_infos.pkl")

    split_infos: dict[str, list[dict[str, Any]]] = {"train": [], "val": []}

    for split_name, split_scenarios_list in (("train", train_scenarios), ("val", val_scenarios)):
        for num, scenario in enumerate(split_scenarios_list, 1):
            print(
                f"[{split_name} {num}/{len(split_scenarios_list)}] "
                f"{scenario.rel_folder} ({scenario.town})"
            )
            infos, stats = process_scenario(
                scenario=scenario,
                data_root=data_root,
                visibility_mode=visibility_mode,
                vis_utils=vis_utils,
                max_distance=args.max_distance,
                z_threshold=args.z_threshold,
                errors=errors,
                frame_rows=frame_rows,
            )
            split_infos[split_name].extend(infos)
            scenario_rows.append(
                {
                    "split": split_name,
                    "folder": scenario.rel_folder,
                    "town": scenario.town,
                    **stats.__dict__,
                }
            )

        # Important: keep each scenario contiguous and frame-sorted.
        split_infos[split_name].sort(key=lambda x: (x["folder"], int(x["frame_idx"])))
        save_pickle(output_dir / f"b2d_infos_{split_name}.pkl", split_infos[split_name])
        print(f"[{split_name}] samples={len(split_infos[split_name])}")

    rows_by_key = {
        (str(r.get("folder")), int(r["frame_idx"])): r
        for r in frame_rows
        if r.get("status") == "OK" and str(r.get("frame_idx", "")).isdigit()
    }
    pc_range = np.asarray(args.point_cloud_range, dtype=np.float64)

    validation = []
    for split_name in ("train", "val"):
        validation.append(
            validate_infos(
                split=split_name,
                infos=split_infos[split_name],
                map_infos=map_infos,
                pc_range=pc_range,
                sample_interval=args.sample_interval,
                future_frames=args.future_frames,
                rows_by_key=rows_by_key,
                errors=errors,
            )
        )

    write_csv(output_dir / "debug" / "sample_summary.csv", frame_rows)
    write_csv(output_dir / "debug" / "scenario_summary.csv", scenario_rows)
    write_csv(output_dir / "debug" / "errors.csv", errors)
    write_csv(output_dir / "debug" / "validation_summary.csv", validation)

    metadata = {
        "format": "Bench2DriveZoo B2D_VAD_Dataset info contract",
        "data_root": str(data_root),
        "maps_root": str(maps_root),
        "visibility_filter": visibility_mode,
        "max_distance": args.max_distance,
        "z_threshold": args.z_threshold,
        "sample_interval": args.sample_interval,
        "past_frames": args.past_frames,
        "future_frames": args.future_frames,
        "point_cloud_range": pc_range.tolist(),
        "polyline_points_num": DEFAULT_POLYLINE_POINTS,
        "map_element_class": MAP_ELEMENT_CLASS,
        "train_scenarios": [s.rel_folder for s in train_scenarios],
        "val_scenarios": [s.rel_folder for s in val_scenarios],
        "notes": [
            "Agent/ego future targets are derived by B2D_VAD_Dataset from temporal info entries.",
            "Map GT vectors are derived by B2D_VAD_Dataset.get_map_info from b2d_map_infos.pkl.",
            "traffic_controls is an extension and is ignored by the stock loader unless custom code reads it.",
        ],
    }
    (output_dir / "build_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("\n=== done ===")
    print(output_dir / "b2d_infos_train.pkl")
    print(output_dir / "b2d_infos_val.pkl")
    print(output_dir / "b2d_map_infos.pkl")
    print(output_dir / "debug" / "sample_summary.csv")
    print(output_dir / "debug" / "validation_summary.csv")
    if errors:
        print(f"[warning] errors/warnings recorded: {len(errors)} -> debug/errors.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
