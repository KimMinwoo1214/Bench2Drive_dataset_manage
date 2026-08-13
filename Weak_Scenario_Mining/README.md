# Weak Scenario Mining과 비율 기반 Production Split

이 디렉터리는 closed-loop 결과에서 취약 시나리오를 찾는 flywheel 도구와,
현재 Base1000+Weak329 production snapshot과, 다음 mining 크기에도 그대로 적용할
비율 기반 split 정책을 함께 관리한다.

> 작업 기준일: **2026-08-13**

## 왜 `data` 파일이 기존보다 늘었는가

기존 `main`의 `data`에는 Full manifest와 Base manifest 두 개만 있었다. 이번에는
Weak mining 자체뿐 아니라, 그 결과를 Scenario 보정과 PKL 변환에 넘길 수 있도록
train/val split을 생성·검증·고정하는 production 단계가 추가됐다.

```text
result.json
→ bench2drive_flywheel.py
→ outputs/experiment_001/selected_additional_{manifest,details}.json
→ build_production_split.py
→ data/Weak split + Base/Weak combined split + 검증 통계
```

Weak selection 원본은 `outputs/experiment_001`가 소유한다. `data`에 같은 파일을
복사하지 않는다. `data`에 새로 추가된 파일은 현재 production snapshot을 다른
컴퓨터와 서버에서도 동일하게 재현하기 위한 split 계약과 통계다.

## 정책과 snapshot을 분리한다

[`production_split_config.json`](production_split_config.json)이 앞으로 수정할
정책 파일이다. clip 수와 val 개수는 코드 상수가 아니다.

```json
{
  "seed": 42,
  "weak_validation_ratio": 0.05,
  "quota_rounding": "ceil",
  "minimum_validation_per_scenario": 1,
  "preserve_train_per_scenario": true
}
```

먼저 Weak 전체의 val 목표 개수를 계산한다.

```text
target_val = ceil(전체 Weak clip 수 × weak_validation_ratio)
```

이 목표 개수를 각 시나리오의 `scenario_count × ratio`에 가장 가까워지도록
largest-deficit 방식으로 배분한다. 시나리오별로 각각 올림하지 않으므로 시나리오
종류가 늘어날 때 전체 val 비율이 의도보다 커지는 문제를 피한다. 최소 시나리오
coverage를 우선하며, 두 개 이상 있는 시나리오는 기본적으로 train에 최소 한 개를
남긴다. 이 제약 때문에 요청 비율과 정확히 맞출 수 없을 때는 가능한 가장 가까운
값을 쓰고 requested/effective val 개수를 artifact에 함께 기록한다. 새 시나리오는
`scenario_order`에 없어도 이름순으로 뒤에 추가되어 같은 선택을 적용받는다.

`snapshot_expectations`는 현재 입력을 실수로 바꾸지 않기 위한 선택적 회귀 검증값일
뿐이다. 새 manifest 또는 hyperparameter로 새 snapshot을 만들 때는 입력 해시·예상
선택 해시를 새 값으로 갱신하거나 이 블록을 제거하고 생성 결과를 검토한 뒤 다시
고정한다.

## Production과 legacy 구분

| 파일 | 개수 | 용도 |
| --- | ---: | --- |
| `data/bench2drive_full+sup_13638.json` | 13,638 | Weak 후보를 찾는 Full manifest |
| `data/bench2drive_base_1000.json` | 1,000 | 현재 flywheel Base manifest |
| `data/bench2drive_base_train_val_split_full.json` | 1,000 | 실제 Base production split 950/50 |
| `data/bench2drive_base_split_stats.json` | 1,000 | Base 시나리오·타운·날씨·프레임 통계 |
| `outputs/experiment_001/selected_additional_manifest.json` | 329 | Weak archive manifest 정본 |
| `outputs/experiment_001/selected_additional_details.json` | 329 | Weak scenario/town/weather와 selection 근거 |
| `data/bench2drive_weak329_train_val_split.json` | 329 | Weak 고정 split 312/17 |
| `data/bench2drive_weak329_split_stats.json` | 329 | Weak split 분포 검증 결과 |
| `data/bench2drive_base1000_weak329_train_val_split.json` | 1,329 | Scenario와 PKL이 함께 쓰는 결합 split 1,262/67 |
| `data/bench2drive_base1000_weak329_split_stats.json` | 1,329 | 결합 split 분포 검증 결과 |

과거 `outputs/experiment_001/combined_unique_manifest.json`은 당시 Base500과
Weak329를 합친 829개 historical output이다. 파일명에 `unique`가 있어도
production 1,329 manifest가 아니다. 현재 `data/bench2drive_base_1000.json`은
2026-08-13 기준 실제 1,000개이며 production split의 clip 목록과 일치해야 한다.

