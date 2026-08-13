#!/usr/bin/env python3
"""Run traffic-light correction, visualization, videos, and CSV reporting."""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import cv2

from apply_visualize_compare_from_summary import (
    bbox_view_dirs,
    frame_sort_key,
    image_map,
    make_comparison_video,
)
from production_contract import (
    ManifestSelection,
    annotation_digest,
    build_completion,
    canonical_sha256,
    completion_errors,
    implementation_hash,
    load_approvals,
    load_manifest,
    matching_approval,
    read_completion,
    validate_source_inventory,
    write_completion,
)
from traffic_light_relevance import RelevanceConfig, correct_affects_ego


SCRIPT_DIR = Path(__file__).resolve().parent
FIX_SCRIPT = SCRIPT_DIR / "fix_tl_bbox_permutation.py"
VISUALIZE_SCRIPT = SCRIPT_DIR / "visualize.py"
ROUTE_RE = re.compile(r"_Route(\d+)_")
TOWN_RE = re.compile(r"_Town([^_]+?)_Route")

RESULT_FIELDS = [
    "component",
    "scenario",
    "input_anno",
    "output_anno",
    "annotation_frames",
    "bbox_changed_frames",
    "bbox_reassigned_entries",
    "crossing_events",
    "keep_frames",
    "auto_fix_frames",
    "review_frames",
    "affects_ego_changed_frames",
    "affects_ego_changed_entries",
    "visualized_frames",
    "vector_map_frames",
    "after_front_video",
    "after_bev_video",
    "vector_map_video",
    "comparison_created",
    "comparison_front_video",
    "comparison_bev_video",
    "detail_csv",
    "summary_csv",
    "elapsed_seconds",
    "status",
    "completion_sha256",
    "approved",
    "error",
]

REVIEW_FIELDS = [
    "component",
    "scenario",
    "status",
    "review_frames",
    "completion_sha256",
    "approved",
    "approved_by",
    "approval_reason",
    "error",
]

COMPLETION_METRIC_FIELDS = (
    "annotation_frames",
    "bbox_changed_frames",
    "bbox_reassigned_entries",
    "crossing_events",
    "keep_frames",
    "auto_fix_frames",
    "review_frames",
    "affects_ego_changed_frames",
    "affects_ego_changed_entries",
)


def is_annotation_file(path: Path) -> bool:
    return path.is_file() and (
        path.name.endswith(".json") or path.name.endswith(".json.gz")
    )


def annotation_files(anno_dir: Path) -> list[Path]:
    return sorted(path for path in anno_dir.iterdir() if is_annotation_file(path))


def discover_scenarios(input_path: Path) -> list[tuple[str, Path, Path]]:
    """Return (scenario name, scenario directory, annotation directory)."""
    if input_path.name == "anno" and annotation_files(input_path):
        return [(input_path.parent.name, input_path.parent, input_path)]

    direct_anno = input_path / "anno"
    if direct_anno.is_dir() and annotation_files(direct_anno):
        return [(input_path.name, input_path, direct_anno)]

    discovered = []
    names = set()
    for anno_dir in sorted(input_path.rglob("anno")):
        if not anno_dir.is_dir() or not annotation_files(anno_dir):
            continue
        scenario_dir = anno_dir.parent
        scenario = scenario_dir.name
        if scenario in names:
            raise ValueError(f"중복 시나리오 이름이 있습니다: {scenario}")
        names.add(scenario)
        discovered.append((scenario, scenario_dir, anno_dir))
    return discovered


def discover_manifest_scenarios(
    input_path: Path, clip_names: Sequence[str]
) -> list[tuple[str, Path, Path]]:
    """Resolve a validated manifest against a flat Bench2Drive clip root."""
    scenarios = []
    for scenario in clip_names:
        scenario_dir = input_path / scenario
        anno_dir = scenario_dir / "anno"
        frames = annotation_files(anno_dir) if anno_dir.is_dir() else []
        if not frames:
            raise FileNotFoundError(
                f"manifest clip annotation이 없거나 비어 있습니다: {anno_dir}"
            )
        scenarios.append((scenario, scenario_dir, anno_dir))
    return scenarios


def write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=RESULT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def read_results_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open("r", newline="", encoding="utf-8-sig") as file:
        return list(csv.DictReader(file))


