#!/usr/bin/env python3
"""One-command traffic-light QA, correction, rendering, and video pipeline."""

from __future__ import annotations

import argparse
import csv
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from b2d_traffic_light_relabeler import (
    EVENT_FIELDS,
    FRAME_FIELDS,
    STATE_NAMES,
    Config,
    annotation_files,
    assign_event_ids,
    build_candidates,
    event_rows,
    frame_rows,
    load_frames,
    make_frame_labels,
    open_annotation,
    resolve_candidates,
    write_annotation,
)
from make_video import make_video
from render_corrected_clip import parse_camera_selection, render_clip
from traffic_light_affect import find_ego_traffic_lights


CHANGE_FIELDS = [
    "frame",
    "traffic_light_id",
    "state",
    "state_name",
    "before_affects_ego",
    "after_affects_ego",
    "event_id",
    "confidence",
    "reason",
]

SUMMARY_FIELDS = [
    "scenario",
    "frames",
    "detected_trigger_events",
    "selected_traffic_light_events",
    "changed_frames",
    "review_events",
    "camera_views",
    "lidar_rendered",
    "rendered_frames",
    "video_encoder",
    "elapsed_seconds",
    "status",
    "error",
]


def discover_scenarios(input_path: Path) -> list[tuple[str, Path, Path]]:
    """Return (scenario name, scenario directory, anno directory)."""
    if input_path.name == "anno" and input_path.is_dir():
        if annotation_files(input_path):
            return [(input_path.parent.name, input_path.parent, input_path)]
    direct_anno = input_path / "anno"
    if direct_anno.is_dir() and annotation_files(direct_anno):
        return [(input_path.name, input_path, direct_anno)]

    discovered: list[tuple[str, Path, Path]] = []
    names: set[str] = set()
    for anno_dir in sorted(input_path.rglob("anno")):
        if not anno_dir.is_dir() or not annotation_files(anno_dir):
            continue
        scenario_dir = anno_dir.parent
        name = scenario_dir.name
        if name in names:
            raise ValueError(f"중복 클립 이름이 있습니다: {name}")
        names.add(name)
        discovered.append((name, scenario_dir, anno_dir))
    return discovered


def infer_workspace_root(input_path: Path) -> Path:
    candidates = [input_path, *input_path.parents]
    data_dir = next((path for path in candidates if path.name == "data"), None)
    return data_dir.parent if data_dir is not None else input_path.parent


