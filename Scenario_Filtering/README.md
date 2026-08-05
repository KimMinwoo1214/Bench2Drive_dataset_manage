# Bench2Drive 관련 제작 코드 사용 및 계산 방법

## 1. 문서 범위

현재 작업공간에서 직접 제작하거나 전달받아 사용하는 분석 코드는 다음 네 개입니다.

| 코드 | 목적 | 원본 수정 여부 |
|---|---|---|
| `b2d_traffic_light_relabeler.py` | Ego 전체 궤적으로 신호등 통과 이벤트를 찾고 누락된 `affects_ego`를 보정 | 기본 실행은 수정 안 함. `--write-corrected-anno` 사용 시 복사본만 생성 |
| `check_ego_lane_change(1).py` | 프레임별 Ego의 `road_id`, `section_id`, `lane_id`를 이용해 차선 변경 시점 검출 | 수정 안 함 |
| `read_json_gz.py` | `command_near`의 전체·시나리오별 분포, 연속 구간 수, 선택 명령이 포함된 시나리오를 집계 | 수정 안 함 |
| `traffic_light_affect.py` | 기존 또는 보정 annotation에서 `affects_ego=true`인 신호등을 프레임별로 추출 | 수정 안 함 |

`b2d_check/` 폴더는 직접 만든 알고리즘이 아니라 Bench2Drive의 데이터 확인, 시각화, 평가를 위해 정리한 공식 코드 묶음입니다. 이 문서 뒤쪽에 자주 사용하는 파일과 실행 방법만 따로 정리합니다.

---

## 2. 공통 입력 데이터

네 코드 모두 시나리오의 다음 annotation 구조를 사용합니다.

```text
Scenario/
└── 시나리오_이름/
    └── anno/
        ├── 00000.json.gz
        ├── 00001.json.gz
        ├── 00002.json.gz
        └── ...
```

`json.gz`는 JSON을 gzip으로 압축한 파일입니다. 미리 압축을 해제할 필요가 없습니다.

주요 annotation 필드는 다음과 같습니다.

```json
{
    "x": 12.3,
    "y": 45.6,
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

# 3. 신호등 라벨 보정 코드

## 3.1 파일

```text
b2d_traffic_light_relabeler.py
```

외부 패키지를 사용하지 않으며 Python 표준 라이브러리만 필요합니다.

## 3.2 목적

Bench2Drive annotation에서 두 번째 교차로의 신호등처럼 실제 Ego 경로에 영향을 주지만 `affects_ego=false`로 기록된 신호등을 찾기 위한 코드입니다.

사람이 모든 프레임을 확인하지 않고 다음 작업을 자동화합니다.

1. 시나리오 전체 프레임에서 Ego 궤적을 구성합니다.
2. 모든 신호등의 trigger 중심 위치를 수집합니다.
3. Ego가 trigger 중심 부근을 실제로 지나갔는지 검사합니다.
4. 기존 `affects_ego=true`와 비교합니다.
5. 명확한 누락은 `missing_label`, 불명확한 경우는 `ambiguous` 또는 `approach_only`로 분류합니다.
6. 프레임별 라벨 CSV와 이벤트 CSV를 생성합니다.
7. 옵션을 사용하면 원본 구조를 유지한 보정 annotation 복사본을 만듭니다.

## 3.3 가장 기본적인 실행 방법

### 단일 시나리오 처리

```bash
python3 b2d_traffic_light_relabeler.py \
  --input "/home/kmw/Summer_Intership/Carla/Scenario/시나리오_폴더" \
  --output "/home/kmw/Summer_Intership/Carla/traffic_light_result"
```

이 실행은 CSV와 JSONL 분석 결과만 만들고 annotation을 만들거나 수정하지 않습니다.

### 데이터셋 루트 아래 모든 시나리오 처리

```bash
python3 b2d_traffic_light_relabeler.py \
  --input "/home/kmw/Summer_Intership/Carla/Scenario" \
  --output "/home/kmw/Summer_Intership/Carla/traffic_light_result"
```

코드가 하위 폴더의 모든 `anno` 디렉터리를 재귀적으로 찾습니다.

### 보정된 `json.gz`까지 생성

```bash
python3 b2d_traffic_light_relabeler.py \
  --input "/home/kmw/Summer_Intership/Carla/Scenario" \
  --output "/home/kmw/Summer_Intership/Carla/traffic_light_result_v3" \
  --write-corrected-anno
