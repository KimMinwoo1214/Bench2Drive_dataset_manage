# 2026-08-13 Base1000 + Weak329 corrected production TODO

이 문서는 `Base1000 + Weak329`에서 expert-driving 품질 검사를 먼저 끝내고, 승인된 클립만 traffic-light relabel한 뒤 `original-filtered`와 `corrected-filtered` PKL을 만들고 검증하기 위한 실행 체크리스트다.

## 0. 현재 결론

**Phase 2.5 전체는 아직 완료되지 않았다.** 완료된 것은 Phase 2.5용 코드 구현·단위 테스트·저장소 push까지다. 실제 데이터 1,329개에 대한 최종 `PASS / REVIEW / EXCLUDE`, 수동 REVIEW 확정, filtered split 생성은 아직 남아 있다.

| 구간 | 현재 상태 | 완료 의미 |
|---|---|---|
| Phase 0~1 | 완료 | 기존 학습과 격리된 detached worktree 확보 |
| Phase 2 | 완료 | production 코드 기본 테스트 통과 |
| Phase 2.5-A | 완료 | quality gate 구현, 단순화, 테스트, Bench/Internship push |
| Phase 2.5-B | 완료 (push 대기) | Python 3.8·재실행·무덮어쓰기 등 production readiness 보강 |
| Phase 2.5-C | 완료 (방식 변경) | Weak depth 284개를 **canonical 복사가 아니라 patched overlay**로 해결 |
| Phase 2.5-D | 완료 | **`calibration_v4`**로 1,329개 재검사, structural fatal 0 |
| Phase 2.5-E | 진행 중 | **전수조사 완료(36클립 검토 대상)**, 영상 렌더링 중, threshold 미승인 |
| Phase 2.5-F | 미실행 | REVIEW 수동 확정 및 filtered split 생성 |
| Phase 3 | 미실행 | accepted clip만 traffic-light relabel 및 검증 |
| Phase 4 | 미실행 | accepted clip union 생성 및 검증 |
| Phase 5 | 미실행 | original/corrected filtered PKL 생성 |
| Phase 6 | 미실행 | PKL 쌍 검증, loader smoke, checksum, 최종 보고 |

따라서 실제 흐름은 다음과 같다.

```text
2.5 readiness 보강                      [완료, push 대기]
  -> depth 284개 patched overlay 해결     [완료]
  -> calibration_v4 1,329개 재검사        [완료]
  -> 시각 증거/threshold 승인             [진행 중]
  -> REVIEW 수동 확정
  -> filtered split 생성
  -> Phase 3 relabel
  -> clip union
  -> original/corrected PKL
  -> 최종 validator 및 runtime smoke
```

`PKL 파일이 생성됨`은 완료 조건이 아니다. 네 split PKL의 validator, 원본/수정본 순서 동일성, 허용 필드 외 무변경, runtime path 해석, checksum과 최종 보고까지 끝나야 corrected production 완료다.

## 1. 절대 지킬 안전 규칙

- 현재 학습 job, 기존 checkout, Zoo copy, 기존 PKL, 학습 output은 수정하거나 재시작하지 않는다.
- 작업은 아래 두 versioned worktree에서만 한다.
- Base/Weak 원본 annotation은 항상 read-only다.
- 기존 `production_1329/clips`는 현재 학습에서 사용할 가능성이 있으므로 읽기 전용 runtime 검증에만 쓴다.
- 기존 PKL, dataset, symlink, relabel output, 로그를 삭제하거나 덮어쓰지 않는다.
- `git reset`, `git clean`, `rm`은 사용하지 않는다.
- 기존 로그가 있으면 새 run label을 사용한다. 성공 승인된 이전 로그 삭제는 별도 승인 후에만 한다.
- REVIEW는 자동 승인하지 않는다. reviewer, reason code, note, metrics/events hash를 남긴다.
- collision threshold와 hyperparameter는 실측 증거와 사용자 승인 전에는 production config에 넣지 않는다.
- 각 단계는 completion과 검증 결과를 요약하고 멈춘다. 다음 단계는 사용자 지시 후 실행한다.
- 명령, code commit, split/config/decision hash, 입력·출력 경로, 시작·종료 시각, exit code를 남긴다.
- 새 경로나 output directory가 이미 존재하면서 contract가 다르면 덮어쓰지 않고 중단한다.

## 2. 고정 경로와 현재 기준점

명령 실행 전 각 셸에서 다음 값만 사용한다. `$HOME` 같은 공용 시스템 변수는 재정의하지 않는다.

```bash
WORK=/home/ailab/AILabSSD/99_Management/2026_internship/kimminseong/production_1329_v1_code
BENCH="$WORK/Bench2Drive_dataset_manage"
INTERN="$WORK/2026-Summer-Internship"
BASE=/home/ailab/AILabDataset/01_Open_Dataset/41_Bench2Drive/Bench2Drive/unzip_data
# canonical Weak (read-only, 여전히 depth 284개 누락)
WEAK_CANONICAL=/home/ailab/AILabDataset/03_Shared_Repository/2026-intern/Team_two/Scenario
# 이후 모든 stage가 쓰는 Weak root = patched overlay
WEAK="$RELEASE/inputs/weak_root_patched_v1"
MAP=/home/ailab/AILabDataset/01_Open_Dataset/41_Bench2Drive/Bench2Drive-Map
SPLIT="$BENCH/Weak_Scenario_Mining/data/bench2drive_base1000_weak329_train_val_split.json"
RELEASE=/home/ailab/AILabDataset/03_Shared_Repository/2026-intern/Team_two/Bench2Drive_relabel/production_1329_corrected_v1
PATCH=/home/ailab/AILabDataset/03_Shared_Repository/2026-intern/Team_two/Bench2Drive_relabel/production_1329/depth_patch
RUNTIME=/home/ailab/AILabDataset/03_Shared_Repository/2026-intern/Team_two/Bench2Drive_relabel/production_1329/clips
RUNNER="$BENCH/Driving_Quality_Filtering/run_quality_gated_pipeline.py"
export PYTHONDONTWRITEBYTECODE=1
```

