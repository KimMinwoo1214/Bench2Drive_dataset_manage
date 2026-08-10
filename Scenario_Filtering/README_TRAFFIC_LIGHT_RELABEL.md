# Bench2Drive traffic light relevance 보수적 재라벨링

이 문서는 `b2d_traffic_light_relabeler.py`의 현재 권장 실행법을 설명합니다.
이 도구는 기존 `affects_ego=true`를 정답으로 사용하지 않고, 모든 신호등의
trigger/stop line과 실제 ego future trajectory를 다시 비교합니다.

## 안전 정책

- 원본 `json.gz`는 열기만 하며 절대 덮어쓰지 않습니다.
- `--dry-run`은 CSV report만 만들고 annotation output은 만들지 않습니다.
- `KEEP`과 `REVIEW` frame의 output은 원본 파일을 그대로 복사합니다.
- `AUTO_FIX` frame만 traffic light 객체의 기존 `affects_ego` 값을 바꿉니다.
- `state`, 위치, rotation 및 다른 annotation key는 수정하지 않습니다.
- 애매한 불일치는 `REVIEW`로 보내고 자동 수정하지 않습니다.

## 실제 데이터 구조와 좌표계

현재 샘플 annotation에서 확인한 traffic light key는 다음과 같습니다.

```text
id, state, affects_ego
location, rotation
trigger_volume_location, trigger_volume_rotation, trigger_volume_extent
road_id, section_id, lane_id
```

Ego에는 `location`, `rotation`, `road_id`, `section_id`, `lane_id`와 최상위
route command 좌표가 있습니다. Annotation의 위치와 `Town*_HD_map.npz` lane
point는 모두 CARLA world 좌표입니다. 이 판정에서는 y축 반전이나 중복 좌표
변환을 하지 않습니다. Camera/LiDAR 시각화에서만 기존 코드의 센서 좌표 변환을
그대로 사용합니다.

## 판정 흐름

1. 전체 frame에서 ego trajectory와 traffic light catalog를 구성합니다.
2. Ego trajectory와 회전된 `trigger_volume` rectangle의 거리를 계산합니다.
3. Future route가 trigger를 실제로 통과하는 encounter를 찾습니다.
4. Map이 있으면 trigger 위치에서 가장 가까운 lane point의 road/lane/yaw를 찾습니다.
5. Route lane, trajectory heading, stop-line 축, 전방 여부와 거리를 점수화합니다.
6. 각 frame에서 1위와 2위 후보 및 margin을 계산합니다.
7. Encounter 내부의 연속 ID support와 ID switch를 계산합니다.
8. 원본 ID 집합과 예측 ID를 비교해 `KEEP`, `AUTO_FIX`, `REVIEW`로 분류합니다.
9. 연속된 불일치는 frame 단위가 아니라 event 단위 CSV로 묶습니다.

앞선 stop line을 통과하면 다음 encounter가 새 temporal 구간을 시작합니다. 따라서
같은 encounter 안에서 ID가 번갈아 선택되는 것은 `temporal_id_switch`이지만,
교차로를 통과한 뒤 다음 신호등으로 바뀌는 정상 전환은 switch로 취급하지 않습니다.

## Score

최종 candidate score의 합은 1.0이며 가중치는 다음과 같습니다.

| 요소 | 가중치 | 의미 |
|---|---:|---|
| Route-stop-line geometry | 0.38 | Future trajectory와 회전된 trigger 영역의 대응 |
| Clean crossing | 0.15 | Trigger 전후의 연결된 trajectory가 모두 존재 |
| Map lane match | 0.14 | Trigger 인근 HD-map lane과 실제 route lane/yaw 대응 |
| Lane/heading | 0.10 | Stop-line의 좁은 축과 ego 진행축 정렬 |
| Same junction | 0.05 | Route가 해당 stop line을 통과하는 junction으로 추론 |
| State response | 0.05 | 정지 중 green 전환 뒤 지속 출발했는지 확인 |
| Upcoming/forward | 0.04 | 아직 통과하지 않은 future encounter |
| Distance | 0.04 | Ego와 trigger의 현재 거리 |
| Temporal support | 0.05 | 같은 ID가 encounter에서 연속 선택되는 비율 |

Map이 없거나 메모리 보호 때문에 생략되면 map lane 항목은 중립값 0.5를 사용하고
`map_lane_unavailable`을 reason에 기록합니다. 기본값에서 256MB보다 큰 dense NPZ는
생략합니다. 충분한 메모리가 있을 때 `--max-map-file-mb`로 상한을 올릴 수 있습니다.

State response는 geometry를 대체하지 않는 보조 신호입니다. 정지 중 후보 신호가
green으로 바뀐 뒤 기본 15 frame 안에 `0.30m/s` 이상 속도가 3 frame 이상 지속되면
positive evidence로 기록합니다. 장애물이나 앞차 때문에 green에도 출발하지 못할 수
있으므로 이 항목 하나만으로 AUTO_FIX하지 않습니다.

## 상태 기준

```text
원본 ID 집합 == 예측 ID 집합
  -> KEEP

불일치
+ best_score >= --score-threshold (기본 0.90)
+ best_score - second_score >= --margin-threshold (기본 0.20)
+ temporal support >= 0.80
+ 동일 ID run >= 3 frame
+ temporal ID switch 없음
+ clean stop-line crossing
  -> AUTO_FIX

그 외 불일치
  -> REVIEW
```

