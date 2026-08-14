# 충돌 검토 — 2026-08-14

전수조사 1,329클립 → **검토 대상 77클립** (Base 58 / Weak 19)
선정 로직은 `COLLISION_FILTERING_BRIEF.md` 참조.

---

## 보는 법

각 클립 폴더에 **4시점 × 2형식**이 있다. VS Code 이미지 뷰어가 바로 연다 (MP4는 원격에서 재생 안 됨).

| 시점 | 격자 (한눈에) | 애니메이션 |
|---|---|---|
| 위에서 | `REVIEW_top_sheet.jpg` | `REVIEW_top.gif` |
| 전방 | `REVIEW_front_sheet.jpg` | `REVIEW_front.gif` |
| 전방 좌 | `REVIEW_front_left_sheet.jpg` | `REVIEW_front_left.gif` |
| 전방 우 | `REVIEW_front_right_sheet.jpg` | `REVIEW_front_right.gif` |

격자는 접촉 전후 12프레임이고 **접촉 프레임은 빨간 테두리**다.

**순서**: `REVIEW_top_sheet.jpg` → 애매하면 `REVIEW_top.gif` → **측면 의심되면 좌/우 시점**.
측면 접촉은 전방 카메라에 안 보인다 (`LaneChange_Town12_Route17604`가 그 예).

판단 결과를 클립별로 `ACCEPT`(문제 없음) / `EXCLUDE`(충돌)로 표시해 주면 된다.

---

## A_충돌유력 (16개)

**사람·자전거와 겹쳤거나(12), 제동으로 불가능한 충격량(4).** 가장 확실하다.

| 클립 | 접촉 프레임 | 상대 | 근거 | ego Δv | 상대 Δv | 관통 m |
|---|---|---|---|---|---|---|
| `CrossingBicycleFlow_Town12_Route1078_Weather12` | 444/939 (중간) | bicycle | 상대만반응, 깊은관통 | 0.01 | 2.45 | 0.587 |
| `CrossingBicycleFlow_Town12_Route1062_Weather22` | 367/469 (중간) | bicycle | 상대만반응, 깊은관통 | 0.23 | 1.82 | 0.582 |
| `PedestrianCrossing_Town12_Route1033_Weather20` | 48/166 (중간) | pedestrian | 상대만반응, 깊은관통 | 2.20 | 12.23 | 0.357 |
| `VehicleTurningRoute_Town13_Route700_Weather23` | 201/231 (중간) | bicycle | 양측반응 | 0.45 | 2.33 | 0.290 |
| `BlockedIntersection_Town07_Route353_Weather15` | 190/192 (끝) | vehicle | 제동한계초과, 접촉후종료 | 4.78 | 0.01 | 0.269 |
| `CrossingBicycleFlow_Town12_Route1012_Weather23` | 364/427 (중간) | bicycle | 겹침만 | 1.86 | 0.09 | 0.254 |
| `PedestrianCrossing_Town12_Route864_Weather6` | 55/651 (중간) | pedestrian | 상대만반응 | 0.00 | 1.62 | 0.242 |
| `CrossingBicycleFlow_Town12_Route860_Weather2` | 360/440 (중간) | bicycle | 상대만반응 | 1.02 | 2.47 | 0.230 |
| `VehicleTurningRoute_Town15_Route1370_Weather7` | 118/154 (중간) | bicycle | 상대만반응 | 2.31 | 0.37 | 0.188 |
| `BlockedIntersection_Town07_Route352_Weather14` | 242/244 (끝) | vehicle | 제동한계초과, 양측반응, 접촉후종료 | 4.83 | 4.97 | 0.156 |
| `CrossingBicycleFlow_Town12_Route977_Weather15` | 357/361 (끝) | bicycle | 양측반응 | 1.73 | 1.63 | 0.119 |
| `InterurbanAdvancedActorFlow_Town06_Route331_Weather19` | 119/121 (끝) | vehicle | 제동한계초과, 양측반응, 접촉후종료 | 5.19 | 5.51 | 0.064 |
| `CrossingBicycleFlow_Town12_Route1067_Weather1` | 68/134 (중간) | bicycle | 겹침만 | 2.57 | 0.04 | 0.062 |
| `VanillaSignalizedTurnEncounterRedLight_Town10HD_Route24840_Weather26` | 143/145 (끝) | vehicle | 제동한계초과, 접촉후종료 | 7.79 | 0.00 | 0.056 |
| `DynamicObjectCrossing_Town02_Route13_Weather6` | 94/214 (중간) | pedestrian | 겹침만 | 2.06 | 0.00 | 0.020 |
| `DynamicObjectCrossing_Town13_Route24_Weather23` | 84/191 (중간) | pedestrian | 겹침만 | 2.63 | 0.58 | 0.011 |

