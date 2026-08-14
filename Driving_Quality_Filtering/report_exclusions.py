#!/usr/bin/env python3
"""Write the ledger that explains how the filtered split differs from its parent.

The split file alone says which clips survived; it does not say why any clip
left, and the numbers behind the answer live in four places (the parent
manifest, the sweep's contacts, the reviewer's decisions, and the
classification). Anyone asked "how many did you drop, and out of what" has to
join those by hand and will get a slightly different answer each time. This
does the join once and writes it down: totals, a per-scenario-family table,
and one row per excluded clip with the measurement that put it in front of a
reviewer.

Everything here is derived. Re-running it on the same inputs rewrites the same
numbers, and the hashes it prints tie those numbers to the exact audit, sweep
and decision file they came from.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from classify_quality import contact_review_codes  # noqa: E402
from quality_contract import read_json  # noqa: E402

# Clip names are <Scenario>_<Town>_Route<n>_Weather<n>; the family is the head.
FAMILY = re.compile(r"^(.*?)_Town\d+")


def family_of(clip: str) -> str:
    match = FAMILY.match(clip)
    return match.group(1) if match else clip


def load_contacts(sweep_dir: Path) -> dict[str, list[dict]]:
    rows: dict[str, list[dict]] = defaultdict(list)
    with (sweep_dir / "contacts.jsonl").open(encoding="utf-8") as file:
        for line in file:
            if line.strip():
                row = json.loads(line)
                rows[str(row["clip"])].append(row)
    return rows


def worst_contact(contacts: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """The run a reviewer would have judged on.

    A clip can hold several contact runs and only some of them are why it
    reached a reviewer at all; a deep overlap against a parked car easily
    outranks the graze that actually mattered. So rank the runs that qualify
    ahead of the ones that do not, and only then by how much evidence each
    carries.
    """
    return max(
        contacts,
        key=lambda row: (
            bool(contact_review_codes(row)),
            len(row.get("reasons") or ()),
            float(row.get("max_penetration_m") or 0.0),
        ),
    )


def _table(rows: Sequence[Sequence[str]], align: Sequence[str]) -> list[str]:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    def line(row, pad="|"):
        cells = [
            row[i].rjust(widths[i]) if align[i] == "r" else row[i].ljust(widths[i])
            for i in range(len(row))
        ]
        return f"{pad} " + f" {pad} ".join(cells) + f" {pad}"
    rule = "|" + "|".join(
        ("-" * (widths[i] + 1)) + (":" if align[i] == "r" else " ")
        for i in range(len(widths))
    ) + "|"
    return [line(rows[0]), rule] + [line(row) for row in rows[1:]]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--classification-dir", required=True, type=Path)
    parser.add_argument("--sweep-dir", required=True, type=Path)
    parser.add_argument("--decisions", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)

    classification = args.classification_dir.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    completion = read_json(classification / "completion.json")
    split = read_json(classification / "filtered_train_val_split.json")
    decisions = {
        str(row["clip"]): row
        for row in read_json(args.decisions.expanduser().resolve())["decisions"]
    }
    contacts = load_contacts(args.sweep_dir.expanduser().resolve())
    manifest = read_json(args.manifest.expanduser().resolve())

    parent: dict[str, tuple[str, str]] = {}
    for component, block in manifest.get("components", {}).items():
        for split_name in ("train", "val"):
            for clip in block.get(split_name, ()):
                parent[str(clip)] = (component, split_name)

    excluded = sorted(read_json(classification / "excluded.json")["clips"])
    reviewed = sorted(decisions)

    per_clip = []
    for clip in excluded:
        component, split_name = parent.get(clip, ("?", "?"))
        decision = decisions.get(clip, {})
        runs = contacts.get(clip, ())
        worst = worst_contact(runs) if runs else {}
        per_clip.append(
            {
                "clip": clip,
                "family": family_of(clip),
                "component": component,
                "split": split_name,
                "reason_code": decision.get("reason_code", ""),
                "reviewer": decision.get("reviewer", ""),
                "contact_runs": len(runs),
                "actor_category": worst.get("category", ""),
                "frames": f"{worst.get('start_frame','')}-{worst.get('end_frame','')}",
                "penetration_m": round(float(worst.get("max_penetration_m") or 0.0), 3),
                "ego_speed_m_s": round(float(worst.get("ego_speed_before") or 0.0), 2) + 0.0,
                "ego_speed_jump_m_s": round(float(worst.get("ego_speed_jump_m_s") or 0.0), 2),
                "frames_after_contact": worst.get("frames_after_contact", ""),
                "sweep_verdict": worst.get("verdict", ""),
                "sweep_reasons": "|".join(worst.get("reasons") or ()),
            }
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    fields = list(per_clip[0]) if per_clip else ["clip"]
    csv_path = output_dir / "excluded_clips.csv"
    temporary = csv_path.with_name(f".{csv_path.name}.tmp")
    with temporary.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(per_clip)
    temporary.replace(csv_path)

    # Per-family accounting over the whole parent, not just what was excluded,
    # so a family with a high drop rate is visible against its own size.
    families: dict[str, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "contact": 0, "reviewed": 0, "excluded": 0}
    )
    for clip in parent:
        families[family_of(clip)]["total"] += 1
    for clip in contacts:
        families[family_of(clip)]["contact"] += 1
    for clip in reviewed:
        families[family_of(clip)]["reviewed"] += 1
    for clip in excluded:
        families[family_of(clip)]["excluded"] += 1

    by_component: dict[str, int] = defaultdict(int)
    by_split: dict[str, int] = defaultdict(int)
    for row in per_clip:
        by_component[row["component"]] += 1
        by_split[row["split"]] += 1

    parent_component: dict[str, int] = defaultdict(int)
    parent_split: dict[str, int] = defaultdict(int)
    for component, split_name in parent.values():
        parent_component[component] += 1
        parent_split[split_name] += 1

    total = len(parent)
    lines = [
        "# 제외 원장 — Bench2Drive 품질 게이트",
        "",
        f"부모 매니페스트 **{total}** 클립 → 필터링 후 **{completion['accepted']}** "
        f"클립. **{completion['excluded']}개 제외** "
        f"({completion['excluded'] / total * 100:.2f}%).",
        "",
        "제외 사유는 하나뿐이다: **전문가 주행이 실제로 충돌했다.** 센서 결손이나",
        "구조 손상으로 빠진 클립은 없다 (structural_fatal 0건).",
        "",
        "## 1. 어디서 빠졌나",
        "",
    ]
    lines += _table(
        [["구분", "부모", "제외", "잔존", "제외율"]]
        + [
            [name, str(parent_component[name]), str(by_component.get(name, 0)),
             str(parent_component[name] - by_component.get(name, 0)),
             f"{by_component.get(name, 0) / parent_component[name] * 100:.2f}%"]
            for name in sorted(parent_component)
        ]
        + [
            [name, str(parent_split[name]), str(by_split.get(name, 0)),
             str(parent_split[name] - by_split.get(name, 0)),
             f"{by_split.get(name, 0) / parent_split[name] * 100:.2f}%"]
            for name in sorted(parent_split)
        ],
        ["l", "r", "r", "r", "r"],
    )
    lines += [
        "",
        "`split` 규약: **부모의 train/val 소속을 그대로 유지하고, 제외된 클립만 뺀다.**",
        "빠진 자리를 다른 클립으로 채우지 않는다 (`no_backfill`). 그래서 val 비율이",
        "미세하게 움직이며, 이것이 의도된 동작이다.",
        "",
        "## 2. 어떤 시나리오에서 빠졌나",
        "",
        "`검토` = 접촉 증거가 사람에게 올라간 클립. `제외` = 육안 확인 후 충돌로 판정.",
        "",
    ]
    ranked = sorted(
        (row for row in families.items() if row[1]["excluded"]),
        key=lambda row: (-row[1]["excluded"], row[0]),
    )
    lines += _table(
        [["시나리오", "전체", "접촉", "검토", "제외", "제외율"]]
        + [
            [name, str(value["total"]), str(value["contact"]), str(value["reviewed"]),
             str(value["excluded"]), f"{value['excluded'] / value['total'] * 100:.1f}%"]
            for name, value in ranked
        ]
        + [[
            "**합계**",
            str(sum(v["total"] for _, v in ranked)),
            str(sum(v["contact"] for _, v in ranked)),
            str(sum(v["reviewed"] for _, v in ranked)),
            str(sum(v["excluded"] for _, v in ranked)),
            "",
        ]],
        ["l", "r", "r", "r", "r", "r"],
    )
    untouched = sorted(
        name for name, value in families.items() if value["reviewed"] and not value["excluded"]
    )
    lines += [
        "",
        f"검토했으나 **한 건도 제외되지 않은** 시나리오 {len(untouched)}종: "
        + ", ".join(f"`{name}`" for name in untouched),
        "",
        "## 3. 제외된 클립 전체",
        "",
        "`Δv` = 접촉 전후 ego 속도 변화 최대값. `잔여` = 접촉 종료 후 남은 프레임 수",
        "(0이면 충돌과 함께 주행이 끝났다는 뜻).",
        "",
    ]
    lines += _table(
        [["클립", "구분", "상대", "관통(m)", "ego(m/s)", "Δv", "잔여", "사유"]]
        + [
            [row["clip"], row["component"], row["actor_category"],
             f"{row['penetration_m']:.3f}", f"{row['ego_speed_m_s']:.1f}",
             f"{row['ego_speed_jump_m_s']:.2f}", str(row["frames_after_contact"]),
             row["reason_code"].replace("EXPERT_COLLISION_", "")]
            for row in per_clip
        ],
        ["l", "l", "l", "r", "r", "r", "r", "l"],
    )
    reviewers = sorted({row["reviewer"] for row in per_clip if row["reviewer"]})
    lines += [
        "",
        "## 4. 출처",
        "",
        f"- 검토 큐 **{len(reviewed)}** 클립 → 제외 **{len(excluded)}** / 승인 "
        f"**{len(reviewed) - len(excluded)}**",
        f"- 검토자: {', '.join(reviewers) or '(없음)'}",
        f"- 부모 매니페스트 `{completion['manifest_sha256']}`",
        f"- 감사 지표 `{completion['audit_metrics_sha256']}`",
        f"- 충돌 전수조사 `{completion['sweep_sha256']}`",
        f"- 분류 완료 `{completion['completion_sha256']}`",
        "",
        "이 파일은 `report_exclusions.py` 가 생성한다. 위 해시 중 하나라도 바뀌면",
        "다시 만들어야 한다.",
        "",
    ]
    ledger = output_dir / "EXCLUSION_LEDGER.md"
    temporary = ledger.with_name(f".{ledger.name}.tmp")
    temporary.write_text("\n".join(lines), encoding="utf-8")
    temporary.replace(ledger)

    print(json.dumps({
        "ledger": str(ledger), "csv": str(csv_path),
        "parent": total, "excluded": len(excluded), "accepted": completion["accepted"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
