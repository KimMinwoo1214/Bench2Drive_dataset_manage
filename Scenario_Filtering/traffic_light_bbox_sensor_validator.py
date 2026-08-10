#!/usr/bin/env python3
"""
Bench2Drive traffic-light bbox association validator using sensor evidence.

Why this script exists
----------------------
Bench2Drive's collector associates each traffic-light actor with a nearby
CityObjectLabel.TrafficLight level bounding box using a greedy nearest-neighbor
loop. The selected bbox is removed from the candidate list. In dense junctions
this can produce cyclic bbox/actor permutations.

Important: for traffic lights, JSON `location`/`center` come from the selected
level bbox, not directly from the traffic-light actor transform. Therefore
`location <-> center` consistency cannot detect this error.

This validator uses independent sensor evidence:
  1) instance segmentation: identifies the physical traffic-light head/mesh;
  2) RGB: estimates the visible signal state (Red/Yellow/Green) for that mesh;
  3) depth: estimates the physical mesh position in world coordinates;
  4) JSON actor metadata: state, distance, trigger_volume_location.

It never edits the original json.gz files.

Conservative decision policy
----------------------------
- CONFIRMED_CYCLE: a closed permutation cycle where every actor-to-physical-bbox
  assignment is supported by strong sensor evidence and no strong evidence
  conflicts.
- CANDIDATE: a likely mismatch/cycle, but evidence is incomplete.
- PASS: current bbox is the best supported bbox and evidence is sufficient.
- MISSING_BBOX: annotation object exists but has no usable center/extent.
- MISSING_BBOX_CANDIDATE: a stable nearby traffic-light instance is visible in
  instance segmentation but no annotation bbox consistently projects to it.
- UNRESOLVED: not enough independent evidence; do NOT auto-fix.

Dependencies
------------
- numpy
- opencv-python (cv2)
No scipy is required.

Recommended first run (Town12 only)
-----------------------------------
python3 traffic_light_bbox_sensor_validator.py \
    -r /home/kmw/dataset/Carla \
    -o ./tl_sensor_qa \
    --path-contains Town12 \
    --cameras front,front_left,front_right \
    --frame-step 5 \
    --visualize 3

For maximum coverage (slower):
python3 traffic_light_bbox_sensor_validator.py \
    -r /home/kmw/dataset/Carla \
    -o ./tl_sensor_qa_all \
    --path-contains Town12 \
    --cameras all \
    --frame-step 5 \
    --visualize 3
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import cv2
import numpy as np


# -----------------------------------------------------------------------------
# Bench2Drive camera folders
# -----------------------------------------------------------------------------

CAMERAS = {
    "front": {
        "key": "CAM_FRONT",
        "rgb": "rgb_front",
        "instance": "instance_front",
        "depth": "depth_front",
    },
    "front_left": {
        "key": "CAM_FRONT_LEFT",
        "rgb": "rgb_front_left",
        "instance": "instance_front_left",
        "depth": "depth_front_left",
    },
    "front_right": {
        "key": "CAM_FRONT_RIGHT",
        "rgb": "rgb_front_right",
        "instance": "instance_front_right",
        "depth": "depth_front_right",
    },
    "back": {
        "key": "CAM_BACK",
        "rgb": "rgb_back",
        "instance": "instance_back",
        "depth": "depth_back",
    },
    "back_left": {
        "key": "CAM_BACK_LEFT",
        "rgb": "rgb_back_left",
        "instance": "instance_back_left",
        "depth": "depth_back_left",
    },
    "back_right": {
        "key": "CAM_BACK_RIGHT",
        "rgb": "rgb_back_right",
        "instance": "instance_back_right",
        "depth": "depth_back_right",
    },
}

STATE_SET = {"RED", "YELLOW", "GREEN"}


# -----------------------------------------------------------------------------
# Data classes
# -----------------------------------------------------------------------------

@dataclass
class ActorFrame:
    frame: str
    ego_xy: np.ndarray
    state: str
    distance: Optional[float]
    trigger_xyz: Optional[np.ndarray]
    center: Optional[np.ndarray]
    extent: Optional[np.ndarray]
    rotation: Optional[np.ndarray]


@dataclass
class BBoxCluster:
    idx: int
    centers: List[np.ndarray] = field(default_factory=list)
    extents: List[np.ndarray] = field(default_factory=list)
    rotations: List[np.ndarray] = field(default_factory=list)
    owner_votes: Counter = field(default_factory=Counter)
    frames: set = field(default_factory=set)

    @property
    def center(self) -> np.ndarray:
        return np.median(np.stack(self.centers), axis=0)

    @property
    def extent(self) -> np.ndarray:
        return np.median(np.stack(self.extents), axis=0)

    @property
    def rotation(self) -> np.ndarray:
        if not self.rotations:
            return np.zeros(3, dtype=np.float64)
        return np.median(np.stack(self.rotations), axis=0)

    def dominant_owner(self) -> Tuple[str, int, float]:
        if not self.owner_votes:
            return "", 0, 0.0
        owner, count = self.owner_votes.most_common(1)[0]
        total = sum(self.owner_votes.values())
        return str(owner), int(count), float(count / total) if total else 0.0


@dataclass
class CameraInstanceObs:
    frame: str
    camera: str
    uid: int
    pixel_count: int
    state: str
    state_conf: float
    world_xyz: Optional[np.ndarray]
    ego_distance_xy: Optional[float]


@dataclass
class AggregatedInstanceFrame:
    frame: str
    state: str
    state_conf: float
    world_xyz: Optional[np.ndarray]
    ego_distance_xy: Optional[float]
    cameras: int
    pixels: int


@dataclass
class PairMetrics:
    actor_id: str
    cluster_id: int
    instance_uid: Optional[int]
    state_count: int
    state_matches: int
    state_accuracy: float
    state_distinct: int
    range_count: int
    range_mae: float
    range_shape_rmse: float
    trigger_distance: float


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Bench2Drive traffic-light bbox validator using instance/RGB/depth"
    )
    p.add_argument("-r", "--root", required=True,
                   help="scenario folder or parent folder containing scenarios")
    p.add_argument("-o", "--output", default="tl_sensor_qa",
                   help="output directory")
    p.add_argument("--path-contains", default="",
                   help="only scenarios whose path contains this text, e.g. Town12")
    p.add_argument(
        "--cameras",
        default="front,front_left,front_right",
        help="comma-separated: front,front_left,front_right,back,back_left,back_right or all",
    )
    p.add_argument("--frame-step", type=int, default=5,
                   help="read sensor images every N annotation frames (default 5)")
    p.add_argument("--max-frames", type=int, default=0,
                   help="maximum sampled frames per scenario; 0 = unlimited")
    p.add_argument("--visualize", type=int, default=3,
                   help="QA images per problematic scenario; 0 disables")
    p.add_argument("--qa-camera", default="front", choices=list(CAMERAS),
                   help="camera used for QA images")

    # CARLA 0.9.15 semantic tag after tag re-numbering.
    p.add_argument("--traffic-light-tag", type=int, default=7,
                   help="instance segmentation semantic tag for TrafficLight (CARLA 0.9.15: 7)")

    # Physical bbox clustering / local candidate group.
    p.add_argument("--bbox-cluster-tol", type=float, default=0.50,
                   help="world-space center distance to merge same physical bbox [m]")
    p.add_argument("--group-radius", type=float, default=35.0,
                   help="connect nearby bbox clusters into one local junction group [m]")

    # Bbox -> instance overlap.
    p.add_argument("--bbox-pad", type=int, default=5,
                   help="pixel dilation around projected bbox polygon")
    p.add_argument("--min-overlap-pixels", type=int, default=2,
                   help="minimum traffic-light instance pixels inside projected bbox")
    p.add_argument("--bbox-instance-consensus", type=float, default=0.75,
                   help="minimum temporal vote ratio for bbox->instance mapping")
    p.add_argument("--min-bbox-instance-votes", type=int, default=3,
                   help="minimum votes for bbox->instance mapping")

    # RGB state classifier.
    p.add_argument("--min-instance-pixels", type=int, default=2,
                   help="minimum traffic-light mask pixels to analyze")
    p.add_argument("--min-state-pixels", type=int, default=2,
                   help="minimum active-color pixels for state classification")
    p.add_argument("--state-color-ratio", type=float, default=1.20,
                   help="top/second active-color score ratio")

    # Actor<->instance evidence.
    p.add_argument("--min-state-frames", type=int, default=5,
                   help="minimum comparable RGB/JSON state frames")
    p.add_argument("--strong-state-accuracy", type=float, default=0.90,
                   help="state accuracy required for strong state evidence")
    p.add_argument("--strong-state-margin", type=float, default=0.15,
                   help="accuracy gap to second candidate for strong state evidence")
    p.add_argument("--min-range-frames", type=int, default=3,
                   help="minimum depth/range comparable frames")
    p.add_argument("--strong-range-mae", type=float, default=6.0,
                   help="maximum median actor-distance error for strong range evidence [m]")
    p.add_argument("--strong-range-shape", type=float, default=3.0,
                   help="maximum centered range curve RMSE [m]")
    p.add_argument("--strong-range-margin", type=float, default=2.0,
                   help="range cost gap to second candidate for strong evidence [m]")
    p.add_argument("--candidate-range-mae", type=float, default=10.0,
                   help="looser range threshold for CANDIDATE [m]")
    p.add_argument("--candidate-range-margin", type=float, default=0.75,
                   help="minimum range cost gap for a non-confirmed CANDIDATE [m]")
    p.add_argument("--candidate-state-margin", type=float, default=0.05,
                   help="minimum state accuracy gap for a non-confirmed CANDIDATE")

    # Missing bbox candidate filtering.
    p.add_argument("--missing-min-frames", type=int, default=3,
                   help="minimum frames an unclaimed instance must be visible")
    p.add_argument("--missing-near-cluster", type=float, default=35.0,
                   help="unclaimed instance must be within this XY distance of an annotated bbox [m]")
    p.add_argument("--max-near-distance", type=float, default=120.0,
                   help="only flag unclaimed instances within this ego XY distance [m]")

    return p.parse_args()


# -----------------------------------------------------------------------------
# General helpers
# -----------------------------------------------------------------------------

def normalize_state(value) -> str:
    s = str(value).upper()
    if "YELLOW" in s:
        return "YELLOW"
    if "GREEN" in s:
        return "GREEN"
    if "RED" in s:
        return "RED"
    if "OFF" in s:
        return "OFF"
    return "UNKNOWN"


def safe_float(value) -> Optional[float]:
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def vec3(value) -> Optional[np.ndarray]:
    if value is None:
        return None
    try:
        a = np.asarray(value, dtype=np.float64).reshape(-1)
    except Exception:
        return None
    if a.size < 3 or not np.all(np.isfinite(a[:3])):
        return None
    return a[:3]


def load_json_gz(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def write_csv(path: Path, rows: List[dict], fields: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def relative_name(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except Exception:
        return path.name


def find_scenarios(root: Path, path_contains: str) -> List[Path]:
    root = root.resolve()
    if (root / "anno").is_dir() and any((root / "anno").glob("*.json.gz")):
        scenarios = [root]
    else:
        scenarios = []
        for anno_dir in root.rglob("anno"):
            if anno_dir.is_dir() and any(anno_dir.glob("*.json.gz")):
                scenarios.append(anno_dir.parent)
        scenarios = sorted(set(scenarios))

    if path_contains:
        needle = path_contains.lower()
        scenarios = [s for s in scenarios if needle in str(s).lower()]
    return scenarios


def parse_camera_list(text: str) -> List[str]:
    if text.strip().lower() == "all":
        return list(CAMERAS.keys())
    items = [x.strip().lower() for x in text.split(",") if x.strip()]
    bad = [x for x in items if x not in CAMERAS]
    if bad:
        raise SystemExit(f"Unknown camera name(s): {bad}")
    return items


def find_frame_file(folder: Path, frame: str, exts=(".png", ".jpg", ".jpeg")) -> Optional[Path]:
    for ext in exts:
        p = folder / f"{frame}{ext}"
        if p.exists():
            return p
    return None


def ego_xy_from_anno(anno: dict) -> Optional[np.ndarray]:
    x = safe_float(anno.get("x"))
    y = safe_float(anno.get("y"))
    if x is not None and y is not None:
        return np.array([x, y], dtype=np.float64)
    for obj in anno.get("bounding_boxes", []):
        if isinstance(obj, dict) and obj.get("class") == "ego_vehicle":
            loc = vec3(obj.get("location"))
            if loc is not None:
                return loc[:2]
    return None


# -----------------------------------------------------------------------------
# Read annotations + cluster physical bboxes
# -----------------------------------------------------------------------------

def collect_annotation_data(scenario: Path):
    actor_frames: Dict[str, Dict[str, ActorFrame]] = defaultdict(dict)
    bbox_records = []
    frame_ego: Dict[str, np.ndarray] = {}
    anno_files = sorted((scenario / "anno").glob("*.json.gz"))
    raw_missing_actor_frames = Counter()

    for path in anno_files:
        frame = path.name.replace(".json.gz", "")
        try:
            anno = load_json_gz(path)
        except Exception:
            continue
        ego_xy = ego_xy_from_anno(anno)
        if ego_xy is None:
            continue
        frame_ego[frame] = ego_xy

        for obj in anno.get("bounding_boxes", []):
            if not isinstance(obj, dict) or obj.get("class") != "traffic_light":
                continue
            aid = str(obj.get("id", ""))
            if not aid:
                continue

            center = vec3(obj.get("center"))
            extent = vec3(obj.get("extent"))
            rotation = vec3(obj.get("rotation"))
            trigger = vec3(obj.get("trigger_volume_location"))
            distance = safe_float(obj.get("distance"))

            actor_frames[aid][frame] = ActorFrame(
                frame=frame,
                ego_xy=ego_xy.copy(),
                state=normalize_state(obj.get("state")),
                distance=distance,
                trigger_xyz=trigger,
                center=center,
                extent=extent,
                rotation=rotation,
            )

            if center is None or extent is None:
                raw_missing_actor_frames[aid] += 1
            else:
                bbox_records.append({
                    "frame": frame,
                    "owner": aid,
                    "center": center,
                    "extent": extent,
                    "rotation": rotation if rotation is not None else np.zeros(3),
                })

    return actor_frames, bbox_records, frame_ego, anno_files, raw_missing_actor_frames


def cluster_bboxes(records: List[dict], tol: float) -> List[BBoxCluster]:
    clusters: List[BBoxCluster] = []
    for rec in records:
        center = rec["center"]
        best = -1
        best_d = float("inf")
        for i, cl in enumerate(clusters):
            d = float(np.linalg.norm(center - cl.center))
            if d < best_d:
                best_d = d
                best = i
        if best < 0 or best_d > tol:
            cl = BBoxCluster(idx=len(clusters))
            clusters.append(cl)
        else:
            cl = clusters[best]
        cl.centers.append(center)
        cl.extents.append(rec["extent"])
        cl.rotations.append(rec["rotation"])
        cl.owner_votes[rec["owner"]] += 1
        cl.frames.add(rec["frame"])

    # Stable ordering makes CSVs reproducible.
    clusters.sort(key=lambda c: tuple(np.round(c.center, 3).tolist()))
    for i, cl in enumerate(clusters):
        cl.idx = i
    return clusters


def nearest_cluster(center: np.ndarray, clusters: Sequence[BBoxCluster]) -> Tuple[int, float]:
    if not clusters:
        return -1, float("inf")
    d = np.array([np.linalg.norm(center - cl.center) for cl in clusters])
    idx = int(np.argmin(d))
    return idx, float(d[idx])


def actor_current_cluster_votes(
    actor_frames: Dict[str, Dict[str, ActorFrame]],
    clusters: Sequence[BBoxCluster],
    tol: float,
) -> Dict[str, Counter]:
    out: Dict[str, Counter] = defaultdict(Counter)
    for aid, frames in actor_frames.items():
        for obs in frames.values():
            if obs.center is None:
                continue
            idx, d = nearest_cluster(obs.center, clusters)
            if idx >= 0 and d <= max(1.0, tol * 2.0):
                out[aid][idx] += 1
    return out


def dominant_counter(counter: Counter) -> Tuple[Optional[int], int, float]:
    if not counter:
        return None, 0, 0.0
    key, count = counter.most_common(1)[0]
    total = sum(counter.values())
    return key, int(count), float(count / total) if total else 0.0


def connected_cluster_groups(clusters: Sequence[BBoxCluster], radius: float) -> Tuple[List[List[int]], Dict[int, int]]:
    n = len(clusters)
    visited = [False] * n
    groups: List[List[int]] = []
    cluster_to_group = {}

    for start in range(n):
        if visited[start]:
            continue
        stack = [start]
        visited[start] = True
        comp = []
        while stack:
            i = stack.pop()
            comp.append(i)
            pi = clusters[i].center[:2]
            for j in range(n):
                if visited[j]:
                    continue
                pj = clusters[j].center[:2]
                if np.linalg.norm(pi - pj) <= radius:
                    visited[j] = True
                    stack.append(j)
        gid = len(groups)
        for i in comp:
            cluster_to_group[i] = gid
        groups.append(sorted(comp))
    return groups, cluster_to_group


# -----------------------------------------------------------------------------
# Projection helpers
# -----------------------------------------------------------------------------

def cube_vertices(center: np.ndarray, extent: np.ndarray) -> np.ndarray:
    cx, cy, cz = center
    ex, ey, ez = extent
    return np.array([
        [cx + ex, cy + ey, cz + ez],
        [cx + ex, cy + ey, cz - ez],
        [cx + ex, cy - ey, cz + ez],
        [cx + ex, cy - ey, cz - ez],
        [cx - ex, cy + ey, cz + ez],
        [cx - ex, cy + ey, cz - ez],
        [cx - ex, cy - ey, cz + ez],
        [cx - ex, cy - ey, cz - ez],
    ], dtype=np.float64)


def project_point(world_xyz: np.ndarray, K: np.ndarray, world2cam: np.ndarray):
    p = np.array([world_xyz[0], world_xyz[1], world_xyz[2], 1.0], dtype=np.float64)
    raw = world2cam @ p
    # Bench2Drive/CARLA conversion: UE (x,y,z) -> standard camera (y,-z,x)
    cam = np.array([raw[1], -raw[2], raw[0]], dtype=np.float64)
    depth = float(cam[2])
    if depth <= 1e-6:
        return None, depth
    q = K @ cam
    if abs(q[2]) <= 1e-9:
        return None, depth
    return q[:2] / q[2], depth


def projected_bbox_mask(
    center: np.ndarray,
    extent: np.ndarray,
    K: np.ndarray,
    world2cam: np.ndarray,
    h: int,
    w: int,
    pad: int,
):
    points = []
    for v in cube_vertices(center, extent):
        uv, depth = project_point(v, K, world2cam)
        if uv is not None and depth > 0 and np.all(np.isfinite(uv)):
            points.append(uv)
    if len(points) < 4:
        return None

    pts = np.asarray(points, dtype=np.float32)
    hull = cv2.convexHull(pts).reshape(-1, 2)
    x, y, bw, bh = cv2.boundingRect(hull.astype(np.int32))
    x0 = max(0, x - pad)
    y0 = max(0, y - pad)
    x1 = min(w, x + bw + pad)
    y1 = min(h, y + bh + pad)
    if x1 <= x0 or y1 <= y0:
        return None

    local = np.zeros((y1 - y0, x1 - x0), dtype=np.uint8)
    local_hull = np.round(hull - np.array([x0, y0], dtype=np.float32)).astype(np.int32)
    cv2.fillConvexPoly(local, local_hull, 255)
    if pad > 0:
        k = 2 * pad + 1
        local = cv2.dilate(local, np.ones((k, k), dtype=np.uint8), iterations=1)
    return x0, y0, x1, y1, local.astype(bool), hull


# -----------------------------------------------------------------------------
# Instance segmentation / RGB state / depth
# -----------------------------------------------------------------------------

def decode_instance_image(img: np.ndarray, traffic_light_tag: int):
    if img is None or img.ndim != 3 or img.shape[2] < 3:
        return None, None
    # Bench2Drive saves the raw CARLA OpenCV BGR(A) array.
    b = img[:, :, 0].astype(np.uint16)
    g = img[:, :, 1].astype(np.uint16)
    r = img[:, :, 2].astype(np.uint16)
    semantic = r.astype(np.uint8)
    uid = (g << 8) | b
    tl_mask = semantic == int(traffic_light_tag)
    return uid, tl_mask


def classify_light_state(rgb_bgr: np.ndarray, mask: np.ndarray, min_pixels: int, ratio: float):
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return "UNKNOWN", 0.0, {"RED": 0, "YELLOW": 0, "GREEN": 0}

    pixels = rgb_bgr[ys, xs]
    if pixels.size == 0:
        return "UNKNOWN", 0.0, {"RED": 0, "YELLOW": 0, "GREEN": 0}

    hsv = cv2.cvtColor(pixels.reshape(-1, 1, 3), cv2.COLOR_BGR2HSV).reshape(-1, 3)
    h = hsv[:, 0].astype(np.int16)
    s = hsv[:, 1].astype(np.int16)
    v = hsv[:, 2].astype(np.int16)

    # Broad enough for JPEG quality=20, but still requires saturation/brightness.
    red = (s >= 75) & (v >= 85) & ((h <= 13) | (h >= 168))
    yellow = (s >= 65) & (v >= 90) & (h >= 14) & (h <= 40)
    green = (s >= 55) & (v >= 70) & (h >= 41) & (h <= 100)

    counts = {
        "RED": int(red.sum()),
        "YELLOW": int(yellow.sum()),
        "GREEN": int(green.sum()),
    }
    ordered = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    top_state, top = ordered[0]
    second = ordered[1][1]
    total_colored = sum(counts.values())

    if top < min_pixels:
        return "UNKNOWN", 0.0, counts
    if second > 0 and top / second < ratio:
        return "UNKNOWN", float(top / max(1, total_colored)), counts

    conf = float(top / max(1, total_colored))
    return top_state, conf, counts


def read_metric_depth(path: Path) -> Optional[np.ndarray]:
    img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if img is None:
        return None

    # Bench2Drive saves convert_depth(...) which is already in meters and is
    # normally written as a single-channel PNG by cv2.imwrite.
    if img.ndim == 2:
        return img.astype(np.float32)

    if img.ndim == 3 and img.shape[2] >= 3:
        # Fallback for raw CARLA depth if a user has a differently stored copy.
        b = img[:, :, 0].astype(np.float64)
        g = img[:, :, 1].astype(np.float64)
        r = img[:, :, 2].astype(np.float64)
        normalized = (b * 65536.0 + g * 256.0 + r) / (256.0**3 - 1.0)
        return (normalized * 1000.0).astype(np.float32)
    return None


def backproject_mask_world(
    mask: np.ndarray,
    depth_m: Optional[np.ndarray],
    K: np.ndarray,
    world2cam: np.ndarray,
    max_samples: int = 128,
) -> Optional[np.ndarray]:
    if depth_m is None or depth_m.shape[:2] != mask.shape[:2]:
        return None
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return None

    if len(xs) > max_samples:
        idx = np.linspace(0, len(xs) - 1, max_samples).round().astype(int)
        xs = xs[idx]
        ys = ys[idx]

    d = depth_m[ys, xs].astype(np.float64)
    good = np.isfinite(d) & (d > 0.1) & (d < 300.0)
    if int(good.sum()) < 1:
        return None
    xs = xs[good].astype(np.float64)
    ys = ys[good].astype(np.float64)
    d = d[good]

    fx = float(K[0, 0])
    fy = float(K[1, 1])
    cx = float(K[0, 2])
    cy = float(K[1, 2])

    x_cv = (xs - cx) / fx * d
    y_cv = (ys - cy) / fy * d
    z_cv = d

    # Inverse of (raw_x, raw_y, raw_z) -> (raw_y, -raw_z, raw_x)
    raw = np.stack([z_cv, x_cv, -y_cv, np.ones_like(d)], axis=1)
    try:
        cam2world = np.linalg.inv(world2cam)
    except np.linalg.LinAlgError:
        return None
    world = (cam2world @ raw.T).T[:, :3]

    # Use medians to suppress pixels from borders/compression artifacts.
    med = np.median(world, axis=0)
    if not np.all(np.isfinite(med)):
        return None
    return med


def extract_camera_observations(
    scenario: Path,
    frame: str,
    anno: dict,
    camera_name: str,
    traffic_light_tag: int,
    min_instance_pixels: int,
    min_state_pixels: int,
    state_ratio: float,
):
    cfg = CAMERAS[camera_name]
    cam = anno.get("sensors", {}).get(cfg["key"])
    if not isinstance(cam, dict) or "intrinsic" not in cam or "world2cam" not in cam:
        return None

    rgb_path = find_frame_file(scenario / "camera" / cfg["rgb"], frame, (".jpg", ".jpeg", ".png"))
    ins_path = find_frame_file(scenario / "camera" / cfg["instance"], frame, (".png",))
    depth_path = find_frame_file(scenario / "camera" / cfg["depth"], frame, (".png",))
    if rgb_path is None or ins_path is None:
        return None

    rgb = cv2.imread(str(rgb_path), cv2.IMREAD_COLOR)
    ins = cv2.imread(str(ins_path), cv2.IMREAD_UNCHANGED)
    if rgb is None or ins is None:
        return None
    if rgb.shape[:2] != ins.shape[:2]:
        return None

    uid_img, tl_mask = decode_instance_image(ins, traffic_light_tag)
    if uid_img is None:
        return None

    depth = read_metric_depth(depth_path) if depth_path is not None else None
    if depth is not None and depth.shape[:2] != rgb.shape[:2]:
        depth = None

    K = np.asarray(cam["intrinsic"], dtype=np.float64)
    world2cam = np.asarray(cam["world2cam"], dtype=np.float64)

    uids, counts = np.unique(uid_img[tl_mask], return_counts=True) if np.any(tl_mask) else (np.array([]), np.array([]))
    observations = []
    for uid, pix_count in zip(uids.tolist(), counts.tolist()):
        if int(uid) == 0 or int(pix_count) < min_instance_pixels:
            continue
        mask = tl_mask & (uid_img == int(uid))
        state, state_conf, color_counts = classify_light_state(
            rgb, mask, min_state_pixels, state_ratio
        )
        world_xyz = backproject_mask_world(mask, depth, K, world2cam)
        observations.append({
            "uid": int(uid),
            "pixel_count": int(pix_count),
            "mask": mask,
            "state": state,
            "state_conf": float(state_conf),
            "world_xyz": world_xyz,
            "color_counts": color_counts,
        })

    return {
        "rgb": rgb,
        "uid_img": uid_img,
        "tl_mask": tl_mask,
        "depth": depth,
        "K": K,
        "world2cam": world2cam,
        "observations": observations,
    }


# -----------------------------------------------------------------------------
# Sensor pass: cluster->instance votes + instance temporal observations
# -----------------------------------------------------------------------------

def process_sensors(
    scenario: Path,
    anno_files: List[Path],
    frame_ego: Dict[str, np.ndarray],
    clusters: Sequence[BBoxCluster],
    cameras: Sequence[str],
    args: argparse.Namespace,
):
    cluster_instance_votes: Dict[int, Counter] = defaultdict(Counter)
    cluster_vote_details = defaultdict(list)
    instance_camera_obs: Dict[int, List[CameraInstanceObs]] = defaultdict(list)
    sensor_frame_count = 0
    sensor_camera_count = 0
    missing_sensor_files = Counter()

    sampled = anno_files[::max(1, args.frame_step)]
    if args.max_frames > 0:
        sampled = sampled[:args.max_frames]

    for path in sampled:
        frame = path.name.replace(".json.gz", "")
        try:
            anno = load_json_gz(path)
        except Exception:
            continue
        ego_xy = frame_ego.get(frame)
        if ego_xy is None:
            ego_xy = ego_xy_from_anno(anno)
        if ego_xy is None:
            continue

        frame_used = False
        for camera_name in cameras:
            data = extract_camera_observations(
                scenario=scenario,
                frame=frame,
                anno=anno,
                camera_name=camera_name,
                traffic_light_tag=args.traffic_light_tag,
                min_instance_pixels=args.min_instance_pixels,
                min_state_pixels=args.min_state_pixels,
                state_ratio=args.state_color_ratio,
            )
            if data is None:
                missing_sensor_files[camera_name] += 1
                continue

            frame_used = True
            sensor_camera_count += 1
            rgb = data["rgb"]
            uid_img = data["uid_img"]
            tl_mask = data["tl_mask"]
            K = data["K"]
            world2cam = data["world2cam"]
            h, w = rgb.shape[:2]

            for ob in data["observations"]:
                world_xyz = ob["world_xyz"]
                ego_d = None
                if world_xyz is not None:
                    ego_d = float(np.linalg.norm(world_xyz[:2] - ego_xy))
                instance_camera_obs[ob["uid"]].append(
                    CameraInstanceObs(
                        frame=frame,
                        camera=camera_name,
                        uid=ob["uid"],
                        pixel_count=ob["pixel_count"],
                        state=ob["state"],
                        state_conf=ob["state_conf"],
                        world_xyz=world_xyz,
                        ego_distance_xy=ego_d,
                    )
                )

            # Map each physical annotation bbox to the instance mask it actually covers.
            for cl in clusters:
                proj = projected_bbox_mask(
                    cl.center, cl.extent, K, world2cam, h, w, args.bbox_pad
                )
                if proj is None:
                    continue
                x0, y0, x1, y1, local_mask, _ = proj
                local_tl = tl_mask[y0:y1, x0:x1] & local_mask
                if not np.any(local_tl):
                    continue
                local_uid = uid_img[y0:y1, x0:x1]
                ids, cnts = np.unique(local_uid[local_tl], return_counts=True)
                if len(ids) == 0:
                    continue
                order = np.argsort(cnts)[::-1]
                top_uid = int(ids[order[0]])
                top_count = int(cnts[order[0]])
                total = int(cnts.sum())
                purity = float(top_count / total) if total else 0.0
                if top_uid == 0 or top_count < args.min_overlap_pixels:
                    continue
                # Do not require extreme per-frame purity; temporal consensus handles it.
                if purity < 0.35:
                    continue
                cluster_instance_votes[cl.idx][top_uid] += 1
                cluster_vote_details[cl.idx].append({
                    "frame": frame,
                    "camera": camera_name,
                    "uid": top_uid,
                    "pixels": top_count,
                    "purity": purity,
                })

        if frame_used:
            sensor_frame_count += 1

    return {
        "cluster_instance_votes": cluster_instance_votes,
        "cluster_vote_details": cluster_vote_details,
        "instance_camera_obs": instance_camera_obs,
        "sensor_frame_count": sensor_frame_count,
        "sensor_camera_count": sensor_camera_count,
        "missing_sensor_files": missing_sensor_files,
    }


def aggregate_instance_frames(instance_camera_obs: Dict[int, List[CameraInstanceObs]]):
    result: Dict[int, Dict[str, AggregatedInstanceFrame]] = defaultdict(dict)
    for uid, obs_list in instance_camera_obs.items():
        by_frame = defaultdict(list)
        for ob in obs_list:
            by_frame[ob.frame].append(ob)
        for frame, items in by_frame.items():
            state_votes = Counter()
            state_weight = defaultdict(float)
            positions = []
            ego_distances = []
            pixels = 0
            for ob in items:
                pixels += ob.pixel_count
                if ob.state in STATE_SET:
                    # pixel count helps tiny/noisy observations contribute less.
                    weight = max(1.0, ob.state_conf * math.sqrt(max(1, ob.pixel_count)))
                    state_weight[ob.state] += weight
                    state_votes[ob.state] += 1
                if ob.world_xyz is not None:
                    positions.append(ob.world_xyz)
                if ob.ego_distance_xy is not None and math.isfinite(ob.ego_distance_xy):
                    ego_distances.append(ob.ego_distance_xy)

            if state_weight:
                ordered = sorted(state_weight.items(), key=lambda kv: kv[1], reverse=True)
                state = ordered[0][0]
                total_w = sum(state_weight.values())
                conf = float(ordered[0][1] / total_w) if total_w else 0.0
            else:
                state = "UNKNOWN"
                conf = 0.0

            world_xyz = np.median(np.stack(positions), axis=0) if positions else None
            ego_distance_xy = float(np.median(ego_distances)) if ego_distances else None
            result[uid][frame] = AggregatedInstanceFrame(
                frame=frame,
                state=state,
                state_conf=conf,
                world_xyz=world_xyz,
                ego_distance_xy=ego_distance_xy,
                cameras=len(items),
                pixels=pixels,
            )
    return result


# -----------------------------------------------------------------------------
# Evidence metrics
# -----------------------------------------------------------------------------

def trigger_median(actor_frames: Dict[str, ActorFrame]) -> Optional[np.ndarray]:
    vals = [o.trigger_xyz for o in actor_frames.values() if o.trigger_xyz is not None]
    if not vals:
        return None
    return np.median(np.stack(vals), axis=0)


def instance_world_median(instance_frames: Dict[str, AggregatedInstanceFrame]) -> Optional[np.ndarray]:
    vals = [o.world_xyz for o in instance_frames.values() if o.world_xyz is not None]
    if not vals:
        return None
    return np.median(np.stack(vals), axis=0)


def compute_pair_metrics(
    aid: str,
    cluster_id: int,
    uid: Optional[int],
    actor_data: Dict[str, ActorFrame],
    instance_frames_all: Dict[int, Dict[str, AggregatedInstanceFrame]],
) -> PairMetrics:
    if uid is None or uid not in instance_frames_all:
        return PairMetrics(
            actor_id=aid, cluster_id=cluster_id, instance_uid=uid,
            state_count=0, state_matches=0, state_accuracy=float("nan"), state_distinct=0,
            range_count=0, range_mae=float("inf"), range_shape_rmse=float("inf"),
            trigger_distance=float("inf"),
        )

    inst = instance_frames_all[uid]

    state_pairs = []
    actor_states = set()
    range_true = []
    range_pred = []

    for frame, a in actor_data.items():
        i = inst.get(frame)
        if i is None:
            continue
        if a.state in STATE_SET and i.state in STATE_SET:
            state_pairs.append((a.state, i.state))
            actor_states.add(a.state)
        if a.distance is not None and i.world_xyz is not None:
            pred = float(np.linalg.norm(i.world_xyz[:2] - a.ego_xy))
            if math.isfinite(pred):
                range_true.append(float(a.distance))
                range_pred.append(pred)

    state_count = len(state_pairs)
    state_matches = sum(1 for a, b in state_pairs if a == b)
    state_accuracy = float(state_matches / state_count) if state_count else float("nan")

    if range_true:
        true = np.asarray(range_true, dtype=np.float64)
        pred = np.asarray(range_pred, dtype=np.float64)
        diff = pred - true
        range_mae = float(np.median(np.abs(diff)))
        # Remove constant head/root offset bias before curve-shape comparison.
        true_c = true - np.median(true)
        pred_c = pred - np.median(pred)
        range_shape = float(np.sqrt(np.mean((pred_c - true_c) ** 2)))
    else:
        range_mae = float("inf")
        range_shape = float("inf")

    trig = trigger_median(actor_data)
    inst_world = instance_world_median(inst)
    if trig is not None and inst_world is not None:
        trigger_distance = float(np.linalg.norm(trig[:2] - inst_world[:2]))
    else:
        trigger_distance = float("inf")

    return PairMetrics(
        actor_id=aid,
        cluster_id=cluster_id,
        instance_uid=uid,
        state_count=state_count,
        state_matches=state_matches,
        state_accuracy=state_accuracy,
        state_distinct=len(actor_states),
        range_count=len(range_true),
        range_mae=range_mae,
        range_shape_rmse=range_shape,
        trigger_distance=trigger_distance,
    )


def metric_state_key(m: PairMetrics):
    # Higher is better. Distinct states make state evidence more discriminative.
    acc = -1.0 if not math.isfinite(m.state_accuracy) else m.state_accuracy
    informative = 1 if m.state_distinct >= 2 else 0
    return (informative, acc, m.state_count)


def metric_range_cost(m: PairMetrics) -> float:
    if not math.isfinite(m.range_mae) or not math.isfinite(m.range_shape_rmse):
        return float("inf")
    return float(m.range_mae + m.range_shape_rmse)


def choose_actor_cluster(
    aid: str,
    current_cluster: Optional[int],
    candidate_clusters: Sequence[int],
    cluster_uid: Dict[int, Optional[int]],
    actor_data: Dict[str, ActorFrame],
    instance_frames_all: Dict[int, Dict[str, AggregatedInstanceFrame]],
    args: argparse.Namespace,
):
    metrics = [
        compute_pair_metrics(aid, cid, cluster_uid.get(cid), actor_data, instance_frames_all)
        for cid in candidate_clusters
        if cluster_uid.get(cid) is not None
    ]
    if not metrics:
        return {
            "decision": "UNRESOLVED",
            "best_cluster": current_cluster,
            "best_uid": None,
            "metrics": None,
            "state_strong": False,
            "range_strong": False,
            "state_conflict": False,
            "range_conflict": False,
            "state_margin": float("nan"),
            "range_margin": float("nan"),
            "all_metrics": [],
            "reason": "no bbox cluster has a stable instance mapping",
        }

    # ---- State ranking ----
    state_eligible = [m for m in metrics if m.state_count >= args.min_state_frames and math.isfinite(m.state_accuracy)]
    state_best = None
    state_second = None
    state_margin = float("nan")
    state_strong = False

    if state_eligible:
        state_sorted = sorted(state_eligible, key=metric_state_key, reverse=True)
        state_best = state_sorted[0]
        state_second = state_sorted[1] if len(state_sorted) > 1 else None
        second_acc = state_second.state_accuracy if state_second is not None else 0.0
        state_margin = float(state_best.state_accuracy - second_acc)
        # If only one state was ever seen, accuracy alone is much less informative.
        informative = state_best.state_distinct >= 2
        state_strong = (
            informative
            and state_best.state_accuracy >= args.strong_state_accuracy
            and (state_second is None or state_margin >= args.strong_state_margin)
        )

    # ---- Range ranking ----
    range_eligible = [m for m in metrics if m.range_count >= args.min_range_frames and math.isfinite(metric_range_cost(m))]
    range_best = None
    range_second = None
    range_margin = float("nan")
    range_strong = False

    if range_eligible:
        range_sorted = sorted(range_eligible, key=metric_range_cost)
        range_best = range_sorted[0]
        range_second = range_sorted[1] if len(range_sorted) > 1 else None
        best_cost = metric_range_cost(range_best)
        second_cost = metric_range_cost(range_second) if range_second is not None else float("inf")
        range_margin = float(second_cost - best_cost) if math.isfinite(second_cost) else float("inf")
        range_strong = (
            range_best.range_mae <= args.strong_range_mae
            and range_best.range_shape_rmse <= args.strong_range_shape
            and (range_second is None or range_margin >= args.strong_range_margin)
        )

    state_cluster = state_best.cluster_id if state_best is not None else None
    range_cluster = range_best.cluster_id if range_best is not None else None

    # Strong independent signals disagree -> never auto-fix.
    if state_strong and range_strong and state_cluster != range_cluster:
        return {
            "decision": "UNRESOLVED",
            "best_cluster": current_cluster,
            "best_uid": cluster_uid.get(current_cluster) if current_cluster is not None else None,
            "metrics": None,
            "state_strong": True,
            "range_strong": True,
            "state_conflict": True,
            "range_conflict": True,
            "state_margin": state_margin,
            "range_margin": range_margin,
            "all_metrics": metrics,
            "reason": f"strong state says B{state_cluster}, strong range says B{range_cluster}",
        }

    chosen_cluster = None
    chosen_metrics = None
    decision = "UNRESOLVED"
    reason = "insufficient evidence"

    if state_strong and range_strong and state_cluster == range_cluster:
        chosen_cluster = state_cluster
        chosen_metrics = state_best
        decision = "CONFIRMED"
        reason = "strong state and strong range agree"
    elif state_strong:
        # Only another STRONG independent signal is allowed to veto this, and
        # that conflict was already handled above. A weak/noisy range ranking
        # must not downgrade strong informative state evidence.
        chosen_cluster = state_cluster
        chosen_metrics = state_best
        decision = "CONFIRMED"
        reason = "strong informative state evidence"
    elif range_strong:
        # Same rule in the other direction: weak RGB evidence cannot veto a
        # strong depth/range assignment.
        chosen_cluster = range_cluster
        chosen_metrics = range_best
        decision = "CONFIRMED"
        reason = "strong depth/range evidence"
    else:
        # Looser candidate path: do not call it confirmed.
        if (
            range_best is not None
            and range_best.range_mae <= args.candidate_range_mae
            and (range_second is None or (math.isfinite(range_margin) and range_margin >= args.candidate_range_margin))
        ):
            chosen_cluster = range_best.cluster_id
            chosen_metrics = range_best
            decision = "CANDIDATE"
            reason = "moderate range evidence with a unique best candidate"
        elif (
            state_best is not None
            and state_best.state_accuracy >= 0.80
            and state_best.state_distinct >= 2
            and (state_second is None or (math.isfinite(state_margin) and state_margin >= args.candidate_state_margin))
        ):
            chosen_cluster = state_best.cluster_id
            chosen_metrics = state_best
            decision = "CANDIDATE"
            reason = "moderate informative state evidence with a unique best candidate"

    if chosen_cluster is None:
        chosen_cluster = current_cluster
        if current_cluster is not None:
            for m in metrics:
                if m.cluster_id == current_cluster:
                    chosen_metrics = m
                    break

    return {
        "decision": decision,
        "best_cluster": chosen_cluster,
        "best_uid": cluster_uid.get(chosen_cluster) if chosen_cluster is not None else None,
        "metrics": chosen_metrics,
        "state_strong": state_strong,
        "range_strong": range_strong,
        "state_conflict": False,
        "range_conflict": False,
        "state_margin": state_margin,
        "range_margin": range_margin,
        "all_metrics": metrics,
        "reason": reason,
    }


# -----------------------------------------------------------------------------
# Cycle logic
# -----------------------------------------------------------------------------

def decompose_cycles(mapping: Dict[str, str]) -> List[List[str]]:
    cycles = []
    globally_seen = set()
    for start in sorted(mapping):
        if start in globally_seen:
            continue
        path = []
        pos = {}
        cur = start
        while cur in mapping and cur not in pos and cur not in globally_seen:
            pos[cur] = len(path)
            path.append(cur)
            cur = mapping[cur]
        if cur in pos:
            cyc = path[pos[cur]:]
            if len(cyc) > 1:
                cycles.append(cyc)
        globally_seen.update(path)
    return cycles


def cycle_direction(
    cycle: List[str],
    actor_expected_cluster: Dict[str, int],
    actor_current_cluster: Dict[str, int],
    clusters: Sequence[BBoxCluster],
):
    """
    Direction is defined from the user's visual point of view:

        correct bbox position -> bbox currently attached to that actor ID

    atan2 ascending is CCW in CARLA world XY. Therefore +1 means each actor's
    current bbox is one CCW position away from its expected/correct bbox.
    """
    if len(cycle) == 2:
        return "SWAP", ""

    expected_ids = []
    pts = []
    for aid in cycle:
        cid = actor_expected_cluster.get(aid)
        if cid is None or cid < 0:
            return "UNKNOWN", ""
        expected_ids.append(cid)
        pts.append(clusters[cid].center[:2])

    if len(set(expected_ids)) != len(expected_ids):
        return "NON_UNIFORM", "duplicate expected clusters"

    pts = np.stack(pts)
    centroid = pts.mean(axis=0)
    angles = np.arctan2(pts[:, 1] - centroid[1], pts[:, 0] - centroid[0])
    order = np.argsort(angles)
    ordered_clusters = [expected_ids[i] for i in order]
    rank = {cid: i for i, cid in enumerate(ordered_clusters)}
    n = len(ordered_clusters)

    offsets = []
    for aid in cycle:
        exp = actor_expected_cluster.get(aid)
        cur = actor_current_cluster.get(aid)
        if exp not in rank or cur not in rank:
            return "NON_UNIFORM", "current/expected cluster sets differ"
        offsets.append((rank[cur] - rank[exp]) % n)

    if len(set(offsets)) != 1:
        return "NON_UNIFORM", str(offsets)

    k = offsets[0]
    if k == 0:
        return "IDENTITY", "0"
    if n % 2 == 0 and k == n // 2:
        return "HALF_TURN", str(k)

    signed = k if k <= n // 2 else k - n
    if signed > 0:
        return "CCW", f"+{signed}"
    return "CW", str(signed)



# -----------------------------------------------------------------------------
# Missing bbox candidates
# -----------------------------------------------------------------------------

def find_unclaimed_instances(
    instance_frames_all: Dict[int, Dict[str, AggregatedInstanceFrame]],
    claimed_uids: set,
    clusters: Sequence[BBoxCluster],
    args: argparse.Namespace,
):
    rows = []
    if not clusters:
        return rows

    cluster_xy = np.stack([c.center[:2] for c in clusters])
    for uid, frames in instance_frames_all.items():
        if uid in claimed_uids:
            continue
        world_vals = [o.world_xyz for o in frames.values() if o.world_xyz is not None]
        if len(world_vals) < args.missing_min_frames:
            continue
        world_med = np.median(np.stack(world_vals), axis=0)
        nearest = float(np.min(np.linalg.norm(cluster_xy - world_med[:2][None, :], axis=1)))
        if nearest > args.missing_near_cluster:
            continue
        ego_ds = [o.ego_distance_xy for o in frames.values() if o.ego_distance_xy is not None and math.isfinite(o.ego_distance_xy)]
        median_ego_d = float(np.median(ego_ds)) if ego_ds else float("inf")
        if math.isfinite(median_ego_d) and median_ego_d > args.max_near_distance:
            continue

        rows.append({
            "instance_uid": uid,
            "frames_visible_with_depth": len(world_vals),
            "world_x": f"{world_med[0]:.6f}",
            "world_y": f"{world_med[1]:.6f}",
            "world_z": f"{world_med[2]:.6f}",
            "nearest_bbox_cluster_distance_m": f"{nearest:.6f}",
            "median_ego_distance_m": "" if not math.isfinite(median_ego_d) else f"{median_ego_d:.6f}",
            "status": "MISSING_BBOX_CANDIDATE",
        })
    return rows


# -----------------------------------------------------------------------------
# QA visualization
# -----------------------------------------------------------------------------

def draw_projected_cluster(
    img: np.ndarray,
    cl: BBoxCluster,
    K: np.ndarray,
    world2cam: np.ndarray,
    color,
    label: str,
    pad: int = 0,
):
    h, w = img.shape[:2]
    proj = projected_bbox_mask(cl.center, cl.extent, K, world2cam, h, w, pad)
    if proj is None:
        return False
    _, _, _, _, _, hull = proj
    pts = np.round(hull).astype(np.int32).reshape(-1, 1, 2)
    cv2.polylines(img, [pts], True, color, 2, cv2.LINE_AA)
    x = int(np.clip(np.min(hull[:, 0]), 0, w - 1))
    y = int(np.clip(np.min(hull[:, 1]), 0, h - 1))
    cv2.putText(img, label, (x, max(18, y - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)
    return True


def save_qa_images(
    scenario: Path,
    scenario_out: Path,
    anno_files: List[Path],
    clusters: Sequence[BBoxCluster],
    cluster_uid: Dict[int, Optional[int]],
    cluster_expected_actor: Dict[int, str],
    max_images: int,
    camera_name: str,
    args: argparse.Namespace,
):
    if max_images <= 0:
        return 0
    cfg = CAMERAS[camera_name]
    candidates = anno_files[::max(1, args.frame_step)]
    if not candidates:
        return 0
    if len(candidates) > max_images:
        idx = np.linspace(0, len(candidates) - 1, max_images).round().astype(int)
        candidates = [candidates[i] for i in idx]

    saved = 0
    for path in candidates:
        frame = path.name.replace(".json.gz", "")
        try:
            anno = load_json_gz(path)
        except Exception:
            continue
        cam = anno.get("sensors", {}).get(cfg["key"])
        if not isinstance(cam, dict) or "intrinsic" not in cam or "world2cam" not in cam:
            continue
        rgb_path = find_frame_file(scenario / "camera" / cfg["rgb"], frame, (".jpg", ".jpeg", ".png"))
        ins_path = find_frame_file(scenario / "camera" / cfg["instance"], frame, (".png",))
        if rgb_path is None or ins_path is None:
            continue
        img = cv2.imread(str(rgb_path), cv2.IMREAD_COLOR)
        ins = cv2.imread(str(ins_path), cv2.IMREAD_UNCHANGED)
        if img is None or ins is None:
            continue
        uid_img, tl_mask = decode_instance_image(ins, args.traffic_light_tag)
        if uid_img is None:
            continue

        K = np.asarray(cam["intrinsic"], dtype=np.float64)
        world2cam = np.asarray(cam["world2cam"], dtype=np.float64)

        # Draw mapped traffic-light instance contours in cyan.
        for uid in sorted({u for u in cluster_uid.values() if u is not None}):
            mask = (tl_mask & (uid_img == uid)).astype(np.uint8) * 255
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if contours:
                cv2.drawContours(img, contours, -1, (255, 255, 0), 1)

        for cl in clusters:
            current_owner, _, _ = cl.dominant_owner()
            expected_actor = cluster_expected_actor.get(cl.idx, "?")
            uid = cluster_uid.get(cl.idx)
            if expected_actor == "?":
                color = (0, 165, 255)  # unresolved
            elif current_owner == expected_actor:
                color = (0, 180, 0)
            else:
                color = (0, 0, 255)
            label = f"B{cl.idx} cur:{current_owner} exp:{expected_actor} uid:{uid if uid is not None else '?'}"
            draw_projected_cluster(img, cl, K, world2cam, color, label)

        out = scenario_out / "qa_images" / f"{frame}_{camera_name}.jpg"
        out.parent.mkdir(parents=True, exist_ok=True)
        if cv2.imwrite(str(out), img):
            saved += 1
    return saved


# -----------------------------------------------------------------------------
# Scenario analysis
# -----------------------------------------------------------------------------

def analyze_scenario(scenario: Path, root: Path, out_root: Path, cameras: Sequence[str], args: argparse.Namespace):
    rel = relative_name(scenario, root)
    scenario_out = out_root / rel.replace("/", "__")
    scenario_out.mkdir(parents=True, exist_ok=True)

    actor_frames, bbox_records, frame_ego, anno_files, raw_missing = collect_annotation_data(scenario)
    clusters = cluster_bboxes(bbox_records, args.bbox_cluster_tol)
    current_votes = actor_current_cluster_votes(actor_frames, clusters, args.bbox_cluster_tol)
    groups, cluster_to_group = connected_cluster_groups(clusters, args.group_radius)

    sensor = process_sensors(
        scenario=scenario,
        anno_files=anno_files,
        frame_ego=frame_ego,
        clusters=clusters,
        cameras=cameras,
        args=args,
    )
    instance_frames_all = aggregate_instance_frames(sensor["instance_camera_obs"])

    # Stable physical bbox -> instance mapping.
    cluster_uid: Dict[int, Optional[int]] = {}
    cluster_map_rows = []
    claimed_uids = set()
    for cl in clusters:
        votes = sensor["cluster_instance_votes"].get(cl.idx, Counter())
        if votes:
            uid, cnt = votes.most_common(1)[0]
            total = sum(votes.values())
            consensus = cnt / total if total else 0.0
        else:
            uid, cnt, total, consensus = None, 0, 0, 0.0

        stable = (
            uid is not None
            and cnt >= args.min_bbox_instance_votes
            and consensus >= args.bbox_instance_consensus
        )
        cluster_uid[cl.idx] = int(uid) if stable else None
        if stable:
            claimed_uids.add(int(uid))
        current_owner, owner_votes, owner_cons = cl.dominant_owner()
        cluster_map_rows.append({
            "scenario": rel,
            "bbox_cluster": cl.idx,
            "center_x": f"{cl.center[0]:.6f}",
            "center_y": f"{cl.center[1]:.6f}",
            "center_z": f"{cl.center[2]:.6f}",
            "current_owner": current_owner,
            "current_owner_consensus": f"{owner_cons:.6f}",
            "instance_uid": "" if uid is None else int(uid),
            "bbox_instance_votes": cnt,
            "bbox_instance_total_votes": total,
            "bbox_instance_consensus": f"{consensus:.6f}",
            "stable_instance_mapping": stable,
            "vote_distribution": json.dumps(votes, ensure_ascii=False),
        })

    # Determine each actor's local candidate cluster set and best supported cluster.
    actor_decisions = {}
    actor_rows = []
    actor_expected_cluster = {}

    for aid in sorted(actor_frames):
        cur, cur_votes_count, cur_cons = dominant_counter(current_votes.get(aid, Counter()))
        cur = int(cur) if cur is not None else None

        if raw_missing.get(aid, 0) > 0 and cur is None:
            actor_decisions[aid] = {
                "decision": "MISSING_BBOX",
                "best_cluster": None,
                "best_uid": None,
                "metrics": None,
                "reason": "annotation center/extent missing",
                "all_metrics": [],
                "state_strong": False,
                "range_strong": False,
                "state_margin": float("nan"),
                "range_margin": float("nan"),
            }
            continue

        if cur is not None and cur in cluster_to_group:
            candidate_clusters = groups[cluster_to_group[cur]]
        else:
            # Fallback: nearest group to actor trigger location.
            trig = trigger_median(actor_frames[aid])
            if trig is not None and clusters:
                d = np.array([np.linalg.norm(c.center[:2] - trig[:2]) for c in clusters])
                near = int(np.argmin(d))
                candidate_clusters = groups[cluster_to_group[near]]
            else:
                candidate_clusters = list(range(len(clusters)))

        decision = choose_actor_cluster(
            aid=aid,
            current_cluster=cur,
            candidate_clusters=candidate_clusters,
            cluster_uid=cluster_uid,
            actor_data=actor_frames[aid],
            instance_frames_all=instance_frames_all,
            args=args,
        )
        actor_decisions[aid] = decision
        if decision["best_cluster"] is not None:
            actor_expected_cluster[aid] = int(decision["best_cluster"])

    # Reverse map: physical cluster -> actor expected to own it, only when actor evidence is not unresolved.
    cluster_expected_actor = {}
    expected_conflicts = defaultdict(list)
    for aid, d in actor_decisions.items():
        cid = d.get("best_cluster")
        if cid is None or d.get("decision") not in ("CONFIRMED", "CANDIDATE"):
            continue
        expected_conflicts[int(cid)].append(aid)
    for cid, aids in expected_conflicts.items():
        if len(aids) == 1:
            cluster_expected_actor[cid] = aids[0]

    # Build actor -> current owner of actor's expected physical bbox.
    permutation_map = {}
    for aid, d in actor_decisions.items():
        cid = d.get("best_cluster")
        if cid is None or cid >= len(clusters):
            continue
        if d.get("decision") not in ("CONFIRMED", "CANDIDATE"):
            continue
        owner, _, owner_cons = clusters[cid].dominant_owner()
        if owner and owner_cons >= 0.90:
            permutation_map[aid] = owner

    actor_current_cluster = {}
    for aid in actor_frames:
        cur, _, _ = dominant_counter(current_votes.get(aid, Counter()))
        if cur is not None:
            actor_current_cluster[aid] = int(cur)

    cycles = decompose_cycles(permutation_map)
    cycle_rows = []
    cycle_actor_status = {}
    for i, cyc in enumerate(cycles, 1):
        cid = f"C{i}"
        decisions = [actor_decisions[a]["decision"] for a in cyc]
        unique_expected = len({actor_decisions[a].get("best_cluster") for a in cyc}) == len(cyc)
        all_confirmed = all(x == "CONFIRMED" for x in decisions)
        if all_confirmed and unique_expected:
            cstatus = "CONFIRMED_CYCLE"
        elif unique_expected and all(x in ("CONFIRMED", "CANDIDATE") for x in decisions):
            cstatus = "CANDIDATE_CYCLE"
        else:
            cstatus = "UNRESOLVED_CYCLE"
        direction, shift = cycle_direction(
            cyc, actor_expected_cluster, actor_current_cluster, clusters
        )
        for a in cyc:
            cycle_actor_status[a] = (cid, cstatus)
        cycle_rows.append({
            "scenario": rel,
            "cycle_id": cid,
            "status": cstatus,
            "length": len(cyc),
            "actors": " -> ".join(cyc + [cyc[0]]),
            "direction": direction,
            "shift": shift,
            "all_actor_decisions": json.dumps({a: actor_decisions[a]["decision"] for a in cyc}, ensure_ascii=False),
        })

    # Final actor rows/status.
    for aid in sorted(actor_frames):
        d = actor_decisions[aid]
        cur, cur_cnt, cur_cons = dominant_counter(current_votes.get(aid, Counter()))
        cur = int(cur) if cur is not None else None
        best = d.get("best_cluster")
        best = int(best) if best is not None else None
        m: Optional[PairMetrics] = d.get("metrics")
        cycle_id, cycle_status = cycle_actor_status.get(aid, ("", ""))

        if d["decision"] == "MISSING_BBOX":
            final_status = "MISSING_BBOX"
        elif cycle_status == "CONFIRMED_CYCLE":
            final_status = "CONFIRMED_CYCLE"
        elif cycle_status == "CANDIDATE_CYCLE":
            final_status = "CANDIDATE"
        elif d["decision"] == "CONFIRMED":
            final_status = "PASS" if best == cur else "CANDIDATE"
        elif d["decision"] == "CANDIDATE":
            final_status = "CANDIDATE"
        else:
            final_status = "UNRESOLVED"

        expected_owner = ""
        expected_owner_cons = 0.0
        if best is not None and 0 <= best < len(clusters):
            expected_owner, _, expected_owner_cons = clusters[best].dominant_owner()

        actor_rows.append({
            "scenario": rel,
            "actor_id": aid,
            "current_bbox_cluster": "" if cur is None else cur,
            "current_bbox_consensus": f"{cur_cons:.6f}",
            "expected_bbox_cluster": "" if best is None else best,
            "expected_bbox_instance_uid": "" if d.get("best_uid") is None else d.get("best_uid"),
            "expected_bbox_current_owner": expected_owner,
            "expected_bbox_owner_consensus": f"{expected_owner_cons:.6f}",
            "state_frames": 0 if m is None else m.state_count,
            "state_matches": 0 if m is None else m.state_matches,
            "state_accuracy": "" if m is None or not math.isfinite(m.state_accuracy) else f"{m.state_accuracy:.6f}",
            "state_distinct": 0 if m is None else m.state_distinct,
            "state_strong": bool(d.get("state_strong", False)),
            "state_margin": "" if not math.isfinite(d.get("state_margin", float("nan"))) else f"{d['state_margin']:.6f}",
            "range_frames": 0 if m is None else m.range_count,
            "range_mae_m": "" if m is None or not math.isfinite(m.range_mae) else f"{m.range_mae:.6f}",
            "range_shape_rmse_m": "" if m is None or not math.isfinite(m.range_shape_rmse) else f"{m.range_shape_rmse:.6f}",
            "range_strong": bool(d.get("range_strong", False)),
            "range_margin_m": "" if not math.isfinite(d.get("range_margin", float("nan"))) else f"{d['range_margin']:.6f}",
            "trigger_to_instance_xy_m": "" if m is None or not math.isfinite(m.trigger_distance) else f"{m.trigger_distance:.6f}",
            "decision": d.get("decision", "UNRESOLVED"),
            "reason": d.get("reason", ""),
            "cycle_id": cycle_id,
            "status": final_status,
        })

    missing_rows = find_unclaimed_instances(
        instance_frames_all=instance_frames_all,
        claimed_uids=claimed_uids,
        clusters=clusters,
        args=args,
    )
    for r in missing_rows:
        r["scenario"] = rel

    # Instance diagnostics.
    instance_rows = []
    for uid, frames in sorted(instance_frames_all.items()):
        states = Counter(o.state for o in frames.values() if o.state in STATE_SET)
        world = instance_world_median(frames)
        instance_rows.append({
            "scenario": rel,
            "instance_uid": uid,
            "frames_seen": len(frames),
            "state_distribution": json.dumps(states, ensure_ascii=False),
            "world_x": "" if world is None else f"{world[0]:.6f}",
            "world_y": "" if world is None else f"{world[1]:.6f}",
            "world_z": "" if world is None else f"{world[2]:.6f}",
            "claimed_by_bbox": uid in claimed_uids,
        })

    # QA only if there is an actual issue/candidate.
    issue_exists = any(r["status"] in ("CONFIRMED_CYCLE", "CANDIDATE", "MISSING_BBOX") for r in actor_rows) or bool(missing_rows)
    qa_saved = 0
    if issue_exists:
        qa_saved = save_qa_images(
            scenario=scenario,
            scenario_out=scenario_out,
            anno_files=anno_files,
            clusters=clusters,
            cluster_uid=cluster_uid,
            cluster_expected_actor=cluster_expected_actor,
            max_images=args.visualize,
            camera_name=args.qa_camera,
            args=args,
        )

    # Output per scenario.
    actor_fields = [
        "scenario", "actor_id", "current_bbox_cluster", "current_bbox_consensus",
        "expected_bbox_cluster", "expected_bbox_instance_uid", "expected_bbox_current_owner",
        "expected_bbox_owner_consensus", "state_frames", "state_matches", "state_accuracy",
        "state_distinct", "state_strong", "state_margin", "range_frames", "range_mae_m",
        "range_shape_rmse_m", "range_strong", "range_margin_m", "trigger_to_instance_xy_m",
        "decision", "reason", "cycle_id", "status",
    ]
    cluster_fields = [
        "scenario", "bbox_cluster", "center_x", "center_y", "center_z", "current_owner",
        "current_owner_consensus", "instance_uid", "bbox_instance_votes", "bbox_instance_total_votes",
        "bbox_instance_consensus", "stable_instance_mapping", "vote_distribution",
    ]
    cycle_fields = ["scenario", "cycle_id", "status", "length", "actors", "direction", "shift", "all_actor_decisions"]
    instance_fields = ["scenario", "instance_uid", "frames_seen", "state_distribution", "world_x", "world_y", "world_z", "claimed_by_bbox"]
    missing_fields = ["scenario", "instance_uid", "frames_visible_with_depth", "world_x", "world_y", "world_z", "nearest_bbox_cluster_distance_m", "median_ego_distance_m", "status"]

    write_csv(scenario_out / "actor_validation.csv", actor_rows, actor_fields)
    write_csv(scenario_out / "cluster_instance_map.csv", cluster_map_rows, cluster_fields)
    write_csv(scenario_out / "cycle_summary.csv", cycle_rows, cycle_fields)
    write_csv(scenario_out / "instance_observations.csv", instance_rows, instance_fields)
    write_csv(scenario_out / "missing_bbox_candidates.csv", missing_rows, missing_fields)

    confirmed_cycles = sum(r["status"] == "CONFIRMED_CYCLE" for r in cycle_rows)
    candidate_cycles = sum(r["status"] == "CANDIDATE_CYCLE" for r in cycle_rows)
    actor_counts = Counter(r["status"] for r in actor_rows)
    stable_cluster_instance = sum(bool(r["stable_instance_mapping"]) for r in cluster_map_rows)

    if confirmed_cycles > 0:
        overall = "CONFIRMED_CYCLE"
    elif actor_counts["MISSING_BBOX"] > 0 or missing_rows:
        overall = "MISSING_BBOX_CANDIDATE"
    elif candidate_cycles > 0 or actor_counts["CANDIDATE"] > 0:
        overall = "CANDIDATE"
    elif actor_counts["UNRESOLVED"] > 0:
        overall = "UNRESOLVED"
    else:
        overall = "PASS"

    summary = {
        "scenario": rel,
        "anno_frames": len(anno_files),
        "sensor_sampled_frames": sensor["sensor_frame_count"],
        "traffic_light_actors": len(actor_frames),
        "physical_bbox_clusters": len(clusters),
        "stable_bbox_instance_maps": stable_cluster_instance,
        "instance_ids_seen": len(instance_frames_all),
        "pass": actor_counts["PASS"],
        "confirmed_cycle_actors": actor_counts["CONFIRMED_CYCLE"],
        "candidate_actors": actor_counts["CANDIDATE"],
        "unresolved_actors": actor_counts["UNRESOLVED"],
        "missing_bbox_actors": actor_counts["MISSING_BBOX"],
        "confirmed_cycles": confirmed_cycles,
        "candidate_cycles": candidate_cycles,
        "missing_bbox_instance_candidates": len(missing_rows),
        "qa_images": qa_saved,
        "overall_status": overall,
    }

    # Machine-readable correction proposal; still diagnostic only.
    proposal = {
        "scenario": rel,
        "overall_status": overall,
        "note": "Diagnostic proposal only. Original annotation files were NOT modified.",
        "actors": {},
        "cycles": cycle_rows,
        "missing_bbox_candidates": missing_rows,
    }
    for row in actor_rows:
        proposal["actors"][str(row["actor_id"])] = {
            "status": row["status"],
            "current_bbox_cluster": row["current_bbox_cluster"],
            "expected_bbox_cluster": row["expected_bbox_cluster"],
            "expected_bbox_current_owner": row["expected_bbox_current_owner"],
            "expected_instance_uid": row["expected_bbox_instance_uid"],
            "decision": row["decision"],
            "reason": row["reason"],
            "cycle_id": row["cycle_id"],
        }
    with (scenario_out / "correction_proposal.json").open("w", encoding="utf-8") as f:
        json.dump(proposal, f, ensure_ascii=False, indent=2)

    return summary, actor_rows, cluster_map_rows, cycle_rows, instance_rows, missing_rows


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main():
    args = parse_args()
    root = Path(args.root)
    if not root.exists():
        raise SystemExit(f"Path does not exist: {root}")
    cameras = parse_camera_list(args.cameras)

    out_root = Path(args.output).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    scenarios = find_scenarios(root, args.path_contains)
    if not scenarios:
        raise SystemExit("No scenario with anno/*.json.gz found.")

    print(f"Found {len(scenarios)} scenario(s)")
    print(f"Cameras: {', '.join(cameras)} | frame_step={args.frame_step}")

    all_summary = []
    all_actor = []
    all_cluster = []
    all_cycle = []
    all_instance = []
    all_missing = []

    for idx, scenario in enumerate(scenarios, 1):
        print(f"[{idx}/{len(scenarios)}] {scenario}")
        try:
            summary, actor_rows, cluster_rows, cycle_rows, instance_rows, missing_rows = analyze_scenario(
                scenario, root, out_root, cameras, args
            )
        except Exception as e:
            rel = relative_name(scenario, root)
            print(f"  ERROR: {e}", file=sys.stderr)
            summary = {
                "scenario": rel,
                "anno_frames": "", "sensor_sampled_frames": "", "traffic_light_actors": "",
                "physical_bbox_clusters": "", "stable_bbox_instance_maps": "", "instance_ids_seen": "",
                "pass": "", "confirmed_cycle_actors": "", "candidate_actors": "",
                "unresolved_actors": "", "missing_bbox_actors": "", "confirmed_cycles": "",
                "candidate_cycles": "", "missing_bbox_instance_candidates": "", "qa_images": "",
                "overall_status": f"ERROR: {e}",
            }
            actor_rows, cluster_rows, cycle_rows, instance_rows, missing_rows = [], [], [], [], []

        all_summary.append(summary)
        all_actor.extend(actor_rows)
        all_cluster.extend(cluster_rows)
        all_cycle.extend(cycle_rows)
        all_instance.extend(instance_rows)
        all_missing.extend(missing_rows)

        print(
            f"  [{summary['overall_status']}] "
            f"actors={summary['traffic_light_actors']} "
            f"bbox={summary['physical_bbox_clusters']} "
            f"bbox->instance={summary['stable_bbox_instance_maps']} "
            f"pass={summary['pass']} "
            f"confirmed_cycle_actors={summary['confirmed_cycle_actors']} "
            f"candidate={summary['candidate_actors']} "
            f"unresolved={summary['unresolved_actors']} "
            f"confirmed_cycles={summary['confirmed_cycles']} "
            f"missing_instance={summary['missing_bbox_instance_candidates']}"
        )

    summary_fields = [
        "scenario", "anno_frames", "sensor_sampled_frames", "traffic_light_actors",
        "physical_bbox_clusters", "stable_bbox_instance_maps", "instance_ids_seen", "pass",
        "confirmed_cycle_actors", "candidate_actors", "unresolved_actors", "missing_bbox_actors",
        "confirmed_cycles", "candidate_cycles", "missing_bbox_instance_candidates", "qa_images",
        "overall_status",
    ]
    actor_fields = [
        "scenario", "actor_id", "current_bbox_cluster", "current_bbox_consensus",
        "expected_bbox_cluster", "expected_bbox_instance_uid", "expected_bbox_current_owner",
        "expected_bbox_owner_consensus", "state_frames", "state_matches", "state_accuracy",
        "state_distinct", "state_strong", "state_margin", "range_frames", "range_mae_m",
        "range_shape_rmse_m", "range_strong", "range_margin_m", "trigger_to_instance_xy_m",
        "decision", "reason", "cycle_id", "status",
    ]
    cluster_fields = [
        "scenario", "bbox_cluster", "center_x", "center_y", "center_z", "current_owner",
        "current_owner_consensus", "instance_uid", "bbox_instance_votes", "bbox_instance_total_votes",
        "bbox_instance_consensus", "stable_instance_mapping", "vote_distribution",
    ]
    cycle_fields = ["scenario", "cycle_id", "status", "length", "actors", "direction", "shift", "all_actor_decisions"]
    instance_fields = ["scenario", "instance_uid", "frames_seen", "state_distribution", "world_x", "world_y", "world_z", "claimed_by_bbox"]
    missing_fields = ["scenario", "instance_uid", "frames_visible_with_depth", "world_x", "world_y", "world_z", "nearest_bbox_cluster_distance_m", "median_ego_distance_m", "status"]

    write_csv(out_root / "scenario_summary.csv", all_summary, summary_fields)
    write_csv(out_root / "actor_validation.csv", all_actor, actor_fields)
    write_csv(out_root / "cluster_instance_map.csv", all_cluster, cluster_fields)
    write_csv(out_root / "cycle_summary.csv", all_cycle, cycle_fields)
    write_csv(out_root / "instance_observations.csv", all_instance, instance_fields)
    write_csv(out_root / "missing_bbox_candidates.csv", all_missing, missing_fields)

    print("\nDone")
    print(f"Output: {out_root}")
    print(f"  - {out_root / 'scenario_summary.csv'}")
    print(f"  - {out_root / 'actor_validation.csv'}")
    print(f"  - {out_root / 'cluster_instance_map.csv'}")
    print(f"  - {out_root / 'cycle_summary.csv'}")
    print(f"  - {out_root / 'missing_bbox_candidates.csv'}")


if __name__ == "__main__":
    main()
