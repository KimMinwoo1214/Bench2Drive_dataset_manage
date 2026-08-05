# Bench2Drive Data Flywheel & Analysis Toolkit

Bench2Drive closed-loop 평가 결과를 분석하여 취약 시나리오를 찾고, Full 데이터에서 추가 학습 데이터를 선택한 뒤 Base replay와 혼합하는 데이터 플라이휠 도구입니다.

추가로 Bench2Drive annotation의 `command_near`, Ego 차선 변경, 신호등 `affects_ego` 라벨을 분석·검증하기 위한 유틸리티와 공식 평가·시각화 도구를 포함합니다.

## 주요 기능

| 구분 | 기능 |
|---|---|
| 평가 결과 분석 | 원본 `result.json`에서 DS, RC, infraction, runtime failure 집계 |
| 취약 시나리오 선정 | 시나리오별 성능을 바탕으로 추가 데이터 수집 대상 선정 |
| 데이터 선택 | Full manifest에서 취약 시나리오 데이터를 선택하고 Base 중복 제외 |
| Replay 구성 | 신규 데이터와 Base replay를 혼합한 fine-tuning 목록 생성 |
| 전후 비교 | Base 모델과 fine-tuned 모델의 시나리오·Ability 성능 비교 |
| Annotation 분석 | `command_near`, Ego lane ID 변화, 신호등 적용 라벨 분석 |
| 신호등 라벨 보정 | Ego 전체 궤적을 이용해 누락된 `affects_ego` 후보 검출 및 선택적 보정 |
| 공식 도구 연동 | Bench2Drive 평가 JSON 병합, Ability 평가, smoothness 계산, 시각화 |

---

## 전체 워크플로

```text
Base manifest + Full manifest
            │
            ├─ base-distribution
            │    └─ 기존 학습 데이터 분포 확인
            │
Base 모델 학습 및 Bench2Drive 평가
            │
            └─ results/result.json
                    │
                    ├─ validate
                    │    └─ route 수, runtime failure, 입력 상태 검증
                    │
                    ├─ analyze
                    │    └─ 시나리오·Ability 성능 및 취약 시나리오 분석
                    │
                    ├─ select
                    │    └─ Full 데이터에서 추가 데이터 선택
                    │
                    └─ build
                         └─ 신규 데이터 + Base replay 혼합
                                  │
                                  └─ Fine-tuning
                                           │
                                           └─ result_finetuned.json
                                                    │
                                                    └─ compare
                                                         └─ 전후 성능 비교
```

Annotation 분석 도구는 위 과정과 별도로 또는 데이터 검증 단계에서 사용할 수 있습니다.

```text
원본 anno/*.json.gz
        │
        ├─ read_json_gz.py
        │    └─ command_near 분포 및 후보 시나리오 검색
        │
        ├─ check_ego_lane_change(1).py
        │    └─ 실제 lane_id 변경 시점 확인
        │
        ├─ b2d_traffic_light_relabeler.py
        │    └─ 신호등 통과 이벤트 및 누락 라벨 검출
        │
        └─ traffic_light_affect.py
             └─ affects_ego=true 라벨 검증
```

---

## 저장소 구조

```text
.
├── bench2drive_flywheel.py
├── ability_map.json
├── config.json
│
├── b2d_traffic_light_relabeler.py
├── check_ego_lane_change(1).py
├── read_json_gz.py
├── traffic_light_affect.py
│
├── data/
│   ├── bench2drive_base_1000.json
│   └── bench2drive_full+sup_13638.json
├── results/
│   ├── result.json
│   └── result_finetuned.json          # 선택 사항
├── inputs/
│   └── evaluation_plan.csv            # 선택 사항
├── outputs/
├── downloads/
└── b2d_check/
    ├── tools/
    ├── leaderboard/
    └── maps/
```

실제 저장소 구조가 다르면 `config.json`의 경로를 현재 파일 위치에 맞게 수정하십시오.

---

## 요구 사항

### 기본 분석 도구

