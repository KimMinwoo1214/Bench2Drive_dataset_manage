import argparse
import csv
import gzip
import json
from pathlib import Path


STATE_NAMES = {
    0: "Red",
    1: "Yellow",
    2: "Green",
    3: "Off",
    4: "Unknown",
}


def load_annotation(json_path: Path):
    """일반 JSON과 JSON.GZ 파일을 모두 읽습니다."""
    if json_path.name.endswith(".json.gz"):
        with gzip.open(json_path, "rt", encoding="utf-8") as file:
            return json.load(file)

    with open(json_path, "r", encoding="utf-8") as file:
        return json.load(file)


def get_frame_name(json_path: Path):
    if json_path.name.endswith(".json.gz"):
        return json_path.name.removesuffix(".json.gz")

    return json_path.stem


def find_annotation_files(input_path: Path):
    """
    입력으로 다음 형식을 지원합니다.

    1. 단일 JSON 파일
    2. anno 폴더
    3. anno 폴더를 포함하는 시나리오 폴더
    """
    if input_path.is_file():
        if (
            input_path.suffix == ".json"
            or input_path.name.endswith(".json.gz")
        ):
            return [input_path]

        raise ValueError(
            f"JSON 또는 JSON.GZ 파일이 아닙니다: {input_path}"
        )

    if not input_path.is_dir():
        raise FileNotFoundError(f"입력 경로가 존재하지 않습니다: {input_path}")

    # 시나리오 폴더를 입력한 경우 anno 폴더 사용
    anno_dir = input_path / "anno"

    if anno_dir.is_dir():
        input_path = anno_dir

    # 같은 프레임의 .json과 .json.gz가 모두 있을 경우 중복 집계하지
    # 않도록 압축본(.json.gz)을 우선합니다.
    selected = {}
    for path in list(input_path.glob("*.json")) + list(input_path.glob("*.json.gz")):
        frame = get_frame_name(path)
        current = selected.get(frame)
        if current is None or path.name.endswith(".json.gz"):
            selected[frame] = path

    files = sorted(selected.values(), key=lambda path: get_frame_name(path))

    if not files:
        raise FileNotFoundError(
            f"Annotation 파일을 찾을 수 없습니다: {input_path}"
        )

    return files


def find_ego_traffic_lights(
    input_path,
    output_path,
    verbose=True,
    print_summary=True,
):
    input_path = Path(input_path)
    output_path = Path(output_path)

    annotation_files = find_annotation_files(input_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "frame",
        "traffic_light_id",
        "state",
        "state_name",
        "distance_m",
        "location",
        "trigger_volume_location",
        "source_file",
    ]

    detected_count = 0

    with open(output_path, "w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()

        for json_path in annotation_files:
            anno = load_annotation(json_path)
            frame = get_frame_name(json_path)

            affected_lights = [
                obj
                for obj in anno.get("bounding_boxes", [])
                if obj.get("class") == "traffic_light"
                and obj.get("affects_ego") is True
            ]

            if not affected_lights:
                continue

            for light in affected_lights:
                state = light.get("state")
                distance = light.get("distance")

                writer.writerow(
                    {
                        "frame": frame,
                        "traffic_light_id": light.get("id"),
                        "state": state,
                        "state_name": STATE_NAMES.get(
                            state,
                            f"Unknown({state})",
                        ),
                        "distance_m": (
                            f"{distance:.2f}"
                            if isinstance(distance, (int, float))
                            else ""
                        ),
                        "location": json.dumps(
                            light.get("location"),
                            ensure_ascii=False,
                        ),
                        "trigger_volume_location": json.dumps(
                            light.get("trigger_volume_location"),
                            ensure_ascii=False,
                        ),
                        "source_file": str(json_path),
                    }
                )

                detected_count += 1

                if verbose:
                    print(
                        f"[Frame {frame}] "
                        f"ID={light.get('id')}, "
                        f"State={STATE_NAMES.get(state, state)}, "
                        f"Distance={distance}"
                    )

    if print_summary:
        print()
        print(f"처리한 annotation 수: {len(annotation_files)}")
        print(f"Ego 적용 신호등 검출 수: {detected_count}")
        print(f"CSV 저장 위치: {output_path}")
    return detected_count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "Bench2Drive annotation에서 Ego에 적용되는 "
            "신호등과 상태를 추적합니다."
        )
    )

    parser.add_argument(
        "--input",
        required=True,
        help="단일 json.gz, anno 폴더 또는 시나리오 폴더 경로",
    )

    parser.add_argument(
        "--output",
        default="traffic_lights.csv",
        help="출력 CSV 파일 경로",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="프레임별 메시지는 숨기고 요약만 출력",
    )

    args = parser.parse_args()

    find_ego_traffic_lights(
        input_path=args.input,
        output_path=args.output,
        verbose=not args.quiet,
    )
