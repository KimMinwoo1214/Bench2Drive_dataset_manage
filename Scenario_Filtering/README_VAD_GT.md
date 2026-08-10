# Bench2Drive → VAD-B2D 학습 GT 생성 도구

이 도구는 **Bench2DriveZoo `uniad/vad` 브랜치의 실제 `B2D_VAD_Dataset` 입력 구조**에 맞춰 학습용 PKL을 생성합니다.

중요한 점은 VAD-B2D에서 모든 GT tensor를 미리 하나의 PKL에 저장하지 않는다는 것입니다.

```text
raw anno/*.json.gz
        ↓
build_vad_training_gt.py
        ├─ b2d_infos_train.pkl
        ├─ b2d_infos_val.pkl
        └─ b2d_map_infos.pkl
                  ↓
        B2D_VAD_Dataset
        ├─ Detection GT
        ├─ Agent future trajectory GT
        ├─ Ego future trajectory GT
        └─ Vector-map GT
```

`B2D_VAD_Dataset`이 `gt_ids`, `npc2world`, `world2lidar`, map info를 이용하여 **학습 시점에** future trajectory와 vector map GT를 생성합니다.

## 1. VAD-B2D 기준

공식 VAD-B2D config 기준:

- 데이터: 10 Hz
- temporal sample interval: 5 frame
- `past_frames = 2`
- `future_frames = 6`
- future horizon: 3 s
- map point 수: 20
- ROI: `[-15, -30, -2, 15, 30, 2]`
- Map class:
  - `Broken`
  - `Solid`
  - `SolidSolid`
  - `Center`
  - `TrafficLight`
  - `StopSign`

따라서 원본 VAD 논문의 `lane divider / road boundary / pedestrian crossing` 3종을 임의로 만드는 대신 **Bench2DriveZoo VAD-B2D 구현의 6개 class를 그대로 따릅니다.**

## 2. 생성 파일

```text
outputs/vad_infos/
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

`b2d_infos_*.pkl`의 핵심 필드는 다음과 같습니다.

```text
folder
town_name
frame_idx

command_near
command_near_xy
command_far
command_far_xy

ego_yaw
ego_translation
ego_vel
ego_accel
ego_rotation_rate
ego_size
world2ego

sensors
  CAM_*
    cam2ego
    intrinsic
    world2cam
    data_path
  LIDAR_TOP
    lidar2ego
    world2lidar

gt_boxes      # N x 9
gt_names      # raw CARLA type_id
gt_ids        # persistent actor ID
num_points
npc2world     # N x 4 x 4

throttle
steer
brake
```

추가로 현재 프로젝트의 traffic-control head를 위해 다음 필드를 저장합니다.

```text
traffic_controls
  traffic_lights
    id
    state
    affects_ego
    location
    trigger_volume_location

  stop_signs
    id
    affects_ego
    location
    trigger_volume_location

  relevant_traffic_light_ids
  relevant_stop_sign_ids
```

**기본 Bench2DriveZoo loader는 `traffic_controls`를 사용하지 않습니다.**
수정 VAD에서 traffic-control head를 학습시키려면 dataset의 `get_data_info()`/pipeline에서 이 필드를 꺼내 target으로 전달해야 합니다.

## 3. 기본 실행

데이터 폴더를 지정:

```bash
python3 build_vad_training_gt.py \
  /path/to/bench2drive_data \
  --maps-root /path/to/maps \
  --output-dir /path/to/infos
