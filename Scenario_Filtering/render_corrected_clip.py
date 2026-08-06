#!/usr/bin/env python3
"""Render selected Bench2Drive camera views and LiDAR from corrected labels."""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Sequence

import cv2
import laspy
import numpy as np

from utils import calculate_cube_vertices, draw_dashed_line, edges, get_image_point


IMAGE_WIDTH = 1600
IMAGE_HEIGHT = 900
LIDAR_RANGE_METRES = 85.0

CAMERA_SPECS = {
    "front": ("CAM_FRONT", "rgb_front", "FRONT"),
    "front_left": ("CAM_FRONT_LEFT", "rgb_front_left", "FRONT LEFT"),
    "front_right": ("CAM_FRONT_RIGHT", "rgb_front_right", "FRONT RIGHT"),
    "back": ("CAM_BACK", "rgb_back", "BACK"),
    "back_left": ("CAM_BACK_LEFT", "rgb_back_left", "BACK LEFT"),
    "back_right": ("CAM_BACK_RIGHT", "rgb_back_right", "BACK RIGHT"),
}
CAMERA_ORDER = tuple(CAMERA_SPECS)

STATE_NAMES = {
    0: "RED",
    1: "YELLOW",
    2: "GREEN",
    3: "OFF",
    4: "UNKNOWN",
}

# OpenCV uses BGR. Annotation boxes intentionally use only two colors so the
# video answers one question at a glance: does this object affect Ego?
NOT_AFFECTING_EGO = (255, 255, 255)
AFFECTING_EGO = (0, 0, 255)


def parse_camera_selection(value: str) -> tuple[str, ...]:
    """Parse all/none or comma-separated camera aliases."""
    normalized = value.strip().lower().replace("-", "_")
    if normalized == "all":
        return CAMERA_ORDER
    if normalized == "none":
        return ()

    selected: list[str] = []
    for raw_name in normalized.split(","):
        name = raw_name.strip()
        for prefix in ("cam_", "rgb_"):
            if name.startswith(prefix):
                name = name[len(prefix) :]
        if name not in CAMERA_SPECS:
            choices = ", ".join(("all", "none", *CAMERA_ORDER))
            raise ValueError(f"알 수 없는 카메라 '{raw_name}'. 사용 가능: {choices}")
        if name not in selected:
            selected.append(name)
    if not selected:
        raise ValueError("--cameras 값이 비어 있습니다. all 또는 none을 사용하세요.")
    return tuple(selected)


def frame_name(path: Path) -> str:
    return path.name.removesuffix(".json.gz")


def frame_sort_key(path: Path) -> tuple[int, int | str]:
    name = frame_name(path)
    return (0, int(name)) if name.isdigit() else (1, name)


def annotation_files(
    anno_dir: Path,
    start_frame: int | None = None,
    max_frames: int | None = None,
) -> list[Path]:
    files = sorted(anno_dir.glob("*.json.gz"), key=frame_sort_key)
    if start_frame is not None:
        files = [
            path
            for path in files
            if frame_name(path).isdigit() and int(frame_name(path)) >= start_frame
        ]
    if max_frames is not None:
        files = files[:max_frames]
    return files


