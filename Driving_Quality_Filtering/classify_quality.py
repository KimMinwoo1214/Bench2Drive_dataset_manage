#!/usr/bin/env python3
"""Classify a completed calibration audit into accepted and excluded clips.

Collision evidence comes from the frame sweep (--sweep-dir), not from the
calibration audit's penetration thresholds. Penetration cannot rank collisions
in a rigid-body simulator: measured over the 1,329 clips, stationary box
overlaps were deeper (median 0.20 m) than contacts made while driving (0.12 m).
The sweep instead grades a contact by whether both bodies changed motion, and a
moving car overlapping a pedestrian or cyclist counts on its own.

Every clip the sweep grades as collision evidence becomes REVIEW, so a person
decides it. Nothing is excluded for a collision automatically.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .quality_contract import (
        canonical_sha256,
        filtered_split,
        load_config,
        load_manifest,
        metrics_hash,
        read_json,
        sha256_file,
        write_json_atomic,
    )
except ImportError:
    from quality_contract import (
        canonical_sha256,
        filtered_split,
        load_config,
        load_manifest,
        metrics_hash,
        read_json,
        sha256_file,
        write_json_atomic,
    )


STATUSES = {"PASS", "REVIEW", "EXCLUDE"}
# A contact reaches a person when any of three rules fires. Reaction evidence
# alone is not enough, because two kinds of real collision leave no reaction:
#   - hitting a pedestrian or a bicycle does not slow a car down, so the
#     two-body reaction test is structurally blind to them;
#   - a side swipe transfers no momentum along the direction of travel, so a
#     confirmed one (LaneChange_Town12_Route17604, 54 km/h) had ego dv 0.00.
# "static_overlap" never qualifies: neither body was moving, so the boxes
# merely share space (a parked car alongside), which cannot be a collision.
VULNERABLE_CATEGORIES = {"pedestrian", "bicycle"}
STATIC_VERDICT = "static_overlap"
# A vehicle contact with no reaction still needs eyes on it when the ego was
# driving and the boxes genuinely interpenetrated rather than just grazed.
VEHICLE_CONTACT_MIN_EGO_SPEED_M_S = 2.0
VEHICLE_CONTACT_MIN_PENETRATION_M = 0.10
DECISIONS = {"ACCEPT", "EXCLUDE"}
CLASSIFICATION_FIELDS = (
    "clip", "component", "split", "automatic_status", "final_status",
    "reason_codes", "reviewer", "review_reason_code", "review_note",
    "clip_metrics_sha256",
)


def _read_result(path: Path) -> dict[str, Any]:
    with gzip.open(str(path), "rt", encoding="utf-8") as file:
        value = json.load(file)
    if not isinstance(value, dict):
        raise ValueError(f"clip result is not an object: {path}")
    return value


def _qualifying_collision_run(
    events: Sequence[Mapping[str, Any]],
    categories: set[str],
    minimum_penetration: float,
    minimum_iou: float,
) -> int:
    qualifying = []
    for event in events:
        if event.get("event_type") != "positive_3d_overlap":
            continue
        if str(event.get("actor_category")) not in categories:
            continue
        penetration = float(event.get("bev_penetration_m") or 0.0)
        iou = float(event.get("bev_iou") or 0.0)
        if penetration >= minimum_penetration and iou >= minimum_iou:
            qualifying.append(
                (
                    str(event.get("actor_category")),
                    str(event.get("actor_id")),
                    int(event.get("start_frame")),
                )
            )
    longest = 0
    current = 0
    previous: tuple[str, str, int] | None = None
    for actor_class, actor_id, frame in sorted(qualifying):
        if previous is not None and (actor_class, actor_id) == previous[:2] and frame == previous[2] + 1:
            current += 1
        else:
            current = 1
        longest = max(longest, current)
        previous = actor_class, actor_id, frame
    return longest


def _has_severe_collision(
    events: Sequence[Mapping[str, Any]], categories: set[str], threshold: float
) -> bool:
    return any(
        event.get("event_type") == "positive_3d_overlap"
        and str(event.get("actor_category")) in categories
        and float(event.get("bev_penetration_m") or 0.0) >= threshold
        for event in events
    )


def contact_review_codes(row: Mapping[str, Any]) -> list[str]:
    """Reason codes that send one contact run to a reviewer, empty if none do."""
    if row.get("verdict") == STATIC_VERDICT:
        return []
    overlapped = int(row.get("overlap_frames") or 0) > 0
    if row.get("category") in VULNERABLE_CATEGORIES and overlapped:
        return ["VULNERABLE_ROAD_USER_OVERLAP"]
    codes = [str(code) for code in row.get("reasons", ())]
    if codes:
        if row.get("verdict") == "likely_collision":
            codes.append("SWEEP_LIKELY_COLLISION")
        return codes
    if (
        overlapped
        and float(row.get("ego_speed_before") or 0.0) >= VEHICLE_CONTACT_MIN_EGO_SPEED_M_S
        and float(row.get("max_penetration_m") or 0.0) >= VEHICLE_CONTACT_MIN_PENETRATION_M
    ):
        return ["MOVING_VEHICLE_CONTACT"]
    return []


def load_sweep(sweep_dir: Path) -> tuple[dict[str, list[str]], str]:
    """Return per-clip sweep reason codes and the sweep's own hash."""
    summary = read_json(sweep_dir / "sweep_summary.json")
    if not isinstance(summary, dict) or "summary_sha256" not in summary:
        raise ValueError(f"sweep summary is missing or malformed: {sweep_dir}")
    reasons: dict[str, list[str]] = {}
    with (sweep_dir / "contacts.jsonl").open(encoding="utf-8") as file:
        for line in file:
            if not line.strip():
                continue
            row = json.loads(line)
            codes = contact_review_codes(row)
            if codes:
                reasons.setdefault(str(row["clip"]), []).extend(codes)
    return {clip: sorted(set(codes)) for clip, codes in reasons.items()}, str(summary["summary_sha256"])