- Python 3
- `bench2drive_flywheel.py`와 annotation 분석 스크립트의 기본 기능은 제공된 코드 기준으로 실행합니다.
- `b2d_traffic_light_relabeler.py`, `check_ego_lane_change(1).py`, `read_json_gz.py`, `traffic_light_affect.py`는 Python 표준 라이브러리만 사용합니다.

Python 버전 확인:

```bash
python --version
```

환경에 따라 이후 명령의 `python`을 `python3`로 바꾸십시오.

### 선택 데이터 다운로드

Hugging Face 저장소에서 선택 파일만 다운로드할 때 필요합니다.

```bash
pip install huggingface_hub
```

### Bench2Drive 공식 도구

`b2d_check/`의 시각화, Ability 평가, CARLA map 기반 기능은 Bench2Drive와 CARLA가 정상적으로 구성된 환경이 필요합니다.

---

## 입력 데이터

### 1. 학습 데이터 manifest

다음 파일을 `data/`에 배치합니다.

```text
data/bench2drive_base_1000.json
data/bench2drive_full+sup_13638.json
```

### 2. 평가 결과

Bench2Drive 평가기가 생성한 원본 결과를 그대로 사용합니다. 별도 CSV 변환은 필요하지 않습니다.

```text
results/result.json
```

Fine-tuning 전후 비교를 수행하려면 다음 파일도 배치합니다.

```text
results/result_finetuned.json
```

### 3. Annotation 데이터

Annotation 분석 스크립트는 다음 구조를 사용합니다.

```text
Scenario/
└── ScenarioName/
    └── anno/
        ├── 00000.json.gz
        ├── 00001.json.gz
        └── ...
```

`json.gz`는 압축을 해제하지 않고 직접 읽습니다.

주요 필드 예시는 다음과 같습니다.

```json
{
  "x": 12.3,
  "y": 45.6,
  "command_near": 4,
  "bounding_boxes": [
    {
      "class": "ego_vehicle",
      "location": [12.3, 45.6, 0.1],
      "road_id": 67,
      "section_id": 0,
      "lane_id": -1
    },
    {
      "class": "traffic_light",
      "id": 14206,
      "state": 0,
      "trigger_volume_location": [18.0, 51.0, 0.0],
      "affects_ego": false
    }
  ]
}
```

---

# 빠른 시작

## 1. 프로젝트 폴더로 이동

```bash
cd /path/to/project
```

## 2. `config.json` 설정

기본 경로 예시:

