#!/usr/bin/env python3
"""Bench2Drive traffic-light event detector and annotation relabeler.

The script scans the complete ego trajectory of each scenario, detects
crossings of traffic-light trigger centres, and writes frame-level analysis
labels separately.  When corrected annotations are requested, it preserves
the original annotation schema and changes only the existing ``affects_ego``
value of a clearly recovered traffic-light object.

Only the Python standard library is required.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import multiprocessing
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

try:
    import numpy as np
except ImportError:  # HD-map support is optional; trajectory-only scoring still works.
    np = None


STATE_NAMES = {
    0: "Red",
    1: "Yellow",
    2: "Green",
    3: "Off",
    4: "Unknown",
}

FRAME_FIELDS = [
    "scenario",
    "frame",
    "relevant_light_id",
    "state",
    "state_name",
    "label_source",
    "confidence",
    "event_id",
    "original_light_ids",
    "needs_review",
    "reason",
    "source_file",
]

EVENT_FIELDS = [
    "scenario",
    "event_id",
    "light_id",
    "status",
    "start_frame",
    "crossing_frame",
    "end_frame",
    "min_distance_m",
    "trigger_x",
    "trigger_y",
    "original_true_count",
    "labelled_frame_count",
    "confidence",
    "needs_review",
    "reason",
]

TRAFFIC_LIGHT_REPORT_FIELDS = [
    "scenario",
    "frame",
    "traffic_light_id",
    "original_affects",
    "predicted_affects",
    "score",
    "best_score",
    "second_score",
    "margin",
    "status",
    "reason",
]

DECISION_EVENT_FIELDS = [
    "scenario",
    "event_id",
    "start_frame",
    "end_frame",
    "original_tl",
    "predicted_tl",
    "best_score",
    "min_score",
    "max_score",
    "min_margin",
    "frame_count",
    "reason",
    "status",
]


@dataclass(frozen=True)
class Config:
    approach_distance: float = 40.0
    contact_radius: float = 2.0
    max_step: float = 5.0
    merge_gap_frames: int = 10
    crossing_margin: float = 0.5
    ambiguity_margin: float = 0.75
    event_index_tolerance: int = 20
    route_match_distance: float = 2.0
    score_threshold: float = 0.90
    margin_threshold: float = 0.20
    prediction_threshold: float = 0.50
    temporal_window: int = 5
    temporal_min_frames: int = 3
    temporal_support_threshold: float = 0.80
    map_lane_search_radius: float = 4.0
    map_sample_stride: int = 10
    state_response_window: int = 15
    state_response_min_speed: float = 0.30
    state_response_run_frames: int = 3


@dataclass
class LightObservation:
    light_id: str
    trigger_x: float
    trigger_y: float
    state: Any
    affects_ego: bool
    location_x: float = 0.0
    location_y: float = 0.0
    trigger_yaw: float = 0.0
    trigger_extent_x: float = 0.5
    trigger_extent_y: float = 0.5
    road_id: Any = None
    section_id: Any = None
    lane_id: Any = None


@dataclass
class Frame:
    name: str
    path: Path
    x: float
    y: float
    lights: dict[str, LightObservation]
    original_ids: list[str]
    cumulative_distance: float = 0.0
    yaw: float = 0.0
    road_id: Any = None
    section_id: Any = None
    lane_id: Any = None
    speed: float = 0.0


@dataclass
class Event:
    light_id: str
    trigger_x: float
    trigger_y: float
    near_start: int
    near_end: int
    start_index: int
    crossing_index: int | None
    end_index: int
    min_distance: float
    original_true_count: int
    status: str
    confidence: str
    needs_review: bool
    reason: str
    event_id: str = ""
    selected: bool = False
    labelled_frame_count: int = 0


@dataclass
class FrameLabel:
    light_id: str | None = None
    source: str = "none"
    confidence: str = ""
    event_id: str = ""
    needs_review: bool = False
    reason: str = "no_relevant_light"


def open_annotation(path: Path) -> dict[str, Any]:
    if path.name.endswith(".json.gz"):
        with gzip.open(path, "rt", encoding="utf-8") as file:
            return json.load(file)
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def write_annotation(path: Path, data: dict[str, Any]) -> None:
    """Write an annotation using Bench2Drive's original JSON layout.

    Compression changes only the container.  Both ``.json`` and ``.json.gz``
    therefore use the same four-space indentation as the source annotations.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.name.endswith(".json.gz"):
        with gzip.open(path, "wt", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=4)
        return
    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=4)


def frame_name(path: Path) -> str:
    if path.name.endswith(".json.gz"):
        return path.name[: -len(".json.gz")]
    return path.stem


def frame_sort_key(path: Path) -> tuple[int, int | str, str]:
    name = frame_name(path)
    if name.isdigit():
        return (0, int(name), path.name)
    return (1, name, path.name)


def annotation_files(anno_dir: Path) -> list[Path]:
    """Return one file per frame, preferring json.gz over duplicate json."""
    selected: dict[str, Path] = {}
    for path in anno_dir.iterdir():
        if not path.is_file():
            continue
        if path.suffix != ".json" and not path.name.endswith(".json.gz"):
            continue
        name = frame_name(path)
        current = selected.get(name)
        if current is None or path.name.endswith(".json.gz"):
            selected[name] = path
    return sorted(selected.values(), key=frame_sort_key)


def discover_scenarios(input_path: Path) -> list[tuple[str, Path]]:
    input_path = input_path.resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"입력 경로가 없습니다: {input_path}")
    if not input_path.is_dir():
        raise ValueError("전체 궤적이 필요하므로 --input에는 파일이 아니라 폴더를 지정하십시오.")

    if input_path.name == "anno" and annotation_files(input_path):
        return [(input_path.parent.name, input_path)]
    if (input_path / "anno").is_dir() and annotation_files(input_path / "anno"):
        return [(input_path.name, input_path / "anno")]

    found = [path for path in input_path.rglob("anno") if path.is_dir()]
    scenarios: list[tuple[str, Path]] = []
    for anno_dir in sorted(found):
        if not annotation_files(anno_dir):
            continue
        scenario_dir = anno_dir.parent
        relative = scenario_dir.relative_to(input_path)
        scenarios.append((relative.as_posix(), anno_dir))
    if not scenarios:
        raise FileNotFoundError(f"annotation이 들어 있는 anno 폴더를 찾지 못했습니다: {input_path}")
    return scenarios


