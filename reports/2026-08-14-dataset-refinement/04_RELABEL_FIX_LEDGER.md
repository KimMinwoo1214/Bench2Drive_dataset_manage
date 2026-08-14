# 신호등 relabel — 무엇이 틀렸고 무엇을 고쳤나

대상 **717 클립** · 신호등 엔트리 **1,119,119개**

## 1. 무엇이 틀렸나

수집 코드가 actor와 level bbox를 **2D 최근접 greedy**로 붙였다. 그런데
마스트형 신호등은 폴(actor 원점)이 모퉁이에 있고, 그 폴이 통제하는
등(head)은 **교차로 건너편**에 매달려 있다. 그래서 최근접으로 고르면
**항상 옆 접근로의 head**가 잡히고, 배정이 교차로 단위로 한 칸씩 회전한다.

그 결과 한 엔트리 안에 서로 다른 두 신호등의 정보가 섞인다.

| 필드                                                                        | 출처         | 상태              |
|-------------------------------------------------------------------------- |----------- |---------------- |
| `location` `rotation` `center` `extent` `road_id` `lane_id` `section_id`  | level bbox | **틀림 (한 칸 밀림)** |
| `id` `state` `distance` `trigger_volume_location` `trigger_volume_extent` | actor      | 정확              |
| `trigger_volume_rotation`                                                 | 둘의 합       | **오염**          |

## 2. 어떻게 고쳤나

head는 자기 trigger volume의 **건너편**에 있어야 한다. 교차로 중심 기준
각도로 `head_angle ≈ tv_angle + 180°`. 이 조건을 비용으로 두고 교차로
단위로 **Hungarian**을 풀면 배정이 유일하게 확정된다.

`distance` 기반 재배정은 쓰지 않는다. 수집 코드의 greedy가 바로 그 값을
최소화했으므로 **같은 오답을 재생산**하기 때문이다.

bbox를 고친 **같은 annotation 객체**에서 곧바로 `affects_ego`를 다시
계산하고, 프레임 파일은 그 뒤 한 번만 저장한다. 두 수정본이 따로 생기지
않게 하려는 것이다.

## 3. 얼마나 틀렸나

| 판정    |     엔트리 |    비율 | 뜻                  |
|------ |--------:|------:|------------------- |
| 합의 없음 | 466,963 | 41.7% | 투표가 갈려 손대지 않았다     |
| 이미 정상 | 458,686 | 41.0% | 배정이 원래 맞았다         |
| 재배정됨  | 186,592 | 16.7% | **틀렸고, 고쳤다**       |
| 대상 부재 |   6,878 |  0.6% | 목표 head가 그 프레임에 없다 |

## 4. 고쳐졌는지 어떻게 아나

배정이 맞으면 head는 자기 trigger volume 쪽을 향하므로 그 각도(**facing
error**)가 **80~100°**로 모인다. 한 칸 밀린 배정은 그 근처에 올 수 없다.
이 값은 복구 결과와 무관하게 측정되므로, 스스로를 채점하는 지표가 아니다.

| 구간   |       엔트리 |               정상 판정 |
|----- |----------:|--------------------:|
| 수정 전 | 1,119,119 |     646,025 (57.7%) |
| 수정 후 | 1,119,119 | **844,111 (75.4%)** |

**실제로 재배정된 186,592개만** 떼어놓고 보면 결정적이다.

|      | facing error 중앙값 |      정상 판정 |
|----- |-----------------:|-----------:|
| 수정 전 |            19.3° |       0.0% |
| 수정 후 |        **84.6°** | **100.0%** |

> 한 칸 밀린 배정이 만들던 각도에서 **물리적으로 옳은 90° 부근으로**
> 이동했다. 이 표본은 정의상 수정 전 정상 판정이 0%이므로, 이동은
> 전부 복구에서 나온 것이다.

![facing error 분포](facing_error_before_after.png)

위: 수정 전 — 옳은 구간(파란 띠) 밖에 큰 봉우리가 따로 있다.
아래: 수정 후 — 그 봉우리가 사라진다.

## 5. 손대지 않은 것은 문제인가

`합의 없음`이 466,963개로 크지만, 그 대부분은 **ego와
무관한 신호등**이다. 복구 규칙은 head가 교차로 건너편에 있다는 기하에
기대는데, T자 교차로나 신호등이 1~2개뿐인 곳에서는 그 조건으로 배정이
**유일하게 결정되지 않는다.** 그런 엔트리는 추측하지 않고 그대로 둔다.

전체 1,119,119개 중 **ego에 영향을 주는 것은 74,779개
(6.7%)** 뿐이다. 그 기준으로 다시 세면:

| 판정    | ego 영향 | 그중 각도 불일치 |
|------ |-------:|----------:|
| 이미 정상 | 36,872 |         0 |
| 합의 없음 | 23,245 | **8,983** |
| 재배정됨  | 14,662 |         0 |
| 대상 부재 |      0 |         0 |

> **재배정한 것 중 각도가 안 맞는 엔트리는 0개다.** 고친 것은 전부
> 고쳐졌다. 남은 것은 `합의 없음` 쪽의 **8,983개
> (전체의 0.80%)** 이고, 이것이 육안으로
> 확인할 실제 범위다.

그 엔트리들이 속한 교차로의 신호등 수:

| 교차로 신호등 수 |   엔트리 |
|---------- |------:|
| 1         | 4,745 |
| 3         | 3,631 |
| 2         |   607 |

3개 미만이면 배정을 결정할 정보 자체가 없다.

### 확인 대상 클립 50개