```

기존 출력 폴더와 섞이지 않도록 새로운 출력 폴더를 사용하는 것이 안전합니다.

## 3.4 입력 경로로 사용할 수 있는 범위

`--input`에는 다음 중 하나를 지정할 수 있습니다.

| 입력 | 예시 |
|---|---|
| `anno` 폴더 | `.../Scenario01/anno` |
| 단일 시나리오 폴더 | `.../Scenario01` |
| 여러 시나리오가 들어 있는 루트 | `.../Scenario` |

단일 `json.gz` 파일은 입력할 수 없습니다. 신호등 통과 여부를 계산하려면 시나리오의 전체 궤적이 필요하기 때문입니다.

## 3.5 전체 계산 과정

### 3.5.1 Ego 위치와 신호등 정보 추출

각 프레임에서 다음 정보를 읽습니다.

- Ego 위치: `bounding_boxes`의 `class="ego_vehicle"` 또는 `class="ego"`인 객체의 `location[:2]`
- Ego 객체에서 위치를 찾지 못하면 최상위 `x`, `y` 사용
- 신호등 ID: `traffic_light.id`
- trigger 위치: `traffic_light.trigger_volume_location[:2]`
- 신호등 상태: `traffic_light.state`
- 원본 적용 여부: `traffic_light.affects_ego`

신호등 상태 값은 다음처럼 출력 이름으로 변환합니다.

| 값 | 이름 |
|---:|---|
| 0 | Red |
| 1 | Yellow |
| 2 | Green |
| 3 | Off |
| 4 | Unknown |

### 3.5.2 Ego 누적 이동 거리

프레임 `i-1`과 `i` 사이의 평면 이동 거리는 다음과 같습니다.

$$
\Delta s_i = \sqrt{(x_i-x_{i-1})^2+(y_i-y_{i-1})^2}
$$

기본값에서 `Δs_i ≤ 5 m`인 경우에만 정상적으로 연결된 궤적으로 판단하고 누적 거리에 더합니다.

$$
s_i = s_{i-1}+\Delta s_i
$$

한 프레임 사이의 이동이 `5 m`보다 크면 순간이동, 프레임 누락 또는 궤적 단절로 보고 해당 구간을 연결하지 않습니다.

### 3.5.3 trigger 접근 후보 검출

Ego와 신호등 trigger 중심 사이 거리는 다음과 같습니다.

$$
d_{i,l}=\sqrt{(x_i-x_l)^2+(y_i-y_l)^2}
$$

여기서 `i`는 프레임, `l`은 신호등입니다.

기본값으로 `d_i,l ≤ 2 m`이고 해당 프레임에 그 신호등 객체가 존재하면 trigger 접촉 후보로 봅니다.

후보 프레임 사이의 간격이 10프레임 이하이면 하나의 이벤트로 묶습니다.

### 3.5.4 trigger를 실제로 통과했는지 판정

trigger 주변 후보 구간의 앞과 뒤에서 trigger로부터 최소 `2.5 m` 떨어진 연결된 궤적점을 찾습니다.

```text
2.5 m = contact_radius 2.0 m + crossing_margin 0.5 m
```

앞쪽 점을 `B`, 뒤쪽 점을 `A`, trigger 중심을 `T`라고 하면 진행 방향 단위벡터는 다음과 같습니다.

$$
\mathbf{u}=\frac{A-B}{\lVert A-B\rVert}
$$

trigger를 기준으로 앞·뒤 점을 진행축에 투영합니다.

$$
p_B=(B-T)\cdot\mathbf{u}
$$

$$
p_A=(A-T)\cdot\mathbf{u}
$$

다음 세 조건을 모두 만족해야 정상 통과로 판단합니다.

1. `p_B < -0.5 m`
2. `p_A > +0.5 m`
3. 선분 `BA`와 trigger 중심 사이의 최단거리가 `2 m` 이하

즉, 단순히 trigger 가까이에 갔다는 것만으로는 부족하며, 진행 방향 기준으로 trigger 전방과 후방이 모두 궤적에 나타나야 합니다.

통과 프레임은 투영값이 처음으로 0보다 커진 프레임입니다.

### 3.5.5 신호등 영향 시작과 종료 프레임

종료 프레임은 통과 프레임 바로 전까지입니다.

```text
end_index = crossing_index - 1
```

시작 프레임은 종료 프레임부터 누적 이동 거리 기준으로 최대 40 m 이전까지 역방향 탐색해서 정합니다. 중간에 프레임 간 이동이 5 m를 초과하면 더 이상 이전 프레임으로 확장하지 않습니다.

따라서 두 번째 신호등이 304프레임에서 보였다고 해서 304~605 프레임 전체를 일괄적으로 `true`로 만들지 않습니다. 해당 신호등 접근 시작부터 실제 trigger 통과 직전까지만 보정 대상이 됩니다.

### 3.5.6 동일 교차로의 여러 신호등 후보 해결

다음 조건을 동시에 만족하면 같은 교차 이벤트의 경쟁 후보로 묶습니다.

- 통과 프레임 차이: 20프레임 이하
- trigger 중심 간 거리: 기본 설정에서는 8 m 이하

후보 선택 순서는 다음과 같습니다.

1. 기존 `affects_ego=true`가 정확히 하나 있으면 그 신호등을 선택합니다.
2. 그렇지 않으면 Ego 궤적과 trigger의 최소 거리가 가장 작은 신호등을 선택합니다.
3. 1위와 2위 후보의 최소거리 차이가 `0.75 m`보다 작으면 자동 선택하지 않고 `ambiguous`로 분류합니다.

### 3.5.7 기존 라벨과 비교한 이벤트 상태

| 상태 | 의미 | 자동 보정 |
|---|---|---|
| `matched` | 기하학적으로 통과했고 기존 `affects_ego=true`도 존재 | 기존 값 유지 |
| `missing_label` | 기하학적으로 통과했지만 기존 `affects_ego=true`가 없음 | 명확한 단일 후보만 보정 |
| `approach_only` | trigger 부근에 접근했지만 앞·뒤 궤적이 모두 없음 | 보정 안 함, 검토 필요 |
| `ambiguous` | trigger를 깨끗하게 통과하지 않았거나 후보가 겹침 | 보정 안 함, 검토 필요 |
| `unrelated` | 같은 교차 시점의 다른 후보가 더 적합 | 보정 안 함 |

### 3.5.8 프레임 라벨 우선순위

프레임별 최종 분석 라벨은 다음 순서로 결정합니다.

1. 원본에서 `affects_ego=true`가 하나 있으면 `original`로 유지합니다.
2. 한 프레임에 원본 `true`가 여러 개면 `original_conflict`로 분류하고 검토 대상으로 남깁니다.
3. 원본 라벨이 없는 프레임만 선택된 `missing_label` 이벤트의 `recovered` 라벨을 받습니다.
4. 원본 라벨과 복구 라벨의 ID가 충돌하면 자동 변경하지 않고 검토 대상으로 남깁니다.

## 3.6 기본 임계값과 변경 옵션

| 옵션 | 기본값 | 의미 |
|---|---:|---|
| `--approach-distance` | 40.0 m | 통과 전 신호등 영향 구간을 역추적할 거리 |
| `--contact-radius` | 2.0 m | Ego가 trigger 중심에 접촉했다고 볼 거리 |
| `--max-step` | 5.0 m | 연결된 연속 프레임으로 인정할 최대 이동 거리 |
| `--merge-gap-frames` | 10 | 근접 후보를 같은 이벤트로 묶을 최대 프레임 간격 |
| `--crossing-margin` | 0.5 m | trigger 전·후 통과 판정 여유 거리 |
| `--ambiguity-margin` | 0.75 m | 두 후보의 거리 차이가 이 값보다 작으면 모호 판정 |
| `--event-index-tolerance` | 20 | 같은 시점의 교차 이벤트로 묶을 프레임 차이 |

예를 들어 trigger 중심과 Ego 경로가 다소 떨어져 있어 아무 이벤트도 잡히지 않는 경우 다음처럼 접촉 반경을 늘릴 수 있습니다.

```bash
python3 b2d_traffic_light_relabeler.py \
  --input "/path/to/Scenario" \
  --output "/path/to/output" \
  --contact-radius 3.0
