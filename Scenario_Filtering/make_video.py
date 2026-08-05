import argparse
import re
from pathlib import Path

import cv2


def frame_number(path: Path):
    numbers = re.findall(r"\d+", path.stem)
    return int(numbers[-1]) if numbers else path.stem


def make_video(image_dir, output_path, fps):
    image_dir = Path(image_dir)
    output_path = Path(output_path)

    frames = list(image_dir.glob("*.jpg")) + list(image_dir.glob("*.png"))
    frames = sorted(frames, key=frame_number)

    if not frames:
        raise FileNotFoundError(
            f"이미지 파일을 찾을 수 없습니다: {image_dir}"
        )

    first_frame = cv2.imread(str(frames[0]))
    if first_frame is None:
        raise RuntimeError(f"이미지를 읽을 수 없습니다: {frames[0]}")

    height, width = first_frame.shape[:2]
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(
        str(output_path),
        fourcc,
        fps,
        (width, height),
    )

    if not writer.isOpened():
        raise RuntimeError(f"영상을 생성할 수 없습니다: {output_path}")

    for index, frame_path in enumerate(frames, start=1):
        frame = cv2.imread(str(frame_path))

        if frame is None:
            print(f"[경고] 읽기 실패: {frame_path}")
            continue

        if frame.shape[:2] != (height, width):
            frame = cv2.resize(frame, (width, height))

        # 프레임 번호 표시
        cv2.putText(
            frame,
            f"Frame: {index - 1:05d}",
            (20, 40),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.0,
            (0, 255, 0),
            2,
            cv2.LINE_AA,
        )

        writer.write(frame)
        print(f"\r처리 중: {index}/{len(frames)}", end="")

    writer.release()

    print()
    print(f"영상 생성 완료: {output_path}")
    print(f"프레임 수: {len(frames)}")
    print(f"해상도: {width}x{height}")
    print(f"FPS: {fps}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Bench2Drive 시각화 프레임을 MP4 영상으로 변환"
    )
    parser.add_argument(
        "--image-dir",
        required=True,
        help="JPG 또는 PNG 프레임이 들어 있는 폴더",
    )
    parser.add_argument(
        "--output",
        default="visualization.mp4",
        help="출력 MP4 경로",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=10.0,
        help="출력 영상 FPS",
    )

    args = parser.parse_args()

    make_video(
        image_dir=args.image_dir,
        output_path=args.output,
        fps=args.fps,
    )