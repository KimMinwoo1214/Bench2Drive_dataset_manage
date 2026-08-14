#!/usr/bin/env python3
"""Turn a reviewer's verdicts on the relabel review queue into an approval file.

A clip lands in the queue when the relabel had an opinion it would not act on
by itself: the trigger geometry says a light controls the ego but the bbox
assignment could not be confirmed, or the collected annotation names a light
the ego's path never actually crosses. In both cases the frames are written
through unchanged, so approving one means "leave these as collected", not
"apply a guess". Nothing downstream may proceed until a person says that.

Approvals are bound to the completion hash of the exact clip they were given
for, so re-running the relabel invalidates them rather than silently carrying
an old verdict onto new output.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def read_queue(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as file:
        return [row for row in csv.DictReader(file) if row.get("status") == "review"]


def read_tokens(path: Path) -> list[str]:
    """Whitespace/comma separated names, '#' starts a comment."""
    text = re.sub(r"#[^\n]*", " ", path.read_text(encoding="utf-8")).replace(",", " ")
    return [token for token in text.split() if token]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-queue", required=True, type=Path)
    parser.add_argument(
        "--approve-list", type=Path,
        help="승인할 클립 목록. 생략하면 큐 전체를 승인한다 (--approve-all 필요)",
    )
    parser.add_argument(
        "--approve-all", action="store_true",
        help="큐 전체를 같은 사유로 승인. 목록 없이 쓰려면 명시해야 한다",
    )
    parser.add_argument("--approved-by", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)

    if (args.approve_list is None) == (not args.approve_all):
        parser.error("--approve-list 와 --approve-all 중 정확히 하나를 쓰세요")

    output = args.output.expanduser().resolve()
    if output.exists():
        parser.error(f"승인 파일이 이미 있습니다. 새 이름을 쓰세요: {output}")

    queue = read_queue(args.review_queue.expanduser().resolve())
    if not queue:
        parser.error(f"review 상태인 행이 없습니다: {args.review_queue}")
    by_clip = {row["scenario"]: row for row in queue}

    if args.approve_all:
        chosen = list(by_clip)
    else:
        chosen = []
        for token in read_tokens(args.approve_list.expanduser().resolve()):
            if token not in by_clip:
                raise SystemExit(f"검토 큐에 없는 클립입니다: {token}")
            if token in chosen:
                raise SystemExit(f"중복 승인: {token}")
            chosen.append(token)

    approvals = [
        {
            "clip": clip,
            "component": by_clip[clip]["component"],
            "completion_sha256": by_clip[clip]["completion_sha256"],
            "approved_by": args.approved_by,
            "reason": args.reason,
        }
        for clip in sorted(chosen)
    ]
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_text(
        json.dumps(
            {"schema_version": 1, "approvals": approvals},
            indent=2, ensure_ascii=False, sort_keys=False,
        ) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)

    withheld = sorted(set(by_clip) - set(chosen))
    print(json.dumps({
        "output": str(output),
        "queue": len(by_clip),
        "approved": len(approvals),
        "withheld": len(withheld),
        "withheld_clips": withheld[:10],
    }, indent=2, ensure_ascii=False))
    if withheld:
        print(
            "\n승인하지 않은 클립이 있으면 relabel-check가 실패합니다. "
            "그 클립들을 split에서 빼거나, 검토 후 승인해야 합니다.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
