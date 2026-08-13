#!/usr/bin/env python3
"""Bench2Drive json.gz에서 실제 Ego 차선 변경과 command_near 분포를 함께 분석한다.

분석 항목
---------
1. 실제 Ego 차선 변경 횟수
   - 같은 (road_id, section_id) 안에서 lane_id가 변경됨
   - 변경 전/후 lane이 각각 --min-stable-frames 이상 유지된 경우만 집계
2. command_near 분포
   - LEFT, RIGHT, STRAIGHT, LANEFOLLOW, CHANGELANELEFT,
     CHANGELANERIGHT 등의 프레임 수와 비율

입력
----
- 인자를 생략하면 이 스크립트 파일이 있는 폴더 아래의 모든 시나리오
- 단일 .json.gz 파일
- 시나리오 폴더 또는 anno 폴더
- 여러 시나리오가 들어 있는 상위 폴더
- 여러 입력 폴더/파일을 한 번에 지정
- 경로를 한 줄씩 적은 .txt 파일

시나리오는 기본적으로 ``<scenario>/anno/*.json.gz`` 구조로 자동 탐색한다.

비율의 분모는 command_near를 정상적으로 읽은 전체 프레임 수이다.
차선 변경 판정은 Ego 중심점에 할당된 lane_id를 기준으로 하며,
차량 외곽이 차선 경계를 일부 침범했는지는 판정하지 않는다.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


SCRIPT_VERSION = "all-child-scenarios"


def add_boolean_flag(
    parser: argparse.ArgumentParser, name: str, default: bool, help: str
) -> None:
    """Add ``--name``/``--no-name`` with last-flag-wins semantics.

    argparse.BooleanOptionalAction needs Python 3.9; the production servers run
    Python 3.8, so the pair is registered explicitly on a shared dest.
    """
    destination = name.replace("-", "_")
    parser.add_argument(
        f"--{name}", dest=destination, action="store_true", default=default, help=help
    )
    parser.add_argument(
        f"--no-{name}",
        dest=destination,
        action="store_false",
        default=default,
        help=f"--{name} 비활성화",
    )


COMMAND_NAMES = {
    -1: "VOID",
    0: "VOID_OR_UNKNOWN",
    1: "LEFT",
    2: "RIGHT",
    3: "STRAIGHT",
    4: "LANEFOLLOW",
    5: "CHANGELANELEFT",
    6: "CHANGELANERIGHT",
}

NAME_TO_COMMAND = {
    "VOID": -1,
    "VOID_OR_UNKNOWN": 0,
    "LEFT": 1,
    "RIGHT": 2,
    "STRAIGHT": 3,
    "LANEFOLLOW": 4,
    "LANE_FOLLOW": 4,
    "CHANGELANELEFT": 5,
    "CHANGE_LANE_LEFT": 5,
    "CHANGELANERIGHT": 6,
    "CHANGE_LANE_RIGHT": 6,
}


@dataclass(frozen=True)
class LaneKey:
    road_id: Any
    section_id: Any
    lane_id: Any


@dataclass
class FrameRecord:
    clip: str
    clip_path: str
    annotation_file: str
    frame: str
    frame_number: int
    timestamp: Any
    command_id: int | None
    command_raw: Any
    lane_key: LaneKey | None
    x: Any
    y: Any
    z: Any


@dataclass
class Segment:
    key: LaneKey
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1


@dataclass
class ClipStats:
    clip: str
    clip_path: str
    discovered_frames: int = 0
    parsed_frames: int = 0
    command_valid_frames: int = 0
    command_missing_frames: int = 0
    lane_valid_frames: int = 0
    lane_missing_frames: int = 0
    error_count: int = 0
    ego_lane_change_count: int = 0
    road_or_section_change_count: int = 0
    ignored_short_lane_change_count: int = 0
    return_pair_count: int = 0


def numeric_frame(path: Path) -> int:
    """파일명에서 마지막 숫자를 프레임 번호로 사용한다."""
    numbers = re.findall(r"\d+", path.name)
    return int(numbers[-1]) if numbers else -1


def normalize_command(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, dict):
        for key in ("value", "id", "command", "name"):
            if key in value:
                return normalize_command(value[key])
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
            return int(number) if number.is_integer() else None
        except ValueError:
            pass

        normalized = text.upper().replace(" ", "_")
        if "." in normalized:
            normalized = normalized.rsplit(".", 1)[-1]
        return NAME_TO_COMMAND.get(normalized)
    return None


def nested_get(data: dict[str, Any], field_path: str) -> Any:
    current: Any = data
    for key in field_path.split("."):
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def recursive_find(data: Any, key: str) -> Any:
    if isinstance(data, dict):
        if key in data:
            return data[key]
        for value in data.values():
            found = recursive_find(value, key)
            if found is not None:
                return found
    elif isinstance(data, list):
        for value in data:
            found = recursive_find(value, key)
            if found is not None:
                return found
    return None


def read_json_gz(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError("JSON root must be an object")
    return data


def clip_root(annotation_path: Path) -> Path:
    """가장 가까운 anno 폴더의 부모를 clip root로 사용한다."""
    current = annotation_path.parent
    while True:
        if current.name.lower() == "anno":
            return current.parent
        if current == current.parent:
            break
        current = current.parent
    return annotation_path.parent


def discover_from_one_path(input_path: Path, recursive: bool) -> list[Path]:
    """입력 경로에서 Bench2Drive annotation 파일을 찾는다.

    폴더 입력 시 ``anno`` 디렉터리를 시나리오 경계로 사용한다. 따라서 여러
    시나리오가 들어 있는 상위 폴더를 지정해도 각 시나리오의 ``anno``만
    수집하며, 다른 결과 폴더의 json.gz가 섞이는 것을 방지한다.
    """
    input_path = input_path.expanduser().resolve()
    if not input_path.exists():
        raise FileNotFoundError(input_path)

    if input_path.is_file() and input_path.name.endswith(".json.gz"):
        return [input_path]

    if not input_path.is_dir():
        raise ValueError(f"지원하지 않는 입력 경로입니다: {input_path}")

    # anno 폴더 자체를 입력한 경우
    if input_path.name.lower() == "anno":
        return sorted(
            (path.resolve() for path in input_path.glob("*.json.gz")),
            key=lambda path: (numeric_frame(path), path.name),
        )

    # 여러 시나리오가 있는 상위 폴더를 먼저 확인한다.
    # 입력 폴더 자체의 ``anno``가 있더라도 즉시 반환하지 않는다. 작업용 상위
    # 폴더에 ``anno``와 여러 시나리오 폴더가 함께 있는 경우, 즉시 반환하면
    # 하위 시나리오가 모두 누락되기 때문이다.
    direct_anno = input_path / "anno"

    if recursive:
        descendant_anno_dirs = [
            path
            for path in input_path.rglob("anno")
            if path.is_dir() and path.parent != input_path
        ]
    else:
        descendant_anno_dirs = [
            child / "anno"
            for child in input_path.iterdir()
            if child.is_dir() and (child / "anno").is_dir()
        ]

    # 하위 시나리오가 있으면 입력 폴더 자체의 anno는 작업용/중복 데이터일 수
    # 있으므로 제외한다. 하위 시나리오가 없을 때만 단일 시나리오로 간주한다.
    if descendant_anno_dirs:
        anno_dirs = descendant_anno_dirs
    elif direct_anno.is_dir():
        anno_dirs = [direct_anno]
    else:
        anno_dirs = []

    found: list[Path] = []
    for anno_dir in sorted(set(anno_dirs)):
        found.extend(
            sorted(
                (path.resolve() for path in anno_dir.glob("*.json.gz")),
                key=lambda path: (numeric_frame(path), path.name),
            )
        )

    if found:
        return found

    # 표준 anno 구조가 아닌 데이터에도 사용할 수 있도록 마지막으로 직접 검색한다.
    pattern = "**/*.json.gz" if recursive else "*.json.gz"
    return sorted(path.resolve() for path in input_path.glob(pattern))


def discover(input_paths: list[Path], recursive: bool) -> list[Path]:
    """여러 파일/폴더/txt 목록에서 json.gz를 중복 없이 찾는다."""
    found: list[Path] = []

    for input_path in input_paths:
        input_path = input_path.expanduser()
        if input_path.is_file() and input_path.suffix.lower() == ".txt":
            for line_number, line in enumerate(
                input_path.read_text(encoding="utf-8").splitlines(), 1
            ):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                listed_path = Path(line).expanduser()
                if not listed_path.is_absolute():
                    listed_path = input_path.parent / listed_path
                try:
                    found.extend(discover_from_one_path(listed_path, recursive))
                except (FileNotFoundError, ValueError) as error:
                    print(
                        f"[warning] {input_path}:{line_number}: {error}",
                        file=sys.stderr,
                    )
            continue

        found.extend(discover_from_one_path(input_path, recursive))

    return sorted(set(found))


def find_ego(annotation: dict[str, Any]) -> dict[str, Any] | None:
    boxes = annotation.get("bounding_boxes")
    if not isinstance(boxes, list):
        return None
    for obj in boxes:
        if isinstance(obj, dict) and obj.get("class") == "ego_vehicle":
            return obj
    return None


def get_location(ego: dict[str, Any] | None) -> tuple[Any, Any, Any]:
    if ego is None:
        return None, None, None
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


def get_lane_key(ego: dict[str, Any] | None) -> LaneKey | None:
    if ego is None:
        return None
    required = ("road_id", "section_id", "lane_id")
    if any(key not in ego for key in required):
        return None
    return LaneKey(ego["road_id"], ego["section_id"], ego["lane_id"])


def build_segments(records: list[FrameRecord]) -> list[Segment]:
    """lane_key가 유효한 연속 레코드만 segment로 만든다.

    중간에 lane 정보가 없는 프레임이 있으면 segment를 끊어, 누락 구간을
    건너뛴 lane 변화를 차선 변경으로 오인하지 않도록 한다.
    """
    segments: list[Segment] = []
    start: int | None = None
    current_key: LaneKey | None = None
    previous_index: int | None = None

    for index, record in enumerate(records):
        key = record.lane_key
        is_contiguous = previous_index is not None and index == previous_index + 1

        if key is None:
            if start is not None and current_key is not None and previous_index is not None:
                segments.append(Segment(current_key, start, previous_index))
            start = None
            current_key = None
            previous_index = None
            continue

        if start is None:
            start = index
            current_key = key
        elif not is_contiguous or key != current_key:
            assert current_key is not None and previous_index is not None
            segments.append(Segment(current_key, start, previous_index))
            start = index
            current_key = key

        previous_index = index

    if start is not None and current_key is not None and previous_index is not None:
        segments.append(Segment(current_key, start, previous_index))

    return segments


def transition_type(before: LaneKey, after: LaneKey) -> str:
    same_road_section = (
        before.road_id == after.road_id
        and before.section_id == after.section_id
    )
    if same_road_section and before.lane_id != after.lane_id:
        return "LANE_CHANGE"
    return "ROAD_OR_SECTION_CHANGE"


def iter_lane_events(
    records: list[FrameRecord],
    segments: list[Segment],
    min_stable_frames: int,
) -> Iterable[dict[str, Any]]:
    for previous, current in zip(segments, segments[1:]):
        # lane 정보 누락으로 끊긴 segment끼리는 연결하지 않는다.
        if current.start != previous.end + 1:
            continue

        event_type = transition_type(previous.key, current.key)
        source_stable = previous.length >= min_stable_frames
        destination_stable = current.length >= min_stable_frames
        counted = event_type == "LANE_CHANGE" and source_stable and destination_stable
        record = records[current.start]

        yield {
            "clip": record.clip,
            "clip_path": record.clip_path,
            "event_type": event_type,
            "counted_as_ego_lane_change": counted,
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


def find_return_pairs(
    counted_lane_events: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
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


def command_runs(records: list[FrameRecord]) -> Counter[int]:
    """연속된 동일 command를 하나의 run으로 집계한다."""
    runs: Counter[int] = Counter()
    previous: int | None = None
    previous_was_valid = False

    for record in records:
        command = record.command_id
        if command is None:
            previous = None
            previous_was_valid = False
            continue
        if not previous_was_valid or command != previous:
            runs[command] += 1
        previous = command
        previous_was_valid = True
    return runs


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        fieldnames = list(rows[0].keys()) if rows else []

    with path.open("w", encoding="utf-8-sig", newline="") as file:
        if not fieldnames:
            return
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Bench2Drive json.gz에서 실제 Ego 차선 변경 횟수와 "
            "command_near 분포를 함께 분석합니다."
        )
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help=(
            "분석할 폴더/파일. 생략하면 이 스크립트가 있는 폴더를 분석합니다. "
            "여러 경로를 공백으로 구분해 지정할 수 있습니다."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("outputs/lane_and_command_analysis"),
        help="결과 CSV 저장 폴더",
    )
    parser.add_argument(
        "--field",
        default="command_near",
        help="command 필드 경로 (기본값: command_near)",
    )
    parser.add_argument(
        "--min-stable-frames",
        type=int,
        default=3,
        help="차선 변경 전/후 lane의 최소 연속 유지 프레임 수 (기본값: 3)",
    )
    add_boolean_flag(
        parser, "recursive", True, "입력 폴더를 재귀적으로 탐색 (기본값: true)"
    )
    add_boolean_flag(
        parser, "recursive-field-search", True,
        "command_near가 최상위에 없으면 JSON 내부에서 재귀 검색",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print(f"[version] {SCRIPT_VERSION}", file=sys.stderr)

    if args.min_stable_frames < 1:
        print("[ERROR] --min-stable-frames는 1 이상이어야 합니다.", file=sys.stderr)
        return 2

    # 경로를 생략하면 현재 작업 디렉터리가 아니라, 이 스크립트 파일이
    # 저장된 폴더를 기준으로 모든 시나리오/anno를 탐색한다.
    input_paths = list(args.paths)
    if not input_paths:
        input_paths = [Path(__file__).resolve().parent]

    try:
        files = discover(input_paths, args.recursive)
    except Exception as error:
        print(f"[ERROR] {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    if not files:
        print("[ERROR] .json.gz 파일을 찾지 못했습니다.", file=sys.stderr)
        return 1

    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    grouped_files: dict[str, list[Path]] = defaultdict(list)
    clip_names: dict[str, str] = {}
    for path in files:
        root = clip_root(path).resolve()
        clip_key = str(root)
        grouped_files[clip_key].append(path)
        clip_names[clip_key] = root.name

    for clip_key in grouped_files:
        grouped_files[clip_key].sort(key=lambda path: (numeric_frame(path), path.name))

    clip_records: dict[str, list[FrameRecord]] = defaultdict(list)
    stats: dict[str, ClipStats] = {}
    error_rows: list[dict[str, Any]] = []
    simple_key = args.field.split(".")[-1]

    total_files = len(files)
    scanned = 0

    for clip_key in sorted(grouped_files, key=lambda key: (clip_names[key], key)):
        clip = clip_names[clip_key]
        clip_stats = ClipStats(clip=clip, clip_path=clip_key)
        clip_stats.discovered_frames = len(grouped_files[clip_key])
        stats[clip_key] = clip_stats

        for path in grouped_files[clip_key]:
            scanned += 1
            try:
                data = read_json_gz(path)
                raw_command = nested_get(data, args.field)
                if (
                    raw_command is None
                    and args.recursive_field_search
                    and "." not in args.field
                ):
                    raw_command = recursive_find(data, simple_key)
                command = normalize_command(raw_command)

                ego = find_ego(data)
                lane_key = get_lane_key(ego)
                x, y, z = get_location(ego)

                record = FrameRecord(
                    clip=clip,
                    clip_path=clip_key,
                    annotation_file=str(path),
                    frame=(
                        path.name[:-len(".json.gz")]
                        if path.name.endswith(".json.gz")
                        else path.stem
                    ),
                    frame_number=numeric_frame(path),
                    timestamp=get_timestamp(data),
                    command_id=command,
                    command_raw=raw_command,
                    lane_key=lane_key,
                    x=x,
                    y=y,
                    z=z,
                )
                clip_records[clip_key].append(record)
                clip_stats.parsed_frames += 1

                if command is None:
                    clip_stats.command_missing_frames += 1
                else:
                    clip_stats.command_valid_frames += 1

                if lane_key is None:
                    clip_stats.lane_missing_frames += 1
                else:
                    clip_stats.lane_valid_frames += 1

            except Exception as error:
                clip_stats.error_count += 1
                error_rows.append(
                    {
                        "clip": clip,
                        "clip_path": clip_key,
                        "annotation_file": str(path),
                        "error": f"{type(error).__name__}: {error}",
                    }
                )

            if scanned % 1000 == 0 or scanned == total_files:
                print(f"[scan] {scanned}/{total_files}", file=sys.stderr)

    # command 집계
    overall_commands: Counter[int] = Counter()
    per_clip_commands: dict[str, Counter[int]] = {}
    per_clip_runs: dict[str, Counter[int]] = {}

    for clip_key, records in clip_records.items():
        counter = Counter(
            record.command_id for record in records if record.command_id is not None
        )
        per_clip_commands[clip_key] = counter
        per_clip_runs[clip_key] = command_runs(records)
        overall_commands.update(counter)

    # lane event 집계
    lane_event_rows: list[dict[str, Any]] = []
    for clip_key, records in clip_records.items():
        segments = build_segments(records)
        events = list(iter_lane_events(records, segments, args.min_stable_frames))
        lane_event_rows.extend(events)

        counted = [event for event in events if event["counted_as_ego_lane_change"]]
        road_changes = [
            event for event in events if event["event_type"] == "ROAD_OR_SECTION_CHANGE"
        ]
        ignored = [
            event
            for event in events
            if event["event_type"] == "LANE_CHANGE"
            and not event["counted_as_ego_lane_change"]
        ]
        return_pairs = find_return_pairs(counted)

        stats[clip_key].ego_lane_change_count = len(counted)
        stats[clip_key].road_or_section_change_count = len(road_changes)
        stats[clip_key].ignored_short_lane_change_count = len(ignored)
        stats[clip_key].return_pair_count = len(return_pairs)

    total_valid_commands = sum(overall_commands.values())
    total_lane_changes = sum(item.ego_lane_change_count for item in stats.values())
    total_road_changes = sum(item.road_or_section_change_count for item in stats.values())
    total_ignored_lane_changes = sum(
        item.ignored_short_lane_change_count for item in stats.values()
    )

    command_ids = sorted(set(COMMAND_NAMES) | set(overall_commands))

    overall_distribution_rows: list[dict[str, Any]] = []
    for command in command_ids:
        count = overall_commands.get(command, 0)
        overall_distribution_rows.append(
            {
                "command_id": command,
                "command_name": COMMAND_NAMES.get(command, f"UNKNOWN_{command}"),
                "frame_count": count,
                "percentage": round(count / total_valid_commands * 100, 6)
                if total_valid_commands
                else 0.0,
                "clip_count": sum(
                    per_clip_commands.get(key, Counter()).get(command, 0) > 0
                    for key in stats
                ),
                "run_count": sum(
                    per_clip_runs.get(key, Counter()).get(command, 0)
                    for key in stats
                ),
            }
        )

    clip_summary_rows: list[dict[str, Any]] = []
    for clip_key in sorted(stats, key=lambda key: (stats[key].clip, key)):
        item = stats[clip_key]
        valid_commands = item.command_valid_frames
        row: dict[str, Any] = {
            "clip": item.clip,
            "clip_path": item.clip_path,
            "discovered_frame_count": item.discovered_frames,
            "parsed_frame_count": item.parsed_frames,
            "command_valid_frame_count": item.command_valid_frames,
            "command_missing_frame_count": item.command_missing_frames,
            "lane_valid_frame_count": item.lane_valid_frames,
            "lane_missing_frame_count": item.lane_missing_frames,
            "error_count": item.error_count,
            "ego_lane_change_count": item.ego_lane_change_count,
            "road_or_section_change_count": item.road_or_section_change_count,
            "ignored_short_lane_change_count": item.ignored_short_lane_change_count,
            "return_pair_count": item.return_pair_count,
        }

        for command in command_ids:
            name = COMMAND_NAMES.get(command, f"UNKNOWN_{command}")
            count = per_clip_commands.get(clip_key, Counter()).get(command, 0)
            row[f"{name}_frames"] = count
            row[f"{name}_percentage"] = (
                round(count / valid_commands * 100, 6) if valid_commands else 0.0
            )
            row[f"{name}_runs"] = per_clip_runs.get(clip_key, Counter()).get(
                command, 0
            )

        clip_summary_rows.append(row)

    frame_rows: list[dict[str, Any]] = []
    for clip_key in sorted(clip_records, key=lambda key: (clip_names[key], key)):
        for record in clip_records[clip_key]:
            lane_key = record.lane_key
            frame_rows.append(
                {
                    "clip": record.clip,
                    "clip_path": record.clip_path,
                    "annotation_file": record.annotation_file,
                    "frame": record.frame,
                    "frame_number": record.frame_number,
                    "timestamp": record.timestamp,
                    "command_id": "" if record.command_id is None else record.command_id,
                    "command_name": (
                        "MISSING_OR_UNKNOWN"
                        if record.command_id is None
                        else COMMAND_NAMES.get(
                            record.command_id, f"UNKNOWN_{record.command_id}"
                        )
                    ),
                    "command_raw": json.dumps(record.command_raw, ensure_ascii=False),
                    "road_id": "" if lane_key is None else lane_key.road_id,
                    "section_id": "" if lane_key is None else lane_key.section_id,
                    "lane_id": "" if lane_key is None else lane_key.lane_id,
                    "x": record.x,
                    "y": record.y,
                    "z": record.z,
                }
            )

    overall_summary_rows = [
        {"metric": "input_path_count", "value": len(input_paths)},
        {"metric": "json_gz_file_count", "value": len(files)},
        {"metric": "clip_count", "value": len(stats)},
        {
            "metric": "parsed_frame_count",
            "value": sum(item.parsed_frames for item in stats.values()),
        },
        {"metric": "valid_command_frame_count", "value": total_valid_commands},
        {
            "metric": "missing_command_frame_count",
            "value": sum(item.command_missing_frames for item in stats.values()),
        },
        {
            "metric": "valid_lane_frame_count",
            "value": sum(item.lane_valid_frames for item in stats.values()),
        },
        {
            "metric": "missing_lane_frame_count",
            "value": sum(item.lane_missing_frames for item in stats.values()),
        },
        {"metric": "parse_error_count", "value": len(error_rows)},
        {"metric": "ego_lane_change_count", "value": total_lane_changes},
        {"metric": "road_or_section_change_count", "value": total_road_changes},
        {
            "metric": "ignored_short_lane_change_count",
            "value": total_ignored_lane_changes,
        },
        {"metric": "min_stable_frames", "value": args.min_stable_frames},
    ]

    write_csv(output_dir / "overall_summary.csv", overall_summary_rows)
    write_csv(
        output_dir / "overall_command_distribution.csv",
        overall_distribution_rows,
    )
    write_csv(output_dir / "clip_summary.csv", clip_summary_rows)
    write_csv(output_dir / "ego_lane_events.csv", lane_event_rows)
    write_csv(output_dir / "frame_analysis.csv", frame_rows)
    write_csv(output_dir / "scan_errors.csv", error_rows)

    print("\n=== 입력 경로 ===")
    for input_path in input_paths:
        print(f"- {input_path.expanduser().resolve()}")

    print("\n=== 전체 분석 결과 ===")
    print(f"JSON.GZ 파일: {len(files):,}")
    print(f"시나리오 수: {len(stats):,}")
    print(f"정상 command 프레임: {total_valid_commands:,}")
    print(
        f"실제 ego_lane_change: {total_lane_changes:,}회 "
        f"(변경 전/후 각각 {args.min_stable_frames}프레임 이상 유지)"
    )
    print(f"도로/section 전환: {total_road_changes:,}회")
    print(f"짧아서 제외된 lane 변화: {total_ignored_lane_changes:,}회")

    print("\n=== 시나리오별 요약 ===")
    for clip_key in sorted(stats, key=lambda key: (stats[key].clip, key)):
        item = stats[clip_key]
        valid = item.command_valid_frames
        lane_follow = per_clip_commands.get(clip_key, Counter()).get(4, 0)
        left = per_clip_commands.get(clip_key, Counter()).get(1, 0)
        right = per_clip_commands.get(clip_key, Counter()).get(2, 0)
        straight = per_clip_commands.get(clip_key, Counter()).get(3, 0)

        def pct(count: int) -> float:
            return count / valid * 100 if valid else 0.0

        print(
            f"- {item.clip}: frames={item.parsed_frames:,}, "
            f"ego_lane_change={item.ego_lane_change_count:,}, "
            f"LANEFOLLOW={pct(lane_follow):.2f}%, "
            f"LEFT={pct(left):.2f}%, RIGHT={pct(right):.2f}%, "
            f"STRAIGHT={pct(straight):.2f}%"
        )

    print("\n=== command_near 프레임 비율 ===")
    if total_valid_commands == 0:
        print("정상적으로 읽힌 command_near가 없습니다.")
    else:
        for row in overall_distribution_rows:
            if row["frame_count"] == 0:
                continue
            print(
                f'{row["command_name"]:<20} '
                f'{row["frame_count"]:>10,} frames '
                f'({row["percentage"]:>8.3f}%) '
                f'runs={row["run_count"]:,}'
            )

    print(f"\n결과 폴더: {output_dir}")
    print("- overall_summary.csv")
    print("- overall_command_distribution.csv")
    print("- clip_summary.csv  # 시나리오별 상세 결과")
    print("- ego_lane_events.csv")
    print("- frame_analysis.csv")
    print("- scan_errors.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
