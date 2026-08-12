# Bench2Drive Scenario Pipeline

Bench2Drive 시나리오의 traffic-light annotation 수정부터 카메라/BEV 시각화,
VAD 학습용 vector-map GT, 영상, 최종 CSV까지 한 번에 생성하는 파이프라인이다.

PKL은 생성하지 않는다. VAD vector-map GT는 수정된 annotation과
`Town*_HD_map.npz`를 `visualize_vad_gt.py`가 직접 읽어 프레임별 `.npz`로 저장한다.

## 주요 파일

| 파일 | 역할 |
| --- | --- |
| `run_scenario_pipeline.py` | 전체 파이프라인 진입점 |
| `fix_tl_bbox_permutation.py` | traffic-light bbox permutation 복구와 `affects_ego` 재계산 |
| `visualize.py` | 전방 카메라와 TOP_DOWN 카메라 BEV에 3D bbox 렌더링 |
| `visualize_vad_gt.py` | 수정 annotation과 HD map에서 VAD vector GT와 BEV 이미지 생성 |
| `apply_visualize_compare_from_summary.py` | BEFORE/AFTER 비교 영상 공통 함수 |
| `requirements.txt` | Python 의존성 |

## 기본 실행

```bash
python3 run_scenario_pipeline.py \
  --input ./Scenario \
  --output ./outputs
```

이 명령은 `./Scenario` 아래에서 `anno` 폴더가 있는 모든 시나리오를 찾아
처리한다. 시나리오 폴더 하나나 그 안의 `anno` 폴더를 `--input`으로 지정해도
된다.

기본 실행 결과:

1. traffic-light bbox permutation과 `affects_ego`를 한 번에 수정
2. 수정 annotation을 시나리오별 `anno/`에 한 번만 저장
3. 프레임별 VAD vector-map GT `.npz` 생성
4. vector-map GT BEV 이미지 및 MP4 생성
5. 전방 카메라와 TOP_DOWN 카메라 BEV bbox 이미지 및 MP4 생성
6. `affects_ego`가 실제로 바뀐 시나리오만 BEFORE/AFTER 비교 영상 생성
7. 시나리오별 결과를 `results.csv`에 기록

원본 annotation은 덮어쓰지 않는다. `--output`은 `--input` 바깥 경로로
지정해야 한다.

## 입력 구조

```text
<scenario>/
├── anno/
│   ├── 00000.json.gz
│   └── ...
└── camera/
    ├── rgb_front/
    └── rgb_top_down/

maps/
├── Town03_HD_map.npz
├── Town04_HD_map.npz
└── ...
```

VAD vector GT에는 annotation의 `sensors.LIDAR_TOP.world2lidar`가 필요하다.
카메라 시각화에는 `rgb_front`와 `rgb_top_down` 원본 이미지가 추가로 필요하다.

시나리오 이름의 `TownXX`와 일치하는 map 파일을 기본 `./maps`에서 찾는다.
다른 위치를 사용할 때는 `--maps-root /path/to/maps`를 지정한다.

## 처리 순서

시나리오별 실행 순서는 다음과 같다.

1. 원본 annotation 전체를 읽고 traffic-light bbox 대응 관계 계산
2. 잘못 순환 배정된 bbox permutation 복구
3. 복구된 bbox를 기준으로 `traffic_light.affects_ego` 재계산
4. 최종 수정 annotation을 `<output>/<scenario>/anno`에 저장
5. 수정 annotation과 Town HD map으로 vector-map GT와 BEV 생성
6. 수정 annotation으로 전방 카메라/카메라 BEV bbox 시각화
7. MP4와 조건부 BEFORE/AFTER 비교 영상 생성
8. `results.csv` 기록

bbox와 `affects_ego` 수정은 같은 annotation 객체에 연속 적용한 뒤 프레임당
한 번만 저장한다. 두 수정 단계 때문에 annotation 결과가 중복 생성되지 않는다.

## 출력 구조

```text
<output>/
├── results.csv
└── <scenario>/
    ├── anno/
    │   ├── 00000.json.gz
    │   └── ...
    ├── reports/
    │   ├── traffic_light.csv
    │   └── traffic_light_summary.csv
    ├── vad_vector_gt/
    │   ├── vectors/
    │   │   ├── 00000.npz
    │   │   └── ...
    │   └── visualization/
    │       ├── <scenario>_00000.png
    │       └── ...
    ├── visualization/
    │   ├── after/
    │   │   └── camera/
    │   │       ├── rgb_front_3d_bbox/
    │   │       └── rgb_top_down_3d_bbox/
    │   └── before/                    # affects_ego 변경 시에만
    └── videos/
        ├── vad_vector_gt.mp4
        ├── after_front.mp4
        ├── after_bev.mp4
        ├── before_after_front.mp4     # affects_ego 변경 시에만
        └── before_after_bev.mp4       # affects_ego 변경 시에만
```

