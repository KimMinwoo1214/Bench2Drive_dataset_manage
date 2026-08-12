#!/usr/bin/env python3
"""Assign ``traffic_light.affects_ego`` from actual trigger-volume crossings.

The input annotations must already have their traffic-light bbox permutation
repaired.  This module keeps HD maps and vehicle-response heuristics out of the
decision path: a high-confidence correction requires a continuous ego segment
to intersect the light's trigger rectangle, compatible travel direction, a
reliable repaired bbox, and a stable event interval.  Relevance remains active
until the first sampled Ego pose fully beyond the rectangle's far edge.
Uncertain frames are reported as REVIEW and are copied without changing
``affects_ego``.
"""

from __future__ import annotations

import csv
import gzip
import json
import math
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence


DECISION_FIELDS = [
    "scenario",
    "frame",
    "original_ids",
    "predicted_id",
    "status",
    "confidence",
    "event_id",
    "crossing_frame",
    "heading_error_degrees",
    "temporal_run",
    "bbox_reliable",
    "reason",
]

EVENT_FIELDS = [
    "scenario",
    "event_id",
    "traffic_light_id",
    "start_frame",
    "trigger_entry_frame",
    "trigger_center_frame",
    "trigger_exit_frame",
    "crossing_frame",
    "end_frame",
    "heading_error_degrees",
    "bbox_reliable",
    "competing_event_ids",
    "confidence",
    "reason",
]

CHANGE_FIELDS = [
    "scenario",
    "frame",
    "traffic_light_id",
    "before_affects_ego",
    "after_affects_ego",
    "event_id",
    "reason",
]


@dataclass(frozen=True)
class RelevanceConfig:
    approach_distance_metres: float = 60.0
    maximum_step_metres: float = 5.0
    trigger_margin_metres: float = 0.0
    maximum_heading_error_degrees: float = 35.0
    simultaneous_crossing_frames: int = 3
    minimum_temporal_run_frames: int = 3


@dataclass(frozen=True)
class Light:
    light_id: str
    trigger_x: float
    trigger_y: float
    trigger_extent_x: float
    trigger_extent_y: float
    trigger_yaw_degrees: float
    control_yaw_degrees: float | None
    affects_ego: bool


@dataclass
class Frame:
    name: str
    path: Path
    data: dict[str, Any]
    ego_x: float
    ego_y: float
    lights: dict[str, Light]
    original_ids: tuple[str, ...]


@dataclass
class CrossingEvent:
    event_id: str
    light_id: str
    start_index: int
    end_index: int
    crossing_index: int
    entry_index: int
    center_index: int
    heading_error_degrees: float
    bbox_reliable: bool
    competing_event_ids: tuple[str, ...] = ()
    confidence: str = "high"
    reason_tokens: tuple[str, ...] = ()


@dataclass
class Decision:
    predicted_id: str | None
    original_ids: tuple[str, ...]
    status: str
    confidence: str
    event_id: str
    crossing_index: int | None
    heading_error_degrees: float | None
    temporal_run: int
    bbox_reliable: bool
    reason_tokens: tuple[str, ...]


def _read_annotation(path: Path) -> dict[str, Any]:
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as file:
        return json.load(file)


def _write_annotation(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.name.endswith(".gz"):
        with gzip.GzipFile(path, "wb", mtime=0) as file:
            file.write(
                json.dumps(data, ensure_ascii=False, indent=4).encode("utf-8")
            )
        return
    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=4)


def _frame_name(path: Path) -> str:
    return path.name[: -len(".json.gz")] if path.name.endswith(".json.gz") else path.stem


def _frame_sort_key(path: Path) -> tuple[int, int | str, str]:
    name = _frame_name(path)
    return (0, int(name), path.name) if name.isdigit() else (1, name, path.name)


def annotation_files(directory: Path) -> list[Path]:
    selected: dict[str, Path] = {}
    for path in directory.iterdir():
        if not path.is_file():
            continue
        if path.suffix != ".json" and not path.name.endswith(".json.gz"):
            continue
        name = _frame_name(path)
        previous = selected.get(name)
        if previous is None or path.name.endswith(".json.gz"):
            selected[name] = path
    return sorted(selected.values(), key=_frame_sort_key)


