# VAD-B2D 학습용 GT 생성 및 검증 정리

## 1. 목적

Bench2Drive/CARLA raw annotation과 HD Map을 이용해 **VAD 전체 학습에 사용할 GT 데이터**를 생성하고, 생성 결과가 정상인지 BEV 시각화와 자동 검사를 통해 검증하는 파이프라인을 구성했습니다.

전체 흐름은 다음과 같습니다.

```text
Bench2Drive raw data
    ├─ anno/*.json.gz
    ├─ camera/*
    └─ maps/TownXX_HD_map.npz
            ↓
build_vad_training_gt.py
            ↓
VAD-B2D 학습용 PKL 생성
            ↓
visualize_vad_gt.py
            ↓
BEV 기반 GT 시각화 / 방향 검증
            ↓
test_vad_info_contract.py
            ↓
GT bbox orientation / transform 일치 검사
```

---

## 2. VAD-B2D 학습 GT 생성

### 사용 파일

```text
build_vad_training_gt.py
```

Bench2DriveZoo의 VAD-B2D dataset 구조를 기준으로 다음 파일을 생성합니다.

```text
outputs/vad_all/
├── b2d_infos_train.pkl
├── b2d_infos_val.pkl
├── b2d_map_infos.pkl
├── build_metadata.json
└── debug/
    ├── sample_summary.csv
    ├── scenario_summary.csv
    ├── validation_summary.csv
    └── errors.csv
```

### 생성되는 주요 GT

#### Agent

- 3D Bounding Box
- class/type
- persistent actor ID
- yaw
- velocity
- `npc2world`
- LiDAR point 수

Agent future trajectory는 현재 actor ID를 미래 frame에서 추적하여 VAD dataset loader가 생성할 수 있도록 구성했습니다.

#### Ego

- Ego pose
- velocity / acceleration
- rotation rate
- `world2ego`
- control 값
- future trajectory 생성에 필요한 temporal pose

#### Navigation

- `command_near`
- `command_far`
- near/far command target position

#### Vector Map

`TownXX_HD_map.npz`를 이용하며 VAD-B2D 기준 map class를 사용합니다.

```text
Broken
Solid
SolidSolid
Center
TrafficLight
StopSign
```

처리 과정:

```text
CARLA World Coordinate
        ↓
Right-handed coordinate 변환
        ↓
Current Ego/LiDAR coordinate
        ↓
VAD ROI crop
        ↓
Vector Map GT
```

기본 ROI:

```text
x: -15 ~ 15 m
y: -30 ~ 30 m
```

VAD의 map representation에 맞춰 각 vector는 `fixed_num=20` point 형태로 사용할 수 있도록 구성했습니다.

#### Traffic Control 추가 정보

기존 VAD 필드와 별도로 현재 프로젝트에서 사용할 수 있도록 다음 정보도 저장했습니다.

```text
traffic_controls
├─ traffic_lights
│  ├─ id
│  ├─ state
│  └─ affects_ego
│
└─ stop_signs
   ├─ id
   └─ affects_ego
```

기존 VAD는 이 필드를 자동으로 사용하지 않으므로 traffic-control head를 추가할 경우 dataset loader에서 별도로 target으로 전달해야 합니다.

---

## 3. 전체 시나리오 GT 생성

예시:

```bash
python3 build_vad_training_gt.py \
  Scenario \
  --maps-root ./maps \
  --output-dir ./outputs/vad_all \
  --visibility-filter off
```

현재 테스트에서는 8개 시나리오가 정상적으로 처리되었습니다.

```text
Accident
HardBreakRoute
InvadingTurn × 2
LaneChange
SignalizedJunctionLeftTurn
SignalizedJunctionRightTurn
YieldToEmergencyVehicle
```

총 생성 sample:

```text
2028 frames
```

필요한 Town map만 선택적으로 읽도록 수정하여 전체 HD Map을 동시에 로딩하면서 발생하던 OOM 문제도 해결했습니다.

---

## 4. GT BEV 시각화

### 사용 파일

```text
visualize_vad_gt.py
```

시각화 항목:

- Ego
- Ego future trajectory
- Agent 3D bbox의 BEV projection
- Agent future trajectory
- Actor ID
- Agent heading
- Agent yaw
- Vector Map
- Traffic Light / Stop Sign
- `affects_ego` traffic control

시나리오별 대표 frame 1장씩 생성:

```bash
python3 visualize_vad_gt.py \
  --infos outputs/vad_all/b2d_infos_train.pkl \
  --map-infos outputs/vad_all/b2d_map_infos.pkl \
  --one-per-scenario \
  --output-dir outputs/vad_all/vis \
  --show-ids \
  --show-heading \
  --show-yaw \
  --show-training-points
```

`--show-training-points`는 각 map vector에 VAD의 `fixed_num=20`에 대응하는 point를 표시합니다.

---

## 5. Agent BBox / Heading 검증

### 90° 회전해 보였던 원인

PKL의 `gt_boxes[..., 6]`는 Bench2DriveZoo가 `LiDARInstance3DBoxes`에
넘기는 yaw 규약을 따릅니다. 이 규약은 BEV에서 양의 yaw가
시계방향으로 보입니다.

