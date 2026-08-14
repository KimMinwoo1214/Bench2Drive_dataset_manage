#!/usr/bin/env python3
"""Turn a reviewer's exclusion list into the classifier's decision file.

The reviewer watches the rendered evidence and writes down which clips the
expert crashed in. That list arrives as route numbers, which are short enough
to read off a screen but ambiguous on their own, so this resolves each one
against the review queue and refuses anything that does not land on exactly
one clip. Every clip the sweep sent to review gets a decision here: the ones
on the list are EXCLUDE, the rest are ACCEPT. Leaving one out would strand the
classification in "unresolved" and produce no split at all.

The output is bound to the audit metrics, the audit events and the sweep by
their hashes, so it stops being valid the moment any of those are re-run.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from classify_quality import load_sweep  # noqa: E402
from quality_contract import read_json, write_json_atomic  # noqa: E402

ACCEPT_REASON = "NO_COLLISION_ON_REVIEW"
# Which exclusion reason to record, in order of what the evidence showed.
EXCLUDE_REASONS = (
    ("VULNERABLE_ROAD_USER_OVERLAP", "EXPERT_COLLISION_VRU"),
    ("SWEEP_LIKELY_COLLISION", "EXPERT_COLLISION_IMPACT"),
    ("MOVING_VEHICLE_CONTACT", "EXPERT_COLLISION_SIDE_CONTACT"),
)
EXCLUDE_REASON_DEFAULT = "EXPERT_COLLISION_VEHICLE"


def read_tokens(path: Path) -> list[str]:
    """Read a whitespace/comma separated list, ignoring '#' comments."""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"#[^\n]*", " ", text).replace(",", " ")
    return [token for token in text.split() if token]


def resolve(token: str, review: Sequence[str]) -> str:
    """Map one reviewer token to exactly one clip in the review queue."""
    if token in review:
        return token
    matches = [clip for clip in review if f"_Route{token}_" in clip]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SystemExit(f"no clip in the review queue matches {token!r}")
    raise SystemExit(f"{token!r} is ambiguous across {len(matches)}: {sorted(matches)}")


def exclude_reason(codes: Sequence[str]) -> str:
    for code, reason in EXCLUDE_REASONS:
        if code in codes:
            return reason
    return EXCLUDE_REASON_DEFAULT


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-dir", required=True, type=Path)
    parser.add_argument("--sweep-dir", required=True, type=Path)
    parser.add_argument("--exclude-list", required=True, type=Path)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--note", default="rendered 4-view evidence reviewed")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)

    output = args.output.expanduser().resolve()
    if output.exists():
        parser.error(f"decision file already exists, pick a new name: {output}")

    audit = args.audit_dir.expanduser().resolve()
    completion = read_json(audit / "completion.json")
    metrics_sha = str(completion["metrics_sha256"])
    events_sha = str(completion["events_sha256"])

    hashes = {}
    with (audit / "clip_metrics.jsonl").open(encoding="utf-8") as file:
        for line in file:
            if line.strip():
                row = json.loads(line)
                hashes[str(row["clip"])] = str(row["clip_metrics_sha256"])

    sweep_reasons, sweep_sha = load_sweep(args.sweep_dir.expanduser().resolve())
    review = sorted(sweep_reasons)

    excluded = []
    for token in read_tokens(args.exclude_list):
        clip = resolve(token, review)
        if clip in excluded:
            raise SystemExit(f"{token!r} resolves to an already-excluded clip: {clip}")
        excluded.append(clip)
    excluded_set = set(excluded)

    decisions = []
    for clip in review:
        codes = sweep_reasons[clip]
        drop = clip in excluded_set
        decisions.append(
            {
                "clip": clip,
                "decision": "EXCLUDE" if drop else "ACCEPT",
                "reviewer": args.reviewer,
                "reason_code": exclude_reason(codes) if drop else ACCEPT_REASON,
                "note": f"{args.note}; sweep codes: {'|'.join(codes) or 'none'}",
                "clip_metrics_sha256": hashes[clip],
            }
        )

    write_json_atomic(
        output,
        {
            "schema_version": 1,
            "metrics_sha256": metrics_sha,
            "events_sha256": events_sha,
            "sweep_sha256": sweep_sha,
            "decisions": decisions,
        },
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "review_queue": len(review),
                "excluded": len(excluded),
                "accepted": len(review) - len(excluded),
                "sweep_sha256": sweep_sha,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
