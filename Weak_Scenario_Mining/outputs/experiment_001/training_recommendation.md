# Bench2Drive 취약 시나리오 학습 데이터 추천

## 결론

- 취약 시나리오 수: 4개
- 추가 추천 데이터: 329개
- Base replay 후보: 500개
- 현재 설정 비율: Base 40.0% / New 60.0%
- 추천 비율: Base 30.0% / New 70.0%
- 추천 이유: 충돌/교착 등 고위험 취약 시나리오가 여러 개라 targeted new 비중을 높임

## 가장 먼저 넣을 시나리오

| Rank | Scenario | 추가 권장 개수 | 후보 수 | 우선순위 | 주요 문제 | 추천 방향 | 부족하면 대체 |
|---:|---|---:|---:|---:|---|---|---|
| 1 | VanillaNonSignalizedTurnEncounterStopsign | 100 | 246 | 8.050 | blocked=1, mean_DS=0.000<50, mean_RC=0.000<80, success_rate=0.000<1 | 정체/교착 대응 샘플을 같은 Town 조건 중심으로 추가; route completion이 낮은 조건의 완주 샘플을 우선 추가; 전체 driving quality가 낮아 같은 scenario의 다양성 확보; 실패 route와 유사한 Town/Weather를 일정 비율 포함 | Traffic Sign 계열의 신호등/정지선 scenario |
| 2 | VanillaSignalizedTurnEncounterRedLight | 100 | 833 | 8.050 | blocked=1, mean_DS=0.000<50, mean_RC=0.000<80, success_rate=0.000<1 | 정체/교착 대응 샘플을 같은 Town 조건 중심으로 추가; route completion이 낮은 조건의 완주 샘플을 우선 추가; 전체 driving quality가 낮아 같은 scenario의 다양성 확보; 실패 route와 유사한 Town/Weather를 일정 비율 포함 | Traffic Sign 계열의 신호등/정지선 scenario |
| 3 | LaneChange | 50 | 50 | 7.660 | collision=2, mean_DS=36.000<50, success_rate=0.000<1 | 충돌이 직접 원인이므로 동일 scenario를 최우선으로 보강; 전체 driving quality가 낮아 같은 scenario의 다양성 확보; 실패 route와 유사한 Town/Weather를 일정 비율 포함 | Merging 계열의 인접 합류/차선 변경 scenario |
| 4 | YieldToEmergencyVehicle | 79 | 79 | 4.550 | yield_violation=1, success_rate=0.000<1 | 양보 판단이 필요한 교차/합류 상황을 더 넣기; 실패 route와 유사한 Town/Weather를 일정 비율 포함 | Give Way 계열의 양보/교차로 scenario |

## 시나리오별 세부 추천

### 1. VanillaNonSignalizedTurnEncounterStopsign

- 추가 권장 개수: 100개
- 요청 quota: 100개
- Base 제외 후 후보 수: 246개
- 평균 DS/RC/성공률: 0.0/0.0/0.0%
- 문제 근거: blocked=1, mean_DS=0.000<50, mean_RC=0.000<80, success_rate=0.000<1
- 우선 Town: Town12
- 우선 Weather: 18
- 추천: 정체/교착 대응 샘플을 같은 Town 조건 중심으로 추가; route completion이 낮은 조건의 완주 샘플을 우선 추가; 전체 driving quality가 낮아 같은 scenario의 다양성 확보; 실패 route와 유사한 Town/Weather를 일정 비율 포함

### 2. VanillaSignalizedTurnEncounterRedLight

- 추가 권장 개수: 100개
- 요청 quota: 100개
- Base 제외 후 후보 수: 833개
- 평균 DS/RC/성공률: 0.0/0.0/0.0%
- 문제 근거: blocked=1, mean_DS=0.000<50, mean_RC=0.000<80, success_rate=0.000<1
- 우선 Town: Town12
- 우선 Weather: 18
- 추천: 정체/교착 대응 샘플을 같은 Town 조건 중심으로 추가; route completion이 낮은 조건의 완주 샘플을 우선 추가; 전체 driving quality가 낮아 같은 scenario의 다양성 확보; 실패 route와 유사한 Town/Weather를 일정 비율 포함

### 3. LaneChange

- 추가 권장 개수: 50개
- 요청 quota: 100개
- Base 제외 후 후보 수: 50개
- 평균 DS/RC/성공률: 36.0/100.0/0.0%
- 문제 근거: collision=2, mean_DS=36.000<50, success_rate=0.000<1
- 우선 Town: Town12
- 우선 Weather: 18
- 추천: 충돌이 직접 원인이므로 동일 scenario를 최우선으로 보강; 전체 driving quality가 낮아 같은 scenario의 다양성 확보; 실패 route와 유사한 Town/Weather를 일정 비율 포함
- 후보가 부족하면: 같은 scenario를 우선 추가 확보하고, 어렵다면 Merging 계열의 인접 합류/차선 변경 scenario를 보강

### 4. YieldToEmergencyVehicle

- 추가 권장 개수: 79개
- 요청 quota: 100개
- Base 제외 후 후보 수: 79개
- 평균 DS/RC/성공률: 70.0/100.0/0.0%
- 문제 근거: yield_violation=1, success_rate=0.000<1
- 우선 Town: Town13
- 우선 Weather: 14
- 추천: 양보 판단이 필요한 교차/합류 상황을 더 넣기; 실패 route와 유사한 Town/Weather를 일정 비율 포함
- 후보가 부족하면: 같은 scenario를 우선 추가 확보하고, 어렵다면 Give Way 계열의 양보/교차로 scenario를 보강

## 이상 징후와 보수적 해석

- Runtime 재실행이 필요한 시나리오가 1개 있습니다. `runtime_rerun_routes.csv` route를 재실행한 뒤 최종 result.json으로 다시 `run-all` 하는 것을 권장합니다.
- VanillaNonSignalizedTurnEncounterStopsign: valid route가 1개라 표본이 적음
- VanillaSignalizedTurnEncounterRedLight: valid route가 1개라 표본이 적음
- LaneChange: valid route가 1개라 표본이 적음
- YieldToEmergencyVehicle: valid route가 1개라 표본이 적음
- LaneChange: 요청 100개 중 50개만 선택 가능
- YieldToEmergencyVehicle: 요청 100개 중 79개만 선택 가능

## 학습 비율 추천

- 1차 fine-tuning은 Base 30.0% / Targeted new 70.0%를 권장합니다.
- 충돌, blocked, timeout이 많은 시나리오가 개선되지 않으면 Targeted new를 70%까지 올려 한 번 더 실험합니다.
- 전체 DS가 개선되지만 기존 강점 scenario가 흔들리면 Base replay를 50%로 올립니다.
- `train_files_mixed.txt`에는 oversampling 때문에 같은 파일이 여러 번 나올 수 있으며 정상입니다.

## 생성된 파일

- `analysis_report.md`: 평가 결과 요약
- `weak_scenarios.json`: 취약 시나리오 원본 목록
- `selected_additional_files.txt`: 실제 추가 학습 파일 목록
- `train_files_mixed.txt`: Base replay와 targeted new가 섞인 최종 학습 목록
- `training_plan_summary.json`: 혼합 비율 요약

## 현재 혼합 결과

- Combined unique: 829
- Mixed list length: 1250
- 실제 Base fraction: 40.0%
