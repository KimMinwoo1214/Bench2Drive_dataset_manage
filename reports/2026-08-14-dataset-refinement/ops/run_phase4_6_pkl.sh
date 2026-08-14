#!/usr/bin/env bash
# Phase 4-6: verify the relabel, build the clip union, write both PKL sets,
# then validate all four.
#
# Runs strictly in order and stops on the first failure. Every stage refuses
# to overwrite what a previous run produced, so a stop leaves the release in
# a state you can inspect rather than one you have to unpick.
set -euo pipefail

W=/home/ailab/AILabSSD/99_Management/2026_internship/kimminseong/production_1329_v1_code
B="$W/Bench2Drive_dataset_manage"
R=/mnt/AILabDataset/03_Shared_Repository/2026-intern/Team_two/Bench2Drive_relabel/production_1329_corrected_v1
RUNTIME=/home/ailab/AILabDataset/03_Shared_Repository/2026-intern/Team_two/Bench2Drive_relabel/production_1329/clips

export PYTHONDONTWRITEBYTECODE=1

run() {
    echo ""
    echo "=== $1  $(date -Is) ==="
    python3 "$B/Driving_Quality_Filtering/run_quality_gated_pipeline.py" \
        --stage "$1" \
        --manifest "$B/Weak_Scenario_Mining/data/bench2drive_base1000_weak329_train_val_split.json" \
        --base-root /mnt/AILabDataset/01_Open_Dataset/41_Bench2Drive/Bench2Drive/unzip_data \
        --weak-root "$R/inputs/weak_root_patched_v1" \
        --map-root /mnt/AILabDataset/01_Open_Dataset/41_Bench2Drive/Bench2Drive-Map \
        --release-root "$R" \
        --internship-root "$W/2026-Summer-Internship" \
        --calibration-version calibration_v4 \
        --runtime-data-root "$RUNTIME" \
        --workers 12 \
        --review-approvals "$R/relabel/REVIEW_v3/approvals_v1.json" \
        --run-label overlay-fix2 \
        "${@:2}"
}

# The relabel wrote annotations; this re-reads them and checks each clip's
# completion against its inputs before anything downstream trusts them.
run relabel-check

# One flat symlink tree over exactly the clips the filtered split keeps, then
# the same check again with nothing allowed to change.
run clip-union
run clip-union-check

# Two PKL sets over the identical union and split: one from the annotations as
# collected, one from the relabelled ones. Anything that differs between them
# other than traffic lights is a bug, which is what the validators look for.
run pkl-original
run pkl-corrected

run validate-pkls

echo ""
echo "=== phase 4-6 done  $(date -Is) ==="
