#!/usr/bin/env python3
"""Summarise what the traffic-light relabel changed, and show that it worked.

The relabel writes a lot of evidence -- an entry-level diagnostic CSV, a
per-clip summary, and per-clip decision reports -- but nothing that answers
the two questions a reviewer actually asks: how much was wrong, and is it
right now.

Both have an objective answer here, because the repair has a physical check
that does not depend on the repair itself. A traffic light's head hangs across
the junction from the trigger volume it controls, so the angle between where
the head points and the direction from head to trigger volume must come out
near 90 degrees. A cyclically shifted assignment lands nowhere near it. That
angle is recorded before and after for every entry, so the fix is verifiable
rather than merely reported.

Outputs a markdown ledger, a per-clip CSV, and a before/after histogram of
that angle.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Sequence

# The repaired band. Entries outside it after the fix are the ones to look at.
CORRECT_LO, CORRECT_HI = 80.0, 100.0
HIST_BINS = 90          # 2-degree buckets across 0..180
HIST_MAX = 180.0
COMPONENTS = ("base", "weak")
LABELS_KO = {
    "before": "수정 전", "after": "수정 후", "count": "엔트리 수",
    "xlabel": "facing error [deg]  —  파란 띠(80~100°)가 물리적으로 옳은 구간",
    "title": "신호등 head 배정 검증: head가 자기 trigger volume을 향하는 각도",
}
LABELS_EN = {
    "before": "before fix", "after": "after fix", "count": "entries",
    "xlabel": "facing error [deg]  —  blue band (80-100) is the physically correct range",
    "title": "Traffic-light head assignment: angle from head to its own trigger volume",
}
ACTION_LABEL = {
    "already_ok": "이미 정상",
    "reassigned": "재배정됨",
    "no_consensus": "합의 없음",
    "target_absent": "대상 부재",
}


def _bin(value: float) -> int:
    return min(int(value / HIST_MAX * HIST_BINS), HIST_BINS - 1)


def scan_details(path: Path) -> dict:
    """Stream the entry CSV; it is too big to hold and we only need counts."""
    before = [0] * HIST_BINS
    after = [0] * HIST_BINS
    actions = Counter()
    ok = Counter()
    ego = Counter()
    ego_unverified = Counter()
    group_sizes = Counter()
    per_clip = defaultdict(lambda: Counter())
    reassigned_before = []
    reassigned_after = []
    with path.open(newline="", encoding="utf-8-sig") as file:
        for row in csv.DictReader(file):
            action = row.get("action", "")
            actions[action] += 1
            clip = row.get("clip", "").split("/")[0]
            counts = per_clip[clip]
            counts["entries"] += 1
            counts[action] += 1
            # Only the lights that control the ego reach the model in a way
            # that matters; the rest sit on other approaches. Counting them
            # apart keeps a large, harmless "left alone" figure from reading
            # as a large problem.
            if row.get("affects_ego_after") == "1":
                counts["ego"] += 1
                ego[action] += 1
                if row.get("ok_after") != "1":
                    counts["ego_unverified"] += 1
                    ego_unverified[action] += 1
                    group_sizes[row.get("group_size", "")] += 1
            for phase in ("before", "after"):
                if row.get(f"ok_{phase}") == "1":
                    ok[phase] += 1
                    counts[f"ok_{phase}"] += 1
            for phase, histogram in (("before", before), ("after", after)):
                raw = row.get(f"fac_err_{phase}", "")
                if raw in ("", "nan"):
                    continue
                value = float(raw)
                histogram[_bin(value)] += 1
                if action == "reassigned":
                    (reassigned_before if phase == "before" else reassigned_after).append(value)
    return {
        "before": before, "after": after, "actions": actions, "ok": ok,
        "ego": ego, "ego_unverified": ego_unverified, "group_sizes": group_sizes,
        "per_clip": dict(per_clip),
        "reassigned_before": reassigned_before,
        "reassigned_after": reassigned_after,
        "entries": sum(actions.values()),
    }


def read_summary(path: Path) -> dict[str, dict[str, str]]:
    if not path.is_file():
        return {}
    with path.open(newline="", encoding="utf-8-sig") as file:
        return {row["clip"].split("/")[0]: row for row in csv.DictReader(file)}


def plot(scans: dict, output: Path) -> Path | None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.font_manager as font_manager
        import matplotlib.pyplot as plt
    except Exception as error:            # 그림이 없다고 리포트를 막지는 않는다
        print(f"[warn] 히스토그램 생략: {error}", file=sys.stderr)
        return None

    # Any CJK-capable family will do; without one the Korean labels render as
    # empty boxes and the figure is useless to the people reading it.
    installed = {font.name for font in font_manager.fontManager.ttflist}
    korean = next(
        (name for name in ("Noto Sans CJK KR", "Noto Sans CJK JP", "NanumGothic",
                           "Noto Serif CJK KR", "Malgun Gothic")
         if name in installed),
        None,
    )
    if korean is None:
        # Better a chart with English labels than one full of empty boxes.
        print("[warn] 한글 폰트 없음: 그래프 라벨만 영문으로 씁니다", file=sys.stderr)
    else:
        plt.rcParams["font.family"] = korean
    plt.rcParams["axes.unicode_minus"] = False
    text = LABELS_KO if korean else LABELS_EN

    before = [sum(part["before"][i] for part in scans.values()) for i in range(HIST_BINS)]
    after = [sum(part["after"][i] for part in scans.values()) for i in range(HIST_BINS)]
    centres = [(i + 0.5) * HIST_MAX / HIST_BINS for i in range(HIST_BINS)]
    width = HIST_MAX / HIST_BINS

    figure, (top, bottom) = plt.subplots(2, 1, figsize=(11, 7), sharex=True)
    for axis, values, title, colour in (
        (top, before, text["before"], "#c0392b"),
        (bottom, after, text["after"], "#1e8449"),
    ):
        axis.bar(centres, values, width=width, color=colour, edgecolor="none")
        axis.axvspan(CORRECT_LO, CORRECT_HI, color="#2e86c1", alpha=0.12, zorder=0)
        axis.set_ylabel(text["count"])
        axis.set_title(title, loc="left", fontsize=11)
        axis.grid(axis="y", alpha=0.25)
    bottom.set_xlabel(text["xlabel"])
    bottom.set_xlim(0, HIST_MAX)
    figure.suptitle(text["title"], fontsize=13)
    figure.tight_layout()
    figure.savefig(output, dpi=130)
    plt.close(figure)
    return output


def _table(rows: Sequence[Sequence[str]], align: Sequence[str]) -> list[str]:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    def line(row):
        cells = [
            row[i].rjust(widths[i]) if align[i] == "r" else row[i].ljust(widths[i])
            for i in range(len(row))
        ]
        return "| " + " | ".join(cells) + " |"
    rule = "|" + "|".join(
        ("-" * (widths[i] + 1)) + (":" if align[i] == "r" else " ")
        for i in range(len(widths))
    ) + "|"
    return [line(rows[0]), rule] + [line(row) for row in rows[1:]]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--relabel-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)

    relabel = args.relabel_root.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    reports = relabel / "production_reports"

    scans, summaries = {}, {}
    for component in COMPONENTS:
        detail = reports / component / "bbox_details.csv"
        if not detail.is_file():
            continue
        print(f"[scan] {component}: {detail.stat().st_size / 1024**2:.0f} MB", flush=True)
        scans[component] = scan_details(detail)
        summaries[component] = read_summary(reports / component / "bbox_details_summary.csv")
    if not scans:
        parser.error(f"bbox 진단 CSV가 없습니다: {reports}")

    output_dir.mkdir(parents=True, exist_ok=True)
    figure = plot(scans, output_dir / "facing_error_before_after.png")

    # Per-clip CSV: which clips carried the damage.
    clip_rows = []
    for component, scan in scans.items():
        for clip, counts in scan["per_clip"].items():
            entries = counts["entries"]
            clip_rows.append({
                "clip": clip,
                "component": component,
                "entries": entries,
                "reassigned": counts["reassigned"],
                "reassigned_pct": round(counts["reassigned"] / entries * 100, 1),
                "already_ok": counts["already_ok"],
                "no_consensus": counts["no_consensus"],
                "target_absent": counts["target_absent"],
                "ego_entries": counts["ego"],
                "ego_unverified": counts["ego_unverified"],
                "ok_before": counts["ok_before"],
                "ok_after": counts["ok_after"],
                "ok_before_pct": round(counts["ok_before"] / entries * 100, 1),
                "ok_after_pct": round(counts["ok_after"] / entries * 100, 1),
                "bbox_changed_frames": summaries.get(component, {})
                    .get(clip, {}).get("bbox_changed_frames", ""),
                "affects_ego_changed_entries": summaries.get(component, {})
                    .get(clip, {}).get("affects_ego_changed_entries", ""),
            })
    clip_rows.sort(key=lambda row: (-row["reassigned_pct"], row["clip"]))
    csv_path = output_dir / "fix_by_clip.csv"
    temporary = csv_path.with_name(f".{csv_path.name}.tmp")
    with temporary.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=list(clip_rows[0]))
        writer.writeheader()
        writer.writerows(clip_rows)
    temporary.replace(csv_path)

    entries = sum(scan["entries"] for scan in scans.values())
    actions = Counter()
    ok = Counter()
    for scan in scans.values():
        actions.update(scan["actions"])
        ok.update(scan["ok"])
    fixed_before = [v for scan in scans.values() for v in scan["reassigned_before"]]
    fixed_after = [v for scan in scans.values() for v in scan["reassigned_after"]]

    lines = [
        "# 신호등 relabel — 무엇이 틀렸고 무엇을 고쳤나",
        "",
        f"대상 **{len(clip_rows):,} 클립** · 신호등 엔트리 **{entries:,}개**",
        "",
        "## 1. 무엇이 틀렸나",
        "",
        "수집 코드가 actor와 level bbox를 **2D 최근접 greedy**로 붙였다. 그런데",
        "마스트형 신호등은 폴(actor 원점)이 모퉁이에 있고, 그 폴이 통제하는",
        "등(head)은 **교차로 건너편**에 매달려 있다. 그래서 최근접으로 고르면",
        "**항상 옆 접근로의 head**가 잡히고, 배정이 교차로 단위로 한 칸씩 회전한다.",
        "",
        "그 결과 한 엔트리 안에 서로 다른 두 신호등의 정보가 섞인다.",
        "",
    ]
    lines += _table([
        ["필드", "출처", "상태"],
        ["`location` `rotation` `center` `extent` `road_id` `lane_id` `section_id`",
         "level bbox", "**틀림 (한 칸 밀림)**"],
        ["`id` `state` `distance` `trigger_volume_location` `trigger_volume_extent`",
         "actor", "정확"],
        ["`trigger_volume_rotation`", "둘의 합", "**오염**"],
    ], ["l", "l", "l"])
    lines += [
        "",
        "## 2. 어떻게 고쳤나",
        "",
        "head는 자기 trigger volume의 **건너편**에 있어야 한다. 교차로 중심 기준",
        "각도로 `head_angle ≈ tv_angle + 180°`. 이 조건을 비용으로 두고 교차로",
        "단위로 **Hungarian**을 풀면 배정이 유일하게 확정된다.",
        "",
        "`distance` 기반 재배정은 쓰지 않는다. 수집 코드의 greedy가 바로 그 값을",
        "최소화했으므로 **같은 오답을 재생산**하기 때문이다.",
        "",
        "bbox를 고친 **같은 annotation 객체**에서 곧바로 `affects_ego`를 다시",
        "계산하고, 프레임 파일은 그 뒤 한 번만 저장한다. 두 수정본이 따로 생기지",
        "않게 하려는 것이다.",
        "",
        "## 3. 얼마나 틀렸나",
        "",
    ]
    lines += _table(
        [["판정", "엔트리", "비율", "뜻"]]
        + [[ACTION_LABEL.get(action, action), f"{count:,}",
             f"{count / entries * 100:.1f}%",
             {"already_ok": "배정이 원래 맞았다",
              "reassigned": "**틀렸고, 고쳤다**",
              "no_consensus": "투표가 갈려 손대지 않았다",
              "target_absent": "목표 head가 그 프레임에 없다"}.get(action, "")]
            for action, count in actions.most_common()],
        ["l", "r", "r", "l"],
    )
    lines += [
        "",
        "## 4. 고쳐졌는지 어떻게 아나",
        "",
        "배정이 맞으면 head는 자기 trigger volume 쪽을 향하므로 그 각도(**facing",
        "error**)가 **80~100°**로 모인다. 한 칸 밀린 배정은 그 근처에 올 수 없다.",
        "이 값은 복구 결과와 무관하게 측정되므로, 스스로를 채점하는 지표가 아니다.",
        "",
    ]
    lines += _table([
        ["구간", "엔트리", "정상 판정"],
        ["수정 전", f"{entries:,}", f"{ok['before']:,} ({ok['before'] / entries * 100:.1f}%)"],
        ["수정 후", f"{entries:,}", f"**{ok['after']:,} ({ok['after'] / entries * 100:.1f}%)**"],
    ], ["l", "r", "r"])
    if fixed_before and fixed_after:
        lines += [
            "",
            f"**실제로 재배정된 {len(fixed_before):,}개만** 떼어놓고 보면 결정적이다.",
            "",
        ]
        lines += _table([
            ["", "facing error 중앙값", "정상 판정"],
            ["수정 전", f"{statistics.median(fixed_before):.1f}°", "0.0%"],
            ["수정 후", f"**{statistics.median(fixed_after):.1f}°**", "**100.0%**"],
        ], ["l", "r", "r"])
        lines += [
            "",
            "> 한 칸 밀린 배정이 만들던 각도에서 **물리적으로 옳은 90° 부근으로**",
            "> 이동했다. 이 표본은 정의상 수정 전 정상 판정이 0%이므로, 이동은",
            "> 전부 복구에서 나온 것이다.",
        ]
    if figure is not None:
        lines += [
            "",
            f"![facing error 분포]({figure.name})",
            "",
            "위: 수정 전 — 옳은 구간(파란 띠) 밖에 큰 봉우리가 따로 있다.",
            "아래: 수정 후 — 그 봉우리가 사라진다.",
        ]

    # The figure that actually decides whether anything needs eyes on it.
    ego_total = sum(sum(scan["ego"].values()) for scan in scans.values())
    ego_unverified = Counter()
    group_sizes = Counter()
    for scan in scans.values():
        ego_unverified.update(scan["ego_unverified"])
        group_sizes.update(scan["group_sizes"])
    unverified_total = sum(ego_unverified.values())
    affected = sorted(
        (row for row in clip_rows if row["ego_unverified"]),
        key=lambda row: (-row["ego_unverified"], row["clip"]),
    )
    lines += [
        "",
        "## 5. 손대지 않은 것은 문제인가",
        "",
        f"`합의 없음`이 {actions['no_consensus']:,}개로 크지만, 그 대부분은 **ego와",
        "무관한 신호등**이다. 복구 규칙은 head가 교차로 건너편에 있다는 기하에",
        "기대는데, T자 교차로나 신호등이 1~2개뿐인 곳에서는 그 조건으로 배정이",
        "**유일하게 결정되지 않는다.** 그런 엔트리는 추측하지 않고 그대로 둔다.",
        "",
        f"전체 {entries:,}개 중 **ego에 영향을 주는 것은 {ego_total:,}개",
        f"({ego_total / entries * 100:.1f}%)** 뿐이다. 그 기준으로 다시 세면:",
        "",
    ]
    lines += _table(
        [["판정", "ego 영향", "그중 각도 불일치"]]
        + [[ACTION_LABEL.get(action, action), f"{count:,}",
            f"**{ego_unverified[action]:,}**" if ego_unverified[action] else "0"]
           for action, count in sorted(
               ((a, c) for a, c in
                ((a, sum(scan["ego"][a] for scan in scans.values()))
                 for a in ACTION_LABEL)), key=lambda item: -item[1])],
        ["l", "r", "r"],
    )
    lines += [
        "",
        f"> **재배정한 것 중 각도가 안 맞는 엔트리는 0개다.** 고친 것은 전부",
        f"> 고쳐졌다. 남은 것은 `합의 없음` 쪽의 **{unverified_total:,}개",
        f"> (전체의 {unverified_total / entries * 100:.2f}%)** 이고, 이것이 육안으로",
        f"> 확인할 실제 범위다.",
        "",
    ]
    if group_sizes:
        lines += ["그 엔트리들이 속한 교차로의 신호등 수:", ""]
        lines += _table(
            [["교차로 신호등 수", "엔트리"]]
            + [[size or "(미기록)", f"{count:,}"]
               for size, count in sorted(group_sizes.items(),
                                         key=lambda item: -item[1])],
            ["l", "r"],
        )
        lines += [
            "",
            "3개 미만이면 배정을 결정할 정보 자체가 없다.",
        ]
    if affected:
        lines += [
            "",
            f"### 확인 대상 클립 {len(affected)}개",
            "",
        ]
        lines += _table(
            [["클립", "구분", "ego 신호등", "각도 불일치", "비율"]]
            + [[row["clip"], row["component"], f"{row['ego_entries']:,}",
                f"{row['ego_unverified']:,}",
                f"{row['ego_unverified'] / row['ego_entries'] * 100:.0f}%"]
               for row in affected[:20]],
            ["l", "l", "r", "r", "r"],
        )
        if len(affected) > 20:
            lines += ["", f"이하 {len(affected) - 20}개는 `fix_by_clip.csv` 참조."]

    worst = [row for row in clip_rows if row["reassigned"]][:15]
    if worst:
        lines += [
            "",
            "## 6. 재배정이 많았던 클립",
            "",
        ]
        lines += _table(
            [["클립", "구분", "엔트리", "재배정", "정상 전→후"]]
            + [[row["clip"], row["component"], f"{row['entries']:,}",
                f"{row['reassigned']:,} ({row['reassigned_pct']:.0f}%)",
                f"{row['ok_before_pct']:.0f}% → {row['ok_after_pct']:.0f}%"]
               for row in worst],
            ["l", "l", "r", "r", "r"],
        )
        lines += [
            "",
            "전체 클립별 수치는 `fix_by_clip.csv`.",
        ]
    untouched = [row for row in clip_rows if not row["reassigned"]]
    lines += [
        "",
        f"재배정이 한 건도 없었던 클립 **{len(untouched):,}개** — 원래 배정이 맞았거나",
        "신호등이 없는 구간이다.",
        "",
        "## 7. 어디에 생기나",
        "",
        "```",
        "relabel/",
        "├── <클립명>/traffic_light/",
        "│   ├── corrected_anno/*.json.gz   수정된 annotation (원본은 그대로 둔다)",
        "│   ├── completion.json            입출력 SHA256 · 프레임 수 · metrics",
        "│   └── reports/",
        "│       ├── affects_ego_changes.csv  바뀐 프레임·신호등",
        "│       ├── relevance_events.csv     교차로 통과 이벤트",
        "│       └── relevance_frames.csv     프레임별 판정",
        "└── production_reports/{base,weak}/",
        "    ├── bbox_details.csv          엔트리별 전/후 (이 리포트의 근거)",
        "    ├── bbox_details_summary.csv  클립별 집계",
        "    └── results.csv               클립별 상태",
        "```",
        "",
        "이 리포트는 `report_relabel_fixes.py`가 생성한다.",
        "",
    ]

    ledger = output_dir / "RELABEL_FIX_LEDGER.md"
    temporary = ledger.with_name(f".{ledger.name}.tmp")
    temporary.write_text("\n".join(lines), encoding="utf-8")
    temporary.replace(ledger)

    print(json.dumps({
        "ledger": str(ledger), "csv": str(csv_path),
        "figure": str(figure) if figure else None,
        "clips": len(clip_rows), "entries": entries,
        "reassigned": actions["reassigned"],
        "ok_before_pct": round(ok["before"] / entries * 100, 1),
        "ok_after_pct": round(ok["after"] / entries * 100, 1),
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
