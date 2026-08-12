#!/usr/bin/env python3
"""Run traffic-light correction, visualization, videos, and CSV reporting."""

from __future__ import annotations

import argparse
import csv
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

import cv2

from apply_visualize_compare_from_summary import (
    bbox_view_dirs,
    frame_sort_key,
    image_map,
    make_comparison_video,
)


SCRIPT_DIR = Path(__file__).resolve().parent
FIX_SCRIPT = SCRIPT_DIR / "fix_tl_bbox_permutation.py"
VISUALIZE_SCRIPT = SCRIPT_DIR / "visualize.py"
ROUTE_RE = re.compile(r"_Route(\d+)_")
TOWN_RE = re.compile(r"_Town([^_]+?)_Route")

RESULT_FIELDS = [
    "scenario",
    "input_anno",
    "output_anno",
    "annotation_frames",
    "bbox_changed_frames",
    "bbox_reassigned_entries",
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
    "error",
]


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


def write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=RESULT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def run_command(command: list[str], label: str) -> None:
    print(f"    [{label}] {' '.join(command)}", flush=True)
    result = subprocess.run(command, cwd=SCRIPT_DIR)
    if result.returncode != 0:
        raise RuntimeError(f"{label} 실패 (exit={result.returncode})")


def read_fix_summary(path: Path) -> dict[str, int]:
    with path.open("r", newline="", encoding="utf-8-sig") as file:
        rows = list(csv.DictReader(file))
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


def run_annotation_fix(
    anno_dir: Path,
    clip_output: Path,
) -> tuple[Path, Path, dict[str, int]]:
    output_anno = clip_output / "anno"
    report_dir = clip_output / "reports"
    detail_csv = report_dir / "traffic_light.csv"
    summary_csv = report_dir / "traffic_light_summary.csv"

    command = [
        sys.executable,
        str(FIX_SCRIPT),
        "--root",
        str(anno_dir),
        "--out",
        str(output_anno),
        "--csv",
        str(detail_csv),
    ]
    run_command(command, "ANNO")
    if not output_anno.is_dir():
        raise FileNotFoundError(f"수정 annotation이 생성되지 않았습니다: {output_anno}")
    return output_anno, detail_csv, read_fix_summary(summary_csv)


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


def empty_result(scenario: str, anno_dir: Path, clip_output: Path) -> dict[str, Any]:
    return {
        "scenario": scenario,
        "input_anno": str(anno_dir),
        "output_anno": str(clip_output / "anno"),
        "annotation_frames": len(annotation_files(anno_dir)),
        "bbox_changed_frames": 0,
        "bbox_reassigned_entries": 0,
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
        "detail_csv": str(clip_output / "reports" / "traffic_light.csv"),
        "summary_csv": str(clip_output / "reports" / "traffic_light_summary.csv"),
        "elapsed_seconds": "",
        "status": "failed",
        "error": "",
    }


def process_scenario(
    scenario: str,
    scenario_dir: Path,
    anno_dir: Path,
    output_root: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    started = time.perf_counter()
    clip_output = output_root / scenario
    if clip_output.exists():
        shutil.rmtree(clip_output)
    clip_output.mkdir(parents=True)
    result = empty_result(scenario, anno_dir, clip_output)

    print("  1/4 annotation 통합 수정", flush=True)
    output_anno, detail_csv, metrics = run_annotation_fix(anno_dir, clip_output)
    result.update(metrics)
    result["output_anno"] = str(output_anno)
    result["detail_csv"] = str(detail_csv)

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
        print("  2/4 수정 annotation 시각화", flush=True)
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
            print("  3/4 수정 결과 영상 생성", flush=True)
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
            print("  3/4 영상 생성 생략 (--no-video)")

        if args.video and metrics["affects_ego_changed_frames"] > 0:
            print("  4/4 affects_ego 변경 감지: BEFORE/AFTER 비교 영상 생성",
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
            reason = "affects_ego 변경 없음"
            if not args.video:
                reason = "--no-video"
            print(f"  4/4 BEFORE/AFTER 비교 영상 생략 ({reason})")
    else:
        print("  2/4 시각화 생략 (--no-visualization)")
        print("  3/4 영상 생성 생략")
        print("  4/4 비교 영상 생성 생략")

    result["elapsed_seconds"] = f"{time.perf_counter() - started:.2f}"
    result["status"] = "completed"
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    input_path = args.input.expanduser().resolve()
    output_root = args.output.expanduser().resolve()
    args.map_root = args.map_root.expanduser().resolve()

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

    try:
        scenarios = discover_scenarios(input_path)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if not scenarios:
        parser.error(f"annotation이 있는 시나리오를 찾지 못했습니다: {input_path}")

    output_root.mkdir(parents=True, exist_ok=True)
    print(f"입력: {input_path}")
    print(f"출력: {output_root}")
    print(f"시나리오: {len(scenarios)}개\n", flush=True)

    results = []
    failures = 0
    for index, (scenario, scenario_dir, anno_dir) in enumerate(scenarios, start=1):
        print(f"[{index}/{len(scenarios)}] {scenario}", flush=True)
        clip_output = output_root / scenario
        try:
            result = process_scenario(
                scenario, scenario_dir, anno_dir, output_root, args)
            print(
                "  완료: bbox_frames={bbox}, affects_frames={affects}, "
                "comparison={comparison}\n".format(
                    bbox=result["bbox_changed_frames"],
                    affects=result["affects_ego_changed_frames"],
                    comparison=result["comparison_created"],
                )
            )
        except Exception as error:
            failures += 1
            result = empty_result(scenario, anno_dir, clip_output)
            summary_csv = Path(result["summary_csv"])
            if summary_csv.is_file():
                try:
                    result.update(read_fix_summary(summary_csv))
                except (OSError, ValueError):
                    pass
            result["error"] = f"{type(error).__name__}: {error}"
            print(f"  실패: {result['error']}\n", file=sys.stderr)
        results.append(result)
        write_csv(output_root / "results.csv", results)

    print(f"최종 결과 CSV: {output_root / 'results.csv'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
