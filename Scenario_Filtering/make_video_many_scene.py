#!/usr/bin/env python3
"""Create MP4 videos from each scenario's camera/rgb_front_3d_bbox images.

기본 동작
---------
인자를 주지 않으면 이 스크립트가 위치한 폴더 아래를 재귀적으로 검색하여
각 시나리오의 다음 폴더를 영상으로 변환한다.

    <scenario>/camera/rgb_front_3d_bbox/*.{jpg,jpeg,png}

기본 출력
---------
현재 작업 폴더 아래의 공통 출력 폴더에 다음과 같이 저장한다.

    outputs/videos/<scenario_name>_rgb_front_3d_bbox.mp4

사용 예
-------
# 스크립트가 있는 폴더 아래의 모든 시나리오 처리
python3 make_video.py

# 지정한 폴더 아래의 모든 시나리오 처리
python3 make_video.py /path/to/scenarios

# FPS 변경
python3 make_video.py --fps 10

# 모든 영상을 한 출력 폴더에 저장
python3 make_video.py /path/to/scenarios --output-dir ./outputs/videos

# 기존 영상 덮어쓰기
python3 make_video.py --overwrite
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import cv2


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}


@dataclass(frozen=True)
class VideoJob:
    scenario_dir: Path
    image_dir: Path
    output_path: Path


def natural_key(path: Path) -> list[object]:
    """숫자를 숫자값으로 비교하여 2.jpg가 10.jpg보다 먼저 오도록 정렬한다."""
    parts = re.split(r"(\d+)", path.name)
    return [int(part) if part.isdigit() else part.lower() for part in parts]


def default_search_root() -> Path:
    """인자가 없을 때 현재 작업 폴더가 아니라 스크립트 위치를 기준으로 한다."""
    return Path(__file__).resolve().parent


def resolve_input_path(path: Path | None) -> Path:
    root = default_search_root() if path is None else path.expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"입력 경로가 없습니다: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"입력 경로가 폴더가 아닙니다: {root}")
    return root


def is_bbox_image_dir(path: Path) -> bool:
    return (
        path.is_dir()
        and path.name == "rgb_front_3d_bbox"
        and path.parent.name == "camera"
    )


def discover_image_dirs(root: Path) -> list[Path]:
    """root 자신과 모든 하위 폴더에서 camera/rgb_front_3d_bbox를 찾는다."""
    found: set[Path] = set()

    if is_bbox_image_dir(root):
        found.add(root.resolve())

    direct = root / "camera" / "rgb_front_3d_bbox"
    if direct.is_dir():
        found.add(direct.resolve())

    for candidate in root.rglob("rgb_front_3d_bbox"):
        if is_bbox_image_dir(candidate):
            found.add(candidate.resolve())

    return sorted(found, key=lambda path: str(path))


def list_images(image_dir: Path) -> list[Path]:
    images = [
        path
        for path in image_dir.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    ]
    return sorted(images, key=natural_key)


def scenario_dir_from_image_dir(image_dir: Path) -> Path:
    # <scenario>/camera/rgb_front_3d_bbox
    return image_dir.parent.parent


def sanitize_relative_name(path: Path) -> str:
    text = "__".join(path.parts)
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_") or "scenario"


def build_output_path(
    scenario_dir: Path,
    search_root: Path,
    output_dir: Path | None,
) -> Path:
    # 같은 이름의 시나리오가 서로 다른 하위 폴더에 있을 수 있으므로
    # search_root 기준 상대경로를 파일명에 반영한다.
    try:
        relative = scenario_dir.resolve().relative_to(search_root.resolve())
        output_name = sanitize_relative_name(relative)
    except ValueError:
        output_name = sanitize_relative_name(scenario_dir.resolve())

    if output_dir is None:
        output_dir = Path("outputs") / "videos"
    else:
        output_dir = output_dir.expanduser().resolve()

    return output_dir / f"{output_name}_rgb_front_3d_bbox.mp4"


def build_jobs(
    search_root: Path,
    image_dirs: Iterable[Path],
    output_dir: Path | None,
) -> list[VideoJob]:
    jobs = []
    for image_dir in image_dirs:
        scenario_dir = scenario_dir_from_image_dir(image_dir)
        jobs.append(
            VideoJob(
                scenario_dir=scenario_dir,
                image_dir=image_dir,
                output_path=build_output_path(
                    scenario_dir=scenario_dir,
                    search_root=search_root,
                    output_dir=output_dir,
                ),
            )
        )
    return jobs


def create_video(
    job: VideoJob,
    fps: float,
    codec: str,
    overwrite: bool,
) -> tuple[bool, str]:
    images = list_images(job.image_dir)
    if not images:
        return False, f"이미지 없음: {job.image_dir}"

    if job.output_path.exists() and not overwrite:
        return False, f"기존 파일 건너뜀: {job.output_path}"

    first_frame = None
    first_image_path = None

    for image_path in images:
        frame = cv2.imread(str(image_path))
        if frame is not None:
            first_frame = frame
            first_image_path = image_path
            break

    if first_frame is None:
        return False, f"읽을 수 있는 이미지가 없음: {job.image_dir}"

    height, width = first_frame.shape[:2]
    job.output_path.parent.mkdir(parents=True, exist_ok=True)

    if len(codec) != 4:
        return False, f"codec은 4글자여야 함: {codec}"

    writer = cv2.VideoWriter(
        str(job.output_path),
        cv2.VideoWriter_fourcc(*codec),
        fps,
        (width, height),
    )

    if not writer.isOpened():
        return False, f"VideoWriter 열기 실패: {job.output_path}"

    written = 0
    unreadable = 0
    resized = 0

    try:
        for image_path in images:
            frame = cv2.imread(str(image_path))
            if frame is None:
                unreadable += 1
                continue

            frame_height, frame_width = frame.shape[:2]
            if (frame_width, frame_height) != (width, height):
                frame = cv2.resize(
                    frame,
                    (width, height),
                    interpolation=cv2.INTER_AREA,
                )
                resized += 1

            writer.write(frame)
            written += 1
    finally:
        writer.release()

    if written == 0:
        try:
            job.output_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False, f"기록된 프레임이 없음: {job.image_dir}"

    detail = (
        f"{written} frames, {width}x{height}, {fps:g} FPS"
        f", 첫 이미지={first_image_path.name if first_image_path else '-'}"
    )
    if unreadable:
        detail += f", 읽기 실패={unreadable}"
    if resized:
        detail += f", 크기 조정={resized}"

    return True, detail


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "각 시나리오의 camera/rgb_front_3d_bbox 이미지를 "
            "시나리오별 MP4로 변환합니다."
        )
    )
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=None,
        help=(
            "검색할 시나리오 또는 상위 폴더. "
            "생략하면 make_video.py가 있는 폴더를 사용합니다."
        ),
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=10.0,
        help="출력 영상 FPS (기본값: 10)",
    )
    parser.add_argument(
        "--codec",
        default="mp4v",
        help="OpenCV fourcc codec 4글자 (기본값: mp4v)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help=(
            "모든 영상을 저장할 공통 폴더. 생략하면 outputs/videos에 "
            "저장합니다."
        ),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="기존 MP4를 덮어씁니다.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.fps <= 0:
        print("[ERROR] --fps는 0보다 커야 합니다.", file=sys.stderr)
        return 2

    try:
        search_root = resolve_input_path(args.path)
    except (FileNotFoundError, NotADirectoryError) as error:
        print(f"[ERROR] {error}", file=sys.stderr)
        return 1

    image_dirs = discover_image_dirs(search_root)

    print(f"검색 경로: {search_root}")
    print(f"발견된 시나리오: {len(image_dirs)}")

    if not image_dirs:
        print(
            "[ERROR] camera/rgb_front_3d_bbox 폴더를 찾지 못했습니다.",
            file=sys.stderr,
        )
        return 1

    jobs = build_jobs(
        search_root=search_root,
        image_dirs=image_dirs,
        output_dir=args.output_dir,
    )

    successes = 0
    failures = 0

    for index, job in enumerate(jobs, 1):
        print(
            f"[{index}/{len(jobs)}] {job.scenario_dir.name}\n"
            f"  입력: {job.image_dir}\n"
            f"  출력: {job.output_path}"
        )

        success, message = create_video(
            job=job,
            fps=args.fps,
            codec=args.codec,
            overwrite=args.overwrite,
        )

        if success:
            successes += 1
            print(f"  완료: {message}")
        else:
            failures += 1
            print(f"  건너뜀/실패: {message}")

    print()
    print("=== 처리 결과 ===")
    print(f"전체: {len(jobs)}")
    print(f"완료: {successes}")
    print(f"건너뜀/실패: {failures}")

    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