def sweep_classification(
    result: Mapping[str, Any], sweep_reasons: Mapping[str, Sequence[str]]
) -> tuple[str, list[str]]:
    """Structural damage still excludes; collision evidence goes to a person."""
    metrics = result["metrics"]
    clip = str(metrics["clip"])
    if int(metrics.get("structural_fatal_count") or 0) > 0:
        return "EXCLUDE", ["STRUCTURAL_FATAL"]
    codes = list(sweep_reasons.get(clip, ()))
    if int(metrics.get("structural_review_count") or 0) > 0:
        codes.append("STRUCTURAL_REVIEW")
    if codes:
        return "REVIEW", sorted(set(codes))
    return "PASS", []


def automatic_classification(
    result: Mapping[str, Any], config: Mapping[str, Any]
) -> tuple[str, list[str]]:
    metrics = result["metrics"]
    events = result["events"]
    reasons = []
    if int(metrics.get("structural_fatal_count") or 0) > 0:
        reasons.append("STRUCTURAL_FATAL")
    collision = config["collision"]
    categories = set(config["collision_categories"])
    exclude_run = _qualifying_collision_run(
        events,
        categories,
        float(collision["exclude_minimum_penetration_m"]),
        float(collision["exclude_minimum_iou"]),
    )
    if _has_severe_collision(
        events,
        categories,
        float(collision["exclude_severe_single_frame_penetration_m"]),
    ):
        reasons.append("SEVERE_COLLISION")
    if exclude_run >= int(collision["exclude_minimum_consecutive_frames"]):
        reasons.append("PERSISTENT_COLLISION")
    if reasons:
        return "EXCLUDE", sorted(set(reasons))

    review_reasons = []
    if int(metrics.get("structural_review_count") or 0) > 0:
        review_reasons.append("STRUCTURAL_REVIEW")
    review_run = _qualifying_collision_run(
        events,
        categories,
        float(collision["review_minimum_penetration_m"]),
        float(collision["review_minimum_iou"]),
    )
    if review_run >= int(collision["review_minimum_consecutive_frames"]):
        review_reasons.append("COLLISION_REVIEW_BAND")
    if review_reasons:
        return "REVIEW", sorted(set(review_reasons))
    return "PASS", []


