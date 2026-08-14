# Phase 2.5-B — Production readiness 보강 결과 보고

실행일: 2026-08-13
결과: **코드 수정과 전체 테스트 완료. push는 자격증명 부재로 미완료.**

## 1. 결론

Phase 2.5-B의 코드 수정 범위와 테스트 완료 조건은 모두 충족했다. 두 저장소 모두
feature branch에 논리적으로 분리된 commit이 올라가 있고 working tree는 clean이다.
남은 항목은 원격 push 하나이며, 이는 코드 문제가 아니라 이 세션에서 GitHub 자격증명을
사용할 수 없기 때문이다 (아래 5절).

## 2. 이번에 발견한 추가 blocker

todo 문서에 없었으나 실제 데이터 작업을 막았을 문제 두 가지를 함께 고쳤다.

### 2-1. Phase 3 relabel은 인자 파싱에서 즉시 실패했을 것이다

`run_scenario_pipeline.py`의 `--build-pkl` 기본값이 true이고, production runner는 항상
`--manifest`를 넘긴다. 이 조합에서 `main()`은 다음 경로의 converter를 요구한다.

```
Bench2Drive_dataset_manage/Scenario_Filtering/2026-Summer-Internship/team_code/data/prepare_b2d_infos.py
```

이 경로는 현재 layout에 존재하지 않으므로 `parser.error`로 종료된다. 즉 depth 권한을
해결하고 calibration_v3를 끝냈더라도 Phase 3-A는 첫 clip 처리 전에 실패했을 것이다.
runner가 `--no-build-pkl`을 명시적으로 넘기도록 고정해 해결했다. filtered PKL은 Phase 5에서
hardening된 converter가 별도로 만들므로 의미상으로도 이쪽이 맞다.

### 2-2. VAD vector map 테스트 4개는 실제로 성격이 달랐다

- 1개는 `BooleanOptionalAction` 자체 때문에 실패했고 Python 3.8 수정으로 통과했다.
- 나머지 3개는 geometry 버그가 아니라 **stale 테스트**였다. `_trigger_edge_to_stopline`,
  `stopline_points/stopline_types` key, lane polyline의 경계 clipping·분할을 가정하는데,
  이는 commit `67de062` 이전 설계다.

현재 `map_vectors`는 `B2D_VAD_Dataset.get_map_info()`를 그대로 복제한다. 학습 loader
(`team_code/data/b2d_vad_dataset.py:276`)와 대조해 다음이 동일함을 확인했다.

- `map_element_class` 6개 key 동일
- 경계 mask가 `>`/`<` (strict)
- 범위 밖 lane point는 clipping이 아니라 **삭제** 후 남은 점을 하나의 polyline으로 연결
- trigger volume은 `mask.all()`일 때만 유지하고 닫힌 polygon으로 저장
- prefilter distance 50 m

따라서 코드가 옳고 테스트가 낡은 것이므로, 테스트를 현재 계약(=학습 loader와의 parity)에
맞춰 다시 썼다. **결과적으로 `--no-vad-vector-gt` 미인증 항목 없이 전체 suite가 green이다.**

## 3. 변경 내용

### Bench2Drive_dataset_manage — branch `production-1329-quality-gate-v1`

| commit | 내용 |
|---|---|
| `92a102d` | Scenario parser Python 3.8 호환 |
| `b716cae` | relabel headless 고정 + 재실행 log label 분리 + map PKL 대조 배선 |
| `740bc64` | top-level ego pose NaN 비차단 정책 |
| `56e9cb6` | VAD vector map 테스트를 실제 loader parity 계약으로 재작성 |

- `argparse.BooleanOptionalAction` 5곳(`run_scenario_pipeline.py`) + 2곳(`read_json_gz.py`)을
  `--flag/--no-flag` 쌍으로 교체했다. 기존 flag 이름·기본값·last-flag-wins 의미를 유지한다.
