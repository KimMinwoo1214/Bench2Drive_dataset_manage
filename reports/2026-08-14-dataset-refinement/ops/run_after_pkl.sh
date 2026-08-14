#!/usr/bin/env bash
# Wait for the PKL pipeline, then finish the paperwork without supervision.
#
# The author leaves before the conversion ends, so whatever happens has to
# record itself. On success this writes the quality report, folds the PKL
# numbers into the bundle and commits. It does not push -- that is an outward
# action and stays a human decision. On failure it writes down what broke, in
# the same place, so the answer on return is in one file either way.

W=/home/ailab/AILabSSD/99_Management/2026_internship/kimminseong/production_1329_v1_code
R=/mnt/AILabDataset/03_Shared_Repository/2026-intern/Team_two/Bench2Drive_relabel/production_1329_corrected_v1
BUNDLE="$W/Bench2Drive_dataset_manage/reports/2026-08-14-dataset-refinement"
PIPELINE_LOG="$W/phase2_5_logs/PHASE4_6_PKL_20260814_v3.log"
STATUS="$BUNDLE/STATUS.md"

export PYTHONDONTWRITEBYTECODE=1

# Poll rather than wait: the pipeline was started by a different shell, so this
# one cannot wait on its pid.
while pgrep -f run_phase4_6_pkl.sh > /dev/null 2>&1; do sleep 60; done

stamp() { date -Is; }

if grep -aq "phase 4-6 done" "$PIPELINE_LOG" 2>/dev/null; then
    cd "$W/2026-Summer-Internship"
    python3 -m team_code.data.report_pkl_quality \
        --release-root "$R" \
        --out-dir "$R/pkl/quality_report_v1" \
        > "$W/phase2_5_logs/PKL_QUALITY_REPORT_RUN.log" 2>&1
    report_status=$?

    if [ $report_status -eq 0 ]; then
        cp "$R/pkl/quality_report_v1/PKL_QUALITY_REPORT.md" "$BUNDLE/06_PKL.md"
        mkdir -p "$BUNDLE/figures/07_pkl"
        cp "$R"/pkl/quality_report_v1/figures/*.png "$BUNDLE/figures/07_pkl/" 2>/dev/null
        cp "$R/pkl/quality_report_v1/metrics.json" "$BUNDLE/tables/pkl_metrics.json"
    fi

    {
        echo "# 오늘 마지막 상태"
        echo
        echo "생성 $(stamp)"
        echo
        echo "## PKL 파이프라인: 완료"
        echo
        echo '```'
        find "$R/pkl" -name "*.pkl" -printf "%-58p %10s bytes\n" 2>/dev/null | sed "s|$R/pkl/||"
        echo '```'
        echo
        for source in original_filtered_v1 corrected_filtered_v1; do
            for split in train val; do
                file="$R/pkl/$source/${split}_validation_report.json"
                [ -f "$file" ] || continue
                echo "- \`$source/$split\`: $(python3 -c "
import json,sys
d=json.load(open('$file'))
print(f\"status={d['status']} records={d['records']:,} errors={len(d['errors'])}\")" 2>/dev/null)"
            done
        done
        echo
        if [ $report_status -eq 0 ]; then
            echo "## 품질 리포트: 생성됨"
            echo
            echo "- \`06_PKL.md\` — 상태 분포·전이·마스크·무결성"
            echo "- \`figures/07_pkl/\` — 그림"
            echo "- \`tables/pkl_metrics.json\` — 수치"
        else
            echo "## 품질 리포트: 실패"
            echo
            echo '```'
            tail -20 "$W/phase2_5_logs/PKL_QUALITY_REPORT_RUN.log"
            echo '```'
        fi
    } > "$STATUS"
else
    {
        echo "# 오늘 마지막 상태"
        echo
        echo "생성 $(stamp)"
        echo
        echo "## PKL 파이프라인: 실패 또는 중단"
        echo
        echo "마지막 단계와 오류:"
        echo
        echo '```'
        grep -aoE "^=== [a-z0-9 -]+" "$PIPELINE_LOG" | tail -3
        echo "---"
        grep -aE "Error|error:|Traceback|EXIT_CODE" "$PIPELINE_LOG" | tail -8
        echo '```'
        echo
        echo "해당 단계 로그: \`$R/logs/\` 아래 \`<stage>-overlay-fix2.log\`"
        echo
        echo "재개하려면 \`--run-label\` 만 새로 주고 다시 실행하면 된다:"
        echo
        echo '```bash'
        echo "cd $W/phase2_5_logs && ./run_phase4_6_pkl.sh"
        echo '```'
    } > "$STATUS"
fi

cd "$W/Bench2Drive_dataset_manage"
git add -A reports/ 2>/dev/null
git -c user.name=ailab-hanyang-bot \
    -c user.email=ailab-hanyang-bot@users.noreply.github.com \
    commit -q -m "docs: record how the PKL run ended

Written unattended after the conversion finished, so the outcome is in the
bundle rather than only in a log on the server.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>" 2>/dev/null

echo "완료 $(stamp)"
