# Bench2Drive Traffic Light Annotation Pipeline

Bench2Drive 시나리오의 traffic light annotation을 보정하고, 전방 카메라,
TOP_DOWN BEV bbox, HD vector map 시각화, MP4 영상, 판정 CSV를 한 번에 생성하는
파이프라인이다.

최종 annotation은 다음 두 작업을 순서대로 적용해 만든다.

1. 선택된 전체 데이터에서 잘못 순환 배정된 traffic light bbox permutation 복구
2. 복구된 bbox의 실제 trigger volume을 Ego 궤적이 통과하는지 검사해
   `traffic_light.affects_ego` 판정

`affects_ego` 판정에는 HD map이나 차량 반응 휴리스틱을 사용하지 않는다. 실제
trigger volume 교차, 통과 방향, bbox 신뢰성, 시간 연속성을 모두 만족하는 경우에만
자동 수정하고, 근거가 부족하거나 충돌하면 원본 값을 유지한 채 `REVIEW`로
기록한다. HD vector map은 선택적인 시각화와 BEV lane overlay에만 사용한다.

## 주요 파일

| 파일 | 역할 |
| --- | --- |
| `run_scenario_pipeline.py` | bbox 복구, relevance 판정, 시각화, 영상, CSV를 실행하는 메인 진입점 |
| `fix_tl_bbox_permutation.py` | traffic light bbox permutation 복구. 메인 파이프라인은 항상 `--bbox-only`로 실행 |
| `traffic_light_relevance.py` | trigger-volume 교차 기반 `affects_ego` KEEP/AUTO_FIX/REVIEW 판정 |
| `test_traffic_light_relevance.py` | 교차 판정, 방향 불일치, 다중 실제 교차 단위 테스트 |
| `visualize.py` | 전방 카메라, TOP_DOWN BEV 및 HD vector map 렌더링 |
| `apply_visualize_compare_from_summary.py` | BEFORE/AFTER 비교 영상 생성 공통 함수 |
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
NumPy와 SciPy import 오류가 나면 활성화된 Python과 설치 버전을 확인한다.

```bash
which python3
python3 -c "import numpy, scipy; print(numpy.__version__, scipy.__version__)"
```

## 입력 구조

`--input`에는 개별 JSON 파일이 아니라 다음 중 하나의 폴더 경로를 입력한다.

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

annotation만 보정하려면 센서 이미지 없이 `--no-visualization --no-video`로
실행할 수 있다.

vector map 생성을 활성화하면 `--map-root` 아래에 시나리오 Town과 맞는 파일이
있어야 한다.

```text
maps/
├── Town03_HD_map.npz
├── Town04_HD_map.npz
└── ...
```

map이 다른 위치에 있으면 `--map-root /path/to/maps`를 지정한다. vector map 결과가
필요하지 않으면 `--no-vector-map`을 사용한다.

## Base1000 + Weak329 production

Production에서는 폴더 전체를 재귀적으로 처리하지 않고 고정 결합 split의 clip만
처리한다. Base와 Weak는 서로 다른 물리 root를 사용하되 같은 output root에
component별로 누적한다.

```bash
SPLIT=Weak_Scenario_Mining/data/bench2drive_base1000_weak329_train_val_split.json

python3 Scenario_Filtering/run_scenario_pipeline.py \
  --input "$BASE_ROOT" --manifest "$SPLIT" --component base \
  --output "$RELABEL_ROOT" --resume \
  --no-visualization --no-video --no-vector-map

python3 Scenario_Filtering/run_scenario_pipeline.py \
  --input "$WEAK_ROOT" --manifest "$SPLIT" --component weak \
  --output "$RELABEL_ROOT" --resume \
  --no-visualization --no-video --no-vector-map
```

입력 root는 해당 component 목록과 정확히 같아야 한다. 누락, 중복 이름, split 밖
annotation clip이 있으면 수정 전에 실패한다. bbox consensus는 매 실행마다 선택된
component 전체를 스캔하므로, 일부 실패 clip을 재처리할 때도 나머지 clip의 bbox
근거가 유지된다.

각 clip의 `traffic_light/completion.json`에는 component, manifest/config/구현
SHA256, 원본·corrected annotation SHA256, frame 수와 집합 일치 여부, status와
판정 지표가 저장된다. `--resume`은 현재 값과 모두 일치하는 `completed` clip만
건너뛴다. `review`나 hash가 오래된 clip은 다시 처리된다. 주행과 무관한 다른
신호등의 bbox `no_consensus`는 warning이지만 관련 trigger 판정이 명확하면 clip은
`completed`가 될 수 있다.

사람 승인은 annotation을 바꾸지 않고 별도 JSON으로 관리한다. component, clip,
`completion_sha256`가 모두 같아야 유효하므로 결과가 바뀌면 옛 승인은 자동
무효다.