## 현재 snapshot의 Weak val 생성 결과

현재 설정은 `coverage_aware_v1`, seed 42, Weak 전체 val 5%를 사용한다.

- 현재 계산 quota: Stopsign 5 / RedLight 5 / LaneChange 3 / Yield 4
- 위 시나리오 순서로 round-robin 선택
- 후보 우선순위:
  1. Weak val 전체에서 새로운 town
  2. Weak val 전체에서 새로운 weather
  3. 해당 시나리오에서 새로운 town
  4. 해당 시나리오에서 새로운 weather
  5. 해당 시나리오에서 희소한 town
  6. 해당 시나리오에서 희소한 weather
  7. 작은 `SHA256("42|scenario|clip")`

선택된 17개 clip 이름을 정렬해 newline으로 연결하고 마지막 newline을 붙인
SHA256은 다음으로 고정한다.

```text
d40b361637d35c8758853adbd9772eaaac2eb08b5fdae13095e06088eec06a94
```

## 재생성과 검증

다음 명령은 현재 입력 개수를 읽어 파일명까지 동적으로 정한 네 개의 split/stats
산출물을 다시 쓴다. 예를 들어 Weak가 500개가 되면 `weak500` 및
`base1000_weak500` 이름으로 생성된다.

```bash
python3 Weak_Scenario_Mining/build_production_split.py \
  --config Weak_Scenario_Mining/production_split_config.json
```

CI나 인계 전에는 쓰기 없는 검사를 사용한다.

```bash
python3 Weak_Scenario_Mining/build_production_split.py \
  --config Weak_Scenario_Mining/production_split_config.json \
  --check
```

검사는 다음을 모두 fail-fast한다.

- Base split SHA256 불일치
- Weak manifest/details SHA256 또는 목록 불일치
- 설정된 비율·rounding·seed와 생성 artifact의 불일치
- Base/Weak, train/val 중복
- manifest에 선언한 개수와 실제 목록의 불일치

## 다음 mining snapshot을 만드는 순서

1. 새 Weak manifest와 details를 별도 이름으로 저장한다.
2. `production_split_config.json`의 `inputs`를 새 파일로 바꾼다.
3. 필요하면 `weak_validation_ratio`, seed와 rounding을 변경한다.
4. 기존 snapshot을 보존하려면 생성 전 현재 split/stats 파일을 release 또는 날짜
   디렉터리에 보관한다.
5. `snapshot_expectations`를 제거한 상태에서 한 번 생성하고 분포를 검토한다.
6. 검토한 입력 SHA256·clip 수·selection SHA256을 다시 expectations에 고정한다.
7. `--check`와 Scenario `--check-only`가 모두 통과한 snapshot만 PKL에 넘긴다.

## 기존 flywheel 도구

`bench2drive_flywheel.py`는 평가 결과 분석, 취약 시나리오 선정과 archive manifest
생성을 위한 기존 도구다. 기본 `config.json`은 현재 실제 1,000개인
`data/bench2drive_base_1000.json`을 가리킨다. production split을 만드는 도구는
별도의 `build_production_split.py`다.

두 설정 파일의 책임은 다르다.

| 설정 | 바꾸는 것 |
| --- | --- |
| `config.json`의 `selection` | 어떤 취약 시나리오를 Full에서 몇 개 수집할지 |
| `production_split_config.json` | 수집 완료된 Weak 전체를 train/val로 어떻게 나눌지 |

저장소 루트에서 다음 순서로 실행한다.

```bash
python3 Weak_Scenario_Mining/bench2drive_flywheel.py base-distribution \
  --config Weak_Scenario_Mining/config.json
python3 Weak_Scenario_Mining/bench2drive_flywheel.py validate \
  --config Weak_Scenario_Mining/config.json
python3 Weak_Scenario_Mining/bench2drive_flywheel.py analyze \
  --config Weak_Scenario_Mining/config.json
python3 Weak_Scenario_Mining/bench2drive_flywheel.py select \
  --config Weak_Scenario_Mining/config.json
```

`select`가 갱신한 `outputs/experiment_001/selected_additional_manifest.json`과
`selected_additional_details.json`을 사람이 검토한 뒤 production split을 생성한다.

```bash
python3 Weak_Scenario_Mining/build_production_split.py \
  --config Weak_Scenario_Mining/production_split_config.json
```

기존 replay fine-tuning 실험이 필요할 때만 `build` 또는 `run-all`을 사용한다.
그 결과인 `combined_unique_manifest.json`은 production Scenario/PKL split과 다른
flywheel 산출물이다.
