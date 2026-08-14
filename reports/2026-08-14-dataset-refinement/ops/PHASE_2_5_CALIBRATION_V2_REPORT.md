# Phase 2.5 Expert Driving Quality Gate — Calibration v2

- 작성 시각: 2026-08-13 (Asia/Seoul)
- 상태: `calibration_only` 완료, classification/relabel/PKL 미실행
- authoritative calibration: `quality_gate/calibration_v2`
- parent split SHA256: `448438d80b43886cd1d7d45730ee500033ae2c44e5b190ac9fcd5862f7433c65`

## 입력과 코드

- Bench detached worktree: `/home/ailab/AILabSSD/99_Management/2026_internship/kimminseong/production_1329_v1_code/Bench2Drive_dataset_manage`
  - parent HEAD: `1238eac83b044cf17dc43785c66047929441c887`
  - quality-gate 구현 및 Python 3.8 호환 수정이 아직 commit되지 않은 dirty/untracked 상태다.
- Internship detached worktree: `/home/ailab/AILabSSD/99_Management/2026_internship/kimminseong/production_1329_v1_code/2026-Summer-Internship`
  - parent HEAD: `bf42647640aea186508a8b056b5572c5a2b380f0`
  - quality-filtered clip union contract/test 변경이 아직 commit되지 않은 dirty 상태다.
- Base root: `/home/ailab/AILabDataset/01_Open_Dataset/41_Bench2Drive/Bench2Drive/unzip_data`
- Weak root: `/home/ailab/AILabDataset/03_Shared_Repository/2026-intern/Team_two/Scenario`
- Map root: `/home/ailab/AILabDataset/01_Open_Dataset/41_Bench2Drive/Bench2Drive-Map`
- release root: `/home/ailab/AILabDataset/03_Shared_Repository/2026-intern/Team_two/Bench2Drive_relabel/production_1329_corrected_v1`
- calibration config SHA256: `c039266198415f84ebec5be34d13ab53f6179aaf75784cde9b44e98e2f80232e`
- audit implementation SHA256: `1404c4b475f7767d344cbf11babf8380bb4fd8d97742a3770c41d00fdb67cceb`
- release contract SHA256: `8b594fdbacf2fac930ccc5cdd803f837ef1e669c11e7a2945798170d77d952b9`

실제 audit 명령은 `logs/quality-audit-calibration_v2.log` 첫 줄에 JSON 배열로 기록돼 있다.

```bash
python3 Driving_Quality_Filtering/run_quality_gated_pipeline.py \
  --stage quality-audit --calibration-version calibration_v2 \
  --manifest Weak_Scenario_Mining/data/bench2drive_base1000_weak329_train_val_split.json \
  --base-root /home/ailab/AILabDataset/01_Open_Dataset/41_Bench2Drive/Bench2Drive/unzip_data \
  --weak-root /home/ailab/AILabDataset/03_Shared_Repository/2026-intern/Team_two/Scenario \
  --map-root /home/ailab/AILabDataset/01_Open_Dataset/41_Bench2Drive/Bench2Drive-Map \
  --release-root /home/ailab/AILabDataset/03_Shared_Repository/2026-intern/Team_two/Bench2Drive_relabel/production_1329_corrected_v1 \
  --internship-root /home/ailab/AILabSSD/99_Management/2026_internship/kimminseong/production_1329_v1_code/2026-Summer-Internship \
  --workers 8
```

## 완성·무결성 hash

- clip count: `1329`
- frame count: `329901`
- component: Base `1000`, Weak `329`
- split: train `1262`, val `67`
- source annotation stat unchanged: `1329/1329`
- metrics SHA256: `94a149f11308184ec0e1898bfe5fa8570060c7981142d8704d3b655d1407fcc8`
- events SHA256: `9787421831da1ea2d39ef65d46cec050e36db8a16d11e6ce89786dd244eb57a2`
- completion SHA256: `6627c0fa0d10e17a14756e0a1e49a02b2ee382f57353797ef7c8db88ba40cec8`
- summary manifest SHA256: `9a6c5d1f7358db9ffa7e7573e983d17b56534d24793c5ed8f70146d64f3305af`
- audit log SHA256: `cc3ea5f6acec7f47cc036c8f2e65a1b00fb347eaa4558562c19330349daadb1b`
- successful validation log SHA256: `c93fef0755b35da2277cbfcb0cc9bcedfbae7c27b5e5a1c29eb76d5fa99e9607`

## calibration-only 원시 결과

이 수치는 판정 결과가 아니다. `PASS`, `REVIEW`, `EXCLUDE`는 아직 한 건도 생성하지 않았다.

- 구조 오류 후보: `9` clips
  - Base `5`, Weak `4`; 모두 기존 train 소속
  - `EGO_STATE_INVALID`: 7 clips, 21 frames. 재확인 결과 21개 모두 top-level `theta=NaN`이다.
  - `MEDIA_MISSING`: 2 clips
    - `VanillaSignalizedTurnEncounterRedLight_Town12_Route15555_Weather10`: `depth_front_right` 102 frames 누락
    - `YieldToEmergencyVehicle_Town12_Route20796_Weather21`: `depth_front` 182 frames 누락
- 양의 oriented 3D overlap 후보: `191` clips, `5352` frame events
  - train `179`, val `12`
  - vehicle: `165` clips / `4376` frames
  - traffic_sign: `22` clips / `900` frames
  - walker: `4` clips / `76` frames
  - `traffic_sign`의 깊은 overlap이 전체 penetration 상위권을 지배하므로 collidable class 승인 전 자동 충돌로 취급하면 안 된다.
- map 파일 누락: `0` clips
- ego road/lane ID 누락 frame: `0`

구조 오류 후보 clip:

