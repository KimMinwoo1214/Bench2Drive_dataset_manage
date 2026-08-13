# Bench2Drive Dataset Management

Bench2Drive 데이터 선정과 traffic-light annotation 보정을 관리하는 데이터팀
저장소다. Production PKL 자체는 이 저장소에서 만들지 않고
`2026-Summer-Internship/team_code/data`의 converter와 validator를 사용한다.

> 작업 기준일: **2026-08-13**
>
> 현재 상태: 로컬 production pipeline과 606-frame 회귀 검증 완료. 서버 전체
> Base+Weak 실행과 1,329-clip 인계 검증은 아직 수행하지 않았다.

## 책임 경계

| 위치 | 책임 |
| --- | --- |
| [`Weak_Scenario_Mining`](Weak_Scenario_Mining/README.md) | 평가 결과 기반 취약 시나리오 선정과 Base+Weak production split 생성 |
| [`Scenario_Filtering`](Scenario_Filtering/README.md) | bbox permutation 복구, trigger-volume `affects_ego`, production resume/review/report |
| `2026-Summer-Internship/team_code/data` | Base+Weak symlink union, original/corrected Unified PKL, validator와 Dataset probe |

처리 순서는 다음과 같다.

```text
Base1000 + Weak329 split 고정
→ Base/Weak source inventory 검사
→ bbox permutation 복구
→ trigger-volume affects_ego 보정
→ review 해소 및 1,329 completion 검사
→ flat symlink union
→ original/corrected Unified PKL
→ validator + Dataset batch probe
```

## 2026-08-13 변경 사항

- GitHub `main`의 `bench2drive_base_1000.json`은 실제 1,000개 manifest로 갱신됐다.
- 해당 manifest에서 잘린 clip 이름
  `VehicleTurningRoutePedestrian_Town15_Route523_Weathe.tar.gz`를 실제 이름
  `..._Weather2.tar.gz`로 바로잡았다.
- 기존 Weak mining은 `result.json`을 분석해 취약 시나리오를 고르고
  `outputs/experiment_001/selected_additional_manifest.json`과 `details.json`을
  생성한다.
- 이번 작업은 그 Weak 결과와 Base1000 고정 split을 입력으로 사용해 Weak 및
  Base+Weak train/val split을 생성하는 production 단계를 추가했다.
- 그래서 `Weak_Scenario_Mining/data`에는 기존 두 dataset manifest 외에 split과
  검증 통계가 추가된다. Weak selection 원본은 `outputs/experiment_001`에 한 번만
  두며 `data`에 중복 복사하지 않는다.
- Scenario pipeline에는 manifest/component 실행, resume, check-only, review 승인과
  completion hash 계약을 추가했다.

파일 역할과 재생성 방법은
[`Weak_Scenario_Mining/README.md`](Weak_Scenario_Mining/README.md)에 정리돼 있다.

## 현재 Production snapshot

- Base split: `Weak_Scenario_Mining/data/bench2drive_base_train_val_split_full.json`
  - train 950 / val 50 / 총 1,000
  - SHA256 `030cfc19c9f885294de90f9921eee8e59e354fbd4c10e8ae1c98e2cd7adae451`
- Weak selection:
  `Weak_Scenario_Mining/outputs/experiment_001/selected_additional_manifest.json`
  - train 312 / val 17 / 총 329
- 결합 split:
  `Weak_Scenario_Mining/data/bench2drive_base1000_weak329_train_val_split.json`
  - train 1,262 / val 67 / 총 1,329

다음 명령은 입력 해시, Weak coverage-aware 선택 결과, component 합계와 모든
중복·누락 조건을 쓰기 없이 다시 검사한다.

```bash
python3 Weak_Scenario_Mining/build_production_split.py --check
```

`1000`, `329`, `17`, `1329`는 현재 snapshot의 결과이지 split 코드의 고정
hyperparameter가 아니다. 다음 Weak mining 결과에는
`Weak_Scenario_Mining/production_split_config.json`의 val 비율·seed·입력 경로를
사용하고, 생성기는 실제 개수에 맞춘 artifact 이름과 component 합계를 만든다.

과거 실행 폴더의 `combined_unique_manifest.json`은 당시 Base500+Weak329로 만든
829개 결과이므로 historical output으로만 남긴다. Production 1,329 inventory나
PKL 입력으로 사용하지 않는다.

## Scenario production 실행

Base와 Weak는 서로 다른 물리 root에서 같은 output root로 순차 실행한다.

```bash
python3 Scenario_Filtering/run_scenario_pipeline.py \
  --input "$BASE_ROOT" \
  --manifest Weak_Scenario_Mining/data/bench2drive_base1000_weak329_train_val_split.json \
  --component base \
  --output "$RELABEL_ROOT" \
  --resume --no-visualization --no-video --no-vector-map

python3 Scenario_Filtering/run_scenario_pipeline.py \
  --input "$WEAK_ROOT" \
  --manifest Weak_Scenario_Mining/data/bench2drive_base1000_weak329_train_val_split.json \
  --component weak \
  --output "$RELABEL_ROOT" \
  --resume --no-visualization --no-video --no-vector-map
```

각 component root는 manifest와 정확히 같은 clip inventory여야 한다. 일부 clip만
재처리하더라도 bbox consensus는 해당 component 전체에서 다시 계산된다.
최종 확인은 Base와 Weak 각각 같은 실행 옵션으로 `--check-only`를 실행한다.

## 완료 기준

- split 1,329개, train/val 및 Base/Weak overlap·누락 0
- `production_reports/aggregate_results.csv` 1,329행
- `failed=0`
- `review=0` 또는 현재 completion SHA256에 묶인 사람 승인
- 모든 corrected frame 집합이 원본과 동일
- original/corrected PKL validator `status=passed`, `errors=[]`
- train/val frame overlap과 `multiple_affecting_lights` 계약 위반 0
- 실제 Dataset batch `gt_tl=float32 [B,7]`

모델 학습, traffic loss와 CARLA 평가는 데이터팀 인계 이후 모델팀 범위다.

## 로컬 검증 상태 — 2026-08-13

- production split: Base 950/50, Weak 312/17, combined 1,262/67
- split Base/Weak 및 train/val overlap·누락 0
- split 회귀 테스트 4개 통과
- Scenario 및 production contract 테스트 10개 통과
- `SignalizedJunctionLeftTurn_Town04_Route173_Weather26` 606-frame smoke 통과
- resume 재실행과 `--check-only` 통과
- Python compile과 `git diff --check` 통과

서버의 실제 Base/Weak root 전체 실행, review 해소, 1,329 corrected frame 집합 검사와
PKL 인계 검증 전에는 “1,329 전체 완료”로 기록하지 않는다.