def load_decisions(
    path: Path | None,
    *,
    metrics_sha256: str,
    events_sha256: str,
    sweep_sha256: str | None = None,
) -> dict[str, Mapping[str, Any]]:
    if path is None:
        return {}
    raw = read_json(path.expanduser().resolve())
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("decision file must have schema_version=1")
    if raw.get("metrics_sha256") != metrics_sha256 or raw.get("events_sha256") != events_sha256:
        raise ValueError("decision file is stale: audit metrics/events hash mismatch")
    if sweep_sha256 is not None and raw.get("sweep_sha256") != sweep_sha256:
        raise ValueError("decision file is stale: collision sweep hash mismatch")
    rows = raw.get("decisions")
    if not isinstance(rows, list):
        raise ValueError("decision file decisions must be a list")
    decisions = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each decision must be an object")
        required = ("clip", "decision", "reviewer", "reason_code", "note", "clip_metrics_sha256")
        if any(not isinstance(row.get(field), str) or not row.get(field) for field in required):
            raise ValueError(f"decision has missing fields: {row}")
        if row["decision"] not in DECISIONS:
            raise ValueError(f"invalid review decision: {row['decision']}")
        if row["clip"] in decisions:
            raise ValueError(f"duplicate decision for clip: {row['clip']}")
        decisions[row["clip"]] = row
    return decisions


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=CLASSIFICATION_FIELDS)
        writer.writeheader()
        for row in rows:
            value = dict(row)
            value["reason_codes"] = json.dumps(value["reason_codes"], ensure_ascii=False)
            writer.writerow(value)
    temporary.replace(path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-dir", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument(
        "--sweep-dir",
        type=Path,
        help="collision sweep directory; its verdicts replace the penetration thresholds",
    )
    parser.add_argument("--decisions", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)

    audit_dir = args.audit_dir.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    if output_dir.exists():
        parser.error(f"output already exists; refusing overwrite: {output_dir}")
    completion = read_json(audit_dir / "completion.json")
    if not isinstance(completion, dict) or completion.get("status") != "completed":
        parser.error("audit completion is missing or incomplete")
    manifest = load_manifest(args.manifest)
    sweep_dir = args.sweep_dir.expanduser().resolve() if args.sweep_dir else None
    sweep_reasons: dict[str, list[str]] = {}
    sweep_sha: str | None = None
    if sweep_dir is not None:
        sweep_reasons, sweep_sha = load_sweep(sweep_dir)
    # A sweep supplies the collision evidence itself, so the config only has to
    # name the collision categories; its thresholds are unused in that mode.
    config = load_config(args.config, require_production=sweep_dir is None)
    results = []
    for record in manifest.clips:
        result = _read_result(audit_dir / "clips" / f"{record.name}.json.gz")
        if result.get("metrics", {}).get("clip") != record.name:
            parser.error(f"clip result mismatch: {record.name}")
        results.append(result)
    metrics_rows = [result["metrics"] for result in results]
    event_rows = [event for result in results for event in result["events"]]
    metrics_sha = metrics_hash(metrics_rows)
    events_sha = canonical_sha256(event_rows)
    if metrics_sha != completion.get("metrics_sha256") or events_sha != completion.get("events_sha256"):
        parser.error("audit completion hash does not match clip artifacts")
    decisions = load_decisions(
        args.decisions, metrics_sha256=metrics_sha, events_sha256=events_sha,
        sweep_sha256=sweep_sha,
    )
    if sweep_dir is not None:
        unknown = set(sweep_reasons) - {record.name for record in manifest.clips}
        if unknown:
            parser.error(f"sweep grades clips outside the manifest: {sorted(unknown)[:5]}")

    rows = []
    accepted = []
    excluded = []
    unresolved = []
    for result in results:
        metrics = result["metrics"]
        clip = str(metrics["clip"])
        if sweep_dir is not None:
            automatic_status, reasons = sweep_classification(result, sweep_reasons)
        else:
            automatic_status, reasons = automatic_classification(result, config)
        if automatic_status == "PASS":
            final_status = "ACCEPTED"
            accepted.append(clip)
        elif automatic_status == "EXCLUDE":
            final_status = "EXCLUDED"
            excluded.append(clip)
        else:
            decision = decisions.get(clip)
            if decision is None:
                final_status = "UNRESOLVED"
                unresolved.append(clip)
            else:
                if decision["clip_metrics_sha256"] != metrics["clip_metrics_sha256"]:
                    parser.error(f"stale clip decision hash: {clip}")
                final_status = "ACCEPTED" if decision["decision"] == "ACCEPT" else "EXCLUDED"
                (accepted if final_status == "ACCEPTED" else excluded).append(clip)
        decision = decisions.get(clip, {})
        rows.append(
            {
                "clip": clip,
                "component": metrics["component"],
                "split": metrics["split"],
                "automatic_status": automatic_status,
                "final_status": final_status,
                "reason_codes": reasons,
                "reviewer": decision.get("reviewer", ""),
                "review_reason_code": decision.get("reason_code", ""),
                "review_note": decision.get("note", ""),
                "clip_metrics_sha256": metrics["clip_metrics_sha256"],
            }
        )
    if set(decisions) - {row["clip"] for row in rows if row["automatic_status"] == "REVIEW"}:
        parser.error("decision file contains a non-REVIEW or unknown clip")
    all_names = {record.name for record in manifest.clips}
    if set(accepted) | set(excluded) | set(unresolved) != all_names:
        parser.error("classification output does not cover the parent manifest")
    if any(
        left & right
        for left, right in (
            (set(accepted), set(excluded)),
            (set(accepted), set(unresolved)),
            (set(excluded), set(unresolved)),
        )
    ):
        parser.error("classification sets overlap")

    output_dir.mkdir(parents=True)
    _write_csv(output_dir / "classification.csv", rows)
    write_json_atomic(output_dir / "accepted.json", {"clips": accepted})
    write_json_atomic(output_dir / "excluded.json", {"clips": excluded})
    write_json_atomic(output_dir / "unresolved_review.json", {"clips": unresolved})
    output_completion = {
        "schema_version": 1,
        "status": "blocked_review" if unresolved else "completed",
        "manifest_sha256": manifest.sha256,
        "audit_metrics_sha256": metrics_sha,
        "audit_events_sha256": events_sha,
        "config_sha256": sha256_file(args.config.expanduser().resolve()),
        "collision_evidence": "sweep" if sweep_dir is not None else "audit_thresholds",
        "sweep_sha256": sweep_sha,
        "accepted": len(accepted),
        "excluded": len(excluded),
        "unresolved": len(unresolved),
        "total": len(all_names),
    }
    output_completion["completion_sha256"] = canonical_sha256(output_completion)
    write_json_atomic(output_dir / "completion.json", output_completion)
    if not unresolved:
        split = filtered_split(manifest, accepted, excluded)
        write_json_atomic(output_dir / "filtered_train_val_split.json", split)
    print(json.dumps(output_completion, indent=2, ensure_ascii=False))
    # An unresolved review queue is a valid classification artifact. Downstream
    # production gates require status=completed and therefore remain blocked.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