- relabel command에 `--resume --no-visualization --no-video --no-vector-map
  --no-vad-vector-gt --no-build-pkl`을 고정했다. `--check-only`는 `--resume`과 상호배타이므로
  check 단계에서만 `--check-only`를 쓴다.
- log label 분리:
  - classification: `quality-classify-candidates-<cal>` / `quality-classify-final-<cal>`
  - relabel: `relabel-<component>` / `relabel-<component>-approved-resume` /
    `relabel-check-<component>`
  - 같은 label의 기존 로그가 있으면 종전대로 덮어쓰지 않고 중단한다.

### 2026-Summer-Internship — branch `data/production-1329-quality-filtered-v1`

| commit | 내용 |
|---|---|
| `174455d` | converter Python 3.8 호환 |
| `f8f38d7` | PKL output hardening + original/corrected paired validation 강화 |

- output directory가 비어 있지 않거나 대상 PKL이 이미 있으면 `FileExistsError`로 중단한다.
- 모든 PKL은 임시 파일에 완전히 쓰고 `fsync` 후 atomic rename한다.
- `completion.json`에 명령, code commit, data_root, split 경로·SHA256, annotation source,
  corrected_root, map_root, path_prefix, filter_invisible, workers, split별 clip 수,
  파일별 record/town 수·바이트·SHA256을 남긴다.
- paired validation은 key 집합 비교를 버리고 **순서가 있는 시퀀스**로 비교한다. 개수와
  `(folder, frame_idx)` 순서가 완전히 같아야 하고, 첫 불일치 index를 보고한다.
- traffic-light 허용 필드 7개 외의 값이 하나라도 다르면 실패한다. 비교는 numpy
  shape/dtype까지 엄격하며 NaN은 같은 값으로 본다.
- map PKL은 SHA256으로 대조한다 (`--original-map-file`). Phase 6의 "map PKL hash 동일"
  조건을 validator가 실제로 강제한다.

### 승인된 정책 변경: top-level `x`/`y`/`theta`

사용자 승인(옵션 A)에 따라 구현했다.

- 비유한값은 `EGO_STATE_NONFINITE`(severity `note`)로 기록하고 신규 지표
  `nonfinite_ego_state_frames`로 집계한다.
- structural fatal이 **아니며**, 해당 프레임의 bbox 충돌 검사를 중단하지 **않는다**.
- 판정에 실제로 쓰는 bbox·transform·extent, sensor/calibration, expert action의 finite
  검사는 종전대로 fatal로 유지한다.
- 계약은 `Driving_Quality_Filtering/README.md`에 명시했고 테스트 3개로 고정했다.

영향: calibration_v2에서 `EGO_STATE_INVALID`로 잡혔던 7개 clip(21 frame, 전부 `theta=NaN`)이
충돌 증거 없이 자동 EXCLUDE되지 않는다. 대신 실제 collision 지표로 판정된다.

## 4. 테스트 및 완료 조건

| 항목 | 결과 |
|---|---|
| Bench Scenario production 경로 테스트 | 17/17 통과 (기존 4개 실패 전부 해소) |
| Bench Weak split 테스트 | 5/5 통과 |
| Bench quality gate 테스트 | 21/21 통과 (기존 18개 + 신규 3개) |
| VAD 4개 | 전부 통과. 미인증 항목 없음 |
| Internship data unit test | 50/50 통과 (기존 40개 + 신규 10개) |
| Python 3.8 parser `--help` | 13개 전부 성공 |
| `compileall` | 두 저장소 성공 |
| shell `bash -n` | 성공 |
| `git diff --check` | 두 저장소 성공 |
| `build_production_split.py --check` | 성공 — Base 950/50, Weak 312/17, combined 1262/67 |
| 실제 root 재확인 | Base 1000, Weak 329, 교집합 0 |
| split SHA256 | `448438d80b43886cd1d7d45730ee500033ae2c44e5b190ac9fcd5862f7433c65` (고정값과 일치) |
| 논리 분리 commit | Bench 4개, Internship 2개 |
| feature branch push | **미완료 — 5절 참조** |

