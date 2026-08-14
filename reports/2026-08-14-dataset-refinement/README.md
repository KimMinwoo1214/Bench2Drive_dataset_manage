# Bench2Drive 데이터셋 정제 — 2026-08-14

Base 1,000 + Weak 329 = **1,329 클립 / 324,566 프레임**
릴리스 `production_1329_corrected_v1`

> **이 폴더만 있으면 서버 없이 발표 자료를 만들 수 있다.**
> 모든 수치는 `metrics.json` 에, 표는 `tables/` 에, 그림은 `figures/` 에 있다.

---

## 한 장 요약

**전문가가 사고를 낸 클립 29개를 걷어내고, 신호등 annotation 의 수집 단계 버그를
기하 규칙으로 복구해 배정 정상률을 57.7% → 75.4% 로 올렸다. 어느 신호등이 차를
통제하는지도 다시 계산해 17,196 엔트리를 고쳤고, 확신하지 못한 8,983 엔트리는
고치는 대신 학습에서 빼도록 마스킹했다.**

| | 값 |
|---|---:|
| 대상 클립 | 1,329 (base 1,000 / weak 329) |
| annotation 프레임 | 324,566 |
| 신호등 엔트리 | 1,119,119 |
| **충돌로 제외** | **29 (2.18%)** |
| **학습용 클립** | **1,300** (train 1,237 / val 63) |
| **bbox 재배정** | **186,592 (16.7%)** |
| **배정 정상률** | **57.7% → 75.4%** |
| **`affects_ego` 수정** | **17,196** (false→true 17,098 / true→false 98) |
| 판단 보류 | 83 클립 / 7,546 프레임 (2.3%) |
| UV 마스킹 | 8,983 (UV 감독 대상의 **12.0%**) |

---

## 서사와 문서

| # | 문서 | 무엇을 답하나 |
|---|---|---|
| 01 | [PIPELINE_REPORT](01_PIPELINE_REPORT.md) | **전체 서술.** 문제 → 로직 → 결과 → 검증 → 한계 |
| 02 | [COLLISION_SELECTION](02_COLLISION_SELECTION.md) | **선별**: 충돌을 어떻게 판정했나. 수식과 등급 규칙 |
| 03 | [EXCLUSION_LEDGER](03_EXCLUSION_LEDGER.md) | **제외**: 29개가 어디서 왜 빠졌나. 클립별 측정값 |
| 04 | [RELABEL_FIX_LEDGER](04_RELABEL_FIX_LEDGER.md) | **처리**: 신호등 배정을 어떻게 고쳤나 |
| 05 | [REVIEW_DECISION](05_REVIEW_DECISION.md) | **판단**: 보류 83개를 왜 승인했나, 무엇을 포기했나 |
| 06 | `06_PKL.md` | **반영**: PKL 에 어떻게 들어갔나 *(PKL 생성 후 추가)* |

`ops/` 에는 진행 중 남긴 운영 기록이 있다(작업 계획, 중간 감사, 단계별 리포트).

---

## 수치 — `metrics.json`

모든 카운트가 **총계와 비율을 함께** 담고 있어 퍼센트를 다시 계산할 필요가 없다.

```
dataset         클립·프레임·부모 split
collision       접촉 296건/237클립, 판정 분포, 시나리오 계열별
exclusion       제외 29, 컴포넌트별·split별·계열별 비율, 검토자
split           1,237/63, val 비율 5.04% → 4.85%, no_backfill 규약
relabel         재배정·정상률·action 분포·affects_ego 변경 방향
review_reasons  보류 83의 사유 4종과 프레임 수
stop_and_go     정차→출발 전수 검증 (original vs corrected)
provenance      모든 입력의 sha256, 두 repo 의 커밋
```

## 표 — `tables/`

| 파일 | 내용 |
|---|---|
| `funnel.csv` | **1,329 → 1,300 단계별 깔때기.** 슬라이드 한 장으로 쓸 것 |
| `by_scenario.csv` | 시나리오 계열별 접촉·검토·제외 |
| `excluded_clips.csv` | 제외 29개의 클립별 관통·속도·잔여 프레임 |
| `relabel_by_clip.csv` | 클립별 재배정 수와 정상률 |
| `review_queue.csv` | 보류 83개와 사유·예시 프레임 |
| `stop_and_go_*.json` | 정차→출발 사건 전건 (712건) |

### 깔때기

| 단계 | 클립 | 엔트리/프레임 | 비고 |
|---|---:|---:|---|
| 수집 대상 | 1,329 | 324,566 프레임 | base 1,000 + weak 329 |
| 접촉 검출 | 237 | 296 런 | 전 프레임 회전 3D 박스 교차 |
| 사람 검토 대상 | 77 | | 등급 A/B/C |
| **충돌 제외** | **29** | | 육안 확인, 2.18% |
| **필터링 후** | **1,300** | | train 1,237 / val 63 |
| bbox 재배정 | | 186,592 | 정상률 57.7% → 75.4% |
| `affects_ego` 수정 | | 17,196 | false→true 17,098 |
| 판단 보류 | 83 | 7,546 프레임 | 원본 유지 (2.3%) |
| UV 마스킹 | 50 | 8,983 | UV 감독 대상의 12.0% |

