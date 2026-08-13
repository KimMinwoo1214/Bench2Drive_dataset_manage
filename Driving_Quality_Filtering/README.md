# Bench2Drive Expert Driving Quality Gate

이 디렉터리는 expert 주행의 파일 무결성과 회전 3D bbox 접촉을 측정한다.
신호등 relabel보다 먼저 실행하며 `REVIEW`는 사람의
명시적 결정 없이는 다음 단계로 넘어가지 않는다.

## 판정 경계

- `audit_expert_driving.py`는 calibration 전용이다. 1,329개 연속 지표와 모든 양의
  3D overlap을 기록하지만 `PASS/REVIEW/EXCLUDE`를 만들지 않는다. 이 overlap은
  좌표상 회전 BEV 교차와 z 중첩이 동시에 양수라는 뜻이며, 실제 영상 충돌 확정 수가 아니다.
- 충돌 대상은 정규화된 `vehicle`, `pedestrian`, `bicycle` 세 범주뿐이다. 자전거는
  raw `vehicle`이어도 `base_type` 또는 `type_id`로 분리하며 `traffic_sign` 등은 제외한다.
- 센서 stream은 모든 frame 이름과 non-zero size를 전수검사하고 각 stream의 첫·중간·
  마지막 파일 signature를 읽는다. 판정 원본인 annotation과 expert NPZ는 전 프레임을
  실제로 읽고 검증한다. 이 정책 문자열도 config와 run hash에 포함된다.
- `classify_quality.py`는 `mode=production`이고 review/exclude collision band가 모두
  채워진 config만 허용한다. calibration config를 넘기면 즉시 실패한다.
- 위치 미분 속도·가속도·jerk·yaw-rate 등 ego 궤적 판정은 이 gate에서 사용하지 않는다.
- 위 궤적 판정 제거에 따라 top-level `x`/`y`/`theta`는 어떤 판정에도 쓰이지 않는다.
  이 세 필드의 비유한값은 `EGO_STATE_NONFINITE`(severity `note`)로 기록하고
  `nonfinite_ego_state_frames`로 집계할 뿐, structural fatal이 아니며 해당 프레임의
  bbox 충돌 검사를 중단하지도 않는다. 반면 실제 판정에 쓰는 bbox·transform·extent,
  sensor/calibration, expert action의 비유한값은 그대로 structural fatal이다.
- 자동 `EXCLUDE`는 구조 파손과 승인된 지속/심각 충돌 기준에만 사용한다. 양의 overlap
  전부를 사람이 보는 것이 아니라, 승인된 review band 이상만 `REVIEW`로 보낸다.
- 사람 결정은 audit 전체 metrics/events hash와 clip metrics hash에 묶인다. audit이나
  코드가 바뀌면 이전 결정은 stale로 거절된다.
- filtered split은 부모 train/val 소속을 유지하고 제외만 한다. val backfill은 없다.

## Calibration 실행

실제 production은 한 번에 한 stage만 허용하는 runner를 사용한다. 아래 공통 경로를
각 명령에 동일하게 전달한다.

```bash
BENCH=/path/to/detached/Bench2Drive_dataset_manage
INTERN=/path/to/detached/2026-Summer-Internship
SPLIT="$BENCH/Weak_Scenario_Mining/data/bench2drive_base1000_weak329_train_val_split.json"
BASE=/read-only/base/root
WEAK=/read-only/weak/root
MAP=/read-only/map/root
RELEASE=/versioned/production_1329_corrected_v1

python3 "$BENCH/Driving_Quality_Filtering/run_quality_gated_pipeline.py" \
  --stage quality-audit \
  --manifest "$SPLIT" --base-root "$BASE" --weak-root "$WEAK" \
  --map-root "$MAP" --release-root "$RELEASE" \
  --internship-root "$INTERN" --workers 8
```

같은 release에서 metric schema를 확장해 calibration을 다시 수행할 때는 기존 결과를
덮어쓰지 않고 `--calibration-version calibration_v2`처럼 새 tag를 명시한다. 이후
summary, evidence, classification도 같은 version을 명시해야 한다.

산출물은 다음과 같다.

- `clip_metrics.csv`: 1,329개 정렬 가능한 clip 요약
- `events.csv`: 구조 문제와 모든 양의 3D overlap
- `clips/<clip>.json.gz`: frame timeline, event, clip metric 원본
- `index.html`: lightweight sortable report
- `completion.json`: 입력·코드·config 및 metrics/events SHA256
- `run_manifest.json`: commit, dirty 상태, 명령, 입력 절대경로

동일 output은 덮어쓰지 않는다. 중단 후 입력·code·config contract가 같을 때만 audit
script의 `--resume`을 직접 사용한다.

## Weak depth patch를 canonical stream에 반영