고정 provenance:

- Bench worktree: `production-1329-quality-gate-v1`
- Bench 현재 선택 commit: `85f8c73` (origin/main `debf146` + **미push 7개**)
- Bench 필수 production commit 포함: `ed9711f`
- Internship worktree: `data/production-1329-quality-filtered-v1`
- Internship 현재 선택 commit: `f8f38d7` (origin/team_two `a660004` + **미push 4개**, 그중 오늘 2개)
- Internship 필수 PKL commit 포함: `d5621b1`
- parent split SHA256: `448438d80b43886cd1d7d45730ee500033ae2c44e5b190ac9fcd5862f7433c65`
- parent split: Base `950/50`, Weak `312/17`, combined `1262/67`
- 현재 디스크 관측값: SSD 약 `1.1T` 여유, shared NFS 약 `8.3T` 여유. 각 대용량 단계 직전에 다시 확인한다.

`$RUNTIME`에는 1,329개 `v1/<clip>` wrapper와 두 Weak depth patch가 이미 해석된다. 이 경로는 최종 validator에서 **읽기 전용**으로 사용하며 여기에 새 symlink를 만들지 않는다. Phase 2.5-C의 `$WEAK` overlay는 이것과 별개로 `$RELEASE/inputs/` 아래에 새로 만든 것이다.

## 3. 오늘의 승인 지점

각 항목은 독립 phase다. 한 항목이 통과했다고 다음 항목을 자동 실행하지 않는다.

- [x] Phase 2.5-B production readiness 수정·전체 테스트 (push는 남음)
- [x] Phase 2.5-C depth 해결·검증 (patched overlay 방식으로 변경)
- [x] Phase 2.5-D calibration_v4 1,329개 완료
- [ ] Phase 2.5-E 시각 증거·quality config v1 threshold 승인
- [ ] Phase 2.5-F 수동 REVIEW·filtered split 완료 후 승인 — 이때 Phase 2.5 전체 완료
- [ ] Phase 3 Base relabel 후 승인
- [ ] Phase 3 Weak relabel 후 승인
- [ ] Phase 3 relabel check 후 승인
- [ ] Phase 4 clip union/check 후 승인
- [ ] Phase 5 original-filtered PKL 후 승인
- [ ] Phase 5 corrected-filtered PKL 후 승인
- [ ] Phase 6 최종 validation/runtime smoke/checksum 후 완료 승인

---

## Phase 2.5-B — Production readiness 보강

### 왜 depth보다 먼저 하는가

depth 권한만 해결해도 현재 최신 코드 그대로는 Python 3.8 서버에서 Phase 3 parser와 PKL converter가 실패한다. 또한 같은 단계를 재실행할 때 로그명이 충돌하고, PKL output이 이미 있으면 덮어쓸 수 있다. 실제 데이터 장시간 작업 전에 이 부분을 먼저 고친다.

### Bench 수정 범위

1. Python 3.8 호환
   - `Scenario_Filtering/run_scenario_pipeline.py`의 `argparse.BooleanOptionalAction` 제거
   - `Scenario_Filtering/read_json_gz.py`의 동일 사용 제거
   - 기존 `--visualization/--no-visualization` 등 CLI 의미는 유지

2. Phase 3 기본 실행을 headless로 고정
   - production runner가 relabel command에 다음을 명시적으로 전달하게 한다.
   - `--resume --no-visualization --no-video --no-vector-map --no-vad-vector-gt`
   - quality REVIEW evidence는 Phase 2.5-E에서 별도로 만들므로 relabel 전 클립 전체 영상은 만들지 않는다.

3. 재실행 provenance 분리
   - quality classification의 `candidate`와 `final`이 서로 다른 output/log label을 사용하게 한다.
   - relabel의 최초 실행, approval 적용 resume, check-only가 서로 다른 log label을 사용하게 한다.
   - 같은 label의 기존 로그가 있으면 현재처럼 덮어쓰지 않고 중단한다.

4. annotation finite-value 정책 고정
   - 현재 7개 클립의 top-level `theta=NaN`은 audit에서 structural fatal이 되어 bbox collision 검사까지 중단할 수 있다.
   - 반면 현재 PKL converter는 `theta=NaN`을 `pi`로 보정한다.
   - 사용자가 궤적 미분 판정을 제거했으므로 권장 정책은 **top-level x/y/theta를 자동 EXCLUDE 또는 collision audit 중단 근거로 사용하지 않고**, 실제 collision/sensor/expert 판정에 쓰는 bbox·transform·extent·sensor/calibration·expert action 필드의 finite 검사는 유지하는 것이다.
   - 이 좁은 정책 변경을 테스트와 contract에 명시하고 승인받는다. 승인하지 않으면 7개는 기존 structural-fatal 정책으로 자동 EXCLUDE한다. 임의로 결정하지 않는다.

5. 최신 Bench 전체 테스트 정리
   - 현재 latest `main`의 VAD 관련 테스트 4개가 Python 3.8/API·geometry 문제로 실패한다.
   - production Phase 3에서는 `--no-vad-vector-gt`로 해당 경로를 실행하지 않는다.
   - 그래도 전체 suite를 green으로 만들 수 있으면 함께 수정한다. 당일 범위를 벗어나면 `VAD vector GT disabled/not certified`를 명시하고, relabel production 경로 테스트는 반드시 전부 통과시킨다.

### Internship 수정 범위

1. `team_code/data/prepare_b2d_infos.py`의 `argparse.BooleanOptionalAction`을 Python 3.8 호환 플래그 쌍으로 교체한다.
2. PKL output hardening
   - output directory 또는 대상 `b2d_infos_train.pkl`, `b2d_infos_val.pkl`, `b2d_map_infos.pkl`가 있으면 덮어쓰지 않고 중단한다.
   - 각 파일은 temporary file에 완전히 쓴 뒤 atomic rename한다.
   - 명령, code commit, split/config/source hash, record count, 파일 SHA256을 completion/run manifest에 남긴다.
