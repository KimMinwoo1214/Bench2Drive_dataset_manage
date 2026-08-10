#!/usr/bin/env python3
"""
Visualize GT contained/derivable from Bench2DriveZoo VAD-B2D info PKLs.

Shows in BEV/LiDAR coordinates:
- current agent 3D boxes (BEV footprint)
- persistent actor IDs
- agent future trajectories (derived from gt_ids + npc2world)
- ego future trajectory (derived from world2lidar of future frames)
- VAD-B2D vector-map source elements inside the official ROI
- traffic-control objects stored by build_vad_training_gt.py, if present

This is a debug/QA visualizer. It does not alter the training PKL.
"""

from __future__ import annotations

import argparse
import math
import pickle
from pathlib import Path
from typing import Any

import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


MAP_ELEMENT_CLASS = {
    "Broken": 0,
    "Solid": 1,
    "SolidSolid": 2,
    "Center": 3,
    "TrafficLight": 4,
    "StopSign": 5,
}
DEFAULT_PC_RANGE = np.array([-15.0, -30.0, -2.0, 15.0, 30.0, 2.0], dtype=float)
DEFAULT_POLYLINE_POINTS = 20

MAP_STYLE = {
    "Broken": {"linestyle": "--", "color": "tab:blue"},
    "Solid": {"linestyle": "-", "color": "tab:orange"},
    "SolidSolid": {"linestyle": "-.", "color": "tab:red"},
    "Center": {"linestyle": ":", "color": "tab:green"},
    "TrafficLight": {"linestyle": "-", "color": "tab:pink"},
    "StopSign": {"linestyle": "-", "color": "tab:purple"},
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


def load_pickle(path: Path) -> Any:
    with path.open("rb") as f:
        return pickle.load(f)


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
    town = info["town_name"]
    if town not in map_infos:
        return []

    m = map_infos[town]
    w2l = np.asarray(info["sensors"]["LIDAR_TOP"]["world2lidar"], dtype=float)
    ego_xy_world = np.linalg.inv(w2l)[:2, 3]
    vectors = []

    for pts, samples, typ in zip(m["lane_points"], m["lane_sample_points"], m["lane_types"]):
        if typ not in MAP_ELEMENT_CLASS:
            continue
        if np.min(np.linalg.norm(np.asarray(samples)[:, :2] - ego_xy_world, axis=-1)) >= 50:
            continue
        pts = np.asarray(pts, dtype=float)
        hom = np.concatenate([pts, np.ones((len(pts), 1))], axis=1)
        local = (w2l @ hom.T).T
        mask = (
            (local[:, 0] > pc_range[0]) & (local[:, 0] < pc_range[3])
            & (local[:, 1] > pc_range[1]) & (local[:, 1] < pc_range[4])
        )
        xy = local[mask, :2]
        if len(xy) > 1:
            vectors.append((typ, xy, False))

    for pts, typ in zip(m["trigger_volumes_points"], m["trigger_volumes_types"]):
        if typ not in MAP_ELEMENT_CLASS:
            continue
        pts = np.asarray(pts, dtype=float)
        hom = np.concatenate([pts, np.ones((len(pts), 1))], axis=1)
        local = (w2l @ hom.T).T
        mask = (
            (local[:, 0] > pc_range[0]) & (local[:, 0] < pc_range[3])
            & (local[:, 1] > pc_range[1]) & (local[:, 1] < pc_range[4])
        )
        if mask.all():
            xy = local[:, :2]
            xy = np.concatenate([xy, xy[:1]], axis=0)
            vectors.append((typ, xy, True))
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
    fig, ax = plt.subplots(figsize=(8, 10))

    # Map geometry follows the same world->LiDAR + ROI point masking used by
    # Bench2DriveZoo B2D_VAD_Dataset.get_map_info().
    seen_labels = set()
    for typ, xy, closed in map_vectors(info, map_infos, pc_range):
        style = MAP_STYLE.get(typ, {"linestyle": "-", "color": "black"})
        label = typ if typ not in seen_labels else None
        ax.plot(
            xy[:, 0], xy[:, 1],
            linestyle=style["linestyle"],
            color=style["color"],
            linewidth=1.35,
            label=label,
        )
        if show_training_points:
            sampled = resample_polyline(xy, DEFAULT_POLYLINE_POINTS)
            ax.scatter(
                sampled[:, 0], sampled[:, 1],
                s=9,
                color=style["color"],
                alpha=0.65,
                zorder=2,
            )
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

        if is_highlight:
            ax.plot(
                corners[:, 0],
                corners[:, 1],
                linewidth=2.8,
                color="black",
                zorder=7,
                label="Tracked agent",
            )
        else:
            ax.plot(
                corners[:, 0],
                corners[:, 1],
                linewidth=1.2,
                zorder=4,
            )

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
            ax.arrow(
                cx,
                cy,
                dx,
                dy,
                width=0.035 if not is_highlight else 0.065,
                head_width=0.45 if not is_highlight else 0.65,
                head_length=0.65 if not is_highlight else 0.9,
                length_includes_head=True,
                color="black" if is_highlight else "dimgray",
                alpha=0.9,
                zorder=8,
            )
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
                ax.plot(
                    future[roi_valid, 0],
                    future[roi_valid, 1],
                    marker=".",
                    linewidth=2.2 if is_highlight else 0.9,
                    color="black" if is_highlight else None,
                    zorder=6 if is_highlight else 3,
                )

        if show_ids or show_yaw:
            parts = []
            if show_ids:
                parts.append(str(actor_id_int))
            if show_yaw:
                parts.append(f"{normalize_angle_deg(math.degrees(yaw)):.1f}°")
            label_text = "\n".join(parts)
            ax.text(
                cx,
                cy,
                label_text,
                fontsize=8 if is_highlight else 7,
                fontweight="bold" if is_highlight else "normal",
                color="black",
                clip_on=True,
                zorder=9,
            )

    ego = ego_future(infos, idx, sample_interval, future_frames)
    valid = np.isfinite(ego).all(axis=1)
    ax.scatter([0.0], [0.0], marker="^", s=70, label="Ego")
    if valid.any():
        ax.plot(ego[valid, 0], ego[valid, 1], marker="o", linewidth=2.0, label="Ego future")

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
            ax.scatter(
                [local[0]], [local[1]],
                marker="x", s=65,
                label=kind if first else None,
            )
            first = False

    ax.set_xlim(pc_range[0], pc_range[3])
    ax.set_ylim(pc_range[1], pc_range[4])
    ax.set_aspect("equal", adjustable="box")
    ax.grid(True, alpha=0.25)
    ax.set_xlabel("LiDAR x [m] (lateral)")
    ax.set_ylabel("LiDAR y [m] (forward/backward)")
    ax.text(
        0.01, 0.01,
        "Ego=(0,0), +y ≈ forward | agent arrow = bbox long-axis aligned to motion",
        transform=ax.transAxes,
        fontsize=7,
        alpha=0.7,
        ha="left",
        va="bottom",
    )
    ax.set_title(
        f"{Path(str(info['folder'])).name}\n"
        f"frame {int(info['frame_idx']):05d} | cmd={info.get('command_near')} | "
        f"train agents in ROI={visible_agent_count}"
        + (
            f" | track={highlight_actor_id}"
            if highlight_actor_id is not None
            else ""
        )
    )
    handles, labels = ax.get_legend_handles_labels()
    if handles:
        ax.legend(loc="upper right", fontsize=8)
    output.parent.mkdir(parents=True, exist_ok=True)

    # clip_on=True와 ROI filtering으로 축 밖 annotation이 layout을 밀어내지 않는다.
    fig.tight_layout()
    fig.savefig(output, dpi=150)
    plt.close(fig)



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
    p = argparse.ArgumentParser(description="Visualize VAD-B2D GT info in BEV.")
    p.add_argument("--infos", type=Path, required=True)
    p.add_argument("--map-infos", type=Path, required=True)
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

    infos = load_pickle(args.infos.expanduser().resolve())
    map_infos = load_pickle(args.map_infos.expanduser().resolve())
    if not isinstance(infos, list):
        raise TypeError("infos PKL must contain a list")

    pc_range = np.asarray(args.point_cloud_range, dtype=float)
    output_dir = args.output_dir.expanduser().resolve()

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
    generated = 0
    for n in range(args.count):
        idx = start + n * args.stride
        if idx >= len(infos):
            break
        info = infos[idx]
        name = f"{Path(str(info['folder'])).name}_{int(info['frame_idx']):05d}.png"
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
        print(output)
        generated += 1

    print(f"generated={generated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