`after_bev.mp4`는 TOP_DOWN 카메라 이미지 위에 bbox를 그린 영상이다.
`vad_vector_gt.mp4`는 VAD 학습 좌표계의 수치 vector-map GT를 BEV로 확인하는
영상이다. 둘은 용도가 다르며 기본 실행에서는 모두 생성된다.

## Vector GT 형식

각 `<frame>.npz`는 해당 프레임의 LiDAR 좌표계 ROI 안에 있는 벡터 인스턴스를
담는다. 중간 PKL이나 train/val PKL은 만들지 않는다.

| 키 | dtype / shape | 의미 |
| --- | --- | --- |
| `points` | `float32 (N, 20, 2)` | 인스턴스마다 호 길이 기준으로 20점 샘플링한 XY 좌표 |
| `labels` | `int64 (N,)` | 벡터 클래스 번호 |
| `types` | 문자열 `(N,)` | 사람이 확인할 수 있는 클래스 이름 |
| `closed` | `bool (N,)` | 닫힌 선 여부. 현재 lane/정지선은 모두 열린 선이므로 항상 `False` |
| `frame_idx` | `int64` scalar | annotation 프레임 번호 |
| `town` | 문자열 scalar | 사용한 Town |
| `pc_range` | `float32 (6,)` | `[xmin, ymin, zmin, xmax, ymax, zmax]` |

기본 ROI는 B2D_VAD_Dataset 기본값과 같은
`[-51.2, -51.2, -5, 51.2, 51.2, 3]`, 점 개수는 VAD의
`fixed_num=20`에 맞춘다.

클래스 번호:

| 번호 | 타입 |
| --- | --- |
| `0` | `Broken` |
| `1` | `Solid` |
| `2` | `SolidSolid` |
| `3` | `Center` |
| `4` | `TrafficLight` |
| `5` | `StopSign` |

간단한 로드 예시:

```python
import numpy as np

gt = np.load("outputs/<scenario>/vad_vector_gt/vectors/00000.npz")
points = gt["points"]  # (N, 20, 2)
labels = gt["labels"]  # (N,)
```

### 학습 시 map GT 소비 경로

Stock Bench2DriveZoo 학습은 위 프레임 NPZ를 읽지 않는다. Config의
`map_file`이 가리키는 `b2d_map_infos.pkl`을 읽고,
`B2D_VAD_Dataset.get_map_info()`가 매 sample마다 map GT를 생성한다. 따라서
NPZ는 현재 QA/export 산출물이며, 학습 GT를 일치시키려면 런타임 Dataset도
같이 수정해야 한다.

이 저장소의 패치는 공식 `uniad/vad` 브랜치의 런타임 Dataset에 같은 정지선
추출, lane 조각 crop, `mask.any()` + ROI clip을 적용한다.

```bash
git -C /path/to/Bench2DriveZoo apply \
  /path/to/Scenario_Filtering/patches/bench2drivezoo_b2d_vad_map_gt.patch
```

런타임의 `LiDARInstanceLines.shift_fixed_num_sampled_points_v2`가 열린 선의
정방향/역방향 permutation-equivalence를 생성하므로, stock 학습 경로에서는
별도 shift 배열을 NPZ에 저장할 필요가 없다. 향후 custom NPZ loader를 붙일
경우에는 `visualize_vad_gt.shift_permutation_variants(points, closed)`와 같은
정방향/역방향 복원이 필요하다.

주의: Dataset 생성자의 기본 ROI는 위 51.2m 범위지만, 공식
`VAD_base_e2e_b2d.py`는 15m x 30m 범위를 명시적으로 전달한다. 실제 학습
config가 범위를 override한다면 생성 시 `--point-cloud-range`에도 같은 6개
값을 전달해야 한다. 각 NPZ의 `pc_range` 키로 사용값을 확인할 수 있다.

## 영상 생성 조건

수정 결과 영상과 vector-map GT 영상은 `--video`가 활성화되면 생성한다.

BEFORE/AFTER 영상은 다음 조건을 모두 만족할 때만 생성한다.

- 카메라 시각화 활성화
- 영상 생성 활성화
- `affects_ego_changed_frames >= 1`

신호등 bbox만 바뀌거나 다른 annotation 값이 수정됐더라도 `affects_ego` 값이
전후 동일하면 비교 영상은 만들지 않는다. 반대로 bbox permutation 수정 과정의
결과로 `affects_ego`가 바뀌었다면 비교 영상 생성 조건에 포함된다.