```json
{
  "evaluation": {
    "expected_routes": 220
  },
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

전체 220개 route가 아니라 10개 route만 평가했다면 반드시 수정합니다.

```json
{
  "evaluation": {
    "expected_routes": 10
  }
}
```

## 3. 입력 검증

```bash
python bench2drive_flywheel.py validate --config config.json
```

## 4. 분석·선택·혼합 실행

```bash
python bench2drive_flywheel.py run-all --config config.json
```

`run-all`은 기본적으로 다음 과정을 실행합니다.

```text
result.json 분석
→ 취약 시나리오 선정
→ Full 데이터 선택
→ Base replay 혼합 목록 생성
```

## 5. 핵심 결과 확인

```text
outputs/experiment_001/
├── analysis_report.md
├── runtime_rerun_routes.csv
├── weak_scenarios.json
├── selected_additional_files.txt
└── train_files_mixed.txt
```

- `analysis_report.md`: 평가 결과 요약
- `runtime_rerun_routes.csv`: 재실행이 필요한 runtime failure route
- `weak_scenarios.json`: 취약 시나리오 목록
- `selected_additional_files.txt`: Full 데이터에서 추가할 파일 목록
- `train_files_mixed.txt`: Fine-tuning 데이터 로더에 연결할 최종 혼합 목록

---

# Data Flywheel 명령

## Base 데이터 분포 분석

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

Ability는 서로 중복될 수 있으므로 Ability별 비율의 합이 100%를 넘을 수 있습니다.

## 입력 검증

```bash
python bench2drive_flywheel.py validate --config config.json
```

다음 항목을 확인합니다.

- 평가 route 개수
- runtime failure route 개수
- 성공 route 개수
- 중복 route ID
- Ability에 매핑되지 않은 시나리오
- 평가 계획과 실제 route ID의 일치 여부
- Base 및 Full manifest 파일 상태

결과:

```text
outputs/experiment_001/validation.json
```

`warnings`에 route 수 불일치가 표시되면 `expected_routes`와 실제 `result.json`의 route 수를 확인하십시오.

## 평가 결과 분석

```bash
python bench2drive_flywheel.py analyze --config config.json
```

처리 내용:

```text
result.json의 _checkpoint.records 읽기
→ 시나리오 이름 정규화
→ DS, RC, Infraction Penalty 파싱
→ Collision, Blocked, Timeout 집계
→ TickRuntime 등 runtime failure 분리
→ 취약 시나리오 판정
→ Ability 결과 집계
```

주요 결과:

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

### Runtime failure 처리

다음과 같은 실행 환경 실패는 기본적으로 취약 시나리오 선정에서 제외됩니다.

```text
Failed - TickRuntime
Simulation crashed
Agent setup failed
```

`runtime_rerun_routes.csv`에 기록된 route를 재실행한 뒤, 확정된 `result.json`으로 다시 분석하십시오.

## 추가 데이터 선택

```bash
python bench2drive_flywheel.py select --config config.json
```

선택 과정:

```text
취약 시나리오 확인
→ Full manifest에서 동일 시나리오 검색
→ Base에 이미 포함된 파일 제외
→ 실패 Town 또는 Weather 일부 우선
→ 나머지는 Town과 Weather 다양성 우선
```

결과:

```text
outputs/experiment_001/
├── selected_additional_manifest.json
├── selected_additional_details.csv
├── selected_additional_files.txt
└── selection_summary.csv
```

## Base replay 혼합 목록 생성

```bash
python bench2drive_flywheel.py build --config config.json
```

기본 replay 설정:

```json
{
  "replay": {
    "base_replay_ratio": 0.4
  }
}
```

기본 해석:

```text
Base replay 약 40%
신규 취약 시나리오 데이터 약 60%
```

결과:

```text
outputs/experiment_001/
├── combined_unique_manifest.json
├── train_sampling_plan.csv
├── train_files_mixed.txt
└── training_plan_summary.json
```

`train_files_mixed.txt`에 동일 파일이 여러 번 나타날 수 있습니다. 이는 oversampling을 위한 정상적인 결과입니다.

## Fine-tuning 전후 비교

`config.json`에 fine-tuned 결과 경로를 설정합니다.

```json
{
  "paths": {
    "evaluation_results": "results/result.json",
    "evaluation_results_finetuned": "results/result_finetuned.json"
  }
}
```

동일한 평가 route를 사용한 결과로 실행합니다.

```bash
python bench2drive_flywheel.py compare --config config.json
```

결과:

```text
outputs/experiment_001/
├── scenario_before_after.csv
├── ability_before_after_table2.csv
└── ability_before_after_target.csv
```

## 선택 데이터 다운로드

`config.json` 예시:

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

---

# 선택 평가 Route의 Ability 지정

5개 Ability에서 각각 일부 route만 직접 선정한 실험은 `inputs/evaluation_plan.csv`에 target Ability를 지정할 수 있습니다.

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

`config.json`:

```json
{
  "paths": {
    "evaluation_plan": "inputs/evaluation_plan.csv"
  }
}
```

220개 전체 평가에서 Bench2Drive Table 2 기준으로 Ability를 중복 집계하려면 다음과 같이 설정합니다.

```json
{
  "paths": {
    "evaluation_plan": null
  }
}
```

---

# 데이터 선택 설정

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

| 설정 | 의미 |
|---|---|
| `quota_per_scenario` | 취약 시나리오 하나당 기본 선택 개수 |
| `scenario_quotas` | 특정 시나리오의 선택 개수 개별 지정 |
| `max_weak_scenarios` | 상위 몇 개 취약 시나리오만 처리할지 지정 |
| `failed_condition_focus_fraction` | 실패 Town 또는 Weather를 우선 선택할 비율 |
| `random_seed` | 선택 결과 재현을 위한 seed |

특정 시나리오의 quota를 변경하는 예:

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

상위 3개 취약 시나리오만 사용하는 예:

```json
{
  "selection": {
    "max_weak_scenarios": 3
  }
}
```

---

# Annotation 분석 도구

## 1. `command_near` 분포 분석

파일:

```text
read_json_gz.py
```

전체·시나리오별 명령 분포, 연속 구간 수, 선택 명령이 포함된 시나리오를 집계합니다.

### 명령 ID

| ID | 이름 |
|---:|---|
| -1 | `VOID` |
| 0 | `VOID_OR_UNKNOWN` |
| 1 | `LEFT` |
| 2 | `RIGHT` |
| 3 | `STRAIGHT` |
| 4 | `LANEFOLLOW` |
| 5 | `CHANGELANELEFT` |
| 6 | `CHANGELANERIGHT` |

숫자, 문자열, enum 형태의 문자열을 정규화합니다.

### 기본 실행

```bash
python read_json_gz.py \
  --input "/path/to/Scenario" \
  --output-dir "/path/to/command_near_result"
