#!/usr/bin/env python3
"""Sweep every frame of every clip for expert collisions, anywhere in the clip.

The CARLA leaderboard records a vehicle collision as an infraction but keeps
driving, so most expert collisions happen mid-clip and leave the episode length
untouched. An end-of-clip check therefore only ever finds the few that happened
to land on the last frame.

What separates a real impact from hard braking or a parked-car box overlap is
that a collision moves BOTH bodies. Braking changes only the ego. A box that
overlaps a parked car changes neither. So for every contact this records the
motion discontinuity of the ego and of the specific actor it touched, taken
from the speed and yaw Bench2Drive already logs per actor, and reports contacts
where both bodies react at the same moment.

Contacts include near misses (small clearance) because at 10 Hz a fast impact
can happen between two samples and never show a positive box overlap.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .geometry import oriented_box_metrics
    from .quality_contract import canonical_sha256, load_manifest, write_json_atomic
    from .audit_expert_driving import actor_category, annotation_files, frame_number
except ImportError:
    from geometry import oriented_box_metrics
    from quality_contract import canonical_sha256, load_manifest, write_json_atomic
    from audit_expert_driving import actor_category, annotation_files, frame_number


SCHEMA_VERSION = 1
# Only actors this close to the ego centre can touch it; skips the far traffic.
NEIGHBOUR_RADIUS_M = 12.0
# A 10 Hz sample can straddle an impact, so treat a very small gap as contact.
CONTACT_CLEARANCE_M = 0.10
# Frames inspected on each side of a contact run for the impulse response.
IMPULSE_WINDOW = 4
# A speed change larger than this in one frame is a spawn or teleport in the
# log, not an impact: it would be 150 m/s^2 sustained over the whole frame.
ARTIFACT_JUMP_M_S = 15.0


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _wrap(angle: float) -> float:
    while angle > math.pi:
        angle -= 2 * math.pi
    while angle < -math.pi:
        angle += 2 * math.pi
    return angle


def _yaw_radians(box: Mapping[str, Any]) -> float | None:
    rotation = box.get("rotation")
    if not isinstance(rotation, (list, tuple)) or len(rotation) < 3:
        return None
    yaw = _finite(rotation[2])
    return math.radians(yaw) if yaw is not None else None


def _centre(box: Mapping[str, Any]) -> tuple[float, float] | None:
    centre = box.get("center", box.get("location"))
    if not isinstance(centre, (list, tuple)) or len(centre) < 2:
        return None
    x, y = _finite(centre[0]), _finite(centre[1])
    return (x, y) if x is not None and y is not None else None


def _series_jump(values: Sequence[float | None]) -> float:
    """Largest physically possible change between consecutive samples.

    Changes above ARTIFACT_JUMP_M_S are dropped rather than reported. A car
    cannot shed 25 m/s in one 100 ms frame; that value shows up because actors
    are logged at full speed the frame they spawn, which made every
    YieldToEmergencyVehicle clip look like a two-body impact.
    """
    best = 0.0
    for before, after in zip(values, values[1:]):
        if before is None or after is None:
            continue
        change = abs(after - before)
        if change > ARTIFACT_JUMP_M_S:
            continue
        best = max(best, change)
    return best


def _angle_jump(values: Sequence[float | None]) -> float:
    best = 0.0
    for before, after in zip(values, values[1:]):
        if before is None or after is None:
            continue
        best = max(best, abs(_wrap(after - before)))
    return best


# There is deliberately no travel-direction signal here. Bench2Drive's logged
# ego x/y jitter by more than a metre between frames even on a straight road at
# 43 km/h, while the logged speed over the same frames is smooth, so any angle
# derived from consecutive positions is noise rather than a knock off course.
# That rules out detecting a light side-swipe from the ego's own motion: the
# only evidence such a contact leaves is the contact geometry itself and, when
# the impact ends the route, the missing frames after it.


def _read_clip(clip_dir: Path) -> list[dict[str, Any]]:
    """Return one compact state record per annotation frame."""
    frames: list[dict[str, Any]] = []
    for path in annotation_files(clip_dir / "anno"):
        number = frame_number(path)
        if number is None:
            continue
        try:
            with gzip.open(str(path), "rt", encoding="utf-8") as file:
                annotation = json.load(file)
        except (OSError, ValueError):
            continue
        if not isinstance(annotation, dict):
            continue
        boxes = annotation.get("bounding_boxes")
        if not isinstance(boxes, list):
            continue
        ego = next(
            (b for b in boxes if isinstance(b, dict) and b.get("class") == "ego_vehicle"),
            None,
        )
        if ego is None:
            continue
        actors: dict[str, dict[str, Any]] = {}
        ego_centre = _centre(ego)
        for box in boxes:
            if not isinstance(box, dict) or box is ego:
                continue
            category = actor_category(box)
            if category is None:
                continue
            centre = _centre(box)
            if centre is None or ego_centre is None:
                continue
            if math.hypot(centre[0] - ego_centre[0], centre[1] - ego_centre[1]) > NEIGHBOUR_RADIUS_M:
                continue
            actor_id = str(box.get("id", box.get("actor_id", "")))
            if not actor_id:
                continue
            actors[actor_id] = {
                "box": box,
                "category": category,
                "speed": _finite(box.get("speed")),
                "yaw": _yaw_radians(box),
            }
        frames.append(
            {
                "frame": number,
                "ego_box": ego,
                "ego_speed": _finite(annotation.get("speed")),
                "ego_yaw": _finite(annotation.get("theta")),
                "brake": _finite(annotation.get("brake")) or 0.0,
                "steer": _finite(annotation.get("steer")) or 0.0,
                "throttle": _finite(annotation.get("throttle")) or 0.0,
                "actors": actors,
            }
        )
    return frames


def _contacts(frames: Sequence[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Return per-actor lists of frames in contact with the ego."""
    hits: dict[str, list[dict[str, Any]]] = {}
    for state in frames:
        for actor_id, actor in state["actors"].items():
            try:
                geometry = oriented_box_metrics(state["ego_box"], actor["box"])
            except (TypeError, ValueError):
                continue
            touching = geometry["positive_3d_overlap"] or (
                geometry["z_overlap_m"] > 0
                and geometry["bev_clearance_m"] <= CONTACT_CLEARANCE_M
            )
            if not touching:
                continue
            hits.setdefault(actor_id, []).append(
                {
                    "frame": state["frame"],
                    "category": actor["category"],
                    "overlap": bool(geometry["positive_3d_overlap"]),
                    "penetration_m": float(geometry["bev_penetration_m"]),
                    "clearance_m": float(geometry["bev_clearance_m"]),
                    "iou": float(geometry["bev_iou"]),
                }
            )
    return hits