def write_review_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=REVIEW_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def run_command(command: list[str], label: str) -> None:
    print(f"    [{label}] {' '.join(command)}", flush=True)
    result = subprocess.run(command, cwd=SCRIPT_DIR)
    if result.returncode != 0:
        raise RuntimeError(f"{label} 실패 (exit={result.returncode})")


def read_fix_summary(path: Path, clip_key: str | None = None) -> dict[str, int]:
    with path.open("r", newline="", encoding="utf-8-sig") as file:
        rows = list(csv.DictReader(file))
    if clip_key is not None:
        rows = [row for row in rows if row.get("clip") == clip_key]
    if not rows:
        raise ValueError(f"수정 summary가 비어 있습니다: {path}")

    def total(column: str) -> int:
        try:
            return sum(int(float(row.get(column) or 0)) for row in rows)
        except ValueError as error:
            raise ValueError(f"summary 숫자 컬럼 오류: {column}") from error

    return {
        "bbox_changed_frames": total("bbox_changed_frames"),
        "bbox_reassigned_entries": total("action_reassigned"),
        "affects_ego_changed_frames": total("affects_ego_changed_frames"),
        "affects_ego_changed_entries": total("affects_ego_changed_entries"),
    }


def run_bbox_fix(
    input_root: Path,
    bbox_output_root: Path,
    detail_csv: Path,
    clip_list_path: Path | None = None,
) -> Path:
    """Repair all selected clips in one pass so consensus is dataset-wide."""
    command = [
        sys.executable,
        str(FIX_SCRIPT),
        "--root",
        str(input_root),
        "--out",
        str(bbox_output_root),
        "--csv",
        str(detail_csv),
        "--bbox-only",
    ]
    if clip_list_path is not None:
        command.extend(["--clip-list", str(clip_list_path)])
    run_command(command, "BBOX")
    summary_csv = detail_csv.with_name(f"{detail_csv.stem}_summary.csv")
    if not summary_csv.is_file():
        raise FileNotFoundError(f"bbox summary가 생성되지 않았습니다: {summary_csv}")
    return summary_csv


def run_visualization(
    scenario_dir: Path,
    anno_dir: Path,
    output_dir: Path,
    start_frame: int | None,
    max_frames: int | None,
    map_path: Path | None = None,
) -> None:
    command = [
        sys.executable,
        str(VISUALIZE_SCRIPT),
        "--input",
        str(scenario_dir),
        "--anno-dir",
        str(anno_dir),
        "--output-dir",
        str(output_dir),
        "--profile",
        "camera-bev-map" if map_path is not None else "camera-bev",
    ]
    if map_path is not None:
        command.extend(["--map-file", str(map_path)])
    if start_frame is not None:
        command.extend(["--start-frame", str(start_frame)])
    if max_frames is not None:
        command.extend(["--max-frames", str(max_frames)])
    run_command(command, "VIS")


def make_video(frame_dir: Path, output_path: Path, fps: float) -> int:
    frames = image_map(frame_dir)
    ordered = sorted(frames, key=frame_sort_key)
    first = None
    for stem in ordered:
        first = cv2.imread(str(frames[stem]))
        if first is not None:
            break
    if first is None:
        raise FileNotFoundError(f"영상으로 만들 이미지가 없습니다: {frame_dir}")

    height, width = first.shape[:2]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(
        str(output_path),
        cv2.VideoWriter_fourcc(*"mp4v"),
        fps,
        (width, height),
    )
    if not writer.isOpened():
        raise RuntimeError(f"VideoWriter 열기 실패: {output_path}")

    written = 0
    try:
        for stem in ordered:
            image = cv2.imread(str(frames[stem]))
            if image is None:
                continue
            if image.shape[1] != width or image.shape[0] != height:
                image = cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)
            writer.write(image)
            written += 1
    finally:
        writer.release()
    if written == 0:
        raise RuntimeError(f"영상에 기록된 프레임이 없습니다: {output_path}")
    return written


