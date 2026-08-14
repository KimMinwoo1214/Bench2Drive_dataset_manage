#!/usr/bin/env python3
"""Check affects_ego against the one thing it never looked at: the ego moving.

The bbox repair can be graded by a rule it does not control -- a light's head
hangs across the junction from its own trigger volume, so the angle has to
come out near 90. The affects_ego decision has no such rule. It is accepted on
the tool's own confidence criteria, which makes any report built from those
criteria a restatement rather than a check, and eyeballing a handful of clips
only ever says something about the handful.

But driving supplies an outside criterion. When a car is stopped at a light
and then pulls away, the light controlling it was green. The relabel never
read the ego's speed, so agreement between "which light controls the ego" and
"the ego started moving" is real evidence, and it can be measured over every
clip rather than sampled.

Run it against the annotations as collected and against the relabelled ones.
The number that matters is the difference: if the repair picked better lights,
more departures line up with a green.
"""

from __future__ import annotations

import argparse
import gzip
import json
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Sequence

STATE_NAMES = {0: "RED", 1: "YELLOW", 2: "GREEN", 3: "OFF", 4: "UNKNOWN"}
GREEN, YELLOW, RED = 2, 1, 0

STOPPED_M_S = 0.10        # 이 아래면 정지로 본다
MOVING_M_S = 1.00         # 이 위로 올라오면 출발로 본다
MIN_STOP_FRAMES = 10      # 10Hz 이므로 1초. 이보다 짧으면 서행이지 정차가 아니다
DEPART_WINDOW = 5         # 출발 전후 이 프레임 안의 신호를 본다 (반응 시간)
DEPART_LIMIT = 30         # 정차가 끝나고 이 안에 출발 속도에 닿아야 한 사건으로 본다
# 정지선 앞에 선 것과 장애물 뒤에 섰는데 마침 신호등이 그 차선을 통제하는 것은
# 다르다. AccidentTwoWays 처럼 사고 차량을 우회하려고 10초씩 기다리는 시나리오가
# 후자인데, 그 출발은 신호와 아무 상관이 없어서 지표를 오염시킨다. 신호등까지의
# 거리는 actor 에서 온 값이라 치환 손상을 받지 않으므로 여기 쓸 수 있다.
STOPLINE_M = 20.0


def load(path: Path) -> dict:
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(str(path), "rt", encoding="utf-8") as file:
        return json.load(file)


def frame_state(annotation: dict) -> tuple[float, int | None, str, float]:
    """(ego 속도, 통제 신호등 상태, 그 신호등 id, 그 신호등까지 거리)."""
    speed = annotation.get("speed")
    speed = float(speed) if isinstance(speed, (int, float)) else 0.0
    for box in annotation.get("bounding_boxes", ()):
        if box.get("class") == "traffic_light" and box.get("affects_ego") is True:
            state = box.get("state")
            distance = box.get("distance")
            return (
                speed,
                (int(state) if isinstance(state, int) else None),
                str(box.get("id", "")),
                float(distance) if isinstance(distance, (int, float)) else float("inf"),
            )
    return speed, None, "", float("inf")


def stop_and_go(speeds: Sequence[float]) -> list[tuple[int, int]]:
    """(정차 시작, 출발) 프레임 쌍. 정차가 충분히 길었던 것만.

    정차 구간을 먼저 다 찾고, 각 구간 뒤에서 출발 속도에 도달하는 프레임을
    찾는다. 두 문턱 사이를 한 번에 통과할 것을 기대하면 안 된다 -- 차는
    0.1 에서 1.0 m/s 까지 여러 프레임에 걸쳐 서서히 올라간다.
    """
    runs = []
    start = None
    for index, speed in enumerate(speeds):
        if speed <= STOPPED_M_S:
            if start is None:
                start = index
        elif start is not None:
            if index - start >= MIN_STOP_FRAMES:
                runs.append((start, index))
            start = None
    if start is not None and len(speeds) - start >= MIN_STOP_FRAMES:
        runs.append((start, len(speeds)))     # 클립이 정차 상태로 끝남

    events = []
    for stop_start, stop_end in runs:
        depart = next(
            (i for i in range(stop_end, min(len(speeds), stop_end + DEPART_LIMIT))
             if speeds[i] >= MOVING_M_S),
            None,
        )
        if depart is not None:
            events.append((stop_start, depart))
    return events


