#!/usr/bin/env python3
"""Render before/after frames so a person can judge the traffic-light repair.

The numbers say the repair worked -- the facing angle moves into the band it
has to be in -- but they cannot say whether a box now sits on an actual
traffic light in the image. Only looking can. This renders the same frames
twice, once from the annotations as collected and once from the repaired
ones, and puts them side by side.

Frames are chosen where a light actually controls the ego, because that is
the only place a wrong assignment reaches the model. Clips where the repair
declined to act (a T-junction, or too few lights to pin the assignment down)
are the ones worth looking at, and those are exactly what the fix report
lists.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
TOWN = re.compile(r"_(Town\d+(?:HD)?)_Route")


def ego_frames(detail_csv: Path, clip: str, limit: int) -> list[str]:
    """Frames where a light affects the ego, spread across the clip.

    Consecutive frames look almost identical at 10 Hz, so take them evenly
    spaced rather than the first few.
    """
    found = []
    with detail_csv.open(newline="", encoding="utf-8-sig") as file:
        for row in csv.DictReader(file):
            if row.get("clip", "").split("/")[0] != clip:
                continue
            if row.get("affects_ego_after") != "1":
                continue
            frame = row.get("frame", "").split(".")[0]
            if frame and (not found or found[-1] != frame):
                found.append(frame)
    if len(found) <= limit:
        return found
    step = len(found) / limit
    return [found[int(index * step)] for index in range(limit)]


def _render(job: dict) -> tuple[str, str, bool, str]:
    command = [
        sys.executable, "visualize.py",
        "--input", job["source"], "--output-dir", job["output"],
        "--profile", "camera-bev",
        "--bbox-cameras", "CAM_FRONT", "CAM_FRONT_LEFT", "CAM_FRONT_RIGHT",
        "--start-frame", job["start"], "--max-frames", job["count"],
    ]
    if job["anno"]:
        command += ["--anno-dir", job["anno"]]
    result = subprocess.run(
        command, cwd=str(SCRIPT_DIR), capture_output=True, text=True
    )
    tail = (result.stderr.strip().splitlines() or [""])[-1]
    return job["clip"], job["phase"], result.returncode == 0, tail


def stitch(before_dir: Path, after_dir: Path, output: Path) -> int:
    """Pair up matching renders into one image, before on top of after."""
    try:
        from PIL import Image
    except Exception as error:
        print(f"[warn] 비교 이미지 생략 (PIL 없음): {error}", file=sys.stderr)
        return 0
    output.mkdir(parents=True, exist_ok=True)
    made = 0
    for before in sorted(before_dir.rglob("*.jpg")) + sorted(before_dir.rglob("*.png")):
        after = after_dir / before.relative_to(before_dir)
        if not after.is_file():
            continue
        top, bottom = Image.open(before), Image.open(after)
        width = max(top.width, bottom.width)
        canvas = Image.new("RGB", (width, top.height + bottom.height), "black")
        canvas.paste(top, (0, 0))
        canvas.paste(bottom, (0, top.height))
        name = "__".join(before.relative_to(before_dir).parts)
        canvas.save(output / f"{Path(name).stem}.jpg", quality=88)
        made += 1
    return made


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--relabel-root", required=True, type=Path)
    parser.add_argument("--base-root", required=True, type=Path)
    parser.add_argument("--weak-root", required=True, type=Path)
    parser.add_argument("--clips", required=True, type=Path,
                        help="한 줄에 클립 하나. fix_by_clip.csv에서 뽑은 목록")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--frames-per-clip", type=int, default=4)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args(argv)

    relabel = args.relabel_root.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    clips = [line for line in args.clips.read_text(encoding="utf-8").split() if line]

    jobs = []
    for clip in clips:
        source = args.base_root / clip
        detail = relabel / "production_reports" / "base" / "bbox_details.csv"
        if not (source / "anno").is_dir():
            source = args.weak_root / clip
            detail = relabel / "production_reports" / "weak" / "bbox_details.csv"
        corrected = relabel / clip / "traffic_light" / "corrected_anno"
        if not corrected.is_dir():
            print(f"[skip] 수정본 없음: {clip}", flush=True)
            continue
        frames = ego_frames(detail, clip, args.frames_per_clip) if detail.is_file() else []
        if not frames:
            print(f"[skip] ego 신호등 프레임 없음: {clip}", flush=True)
            continue
        target = output_dir / clip
        for frame in frames:
            for phase, anno in (("before", ""), ("after", str(corrected))):
                jobs.append({
                    "clip": clip, "phase": phase, "source": str(source),
                    "anno": anno, "start": frame, "count": "1",
                    "output": str(target / phase / frame),
                })

    print(f"clips={len(clips)} jobs={len(jobs)} workers={args.workers}", flush=True)
    ok = failed = 0
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for clip, phase, good, tail in pool.map(_render, jobs):
            if good:
                ok += 1
            else:
                failed += 1
                print(f"[FAIL] {clip} {phase}: {tail}", flush=True)

    compared = 0
    for clip in clips:
        target = output_dir / clip
        if (target / "before").is_dir() and (target / "after").is_dir():
            compared += stitch(target / "before", target / "after", target / "compare")
    print(json.dumps({
        "output": str(output_dir), "rendered": ok, "failed": failed,
        "comparisons": compared,
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