## 실행 옵션

| 옵션 | 설명 | 기본값 |
| --- | --- | --- |
| `--input PATH` | 시나리오, `anno`, 또는 여러 시나리오의 상위 폴더 | 필수 |
| `--output PATH` | 모든 결과를 저장할 폴더 | 필수 |
| `--visualization`, `--no-visualization` | 카메라/카메라 BEV bbox 이미지 | 생성 |
| `--video`, `--no-video` | 각 시각화의 MP4 | 생성 |
| `--vad-vector-gt`, `--no-vad-vector-gt` | 수치 vector GT와 확인용 BEV | 생성 |
| `--vad-vector-stride N` | vector GT를 생성할 annotation 프레임 간격 | `1` |
| `--maps-root PATH` | `Town*_HD_map.npz` 폴더 | `./maps` |
| `--fps NUMBER` | MP4 FPS | `10.0` |
| `--scale NUMBER` | BEFORE/AFTER 영상 배율 | `0.75` |
| `--start-frame NUMBER` | 카메라 bbox 시각화 시작 프레임 | 처음 |
| `--max-frames NUMBER` | 카메라 bbox 시각화 최대 프레임 수 | 전체 |

`--start-frame`과 `--max-frames`는 카메라 bbox 시각화만 제한한다. annotation
수정과 vector GT는 전체 프레임을 처리하며, vector GT의 간격은
`--vad-vector-stride`로 조절한다.

## 실행 예시

영상 없이 수정 annotation과 모든 프레임 vector GT를 만든다.

```bash
python3 run_scenario_pipeline.py \
  --input /path/to/scenario \
  --output ./outputs \
  --no-visualization \
  --no-video
```

annotation과 CSV만 만든다.

```bash
python3 run_scenario_pipeline.py \
  --input /path/to/scenario/anno \
  --output ./annotation_only \
  --no-visualization \
  --no-video \
  --no-vad-vector-gt
```

빠른 확인을 위해 카메라는 30프레임, vector GT는 10프레임 간격으로 만든다.

```bash
python3 run_scenario_pipeline.py \
  --input /path/to/scenario \
  --output ./pipeline_test \
  --max-frames 30 \
  --vad-vector-stride 10
```

## CSV 결과

`results.csv`는 시나리오당 한 행을 기록한다. 주요 컬럼은 다음과 같다.

| 컬럼 | 의미 |
| --- | --- |
| `bbox_changed_frames` | bbox가 바뀐 프레임 수 |
| `bbox_reassigned_entries` | 다른 bbox로 재배정된 traffic-light 엔트리 수 |
| `affects_ego_changed_frames` | `affects_ego`가 바뀐 프레임 수 |
| `affects_ego_changed_entries` | 값이 바뀐 traffic-light 엔트리 수 |
| `visualized_frames` | 카메라 bbox 시각화 프레임 수 |
| `comparison_created` | BEFORE/AFTER 비교 영상 생성 여부 |
| `vad_vector_gt_status` | `completed`, `skipped`, 또는 실패 상태 |
| `vad_vector_gt_frames` | 생성된 vector GT 프레임 수 |
| `vad_vector_gt_dir` | 수치 `.npz` 폴더 |
| `vad_vector_gt_visualization_dir` | vector GT BEV 이미지 폴더 |
| `vad_vector_gt_video` | vector GT MP4 경로 |
| `status`, `error` | 시나리오 실행 상태와 오류 |

`traffic_light.csv`에는 신호등별 bbox와 `affects_ego` 수정 전후 값이 기록된다.
`traffic_light_summary.csv`의 `affects_ego_changed_frames`가 비교 영상 생성 여부를
결정한다.

## 직접 Vector GT 생성

파이프라인 없이 `visualize_vad_gt.py`만 실행할 수도 있다.

```bash
python3 visualize_vad_gt.py \
  --anno-dir ./outputs/<scenario>/anno \
  --map-file ./maps/Town03_HD_map.npz \
  --scenario-name <scenario> \
  --vectors-dir ./vector_gt/vectors \
  --output-dir ./vector_gt/visualization \
  --all-frames \
  --show-training-points
```

## 설치

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
```

`fix_tl_bbox_permutation.py`의 Hungarian assignment에 SciPy가 필요하다.
현재 의존성 조건은 `requirements.txt`를 따른다.

전체 옵션은 다음 명령으로 확인한다.

```bash
python3 run_scenario_pipeline.py --help
python3 visualize_vad_gt.py --help
```