3. original/corrected paired validation 강화
   - 단순 key set 일치가 아니라 `(folder, frame_idx)` **순서와 개수**가 정확히 같은지 검사한다.
   - 허용된 traffic-light 필드 외 값이 달라지면 실패한다.
   - train/val 교집합 0, map PKL 동일성, path resolution을 유지한다.

### 테스트 및 완료 조건

- [ ] Bench Phase 2 테스트 전체 통과
- [ ] Bench quality gate 테스트 18개 전체 통과
- [ ] Bench Scenario production 경로 테스트 전체 통과
- [ ] VAD 4개는 수정 후 통과하거나 `--no-vad-vector-gt` 비활성 contract와 미인증 사유 기록
- [ ] Internship data unit test 40개 전체 통과
- [ ] Python 3.8에서 각 parser `--help` 성공
- [ ] `compileall` 성공
- [ ] shell script `bash -n` 성공
- [ ] 두 저장소 `git diff --check` 성공
- [ ] `build_production_split.py --check` 성공
- [ ] 실제 root에서 Base 1000, Weak 329, 교집합 0 재확인
- [ ] split Base 950/50, Weak 312/17, combined 1262/67 재확인
- [ ] 호환성/quality runner/PKL hardening 변경을 논리적으로 분리해 commit
- [ ] 기존 checkout이나 target branch에 직접 push하지 않고 feature branch push
- [ ] 테스트 로그와 readiness 보고서 생성

완료 후 결과를 요약하고 멈춘다.

---

## Phase 2.5-C — Weak depth 284개 (완료, 방식 변경)

### 결론: canonical에 쓰지 않고 patched overlay로 해결했다

누락은 전체 depth 폴더가 아니라 아래 두 Weak clip의 특정 프레임이다. 경로는
문서 초안과 달리 `<clip>/camera/<stream>` 아래다.

- `VanillaSignalizedTurnEncounterRedLight_Town12_Route15555_Weather10/camera/depth_front_right`: 102개
- `YieldToEmergencyVehicle_Town12_Route20796_Weather21/camera/depth_front`: 182개

**canonical 쓰기는 불가능하다.** `$WEAK` 아래 329개 clip이 전부 `root:root`
`drwxr-xr-x`이고, NFS(`sec=sys`) 위라 `fuse-overlayfs`/`bindfs`/`unshare -r`
어느 것으로도 우회되지 않으며 sudo 암호가 없다.

**그런데 canonical에 쓸 필요가 없었다.** patch root는 두 stream의 완전한
superset(608/608, 774/774)이고, 겹치는 파일은 SHA256 불일치 0건이다. 따라서
ailab 소유 symlink tree를 만들어 두 stream만 patch로 연결하면 audit은 보완된
canonical과 동일한 바이트를 읽는다.

### 실제로 한 것

```bash
python3 "$BENCH/Driving_Quality_Filtering/build_patched_weak_root.py" \
  --weak-root "$WEAK" --patch-root "$PATCH" \
  --output-root "$RELEASE/inputs/weak_root_patched_v1" \
  --output-manifest "$RELEASE/quality_gate/depth_materialization_v1/patched_weak_root_manifest.json"
```

- 327개 clip은 canonical로 향하는 단순 symlink, 문제의 2개만 stream별 symlink
- 검증: patched root 기준 `missing_total_before=0`, canonical 기준 **여전히 284**
  (= 원본 무수정 증명), 구조 검증 전 항목 통과
- **canonical dataset은 지금도 284개가 누락된 상태다.** 나중에 dataset owner가
  `run_phase2_5c_depth_apply.sh`를 sudo로 실행하면 실제 보완이 가능하다.

### 이후 모든 stage의 `$WEAK`는 patched root를 쓴다

```bash
WEAK="$RELEASE/inputs/weak_root_patched_v1"
```

`release_contract.json`의 `weak_root`를 이 경로로 갱신했고, 이전 값·변경 사유·
증거 hash를 `$RELEASE/release_contract_change_v1.json`에 남겼다.

### 실행 전 preflight

```bash
python3 "$BENCH/Driving_Quality_Filtering/materialize_depth_patches.py" \
  --weak-root "$WEAK" \
  --patch-root "$PATCH" \
  --output-manifest "$RELEASE/quality_gate/depth_materialization_v1/preflight_authorized.json"
```

필수 확인:

- patch 284개가 모두 존재하고 PNG로 읽힌다.
- source/target clip과 sensor가 contract와 정확히 일치한다.
- canonical에 이미 있는 파일은 내용이 동일하고 절대 덮어쓰지 않는다.
- authorized RW mount 또는 dataset owner 권한이 canonical 두 directory에만 적용된다.

### authorized apply

기존 실패 로그를 재사용하지 않는다. 로그 예:

`$RELEASE/logs/depth-materialization-v1-authorized-20260813.log`

```bash
python3 "$BENCH/Driving_Quality_Filtering/materialize_depth_patches.py" \
  --weak-root "$WEAK" \
  --patch-root "$PATCH" \
  --output-manifest "$RELEASE/quality_gate/depth_materialization_v1/manifest.json" \
  --apply
```

### 완료 조건 (전부 충족)

- [x] patched root 기준 `missing_total_before=0`
- [x] canonical 기준 여전히 `284` = 원본 무수정
- [x] 두 stream이 608/774 프레임 완전 해석
- [x] 겹치는 파일 SHA256 불일치 0건, patch 원본 보존
- [x] 다른 Weak 파일·annotation·symlink 변경 없음 (327개는 단순 symlink)
- [x] manifest와 명령/log/exit code 기록

---

## Phase 2.5-D — calibration_v4로 1,329개 재검사 (완료)

### calibration 버전 이력 — 기준은 v4다