기존 `visualize_vad_gt.py`는 이를 일반 Cartesian 반시계방향
회전행렬로 그려 yaw의 부호를 반대로 해석했습니다. 그 결과
보이는 방향 오차가 `2 × yaw`가 되었고, yaw가 약 45°인
agent는 bbox가 90° 돌아간 것처럼 보였습니다.

수정 후에는 LiDAR yaw 규약에 맞는 시계방향 회전으로
bbox를 그립니다. 기존 PKL의 bbox 크기와 yaw 생성식은 공식
`prepare_B2D.py`와 일치하므로 PKL을 수정하거나 다시 생성할
필요는 없습니다.

### Heading arrow

현재 heading arrow는:

```text
Agent bbox의 긴 축
        ↓
vehicle longitudinal direction 후보
        ↓
future trajectory 방향 확인
        ↓
실제 이동 방향과 같은 방향으로 heading 선택
```

즉 **heading arrow는 올바르게 해석된 bbox의 긴 축과 일치하면서
실제 future motion 방향과 같은 쪽을 가리킵니다.**

---

## 6. 특정 Agent 연속 검증

특정 actor의 bbox orientation이 정상인지 확인하려면 persistent actor ID를 기준으로 연속 frame을 확인할 수 있습니다.

```bash
python3 visualize_vad_gt.py \
  --infos outputs/vad_all/b2d_infos_train.pkl \
  --map-infos outputs/vad_all/b2d_map_infos.pkl \
  --folder Accident_Town03 \
  --track-agent 150 \
  --track-sequence \
  --show-ids \
  --show-heading \
  --show-yaw \
  --output-dir outputs/vad_all/track_150
```

이를 통해 frame별:

```text
frame
x
y
yaw
bbox orientation
future trajectory
```

를 함께 비교할 수 있습니다.

정상적인 경우:

```text
bbox longitudinal direction
        ≈
heading
        ≈
future trajectory 초기 진행 방향
```

이어야 합니다.

---

## 7. Agent BBox Orientation 자동 검사

`test_vad_info_contract.py`는 각 bbox의 긴 축과
`world2lidar @ npc2world`로 얻은 actor 전방축의 각도를 비교합니다.
방향의 앞/뒤는 bbox만으로 구분할 수 없으므로 180° 대칭으로
비교하며, 1°를 넘으면 오류로 보고합니다.

검사 기준:

```text
gt_boxes yaw / dimension
    ↓
LiDAR bbox 긴 축 계산
    ↓
npc2world actor 전방축을 current LiDAR로 변환
    ↓
두 축의 각도 오차 확인
```

실행:

```bash
python3 test_vad_info_contract.py \
  --train outputs/vad_all/b2d_infos_train.pkl \
  --val outputs/vad_all/b2d_infos_val.pkl \
  --map outputs/vad_all/b2d_map_infos.pkl
```

현재 repository에는 기존 문서에 적힌
`check_gt_agent_collisions.py`가 존재하지 않습니다. 따라서 3D collision
자동 검사를 완료했다는 기존 설명은 삭제했습니다.

---

## 8. 현재 검증 기준

GT를 최종 학습에 사용하기 전에 다음을 확인합니다.

### Vector Map

- Ego가 `(0, 0)`에 위치하는지
- Ego future가 실제 도로 방향을 따라가는지
- map vector가 갑자기 다른 도로로 튀지 않는지
- 교차로에서 lane vector 구조가 실제 geometry와 맞는지

### Agent

- bbox의 긴 축이 차량 body orientation과 맞는지
- heading과 future motion 방향이 대체로 일치하는지
- yaw가 frame 사이에서 비정상적으로 튀지 않는지
- 동일 actor ID가 연속 frame에서 유지되는지

### Annotation

- NaN / Inf 없음
- map 누락 없음
- temporal frame gap 확인
- `num_points == 0` 객체는 VAD 학습 GT에서 제외
- bbox 긴 축과 actor transform 전방축 일치 확인

---

## 9. 현재 파일 구성

```text
build_vad_training_gt.py
    └─ VAD-B2D 학습용 GT PKL 생성

visualize_vad_gt.py
    └─ Vector Map / Agent / Ego trajectory 시각화 및 방향 검증

test_vad_info_contract.py
    └─ PKL key / shape / transform / bbox orientation 검사
```

---

## 10. 최종 파이프라인

```text
Raw Bench2Drive Data
        ↓
GT PKL 생성
        ↓
PKL Contract 검사
        ↓
Vector Map / Agent BEV 시각화
        ↓
Agent Heading / Yaw 확인
        ↓
특정 Actor 연속 추적
        ↓
BBox / actor transform orientation 검사
        ↓
최종 QA
        ↓
VAD 전체 학습
```

현재 단계에서는 **GT 생성 자체뿐 아니라 생성된 좌표계, Vector Map, Agent orientation, temporal trajectory가 실제로 맞는지 검증하는 과정까지 포함하도록 구성한 상태**입니다.
