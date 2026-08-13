#!/usr/bin/env python3
"""Render explicitly selected calibration candidates with the repository renderer."""

from __future__ import annotations

import argparse
import csv
import gzip
import html
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import cv2
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

try:
    from .quality_contract import load_manifest, read_json, write_json_atomic
except ImportError:
    from quality_contract import load_manifest, read_json, write_json_atomic


SCRIPT_DIR = Path(__file__).resolve().parent
RENDERER = SCRIPT_DIR.parent / "Scenario_Filtering" / "visualize.py"


def _read_clip_list(path: Path) -> list[str]:
    names = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        value = raw.strip()
        if value and not value.startswith("#"):
            names.append(value)
    if not names or len(set(names)) != len(names):
        raise ValueError("clip list must be non-empty and contain no duplicates")
    return names


def _read_result(path: Path) -> dict[str, Any]:
    with gzip.open(str(path), "rt", encoding="utf-8") as file:
        return json.load(file)


def _event_frames(events: Sequence[Mapping[str, Any]]) -> set[int]:
    frames = set()
    for event in events:
        start = event.get("start_frame")
        end = event.get("end_frame")
        if isinstance(start, int) and isinstance(end, int):
            frames.update(range(start, end + 1))
    return frames


def _write_events(path: Path, clip: str, events: Sequence[Mapping[str, Any]]) -> None:
    fields = ("scenario", "start_frame", "end_frame", "event_type", "actor_id", "message")
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for event in events:
            if isinstance(event.get("start_frame"), int) and isinstance(event.get("end_frame"), int):
                writer.writerow(
                    {
                        "scenario": clip,
                        "start_frame": event["start_frame"],
                        "end_frame": event["end_frame"],
                        "event_type": event.get("event_type", ""),
                        "actor_id": event.get("actor_id", ""),
                        "message": event.get("message", ""),
                    }
                )


def _load_annotation(path: Path) -> dict[str, Any]:
    with gzip.open(str(path), "rt", encoding="utf-8") as file:
        return json.load(file)


def _project(point: Sequence[float], sensor: Mapping[str, Any]) -> tuple[int, int] | None:
    world = np.asarray([point[0], point[1], point[2], 1.0], dtype=float)
    camera = np.asarray(sensor["world2cam"], dtype=float) @ world
    camera = np.asarray([camera[1], -camera[2], camera[0]], dtype=float)
    if camera[2] <= 0:
        return None
    image = np.asarray(sensor["intrinsic"], dtype=float) @ camera
    image = image[:2] / image[2]
    if not np.all(np.isfinite(image)):
        return None
    return int(round(image[0])), int(round(image[1]))


def _annotate_frame(
    image_path: Path,
    annotation: Mapping[str, Any],
    sensor_name: str,
    events: Sequence[Mapping[str, Any]],
) -> None:
    image = cv2.imread(str(image_path))
    if image is None:
        raise FileNotFoundError(f"rendered image is unreadable: {image_path}")
    overlay = image.copy()
    ego = next(
        (box for box in annotation.get("bounding_boxes", []) if box.get("class") == "ego_vehicle"),
        None,
    )
    z = float((ego or {}).get("center", (0, 0, 0))[2])
    sensor = annotation["sensors"][sensor_name]
    labels = []
    for event in events:
        polygon = event.get("intersection_polygon")
        if isinstance(polygon, list) and len(polygon) >= 3:
            projected = [_project((point[0], point[1], z), sensor) for point in polygon]
            if all(point is not None for point in projected):
                points = np.asarray(projected, dtype=np.int32)
                cv2.fillConvexPoly(overlay, points, (0, 0, 255), cv2.LINE_AA)
                cv2.polylines(image, [points], True, (0, 0, 255), 4, cv2.LINE_AA)
        labels.append(
            "{category}/{raw} actor={actor} pen={pen}m IoU={iou}".format(
                category=event.get("actor_category", ""),
                raw=event.get("actor_class", ""),
                actor=event.get("actor_id", ""),
                pen=event.get("bev_penetration_m", ""),
                iou=event.get("bev_iou", ""),
            )
        )
    image = cv2.addWeighted(overlay, 0.28, image, 0.72, 0)
    for index, label in enumerate(labels[:4]):
        cv2.putText(
            image, label, (12, 28 + index * 24), cv2.FONT_HERSHEY_SIMPLEX,
            0.55, (0, 0, 255), 2, cv2.LINE_AA,
        )
    if not cv2.imwrite(str(image_path), image):
        raise OSError(f"failed to update evidence image: {image_path}")