| 버전 | 상태 |
|---|---|
| v1, v2 | 폐기. **v2는 커밋되지 않은 작업본으로 실행돼 재현 불가**하다. v2 보고서의 `structural 9개`는 신뢰할 수 없는 baseline이므로 인용하지 않는다 |
| v3 | 폐기. blanket `_finite_tree(annotation)` 스캔이 actor의 `brake=NaN`을 구조 파손으로 취급해 1,329개 중 **1,041개**를 fatal 처리했다. 증거로 보존 |
| **v4** | **현재 기준.** 위 버그 수정 + ego dynamics 측정 추가 |

v4 결과: `structural_fatal_clips=0`, `overlap_candidate_clips=169`,
`source_unchanged=1329`, verify `status=verified`.
`metrics_sha256=876f268ec873c9e04eeafb36b89ae24b2f3f0e888f70df6a292ef45fd4e00642`,
`events_sha256=66a9d2adef0f5afc5d2408433afcaaadd53e1d1b67e778f052bbc96a47f05cb0`,
`completion_sha256=829e8d791d4613ff62573a33b6ab6d6fce604d6b83d78f69fb7e1fa8910870a4`.

### 실행 (실제 사용한 명령)

```bash
python3 "$RUNNER" \
  --stage quality-audit \
  --manifest "$SPLIT" \
  --base-root "$BASE" \
  --weak-root "$WEAK" \
  --map-root "$MAP" \
  --release-root "$RELEASE" \
  --internship-root "$INTERN" \
  --calibration-version calibration_v4 \
  --workers 8
```

audit가 성공한 뒤 별도 로그로 summary와 artifact hash를 검증한다.

```bash
python3 "$BENCH/Driving_Quality_Filtering/summarize_calibration.py" \
  --manifest "$SPLIT" \
  --audit-dir "$RELEASE/quality_gate/calibration_v4"

python3 "$BENCH/Driving_Quality_Filtering/verify_calibration.py" \
  --manifest "$SPLIT" \
  --audit-dir "$RELEASE/quality_gate/calibration_v4"
```

### 완료 조건

- [x] manifest 1,329개가 정확히 한 번씩 완료
- [x] Base 1,000, Weak 329, 교집합 0
- [x] 모든 clip result가 completion metrics/events hash와 일치
- [x] depth 관련 `MEDIA_MISSING` 0
- [x] 충돌 actor 범위는 vehicle, pedestrian/walker, bicycle만 사용
- [x] AABB 단독이 아니라 yaw 적용 BEV overlap + z overlap + penetration/IoU/run length 기록
- [x] old `calibration_v1/v2/v3`를 수정하지 않고 `calibration_v4`에 새로 생성
- [x] Base/Weak·scenario·town 분포와 collision ranking CSV/JSONL/HTML 생성
- [x] 입력 annotation/dataset 변경 없음 확인

`calibration_v4`는 **측정 단계**다. 여기서 overlap candidate를 자동 충돌로 확정하거나 EXCLUDE하지 않는다. 요약 후 멈춘다.

---

## Phase 2.5-E — 시각 증거 검토와 threshold 승인

기존 calibration의 161개/169개 같은 수는 좌표상 3D overlap candidate 수이지, 사람이 영상으로 확인한 실제 충돌 수가 아니다. 1,329개 전체 영상을 만들거나 모든 candidate를 무조건 수동 검토하지 않는다.

### 판정 지표 전환 — 실측으로 폐기한 것과 채택한 것

CARLA는 강체 시뮬레이터라 충돌해도 찌그러지지 않고 튕긴다. 물리 엔진이 관통을
즉시 밀어내므로 **penetration 깊이는 심각도를 나타내지 못한다.** 대신 튕김이
남기는 **충격량**이 신호다. v4 실측으로 다음이 확인됐다.

**폐기한 지표 (전부 정상 주행과 구분 불가):**

| 지표 | 폐기 근거 |
|---|---|
| `penetration` | 정지 런 p50 `0.20` > 움직임 런 p50 `0.12`. 오히려 역전 |
| 원시 `yaw rate` | 상위가 전부 `NonSignalizedJunctionLeftTurn` 등 — 그냥 좌회전 |
| 원시 `가속도` | 전체 p90이 이미 `26.5`. `26.45/26.30/26.29` 반복 = 포화된 급제동 |
| 원시 `속도 강하` | `HardBreakRoute` 시나리오가 잡힘 — 정상 급제동 |

**채택한 2단 필터:**

1차 — 물리적 가능성. 정지 상태에서 충돌은 불가능하다.
`max(ego_speed, actor_speed) >= 1.0 m/s` → 206 런 중 **186 런 / 152 클립**.
제외된 정지 런 20개는 접촉 프레임 중앙값이 236개로, 클립 내내 주차차량 옆에
서 있던 경우다. (프레임 기준 4,452건 중 93%가 여기 해당했다.)

2차 — 충격 증거. **충돌은 운전자가 명령하지 않은 운동 변화다.** annotation의
`brake`/`steer` 조작 입력과 대조한다.

| 증거 | 제안 임계 (미승인) | 잡는 충돌 유형 |
|---|---|---|
| **A** 무명령 감속 (`brake<0.1`인데 감속) | `>= 2.5 m/s/frame` (25 m/s²) | 정면·추돌 |
| **B** 접촉 직후 에피소드 종료 | `tail <= 2` 이고 이동 중 | **측면 스침** |
| **C** 무명령 회전 (`steer<0.1`인데 회전) | `>= 0.05 rad/frame` | 스핀 |

**셋 중 하나라도 만족하면 후보** — 합집합 **15 클립**.
A만 쓰면 안 되는 이유: **측면 충돌은 감속을 만들지 않는다.** 감속 0.00에
시속 54km로 차선변경하다 옆차와 접촉하고 클립이 끝나는 케이스가 5개 있었고
(`LaneChange_*`), 육안으로 실제 측면 접촉임을 확인했다. 감속 기준으로만
순위를 매기면 이들이 186개 중 꼴찌로 밀려 리뷰 표본에 들어가지 못한다.