## B_의심 (24개)

근거 코드가 1개 이상 잡힌 것. 육안 확인 필요.

| 클립 | 접촉 프레임 | 상대 | 근거 | ego Δv | 상대 Δv | 관통 m |
|---|---|---|---|---|---|---|
| `MergerIntoSlowTraffic_Town12_Route1004_Weather8` | 47/149 (중간) | vehicle | 양측반응, 깊은관통 | 1.54 | 0.39 | 0.513 |
| `MergerIntoSlowTraffic_Town12_Route1003_Weather8` | 38/168 (중간) | vehicle | 양측반응, 깊은관통 | 2.62 | 0.51 | 0.467 |
| `NonSignalizedJunctionLeftTurnEnterFlow_Town13_Route661_Weather11` | 109/185 (중간) | vehicle | 상대만반응, 깊은관통 | 0.95 | 3.64 | 0.464 |
| `SignalizedJunctionLeftTurnEnterFlow_Town13_Route737_Weather23` | 125/201 (중간) | vehicle | 상대만반응, 깊은관통 | 0.92 | 4.34 | 0.438 |
| `NonSignalizedJunctionLeftTurn_Town07_Route342_Weather3` | 99/100 (끝) | vehicle | 상대만반응, 접촉후종료, 깊은관통 | 1.09 | 6.44 | 0.407 |
| `ConstructionObstacleTwoWays_Town12_Route1080_Weather14` | 79/203 (중간) | vehicle | 깊은관통 | 1.73 | 2.65 | 0.406 |
| `EnterActorFlow_Town07_Route349_Weather11` | 76/126 (중간) | vehicle | 양측반응, 깊은관통 | 1.77 | 2.60 | 0.396 |
| `HighwayExit_Town12_Route937_Weather1` | 40/155 (중간) | vehicle | 깊은관통 | 2.19 | 0.00 | 0.377 |
| `YieldToEmergencyVehicle_Town04_Route207_Weather25` | 78/159 (중간) | vehicle | 깊은관통 | 0.54 | 2.56 | 0.351 |
| `LaneChange_Town13_Route726_Weather21` | 126/161 (중간) | vehicle | 깊은관통 | 0.25 | 2.39 | 0.312 |
| `NonSignalizedJunctionLeftTurnEnterFlow_Town12_Route949_Weather13` | 65/67 (끝) | vehicle | 양측반응, 접촉후종료 | 2.52 | 0.67 | 0.290 |
| `YieldToEmergencyVehicle_Town05_Route225_Weather9` | 121/342 (중간) | vehicle | 양측반응 | 0.38 | 1.99 | 0.232 |
| `MergerIntoSlowTrafficV2_Town15_Route525_Weather5` | 15/232 (중간) | vehicle | 상대만반응 | 0.03 | 12.24 | 0.190 |
| `LaneChange_Town12_Route17604_Weather1` | 206/207 (끝) | vehicle | 접촉후종료 | 0.29 | 0.00 | 0.096 |
| `ParkingCutIn_Town12_Route1304_Weather9` | 76/218 (중간) | vehicle | 상대만반응 | 2.61 | 0.39 | 0.083 |
| `SignalizedJunctionRightTurn_Town07_Route339_Weather1` | 171/174 (끝) | vehicle | 접촉후종료 | 0.85 | 0.50 | 0.083 |
| `SignalizedJunctionLeftTurnEnterFlow_Town13_Route659_Weather3` | 98/140 (중간) | vehicle | 양측반응 | 0.83 | 0.57 | 0.068 |
| `LaneChange_Town13_Route740_Weather0` | 151/161 (끝) | vehicle | 접촉후종료 | 0.26 | 0.57 | 0.063 |
| `LaneChange_Town12_Route17690_Weather9` | 174/176 (끝) | vehicle | 접촉후종료 | 0.36 | 0.00 | 0.054 |
| `SignalizedJunctionLeftTurnEnterFlow_Town13_Route1633_Weather12` | 98/145 (중간) | vehicle | 양측반응 | 0.70 | 2.09 | 0.046 |
| `LaneChange_Town12_Route17707_Weather0` | 199/201 (끝) | vehicle | 접촉후종료 | 0.36 | 0.75 | 0.044 |
| `LaneChange_Town12_Route17598_Weather21` | 155/161 (끝) | vehicle | 접촉후종료 | 0.27 | 0.14 | 0.012 |
| `InterurbanActorFlow_Town12_Route938_Weather2` | 66/231 (중간) | vehicle | 상대만반응 | 2.00 | 2.21 | 0.003 |
| `LaneChange_Town06_Route307_Weather21` | 132/133 (끝) | vehicle | 접촉후종료 | 0.21 | 0.91 | 0.000 |

## C_차량접촉 (37개)

