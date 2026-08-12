# Bench2Drive Traffic Light Annotation Pipeline

Bench2Drive 시나리오의 traffic light annotation을 수정하고, 전방 카메라와
TOP_DOWN BEV bbox 시각화, MP4 영상, 최종 결과 CSV까지 한 번에 생성하는
파이프라인이다.

현재 annotation 수정은 다음 두 작업을 하나의 파일에서 연속으로 수행한다.

1. 잘못 순환 배정된 traffic light bbox permutation 복구
2. 복구된 bbox를 기준으로 `traffic_light.affects_ego` 재계산

두 작업은 같은 annotation 객체에 적용되며, 모든 수정이 끝난 뒤 프레임별
annotation 파일을 한 번만 저장한다. bbox 수정본과 `affects_ego` 수정본이
서로 다른 중간 파일로 생성되지 않는다.

## 주요 파일

| 파일 | 역할 |
| --- | --- |
| `run_scenario_pipeline.py` | annotation 수정, 시각화, 영상, 최종 CSV를 실행하는 메인 진입점 |
| `fix_tl_bbox_permutation.py` | bbox permutation 복구와 `affects_ego` 재계산을 통합 수행 |
| `visualize.py` | 전방 카메라와 TOP_DOWN 카메라 BEV에 3D bbox 렌더링 |
| `apply_visualize_compare_from_summary.py` | BEFORE/AFTER 비교 영상 생성에 사용하는 공통 함수 및 별도 적용 도구 |
| `requirements.txt` | Python 의존성 목록 |

## 설치

Python 가상환경 사용을 권장한다.

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
```

`fix_tl_bbox_permutation.py`는 SciPy의 Hungarian assignment를 사용한다.
NumPy가 너무 오래되면 SciPy import 단계에서 실패하므로 다음 버전 조건을
만족해야 한다.

```text
numpy>=1.23.5,<2.5
```

현재 환경에서 SciPy 관련 import 오류가 나오면 활성화된 Python과 설치 위치를
먼저 확인한다.

```bash
which python3
python3 -c "import numpy, scipy; print(numpy.__version__, scipy.__version__)"
```

## 입력 구조

`--input`에는 개별 JSON 파일이 아니라 다음 중 하나의 **폴더 경로**를 입력한다.

- 시나리오 폴더: `<scenario>/anno`가 있는 폴더
- annotation 폴더: 이름이 `anno`인 폴더
- 여러 시나리오가 들어 있는 상위 데이터셋 폴더

시각화까지 실행하려면 시나리오 폴더에 원본 센서 이미지가 있어야 한다.

```text
<scenario>/
├── anno/
│   ├── 00000.json.gz
│   ├── 00001.json.gz
│   └── ...
└── camera/
    ├── rgb_front/
    └── rgb_top_down/
```

annotation만 수정하려면 센서 이미지 없이 `--no-visualization --no-video`로
실행할 수 있다.

## 기본 실행

시나리오 하나를 전체 처리한다.

```bash
python3 run_scenario_pipeline.py \
  --input /path/to/scenario \
  --output /path/to/pipeline_output
```

여러 시나리오가 있는 상위 폴더도 같은 방식으로 처리한다.

```bash
python3 run_scenario_pipeline.py \
  --input /path/to/Carla \
  --output /path/to/pipeline_output
```

`--output`은 `--input` 내부가 아닌 별도 경로여야 한다. 동일한 시나리오를
재실행하면 해당 출력 시나리오 폴더는 지우고 새로 생성한다. 다른 시나리오의
기존 출력은 유지한다.

## 빠른 테스트

처음 30프레임만 수정 및 시각화한다.

```bash
python3 run_scenario_pipeline.py \
  --input /path/to/scenario \
  --output ./pipeline_test \
  --max-frames 30
```

주의: `--max-frames`와 `--start-frame`은 시각화 범위만 제한한다. annotation
수정은 클립 전체 프레임을 대상으로 수행한다.

annotation과 CSV만 생성한다.

```bash
python3 run_scenario_pipeline.py \
  --input /path/to/scenario/anno \
  --output ./annotation_result \
  --no-visualization \
  --no-video
```

시각화 이미지는 만들되 MP4는 만들지 않는다.

```bash
python3 run_scenario_pipeline.py \
  --input /path/to/scenario \
  --output ./visualization_result \
  --no-video