```

다만 임계값을 크게 만들수록 인접 차로나 다른 방향 신호등을 잘못 고를 가능성이 커집니다. 먼저 기본값 결과의 `review_queue.csv`를 확인하는 것이 좋습니다.

## 3.7 생성 결과

전체 출력 폴더에는 다음 파일이 생깁니다.

```text
traffic_light_result/
├── all_traffic_light_frame_labels.csv
├── all_traffic_light_frame_labels.jsonl
├── all_traffic_light_events.csv
├── review_queue.csv
└── 시나리오_이름/
    ├── traffic_light_frame_labels.csv
    ├── traffic_light_events.csv
    └── corrected_anno/              # 옵션 사용 시 생성
        ├── 00000.json.gz
        ├── 00001.json.gz
        └── ...
```

### `traffic_light_frame_labels.csv`

프레임별 최종 신호등 라벨입니다.

중요 열은 다음과 같습니다.

| 열 | 의미 |
|---|---|
| `frame` | 프레임 번호 |
| `relevant_light_id` | 해당 프레임에서 Ego에 적용되는 신호등 ID |
| `state`, `state_name` | 신호등 상태 값과 이름 |
| `label_source` | `original`, `recovered`, `none`, `original_conflict` |
| `confidence` | 자동 판정 신뢰도 |
| `event_id` | 이벤트 ID. 예: `TL002` |
| `needs_review` | 사람이 확인해야 하는지 여부 |
| `reason` | 판정 사유 |

### `traffic_light_events.csv`

신호등 통과 이벤트 단위 요약입니다.

| 열 | 의미 |
|---|---|
| `status` | `matched`, `missing_label`, `approach_only`, `ambiguous`, `unrelated` |
| `start_frame` | 신호등 영향 시작 프레임 |
| `crossing_frame` | trigger 중심을 지난 것으로 판정된 프레임 |
| `end_frame` | 신호등 영향 종료 프레임 |
| `min_distance_m` | Ego 궤적과 trigger 중심의 최소거리 |
| `original_true_count` | 이벤트 주변의 원본 `affects_ego=true` 프레임 수 |
| `labelled_frame_count` | 해당 이벤트에 연결된 프레임 수 |

### `review_queue.csv`

`needs_review=true`인 이벤트만 모은 파일입니다. 전체 프레임을 사람이 확인하지 말고 이 파일의 이벤트만 우선 확인하면 됩니다.

## 3.8 보정된 annotation에서 실제로 바뀌는 값

`--write-corrected-anno`를 사용해도 원본 파일은 수정되지 않습니다. 출력의 `corrected_anno`에 복사본을 만듭니다.

보정본에는 새 최상위 필드나 `training_affects_ego`를 추가하지 않습니다. 명확한 `recovered` 프레임에서 선택된 신호등 객체의 기존 값만 바뀝니다.

```json
{
    "class": "traffic_light",
    "type_id": "traffic.traffic_light",
    "id": 14206,
    "affects_ego": true
}
```

즉 의미상 차이는 다음 하나입니다.

```text
대상 traffic_light의 affects_ego: false → true
```

나머지 최상위 필드, 객체 수, 차량·센서·표지판 값은 그대로 유지합니다. JSON은 원본과 같은 4칸 들여쓰기로 저장합니다. 다만 gzip 파일은 다시 압축되므로 압축 파일의 바이트 크기나 해시는 원본과 달라질 수 있습니다.

## 3.9 결과 확인 순서

권장 확인 순서는 다음과 같습니다.

1. `all_traffic_light_events.csv`에서 `missing_label` 개수를 확인합니다.
2. `review_queue.csv`에서 `ambiguous`와 `approach_only`만 확인합니다.
3. `traffic_light_frame_labels.csv`에서 보정 시작·종료 프레임이 실제 교차로 구간과 맞는지 봅니다.
4. 문제가 없을 때만 `--write-corrected-anno`로 보정본을 생성합니다.
5. 원본 시나리오 전체를 교체하지 말고 학습·시각화 코드의 annotation 입력만 `corrected_anno`로 지정합니다.

## 3.10 시각화 시 주의사항

카메라 영상, 객체 위치, 신호등 `state`는 바꾸지 않으므로 장면 자체는 동일합니다. 시각화 코드가 `affects_ego=true`인 신호등을 강조할 때만 표시가 달라집니다.

시각화 코드가 항상 `시나리오/anno`를 읽는 구조라면 원본을 덮어쓰지 말고 다음 중 하나를 사용하십시오.

- 시각화 코드의 annotation 경로를 `corrected_anno`로 변경
- 테스트용 시나리오 복사본을 만들고 그 복사본의 `anno`만 보정본으로 교체

다음 오류는 신호등 보정과 직접적인 관계가 없습니다.

```text
KeyError: 67
road_points = map_info[anno['bounding_boxes'][0]['road_id']]
```

이 오류는 Ego의 `road_id=67`이 지정한 HD map에 없다는 뜻입니다. 시나리오 Town과 `Town*_HD_map.npz`가 일치하는지, map key가 정수 `67`인지 문자열 `"67"`인지 확인해야 합니다.

## 3.11 이 코드의 한계

이 부분은 반드시 알고 사용해야 합니다.

1. 현재 코드는 trigger volume의 회전·크기를 이용한 사각형 교차 검사가 아니라 `trigger_volume_location`의 중심점을 기준으로 계산합니다.
2. Ego의 yaw, 차로 방향, `road_id`, `lane_id`를 신호등 선택에 직접 사용하지 않습니다.
3. trigger 중심이 실제 주행 경로에서 2 m보다 멀면 실제 관련 신호등도 놓칠 수 있습니다.
4. 같은 위치에 여러 방향 신호등 trigger가 겹치면 `ambiguous`가 될 수 있습니다.
5. 궤적이 trigger 직전에서 종료되면 `approach_only`가 되며 자동 보정하지 않습니다.
6. 자동 복구 결과를 바로 전체 학습 데이터에 적용하기보다 `review_queue.csv`와 대표 시각화를 먼저 확인해야 합니다.

따라서 이 코드는 완전한 정답 생성기라기보다 명확한 누락을 자동 복구하고 모호한 경우만 사람에게 넘기는 반자동 보정기입니다.

---

# 4. Ego 차선 변경 분석 코드

## 4.1 파일

```text
check_ego_lane_change(1).py
```

파일명에 괄호가 있으므로 셸 명령에서는 파일명을 따옴표로 감싸는 것이 안전합니다. 괄호 없는 이름으로 바꾸어 사용해도 코드 동작은 같습니다.

외부 패키지는 필요하지 않습니다.

## 4.2 목적

각 프레임의 Ego 객체에 기록된 다음 조합을 이용해 차선 변경 시점을 찾습니다.

```text
(road_id, section_id, lane_id)
```

이 코드는 신호등 라벨을 수정하지 않습니다. Ego가 실제로 어느 프레임에서 다른 `lane_id`로 이동했는지 분석하는 코드입니다.

## 4.3 실행 방법

### 시나리오 폴더 입력

```bash
python3 "check_ego_lane_change(1).py" \
  "/home/kmw/Summer_Intership/Carla/Scenario/시나리오_폴더"