def empty_result(
    scenario: str,
    anno_dir: Path,
    clip_output: Path,
    bbox_detail_csv: Path | None = None,
    bbox_summary_csv: Path | None = None,
    component: str = "all",
) -> dict[str, Any]:
    return {
        "component": component,
        "scenario": scenario,
        "input_anno": str(anno_dir),
        "output_anno": str(clip_output / "traffic_light" / "corrected_anno"),
        "annotation_frames": len(annotation_files(anno_dir)),
        "bbox_changed_frames": 0,
        "bbox_reassigned_entries": 0,
        "crossing_events": 0,
        "keep_frames": 0,
        "auto_fix_frames": 0,
        "review_frames": 0,
        "affects_ego_changed_frames": 0,
        "affects_ego_changed_entries": 0,
        "visualized_frames": 0,
        "vector_map_frames": 0,
        "after_front_video": "",
        "after_bev_video": "",
        "vector_map_video": "",
        "comparison_created": "false",
        "comparison_front_video": "",
        "comparison_bev_video": "",
        "detail_csv": str(bbox_detail_csv or ""),
        "summary_csv": str(bbox_summary_csv or ""),
        "elapsed_seconds": "",
        "status": "failed",
        "completion_sha256": "",
        "approved": "false",
        "error": "",
    }