육안 1건 검증 완료: 무명령 감속 최상위
`VanillaSignalizedTurnEncounterRedLight_Town10HD` (7.79 m/s/frame ≈ 8g)는
front/TOP_DOWN 모두에서 **적신호 정차 차량 추돌**로 확인됐다.

### 전수조사로 전환 (2026-08-13 야간)

겹침 기반 후보 선정에는 두 가지 구조적 한계가 있었다.

1. **클립 중간 충돌을 놓친다.** CARLA leaderboard는 차량 충돌을 infraction으로
   기록만 하고 주행을 계속시킨다. 즉 대부분의 충돌은 클립 중간에 있고 클립
   길이는 그대로다. `EPISODE_ENDS_AT_CONTACT`만으로는 우연히 마지막 프레임에
   걸린 것만 잡힌다.
2. **측면 충돌을 놓친다.** 옆에서 긁히면 속도가 줄지 않아 감속 기준으로
   순위를 매기면 최하위로 밀린다. 실제로 시속 54km 차선변경 측면 접촉 5건이
   감속 0.00으로 리뷰 표본에서 빠져 있었다 (1건 육안 확인됨).

그래서 `sweep_collision_evidence.py`로 **1,329개 전 클립의 전 프레임**을 훑는
전수조사로 바꿨다. 핵심 판별 기준:

> **급제동은 ego만 변한다. 주차차량 겹침은 둘 다 안 변한다. 충돌은 둘 다 변한다.**

annotation의 actor별 `id`/`speed`/`rotation`으로 접촉한 상대가 같은 순간
함께 튕겼는지 본다. 10Hz에서 충돌이 두 샘플 사이에 끝날 수 있으므로 근접
10cm 이내도 접촉으로 센다.

**`collision_sweep_v2` 결과** (`summary_sha256` `9ed823df…`):

| 등급 | 클립 | 근거 |
|---|---|---|
| **충돌 유력** | **4** | `EGO_IMPULSE_BEYOND_BRAKING` (제동 포화 26 m/s² 초과) |
| **의심** | **32** | 양측반응 / 상대만반응 / 접촉후종료 / 깊은관통 중 1개 이상 |
| 반응 없는 접촉 | 191 | 스침 추정 |
| 정지 중 겹침 | 24 | 충돌 아님 |
| 접촉 없음 | 1,092 | — |

검토 대상 **36클립(2.7%)**, 그중 **21개가 클립 중간 충돌**이다.

폐기한 것 추가:

- **위치 미분**: Bench2Drive의 ego `x`/`y`가 시속 43km 직진 중에도 매 프레임
  1m 넘게 흔들린다(기록 속도는 매끄러움). 진행방향 신호는 전부 노이즈다.
- **actor 스폰 속도**: 첫 sweep에서 `YieldToEmergencyVehicle` 9개가 상대 속도
  25 m/s(25g) 변화로 상위를 차지했다. 긴급차량이 스폰 프레임에 최고속으로
  기록되는 결함이다. 한 프레임 15 m/s 초과 변화는 제외한다.

검토 목록은 `$RELEASE/quality_gate/collision_sweep_v2/REVIEW_LIST.md`와
저장소의 `COLLISION_REVIEW_LIST.md`에 있다.

### 검토 대상 선정 (구 규칙, 참고용)

`calibration_v4`에서 30개를 deterministic하게 뽑아
`$RELEASE/quality_gate/calibration_v4/review_selection.txt`
(sha256 `46fd775d…`)와 지표 `review_selection_metrics.json`에 남겼다.
경계를 가로지르는 표본에 **대조군을 반드시 포함**한다 — "충돌 아님"을 확인해야
임계값이 정당화되기 때문이다.

| 그룹 | n | 성격 |
|---|---|---|
| A 제동 불가 영역 (감속 2.5~7.79) | 4 | 진짜 충돌이어야 함 |
| B 경계 1.5~2.5 | 6 | 여기서 선이 갈림 |
| C 경계 0.8~1.5 | 5 | 애매 구간 |
| D 무명령 회전 상위 | 4 | 회전 신호 검증 |
| E 보행자·자전거 | 3 | 취약 카테고리 |
| F 대조군 좌회전 | 2 | **충돌 아니어야 함** |
| G 대조군 급제동/차선변경 | 2 | **충돌 아니어야 함** |
| H 대조군 저신호 | 4 | **스침이어야 함** |

내일 A와 F/G/H를 나란히 보고 임계값을 확정한다. A가 충돌로 보이고 F/G/H가
정상이면 2.5 m/s/frame 선이 맞고, 아니면 B/C 구간에서 조정한다.

### evidence 생성

승인한 clip list를 사용한다.

```bash
python3 "$RUNNER" \
  --stage quality-evidence \
  --manifest "$SPLIT" \
  --base-root "$BASE" \
  --weak-root "$WEAK" \
  --map-root "$MAP" \
  --release-root "$RELEASE" \
  --internship-root "$INTERN" \
  --calibration-version calibration_v4 \
  --clip-list "$RELEASE/quality_gate/calibration_v4/review_selection.txt"
```

필수 evidence:

- event 전후 2초 front RGB와 TOP_DOWN
- ego/상대 actor oriented bbox, actor ID/category
- overlap area, IoU, penetration/clearance, run length timeline
- source frame과 audit event를 역추적할 수 있는 manifest

궤적 미분 speed/acceleration/jerk 기반 판정과 그 그래프는 사용하지 않는다.

### quality config v1 승인

최종 파일:

`$RELEASE/quality_gate/configs/quality_config_v1.json`

반드시 포함할 것:

- collision categories: vehicle, pedestrian/walker, bicycle의 실제 schema 매핑
- REVIEW penetration/IoU/consecutive-frame 기준
- EXCLUDE penetration/IoU/consecutive-frame 기준
- severe single-frame 기준 사용 여부
- config schema version, reviewer, 승인 시각, 근거 문서
- parent calibration metrics/events SHA256
- config SHA256