```

### `anno` 폴더 직접 입력

```bash
python3 "check_ego_lane_change(1).py" \
  "/home/kmw/Summer_Intership/Carla/Scenario/시나리오_폴더/anno"
```

### 출력 폴더와 안정 프레임 수 지정

```bash
python3 "check_ego_lane_change(1).py" \
  "/path/to/scenario" \
  --min-stable-frames 5 \
  --output-dir "/path/to/lane_analysis"
```

기본 출력 폴더는 다음입니다.

```text
시나리오_폴더/lane_analysis/
```

## 4.4 계산 방법

### 4.4.1 프레임 정렬

파일명에서 마지막 숫자를 추출해 프레임 번호로 사용합니다.

```text
00000.json.gz → 0
00001.json.gz → 1
```

그 숫자를 기준으로 정렬합니다.

### 4.4.2 Ego 차선 키 구성

각 프레임에서 `class="ego_vehicle"` 객체를 찾고 다음 키를 만듭니다.

$$
K_i=(road\_id_i,\ section\_id_i,\ lane\_id_i)
$$

연속해서 같은 키가 유지되는 프레임을 하나의 구간으로 묶습니다.

예를 들어 다음 궤적은 세 구간이 됩니다.

```text
frame 000~049: (10, 0, -1)
frame 050~089: (10, 0, -2)
frame 090~120: (11, 0, -1)
```

### 4.4.3 차선 변경과 도로 전환 구분

이전 키를 `K_before`, 이후 키를 `K_after`라고 할 때 다음 조건이면 `LANE_CHANGE`입니다.

```text
before.road_id    == after.road_id
before.section_id == after.section_id
before.lane_id    != after.lane_id
```

`road_id` 또는 `section_id`가 바뀌면 `ROAD_OR_SECTION_CHANGE`로 분류합니다. 교차로나 도로 연결부에서 lane ID가 바뀐 것을 일반적인 차선 변경으로 잘못 세지 않기 위한 조건입니다.

### 4.4.4 짧은 lane ID 흔들림 제거

기본값에서는 변경 전 구간과 변경 후 구간이 각각 3프레임 이상 유지되어야 실제 차선 변경으로 셉니다.

```text
source_frames >= 3
destination_frames >= 3
```

조건을 만족하지 않는 짧은 변화는 이벤트 CSV에는 기록하지만 `counted=false`로 저장합니다.

`--min-stable-frames`를 크게 하면 순간적인 lane ID 흔들림은 더 잘 제거되지만 짧은 실제 차선 변경도 놓칠 수 있습니다.

### 4.4.5 원래 차선 복귀 후보

카운트된 차선 변경 이벤트가 연속해서 다음 형태이면 이탈 후 원래 차선으로 복귀한 후보로 출력합니다.

```text
A lane → B lane
B lane → A lane
```

이때 `road_id`와 `section_id`도 서로 대응해야 합니다.

## 4.5 생성 결과

### `ego_lane_trace.csv`

모든 프레임의 Ego 차선 정보입니다.

```text
frame, frame_number, timestamp, x, y, z,
road_id, section_id, lane_id
```

### `ego_lane_events.csv`

키가 바뀐 시점의 이벤트입니다.

주요 열은 다음과 같습니다.

| 열 | 의미 |
|---|---|
| `event_type` | `LANE_CHANGE` 또는 `ROAD_OR_SECTION_CHANGE` |
| `counted` | 안정 프레임 조건을 만족해 실제 차선 변경으로 센 경우 |
| `source_frames` | 변경 전 차선 유지 프레임 수 |
| `destination_frames` | 변경 후 차선 유지 프레임 수 |
| `frame` | 새 키가 시작된 첫 프레임 |
| `from_*` | 변경 전 road, section, lane |
| `to_*` | 변경 후 road, section, lane |

## 4.6 이 코드의 한계

1. CARLA가 Ego 중심점에 할당한 `lane_id`가 바뀌는 시점을 찾는 코드입니다.
2. 차량 바퀴나 bounding box 일부가 차선을 밟았는지는 계산하지 않습니다.
3. 물리적인 차선 경계 침범을 판단하려면 차량 bounding box와 HD map 차선 경계 좌표가 추가로 필요합니다.
4. annotation의 잘못된 `road_id`, `section_id`, `lane_id`는 그대로 결과에 반영됩니다.
5. `class="ego_vehicle"`가 없거나 세 ID 중 하나가 없으면 해당 시나리오 분석이 중단됩니다.

---

# 5. `command_near` 분포 분석 코드

## 5.1 파일

```text
read_json_gz.py
```

외부 패키지는 필요하지 않습니다. 파일명은 단순히 JSON.GZ를 읽는 코드처럼 보이지만, 실제 목적은 Bench2Drive annotation의 `command_near` 분포와 선택 명령이 포함된 시나리오를 찾는 것입니다.

## 5.2 목적

다음 질문에 답하기 위한 코드입니다.

- 전체 데이터에서 `LANEFOLLOW`, `LEFT`, `RIGHT`, `CHANGELANELEFT` 등이 각각 몇 프레임인가?
- 각 시나리오에 특정 명령이 몇 프레임 포함되어 있는가?
- 같은 명령이 몇 개의 연속 구간으로 나타나는가?
- 차선 변경 명령이 포함된 시나리오만 골라낼 수 있는가?
- `command_near` 누락 또는 JSON 읽기 오류가 있는 파일은 무엇인가?

## 5.3 명령 ID

| ID | 이름 | 의미 |
|---:|---|---|
| -1 | `VOID` | 유효한 경로 명령 없음 |
| 0 | `VOID_OR_UNKNOWN` | 미정 또는 알 수 없음 |
| 1 | `LEFT` | 좌회전 |
| 2 | `RIGHT` | 우회전 |
| 3 | `STRAIGHT` | 직진 |
| 4 | `LANEFOLLOW` | 현재 차로 추종 |
| 5 | `CHANGELANELEFT` | 좌측 차선 변경 |
| 6 | `CHANGELANERIGHT` | 우측 차선 변경 |

숫자, 문자열, enum 형태의 문자열을 모두 정규화합니다. 예를 들어 `5`, `"5"`, `"CHANGELANELEFT"`, `"RoadOption.CHANGELANELEFT"`를 모두 ID 5로 처리할 수 있습니다.

## 5.4 실행 방법

### 데이터셋 또는 시나리오 폴더 전체 분석

```bash
python3 read_json_gz.py \
  --input "/home/kmw/Summer_Intership/Carla/Scenario" \
  --output-dir "/home/kmw/Summer_Intership/Carla/command_near_result"
