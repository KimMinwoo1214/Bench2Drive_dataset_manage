#!/usr/bin/env python3
"""Re-verify a completed audit and its descriptive calibration summaries."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

try:
    from .quality_contract import canonical_sha256, sha256_file
    from .summarize_calibration import _load_verified_results
except ImportError:
    from quality_contract import canonical_sha256, sha256_file
    from summarize_calibration import _load_verified_results


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"required calibration artifact is missing: {path}")
    if not isinstance(value, dict):
        raise ValueError(f"calibration artifact must contain an object: {path}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--audit-dir", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    audit_dir = args.audit_dir.expanduser().resolve()
    metrics_rows, event_rows, completion = _load_verified_results(audit_dir, args.manifest)

    summary = _read_object(audit_dir / "distribution_summary.json")
    summary_manifest = _read_object(audit_dir / "summary_manifest.json")
    expected_manifest_hash = summary_manifest.get("summary_manifest_sha256")
    unhashed_manifest = dict(summary_manifest)
    unhashed_manifest.pop("summary_manifest_sha256", None)
    if not isinstance(expected_manifest_hash, str) or canonical_sha256(unhashed_manifest) != expected_manifest_hash:
        raise ValueError("summary manifest hash mismatch")
    if summary_manifest.get("status") != "completed":
        raise ValueError("calibration summary is not complete")
    if summary_manifest.get("mode") != "calibration_only":
        raise ValueError("calibration summary mode is not calibration_only")
    if summary_manifest.get("classification_performed") is not False:
        raise ValueError("calibration summary unexpectedly records classification")
    if summary_manifest.get("thresholds_selected") is not False:
        raise ValueError("calibration summary unexpectedly records selected thresholds")
    if summary_manifest.get("audit_completion_sha256") != completion.get("completion_sha256"):
        raise ValueError("summary is bound to a different audit completion")

    artifacts = summary_manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise ValueError("summary manifest has no artifact hashes")
    for name, expected_hash in artifacts.items():
        path = audit_dir / name
        if not path.is_file() or sha256_file(path) != expected_hash:
            raise ValueError(f"summary artifact hash mismatch: {path}")

    if summary.get("mode") != "calibration_only":
        raise ValueError("distribution summary mode is not calibration_only")
    if summary.get("audit_metrics_sha256") != completion.get("metrics_sha256"):
        raise ValueError("distribution summary metrics hash mismatch")
    if summary.get("audit_events_sha256") != completion.get("events_sha256"):
        raise ValueError("distribution summary events hash mismatch")
    overall = summary.get("overall")
    if not isinstance(overall, dict) or overall.get("clip_count") != len(metrics_rows):
        raise ValueError("distribution summary clip count mismatch")
    if overall.get("source_unchanged_clips") != len(metrics_rows):
        raise ValueError("not every audited annotation source remained unchanged")

    print(
        json.dumps(
            {
                "status": "verified",
                "mode": "calibration_only",
                "clip_count": len(metrics_rows),
                "event_count": len(event_rows),
                "source_unchanged_clips": overall["source_unchanged_clips"],
                "metrics_sha256": completion["metrics_sha256"],
                "events_sha256": completion["events_sha256"],
                "completion_sha256": completion["completion_sha256"],
                "summary_manifest_sha256": expected_manifest_hash,
                "classification_performed": False,
                "thresholds_selected": False,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
