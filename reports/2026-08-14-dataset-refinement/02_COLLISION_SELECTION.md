# Bench2Drive 충돌 클립 선별 — 로직과 결과

2026-08-14 · 대상 **1,329 클립** (Base 1,000 + Weak 329, 전수조사)
현재 상태: **육안 검토 완료 → 29클립 제외 확정 → 필터링 split 1,300 확정**

---

## 1. 왜 필요한가

학습 데이터에 **전문가가 사고를 낸 클립**이 섞이면 모델이 사고 주행을 정답으로
배운다. 그래서 신호등 라벨을 고치기 전에 "이 클립에서 ego가 실제로 충돌했는가"를
먼저 걸러야 한다.

문제는 **데이터에 충돌 정답이 없다는 것.** 클립에는
`anno / camera / expert_assessment / lidar / radar`뿐이고 CARLA 충돌 센서 기록이
없다. 즉 **annotation에서 추론해야 한다.**

---

## 2. 쓴 필드

전부 `anno/*.json.gz`에 프레임마다 기록된 값이다. 계산해서 만든 값이 아니다.

| 필드 | 위치 | 쓰임 |
|---|---|---|
| `bounding_boxes[].center/extent/rotation` | 프레임 | ego·상대의 3D 박스 (겹침 계산) |
| `bounding_boxes[].class/base_type/type_id` | 프레임 | 상대 종류 (vehicle/pedestrian/bicycle) |
| `bounding_boxes[].id` | 프레임 | 상대 추적 (프레임 간 동일 actor 연결) |
| `bounding_boxes[].speed` | 프레임 | **상대의** 속도 |
| `bounding_boxes[].rotation[2]` | 프레임 | **상대의** 방향 |
| `speed` | 최상위 | ego 속도 |
| `brake` / `steer` | 최상위 | ego **조작 입력** |
| 프레임 개수 | 클립 | 접촉 후 남은 프레임 수 |

**쓰지 않은 것**: 최상위 `x`/`y` (위치). 시속 43km 직진 중에도 매 프레임 1m 넘게
흔들려서 위치 미분값은 전부 노이즈다. 같은 구간 기록 속도는 매끄럽다.

---

## 3. 1단계 — 접촉 찾기

모든 프레임에서 ego 박스와 주변 actor 박스의 교차를 계산한다.

```
접촉 = (BEV 교차면적 > 0  AND  z축 겹침 > 0)          ← 실제 겹침
       OR (z축 겹침 > 0  AND  BEV 간격 ≤ 0.10 m)      ← 아슬아슬한 근접
```

- **회전 반영**: 축 정렬 박스가 아니라 차량 yaw를 적용한 사각형끼리 교차
- **z축 확인**: 고가도로 위아래로 지나가는 차가 위에서 보면 겹치는 것을 배제
- **간격 0.10m도 접촉**: 10Hz라 충돌이 두 샘플 사이에 끝나면 겹침이 안 잡힌다
- 대상은 `vehicle`, `pedestrian`, `bicycle` 셋뿐 (표지판·건물 제외)
- ego 중심 12m 밖은 계산 생략

연속 프레임은 상대 `id`별로 하나의 **접촉 런**으로 묶는다.

> 결과: **296건의 접촉 런 / 237 클립**

---

## 4. 2단계 — 진짜 충돌인가

### 핵심 원리

> **급제동은 ego만 변한다.
> 주차차량과 박스가 겹치는 건 둘 다 안 변한다.
> 충돌은 둘 다 변한다.**

접촉 런의 앞뒤 4프레임을 보고 **ego와 그 상대 각각**의 속도·방향 변화량을 잰다.

```
Δv    = max |speed(t+1) - speed(t)|          (구간 내 최대)
Δyaw  = max |rotation(t+1) - rotation(t)|
tail  = 클립 마지막 프레임 - 접촉 끝 프레임
```

한 프레임에 **15 m/s 넘는 변화는 계산에서 제외**한다. 물리적으로 불가능(150 m/s²)
하고, 실제로는 actor가 스폰되는 프레임에 최고속으로 기록되는 로그 결함이다.
(이걸 안 걸렀을 때 `YieldToEmergencyVehicle` 9개가 상대 Δv 25 m/s로 상위를 차지했다.)

### 근거 코드

