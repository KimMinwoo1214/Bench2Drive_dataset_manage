# Phase 2.5 simplification, depth materialization, and push report

- Date: 2026-08-13 Asia/Seoul
- Phase 3: not started
- Classification: not started
- Calibration v3: blocked before execution
- Parent split SHA256: `448438d80b43886cd1d7d45730ee500033ae2c44e5b190ac9fcd5862f7433c65`

## Frozen quality-gate scope

- Removed ego position-differentiation metrics and decisions: derived speed, acceleration,
  jerk, yaw rate, curvature, stationary/reverse runs, and control mismatch.
- Collision geometry is evaluated only for normalized `vehicle`, `pedestrian`, and
  `bicycle` categories.
- A bicycle stored as raw class `vehicle` is identified through `base_type` or `type_id`.
- `traffic_sign` and all other actor classes are ignored by collision classification.
- Every positive oriented 3D bbox intersection remains calibration evidence, not a
  visually confirmed collision.
- Production classification requires separately approved review and exclude bands.
  Positive overlap below the review band is PASS; REVIEW is never auto-approved.

The previous calibration v2 contains 5,352 positive-overlap frame events across 191
clips. Filtering that historical evidence by raw dynamic classes gives 169 coordinate-only
candidate clips: vehicle 165, walker 4. The other 22 are traffic-sign-only clips and the
dynamic/sign clip intersection is zero. These counts are descriptive and threshold-free;
they are not confirmed collision counts.

## Weak depth patch preflight and blocked write

Patch root:

`/home/ailab/AILabDataset/03_Shared_Repository/2026-intern/Team_two/Bench2Drive_relabel/production_1329/depth_patch`

Canonical Weak root:

`/home/ailab/AILabDataset/03_Shared_Repository/2026-intern/Team_two/Scenario`

The versioned contract verified the two complete patch streams, PNG headers and dimensions,
and SHA256 equality for every already-existing canonical file. The canonical streams still
miss 284 files:

- `VanillaSignalizedTurnEncounterRedLight_Town12_Route15555_Weather10/camera/depth_front_right`: 102
- `YieldToEmergencyVehicle_Town12_Route20796_Weather21/camera/depth_front`: 182

The apply command attempted exclusive-create on the first missing destination and stopped
with `PermissionError`. The canonical directory resolves to the NFS mount and is owned by
`nobody:nogroup` with mode `drwxr-xr-x`. No depth file was copied, no existing file was
overwritten, the patch root was preserved, and no materialization manifest was created.

Command:

```bash
python3 Driving_Quality_Filtering/materialize_depth_patches.py \
  --weak-root /home/ailab/AILabDataset/03_Shared_Repository/2026-intern/Team_two/Scenario \
  --patch-root /home/ailab/AILabDataset/03_Shared_Repository/2026-intern/Team_two/Bench2Drive_relabel/production_1329/depth_patch \
  --output-manifest /home/ailab/AILabDataset/03_Shared_Repository/2026-intern/Team_two/Bench2Drive_relabel/production_1329_corrected_v1/quality_gate/depth_materialization_v1/manifest.json \
  --apply
```

Artifacts:

- Read-only post-failure preflight: `DEPTH_PREFLIGHT_CANONICAL_RO.log`
- Preflight log SHA256: `2e97784dbd3e296abd35c3532f115c0d998077e7e60e3d996ff225b950383027`
- Failed apply log: `/home/ailab/AILabDataset/03_Shared_Repository/2026-intern/Team_two/Bench2Drive_relabel/production_1329_corrected_v1/logs/depth-materialization-v1.log`
- Failed apply log SHA256: `7c0f6b078ff355c7480c245679b7eb9246580e34b3e464fa224b5b0c95beb339`

## Commits and remote integration

Bench local branch: `production-1329-quality-gate-v1`

- Python 3.8 compatibility: `765b1bf8f49f574c6b9585434f67679a1d1fe2bb`
- Expert-driving quality gate: `9ea545f48f1fc9077a44b05cc04d3f23bf248511`
- Latest fetched `origin/main`: `c431659457ca260425910a3bec42c091cb6d7445`
- Merge preserving latest main: `debf1465551c8efcd7e54c94ce5f9b5b0d2c8953`
- Required Bench production commit `ed9711f`: ancestor of local HEAD
- Push target: `main`
- Push result: success through the authenticated `minsungk02` GitHub account
- Verified remote `main`: `debf1465551c8efcd7e54c94ce5f9b5b0d2c8953`
- The isolated GitHub CLI login was removed after remote verification

Internship local branch: `data/production-1329-quality-filtered-v1`

- Filtered clip-union contract: `09ccee07a6720f4620d6d9d1e17dc1bd4ea2c4af`
- Latest fetched `origin/team_two`: `a660004e727f1f629e780168e588418ff048fe8f`
- Merge preserving latest team_two: `b601c1256ba873073b7db22b9efedd40569d6fbb`
- Required Internship production commit `d5621b1`: ancestor of pushed HEAD
- Push target: `team_two`
- Push result: success
- Verified remote `team_two`: `b601c1256ba873073b7db22b9efedd40569d6fbb`

Both worktrees were clean after commit, merge, validation, and push attempt.

## Validation

Targeted post-merge validation passed:

- Traffic-light relevance: 6/6
- Production contract: 5/5
- Weak split: 5/5
- Quality gate: 18/18
- Internship data: 40/40
- Python 3.8 compileall: Bench and Internship PASS
- Tracked shell syntax: PASS
- `git diff --check`: PASS
- Production split check: Base 950/50, Weak 312/17, combined 1262/67
- Live roots: Base 1000, Weak 329, intersection 0
- Validation log: `PHASE_2_5_TARGETED_POSTMERGE_VALIDATION.log`
- Validation log SHA256: `475ef48be38fd9fd60dd963a7131d4e3500ca153530c728663bdce50d6168788`

The newly fetched Bench `origin/main` also introduced four unrelated VAD test failures on
this Python 3.8 server: one use of `argparse.BooleanOptionalAction` and three
`visualize_vad_gt.py` geometry/API expectation failures. None of the failing VAD files are
changed by the quality-gate diff. The failures are preserved in
`PHASE_2_5_POSTMERGE_VALIDATION.log` with SHA256
`b76eb21eb68f9cab9b8548f46038065b592e1abcc5a0b072cd323f37fb5c86d6`.

## Required next actions

1. Grant a writable canonical Weak destination or have an authorized owner run the
   exact materialization command; rerun check-only and require `missing_total_before=0`.
2. Run a new non-overwriting `calibration_v3`, summarize it,
   and inspect representative/boundary videos before approving any production thresholds.
3. Do not start Phase 3 while calibration, threshold approval, classification, or REVIEW
   resolution remains incomplete.