def _xy(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    if not all(isinstance(item, (int, float)) for item in value[:2]):
        return None
    return float(value[0]), float(value[1])


def _number(value: Any, index: int, default: float = 0.0) -> float:
    if not isinstance(value, (list, tuple)) or len(value) <= index:
        return default
    item = value[index]
    return float(item) if isinstance(item, (int, float)) else default


def _control_yaw(box: dict[str, Any], trigger: tuple[float, float]) -> float | None:
    pole = _xy(box.get("location"))
    if pole is None:
        return None
    dx, dy = pole[0] - trigger[0], pole[1] - trigger[1]
    if math.hypot(dx, dy) < 1e-3:
        return None
    return math.degrees(math.atan2(dy, dx))


def _parse_frame(path: Path) -> Frame:
    data = _read_annotation(path)
    boxes = data.get("bounding_boxes", [])
    ego = next(
        (box for box in boxes if box.get("class") in {"ego_vehicle", "ego"}),
        None,
    )
    ego_xy = _xy(ego.get("location")) if ego is not None else None
    if ego_xy is None:
        x, y = data.get("x"), data.get("y")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            raise ValueError(f"Ego 위치를 읽을 수 없습니다: {path}")
        ego_xy = float(x), float(y)

    lights: dict[str, Light] = {}
    original_ids = []
    for box in boxes:
        if box.get("class") != "traffic_light":
            continue
        trigger = _xy(box.get("trigger_volume_location"))
        raw_id = box.get("id")
        if trigger is None or raw_id is None:
            continue
        light_id = str(raw_id)
        affects_ego = box.get("affects_ego") is True
        lights[light_id] = Light(
            light_id=light_id,
            trigger_x=trigger[0],
            trigger_y=trigger[1],
            trigger_extent_x=max(
                0.05, abs(_number(box.get("trigger_volume_extent"), 0, 0.5))
            ),
            trigger_extent_y=max(
                0.05, abs(_number(box.get("trigger_volume_extent"), 1, 0.5))
            ),
            trigger_yaw_degrees=_number(box.get("trigger_volume_rotation"), 2),
            control_yaw_degrees=_control_yaw(box, trigger),
            affects_ego=affects_ego,
        )
        if affects_ego:
            original_ids.append(light_id)
    return Frame(
        name=_frame_name(path),
        path=path,
        data=data,
        ego_x=ego_xy[0],
        ego_y=ego_xy[1],
        lights=lights,
        original_ids=tuple(sorted(set(original_ids))),
    )


def load_frames(directory: Path) -> list[Frame]:
    paths = annotation_files(directory)
    if not paths:
        raise FileNotFoundError(f"annotation 파일이 없습니다: {directory}")
    return [_parse_frame(path) for path in paths]


def _wrap_degrees(value: float) -> float:
    return (value + 180.0) % 360.0 - 180.0


def _directed_angle_error(first: float, second: float) -> float:
    return abs(_wrap_degrees(first - second))


def _local_point(
    point: tuple[float, float], light: Light
) -> tuple[float, float]:
    yaw = math.radians(light.trigger_yaw_degrees)
    cosine, sine = math.cos(yaw), math.sin(yaw)
    dx, dy = point[0] - light.trigger_x, point[1] - light.trigger_y
    return cosine * dx + sine * dy, -sine * dx + cosine * dy


def segment_intersects_trigger(
    start: tuple[float, float],
    end: tuple[float, float],
    light: Light,
    margin_metres: float,
) -> bool:
    """Return whether one continuous ego segment intersects the rotated box."""
    x0, y0 = _local_point(start, light)
    x1, y1 = _local_point(end, light)
    extent_x = light.trigger_extent_x + margin_metres
    extent_y = light.trigger_extent_y + margin_metres
    dx, dy = x1 - x0, y1 - y0
    lower, upper = 0.0, 1.0
    for origin, delta, minimum, maximum in (
        (x0, dx, -extent_x, extent_x),
        (y0, dy, -extent_y, extent_y),
    ):
        if abs(delta) < 1e-12:
            if origin < minimum or origin > maximum:
                return False
            continue
        first = (minimum - origin) / delta
        second = (maximum - origin) / delta
        entry, exit_ = min(first, second), max(first, second)
        lower, upper = max(lower, entry), min(upper, exit_)
        if lower > upper:
            return False
    return True


def _step_distance(left: Frame, right: Frame) -> float:
    return math.hypot(right.ego_x - left.ego_x, right.ego_y - left.ego_y)


def _trajectory_yaw(frames: Sequence[Frame], segment_index: int) -> float:
    start = max(0, segment_index - 1)
    end = min(len(frames) - 1, segment_index + 2)
    dx = frames[end].ego_x - frames[start].ego_x
    dy = frames[end].ego_y - frames[start].ego_y
    if math.hypot(dx, dy) < 0.2:
        dx = frames[segment_index + 1].ego_x - frames[segment_index].ego_x
        dy = frames[segment_index + 1].ego_y - frames[segment_index].ego_y
    return math.degrees(math.atan2(dy, dx))


def _trigger_travel_yaw(light: Light) -> float:
    if light.control_yaw_degrees is not None:
        return light.control_yaw_degrees
    if light.trigger_extent_x <= light.trigger_extent_y:
        return light.trigger_yaw_degrees
    return light.trigger_yaw_degrees + 90.0


def _first_center_crossing(
    frames: Sequence[Frame],
    first_index: int,
    last_index: int,
    light: Light,
    route_yaw_degrees: float,
) -> int:
    """Return the first sampled frame at/past the trigger centre plane."""
    yaw = math.radians(route_yaw_degrees)
    ux, uy = math.cos(yaw), math.sin(yaw)
    for index in range(first_index, last_index + 1):
        projection = (
            (frames[index].ego_x - light.trigger_x) * ux
            + (frames[index].ego_y - light.trigger_y) * uy
        )
        if projection >= 0.0:
            return index
    return last_index


def _group_consecutive(indices: Iterable[int]) -> list[list[int]]:
    groups: list[list[int]] = []
    for index in sorted(set(indices)):
        if not groups or index > groups[-1][-1] + 1:
            groups.append([index])
        else:
            groups[-1].append(index)
    return groups


def _approach_start(
    frames: Sequence[Frame], end_index: int, config: RelevanceConfig
) -> int:
    distance = 0.0
    start = end_index
    while start > 0:
        step = _step_distance(frames[start - 1], frames[start])
        if step > config.maximum_step_metres:
            break
        if distance + step > config.approach_distance_metres:
            break
        distance += step
        start -= 1
    return start


def read_bbox_reliability(
    detail_csv: Path | None,
    clip_key: str,
) -> dict[tuple[str, str], bool]:
    if detail_csv is None or not detail_csv.is_file():
        return {}
    result: dict[tuple[str, str], bool] = {}
    with detail_csv.open(newline="", encoding="utf-8-sig") as file:
        for row in csv.DictReader(file):
            if row.get("clip") != clip_key:
                continue
            frame = str(row.get("frame", ""))
            if frame.endswith(".json"):
                frame = frame[: -len(".json")]
            key = frame, str(row.get("tl_id", ""))
            result[key] = str(row.get("ok_after", "0")) == "1"
    return result


def build_crossing_events(
    frames: Sequence[Frame],
    scenario: str,
    bbox_reliability: dict[tuple[str, str], bool],
    config: RelevanceConfig,
) -> list[CrossingEvent]:
    light_ids = sorted({light_id for frame in frames for light_id in frame.lights})
    events: list[CrossingEvent] = []
    for light_id in light_ids:
        crossing_segments = []
        for index in range(len(frames) - 1):
            if _step_distance(frames[index], frames[index + 1]) > config.maximum_step_metres:
                continue
            light = frames[index].lights.get(light_id) or frames[index + 1].lights.get(light_id)
            if light is None:
                continue
            if segment_intersects_trigger(
                (frames[index].ego_x, frames[index].ego_y),
                (frames[index + 1].ego_x, frames[index + 1].ego_y),
                light,
                config.trigger_margin_metres,
            ):
                crossing_segments.append(index)

        for group in _group_consecutive(crossing_segments):
            segment_index = group[0]
            light = frames[segment_index].lights.get(light_id) or frames[segment_index + 1].lights[light_id]
            route_yaw = _trajectory_yaw(frames, segment_index)
            heading_error = _directed_angle_error(
                route_yaw, _trigger_travel_yaw(light)
            )
            entry_index = segment_index + 1
            # Each group contains every consecutive trajectory segment that
            # touches the rectangle.  The endpoint after the last such segment
            # is the first sampled Ego pose fully beyond the far edge.  Keep the
            # light relevant through the preceding frame instead of clearing it
            # as soon as Ego enters the volume.
            crossing_index = group[-1] + 1
            center_index = _first_center_crossing(
                frames,
                entry_index,
                crossing_index,
                light,
                route_yaw,
            )
            end_index = crossing_index - 1
            start_index = _approach_start(frames, end_index, config)
            reliability_values = [
                bbox_reliability.get((frames[index].name, light_id))
                for index in range(max(0, segment_index - 1), min(len(frames), segment_index + 2))
            ]
            known_values = [value for value in reliability_values if value is not None]
            bbox_reliable = bool(known_values) and all(known_values)
            reasons = [
                "trajectory_segment_crosses_trigger_volume",
                "affects_ego_until_trigger_volume_exit",
            ]
            if heading_error <= config.maximum_heading_error_degrees:
                reasons.append("trajectory_heading_matches_control_direction")
            else:
                reasons.append("trajectory_heading_mismatch")
            reasons.append("bbox_reliable" if bbox_reliable else "bbox_not_verified")
            confidence = (
                "high"
                if bbox_reliable
                and heading_error <= config.maximum_heading_error_degrees
                else "review"
            )
            events.append(
                CrossingEvent(
                    event_id="",
                    light_id=light_id,
                    start_index=start_index,
                    end_index=end_index,
                    crossing_index=crossing_index,
                    entry_index=entry_index,
                    center_index=center_index,
                    heading_error_degrees=heading_error,
                    bbox_reliable=bbox_reliable,
                    confidence=confidence,
                    reason_tokens=tuple(reasons),
                )
            )

    events.sort(key=lambda event: (event.center_index, event.light_id))
    for index, event in enumerate(events):
        previous_crossings = [
            previous.crossing_index
            for previous in events[:index]
            if previous.light_id != event.light_id
            and previous.crossing_index < event.crossing_index
        ]
        if previous_crossings:
            event.start_index = max(event.start_index, max(previous_crossings))
    for index, event in enumerate(events, start=1):
        event.event_id = f"CROSS{index:04d}"
    for event in events:
        competing = tuple(
            other.event_id
            for other in events
            if other is not event
            and other.light_id != event.light_id
            and abs(other.crossing_index - event.crossing_index)
            <= config.simultaneous_crossing_frames
        )
        if competing:
            event.competing_event_ids = competing
            event.confidence = "review"
            event.reason_tokens = tuple(
                dict.fromkeys((*event.reason_tokens, "multiple_near_simultaneous_crossings"))
            )
    return events


def _contiguous_run(values: Sequence[str | None], index: int) -> int:
    value = values[index]
    if value is None:
        return 0
    start, end = index, index
    while start > 0 and values[start - 1] == value:
        start -= 1
    while end + 1 < len(values) and values[end + 1] == value:
        end += 1
    return end - start + 1


def make_decisions(
    frames: Sequence[Frame],
    events: Sequence[CrossingEvent],
    config: RelevanceConfig,
) -> list[Decision]:
    selected: list[CrossingEvent | None] = []
    for frame_index in range(len(frames)):
        active = [
            event
            for event in events
            if event.start_index <= frame_index <= event.end_index
        ]
        active.sort(key=lambda event: (event.crossing_index, event.light_id))
        selected.append(active[0] if active else None)
    predicted_ids = [event.light_id if event is not None else None for event in selected]

    decisions = []
    for index, (frame, event) in enumerate(zip(frames, selected)):
        predicted_id = event.light_id if event is not None else None
        predicted_ids_tuple = (predicted_id,) if predicted_id is not None else ()
        run = _contiguous_run(predicted_ids, index)
        reasons = list(event.reason_tokens) if event is not None else ["no_trigger_crossing_event"]
        bbox_reliable = event.bbox_reliable if event is not None else False
        confidence = event.confidence if event is not None else "none"

        if frame.original_ids == predicted_ids_tuple:
            status = "KEEP"
            reasons.append("annotation_matches_prediction")
        elif predicted_id is None:
            status = "REVIEW" if frame.original_ids else "KEEP"
            confidence = "review" if frame.original_ids else "none"
            reasons.append("existing_label_without_crossing_evidence")
        elif len(frame.original_ids) > 1:
            status = "REVIEW"
            confidence = "review"
            reasons.append("multiple_original_affects_ego")
        elif event is not None and event.competing_event_ids:
            status = "REVIEW"
            confidence = "review"
        elif event is not None and event.confidence == "high" and run >= config.minimum_temporal_run_frames:
            status = "AUTO_FIX"
            confidence = "high"
            reasons.append("temporal_run_sufficient")
        else:
            status = "REVIEW"
            confidence = "review"
            if run < config.minimum_temporal_run_frames:
                reasons.append("temporal_run_too_short")

        decisions.append(
            Decision(
                predicted_id=predicted_id,
                original_ids=frame.original_ids,
                status=status,
                confidence=confidence,
                event_id=event.event_id if event is not None else "",
                crossing_index=event.crossing_index if event is not None else None,
                heading_error_degrees=(
                    event.heading_error_degrees if event is not None else None
                ),
                temporal_run=run,
                bbox_reliable=bbox_reliable,
                reason_tokens=tuple(dict.fromkeys(reasons)),
            )
        )
    return decisions


def _write_csv(path: Path, fieldnames: Sequence[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def apply_decisions(
    scenario: str,
    frames: Sequence[Frame],
    decisions: Sequence[Decision],
    output_directory: Path,
) -> tuple[list[dict[str, Any]], int]:
    changes = []
    changed_frames = 0
    for frame, decision in zip(frames, decisions):
        if decision.status != "AUTO_FIX" or decision.predicted_id is None:
            shutil.copy2(frame.path, output_directory / frame.path.name)
            continue
        writable = [
            box
            for box in frame.data.get("bounding_boxes", [])
            if box.get("class") == "traffic_light" and "affects_ego" in box
        ]
        if not any(str(box.get("id")) == decision.predicted_id for box in writable):
            shutil.copy2(frame.path, output_directory / frame.path.name)
            continue
        changed = False
        for box in writable:
            before = box.get("affects_ego") is True
            after = str(box.get("id")) == decision.predicted_id
            if before == after:
                continue
            box["affects_ego"] = after
            changed = True
            changes.append(
                {
                    "scenario": scenario,
                    "frame": frame.name,
                    "traffic_light_id": str(box.get("id")),
                    "before_affects_ego": str(before).lower(),
                    "after_affects_ego": str(after).lower(),
                    "event_id": decision.event_id,
                    "reason": ";".join(decision.reason_tokens),
                }
            )
        if changed:
            _write_annotation(output_directory / frame.path.name, frame.data)
            changed_frames += 1
        else:
            shutil.copy2(frame.path, output_directory / frame.path.name)
    return changes, changed_frames


def correct_affects_ego(
    scenario: str,
    bbox_annotation_directory: Path,
    output_annotation_directory: Path,
    report_directory: Path,
    bbox_detail_csv: Path | None = None,
    bbox_clip_key: str = ".",
    config: RelevanceConfig | None = None,
) -> dict[str, Any]:
    config = config or RelevanceConfig()
    frames = load_frames(bbox_annotation_directory)
    reliability = read_bbox_reliability(bbox_detail_csv, bbox_clip_key)
    events = build_crossing_events(frames, scenario, reliability, config)
    decisions = make_decisions(frames, events, config)
    output_annotation_directory.mkdir(parents=True, exist_ok=True)
    changes, changed_frames = apply_decisions(
        scenario, frames, decisions, output_annotation_directory
    )

    decision_rows = []
    for frame, decision in zip(frames, decisions):
        decision_rows.append(
            {
                "scenario": scenario,
                "frame": frame.name,
                "original_ids": "|".join(decision.original_ids),
                "predicted_id": decision.predicted_id or "",
                "status": decision.status,
                "confidence": decision.confidence,
                "event_id": decision.event_id,
                "crossing_frame": (
                    frames[decision.crossing_index].name
                    if decision.crossing_index is not None
                    else ""
                ),
                "heading_error_degrees": (
                    f"{decision.heading_error_degrees:.3f}"
                    if decision.heading_error_degrees is not None
                    else ""
                ),
                "temporal_run": decision.temporal_run,
                "bbox_reliable": str(decision.bbox_reliable).lower(),
                "reason": ";".join(decision.reason_tokens),
            }
        )
    event_rows = [
        {
            "scenario": scenario,
            "event_id": event.event_id,
            "traffic_light_id": event.light_id,
            "start_frame": frames[event.start_index].name,
            "trigger_entry_frame": frames[event.entry_index].name,
            "trigger_center_frame": frames[event.center_index].name,
            "trigger_exit_frame": frames[event.crossing_index].name,
            "crossing_frame": frames[event.crossing_index].name,
            "end_frame": frames[event.end_index].name,
            "heading_error_degrees": f"{event.heading_error_degrees:.3f}",
            "bbox_reliable": str(event.bbox_reliable).lower(),
            "competing_event_ids": "|".join(event.competing_event_ids),
            "confidence": event.confidence,
            "reason": ";".join(event.reason_tokens),
        }
        for event in events
    ]
    _write_csv(report_directory / "relevance_frames.csv", DECISION_FIELDS, decision_rows)
    _write_csv(report_directory / "relevance_events.csv", EVENT_FIELDS, event_rows)
    _write_csv(report_directory / "affects_ego_changes.csv", CHANGE_FIELDS, changes)

    counts = {
        status: sum(decision.status == status for decision in decisions)
        for status in ("KEEP", "AUTO_FIX", "REVIEW")
    }
    return {
        "crossing_events": len(events),
        "keep_frames": counts["KEEP"],
        "auto_fix_frames": counts["AUTO_FIX"],
        "review_frames": counts["REVIEW"],
        "affects_ego_changed_frames": changed_frames,
        "affects_ego_changed_entries": len(changes),
    }