| 코드 | 조건 | 뜻 |
|---|---|---|
| `EGO_IMPULSE_BEYOND_BRAKING` | ego Δv ≥ **3.0 m/s** (=30 m/s²) | 이 데이터 제동은 26 m/s²에서 포화. **브레이크로는 불가능** |
| `BOTH_BODIES_REACT` | ego와 상대 둘 다 Δv ≥ 1.0 또는 Δyaw ≥ 0.05 | 두 물체가 함께 튕김 |
| `ACTOR_ONLY_REACTION` | 상대만 반응 + 실제 겹침 | ego가 들이받음 |
| `EPISODE_ENDS_AT_CONTACT` | tail ≤ 2 프레임 | 충돌로 주행 종료. **측면 스침처럼 반응이 없는 충돌**을 잡음 |
| `DEEP_PENETRATION` | 관통 ≥ 0.30 m | 보조 |

전제: `max(ego 속도, 상대 속도) ≥ 1.0 m/s`. **정지 상태에서 충돌은 불가능**하고,
주차차량 옆에 서 있으면 박스만 겹친다(그런 런은 접촉 프레임 중앙값이 236개였다).

---

## 5. 3단계 — 등급 나누기

여기서 **상대 종류에 따라 기준을 다르게 적용한다.** 이게 핵심이다.

```python
if max(ego_speed, actor_speed) < 1.0:            → D. 정지 겹침 (충돌 아님)

elif 상대가 pedestrian/bicycle and 실제겹침 > 0:  → A. 보행자/자전거 충돌
elif EGO_IMPULSE_BEYOND_BRAKING:                 → A. 충격량
elif 근거 코드가 1개 이상:                         → B. 의심
elif 실제겹침 > 0 and ego ≥ 2.0 and 관통 ≥ 0.10:  → C. 차량 접촉
else:                                            → E. 경미 (스침)
```

### 왜 보행자·자전거는 반응을 안 따지는가

**사람을 치면 차가 안 느려진다.** 질량이 무시할 수준이라 ego Δv가 0에 가깝고,
보행자 쪽 기록도 신뢰할 수 없다. "두 물체가 함께 반응" 기준은 차량-차량엔 맞지만
**보행자·자전거엔 구조적으로 틀렸다.**

실제로 이 기준을 고치기 전 `DynamicObjectCrossing`은 접촉 5건이 **전부 "반응없음"**
으로 빠져 우선 목록에 0개였다.

> **움직이는 차가 사람·자전거 박스와 겹쳤다면, 그 자체가 충돌이다.**

### 왜 차량은 반응이 없어도 C 등급으로 보는가

**옆에서 긁히면 속도가 안 줄어든다.** 측면 스침은 어떤 반응 신호도 남기지 않는다.
그래서 이동 중 실제 겹침이 0.10m 이상이면 반응이 없어도 검토 대상에 넣는다.
(확인된 사례: `LaneChange_Town12_Route17604`, 시속 54km, ego Δv **0.00**,
영상으로는 명백한 측면 접촉)

---

## 6. 결과

| 등급 | 클립 | 근거 |
|---|---|---|
| **A. VRU 충돌** | **12** | 움직이는 차가 사람·자전거와 겹침 |
| **A. 충격량** | **4** | 제동으로 불가능한 Δv |
| **B. 의심** | **25** | 근거 코드 1개 이상 |
| **C. 차량 접촉** | **40** | 이동 중 겹침 0.10m↑, 반응 신호 없음 |
| **검토 합계** | **77** | 1,329의 **5.8%** (Base 58 / Weak 19) |
| E. 경미 | 153 | 스침 추정 |
| D. 정지 겹침 | 24 | 충돌 아님 |
| 접촉 없음 | 1,092 | — |

### 지정 시나리오 4종

| 시나리오 | 전체 | 접촉 | 검토 대상 |
|---|---|---|---|
| CrossingBicycleFlow | 26 | 7 | **6** |
| DynamicObjectCrossing | 26 | 5 | **2** |
| PedestrianCrossing | 20 | 2 | **2** |
| VanillaSignalizedTurnEncounterRedLight | 130 | 1 | **1** |

검토 77개의 계열 상위: `LaneChange` 18, `YieldToEmergencyVehicle` 9,
`CrossingBicycleFlow` 6, `NonSignalizedJunctionLeftTurn` 5.

**Weak(추가 수집분)도 전수조사 대상이었고**, 검토 77개 중 19개가 Weak이다.

---

## 7. 시도했다 버린 지표