```

디렉터리를 입력하면 기본적으로 하위의 모든 `*.json.gz`를 재귀적으로 검색합니다.

### 단일 annotation 분석

```bash
python3 read_json_gz.py \
  --input "/path/to/scenario/anno/00304.json.gz" \
  --output-dir "/path/to/command_near_result"
```

### 경로 목록 파일 사용

```bash
python3 read_json_gz.py \
  --input "/path/to/scenario_paths.txt" \
  --output-dir "/path/to/command_near_result"
```

`scenario_paths.txt`에는 한 줄에 하나씩 `json.gz`, `anno` 폴더 또는 시나리오 폴더 경로를 적습니다. `#`으로 시작하는 줄과 빈 줄은 무시합니다.

### 좌·우 차선 변경 시나리오 찾기

```bash
python3 read_json_gz.py \
  --input "/path/to/Scenario" \
  --output-dir "/path/to/command_near_result" \
  --selected-command CHANGELANELEFT \
  --selected-command CHANGELANERIGHT \
  --min-selected-frames 3
```

`--selected-command`를 생략하면 기본값이 좌·우 차선 변경 명령입니다. 쉼표로 묶어 다음처럼 써도 같습니다.

```bash
--selected-command "CHANGELANELEFT,CHANGELANERIGHT"
```

### 중첩된 다른 필드 분석

```bash
python3 read_json_gz.py \
  --input "/path/to/Scenario" \
  --field "planner.command_near"
```

기본 필드는 최상위 `command_near`입니다. 단순 필드명이 최상위에 없으면 기본 설정에서는 JSON 내부를 재귀적으로 찾아 첫 번째 동일 키를 사용합니다. 정확한 위치만 읽으려면 `--no-recursive-field-search`를 추가합니다.

## 5.5 계산 방법

### 전체 명령 비율

명령 `c`의 프레임 수를 `N_c`, 정상적으로 명령을 읽은 전체 프레임 수를 `N_valid`라고 하면 다음과 같이 계산합니다.

$$
Percentage_c=\frac{N_c}{N_{valid}}\times100
$$

명령 누락 프레임과 읽기 오류 프레임은 분모 `N_valid`에 포함하지 않습니다.

### 시나리오 수