1. `AccidentTwoWays_Town12_Route1109_Weather9`
2. `BlockedIntersection_Town03_Route136_Weather6`
3. `NonSignalizedJunctionLeftTurnEnterFlow_Town12_Route1022_Weather8`
4. `NonSignalizedJunctionLeftTurn_Town12_Route1362_Weather15`
5. `OppositeVehicleRunningRedLight_Town13_Route590_Weather18`
6. `VanillaNonSignalizedTurnEncounterStopsign_Town12_Route16566_Weather7`
7. `VanillaNonSignalizedTurnEncounterStopsign_Town12_Route16691_Weather2`
8. `VanillaSignalizedTurnEncounterRedLight_Town12_Route15555_Weather10`
9. `YieldToEmergencyVehicle_Town12_Route20796_Weather21`

## 연속 지표 분포

아래는 clip별 최대값의 분포이며 cutoff가 아니다.

| metric | p50 | p95 | p99 | max |
|---|---:|---:|---:|---:|
| overlap run frames | 0 | 6 | 72.32 | 321 |
| penetration m, positive clips only | 0.1463 | 0.8282 | 1.5176 | 1.6375 |
| BEV IoU, positive clips only | 0.00226 | 0.13375 | 0.31244 | 0.31399 |
| 10 Hz step m | 2.9819 | 3.6068 | 3.8063 | 3.9707 |
| 10 Hz derived speed m/s | 29.8189 | 36.0675 | 38.0631 | 39.7067 |
| speed mismatch m/s | 26.4764 | 29.8189 | 29.8318 | 29.9109 |
| acceleration vector mismatch m/s2 | 440.7187 | 519.9118 | 527.1995 | 141622.2615 |
| exact-zero speed run frames | 0 | 120.6 | 384.16 | 468 |
| reverse run frames | 1 | 2 | 2 | 3 |
| action-control error | 1 | 1 | 1.0592 | 1.6022 |
| road transitions | 2 | 11 | 14 | 15 |

`HazardAtSideLaneTwoWays_Town12_Route1136_Weather18`에서 reported acceleration XY 최대값
`141406.7571 m/s2`가 발견됐다. 또한 top-level `x/y`의 10 Hz 단일 차분은 여러 정상-looking
frame에서도 1 frame에 2~3 m 흔들려 파생 speed/acceleration/jerk 최대값을 크게 만든다.
따라서 raw 최대값에 즉시 threshold를 적용하면 전수 오탐 위험이 있다. smoothing window,
robust derivative, 대체 ego 위치 사용 여부는 hyperparameter/contract 변경이므로 자동 결정하지 않았다.

## 구현한 gate

- calibration config로 classifier 실행 거부
- oriented BEV polygon + z-overlap, penetration, clearance, IoU, TTC 원시 지표
- top-level x/y 10 Hz step/speed/acceleration/jerk/yaw/curvature와 reported speed/acceleration 비교
- exact-zero speed 및 annotation `reverse` 연속 run
- expert 이전-frame NPZ 정렬과 action ID `0..38` 검증; latent/value 미사용
- structural EXCLUDE 우선순위, ambiguous REVIEW, unresolved REVIEW downstream 차단
- reviewer/reason/note/clip metrics hash 및 global metrics/events hash 결합
- filtered split = parent split과 accepted의 교집합, 기존 train/val 소속 유지, backfill 없음
- quality-filtered source extra를 명시된 excluded clip에만 허용
- 한 invocation에 한 stage만 허용하는 audit→evidence→classification→relabel→union→PKL runner

## 검증 결과

- Python: `3.8.10`
- Bench Scenario: `6/6`
- Bench production contract: `5/5`
- Weak split: `5/5`
- quality gate: `13/13`
- Internship data: `40/40`
- `compileall`: Bench/Internship PASS
- shell syntax: Bench/Internship PASS
- `git diff --check`: Bench/Internship PASS
- `build_production_split.py --check`: Base `950/50`, Weak `312/17`, combined `1262/67`
- actual roots: Base `1000`, Weak `329`, 교집합 `0`, missing/unexpected `0`
- calibration hash 재검증: `1329` clips, `5375` events, source unchanged `1329/1329`

성공 로그: `logs/phase2_5_validation_calibration_v2_retry1.log`.
최초 validation 호출은 실행 비트가 없어 test 시작 전 exit 126이었고,
`logs/phase2_5_validation_calibration_v2.log`에 그대로 보존했다.

## 제한과 다음 승인 지점

- 모든 annotation과 expert NPZ는 전 frame 열어 파싱했다.
- camera/lidar/radar는 모든 frame inventory와 non-zero size를 검사하고 stream별 first/middle/last
  signature를 검사했다. 모든 payload를 전수 decode한 것은 아니므로 깊은 sensor corruption까지
  완전히 배제했다고 말할 수 없다. 전수 signature 시도는 10 clips에 약 320초가 걸려 중단했으며
  `quality_gate/attempts/calibration_v1_full_signature_interrupted_20260813T1552`에 보존돼 있다.
- map 파일 및 road/lane ID는 모두 존재하지만 실제 lane topology 연속성은 아직 해석·판정하지
  않았다. 현재는 road/lane transition 원시 횟수만 제공한다.
- collidable class, collision/trajectory/review threshold, smoothing 방식은 모두 미승인이다.
- review evidence, classification, relabel, clip union, PKL은 실행하지 않았다. `relabel/`과 `pkl/`은 빈 stage 디렉터리다.

다음에 진행할 수 있는 단계는 threshold 경계 사례를 고르기 위한 `quality-evidence` 생성이다.
production classification과 Phase 3은 config 승인 및 모든 REVIEW 수동 확정 전까지 차단된다.