```

### 차선 변경 명령이 포함된 시나리오 검색

```bash
python read_json_gz.py \
  --input "/path/to/Scenario" \
  --output-dir "/path/to/command_near_result" \
  --selected-command "CHANGELANELEFT,CHANGELANERIGHT" \
  --min-selected-frames 3
```

### 결과

| 파일 | 내용 |
|---|---|
| `overall_command_distribution.csv` | 전체 명령별 프레임 수, 비율, 시나리오 수, 연속 구간 수 |
| `clip_command_distribution.csv` | 시나리오별 명령 분포 |
| `frame_commands.csv` | 프레임별 원본 값과 정규화된 명령 |
| `selected_clips.txt` | 선택 조건을 만족한 시나리오 경로 |
| `scan_errors.csv` | 압축 해제, JSON 파싱, 필드 처리 오류 |

> `command_near`는 planner가 기록한 경로 명령입니다. Ego가 실제로 해당 행동을 수행했다는 증거는 아니므로 lane ID 변화와 함께 확인해야 합니다.

## 2. Ego 차선 변경 분석

파일:

```text
check_ego_lane_change(1).py
```

각 프레임의 다음 키를 이용해 lane 변화 시점을 찾습니다.

```text
(road_id, section_id, lane_id)
```

### 실행

```bash
python "check_ego_lane_change(1).py" \
  "/path/to/scenario" \
  --min-stable-frames 3 \
  --output-dir "/path/to/lane_analysis"
```

동일한 `road_id`, `section_id`에서 `lane_id`만 바뀌면 `LANE_CHANGE`로 분류합니다. `road_id` 또는 `section_id`가 바뀌면 `ROAD_OR_SECTION_CHANGE`로 분리합니다.

기본적으로 변경 전·후 구간이 각각 3프레임 이상 유지되어야 실제 차선 변경으로 집계합니다.

### 결과

```text
lane_analysis/
├── ego_lane_trace.csv
└── ego_lane_events.csv
```

이 스크립트는 Ego 중심점에 CARLA가 할당한 lane ID 변화를 분석합니다. 차량 bounding box가 실제 차선 경계를 침범한 시점은 계산하지 않습니다.

## 3. 신호등 라벨 분석 및 보정

파일:

```text
b2d_traffic_light_relabeler.py
```

Ego 전체 궤적과 신호등 `trigger_volume_location` 중심을 이용해 실제 통과 이벤트를 찾고 기존 `affects_ego` 라벨과 비교합니다.

### 분석만 수행

```bash
python b2d_traffic_light_relabeler.py \
  --input "/path/to/Scenario" \
  --output "/path/to/traffic_light_result"
```

이 실행은 원본 annotation을 수정하지 않습니다.

### 보정 annotation 복사본 생성

```bash
python b2d_traffic_light_relabeler.py \
  --input "/path/to/Scenario" \
  --output "/path/to/traffic_light_result_v2" \
  --write-corrected-anno