`calibration_only` 또는 null threshold가 하나라도 남아 있으면 classifier가 실행을 거부해야 한다. config와 경계 사례 요약을 보고하고 멈춘다. 사용자 승인 전 Phase 2.5-F를 실행하지 않는다.

---

## Phase 2.5-F — 분류, 수동 REVIEW, filtered split

### 1차 candidate classification

- 승인된 `quality_config_v1.json`으로만 실행한다.
- decisions 없이 실행해 `PASS`, `REVIEW`, `EXCLUDE` 후보를 생성한다.
- candidate output/log는 final output/log와 분리한다.

예정 output:

`$RELEASE/quality_gate/classification_candidates_calibration_v4/`

### REVIEW 수동 확정

- 자동 승인 금지
- 각 REVIEW clip에 `ACCEPT` 또는 `EXCLUDE`를 사람이 기록
- reviewer, reason_code, 구체 note, `clip_metrics_sha256` 필수
- decision 파일 최상위에 calibration `metrics_sha256`과 `events_sha256` 필수
- hash가 달라진 stale decision은 classifier가 거부

최종 decision 파일:

`$RELEASE/quality_gate/review_decisions_v1.json`

### 최종 classification과 filtered split

최종 output:

`$RELEASE/quality_gate/classification_v1/`

최종 split:

`$RELEASE/quality_gate/classification_v1/filtered_train_val_split.json`

### Phase 2.5 전체 완료 조건

- [ ] `accepted + excluded + unresolved = 1329`
- [ ] accepted/excluded/unresolved 집합 간 교집합 0
- [ ] unresolved 0
- [ ] accepted와 excluded가 parent 1,329개를 정확히 분할
- [ ] filtered split이 정확히 `parent split ∩ accepted`
- [ ] 원래 train/val 소속 유지
- [ ] val 제외분을 train에서 보충하지 않음
- [ ] final train/val 및 Base/Weak·scenario별 변화량 보고
- [ ] config, decisions, metrics/events, filtered split SHA256 기록
- [ ] `completion.json` status completed, unresolved 0

이 조건을 모두 만족한 시점에만 **Phase 2.5 완료**라고 선언한다. 결과를 요약하고 멈춘다.

---

## Phase 3 — accepted clip traffic-light relabel

Phase 3은 원래 1,329개 전체가 아니라 `filtered_train_val_split.json`의 accepted clip만 처리한다.

### 시작 조건

- Phase 2.5-F 완료 및 사용자 승인
- relabel output과 stage log 대상이 기존 산출물을 덮어쓰지 않음
- Phase 2.5-B에서 `--resume --no-visualization --no-video --no-vector-map --no-vad-vector-gt`가 runner command에 고정됨
- 기존 `production_1329` relabel, PKL, clips는 수정하지 않음

### Phase 3-A Base relabel

```bash
python3 "$RUNNER" \
  --stage relabel-base \
  --manifest "$SPLIT" \
  --base-root "$BASE" \
  --weak-root "$WEAK" \
  --map-root "$MAP" \
  --release-root "$RELEASE" \
  --internship-root "$INTERN"
```

Base 결과, failed clip, traffic-light REVIEW를 요약하고 멈춘다.

### Phase 3-B Weak relabel

Base 승인 후 동일 공통 인자로 `--stage relabel-weak`를 실행한다. Weak 결과와 REVIEW를 요약하고 멈춘다.

### Phase 3-C traffic-light REVIEW 승인 및 resume

quality-gate REVIEW decision과 traffic-light relabel approval은 서로 다른 파일이다.

- traffic-light REVIEW를 자동 승인하지 않는다.
- 승인 파일에 reviewer/reason/hash를 기록한다.
- 승인된 파일을 `--review-approvals`로 전달하고 Base/Weak를 각각 `--resume`한다.
- 최초 실행과 승인 resume는 서로 다른 log label을 사용한다.

### Phase 3-D relabel check

```bash
python3 "$RUNNER" \
  --stage relabel-check \
  --manifest "$SPLIT" \
  --base-root "$BASE" \
  --weak-root "$WEAK" \
  --map-root "$MAP" \
  --release-root "$RELEASE" \
  --internship-root "$INTERN" \
  --review-approvals "$RELEASE/relabel/review_approvals_v1.json"
```

### Phase 3 완료 조건

- [ ] relabel 대상 clip 집합이 accepted 집합과 정확히 동일
- [ ] excluded clip은 relabel output에 없음
- [ ] 각 clip frame inventory가 원본 annotation frame과 정확히 동일
- [ ] failed 0
- [ ] unresolved traffic-light REVIEW 0
- [ ] completion/config/approval hash 일치
- [ ] 원본 annotation write 0
- [ ] 불필요한 전체 visualization/video/vector-map/VAD GT 생성 0

check 결과를 보고하고 멈춘다.

---

## Phase 4 — accepted clip union

새 union 경로:

`$RELEASE/pkl/clip_union_filtered_v1`

기존 `$RUNTIME`이나 기존 union을 수정하지 않는다.

### 생성

```bash
python3 "$RUNNER" \
  --stage clip-union \
  --manifest "$SPLIT" \
  --base-root "$BASE" \
  --weak-root "$WEAK" \
  --map-root "$MAP" \
  --release-root "$RELEASE" \
  --internship-root "$INTERN"
```

### 검사

승인 후 동일 공통 인자로 `--stage clip-union-check`를 실행한다.

### 완료 조건

- [ ] union clip 수 = accepted clip 수
- [ ] 각 link가 정확한 Base 또는 Weak 원본 clip을 가리킴
- [ ] excluded clip 0
- [ ] missing/broken/wrong-target/extra link 0
- [ ] `.clip_union_manifest.json`의 split/hash/count가 classification completion과 일치

요약 후 멈춘다.

---

## Phase 5 — original/corrected filtered PKL 생성

### 시작 조건

- Phase 4 union check 통과
- Internship converter의 Python 3.8·atomic/no-overwrite hardening 통과
- output directory가 존재하지 않음
- 디스크/inode 재확인
- 같은 filtered split, 같은 clip union, 같은 map을 두 실행에 사용