```

입력을 생략하면 `build_vad_training_gt.py`가 위치한 폴더 아래에서 `anno/*.json.gz`를 재귀적으로 찾습니다.

```bash
python3 build_vad_training_gt.py
```

### validation split 10%

scenario 단위로 나눕니다. 프레임 단위 random split은 temporal leakage가 생기므로 사용하지 않습니다.

```bash
python3 build_vad_training_gt.py \
  /path/to/data \
  --maps-root /path/to/maps \
  --output-dir /path/to/infos \
  --val-ratio 0.1 \
  --seed 42
```

### validation scenario 목록을 직접 지정

```bash
python3 build_vad_training_gt.py \
  /path/to/data \
  --maps-root /path/to/maps \
  --output-dir /path/to/infos \
  --val-list val_scenarios.txt
```

`val_scenarios.txt`:

```text
SignalizedJunctionLeftTurn_Town04_Route173_Weather26
Accident_Town03_Route24816_Weather25
```

공식 split JSON처럼 `{"val": [...]}` 형식도 지원합니다.

## 4. visibility filter

공식 `prepare_B2D.py`는 6-view depth image를 이용해 가려진 bbox를 필터링합니다.

정확한 공식 필터를 쓰려면 Bench2DriveZoo repository와 depth image가 있어야 합니다.

```bash
python3 build_vad_training_gt.py \
  /path/to/data \
  --maps-root /path/to/maps \
  --output-dir /path/to/infos \
  --visibility-filter official \
  --bench2drive-zoo-root /path/to/Bench2DriveZoo
```

추가 수집 데이터에 depth가 없다면:

```bash
--visibility-filter off
```

를 사용합니다.

이 경우 PKL schema는 동일하지만, bbox filtering 결과는 공식 Base 데이터 준비 결과와 완전히 동일하지 않을 수 있습니다.

## 5. GT 시각화

```bash
python3 visualize_vad_gt.py \
  --infos /path/to/infos/b2d_infos_train.pkl \
  --map-infos /path/to/infos/b2d_map_infos.pkl \
  --sample 100 \
  --show-ids
```

특정 scenario/frame:

```bash
python3 visualize_vad_gt.py \
  --infos /path/to/infos/b2d_infos_train.pkl \
  --map-infos /path/to/infos/b2d_map_infos.pkl \
  --folder SignalizedJunctionLeftTurn \
  --frame 150 \
  --show-ids
```

여러 장:

```bash
python3 visualize_vad_gt.py \
  --infos /path/to/infos/b2d_infos_train.pkl \
  --map-infos /path/to/infos/b2d_map_infos.pkl \
  --sample 100 \
  --count 20 \
  --stride 5
```

시각화에는 다음이 포함됩니다.

- GT agent bbox
- actor ID
- agent future trajectory
- Ego future trajectory
- `Broken/Solid/SolidSolid/Center`
- `TrafficLight/StopSign` trigger volume
- `affects_ego=True` traffic control 위치(확장 metadata가 있을 때)

`gt_boxes[..., 6]`의 yaw는 일반적인 2D Cartesian 각도가 아니라
`LiDARInstance3DBoxes` 규약입니다. 이 BEV 화면에서는 양의 yaw가 시계방향으로
보이므로 bbox를 직접 그릴 때 일반적인 반시계 회전행렬을 사용하면 안 됩니다.
특히 yaw가 약 45도일 때 부호를 반대로 적용하면 bbox가 실제 방향에서 약 90도
돌아간 것처럼 보입니다.

## 6. Contract 검사

```bash
python3 test_vad_info_contract.py \
  --train /path/to/infos/b2d_infos_train.pkl \
  --val /path/to/infos/b2d_infos_val.pkl \
  --map /path/to/infos/b2d_map_infos.pkl
```

검사:

- 필수 key 존재
- `gt_boxes.shape == (N, 9)`
- `npc2world.shape == (N, 4, 4)`
- `gt_ids/gt_names/num_points` 길이 일치
- 6개 camera + LIDAR transform 존재
- NaN/Inf
- bbox 긴 축과 `world2lidar @ npc2world` actor 전방축의 일치 여부

## 7. Bench2DriveZoo에 연결

VAD-B2D config의 기본 경로는 다음입니다.

```python
data_root = "data/bench2drive"
info_root = "data/infos"
map_file = "data/infos/b2d_map_infos.pkl"

ann_file_train = "data/infos/b2d_infos_train.pkl"
ann_file_val = "data/infos/b2d_infos_val.pkl"
```

따라서 생성 결과를 `Bench2DriveZoo/data/infos/`에 두거나 config 경로를 생성 위치에 맞게 변경하면 됩니다.

## 8. 반드시 확인할 부분

### 8.1 frame이 빠지면 temporal horizon이 틀어질 수 있음

공식 loader는 미래 데이터를 실제 `frame_idx`가 아니라 **PKL list index + sample_interval**로 찾습니다.

따라서 전처리 중 frame이 빠지면:

```text
실제 0.5 s 후 frame
≠
PKL index + 5
```

가 될 수 있습니다.

`debug/sample_summary.csv`의 `frame_gap_from_previous`와
`debug/validation_summary.csv`를 반드시 확인하십시오.

### 8.2 `gt_ids`는 persistent actor ID여야 함

Agent future trajectory GT는 같은 ID를 future frame에서 검색해서 만듭니다.
frame 내부 순번을 ID로 사용하면 안 됩니다.

### 8.3 좌표계를 임의로 바꾸지 말 것

Bench2Drive raw annotation은 left-handed이고,
VAD-B2D PKL은 공식 preprocessing과 같은 right-handed/LiDAR convention으로 변환합니다.

이 스크립트는 그 변환 행렬을 그대로 따르도록 작성했습니다.

### 8.4 Stop Sign / Traffic Light

VAD-B2D 기본 map head에는 이미 `TrafficLight`, `StopSign` trigger-volume class가 있습니다.

하지만 현재 프로젝트에서 말하는:

```text
traffic_light_state
traffic_light.affects_ego
stop_sign.affects_ego / relevance
```

는 별도의 traffic-control supervision입니다.

따라서 **map GT와 traffic-control GT를 동일한 것으로 취급하면 안 됩니다.**

`traffic_controls` 확장 필드에는 원본/보정 annotation의 `affects_ego`를 보존해서 저장해 두었습니다.