```

원본은 유지되고 출력 폴더 아래 `corrected_anno/`에 복사본이 생성됩니다. 명확하게 복구된 프레임에서는 선택된 traffic light 객체의 다음 값만 변경됩니다.

```text
affects_ego: false → true
```

### 이벤트 상태

| 상태 | 의미 | 자동 보정 |
|---|---|---|
| `matched` | 통과 이벤트와 기존 라벨이 일치 | 기존 값 유지 |
| `missing_label` | 통과했지만 기존 `affects_ego=true`가 없음 | 명확한 단일 후보만 보정 |
| `approach_only` | trigger 부근에 접근했지만 통과가 확인되지 않음 | 보정하지 않음 |
| `ambiguous` | 후보가 겹치거나 통과 판정이 불명확 | 보정하지 않음 |
| `unrelated` | 같은 교차 이벤트의 다른 후보가 더 적합 | 보정하지 않음 |

### 주요 옵션

| 옵션 | 기본값 | 의미 |
|---|---:|---|
| `--approach-distance` | `40.0` | 통과 전 영향 구간을 역추적할 거리(m) |
| `--contact-radius` | `2.0` | trigger 중심 접촉 후보 거리(m) |
| `--max-step` | `5.0` | 연속 궤적으로 인정할 최대 프레임 이동 거리(m) |
| `--merge-gap-frames` | `10` | 근접 후보 이벤트 병합 간격 |
| `--crossing-margin` | `0.5` | trigger 전·후 통과 판정 여유 거리(m) |
| `--ambiguity-margin` | `0.75` | 후보 간 최소거리 차이의 모호성 기준(m) |
| `--event-index-tolerance` | `20` | 동일 교차 이벤트로 묶을 프레임 차이 |

### 결과

```text
traffic_light_result/
├── all_traffic_light_frame_labels.csv
├── all_traffic_light_frame_labels.jsonl
├── all_traffic_light_events.csv
├── review_queue.csv
└── ScenarioName/
    ├── traffic_light_frame_labels.csv
    ├── traffic_light_events.csv
    └── corrected_anno/              # --write-corrected-anno 사용 시
```

권장 검토 순서:

1. `all_traffic_light_events.csv`에서 `missing_label` 확인
2. `review_queue.csv`에서 `ambiguous`, `approach_only` 확인
3. 시작·종료 프레임을 영상 또는 top-down 시각화로 검증
4. 검증 후 `--write-corrected-anno` 실행
5. 원본 대신 학습 데이터 로더의 annotation 경로만 `corrected_anno`로 변경

> 이 도구는 trigger volume의 회전·크기를 이용한 사각형 교차가 아니라 trigger 중심점과 Ego 궤적을 기준으로 하는 반자동 보정기입니다. `review_queue.csv` 검토 없이 전체 학습 데이터에 바로 적용하지 마십시오.

## 4. `affects_ego=true` 신호등 추출

파일:

```text
traffic_light_affect.py
```

현재 annotation에서 다음 조건을 만족하는 신호등을 프레임별 CSV로 추출합니다.

```text
class == "traffic_light"
affects_ego is True
```

### 원본 annotation 확인

```bash
python traffic_light_affect.py \
  --input "/path/to/scenario/anno" \
  --output "/path/to/original_traffic_lights.csv"
```

### 보정본 확인

```bash
python traffic_light_affect.py \
  --input "/path/to/result/ScenarioName/corrected_anno" \
  --output "/path/to/corrected_traffic_lights.csv"