---

## 그림 — `figures/`

48장. 각 묶음이 하나의 주장을 뒷받침한다.

| 폴더 | 장수 | 무엇을 보여주나 |
|---|---:|---|
| `01_collision/` | 32 | **육안으로 확인된 충돌 4건.** 4시점 격자 + GIF. `LaneChange_Route17604` 는 감속이 전혀 없어(Δv 0.00) 반응 기반 탐지로는 못 잡는 측면 접촉 |
| `02_bbox_repair/` | 9 | **배정 복구 전/후.** 위=원본 / 아래=수정본, 같은 프레임. 검토자가 "정면 신호등에 박스가 안 붙는다"고 지적해 잔차 게이트 결함을 찾아낸 클립 |
| `03_affects_ego/` | 12 | **`affects_ego` 가 실제로 바뀐 프레임.** 시나리오 계열이 겹치지 않게 선정. `UNAFFECTED` → `AFFECTS` 로 켜지는 것이 보인다 |
| `04_withheld/` | 2 | **왜 보류했나.** T자 교차로 — 신호등이 3개 미만이라 "head 는 건너편에 있다" 규칙으로 배정이 결정되지 않는다 |
| `05_charts/` | 1 | **facing error 전/후 히스토그램.** 수정 전 18° 봉우리(=한 칸 밀린 배정의 지문)가 수정 후 사라진다 |

> 비교 이미지는 전부 **위 = 원본 anno / 아래 = 수정본 anno, 같은 프레임** 이다.

---

## 검증이 자기채점이 아닌 이유

두 지표 모두 **복구 로직이 건드리지 않는 값**으로 잰다.

**① facing error** — 신호등 head 는 자기 trigger volume 의 건너편에 매달려 있으므로
그 각도가 물리적으로 90° 부근이어야 한다. 한 칸 밀린 배정은 거기 올 수 없다(≈18°).

| | 값 |
|---|---:|
| 재배정한 엔트리 | 186,592 |
| **그중 수정 후에도 대역 밖** | **0** |

**② 정차 후 재출발** — 신호에 서 있다가 출발하는 순간 그 신호등은 초록이어야
한다. relabel 은 **ego 속도를 전혀 읽지 않으므로** 이 일치는 외부 증거다.
표본이 아니라 **1,300 클립 전수**로 쟀다.

| 정지선 40m 이내 | original | corrected |
|---|---:|---:|
| 정차→출발 사건 | 209 | **236 (+27)** |
| 출발 시 GREEN | 201 | **227** |
| **일치율** | **96.2%** | **96.2%** |

일치율을 유지한 채 **진짜 신호 대기 사건을 27건(+13%) 더 찾아냈다.**

---

## 한계 — 못 고친 것

배정을 끝내 확정하지 못한 것은 **구조적 한계**다. 복구 규칙이 "head 는 교차로
건너편에 있다" 인데, 그 조건으로 배정이 유일하게 정해지려면 교차로에 신호등이
충분해야 한다.

| 미확정 엔트리가 속한 교차로 | 엔트리 |
|---|---:|
| 신호등 1개 | 4,745 |
| 신호등 2개 | 607 |
| 신호등 3개인데 기하가 안 맞음 | 3,631 |

임계값 문제가 아니다. 잔차 게이트를 걷어냈을 때 재배정이 22,731 늘었지만 보류
클립은 85 → 83 으로 2개만 줄었다.

**대응**: 그 신호등의 `center` 는 다른 head 를 가리키므로 거기서 계산되는
`ego_tl_uv` 는 확실히 틀린 좌표다. PKL 생성 시 그 좌표를 비워 `valid_uv=0` 이
되게 했다 — 손실에서 UV 회귀만 빠지고 **신호 색과 정지선 감독은 유지**된다.
클립 손실은 0 이다. 자세한 근거는 [05_REVIEW_DECISION](05_REVIEW_DECISION.md).

---

## 재현

```
collision_sweep_v2/contacts.jsonl          전수조사 접촉 296건
  └ classify_quality.load_sweep()          → 검토 큐 77
      └ EXCLUDE_LIST.txt                   검토자가 적은 Route 번호
          └ build_review_decisions.py      → review_decisions_v1.json
              └ classify_quality --sweep-dir --decisions
                  └ filtered_train_val_split.json     1,237 / 63

run_quality_gated_pipeline.py --stage relabel-{base,weak}
  └ fix_tl_bbox_permutation.py             bbox 치환 복구 (전역 합의)
      └ traffic_light_relevance.py         affects_ego 재계산
          └ relabel/<클립>/traffic_light/corrected_anno/

검증
  report_relabel_fixes.py   원장 + facing error 히스토그램
  verify_stop_and_go.py     정차-재출발 전수 지표
  render_relabel_check.py   전/후 대조 이미지
  collect_report_metrics.py 이 폴더의 metrics.json + tables/
```

각 단계는 앞 단계의 SHA256 에 묶여 있다. 감사나 전수조사를 다시 돌리면 결정
파일이 자동으로 무효가 되고 분류가 거부되므로, 낡은 판정이 조용히 통과할 수 없다.