이동 중 실제 겹침 0.10m 이상인데 **반응 신호가 없는** 것. 측면 스침은 여기밖에 안 잡히지만, 상당수는 단순 스침일 수 있다.

| 클립 | 접촉 프레임 | 상대 | 근거 | ego Δv | 상대 Δv | 관통 m |
|---|---|---|---|---|---|---|
| `HazardAtSideLane_Town12_Route1524_Weather21` | 136/204 (중간) | vehicle | 겹침만 | 0.65 | 0.00 | 0.290 |
| `SignalizedJunctionRightTurn_Town03_Route151_Weather2` | 123/215 (중간) | vehicle | 겹침만 | 1.87 | 0.01 | 0.246 |
| `NonSignalizedJunctionLeftTurn_Town12_Route930_Weather20` | 76/134 (중간) | vehicle | 겹침만 | 1.84 | 0.11 | 0.242 |
| `MergerIntoSlowTrafficV2_Town12_Route1060_Weather20` | 101/139 (중간) | vehicle | 겹침만 | 0.59 | 2.34 | 0.234 |
| `LaneChange_Town12_Route17569_Weather18` | 109/185 (중간) | vehicle | 겹침만 | 2.62 | 0.00 | 0.232 |
| `LaneChange_Town12_Route894_Weather10` | 139/192 (중간) | vehicle | 겹침만 | 0.22 | 2.68 | 0.232 |
| `YieldToEmergencyVehicle_Town12_Route20832_Weather5` | 65/148 (중간) | vehicle | 겹침만 | 0.68 | 2.74 | 0.230 |
| `LaneChange_Town06_Route24247_Weather5` | 84/137 (중간) | vehicle | 겹침만 | 0.69 | 1.53 | 0.224 |
| `ConstructionObstacle_Town12_Route78_Weather0` | 100/213 (중간) | vehicle | 겹침만 | 0.47 | 2.56 | 0.223 |
| `NonSignalizedJunctionLeftTurn_Town07_Route344_Weather6` | 88/139 (중간) | vehicle | 겹침만 | 0.57 | 0.28 | 0.214 |
| `LaneChange_Town13_Route664_Weather14` | 118/185 (중간) | vehicle | 겹침만 | 2.26 | 0.30 | 0.211 |
| `LaneChange_Town12_Route2427_Weather3` | 130/144 (중간) | vehicle | 겹침만 | 0.64 | 0.35 | 0.196 |
| `HardBreakRoute_Town03_Route38_Weather12` | 248/498 (중간) | vehicle | 겹침만 | 2.61 | 2.39 | 0.190 |
| `InterurbanAdvancedActorFlow_Town13_Route735_Weather7` | 132/152 (중간) | vehicle | 겹침만 | 0.57 | 0.00 | 0.178 |
| `SignalizedJunctionLeftTurnEnterFlow_Town13_Route1629_Weather6` | 91/152 (중간) | vehicle | 겹침만 | 0.74 | 0.00 | 0.177 |
| `LaneChange_Town06_Route24277_Weather12` | 97/141 (중간) | vehicle | 겹침만 | 0.25 | 2.77 | 0.176 |
| `LaneChange_Town12_Route17629_Weather0` | 91/138 (중간) | vehicle | 겹침만 | 0.40 | 0.84 | 0.173 |
| `YieldToEmergencyVehicle_Town12_Route20646_Weather1` | 71/505 (중간) | vehicle | 겹침만 | 0.78 | 0.82 | 0.168 |
| `LaneChange_Town12_Route17663_Weather8` | 294/317 (중간) | vehicle | 겹침만 | 0.25 | 1.38 | 0.167 |
| `Accident_Town13_Route552_Weather6` | 70/326 (중간) | vehicle | 겹침만 | 2.67 | 0.00 | 0.166 |
| `AccidentTwoWays_Town12_Route1106_Weather14` | 275/350 (중간) | vehicle | 겹침만 | 0.52 | 0.30 | 0.165 |
| `LaneChange_Town12_Route17614_Weather11` | 125/211 (중간) | vehicle | 겹침만 | 0.37 | 2.65 | 0.164 |
| `HazardAtSideLane_Town05_Route223_Weather15` | 107/206 (중간) | vehicle | 겹침만 | 1.70 | 0.80 | 0.162 |
| `NonSignalizedJunctionRightTurn_Town12_Route817_Weather11` | 117/146 (중간) | vehicle | 겹침만 | 0.53 | 0.00 | 0.162 |
| `LaneChange_Town05_Route24359_Weather1` | 89/161 (중간) | vehicle | 겹침만 | 0.50 | 2.16 | 0.159 |
| `NonSignalizedJunctionLeftTurn_Town12_Route1353_Weather3` | 374/441 (중간) | vehicle | 겹침만 | 0.80 | 0.01 | 0.147 |
| `SignalizedJunctionLeftTurn_Town12_Route799_Weather0` | 107/189 (중간) | vehicle | 겹침만 | 2.60 | 0.01 | 0.146 |
| `YieldToEmergencyVehicle_Town04_Route165_Weather7` | 87/149 (중간) | vehicle | 겹침만 | 0.64 | 0.78 | 0.143 |
| `LaneChange_Town12_Route17581_Weather3` | 74/121 (중간) | vehicle | 겹침만 | 0.26 | 2.19 | 0.141 |
| `VehicleTurningRoutePedestrian_Town15_Route445_Weather11` | 110/193 (중간) | vehicle | 겹침만 | 0.43 | 2.62 | 0.125 |
| `YieldToEmergencyVehicle_Town12_Route20791_Weather8` | 77/189 (중간) | vehicle | 겹침만 | 2.61 | 2.62 | 0.120 |
| `VanillaNonSignalizedTurnEncounterStopsign_Town15_Route535_Weather15` | 77/126 (중간) | vehicle | 겹침만 | 0.33 | 2.38 | 0.116 |
| `YieldToEmergencyVehicle_Town12_Route20740_Weather9` | 70/182 (중간) | vehicle | 겹침만 | 2.31 | 0.97 | 0.116 |
| `InvadingTurn_Town12_Route924_Weather14` | 91/168 (중간) | vehicle | 겹침만 | 2.35 | 0.62 | 0.114 |
| `YieldToEmergencyVehicle_Town12_Route2599_Weather19` | 127/231 (중간) | vehicle | 겹침만 | 0.77 | 0.75 | 0.112 |
| `NonSignalizedJunctionLeftTurn_Town12_Route813_Weather26` | 97/144 (중간) | vehicle | 겹침만 | 0.67 | 0.00 | 0.104 |
| `YieldToEmergencyVehicle_Town12_Route781_Weather8` | 137/239 (중간) | vehicle | 겹침만 | 0.38 | 0.74 | 0.101 |