def write_csv(path: Path, fieldnames: Sequence[str], rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def correct_annotations(
    frames: Sequence[Any],
    labels: Sequence[Any],
    corrected_dir: Path,
) -> list[dict[str, Any]]:
    corrected_dir.mkdir(parents=True, exist_ok=True)
    changes: list[dict[str, Any]] = []
    for frame, label in zip(frames, labels):
        output_path = corrected_dir / frame.path.name
        if label.source != "recovered" or label.light_id is None:
            shutil.copy2(frame.path, output_path)
            continue

        data = open_annotation(frame.path)
        changed = False
        for box in data.get("bounding_boxes", []):
            if (
                box.get("class") == "traffic_light"
                and str(box.get("id")) == label.light_id
                and box.get("affects_ego") is False
            ):
                before = box["affects_ego"]
                box["affects_ego"] = True
                changes.append(
                    {
                        "frame": frame.name,
                        "traffic_light_id": label.light_id,
                        "state": box.get("state"),
                        "state_name": STATE_NAMES.get(box.get("state"), "Unknown"),
                        "before_affects_ego": str(before).lower(),
                        "after_affects_ego": "true",
                        "event_id": label.event_id,
                        "confidence": label.confidence,
                        "reason": label.reason,
                    }
                )
                changed = True
        if changed:
            write_annotation(output_path, data)
        else:
            shutil.copy2(frame.path, output_path)
    return changes


def relabel_clip(
    scenario: str,
    anno_dir: Path,
    traffic_dir: Path,
) -> dict[str, Any]:
    config = Config()
    files = annotation_files(anno_dir)
    frames = load_frames(files, config)
    events = build_candidates(frames, config)
    resolve_candidates(events, config)
    assign_event_ids(events)
    labels = make_frame_labels(frames, events)

    frame_data = list(frame_rows(scenario, frames, labels))
    event_data = list(event_rows(scenario, frames, events))
    write_csv(traffic_dir / "frame_labels.csv", FRAME_FIELDS, frame_data)
    write_csv(traffic_dir / "events.csv", EVENT_FIELDS, event_data)
    write_csv(
        traffic_dir / "review_queue.csv",
        EVENT_FIELDS,
        [row for row in event_data if row["needs_review"] == "true"],
    )
    changes = correct_annotations(
        frames,
        labels,
        traffic_dir / "corrected_anno",
    )
    write_csv(traffic_dir / "changes.csv", CHANGE_FIELDS, changes)
    original_true_count = find_ego_traffic_lights(
        anno_dir,
        traffic_dir / "original_affects_ego.csv",
        verbose=False,
        print_summary=False,
    )
    corrected_true_count = find_ego_traffic_lights(
        traffic_dir / "corrected_anno",
        traffic_dir / "corrected_affects_ego.csv",
        verbose=False,
        print_summary=False,
    )

    return {
        "frames": len(frames),
        "detected_events": len(event_data),
        "selected_events": sum(
            row["status"] in {"matched", "missing_label"} for row in event_data
        ),
        "changed_frames": len(changes),
        "review_events": sum(row["needs_review"] == "true" for row in event_data),
        "event_data": event_data,
        "changes": changes,
        "original_true_count": original_true_count,
        "corrected_true_count": corrected_true_count,
        "corrected_dir": traffic_dir / "corrected_anno",
    }


def print_event_report(relabel: dict[str, Any]) -> None:
    changes_per_event: dict[str, int] = {}
    for change in relabel["changes"]:
        event_id = str(change["event_id"] or "")
        changes_per_event[event_id] = changes_per_event.get(event_id, 0) + 1

    print(f"    원본 annotation: {relabel['frames']}프레임")
    print(f"    검출된 trigger 이벤트: {len(relabel['event_data'])}개")
    if not relabel["event_data"]:
        print("    - 신호등 trigger 이벤트 없음")
    for event in relabel["event_data"]:
        crossing = event["crossing_frame"] or "-"
        changed = changes_per_event.get(str(event["event_id"]), 0)
        print(
            f"    - {event['event_id']} / light_id={event['light_id']}: "
            f"status={event['status']}, "
            f"frames={event['start_frame']}..{event['end_frame']}, "
            f"crossing={crossing}, changed={changed}, "
            f"confidence={event['confidence']}, "
            f"review={event['needs_review']}"
        )
        print(f"      reason={event['reason']}")
    print(
        f"    affects_ego=true 행: 원본={relabel['original_true_count']}, "
        f"보정={relabel['corrected_true_count']}"
    )
    print(
        f"    실제 JSON 변경: {relabel['changed_frames']}프레임, "
        f"검토 필요: {relabel['review_events']}이벤트"
    )


def prepare_clip_output(output_root: Path, scenario: str) -> Path:
    clip_output = (output_root / scenario).resolve()
    if output_root.resolve() not in clip_output.parents:
        raise ValueError(f"안전하지 않은 출력 경로입니다: {clip_output}")
    if clip_output.exists():
        shutil.rmtree(clip_output)
    clip_output.mkdir(parents=True)
    return clip_output


def process_clip(
    scenario: str,
    scenario_dir: Path,
    anno_dir: Path,
    output_root: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    started = time.perf_counter()
    clip_output = prepare_clip_output(output_root, scenario)
    traffic_dir = clip_output / "traffic_light"

    print("  1/3 신호등 이벤트 분석 및 annotation 보정")
    relabel = relabel_clip(scenario, anno_dir, traffic_dir)
    print_event_report(relabel)
    rendered_frames = 0
    video_encoder = ""

    if args.render:
        selected = ",".join(args.camera_names) or "none"
        print(
            "  2/3 corrected annotation 시각화 "
            f"(cameras={selected}, lidar={args.lidar})"
        )
        render_result = render_clip(
            scenario_dir,
            relabel["corrected_dir"],
            clip_output / "visualization",
            camera_names=args.camera_names,
            render_lidar=args.lidar,
            start_frame=args.start_frame,
            max_frames=args.max_frames,
            workers=args.workers,
        )
        rendered_frames = int(render_result["frames"])

        if args.video:
            print("  3/3 MP4 생성")
            encoder_results: list[str] = []
            for camera_name, camera_dir in render_result["camera_dirs"].items():
                camera_video = make_video(
                    camera_dir,
                    clip_output / "videos" / "camera" / f"{camera_dir.name}.mp4",
                    args.fps,
                    encoder=args.encoder,
                )
                encoder_results.append(
                    f"{camera_name}={camera_video['encoder']}"
                )
            if render_result["lidar_dir"] is not None:
                lidar_video = make_video(
                    render_result["lidar_dir"],
                    clip_output / "videos" / "lidar_bev_bbox.mp4",
                    args.fps,
                    encoder=args.encoder,
                )
                encoder_results.append(f"lidar={lidar_video['encoder']}")
            video_encoder = ";".join(encoder_results)
        else:
            print("  3/3 영상 생성 안 함 (--no-video)")
    else:
        print("  2/3 시각화 안 함 (--no-render)")
        print("  3/3 영상 생성 안 함")

    elapsed = time.perf_counter() - started
    return {
        "scenario": scenario,
        "frames": relabel["frames"],
        "detected_trigger_events": relabel["detected_events"],
        "selected_traffic_light_events": relabel["selected_events"],
        "changed_frames": relabel["changed_frames"],
        "review_events": relabel["review_events"],
        "camera_views": ",".join(args.camera_names),
        "lidar_rendered": str(bool(args.lidar)).lower(),
        "rendered_frames": rendered_frames,
        "video_encoder": video_encoder,
        "elapsed_seconds": f"{elapsed:.2f}",
        "status": "completed",
        "error": "",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "클립 하나 또는 data 폴더 전체를 신호등 분석→보정→"
            "변경 CSV→카메라/LiDAR 시각화→MP4로 처리합니다."
        )
    )
    parser.add_argument(
        "input",
        type=Path,
        help="클립 폴더 하나 또는 여러 클립이 있는 data 폴더",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="출력 폴더 (기본값: input의 data 폴더 옆 outputs)",
    )
    parser.add_argument(
        "--render",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="corrected 카메라/LiDAR 프레임 생성 (기본값: true)",
    )
    parser.add_argument(
        "--video",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="카메라/LiDAR MP4 생성 (기본값: true)",
    )
    parser.add_argument("--fps", type=float, default=10.0)
    parser.add_argument("--workers", type=int, help="렌더링 worker 수 (기본값: 최대 4)")
    parser.add_argument(
        "--cameras",
        default="all",
        help=(
            "all, none 또는 쉼표 목록: "
            "front,front_left,front_right,back,back_left,back_right"
        ),
    )
    parser.add_argument(
        "--no-lidar",
        dest="lidar",
        action="store_false",
        help="LiDAR 이미지와 영상을 생성하지 않음",
    )
    parser.set_defaults(lidar=True)
    parser.add_argument("--start-frame", type=int, help="시각화를 시작할 프레임")
    parser.add_argument("--max-frames", type=int, help="시각화할 최대 프레임 수")
    parser.add_argument(
        "--encoder",
        choices=("auto", "videotoolbox", "libx264", "opencv"),
        default="auto",
        help="MP4 encoder (기본값: auto)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    input_path = args.input.expanduser().resolve()
    if not input_path.is_dir():
        parser.error(f"입력 폴더가 없습니다: {input_path}")
    if args.fps <= 0:
        parser.error("--fps는 0보다 커야 합니다.")
    if args.workers is not None and args.workers < 1:
        parser.error("--workers는 1 이상이어야 합니다.")
    if args.start_frame is not None and args.start_frame < 0:
        parser.error("--start-frame은 0 이상이어야 합니다.")
    if args.max_frames is not None and args.max_frames < 1:
        parser.error("--max-frames는 1 이상이어야 합니다.")
    if args.video and not args.render:
        parser.error("--video는 --render와 함께만 사용할 수 있습니다.")
    try:
        args.camera_names = parse_camera_selection(args.cameras)
    except ValueError as error:
        parser.error(str(error))
    if args.render and not args.camera_names and not args.lidar:
        parser.error("카메라와 LiDAR를 모두 제외하면 렌더링할 항목이 없습니다.")

    scenarios = discover_scenarios(input_path)
    if not scenarios:
        parser.error(f"anno/*.json.gz가 있는 클립을 찾지 못했습니다: {input_path}")
    workspace_root = infer_workspace_root(input_path)
    output_root = (
        args.output.expanduser().resolve()
        if args.output is not None
        else workspace_root / "outputs"
    )
    if (
        output_root == input_path
        or output_root in input_path.parents
        or input_path in output_root.parents
    ):
        parser.error(f"출력을 입력 폴더 안에 지정할 수 없습니다: {output_root}")
    output_root.mkdir(parents=True, exist_ok=True)

    print(f"입력: {input_path}")
    print(f"출력: {output_root}")
    print(f"클립: {len(scenarios)}개")
    print("기존에 같은 클립 결과가 있으면 해당 클립 폴더만 새로 만듭니다.\n")

    summaries: list[dict[str, Any]] = []
    failures = 0
    for index, (scenario, scenario_dir, anno_dir) in enumerate(scenarios, start=1):
        print(f"[{index}/{len(scenarios)}] {scenario}")
        try:
            summary = process_clip(
                scenario,
                scenario_dir,
                anno_dir,
                output_root,
                args,
            )
            print(
                f"  완료: changed={summary['changed_frames']}, "
                f"review={summary['review_events']}, elapsed={summary['elapsed_seconds']}s\n"
            )
        except Exception as error:  # keep remaining clips running in multi-clip mode
            failures += 1
            summary = {
                "scenario": scenario,
                "frames": "",
                "detected_trigger_events": "",
                "selected_traffic_light_events": "",
                "changed_frames": "",
                "review_events": "",
                "camera_views": "",
                "lidar_rendered": "",
                "rendered_frames": "",
                "video_encoder": "",
                "elapsed_seconds": "",
                "status": "failed",
                "error": f"{type(error).__name__}: {error}",
            }
            print(f"  실패: {summary['error']}\n", file=sys.stderr)
        summaries.append(summary)

    write_csv(output_root / "summary.csv", SUMMARY_FIELDS, summaries)
    print(f"요약: {output_root / 'summary.csv'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
