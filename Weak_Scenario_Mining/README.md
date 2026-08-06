# Bench2Drive Data Flywheel 사용 방법

## 1. 포함된 파일

```text
bench2drive_flywheel_clean/
├── bench2drive_flywheel.py
├── ability_map.json
├── config.json
├── HOW_TO_USE.md
├── data/
├── results/
├── inputs/
├── outputs/
└── downloads/
```

데모, 샘플 결과, 테스트용 Base 목록, 이전 버전 문서, 변환 스크립트는 포함하지 않았습니다.

이 프로그램은 Bench2Drive 평가기가 생성한 **원본 `result.json`을 직접 입력**으로 사용합니다. 별도 CSV 변환은 필요하지 않습니다.

---

## 2. 필요한 파일 배치

다음 두 manifest를 `data/`에 넣습니다.

```text
data/bench2drive_base_1000.json
data/bench2drive_full+sup_13638.json
```

Bench2Drive 평가 결과를 `results/`에 넣습니다.

```text
results/result.json
```

Fine-tuning 후 비교할 결과가 있으면 다음과 같이 넣습니다.

```text
results/result_finetuned.json
```

최종 구조:

```text
bench2drive_flywheel_clean/
├── bench2drive_flywheel.py
├── ability_map.json
├── config.json
├── README.md
├── data/
│   ├── bench2drive_base_1000.json
│   └── bench2drive_full+sup_13638.json
├── results/
│   ├── result.json
│   └── result_finetuned.json
├── inputs/
├── outputs/
└── downloads/
```

---

## 3. 프로젝트 폴더로 이동

```bash
cd /본인경로/bench2drive_flywheel_clean
```

Python 확인:

```bash
python --version
```

`python` 명령이 없는 환경에서는 이후 명령의 `python`을 `python3`로 바꿉니다.

---

## 4. `config.json` 설정

### 4.1 평가 route 수

220개 전체 route를 평가했다면 그대로 둡니다.

```json
{
  "evaluation": {
    "expected_routes": 220
  }
}
```

10개 route만 평가했다면 다음처럼 수정합니다.

```json
{
  "evaluation": {
    "expected_routes": 10
  }
}
```

### 4.2 입력 경로

기본 설정:

```json
{
  "paths": {
    "full_manifest": "data/bench2drive_full+sup_13638.json",
    "base_manifest": "data/bench2drive_base_1000.json",
    "evaluation_results": "results/result.json",
    "evaluation_results_finetuned": null,
    "evaluation_plan": null,
    "ability_map": "ability_map.json",
    "output_dir": "outputs/experiment_001"
  }
}
```

Fine-tuning 전후 비교를 할 때는 다음 항목을 설정합니다.

```json
{
  "paths": {
    "evaluation_results_finetuned": "results/result_finetuned.json"
  }
}
```

---

## 5. 선택 평가 route의 Ability를 직접 지정하는 경우

5개 Ability에서 각각 2개 route를 직접 선정한 실험이라면 `inputs/evaluation_plan.csv`를 만듭니다.

```csv
route_id,target_ability
RouteScenario_17569_rep0,Merging
RouteScenario_2286_rep0,Merging
RouteScenario_1792_rep0,Overtaking
RouteScenario_24367_rep0,Overtaking
RouteScenario_2715_rep0,Emergency Brake
RouteScenario_27582_rep0,Emergency Brake
RouteScenario_2790_rep0,Give Way
RouteScenario_3373_rep0,Give Way
RouteScenario_2416_rep0,Traffic Sign
RouteScenario_3144_rep0,Traffic Sign
```

그다음 `config.json`을 수정합니다.

```json
{
  "paths": {
    "evaluation_plan": "inputs/evaluation_plan.csv"
  }
}
```

220개 전체 평가에서 Table 2 기준으로 Ability를 중복 집계할 경우에는 `evaluation_plan`을 `null`로 둡니다.

```json
{
  "paths": {
    "evaluation_plan": null
  }
}
```

---

# 6. 실행 순서

## 6.1 Base 데이터 분포 분석

```bash
python bench2drive_flywheel.py base-distribution --config config.json
```

생성 결과:

```text
outputs/experiment_001/base_distribution/
├── scenario_distribution.csv
├── ability_distribution.csv
└── distribution_summary.json
```

`outputs/` 아래 결과는 실행할 때마다 다시 생성할 수 있어 Git에는
올리지 않습니다. 팀원은 같은 `config.json`과 입력 `result.json`으로 동일한
결과를 재생성할 수 있습니다.

주요 의미:

- `scenario_distribution.csv`: 시나리오별 Base 데이터 개수와 비율
- `ability_distribution.csv`: 5개 Ability별 관련 Base 데이터 개수와 비율

Ability는 서로 중복될 수 있으므로 Ability 비율의 합은 100%를 넘을 수 있습니다.

---

## 6.2 입력 검증

```bash
python bench2drive_flywheel.py validate --config config.json
```

확인 내용:

- 평가 route 개수
- Runtime 실패 route 개수
- 성공 route 개수
- 중복 route ID
- Ability에 매핑되지 않은 시나리오
- 평가 계획과 실제 route ID 일치 여부
- Base 및 Full manifest 파일 상태

생성 결과:

```text
outputs/experiment_001/validation.json
```

`warnings`에 route 수 불일치가 표시되면 `expected_routes` 설정과 실제 `result.json`을 확인합니다.

---

## 6.3 원본 `result.json` 분석

```bash
python bench2drive_flywheel.py analyze --config config.json
```

내부 처리:

```text
result.json의 _checkpoint.records 읽기
→ 시나리오 이름 정규화
→ DS, RC, Infraction Penalty 파싱
→ Collision, Blocked, Timeout 등 집계
→ TickRuntime 등 Runtime 실패 분리
→ 취약 Scenario 판정
→ Ability 결과 집계
```

생성 결과:

```text
outputs/experiment_001/
├── route_metrics.csv
├── route_metrics.json
├── runtime_rerun_routes.csv
├── scenario_metrics.csv
├── scenario_metrics.json
├── ability_metrics_table2.csv
├── ability_metrics_target.csv
├── weak_scenarios.json
└── analysis_report.md
```

우선 확인할 파일:

```text
analysis_report.md
runtime_rerun_routes.csv
scenario_metrics.csv
ability_metrics_target.csv
```

### Runtime 실패 처리

`runtime_rerun_routes.csv`에 route가 있으면 해당 route를 먼저 재실행합니다.

예:

```text
Failed - TickRuntime
Simulation crashed
Agent setup failed
```

Runtime 실패는 기본적으로 취약 시나리오 선정에서 제외됩니다. 재실행 후 확정된 `result.json`으로 다시 분석해야 합니다.

---

## 6.4 Full 데이터에서 추가 데이터 선택

Runtime 실패를 정리한 최종 `result.json`으로 실행합니다.

```bash
python bench2drive_flywheel.py select --config config.json
```

처리:

```text
취약 Scenario 확인
→ Full manifest에서 동일 Scenario 검색
→ Base에 이미 포함된 파일 제외
→ 실패 Town 또는 Weather 일부 우선
→ 나머지는 Town과 Weather 다양성 우선
```

생성 결과:

```text
outputs/experiment_001/
├── selected_additional_manifest.json
├── selected_additional_details.csv
├── selected_additional_files.txt
└── selection_summary.csv
```

실제로 추가할 파일 목록:

```text
outputs/experiment_001/selected_additional_files.txt
```

---

## 6.5 Base replay 혼합 목록 생성

```bash
python bench2drive_flywheel.py build --config config.json
```

기본 설정:

```json
{
  "replay": {
    "base_replay_ratio": 0.4
  }
}
```

의미:

```text
Base replay 약 40%
신규 취약 Scenario 데이터 약 60%
```

생성 결과:

```text
outputs/experiment_001/
├── combined_unique_manifest.json
├── train_sampling_plan.csv
├── train_files_mixed.txt
└── training_plan_summary.json
```

Fine-tuning에 연결할 핵심 파일:

```text
outputs/experiment_001/train_files_mixed.txt
```

동일 파일이 여러 번 나타날 수 있습니다. 이는 oversampling을 위한 정상적인 결과입니다.

---

## 6.6 전체 분석·선택·혼합을 한 번에 실행

```bash
python bench2drive_flywheel.py run-all --config config.json
```