```

원본과 보정본의 프레임별 `traffic_light_id`를 비교하여 새로 `true`가 된 구간을 확인할 수 있습니다.

---

# Bench2Drive 공식 보조 도구

`b2d_check/`는 직접 제작한 분석 알고리즘이 아니라 Bench2Drive의 데이터 확인, 시각화, 평가를 위해 정리한 공식 코드 묶음입니다.

| 파일 | 용도 |
|---|---|
| `tools/data_collect.py` | CARLA 센서 데이터 및 annotation 수집 |
| `tools/visualize.py` | 카메라 bbox, top-down, road, LiDAR 시각화 |
| `tools/merge_route_json.py` | 여러 평가 JSON을 `merged.json`으로 병합 |
| `tools/ability_benchmark.py` | 5개 Ability별 성공률 계산 |
| `tools/efficiency_smoothness_benchmark.py` | Driving Efficiency와 Smoothness 계산 |
| `tools/generate_video.py` | 영상에 speed, steer, throttle, brake 표시 |
| `tools/split_xml.py` | route XML 분할 |
| `tools/gen_hdmap.py` | CARLA Town HD map 생성 |
| `tools/clean_carla.sh` | 남아 있는 CARLA·평가 프로세스 종료 |

## 평가 JSON 병합

```bash
python b2d_check/tools/merge_route_json.py \
  -f "/path/to/evaluation_json_folder"
```

공식 병합 스크립트는 전체 220개 route를 기준으로 Driving Score와 Success Rate를 계산합니다.

```text
Driving Score = Σ route score_composed / 220
Success Rate  = successful routes / 220
```

220개보다 적은 route만 평가한 경우 공식 전체 점수를 그대로 해석하지 마십시오. 예를 들어 10개 route 실험은 route별 `score_composed`, `score_route`, infraction을 직접 비교하거나 분모 10 기준의 별도 요약을 사용해야 합니다.

## Ability 평가

```bash
python b2d_check/tools/ability_benchmark.py \
  -f "b2d_check/leaderboard/data/bench2drive220.xml" \
  -r "/path/to/merged.json"
```

평가 Ability:

- Overtaking
- Merging
- Emergency Brake
- Give Way
- Traffic Signs

`ability_benchmark.py`는 CARLA map과 `GlobalRoutePlanner`를 사용하므로 CARLA 서버 실행 환경이 필요합니다.

## Efficiency 및 Smoothness

```bash
python b2d_check/tools/efficiency_smoothness_benchmark.py \
  -f "/path/to/merged.json" \
  -m "/path/to/metric_folder"
```

공식 코드의 `_z_yaw_acc`는 이름과 주석상 yaw acceleration이지만 현재 구현에서는 yaw rate에 미분 옵션을 적용하지 않고 필터만 수행합니다. 논문 수식과 완전히 동일한 지표가 필요하면 이 부분을 별도로 검증해야 합니다.

또한 `min_speed_infractions`가 없는 route는 efficiency 평균 목록에서 제외될 수 있으므로 출력 분모가 전체 route 수와 다를 수 있습니다.

## 시각화

```bash
cd b2d_check

python tools/visualize.py \
  -f "/path/to/scenario" \
  -m 12