알려진 두 Weak stream은 `depth_patch`의 모든 파일과 기존 canonical 공통 파일의 SHA256이
같아야 한다. `materialize_depth_patches.py`는 versioned contract에 명시된 누락 파일만
exclusive-create로 복사하며 기존 파일과 patch root를 덮어쓰거나 삭제하지 않는다.

```bash
python3 "$BENCH/Driving_Quality_Filtering/materialize_depth_patches.py" \
  --weak-root "$WEAK" --patch-root /path/to/depth_patch

python3 "$BENCH/Driving_Quality_Filtering/materialize_depth_patches.py" \
  --weak-root "$WEAK" --patch-root /path/to/depth_patch \
  --output-manifest "$RELEASE/quality_gate/depth_materialization_v1/manifest.json" \
  --apply
```

첫 명령은 preflight만 수행한다. `--apply`는 contract와 기존/patch SHA256, PNG header와
크기를 모두 확인한 뒤 누락분만 채우고 postflight manifest를 남긴다.

audit 완료 뒤 아래 명령은 completion, clip별 metric, 전체 metrics/events hash를 다시
검사하고 threshold 없는 분포·전체 순위를 추가한다. 기존 파일이나 이미 존재하는 요약은
덮어쓰지 않는다.

```bash
python3 "$BENCH/Driving_Quality_Filtering/summarize_calibration.py" \
  --manifest "$SPLIT" --audit-dir "$RELEASE/quality_gate/calibration_v1"
```

- `distribution_summary.json`: 전체 및 Base/Weak·scenario·town별 분포와 분위수
- `metric_rankings.csv`: 각 연속 지표의 값이 있는 모든 clip 순위; cutoff 없음
- `clip_metrics.jsonl`, `events.jsonl`: machine-readable 원시 증거
- `summary_manifest.json`: audit completion과 모든 요약 파일의 SHA256 결합
- `CALIBRATION_REPORT.md`: calibration-only 중단 지점 요약

## Threshold 검토와 시각 증거

`clip_metrics.csv`와 `events.csv`에서 상위값뿐 아니라 경계값, Base/Weak, scenario,
town 표본을 선택해 한 줄당 clip 하나인 파일을 만든다. 선택 없이 모든 영상을
자동 생성하지 않는다.

```bash
python3 "$BENCH/Driving_Quality_Filtering/run_quality_gated_pipeline.py" \
  --stage quality-evidence --clip-list /path/to/review_clips.txt \
  --manifest "$SPLIT" --base-root "$BASE" --weak-root "$WEAK" \
  --map-root "$MAP" --release-root "$RELEASE" --internship-root "$INTERN"
```

기존 Scenario renderer를 재사용해 event ±20 frame의 front/TOP_DOWN bbox, intersection
overlay, overlap/penetration/IoU timeline, contact sheet, MP4, HTML index를 만든다. 이는 증거일 뿐 승인 파일을
자동 생성하지 않는다.

검토 후 `quality_config_production.template.json`의 빈 값을 실측 근거로 채워 별도
versioned config로 고정한다. 임의 기본 threshold는 제공하지 않는다.

## Classification과 REVIEW 결정

사람 결정 없이 classification하면 `classification_candidates_v1`에 review queue가
생기며 relabel gate는 계속 닫혀 있다.

```bash
python3 "$BENCH/Driving_Quality_Filtering/run_quality_gated_pipeline.py" \
  --stage quality-classify --quality-config /approved/quality_config_v1.json \
  --manifest "$SPLIT" --base-root "$BASE" --weak-root "$WEAK" \
  --map-root "$MAP" --release-root "$RELEASE" --internship-root "$INTERN"
```

`review_decisions.template.json` 형식으로 모든 REVIEW를 결정한 뒤 같은 stage를 새
`classification_v1`에 실행한다. `accepted + excluded + unresolved = parent total`, 집합
교집합 0, `unresolved=0`일 때만 filtered split을 쓴다.

## 이후 명시적 단계

runner에는 다음 stage가 있으나 별도 사용자 승인 없이 연속 실행하지 않는다.

```text
relabel-base → relabel-weak → relabel-check
→ clip-union → clip-union-check
→ pkl-original → pkl-corrected → validate-pkls
```

각 호출은 이전 completion과 hash를 검사하고 고유 log를 남긴다. original/corrected
PKL은 동일 filtered split과 union을 사용한다. `validate-pkls`에는 실제 VAD가
`v1/<clip>`을 해석하는 `--runtime-data-root`가 추가로 필요하다.

## 테스트

```bash
cd "$BENCH/Driving_Quality_Filtering"
python3 -m unittest -v test_quality_gate.py
```

geometry 테스트는 회전 비충돌, 경계 접촉, 얕은/깊은 관통, z 분리 고가도로를 포함한다.
audit 테스트는 actor 3종 정규화와 frame 0의 `-0001.npz`, 이후 이전-frame expert action
정렬을 검사한다. depth materialization 테스트는 기존 파일 불변과 누락분만 복사를 검사한다.