예측 후보가 없는데 원본 true가 남아 있는 경우도 자동으로 지우지 않고 `REVIEW`로
보냅니다. 이는 false correction을 줄이기 위한 의도적인 보수 정책입니다.

## Dry-run

```bash
python3 b2d_traffic_light_relabeler.py \
  --input ./Scenario \
  --output ./Scenario_relabel \
  --report-dir ./reports \
  --score-threshold 0.90 \
  --margin-threshold 0.20 \
  --dry-run
```

이 명령은 `./reports`만 만들고 `./Scenario_relabel` 아래 annotation은 만들지 않습니다.

## 소수 scenario 검증

앞의 3개만 처리:

```bash
python3 b2d_traffic_light_relabeler.py \
  --input ./Scenario \
  --output ./Scenario_relabel_sample \
  --report-dir ./reports_sample \
  --max-scenarios 3 \
  --dry-run
```

이름으로 선택할 때는 `--scenario`를 반복할 수 있습니다.

```bash
python3 b2d_traffic_light_relabeler.py \
  --input ./Scenario \
  --output ./Scenario_relabel_sample \
  --report-dir ./reports_sample \
  --scenario SignalizedJunctionLeftTurn \
  --scenario SignalizedJunctionRightTurn \
  --dry-run
```

## Annotation 복사본 생성

`--dry-run`을 제거하면 `AUTO_FIX`만 반영한 별도 annotation tree를 만듭니다.

```bash
python3 b2d_traffic_light_relabeler.py \
  --input ./Scenario \
  --output ./Scenario_relabel \
  --report-dir ./reports \
  --score-threshold 0.90 \
  --margin-threshold 0.20
```

## 출력

```text
Scenario_relabel/
└── <scenario>/anno/*.json.gz

reports/
├── traffic_light_report.csv
├── review_events.csv
├── auto_fix_events.csv
└── summary.csv
```

`traffic_light_report.csv`는 각 frame의 모든 traffic light를 한 행씩 기록합니다.
신호등이 없는 frame도 빈 `traffic_light_id` 행을 하나 기록합니다.

```text
scenario, frame, traffic_light_id
original_affects, predicted_affects, score
best_score, second_score, margin, status, reason
```

`review_events.csv`와 `auto_fix_events.csv`는 연속된 동일 불일치를 묶으며 다음
정보를 포함합니다.

```text
scenario, event_id, start_frame, end_frame
original_tl, predicted_tl
best_score, min_score, max_score, min_margin
frame_count, reason, status
```

Temporal switching 구간은 예측 ID가 번갈아도 하나의 REVIEW event로 묶고,
`predicted_tl`에 해당 ID들을 `|`로 연결해 기록합니다.

## Review event 시각화

```bash
python3 visualize.py \
  -f ./Scenario/<scenario> \
  -m 04 \
  --output-dir ./review_visualization \
  --review-events ./reports/review_events.csv \
  --review-context 20
```

AUTO_FIX 표본 검수는 `--review-events ./reports/auto_fix_events.csv`로 같은 방식으로
실행합니다. Event 범위가 겹치면 자동으로 병합합니다. `--anno-dir`에 보정본의
`anno` 폴더를 지정하면 보정 결과 기준으로 렌더링할 수 있습니다.

기존 `traffic_light_affect.py`는 원본 또는 보정본에서 최종
`affects_ego=true`만 추출하는 용도로 그대로 사용할 수 있습니다.

## Camera ID 표시 예외

`VanillaSignalizedTurnEncounterRedLight_Town12_Route15567_Weather22`는 RGB에
렌더링된 signal mesh와 annotation traffic-light ID가 한 칸씩 순환 연결됩니다.
이 문제는 relevance나 원본 annotation을 수정하지 않고 카메라 bbox에만 명시적인
override를 적용합니다.

```text
annotation source ID -> camera geometry ID
16710 -> 17142
16762 -> 16710
16976 -> 16762
17142 -> 16976
```

`traffic_light_visual_overrides.json`은 이 시나리오만 포함합니다. 옵션을 생략하면
모든 시나리오가 기존 방식으로 렌더링됩니다.

```bash
python3 render_corrected_clip.py \
  ./Scenario/VanillaSignalizedTurnEncounterRedLight_Town12_Route15567_Weather22 \
  --anno-dir ./Scenario/VanillaSignalizedTurnEncounterRedLight_Town12_Route15567_Weather22/anno \
  --output-dir ./outputs/vanilla_visual_id_fixed \
  --cameras front \
  --no-lidar \
  --traffic-light-visual-overrides ./traffic_light_visual_overrides.json
```

`visualize.py`에도 같은 `--traffic-light-visual-overrides` 옵션을 사용할 수 있습니다.
카메라 label의 `[box <ID>]`는 override에 사용한 실제 geometry ID를 남기는 감사용
표시입니다. LiDAR BEV의 trigger volume과 relabel report에는 이 치환을 적용하지
않습니다.

## 회귀 테스트

```bash
python3 -m unittest -v test_traffic_light_relabeler.py
```

테스트에는 정상 라벨 KEEP, 잘못된 ID AUTO_FIX, 동점 후보 REVIEW, temporal ID
switch REVIEW, stop line 통과 후 해제, 무신호 정상 구간, state 보존 검사가 포함됩니다.