```

`-m 12`는 `./maps/Town12_HD_map.npz`를 사용한다는 의미입니다. 시나리오 Town과 다른 map을 지정하면 `road_id` 관련 `KeyError`가 발생할 수 있습니다.

---

# 권장 운영 순서

1. Base 및 Full manifest를 `data/`에 배치합니다.
2. `base-distribution`으로 기존 데이터 분포를 확인합니다.
3. Base 모델을 학습합니다.
4. Bench2Drive closed-loop 평가를 수행합니다.
5. 원본 결과를 `results/result.json`에 저장합니다.
6. `validate`를 실행합니다.
7. `runtime_rerun_routes.csv`의 route를 재실행합니다.
8. 확정된 결과로 `analyze`를 실행합니다.
9. `weak_scenarios.json`과 `analysis_report.md`를 검토합니다.
10. 필요한 경우 annotation 분석 도구로 실패 구간과 데이터 라벨을 확인합니다.
11. `select`로 Full 데이터에서 추가 데이터를 선택합니다.
12. `selected_additional_files.txt`를 검토합니다.
13. `build`로 Base replay 혼합 목록을 생성합니다.
14. `train_files_mixed.txt`를 fine-tuning 데이터 로더에 연결합니다.
15. Fine-tuning 후 동일 route로 재평가합니다.
16. 결과를 `results/result_finetuned.json`에 저장합니다.
17. `compare`로 전후 성능을 비교합니다.

---

# 주의 사항

1. **원본 annotation을 덮어쓰지 마십시오.** 신호등 보정은 `corrected_anno/` 복사본으로 수행합니다.
2. **Runtime failure는 모델 취약성과 구분하십시오.** 먼저 해당 route를 재실행한 뒤 분석해야 합니다.
3. **`expected_routes`를 실제 평가 route 수와 일치시키십시오.** 10개 route 실험에 220을 사용하면 집계 해석이 잘못됩니다.
4. **Ability 집계는 중복될 수 있습니다.** 하나의 시나리오가 여러 Ability에 포함되면 비율 합이 100%를 넘을 수 있습니다.
5. **`command_near`와 실제 행동을 동일시하지 마십시오.** 차선 변경 여부는 lane ID 변화와 함께 확인해야 합니다.
6. **신호등 자동 복구 결과는 검토가 필요합니다.** `ambiguous`, `approach_only`, `original_conflict`는 사람이 확인해야 합니다.
7. **시각화 map과 시나리오 Town을 일치시키십시오.** 잘못된 Town map은 `road_id KeyError`의 주요 원인입니다.
8. **출력 폴더를 입력 데이터 내부에 두지 않는 것이 안전합니다.** 재귀 검색 스크립트가 출력 파일을 다시 읽을 수 있습니다.

---

# 문제 해결

## `validation.json`에 route 수 불일치 경고가 발생하는 경우

- `config.json`의 `evaluation.expected_routes`를 확인합니다.
- `result.json`의 `_checkpoint.records` 개수를 확인합니다.
- 중복 route 또는 누락된 평가 결과가 있는지 확인합니다.

## `runtime_rerun_routes.csv`에 route가 기록되는 경우

해당 route는 취약 시나리오 분석 전에 재실행하십시오. Simulation crash나 agent setup failure를 모델 성능 문제로 집계하면 안 됩니다.

## 신호등 이벤트가 검출되지 않는 경우

- Ego 좌표와 `trigger_volume_location`이 동일 좌표계인지 확인합니다.
- 기본 `--contact-radius 2.0`으로 먼저 실행합니다.
- 필요할 때만 반경을 조금씩 늘립니다.

```bash
python b2d_traffic_light_relabeler.py \
  --input "/path/to/Scenario" \
  --output "/path/to/result" \
  --contact-radius 3.0
```

반경을 과도하게 늘리면 인접 차로나 다른 방향 신호등을 잘못 선택할 수 있습니다.

## 시각화에서 `road_id KeyError`가 발생하는 경우

예:

```text
KeyError: 67
```

다음을 확인합니다.

- 시나리오 Town과 `Town*_HD_map.npz`가 일치하는지
- map key가 정수인지 문자열인지
- 현재 annotation의 `road_id`가 해당 map에 존재하는지

## 차선 변경 명령은 있지만 lane change가 검출되지 않는 경우

`command_near`는 경로 명령이고 실제 차량 행동이 아닙니다. `ego_lane_events.csv`에서 `lane_id`가 안정적으로 변경되었는지 확인하십시오.

---

# 최소 명령 모음

```bash
# Base 분포
python bench2drive_flywheel.py base-distribution --config config.json

# 입력 검증
python bench2drive_flywheel.py validate --config config.json

# 결과 분석
python bench2drive_flywheel.py analyze --config config.json

# 추가 데이터 선택
python bench2drive_flywheel.py select --config config.json

# Replay 혼합 목록 생성
python bench2drive_flywheel.py build --config config.json

# 분석·선택·혼합 일괄 실행
python bench2drive_flywheel.py run-all --config config.json

# Fine-tuning 전후 비교
python bench2drive_flywheel.py compare --config config.json

# command_near 분석
python read_json_gz.py --input "/path/to/Scenario" --output-dir "/path/to/command_result"

# Ego lane change 분석
python "check_ego_lane_change(1).py" "/path/to/scenario" --min-stable-frames 3

# 신호등 분석
python b2d_traffic_light_relabeler.py --input "/path/to/Scenario" --output "/path/to/tl_result"

# affects_ego=true 확인
python traffic_light_affect.py --input "/path/to/scenario/anno" --output "/path/to/traffic_lights.csv"
```