def _timeline(path: Path, frames: Sequence[Mapping[str, Any]], events: Sequence[Mapping[str, Any]]) -> None:
    x = [row["frame"] for row in frames]
    figure, axes = plt.subplots(3, 1, figsize=(14, 7), sharex=True)
    series = (
        (axes[0], (("overlap_count", "overlapping actors"),), "count"),
        (axes[1], (("max_penetration_m", "max penetration"),), "m"),
        (axes[2], (("max_bev_iou", "max BEV IoU"),), "IoU"),
    )
    for axis, values, ylabel in series:
        for key, label in values:
            axis.plot(x, [row.get(key) for row in frames], label=label, linewidth=1.0)
        for event in events:
            if isinstance(event.get("start_frame"), int):
                axis.axvspan(event["start_frame"], event["end_frame"], color="red", alpha=0.1)
        axis.set_ylabel(ylabel)
        axis.grid(True, alpha=0.3)
        axis.legend(loc="upper right")
    axes[-1].set_xlabel("annotation frame")
    figure.tight_layout()
    figure.savefig(str(path), dpi=140)
    plt.close(figure)


def _video(frame_dir: Path, output: Path, fps: float = 10.0) -> None:
    paths = sorted(frame_dir.glob("*.jpg"), key=lambda path: int(path.stem))
    first = cv2.imread(str(paths[0])) if paths else None
    if first is None:
        raise FileNotFoundError(f"no rendered frames: {frame_dir}")
    output.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(
        str(output), cv2.VideoWriter_fourcc(*"mp4v"), fps,
        (first.shape[1], first.shape[0]),
    )
    if not writer.isOpened():
        raise RuntimeError(f"failed to open video writer: {output}")
    try:
        for path in paths:
            image = cv2.imread(str(path))
            if image is not None:
                writer.write(image)
    finally:
        writer.release()