`clip_count`는 명령 `c`가 한 프레임 이상 나타난 시나리오 수입니다.

$$
ClipCount_c=\sum_k \mathbf{1}(N_{k,c}>0)
$$

여기서 `k`는 시나리오, `N_k,c`는 시나리오 `k`에서 명령 `c`인 프레임 수입니다.

### 연속 구간 수

`run_count`는 프레임 수가 아니라 명령이 새로 시작된 횟수입니다. 정렬된 명령열에서 직전 명령과 현재 명령이 다를 때 현재 명령의 구간 수를 1 증가시킵니다.

```text
LANEFOLLOW, LANEFOLLOW, LEFT, LEFT, LANEFOLLOW
```

위 예시의 프레임 수는 `LANEFOLLOW=3`, `LEFT=2`이지만 구간 수는 `LANEFOLLOW=2`, `LEFT=1`입니다.

주의할 점은 누락 또는 오류 프레임은 `sequences`에 들어가지 않는다는 것입니다. 따라서 누락 프레임 앞뒤의 명령이 같으면 코드상 하나의 연속 구간으로 이어질 수 있습니다.

### 선택 시나리오 판정

선택 명령 집합을 `S`라고 하면 시나리오 `k`의 선택 프레임 수는 다음과 같습니다.

$$
N_{selected,k}=\sum_{c\in S}N_{k,c}
$$

다음 조건을 만족할 때 `contains_selected_command=true`가 됩니다.

$$
N_{selected,k}\geq min\_selected\_frames
$$

## 5.6 생성 결과

| 파일 | 내용 |
|---|---|
| `overall_command_distribution.csv` | 전체 명령별 프레임 수, 비율, 포함 시나리오 수, 연속 구간 수 |
| `clip_command_distribution.csv` | 시나리오별 유효·누락·오류 수와 명령별 프레임·구간 수 |
| `frame_commands.csv` | 각 annotation 파일의 원본 값과 정규화된 명령 ID·이름 |
| `selected_clips.txt` | 선택 명령 프레임 수 조건을 만족한 시나리오의 절대경로 |
| `scan_errors.csv` | JSON 압축 해제, 파싱 또는 필드 처리 중 발생한 오류 |

`selected_clips.txt`는 실제 데이터 복사 목록이 아니라 조건에 맞는 시나리오 경로 목록입니다.

## 5.7 이 코드의 한계

1. `command_near`는 planner가 기록한 경로 명령이지, Ego가 실제로 그 행동을 수행했다는 증거는 아닙니다.
2. 실제 차선 변경 여부는 `check_ego_lane_change(1).py`의 `lane_id` 변화 결과와 함께 확인해야 합니다.
3. 전체 폴더를 재귀 검색하면 출력 폴더가 입력 폴더 안에 있고 그 안에 `json.gz`가 존재하는 경우 의도하지 않은 파일까지 읽을 수 있습니다.
4. JSON에 같은 이름의 키가 여러 위치에 있으면 재귀 검색은 처음 찾은 값을 사용합니다. 구조를 아는 경우 `--field`에 전체 경로를 지정하는 편이 안전합니다.

---

# 6. Ego 적용 신호등 추출 코드

## 6.1 파일

```text
traffic_light_affect.py
```

외부 패키지는 필요하지 않습니다.

## 6.2 목적

annotation의 `bounding_boxes`에서 다음 두 조건을 동시에 만족하는 객체만 추출합니다.

```text
class == "traffic_light"
affects_ego is True
```

이 코드는 신호등을 새로 판정하거나 수정하지 않습니다. 원본 annotation 또는 `b2d_traffic_light_relabeler.py`가 만든 보정본에서 현재 `affects_ego=true`로 저장된 결과를 빠르게 검증하는 확인용 코드입니다.

## 6.3 실행 방법

### 시나리오 또는 `anno` 폴더 분석

```bash
python3 traffic_light_affect.py \
  --input "/home/kmw/Summer_Intership/Carla/Scenario/시나리오_폴더" \
  --output "/home/kmw/Summer_Intership/Carla/traffic_lights.csv"
```

### 보정본 검증

```bash
python3 traffic_light_affect.py \
  --input "/path/to/traffic_light_result/시나리오_이름/corrected_anno" \
  --output "/path/to/corrected_traffic_lights.csv"
```

### 단일 프레임 확인

```bash
python3 traffic_light_affect.py \
  --input "/path/to/anno/00304.json.gz" \
  --output "/path/to/frame_00304_traffic_light.csv"
```

일반 `.json`과 압축된 `.json.gz`를 모두 지원합니다. 디렉터리 입력은 해당 폴더 바로 아래 파일만 읽으며 하위 폴더를 재귀 검색하지 않습니다.

## 6.4 추출 값

| CSV 열 | annotation 원본 필드 또는 계산 |
|---|---|
| `frame` | 파일명에서 `.json` 또는 `.json.gz`를 제거한 값 |
| `traffic_light_id` | `traffic_light.id` |
| `state` | `traffic_light.state` |
| `state_name` | 상태 숫자를 `Red`, `Yellow`, `Green`, `Off`, `Unknown`으로 변환 |
| `distance_m` | `traffic_light.distance`를 소수 둘째 자리까지 표시 |
| `location` | 신호등 객체의 `location` |
| `trigger_volume_location` | 신호등 trigger 중심 위치 |
| `source_file` | 원본 annotation 경로 |

상태 변환은 다음과 같습니다.

| 값 | 이름 |
|---:|---|
| 0 | `Red` |
| 1 | `Yellow` |
| 2 | `Green` |
| 3 | `Off` |
| 4 | `Unknown` |

`distance_m`은 코드가 Ego와 신호등 위치로 새로 계산한 거리가 아닙니다. annotation에 이미 저장된 `distance` 값을 표시 형식만 `0.01 m` 단위로 바꾼 것입니다.

