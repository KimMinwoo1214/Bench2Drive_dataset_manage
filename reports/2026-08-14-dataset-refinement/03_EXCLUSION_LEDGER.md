# 제외 원장 — Bench2Drive 품질 게이트

부모 매니페스트 **1329** 클립 → 필터링 후 **1300** 클립. **29개 제외** (2.18%).

제외 사유는 하나뿐이다: **전문가 주행이 실제로 충돌했다.** 센서 결손이나
구조 손상으로 빠진 클립은 없다 (structural_fatal 0건).

## 1. 어디서 빠졌나

| 구분    |   부모 | 제외 |   잔존 |   제외율 |
|------ |-----:|---:|-----:|------:|
| base  | 1000 | 23 |  977 | 2.30% |
| weak  |  329 |  6 |  323 | 1.82% |
| train | 1262 | 25 | 1237 | 1.98% |
| val   |   67 |  4 |   63 | 5.97% |

`split` 규약: **부모의 train/val 소속을 그대로 유지하고, 제외된 클립만 뺀다.**
빠진 자리를 다른 클립으로 채우지 않는다 (`no_backfill`). 그래서 val 비율이
미세하게 움직이며, 이것이 의도된 동작이다.

## 2. 어떤 시나리오에서 빠졌나

`검토` = 접촉 증거가 사람에게 올라간 클립. `제외` = 육안 확인 후 충돌로 판정.

| 시나리오                                   |  전체 |  접촉 | 검토 | 제외 |   제외율 |
|--------------------------------------- |----:|----:|---:|---:|------:|
| LaneChange                             |  65 |  31 | 18 |  9 | 13.8% |
| CrossingBicycleFlow                    |  26 |   7 |  6 |  2 |  7.7% |
| MergerIntoSlowTraffic                  |   8 |   2 |  2 |  2 | 25.0% |
| NonSignalizedJunctionLeftTurnEnterFlow |  18 |   2 |  2 |  2 | 11.1% |
| YieldToEmergencyVehicle                |  98 |  58 |  9 |  2 |  2.0% |
| ConstructionObstacleTwoWays            |  27 |   3 |  1 |  1 |  3.7% |
| DynamicObjectCrossing                  |  26 |   5 |  2 |  1 |  3.8% |
| EnterActorFlow                         |  15 |   1 |  1 |  1 |  6.7% |
| HighwayExit                            |  29 |   1 |  1 |  1 |  3.4% |
| InterurbanActorFlow                    |  13 |   2 |  1 |  1 |  7.7% |
| InterurbanAdvancedActorFlow            |  15 |   4 |  2 |  1 |  6.7% |
| MergerIntoSlowTrafficV2                |  18 |   4 |  2 |  1 |  5.6% |
| NonSignalizedJunctionLeftTurn          |  34 |  10 |  5 |  1 |  2.9% |
| SignalizedJunctionLeftTurn             |  21 |   4 |  1 |  1 |  4.8% |
| SignalizedJunctionLeftTurnEnterFlow    |  27 |   4 |  4 |  1 |  3.7% |
| SignalizedJunctionRightTurn            |  15 |   2 |  2 |  1 |  6.7% |
| VehicleTurningRoutePedestrian          |  18 |   1 |  1 |  1 |  5.6% |
| **합계**                                 | 473 | 141 | 60 | 29 |       |

검토했으나 **한 건도 제외되지 않은** 시나리오 13종: `Accident`, `AccidentTwoWays`, `BlockedIntersection`, `ConstructionObstacle`, `HardBreakRoute`, `HazardAtSideLane`, `InvadingTurn`, `NonSignalizedJunctionRightTurn`, `ParkingCutIn`, `PedestrianCrossing`, `VanillaNonSignalizedTurnEncounterStopsign`, `VanillaSignalizedTurnEncounterRedLight`, `VehicleTurningRoute`

## 3. 제외된 클립 전체

`Δv` = 접촉 전후 ego 속도 변화 최대값. `잔여` = 접촉 종료 후 남은 프레임 수
(0이면 충돌과 함께 주행이 끝났다는 뜻).