def _contact_sheet(path: Path, front: Path, bev: Path, timeline: Path) -> None:
    images = [cv2.imread(str(value)) for value in (front, bev, timeline)]
    if any(image is None for image in images):
        raise FileNotFoundError("contact sheet input is unreadable")
    target_height = min(image.shape[0] for image in images)
    resized = []
    for image in images:
        width = int(round(image.shape[1] * target_height / image.shape[0]))
        resized.append(cv2.resize(image, (width, target_height), interpolation=cv2.INTER_AREA))
    sheet = cv2.hconcat(resized)
    if not cv2.imwrite(str(path), sheet):
        raise OSError(f"failed to write contact sheet: {path}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-dir", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--base-root", required=True, type=Path)
    parser.add_argument("--weak-root", required=True, type=Path)
    parser.add_argument("--map-root", required=True, type=Path)
    parser.add_argument("--clip-list", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--context-frames", type=int, default=20)
    args = parser.parse_args(argv)
    if args.context_frames != 20:
        parser.error("v1 evidence contract fixes context at 20 frames (2 seconds at 10 Hz)")
    output = args.output_dir.expanduser().resolve()
    if output.exists():
        parser.error(f"output already exists; refusing overwrite: {output}")
    completion = read_json(args.audit_dir.expanduser().resolve() / "completion.json")
    if completion.get("status") != "completed":
        parser.error("audit is incomplete")
    manifest = load_manifest(args.manifest)
    records = {record.name: record for record in manifest.clips}
    selected = _read_clip_list(args.clip_list)
    unknown = sorted(set(selected) - set(records))
    if unknown:
        parser.error(f"clip list contains unknown clips: {unknown[:5]}")
    output.mkdir(parents=True)
    index_rows = []
    for index, clip in enumerate(selected, start=1):
        record = records[clip]
        root = args.base_root if record.component == "base" else args.weak_root
        clip_dir = root.expanduser().resolve() / clip
        result = _read_result(args.audit_dir.expanduser().resolve() / "clips" / f"{clip}.json.gz")
        events = [event for event in result["events"] if isinstance(event.get("start_frame"), int)]
        if not events:
            parser.error(f"selected clip has no numeric candidate event: {clip}")
        clip_output = output / clip
        clip_output.mkdir()
        event_csv = clip_output / "events.csv"
        _write_events(event_csv, clip, events)
        map_path = args.map_root.expanduser().resolve() / f"{record.town}_HD_map.npz"
        command = [
            sys.executable, str(RENDERER), "--input", str(clip_dir),
            "--output-dir", str(clip_output / "rendered"),
            "--review-events", str(event_csv), "--review-context", "20",
            "--profile", "camera-bev-map", "--map-file", str(map_path),
        ]
        subprocess.run(command, cwd=str(RENDERER.parent), check=True)
        events_by_frame: dict[int, list[Mapping[str, Any]]] = {}
        for event in events:
            for number in range(event["start_frame"], event["end_frame"] + 1):
                events_by_frame.setdefault(number, []).append(event)
        for number, frame_events in events_by_frame.items():
            annotation_path = clip_dir / "anno" / f"{number:05d}.json.gz"
            if not annotation_path.is_file():
                continue
            annotation = _load_annotation(annotation_path)
            for folder, sensor in (("rgb_front_3d_bbox", "CAM_FRONT"), ("rgb_top_down_3d_bbox", "TOP_DOWN")):
                image_path = clip_output / "rendered" / "camera" / folder / f"{number:05d}.jpg"
                if image_path.is_file():
                    _annotate_frame(image_path, annotation, sensor, frame_events)
        timeline = clip_output / "timeline.png"
        _timeline(timeline, result["frames"], events)
        front_dir = clip_output / "rendered" / "camera" / "rgb_front_3d_bbox"
        bev_dir = clip_output / "rendered" / "camera" / "rgb_top_down_3d_bbox"
        _video(front_dir, clip_output / "front.mp4")
        _video(bev_dir, clip_output / "bev.mp4")
        worst = max(events, key=lambda event: float(event.get("bev_penetration_m") or 0.0))
        worst_frame = int(worst["start_frame"])
        _contact_sheet(
            clip_output / "contact_sheet.jpg",
            front_dir / f"{worst_frame:05d}.jpg",
            bev_dir / f"{worst_frame:05d}.jpg",
            timeline,
        )
        index_rows.append(
            {
                "clip": clip,
                "component": record.component,
                "event_count": len(events),
                "max_penetration_m": max(float(event.get("bev_penetration_m") or 0.0) for event in events),
            }
        )
        print(f"rendered evidence {index}/{len(selected)}: {clip}", flush=True)
    body = "".join(
        "<tr><td>{clip}</td><td>{component}</td><td>{event_count}</td><td>{max_penetration_m}</td>"
        "<td><a href='{clip}/contact_sheet.jpg'>sheet</a> <a href='{clip}/front.mp4'>front</a> "
        "<a href='{clip}/bev.mp4'>bev</a> <a href='{clip}/timeline.png'>timeline</a></td></tr>".format(
            **{key: html.escape(str(value)) for key, value in row.items()}
        )
        for row in index_rows
    )
    (output / "index.html").write_text(
        "<!doctype html><meta charset='utf-8'><title>Quality review evidence</title>"
        "<h1>Quality review evidence</h1><p>Evidence only; no automatic approval.</p>"
        "<table border='1'><tr><th>clip</th><th>component</th><th>events</th>"
        f"<th>max penetration</th><th>artifacts</th></tr>{body}</table>",
        encoding="utf-8",
    )
    write_json_atomic(
        output / "completion.json",
        {"schema_version": 1, "status": "completed", "clips": selected, "audit_completion_sha256": completion["completion_sha256"]},
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