def as_xy(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    if not all(isinstance(item, (int, float)) for item in value[:2]):
        return None
    return float(value[0]), float(value[1])


def numeric_at(value: Any, index: int, default: float = 0.0) -> float:
    if not isinstance(value, (list, tuple)) or len(value) <= index:
        return default
    item = value[index]
    return float(item) if isinstance(item, (int, float)) else default


def parse_frame(path: Path) -> Frame:
    data = open_annotation(path)
    boxes = data.get("bounding_boxes", [])
    ego_box = next(
        (box for box in boxes if box.get("class") in {"ego_vehicle", "ego"}),
        None,
    )
    ego_xy = as_xy(ego_box.get("location")) if ego_box else None
    if ego_xy is None:
        x, y = data.get("x"), data.get("y")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            raise ValueError(f"Ego 위치를 읽을 수 없습니다: {path}")
        ego_xy = float(x), float(y)

    ego_yaw = numeric_at(ego_box.get("rotation"), 2) if ego_box else 0.0

    lights: dict[str, LightObservation] = {}
    original_ids: list[str] = []
    for box in boxes:
        if box.get("class") != "traffic_light":
            continue
        light_id = str(box.get("id"))
        trigger_xy = as_xy(box.get("trigger_volume_location"))
        if light_id == "None" or trigger_xy is None:
            continue
        affects_ego = box.get("affects_ego") is True
        location_xy = as_xy(box.get("location")) or trigger_xy
        lights[light_id] = LightObservation(
            light_id=light_id,
            trigger_x=trigger_xy[0],
            trigger_y=trigger_xy[1],
            state=box.get("state"),
            affects_ego=affects_ego,
            location_x=location_xy[0],
            location_y=location_xy[1],
            trigger_yaw=numeric_at(box.get("trigger_volume_rotation"), 2),
            trigger_extent_x=max(
                0.05,
                abs(numeric_at(box.get("trigger_volume_extent"), 0, 0.5)),
            ),
            trigger_extent_y=max(
                0.05,
                abs(numeric_at(box.get("trigger_volume_extent"), 1, 0.5)),
            ),
            road_id=box.get("road_id"),
            section_id=box.get("section_id"),
            lane_id=box.get("lane_id"),
        )
        if affects_ego:
            original_ids.append(light_id)

    return Frame(
        name=frame_name(path),
        path=path,
        x=ego_xy[0],
        y=ego_xy[1],
        lights=lights,
        original_ids=sorted(set(original_ids)),
        yaw=ego_yaw,
        road_id=ego_box.get("road_id") if ego_box else None,
        section_id=ego_box.get("section_id") if ego_box else None,
        lane_id=ego_box.get("lane_id") if ego_box else None,
        speed=(
            float(ego_box.get("speed", 0.0))
            if ego_box and isinstance(ego_box.get("speed", 0.0), (int, float))
            else 0.0
        ),
    )


def load_frames(files: Sequence[Path], config: Config) -> list[Frame]:
    frames = [parse_frame(path) for path in files]
    total = 0.0
    for index, frame in enumerate(frames):
        if index:
            step = distance_xy(
                (frames[index - 1].x, frames[index - 1].y),
                (frame.x, frame.y),
            )
            # A large discontinuity is not valid travelled distance and also
            # acts as a barrier when the approach interval is constructed.
            if step <= config.max_step:
                total += step
        frame.cumulative_distance = total
    return frames


def distance_xy(first: tuple[float, float], second: tuple[float, float]) -> float:
    return math.hypot(first[0] - second[0], first[1] - second[1])


def point_segment_distance(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    dx, dy = end[0] - start[0], end[1] - start[1]
    length_sq = dx * dx + dy * dy
    if length_sq == 0.0:
        return distance_xy(point, start)
    t = ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / length_sq
    t = max(0.0, min(1.0, t))
    closest = start[0] + t * dx, start[1] + t * dy
    return distance_xy(point, closest)


def step_is_valid(frames: Sequence[Frame], left: int, right: int, config: Config) -> bool:
    return distance_xy(
        (frames[left].x, frames[left].y),
        (frames[right].x, frames[right].y),
    ) <= config.max_step


def group_indices(indices: Sequence[int], max_gap: int) -> list[tuple[int, int]]:
    if not indices:
        return []
    groups: list[tuple[int, int]] = []
    start = previous = indices[0]
    for index in indices[1:]:
        if index - previous > max_gap:
            groups.append((start, previous))
            start = index
        previous = index
    groups.append((start, previous))
    return groups


def nearest_moving_point(
    frames: Sequence[Frame],
    origin_index: int,
    direction: int,
    trigger: tuple[float, float],
    config: Config,
) -> int | None:
    """Find a connected point sufficiently far from the trigger."""
    index = origin_index
    while 0 <= index + direction < len(frames):
        next_index = index + direction
        if not step_is_valid(frames, min(index, next_index), max(index, next_index), config):
            return None
        index = next_index
        if distance_xy((frames[index].x, frames[index].y), trigger) >= (
            config.contact_radius + config.crossing_margin
        ):
            return index
    return None


def find_start_index(frames: Sequence[Frame], end_index: int, config: Config) -> int:
    start = end_index
    end_s = frames[end_index].cumulative_distance
    while start > 0:
        if not step_is_valid(frames, start - 1, start, config):
            break
        if end_s - frames[start - 1].cumulative_distance > config.approach_distance:
            break
        start -= 1
    return start


def collect_light_catalog(frames: Sequence[Frame]) -> dict[str, tuple[float, float]]:
    catalog: dict[str, tuple[float, float]] = {}
    for frame in frames:
        for light_id, light in frame.lights.items():
            catalog.setdefault(light_id, (light.trigger_x, light.trigger_y))
    return catalog


def build_candidates(frames: Sequence[Frame], config: Config) -> list[Event]:
    events: list[Event] = []
    for light_id, trigger in collect_light_catalog(frames).items():
        distances = [distance_xy((frame.x, frame.y), trigger) for frame in frames]
        near = [
            index
            for index, value in enumerate(distances)
            if value <= config.contact_radius and light_id in frames[index].lights
        ]
        for near_start, near_end in group_indices(near, config.merge_gap_frames):
            # Do not bridge a dropped/teleported part of the trajectory.
            if any(
                not step_is_valid(frames, index, index + 1, config)
                for index in range(near_start, near_end)
            ):
                continue

            before = nearest_moving_point(frames, near_start, -1, trigger, config)
            after = nearest_moving_point(frames, near_end, +1, trigger, config)
            min_distance = min(distances[near_start : near_end + 1])
            original_count = sum(
                light_id in frame.original_ids
                for frame in frames[max(0, near_start - config.event_index_tolerance) :
                                    min(len(frames), near_end + config.event_index_tolerance + 1)]
            )

            if before is None or after is None:
                end_index = near_end
                start_index = find_start_index(frames, end_index, config)
                events.append(
                    Event(
                        light_id=light_id,
                        trigger_x=trigger[0],
                        trigger_y=trigger[1],
                        near_start=near_start,
                        near_end=near_end,
                        start_index=start_index,
                        crossing_index=None,
                        end_index=end_index,
                        min_distance=min_distance,
                        original_true_count=original_count,
                        status="approach_only",
                        confidence="low",
                        needs_review=True,
                        reason="trajectory_does_not_show_both_sides_of_trigger",
                    )
                )
                continue

            before_xy = frames[before].x, frames[before].y
            after_xy = frames[after].x, frames[after].y
            vx, vy = after_xy[0] - before_xy[0], after_xy[1] - before_xy[1]
            norm = math.hypot(vx, vy)
            if norm == 0.0:
                crossed = False
                projections: list[float] = []
            else:
                ux, uy = vx / norm, vy / norm
                projection_before = (
                    (before_xy[0] - trigger[0]) * ux
                    + (before_xy[1] - trigger[1]) * uy
                )
                projection_after = (
                    (after_xy[0] - trigger[0]) * ux
                    + (after_xy[1] - trigger[1]) * uy
                )
                crossed = (
                    projection_before < -config.crossing_margin
                    and projection_after > config.crossing_margin
                    and point_segment_distance(trigger, before_xy, after_xy)
                    <= config.contact_radius
                )
                projections = [
                    (frames[index].x - trigger[0]) * ux
                    + (frames[index].y - trigger[1]) * uy
                    for index in range(before, after + 1)
                ]

            if not crossed:
                end_index = near_end
                start_index = find_start_index(frames, end_index, config)
                events.append(
                    Event(
                        light_id=light_id,
                        trigger_x=trigger[0],
                        trigger_y=trigger[1],
                        near_start=near_start,
                        near_end=near_end,
                        start_index=start_index,
                        crossing_index=None,
                        end_index=end_index,
                        min_distance=min_distance,
                        original_true_count=original_count,
                        status="ambiguous",
                        confidence="low",
                        needs_review=True,
                        reason="trajectory_touches_trigger_but_does_not_cross_cleanly",
                    )
                )
                continue

            crossing_index = after
            for offset, projection in enumerate(projections):
                if projection > 0.0:
                    crossing_index = before + offset
                    break
            end_index = max(near_start, crossing_index - 1)
            start_index = find_start_index(frames, end_index, config)
            events.append(
                Event(
                    light_id=light_id,
                    trigger_x=trigger[0],
                    trigger_y=trigger[1],
                    near_start=near_start,
                    near_end=near_end,
                    start_index=start_index,
                    crossing_index=crossing_index,
                    end_index=end_index,
                    min_distance=min_distance,
                    original_true_count=original_count,
                    status="matched" if original_count else "missing_label",
                    confidence="high",
                    needs_review=False,
                    reason=(
                        "original_label_matches_geometric_crossing"
                        if original_count
                        else "ego_trajectory_crosses_unlabelled_trigger"
                    ),
                )
            )
    return events


def resolve_candidates(events: list[Event], config: Config) -> None:
    """Select at most one light for crossings occurring at the same place/time."""
    passed = [event for event in events if event.crossing_index is not None]
    passed.sort(key=lambda event: (event.crossing_index or -1, event.min_distance))
    groups: list[list[Event]] = []
    for event in passed:
        same_time = bool(groups) and abs(
            (event.crossing_index or 0) - (groups[-1][-1].crossing_index or 0)
        ) <= config.event_index_tolerance
        same_place = bool(groups) and distance_xy(
            (event.trigger_x, event.trigger_y),
            (groups[-1][-1].trigger_x, groups[-1][-1].trigger_y),
        ) <= max(8.0, config.contact_radius * 4.0)
        if not (same_time and same_place):
            groups.append([event])
        else:
            groups[-1].append(event)

    for group in groups:
        original_matches = [event for event in group if event.original_true_count]
        if len(original_matches) == 1:
            winner = original_matches[0]
        else:
            ranked = sorted(group, key=lambda event: event.min_distance)
            winner = ranked[0]
            if len(ranked) > 1 and (
                ranked[1].min_distance - ranked[0].min_distance
                < config.ambiguity_margin
            ):
                for event in group:
                    event.status = "ambiguous"
                    event.confidence = "low"
                    event.needs_review = True
                    event.reason = "multiple_trigger_candidates_at_same_crossing"
                continue
        winner.selected = True
        for event in group:
            if event is winner:
                continue
            event.status = "unrelated"
            event.confidence = "high"
            event.needs_review = False
            event.reason = "another_trigger_is_closer_or_matches_original_label"


def assign_event_ids(events: list[Event]) -> None:
    ordered = sorted(
        events,
        key=lambda event: (
            event.crossing_index if event.crossing_index is not None else event.near_start,
            event.light_id,
        ),
    )
    for number, event in enumerate(ordered, start=1):
        event.event_id = f"TL{number:03d}"


def make_frame_labels(frames: Sequence[Frame], events: list[Event]) -> list[FrameLabel]:
    labels = [FrameLabel() for _ in frames]

    # Original labels always have priority.
    for index, frame in enumerate(frames):
        if len(frame.original_ids) == 1:
            labels[index] = FrameLabel(
                light_id=frame.original_ids[0],
                source="original",
                confidence="high",
                reason="original_affects_ego_true",
            )
        elif len(frame.original_ids) > 1:
            labels[index] = FrameLabel(
                source="original_conflict",
                confidence="low",
                needs_review=True,
                reason="multiple_original_affects_ego_true",
            )

    for event in events:
        if not event.selected or event.status != "missing_label":
            continue
        for index in range(event.start_index, event.end_index + 1):
            label = labels[index]
            if label.source == "none":
                labels[index] = FrameLabel(
                    light_id=event.light_id,
                    source="recovered",
                    confidence=event.confidence,
                    event_id=event.event_id,
                    reason=event.reason,
                )
                event.labelled_frame_count += 1
            elif label.light_id != event.light_id:
                label.needs_review = True
                label.confidence = "low"
                label.reason = "original_and_recovered_labels_conflict"
                event.needs_review = True
                event.confidence = "low"
                event.reason = "original_and_recovered_labels_conflict"

    # Link matching original frames to the detected event for easier inspection.
    for event in events:
        if not event.selected or event.status != "matched":
            continue
        for index in range(event.start_index, event.end_index + 1):
            if labels[index].light_id == event.light_id:
                labels[index].event_id = event.event_id
                event.labelled_frame_count += 1
    return labels


def state_for_label(frame: Frame, light_id: str | None) -> Any:
    if light_id is None or light_id not in frame.lights:
        return None
    return frame.lights[light_id].state


def frame_rows(
    scenario: str,
    frames: Sequence[Frame],
    labels: Sequence[FrameLabel],
) -> Iterator[dict[str, Any]]:
    for frame, label in zip(frames, labels):
        state = state_for_label(frame, label.light_id)
        yield {
            "scenario": scenario,
            "frame": frame.name,
            "relevant_light_id": label.light_id or "",
            "state": "" if state is None else state,
            "state_name": "" if state is None else STATE_NAMES.get(state, str(state)),
            "label_source": label.source,
            "confidence": label.confidence,
            "event_id": label.event_id,
            "original_light_ids": "|".join(frame.original_ids),
            "needs_review": str(label.needs_review).lower(),
            "reason": label.reason,
            "source_file": str(frame.path),
        }


def event_rows(scenario: str, frames: Sequence[Frame], events: Sequence[Event]) -> Iterator[dict[str, Any]]:
    for event in sorted(events, key=lambda item: item.event_id):
        crossing_frame = (
            frames[event.crossing_index].name if event.crossing_index is not None else ""
        )
        yield {
            "scenario": scenario,
            "event_id": event.event_id,
            "light_id": event.light_id,
            "status": event.status,
            "start_frame": frames[event.start_index].name,
            "crossing_frame": crossing_frame,
            "end_frame": frames[event.end_index].name,
            "min_distance_m": f"{event.min_distance:.3f}",
            "trigger_x": f"{event.trigger_x:.6f}",
            "trigger_y": f"{event.trigger_y:.6f}",
            "original_true_count": event.original_true_count,
            "labelled_frame_count": event.labelled_frame_count,
            "confidence": event.confidence,
            "needs_review": str(event.needs_review).lower(),
            "reason": event.reason,
        }


def write_corrected_annotations(
    scenario_output: Path,
    frames: Sequence[Frame],
    labels: Sequence[FrameLabel],
) -> None:
    output_dir = scenario_output / "corrected_anno"
    for frame, label in zip(frames, labels):
        data = open_annotation(frame.path)
        # Keep the original annotation structure exactly as-is.  A corrected
        # JSON differs only when a high-confidence missing label was recovered:
        # the selected traffic-light object's existing affects_ego value changes
        # from false to true.  Analysis metadata stays in the sidecar CSV/JSONL.
        if label.source == "recovered" and label.light_id is not None:
            for box in data.get("bounding_boxes", []):
                if (
                    box.get("class") == "traffic_light"
                    and str(box.get("id")) == label.light_id
                    and "affects_ego" in box
                ):
                    box["affects_ego"] = True
        write_annotation(output_dir / frame.path.name, data)


def safe_relative_parts(value: str) -> list[str]:
    """Return safe relative path parts and reject absolute/parent traversal."""
    path = Path(value)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise ValueError(f"절대 경로나 상위 경로는 사용할 수 없습니다: {value}")
    return [part for part in path.parts if part not in {"", "."}]


def safe_scenario_output(
    output_root: Path,
    scenario: str,
    scenario_subdir: str = "",
) -> Path:
    parts = [part for part in Path(scenario).parts if part not in {"", ".", ".."}]
    return output_root.joinpath(*parts, *safe_relative_parts(scenario_subdir))


def write_per_scenario(
    output_root: Path,
    scenario: str,
    frames: Sequence[Frame],
    labels: Sequence[FrameLabel],
    events: Sequence[Event],
    scenario_subdir: str = "",
) -> None:
    scenario_output = safe_scenario_output(output_root, scenario, scenario_subdir)
    scenario_output.mkdir(parents=True, exist_ok=True)
    with (scenario_output / "traffic_light_frame_labels.csv").open(
        "w", newline="", encoding="utf-8-sig"
    ) as file:
        writer = csv.DictWriter(file, fieldnames=FRAME_FIELDS)
        writer.writeheader()
        writer.writerows(frame_rows(scenario, frames, labels))
    with (scenario_output / "traffic_light_events.csv").open(
        "w", newline="", encoding="utf-8-sig"
    ) as file:
        writer = csv.DictWriter(file, fieldnames=EVENT_FIELDS)
        writer.writeheader()
        writer.writerows(event_rows(scenario, frames, events))


def process_scenario(
    scenario: str,
    anno_dir: Path,
    output_root: Path,
    config: Config,
    write_corrected: bool,
    scenario_subdir: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    files = annotation_files(anno_dir)
    frames = load_frames(files, config)
    events = build_candidates(frames, config)
    resolve_candidates(events, config)
    assign_event_ids(events)
    labels = make_frame_labels(frames, events)

    frame_data = list(frame_rows(scenario, frames, labels))
    event_data = list(event_rows(scenario, frames, events))
    write_per_scenario(
        output_root,
        scenario,
        frames,
        labels,
        events,
        scenario_subdir=scenario_subdir,
    )
    if write_corrected:
        write_corrected_annotations(
            safe_scenario_output(output_root, scenario, scenario_subdir),
            frames,
            labels,
        )
    review_count = sum(row["needs_review"] == "true" for row in event_data)
    return frame_data, event_data, review_count


def write_jsonl(file: Any, rows: Iterable[dict[str, Any]]) -> None:
    for row in rows:
        file.write(json.dumps(row, ensure_ascii=False) + "\n")


@dataclass(frozen=True)
class MapLaneMatch:
    road_id: Any
    lane_id: Any
    distance: float
    yaw: float


class MapLaneIndex:
    """Sampled CARLA HD-map lane points used only for stop-line association."""

    def __init__(
        self,
        xy: Any,
        yaw: Any,
        road_ids: Any,
        lane_ids: Any,
    ) -> None:
        self.xy = xy
        self.yaw = yaw
        self.road_ids = road_ids
        self.lane_ids = lane_ids

    @classmethod
    def from_file(cls, path: Path, sample_stride: int) -> "MapLaneIndex":
        if np is None:
            raise RuntimeError("HD map을 사용하려면 numpy가 필요합니다.")
        context = multiprocessing.get_context(
            "fork" if sys.platform.startswith("linux") else "spawn"
        )
        receive, send = context.Pipe(duplex=False)
        process = context.Process(
            target=_load_map_lane_arrays,
            args=(str(path), sample_stride, send),
        )
        process.start()
        send.close()
        try:
            message = receive.recv()
        except EOFError as error:
            process.join()
            raise ValueError(f"HD map worker가 종료되었습니다: {path}") from error
        finally:
            receive.close()
        process.join()
        if process.exitcode != 0:
            raise ValueError(f"HD map worker exit code={process.exitcode}: {path}")
        status, payload = message
        if status != "ok":
            raise ValueError(f"HD map을 읽지 못했습니다: {path}: {payload}")
        xy, yaws, road_ids, lane_ids = payload
        return cls(xy, yaws, road_ids, lane_ids)

    def nearest(
        self,
        point: tuple[float, float],
        route_yaw: float,
        max_distance: float,
    ) -> MapLaneMatch | None:
        delta = self.xy - np.asarray(point, dtype=np.float64)
        squared = np.einsum("ij,ij->i", delta, delta)
        nearest_index = int(np.argmin(squared))
        nearest_distance = math.sqrt(float(squared[nearest_index]))
        if nearest_distance > max_distance:
            return None

        # At overlapping junction lanes, prefer points nearly as close as the
        # nearest one whose directed lane yaw agrees with actual route motion.
        candidate_indices = np.flatnonzero(
            squared <= (min(max_distance, nearest_distance + 1.0) ** 2)
        )
        best_index = nearest_index
        best_cost = float("inf")
        for raw_index in candidate_indices:
            index = int(raw_index)
            distance = math.sqrt(float(squared[index]))
            heading_error = directed_angle_error(route_yaw, float(self.yaw[index]))
            cost = distance + 0.02 * heading_error
            if cost < best_cost:
                best_cost = cost
                best_index = index
        return MapLaneMatch(
            road_id=int(self.road_ids[best_index]),
            lane_id=int(self.lane_ids[best_index]),
            distance=math.sqrt(float(squared[best_index])),
            yaw=float(self.yaw[best_index]),
        )


def _load_map_lane_arrays(path: str, sample_stride: int, connection: Any) -> None:
    """Unpickle one dense map in an isolated process and return sampled arrays."""
    try:
        archive = np.load(path, allow_pickle=True)
        if "arr" not in archive.files:
            raise ValueError("arr 배열 없음")
        map_info = dict(archive["arr"])
        archive.close()
        xy: list[tuple[float, float]] = []
        yaws: list[float] = []
        road_ids: list[int] = []
        lane_ids: list[int] = []
        stride = max(1, sample_stride)
        for road_id, road in map_info.items():
            if not isinstance(road, dict):
                continue
            for lane_id, segments in road.items():
                if not isinstance(lane_id, int) or not isinstance(segments, list):
                    continue
                for segment in segments:
                    points = segment.get("Points", []) if isinstance(segment, dict) else []
                    if not points:
                        continue
                    selected = list(points[::stride])
                    if selected[-1] is not points[-1]:
                        selected.append(points[-1])
                    for point in selected:
                        if not isinstance(point, (list, tuple)) or len(point) < 2:
                            continue
                        location, rotation = point[0], point[1]
                        location_xy = as_xy(location)
                        if location_xy is None:
                            continue
                        xy.append(location_xy)
                        yaws.append(numeric_at(rotation, 2))
                        road_ids.append(int(road_id))
                        lane_ids.append(int(lane_id))
        if not xy:
            raise ValueError("lane point 없음")
        connection.send(
            (
                "ok",
                (
                    np.asarray(xy, dtype=np.float64),
                    np.asarray(yaws, dtype=np.float64),
                    np.asarray(road_ids, dtype=np.int64),
                    np.asarray(lane_ids, dtype=np.int64),
                ),
            )
        )
    except Exception as error:
        connection.send(("error", f"{type(error).__name__}: {error}"))
    finally:
        connection.close()


@dataclass
class ScoredEvent:
    light_id: str
    start_index: int
    end_index: int
    crossing_index: int | None
    closest_index: int
    min_box_distance: float
    route_score: float
    crossing_score: float
    lane_score: float
    heading_score: float
    junction_score: float
    behavior_score: float
    clean_crossing: bool
    map_lane: MapLaneMatch | None
    reason_tokens: tuple[str, ...]


@dataclass
class CandidateValue:
    light_id: str
    score: float
    geometric_score: float
    temporal_support: float
    event: ScoredEvent


@dataclass
class FrameDecision:
    predicted_id: str | None
    original_ids: tuple[str, ...]
    candidates: dict[str, CandidateValue]
    best_score: float
    second_score: float
    margin: float
    status: str
    temporal_support: float
    temporal_run: int
    temporal_switches: int
    reason_tokens: tuple[str, ...]


@dataclass
class RelevanceAnalysis:
    frames: list[Frame]
    decisions: list[FrameDecision]
    scored_events: list[ScoredEvent]
    behavior_evidence: dict[str, tuple[float, str]] = field(default_factory=dict)


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def normalize_degrees(value: float) -> float:
    return (value + 180.0) % 360.0 - 180.0


def directed_angle_error(first: float, second: float) -> float:
    return abs(normalize_degrees(first - second))


def undirected_angle_error(first: float, second: float) -> float:
    directed = directed_angle_error(first, second)
    return min(directed, abs(180.0 - directed))


def heading_score(error_degrees: float, maximum: float = 60.0) -> float:
    return clamp01(1.0 - error_degrees / maximum)


def lane_key(frame: Frame) -> tuple[Any, Any] | None:
    if frame.road_id is None or frame.lane_id is None:
        return None
    return frame.road_id, frame.lane_id


def trajectory_heading(frames: Sequence[Frame], index: int, config: Config) -> float:
    before = index
    while before > 0:
        if not step_is_valid(frames, before - 1, before, config):
            break
        before -= 1
        if distance_xy(
            (frames[before].x, frames[before].y),
            (frames[index].x, frames[index].y),
        ) >= 1.0:
            break
    after = index
    while after + 1 < len(frames):
        if not step_is_valid(frames, after, after + 1, config):
            break
        after += 1
        if distance_xy(
            (frames[index].x, frames[index].y),
            (frames[after].x, frames[after].y),
        ) >= 1.0:
            break
    dx = frames[after].x - frames[before].x
    dy = frames[after].y - frames[before].y
    if math.hypot(dx, dy) < 0.2:
        return frames[index].yaw
    return math.degrees(math.atan2(dy, dx))


def oriented_box_distance(
    point: tuple[float, float],
    light: LightObservation,
) -> float:
    yaw = math.radians(light.trigger_yaw)
    cos_yaw, sin_yaw = math.cos(yaw), math.sin(yaw)
    dx = point[0] - light.trigger_x
    dy = point[1] - light.trigger_y
    local_x = cos_yaw * dx + sin_yaw * dy
    local_y = -sin_yaw * dx + cos_yaw * dy
    outside_x = max(abs(local_x) - light.trigger_extent_x, 0.0)
    outside_y = max(abs(local_y) - light.trigger_extent_y, 0.0)
    return math.hypot(outside_x, outside_y)


def stopline_travel_axis(light: LightObservation) -> float:
    # CARLA trigger boxes are long across a stop line and narrow in the lane's
    # travel direction.  Direction is undirected here; route motion selects sign.
    if light.trigger_extent_x <= light.trigger_extent_y:
        return light.trigger_yaw
    return light.trigger_yaw + 90.0


def connected_projection_side(
    frames: Sequence[Frame],
    origin: int,
    direction: int,
    trigger: tuple[float, float],
    route_yaw: float,
    required_projection: float,
    config: Config,
) -> int | None:
    ux = math.cos(math.radians(route_yaw))
    uy = math.sin(math.radians(route_yaw))
    index = origin
    while 0 <= index + direction < len(frames):
        next_index = index + direction
        if not step_is_valid(frames, min(index, next_index), max(index, next_index), config):
            return None
        index = next_index
        projection = (
            (frames[index].x - trigger[0]) * ux
            + (frames[index].y - trigger[1]) * uy
        )
        if direction < 0 and projection <= -required_projection:
            return index
        if direction > 0 and projection >= required_projection:
            return index
    return None


def map_lane_score(
    frames: Sequence[Frame],
    closest_index: int,
    match: MapLaneMatch | None,
    route_yaw: float,
) -> tuple[float, list[str]]:
    if match is None:
        return 0.5, ["map_lane_unavailable"]
    candidate_key = (match.road_id, match.lane_id)
    nearby_keys = {
        lane_key(frames[index])
        for index in range(
            max(0, closest_index - 10),
            min(len(frames), closest_index + 11),
        )
    }
    map_heading_error = directed_angle_error(route_yaw, match.yaw)
    if candidate_key in nearby_keys:
        return 1.0, ["route_lane_match", "map_heading_match"]
    if match.distance <= 2.0 and map_heading_error <= 25.0:
        return 0.8, ["route_lane_proximity", "map_heading_match"]
    if match.distance <= 3.0 and map_heading_error <= 45.0:
        return 0.6, ["route_lane_proximity"]
    return 0.2, ["no_route_lane_match"]


def has_sustained_departure(
    frames: Sequence[Frame],
    start_index: int,
    end_index: int,
    config: Config,
) -> bool:
    run = 0
    for index in range(start_index, end_index + 1):
        if abs(frames[index].speed) >= config.state_response_min_speed:
            run += 1
            if run >= config.state_response_run_frames:
                return True
        else:
            run = 0
    return False


def signal_state_response_score(
    frames: Sequence[Frame],
    light_id: str,
    start_index: int,
    end_index: int,
    config: Config,
) -> tuple[float, str]:
    """Use green-to-departure response as supporting, never primary, evidence."""
    positive = negative = 0
    previous_state: Any = None
    for index in range(start_index, end_index + 1):
        light = frames[index].lights.get(light_id)
        if light is None:
            continue
        state = light.state
        green_transition = state == 2 and previous_state is not None and previous_state != 2
        previous_state = state
        if not green_transition:
            continue
        history_start = max(start_index, index - 5)
        was_stopped = all(
            abs(frames[history].speed) < config.state_response_min_speed * 0.5
            for history in range(history_start, index)
        )
        if not was_stopped:
            continue
        response_end = min(end_index, index + config.state_response_window)
        if has_sustained_departure(frames, index, response_end, config):
            positive += 1
        else:
            negative += 1
    if positive + negative == 0:
        return 0.5, "no_stopped_green_transition_evidence"
    score = positive / (positive + negative)
    if score >= 0.75:
        return score, "green_transition_followed_by_ego_departure"
    if score <= 0.25:
        return score, "green_transition_without_ego_departure"
    return score, "mixed_green_transition_response"


def build_scored_events(
    frames: Sequence[Frame],
    config: Config,
    map_index: MapLaneIndex | None,
) -> list[ScoredEvent]:
    catalog: dict[str, LightObservation] = {}
    for frame in frames:
        for light_id, light in frame.lights.items():
            catalog.setdefault(light_id, light)

    result: list[ScoredEvent] = []
    for light_id, light in catalog.items():
        box_distances = [
            oriented_box_distance((frame.x, frame.y), light) for frame in frames
        ]
        near_indices = [
            index
            for index, value in enumerate(box_distances)
            if value <= config.route_match_distance
        ]
        for near_start, near_end in group_indices(near_indices, config.merge_gap_frames):
            if any(
                not step_is_valid(frames, index, index + 1, config)
                for index in range(near_start, near_end)
            ):
                continue
            closest_index = min(
                range(near_start, near_end + 1),
                key=lambda index: box_distances[index],
            )
            min_box_distance = box_distances[closest_index]
            route_yaw = trajectory_heading(frames, closest_index, config)
            axis_error = undirected_angle_error(
                route_yaw,
                stopline_travel_axis(light),
            )
            stop_heading_score = heading_score(axis_error)
            trigger = light.trigger_x, light.trigger_y
            required_projection = max(
                config.crossing_margin,
                min(light.trigger_extent_x, light.trigger_extent_y) + 0.25,
            )
            before = connected_projection_side(
                frames,
                closest_index,
                -1,
                trigger,
                route_yaw,
                required_projection,
                config,
            )
            after = connected_projection_side(
                frames,
                closest_index,
                1,
                trigger,
                route_yaw,
                required_projection,
                config,
            )
            clean_crossing = (
                min_box_distance <= 1e-6
                and before is not None
                and after is not None
                and stop_heading_score >= 0.5
            )
            crossing_index: int | None = None
            if clean_crossing and before is not None and after is not None:
                ux = math.cos(math.radians(route_yaw))
                uy = math.sin(math.radians(route_yaw))
                crossing_index = after
                for index in range(before, after + 1):
                    projection = (
                        (frames[index].x - light.trigger_x) * ux
                        + (frames[index].y - light.trigger_y) * uy
                    )
                    if projection >= 0.0:
                        crossing_index = index
                        break
            end_index = (
                max(near_start, crossing_index - 1)
                if crossing_index is not None
                else closest_index
            )
            start_index = find_start_index(frames, end_index, config)
            behavior_component, behavior_reason = signal_state_response_score(
                frames,
                light_id,
                start_index,
                end_index,
                config,
            )

            map_match = (
                map_index.nearest(trigger, route_yaw, config.map_lane_search_radius)
                if map_index is not None
                else None
            )
            lane_component, lane_reasons = map_lane_score(
                frames,
                closest_index,
                map_match,
                route_yaw,
            )
            route_component = (
                1.0
                if min_box_distance <= 1e-6
                else clamp01(1.0 - min_box_distance / config.route_match_distance)
            )
            crossing_component = 1.0 if clean_crossing else 0.35
            junction_component = 1.0 if clean_crossing else 0.5
            reasons = [
                "route_stopline_match"
                if route_component >= 0.8
                else "weak_route_stopline_match",
                "clean_stopline_crossing"
                if clean_crossing
                else "trajectory_does_not_cross_stopline_cleanly",
            ]
            if stop_heading_score >= 0.75:
                reasons.append("same_lane_and_heading")
            else:
                reasons.append("cross_traffic_direction")
            reasons.extend(lane_reasons)
            reasons.append(behavior_reason)
            reasons.append(
                "same_junction_inferred_from_route"
                if clean_crossing
                else "junction_match_uncertain"
            )
            result.append(
                ScoredEvent(
                    light_id=light_id,
                    start_index=start_index,
                    end_index=end_index,
                    crossing_index=crossing_index,
                    closest_index=closest_index,
                    min_box_distance=min_box_distance,
                    route_score=route_component,
                    crossing_score=crossing_component,
                    lane_score=lane_component,
                    heading_score=stop_heading_score,
                    junction_score=junction_component,
                    behavior_score=behavior_component,
                    clean_crossing=clean_crossing,
                    map_lane=map_match,
                    reason_tokens=tuple(dict.fromkeys(reasons)),
                )
            )
    clean_events = sorted(
        (event for event in result if event.crossing_index is not None),
        key=lambda event: event.crossing_index or -1,
    )
    for event in clean_events:
        assert event.crossing_index is not None
        previous_crossings = [
            other.crossing_index
            for other in clean_events
            if other.light_id != event.light_id
            and other.crossing_index is not None
            and other.crossing_index + config.event_index_tolerance
            < event.crossing_index
        ]
        if previous_crossings:
            # Once an earlier stop line has been crossed, a later light may take
            # over even when both lie inside the metric look-ahead distance.
            event.start_index = max(event.start_index, max(previous_crossings))
    return result


def geometric_candidate_score(
    frame: Frame,
    light: LightObservation,
    event: ScoredEvent,
    config: Config,
) -> float:
    euclidean_distance = distance_xy(
        (frame.x, frame.y),
        (light.trigger_x, light.trigger_y),
    )
    distance_component = clamp01(
        1.0 - euclidean_distance / max(1.0, 2.0 * config.approach_distance)
    )
    return clamp01(
        0.38 * event.route_score
        + 0.15 * event.crossing_score
        + 0.14 * event.lane_score
        + 0.10 * event.heading_score
        + 0.05 * event.junction_score
        + 0.05 * event.behavior_score
        + 0.04  # upcoming on the recorded future route
        + 0.04 * distance_component
    )


def active_geometric_candidates(
    frames: Sequence[Frame],
    events: Sequence[ScoredEvent],
    config: Config,
) -> list[dict[str, tuple[float, ScoredEvent]]]:
    active: list[dict[str, tuple[float, ScoredEvent]]] = [dict() for _ in frames]
    for event in events:
        for index in range(event.start_index, event.end_index + 1):
            light = frames[index].lights.get(event.light_id)
            if light is None:
                continue
            score = geometric_candidate_score(frames[index], light, event, config)
            previous = active[index].get(event.light_id)
            if previous is None or score > previous[0]:
                active[index][event.light_id] = score, event
    return active


def temporal_window_stats(
    raw_winners: Sequence[str | None],
    index: int,
    light_id: str,
    window: int,
    event_start: int = 0,
    event_end: int | None = None,
) -> tuple[float, int, int]:
    if event_end is None:
        event_end = len(raw_winners) - 1
    start = max(0, event_start, index - window)
    end = min(len(raw_winners), event_end + 1, index + window + 1)
    choices = [value for value in raw_winners[start:end] if value is not None]
    support = choices.count(light_id) / len(choices) if choices else 0.0
    compressed: list[str] = []
    for value in choices:
        if not compressed or compressed[-1] != value:
            compressed.append(value)
    switches = max(0, len(compressed) - 1)
    run = 1 if raw_winners[index] == light_id else 0
    if run:
        cursor = index - 1
        while cursor >= event_start and raw_winners[cursor] == light_id:
            run += 1
            cursor -= 1
        cursor = index + 1
        while cursor <= event_end and raw_winners[cursor] == light_id:
            run += 1
            cursor += 1
    return support, run, switches


def make_relevance_decisions(
    frames: Sequence[Frame],
    events: Sequence[ScoredEvent],
    config: Config,
) -> list[FrameDecision]:
    geometric = active_geometric_candidates(frames, events, config)
    raw_winners: list[str | None] = []
    for candidates in geometric:
        ranked = sorted(candidates.items(), key=lambda item: (-item[1][0], item[0]))
        raw_winners.append(
            ranked[0][0]
            if ranked and ranked[0][1][0] >= config.prediction_threshold
            else None
        )

    decisions: list[FrameDecision] = []
    for index, (frame, frame_candidates) in enumerate(zip(frames, geometric)):
        values: dict[str, CandidateValue] = {}
        for light_id, (base_score, event) in frame_candidates.items():
            support, _, _ = temporal_window_stats(
                raw_winners,
                index,
                light_id,
                config.temporal_window,
                event.start_index,
                event.end_index,
            )
            values[light_id] = CandidateValue(
                light_id=light_id,
                score=clamp01(base_score + 0.05 * support),
                geometric_score=base_score,
                temporal_support=support,
                event=event,
            )
        ranked_values = sorted(values.values(), key=lambda value: (-value.score, value.light_id))
        best = ranked_values[0] if ranked_values else None
        second_score = ranked_values[1].score if len(ranked_values) > 1 else 0.0
        predicted_id = (
            best.light_id
            if best is not None and best.score >= config.prediction_threshold
            else None
        )
        best_score = best.score if best is not None else 0.0
        margin = best_score - second_score
        if predicted_id is not None:
            predicted_event = values[predicted_id].event
            temporal_support, temporal_run, temporal_switches = temporal_window_stats(
                raw_winners,
                index,
                predicted_id,
                config.temporal_window,
                predicted_event.start_index,
                predicted_event.end_index,
            )
        else:
            temporal_support, temporal_run, temporal_switches = 0.0, 0, 0
        original_ids = tuple(frame.original_ids)
        predicted_ids = (predicted_id,) if predicted_id is not None else ()
        reasons: list[str] = []
        if original_ids == predicted_ids:
            status = "KEEP"
            reasons.append("annotation_matches_prediction")
        else:
            reasons.append("annotation_mismatch")
            if predicted_id is None:
                reasons.extend(("no_route_match", "no_predicted_light"))
            elif best is not None:
                reasons.extend(best.event.reason_tokens)
            if best_score < config.score_threshold:
                reasons.append("score_below_threshold")
            if margin < config.margin_threshold:
                reasons.append("ambiguous_candidates")
            temporal_ok = (
                predicted_id is not None
                and temporal_support >= config.temporal_support_threshold
                and temporal_run >= config.temporal_min_frames
                and temporal_switches == 0
            )
            if temporal_switches:
                reasons.append("temporal_id_switch")
            elif temporal_ok:
                reasons.append("temporal_consistent")
            else:
                reasons.append("temporal_consistency_insufficient")
            clean_crossing = best is not None and best.event.clean_crossing
            if (
                predicted_id is not None
                and best_score >= config.score_threshold
                and margin >= config.margin_threshold
                and temporal_ok
                and clean_crossing
            ):
                status = "AUTO_FIX"
            else:
                status = "REVIEW"
        decisions.append(
            FrameDecision(
                predicted_id=predicted_id,
                original_ids=original_ids,
                candidates=values,
                best_score=best_score,
                second_score=second_score,
                margin=margin,
                status=status,
                temporal_support=temporal_support,
                temporal_run=temporal_run,
                temporal_switches=temporal_switches,
                reason_tokens=tuple(dict.fromkeys(reasons)),
            )
        )
    return decisions


def analyze_relevance(
    files: Sequence[Path],
    config: Config,
    map_index: MapLaneIndex | None = None,
) -> RelevanceAnalysis:
    frames = load_frames(files, config)
    scored_events = build_scored_events(frames, config, map_index)
    decisions = make_relevance_decisions(frames, scored_events, config)
    light_ids = sorted(
        {light_id for frame in frames for light_id in frame.lights}
    )
    behavior_evidence = {
        light_id: signal_state_response_score(
            frames,
            light_id,
            0,
            len(frames) - 1,
            config,
        )
        for light_id in light_ids
    }
    return RelevanceAnalysis(frames, decisions, scored_events, behavior_evidence)


def bool_text(value: bool) -> str:
    return "true" if value else "false"


def traffic_light_report_rows(
    scenario: str,
    analysis: RelevanceAnalysis,
) -> Iterator[dict[str, Any]]:
    for frame, decision in zip(analysis.frames, analysis.decisions):
        light_ids = sorted(frame.lights)
        if not light_ids:
            light_ids = [""]
        for light_id in light_ids:
            candidate = decision.candidates.get(light_id)
            candidate_reasons = (
                candidate.event.reason_tokens if candidate is not None else ("no_route_match",)
            )
            behavior_reason = analysis.behavior_evidence.get(
                light_id,
                (0.5, "no_stopped_green_transition_evidence"),
            )[1]
            reasons = tuple(
                dict.fromkeys(
                    (*decision.reason_tokens, *candidate_reasons, behavior_reason)
                )
            )
            yield {
                "scenario": scenario,
                "frame": frame.name,
                "traffic_light_id": light_id,
                "original_affects": bool_text(light_id in decision.original_ids),
                "predicted_affects": bool_text(light_id == decision.predicted_id),
                "score": f"{candidate.score:.6f}" if candidate is not None else "0.000000",
                "best_score": f"{decision.best_score:.6f}",
                "second_score": f"{decision.second_score:.6f}",
                "margin": f"{decision.margin:.6f}",
                "status": decision.status,
                "reason": ";".join(reasons),
            }


def decisions_are_same_event(left: FrameDecision, right: FrameDecision) -> bool:
    if left.status != right.status or left.status == "KEEP":
        return False
    if (
        "temporal_id_switch" in left.reason_tokens
        or "temporal_id_switch" in right.reason_tokens
    ):
        return bool(set(left.candidates) & set(right.candidates))
    return (
        left.original_ids == right.original_ids
        and left.predicted_id == right.predicted_id
    )


def grouped_decision_events(
    scenario: str,
    frames: Sequence[Frame],
    decisions: Sequence[FrameDecision],
    status: str,
) -> list[dict[str, Any]]:
    groups: list[tuple[int, int]] = []
    start: int | None = None
    previous: int | None = None
    for index, decision in enumerate(decisions):
        if decision.status != status:
            if start is not None and previous is not None:
                groups.append((start, previous))
            start = previous = None
            continue
        if (
            start is None
            or previous is None
            or not decisions_are_same_event(decisions[previous], decision)
        ):
            if start is not None and previous is not None:
                groups.append((start, previous))
            start = index
        previous = index
    if start is not None and previous is not None:
        groups.append((start, previous))

    rows: list[dict[str, Any]] = []
    prefix = "REVIEW" if status == "REVIEW" else "AUTOFIX"
    for number, (start_index, end_index) in enumerate(groups, start=1):
        group = decisions[start_index : end_index + 1]
        scores = [decision.best_score for decision in group]
        margins = [decision.margin for decision in group]
        originals = sorted(
            {light_id for decision in group for light_id in decision.original_ids}
        )
        predictions = sorted(
            {
                decision.predicted_id
                for decision in group
                if decision.predicted_id is not None
            }
        )
        reasons = tuple(
            dict.fromkeys(
                token for decision in group for token in decision.reason_tokens
            )
        )
        rows.append(
            {
                "scenario": scenario,
                "event_id": f"{prefix}{number:04d}",
                "start_frame": frames[start_index].name,
                "end_frame": frames[end_index].name,
                "original_tl": "|".join(originals),
                "predicted_tl": "|".join(predictions),
                "best_score": f"{sum(scores) / len(scores):.6f}",
                "min_score": f"{min(scores):.6f}",
                "max_score": f"{max(scores):.6f}",
                "min_margin": f"{min(margins):.6f}",
                "frame_count": len(group),
                "reason": ";".join(reasons),
                "status": status,
            }
        )
    return rows


def write_relabelled_annotations(
    output_root: Path,
    scenario: str,
    analysis: RelevanceAnalysis,
) -> int:
    output_dir = safe_scenario_output(output_root, scenario) / "anno"
    output_dir.mkdir(parents=True, exist_ok=True)
    changed_frames = 0
    for frame, decision in zip(analysis.frames, analysis.decisions):
        destination = output_dir / frame.path.name
        if destination.resolve() == frame.path.resolve():
            raise ValueError(f"원본 annotation을 출력으로 지정할 수 없습니다: {destination}")
        if decision.status != "AUTO_FIX":
            shutil.copy2(frame.path, destination)
            continue
        data = open_annotation(frame.path)
        changed = False
        for box in data.get("bounding_boxes", []):
            if box.get("class") != "traffic_light" or "affects_ego" not in box:
                continue
            predicted = str(box.get("id")) == decision.predicted_id
            if box.get("affects_ego") is not predicted:
                box["affects_ego"] = predicted
                changed = True
        if changed:
            write_annotation(destination, data)
            changed_frames += 1
        else:
            shutil.copy2(frame.path, destination)
    return changed_frames


def town_map_path(scenario: str, map_root: Path | None) -> Path | None:
    if map_root is None:
        return None
    match = re.search(r"Town([A-Za-z0-9]+)", scenario)
    if match is None:
        return None
    town = match.group(1)
    candidates = [map_root / f"Town{town}_HD_map.npz"]
    if town.endswith("HD"):
        candidates.append(map_root / f"Town{town[:-2]}_HD_map.npz")
    return next((path for path in candidates if path.is_file()), None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Bench2Drive의 모든 traffic light relevance를 future route와 "
            "stop line 기준으로 재판정하고 보수적으로 보정합니다."
        )
    )
    parser.add_argument(
        "--input",
        required=True,
        help="anno 폴더, 단일 시나리오 폴더 또는 데이터셋 루트",
    )
    parser.add_argument(
        "--output",
        default="traffic_light_relabel_output",
        help="AUTO_FIX annotation 복사본 루트",
    )
    parser.add_argument(
        "--report-dir",
        help="CSV report 폴더 (기본값: <output>/reports)",
    )
    parser.add_argument(
        "--map-root",
        type=Path,
        default=Path(__file__).resolve().parent / "maps",
        help="Town*_HD_map.npz 폴더 (파일이 없으면 trajectory-only scoring)",
    )
    parser.add_argument("--no-map", action="store_true", help="HD map matching 비활성화")
    parser.add_argument("--dry-run", action="store_true", help="report만 생성하고 annotation은 만들지 않음")
    parser.add_argument("--reports-only", action="store_true", help="dry-run과 동일하게 annotation 복사본을 만들지 않음")
    parser.add_argument(
        "--write-corrected-anno",
        action="store_true",
        help="호환 옵션. non-dry-run에서는 기본적으로 AUTO_FIX 복사본을 생성",
    )
    parser.add_argument("--scenario", action="append", help="이 문자열이 포함된 scenario만 처리 (반복 가능)")
    parser.add_argument("--max-scenarios", type=int, help="앞에서부터 처리할 scenario 수")
    parser.add_argument("--approach-distance", type=float, default=60.0)
    parser.add_argument("--contact-radius", type=float, default=2.0)
    parser.add_argument("--max-step", type=float, default=5.0)
    parser.add_argument("--merge-gap-frames", type=int, default=10)
    parser.add_argument("--crossing-margin", type=float, default=0.5)
    parser.add_argument("--ambiguity-margin", type=float, default=0.75)
    parser.add_argument("--event-index-tolerance", type=int, default=20)
    parser.add_argument("--route-match-distance", type=float, default=2.0)
    parser.add_argument("--score-threshold", type=float, default=0.90)
    parser.add_argument("--margin-threshold", type=float, default=0.20)
    parser.add_argument("--prediction-threshold", type=float, default=0.50)
    parser.add_argument("--temporal-window", type=int, default=5)
    parser.add_argument("--temporal-min-frames", type=int, default=3)
    parser.add_argument("--temporal-support-threshold", type=float, default=0.80)
    parser.add_argument("--state-response-window", type=int, default=15)
    parser.add_argument("--state-response-min-speed", type=float, default=0.30)
    parser.add_argument("--state-response-run-frames", type=int, default=3)
    parser.add_argument("--map-lane-search-radius", type=float, default=4.0)
    parser.add_argument("--map-sample-stride", type=int, default=10)
    parser.add_argument(
        "--max-map-file-mb",
        type=float,
        default=256.0,
        help="이 크기를 넘는 dense NPZ map은 메모리 보호를 위해 생략 (기본값: 256)",
    )
    # Accepted for command compatibility with the previous relabeler.
    parser.add_argument("--scenario-output-root", help=argparse.SUPPRESS)
    parser.add_argument("--scenario-output-subdir", default="", help=argparse.SUPPRESS)
    return parser


def validate_args(args: argparse.Namespace) -> Config:
    positive = {
        "approach_distance": args.approach_distance,
        "contact_radius": args.contact_radius,
        "max_step": args.max_step,
        "crossing_margin": args.crossing_margin,
        "ambiguity_margin": args.ambiguity_margin,
        "route_match_distance": args.route_match_distance,
        "map_lane_search_radius": args.map_lane_search_radius,
        "max_map_file_mb": args.max_map_file_mb,
        "state_response_min_speed": args.state_response_min_speed,
    }
    for name, value in positive.items():
        if value <= 0:
            raise ValueError(f"--{name.replace('_', '-')} 값은 0보다 커야 합니다.")
    if (
        args.merge_gap_frames < 0
        or args.event_index_tolerance < 0
        or args.temporal_window < 0
        or args.temporal_min_frames < 1
        or args.map_sample_stride < 1
        or args.state_response_window < 1
        or args.state_response_run_frames < 1
    ):
        raise ValueError("frame 관련 정수 옵션은 0 이상이어야 합니다.")
    unit_values = {
        "score_threshold": args.score_threshold,
        "margin_threshold": args.margin_threshold,
        "prediction_threshold": args.prediction_threshold,
        "temporal_support_threshold": args.temporal_support_threshold,
    }
    for name, value in unit_values.items():
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"--{name.replace('_', '-')} 값은 0~1이어야 합니다.")
    if args.max_scenarios is not None and args.max_scenarios < 1:
        raise ValueError("--max-scenarios 값은 1 이상이어야 합니다.")
    return Config(
        approach_distance=args.approach_distance,
        contact_radius=args.contact_radius,
        max_step=args.max_step,
        merge_gap_frames=args.merge_gap_frames,
        crossing_margin=args.crossing_margin,
        ambiguity_margin=args.ambiguity_margin,
        event_index_tolerance=args.event_index_tolerance,
        route_match_distance=args.route_match_distance,
        score_threshold=args.score_threshold,
        margin_threshold=args.margin_threshold,
        prediction_threshold=args.prediction_threshold,
        temporal_window=args.temporal_window,
        temporal_min_frames=args.temporal_min_frames,
        temporal_support_threshold=args.temporal_support_threshold,
        map_lane_search_radius=args.map_lane_search_radius,
        map_sample_stride=args.map_sample_stride,
        state_response_window=args.state_response_window,
        state_response_min_speed=args.state_response_min_speed,
        state_response_run_frames=args.state_response_run_frames,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = validate_args(args)
        input_path = Path(args.input).expanduser().resolve()
        output_root = Path(args.output).resolve()
        report_dir = (
            Path(args.report_dir).expanduser().resolve()
            if args.report_dir
            else output_root / "reports"
        )
        scenarios = discover_scenarios(input_path)
        if args.scenario:
            scenarios = [
                item
                for item in scenarios
                if any(pattern in item[0] for pattern in args.scenario)
            ]
        if args.max_scenarios is not None:
            scenarios = scenarios[: args.max_scenarios]
        if not scenarios:
            raise ValueError("선택 조건에 맞는 scenario가 없습니다.")

        write_annotations = not args.dry_run and not args.reports_only
        if write_annotations:
            for scenario, anno_dir in scenarios:
                destination = safe_scenario_output(output_root, scenario) / "anno"
                if destination.resolve() == anno_dir.resolve():
                    raise ValueError("--output은 원본 annotation과 다른 폴더여야 합니다.")
            output_root.mkdir(parents=True, exist_ok=True)
        report_dir.mkdir(parents=True, exist_ok=True)

        map_root = None if args.no_map else args.map_root.expanduser().resolve()
        map_cache: dict[Path, MapLaneIndex] = {}
        report_path = report_dir / "traffic_light_report.csv"
        review_path = report_dir / "review_events.csv"
        auto_fix_path = report_dir / "auto_fix_events.csv"
        summary_path = report_dir / "summary.csv"
        summary_fields = [
            "scenario",
            "frames",
            "traffic_light_rows",
            "keep_frames",
            "auto_fix_frames",
            "review_frames",
            "review_events",
            "auto_fix_events",
            "changed_output_frames",
            "map_file",
        ]
        summaries: list[dict[str, Any]] = []
        with (
            report_path.open("w", newline="", encoding="utf-8-sig") as report_file,
            review_path.open("w", newline="", encoding="utf-8-sig") as review_file,
            auto_fix_path.open("w", newline="", encoding="utf-8-sig") as auto_file,
        ):
            report_writer = csv.DictWriter(
                report_file,
                fieldnames=TRAFFIC_LIGHT_REPORT_FIELDS,
            )
            review_writer = csv.DictWriter(
                review_file,
                fieldnames=DECISION_EVENT_FIELDS,
            )
            auto_writer = csv.DictWriter(
                auto_file,
                fieldnames=DECISION_EVENT_FIELDS,
            )
            report_writer.writeheader()
            review_writer.writeheader()
            auto_writer.writeheader()

            for number, (scenario, anno_dir) in enumerate(scenarios, start=1):
                map_path = town_map_path(scenario, map_root)
                map_index = None
                if map_path is not None:
                    map_size_mb = map_path.stat().st_size / (1024 * 1024)
                    if map_size_mb > args.max_map_file_mb:
                        print(
                            f"[경고] {scenario} HD map 생략: "
                            f"{map_size_mb:.1f}MB > {args.max_map_file_mb:.1f}MB",
                            file=sys.stderr,
                        )
                    elif map_path not in map_cache:
                        try:
                            map_cache[map_path] = MapLaneIndex.from_file(
                                map_path,
                                config.map_sample_stride,
                            )
                        except (OSError, ValueError, RuntimeError) as error:
                            print(f"[경고] {scenario} HD map 비활성화: {error}", file=sys.stderr)
                    if map_size_mb <= args.max_map_file_mb:
                        map_index = map_cache.get(map_path)
                files = annotation_files(anno_dir)
                analysis = analyze_relevance(files, config, map_index)
                rows = list(traffic_light_report_rows(scenario, analysis))
                reviews = grouped_decision_events(
                    scenario,
                    analysis.frames,
                    analysis.decisions,
                    "REVIEW",
                )
                auto_fixes = grouped_decision_events(
                    scenario,
                    analysis.frames,
                    analysis.decisions,
                    "AUTO_FIX",
                )
                report_writer.writerows(rows)
                review_writer.writerows(reviews)
                auto_writer.writerows(auto_fixes)
                changed = (
                    write_relabelled_annotations(output_root, scenario, analysis)
                    if write_annotations
                    else 0
                )
                counts = {
                    status: sum(
                        decision.status == status for decision in analysis.decisions
                    )
                    for status in ("KEEP", "AUTO_FIX", "REVIEW")
                }
                summaries.append(
                    {
                        "scenario": scenario,
                        "frames": len(analysis.frames),
                        "traffic_light_rows": len(rows),
                        "keep_frames": counts["KEEP"],
                        "auto_fix_frames": counts["AUTO_FIX"],
                        "review_frames": counts["REVIEW"],
                        "review_events": len(reviews),
                        "auto_fix_events": len(auto_fixes),
                        "changed_output_frames": changed,
                        "map_file": str(map_path or ""),
                    }
                )
                mode = "dry-run" if not write_annotations else f"changed={changed}"
                print(
                    f"[{number}/{len(scenarios)}] {scenario}: "
                    f"frames={len(analysis.frames)}, KEEP={counts['KEEP']}, "
                    f"AUTO_FIX={counts['AUTO_FIX']}, REVIEW={counts['REVIEW']}, {mode}"
                )

        with summary_path.open("w", newline="", encoding="utf-8-sig") as summary_file:
            summary_writer = csv.DictWriter(summary_file, fieldnames=summary_fields)
            summary_writer.writeheader()
            summary_writer.writerows(summaries)
        print("\n완료")
        print(f"시나리오: {len(scenarios)}")
        print(f"리포트: {report_dir}")
        print("annotation 생성 안 함" if not write_annotations else f"보정 복사본: {output_root}")
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"오류: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