## _대조군_충돌아님 (10개)

급제동·좌회전·차선변경 등 **충돌이 아닐 것으로 예상**되는 것. 이게 정상으로 보여야 위 기준이 맞다는 뜻이다.

| 클립 | 접촉 프레임 | 상대 | 근거 | ego Δv | 상대 Δv | 관통 m |
|---|---|---|---|---|---|---|
| `Accident_Town15_Route411_Weather21` | 84/202 (중간) | vehicle | 겹침만 | 0.28 | 1.14 | 0.193 |
| `VehicleOpensDoorTwoWays_Town12_Route1196_Weather0` | 200/278 (중간) | vehicle | 겹침만 | 1.79 | 2.62 | 0.187 |
| `YieldToEmergencyVehicle_Town12_Route1809_Weather9` | 74/209 (중간) | vehicle | 겹침만 | 1.64 | 0.59 | 0.181 |
| `YieldToEmergencyVehicle_Town12_Route2587_Weather7` | 76/197 (중간) | vehicle | 겹침만 | 2.63 | 2.63 | 0.070 |
| `LaneChange_Town13_Route3217_Weather14` | 121/165 (중간) | vehicle | 겹침만 | 0.51 | 0.00 | 0.068 |
| `HazardAtSideLane_Town12_Route1519_Weather8` | 125/229 (중간) | vehicle | 겹침만 | 1.43 | 0.34 | 0.065 |
| `HazardAtSideLane_Town13_Route558_Weather12` | 120/506 (중간) | vehicle | 겹침만 | 1.25 | 0.42 | 0.056 |
| `ParkedObstacleTwoWays_Town12_Route1159_Weather23` | 239/344 (중간) | vehicle | 겹침만 | 2.61 | 0.25 | 0.055 |
| `InvadingTurn_Town13_Route575_Weather3` | 135/187 (중간) | vehicle | 겹침만 | 2.62 | 1.35 | 0.034 |
| `InvadingTurn_Town12_Route796_Weather8` | 94/180 (중간) | vehicle | 겹침만 | 2.59 | 0.46 | 0.019 |

---

## 근거 코드

| 코드 | 조건 | 신뢰도 |
|---|---|---|
| 제동한계초과 | ego Δv ≥ 3.0 m/s (30 m/s²). 제동은 26에서 포화 | 가장 강함 |
| 양측반응 | ego와 상대가 함께 변함 | 강함 |
| 상대만반응 | ego는 그대로인데 상대가 튕김 | 중간 |
| 접촉후종료 | 접촉 2프레임 내 클립 종료 | 중간 |
| 깊은관통 | 관통 ≥ 0.30 m | 보조 |
| 겹침만 | 근거 코드 없이 겹침만 (A등급 VRU / C등급) | 종류로 판단 |
