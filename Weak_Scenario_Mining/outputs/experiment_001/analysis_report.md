# Bench2Drive Native Result 분석

## 입력

- 입력 파일 수: 1
- 전체 route record 수: 10
- Runtime 재실행 대상: 1
- 취약 시나리오 수: 4

## Native 전체 지표

### `result.json`

- Driving Score: 62.7684
- Success Rate: 50.0
- Route Completion: 73.336
- Infraction Penalty: 0.8709999999999999
- Efficiency: 126.41513699690404
- Comfortness: 0.300047619047619
- Success: 5/10

## Runtime 재실행 대상

| Route ID | Scenario | Status | DS | RC |
|---|---|---|---:|---:|
| RouteScenario_24367_rep0 | ConstructionObstacle | Failed - TickRuntime | 21.684 | 33.360 |

## 취약 시나리오

| Rank | Scenario | Target Ability | Table 2 Ability | Valid routes | Mean DS | Mean RC | Success | Priority | 근거 |
|---:|---|---|---|---:|---:|---:|---:|---:|---|
| 1 | VanillaNonSignalizedTurnEncounterStopsign | - | Traffic Sign | 1 | 0.000 | 0.000 | 0.000 | 8.050 | blocked=1; mean_DS=0.000<50; mean_RC=0.000<80; success_rate=0.000<1 |
| 2 | VanillaSignalizedTurnEncounterRedLight | - | Traffic Sign | 1 | 0.000 | 0.000 | 0.000 | 8.050 | blocked=1; mean_DS=0.000<50; mean_RC=0.000<80; success_rate=0.000<1 |
| 3 | LaneChange | - | Merging | 1 | 36.000 | 100.000 | 0.000 | 7.660 | collision=2; mean_DS=36.000<50; success_rate=0.000<1 |
| 4 | YieldToEmergencyVehicle | - | Give Way | 1 | 70.000 | 100.000 | 0.000 | 4.550 | yield_violation=1; success_rate=0.000<1 |

## Table 2 다중 Ability 집계

| Ability | Total | Valid | Runtime | Mean DS | Mean RC | Success |
|---|---:|---:|---:|---:|---:|---:|
| Merging | 2 | 2 | 0 | 68.000 | 100.000 | 0.500 |
| Overtaking | 2 | 1 | 1 | 100.000 | 100.000 | 1.000 |
| Emergency Brake | 2 | 2 | 0 | 100.000 | 100.000 | 1.000 |
| Give Way | 2 | 2 | 0 | 85.000 | 100.000 | 0.500 |
| Traffic Sign | 3 | 3 | 0 | 33.333 | 33.333 | 0.333 |