| 클립                                                               | 구분   | 상대         | 관통(m) | ego(m/s) |   Δv |  잔여 | 사유           |
|----------------------------------------------------------------- |----- |----------- |------:|---------:|-----:|----:|------------- |
| ConstructionObstacleTwoWays_Town12_Route1080_Weather14           | base | vehicle    | 0.406 |      1.2 | 1.73 | 103 | VEHICLE      |
| CrossingBicycleFlow_Town12_Route1012_Weather23                   | base | bicycle    | 0.254 |      7.1 | 1.86 |  61 | VRU          |
| CrossingBicycleFlow_Town12_Route1067_Weather1                    | base | bicycle    | 0.062 |      7.8 | 2.57 |  63 | VRU          |
| DynamicObjectCrossing_Town02_Route13_Weather6                    | base | pedestrian | 0.020 |      4.1 | 2.06 | 115 | VRU          |
| EnterActorFlow_Town07_Route349_Weather11                         | base | vehicle    | 0.396 |      8.6 | 1.77 |  38 | VEHICLE      |
| HighwayExit_Town12_Route937_Weather1                             | base | vehicle    | 0.377 |      8.5 | 2.19 | 112 | VEHICLE      |
| InterurbanActorFlow_Town12_Route938_Weather2                     | base | vehicle    | 0.003 |      4.9 | 2.00 | 163 | VEHICLE      |
| InterurbanAdvancedActorFlow_Town13_Route735_Weather7             | base | vehicle    | 0.178 |     10.6 | 0.57 |  17 | SIDE_CONTACT |
| LaneChange_Town06_Route24277_Weather12                           | weak | vehicle    | 0.176 |     12.2 | 0.25 |  41 | SIDE_CONTACT |
| LaneChange_Town06_Route307_Weather21                             | base | vehicle    | 0.000 |     15.2 | 0.21 |   0 | SIDE_CONTACT |
| LaneChange_Town12_Route17604_Weather1                            | weak | vehicle    | 0.096 |     14.1 | 0.29 |   0 | VEHICLE      |
| LaneChange_Town12_Route17629_Weather0                            | weak | vehicle    | 0.173 |     10.5 | 0.40 |  38 | SIDE_CONTACT |
| LaneChange_Town12_Route17690_Weather9                            | weak | vehicle    | 0.054 |     13.3 | 0.36 |   1 | VEHICLE      |
| LaneChange_Town12_Route17707_Weather0                            | weak | vehicle    | 0.044 |     13.1 | 0.36 |   1 | SIDE_CONTACT |
| LaneChange_Town12_Route2427_Weather3                             | weak | vehicle    | 0.196 |     15.1 | 0.64 |   9 | SIDE_CONTACT |
| LaneChange_Town12_Route894_Weather10                             | base | vehicle    | 0.232 |     12.1 | 0.22 |  50 | SIDE_CONTACT |
| LaneChange_Town13_Route740_Weather0                              | base | vehicle    | 0.063 |     12.1 | 0.26 |   2 | SIDE_CONTACT |
| MergerIntoSlowTrafficV2_Town15_Route525_Weather5                 | base | vehicle    | 0.190 |      0.0 | 0.03 | 215 | VEHICLE      |
| MergerIntoSlowTraffic_Town12_Route1003_Weather8                  | base | vehicle    | 0.467 |      7.4 | 2.62 | 123 | VEHICLE      |
| MergerIntoSlowTraffic_Town12_Route1004_Weather8                  | base | vehicle    | 0.513 |      9.8 | 1.54 |  94 | VEHICLE      |
| NonSignalizedJunctionLeftTurnEnterFlow_Town12_Route949_Weather13 | base | vehicle    | 0.290 |      9.4 | 2.52 |   0 | VEHICLE      |
| NonSignalizedJunctionLeftTurnEnterFlow_Town13_Route661_Weather11 | base | vehicle    | 0.464 |      1.2 | 0.95 |  71 | VEHICLE      |
| NonSignalizedJunctionLeftTurn_Town07_Route342_Weather3           | base | vehicle    | 0.407 |      0.7 | 1.09 |   0 | VEHICLE      |
| SignalizedJunctionLeftTurnEnterFlow_Town13_Route737_Weather23    | base | vehicle    | 0.438 |      0.9 | 0.92 |  72 | VEHICLE      |
| SignalizedJunctionLeftTurn_Town12_Route799_Weather0              | base | vehicle    | 0.146 |      7.7 | 2.60 |  75 | SIDE_CONTACT |
| SignalizedJunctionRightTurn_Town03_Route151_Weather2             | base | vehicle    | 0.246 |      3.6 | 1.87 |  88 | SIDE_CONTACT |
| VehicleTurningRoutePedestrian_Town15_Route445_Weather11          | base | vehicle    | 0.125 |      5.9 | 0.43 |  76 | SIDE_CONTACT |
| YieldToEmergencyVehicle_Town04_Route207_Weather25                | base | vehicle    | 0.351 |      3.1 | 0.54 |  77 | VEHICLE      |
| YieldToEmergencyVehicle_Town05_Route225_Weather9                 | base | vehicle    | 0.232 |      2.8 | 0.38 | 219 | VEHICLE      |

## 4. 출처

- 검토 큐 **77** 클립 → 제외 **29** / 승인 **48**
- 검토자: kimminseong
- 부모 매니페스트 `448438d80b43886cd1d7d45730ee500033ae2c44e5b190ac9fcd5862f7433c65`
- 감사 지표 `876f268ec873c9e04eeafb36b89ae24b2f3f0e888f70df6a292ef45fd4e00642`
- 충돌 전수조사 `9ed823df851d7ad89b9fac7d10490c6b0a4e59858a86a7cd431b2f2910458f3c`
- 분류 완료 `beb705673c30d767a665adf5afeeb6904ec5eca97b9975fb82d99efdeaf25921`

이 파일은 `report_exclusions.py` 가 생성한다. 위 해시 중 하나라도 바뀌면
다시 만들어야 한다.
