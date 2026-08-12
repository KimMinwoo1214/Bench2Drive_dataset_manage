#!/usr/bin/env python3
"""
통합 수정 summary에서 bbox 또는 affects_ego가 실제로 바뀐 시나리오를 모두 찾아:

1. /home/kmw/dataset/Carla/<scenario>/anno 를 anno_original로 백업
2. ./fixed/<scenario>/anno 를 실제 anno에 적용
3. 원본/수정 annotation으로 전방 카메라 + TOP_DOWN BEV bbox 이미지 생성
4. 카메라와 BEV의 BEFORE/AFTER 좌우 비교 영상 생성

기본 경로
---------
원본/적용 대상:
    /home/kmw/dataset/Carla

fixed anno:
    ./fixed

변경 전 시각화:
    ./visualization/<scenario>/camera/{rgb_front,rgb_top_down}_3d_bbox

변경 후 시각화:
    ./visualize_applied_bbox/<scenario>/camera/{rgb_front,rgb_top_down}_3d_bbox

비교 영상:
    ./comparison_videos/<scenario>_camera_before_after.mp4
    ./comparison_videos/<scenario>_bev_before_after.mp4

사용법
------
python3 apply_visualize_compare_from_summary.py report_summary.csv

빠른 테스트:
python3 apply_visualize_compare_from_summary.py report_summary.csv --max-frames 30

특정 Route만:
python3 apply_visualize_compare_from_summary.py report_summary.csv --routes 15533 15463

BEFORE/AFTER 시각화를 다시 만들지 않고 기존 결과 재사용:
python3 apply_visualize_compare_from_summary.py report_summary.csv --reuse-before --reuse-after
"""

import argparse
import csv
import re
import shutil
import subprocess
import sys
from pathlib import Path

import cv2


ROUTE_RE = re.compile(r"_Route(\d+)_")
TOWN_RE = re.compile(r"_Town([^_]+?)_Route")
IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp"}


def load_changed_rows(path: Path):
    with path.open('r', newline='', encoding='utf-8-sig') as csv_file:
        reader = csv.DictReader(csv_file)
        fieldnames = set(reader.fieldnames or [])
        required = {'clip'}
        missing = required - fieldnames
        if missing:
            names = ', '.join(sorted(missing))
            raise ValueError(f'summary CSV에 필수 컬럼이 없습니다: {names}')
        change_columns = {'action_reassigned', 'affects_ego_changed_frames'}
        if not fieldnames.intersection(change_columns):
            names = ', '.join(sorted(change_columns))
            raise ValueError(f'summary CSV에 변경 컬럼이 없습니다: {names}')

        rows = []
        for row in reader:
            try:
                reassigned = int(float(row.get('action_reassigned') or 0))
            except ValueError as error:
                raise ValueError(
                    f"action_reassigned 값이 숫자가 아닙니다: {row.get('action_reassigned')}"
                ) from error
            try:
                affects_changed = int(float(
                    row.get('affects_ego_changed_frames') or 0
                ))
            except ValueError as error:
                raise ValueError(
                    "affects_ego_changed_frames 값이 숫자가 아닙니다: "
                    f"{row.get('affects_ego_changed_frames')}"
                ) from error
            if reassigned <= 0 and affects_changed <= 0:
                continue
            selected = dict(row)
            selected['action_reassigned'] = reassigned
            selected['affects_ego_changed_frames'] = affects_changed
            rows.append(selected)
    return rows


def scenario_from_clip(value):
    s = str(value).replace("\\", "/").rstrip("/")
    if s.endswith("/anno"):
        s = s[:-5]
    return Path(s).name


def route_from_name(name):
    m = ROUTE_RE.search(name)
    return m.group(1) if m else None


def town_from_name(name):
    m = TOWN_RE.search(name)
    return m.group(1) if m else None


def find_scenario(root: Path, scenario_name: str):
    direct = root / scenario_name
    if direct.is_dir():
        return direct

    matches = [p for p in root.rglob(scenario_name) if p.is_dir()]
    if len(matches) == 1:
        return matches[0]

    return None


def find_fixed_anno(fixed_root: Path, scenario_name: str):
    direct = fixed_root / scenario_name / "anno"
    if direct.is_dir():
        return direct

    matches = [
        p for p in fixed_root.rglob("anno")
        if p.is_dir() and p.parent.name == scenario_name
    ]
    if len(matches) == 1:
        return matches[0]

    return None