외부 학습 명령을 설정하지 않은 기본 상태에서는 다음만 실행됩니다.

```text
result.json 분석
→ 취약 Scenario 선택
→ 추가 데이터 선택
→ Base replay 혼합 목록 생성
```

---

# 7. 취약 데이터 선택 설정

`config.json`:

```json
{
  "selection": {
    "quota_per_scenario": 100,
    "scenario_quotas": {},
    "max_weak_scenarios": null,
    "failed_condition_focus_fraction": 0.5,
    "random_seed": 42
  }
}
```

각 항목:

| 설정 | 의미 |
|---|---|
| `quota_per_scenario` | 취약 Scenario 하나당 기본 선택 개수 |
| `scenario_quotas` | 특정 Scenario만 별도 개수 지정 |
| `max_weak_scenarios` | 상위 몇 개 취약 Scenario만 처리할지 설정 |
| `failed_condition_focus_fraction` | 실패 Town 또는 Weather를 우선할 비율 |
| `random_seed` | 선택 결과 재현용 seed |

특정 Scenario를 더 많이 선택하는 예:

```json
{
  "selection": {
    "quota_per_scenario": 100,
    "scenario_quotas": {
      "LaneChange": 200,
      "YieldToEmergencyVehicle": 150
    }
  }
}
```

상위 3개 취약 Scenario만 처리하는 예:

```json
{
  "selection": {
    "max_weak_scenarios": 3
  }
}
```

---

# 8. Fine-tuning 전후 비교

Fine-tuned 모델을 동일한 평가 route에서 다시 실행하고 다음 파일을 저장합니다.

```text
results/result_finetuned.json
```

`config.json`:

```json
{
  "paths": {
    "evaluation_results": "results/result.json",
    "evaluation_results_finetuned": "results/result_finetuned.json"
  }
}
```

실행:

```bash
python bench2drive_flywheel.py compare --config config.json
```

생성 결과:+

```text
outputs/experiment_001/
├── scenario_before_after.csv
├── ability_before_after_table2.csv
└── ability_before_after_target.csv
```

- `scenario_before_after.csv`: Scenario별 DS, RC, 성공률 변화
- `ability_before_after_table2.csv`: Table 2 다중 Ability 기준 변화
- `ability_before_after_target.csv`: 평가 계획에 지정한 Target Ability 기준 변화

---

# 9. 선택된 Full 데이터 다운로드

Hugging Face 저장소에서 선택된 파일만 다운로드하려면 먼저 설치합니다.

```bash
pip install huggingface_hub
```

`config.json`:

```json
{
  "huggingface": {
    "enabled": true,
    "repo_id": "OWNER/DATASET_REPOSITORY",
    "remote_prefix": ""
  }
}
```

실행:

```bash
python bench2drive_flywheel.py download --config config.json
```

저장 위치:

```text
downloads/experiment_001/
```

저장소 내부에서 archive가 하위 폴더에 있으면 `remote_prefix`에 해당 폴더를 입력합니다.

---

# 10. 실제 운영 순서

```text
1. Base 및 Full manifest 배치
2. Base 데이터 분포 분석
3. Base 모델 학습
4. Bench2Drive closed-loop 평가
5. results/result.json 저장
6. validate 실행
7. Runtime 실패 route 재실행
8. 최종 result.json으로 analyze 실행
9. weak_scenarios.json 및 analysis_report.md 확인
10. select 실행
11. selected_additional_files.txt 확인
12. build 실행
13. train_files_mixed.txt를 Fine-tuning dataset loader에 연결
14. Fine-tuning
15. 동일 route 재평가
16. result_finetuned.json 저장
17. compare 실행
```

---

# 11. 최소 명령 모음

```bash
python bench2drive_flywheel.py base-distribution --config config.json
python bench2drive_flywheel.py validate --config config.json
python bench2drive_flywheel.py analyze --config config.json
python bench2drive_flywheel.py select --config config.json
python bench2drive_flywheel.py build --config config.json
```

한 번에 실행:

```bash
python bench2drive_flywheel.py run-all --config config.json
```

Fine-tuning 전후 비교:

```bash
python bench2drive_flywheel.py compare --config config.json
```