신규 테스트 13개:

- quality gate 3개: pose NaN 비차단·충돌검사 지속, bbox NaN fatal 유지, sensor NaN fatal 유지
- PKL hardening 5개: 부재/빈 디렉터리 허용, 대상 파일 존재 시 거부, 비어있지 않은 디렉터리
  거부, 임시 파일 잔여 없음
- paired validation 5개: 순서 뒤바뀜 실패, record 누락 실패, 비-traffic-light 필드 변경 실패,
  traffic-light 전용 변경 허용, map PKL 불일치 실패

### 로그

| 파일 | SHA256 |
|---|---|
| `phase2_5_logs/run_phase2_5b_readiness_validation.sh` | `2a14c41dab2c3d10928dd0cb3d85ac381297f7186ba135dd0a9ca923e6ca1b55` |
| `phase2_5_logs/PHASE_2_5B_READINESS_VALIDATION.log` (commit 전) | `fa6fdd2dea7375c8fef3b46a69cfca110cc1f5f51e4129d9d9365cf446585b34` |
| `phase2_5_logs/PHASE_2_5B_POSTCOMMIT_VALIDATION.log` (commit 후) | `b82e0def622415497776adce4c72fdc818b4ecf54db425f234964f9296bea448` |

두 실행 모두 exit code 0이다. 기존 `phase2_5_logs` 로그는 덮어쓰지 않고 새 run label을 썼다.

## 5. 미완료 항목 — feature branch push

두 저장소 모두 push하지 못했다. 코드나 테스트 문제가 아니다.

- credential helper가 설정되어 있지 않다 (`git config --get-all credential.helper` 비어 있음).
- 이 세션은 비대화형이라 자격증명 프롬프트를 띄울 수 없다.
- Bench: `git push --dry-run` →
  `fatal: could not read Username for 'https://github.com'`
- Internship: `git ls-remote` →
  `remote: Repository not found. / Authentication failed`
  (`https://github.com/ailab-hanyang/2026-Summer-Internship.git`)

commit은 모두 로컬 feature branch에 있고 working tree는 clean이므로, 자격증명이 있는
셸에서 아래만 실행하면 된다.

```bash
git -C "$BENCH" push -u origin production-1329-quality-gate-v1
git -C "$INTERN" push -u origin data/production-1329-quality-filtered-v1
```

origin의 `main`은 `debf146`으로 현재 merge base와 같고, `production-1329-quality-gate-v1`은
origin에 아직 없다. 따라서 새 branch 생성이며 기존 branch를 덮어쓰지 않는다.

## 6. 안전 규칙 준수 확인

- 원본 Base/Weak annotation, 기존 PKL, 기존 relabel output, `production_1329/clips`,
  학습 job에 대한 쓰기 0. 이번 작업은 코드 저장소와 `phase2_5_logs`만 수정했다.
- `git reset`, `git clean`, `rm` 미사용.
- 기존 로그 삭제·덮어쓰기 없음. 새 run label 사용.
- calibration_v1/v2 artifact 미수정.

## 7. 다음 단계

Phase 2.5-B 승인 후 Phase 2.5-C(Weak canonical depth 284개 보완)로 진행한다. 그 전에
위 push를 자격증명 있는 셸에서 완료하는 것을 권장한다.

주의: 이번 정책 변경으로 audit metrics에 `nonfinite_ego_state_frames` 컬럼이 추가되었고
severity 집계가 달라졌으므로, calibration_v2의 metrics/events SHA256은 더 이상 재현되지
않는다. calibration_v3는 어차피 새로 생성하는 단계이므로 문제가 되지 않으나,
v2 기준으로 만든 stale decision은 classifier가 거절한다 (의도된 동작).