def _impulse(
    frames: Sequence[Mapping[str, Any]], index: dict[int, int], actor_id: str,
    start: int, end: int,
) -> dict[str, Any]:
    """Measure how much the ego and the touched actor changed around a contact."""
    lo, hi = start - IMPULSE_WINDOW, end + IMPULSE_WINDOW
    window = [frames[index[n]] for n in range(lo, hi + 1) if n in index]
    ego_speed = [w["ego_speed"] for w in window]
    ego_yaw = [w["ego_yaw"] for w in window]
    actor_speed = [w["actors"].get(actor_id, {}).get("speed") for w in window]
    actor_yaw = [w["actors"].get(actor_id, {}).get("yaw") for w in window]
    braked = any(w["brake"] >= 0.1 for w in window if start - 1 <= w["frame"] <= end + 1)
    steered = any(abs(w["steer"]) >= 0.1 for w in window if start - 1 <= w["frame"] <= end + 1)
    return {
        "ego_speed_jump_m_s": _series_jump(ego_speed),
        "ego_yaw_jump_rad": _angle_jump(ego_yaw),
        "actor_speed_jump_m_s": _series_jump(actor_speed),
        "actor_yaw_jump_rad": _angle_jump(actor_yaw),
        "ego_speed_before": next((v for v in ego_speed if v is not None), None),
        "actor_speed_before": next((v for v in actor_speed if v is not None), None),
        "braked": braked,
        "steered": steered,
        "window_frames": len(window),
    }