def scan(job: dict) -> dict:
    directory = Path(job["anno_dir"])
    files = sorted(directory.glob("*.json*"))
    speeds, states, ids, dists = [], [], [], []
    for path in files:
        try:
            speed, state, light, distance = frame_state(load(path))
        except Exception:
            speed, state, light, distance = 0.0, None, "", float("inf")
        speeds.append(speed)
        states.append(state)
        ids.append(light)
        dists.append(distance)

    counts = Counter()
    rows = []
    for start, depart in stop_and_go(speeds):
        # 출발 프레임의 신호를 먼저 본다. 창의 앞쪽부터 훑으면 출발 직전의
        # 빨간불을 집게 되는데, 그건 정확히 출발 직전이라 항상 빨갛다.
        order = [depart] + [
            depart + offset
            for step in range(1, DEPART_WINDOW + 1)
            for offset in (step, -step)
        ]
        index = next(
            (i for i in order if 0 <= i < len(states) and states[i] is not None), None
        )
        state = states[index] if index is not None else None
        light = ids[index] if index is not None else ""
        # 정차 구간에서 통제 신호등이 있었는지 (신호 대기였나, 차량 대기였나)
        stopped_state = next(
            (states[i] for i in range(start, depart) if states[i] is not None), None
        )
        counts["events"] += 1
        if state is None and stopped_state is None:
            counts["no_light"] += 1            # 신호와 무관한 정차 (앞차 등)
            continue
        counts["light_events"] += 1
        # 정차 중 통제 신호등에 가장 가까웠던 거리
        near = min(
            (dists[i] for i in range(start, depart) if states[i] is not None),
            default=float("inf"),
        )
        at_stopline = near <= STOPLINE_M
        counts["at_stopline" if at_stopline else "far_from_stopline"] += 1
        if at_stopline and state is not None:
            counts[f"near_depart_{STATE_NAMES.get(state, state)}"] += 1
        if stopped_state is not None:
            counts[f"stopped_{STATE_NAMES.get(stopped_state, stopped_state)}"] += 1
        if state is None:
            counts["depart_no_light"] += 1
        else:
            counts[f"depart_{STATE_NAMES.get(state, state)}"] += 1
        rows.append({
            "clip": job["clip"], "stop_frame": start, "depart_frame": depart,
            "stopped_state": STATE_NAMES.get(stopped_state, ""),
            "depart_state": STATE_NAMES.get(state, ""),
            "light_id": light,
            "nearest_distance_m": None if near == float("inf") else round(near, 1),
            "at_stopline": at_stopline,
        })
    return {"clip": job["clip"], "counts": counts, "rows": rows, "frames": len(files)}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clips", required=True, type=Path)
    parser.add_argument("--base-root", required=True, type=Path)
    parser.add_argument("--weak-root", required=True, type=Path)
    parser.add_argument(
        "--relabel-root", type=Path,
        help="주면 corrected_anno 를 읽는다. 없으면 수집 원본을 읽는다",
    )
    parser.add_argument("--label", required=True, help="original / corrected 등")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=12)
    args = parser.parse_args(argv)

    clips = [line for line in args.clips.read_text(encoding="utf-8").split() if line]
    jobs = []
    for clip in clips:
        if args.relabel_root is not None:
            anno = args.relabel_root / clip / "traffic_light" / "corrected_anno"
        else:
            anno = args.base_root / clip / "anno"
            if not anno.is_dir():
                anno = args.weak_root / clip / "anno"
        if not anno.is_dir():
            print(f"[skip] anno 없음: {clip}", flush=True)
            continue
        jobs.append({"clip": clip, "anno_dir": str(anno)})

    total = Counter()
    rows = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for result in pool.map(scan, jobs):
            total.update(result["counts"])
            rows.extend(result["rows"])

    light_events = total["light_events"]
    summary = {
        "label": args.label,
        "clips": len(jobs),
        "stop_and_go_events": total["events"],
        "events_with_a_controlling_light": light_events,
        "events_without_any_light": total["no_light"],
        "depart": {
            name: total[f"depart_{name}"] for name in ("GREEN", "YELLOW", "RED", "OFF", "UNKNOWN")
            if total[f"depart_{name}"]
        },
        "depart_no_light": total["depart_no_light"],
        "stopped": {
            name: total[f"stopped_{name}"] for name in ("RED", "YELLOW", "GREEN", "OFF", "UNKNOWN")
            if total[f"stopped_{name}"]
        },
        "at_stopline_events": total["at_stopline"],
        "far_from_stopline_events": total["far_from_stopline"],
        "at_stopline_depart": {
            name: total[f"near_depart_{name}"]
            for name in ("GREEN", "YELLOW", "RED", "OFF", "UNKNOWN")
            if total[f"near_depart_{name}"]
        },
        "at_stopline_depart_green_rate": (
            round(total["near_depart_GREEN"] / total["at_stopline"], 4)
            if total["at_stopline"] else None
        ),
        "depart_green_rate": round(total["depart_GREEN"] / light_events, 4) if light_events else None,
        "stopped_red_rate": round(total["stopped_RED"] / light_events, 4) if light_events else None,
        "thresholds": {
            "stopped_m_s": STOPPED_M_S, "moving_m_s": MOVING_M_S,
            "min_stop_frames": MIN_STOP_FRAMES, "depart_window": DEPART_WINDOW,
        },
    }
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_text(
        json.dumps({"summary": summary, "events": rows}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    temporary.replace(output)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