### Phase 5-A original-filtered

output:

`$RELEASE/pkl/original_filtered_v1`

```bash
python3 "$RUNNER" \
  --stage pkl-original \
  --manifest "$SPLIT" \
  --base-root "$BASE" \
  --weak-root "$WEAK" \
  --map-root "$MAP" \
  --release-root "$RELEASE" \
  --internship-root "$INTERN" \
  --workers 8
```

생성 파일의 존재만 보고하지 않는다. train/val/map record count, file size, SHA256, completion manifest를 확인하고 멈춘다.

### Phase 5-B corrected-filtered

original-filtered 승인 후 output이 비어 있는 것을 확인하고 동일 공통 인자로 `--stage pkl-corrected`를 실행한다.

output:

`$RELEASE/pkl/corrected_filtered_v1`

### Phase 5 완료 조건

- [ ] original train/val/map PKL 각 1개와 completion manifest
- [ ] corrected train/val/map PKL 각 1개와 completion manifest
- [ ] 두 PKL이 동일 filtered split과 union을 사용
- [ ] train/val clip 소속은 parent split에서 accepted만 제거한 결과와 동일
- [ ] converter record count와 file SHA256 기록
- [ ] 기존 production PKL overwrite 0

corrected 생성 뒤에도 Phase 6 validator 전에는 최종 완료로 선언하지 않는다.

---

## Phase 6 — 최종 PKL 검증과 handoff

### 6-A 정적 validator

`$RUNTIME`은 기존 학습에 영향을 주지 않고 path resolution만 읽기 전용으로 확인하는 데 사용한다. 여기에 파일·symlink를 생성하거나 수정하지 않는다.

```bash
python3 "$RUNNER" \
  --stage validate-pkls \
  --manifest "$SPLIT" \
  --base-root "$BASE" \
  --weak-root "$WEAK" \
  --map-root "$MAP" \
  --release-root "$RELEASE" \
  --internship-root "$INTERN" \
  --runtime-data-root "$RUNTIME"
```

예상 report:

- `original_filtered_v1/train_validation_report.json`
- `original_filtered_v1/val_validation_report.json`
- `corrected_filtered_v1/train_validation_report.json`
- `corrected_filtered_v1/val_validation_report.json`

필수 검사:

- [ ] 네 report 모두 passed, errors 0
- [ ] train/val overlap 0
- [ ] path prefix와 runtime files 모두 해석 가능
- [ ] nonfinite/shape/schema 오류 0
- [ ] original/corrected의 `(folder, frame_idx)` 순서·개수 완전 동일
- [ ] 허용된 traffic-light 필드 외 차이 0
- [ ] original/corrected map PKL hash 동일
- [ ] traffic-light 상태/geometry 분포와 정규화 통계 기록

### 6-B 실제 loader/model-input smoke

정적 validator 다음에는 실제 training loader가 새 PKL을 한 batch 이상 읽는지 확인한다.

- 현재 학습 checkout/Zoo copy/config/output은 사용하거나 수정하지 않는다.
- 별도 versioned Zoo/runtime workspace만 사용한다.
- shared symlink를 바꾸거나 내부에서 `rm`을 수행하는 setup script는 실행하지 않는다.
- 먼저 실제 loader가 참조하는 PKL, `data_root`, map path, pipeline field를 read-only로 추적한다.
- `original-filtered`와 `corrected-filtered` 각각 동일 seed/index의 batch를 읽는다.
- `(folder, frame_idx)`, tensor shape/dtype, traffic-light target, map loading, missing file을 확인한다.
- full training, `sbatch`, `srun`, production job 재시작은 하지 않는다.

정확한 smoke 명령은 Phase 6 preflight에서 별도 Zoo copy와 config를 확인한 뒤 기록한다. 경로를 추측해 실행하지 않는다.

### 6-C 최종 provenance와 보고서

최종 산출물:

- `$RELEASE/SHA256SUMS`
- `$RELEASE/FINAL_REPORT.md`
- stage별 command/log/exit code
- Bench/Internship commit
- parent split, calibration metrics/events, quality config, REVIEW decisions, filtered split hash
- relabel completion/approval hash
- union manifest hash
- six PKL file hashes와 four validation report hashes
- final accepted/excluded count, train/val 및 Base/Weak/scenario 변화량
- source dataset write audit 결과
- 알려진 제한 또는 미인증 경로

### 전체 production 완료 조건

- [ ] Phase 2.5 완료
- [ ] Phase 3 relabel completion/check 통과
- [ ] Phase 4 union check 통과
- [ ] Phase 5 두 PKL atomic 생성 완료
- [ ] Phase 6 네 validator report 통과
- [ ] 실제 loader smoke 통과
- [ ] 모든 hash와 명령이 FINAL_REPORT에 기록
- [ ] 기존 학습과 원본 dataset에 영향 없음 재확인

이 조건을 모두 만족해야 최종 corrected PKL을 training handoff 대상으로 승인한다.

---

## 4. 현재 blocker와 결정이 필요한 항목