def process_scenario(
    scenario: str,
    scenario_dir: Path,
    anno_dir: Path,
    bbox_anno_dir: Path,
    bbox_clip_key: str,
    bbox_detail_csv: Path,
    bbox_summary_csv: Path,
    output_root: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    started = time.perf_counter()
    clip_output = output_root / scenario
    if clip_output.exists():
        shutil.rmtree(clip_output)
    clip_output.mkdir(parents=True)
    result = empty_result(
        scenario,
        anno_dir,
        clip_output,
        bbox_detail_csv,
        bbox_summary_csv,
        args.component,
    )

    print("  2/5 trigger-volume relevance 판정", flush=True)
    relevance_config = RelevanceConfig(
        approach_distance_metres=args.approach_distance,
        maximum_step_metres=args.max_step,
        trigger_margin_metres=args.trigger_margin,
        maximum_heading_error_degrees=args.maximum_heading_error,
        simultaneous_crossing_frames=args.simultaneous_crossing_frames,
        minimum_temporal_run_frames=args.minimum_temporal_run_frames,
    )
    traffic_output = clip_output / "traffic_light"
    output_anno = traffic_output / "corrected_anno"
    report_dir = traffic_output / "reports"
    bbox_metrics = read_fix_summary(bbox_summary_csv, bbox_clip_key)
    relevance_metrics = correct_affects_ego(
        scenario,
        bbox_anno_dir,
        output_anno,
        report_dir,
        bbox_detail_csv=bbox_detail_csv,
        bbox_clip_key=bbox_clip_key,
        config=relevance_config,
    )
    metrics = {**bbox_metrics, **relevance_metrics}
    if not output_anno.is_dir():
        raise FileNotFoundError(f"수정 annotation이 생성되지 않았습니다: {output_anno}")
    result.update(metrics)
    result["output_anno"] = str(output_anno)

    if args.visualization:
        map_path = None
        if args.vector_map:
            town_match = TOWN_RE.search(scenario)
            if town_match is None:
                raise ValueError(f"시나리오 이름에서 Town을 찾지 못했습니다: {scenario}")
            map_path = args.map_root / f"Town{town_match.group(1)}_HD_map.npz"
            if not map_path.is_file():
                raise FileNotFoundError(f"HD vector map을 찾지 못했습니다: {map_path}")
        after_visualization = clip_output / "visualization" / "after"
        print("  3/5 수정 annotation 시각화", flush=True)
        run_visualization(
            scenario_dir,
            output_anno,
            after_visualization,
            args.start_frame,
            args.max_frames,
            map_path,
        )
        after_views = bbox_view_dirs(after_visualization)
        vector_map_dir = after_visualization / "camera" / "rgb_front_landmark"
        result["visualized_frames"] = len(image_map(after_views["camera"]))
        result["vector_map_frames"] = len(image_map(vector_map_dir))

        if args.video:
            print("  4/5 수정 결과 영상 생성", flush=True)
            after_front = clip_output / "videos" / "after_front.mp4"
            after_bev = clip_output / "videos" / "after_bev.mp4"
            make_video(after_views["camera"], after_front, args.fps)
            make_video(after_views["bev"], after_bev, args.fps)
            result["after_front_video"] = str(after_front)
            result["after_bev_video"] = str(after_bev)
            if args.vector_map:
                vector_map_video = clip_output / "videos" / "vector_map.mp4"
                make_video(vector_map_dir, vector_map_video, args.fps)
                result["vector_map_video"] = str(vector_map_video)
        else:
            print("  4/5 영상 생성 생략 (--no-video)")

        has_annotation_changes = (
            metrics["bbox_changed_frames"] > 0
            or metrics["affects_ego_changed_frames"] > 0
        )
        if args.video and has_annotation_changes:
            print("  5/5 annotation 변경 감지: BEFORE/AFTER 비교 영상 생성",
                  flush=True)
            before_visualization = clip_output / "visualization" / "before"
            run_visualization(
                scenario_dir,
                anno_dir,
                before_visualization,
                args.start_frame,
                args.max_frames,
                None,
            )
            before_views = bbox_view_dirs(before_visualization)
            route_match = ROUTE_RE.search(scenario)
            route = route_match.group(1) if route_match else scenario
            compare_front = clip_output / "videos" / "before_after_front.mp4"
            compare_bev = clip_output / "videos" / "before_after_bev.mp4"
            make_comparison_video(
                before_views["camera"], after_views["camera"], compare_front,
                route, "FRONT CAMERA", args.fps, args.scale)
            make_comparison_video(
                before_views["bev"], after_views["bev"], compare_bev,
                route, "CAMERA BEV", args.fps, args.scale)
            result["comparison_created"] = "true"
            result["comparison_front_video"] = str(compare_front)
            result["comparison_bev_video"] = str(compare_bev)
        else:
            reason = "bbox/affects_ego 변경 없음"
            if not args.video:
                reason = "--no-video"
            print(f"  5/5 BEFORE/AFTER 비교 영상 생략 ({reason})")
    else:
        print("  3/5 시각화 생략 (--no-visualization)")
        print("  4/5 영상 생성 생략")
        print("  5/5 비교 영상 생성 생략")

    result["elapsed_seconds"] = f"{time.perf_counter() - started:.2f}"
    result["status"] = "review" if result["review_frames"] else "completed"
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "--input의 시나리오를 traffic-light annotation 수정, 카메라/BEV "
            "시각화, 영상, 결과 CSV까지 한 번에 처리합니다."
        )
    )
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="시나리오 폴더, anno 폴더 또는 여러 시나리오가 있는 상위 폴더",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="anno, visualization, videos, results.csv를 저장할 경로",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help="production train/val split JSON (flat clip 이름 계약)",
    )
    parser.add_argument(
        "--component",
        choices=("base", "weak", "all"),
        default="all",
        help="manifest의 처리 component (기본값: all)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="hash가 모두 맞는 completed clip만 건너뜀",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="annotation과 output을 수정하지 않고 production 완결성만 검사",
    )
    parser.add_argument(
        "--review-approvals",
        type=Path,
        help="completion SHA256에 묶인 사람 승인 JSON",
    )
    parser.add_argument(
        "--visualization",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="전방 카메라와 카메라 BEV bbox 시각화 생성 (기본값: true)",
    )
    parser.add_argument(
        "--video",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="시각화 MP4 생성 (기본값: true)",
    )
    parser.add_argument(
        "--vector-map",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Town HD vector map 이미지와 MP4 생성 (기본값: true)",
    )
    parser.add_argument(
        "--map-root",
        type=Path,
        default=SCRIPT_DIR / "maps",
        help="Town*_HD_map.npz가 있는 폴더 (기본값: ./maps)",
    )
    parser.add_argument("--fps", type=float, default=10.0)
    parser.add_argument("--scale", type=float, default=0.75,
                        help="BEFORE/AFTER 비교 영상 배율")
    parser.add_argument("--start-frame", type=int)
    parser.add_argument("--max-frames", type=int)
    parser.add_argument(
        "--approach-distance",
        type=float,
        default=60.0,
        help="trigger 통과 전 affects_ego 구간 거리 m (기본값: 60)",
    )
    parser.add_argument(
        "--max-step",
        type=float,
        default=5.0,
        help="연속 궤적으로 인정할 최대 프레임 이동 거리 m (기본값: 5)",
    )
    parser.add_argument(
        "--trigger-margin",
        type=float,
        default=0.0,
        help="선분-trigger 교차 판정 여유 거리 m (기본값: 0.0, 실제 volume)",
    )
    parser.add_argument(
        "--maximum-heading-error",
        type=float,
        default=35.0,
        help="AUTO_FIX에 허용할 최대 궤적 방향 오차 degree (기본값: 35)",
    )
    parser.add_argument(
        "--simultaneous-crossing-frames",
        type=int,
        default=3,
        help="서로 다른 trigger 통과를 동시 후보로 볼 프레임 차이 (기본값: 3)",
    )
    parser.add_argument(
        "--minimum-temporal-run-frames",
        type=int,
        default=3,
        help="AUTO_FIX 동일 ID 최소 연속 프레임 수 (기본값: 3)",
    )
    return parser


