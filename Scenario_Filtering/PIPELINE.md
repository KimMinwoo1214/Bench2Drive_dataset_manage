# 신호등 annotation 보정·영상 생성 파이프라인

이 파이프라인은 입력 경로 하나만 받아 다음 네 단계를 자동으로 실행합니다.

```text
원본 clip
  → 신호등 affects_ego 분석
  → 원본과 달라진 항목을 changes.csv에 기록하고 corrected_anno 생성
  → corrected_anno 기준 선택한 카메라·LiDAR bbox 이미지 생성
  → view별 카메라 MP4와 LiDAR MP4 생성
```

`command_near`나 lane change 분석은 annotation을 보정하는 작업이 아니므로 기본 파이프라인에서 제외했습니다. 필요할 때 기존 분석 도구를 따로 실행합니다.

원본 `data/<clip>/anno`는 절대로 수정하지 않습니다. 보정본도 새로운 JSON 필드를 추가하지 않고, 선택된 신호등의 기존 `affects_ego` 값만 `false`에서 `true`로 바꿉니다. 판정 근거는 JSON이 아니라 CSV에 둡니다.

## 1. 폴더 구조

```text
b2d/
├── .venv/
├── Bench2Drive_dataset_manage/       # Git으로 공유하는 코드
├── data/                             # 다운로드한 원본 B2D clip
│   ├── SignalizedJunctionLeftTurn_Town04_Route173_Weather26/
│   └── ControlLoss_Town11_Route401_Weather11/
├── maps/                             # 다른/기존 분석 도구에서 사용하는 HD map
├── leaderboard/                      # Bench2Drive 평가 코드·결과
└── outputs/                          # 이 파이프라인의 결과
```

새 clip은 압축을 푼 뒤 `data/<clip-name>/`에 넣습니다. 기본 파이프라인은 `maps`와 `leaderboard`를 사용하지 않습니다.

## 2. 최초 한 번만 환경 만들기