def read_annotation(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as file:
        return json.load(file)


def object_vertices(obj: dict[str, Any]) -> np.ndarray | None:
    world_cord = obj.get("world_cord")
    if isinstance(world_cord, list) and len(world_cord) == 8:
        return np.asarray(world_cord, dtype=np.float64)
    center = obj.get("center")
    extent = obj.get("extent")
    if (
        isinstance(center, list)
        and len(center) >= 3
        and isinstance(extent, list)
        and len(extent) >= 3
    ):
        return np.asarray(calculate_cube_vertices(center, extent), dtype=np.float64)
    return None


def affects_ego_style(obj: dict[str, Any]) -> tuple[tuple[int, int, int], str]:
    """Use corrected affects_ego only; correction provenance stays in changes.csv."""
    if obj.get("affects_ego") is True:
        return AFFECTING_EGO, "AFFECTS"
    return NOT_AFFECTING_EGO, "UNAFFECTED"


def in_front(camera: dict[str, Any], location: Sequence[float]) -> bool:
    yaw = math.radians(float(camera["rotation"][2]))
    forward = np.asarray([math.cos(yaw), math.sin(yaw), 0.0])
    ray = np.asarray(location[:3], dtype=np.float64) - np.asarray(
        camera["location"][:3], dtype=np.float64
    )
    ray_norm = np.linalg.norm(ray)
    if ray_norm == 0:
        return False
    return bool(forward.dot(ray) / ray_norm >= math.cos(math.radians(45.0)))


def draw_object_box(
    image: np.ndarray,
    obj: dict[str, Any],
    intrinsic: np.ndarray,
    world_to_camera: np.ndarray,
    color: tuple[int, int, int],
    label: str,
) -> None:
    vertices = object_vertices(obj)
    if vertices is None:
        return

    projected: list[tuple[np.ndarray, float]] = [
        get_image_point(vertex, intrinsic, world_to_camera) for vertex in vertices
    ]
    for left, right in edges:
        first, first_depth = projected[left]
        second, second_depth = projected[right]
        if first_depth <= 0 or second_depth <= 0:
            continue
        if not np.all(np.isfinite(first)) or not np.all(np.isfinite(second)):
            continue
        if np.max(np.abs(np.concatenate([first, second]))) > 100_000:
            continue
        draw_dashed_line(
            image,
            (int(first[0]), int(first[1])),
            (int(second[0]), int(second[1])),
            color,
            2,
        )

    label_point = next(
        (
            point
            for point, depth in projected
            if depth > 0
            and np.all(np.isfinite(point))
            and 0 <= point[0] < image.shape[1]
            and 0 <= point[1] < image.shape[0]
        ),
        None,
    )
    if label_point is not None:
        cv2.putText(
            image,
            label,
            (int(label_point[0]) + 2, int(label_point[1]) + 2),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            color,
            1,
            cv2.LINE_AA,
        )


def missing_camera_image(camera_label: str, detail: str) -> np.ndarray:
    image = np.zeros((IMAGE_HEIGHT, IMAGE_WIDTH, 3), dtype=np.uint8)
    cv2.putText(
        image,
        camera_label,
        (30, 60),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.2,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    cv2.putText(
        image,
        detail,
        (30, 115),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.9,
        (0, 0, 255),
        2,
        cv2.LINE_AA,
    )
    return image


def draw_affects_ego_legend(image: np.ndarray, frame: str) -> None:
    height, width = image.shape[:2]
    cv2.rectangle(image, (0, height - 38), (width, height), (0, 0, 0), -1)
    entries = [
        (NOT_AFFECTING_EGO, "WHITE=affects_ego false/missing"),
        (AFFECTING_EGO, "RED=affects_ego true"),
    ]
    x = 18
    cv2.putText(
        image,
        f"Frame {frame}",
        (x, height - 12),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.65,
        (0, 255, 0),
        2,
        cv2.LINE_AA,
    )
    x += 180
    for color, label in entries:
        cv2.rectangle(image, (x, height - 27), (x + 20, height - 7), color, 2)
        cv2.putText(
            image,
            label,
            (x + 28, height - 10),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            color,
            1,
            cv2.LINE_AA,
        )
        x += 440


def render_camera_image(
    scenario_dir: Path,
    frame: str,
    corrected_annotation: dict[str, Any],
    camera_name: str,
    output_path: Path,
) -> None:
    sensor_key, image_dir, camera_label = CAMERA_SPECS[camera_name]
    image_path = scenario_dir / "camera" / image_dir / f"{frame}.jpg"
    image = cv2.imread(str(image_path))
    if image is None:
        image = missing_camera_image(camera_label, "IMAGE MISSING")
    camera = corrected_annotation.get("sensors", {}).get(sensor_key)
    if camera is None:
        image = missing_camera_image(camera_label, "CALIBRATION MISSING")
    else:
        intrinsic = np.asarray(camera["intrinsic"], dtype=np.float64)
        world_to_camera = np.asarray(camera["world2cam"], dtype=np.float64)
        boxes = corrected_annotation.get("bounding_boxes", [])
        ego = next(
            (box for box in boxes if box.get("class") == "ego_vehicle"),
            boxes[0] if boxes else None,
        )
        ego_z = float(ego.get("location", [0, 0, 0])[2]) if ego else 0.0
        for obj in boxes:
            if obj.get("class") == "ego_vehicle":
                continue
            object_class = str(obj.get("class", ""))
            location = obj.get("location")
            if not isinstance(location, list) or len(location) < 3:
                continue
            distance = obj.get("distance")
            if isinstance(distance, (int, float)) and distance > 75:
                continue
            if abs(float(location[2]) - ego_z) > 10:
                continue
            if not in_front(camera, location):
                continue

            color, affect_label = affects_ego_style(obj)
            if object_class == "traffic_light":
                state = STATE_NAMES.get(obj.get("state"), str(obj.get("state")))
                label = f"TL {obj.get('id')} {state} {affect_label}"
            elif "vehicle" in object_class:
                label = f"{object_class} {obj.get('id', '')}".strip()
            else:
                label = object_class or "object"
            draw_object_box(
                image,
                obj,
                intrinsic,
                world_to_camera,
                color,
                label,
            )

    cv2.rectangle(image, (0, 0), (330, 48), (0, 0, 0), -1)
    cv2.putText(
        image,
        camera_label,
        (16, 34),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.9,
        (0, 255, 0),
        2,
        cv2.LINE_AA,
    )
    draw_affects_ego_legend(image, frame)
    if not cv2.imwrite(str(output_path), image, [cv2.IMWRITE_JPEG_QUALITY, 90]):
        raise OSError(f"카메라 시각화 저장 실패: {output_path}")


def world_points_to_ego_xy(vertices: np.ndarray, world_to_ego: np.ndarray) -> np.ndarray:
    homogeneous = np.column_stack([vertices[:, :3], np.ones(len(vertices))])
    ego_points = homogeneous @ world_to_ego.T
    return np.column_stack([ego_points[:, 1], -ego_points[:, 0]])


def ego_xy_to_pixels(points: np.ndarray) -> np.ndarray:
    x_pixels = (points[:, 0] + LIDAR_RANGE_METRES) / (
        2 * LIDAR_RANGE_METRES
    ) * (IMAGE_WIDTH - 1)
    y_pixels = (points[:, 1] + LIDAR_RANGE_METRES) / (
        2 * LIDAR_RANGE_METRES
    ) * (IMAGE_HEIGHT - 1)
    return np.column_stack([x_pixels, y_pixels])


def render_lidar_bev(
    scenario_dir: Path,
    frame: str,
    corrected_annotation: dict[str, Any],
    output_path: Path,
) -> None:
    lidar_path = scenario_dir / "lidar" / f"{frame}.laz"
    points = laspy.read(lidar_path).xyz

    pixels = ego_xy_to_pixels(np.column_stack([points[:, 1], -points[:, 0]]))
    pixel_indices = np.rint(pixels).astype(np.int32)
    valid = (
        (pixel_indices[:, 0] >= 0)
        & (pixel_indices[:, 0] < IMAGE_WIDTH)
        & (pixel_indices[:, 1] >= 0)
        & (pixel_indices[:, 1] < IMAGE_HEIGHT)
    )
    pixel_indices = pixel_indices[valid]
    grayscale = np.zeros((IMAGE_HEIGHT, IMAGE_WIDTH), dtype=np.uint8)
    grayscale[pixel_indices[:, 1], pixel_indices[:, 0]] = 255
    grayscale = cv2.dilate(
        grayscale,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
    )
    image = cv2.cvtColor(grayscale, cv2.COLOR_GRAY2BGR)

    boxes = corrected_annotation.get("bounding_boxes", [])
    ego = next(
        (box for box in boxes if box.get("class") == "ego_vehicle"),
        boxes[0] if boxes else None,
    )
    if ego is None or "world2ego" not in ego:
        raise KeyError(f"Ego world2ego가 없습니다: frame={frame}")
    world_to_ego = np.asarray(ego["world2ego"], dtype=np.float64)
    ego_z = float(ego.get("location", [0, 0, 0])[2])

    for obj in boxes:
        object_class = str(obj.get("class", ""))
        if "vehicle" not in object_class:
            continue
        location = obj.get("location", [0, 0, 0])
        if len(location) < 3 or abs(float(location[2]) - ego_z) > 10:
            continue
        vertices = object_vertices(obj)
        if vertices is None:
            continue
        bbox_pixels = ego_xy_to_pixels(world_points_to_ego_xy(vertices, world_to_ego))
        color = (
            (0, 255, 0)
            if object_class == "ego_vehicle"
            else affects_ego_style(obj)[0]
        )
        for left, right in edges:
            first = bbox_pixels[left]
            second = bbox_pixels[right]
            if np.all(np.isfinite(first)) and np.all(np.isfinite(second)):
                cv2.line(
                    image,
                    (int(first[0]), int(first[1])),
                    (int(second[0]), int(second[1])),
                    color,
                    2,
                )

    for light in (
        box for box in boxes if box.get("class") == "traffic_light"
    ):
        trigger = light.get("trigger_volume_location")
        if not isinstance(trigger, list) or len(trigger) < 3:
            continue
        trigger_pixels = ego_xy_to_pixels(
            world_points_to_ego_xy(
                np.asarray([trigger[:3]], dtype=np.float64),
                world_to_ego,
            )
        )[0]
        x, y = int(trigger_pixels[0]), int(trigger_pixels[1])
        if not (0 <= x < IMAGE_WIDTH and 0 <= y < IMAGE_HEIGHT):
            continue
        color, affect_label = affects_ego_style(light)
        radius = 9 if light.get("affects_ego") is True else 6
        cv2.circle(image, (x, y), radius, color, 2)
        cv2.putText(
            image,
            f"TL {light.get('id')} {affect_label}",
            (x + 10, y - 5),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.4,
            color,
            1,
            cv2.LINE_AA,
        )

    draw_affects_ego_legend(image, frame)
    if not cv2.imwrite(str(output_path), image, [cv2.IMWRITE_JPEG_QUALITY, 92]):
        raise OSError(f"LiDAR BEV 저장 실패: {output_path}")


def render_frame(
    job: tuple[
        str,
        str,
        tuple[tuple[str, str], ...],
        str,
    ]
) -> str:
    (
        scenario_dir_text,
        corrected_path_text,
        camera_jobs,
        lidar_dir_text,
    ) = job
    scenario_dir = Path(scenario_dir_text)
    corrected_path = Path(corrected_path_text)
    frame = frame_name(corrected_path)
    corrected_annotation = read_annotation(corrected_path)

    for camera_name, camera_dir_text in camera_jobs:
        render_camera_image(
            scenario_dir,
            frame,
            corrected_annotation,
            camera_name,
            Path(camera_dir_text) / f"{frame}.jpg",
        )
    if lidar_dir_text:
        render_lidar_bev(
            scenario_dir,
            frame,
            corrected_annotation,
            Path(lidar_dir_text) / f"{frame}.jpg",
        )
    return frame


def render_clip(
    scenario_dir: Path,
    anno_dir: Path,
    output_dir: Path,
    *,
    camera_names: Sequence[str] = CAMERA_ORDER,
    render_lidar: bool = True,
    start_frame: int | None = None,
    max_frames: int | None = None,
    workers: int | None = None,
) -> dict[str, Any]:
    scenario_dir = scenario_dir.expanduser().resolve()
    anno_dir = anno_dir.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    selected_cameras = tuple(camera_names)
    if not selected_cameras and not render_lidar:
        raise ValueError("카메라와 LiDAR를 모두 제외하면 렌더링할 항목이 없습니다.")

    camera_dirs: dict[str, Path] = {}
    for camera_name in selected_cameras:
        if camera_name not in CAMERA_SPECS:
            raise ValueError(f"알 수 없는 카메라: {camera_name}")
        image_dir = CAMERA_SPECS[camera_name][1]
        camera_dir = output_dir / "camera" / f"{image_dir}_3d_bbox"
        camera_dir.mkdir(parents=True, exist_ok=True)
        camera_dirs[camera_name] = camera_dir
    lidar_dir = output_dir / "lidar_bev_bbox" if render_lidar else None
    if lidar_dir is not None:
        lidar_dir.mkdir(parents=True, exist_ok=True)

    files = annotation_files(anno_dir, start_frame=start_frame, max_frames=max_frames)
    if not files:
        raise FileNotFoundError(f"렌더링할 corrected annotation이 없습니다: {anno_dir}")
    worker_count = workers or min(4, os.cpu_count() or 1)
    worker_count = max(1, min(worker_count, len(files)))
    camera_jobs = tuple((name, str(path)) for name, path in camera_dirs.items())
    jobs = []
    for path in files:
        jobs.append(
            (
                str(scenario_dir),
                str(path),
                camera_jobs,
                str(lidar_dir) if lidar_dir is not None else "",
            )
        )

    started = time.perf_counter()
    if worker_count == 1 or len(files) < 4:
        completed = [render_frame(job) for job in jobs]
    else:
        previous_cv_threads = cv2.getNumThreads()
        cv2.setNumThreads(1)
        try:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                completed = list(executor.map(render_frame, jobs, chunksize=4))
        finally:
            cv2.setNumThreads(previous_cv_threads)
    elapsed = time.perf_counter() - started
    view_names = ",".join(selected_cameras) or "none"
    print(
        f"시각화 완료: cameras={view_names}, lidar={render_lidar}, "
        f"frames={len(completed)}, workers={worker_count}, "
        f"elapsed={elapsed:.2f}s, fps={len(completed) / elapsed:.2f}"
    )
    return {
        "frames": len(completed),
        "workers": worker_count,
        "elapsed_seconds": elapsed,
        "camera_dirs": camera_dirs,
        "lidar_dir": lidar_dir,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "보정 annotation으로 선택한 카메라 bbox와 LiDAR BEV를 생성"
        )
    )
    parser.add_argument("scenario", type=Path, help="원본 Bench2Drive 클립 폴더")
    parser.add_argument("--anno-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--cameras",
        default="all",
        help=(
            "all, none 또는 쉼표 목록: "
            "front,front_left,front_right,back,back_left,back_right"
        ),
    )
    parser.add_argument(
        "--no-lidar",
        action="store_true",
        help="LiDAR BEV를 생성하지 않음",
    )
    parser.add_argument("--start-frame", type=int)
    parser.add_argument("--max-frames", type=int)
    parser.add_argument("--workers", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        camera_names = parse_camera_selection(args.cameras)
    except ValueError as error:
        parser.error(str(error))
    render_clip(
        args.scenario,
        args.anno_dir,
        args.output_dir,
        camera_names=camera_names,
        render_lidar=not args.no_lidar,
        start_frame=args.start_frame,
        max_frames=args.max_frames,
        workers=args.workers,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