def sweep_clip(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return every ego/actor contact in one clip with its impulse response."""
    clip = str(payload["clip"])
    clip_dir = Path(payload["root"]) / clip
    frames = _read_clip(clip_dir)
    if not frames:
        return {"clip": clip, "component": payload["component"], "frame_count": 0, "contacts": []}
    index = {state["frame"]: position for position, state in enumerate(frames)}
    last = frames[-1]["frame"]
    contacts: list[dict[str, Any]] = []
    for actor_id, rows in _contacts(frames).items():
        rows.sort(key=lambda row: row["frame"])
        run = [rows[0]]
        for row in rows[1:]:
            if row["frame"] == run[-1]["frame"] + 1:
                run.append(row)
            else:
                contacts.append(_summarize(frames, index, actor_id, run, last))
                run = [row]
        contacts.append(_summarize(frames, index, actor_id, run, last))
    return {
        "clip": clip,
        "component": payload["component"],
        "split": payload["split"],
        "frame_count": len(frames),
        "last_frame": last,
        "contacts": contacts,
    }


def _summarize(
    frames: Sequence[Mapping[str, Any]], index: dict[int, int], actor_id: str,
    run: Sequence[Mapping[str, Any]], last_frame: int,
) -> dict[str, Any]:
    start, end = run[0]["frame"], run[-1]["frame"]
    impulse = _impulse(frames, index, actor_id, start, end)
    return {
        "actor_id": actor_id,
        "category": run[0]["category"],
        "start_frame": start,
        "end_frame": end,
        "run_frames": len(run),
        "overlap_frames": sum(1 for row in run if row["overlap"]),
        "max_penetration_m": max(row["penetration_m"] for row in run),
        "min_clearance_m": min(row["clearance_m"] for row in run),
        "max_iou": max(row["iou"] for row in run),
        "frames_after_contact": last_frame - end,
        **impulse,
    }


def score(contact: Mapping[str, Any], config: Mapping[str, float]) -> dict[str, Any]:
    """Rate one contact as a collision candidate.

    A collision moves both bodies. Braking moves only the ego, and a box that
    overlaps a parked car moves neither, so the two-body reaction is what
    separates a real impact from the two common false positives.
    """
    ego_moving = (contact.get("ego_speed_before") or 0.0) >= config["moving_m_s"]
    actor_moving = (contact.get("actor_speed_before") or 0.0) >= config["moving_m_s"]
    if not (ego_moving or actor_moving):
        return {"verdict": "static_overlap", "reasons": [], "both_react": False}

    # Braking saturates near 26 m/s^2 in this dataset, so only a jump beyond
    # that is evidence of an impulse the driver could not have produced.
    ego_impulse = contact["ego_speed_jump_m_s"] >= config["impulse_speed_jump_m_s"]
    actor_impulse = contact["actor_speed_jump_m_s"] >= config["impulse_speed_jump_m_s"]
    ego_reacts = ego_impulse or contact["ego_yaw_jump_rad"] >= config["yaw_jump_rad"]
    actor_reacts = actor_impulse or contact["actor_yaw_jump_rad"] >= config["yaw_jump_rad"]

    reasons = []
    if ego_impulse:
        # Braking in this dataset saturates near 26 m/s^2, so a jump past that
        # is beyond what the pedal can do even when the brake is held down.
        reasons.append("EGO_IMPULSE_BEYOND_BRAKING")
    if ego_reacts and actor_reacts:
        reasons.append("BOTH_BODIES_REACT")
    if actor_reacts and not ego_reacts and contact["overlap_frames"] > 0:
        reasons.append("ACTOR_ONLY_REACTION")
    if contact["frames_after_contact"] <= config["tail_frames"]:
        reasons.append("EPISODE_ENDS_AT_CONTACT")
    if contact["overlap_frames"] > 0 and contact["max_penetration_m"] >= config["deep_penetration_m"]:
        reasons.append("DEEP_PENETRATION")

    strong = {"EGO_IMPULSE_BEYOND_BRAKING"}
    if strong & set(reasons):
        verdict = "likely_collision"
    elif "BOTH_BODIES_REACT" in reasons and ego_impulse and actor_impulse:
        verdict = "likely_collision"
    elif reasons:
        verdict = "suspect"
    else:
        verdict = "contact_without_reaction"
    return {"verdict": verdict, "reasons": reasons, "both_react": ego_reacts and actor_reacts}


DEFAULT_CONFIG = {
    "moving_m_s": 1.0,
    "speed_jump_m_s": 1.0,            # 10 m/s^2 at 10 Hz, any reaction
    "impulse_speed_jump_m_s": 3.0,    # 30 m/s^2, beyond this dataset's braking limit
    "yaw_jump_rad": 0.05,             # 0.5 rad/s at 10 Hz
    "tail_frames": 2.0,
    "deep_penetration_m": 0.30,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--base-root", required=True, type=Path)
    parser.add_argument("--weak-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int, help="only sweep the first N clips (smoke test)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    output = args.output_dir.expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        parser.error(f"output directory is not empty: {output}")
    manifest = load_manifest(args.manifest.expanduser().resolve())
    records = list(manifest.clips)
    if args.limit:
        records = records[: args.limit]
    payloads = [
        {
            "clip": record.name,
            "component": record.component,
            "split": record.split,
            "root": str(
                (args.base_root if record.component == "base" else args.weak_root)
                .expanduser().resolve()
            ),
        }
        for record in records
    ]
    output.mkdir(parents=True, exist_ok=True)
    results = []
    done = 0
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for result in pool.map(sweep_clip, payloads):
            results.append(result)
            done += 1
            if done % 50 == 0:
                print(f"swept {done}/{len(payloads)} clips", flush=True)

    rows = []
    for result in results:
        for contact in result["contacts"]:
            rated = score(contact, DEFAULT_CONFIG)
            rows.append(
                {
                    "clip": result["clip"], "component": result["component"],
                    "split": result["split"], "clip_frames": result["frame_count"],
                    **contact, **rated,
                }
            )
    rows.sort(
        key=lambda row: (
            {"likely_collision": 0, "suspect": 1, "contact_without_reaction": 2,
             "static_overlap": 3}[row["verdict"]],
            -max(row["ego_speed_jump_m_s"], row["actor_speed_jump_m_s"]),
        )
    )
    with (output / "contacts.jsonl").open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    verdicts: dict[str, int] = {}
    clips_by_verdict: dict[str, set] = {}
    for row in rows:
        verdicts[row["verdict"]] = verdicts.get(row["verdict"], 0) + 1
        clips_by_verdict.setdefault(row["verdict"], set()).add(row["clip"])
    summary = {
        "schema_version": SCHEMA_VERSION,
        "swept_at": datetime.now(timezone.utc).isoformat(),
        "config": DEFAULT_CONFIG,
        "neighbour_radius_m": NEIGHBOUR_RADIUS_M,
        "contact_clearance_m": CONTACT_CLEARANCE_M,
        "impulse_window_frames": IMPULSE_WINDOW,
        "clips": len(results),
        "clips_with_contact": len({row["clip"] for row in rows}),
        "contacts": len(rows),
        "contacts_by_verdict": verdicts,
        "clips_by_verdict": {key: len(value) for key, value in clips_by_verdict.items()},
    }
    summary["summary_sha256"] = canonical_sha256(summary)
    write_json_atomic(output / "sweep_summary.json", summary)
    for verdict in ("likely_collision", "suspect"):
        names = sorted(clips_by_verdict.get(verdict, ()))
        (output / f"clips_{verdict}.txt").write_text(
            "".join(f"{name}\n" for name in names), encoding="utf-8"
        )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