```json
{
  "schema_version": 1,
  "approvals": [
    {
      "component": "weak",
      "clip": "Example_Town10HD_Route1_Weather26",
      "completion_sha256": "...",
      "approved_by": "reviewer-name",
      "reason": "front/BEV와 원본 frame 집합 확인"
    }
  ]
}
```

최종 검사는 production 실행과 같은 relevance/visualization 옵션을 전달한다.
`--check-only`는 bbox, annotation, CSV를 새로 쓰지 않는다.

```bash
python3 Scenario_Filtering/run_scenario_pipeline.py \
  --input "$BASE_ROOT" --manifest "$SPLIT" --component base \
  --output "$RELABEL_ROOT" --check-only \
  --review-approvals "$APPROVALS" \
  --no-visualization --no-video --no-vector-map

python3 Scenario_Filtering/run_scenario_pipeline.py \
  --input "$WEAK_ROOT" --manifest "$SPLIT" --component weak \
  --output "$RELABEL_ROOT" --check-only \
  --review-approvals "$APPROVALS" \
  --no-visualization --no-video --no-vector-map
```

## 기본 실행

시나리오 하나를 전체 처리한다.

```bash
python3 run_scenario_pipeline.py \
  --input /path/to/scenario \
  --output /path/to/pipeline_output \
  --map-root /path/to/maps
```

여러 시나리오가 있는 상위 폴더도 같은 방식으로 처리한다.

```bash
python3 run_scenario_pipeline.py \
  --input /path/to/Carla \
  --output /path/to/pipeline_output \
  --map-root /path/to/maps
```

`--output`은 `--input` 내부가 아닌 별도 경로여야 한다. 같은 시나리오를
재실행하면 해당 출력 시나리오 폴더를 새로 만들고, 다른 시나리오의 기존 출력은
유지한다. 원본 annotation은 덮어쓰지 않는다.

## 빠른 테스트

annotation과 CSV만 생성한다.

```bash
python3 run_scenario_pipeline.py \
  --input /path/to/scenario/anno \
  --output ./annotation_result \
  --no-visualization \
  --no-video
```

처음 30프레임만 시각화한다.

```bash
python3 run_scenario_pipeline.py \
  --input /path/to/scenario \
  --output ./pipeline_test \
  --map-root /path/to/maps \
  --max-frames 30
```

카메라와 BEV bbox만 만들고 HD vector map은 제외한다.

```bash
python3 run_scenario_pipeline.py \
  --input /path/to/scenario \
  --output ./visualization_result \
  --no-vector-map
```

`--max-frames`와 `--start-frame`은 시각화 범위만 제한한다. bbox 및
`affects_ego` 판정은 클립 전체 프레임을 대상으로 수행한다.

## 처리 순서와 판정 기준

1. 선택된 모든 annotation을 한 번에 읽어 bbox 대응 관계와 전역 consensus 계산
2. bbox permutation 및 trigger rotation 복구. 이 단계에서는 `affects_ego` 보존
3. 각 클립에서 연속 Ego 선분과 회전된 실제 trigger rectangle의 교차 계산
4. trigger 진입·중심 통과·완전 이탈 프레임을 구분하고, 완전 이탈 직전까지
   `affects_ego` 구간으로 구성
5. 이탈 프레임 이전 최대 60 m를 접근 구간으로 잡되, 앞 신호의 이탈 시점에서
   다음 신호로 takeover하여 이벤트 구간이 겹치지 않게 절단
6. 궤적 진행 방향, bbox 신뢰성, 최소 연속 프레임, 실제 다중 교차 여부 검사
7. `KEEP`, `AUTO_FIX`, `REVIEW` 중 하나로 프레임별 판정
8. `AUTO_FIX` 프레임에만 `affects_ego` 변경 후 최종 annotation 저장
9. 전방 카메라, TOP_DOWN BEV, 선택적 vector map 시각화와 영상 생성
10. 상세 CSV 및 전체 `results.csv` 생성

기본 trigger margin은 `0.0 m`이다. 즉 Ego의 프레임 간 선분이 annotation에
기록된 실제 trigger rectangle과 교차해야 한다. margin을 크게 주면 서로 가까운
다른 차로의 trigger를 실제보다 일찍 통과한 것으로 판단할 수 있으므로, 데이터셋
검수 없이 기본값을 변경하지 않는 것을 권장한다.

종료 기준은 trigger 중심이 아니라 진행 방향 쪽 far edge이다. 프레임 간 선분이
volume에 진입한 순간에는 relevance를 끄지 않고, Ego의 샘플 위치가 rectangle을
완전히 벗어난 첫 프레임부터 `affects_ego=false`로 전환한다. PKL의
`ego_tl_stopline`은 기존 계약대로 `trigger_volume_location` 중심을 유지한다.
따라서 target 좌표 스키마는 바뀌지 않고, 신호 state·UV·stop-point supervision을
trigger 내부 주행 구간까지 유지하는 시간 의미만 명확해진다.

