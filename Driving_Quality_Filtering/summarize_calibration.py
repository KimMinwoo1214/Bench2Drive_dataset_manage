#!/usr/bin/env python3
"""Verify a completed calibration audit and build threshold-free summaries."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    from .quality_contract import (
        canonical_sha256,
        load_manifest,
        metrics_hash,
        sha256_file,
        write_json_atomic,
    )
except ImportError:
    from quality_contract import (
        canonical_sha256,
        load_manifest,
        metrics_hash,
        sha256_file,
        write_json_atomic,
    )


SCHEMA_VERSION = 1
NUMERIC_METRICS = (
    "frame_count",
    "duration_s",
    "structural_fatal_count",
    "structural_review_count",
    "nonfinite_ego_state_frames",
    "sensor_inventory_file_count",
    "sensor_signature_sample_count",
    "positive_overlap_frames",
    "overlap_event_count",
    "max_overlap_run_frames",
    "max_bev_intersection_area_m2",
    "max_bev_iou",
    "max_bev_penetration_m",
)
OUTPUT_NAMES = (
    "clip_metrics.jsonl",
    "events.jsonl",
    "distribution_summary.json",
    "metric_rankings.csv",
    "CALIBRATION_REPORT.md",
    "summary_manifest.json",
)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"required calibration artifact is missing: {path}")
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid JSON in {path}: {error}") from error


def _read_gzip_json(path: Path) -> Any:
    try:
        with gzip.open(str(path), "rt", encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        raise ValueError(f"required clip audit is missing: {path}")


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def percentile(values: Sequence[float], probability: float) -> float | None:
    """Linear-interpolated percentile, used for description rather than a cutoff."""
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def metric_distribution(rows: Sequence[Mapping[str, Any]], metric: str) -> dict[str, Any]:
    values = [number for number in (_finite(row.get(metric)) for row in rows) if number is not None]
    return {
        "value_count": len(values),
        "missing_count": len(rows) - len(values),
        "minimum": min(values) if values else None,
        "p50": percentile(values, 0.50),
        "p90": percentile(values, 0.90),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "maximum": max(values) if values else None,
    }


def group_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    bbox_counts: Counter[str] = Counter()
    bbox_category_counts: Counter[str] = Counter()
    overlap_category_counts: Counter[str] = Counter()
    issue_counts: Counter[str] = Counter()
    for row in rows:
        bbox_counts.update(row.get("bbox_class_counts", {}))
        bbox_category_counts.update(row.get("bbox_category_counts", {}))
        overlap_category_counts.update(row.get("positive_overlap_category_counts", {}))
        issue_counts.update(row.get("issue_codes", {}))
    return {
        "clip_count": len(rows),
        "frame_count": sum(int(row.get("frame_count", 0)) for row in rows),
        "structural_fatal_clips": sum(int(row.get("structural_fatal_count", 0)) > 0 for row in rows),
        "structural_review_clips": sum(int(row.get("structural_review_count", 0)) > 0 for row in rows),
        "positive_overlap_clips": sum(int(row.get("positive_overlap_frames", 0)) > 0 for row in rows),
        "source_unchanged_clips": sum(bool(row.get("source_unchanged")) for row in rows),
        "bbox_class_counts": dict(sorted(bbox_counts.items())),
        "bbox_category_counts": dict(sorted(bbox_category_counts.items())),
        "positive_overlap_category_counts": dict(sorted(overlap_category_counts.items())),
        "issue_code_counts": dict(sorted(issue_counts.items())),
        "metrics": {metric: metric_distribution(rows, metric) for metric in NUMERIC_METRICS},
    }


def grouped_summaries(rows: Sequence[Mapping[str, Any]], field: str) -> dict[str, Any]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(field, ""))].append(row)
    return {name: group_summary(groups[name]) for name in sorted(groups)}


def ranking_rows(rows: Sequence[Mapping[str, Any]]) -> Iterable[dict[str, Any]]:
    for metric in NUMERIC_METRICS:
        direction = "descending"
        available = [row for row in rows if _finite(row.get(metric)) is not None]
        available.sort(
            key=lambda row: (
                _finite(row.get(metric)) if direction == "ascending" else -_finite(row.get(metric)),
                str(row.get("clip", "")),
            )
        )
        for rank, row in enumerate(available, start=1):
            yield {
                "metric": metric,
                "rank": rank,
                "direction": direction,
                "clip": row["clip"],
                "component": row["component"],
                "split": row["split"],
                "scenario": row["scenario"],
                "town": row["town"],
                "value": row[metric],
            }


def _write_jsonl_atomic(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            file.write("\n")
    temporary.replace(path)


def _write_rankings_atomic(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    fields = ("metric", "rank", "direction", "clip", "component", "split", "scenario", "town", "value")
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def _verify_completion(completion: Mapping[str, Any]) -> None:
    if completion.get("status") != "completed":
        raise ValueError("calibration audit is not completed")
    if completion.get("mode") != "calibration_only" or completion.get("calibration_only") is not True:
        raise ValueError("summary accepts calibration_only audit results only")
    expected = completion.get("completion_sha256")
    unhashed = dict(completion)
    unhashed.pop("completion_sha256", None)
    if not isinstance(expected, str) or canonical_sha256(unhashed) != expected:
        raise ValueError("completion hash mismatch")


def _load_verified_results(
    audit_dir: Path, manifest_path: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    manifest = load_manifest(manifest_path)
    completion = _read_json(audit_dir / "completion.json")
    if not isinstance(completion, dict):
        raise ValueError("completion.json must contain an object")
    _verify_completion(completion)
    if completion.get("clip_count") != len(manifest.clips):
        raise ValueError("completion clip count differs from the parent manifest")
    contract = completion.get("contract")
    if not isinstance(contract, dict) or contract.get("manifest_sha256") != manifest.sha256:
        raise ValueError("completion is bound to a different parent manifest")

    expected_names = {record.name for record in manifest.clips}
    actual_names = {path.name[:-8] for path in (audit_dir / "clips").glob("*.json.gz")}
    if actual_names != expected_names:
        raise ValueError(
            "clip audit inventory mismatch: missing=%d unexpected=%d"
            % (len(expected_names - actual_names), len(actual_names - expected_names))
        )

    metrics_rows: list[dict[str, Any]] = []
    event_rows: list[dict[str, Any]] = []
    for record in manifest.clips:
        payload = _read_gzip_json(audit_dir / "clips" / f"{record.name}.json.gz")
        if not isinstance(payload, dict) or not isinstance(payload.get("metrics"), dict):
            raise ValueError(f"invalid clip audit payload: {record.name}")
        metrics = payload["metrics"]
        if metrics.get("clip") != record.name:
            raise ValueError(f"clip audit name mismatch: {record.name}")
        expected_hash = metrics.get("clip_metrics_sha256")
        unhashed = dict(metrics)
        unhashed.pop("clip_metrics_sha256", None)
        if not isinstance(expected_hash, str) or canonical_sha256(unhashed) != expected_hash:
            raise ValueError(f"clip metrics hash mismatch: {record.name}")
        events = payload.get("events")
        if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
            raise ValueError(f"invalid events payload: {record.name}")
        metrics_rows.append(metrics)
        event_rows.extend(events)

    if metrics_hash(metrics_rows) != completion.get("metrics_sha256"):
        raise ValueError("global metrics hash mismatch")
    if canonical_sha256(event_rows) != completion.get("events_sha256"):
        raise ValueError("global events hash mismatch")
    return metrics_rows, event_rows, completion


def _report_text(summary: Mapping[str, Any], output_hashes: Mapping[str, str]) -> str:
    overall = summary["overall"]
    return "\n".join(
        (
            "# Phase 2.5 Calibration Evidence",
            "",
            "This is descriptive evidence only. It does not set thresholds or assign PASS, REVIEW, or EXCLUDE.",
            "",
            f"- Clips: {overall['clip_count']}",
            f"- Frames: {overall['frame_count']}",
            f"- Structural-fatal candidates: {overall['structural_fatal_clips']}",
            f"- Structural-review candidates: {overall['structural_review_clips']}",
            f"- Any positive 3D-overlap candidates: {overall['positive_overlap_clips']}",
            f"- Source unchanged: {overall['source_unchanged_clips']}/{overall['clip_count']}",
            f"- Audit metrics SHA256: `{summary['audit_metrics_sha256']}`",
            f"- Audit events SHA256: `{summary['audit_events_sha256']}`",
            "",
            "## Artifacts",
            "",
            "- `distribution_summary.json`: global, Base/Weak, scenario, and town distributions.",
            "- `metric_rankings.csv`: full rankings for every numeric metric; no candidate cutoff is applied.",
            "- `clip_metrics.jsonl` and `events.jsonl`: machine-readable source evidence.",
            "- `index.html`: sortable lightweight table generated by the audit.",
            "",
            "## Output hashes",
            "",
            *[f"- `{name}`: `{digest}`" for name, digest in sorted(output_hashes.items())],
            "",
            "Next action: inspect distributions and boundary cases, then explicitly approve a versioned production config. Classification remains blocked.",
            "",
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--audit-dir", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    audit_dir = args.audit_dir.expanduser().resolve()
    if not audit_dir.is_dir():
        raise SystemExit(f"audit directory is missing: {audit_dir}")
    collisions = [audit_dir / name for name in OUTPUT_NAMES if (audit_dir / name).exists()]
    if collisions:
        raise SystemExit(f"summary output already exists; refusing overwrite: {collisions[0]}")

    metrics_rows, event_rows, completion = _load_verified_results(audit_dir, args.manifest)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "mode": "calibration_only",
        "classification_performed": False,
        "thresholds_selected": False,
        "quantile_method": "linear_interpolation_sorted_values",
        "audit_completion_sha256": completion["completion_sha256"],
        "audit_metrics_sha256": completion["metrics_sha256"],
        "audit_events_sha256": completion["events_sha256"],
        "overall": group_summary(metrics_rows),
        "by_component": grouped_summaries(metrics_rows, "component"),
        "by_scenario": grouped_summaries(metrics_rows, "scenario"),
        "by_town": grouped_summaries(metrics_rows, "town"),
    }
    ranking_values = list(ranking_rows(metrics_rows))
    _write_jsonl_atomic(audit_dir / "clip_metrics.jsonl", metrics_rows)
    _write_jsonl_atomic(audit_dir / "events.jsonl", event_rows)
    write_json_atomic(audit_dir / "distribution_summary.json", summary)
    _write_rankings_atomic(audit_dir / "metric_rankings.csv", ranking_values)

    artifact_hashes = {
        name: sha256_file(audit_dir / name)
        for name in (
            "clip_metrics.jsonl", "events.jsonl", "distribution_summary.json", "metric_rankings.csv"
        )
    }
    report_path = audit_dir / "CALIBRATION_REPORT.md"
    temporary_report = report_path.with_name(f".{report_path.name}.tmp")
    temporary_report.write_text(_report_text(summary, artifact_hashes), encoding="utf-8")
    temporary_report.replace(report_path)
    artifact_hashes[report_path.name] = sha256_file(report_path)
    summary_manifest = {
        "schema_version": SCHEMA_VERSION,
        "status": "completed",
        "mode": "calibration_only",
        "classification_performed": False,
        "thresholds_selected": False,
        "audit_completion_sha256": completion["completion_sha256"],
        "summarizer_sha256": sha256_file(Path(__file__).resolve()),
        "artifacts": dict(sorted(artifact_hashes.items())),
    }
    summary_manifest["summary_manifest_sha256"] = canonical_sha256(summary_manifest)
    write_json_atomic(audit_dir / "summary_manifest.json", summary_manifest)
    print(json.dumps(summary_manifest, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