```

## 실행 옵션

| 옵션 | 설명 | 기본값 |
| --- | --- | --- |
| `--input PATH` | 시나리오, `anno`, 또는 데이터셋 상위 폴더 | 필수 |
| `--output PATH` | 모든 결과가 생성될 폴더 | 필수 |
| `--visualization`, `--no-visualization` | 카메라/BEV bbox 이미지 생성 여부 | 생성 |
| `--video`, `--no-video` | 수정 결과 및 조건부 비교 MP4 생성 여부 | 생성 |
| `--fps NUMBER` | MP4 초당 프레임 수 | `10.0` |
| `--scale NUMBER` | BEFORE/AFTER 비교 영상 크기 배율 | `0.75` |
| `--start-frame NUMBER` | 시각화를 시작할 프레임 번호 | 전체 시작 |
| `--max-frames NUMBER` | 시각화할 최대 프레임 수 | 전체 |

전체 옵션은 다음 명령으로 확인할 수 있다.

```bash
python3 run_scenario_pipeline.py --help
```

## 처리 순서

시나리오마다 다음 순서로 실행된다.

1. annotation 파일 전체 로드
2. 데이터셋/클립의 traffic light bbox 대응 관계 계산
3. bbox permutation 복구
4. 수정된 bbox를 사용해 `affects_ego` 재계산 및 클립 구간 투표
5. 최종 annotation을 `<output>/<scenario>/anno`에 한 번 저장
6. 수정 annotation으로 전방 카메라와 TOP_DOWN BEV 이미지 생성
7. 수정 결과의 front/BEV MP4 생성
8. `affects_ego` 변경 여부에 따라 BEFORE/AFTER 비교 영상 생성
9. 시나리오별 상세 CSV와 전체 `results.csv` 기록

원본 annotation은 덮어쓰지 않는다. 모든 수정본은 `--output` 아래에 생성된다.

## 비교 영상 생성 조건

BEFORE/AFTER 비교 영상은 아래 조건을 모두 만족할 때만 생성된다.

- 시각화가 활성화되어 있음
- 영상 생성이 활성화되어 있음
- 해당 시나리오의 `affects_ego_changed_frames`가 1 이상임

bbox만 변경되고 `affects_ego`는 바뀌지 않았다면 수정 결과 영상은 생성하지만
BEFORE/AFTER 비교 영상과 BEFORE 시각화 이미지는 생성하지 않는다.

`affects_ego`가 바뀐 경우에는 다음 두 비교 영상이 생성된다.

- `before_after_front.mp4`: 전방 카메라 BEFORE/AFTER
- `before_after_bev.mp4`: TOP_DOWN 카메라 BEV BEFORE/AFTER

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
    ├── visualization/
    │   ├── after/
    │   │   └── camera/
    │   │       ├── rgb_front_3d_bbox/
    │   │       └── rgb_top_down_3d_bbox/
    │   └── before/                         # affects_ego 변경 시에만
    │       └── camera/
    │           ├── rgb_front_3d_bbox/
    │           └── rgb_top_down_3d_bbox/
    └── videos/
        ├── after_front.mp4
        ├── after_bev.mp4
        ├── before_after_front.mp4          # affects_ego 변경 시에만
        └── before_after_bev.mp4            # affects_ego 변경 시에만
```

## CSV 결과

### `results.csv`

전체 실행 결과를 시나리오당 한 행으로 기록한다. 여러 시나리오를 처리할 때도
각 시나리오가 끝날 때마다 갱신되므로 중간 실패가 발생해도 앞선 결과가 남는다.

주요 컬럼:

| 컬럼 | 의미 |
| --- | --- |
| `scenario` | 시나리오 이름 |
| `input_anno` | 원본 annotation 경로 |
| `output_anno` | 최종 수정 annotation 경로 |
| `annotation_frames` | 입력 annotation 프레임 수 |
| `bbox_changed_frames` | bbox가 바뀐 프레임 수 |
| `bbox_reassigned_entries` | 다른 bbox로 재배정된 traffic light 엔트리 수 |
| `affects_ego_changed_frames` | `affects_ego`가 바뀐 프레임 수 |
| `affects_ego_changed_entries` | 값이 바뀐 traffic light 엔트리 수 |
| `visualized_frames` | 생성된 전방 시각화 프레임 수 |
| `after_front_video`, `after_bev_video` | 수정 결과 영상 경로 |
| `comparison_created` | 비교 영상 생성 여부 |
| `comparison_front_video`, `comparison_bev_video` | 비교 영상 경로 |
| `status` | `completed` 또는 `failed` |
| `error` | 실패한 경우 오류 내용 |

### `traffic_light.csv`

traffic light 엔트리별 bbox 및 `affects_ego` 수정 전후 값을 기록한다.
`affects_ego_before`, `affects_ego_after`, `fac_err_before`, `fac_err_after`,
`action`, `bbox_taken_from` 등을 확인할 수 있다.

### `traffic_light_summary.csv`

클립별 bbox 및 `affects_ego` 변경 통계를 기록한다. 파이프라인은 이 파일의
`affects_ego_changed_frames`를 읽어 비교 영상 생성 여부를 결정한다.

## annotation 수정만 직접 실행

전체 파이프라인 없이 통합 annotation 수정 도구만 실행할 수도 있다.

감사 및 CSV 생성만 수행한다.

```bash
python3 fix_tl_bbox_permutation.py \
  --root /path/to/anno \
  --csv ./traffic_light.csv
```

별도 폴더에 최종 수정본을 생성한다.

```bash
python3 fix_tl_bbox_permutation.py \
  --root /path/to/anno \
  --out ./fixed_anno \
  --csv ./traffic_light.csv
```

원본을 직접 수정하는 `--apply`도 제공하지만 `.bak` 백업이 생성된다. 원본 보존과
결과 관리가 필요한 일반 작업에서는 `run_scenario_pipeline.py` 또는 `--out` 사용을
권장한다.

## 문제 해결

### SciPy import 오류

다음과 같이 NumPy와 SciPy 버전을 확인하고 의존성을 다시 설치한다.

```bash
python3 -m pip install -r requirements.txt
python3 -c "import numpy, scipy; print(numpy.__version__, scipy.__version__)"
```

### 시각화에서 이미지 파일을 찾지 못함

`--input`이 `anno`만 복사한 폴더가 아니라, 원본 `camera/rgb_front`와
`camera/rgb_top_down`을 포함하는 시나리오를 가리키는지 확인한다. 센서 데이터가
없다면 `--no-visualization --no-video`로 annotation만 처리한다.

### 비교 영상이 생성되지 않음

`<output>/<scenario>/reports/traffic_light_summary.csv`의
`affects_ego_changed_frames`를 확인한다. 이 값이 `0`이면 의도적으로 비교 영상을
생성하지 않는다. 수정 결과 영상인 `after_front.mp4`와 `after_bev.mp4`는
`--video`가 활성화되어 있으면 별도로 생성된다.