## 6.5 원본과 보정본 비교 방법

```bash
python3 traffic_light_affect.py \
  --input "/path/to/original_scenario/anno" \
  --output "/path/to/original_traffic_lights.csv"

python3 traffic_light_affect.py \
  --input "/path/to/result/scenario/corrected_anno" \
  --output "/path/to/corrected_traffic_lights.csv"
```

두 CSV에서 프레임별 `traffic_light_id`를 비교하면 보정으로 새로 `true`가 된 구간을 확인할 수 있습니다. 신호등의 `state`, 위치, trigger 위치가 달라졌다면 `affects_ego`만 수정한다는 보정 조건을 위반한 것이므로 보정본을 다시 점검해야 합니다.

## 6.6 이 코드의 한계

1. `affects_ego is True`를 엄격히 검사하므로 문자열 `"true"`나 숫자 `1`은 포함하지 않습니다.
2. 한 프레임에 여러 신호등이 `true`이면 모두 CSV에 기록합니다. 충돌을 자동 해결하지 않습니다.
3. `affects_ego=false`이지만 실제로 관련 있는 누락 신호등은 찾지 못합니다. 누락 검출은 `b2d_traffic_light_relabeler.py`를 사용해야 합니다.
4. 이벤트 시작·종료나 trigger 통과 여부를 계산하지 않습니다.
5. 프레임 파일명은 문자열 기준으로 정렬하므로 파일 번호의 자릿수가 일정한 Bench2Drive 형식에서 사용하는 것이 안전합니다.

---

# 7. 네 코드를 함께 사용하는 권장 순서

네 코드는 목적이 다르므로 다음 순서로 사용하는 것이 좋습니다.

```text
원본 anno/*.json.gz
        │
        ├─ read_json_gz.py
        │    └─ command_near 분포·시나리오 후보 확인
        │
        ├─ check_ego_lane_change(1).py
        │    └─ 실제 lane_id 변경·도로 전환 시점 확인
        │
        ├─ b2d_traffic_light_relabeler.py
        │    ├─ 신호등 통과 이벤트 검출
        │    ├─ 누락·모호 이벤트 분류
        │    └─ 선택적으로 corrected_anno 생성
        │
        └─ traffic_light_affect.py
             └─ 원본·보정본의 affects_ego=true 결과 확인
```

실제 작업 순서는 다음과 같습니다.

1. `read_json_gz.py`로 명령 분포와 원하는 명령이 포함된 시나리오를 찾습니다.
2. `check_ego_lane_change(1).py`로 차선 변경 명령과 실제 `lane_id` 변화가 일치하는지 확인합니다.
3. 신호등 보정기는 우선 `--write-corrected-anno` 없이 실행합니다.
4. `missing_label`, `ambiguous`, `approach_only` 이벤트를 확인합니다.
5. 모호한 이벤트만 영상과 top-down 시각화로 확인합니다.
6. 임계값이 적절하면 `--write-corrected-anno`로 보정 annotation을 생성합니다.
7. `traffic_light_affect.py`를 원본과 보정본에 각각 실행해 새로 `true`가 된 프레임과 ID를 비교합니다.
8. 학습 데이터 로더가 보정본의 `anno`를 읽도록 경로를 설정합니다.
9. 원본 데이터는 보존합니다.

---

# 8. `b2d_check/` 공식 도구 묶음

이 폴더는 Bench2Drive의 공식 실행·평가 코드를 정리한 보조 묶음입니다. 직접 제작한 두 분석 코드와 구분해야 합니다.

## 8.1 주요 파일

| 파일 | 용도 |
|---|---|
| `tools/data_collect.py` | CARLA 센서 데이터와 annotation 수집 |
| `tools/visualize.py` | 카메라 bbox, top-down, road, LiDAR 시각화 |
| `tools/merge_route_json.py` | 여러 평가 JSON을 `merged.json`으로 병합 |
| `tools/ability_benchmark.py` | 5개 능력별 성공률 계산 |
| `tools/efficiency_smoothness_benchmark.py` | Driving Efficiency와 Smoothness 계산 |
| `tools/generate_video.py` | 저장 영상에 speed, steer, throttle, brake 표시 |
| `tools/split_xml.py` | route XML을 여러 작업으로 분할 |
| `tools/gen_hdmap.py` | CARLA Town HD map 생성 |
| `tools/clean_carla.sh` | 남아 있는 CARLA·평가 프로세스 종료 |

## 8.2 `command_near`가 저장되는 위치

`tools/data_collect.py`의 `save()`에서는 planner가 전달한 다음 값을 annotation에 저장합니다.

```python
'x_command_near': near_node[0],
'y_command_near': near_node[1],
'command_near': near_command.value,
```

즉 `command_near`는 이 저장 부분에서 새로 계산되는 값이 아닙니다. 상위 agent/planner에서 계산된 `near_command`의 enum 값을 JSON에 기록하는 구조입니다.

annotation 확인만 할 때는 다음처럼 압축을 풀지 않고 읽을 수 있습니다.

```python
import gzip
import json

with gzip.open("00000.json.gz", "rt", encoding="utf-8") as f:
    anno = json.load(f)

print(anno["command_near"])
```

## 8.3 평가 JSON 병합과 계산

```bash
python3 b2d_check/tools/merge_route_json.py \
  -f "/path/to/evaluation_json_folder"
```

공식 스크립트는 220개 route를 기준으로 계산합니다.

$$
Driving\ Score=\frac{\sum_{r=1}^{N}score\_composed_r}{220}
$$

$$
Success\ Rate=\frac{N_{success}}{220}
$$

성공 route는 상태가 `Completed` 또는 `Perfect`이고, `min_speed_infractions`를 제외한 다른 infraction이 없어야 합니다.

