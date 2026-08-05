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
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence


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


@dataclass(frozen=True)
class Config:
    approach_distance: float = 40.0
    contact_radius: float = 2.0
    max_step: float = 5.0
    merge_gap_frames: int = 10
    crossing_margin: float = 0.5
    ambiguity_margin: float = 0.75
    event_index_tolerance: int = 20


@dataclass
class LightObservation:
    light_id: str
    trigger_x: float
    trigger_y: float
    state: Any
    affects_ego: bool


@dataclass
class Frame:
    name: str
    path: Path
    x: float
    y: float
    lights: dict[str, LightObservation]
    original_ids: list[str]
    cumulative_distance: float = 0.0


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
        lights[light_id] = LightObservation(
            light_id=light_id,
            trigger_x=trigger_xy[0],
            trigger_y=trigger_xy[1],
            state=box.get("state"),
            affects_ego=affects_ego,
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


def safe_scenario_output(output_root: Path, scenario: str) -> Path:
    parts = [part for part in Path(scenario).parts if part not in {"", ".", ".."}]
    return output_root.joinpath(*parts)


def write_per_scenario(
    output_root: Path,
    scenario: str,
    frames: Sequence[Frame],
    labels: Sequence[FrameLabel],
    events: Sequence[Event],
) -> None:
    scenario_output = safe_scenario_output(output_root, scenario)
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
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    files = annotation_files(anno_dir)
    frames = load_frames(files, config)
    events = build_candidates(frames, config)
    resolve_candidates(events, config)
    assign_event_ids(events)
    labels = make_frame_labels(frames, events)

    frame_data = list(frame_rows(scenario, frames, labels))
    event_data = list(event_rows(scenario, frames, events))
    write_per_scenario(output_root, scenario, frames, labels, events)
    if write_corrected:
        write_corrected_annotations(
            safe_scenario_output(output_root, scenario), frames, labels
        )
    review_count = sum(row["needs_review"] == "true" for row in event_data)
    return frame_data, event_data, review_count


def write_jsonl(file: Any, rows: Iterable[dict[str, Any]]) -> None:
    for row in rows:
        file.write(json.dumps(row, ensure_ascii=False) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Bench2Drive 전체 Ego 궤적으로 신호등 이벤트를 검출하고 "
            "분석용 sidecar와 선택적인 원본 구조 보정 annotation을 생성합니다."
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
        help="결과 폴더 (기본값: traffic_light_relabel_output)",
    )
    parser.add_argument(
        "--write-corrected-anno",
        action="store_true",
        help=(
            "원본 구조를 유지한 corrected_anno를 만들고, 명확히 복구된 "
            "traffic_light 객체의 기존 affects_ego 값만 true로 변경"
        ),
    )
    parser.add_argument("--approach-distance", type=float, default=40.0)
    parser.add_argument("--contact-radius", type=float, default=2.0)
    parser.add_argument("--max-step", type=float, default=5.0)
    parser.add_argument("--merge-gap-frames", type=int, default=10)
    parser.add_argument("--crossing-margin", type=float, default=0.5)
    parser.add_argument("--ambiguity-margin", type=float, default=0.75)
    parser.add_argument("--event-index-tolerance", type=int, default=20)
    return parser


def validate_args(args: argparse.Namespace) -> Config:
    positive = {
        "approach_distance": args.approach_distance,
        "contact_radius": args.contact_radius,
        "max_step": args.max_step,
        "crossing_margin": args.crossing_margin,
        "ambiguity_margin": args.ambiguity_margin,
    }
    for name, value in positive.items():
        if value <= 0:
            raise ValueError(f"--{name.replace('_', '-')} 값은 0보다 커야 합니다.")
    if args.merge_gap_frames < 0 or args.event_index_tolerance < 0:
        raise ValueError("frame 관련 정수 옵션은 0 이상이어야 합니다.")
    return Config(
        approach_distance=args.approach_distance,
        contact_radius=args.contact_radius,
        max_step=args.max_step,
        merge_gap_frames=args.merge_gap_frames,
        crossing_margin=args.crossing_margin,
        ambiguity_margin=args.ambiguity_margin,
        event_index_tolerance=args.event_index_tolerance,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = validate_args(args)
        input_path = Path(args.input)
        output_root = Path(args.output).resolve()
        scenarios = discover_scenarios(input_path)
        output_root.mkdir(parents=True, exist_ok=True)

        frames_csv = (output_root / "all_traffic_light_frame_labels.csv").open(
            "w", newline="", encoding="utf-8-sig"
        )
        events_csv = (output_root / "all_traffic_light_events.csv").open(
            "w", newline="", encoding="utf-8-sig"
        )
        labels_jsonl = (output_root / "all_traffic_light_frame_labels.jsonl").open(
            "w", encoding="utf-8"
        )
        review_csv = (output_root / "review_queue.csv").open(
            "w", newline="", encoding="utf-8-sig"
        )
        with frames_csv, events_csv, labels_jsonl, review_csv:
            frame_writer = csv.DictWriter(frames_csv, fieldnames=FRAME_FIELDS)
            event_writer = csv.DictWriter(events_csv, fieldnames=EVENT_FIELDS)
            review_writer = csv.DictWriter(review_csv, fieldnames=EVENT_FIELDS)
            frame_writer.writeheader()
            event_writer.writeheader()
            review_writer.writeheader()

            total_frames = total_events = total_reviews = 0
            for number, (scenario, anno_dir) in enumerate(scenarios, start=1):
                frame_data, event_data, review_count = process_scenario(
                    scenario=scenario,
                    anno_dir=anno_dir,
                    output_root=output_root,
                    config=config,
                    write_corrected=args.write_corrected_anno,
                )
                frame_writer.writerows(frame_data)
                event_writer.writerows(event_data)
                review_writer.writerows(
                    row for row in event_data if row["needs_review"] == "true"
                )
                write_jsonl(labels_jsonl, frame_data)
                total_frames += len(frame_data)
                total_events += sum(
                    row["status"] in {"matched", "missing_label"}
                    for row in event_data
                )
                total_reviews += review_count
                recovered = sum(
                    row["label_source"] == "recovered" for row in frame_data
                )
                print(
                    f"[{number}/{len(scenarios)}] {scenario}: "
                    f"frames={len(frame_data)}, events={len(event_data)}, "
                    f"recovered_frames={recovered}, review={review_count}"
                )

        print("\n완료")
        print(f"시나리오: {len(scenarios)}")
        print(f"프레임: {total_frames}")
        print(f"선택된 신호등 이벤트: {total_events}")
        print(f"검토 필요 이벤트: {total_reviews}")
        print(f"결과 폴더: {output_root}")
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"오류: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())