`REVIEW`가 되는 대표적인 경우는 다음과 같다.

- trigger를 통과했지만 궤적 진행 방향이 해당 신호 방향과 맞지 않음
- bbox 복구 결과의 신뢰성을 확인할 수 없음
- 서로 다른 두 trigger를 설정된 프레임 차이 안에서 실제로 모두 통과함
- 예측 ID의 연속 구간이 최소 길이보다 짧음
- trigger 교차 근거가 없는데 원본에 `affects_ego=true`가 존재함

긴 접근 구간 안에 여러 신호등이 보인다는 이유만으로 다중 후보로 보지 않는다.
`--simultaneous-crossing-frames`는 Ego 궤적이 서로 다른 실제 trigger volume을
거의 동시에 교차한 경우에만 적용한다.

## 실행 옵션

| 옵션 | 설명 | 기본값 |
| --- | --- | --- |
| `--input PATH` | 시나리오, `anno`, 또는 데이터셋 상위 폴더 | 필수 |
| `--output PATH` | 모든 결과가 생성될 폴더 | 필수 |
| `--manifest PATH` | 고정 train/val 및 component 목록 | 단일 입력에서는 미사용 |
| `--component {base,weak,all}` | manifest에서 처리할 물리 component | `all` |
| `--resume` | hash가 맞는 `completed` clip만 건너뜀 | 끔 |
| `--check-only` | output을 쓰지 않고 production 완결성 검사 | 끔 |
| `--review-approvals PATH` | completion hash에 묶인 사람 승인 JSON | 없음 |
| `--visualization`, `--no-visualization` | 카메라/BEV bbox 이미지 생성 여부 | 생성 |
| `--video`, `--no-video` | 수정 결과 및 조건부 비교 MP4 생성 여부 | 생성 |
| `--vector-map`, `--no-vector-map` | HD vector map 및 BEV lane overlay 생성 여부 | 생성 |
| `--map-root PATH` | `Town*_HD_map.npz` 파일이 있는 폴더 | `Scenario_Filtering/maps` |
| `--fps NUMBER` | MP4 초당 프레임 수 | `10.0` |
| `--scale NUMBER` | BEFORE/AFTER 비교 영상 크기 배율 | `0.75` |
| `--start-frame NUMBER` | 시각화를 시작할 프레임 번호 | 전체 시작 |
| `--max-frames NUMBER` | 시각화할 최대 프레임 수 | 전체 |
| `--approach-distance METRES` | trigger 통과 전 접근 이벤트 최대 거리 | `60.0` |
| `--max-step METRES` | 연속 궤적으로 인정할 최대 프레임 이동 거리 | `5.0` |
| `--trigger-margin METRES` | trigger 교차 판정 여유 거리 | `0.0` |
| `--maximum-heading-error DEGREES` | AUTO_FIX 허용 진행 방향 오차 | `35.0` |
| `--simultaneous-crossing-frames N` | 실제 다중 trigger 교차 충돌 판정 범위 | `3` |
| `--minimum-temporal-run-frames N` | AUTO_FIX 동일 ID 최소 연속 프레임 | `3` |

전체 옵션은 `python3 run_scenario_pipeline.py --help`로 확인할 수 있다.

## 비교 영상 생성 조건

시각화와 영상 생성이 활성화되어 있고, bbox 또는 `affects_ego`가 한 프레임이라도
바뀌면 BEFORE/AFTER 비교 영상을 생성한다.

- `before_after_front.mp4`: 전방 카메라 BEFORE/AFTER
- `before_after_bev.mp4`: TOP_DOWN 카메라 BEV BEFORE/AFTER

변경이 없으면 수정 결과 영상만 생성한다. vector map이 활성화된 AFTER BEV에는
HD vector lane overlay가 포함된다.

## 출력 구조

```text
<output>/
├── production_reports/                # --manifest 실행
│   ├── base/{bbox_details.csv,bbox_details_summary.csv,results.csv}
│   ├── weak/{bbox_details.csv,bbox_details_summary.csv,results.csv}
│   ├── aggregate_results.csv
│   └── review_queue.csv
├── results.csv                         # 기존 단일 입력 실행
├── bbox_reports/                       # 기존 단일 입력 실행
│   ├── bbox_details.csv
│   └── bbox_details_summary.csv
└── <scenario>/
    ├── traffic_light/
    │   ├── completion.json             # --manifest 실행
    │   ├── corrected_anno/
    │   │   ├── 00000.json.gz
    │   │   └── ...
    │   └── reports/
    │       ├── relevance_frames.csv
    │       ├── relevance_events.csv
    │       └── affects_ego_changes.csv
    ├── visualization/
    │   ├── after/
    │   │   └── camera/
    │   │       ├── rgb_front_3d_bbox/
    │   │       ├── rgb_top_down_3d_bbox/   # vector map 활성화 시 lane overlay
    │   │       └── rgb_front_landmark/     # vector map 활성화 시
    │   └── before/                         # annotation 변경 시
    │       └── camera/
    │           ├── rgb_front_3d_bbox/
    │           └── rgb_top_down_3d_bbox/
    └── videos/
        ├── after_front.mp4
        ├── after_bev.mp4
        ├── vector_map.mp4                  # vector map 활성화 시
        ├── before_after_front.mp4          # annotation 변경 시
        └── before_after_bev.mp4            # annotation 변경 시
```