route가 220개보다 적어도 분모가 220으로 고정되므로, 10개 시나리오만 평가한 결과에 이 스크립트의 전체 점수를 그대로 사용하면 안 됩니다. 10개 실험에서는 route별 `score_composed`, `score_route`, infraction을 직접 비교하거나 별도 분모 10 기준 요약을 만들어야 합니다.

## 8.4 능력별 점수

```bash
python3 b2d_check/tools/ability_benchmark.py \
  -f "b2d_check/leaderboard/data/bench2drive220.xml" \
  -r "/path/to/merged.json"
```

각 능력 점수는 해당 능력에 속한 route 중 성공한 비율입니다.

$$
Ability_k=\frac{N_{success,k}}{N_{routes,k}}
$$

전체 mean은 다음 5개 능력의 산술평균입니다.

- Overtaking
- Merging
- Emergency Brake
- Give Way
- Traffic Signs

Traffic Signs는 단순 완료 여부 외에도 route completion이 첫 junction 통과 기준보다 큰지, stop/red-light infraction이 없는지를 추가로 검사합니다. 이 코드는 CARLA map과 GlobalRoutePlanner를 사용하므로 CARLA 서버 실행 환경이 필요합니다.

## 8.5 Driving Efficiency와 Smoothness

```bash
python3 b2d_check/tools/efficiency_smoothness_benchmark.py \
  -f "/path/to/merged.json" \
  -m "/path/to/metric_folder"
```

Smoothness는 20프레임 단위 구간에서 다음 값이 모두 임계값 안에 있는지를 검사한 뒤, 조건을 만족한 구간 비율로 계산합니다.

| 항목 | 허용 범위 |
|---|---:|
| 종방향 가속도 | `-4.05 < a_lon < 2.40 m/s²` |
| 횡방향 가속도 | `|a_lat| < 4.89 m/s²` |
| 가속도 크기의 jerk | `|j| < 8.37 m/s³` |
| 종방향 jerk | `|j_lon| < 4.13 m/s³` |
| 코드 변수 `_z_yaw_acc` | `|값| < 1.93` |
| yaw rate | `|ω_yaw| < 0.95 rad/s` |

신호는 Savitzky–Golay filter로 평활·미분하며 기본 시간 간격은 `0.1 s`입니다.

주의할 점이 있습니다. 공식 코드의 `_z_yaw_acc`는 이름과 임계값 주석에는 yaw acceleration으로 적혀 있지만, 실제 구현에서는 yaw rate에 미분 옵션을 주지 않고 Savitzky–Golay filter만 한 값입니다. 따라서 현재 파일을 그대로 실행하면 yaw acceleration을 엄밀하게 계산한 것이 아닙니다. 논문 수식과 동일한 지표가 필요한 경우 이 부분은 별도로 수정·검증해야 합니다.

전체 Driving Smoothness는 route별 smoothness의 평균입니다.

Driving Efficiency는 평가 JSON의 `min_speed_infractions` 문자열에서 백분율을 추출해 route 내부 평균을 구하고, 다시 route 평균을 구하는 방식입니다. 현재 공식 스크립트는 `min_speed_infractions`가 없는 route를 efficiency 평균 목록에 넣지 않으므로, 출력값의 분모가 전체 route 수와 다를 수 있다는 점도 주의해야 합니다.

## 8.6 시각화

```bash
cd b2d_check

python3 tools/visualize.py \
  -f "/path/to/scenario" \
  -m 12
```

`-m 12`는 다음 파일을 의미합니다.

```text
./maps/Town12_HD_map.npz
```

시나리오의 Town과 다른 맵을 지정하면 `road_id` KeyError가 발생할 수 있습니다.

---

# 9. 빠른 명령 요약

## 신호등 분석만 수행

```bash
python3 b2d_traffic_light_relabeler.py \
  --input "/path/to/Scenario" \
  --output "/path/to/tl_result"
```

## 신호등 보정본까지 생성

```bash
python3 b2d_traffic_light_relabeler.py \
  --input "/path/to/Scenario" \
  --output "/path/to/tl_result_v3" \
  --write-corrected-anno
```

## Ego 차선 변경 분석

```bash
python3 "check_ego_lane_change(1).py" \
  "/path/to/scenario" \
  --min-stable-frames 3
```

## `command_near` 분포 및 차선 변경 명령 시나리오 찾기

```bash
python3 read_json_gz.py \
  --input "/path/to/Scenario" \
  --output-dir "/path/to/command_near_result" \
  --selected-command "CHANGELANELEFT,CHANGELANERIGHT" \
  --min-selected-frames 3
```

## `affects_ego=true` 신호등 확인

```bash
python3 traffic_light_affect.py \
  --input "/path/to/scenario/anno" \
  --output "/path/to/traffic_lights.csv"
```

## 평가 JSON 병합

```bash
python3 b2d_check/tools/merge_route_json.py \
  -f "/path/to/result_json_folder"
```

## 문제가 생겼을 때 우선 확인할 것

1. 입력 경로 아래에 실제 `anno/*.json.gz`가 있는지 확인합니다.
2. annotation에 `class="ego_vehicle"`와 필요한 위치·ID 필드가 있는지 확인합니다.
3. 신호등 결과가 없으면 annotation의 Ego 좌표와 `trigger_volume_location`을 확인하고, 필요할 때 `--contact-radius`를 조금씩 늘려 다시 검사합니다.
4. `ambiguous`를 강제로 자동 보정하지 말고 대표 프레임을 확인합니다.
5. 시각화 `road_id` 오류가 발생하면 annotation보다 Town map 경로를 먼저 확인합니다.
6. 원본 annotation은 삭제하거나 덮어쓰지 않습니다.
