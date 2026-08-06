#!/usr/bin/env python3
"""Create MP4 videos with FFmpeg hardware acceleration when available."""

from __future__ import annotations

import argparse
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Sequence

import cv2


def frame_number(path: Path) -> tuple[int, int | str]:
    numbers = re.findall(r"\d+", path.stem)
    return (0, int(numbers[-1])) if numbers else (1, path.stem)


def infer_output_path(image_dir: Path) -> Path:
    """Infer video path from the compact or legacy visualization layout."""
    if image_dir.parent.name == "visualization":
        return image_dir.parent.parent / "videos" / f"{image_dir.name}.mp4"

    parts = image_dir.parts
    indices = [index for index, part in enumerate(parts) if part == "visualization"]
    if indices:
        index = indices[-1]
        relative = parts[index + 1 :]
        scenario_output = Path(*parts[:index])
        if len(relative) >= 3 and relative[1] == "camera":
            return scenario_output / "videos" / relative[0] / f"{image_dir.name}.mp4"

    raise ValueError("영상 경로를 자동 계산할 수 없습니다. --output을 지정하세요.")


def image_frames(image_dir: Path) -> list[Path]:
    frames = list(image_dir.glob("*.jpg")) + list(image_dir.glob("*.png"))
    return sorted(frames, key=frame_number)


def ffmpeg_encoders(ffmpeg: str) -> str:
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout + result.stderr


def choose_encoder(requested: str) -> tuple[str, str | None]:
    if requested == "opencv":
        return "opencv", None

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        if requested == "auto":
            return "opencv", None
        raise RuntimeError(f"--encoder {requested}를 사용하려면 ffmpeg가 필요합니다.")

    encoders = ffmpeg_encoders(ffmpeg)
    if requested == "videotoolbox":
        if "h264_videotoolbox" not in encoders:
            raise RuntimeError("현재 ffmpeg에 h264_videotoolbox encoder가 없습니다.")
        return "videotoolbox", ffmpeg
    if requested == "libx264":
        if "libx264" not in encoders:
            raise RuntimeError("현재 ffmpeg에 libx264 encoder가 없습니다.")
        return "libx264", ffmpeg

    if platform.system() == "Darwin" and "h264_videotoolbox" in encoders:
        return "videotoolbox", ffmpeg
    if "libx264" in encoders:
        return "libx264", ffmpeg
    return "opencv", None


def ffmpeg_video(
    image_dir: Path,
    frames: Sequence[Path],
    output_path: Path,
    fps: float,
    encoder: str,
    ffmpeg: str,
) -> None:
    suffixes = {path.suffix.lower() for path in frames}
    if len(suffixes) != 1:
        raise ValueError("FFmpeg 가속 인코딩은 한 폴더에 한 이미지 형식만 있어야 합니다.")
    extension = next(iter(suffixes))
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-framerate",
        str(fps),
        "-pattern_type",
        "glob",
        "-i",
        str(image_dir / f"*{extension}"),
    ]
    if encoder == "videotoolbox":
        command.extend(["-c:v", "h264_videotoolbox", "-b:v", "10M"])
    else:
        command.extend(["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"])
    command.extend(
        [
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        output_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"FFmpeg {encoder} 인코딩 실패: {result.stderr.strip()}"
        )


def opencv_video(
    frames: Sequence[Path],
    output_path: Path,
    fps: float,
) -> None:
    first_frame = cv2.imread(str(frames[0]))
    if first_frame is None:
        raise RuntimeError(f"이미지를 읽을 수 없습니다: {frames[0]}")
    height, width = first_frame.shape[:2]
    writer = cv2.VideoWriter(
        str(output_path),
        cv2.VideoWriter_fourcc(*"mp4v"),
        fps,
        (width, height),
    )
    if not writer.isOpened():
        raise RuntimeError(f"영상을 생성할 수 없습니다: {output_path}")
    try:
        for frame_path in frames:
            frame = cv2.imread(str(frame_path))
            if frame is None:
                raise RuntimeError(f"이미지를 읽을 수 없습니다: {frame_path}")
            if frame.shape[:2] != (height, width):
                frame = cv2.resize(frame, (width, height))
            writer.write(frame)
    finally:
        writer.release()


def make_video(
    image_dir: Path | str,
    output_path: Path | str | None,
    fps: float,
    encoder: str = "auto",
) -> dict[str, object]:
    image_dir = Path(image_dir).expanduser().resolve()
    output = (
        Path(output_path).expanduser().resolve()
        if output_path is not None
        else infer_output_path(image_dir)
    )
    if fps <= 0:
        raise ValueError("fps는 0보다 커야 합니다.")
    frames = image_frames(image_dir)
    if not frames:
        raise FileNotFoundError(f"이미지 파일을 찾을 수 없습니다: {image_dir}")
    output.parent.mkdir(parents=True, exist_ok=True)

    selected, ffmpeg = choose_encoder(encoder)
    started = time.perf_counter()
    try:
        if selected in {"videotoolbox", "libx264"}:
            assert ffmpeg is not None
            ffmpeg_video(image_dir, frames, output, fps, selected, ffmpeg)
        else:
            opencv_video(frames, output, fps)
    except (RuntimeError, ValueError) as error:
        if encoder != "auto" or selected == "opencv":
            raise
        print(f"[경고] {str(error).splitlines()[0]}")
        fallback_succeeded = False
        if selected == "videotoolbox" and ffmpeg is not None:
            try:
                print("[경고] FFmpeg libx264 encoder로 다시 시도합니다.")
                ffmpeg_video(image_dir, frames, output, fps, "libx264", ffmpeg)
                selected = "libx264"
                fallback_succeeded = True
            except (RuntimeError, ValueError) as fallback_error:
                print(f"[경고] {str(fallback_error).splitlines()[0]}")
        if not fallback_succeeded:
            print("[경고] OpenCV CPU encoder로 다시 시도합니다.")
            selected = "opencv"
            opencv_video(frames, output, fps)
    elapsed = time.perf_counter() - started
    print(
        f"영상 완료: {output} "
        f"(frames={len(frames)}, encoder={selected}, elapsed={elapsed:.2f}s)"
    )
    return {
        "output": output,
        "frames": len(frames),
        "encoder": selected,
        "elapsed_seconds": elapsed,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="시각화 프레임을 MP4로 변환")
    parser.add_argument("image_dir", type=Path, help="JPG/PNG 프레임 폴더")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fps", type=float, default=10.0)
    parser.add_argument(
        "--encoder",
        choices=("auto", "videotoolbox", "libx264", "opencv"),
        default="auto",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    make_video(args.image_dir, args.output, args.fps, encoder=args.encoder)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