def replace_anno(scenario_dir: Path, fixed_anno: Path):
    current = scenario_dir / "anno"
    backup = scenario_dir / "anno_original"

    if not current.is_dir():
        raise FileNotFoundError(f"현재 anno 없음: {current}")

    if not backup.exists():
        shutil.copytree(current, backup)
        print(f"  [BACKUP] {backup}")
    else:
        print(f"  [BACKUP] 기존 anno_original 유지")

    # fixed anno를 현재 anno에 완전히 반영
    shutil.rmtree(current)
    shutil.copytree(fixed_anno, current)
    print(f"  [APPLY]  {fixed_anno} -> {current}")
    return backup


def run_visualize(
    visualize_script: Path,
    scenario_dir: Path,
    output_dir: Path,
    map_root: Path,
    anno_dir: Path,
    max_frames=None,
):
    town = town_from_name(scenario_dir.name)
    if town is None:
        raise ValueError(f"Town 추출 실패: {scenario_dir.name}")

    if output_dir.exists():
        shutil.rmtree(output_dir)

    cmd = [
        sys.executable,
        str(visualize_script),
        "--input", str(scenario_dir),
        "--map-id", town,
        "--map-root", str(map_root),
        "--anno-dir", str(anno_dir),
        "--output-dir", str(output_dir),
        "--profile", "camera-bev",
    ]

    if max_frames is not None:
        cmd += ["--max-frames", str(max_frames)]

    print("  [VIS]", " ".join(cmd))

    result = subprocess.run(cmd, cwd=visualize_script.parent)
    if result.returncode != 0:
        raise RuntimeError(
            f"visualize.py 실패: {scenario_dir.name}, exit={result.returncode}"
        )


def image_map(frame_dir: Path):
    out = {}
    if not frame_dir.is_dir():
        return out

    for p in frame_dir.iterdir():
        if p.is_file() and p.suffix.lower() in IMG_EXTS:
            out[p.stem] = p

    return out


def bbox_view_dirs(scenario_output: Path):
    camera_root = scenario_output / 'camera'
    return {
        'camera': camera_root / 'rgb_front_3d_bbox',
        'bev': camera_root / 'rgb_top_down_3d_bbox',
    }


def views_have_images(view_dirs):
    return all(bool(image_map(path)) for path in view_dirs.values())


def frame_sort_key(stem):
    if stem.isdigit():
        return (0, int(stem))

    nums = re.findall(r"\d+", stem)
    if nums:
        return (1, int(nums[-1]), stem)

    return (2, stem)


def resize_to_height(img, height):
    if img.shape[0] == height:
        return img
    ratio = height / img.shape[0]
    width = max(1, int(round(img.shape[1] * ratio)))
    return cv2.resize(img, (width, height), interpolation=cv2.INTER_AREA)


def scale_image(img, scale):
    if scale == 1.0:
        return img
    width = max(1, int(round(img.shape[1] * scale)))
    height = max(1, int(round(img.shape[0] * scale)))
    interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
    return cv2.resize(img, (width, height), interpolation=interpolation)


