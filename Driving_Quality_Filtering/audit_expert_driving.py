#!/usr/bin/env python3
"""Read-only calibration audit for Bench2Drive expert-driving clips."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import html
import json
import math
import os
import shlex
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

try:
    from .geometry import oriented_box_metrics
    from .quality_contract import (
        ClipRecord,
        COLLISION_CATEGORIES,
        canonical_sha256,
        load_config,
        load_manifest,
        metrics_hash,
        sha256_file,
        write_json_atomic,
    )
except ImportError:
    from geometry import oriented_box_metrics
    from quality_contract import (
        ClipRecord,
        COLLISION_CATEGORIES,
        canonical_sha256,
        load_config,
        load_manifest,
        metrics_hash,
        sha256_file,
        write_json_atomic,
    )


SCRIPT_DIR = Path(__file__).resolve().parent
SCHEMA_VERSION = 1
CAMERA_TO_DEPTH = {
    "rgb_front": "depth_front",
    "rgb_front_left": "depth_front_left",
    "rgb_front_right": "depth_front_right",
    "rgb_back": "depth_back",
    "rgb_back_left": "depth_back_left",
    "rgb_back_right": "depth_back_right",
}
VALID_ACTION_IDS = frozenset(range(39))
CSV_FIELDS = (
    "clip", "component", "split", "scenario", "town", "weather",
    "frame_count", "duration_s", "structural_fatal_count",
    "structural_review_count", "map_available", "source_unchanged",
    "sensor_validation_policy", "sensor_inventory_file_count",
    "sensor_signature_sample_count",
    "positive_overlap_frames", "overlap_event_count",
    "max_overlap_run_frames", "max_bev_intersection_area_m2", "max_bev_iou",
    "max_bev_penetration_m", "annotation_sha256", "clip_metrics_sha256",
    "bbox_class_counts", "bbox_category_counts",
    "positive_overlap_category_counts", "issue_codes",
)
EVENT_FIELDS = (
    "clip", "component", "split", "event_type", "severity", "code",
    "start_frame", "end_frame", "actor_id", "actor_category", "actor_class",
    "actor_base_type", "actor_type_id",
    "bev_intersection_area_m2", "bev_iou", "bev_penetration_m",
    "z_overlap_m", "intersection_polygon", "message",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def frame_number(path: Path) -> int | None:
    name = path.name
    for suffix in (".json.gz", ".json"):
        if name.endswith(suffix):
            stem = name[: -len(suffix)]
            return int(stem) if stem.isdigit() else None
    return None


def annotation_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        (
            path
            for path in directory.iterdir()
            if path.is_file()
            and (path.name.endswith(".json.gz") or path.name.endswith(".json"))
        ),
        key=lambda path: (frame_number(path) is None, frame_number(path) or -1, path.name),
    )


def source_inventory(root: Path) -> set[str]:
    if not root.is_dir():
        raise FileNotFoundError(f"source root does not exist: {root}")
    return {
        child.name
        for child in root.iterdir()
        if child.is_dir() and annotation_files(child / "anno")
    }


def _finite_number(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not a numeric field")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("value is non-finite")
    return number


def _finite_tree(value: Any) -> bool:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return True
    if isinstance(value, (int, float)):
        return math.isfinite(float(value))
    if isinstance(value, list):
        return all(_finite_tree(item) for item in value)
    if isinstance(value, dict):
        return all(_finite_tree(item) for item in value.values())
    return True


def actor_category(actor: Mapping[str, Any]) -> str | None:
    """Normalize raw Bench2Drive actors to the three approved collision groups."""
    actor_class = str(actor.get("class", "")).strip().lower()
    base_type = str(actor.get("base_type", "")).strip().lower()
    type_id = str(actor.get("type_id", "")).strip().lower()
    if actor_class in {"walker", "pedestrian"}:
        return "pedestrian"
    if actor_class in {"bicycle", "bike"}:
        return "bicycle"
    if actor_class != "vehicle":
        return None
    if base_type in {"bicycle", "bike"} or "bicycle" in type_id or "bike" in type_id:
        return "bicycle"
    return "vehicle"


def _read_annotation(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    payload = gzip.decompress(raw) if path.name.endswith(".gz") else raw
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("annotation root is not an object")
    return value, raw


def _signature_ok(path: Path, kind: str) -> bool:
    try:
        with path.open("rb") as file:
            header = file.read(8)
            if kind == "jpeg":
                if not header.startswith(b"\xff\xd8"):
                    return False
                file.seek(-2, os.SEEK_END)
                return file.read(2) == b"\xff\xd9"
            if kind == "png":
                return header == b"\x89PNG\r\n\x1a\n"
            if kind == "laz":
                return header.startswith(b"LASF")
            if kind == "hdf5":
                return header == b"\x89HDF\r\n\x1a\n"
            if kind == "npz":
                return header.startswith(b"PK")
    except (OSError, ValueError):
        return False
    raise ValueError(f"unknown signature kind: {kind}")


def _stat_fingerprint(paths: Iterable[Path], root: Path) -> str:
    rows = []
    for path in sorted(set(paths), key=lambda value: str(value)):
        relative = path.relative_to(root).as_posix()
        try:
            stat = path.stat()
            rows.append((relative, stat.st_size, stat.st_mtime_ns))
        except OSError:
            rows.append((relative, None, None))
    return canonical_sha256(rows)


def _empty_event(record: ClipRecord) -> dict[str, Any]:
    return {
        "clip": record.name,
        "component": record.component,
        "split": record.split,
        "event_type": "",
        "severity": "",
        "code": "",
        "start_frame": "",
        "end_frame": "",
        "actor_id": "",
        "actor_category": "",
        "actor_class": "",
        "actor_base_type": "",
        "actor_type_id": "",
        "bev_intersection_area_m2": "",
        "bev_iou": "",
        "bev_penetration_m": "",
        "z_overlap_m": "",
        "intersection_polygon": "",
        "message": "",
    }


def _issue_event(
    record: ClipRecord,
    severity: str,
    code: str,
    message: str,
    frames: Sequence[int] = (),
) -> dict[str, Any]:
    event = _empty_event(record)
    event.update(
        {
            "event_type": "structural",
            "severity": severity,
            "code": code,
            "start_frame": min(frames) if frames else "",
            "end_frame": max(frames) if frames else "",
            "message": message,
        }
    )
    return event


def _expected_streams(
    clip_dir: Path, frame_numbers: Sequence[int], config: Mapping[str, Any]
) -> list[tuple[Path, list[str], str, str]]:
    stems = [f"{number:05d}" for number in frame_numbers]
    streams: list[tuple[Path, list[str], str, str]] = []
    for folder in config["required_rgb_folders"]:
        streams.append(
            (clip_dir / "camera" / folder, [f"{stem}.jpg" for stem in stems], "jpeg", f"camera/{folder}")
        )
    for folder in config["required_depth_folders"]:
        streams.append(
            (clip_dir / "camera" / folder, [f"{stem}.png" for stem in stems], "png", f"camera/{folder}")
        )
    streams.extend(
        [
            (clip_dir / "lidar", [f"{stem}.laz" for stem in stems], "laz", "lidar"),
            (clip_dir / "radar", [f"{stem}.h5" for stem in stems], "hdf5", "radar"),
            (
                clip_dir / "expert_assessment",
                ["-0001.npz", *[f"{number:05d}.npz" for number in frame_numbers[:-1]]],
                "npz",
                "expert_assessment",
            ),
        ]
    )
    return streams


def _validate_stream(
    directory: Path, expected_names: Sequence[str], kind: str
) -> tuple[list[str], list[str], list[str], int]:
    """Validate every inventory entry/size and three distributed byte signatures."""
    if not directory.is_dir():
        return list(expected_names), [], [], 0
    entries = {}
    invalid_size = []
    try:
        with os.scandir(str(directory)) as iterator:
            for entry in iterator:
                if not entry.is_file(follow_symlinks=True):
                    continue
                entries[entry.name] = entry
                try:
                    if entry.stat(follow_symlinks=True).st_size <= 0:
                        invalid_size.append(entry.name)
                except OSError:
                    invalid_size.append(entry.name)
    except OSError:
        return list(expected_names), [], [], 0
    expected_set = set(expected_names)
    missing = sorted(expected_set - set(entries))
    unexpected = sorted(set(entries) - expected_set)
    invalid = sorted(set(invalid_size))
    ordered_present = [name for name in expected_names if name in entries and name not in invalid]
    sampled = 0
    if ordered_present:
        indices = sorted({0, len(ordered_present) // 2, len(ordered_present) - 1})
        for index in indices:
            sampled += 1
            name = ordered_present[index]
            if not _signature_ok(Path(entries[name].path), kind):
                invalid.append(name)
    return missing, unexpected, sorted(set(invalid)), sampled


def _max_or_none(values: Sequence[float]) -> float | None:
    return max(values) if values else None


def audit_clip(payload: Mapping[str, Any]) -> dict[str, Any]:
    record = ClipRecord(**payload["record"])
    config = payload["config"]
    root = Path(payload[f"{record.component}_root"])
    map_root = Path(payload["map_root"])
    clip_dir = root / record.name
    events: list[dict[str, Any]] = []
    frames: list[dict[str, Any]] = []
    issue_counts: Counter[str] = Counter()
    issue_severity: Counter[str] = Counter()

    def issue(severity: str, code: str, message: str, affected: Sequence[int] = ()) -> None:
        issue_counts[code] += 1
        issue_severity[severity] += 1
        events.append(_issue_event(record, severity, code, message, affected))

    anno_paths = annotation_files(clip_dir / "anno")
    numbers = [frame_number(path) for path in anno_paths]
    numeric_numbers = [number for number in numbers if number is not None]
    expected_numbers = list(range(len(anno_paths)))
    if not clip_dir.is_dir():
        issue("fatal", "CLIP_MISSING", f"clip directory is missing: {clip_dir}")
    if not anno_paths:
        issue("fatal", "ANNOTATION_EMPTY", f"annotation directory is empty: {clip_dir / 'anno'}")
    if len(numeric_numbers) != len(anno_paths) or numeric_numbers != expected_numbers:
        issue(
            "fatal",
            "ANNOTATION_FRAME_SEQUENCE",
            f"annotation frames must be contiguous 00000..; observed={numeric_numbers[:5]}.. count={len(numeric_numbers)}",
            numeric_numbers[:10],
        )

    media_streams = _expected_streams(clip_dir, numeric_numbers, config)
    # The code never opens source files for writing. Annotation stat fingerprints
    # are additionally checked before/after because annotation bytes drive every
    # quality decision; their content is independently SHA256-hashed below.
    fingerprint_paths = list(anno_paths)
    source_before = _stat_fingerprint(fingerprint_paths, clip_dir) if clip_dir.is_dir() else ""

    sensor_inventory_file_count = 0
    sensor_signature_sample_count = 0
    for directory, expected_names, kind, label in media_streams:
        sensor_inventory_file_count += len(expected_names)
        missing, unexpected, invalid, sampled = _validate_stream(
            directory, expected_names, kind
        )
        sensor_signature_sample_count += sampled
        if missing:
            issue(
                "fatal", "MEDIA_MISSING",
                f"{label}: missing={len(missing)}, examples={missing[:5]}",
            )
        if unexpected:
            issue(
                "fatal", "MEDIA_UNEXPECTED",
                f"{label}: unexpected={len(unexpected)}, examples={unexpected[:5]}",
            )
        if invalid:
            issue(
                "fatal", "MEDIA_SIZE_OR_SIGNATURE_INVALID",
                f"{label}: invalid={len(invalid)}, examples={invalid[:5]}",
            )

    annotation_digest = hashlib.sha256()
    class_counts: Counter[str] = Counter()
    category_counts: Counter[str] = Counter()
    overlap_category_counts: Counter[str] = Counter()
    overlap_frames: set[int] = set()
    overlap_runs: dict[str, int] = {}
    overlap_last: dict[str, int] = {}
    max_overlap_run = 0
    overlap_areas: list[float] = []
    overlap_ious: list[float] = []
    overlap_penetrations: list[float] = []

    for path, number in zip(anno_paths, numbers):
        if number is None:
            continue
        frame_row: dict[str, Any] = {
            "clip": record.name,
            "component": record.component,
            "split": record.split,
            "frame": number,
        }
        try:
            annotation, raw = _read_annotation(path)
            name = path.name.encode("utf-8")
            annotation_digest.update(len(name).to_bytes(4, "big"))
            annotation_digest.update(name)
            annotation_digest.update(hashlib.sha256(raw).digest())
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            issue("fatal", "ANNOTATION_UNREADABLE", f"frame={number}: {type(error).__name__}: {error}", [number])
            frames.append(frame_row)
            continue

        if not _finite_tree(annotation):
            issue(
                "fatal", "ANNOTATION_NONFINITE",
                f"frame={number}: annotation contains a non-finite numeric value",
                [number],
            )
            frames.append(frame_row)
            continue

        try:
            x = _finite_number(annotation.get("x"))
            y = _finite_number(annotation.get("y"))
            theta = _finite_number(annotation.get("theta"))
        except (TypeError, ValueError) as error:
            issue("fatal", "EGO_STATE_INVALID", f"frame={number}: {error}", [number])
            frames.append(frame_row)
            continue

        sensors = annotation.get("sensors")
        if not isinstance(sensors, dict):
            issue("fatal", "SENSORS_INVALID", f"frame={number}: sensors is not an object", [number])
        else:
            missing_sensors = sorted(set(config["required_sensor_keys"]) - set(sensors))
            if missing_sensors:
                issue("fatal", "SENSOR_KEY_MISSING", f"frame={number}: {missing_sensors}", [number])
            if not _finite_tree(sensors):
                issue("fatal", "SENSOR_NONFINITE", f"frame={number}: sensor calibration contains non-finite values", [number])

        boxes = annotation.get("bounding_boxes")
        if not isinstance(boxes, list):
            issue("fatal", "BBOX_LIST_INVALID", f"frame={number}: bounding_boxes is not a list", [number])
            boxes = []
        egos = [box for box in boxes if isinstance(box, dict) and box.get("class") == "ego_vehicle"]
        if len(egos) != 1:
            issue("fatal", "EGO_BBOX_COUNT", f"frame={number}: ego bbox count={len(egos)}", [number])
            ego = None
        else:
            ego = egos[0]
            try:
                oriented_box_metrics(ego, ego)
            except (TypeError, ValueError) as error:
                issue("fatal", "EGO_BBOX_INVALID", f"frame={number}: {error}", [number])
                ego = None

        frame_overlap_count = 0
        frame_max_penetration = 0.0
        frame_max_iou = 0.0
        for actor in boxes:
            if not isinstance(actor, dict):
                issue("fatal", "BBOX_ENTRY_INVALID", f"frame={number}: bbox entry is not an object", [number])
                continue
            actor_class = str(actor.get("class", "unknown"))
            class_counts[actor_class] += 1
            if actor is ego or actor_class == "ego_vehicle" or ego is None:
                continue
            category = actor_category(actor)
            if category is None:
                continue
            category_counts[category] += 1
            try:
                geometry = oriented_box_metrics(ego, actor)
            except (TypeError, ValueError) as error:
                issue("fatal", "ACTOR_BBOX_INVALID", f"frame={number}, class={actor_class}: {error}", [number])
                continue
            if not geometry["positive_3d_overlap"]:
                continue
            frame_overlap_count += 1
            overlap_category_counts[category] += 1
            overlap_frames.add(number)
            area = float(geometry["bev_intersection_area_m2"])
            iou = float(geometry["bev_iou"])
            penetration = float(geometry["bev_penetration_m"])
            overlap_areas.append(area)
            overlap_ious.append(iou)
            overlap_penetrations.append(penetration)
            frame_max_penetration = max(frame_max_penetration, penetration)
            frame_max_iou = max(frame_max_iou, iou)
            actor_id = str(actor.get("id", actor.get("actor_id", "unknown")))
            run_key = f"{category}:{actor_id}"
            run = overlap_runs.get(run_key, 0) + 1 if overlap_last.get(run_key) == number - 1 else 1
            overlap_runs[run_key] = run
            overlap_last[run_key] = number
            max_overlap_run = max(max_overlap_run, run)
            event = _empty_event(record)
            event.update(
                {
                    "event_type": "positive_3d_overlap",
                    "severity": "candidate",
                    "code": "BBOX_3D_OVERLAP",
                    "start_frame": number,
                    "end_frame": number,
                    "actor_id": actor_id,
                    "actor_category": category,
                    "actor_class": actor_class,
                    "actor_base_type": str(actor.get("base_type", "")),
                    "actor_type_id": str(actor.get("type_id", "")),
                    "bev_intersection_area_m2": area,
                    "bev_iou": iou,
                    "bev_penetration_m": penetration,
                    "z_overlap_m": float(geometry["z_overlap_m"]),
                    "intersection_polygon": geometry["intersection_polygon"],
                    "message": "coordinate-only candidate; visual review has not confirmed a collision",
                }
            )
            events.append(event)

        expert_stem = "-0001" if number == 0 else f"{number - 1:05d}"
        expert_path = clip_dir / "expert_assessment" / f"{expert_stem}.npz"
        if expert_path.is_file():
            try:
                with np.load(str(expert_path), allow_pickle=False) as loaded:
                    expert = np.asarray(loaded["arr_0"])
                if expert.size < 1 or not np.all(np.isfinite(expert)):
                    raise ValueError("arr_0 is empty or non-finite")
                action_value = float(expert.reshape(-1)[-1])
                action_id = int(action_value)
                if action_value != action_id or action_id not in VALID_ACTION_IDS:
                    raise ValueError(f"invalid discrete action id: {action_value}")
                frame_row["expert_action_id"] = action_id
            except (OSError, KeyError, TypeError, ValueError) as error:
                issue("fatal", "EXPERT_INVALID", f"frame={number}: {type(error).__name__}: {error}", [number])

        frame_row.update(
            {
                "x": x, "y": y, "theta_rad": theta,
                "overlap_count": frame_overlap_count,
                "max_penetration_m": frame_max_penetration,
                "max_bev_iou": frame_max_iou,
            }
        )
        frames.append(frame_row)

    map_path = map_root / f"{record.town}_HD_map.npz"
    map_available = map_path.is_file()
    if not map_available:
        issue("review", "MAP_MISSING", f"map file is unavailable: {map_path}")
    source_after = _stat_fingerprint(fingerprint_paths, clip_dir) if clip_dir.is_dir() else ""
    source_unchanged = source_before == source_after
    if not source_unchanged:
        issue("fatal", "SOURCE_CHANGED_DURING_AUDIT", "source file size or mtime changed during read-only audit")

    metrics: dict[str, Any] = {
        "clip": record.name,
        "component": record.component,
        "split": record.split,
        "scenario": record.scenario,
        "town": record.town,
        "weather": record.weather,
        "frame_count": len(anno_paths),
        "duration_s": len(anno_paths) / float(config["sample_hz"]),
        "structural_fatal_count": issue_severity["fatal"],
        "structural_review_count": issue_severity["review"],
        "map_available": map_available,
        "source_unchanged": source_unchanged,
        "sensor_validation_policy": config["sensor_validation_policy"],
        "sensor_inventory_file_count": sensor_inventory_file_count,
        "sensor_signature_sample_count": sensor_signature_sample_count,
        "positive_overlap_frames": len(overlap_frames),
        "overlap_event_count": sum(event["event_type"] == "positive_3d_overlap" for event in events),
        "max_overlap_run_frames": max_overlap_run,
        "max_bev_intersection_area_m2": _max_or_none(overlap_areas),
        "max_bev_iou": _max_or_none(overlap_ious),
        "max_bev_penetration_m": _max_or_none(overlap_penetrations),
        "annotation_sha256": annotation_digest.hexdigest(),
        "source_stat_sha256_before": source_before,
        "source_stat_sha256_after": source_after,
        "bbox_class_counts": dict(sorted(class_counts.items())),
        "bbox_category_counts": dict(sorted(category_counts.items())),
        "positive_overlap_category_counts": dict(sorted(overlap_category_counts.items())),
        "issue_codes": dict(sorted(issue_counts.items())),
    }
    metrics["clip_metrics_sha256"] = canonical_sha256(metrics)
    return {"metrics": metrics, "events": events, "frames": frames}


def _write_gzip_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with gzip.open(str(temporary), "wt", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, separators=(",", ":"))
    temporary.replace(path)


def _read_gzip_json(path: Path) -> Any:
    with gzip.open(str(path), "rt", encoding="utf-8") as file:
        return json.load(file)


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _write_csv(path: Path, fields: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_value(row.get(field)) for field in fields})
    temporary.replace(path)


def _write_html(path: Path, rows: Sequence[Mapping[str, Any]], completion: Mapping[str, Any]) -> None:
    columns = [
        "clip", "component", "split", "frame_count", "structural_fatal_count",
        "structural_review_count", "positive_overlap_frames", "max_overlap_run_frames",
        "max_bev_penetration_m", "max_bev_iou",
        "positive_overlap_category_counts",
    ]
    header = "".join(f"<th onclick=\"sortTable({index})\">{html.escape(column)}</th>" for index, column in enumerate(columns))
    body = []
    for row in rows:
        cells = "".join(f"<td>{html.escape(str(_csv_value(row.get(column))))}</td>" for column in columns)
        body.append(f"<tr>{cells}</tr>")
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Bench2Drive quality calibration</title>
<style>body{{font-family:sans-serif;margin:20px}}table{{border-collapse:collapse;font-size:12px}}th,td{{border:1px solid #bbb;padding:4px}}th{{background:#eee;cursor:pointer;position:sticky;top:0}}tr:nth-child(even){{background:#f8f8f8}}code{{background:#eee;padding:2px 4px}}</style></head>
<body><h1>Bench2Drive quality calibration</h1>
<p>Mode: <code>{html.escape(str(completion['mode']))}</code>; clips: {completion['clip_count']}; metrics SHA256: <code>{completion['metrics_sha256']}</code></p>
<p>This report ranks evidence only. It does not approve PASS, REVIEW, or EXCLUDE.</p>
<table id="quality"><thead><tr>{header}</tr></thead><tbody>{''.join(body)}</tbody></table>
<script>function sortTable(n){{let t=document.getElementById('quality'),r=[...t.tBodies[0].rows],a=t.dataset.col!=n||t.dataset.dir!='asc';r.sort((x,y)=>{{let X=x.cells[n].innerText,Y=y.cells[n].innerText,nx=Number(X),ny=Number(Y);return (!isNaN(nx)&&!isNaN(ny)?nx-ny:X.localeCompare(Y))*(a?1:-1)}});r.forEach(x=>t.tBodies[0].appendChild(x));t.dataset.col=n;t.dataset.dir=a?'asc':'desc'}}</script>
</body></html>"""
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(document, encoding="utf-8")
    temporary.replace(path)


