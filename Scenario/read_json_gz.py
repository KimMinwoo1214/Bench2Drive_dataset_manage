#!/usr/bin/env python3
"""Analyze Bench2Drive command_near values from gzip JSON annotations."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

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
    parts = list(annotation_path.parts)
    lowered = [part.lower() for part in parts]
    if "anno" in lowered:
        index = len(lowered) - 1 - lowered[::-1].index("anno")
        if index > 0:
            return Path(*parts[:index])
    return annotation_path.parent


def discover(input_path: Path, recursive: bool) -> list[Path]:
    if not input_path.exists():
        raise FileNotFoundError(input_path)
    if input_path.is_file() and input_path.name.endswith(".json.gz"):
        return [input_path.resolve()]
    if input_path.is_file() and input_path.suffix.lower() == ".txt":
        found: list[Path] = []
        for line_number, line in enumerate(
            input_path.read_text(encoding="utf-8").splitlines(), 1
        ):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            path = Path(line).expanduser()
            if not path.is_absolute():
                path = (input_path.parent / path).resolve()
            if path.is_file() and path.name.endswith(".json.gz"):
                found.append(path)
            elif path.is_dir():
                pattern = "**/*.json.gz" if recursive else "*.json.gz"
                found.extend(path.glob(pattern))
            else:
                print(
                    f"[warning] {input_path}:{line_number}: missing path: {path}",
                    file=sys.stderr,
                )
        return sorted({path.resolve() for path in found})
    if input_path.is_dir():
        pattern = "**/*.json.gz" if recursive else "*.json.gz"
        return sorted(path.resolve() for path in input_path.glob(pattern))
    raise ValueError("input must be a .json.gz file, directory, or .txt path list")


def parse_selected(values: list[str]) -> set[int]:
    commands: set[int] = set()
    for value in values:
        for token in value.split(","):
            token = token.strip()
            if not token:
                continue
            command = normalize_command(token)
            if command is None:
                raise ValueError(f"unknown command: {token}")
            commands.add(command)
    return commands


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Analyze command_near distribution from Bench2Drive json.gz files"
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument(
        "--output-dir",
        default=Path("outputs/command_near_distribution"),
        type=Path,
    )
    parser.add_argument("--field", default="command_near")
    parser.add_argument(
        "--selected-command",
        action="append",
        default=[],
        help="Command name or ID. Repeat or comma-separate values.",
    )
    parser.add_argument("--min-selected-frames", type=int, default=1)
    parser.add_argument(
        "--recursive",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--recursive-field-search",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    args = parser.parse_args()

    selected_values = args.selected_command or [
        "CHANGELANELEFT",
        "CHANGELANERIGHT",
    ]

    try:
        selected_commands = parse_selected(selected_values)
        files = discover(args.input, args.recursive)
    except Exception as error:
        print(f"[ERROR] {error}", file=sys.stderr)
        return 1

    if not files:
        print("[ERROR] no .json.gz files found", file=sys.stderr)
        return 1

    args.output_dir.mkdir(parents=True, exist_ok=True)

    overall = Counter()
    per_clip = defaultdict(Counter)
    per_clip_valid = Counter()
    per_clip_missing = Counter()
    per_clip_errors = Counter()
    sequences: dict[str, list[int]] = defaultdict(list)
    clip_paths: dict[str, str] = {}
    frame_rows: list[dict[str, Any]] = []
    error_rows: list[dict[str, Any]] = []

    simple_key = args.field.split(".")[-1]

    for index, path in enumerate(files, 1):
        root = clip_root(path)
        clip_key = str(root.resolve())
        clip_paths[clip_key] = root.name

        try:
            data = read_json_gz(path)
            raw = nested_get(data, args.field)
            if raw is None and args.recursive_field_search and "." not in args.field:
                raw = recursive_find(data, simple_key)
            command = normalize_command(raw)

            if command is None:
                per_clip_missing[clip_key] += 1
                frame_rows.append(
                    {
                        "clip": root.name,
                        "clip_path": clip_key,
                        "annotation_file": str(path),
                        "command_id": "",
                        "command_name": "MISSING_OR_UNKNOWN",
                        "raw_value": json.dumps(raw, ensure_ascii=False),
                    }
                )
            else:
                overall[command] += 1
                per_clip[clip_key][command] += 1
                per_clip_valid[clip_key] += 1
                sequences[clip_key].append(command)
                frame_rows.append(
                    {
                        "clip": root.name,
                        "clip_path": clip_key,
                        "annotation_file": str(path),
                        "command_id": command,
                        "command_name": COMMAND_NAMES.get(
                            command, f"UNKNOWN_{command}"
                        ),
                        "raw_value": json.dumps(raw, ensure_ascii=False),
                    }
                )
        except Exception as error:
            per_clip_errors[clip_key] += 1
            error_rows.append(
                {
                    "clip": root.name,
                    "annotation_file": str(path),
                    "error": f"{type(error).__name__}: {error}",
                }
            )

        if index % 1000 == 0 or index == len(files):
            print(f"[scan] {index}/{len(files)}", file=sys.stderr)

    runs: dict[str, Counter[int]] = defaultdict(Counter)
    for clip_key, sequence in sequences.items():
        previous: int | None = None
        for command in sequence:
            if command != previous:
                runs[clip_key][command] += 1
                previous = command

    total = sum(overall.values())
    command_ids = sorted(set(COMMAND_NAMES) | set(overall))

    overall_rows = []
    for command in command_ids:
        count = overall.get(command, 0)
        overall_rows.append(
            {
                "command_id": command,
                "command_name": COMMAND_NAMES.get(command, f"UNKNOWN_{command}"),
                "frame_count": count,
                "percentage": round(count / total * 100, 6) if total else 0.0,
                "clip_count": sum(
                    per_clip[key].get(command, 0) > 0 for key in clip_paths
                ),
                "run_count": sum(runs[key].get(command, 0) for key in clip_paths),
            }
        )

    clip_rows = []
    selected_clips = []
    for clip_key in sorted(clip_paths, key=lambda key: clip_paths[key]):
        selected_frames = sum(
            per_clip[clip_key].get(command, 0) for command in selected_commands
        )
        row: dict[str, Any] = {
            "clip": clip_paths[clip_key],
            "clip_path": clip_key,
            "valid_frame_count": per_clip_valid[clip_key],
            "missing_command_count": per_clip_missing[clip_key],
            "error_count": per_clip_errors[clip_key],
            "selected_command_frame_count": selected_frames,
            "contains_selected_command": selected_frames
            >= args.min_selected_frames,
        }
        for command in command_ids:
            name = COMMAND_NAMES.get(command, f"UNKNOWN_{command}")
            row[f"{name}_frames"] = per_clip[clip_key].get(command, 0)
            row[f"{name}_runs"] = runs[clip_key].get(command, 0)
        clip_rows.append(row)
        if selected_frames >= args.min_selected_frames:
            selected_clips.append(clip_key)

    write_csv(args.output_dir / "overall_command_distribution.csv", overall_rows)
    write_csv(args.output_dir / "clip_command_distribution.csv", clip_rows)
    write_csv(args.output_dir / "frame_commands.csv", frame_rows)
    write_csv(args.output_dir / "scan_errors.csv", error_rows)
    (args.output_dir / "selected_clips.txt").write_text(
        "\n".join(selected_clips) + ("\n" if selected_clips else ""),
        encoding="utf-8",
    )

    print(f"JSON.GZ files: {len(files)}")
    print(f"Clips: {len(clip_paths)}")
    print(f"Valid frames: {total}")
    print(f"Selected clips: {len(selected_clips)}")
    print("\ncommand_near distribution:")
    for row in overall_rows:
        if row["frame_count"]:
            print(
                f'{row["command_id"]:>2} '
                f'{row["command_name"]:<20} '
                f'{row["frame_count"]:>9,} '
                f'({row["percentage"]:>7.3f}%) '
                f'clips={row["clip_count"]}, runs={row["run_count"]}'
            )
    print(f"\nOutput: {args.output_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())