def production_hashes(args: argparse.Namespace) -> tuple[str, str]:
    """Return hashes that invalidate stale completion records."""
    implementation_sha256 = implementation_hash(
        [
            Path(__file__),
            FIX_SCRIPT,
            SCRIPT_DIR / "traffic_light_relevance.py",
            SCRIPT_DIR / "production_contract.py",
        ]
    )
    config = {
        "schema_version": 1,
        "bbox_mode": "bbox_only_then_trigger_relevance",
        "approach_distance_metres": args.approach_distance,
        "maximum_step_metres": args.max_step,
        "trigger_margin_metres": args.trigger_margin,
        "maximum_heading_error_degrees": args.maximum_heading_error,
        "simultaneous_crossing_frames": args.simultaneous_crossing_frames,
        "minimum_temporal_run_frames": args.minimum_temporal_run_frames,
        "visualization": args.visualization,
        "video": args.video,
        "vector_map": args.vector_map,
        "fps": args.fps,
        "comparison_scale": args.scale,
        "visualization_start_frame": args.start_frame,
        "visualization_max_frames": args.max_frames,
    }
    return canonical_sha256(config), implementation_sha256


def completion_path(output_root: Path, clip: str) -> Path:
    return output_root / clip / "traffic_light" / "completion.json"


def validate_existing_completion(
    *,
    selection: ManifestSelection,
    scenario: str,
    anno_dir: Path,
    output_root: Path,
    config_sha256: str,
    implementation_sha256: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    path = completion_path(output_root, scenario)
    try:
        input_sha256, input_frames = annotation_digest(anno_dir)
        output_sha256, output_frames = annotation_digest(
            output_root / scenario / "traffic_light" / "corrected_anno"
        )
        completion = read_completion(path)
    except (OSError, ValueError) as error:
        return None, [f"{type(error).__name__}: {error}"]
    errors = completion_errors(
        completion,
        clip=scenario,
        component=selection.component,
        manifest_sha256=selection.sha256,
        config_sha256=config_sha256,
        implementation_sha256=implementation_sha256,
        input_annotation_sha256=input_sha256,
        input_frames=input_frames,
        output_annotation_sha256=output_sha256,
        output_frames=output_frames,
    )
    return completion, errors


def result_from_completion(
    completion: Mapping[str, Any],
    scenario: str,
    anno_dir: Path,
    output_root: Path,
) -> dict[str, Any]:
    result = empty_result(
        scenario,
        anno_dir,
        output_root / scenario,
        component=str(completion["component"]),
    )
    metrics = completion.get("metrics")
    if isinstance(metrics, dict):
        result.update({key: value for key, value in metrics.items() if key in result})
    result["component"] = str(completion["component"])
    result["scenario"] = scenario
    result["input_anno"] = str(anno_dir)
    result["output_anno"] = str(
        output_root / scenario / "traffic_light" / "corrected_anno"
    )
    bbox_report_dir = (
        output_root / "production_reports" / str(completion["component"])
    )
    result["detail_csv"] = str(bbox_report_dir / "bbox_details.csv")
    result["summary_csv"] = str(bbox_report_dir / "bbox_details_summary.csv")
    result["status"] = str(completion["status"])
    result["completion_sha256"] = str(completion["completion_sha256"])
    result["approved"] = "false"
    return result


def finalize_completion(
    *,
    result: dict[str, Any],
    selection: ManifestSelection,
    anno_dir: Path,
    output_root: Path,
    config_sha256: str,
    implementation_sha256: str,
) -> dict[str, Any]:
    input_sha256, input_frames = annotation_digest(anno_dir)
    output_dir = output_root / result["scenario"] / "traffic_light" / "corrected_anno"
    output_sha256: str | None = None
    output_frames: tuple[str, ...] = ()
    try:
        output_sha256, output_frames = annotation_digest(output_dir)
    except (OSError, ValueError):
        if result["status"] != "failed":
            result["status"] = "failed"
            result["error"] = f"corrected annotation이 없거나 비어 있습니다: {output_dir}"

    if tuple(input_frames) != tuple(output_frames):
        result["status"] = "failed"
        mismatch = (
            "corrected frame 집합 불일치: "
            f"input={len(input_frames)}, output={len(output_frames)}"
        )
        result["error"] = f"{result['error']}; {mismatch}".strip("; ")

    metrics = {key: result[key] for key in COMPLETION_METRIC_FIELDS}
    completion = build_completion(
        clip=str(result["scenario"]),
        component=selection.component,
        manifest_sha256=selection.sha256,
        config_sha256=config_sha256,
        implementation_sha256=implementation_sha256,
        input_annotation_sha256=input_sha256,
        output_annotation_sha256=output_sha256,
        input_frames=input_frames,
        output_frames=output_frames,
        status=str(result["status"]),
        metrics=metrics,
        error=str(result["error"]),
    )
    write_completion(completion_path(output_root, str(result["scenario"])), completion)
    result["completion_sha256"] = completion["completion_sha256"]
    return completion


def update_production_reports(
    output_root: Path,
    approvals: Mapping[tuple[str, str, str], Any],
) -> None:
    report_root = output_root / "production_reports"
    rows: list[dict[str, Any]] = []
    for component in ("base", "weak", "all"):
        rows.extend(read_results_csv(report_root / component / "results.csv"))
    rows.sort(key=lambda row: (str(row.get("component", "")), row["scenario"]))

    review_rows = []
    for row in rows:
        if row.get("status") not in {"review", "failed"}:
            continue
        try:
            completion = read_completion(
                completion_path(output_root, str(row["scenario"]))
            )
        except (OSError, ValueError):
            completion = {
                "component": row.get("component", ""),
                "clip": row["scenario"],
                "completion_sha256": row.get("completion_sha256", ""),
            }
        approval = matching_approval(approvals, completion)
        row["approved"] = "true" if approval else "false"
        review_rows.append(
            {
                "component": row.get("component", ""),
                "scenario": row["scenario"],
                "status": row.get("status", ""),
                "review_frames": row.get("review_frames", ""),
                "completion_sha256": completion.get("completion_sha256", ""),
                "approved": "true" if approval else "false",
                "approved_by": approval.approved_by if approval else "",
                "approval_reason": approval.reason if approval else "",
                "error": row.get("error", ""),
            }
        )
    write_csv(report_root / "aggregate_results.csv", rows)
    write_review_csv(report_root / "review_queue.csv", review_rows)


def check_production_outputs(
    *,
    selection: ManifestSelection,
    scenarios: Sequence[tuple[str, Path, Path]],
    output_root: Path,
    config_sha256: str,
    implementation_sha256: str,
    approvals: Mapping[tuple[str, str, str], Any],
) -> list[str]:
    errors: list[str] = []
    valid_completions: dict[str, Mapping[str, Any]] = {}
    output_clips = {
        child.name
        for child in output_root.iterdir()
        if child.is_dir() and (child / "traffic_light").exists()
    } if output_root.is_dir() else set()
    unexpected = sorted(output_clips - set(selection.all_manifest_clips))
    if unexpected:
        errors.append(f"manifest 밖 output clip: {unexpected[:5]} ({len(unexpected)})")

    for scenario, _, anno_dir in scenarios:
        completion, completion_issues = validate_existing_completion(
            selection=selection,
            scenario=scenario,
            anno_dir=anno_dir,
            output_root=output_root,
            config_sha256=config_sha256,
            implementation_sha256=implementation_sha256,
        )
        if completion_issues:
            errors.append(f"{scenario}: {'; '.join(completion_issues)}")
            continue
        assert completion is not None
        valid_completions[scenario] = completion
        status = completion.get("status")
        if status == "completed":
            continue
        if status == "review" and matching_approval(approvals, completion):
            continue
        errors.append(f"{scenario}: status={status}, 유효한 사람 승인 없음")

    aggregate_path = output_root / "production_reports" / "aggregate_results.csv"
    aggregate_rows = read_results_csv(aggregate_path)
    aggregate_names = [row.get("scenario", "") for row in aggregate_rows]
    duplicates = sorted(
        {name for name in aggregate_names if name and aggregate_names.count(name) > 1}
    )
    missing = sorted(set(selection.all_manifest_clips) - set(aggregate_names))
    extra = sorted(set(aggregate_names) - set(selection.all_manifest_clips))
    if duplicates or missing or extra:
        errors.append(
            "aggregate_results.csv inventory 불일치: "
            f"duplicates={duplicates[:5]} ({len(duplicates)}), "
            f"missing={missing[:5]} ({len(missing)}), "
            f"unexpected={extra[:5]} ({len(extra)})"
        )
    aggregate_by_name = {
        row.get("scenario", ""): row for row in aggregate_rows if row.get("scenario")
    }
    for scenario, completion in valid_completions.items():
        row = aggregate_by_name.get(scenario)
        if row is None:
            continue
        for field, expected in (
            ("component", completion.get("component")),
            ("status", completion.get("status")),
            ("completion_sha256", completion.get("completion_sha256")),
        ):
            if row.get(field) != str(expected):
                errors.append(
                    f"aggregate row stale for {scenario}: {field} "
                    f"expected={expected!r}, actual={row.get(field)!r}"
                )

    component_path = (
        output_root
        / "production_reports"
        / selection.component
        / "results.csv"
    )
    component_rows = read_results_csv(component_path)
    component_names = [row.get("scenario", "") for row in component_rows]
    component_duplicates = sorted(
        {
            name
            for name in component_names
            if name and component_names.count(name) > 1
        }
    )
    component_missing = sorted(set(selection.clips) - set(component_names))
    component_extra = sorted(set(component_names) - set(selection.clips))
    if component_duplicates or component_missing or component_extra:
        errors.append(
            f"{component_path} inventory 불일치: "
            f"duplicates={component_duplicates[:5]} ({len(component_duplicates)}), "
            f"missing={component_missing[:5]} ({len(component_missing)}), "
            f"unexpected={component_extra[:5]} ({len(component_extra)})"
        )
    return errors


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    input_path = args.input.expanduser().resolve()
    output_root = args.output.expanduser().resolve()
    args.map_root = args.map_root.expanduser().resolve()

    if args.manifest is not None:
        args.manifest = args.manifest.expanduser().resolve()
    if args.review_approvals is not None:
        args.review_approvals = args.review_approvals.expanduser().resolve()

    if not input_path.is_dir():
        parser.error(f"입력 폴더가 없습니다: {input_path}")
    if (
        output_root == input_path
        or output_root in input_path.parents
        or input_path in output_root.parents
    ):
        parser.error(f"출력은 입력 폴더 바깥에 지정해야 합니다: {output_root}")
    if args.video and not args.visualization:
        parser.error("--video는 --visualization과 함께만 사용할 수 있습니다")
    if args.fps <= 0:
        parser.error("--fps는 0보다 커야 합니다")
    if args.scale <= 0:
        parser.error("--scale은 0보다 커야 합니다")
    if args.start_frame is not None and args.start_frame < 0:
        parser.error("--start-frame은 0 이상이어야 합니다")
    if args.max_frames is not None and args.max_frames < 1:
        parser.error("--max-frames는 1 이상이어야 합니다")
    if args.approach_distance <= 0:
        parser.error("--approach-distance는 0보다 커야 합니다")
    if args.max_step <= 0:
        parser.error("--max-step은 0보다 커야 합니다")
    if args.trigger_margin < 0:
        parser.error("--trigger-margin은 0 이상이어야 합니다")
    if not 0 <= args.maximum_heading_error <= 180:
        parser.error("--maximum-heading-error는 0~180 범위여야 합니다")
    if args.simultaneous_crossing_frames < 0:
        parser.error("--simultaneous-crossing-frames는 0 이상이어야 합니다")
    if args.minimum_temporal_run_frames < 1:
        parser.error("--minimum-temporal-run-frames는 1 이상이어야 합니다")
    if args.manifest is None and (
        args.component != "all"
        or args.resume
        or args.check_only
        or args.review_approvals is not None
    ):
        parser.error(
            "--component/--resume/--check-only/--review-approvals는 "
            "--manifest와 함께 사용해야 합니다"
        )
    if args.check_only and args.resume:
        parser.error("--check-only와 --resume은 동시에 사용할 수 없습니다")

    selection: ManifestSelection | None = None
    try:
        approvals = load_approvals(args.review_approvals)
        if args.manifest is not None:
            selection = load_manifest(args.manifest, args.component)
            validate_source_inventory(
                input_path,
                selection.clips,
                selection.allowed_source_extras,
            )
            scenarios = discover_manifest_scenarios(input_path, selection.clips)
        else:
            scenarios = discover_scenarios(input_path)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if not scenarios:
        parser.error(f"annotation이 있는 시나리오를 찾지 못했습니다: {input_path}")

    config_sha256, implementation_sha256 = production_hashes(args)
    if args.check_only:
        assert selection is not None
        errors = check_production_outputs(
            selection=selection,
            scenarios=scenarios,
            output_root=output_root,
            config_sha256=config_sha256,
            implementation_sha256=implementation_sha256,
            approvals=approvals,
        )
        if errors:
            print("production check failed:", file=sys.stderr)
            for error in errors:
                print(f"  - {error}", file=sys.stderr)
            return 1
        print(
            f"production check passed: component={selection.component}, "
            f"clips={len(selection.clips)}"
        )
        return 0

    output_root.mkdir(parents=True, exist_ok=True)
    print(f"입력: {input_path}")
    print(f"출력: {output_root}")
    if selection is not None:
        print(
            f"manifest: {selection.path} ({selection.sha256})\n"
            f"component: {selection.component}"
        )
    print(f"시나리오: {len(scenarios)}개\n", flush=True)

    results: list[dict[str, Any]] = []
    if selection is not None:
        component_report_dir = (
            output_root / "production_reports" / selection.component
        )
        results_csv = component_report_dir / "results.csv"
        bbox_report_dir = component_report_dir
    else:
        results_csv = output_root / "results.csv"
        bbox_report_dir = output_root / "bbox_reports"
    bbox_detail_csv = bbox_report_dir / "bbox_details.csv"
    with tempfile.TemporaryDirectory(prefix="b2d_bbox_") as directory:
        bbox_output_root = Path(directory)
        clip_list_path = None
        if selection is not None:
            clip_list_path = bbox_output_root / "selected_clips.txt"
            clip_list_path.write_text(
                "".join(f"{clip}\n" for clip in selection.clips),
                encoding="utf-8",
            )
        print("1/5 선택 clip 전체 bbox permutation 복구", flush=True)
        bbox_summary_csv = run_bbox_fix(
            input_path,
            bbox_output_root,
            bbox_detail_csv,
            clip_list_path,
        )
        for index, (scenario, scenario_dir, anno_dir) in enumerate(scenarios, start=1):
            print(f"[{index}/{len(scenarios)}] {scenario}", flush=True)
            clip_output = output_root / scenario
            relative_anno = anno_dir.relative_to(input_path)
            bbox_clip_key = relative_anno.as_posix() or "."
            bbox_anno_dir = bbox_output_root / relative_anno

            if selection is not None and args.resume:
                completion, issues = validate_existing_completion(
                    selection=selection,
                    scenario=scenario,
                    anno_dir=anno_dir,
                    output_root=output_root,
                    config_sha256=config_sha256,
                    implementation_sha256=implementation_sha256,
                )
                if (
                    completion is not None
                    and not issues
                    and completion.get("status") == "completed"
                ):
                    result = result_from_completion(
                        completion, scenario, anno_dir, output_root
                    )
                    print("  건너뜀: completion/input/output hash 일치\n")
                    results.append(result)
                    write_csv(results_csv, results)
                    continue

            try:
                result = process_scenario(
                    scenario,
                    scenario_dir,
                    anno_dir,
                    bbox_anno_dir,
                    bbox_clip_key,
                    bbox_detail_csv,
                    bbox_summary_csv,
                    output_root,
                    args,
                )
                print(
                    "  완료: bbox_frames={bbox}, affects_frames={affects}, "
                    "comparison={comparison}\n".format(
                        bbox=result["bbox_changed_frames"],
                        affects=result["affects_ego_changed_frames"],
                        comparison=result["comparison_created"],
                    )
                )
            except Exception as error:
                result = empty_result(
                    scenario,
                    anno_dir,
                    clip_output,
                    bbox_detail_csv,
                    bbox_summary_csv,
                    args.component,
                )
                try:
                    result.update(read_fix_summary(bbox_summary_csv, bbox_clip_key))
                except (OSError, ValueError):
                    pass
                result["error"] = f"{type(error).__name__}: {error}"
                print(f"  실패: {result['error']}\n", file=sys.stderr)

            if selection is not None:
                completion = finalize_completion(
                    result=result,
                    selection=selection,
                    anno_dir=anno_dir,
                    output_root=output_root,
                    config_sha256=config_sha256,
                    implementation_sha256=implementation_sha256,
                )
                approval = matching_approval(approvals, completion)
                result["approved"] = "true" if approval else "false"
            results.append(result)
            write_csv(results_csv, results)

    if selection is not None:
        update_production_reports(output_root, approvals)
    failures = sum(result["status"] == "failed" for result in results)
    print(f"최종 결과 CSV: {results_csv}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
