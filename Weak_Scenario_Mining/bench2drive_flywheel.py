#!/usr/bin/env python3
"""
Bench2Drive Native-Result Data Flywheel.

Primary input:
    Bench2Drive evaluator's native result.json

The program detects and parses:
    1) Native Bench2Drive result.json: _checkpoint.records
    2) Generic JSON list / records list
    3) CSV / JSONL

Commands:
    base-distribution
    validate
    analyze
    select
    build
    download
    compare
    run-all

Only optional Hugging Face download requires an external package.
All other stages use the Python standard library.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import re
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import mean
from typing import Any, Iterable


ARCHIVE_RE = re.compile(
    r"^(?P<scenario>.+?)_"
    r"(?P<town>Town[^_]+)_"
    r"Route(?P<route>\d+)_"
    r"Weather(?P<weather>\d+)\.tar\.gz$"
)
EVALUATION_SUFFIX_RE = re.compile(r"_\d+$")

COLUMN_ALIASES = {
    "route_id": ["route_id", "route", "id"],
    "scenario": ["scenario", "scenario_type", "scenario_name", "type"],
    "filename": ["filename", "file", "archive", "clip"],
    "driving_score": ["driving_score", "ds", "score_composed"],
    "route_completion": ["route_completion", "rc", "score_route"],
    "infraction_penalty": ["infraction_penalty", "score_penalty", "penalty"],
    "success": ["success", "succeeded", "is_success"],
    "collision_count": ["collision_count", "collisions", "collision"],
    "blocked": ["blocked", "is_blocked"],
    "timeout": ["timeout", "is_timeout"],
    "infraction_count": ["infraction_count", "infractions"],
    "town": ["town", "town_name", "map"],
    "weather": ["weather", "weather_id"],
    "status": ["status", "result"],
    "notes": ["notes", "note", "failure_reason"],
    "target_ability": ["target_ability", "ability"],
}


@dataclass
class EvalRow:
    source_file: str
    route_id: str
    evaluation_scenario: str
    scenario: str
    target_ability: str | None
    table2_abilities: list[str]
    filename: str | None
    driving_score: float
    route_completion: float
    infraction_penalty: float
    success: bool
    status: str
    runtime_failure: bool
    collision_count: int
    collision_vehicle: int
    collision_pedestrian: int
    collision_layout: int
    blocked: bool
    timeout: bool
    yield_violation_count: int
    red_light_count: int
    stop_infraction_count: int
    route_deviation_count: int
    outside_lane_count: int
    critical_infraction_count: int
    min_speed_record_count: int
    town: str | None
    weather: str | None
    save_name: str | None
    duration_game: float | None
    duration_system: float | None
    notes: str
    raw_infraction_messages: dict[str, list[str]] = field(default_factory=dict)


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def dump_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return

    fieldnames: list[str] = []
    known: set[str] = set()
    for row in rows:
        for key in row:
            if key not in known:
                known.add(key)
                fieldnames.append(key)

    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            encoded: dict[str, Any] = {}
            for key, value in row.items():
                if isinstance(value, (list, dict, tuple, set)):
                    encoded[key] = json.dumps(
                        list(value) if isinstance(value, set) else value,
                        ensure_ascii=False,
                    )
                else:
                    encoded[key] = value
            writer.writerow(encoded)


def resolve_path(base: Path, value: Any) -> Path | None:
    if value in (None, ""):
        return None
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    return path


def resolve_path_list(base: Path, value: Any) -> list[Path]:
    if value in (None, ""):
        return []
    values = value if isinstance(value, list) else [value]
    resolved = []
    for item in values:
        path = resolve_path(base, item)
        if path is not None:
            resolved.append(path)
    return resolved


def load_config(config_path: Path) -> tuple[dict[str, Any], Path]:
    config_path = config_path.resolve()
    return load_json(config_path), config_path.parent


def parse_bool(value: Any, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "success", "succeeded", "pass", "passed"}:
        return True
    if text in {
        "0", "false", "no", "n", "failure", "failed", "fail",
        "timeout", "blocked"
    }:
        return False
    return default


def parse_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def pick_value(row: dict[str, Any], key: str, default: Any = None) -> Any:
    lowered = {str(k).strip().lower(): v for k, v in row.items()}
    for alias in COLUMN_ALIASES[key]:
        if alias.lower() in lowered:
            value = lowered[alias.lower()]
            if value not in (None, ""):
                return value
    return default


def parse_archive_name(filename: str) -> dict[str, str]:
    match = ARCHIVE_RE.match(Path(filename).name)
    if match is None:
        raise ValueError(f"Bench2Drive archive 이름 형식이 아닙니다: {filename}")
    return match.groupdict()


def load_ability_map(
    path: Path,
) -> tuple[dict[str, set[str]], dict[str, str], dict[str, set[str]]]:
    raw = load_json(path)
    aliases = {str(k): str(v) for k, v in raw.get("aliases", {}).items()}
    abilities = {
        str(ability): {aliases.get(str(s), str(s)) for s in scenarios}
        for ability, scenarios in raw["abilities"].items()
    }
    reverse: dict[str, set[str]] = defaultdict(set)
    for ability, scenarios in abilities.items():
        for scenario in scenarios:
            reverse[scenario].add(ability)
    return abilities, aliases, reverse


def normalize_scenario(name: str, aliases: dict[str, str]) -> tuple[str, str]:
    evaluation_name = EVALUATION_SUFFIX_RE.sub("", str(name).strip())
    canonical = aliases.get(evaluation_name, evaluation_name)
    return evaluation_name, canonical


def load_evaluation_plan(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    mapping: dict[str, str] = {}
    for index, row in enumerate(rows, 1):
        route_id = str(row.get("route_id", "")).strip()
        ability = str(row.get("target_ability", "")).strip()
        if not route_id or not ability:
            raise ValueError(
                f"evaluation_plan {index}행에 route_id와 target_ability가 필요합니다."
            )
        if route_id in mapping and mapping[route_id] != ability:
            raise ValueError(f"evaluation_plan에 route_id가 중복되었습니다: {route_id}")
        mapping[route_id] = ability
    return mapping


def is_runtime_failure(status: str, runtime_patterns: list[str]) -> bool:
    lowered = status.lower()
    return any(pattern.lower() in lowered for pattern in runtime_patterns)


def count_list(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def list_messages(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x) for x in value]
    return []


def determine_success(
    *,
    status: str,
    driving_score: float,
    route_completion: float,
    infraction_penalty: float,
    policy: str,
    epsilon: float,
) -> bool:
    completed = status.strip().lower() == "completed"
    if policy == "status_completed":
        return completed
    if policy == "composed_100":
        return completed and driving_score >= 100.0 - epsilon
    if policy == "strict_score":
        return (
            completed
            and route_completion >= 100.0 - epsilon
            and infraction_penalty >= 1.0 - epsilon
            and driving_score >= 100.0 - epsilon
        )
    raise ValueError(
        "evaluation.success_policy는 strict_score, composed_100, "
        "status_completed 중 하나여야 합니다."
    )


def normalize_native_b2d(
    *,
    path: Path,
    data: dict[str, Any],
    aliases: dict[str, str],
    reverse_map: dict[str, set[str]],
    plan: dict[str, str],
    eval_cfg: dict[str, Any],
) -> tuple[list[EvalRow], dict[str, Any]]:
    checkpoint = data.get("_checkpoint")
    if not isinstance(checkpoint, dict) or not isinstance(checkpoint.get("records"), list):
        raise ValueError(f"Native Bench2Drive result 구조가 아닙니다: {path}")

    records = checkpoint["records"]
    runtime_patterns = [
        str(x)
        for x in eval_cfg.get(
            "runtime_failure_patterns",
            [
                "TickRuntime",
                "Simulation crashed",
                "Agent setup failed",
                "RuntimeError",
                "Evaluator exception",
            ],
        )
    ]
    policy = str(eval_cfg.get("success_policy", "strict_score"))
    epsilon = float(eval_cfg.get("score_epsilon", 1e-6))

    rows: list[EvalRow] = []
    for index, record in enumerate(records, 1):
        scenario_raw = record.get("scenario_name")
        if not scenario_raw:
            raise ValueError(f"{path}: records[{index}]에 scenario_name이 없습니다.")

        evaluation_scenario, scenario = normalize_scenario(
            str(scenario_raw), aliases
        )
        scores = record.get("scores") or {}
        infractions = record.get("infractions") or {}
        meta = record.get("meta") or {}

        route_id = str(record.get("route_id", "")).strip()
        status = str(record.get("status", "")).strip()
        driving_score = parse_float(scores.get("score_composed"), 0.0)
        route_completion = parse_float(scores.get("score_route"), 0.0)
        infraction_penalty = parse_float(scores.get("score_penalty"), 1.0)
        runtime_failure = is_runtime_failure(status, runtime_patterns)

        collision_vehicle = count_list(infractions.get("collisions_vehicle"))
        collision_pedestrian = count_list(infractions.get("collisions_pedestrian"))
        collision_layout = count_list(infractions.get("collisions_layout"))
        collision_count = (
            collision_vehicle + collision_pedestrian + collision_layout
        )
        blocked_count = count_list(infractions.get("vehicle_blocked"))
        timeout_count = (
            count_list(infractions.get("scenario_timeouts"))
            + count_list(infractions.get("route_timeout"))
        )
        yield_count = count_list(
            infractions.get("yield_emergency_vehicle_infractions")
        )
        red_light_count = count_list(infractions.get("red_light"))
        stop_count = count_list(infractions.get("stop_infraction"))
        route_dev_count = count_list(infractions.get("route_dev"))
        outside_lane_count = count_list(infractions.get("outside_route_lanes"))
        min_speed_count = count_list(infractions.get("min_speed_infractions"))

        success = determine_success(
            status=status,
            driving_score=driving_score,
            route_completion=route_completion,
            infraction_penalty=infraction_penalty,
            policy=policy,
            epsilon=epsilon,
        )

        critical_infraction_count = (
            collision_count
            + blocked_count
            + timeout_count
            + yield_count
            + red_light_count
            + stop_count
            + route_dev_count
            + outside_lane_count
        )

        reasons: list[str] = []
        if runtime_failure:
            reasons.append("runtime_failure_rerun_required")
        if collision_vehicle:
            reasons.append(f"vehicle_collision={collision_vehicle}")
        if collision_pedestrian:
            reasons.append(f"pedestrian_collision={collision_pedestrian}")
        if collision_layout:
            reasons.append(f"layout_collision={collision_layout}")
        if blocked_count:
            reasons.append(f"vehicle_blocked={blocked_count}")
        if timeout_count:
            reasons.append(f"timeout={timeout_count}")
        if yield_count:
            reasons.append(f"yield_violation={yield_count}")
        if red_light_count:
            reasons.append(f"red_light={red_light_count}")
        if stop_count:
            reasons.append(f"stop_infraction={stop_count}")
        if route_dev_count:
            reasons.append(f"route_deviation={route_dev_count}")
        if outside_lane_count:
            reasons.append(f"outside_lane={outside_lane_count}")
        if route_completion < 100.0 - epsilon:
            reasons.append(f"route_completion={route_completion:g}")
        if infraction_penalty < 1.0 - epsilon:
            reasons.append(f"penalty={infraction_penalty:g}")

        raw_messages = {
            key: list_messages(value)
            for key, value in infractions.items()
            if isinstance(value, list) and value
        }

        rows.append(
            EvalRow(
                source_file=str(path),
                route_id=route_id,
                evaluation_scenario=evaluation_scenario,
                scenario=scenario,
                target_ability=plan.get(route_id),
                table2_abilities=sorted(reverse_map.get(scenario, set())),
                filename=None,
                driving_score=driving_score,
                route_completion=route_completion,
                infraction_penalty=infraction_penalty,
                success=success,
                status=status,
                runtime_failure=runtime_failure,
                collision_count=collision_count,
                collision_vehicle=collision_vehicle,
                collision_pedestrian=collision_pedestrian,
                collision_layout=collision_layout,
                blocked=blocked_count > 0,
                timeout=timeout_count > 0,
                yield_violation_count=yield_count,
                red_light_count=red_light_count,
                stop_infraction_count=stop_count,
                route_deviation_count=route_dev_count,
                outside_lane_count=outside_lane_count,
                critical_infraction_count=critical_infraction_count,
                min_speed_record_count=min_speed_count,
                town=str(record.get("town_name", "")).strip() or None,
                weather=str(record.get("weather_id", "")).strip() or None,
                save_name=str(record.get("save_name", "")).strip() or None,
                duration_game=parse_optional_float(meta.get("duration_game")),
                duration_system=parse_optional_float(meta.get("duration_system")),
                notes="; ".join(reasons),
                raw_infraction_messages=raw_messages,
            )
        )

    source_summary = {
        "source_file": str(path),
        "format": "bench2drive_native",
        "entry_status": data.get("entry_status"),
        "eligible": data.get("eligible"),
        "progress": checkpoint.get("progress"),
        "b2d_metrics": data.get("b2d_metrics"),
        "global_record": checkpoint.get("global_record"),
        "record_count": len(rows),
    }
    return rows, source_summary


def read_generic_records(path: Path, data: Any | None = None) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    if suffix in {".jsonl", ".ndjson"}:
        records = []
        with path.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{line_no} JSON 파싱 실패") from exc
        return records
    if suffix == ".json":
        raw = data if data is not None else load_json(path)
        if isinstance(raw, list):
            return [dict(x) for x in raw]
        if isinstance(raw, dict):
            for key in ("records", "results", "episodes", "routes"):
                if isinstance(raw.get(key), list):
                    return [dict(x) for x in raw[key]]
        raise ValueError(
            "일반 JSON은 list 또는 records/results/episodes/routes list가 필요합니다."
        )
    raise ValueError(f"지원하지 않는 평가 파일 형식입니다: {path}")


def normalize_generic_records(
    *,
    path: Path,
    records: list[dict[str, Any]],
    aliases: dict[str, str],
    reverse_map: dict[str, set[str]],
    plan: dict[str, str],
    eval_cfg: dict[str, Any],
) -> tuple[list[EvalRow], dict[str, Any]]:
    runtime_patterns = [
        str(x)
        for x in eval_cfg.get(
            "runtime_failure_patterns",
            ["TickRuntime", "Simulation crashed", "RuntimeError"],
        )
    ]
    policy = str(eval_cfg.get("success_policy", "strict_score"))
    epsilon = float(eval_cfg.get("score_epsilon", 1e-6))

    normalized: list[EvalRow] = []
    for index, record in enumerate(records, 1):
        filename_raw = pick_value(record, "filename")
        parsed: dict[str, str] = {}
        if filename_raw:
            try:
                parsed = parse_archive_name(str(filename_raw))
            except ValueError:
                parsed = {}

        scenario_raw = pick_value(record, "scenario", parsed.get("scenario"))
        if not scenario_raw:
            raise ValueError(f"{path}: generic record {index}에 scenario가 없습니다.")

        evaluation_scenario, scenario = normalize_scenario(
            str(scenario_raw), aliases
        )
        route_id = str(pick_value(record, "route_id", "")).strip()
        status = str(pick_value(record, "status", "")).strip()
        ds = parse_float(pick_value(record, "driving_score"), 0.0)
        rc = parse_float(pick_value(record, "route_completion"), 0.0)
        penalty = parse_float(pick_value(record, "infraction_penalty"), 1.0)
        runtime_failure = is_runtime_failure(status, runtime_patterns)

        success_raw = pick_value(record, "success")
        if success_raw in (None, ""):
            success = determine_success(
                status=status,
                driving_score=ds,
                route_completion=rc,
                infraction_penalty=penalty,
                policy=policy,
                epsilon=epsilon,
            )
        else:
            success = parse_bool(success_raw)

        collision_count = parse_int(pick_value(record, "collision_count"), 0)
        blocked = parse_bool(pick_value(record, "blocked"), False)
        timeout = parse_bool(pick_value(record, "timeout"), False)
        critical = collision_count + int(blocked) + int(timeout)

        target_ability = (
            plan.get(route_id)
            or str(pick_value(record, "target_ability", "")).strip()
            or None
        )

        normalized.append(
            EvalRow(
                source_file=str(path),
                route_id=route_id,
                evaluation_scenario=evaluation_scenario,
                scenario=scenario,
                target_ability=target_ability,
                table2_abilities=sorted(reverse_map.get(scenario, set())),
                filename=str(filename_raw).strip() if filename_raw else None,
                driving_score=ds,
                route_completion=rc,
                infraction_penalty=penalty,
                success=success,
                status=status,
                runtime_failure=runtime_failure,
                collision_count=collision_count,
                collision_vehicle=0,
                collision_pedestrian=0,
                collision_layout=0,
                blocked=blocked,
                timeout=timeout,
                yield_violation_count=0,
                red_light_count=0,
                stop_infraction_count=0,
                route_deviation_count=0,
                outside_lane_count=0,
                critical_infraction_count=critical,
                min_speed_record_count=0,
                town=(
                    str(pick_value(record, "town", parsed.get("town", ""))).strip()
                    or None
                ),
                weather=(
                    str(pick_value(record, "weather", parsed.get("weather", ""))).strip()
                    or None
                ),
                save_name=None,
                duration_game=None,
                duration_system=None,
                notes=str(pick_value(record, "notes", "")).strip(),
                raw_infraction_messages={},
            )
        )

    return normalized, {
        "source_file": str(path),
        "format": "generic_tabular",
        "record_count": len(normalized),
    }


def load_evaluation_sources(
    *,
    paths: list[Path],
    aliases: dict[str, str],
    reverse_map: dict[str, set[str]],
    plan: dict[str, str],
    eval_cfg: dict[str, Any],
) -> tuple[list[EvalRow], list[dict[str, Any]]]:
    all_rows: list[EvalRow] = []
    summaries: list[dict[str, Any]] = []

    for path in paths:
        if not path.exists():
            raise FileNotFoundError(path)
        if path.suffix.lower() == ".json":
            data = load_json(path)
            if (
                isinstance(data, dict)
                and isinstance(data.get("_checkpoint"), dict)
                and isinstance(data["_checkpoint"].get("records"), list)
            ):
                rows, summary = normalize_native_b2d(
                    path=path,
                    data=data,
                    aliases=aliases,
                    reverse_map=reverse_map,
                    plan=plan,
                    eval_cfg=eval_cfg,
                )
            else:
                records = read_generic_records(path, data=data)
                rows, summary = normalize_generic_records(
                    path=path,
                    records=records,
                    aliases=aliases,
                    reverse_map=reverse_map,
                    plan=plan,
                    eval_cfg=eval_cfg,
                )
        else:
            records = read_generic_records(path)
            rows, summary = normalize_generic_records(
                path=path,
                records=records,
                aliases=aliases,
                reverse_map=reverse_map,
                plan=plan,
                eval_cfg=eval_cfg,
            )
        all_rows.extend(rows)
        summaries.append(summary)

    duplicate_routes = [
        route_id
        for route_id, count in Counter(
            row.route_id for row in all_rows if row.route_id
        ).items()
        if count > 1
    ]
    if duplicate_routes and not eval_cfg.get("allow_duplicate_route_ids", False):
        raise ValueError(
            "평가 입력에 중복 route_id가 있습니다. rerun 병합 시에는 "
            "evaluation.allow_duplicate_route_ids=true를 설정하거나 "
            f"최종 결과만 남기십시오: {duplicate_routes[:10]}"
        )

    return all_rows, summaries


def row_to_export_dict(row: EvalRow, include_raw: bool = False) -> dict[str, Any]:
    data = asdict(row)
    if not include_raw:
        data.pop("raw_infraction_messages", None)
    return data


def summarize_scenarios(
    *,
    rows: list[EvalRow],
    thresholds: dict[str, Any],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[EvalRow]] = defaultdict(list)
    for row in rows:
        grouped[row.scenario].append(row)

    ds_threshold = float(thresholds.get("driving_score_below", 50.0))
    rc_threshold = float(thresholds.get("route_completion_below", 80.0))
    success_threshold = float(thresholds.get("success_rate_below", 1.0))
    exclude_runtime = bool(thresholds.get("exclude_runtime_failures", True))
    collision_weak = bool(thresholds.get("collision_always_weak", True))
    blocked_weak = bool(thresholds.get("blocked_always_weak", True))
    timeout_weak = bool(thresholds.get("timeout_always_weak", True))
    yield_weak = bool(thresholds.get("yield_violation_always_weak", True))
    signal_weak = bool(thresholds.get("signal_violation_always_weak", True))

    summaries: list[dict[str, Any]] = []

    for scenario, scenario_rows in grouped.items():
        runtime_rows = [x for x in scenario_rows if x.runtime_failure]
        valid_rows = (
            [x for x in scenario_rows if not x.runtime_failure]
            if exclude_runtime
            else list(scenario_rows)
        )

        abilities = sorted(
            {
                ability
                for row in scenario_rows
                for ability in row.table2_abilities
            }
        )
        target_abilities = sorted(
            {row.target_ability for row in scenario_rows if row.target_ability}
        )

        if not valid_rows:
            summaries.append(
                {
                    "scenario": scenario,
                    "table2_abilities": abilities,
                    "target_abilities": target_abilities,
                    "episodes_total": len(scenario_rows),
                    "episodes_valid": 0,
                    "runtime_failure_count": len(runtime_rows),
                    "needs_rerun": True,
                    "is_weak": False,
                    "priority_score": 0.0,
                    "reasons": ["all_routes_are_runtime_failures"],
                    "rerun_route_ids": [x.route_id for x in runtime_rows],
                }
            )
            continue

        n = len(valid_rows)
        mean_ds = mean(x.driving_score for x in valid_rows)
        mean_rc = mean(x.route_completion for x in valid_rows)
        mean_penalty = mean(x.infraction_penalty for x in valid_rows)
        success_rate = sum(x.success for x in valid_rows) / n
        collision_total = sum(x.collision_count for x in valid_rows)
        blocked_total = sum(x.blocked for x in valid_rows)
        timeout_total = sum(x.timeout for x in valid_rows)
        yield_total = sum(x.yield_violation_count for x in valid_rows)
        red_light_total = sum(x.red_light_count for x in valid_rows)
        stop_total = sum(x.stop_infraction_count for x in valid_rows)
        critical_total = sum(x.critical_infraction_count for x in valid_rows)

        reasons: list[str] = []
        if collision_weak and collision_total > 0:
            reasons.append(f"collision={collision_total}")
        if blocked_weak and blocked_total > 0:
            reasons.append(f"blocked={blocked_total}")
        if timeout_weak and timeout_total > 0:
            reasons.append(f"timeout={timeout_total}")
        if yield_weak and yield_total > 0:
            reasons.append(f"yield_violation={yield_total}")
        if signal_weak and red_light_total > 0:
            reasons.append(f"red_light={red_light_total}")
        if signal_weak and stop_total > 0:
            reasons.append(f"stop_infraction={stop_total}")
        if mean_ds < ds_threshold:
            reasons.append(f"mean_DS={mean_ds:.3f}<{ds_threshold:g}")
        if mean_rc < rc_threshold:
            reasons.append(f"mean_RC={mean_rc:.3f}<{rc_threshold:g}")
        if success_rate < success_threshold:
            reasons.append(
                f"success_rate={success_rate:.3f}<{success_threshold:g}"
            )

        collision_rate = sum(x.collision_count > 0 for x in valid_rows) / n
        blocked_rate = sum(x.blocked for x in valid_rows) / n
        timeout_rate = sum(x.timeout for x in valid_rows) / n
        yield_rate = sum(x.yield_violation_count > 0 for x in valid_rows) / n
        signal_rate = sum(
            (x.red_light_count + x.stop_infraction_count) > 0
            for x in valid_rows
        ) / n
        failure_rate = 1.0 - success_rate
        ds_deficit = max(
            0.0, (ds_threshold - mean_ds) / max(ds_threshold, 1e-9)
        )
        rc_deficit = max(
            0.0, (rc_threshold - mean_rc) / max(rc_threshold, 1e-9)
        )

        priority = (
            4.0 * collision_rate
            + 3.0 * failure_rate
            + 2.0 * ds_deficit
            + 1.5 * rc_deficit
            + 1.5 * blocked_rate
            + 1.0 * timeout_rate
            + 1.5 * yield_rate
            + 1.0 * signal_rate
            + 0.05 * min(critical_total / n, 10.0)
        )

        failed_rows = [
            x
            for x in valid_rows
            if (
                not x.success
                or x.collision_count > 0
                or x.blocked
                or x.timeout
                or x.yield_violation_count > 0
                or x.red_light_count > 0
                or x.stop_infraction_count > 0
                or x.driving_score < ds_threshold
                or x.route_completion < rc_threshold
            )
        ]

        summaries.append(
            {
                "scenario": scenario,
                "table2_abilities": abilities,
                "target_abilities": target_abilities,
                "episodes_total": len(scenario_rows),
                "episodes_valid": n,
                "runtime_failure_count": len(runtime_rows),
                "needs_rerun": bool(runtime_rows),
                "mean_driving_score": round(mean_ds, 6),
                "min_driving_score": round(
                    min(x.driving_score for x in valid_rows), 6
                ),
                "mean_route_completion": round(mean_rc, 6),
                "min_route_completion": round(
                    min(x.route_completion for x in valid_rows), 6
                ),
                "mean_infraction_penalty": round(mean_penalty, 6),
                "success_rate": round(success_rate, 6),
                "collision_total": collision_total,
                "blocked_total": blocked_total,
                "timeout_total": timeout_total,
                "yield_violation_total": yield_total,
                "red_light_total": red_light_total,
                "stop_infraction_total": stop_total,
                "critical_infraction_total": critical_total,
                "is_weak": bool(reasons),
                "priority_score": round(priority, 6),
                "reasons": reasons,
                "failed_route_ids": [x.route_id for x in failed_rows],
                "rerun_route_ids": [x.route_id for x in runtime_rows],
                "failed_towns": sorted(
                    {x.town for x in failed_rows if x.town}
                ),
                "failed_weathers": sorted(
                    {x.weather for x in failed_rows if x.weather}
                ),
            }
        )

    summaries.sort(
        key=lambda x: (
            not x.get("is_weak", False),
            -float(x.get("priority_score", 0.0)),
            x["scenario"],
        )
    )
    return summaries


def summarize_ability_rows(
    *,
    rows: list[EvalRow],
    mode: str,
    ability_names: Iterable[str],
    exclude_runtime: bool,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []

    for ability in ability_names:
        if mode == "target":
            matched = [x for x in rows if x.target_ability == ability]
        elif mode == "table2":
            matched = [x for x in rows if ability in x.table2_abilities]
        else:
            raise ValueError(mode)

        valid = (
            [x for x in matched if not x.runtime_failure]
            if exclude_runtime
            else matched
        )
        runtime_count = sum(x.runtime_failure for x in matched)

        if not valid:
            output.append(
                {
                    "ability": ability,
                    "aggregation_mode": mode,
                    "episodes_total": len(matched),
                    "episodes_valid": 0,
                    "runtime_failure_count": runtime_count,
                    "scenario_count": len({x.scenario for x in matched}),
                    "mean_driving_score": None,
                    "mean_route_completion": None,
                    "mean_infraction_penalty": None,
                    "success_rate": None,
                    "collision_total": 0,
                    "blocked_total": 0,
                    "timeout_total": 0,
                }
            )
            continue

        output.append(
            {
                "ability": ability,
                "aggregation_mode": mode,
                "episodes_total": len(matched),
                "episodes_valid": len(valid),
                "runtime_failure_count": runtime_count,
                "scenario_count": len({x.scenario for x in valid}),
                "mean_driving_score": round(
                    mean(x.driving_score for x in valid), 6
                ),
                "mean_route_completion": round(
                    mean(x.route_completion for x in valid), 6
                ),
                "mean_infraction_penalty": round(
                    mean(x.infraction_penalty for x in valid), 6
                ),
                "success_rate": round(
                    sum(x.success for x in valid) / len(valid), 6
                ),
                "collision_total": sum(x.collision_count for x in valid),
                "blocked_total": sum(x.blocked for x in valid),
                "timeout_total": sum(x.timeout for x in valid),
            }
        )

    return output


def build_analysis_report(
    *,
    source_summaries: list[dict[str, Any]],
    rows: list[EvalRow],
    scenario_summaries: list[dict[str, Any]],
    table2_abilities: list[dict[str, Any]],
    target_abilities: list[dict[str, Any]],
    path: Path,
) -> None:
    weak = [x for x in scenario_summaries if x.get("is_weak")]
    reruns = [x for x in rows if x.runtime_failure]

    lines = [
        "# Bench2Drive Native Result 분석",
        "",
        "## 입력",
        "",
        f"- 입력 파일 수: {len(source_summaries)}",
        f"- 전체 route record 수: {len(rows)}",
        f"- Runtime 재실행 대상: {len(reruns)}",
        f"- 취약 시나리오 수: {len(weak)}",
        "",
        "## Native 전체 지표",
        "",
    ]

    for source in source_summaries:
        metrics = source.get("b2d_metrics")
        if metrics:
            lines += [
                f'### `{Path(source["source_file"]).name}`',
                "",
                f'- Driving Score: {metrics.get("driving_score")}',
                f'- Success Rate: {metrics.get("success_rate")}',
                f'- Route Completion: {metrics.get("route_completion")}',
                f'- Infraction Penalty: {metrics.get("infraction_penalty")}',
                f'- Efficiency: {metrics.get("efficiency")}',
                f'- Comfortness: {metrics.get("comfortness")}',
                f'- Success: {metrics.get("success_num")}/{metrics.get("eval_num")}',
                "",
            ]

    lines += [
        "## Runtime 재실행 대상",
        "",
        "| Route ID | Scenario | Status | DS | RC |",
        "|---|---|---|---:|---:|",
    ]
    for row in reruns:
        lines.append(
            f"| {row.route_id} | {row.scenario} | {row.status} | "
            f"{row.driving_score:.3f} | {row.route_completion:.3f} |"
        )
    if not reruns:
        lines.append("| - | - | - | - | - |")

    lines += [
        "",
        "## 취약 시나리오",
        "",
        "| Rank | Scenario | Target Ability | Table 2 Ability | Valid routes | "
        "Mean DS | Mean RC | Success | Priority | 근거 |",
        "|---:|---|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for rank, row in enumerate(weak, 1):
        lines.append(
            f'| {rank} | {row["scenario"]} | '
            f'{", ".join(row.get("target_abilities", [])) or "-"} | '
            f'{", ".join(row.get("table2_abilities", [])) or "-"} | '
            f'{row["episodes_valid"]} | '
            f'{row.get("mean_driving_score", 0):.3f} | '
            f'{row.get("mean_route_completion", 0):.3f} | '
            f'{row.get("success_rate", 0):.3f} | '
            f'{row.get("priority_score", 0):.3f} | '
            f'{"; ".join(row.get("reasons", []))} |'
        )

    def ability_section(
        title: str, ability_rows: list[dict[str, Any]]
    ) -> list[str]:
        section = [
            "",
            f"## {title}",
            "",
            "| Ability | Total | Valid | Runtime | Mean DS | Mean RC | Success |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for row in ability_rows:
            ds = (
                "-"
                if row["mean_driving_score"] is None
                else f'{row["mean_driving_score"]:.3f}'
            )
            rc = (
                "-"
                if row["mean_route_completion"] is None
                else f'{row["mean_route_completion"]:.3f}'
            )
            sr = (
                "-"
                if row["success_rate"] is None
                else f'{row["success_rate"]:.3f}'
            )
            section.append(
                f'| {row["ability"]} | {row["episodes_total"]} | '
                f'{row["episodes_valid"]} | {row["runtime_failure_count"]} | '
                f"{ds} | {rc} | {sr} |"
            )
        return section

    lines += ability_section("Table 2 다중 Ability 집계", table2_abilities)
    if any(row["episodes_total"] > 0 for row in target_abilities):
        lines += ability_section("평가 계획 Target Ability 집계", target_abilities)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_manifest(path: Path) -> dict[str, dict[str, Any]]:
    raw = load_json(path)
    if not isinstance(raw, dict):
        raise ValueError(f"manifest는 filename -> metadata JSON이어야 합니다: {path}")
    result: dict[str, dict[str, Any]] = {}
    for filename, metadata in raw.items():
        result[str(filename)] = (
            dict(metadata) if isinstance(metadata, dict) else {}
        )
    return result


def load_base_files(
    *,
    full_manifest: dict[str, dict[str, Any]],
    base_manifest_path: Path | None,
    base_file_list_path: Path | None,
) -> dict[str, dict[str, Any]]:
    if base_manifest_path is not None:
        return load_manifest(base_manifest_path)
    if base_file_list_path is not None:
        result: dict[str, dict[str, Any]] = {}
        with base_file_list_path.open("r", encoding="utf-8") as f:
            for line in f:
                filename = line.strip()
                if not filename or filename.startswith("#"):
                    continue
                result[filename] = dict(full_manifest.get(filename, {}))
        return result
    return {}


def index_manifest(
    manifest: dict[str, dict[str, Any]],
    aliases: dict[str, str],
) -> list[dict[str, Any]]:
    indexed = []
    for filename, metadata in manifest.items():
        parsed = parse_archive_name(filename)
        scenario = aliases.get(parsed["scenario"], parsed["scenario"])
        indexed.append(
            {
                "filename": filename,
                "scenario": scenario,
                "town": parsed["town"],
                "route": parsed["route"],
                "weather": parsed["weather"],
                "sha256": metadata.get("sha256"),
                "size": metadata.get("size"),
            }
        )
    return indexed


def choose_balanced(
    *,
    candidates: list[dict[str, Any]],
    quota: int,
    failed_towns: set[str],
    failed_weathers: set[str],
    focus_fraction: float,
    seed: int,
) -> list[dict[str, Any]]:
    if quota <= 0 or not candidates:
        return []

    rng = random.Random(seed)
    remaining = list(candidates)
    rng.shuffle(remaining)

    selected: list[dict[str, Any]] = []
    town_counts: Counter[str] = Counter()
    weather_counts: Counter[str] = Counter()
    focus_target = min(
        quota,
        round(quota * max(0.0, min(1.0, focus_fraction))),
    )
    focus_selected = 0

    while remaining and len(selected) < quota:
        need_focus = (
            focus_selected < focus_target
            and bool(failed_towns or failed_weathers)
        )

        def key(item: dict[str, Any]) -> tuple[float, int, int, str]:
            town = str(item["town"])
            weather = str(item["weather"])
            focus_match = town in failed_towns or weather in failed_weathers
            focus_penalty = 0.0 if (not need_focus or focus_match) else 1000.0
            diversity_cost = (
                2.0 * town_counts[town]
                + 1.5 * weather_counts[weather]
                + 0.05 * town_counts[town] * weather_counts[weather]
            )
            return (
                focus_penalty + diversity_cost,
                town_counts[town],
                weather_counts[weather],
                item["filename"],
            )

        best_index = min(range(len(remaining)), key=lambda i: key(remaining[i]))
        item = remaining.pop(best_index)
        selected.append(item)
        town_counts[str(item["town"])] += 1
        weather_counts[str(item["weather"])] += 1
        if (
            str(item["town"]) in failed_towns
            or str(item["weather"]) in failed_weathers
        ):
            focus_selected += 1

    return selected


def select_additional_data(
    *,
    indexed_manifest: list[dict[str, Any]],
    scenario_summaries: list[dict[str, Any]],
    base_files: dict[str, dict[str, Any]],
    selection_cfg: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    weak = [x for x in scenario_summaries if x.get("is_weak")]

    max_weak = selection_cfg.get("max_weak_scenarios")
    if max_weak not in (None, ""):
        weak = weak[: int(max_weak)]

    default_quota = int(selection_cfg.get("quota_per_scenario", 100))
    scenario_quotas = {
        str(k): int(v)
        for k, v in selection_cfg.get("scenario_quotas", {}).items()
    }
    focus_fraction = float(
        selection_cfg.get("failed_condition_focus_fraction", 0.5)
    )
    seed = int(selection_cfg.get("random_seed", 42))

    excluded_names = set(base_files)
    excluded_hashes = {
        meta.get("sha256")
        for meta in base_files.values()
        if meta.get("sha256")
    }

    by_scenario: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in indexed_manifest:
        if item["filename"] in excluded_names:
            continue
        if item.get("sha256") and item["sha256"] in excluded_hashes:
            continue
        by_scenario[item["scenario"]].append(item)

    selected_all: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []

    for rank, summary in enumerate(weak, 1):
        scenario = summary["scenario"]
        quota = scenario_quotas.get(scenario, default_quota)
        candidates = by_scenario.get(scenario, [])
        chosen = choose_balanced(
            candidates=candidates,
            quota=quota,
            failed_towns=set(summary.get("failed_towns", [])),
            failed_weathers=set(summary.get("failed_weathers", [])),
            focus_fraction=focus_fraction,
            seed=seed + rank,
        )

        for item in chosen:
            enriched = dict(item)
            enriched["priority_rank"] = rank
            enriched["priority_score"] = summary["priority_score"]
            enriched["weakness_reasons"] = summary["reasons"]
            enriched["target_abilities"] = summary.get(
                "target_abilities", []
            )
            enriched["table2_abilities"] = summary.get(
                "table2_abilities", []
            )
            selected_all.append(enriched)

        summary_rows.append(
            {
                "priority_rank": rank,
                "scenario": scenario,
                "target_abilities": summary.get("target_abilities", []),
                "table2_abilities": summary.get("table2_abilities", []),
                "priority_score": summary["priority_score"],
                "requested_quota": quota,
                "available_after_base_exclusion": len(candidates),
                "selected_count": len(chosen),
                "selected_town_count": len({x["town"] for x in chosen}),
                "selected_weather_count": len(
                    {x["weather"] for x in chosen}
                ),
                "weakness_reasons": summary["reasons"],
            }
        )

    return selected_all, summary_rows


def build_sampling_plan(
    *,
    base_files: dict[str, dict[str, Any]],
    selected: list[dict[str, Any]],
    replay_ratio: float,
    seed: int,
) -> tuple[
    dict[str, dict[str, Any]],
    list[dict[str, Any]],
    list[str],
    dict[str, Any],
]:
    selected_manifest = {
        x["filename"]: {
            "sha256": x.get("sha256"),
            "size": x.get("size"),
        }
        for x in selected
    }
    combined = {**base_files, **selected_manifest}

    base_names = sorted(base_files)
    new_names = sorted(selected_manifest)

    replay_ratio = max(0.0, min(1.0, replay_ratio))
    new_ratio = 1.0 - replay_ratio
    if not base_names:
        replay_ratio, new_ratio = 0.0, 1.0
    if not new_names:
        replay_ratio, new_ratio = 1.0, 0.0

    plan: list[dict[str, Any]] = []
    base_weight = replay_ratio / len(base_names) if base_names else 0.0
    new_weight = new_ratio / len(new_names) if new_names else 0.0

    for filename in base_names:
        plan.append(
            {
                "filename": filename,
                "source": "base_replay",
                "sampling_weight": base_weight,
            }
        )
    for filename in new_names:
        plan.append(
            {
                "filename": filename,
                "source": "targeted_new",
                "sampling_weight": new_weight,
            }
        )

    if base_names and new_names:
        total_length = max(
            math.ceil(len(base_names) / max(replay_ratio, 1e-9)),
            math.ceil(len(new_names) / max(new_ratio, 1e-9)),
        )
        base_draws = round(total_length * replay_ratio)
        new_draws = total_length - base_draws
    else:
        base_draws = len(base_names)
        new_draws = len(new_names)

    rng = random.Random(seed)
    mixed: list[str] = []
    if base_names:
        mixed.extend(
            rng.choices(base_names, k=max(base_draws, len(base_names)))
        )
    if new_names:
        mixed.extend(
            rng.choices(new_names, k=max(new_draws, len(new_names)))
        )
    rng.shuffle(mixed)

    summary = {
        "base_unique_count": len(base_names),
        "targeted_new_unique_count": len(new_names),
        "combined_unique_count": len(combined),
        "mixed_file_list_length": len(mixed),
        "requested_base_replay_ratio": replay_ratio,
        "actual_base_fraction_in_mixed_list": (
            sum(x in base_files for x in mixed) / len(mixed)
            if mixed
            else None
        ),
    }
    return combined, plan, mixed, summary


def build_base_distribution(
    *,
    manifest: dict[str, dict[str, Any]],
    aliases: dict[str, str],
    abilities: dict[str, set[str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    counts: Counter[str] = Counter()
    for filename in manifest:
        parsed = parse_archive_name(filename)
        scenario = aliases.get(parsed["scenario"], parsed["scenario"])
        counts[scenario] += 1

    total = len(manifest)
    scenario_rows = [
        {
            "scenario": scenario,
            "count": count,
            "percentage": round(count / total * 100.0, 6),
        }
        for scenario, count in sorted(
            counts.items(), key=lambda x: (-x[1], x[0])
        )
    ]

    ability_rows = []
    for ability, scenarios in abilities.items():
        count = sum(counts.get(s, 0) for s in scenarios)
        ability_rows.append(
            {
                "ability": ability,
                "count": count,
                "percentage_of_manifest": round(
                    count / total * 100.0, 6
                ),
                "scenario_types_present": sum(
                    counts.get(s, 0) > 0 for s in scenarios
                ),
                "scenario_types_defined": len(scenarios),
            }
        )
    return scenario_rows, ability_rows


def config_paths(
    config: dict[str, Any],
    config_dir: Path,
) -> dict[str, Any]:
    raw = config.get("paths", {})
    return {
        "full_manifest": resolve_path(config_dir, raw.get("full_manifest")),
        "base_manifest": resolve_path(config_dir, raw.get("base_manifest")),
        "base_file_list": resolve_path(config_dir, raw.get("base_file_list")),
        "evaluation_results": resolve_path_list(
            config_dir, raw.get("evaluation_results")
        ),
        "evaluation_results_finetuned": resolve_path_list(
            config_dir, raw.get("evaluation_results_finetuned")
        ),
        "evaluation_plan": resolve_path(
            config_dir, raw.get("evaluation_plan")
        ),
        "ability_map": resolve_path(
            config_dir, raw.get("ability_map", "ability_map.json")
        ),
        "output_dir": resolve_path(
            config_dir, raw.get("output_dir", "outputs/experiment")
        ),
        "download_dir": resolve_path(
            config_dir, raw.get("download_dir", "downloads/experiment")
        ),
    }


def analyze_stage(
    config: dict[str, Any],
    config_dir: Path,
    *,
    evaluation_override: list[Path] | None = None,
    output_override: Path | None = None,
) -> dict[str, Any]:
    paths = config_paths(config, config_dir)
    ability_path = paths["ability_map"]
    if ability_path is None:
        raise ValueError("paths.ability_map이 필요합니다.")

    abilities, aliases, reverse_map = load_ability_map(ability_path)
    plan = load_evaluation_plan(paths["evaluation_plan"])

    evaluation_paths = (
        evaluation_override
        if evaluation_override is not None
        else paths["evaluation_results"]
    )
    if not evaluation_paths:
        raise ValueError("paths.evaluation_results가 필요합니다.")

    rows, source_summaries = load_evaluation_sources(
        paths=evaluation_paths,
        aliases=aliases,
        reverse_map=reverse_map,
        plan=plan,
        eval_cfg=config.get("evaluation", {}),
    )

    scenario_summaries = summarize_scenarios(
        rows=rows,
        thresholds=config.get("weakness_thresholds", {}),
    )
    exclude_runtime = bool(
        config.get("weakness_thresholds", {}).get(
            "exclude_runtime_failures", True
        )
    )
    table2_ability_summaries = summarize_ability_rows(
        rows=rows,
        mode="table2",
        ability_names=abilities.keys(),
        exclude_runtime=exclude_runtime,
    )
    target_ability_summaries = summarize_ability_rows(
        rows=rows,
        mode="target",
        ability_names=abilities.keys(),
        exclude_runtime=exclude_runtime,
    )

    output_dir = (
        output_override
        if output_override is not None
        else paths["output_dir"]
    )
    if output_dir is None:
        raise ValueError("paths.output_dir가 필요합니다.")
    output_dir.mkdir(parents=True, exist_ok=True)

    route_rows = [row_to_export_dict(x) for x in rows]
    raw_route_rows = [row_to_export_dict(x, include_raw=True) for x in rows]
    runtime_rows = [
        row_to_export_dict(x, include_raw=True)
        for x in rows
        if x.runtime_failure
    ]

    write_csv(output_dir / "route_metrics.csv", route_rows)
    dump_json(output_dir / "route_metrics.json", raw_route_rows)
    write_csv(output_dir / "runtime_rerun_routes.csv", runtime_rows)
    dump_json(output_dir / "evaluation_source_summary.json", source_summaries)
    write_csv(output_dir / "scenario_metrics.csv", scenario_summaries)
    dump_json(output_dir / "scenario_metrics.json", scenario_summaries)
    write_csv(
        output_dir / "ability_metrics_table2.csv",
        table2_ability_summaries,
    )
    dump_json(
        output_dir / "ability_metrics_table2.json",
        table2_ability_summaries,
    )
    write_csv(
        output_dir / "ability_metrics_target.csv",
        target_ability_summaries,
    )
    dump_json(
        output_dir / "ability_metrics_target.json",
        target_ability_summaries,
    )
    weak = [x for x in scenario_summaries if x.get("is_weak")]
    dump_json(output_dir / "weak_scenarios.json", weak)

    build_analysis_report(
        source_summaries=source_summaries,
        rows=rows,
        scenario_summaries=scenario_summaries,
        table2_abilities=table2_ability_summaries,
        target_abilities=target_ability_summaries,
        path=output_dir / "analysis_report.md",
    )

    print(
        f"[analyze] routes={len(rows)}, "
        f"runtime_rerun={sum(x.runtime_failure for x in rows)}, "
        f"weak_scenarios={len(weak)}"
    )
    return {
        "paths": paths,
        "abilities": abilities,
        "aliases": aliases,
        "reverse_map": reverse_map,
        "rows": rows,
        "source_summaries": source_summaries,
        "scenario_summaries": scenario_summaries,
        "table2_ability_summaries": table2_ability_summaries,
        "target_ability_summaries": target_ability_summaries,
        "output_dir": output_dir,
    }


def select_stage(
    config: dict[str, Any],
    config_dir: Path,
    analyzed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if analyzed is None:
        analyzed = analyze_stage(config, config_dir)

    paths = analyzed["paths"]
    if paths["full_manifest"] is None:
        raise ValueError("paths.full_manifest가 필요합니다.")

    full_manifest = load_manifest(paths["full_manifest"])
    indexed = index_manifest(full_manifest, analyzed["aliases"])
    base_files = load_base_files(
        full_manifest=full_manifest,
        base_manifest_path=paths["base_manifest"],
        base_file_list_path=paths["base_file_list"],
    )
    selected, selection_summary = select_additional_data(
        indexed_manifest=indexed,
        scenario_summaries=analyzed["scenario_summaries"],
        base_files=base_files,
        selection_cfg=config.get("selection", {}),
    )

    output_dir = analyzed["output_dir"]
    selected_manifest = {
        x["filename"]: {
            "sha256": x.get("sha256"),
            "size": x.get("size"),
        }
        for x in selected
    }

    dump_json(
        output_dir / "selected_additional_manifest.json",
        selected_manifest,
    )
    dump_json(
        output_dir / "selected_additional_details.json",
        selected,
    )
    write_csv(
        output_dir / "selected_additional_details.csv",
        selected,
    )
    write_csv(
        output_dir / "selection_summary.csv",
        selection_summary,
    )
    dump_json(
        output_dir / "selection_summary.json",
        selection_summary,
    )
    (output_dir / "selected_additional_files.txt").write_text(
        "\n".join(x["filename"] for x in selected)
        + ("\n" if selected else ""),
        encoding="utf-8",
    )

    print(
        f"[select] base={len(base_files)}, "
        f"selected_new={len(selected)}"
    )
    return {
        **analyzed,
        "full_manifest": full_manifest,
        "base_files": base_files,
        "selected": selected,
        "selection_summary": selection_summary,
    }


def build_stage(
    config: dict[str, Any],
    config_dir: Path,
    selected_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if selected_state is None:
        selected_state = select_stage(config, config_dir)

    replay_cfg = config.get("replay", {})
    combined, plan, mixed, summary = build_sampling_plan(
        base_files=selected_state["base_files"],
        selected=selected_state["selected"],
        replay_ratio=float(replay_cfg.get("base_replay_ratio", 0.4)),
        seed=int(replay_cfg.get("random_seed", 42)),
    )
    output_dir = selected_state["output_dir"]

    dump_json(output_dir / "combined_unique_manifest.json", combined)
    write_csv(output_dir / "train_sampling_plan.csv", plan)
    (output_dir / "train_files_mixed.txt").write_text(
        "\n".join(mixed) + ("\n" if mixed else ""),
        encoding="utf-8",
    )
    dump_json(output_dir / "training_plan_summary.json", summary)

    print(
        f"[build] combined_unique={len(combined)}, "
        f"mixed_entries={len(mixed)}"
    )
    return {
        **selected_state,
        "combined": combined,
        "sampling_plan": plan,
        "mixed": mixed,
        "training_summary": summary,
    }


def base_distribution_stage(
    config: dict[str, Any],
    config_dir: Path,
) -> None:
    paths = config_paths(config, config_dir)
    if paths["base_manifest"] is None:
        raise ValueError(
            "base-distribution에는 paths.base_manifest가 필요합니다."
        )
    if paths["ability_map"] is None:
        raise ValueError("paths.ability_map이 필요합니다.")
    if paths["output_dir"] is None:
        raise ValueError("paths.output_dir가 필요합니다.")

    abilities, aliases, _ = load_ability_map(paths["ability_map"])
    manifest = load_manifest(paths["base_manifest"])
    scenario_rows, ability_rows = build_base_distribution(
        manifest=manifest,
        aliases=aliases,
        abilities=abilities,
    )

    output_dir = paths["output_dir"] / "base_distribution"
    write_csv(output_dir / "scenario_distribution.csv", scenario_rows)
    write_csv(output_dir / "ability_distribution.csv", ability_rows)
    dump_json(
        output_dir / "distribution_summary.json",
        {
            "total_clips": len(manifest),
            "scenario_type_count": len(scenario_rows),
            "ability_distribution_is_overlapping": True,
        },
    )

    print(f"[base-distribution] total={len(manifest)}")
    for row in ability_rows:
        print(
            f'  {row["ability"]:18s} {row["count"]:5d} '
            f'({row["percentage_of_manifest"]:6.2f}%) '
            f'[{row["scenario_types_present"]}/'
            f'{row["scenario_types_defined"]} scenario types]'
        )


def validate_stage(
    config: dict[str, Any],
    config_dir: Path,
) -> None:
    paths = config_paths(config, config_dir)
    errors: list[str] = []
    warnings: list[str] = []

    for key in ("ability_map", "output_dir"):
        if paths[key] is None:
            errors.append(f"paths.{key}가 없습니다.")

    for path in paths["evaluation_results"]:
        if not path.exists():
            errors.append(f"평가 결과 파일이 없습니다: {path}")

    for key in ("full_manifest", "base_manifest", "base_file_list", "evaluation_plan"):
        path = paths[key]
        if path is not None and not path.exists():
            errors.append(f"{key} 파일이 없습니다: {path}")

    if errors:
        raise ValueError("\n".join(errors))

    abilities, aliases, reverse_map = load_ability_map(paths["ability_map"])
    plan = load_evaluation_plan(paths["evaluation_plan"])
    rows, source_summaries = load_evaluation_sources(
        paths=paths["evaluation_results"],
        aliases=aliases,
        reverse_map=reverse_map,
        plan=plan,
        eval_cfg=config.get("evaluation", {}),
    )

    route_ids = [x.route_id for x in rows if x.route_id]
    duplicate_route_ids = [
        route_id
        for route_id, count in Counter(route_ids).items()
        if count > 1
    ]
    unmapped_scenarios = sorted(
        {x.scenario for x in rows if not x.table2_abilities}
    )
    unmatched_plan_routes = sorted(set(plan) - set(route_ids))
    missing_target_abilities = sorted(
        {x.route_id for x in rows if plan and not x.target_ability}
    )

    expected_routes = config.get("evaluation", {}).get("expected_routes")
    if expected_routes not in (None, "") and len(rows) != int(expected_routes):
        warnings.append(
            f"평가 route 수가 expected_routes와 다릅니다: "
            f"{len(rows)} != {expected_routes}"
        )

    for source in source_summaries:
        metrics = source.get("b2d_metrics") or {}
        expected = metrics.get("expected")
        eval_num = metrics.get("eval_num")
        if expected not in (None, "") and eval_num not in (None, ""):
            if int(eval_num) != int(expected):
                warnings.append(
                    f'{Path(source["source_file"]).name}: '
                    f"eval_num={eval_num}, expected={expected}"
                )

    result: dict[str, Any] = {
        "evaluation_source_count": len(paths["evaluation_results"]),
        "evaluation_record_count": len(rows),
        "runtime_failure_count": sum(x.runtime_failure for x in rows),
        "success_count": sum(x.success for x in rows),
        "duplicate_route_ids": duplicate_route_ids,
        "unmapped_evaluation_scenarios": unmapped_scenarios,
        "evaluation_plan_route_count": len(plan),
        "evaluation_plan_unmatched_routes": unmatched_plan_routes,
        "routes_missing_target_ability": missing_target_abilities,
        "warnings": warnings,
    }

    if paths["full_manifest"] is not None:
        full_manifest = load_manifest(paths["full_manifest"])
        full_scenarios = {
            aliases.get(parse_archive_name(x)["scenario"], parse_archive_name(x)["scenario"])
            for x in full_manifest
        }
        result["full_manifest_count"] = len(full_manifest)
        result["weak_candidate_scenarios_absent_from_full"] = sorted(
            {x.scenario for x in rows} - full_scenarios
        )

    if paths["base_manifest"] is not None:
        result["base_manifest_count"] = len(
            load_manifest(paths["base_manifest"])
        )

    dump_json(paths["output_dir"] / "validation.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def compare_stage(
    config: dict[str, Any],
    config_dir: Path,
) -> None:
    paths = config_paths(config, config_dir)
    before_paths = paths["evaluation_results"]
    after_paths = paths["evaluation_results_finetuned"]
    if not before_paths or not after_paths:
        raise ValueError(
            "compare에는 evaluation_results와 "
            "evaluation_results_finetuned가 필요합니다."
        )

    before_dir = paths["output_dir"] / "_compare_before"
    after_dir = paths["output_dir"] / "_compare_after"

    before = analyze_stage(
        config,
        config_dir,
        evaluation_override=before_paths,
        output_override=before_dir,
    )
    after = analyze_stage(
        config,
        config_dir,
        evaluation_override=after_paths,
        output_override=after_dir,
    )

    before_s = {
        x["scenario"]: x
        for x in before["scenario_summaries"]
        if x.get("episodes_valid", 0) > 0
    }
    after_s = {
        x["scenario"]: x
        for x in after["scenario_summaries"]
        if x.get("episodes_valid", 0) > 0
    }

    scenario_delta = []
    for scenario in sorted(set(before_s) | set(after_s)):
        b = before_s.get(scenario, {})
        a = after_s.get(scenario, {})
        scenario_delta.append(
            {
                "scenario": scenario,
                "before_mean_ds": b.get("mean_driving_score"),
                "after_mean_ds": a.get("mean_driving_score"),
                "delta_mean_ds": (
                    a["mean_driving_score"] - b["mean_driving_score"]
                    if a.get("mean_driving_score") is not None
                    and b.get("mean_driving_score") is not None
                    else None
                ),
                "before_mean_rc": b.get("mean_route_completion"),
                "after_mean_rc": a.get("mean_route_completion"),
                "delta_mean_rc": (
                    a["mean_route_completion"]
                    - b["mean_route_completion"]
                    if a.get("mean_route_completion") is not None
                    and b.get("mean_route_completion") is not None
                    else None
                ),
                "before_success_rate": b.get("success_rate"),
                "after_success_rate": a.get("success_rate"),
                "delta_success_rate": (
                    a["success_rate"] - b["success_rate"]
                    if a.get("success_rate") is not None
                    and b.get("success_rate") is not None
                    else None
                ),
                "before_runtime_failures": b.get("runtime_failure_count", 0),
                "after_runtime_failures": a.get("runtime_failure_count", 0),
            }
        )

    def ability_delta_rows(
        before_rows: list[dict[str, Any]],
        after_rows: list[dict[str, Any]],
        mode: str,
    ) -> list[dict[str, Any]]:
        bmap = {x["ability"]: x for x in before_rows}
        amap = {x["ability"]: x for x in after_rows}
        output = []
        for ability in sorted(set(bmap) | set(amap)):
            b = bmap.get(ability, {})
            a = amap.get(ability, {})
            output.append(
                {
                    "ability": ability,
                    "aggregation_mode": mode,
                    "before_mean_ds": b.get("mean_driving_score"),
                    "after_mean_ds": a.get("mean_driving_score"),
                    "delta_mean_ds": (
                        a["mean_driving_score"] - b["mean_driving_score"]
                        if a.get("mean_driving_score") is not None
                        and b.get("mean_driving_score") is not None
                        else None
                    ),
                    "before_success_rate": b.get("success_rate"),
                    "after_success_rate": a.get("success_rate"),
                    "delta_success_rate": (
                        a["success_rate"] - b["success_rate"]
                        if a.get("success_rate") is not None
                        and b.get("success_rate") is not None
                        else None
                    ),
                }
            )
        return output

    output_dir = paths["output_dir"]
    write_csv(
        output_dir / "scenario_before_after.csv",
        scenario_delta,
    )
    write_csv(
        output_dir / "ability_before_after_table2.csv",
        ability_delta_rows(
            before["table2_ability_summaries"],
            after["table2_ability_summaries"],
            "table2",
        ),
    )
    write_csv(
        output_dir / "ability_before_after_target.csv",
        ability_delta_rows(
            before["target_ability_summaries"],
            after["target_ability_summaries"],
            "target",
        ),
    )
    print("[compare] before/after CSV files written")


def download_stage(
    config: dict[str, Any],
    config_dir: Path,
) -> None:
    paths = config_paths(config, config_dir)
    hf_cfg = config.get("huggingface", {})
    if not hf_cfg.get("enabled", False):
        print("[download] disabled")
        return

    repo_id = str(hf_cfg.get("repo_id", "")).strip()
    if not repo_id:
        raise ValueError(
            "huggingface.enabled=true이면 repo_id가 필요합니다."
        )
    remote_prefix = str(hf_cfg.get("remote_prefix", "")).strip().strip("/")
    selected_path = paths["output_dir"] / "selected_additional_files.txt"
    if not selected_path.exists():
        raise FileNotFoundError(
            "selected_additional_files.txt가 없습니다. select를 먼저 실행하십시오."
        )

    try:
        from huggingface_hub import hf_hub_download
    except ImportError as exc:
        raise RuntimeError(
            "pip install huggingface_hub를 먼저 실행하십시오."
        ) from exc

    paths["download_dir"].mkdir(parents=True, exist_ok=True)
    filenames = [
        x.strip()
        for x in selected_path.read_text(encoding="utf-8").splitlines()
        if x.strip()
    ]
    for index, filename in enumerate(filenames, 1):
        remote_name = (
            f"{remote_prefix}/{filename}" if remote_prefix else filename
        )
        print(f"[download] {index}/{len(filenames)} {remote_name}")
        hf_hub_download(
            repo_id=repo_id,
            repo_type="dataset",
            filename=remote_name,
            local_dir=str(paths["download_dir"]),
        )


def run_external(
    command: str,
    *,
    cwd: Path,
    env_updates: dict[str, str],
) -> None:
    if not command.strip():
        return
    env = os.environ.copy()
    env.update(env_updates)
    print(f"[external] {command}")
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        env=env,
        shell=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"외부 명령 실패(returncode={completed.returncode}): {command}"
        )


def run_all(
    config: dict[str, Any],
    config_dir: Path,
) -> None:
    paths = config_paths(config, config_dir)
    commands = config.get("commands", {})
    env = {
        "B2D_CONFIG_DIR": str(config_dir),
        "B2D_OUTPUT_DIR": str(paths["output_dir"]),
    }

    run_external(
        str(commands.get("train_base", "")),
        cwd=config_dir,
        env_updates=env,
    )
    run_external(
        str(commands.get("evaluate_base", "")),
        cwd=config_dir,
        env_updates=env,
    )

    analyzed = analyze_stage(config, config_dir)
    selected = select_stage(config, config_dir, analyzed)
    built = build_stage(config, config_dir, selected)
    download_stage(config, config_dir)

    env.update(
        {
            "B2D_SELECTED_MANIFEST": str(
                paths["output_dir"] / "selected_additional_manifest.json"
            ),
            "B2D_SELECTED_FILE_LIST": str(
                paths["output_dir"] / "selected_additional_files.txt"
            ),
            "B2D_COMBINED_MANIFEST": str(
                paths["output_dir"] / "combined_unique_manifest.json"
            ),
            "B2D_TRAIN_FILE_LIST": str(
                paths["output_dir"] / "train_files_mixed.txt"
            ),
            "B2D_TRAIN_SAMPLING_PLAN": str(
                paths["output_dir"] / "train_sampling_plan.csv"
            ),
            "B2D_RUNTIME_RERUN_LIST": str(
                paths["output_dir"] / "runtime_rerun_routes.csv"
            ),
        }
    )

    run_external(
        str(commands.get("fine_tune", "")),
        cwd=config_dir,
        env_updates=env,
    )
    run_external(
        str(commands.get("evaluate_finetuned", "")),
        cwd=config_dir,
        env_updates=env,
    )

    if paths["evaluation_results_finetuned"]:
        compare_stage(config, config_dir)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bench2Drive native result.json Data Flywheel"
    )
    parser.add_argument(
        "command",
        choices=[
            "base-distribution",
            "validate",
            "analyze",
            "select",
            "build",
            "download",
            "compare",
            "run-all",
        ],
    )
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()

    try:
        config, config_dir = load_config(args.config)

        if args.command == "base-distribution":
            base_distribution_stage(config, config_dir)
        elif args.command == "validate":
            validate_stage(config, config_dir)
        elif args.command == "analyze":
            analyze_stage(config, config_dir)
        elif args.command == "select":
            select_stage(config, config_dir)
        elif args.command == "build":
            build_stage(config, config_dir)
        elif args.command == "download":
            download_stage(config, config_dir)
        elif args.command == "compare":
            compare_stage(config, config_dir)
        elif args.command == "run-all":
            run_all(config, config_dir)
        return 0
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