## CSV 결과

### `results.csv`

전체 실행 결과를 시나리오당 한 행으로 기록한다. 주요 컬럼은 다음과 같다.

| 컬럼 | 의미 |
| --- | --- |
| `bbox_changed_frames`, `bbox_reassigned_entries` | bbox 복구 변경량 |
| `crossing_events` | 실제 trigger 교차 이벤트 수 |
| `keep_frames`, `auto_fix_frames`, `review_frames` | relevance 판정별 프레임 수 |
| `affects_ego_changed_frames`, `affects_ego_changed_entries` | 실제 `affects_ego` 변경량 |
| `visualized_frames`, `vector_map_frames` | 카메라/BEV 및 vector map 프레임 수 |
| `after_front_video`, `after_bev_video`, `vector_map_video` | 수정 결과 영상 경로 |
| `comparison_created` | BEFORE/AFTER 비교 영상 생성 여부 |
| `detail_csv`, `summary_csv` | 전체 bbox 진단 CSV 경로 |
| `status` | `completed`, `review`, 또는 `failed` |
| `error` | 실패한 경우 오류 내용 |

### bbox 보고서

`bbox_reports/bbox_details.csv`는 traffic light 엔트리별 bbox 복구 전후,
`ok_before`, `ok_after`, `action`, `bbox_taken_from`을 기록한다.
`bbox_details_summary.csv`는 선택된 클립별 bbox 통계를 기록한다.

### relevance 보고서

- `relevance_frames.csv`: 프레임별 원본 ID, 예측 ID, KEEP/AUTO_FIX/REVIEW,
  confidence 및 판정 이유
- `relevance_events.csv`: trigger 교차 이벤트의 신호등 ID, 접근 시작,
  `trigger_entry_frame`, `trigger_center_frame`, `trigger_exit_frame`, 방향 오차,
  bbox 신뢰성, 경쟁 이벤트. 호환용 `crossing_frame`은 exit frame과 같다.
- `affects_ego_changes.csv`: AUTO_FIX로 실제 값이 바뀐 엔트리의 전후 값

## bbox 복구만 직접 실행

메인 파이프라인과 동일하게 `affects_ego`를 보존하면서 bbox만 복구하려면 반드시
`--bbox-only`를 사용한다.

```bash
python3 fix_tl_bbox_permutation.py \
  --root /path/to/dataset_or_anno \
  --out ./bbox_fixed \
  --csv ./bbox_details.csv \
  --bbox-only
```

`fix_tl_bbox_permutation.py`를 `--bbox-only` 없이 직접 실행하면 기존의 단순
`affects_ego` 재계산 경로도 함께 실행된다. 이는 호환성을 위해 남아 있지만,
`run_scenario_pipeline.py`의 최종 판정 경로에서는 사용하지 않는다.

## 테스트

```bash
python3 -m unittest -v test_traffic_light_relevance
python3 -m compileall -q .
```

## 문제 해결

### HD vector map을 찾지 못함

시나리오 이름의 Town과 같은 `Town*_HD_map.npz`가 `--map-root` 아래에 있는지
확인한다. map 시각화가 필요하지 않으면 `--no-vector-map`을 사용한다.

### 시각화에서 이미지 파일을 찾지 못함

`--input`이 원본 `camera/rgb_front`와 `camera/rgb_top_down`을 포함하는 시나리오를
가리키는지 확인한다. 센서 데이터가 없다면 `--no-visualization --no-video`로
annotation만 처리한다.

### AUTO_FIX 대신 REVIEW가 많이 나옴

`relevance_frames.csv`의 `reason`과 `bbox_reliable`, `heading_error_degrees`,
`temporal_run`을 확인한다. REVIEW는 원본을 수정하지 않으므로 원인을 검수한 뒤
필요한 경우에만 threshold를 조정한다.

### 비교 영상이 생성되지 않음

`results.csv`의 `bbox_changed_frames`, `affects_ego_changed_frames`,
`comparison_created`를 확인한다. 두 변경량이 모두 0이면 비교 영상을 만들지 않는
것이 정상이다.