| 클립                                                                   | 구분   | ego 신호등 | 각도 불일치 |   비율 |
|--------------------------------------------------------------------- |----- |--------:|-------:|-----:|
| VanillaSignalizedTurnEncounterRedLight_Town05_Route254_Weather20     | base |     462 |    462 | 100% |
| BlockedIntersection_Town05_Route272_Weather12                        | base |     453 |    453 | 100% |
| EnterActorFlow_Town05_Route271_Weather11                             | base |     450 |    450 | 100% |
| TJunction_Town06_Route304_Weather18                                  | base |     444 |    444 | 100% |
| TJunction_Town06_Route306_Weather20                                  | base |     428 |    428 | 100% |
| TJunction_Town07_Route365_Weather1                                   | base |     424 |    424 | 100% |
| TJunction_Town01_Route90_Weather12                                   | base |     415 |    415 | 100% |
| NonSignalizedJunctionLeftTurn_Town05_Route239_Weather26              | base |     413 |    413 | 100% |
| CrossingBicycleFlow_Town12_Route1032_Weather18                       | base |     332 |    332 | 100% |
| CrossingBicycleFlow_Town12_Route1065_Weather25                       | base |     327 |    327 | 100% |
| VanillaSignalizedTurnEncounterRedLight_Town13_Route3865_Weather12    | weak |     326 |    326 | 100% |
| TJunction_Town13_Route691_Weather15                                  | base |     316 |    316 | 100% |
| TJunction_Town12_Route1017_Weather3                                  | base |     307 |    307 | 100% |
| TJunction_Town15_Route496_Weather2                                   | base |     305 |    305 | 100% |
| VanillaSignalizedTurnEncounterRedLight_Town12_Route15481_Weather14   | weak |     203 |    203 | 100% |
| BlockedIntersection_Town05_Route248_Weather14                        | base |     194 |    194 | 100% |
| VanillaSignalizedTurnEncounterRedLight_Town10HD_Route388_Weather23   | base |     179 |    179 | 100% |
| VanillaSignalizedTurnEncounterRedLight_Town10HD_Route393_Weather3    | base |     161 |    161 | 100% |
| VanillaSignalizedTurnEncounterGreenLight_Town10HD_Route387_Weather23 | base |     154 |    154 | 100% |
| VanillaSignalizedTurnEncounterGreenLight_Town10HD_Route392_Weather2  | base |     152 |    152 | 100% |

이하 30개는 `fix_by_clip.csv` 참조.

## 6. 재배정이 많았던 클립

| 클립                                                                | 구분   |   엔트리 |          재배정 |     정상 전→후 |
|------------------------------------------------------------------ |----- |------:|-------------:|-----------:|
| AccidentTwoWays_Town12_Route1104_Weather12                        | base | 1,060 | 1,060 (100%) |  0% → 100% |
| AccidentTwoWays_Town12_Route1456_Weather15                        | base | 1,048 | 1,048 (100%) |  0% → 100% |
| AccidentTwoWays_Town12_Route1469_Weather3                         | base | 1,064 | 1,064 (100%) |  0% → 100% |
| ConstructionObstacleTwoWays_Town12_Route1097_Weather5             | base | 1,084 | 1,084 (100%) |  0% → 100% |
| OppositeVehicleRunningRedLight_Town12_Route809_Weather3           | base |   632 |   632 (100%) |  0% → 100% |
| OppositeVehicleRunningRedLight_Town12_Route990_Weather2           | base |   588 |   588 (100%) |  0% → 100% |
| ParkingCrossingPedestrian_Town12_Route761_Weather7                | base |   174 |   174 (100%) | 33% → 100% |
| ParkingCutIn_Town12_Route1304_Weather9                            | base |   165 |   165 (100%) | 33% → 100% |
| ParkingCutIn_Town12_Route1314_Weather5                            | base |   165 |   165 (100%) | 33% → 100% |
| ParkingCutIn_Town12_Route765_Weather11                            | base |   162 |   162 (100%) | 33% → 100% |
| ParkingExit_Town12_Route787_Weather7                              | base |   168 |   168 (100%) | 33% → 100% |
| ParkingExit_Town12_Route923_Weather13                             | base |   168 |   168 (100%) | 33% → 100% |
| PedestrianCrossing_Town13_Route747_Weather19                      | base | 2,048 | 2,048 (100%) |  0% → 100% |
| SignalizedJunctionLeftTurnEnterFlow_Town13_Route720_Weather19     | base |   692 |   692 (100%) |  0% → 100% |
| VanillaSignalizedTurnEncounterGreenLight_Town13_Route641_Weather9 | base | 1,476 | 1,476 (100%) |  0% → 100% |

전체 클립별 수치는 `fix_by_clip.csv`.

재배정이 한 건도 없었던 클립 **484개** — 원래 배정이 맞았거나
신호등이 없는 구간이다.

## 7. 어디에 생기나

```
relabel/
├── <클립명>/traffic_light/
│   ├── corrected_anno/*.json.gz   수정된 annotation (원본은 그대로 둔다)
│   ├── completion.json            입출력 SHA256 · 프레임 수 · metrics
│   └── reports/
│       ├── affects_ego_changes.csv  바뀐 프레임·신호등
│       ├── relevance_events.csv     교차로 통과 이벤트
│       └── relevance_frames.csv     프레임별 판정
└── production_reports/{base,weak}/
    ├── bbox_details.csv          엔트리별 전/후 (이 리포트의 근거)
    ├── bbox_details_summary.csv  클립별 집계
    └── results.csv               클립별 상태
```

이 리포트는 `report_relabel_fixes.py`가 생성한다.
