#!/usr/bin/env python3
"""Bench2Drive annotation에서 Ego 차량의 실제 차선 변화를 찾는다.

판정 기준
---------
* 같은 (road_id, section_id)에서 lane_id가 바뀌면 LANE_CHANGE
* road_id 또는 section_id가 바뀌면 ROAD_OR_SECTION_CHANGE

주의: 이 판정은 Ego 중심점에 할당된 lane_id 변화이다. 차량 외곽이 차선을
조금 밟았는지까지 판정하려면 차량 bounding box와 차선 경계 좌표가 추가로 필요하다.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class LaneKey:
    road_id: Any
    section_id: Any
    lane_id: Any


@dataclass
class FrameRecord:
    frame: str
    frame_number: int
    timestamp: Any
    key: LaneKey
    x: Any
    y: Any
    z: Any
    source: Path


@dataclass
class Segment:
    key: LaneKey
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1


def numeric_frame(path: Path) -> int:
    """파일명에서 마지막 숫자를 프레임 번호로 사용한다."""
    numbers = re.findall(r"\d+", path.name)
    return int(numbers[-1]) if numbers else -1


def resolve_anno_dir(input_path: Path) -> Path:
    """시나리오 폴더 또는 anno 폴더를 입력받아 anno 폴더를 반환한다."""
    input_path = input_path.expanduser().resolve()
    if input_path.name == "anno" and input_path.is_dir():
        return input_path
    anno_dir = input_path / "anno"
    if anno_dir.is_dir():
        return anno_dir
    raise FileNotFoundError(
        f"anno 폴더를 찾을 수 없습니다: {input_path}\n"
        "시나리오 폴더 또는 그 안의 anno 폴더를 입력하십시오."
    )


def load_json_gz(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as file:
        return json.load(file)


def find_ego(annotation: dict[str, Any], path: Path) -> dict[str, Any]:
    boxes = annotation.get("bounding_boxes")
    if not isinstance(boxes, list):
        raise KeyError(f"bounding_boxes가 없습니다: {path}")

    for obj in boxes:
        if isinstance(obj, dict) and obj.get("class") == "ego_vehicle":
            return obj

    raise KeyError(f"class='ego_vehicle' 항목이 없습니다: {path}")


def get_location(ego: dict[str, Any]) -> tuple[Any, Any, Any]:
    location = ego.get("location", [None, None, None])
    if not isinstance(location, (list, tuple)):
        return None, None, None
    values = list(location) + [None, None, None]
    return values[0], values[1], values[2]


def get_timestamp(annotation: dict[str, Any]) -> Any:
    for key in ("timestamp", "game_time", "elapsed_seconds"):
        if key in annotation:
            return annotation[key]
    return None


def read_records(anno_dir: Path) -> list[FrameRecord]:
    paths = sorted(anno_dir.glob("*.json.gz"), key=lambda p: (numeric_frame(p), p.name))
    if not paths:
        raise FileNotFoundError(f"*.json.gz 파일이 없습니다: {anno_dir}")

    records: list[FrameRecord] = []
    for path in paths:
        annotation = load_json_gz(path)
        ego = find_ego(annotation, path)

        missing = [name for name in ("road_id", "section_id", "lane_id") if name not in ego]
        if missing:
            raise KeyError(f"Ego에 {', '.join(missing)} 필드가 없습니다: {path}")

        x, y, z = get_location(ego)
        number = numeric_frame(path)
        records.append(
            FrameRecord(
                frame=path.name.removesuffix(".json.gz"),
                frame_number=number,
                timestamp=get_timestamp(annotation),
                key=LaneKey(ego["road_id"], ego["section_id"], ego["lane_id"]),
                x=x,
                y=y,
                z=z,
                source=path,
            )
        )
    return records


def build_segments(records: list[FrameRecord]) -> list[Segment]:
    if not records:
        return []

    segments: list[Segment] = []
    start = 0
    for index in range(1, len(records)):
        if records[index].key != records[start].key:
            segments.append(Segment(records[start].key, start, index - 1))
            start = index
    segments.append(Segment(records[start].key, start, len(records) - 1))
    return segments


def transition_type(before: LaneKey, after: LaneKey) -> str:
    same_road_section = (
        before.road_id == after.road_id
        and before.section_id == after.section_id
    )
    if same_road_section and before.lane_id != after.lane_id:
        return "LANE_CHANGE"
    return "ROAD_OR_SECTION_CHANGE"


def iter_events(
    records: list[FrameRecord],
    segments: list[Segment],
    min_stable_frames: int,
) -> Iterable[dict[str, Any]]:
    for previous, current in zip(segments, segments[1:]):
        event_type = transition_type(previous.key, current.key)
        source_stable = previous.length >= min_stable_frames
        destination_stable = current.length >= min_stable_frames
        stable_transition = source_stable and destination_stable
        record = records[current.start]
        yield {
            "event_type": event_type,
            "counted": event_type == "LANE_CHANGE" and stable_transition,
            "source_stable": source_stable,
            "destination_stable": destination_stable,
            "source_frames": previous.length,
            "destination_frames": current.length,
            "frame": record.frame,
            "frame_number": record.frame_number,
            "timestamp": record.timestamp,
            "x": record.x,
            "y": record.y,
            "z": record.z,
            "from_road_id": previous.key.road_id,
            "from_section_id": previous.key.section_id,
            "from_lane_id": previous.key.lane_id,
            "to_road_id": current.key.road_id,
            "to_section_id": current.key.section_id,
            "to_lane_id": current.key.lane_id,
        }


def write_trace_csv(path: Path, records: list[FrameRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(
            ["frame", "frame_number", "timestamp", "x", "y", "z", "road_id", "section_id", "lane_id"]
        )
        for record in records:
            writer.writerow(
                [
                    record.frame,
                    record.frame_number,
                    record.timestamp,
                    record.x,
                    record.y,
                    record.z,
                    record.key.road_id,
                    record.key.section_id,
                    record.key.lane_id,
                ]
            )


def write_events_csv(path: Path, events: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(events[0].keys()) if events else [
        "event_type",
        "counted",
        "source_stable",
        "destination_stable",
        "source_frames",
        "destination_frames",
        "frame",
        "frame_number",
        "timestamp",
        "x",
        "y",
        "z",
        "from_road_id",
        "from_section_id",
        "from_lane_id",
        "to_road_id",
        "to_section_id",
        "to_lane_id",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(events)


def find_return_pairs(counted_lane_events: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """A→B 직후 B→A가 나타나는 단순 차선 이탈·복귀 쌍을 찾는다."""
    pairs = []
    for leave, comeback in zip(counted_lane_events, counted_lane_events[1:]):
        same_road_section = (
            leave["from_road_id"] == comeback["to_road_id"]
            and leave["from_section_id"] == comeback["to_section_id"]
            and leave["to_road_id"] == comeback["from_road_id"]
            and leave["to_section_id"] == comeback["from_section_id"]
        )
        returned_to_lane = (
            leave["from_lane_id"] == comeback["to_lane_id"]
            and leave["to_lane_id"] == comeback["from_lane_id"]
        )
        if same_road_section and returned_to_lane:
            pairs.append((leave, comeback))
    return pairs


def print_summary(
    records: list[FrameRecord],
    events: list[dict[str, Any]],
    min_stable_frames: int,
    trace_csv: Path,
    events_csv: Path,
) -> None:
    counted = [event for event in events if event["counted"]]
    road_changes = [event for event in events if event["event_type"] == "ROAD_OR_SECTION_CHANGE"]
    ignored = [
        event
        for event in events
        if event["event_type"] == "LANE_CHANGE" and not event["counted"]
    ]
    return_pairs = find_return_pairs(counted)

    print(f"분석 프레임: {len(records)}")
    print(f"차선 변경 감지: {len(counted)}회 (변경 전·후 lane이 각각 {min_stable_frames}프레임 이상 유지된 경우)")
    print(f"도로/section 전환: {len(road_changes)}회")
    print(f"짧아서 제외된 lane 변화: {len(ignored)}회")
    print()

    if counted:
        print("[차선 변경 이벤트]")
        for event in counted:
            print(
                f"- frame {event['frame']}: "
                f"(road={event['from_road_id']}, section={event['from_section_id']}, lane={event['from_lane_id']}) "
                f"-> (road={event['to_road_id']}, section={event['to_section_id']}, lane={event['to_lane_id']}), "
                f"목적 lane 유지 {event['destination_frames']}프레임"
            )
    else:
        print("[판정] 같은 road/section 안에서 유지된 lane_id 변화가 없습니다.")

    if return_pairs:
        print()
        print("[원래 차선 복귀 후보]")
        for leave, comeback in return_pairs:
            print(
                f"- frame {leave['frame']}에 lane {leave['from_lane_id']} -> {leave['to_lane_id']}, "
                f"frame {comeback['frame']}에 lane {comeback['from_lane_id']} -> {comeback['to_lane_id']}"
            )

    print()
    print(f"프레임별 결과: {trace_csv}")
    print(f"변화 이벤트: {events_csv}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bench2Drive anno/*.json.gz에서 Ego의 lane_id 변화를 분석합니다."
    )
    parser.add_argument(
        "path",
        type=Path,
        help="시나리오 폴더 또는 anno 폴더 경로",
    )
    parser.add_argument(
        "--min-stable-frames",
        type=int,
        default=3,
        help="차선 변경으로 인정할 새 lane의 최소 연속 프레임 수 (기본값: 3)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="CSV 저장 폴더 (기본값: 시나리오 폴더/lane_analysis)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.min_stable_frames < 1:
        print("오류: --min-stable-frames는 1 이상이어야 합니다.", file=sys.stderr)
        return 2

    try:
        anno_dir = resolve_anno_dir(args.path)
        records = read_records(anno_dir)
    except (FileNotFoundError, KeyError, OSError, json.JSONDecodeError) as error:
        print(f"오류: {error}", file=sys.stderr)
        return 1

    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir is not None
        else anno_dir.parent / "lane_analysis"
    )
    trace_csv = output_dir / "ego_lane_trace.csv"
    events_csv = output_dir / "ego_lane_events.csv"

    segments = build_segments(records)
    events = list(iter_events(records, segments, args.min_stable_frames))
    write_trace_csv(trace_csv, records)
    write_events_csv(events_csv, events)
    print_summary(records, events, args.min_stable_frames, trace_csv, events_csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