| 항목 | 현재 상태 | 필요한 조치 |
|---|---|---|
| Weak canonical depth write | **우회 완료** | patched overlay로 해결. canonical은 여전히 284개 누락 — 나중에 dataset owner가 `phase2_5_logs/run_phase2_5c_depth_apply.sh`를 sudo로 실행하면 실제 보완 가능 |
| top-level `theta=NaN` 7개 | **해결** | 승인받아 비차단(`EGO_STATE_NONFINITE`, severity `note`)으로 변경 |
| actor `brake=NaN` 1,041개 | **해결** | blanket finite 스캔 제거. 판정 필드만 gate |
| Python 3.8 CLI | **해결** | Scenario 2개 + PKL converter 플래그 쌍 교체 |
| classification/relabel 재실행 | **해결** | candidate/final/approval-resume label 분리 |
| PKL output | **해결** | absent precondition + atomic write + completion manifest |
| relabel `--build-pkl` | **해결** | 기본 true라 인자 파싱에서 즉사했음. `--no-build-pkl` 고정 |
| VAD vector GT | **해결** | stale 테스트 3개를 loader parity 계약으로 재작성. 미인증 항목 없음 |
| evidence renderer | **해결** | `laspy` 지연 import + `camera-bev-map` 프로파일 추가 |
| collision threshold | **불필요해짐** | 임계값을 승인받는 대신 전수조사(`collision_sweep_v2`) 증거를 사람이 직접 판정. `--sweep-dir`를 쓰면 config의 임계값은 아예 로드되지 않으므로 미승인 값이 production config에 들어갈 일이 없다 |
| REVIEW | **완료** | 검토 큐 77 → 제외 29 / 승인 48, unresolved 0. 검토자 `kimminseong`, 결정 파일은 metrics/events/sweep 세 해시에 묶임 |
| **두 저장소 push** | **미완료** | 자격증명 부재. 본인 계정으로 push 필요 (아래 6절) |
| loader smoke 경로 | preflight 필요 | 현재 학습과 분리된 Zoo copy/config를 read-only 조사 후 명령 확정 |

## 6. 미push commit과 push 절차

두 저장소 모두 **fast-forward, 뒤처진 commit 0, working tree clean** — 충돌 불가능.
커밋 명의는 서버 공용 계정 `ailab-hanyang-bot`이며, push 계정과는 별개다.

| 저장소 | 브랜치 | 통합 대상 | 미push |
|---|---|---|---|
| Bench2Drive_dataset_manage | `production-1329-quality-gate-v1` | `main` | **9개** |
| 2026-Summer-Internship | `data/production-1329-quality-filtered-v1` | **`team_two`** (main 아님) | **4개** (오늘 2개) |

```bash
BENCH=/home/ailab/AILabSSD/99_Management/2026_internship/kimminseong/production_1329_v1_code/Bench2Drive_dataset_manage
INTERN=/home/ailab/AILabSSD/99_Management/2026_internship/kimminseong/production_1329_v1_code/2026-Summer-Internship

git -C "$BENCH"  fetch origin main      && git -C "$BENCH"  rev-list --count HEAD..origin/main
git -C "$INTERN" fetch origin team_two  && git -C "$INTERN" rev-list --count HEAD..origin/team_two
# 둘 다 0이어야 충돌 없음. 0이 아니면 feature branch에서 먼저 merge 후 push

GIT_TERMINAL_PROMPT=1 git -C "$BENCH"  push origin production-1329-quality-gate-v1
GIT_TERMINAL_PROMPT=1 git -C "$INTERN" push origin data/production-1329-quality-filtered-v1
```

`credential.helper`가 local/global/system 전부 비어 있어 PAT은 저장되지 않는다.
**`credential.helper store`를 켜지 말 것** — 켜면 본인 PAT이 관리자 것과 같은
`~/.git-credentials`에 기록된다. `-u`도 붙이지 않는다 (upstream 설정이 남는다).

## 7. 전체 기록

**작업 전체 흐름·수치·한계는 `PIPELINE_REPORT_2026-08-14.md` 에 정리했다.**
문제 발견 → 로직 → 적용 → 결과 → 검증 → 한계 순서로 한 문서에 담았고,
발표 자료는 그 문서만 보면 된다. 아래는 실행 상태만 남긴다.

## 8. 실행 상태

### 오늘(2026-08-14) 완료된 것

| 단계 | 결과 |
|---|---|
| 충돌 전수조사 | 1,329 클립 전 프레임 → 접촉 296건 / 237 클립 |
| 검토 큐 산출 | 77 클립 (VRU 겹침 · 반응 증거 · 이동 중 차량 관통 0.10m↑) |
| 4시점 시각 증거 | 77 클립 × (위/전방/전방좌/전방우) GIF + 격자, 대조군 10 별도 |
| **육안 검토** | **제외 29 / 승인 48** — 자동 제외 0건, 전부 사람이 판정 |
| filtered split | **1,329 → 1,300** (train 1,262→1,237 · val 67→63) |
| 제외 원장 | `classification_v1/ledger/EXCLUSION_LEDGER.md` + `excluded_clips.csv` |

**split 규약 확정**: `preserve_parent_membership_remove_excluded_no_backfill` —
부모의 train/val 소속을 유지하고 제외분만 빼며, 빈자리를 채우지 않는다.
백필하면 train 클립이 val로 넘어가 **평가 세트 정의가 바뀌므로** 금지한다.
그 결과 val 비율은 5.04% → 4.85%로 미세하게 움직이며 이는 의도된 동작이다.

### 진행 중 / 남은 순서

1. **Phase 3 relabel** — `base` → `weak` 순차 실행 중 (`phase2_5_logs/run_phase3_relabel.sh`).
   두 컴포넌트를 동시에 돌리지 않는다: 각 실행이 dataset 전역 bbox consensus로
   시작하고, 둘이 같은 release root에 쓰기 때문이다.
2. `relabel-check` → `clip-union` → `clip-union-check`
3. `pkl-original` / `pkl-corrected`
4. `validate-pkls` 4종 (original·corrected × train·val)
5. 두 저장소 push (6절)

### 이 결과를 다시 만들려면

```
collision_sweep_v2/contacts.jsonl              전수조사 296건
  └ classify_quality.load_sweep()              → 검토 큐 77
      └ REVIEW_2026-08-14/EXCLUDE_LIST.txt     검토자가 적은 Route 번호
          └ build_review_decisions.py          → review_decisions_v1.json
              └ classify_quality --sweep-dir --decisions
                  └ filtered_train_val_split.json
                      └ report_exclusions.py   → EXCLUSION_LEDGER.md
```

각 단계는 앞 단계 SHA256에 묶여 있다. 감사나 전수조사를 다시 돌리면 결정 파일이
자동으로 무효가 되고 분류가 거부되므로, 낡은 판정이 조용히 통과할 수 없다.