| 지표 | 왜 못 쓰는가 |
|---|---|
| 관통 깊이 (단독) | CARLA는 강체라 찌그러지지 않고 튕긴다. 정지 런(0.20m)이 움직임 런(0.12m)보다 **더 깊었다** |
| 회전 속도 (원시) | 상위가 전부 좌회전 시나리오. 그냥 핸들 꺾은 것 |
| 가속도 (원시) | 전체 상위 10%가 이미 26 m/s². 급제동과 구분 불가 |
| 속도 감소 (원시) | `HardBreakRoute` 시나리오가 잡힘 |
| 위치 미분 | ego x/y가 직진 중에도 매 프레임 1m 흔들림. 전부 노이즈 |

---

## 8. 육안 검토 결과 — 최종 확정

검토자 `kimminseong`이 77클립의 4시점 영상을 전부 확인하고 **29개를 제외**로,
**48개를 승인**으로 판정했다. 자동 제외는 한 건도 없다 — **충돌 판정은 전부 사람이 했다.**

| | 부모 | 제외 | 잔존 | 제외율 |
|---|---:|---:|---:|---:|
| **전체** | **1,329** | **29** | **1,300** | **2.18%** |
| Base | 1,000 | 23 | 977 | 2.30% |
| Weak | 329 | 6 | 323 | 1.82% |
| train | 1,262 | 25 | 1,237 | 1.98% |
| val | 67 | 4 | 63 | 5.97% |

### split 규약

> **부모의 train/val 소속을 그대로 유지하고, 제외된 클립만 뺀다.
> 빠진 자리를 채우지 않는다** (`preserve_parent_membership_remove_excluded_no_backfill`).

백필하지 않으므로 val 비율이 5.04% → 4.85%로 미세하게 움직인다. 이는 의도된
동작이다. 백필을 하면 train에 있던 클립이 val로 넘어가 **평가 세트의 정의가
바뀌기 때문**이다.

### 제외가 집중된 시나리오

| 시나리오 | 전체 | 접촉 | 검토 | 제외 | 제외율 |
|---|---:|---:|---:|---:|---:|
| LaneChange | 65 | 31 | 18 | **9** | 13.8% |
| MergerIntoSlowTraffic | 8 | 2 | 2 | **2** | 25.0% |
| CrossingBicycleFlow | 26 | 7 | 6 | **2** | 7.7% |
| NonSignalizedJunctionLeftTurnEnterFlow | 18 | 2 | 2 | **2** | 11.1% |
| YieldToEmergencyVehicle | 98 | 58 | 9 | **2** | 2.0% |
| (나머지 12종 각 1개) | — | — | — | **12** | — |

`LaneChange`가 제외의 **31%(9/29)**를 차지한다. 차선 변경 중 측면 접촉이
가장 흔한 전문가 실패 유형이라는 뜻이고, 이는 **반응 신호가 남지 않아
등급 C 규칙이 없었다면 전부 놓쳤을** 유형이다.

검토했으나 **한 건도 제외되지 않은** 시나리오 13종: `Accident`, `AccidentTwoWays`,
`BlockedIntersection`, `ConstructionObstacle`, `HardBreakRoute`, `HazardAtSideLane`,
`InvadingTurn`, `NonSignalizedJunctionRightTurn`, `ParkingCutIn`, `PedestrianCrossing`,
`VanillaNonSignalizedTurnEncounterStopsign`, `VanillaSignalizedTurnEncounterRedLight`,
`VehicleTurningRoute`

> 전체 목록과 클립별 측정값: `classification_v1/ledger/EXCLUSION_LEDGER.md`
> · `excluded_clips.csv`

### 재현 경로

```
collision_sweep_v2/contacts.jsonl        296건 접촉 (전수조사)
  └ classify_quality.load_sweep()        → 검토 큐 77
      └ EXCLUDE_LIST.txt (검토자 기록)    → build_review_decisions.py
          └ review_decisions_v1.json     → classify_quality --sweep-dir --decisions
              └ filtered_train_val_split.json   1,237 / 63
```

각 단계는 앞 단계의 SHA256에 묶여 있다. sweep이나 감사를 다시 돌리면 결정
파일이 무효가 되고 분류가 거부된다.

---

## 9. 한 줄 요약

> 1,329개 전 프레임에서 회전 3D 박스 접촉 296건을 찾고, **"충돌은 두 물체를
> 함께 움직인다"**를 기본 기준으로 삼되 **사람·자전거는 겹침만으로, 차량 측면은
> 반응이 없어도** 잡히도록 등급을 나눠 **77개를 사람 앞에 올렸고, 육안 확인으로
> 29개(2.18%)를 학습 데이터에서 제외**했다.
