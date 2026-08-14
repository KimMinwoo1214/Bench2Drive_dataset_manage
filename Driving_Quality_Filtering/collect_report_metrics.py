#!/usr/bin/env python3
"""Gather every number the refinement produced into one file and a few tables.

The figures behind this work are scattered across a sweep, an audit, a
decision file, a classification, a relabel and two verifications, and each
answers a different question in its own format. Anyone writing the story up
has to join them by hand, which is how a percentage ends up quoted against
the wrong denominator.

This does the join once. It emits `metrics.json` -- every count paired with
its total and share so no rate has to be recomputed -- plus CSV tables for
the parts that belong in a spreadsheet. The point is that the whole account
can be rebuilt away from this machine, from these files alone.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from quality_contract import read_json, sha256_file  # noqa: E402

FAMILY = re.compile(r"^(.*?)_Town\d+")
REVIEW_REASONS = (
    "bbox_not_verified",
    "existing_label_without_crossing_evidence",
    "trajectory_heading_mismatch",
    "multiple_near_simultaneous_crossings",
)


def family_of(clip: str) -> str:
    match = FAMILY.match(clip)
    return match.group(1) if match else clip


def portion(count: int, total: int) -> dict[str, Any]:
    """A count never travels without the total it was taken from."""
    return {
        "count": count,
        "total": total,
        "share": round(count / total, 6) if total else None,
        "percent": round(count / total * 100, 2) if total else None,
    }


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8-sig") as file:
        return list(csv.DictReader(file))


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def git_commit(repository: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        )
    except Exception:
        return None
    return result.stdout.strip() or None


def collect_dataset(manifest: dict) -> dict[str, Any]:
    components = manifest.get("components", {})
    counts = {
        name: {
            split: len(block.get(split, ())) for split in ("train", "val")
        }
        for name, block in components.items()
    }
    for name, block in counts.items():
        block["total"] = block["train"] + block["val"]
    total = sum(block["total"] for block in counts.values())
    train = sum(block["train"] for block in counts.values())
    return {
        "clips": total,
        "components": counts,
        "parent_split": {
            "train": train, "val": total - train,
            "val_share": round((total - train) / total, 6) if total else None,
        },
    }


def collect_collision(sweep_dir: Path, review_clips: Sequence[str]) -> dict[str, Any]:
    summary = read_json(sweep_dir / "sweep_summary.json")
    contacts = read_jsonl(sweep_dir / "contacts.jsonl")
    verdicts = Counter(row.get("verdict") for row in contacts)
    clips_with_contact = {row["clip"] for row in contacts}
    by_family: dict[str, Counter] = defaultdict(Counter)
    for clip in clips_with_contact:
        by_family[family_of(clip)]["contact"] += 1
    for clip in review_clips:
        by_family[family_of(clip)]["review"] += 1
    return {
        "clips_scanned": summary.get("clips"),
        "contact_runs": len(contacts),
        "clips_with_contact": len(clips_with_contact),
        "verdicts": dict(verdicts),
        "review_queue": len(review_clips),
        "by_family": {name: dict(value) for name, value in sorted(by_family.items())},
        "sweep_sha256": summary.get("summary_sha256"),
    }


def collect_exclusion(
    classification_dir: Path, decisions_path: Path, manifest: dict
) -> dict[str, Any]:
    completion = read_json(classification_dir / "completion.json")
    excluded = sorted(read_json(classification_dir / "excluded.json")["clips"])
    decisions = {
        str(row["clip"]): row for row in read_json(decisions_path)["decisions"]
    }
    parent: dict[str, tuple[str, str]] = {}
    for component, block in manifest.get("components", {}).items():
        for split in ("train", "val"):
            for clip in block.get(split, ()):
                parent[str(clip)] = (component, split)

    by_component, by_split, by_family = Counter(), Counter(), Counter()
    for clip in excluded:
        component, split = parent.get(clip, ("?", "?"))
        by_component[component] += 1
        by_split[split] += 1
        by_family[family_of(clip)] += 1
    parent_component, parent_split = Counter(), Counter()
    for component, split in parent.values():
        parent_component[component] += 1
        parent_split[split] += 1

    reviewed_families = {family_of(clip) for clip in decisions}
    return {
        "reviewed": len(decisions),
        "excluded": portion(len(excluded), len(parent)),
        "accepted": completion.get("accepted"),
        "by_component": {
            name: portion(by_component[name], parent_component[name])
            for name in sorted(parent_component)
        },
        "by_split": {
            name: portion(by_split[name], parent_split[name])
            for name in sorted(parent_split)
        },
        "by_family": {
            name: count for name, count in by_family.most_common()
        },
        "families_reviewed_none_excluded": sorted(
            reviewed_families - set(by_family)
        ),
        "reviewers": sorted({row["reviewer"] for row in decisions.values()}),
        "audit_metrics_sha256": completion.get("audit_metrics_sha256"),
        "completion_sha256": completion.get("completion_sha256"),
    }


def collect_split(classification_dir: Path, dataset: dict) -> dict[str, Any]:
    split = read_json(classification_dir / "filtered_train_val_split.json")
    train, val = split["num_train"], split["num_val"]
    parent = dataset["parent_split"]
    return {
        "policy": split.get("policy"),
        "train": train,
        "val": val,
        "total": train + val,
        "val_share_before": parent["val_share"],
        "val_share_after": round(val / (train + val), 6),
        "parent_manifest_sha256": split.get("parent_manifest", {}).get("sha256"),
    }


def collect_relabel(relabel_root: Path) -> dict[str, Any]:
    results, actions, ok = [], Counter(), Counter()
    ego = Counter()
    group_sizes = Counter()
    unverified_clips = set()
    for component in ("base", "weak"):
        reports = relabel_root / "production_reports" / component
        results.extend(read_csv(reports / "results.csv"))
        detail = reports / "bbox_details.csv"
        if not detail.is_file():
            continue
        with detail.open(newline="", encoding="utf-8-sig") as file:
            for row in csv.DictReader(file):
                actions[row.get("action", "")] += 1
                for phase in ("before", "after"):
                    if row.get(f"ok_{phase}") == "1":
                        ok[phase] += 1
                if row.get("affects_ego_after") == "1":
                    ego["total"] += 1
                    if row.get("ok_after") != "1":
                        ego["unverified"] += 1
                        group_sizes[row.get("group_size", "")] += 1
                        unverified_clips.add(row.get("clip", "").split("/")[0])

    entries = sum(actions.values())
    total = lambda field: sum(int(row.get(field) or 0) for row in results)
    changes = Counter()
    for path in relabel_root.glob("*/traffic_light/reports/affects_ego_changes.csv"):
        for row in read_csv(path):
            changes[(row["before_affects_ego"], row["after_affects_ego"])] += 1

    return {
        "clips": len(results),
        "status": dict(Counter(row.get("status") for row in results)),
        "annotation_frames": total("annotation_frames"),
        "traffic_light_entries": entries,
        "bbox": {
            "reassigned": portion(actions["reassigned"], entries),
            "already_ok": portion(actions["already_ok"], entries),
            "no_consensus": portion(actions["no_consensus"], entries),
            "target_absent": portion(actions["target_absent"], entries),
            "changed_frames": total("bbox_changed_frames"),
            "in_band_before": portion(ok["before"], entries),
            "in_band_after": portion(ok["after"], entries),
        },
        "affects_ego": {
            "changed_entries": total("affects_ego_changed_entries"),
            "changed_frames": total("affects_ego_changed_frames"),
            "false_to_true": changes[("false", "true")],
            "true_to_false": changes[("true", "false")],
            "controlling_entries": ego["total"],
            "unverified": portion(ego["unverified"], ego["total"]),
            "unverified_clips": len(unverified_clips),
            "unverified_by_junction_size": dict(sorted(group_sizes.items())),
        },
        "review": {
            "clips": sum(1 for row in results if row.get("status") == "review"),
            "frames": portion(total("review_frames"), total("annotation_frames")),
        },
        "crossing_events": total("crossing_events"),
    }


def collect_review_reasons(relabel_root: Path) -> dict[str, Any]:
    queue = read_csv(relabel_root / "production_reports" / "review_queue.csv")
    reasons, frames = Counter(), Counter()
    for row in queue:
        path = relabel_root / row["scenario"] / "traffic_light" / "reports" / "relevance_frames.csv"
        counts = Counter()
        for frame in read_csv(path):
            if frame.get("status") != "REVIEW":
                continue
            for token in frame.get("reason", "").split(";"):
                token = token.strip()
                if token in REVIEW_REASONS:
                    counts[token] += 1
                    frames[token] += 1
        if counts:
            reasons[counts.most_common(1)[0][0]] += 1
    return {
        "clips": len(queue),
        "by_main_reason": dict(reasons.most_common()),
        "frames_by_reason": dict(frames.most_common()),
    }


def collect_stop_and_go(relabel_root: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for label in ("original", "corrected"):
        path = relabel_root / f"stop_and_go_{label}.json"
        if not path.is_file():
            continue
        data = read_json(path)
        summary, events = data["summary"], data["events"]
        near = [
            event for event in events
            if event.get("nearest_distance_m") is not None
            and event["nearest_distance_m"] <= 40.0
        ]
        green = sum(1 for event in near if event.get("depart_state") == "GREEN")
        out[label] = {
            "stop_and_go_events": summary["stop_and_go_events"],
            "with_controlling_light": summary["events_with_a_controlling_light"],
            "depart": summary["depart"],
            "within_40m": portion(green, len(near)),
        }
    return out


def write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-root", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--bench-repo", required=True, type=Path)
    parser.add_argument("--internship-repo", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)

    release = args.release_root.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    tables = output_dir / "tables"
    sweep = release / "quality_gate" / "collision_sweep_v2"
    classification = release / "quality_gate" / "classification_v1"
    decisions = release / "quality_gate" / "REVIEW_2026-08-14" / "review_decisions_v1.json"
    relabel = release / "relabel"

    manifest = read_json(args.manifest.expanduser().resolve())
    review_clips = [
        line for line in (sweep / "clips_review_v3.txt").read_text(encoding="utf-8").split()
        if line
    ]

    dataset = collect_dataset(manifest)
    metrics = {
        "schema_version": 1,
        "dataset": dataset,
        "collision": collect_collision(sweep, review_clips),
        "exclusion": collect_exclusion(classification, decisions, manifest),
        "split": collect_split(classification, dataset),
        "relabel": collect_relabel(relabel),
        "review_reasons": collect_review_reasons(relabel),
        "stop_and_go": collect_stop_and_go(relabel),
        "provenance": {
            "release_root": str(release),
            "manifest": str(args.manifest.resolve()),
            "manifest_sha256": sha256_file(args.manifest.expanduser().resolve()),
            "bench_commit": git_commit(args.bench_repo.expanduser().resolve()),
            "internship_commit": git_commit(args.internship_repo.expanduser().resolve()),
            "command": list(sys.argv),
        },
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    temporary = (output_dir / "metrics.json").with_name(".metrics.json.tmp")
    temporary.write_text(
        json.dumps(metrics, indent=2, ensure_ascii=False, sort_keys=False),
        encoding="utf-8",
    )
    temporary.replace(output_dir / "metrics.json")

    # --- funnel: the one table that carries the whole story -----------------
    collision, exclusion, split, rel = (
        metrics["collision"], metrics["exclusion"], metrics["split"], metrics["relabel"]
    )
    write_csv(tables / "funnel.csv", [
        {"stage": "수집 대상", "clips": dataset["clips"], "frames": rel["annotation_frames"],
         "note": " + ".join(f"{k} {v['total']}" for k, v in dataset["components"].items())},
        {"stage": "접촉 검출", "clips": collision["clips_with_contact"], "frames": "",
         "note": f"접촉 런 {collision['contact_runs']}건 · 전 프레임 회전 3D 박스 교차"},
        {"stage": "사람 검토 대상", "clips": collision["review_queue"], "frames": "",
         "note": "등급 A/B/C"},
        {"stage": "충돌 제외", "clips": exclusion["excluded"]["count"], "frames": "",
         "note": f"육안 확인 · 제외율 {exclusion['excluded']['percent']}%"},
        {"stage": "필터링 후", "clips": split["total"], "frames": "",
         "note": f"train {split['train']} / val {split['val']}"},
        {"stage": "bbox 재배정", "clips": "", "frames": rel["bbox"]["reassigned"]["count"],
         "note": f"정상률 {rel['bbox']['in_band_before']['percent']}% → {rel['bbox']['in_band_after']['percent']}%"},
        {"stage": "affects_ego 수정", "clips": "", "frames": rel["affects_ego"]["changed_entries"],
         "note": f"false→true {rel['affects_ego']['false_to_true']} / true→false {rel['affects_ego']['true_to_false']}"},
        {"stage": "판단 보류", "clips": rel["review"]["clips"], "frames": rel["review"]["frames"]["count"],
         "note": f"원본 유지 · 전체 프레임의 {rel['review']['frames']['percent']}%"},
        {"stage": "UV 마스킹", "clips": rel["affects_ego"]["unverified_clips"],
         "frames": rel["affects_ego"]["unverified"]["count"],
         "note": f"UV 감독 대상의 {rel['affects_ego']['unverified']['percent']}%"},
    ])

    write_csv(tables / "by_scenario.csv", [
        {"scenario": name,
         "contact_clips": value.get("contact", 0),
         "review_clips": value.get("review", 0),
         "excluded_clips": exclusion["by_family"].get(name, 0)}
        for name, value in sorted(
            collision["by_family"].items(),
            key=lambda item: (-item[1].get("review", 0), item[0]),
        )
    ])

    print(json.dumps({
        "metrics": str(output_dir / "metrics.json"),
        "tables": sorted(path.name for path in tables.glob("*.csv")),
        "clips": dataset["clips"],
        "excluded": exclusion["excluded"]["count"],
        "filtered": split["total"],
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