def add_panel_header(img, title, route):
    header_h = 55
    out = cv2.copyMakeBorder(
        img,
        header_h, 0, 0, 0,
        cv2.BORDER_CONSTANT,
        value=(20, 20, 20),
    )

    cv2.putText(
        out,
        title,
        (15, 24),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.75,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    cv2.putText(
        out,
        f"Route {route}",
        (15, 45),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.5,
        (210, 210, 210),
        1,
        cv2.LINE_AA,
    )
    return out


def make_comparison_video(
    before_dir: Path,
    after_dir: Path,
    output_path: Path,
    route: str,
    view_name: str,
    fps: float,
    scale: float,
):
    before = image_map(before_dir)
    after = image_map(after_dir)

    common = sorted(set(before) & set(after), key=frame_sort_key)

    if not common:
        raise FileNotFoundError(
            f"공통 프레임 없음\nBEFORE={before_dir}\nAFTER={after_dir}"
        )

    # 첫 정상 프레임으로 크기 확정
    first_left = first_right = None
    for stem in common:
        left = cv2.imread(str(before[stem]))
        right = cv2.imread(str(after[stem]))
        if left is not None and right is not None:
            first_left = left
            first_right = right
            break

    if first_left is None:
        raise RuntimeError("비교 가능한 이미지를 읽지 못했습니다.")

    target_h = min(first_left.shape[0], first_right.shape[0])
    first_left = scale_image(resize_to_height(first_left, target_h), scale)
    first_right = scale_image(resize_to_height(first_right, target_h), scale)

    panel_h = min(first_left.shape[0], first_right.shape[0])
    panel_w = max(first_left.shape[1], first_right.shape[1])

    def fit(img):
        img = resize_to_height(img, panel_h)
        if img.shape[1] < panel_w:
            img = cv2.copyMakeBorder(
                img,
                0, 0, 0, panel_w - img.shape[1],
                cv2.BORDER_CONSTANT,
                value=(0, 0, 0),
            )
        elif img.shape[1] > panel_w:
            img = cv2.resize(
                img,
                (panel_w, panel_h),
                interpolation=cv2.INTER_AREA,
            )
        return img

    left_test = add_panel_header(
        fit(first_left.copy()),
        f"BEFORE - {view_name}",
        route,
    )
    right_test = add_panel_header(
        fit(first_right.copy()),
        f"AFTER - {view_name}",
        route,
    )

    video_w = left_test.shape[1] + right_test.shape[1]
    video_h = left_test.shape[0]

    output_path.parent.mkdir(parents=True, exist_ok=True)

    writer = cv2.VideoWriter(
        str(output_path),
        cv2.VideoWriter_fourcc(*"mp4v"),
        fps,
        (video_w, video_h),
    )

    if not writer.isOpened():
        raise RuntimeError(f"VideoWriter 열기 실패: {output_path}")

    written = 0

    for stem in common:
        left = cv2.imread(str(before[stem]))
        right = cv2.imread(str(after[stem]))

        if left is None or right is None:
            continue

        h = min(left.shape[0], right.shape[0])
        left = scale_image(resize_to_height(left, h), scale)
        right = scale_image(resize_to_height(right, h), scale)

        left = fit(left)
        right = fit(right)

        left = add_panel_header(left, f"BEFORE - {view_name}", route)
        right = add_panel_header(right, f"AFTER - {view_name}", route)

        merged = cv2.hconcat([left, right])

        label = f"frame {stem}"
        cv2.putText(
            merged,
            label,
            (20, merged.shape[0] - 20),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )

        if merged.shape[1] != video_w or merged.shape[0] != video_h:
            merged = cv2.resize(
                merged,
                (video_w, video_h),
                interpolation=cv2.INTER_AREA,
            )

        writer.write(merged)
        written += 1

    writer.release()

    return len(before), len(after), written


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("summary_csv", type=Path)

    parser.add_argument(
        "--dataset-root",
        type=Path,
        default=Path("/home/kmw/dataset/Carla"),
    )
    parser.add_argument(
        "--fixed-root",
        type=Path,
        default=Path("./fixed"),
    )
    parser.add_argument(
        "--visualize-script",
        type=Path,
        default=Path("./visualize.py"),
    )
    parser.add_argument(
        "--map-root",
        type=Path,
        default=Path("./maps"),
    )
    parser.add_argument(
        "--before-root",
        type=Path,
        default=Path("./visualization"),
    )
    parser.add_argument(
        "--after-root",
        type=Path,
        default=Path("./visualize_applied_bbox"),
    )
    parser.add_argument(
        "--video-root",
        type=Path,
        default=Path("./comparison_videos"),
    )
    parser.add_argument(
        "--routes",
        nargs="*",
        help="특정 Route만 처리. 예: --routes 15533 15463",
    )
    parser.add_argument(
        "--reuse-before",
        action="store_true",
        help="기존 BEFORE 카메라/BEV 이미지가 모두 있으면 재사용",
    )
    parser.add_argument(
        "--reuse-after",
        action="store_true",
        help="기존 AFTER 카메라/BEV 이미지가 모두 있으면 재사용",
    )
    parser.add_argument(
        "--max-frames",
        type=int,
        help="visualize 테스트용 최대 프레임 수",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=10.0,
    )
    parser.add_argument(
        "--scale",
        type=float,
        default=0.75,
    )

    args = parser.parse_args()

    summary_csv = args.summary_csv.expanduser().resolve()
    dataset_root = args.dataset_root.expanduser().resolve()
    fixed_root = args.fixed_root.expanduser().resolve()
    visualize_script = args.visualize_script.expanduser().resolve()
    map_root = args.map_root.expanduser().resolve()
    before_root = args.before_root.expanduser().resolve()
    after_root = args.after_root.expanduser().resolve()
    video_root = args.video_root.expanduser().resolve()

    try:
        rows = load_changed_rows(summary_csv)
    except (OSError, ValueError) as error:
        parser.error(str(error))

    if args.routes:
        route_set = set(str(x) for x in args.routes)
        rows = [
            row
            for row in rows
            if route_from_name(scenario_from_clip(row['clip'])) in route_set
        ]

    print("=" * 88)
    print(f"변경 시나리오 수 : {len(rows)}")
    print(f"dataset root     : {dataset_root}")
    print(f"fixed root       : {fixed_root}")
    print(f"before root      : {before_root}")
    print(f"after root       : {after_root}")
    print(f"video root       : {video_root}")
    print("=" * 88)

    report = []
    success = 0

    for row in rows:
        scenario = scenario_from_clip(row["clip"])
        route = route_from_name(scenario)
        reassigned = int(row["action_reassigned"])
        affects_changed = int(row["affects_ego_changed_frames"])

        print("\n" + "-" * 88)
        print(f"[{scenario}]")
        print(f"Route      : {route}")
        print(f"reassigned : {reassigned}")
        print(f"affects_ego: {affects_changed} changed frames")

        status = "ok"
        error_text = ""

        try:
            scenario_dir = find_scenario(dataset_root, scenario)
            if scenario_dir is None:
                raise FileNotFoundError(
                    f"dataset에서 시나리오를 찾지 못함: {scenario}"
                )

            fixed_anno = find_fixed_anno(fixed_root, scenario)
            if fixed_anno is None:
                raise FileNotFoundError(
                    f"fixed anno를 찾지 못함: {scenario}"
                )

            # 1) 원본 백업 후 fixed annotation 반영
            original_anno = replace_anno(scenario_dir, fixed_anno)

            # 2) 원본/수정 annotation으로 카메라 + BEV bbox 생성
            before_scenario_dir = before_root / scenario
            after_scenario_dir = after_root / scenario
            before_views = bbox_view_dirs(before_scenario_dir)
            after_views = bbox_view_dirs(after_scenario_dir)

            if not (args.reuse_before and views_have_images(before_views)):
                run_visualize(
                    visualize_script,
                    scenario_dir,
                    before_scenario_dir,
                    map_root,
                    original_anno,
                    max_frames=args.max_frames,
                )
            else:
                print("  [VIS] 기존 BEFORE 카메라/BEV 결과 재사용")

            if not (args.reuse_after and views_have_images(after_views)):
                run_visualize(
                    visualize_script,
                    scenario_dir,
                    after_scenario_dir,
                    map_root,
                    scenario_dir / 'anno',
                    max_frames=args.max_frames,
                )
            else:
                print("  [VIS] 기존 AFTER 카메라/BEV 결과 재사용")

            # 3) 카메라와 BEV BEFORE/AFTER 비교 영상 생성
            video_counts = {}
            video_paths = {}
            for view_key, view_label in (('camera', 'FRONT CAMERA'), ('bev', 'CAMERA BEV')):
                video_path = video_root / (
                    f"Route{route}_{scenario}_{view_key}_before_after.mp4"
                )
                before_n, after_n, written = make_comparison_video(
                    before_views[view_key],
                    after_views[view_key],
                    video_path,
                    route,
                    view_label,
                    args.fps,
                    args.scale,
                )
                video_counts[view_key] = written
                video_paths[view_key] = str(video_path)
                print(
                    f"  [VIDEO:{view_key}] before={before_n}, "
                    f"after={after_n}, common={written}"
                )
                print(f"  [OK] {video_path}")

            success += 1

        except Exception as exc:
            status = "failed"
            error_text = str(exc)
            print(f"  [FAIL] {exc}")

        report.append({
            "scenario": scenario,
            "route": route,
            "reassigned": reassigned,
            "affects_ego_changed_frames": affects_changed,
            "camera_common_frames": video_counts.get('camera', 0) if status == 'ok' else 0,
            "bev_common_frames": video_counts.get('bev', 0) if status == 'ok' else 0,
            "camera_video": video_paths.get('camera', '') if status == 'ok' else '',
            "bev_video": video_paths.get('bev', '') if status == 'ok' else '',
            "status": status,
            "error": error_text,
        })

    video_root.mkdir(parents=True, exist_ok=True)
    report_path = video_root / "apply_visualize_compare_report.csv"

    with report_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "scenario",
                "route",
                "reassigned",
                "affects_ego_changed_frames",
                "camera_common_frames",
                "bev_common_frames",
                "camera_video",
                "bev_video",
                "status",
                "error",
            ],
        )
        writer.writeheader()
        writer.writerows(report)

    print("\n" + "=" * 88)
    print(f"대상         : {len(rows)}")
    print(f"완료         : {success}/{len(rows)}")
    print(f"리포트       : {report_path}")
    print("=" * 88)

    return 0 if success == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