`b2d` 폴더에서 실행합니다.

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r Bench2Drive_dataset_manage/Scenario_Filtering/requirements.txt
```

`ffmpeg`가 설치되어 있으면 macOS에서는 VideoToolbox, 그 외 환경에서는 libx264를 자동으로 선택합니다. `ffmpeg`가 없어도 OpenCV로 영상을 만들 수 있습니다.

FFmpeg가 필요한 경우 macOS는 `brew install ffmpeg`, Ubuntu는 `sudo apt install ffmpeg`로 설치할 수 있습니다.

## 3. 한 clip 실행

입력에 **clip 폴더**를 지정합니다.

```bash
python3 Bench2Drive_dataset_manage/Scenario_Filtering/run_scenario_pipeline.py data/SignalizedJunctionLeftTurn_Town04_Route173_Weather26
```

## 4. data 안의 모든 clip 실행

입력에 **data 폴더**를 지정합니다.

```bash
python3 Bench2Drive_dataset_manage/Scenario_Filtering/run_scenario_pipeline.py data
```

따라서 한 clip/여러 clip을 구분하는 별도 옵션은 없습니다. 마지막 입력 경로가 clip이면 하나, `data`이면 그 아래에서 `anno/*.json.gz`를 가진 모든 clip을 자동으로 찾습니다.

명령은 가능하면 위처럼 한 줄로 실행하십시오. 여러 줄로 나눌 때 `\` 뒤에 공백을 넣으면 셸이 명령을 끊어 `--input`, `2>&1` 같은 이름의 잘못된 파일을 만들 수 있습니다.

## 5. 자주 쓰는 옵션

### 150번부터 30프레임만 시험

신호등 분석과 annotation 보정은 clip 전체에 수행하고, 이미지·영상만 150번부터 30프레임을 만듭니다.

```bash
python3 Bench2Drive_dataset_manage/Scenario_Filtering/run_scenario_pipeline.py data/SignalizedJunctionLeftTurn_Town04_Route173_Weather26 --start-frame 150 --max-frames 30
```

### 분석·보정만 하고 이미지와 영상은 생략

```bash
python3 Bench2Drive_dataset_manage/Scenario_Filtering/run_scenario_pipeline.py data --no-render --no-video
```

### 이미지까지 만들고 MP4만 생략

```bash
python3 Bench2Drive_dataset_manage/Scenario_Filtering/run_scenario_pipeline.py data --no-video
```

### 카메라 view 선택

기본값은 6개 카메라 전체입니다. 각 view는 별도의 이미지 폴더와 MP4로 저장됩니다.

전방 하나만:

```bash
python3 Bench2Drive_dataset_manage/Scenario_Filtering/run_scenario_pipeline.py data --cameras front
```

전방 계열 세 개만:

```bash
python3 Bench2Drive_dataset_manage/Scenario_Filtering/run_scenario_pipeline.py data --cameras front,front_left,front_right
```

카메라 없이 LiDAR만:

```bash
python3 Bench2Drive_dataset_manage/Scenario_Filtering/run_scenario_pipeline.py data --cameras none
```

카메라만 만들고 LiDAR 제외:

```bash
python3 Bench2Drive_dataset_manage/Scenario_Filtering/run_scenario_pipeline.py data --no-lidar
```

카메라 이름은 `front`, `front_left`, `front_right`, `back`, `back_left`, `back_right`입니다. 쉼표 뒤에는 공백을 넣지 않습니다.

### 출력 위치 변경

```bash
python3 Bench2Drive_dataset_manage/Scenario_Filtering/run_scenario_pipeline.py data --output /path/to/my_outputs
```

주요 옵션은 다음과 같습니다.

| 옵션 | 의미 | 기본값 |
|---|---|---|
| `input` | clip 하나 또는 여러 clip이 든 `data` 폴더 | 필수 |
| `--output` | 결과 루트 | 입력의 `data` 옆 `outputs` |
| `--start-frame` | 이미지·영상 시작 프레임 | 첫 프레임 |
| `--max-frames` | 이미지·영상 최대 프레임 수 | 전체 |
| `--fps` | MP4 재생 FPS | `10` |
| `--workers` | 동시에 렌더링할 프레임 수 | CPU 기준 최대 `4` |
| `--cameras` | 저장할 카메라 view. `all`, `none` 또는 쉼표 목록 | `all` |
| `--no-lidar` | LiDAR 이미지와 영상을 제외 | 사용 안 함 |
| `--encoder` | `auto`, `videotoolbox`, `libx264`, `opencv` | `auto` |
| `--no-render` | bbox 이미지 생략 | 사용 안 함 |
| `--no-video` | MP4 생략 | 사용 안 함 |

## 6. 결과물

```text
outputs/
├── summary.csv
└── <clip-name>/
    ├── traffic_light/
    │   ├── events.csv
    │   ├── frame_labels.csv
    │   ├── changes.csv
    │   ├── review_queue.csv
    │   ├── original_affects_ego.csv
    │   ├── corrected_affects_ego.csv
    │   └── corrected_anno/
    │       └── *.json.gz
    ├── visualization/
    │   ├── camera/
    │   │   ├── rgb_front_3d_bbox/*.jpg
    │   │   ├── rgb_front_left_3d_bbox/*.jpg
    │   │   ├── rgb_front_right_3d_bbox/*.jpg
    │   │   ├── rgb_back_3d_bbox/*.jpg
    │   │   ├── rgb_back_left_3d_bbox/*.jpg
    │   │   └── rgb_back_right_3d_bbox/*.jpg
    │   └── lidar_bev_bbox/
    │       └── *.jpg
    └── videos/
        ├── camera/
        │   ├── rgb_front_3d_bbox.mp4
        │   ├── rgb_front_left_3d_bbox.mp4
        │   ├── rgb_front_right_3d_bbox.mp4
        │   ├── rgb_back_3d_bbox.mp4
        │   ├── rgb_back_left_3d_bbox.mp4
        │   └── rgb_back_right_3d_bbox.mp4
        └── lidar_bev_bbox.mp4
```

- `summary.csv`: clip별 검출/선택 이벤트 수, 보정 개수, 성공/실패와 소요 시간 요약
- `events.csv`: Ego 경로와 신호등 trigger를 비교해 찾은 신호등 이벤트
- `frame_labels.csv`: 전체 프레임별 관련 신호등과 original/recovered 판정
- `changes.csv`: 실제로 `affects_ego=false → true`로 달라진 프레임과 신호등만 기록
- `review_queue.csv`: 자동 판정이 애매해 사람이 확인해야 할 이벤트
- `original_affects_ego.csv`: 원본 annotation에서 `affects_ego=true`인 행
- `corrected_affects_ego.csv`: 보정 annotation에서 `affects_ego=true`인 행
- `corrected_anno`: 원본 구조를 유지한 완전한 보정 annotation 세트
- `visualization`: 보정본을 기준으로 그린 선택 카메라와 LiDAR BEV 이미지
- `videos`: 위 이미지를 이어 만든 MP4

시각화의 annotation 박스와 신호등 표시 색은 다음을 뜻합니다.

- 흰색: 보정 annotation의 `affects_ego=false` 또는 해당 필드 없음
- 빨간색: 보정 annotation의 `affects_ego=true`

원본에서부터 `true`였는지, 이번 보정으로 `false → true`가 되었는지는
영상 색으로 구분하지 않고 `traffic_light/changes.csv`에서 확인합니다.

같은 clip을 다시 실행하면 `outputs/<clip-name>`만 지우고 새 결과로 교체합니다. 다른 clip 결과는 유지됩니다. `summary.csv`는 이번 실행에 포함된 clip 기준으로 다시 작성됩니다.

## 7. 속도

- MPS는 PyTorch 텐서 연산용이므로 현재의 OpenCV/NumPy/LAZ/영상 인코딩에는 직접적인 이점이 없습니다.
- LiDAR 점 투영은 Python 점 단위 반복 대신 NumPy 벡터 연산을 사용합니다.
- 여러 프레임은 `--workers`로 병렬 렌더링합니다. 기본 최대값은 4이며 메모리가 충분하면 `--workers 6`처럼 시험할 수 있습니다.
- macOS에서 `--encoder auto`는 FFmpeg의 `h264_videotoolbox`를 우선 사용합니다. 사용할 수 없으면 libx264, 그마저 없으면 OpenCV로 자동 전환합니다.
- Ubuntu에서는 FFmpeg가 있으면 기본적으로 libx264를 사용하므로 같은 명령을 사용할 수 있습니다.

## 8. lane/command 분석은 별도 작업

`command_near` 분포나 실제 lane ID 변화는 데이터 선별용 분석이지, 현재 신호등 `affects_ego` 보정 대상이 아닙니다. 필요할 때 다음 도구를 별도로 사용합니다.

- `read_json_gz.py`: `command_near` 분포 분석
- `check_ego_lane_change(1).py`: lane ID 변화 분석

이 결과가 기본 `outputs/<clip-name>`에 섞이지 않도록 한 것이 현재 구조의 핵심입니다.