def _implementation_sha256() -> str:
    digest = hashlib.sha256()
    for path in sorted((Path(__file__), SCRIPT_DIR / "geometry.py", SCRIPT_DIR / "quality_contract.py"), key=str):
        digest.update(path.name.encode("utf-8"))
        digest.update(bytes.fromhex(sha256_file(path)))
    return digest.hexdigest()


def _git_state() -> tuple[str, str]:
    repository = SCRIPT_DIR.parent
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repository), check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--short"], cwd=str(repository), check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ).stdout
    return commit, status


def _run_contract(
    manifest_sha256: str,
    config_sha256: str,
    implementation_sha256: str,
    base_root: Path,
    weak_root: Path,
    map_root: Path,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "manifest_sha256": manifest_sha256,
        "config_sha256": config_sha256,
        "implementation_sha256": implementation_sha256,
        "base_root": str(base_root),
        "weak_root": str(weak_root),
        "map_root": str(map_root),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--base-root", required=True, type=Path)
    parser.add_argument("--weak-root", required=True, type=Path)
    parser.add_argument("--map-root", required=True, type=Path)
    parser.add_argument("--config", type=Path, default=SCRIPT_DIR / "quality_config_calibration.json")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--resume", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.workers < 1:
        raise SystemExit("--workers must be positive")
    manifest = load_manifest(args.manifest)
    config_path = args.config.expanduser().resolve()
    config = load_config(config_path)
    if config["mode"] != "calibration_only":
        raise SystemExit("audit command requires config.mode=calibration_only")
    base_root = args.base_root.expanduser().resolve()
    weak_root = args.weak_root.expanduser().resolve()
    map_root = args.map_root.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    for root in (base_root, weak_root, map_root):
        if not root.is_dir():
            raise SystemExit(f"required input root is missing: {root}")
    for root in (base_root, weak_root, map_root):
        if output_dir == root or output_dir in root.parents or root in output_dir.parents:
            raise SystemExit(f"output must not overlap an input root: {output_dir} vs {root}")

    expected_base = {record.name for record in manifest.clips if record.component == "base"}
    expected_weak = {record.name for record in manifest.clips if record.component == "weak"}
    actual_base = source_inventory(base_root)
    actual_weak = source_inventory(weak_root)
    errors = []
    for label, expected, actual in (
        ("base", expected_base, actual_base), ("weak", expected_weak, actual_weak)
    ):
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        if missing or unexpected:
            errors.append(
                f"{label} inventory mismatch: missing={missing[:5]} ({len(missing)}), "
                f"unexpected={unexpected[:5]} ({len(unexpected)})"
            )
    if actual_base & actual_weak:
        errors.append(f"base/weak root overlap: {sorted(actual_base & actual_weak)[:5]}")
    if errors:
        raise SystemExit("\n".join(errors))

    implementation_sha256 = _implementation_sha256()
    contract = _run_contract(
        manifest.sha256, sha256_file(config_path), implementation_sha256,
        base_root, weak_root, map_root,
    )
    run_manifest_path = output_dir / "run_manifest.json"
    if output_dir.exists():
        if not args.resume:
            raise SystemExit(f"output already exists; refusing overwrite: {output_dir}")
        if not run_manifest_path.is_file():
            raise SystemExit(f"resume output lacks run_manifest.json: {output_dir}")
        existing = json.loads(run_manifest_path.read_text(encoding="utf-8"))
        if existing.get("contract") != contract:
            raise SystemExit("resume contract differs from existing run; refusing stale reuse")
    else:
        output_dir.mkdir(parents=True)
        (output_dir / "clips").mkdir()
        commit, dirty = _git_state()
        write_json_atomic(
            run_manifest_path,
            {
                "schema_version": SCHEMA_VERSION,
                "status": "running",
                "mode": config["mode"],
                "started_at": utc_now(),
                "command": shlex.join([sys.executable, *sys.argv]),
                "git_commit": commit,
                "git_status_short": dirty.splitlines(),
                "manifest": {"path": str(manifest.path), "sha256": manifest.sha256},
                "config": {"path": str(config_path), "sha256": sha256_file(config_path)},
                "contract": contract,
                "expected_clip_count": len(manifest.clips),
            },
        )

    payloads = []
    completed: dict[str, dict[str, Any]] = {}
    for record in manifest.clips:
        result_path = output_dir / "clips" / f"{record.name}.json.gz"
        if args.resume and result_path.is_file():
            cached = _read_gzip_json(result_path)
            if cached.get("metrics", {}).get("clip") != record.name:
                raise SystemExit(f"stale cached clip result: {result_path}")
            completed[record.name] = cached
            continue
        payloads.append(
            {
                "record": record.__dict__,
                "config": config,
                "base_root": str(base_root),
                "weak_root": str(weak_root),
                "map_root": str(map_root),
            }
        )

    started = time.monotonic()
    if args.workers == 1:
        iterator = map(audit_clip, payloads)
        executor = None
    else:
        executor = ProcessPoolExecutor(max_workers=args.workers)
        iterator = executor.map(audit_clip, payloads, chunksize=1)
    try:
        for index, result in enumerate(iterator, start=1):
            clip = str(result["metrics"]["clip"])
            _write_gzip_json_atomic(output_dir / "clips" / f"{clip}.json.gz", result)
            completed[clip] = result
            if index % 10 == 0 or index == len(payloads):
                elapsed = time.monotonic() - started
                print(
                    f"audited {len(completed)}/{len(manifest.clips)} clips "
                    f"(new={index}/{len(payloads)}, elapsed={elapsed:.1f}s)",
                    flush=True,
                )
    finally:
        if executor is not None:
            executor.shutdown()

    missing_results = sorted({record.name for record in manifest.clips} - set(completed))
    if missing_results:
        raise SystemExit(f"audit result inventory is incomplete: {missing_results[:5]}")
    ordered_results = [completed[record.name] for record in manifest.clips]
    metrics_rows = [result["metrics"] for result in ordered_results]
    event_rows = [event for result in ordered_results for event in result["events"]]
    global_metrics_sha = metrics_hash(metrics_rows)
    events_sha = canonical_sha256(event_rows)
    _write_csv(output_dir / "clip_metrics.csv", CSV_FIELDS, metrics_rows)
    _write_csv(output_dir / "events.csv", EVENT_FIELDS, event_rows)
    completion = {
        "schema_version": SCHEMA_VERSION,
        "status": "completed",
        "mode": config["mode"],
        "calibration_only": True,
        "clip_count": len(metrics_rows),
        "component_counts": dict(sorted(Counter(row["component"] for row in metrics_rows).items())),
        "split_counts": dict(sorted(Counter(row["split"] for row in metrics_rows).items())),
        "structural_fatal_clips": sum(int(row["structural_fatal_count"]) > 0 for row in metrics_rows),
        "structural_review_clips": sum(int(row["structural_review_count"]) > 0 for row in metrics_rows),
        "overlap_candidate_clips": sum(int(row["positive_overlap_frames"]) > 0 for row in metrics_rows),
        "source_unchanged_clips": sum(bool(row["source_unchanged"]) for row in metrics_rows),
        "metrics_sha256": global_metrics_sha,
        "events_sha256": events_sha,
        "contract": contract,
        "completed_at": utc_now(),
        "next_action": "review calibration evidence and freeze a production config; classification remains blocked",
    }
    completion["completion_sha256"] = canonical_sha256(completion)
    write_json_atomic(output_dir / "completion.json", completion)
    _write_html(output_dir / "index.html", metrics_rows, completion)
    run_manifest = json.loads(run_manifest_path.read_text(encoding="utf-8"))
    run_manifest.update(
        {
            "status": "completed",
            "completed_at": completion["completed_at"],
            "metrics_sha256": global_metrics_sha,
            "events_sha256": events_sha,
            "completion_sha256": completion["completion_sha256"],
        }
    )
    write_json_atomic(run_manifest_path, run_manifest)
    print(json.dumps(completion, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
