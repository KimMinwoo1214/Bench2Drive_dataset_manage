# B2D → VAD 데이터 파트 기획서

> 담당: 김민성 / 김민우
> 기간: 2026-08-04 ~ 2026-08-28 (약 3주 반)
> 대상 데이터: Bench2Drive **Base** (1,000 clips, 약 20만 프레임)
> 대상 모델: [hustvl/VAD](https://github.com/hustvl/VAD) 를 CARLA에 이식한 [Bench2DriveZoo (`uniad/vad` 브랜치)](https://github.com/Thinklab-SJTU/Bench2DriveZoo/tree/uniad/vad)

---

## 0. 이 문서를 읽는 법

- **1장**: VAD가 데이터를 어떻게 먹는지 (파이프라인 이해 — 이걸 알아야 "무엇을 검증할지"가 정해짐)
- **2장**: 그래서 우리가 지켜야 할 "계약(contract)" 목록
- **3장**: 검증·큐레이션 작업을 7개 레이어로 분해
- **4장**: 3주 반 주차별 로드맵 (내일 당장 할 일 포함)
- **5장**: 읽을 논문 / 뜯어볼 코드
- **6장**: 산출물 목록과 평가 지표
- **부록 A**: 용어 사전 / **부록 B**: DAgger에 대한 솔직한 판단

---

## 1. VAD는 데이터를 어떻게 먹는가

### 1.1 전체 흐름 한 장 요약

```
[원본 B2D 클립]
  scenario_name/Town{id}_weather{id}_route{id}/
    ├── anno/00000.json.gz        ← 모든 라벨의 원천 (CARLA API 덤프)
    ├── camera/rgb_front/00000.jpg (6방향 + top_down)
    ├── camera/{depth,semantic,instance}_*/00000.png
    ├── lidar/00000.laz
    ├── radar/00000.h5
    └── expert_assessment/00000.npz  ← RL 전문가(Think2Drive)의 내부 값
        +  maps/Town**_HD_map.npz    ← 벡터 HD맵 (타운 단위 별도 파일)
        ↓
[1단계] 오프라인 변환기 (prepare_B2D.py)
        ── 좌표계 통일, 과거/미래 궤적 계산, 맵 요소 잘라내기
        ↓
  data/infos/b2d_infos_train.pkl / b2d_infos_val.pkl / b2d_map_infos.pkl
        ↓
[2단계] Dataset 클래스 (B2D_VAD_Dataset.get_data_info)
        ── pkl 한 줄 → 샘플 dict (이미지 경로, lidar2img, GT 텐서들)
        ↓
[3단계] mmdet3d 전처리 파이프라인
        LoadMultiViewImageFromFiles → PhotoMetricDistortion
        → NormalizeMultiviewImage → PadMultiViewImage → CustomCollect3D
        ↓
[4단계] VAD 모델 forward
        ResNet50 (img_backbone) → FPN (img_neck)
        → BEVFormerEncoder ─ BEV 쿼리 200×200에 6뷰 특징을 투영해 모음
        → Det/Map/Motion decoder → Planning head
```

> **핵심**: 우리가 넘겨줘야 하는 최종 산출물은 이미지 파일 그 자체가 아니라
> **`b2d_infos_*.pkl` (프레임 인덱스 + 라벨 + 변환행렬 묶음)** 입니다.
> "데이터를 잘 검증해서 뒷단에 넘긴다" = **믿을 수 있는 pkl(+ 프레임 선별 매니페스트)을 만든다.**

### 1.2 `prepare_B2D.py`가 실제로 하는 일 (2단계 이해가 핵심)

이 스크립트가 우리 업무의 90%가 걸려 있는 지점입니다. 하는 일은 4가지:

| 하는 일 | 왜 어려운가 |
|---|---|
| **① 좌표계 변환** | CARLA 월드 좌표계(왼손, z-up)와 nuScenes/LiDAR 좌표계가 다름. `world2ego`, `world2lidar`, `cam2ego`, `intrinsic`을 조합해 `lidar2img` 4×4 행렬 6개를 만들어야 함 |
| **② 시간 윈도우 구성** | B2D는 10Hz 전 프레임 라벨, nuScenes는 2Hz 키프레임. VAD 재현을 위해 **궤적 점 간격 0.5초, 윈도우 이동 0.1초**로 맞춤 → 과거 2점, 미래 6점(3초) |
| **③ 맵 요소 추출** | 타운 단위 HD맵(`Town**_HD_map.npz`)에서 ego 주변 (예: 30m×60m) 안의 차선을 잘라 20점 폴리라인으로 리샘플 |
| **④ 클래스/커맨드 매핑** | nuScenes 10클래스 → B2D **9클래스**, nuScenes 3커맨드(좌/우/직진) → CARLA **6커맨드** |

### 1.3 모델이 실제로 받는 딕셔너리 (= 우리가 지켜야 할 "계약")

`CustomCollect3D` 이후 VAD가 받는 키들. 이 표가 **스키마 명세서의 뼈대**가 됩니다.

**입력(센서/메타)**

| 키 | 모양 | 의미 |
|---|---|---|
| `img` | `[B, T_queue, 6, 3, H, W]` | 6개 카메라. 정규화 후 32의 배수로 패딩 |
| `img_metas.lidar2img` | `6 × 4×4` | LiDAR 좌표 → 이미지 픽셀 투영행렬. **BEV 인코더가 3D 점을 어느 픽셀에서 뽑을지 결정** — 여기 틀리면 전부 무너짐 |
| `img_metas.can_bus` | `18` | ego의 이전 프레임 대비 이동/회전량, 가속도, 각속도, 속도, 방위각. BEVFormer의 시간축 정렬에 사용 |
| `img_metas.scene_token` | str | 같은 클립인지 판별. 클립이 바뀌면 `prev_bev`(이전 BEV 기억)를 리셋 |

**정답(GT)**

| 키 | 모양 | 의미 |
|---|---|---|
| `gt_bboxes_3d` | `[N, 9]` | 3D 박스 (x,y,z,w,l,h,yaw,vx,vy) |
| `gt_labels_3d` | `[N]` | 9개 클래스 |
| `gt_attr_labels` | `[N, D]` | 주변 차량의 **미래 궤적(6스텝×2) + 유효 마스크 + 요-각 등**이 한 벡터로 이어붙어 있음 (motion 브랜치용). 정확한 `D`는 config에서 직접 확인할 것 |
| `map_gt_bboxes_3d` | `[M, 20, 2]` | 벡터 맵: divider / ped_crossing / boundary 3종, 각 20점 (VAD-Base는 맵 인스턴스 100개 슬롯) |
| `ego_his_trajs` | `[2, 2]` | ego 과거 궤적 |
| `ego_fut_trajs` | `[6, 2]` | **ego 미래 3초 궤적 = 플래닝 정답** |
| `ego_fut_masks` | `[6]` | 미래 점 유효 여부 (클립 끝부분은 미래가 없음) |
| `ego_fut_cmd` | `[6]` one-hot | CARLA 고수준 명령 |
| `ego_lcf_feat` | `[9]` | 위치·각속도·속도·가속도·조향·차량 크기 |

> **한 줄 정리**: 우리가 검증해야 할 최종 대상은 이 표의 값들이
> ① 존재하고 ② 타입/모양이 맞고 ③ 물리적으로 말이 되고 ④ 원본 anno와 일치하는가.

### 1.4 알려진 지뢰밭 (B2D 공식 문서가 인정한 버그들)

이건 그냥 그대로 **검증 규칙 목록**이 됩니다:

1. **보행자 speed가 전부 0** → 연속 프레임 위치 차분으로 직접 계산해야 함
2. **Speedometer / yaw가 None으로 나올 수 있음** → 0으로 대체 처리 필요
3. **정지 차량(`state=="static"`)의 rotation/location이 틀림** → `center` + `extent` + `inv(world2vehicle)`만 사용
4. **`extent`는 절반 크기(half-size)** → 실수로 2배 크기 박스 만들기 쉬움
5. **트리거 볼륨의 rotation은 부모 액터 기준 상대값** → 부모 회전을 더해야 절대 회전
6. **일부 정지 표지판은 바운딩박스가 없음** (바닥에 그려짐) → 트리거 볼륨으로만 존재
7. **JPEG 품질 20 손실 압축** → 추론 시에도 동일하게 압축해야 train-test 갭이 안 생김
8. **카메라 투영 버그**가 초기 UniAD/VAD team_code에 있었고 이후 수정됨 → 우리 브랜치가 수정본인지 확인 필요

---

## 2. 우리 파트의 정의 (간트 3줄 대응)

| 간트 항목 | 실제로 만드는 것 |
|---|---|
| 데이터 스키마 분석 및 검증 파이프라인 구축 | `b2d_schema.py` (스키마 정의) + `validate_b2d.py` (자동 검증) + `validation_report.json` |
| 학습 데이터 큐레이션 및 데이터셋 버전 관리 | `frame_features.parquet` (프레임 단위 피처 테이블) + `curate.py` + `manifest_v{N}.json` + `b2d_infos_train_v{N}.pkl` |
| 정량적·정성적 평가 지표 시각화 | `dashboard.html` (커버리지·분포·실패사례 갤러리) + `dataset_card.md` |

### 설계 원칙 하나만 기억하자: **"프레임 피처 테이블"이 허브다**

모든 검증·분석·선별을 각각 따로 짜지 말고,
**프레임 1개 = 1행**인 테이블을 딱 한 번 만들고 그 위에서 전부 처리합니다.

```
frame_features.parquet  (약 20만 행)
├── 식별자: clip_id, frame_idx, town, weather, scenario, route
├── ego 상태: x, y, yaw, speed, accel, jerk, steer, throttle, brake, command
├── 씬 통계: n_vehicle, n_pedestrian, n_within_20m, min_distance,
│            min_TTC, traffic_light_state, is_junction
├── 라벨 변화량: Δfut_traj, Δcommand, agent_set_IoU   ← 프레임 선별용
├── 검증 플래그: schema_ok, integrity_ok, physics_ok, projection_ok
└── 큐레이션: risk_score, cluster_id, keep_v1, keep_v2
```

이 테이블 하나면 검증 리포트도, 분포 그래프도, 프레임 선별도, 대시보드도 전부 pandas 한 줄로 나옵니다.
**1주차의 최우선 목표는 이 테이블을 만드는 것.**

---

## 3. 검증 · 큐레이션 7 레이어

### L0. 무결성 (Integrity) — "파일이 멀쩡한가"

| 체크 | 방법 |
|---|---|
| 클립별 프레임 수 일치 | `len(anno/*) == len(rgb_front/*) == len(lidar/*)` … 모든 센서 폴더 |
| 프레임 인덱스 연속성 | `00000..N` 중간에 빠진 번호 없는지 |
| 파일 손상 | gzip CRC 검사, JPEG 디코드 시도, laspy 읽기 시도 |
| 0바이트 / 이상 크기 | 파일 크기 분포에서 하위 이상치 탐지 |
| 해상도 | 전부 1600×900인지 |
| 체크섬 매니페스트 | 클립별 해시 기록 → 나중에 "데이터 안 바뀌었음" 증명 |

> **용어**: *무결성(integrity)* = 데이터가 손상·누락 없이 온전한가. 파일 레벨 문제.

### L1. 스키마 정합성 (Schema conformance) — "필드가 규격대로인가"

- `anno/*.json.gz`의 필수 키 존재 여부, 타입, shape
- **값 범위 규칙**: `throttle∈[0,1]`, `steer∈[-1,1]`, `brake∈{0,1}`, `speed≥0`, `command∈{1..6}`, `extent>0`
- `NaN` / `Inf` / `None` 탐지 (특히 `speed`, `theta`, `yaw`)
- **변환행렬 유효성**: `world2ego`, `world2lidar`, `cam2ego`가 진짜 SE(3)인가
  → `R @ R.T ≈ I` 이고 `det(R) ≈ +1` 인지 검사
- **내부 파라미터 일관성**: `fx == fy == image_size_x / (2·tan(fov/2))` 성립하는지

**구현**: `pydantic` 모델 또는 `jsonschema`로 정의 → 파싱 자체가 검증이 되게.
이러면 "스키마 명세서"라는 문서와 "검증 코드"가 한 파일에서 동기화됩니다.

> **용어**: *정합성(consistency)* = 값들이 서로/규칙과 모순 없이 맞아떨어지는가. 필드 레벨 문제.

### L2. 시간·물리 정합성 — "물리적으로 말이 되는가"

이 레이어가 **가장 논문/발표에서 임팩트 있는** 부분입니다. 남들이 잘 안 함.

| 체크 | 판정식 |
|---|---|
| dt 일정성 | 프레임 간 시간 간격이 0.1초로 일정한가 |
| 속도 자기일관성 | `‖(p_t − p_{t−1})‖ / dt` 와 기록된 `speed` 차이 < ε |
| 가속도 물리 한계 | `\|a\| < 10 m/s²`, jerk 폭주 없는지 |
| yaw 연속성 | ±π 경계에서 unwrap 후 각속도가 한계 이내인지 |
| 객체 텔레포트 | 같은 `id`의 프레임 간 이동거리 > `v_max·dt` 이면 이상 |
| 트랙 ID 안정성 | ID가 중간에 바뀌거나 재사용되는지 (ID switch) |
| **미래 궤적 GT 역검증** | ⭐ `ego_fut_trajs[k]` 를 ego 좌표계에서 월드로 되돌린 값이 실제 `t+5k` 프레임의 ego 위치와 일치하는가 |

마지막 항목이 **변환기(prepare_B2D) 자체를 검증하는 유일한 방법**입니다.
좌표계 실수는 여기서만 잡힙니다. 반드시 1순위로 구현하세요.

### L3. 센서-라벨 교차 검증 (Cross-modal) — "라벨이 센서와 맞는가"

- **투영 검증 (정량 지표화 가능)**: 3D 박스를 `lidar2img`로 이미지에 투영 →
  같은 프레임의 **instance/semantic segmentation 마스크**와 IoU 계산.
  → 평균 IoU가 곧 "우리 변환행렬 정확도 점수". 발표용 숫자로 아주 좋음.
- LiDAR `num_points == 0` 인데 `distance`가 가까운 객체 → 가림(occlusion)인지 라벨 오류인지 분류
- 카메라 FOV 밖 객체 필터링 규칙이 프레임마다 일관적인지

### L4. 분포 · 편향 분석 (Bias) — "데이터가 어디에 쏠려 있는가"

Hidden Biases 논문의 인사이트 (1)(2)에 정확히 대응하는 파트.

- `steer` 히스토그램 → 0 근처에 극단적으로 쏠림 (직진 편향)
- `speed` 분포 → 정지(v≈0) 프레임 비율이 몇 %인가
- `command`, `town`, `weather`, `scenario` 별 프레임 수
- 객체 클래스 분포, 거리 분포, 야간/우천 비율
- **균형 지표**: 분포 엔트로피, Gini 계수 → 큐레이션 전/후 비교용 숫자

> 논문 인사이트: **"클래스 빈도 같은 단순 기준으로 프레임에 가중치를 주면 안 된다."**
> 이유: 많이 등장하는 클래스가 하나의 "지루한 모드"가 아니라, **지루한 부분과 결정적인 부분이 섞여 있기** 때문.
> 예) "직진" 프레임 안에는 진짜 아무 일 없는 직진도 있고, 앞차 급정거 직전의 직진도 있음.

### L5. 프레임 선별 (Curation) — 핵심 기여

#### (a) Hidden Biases 방식: "라벨이 바뀌는 프레임만 남긴다"

논문의 인사이트 (3): **"이전 프레임 대비 정답 라벨이 바뀌는지를 추정하면, 중요한 정보를 잃지 않으면서 데이터셋 크기를 줄일 수 있다."**

VAD/B2D로 번역하면:

```python
# 개념 코드
prev = to_ego_frame_of(t, traj_at(t-1))   # t-1의 미래궤적을 t의 ego 좌표계로 변환
curr = traj_at(t)                          # t의 미래궤적
delta = norm(curr - prev)                  # 실제로 "계획이 바뀌었는가"

keep = (delta > tau_traj) \
    or (command[t] != command[t-1]) \
    or (brake_bucket[t] != brake_bucket[t-1]) \
    or (steer_bucket[t] != steer_bucket[t-1]) \
    or (agent_set_IoU(t, t-1) < tau_agent)   # 새 객체 등장/이탈
```

- `tau_traj`는 실험적으로 정함 (0.1m / 0.3m / 0.5m 세 세팅 비교)
- 항상 **유지율(keep rate)** 를 함께 보고: "60% 줄였는데 성능 동일" 이 우리의 헤드라인 숫자
- 구현 레퍼런스: [autonomousvision/carla_garage](https://github.com/autonomousvision/carla_garage) — 원 저자 코드를 반드시 먼저 읽고 B2D로 포팅

#### (b) 취약 시나리오 서칭 (risk scoring)

프레임마다 위험 점수를 매기고 상위 구간을 "취약 시나리오"로 라벨링:

| 신호 | 계산 |
|---|---|
| **TTC** (충돌까지 남은 시간) | 상대속도/상대거리로 계산, 낮을수록 위험 |
| 급제동 | `brake==1` 이면서 `\|a\|` 큰 프레임 |
| 급조향 | `\|Δsteer/Δt\|` 상위 |
| 근접 상호작용 | 반경 10m 내 동적 객체 수 |
| 신호/교차로 | `traffic_light.affects_ego`, `is_junction` |
| 보행자 근접 | 보행자 최소거리 (속도는 직접 계산해야 함 — L2 참고) |
| 전문가 신호 | `expert_assessment/*.npz` 의 value/uncertainty 활용 (있으면 강력) |

→ `risk_score = 정규화 가중합` 또는 각 항목을 개별 플래그로 유지 (해석 가능성 ↑)

#### (c) 클러스터링으로 희소 상황 찾기

- **입력 특징**: 우선 hand-crafted 벡터 (ego 운동학 + 씬 통계 + 맵 문맥). 3주 반이면 이게 현실적.
- **차원 축소**: PCA → UMAP (시각화용)
- **군집화**: k-means(k=30~50) 또는 HDBSCAN(밀도 기반, 클러스터 수 자동 결정 + 노이즈 분리)
- **활용**: 작은 클러스터 = 희소 상황 → 보존/오버샘플링 대상. 거대 클러스터 = 중복 → 다운샘플링 대상.
- 여유 있으면: DINOv2/CLIP 이미지 임베딩으로 교체해 비교 (ablation 한 줄 추가)

#### (d) 최종 리샘플링 정책

```
v0 (baseline) : 무작위 N% 샘플링          ← 비교 기준
v1 (label-change) : (a) 프레임 선별만
v2 (risk-aware)   : (a) + 취약 시나리오 오버샘플 + 정지 프레임 다운샘플
v3 (cluster-balanced) : (a) + 클러스터별 균등 상한
```

**같은 프레임 예산**에서 v0 vs v1/v2/v3를 비교해야 공정합니다. 이게 실험 설계의 핵심.

### L6. 데이터셋 버전 관리

원본 200GB를 복사하지 마세요. **인덱스만 버전 관리**합니다.

```
datasets/
├── manifest_v1.json      # {clip_id, frame_idx} 유지 목록 + 생성 config 해시
├── b2d_infos_train_v1.pkl
├── dataset_card_v1.md    # 몇 프레임, 어떤 규칙, 분포 요약, 알려진 한계
└── configs/curate_v1.yaml
```

- 원본은 read-only, 매니페스트는 git으로 관리 (JSON이라 diff도 보임)
- 재현성: `config 해시 + 코드 커밋 해시`를 매니페스트에 박아두기
- 규모가 커지면 DVC 도입 검토 (지금 규모에선 오버킬)

### L7. 평가 지표 시각화

**정량 (데이터 자체)**
- 커버리지: 시나리오 × 타운 × 날씨 히트맵 (빈칸 = 학습 사각지대)
- 유지율 / 압축률, 검증 실패 건수 (레이어별)
- 분포 균형: 엔트로피, Gini (큐레이션 전/후)
- 투영 IoU 평균 (L3)

**정성**
- 검증 실패 프레임 카드 (이미지 + BEV 오버레이 + 실패 사유)
- 클러스터별 대표 프레임 갤러리
- 취약 시나리오 Top-50 썸네일

**다운스트림 (가장 중요)**
- 동일 예산에서 v0 vs v1/v2 로 VAD 학습 → open-loop L2 / collision rate, closed-loop Driving Score / Success Rate
- **이것 없이는 "데이터 큐레이션이 도움됐다"를 증명할 수 없습니다.** 모델 팀과 학습 슬롯을 미리 예약하세요.

---

## 4. 3주 반 로드맵

### Week 0 — 내일부터 3일 (8/4 화 ~ 8/6 목): "손으로 만져보기"

가장 흔한 실패가 **논문만 읽다가 1주 날리는 것**입니다. 코드부터 돌리세요.

**Day 1 (8/4)**
1. 서버 실물 확인: `du -sh`, 클립 개수, 폴더 구조가 `anno.md`와 일치하는지
2. 클립 1개 골라 `anno/00000.json.gz` 압축 풀고 **키를 전부 출력해서 눈으로 보기**
3. `python tools/visualize.py -f <clip> -m maps/Town**_lanemarkings.npz` 실행 → 좌표계 감각 잡기
4. [carla_garage/docs/coordinate_systems.md](https://github.com/autonomousvision/carla_garage/blob/main/docs/coordinate_systems.md) 정독 (30분, 필수)

**Day 2 (8/5)**
1. Bench2DriveZoo `uniad/vad` 브랜치 clone + 환경 세팅 (mmcv 내장 버전 사용)
2. `mmcv/datasets/prepare_B2D.py` **한 줄씩 읽으면서** 필드 매핑 표 작성
   → `anno 필드 → pkl 필드 → 모델 입력 키` 3열 표. **이게 곧 스키마 명세서 초안.**
3. Mini 규모(클립 5~10개)로 변환기 실제 실행 → pkl 생성 성공시키기

**Day 3 (8/6)**
1. 생성된 pkl 열어서 1.3절 표의 키/모양이 실제로 맞는지 확인
2. `B2D_vad_dataset.py`의 `get_data_info()` 읽고, `__getitem__` 한 번 호출해 텐서 모양 찍어보기
3. **첫 검증 스크립트**: `lidar2img`로 3D 박스를 `rgb_front`에 투영해서 이미지에 그려보기
   → 박스가 차량 위에 정확히 얹히면 좌표계 이해 완료. 안 맞으면 여기서 며칠 잡아먹더라도 반드시 해결.

> Day 3의 투영 시각화가 성공하는 순간이 이 프로젝트의 진짜 시작점입니다.

### Week 1 (8/7 ~ 8/12): 스키마 + L0/L1/L2 검증 파이프라인

- `b2d_schema.py` — pydantic 스키마 (필드/타입/범위)
- `validate_b2d.py` — L0 무결성 + L1 스키마 + L2 물리 검증, 클립 단위 병렬 처리 (multiprocessing)
- **미래 궤적 역검증** 구현 (L2 마지막 항목)
- `frame_features.parquet` v1 생성 — 전체 1,000클립 스캔
- 산출: `validation_report.json` + 실패 클립 리스트
- ✅ 체크포인트: "1,000클립 중 N개 클립, M개 프레임에서 X종류 이상 발견" 이라고 말할 수 있는 상태

### Week 2 (8/13 ~ 8/19): L3 교차검증 + L4 분포분석 + L5(b) 취약 시나리오

- 투영 IoU 검증 (segmentation 마스크 대조)
- 분포/편향 분석 노트북 → 그래프 일괄 생성
- TTC·급제동·급조향·근접 등 위험 신호 계산 → `risk_score` 컬럼 추가
- `expert_assessment` npz 구조 파악해서 쓸 수 있는지 판단
- ✅ 체크포인트: 취약 시나리오 Top-100 프레임을 눈으로 보고 "정말 위험한 장면인가" 확인

### Week 3 (8/20 ~ 8/26): L5(a)(c)(d) 프레임 선별 + 데이터셋 릴리즈

- carla_garage의 프레임 필터링 코드 정독 → B2D로 포팅
- `Δfut_traj` 계산 + `tau` 스윕 (유지율-정보량 곡선 그리기)
- 클러스터링 (PCA + k-means/HDBSCAN) → `cluster_id`
- `curate.py` → `manifest_v1/v2/v3.json` + `b2d_infos_train_v{N}.pkl`
- 모델 팀에 v1 전달 + 학습 시작 요청 (**늦어도 8/22까지** — 학습 시간 확보)
- ✅ 체크포인트: 모델 팀이 우리 pkl로 학습을 돌리기 시작

### Week 3.5 (8/27 ~ 8/28): 결과 정리

- 다운스트림 결과 수집 (v0 vs v1/v2)
- `dashboard.html` + `dataset_card.md` 완성
- 발표 자료: "발견한 이상 → 검증 파이프라인 → 큐레이션 → 성능" 4장 스토리

> **버퍼**: 실제로 Week 1이 밀릴 확률이 높습니다. 밀리면 **L5(c) 클러스터링을 먼저 버리세요.**
> L5(a) 프레임 선별과 L7 다운스트림 비교는 절대 버리면 안 되는 코어입니다.

---

## 5. 읽을 자료 (우선순위 순)

### 필독 논문

| 순위 | 논문 | 왜 / 어디를 |
|---|---|---|
| ★★★ | [Hidden Biases of End-to-End Driving **Datasets** (2412.09602)](https://arxiv.org/abs/2412.09602) | 우리 방법론의 원전. 프레임 선별 절과 실험 세팅 정독 |
| ★★★ | [Bench2Drive (2406.03877)](https://arxiv.org/abs/2406.03877) | 44개 시나리오 정의, 평가 프로토콜 (DS/SR/Efficiency/Comfort) |
| ★★★ | [VAD (2303.12077)](https://arxiv.org/abs/2303.12077) | 3장(vectorized scene representation, planning constraints)만 정독 |
| ★★ | [BEVFormer (2203.17270)](https://arxiv.org/abs/2203.17270) | VAD의 BEV 인코더. spatial cross-attention이 `lidar2img`를 어떻게 쓰는지 → **왜 투영행렬 검증이 중요한지 알게 됨** |
| ★★ | [Hidden Biases of End-to-End Driving **Models** (2306.07957, ICCV'23)](https://arxiv.org/abs/2306.07957) | 위 논문의 전편. target-speed 분류, 관성 문제 |
| ★ | [MapTR (2208.14437)](https://arxiv.org/abs/2208.14437) | 벡터 맵 GT 구성 방식 (map 브랜치) |
| ★ | [VADv2 (2402.13243)](https://arxiv.org/abs/2402.13243) | 확률적 플래닝. 팀이 VADv2로 갈지 여부에 따라 |

### 뜯어볼 코드

| 레포 | 볼 파일 |
|---|---|
| [Thinklab-SJTU/Bench2DriveZoo (`uniad/vad`)](https://github.com/Thinklab-SJTU/Bench2DriveZoo/tree/uniad/vad) | ⭐ `mmcv/datasets/prepare_B2D.py`, `mmcv/datasets/B2D_vad_dataset.py`, `adzoo/vad/configs/*` |
| [Thinklab-SJTU/Bench2Drive](https://github.com/Thinklab-SJTU/Bench2Drive) | ⭐ `docs/anno.md`, `tools/visualize.py`, `tools/data_collect.py`, `tools/gen_hdmap.py` |
| [hustvl/VAD](https://github.com/hustvl/VAD) | `tools/data_converter/vad_nuscenes_converter.py` (원본 변환기 — B2D 버전과 비교하면 이해가 빠름), `projects/mmdet3d_plugin/VAD/VAD_head.py` |
| [autonomousvision/carla_garage](https://github.com/autonomousvision/carla_garage) | ⭐ 프레임 필터링 구현, `docs/coordinate_systems.md` |

---

## 6. 산출물 체크리스트

```
data-pipeline/
├── schema/
│   ├── b2d_schema.py            # pydantic 스키마 = 명세서 겸 검증기
│   └── FIELD_MAPPING.md         # anno → pkl → 모델입력 3열 매핑표
├── validate/
│   ├── validate_b2d.py          # L0~L2
│   ├── crossmodal_check.py      # L3 투영 IoU
│   └── reports/validation_report.json
├── features/
│   ├── build_features.py
│   └── frame_features.parquet   # ⭐ 모든 것의 허브
├── curate/
│   ├── risk_score.py            # L5(b)
│   ├── frame_filter.py          # L5(a)
│   ├── cluster.py               # L5(c)
│   └── curate.py → manifest_v{N}.json, b2d_infos_train_v{N}.pkl
├── viz/
│   ├── dashboard.py → dashboard.html
│   └── failure_gallery/
└── docs/
    ├── dataset_card_v1.md
    └── VALIDATION_SPEC.md
```

---

## 부록 A. 용어 사전

| 용어 | 쉬운 설명 |
|---|---|
| **BEV (Bird's Eye View)** | 하늘에서 내려다본 평면 지도 형태의 표현. 6개 카메라 이미지를 이 평면 하나로 합쳐서 모델이 씬을 이해함 |
| **BEV 쿼리 (query)** | BEV 격자(예 200×200)의 각 칸마다 배정된 학습 가능한 벡터. "내 칸에 뭐가 있지?"를 이미지 특징에서 찾아옴 |
| **Spatial cross-attention** | BEV 칸의 3D 좌표를 `lidar2img`로 이미지에 투영해서, 그 픽셀 주변 특징만 골라 가져오는 연산. **투영행렬이 틀리면 엉뚱한 픽셀을 봄** |
| **Temporal self-attention** | 이전 프레임 BEV를 ego 이동량(`can_bus`)만큼 정렬해서 현재 BEV와 섞음. 가림·속도 추정에 도움 |
| **`lidar2img`** | LiDAR 좌표계의 3D 점 → 이미지 픽셀로 보내는 4×4 행렬 (= 내부 파라미터 × 외부 파라미터) |
| **`can_bus`** | 차량 자체 상태 18개 값 묶음 (이동량, 회전량, 속도, 가속도 등). 원래 nuScenes의 CAN 버스 로그에서 유래 |
| **SE(3)** | 3D 회전+평행이동 변환의 수학적 집합. 유효하려면 회전부가 `R Rᵀ = I`, `det R = +1` |
| **무결성 vs 정합성** | 무결성 = 파일이 깨지거나 빠지지 않았나 (파일 레벨). 정합성 = 값들이 규칙·서로와 모순 없나 (의미 레벨) |
| **TTC (Time-To-Collision)** | 현재 속도가 유지될 때 충돌까지 남은 시간(초). 작을수록 위험 |
| **오픈루프 / 클로즈드루프** | 오픈루프 = 녹화된 데이터에서 예측만 평가 (L2 오차). 클로즈드루프 = 시뮬레이터에서 실제로 운전시켜 평가 (Driving Score). 후자가 진짜 |
| **Driving Score (DS)** | Bench2Drive/CARLA 평가 지표. 경로 완주율 × 위반 페널티 |
| **HDBSCAN** | 밀도 기반 군집화. k를 미리 안 정해도 되고, 어디에도 안 속하는 점을 "노이즈"로 빼줌 → 희소 상황 발굴에 적합 |
| **Ablation (제거 실험)** | 구성요소를 하나씩 빼면서 성능 변화를 보는 실험. "이 요소가 기여했다"를 증명하는 표준 방법 |
| **Dataset card** | 데이터셋 설명 문서. 규모·수집 방식·분포·알려진 한계·라이선스를 기록 |
| **DVC** | Git처럼 대용량 데이터 버전을 관리하는 도구 |

---

## 부록 B. DAgger를 쓸 것인가 — 솔직한 판단

**DAgger (Dataset Aggregation)** 는 이런 반복 루프입니다:
1. 현재 학습된 모델을 시뮬레이터에서 직접 운전시킨다
2. 모델이 방문한 상태들을 기록한다 (여기가 핵심 — 모델이 실수하는 상태가 모임)
3. **그 상태들에 대해 전문가에게 "너라면 뭐 했겠니?"를 물어 정답을 받는다**
4. 기존 데이터에 합쳐서 재학습한다

풀려는 문제는 **분포 이동(distribution shift)** 입니다. 모방학습 모델은 전문가가 방문한 상태만 배우는데, 실제 주행 중엔 자기 실수로 전문가가 가본 적 없는 상태에 빠지고 → 거기서 더 큰 실수를 함 (compounding error).

### 우리 프로젝트에 적합한가: **직접 도입은 비추천**

이유:
- DAgger는 **온라인 데이터 수집 루프**입니다. CARLA 실행 + 전문가 정책(Think2Drive/PDM-Lite) 구동 + 재학습을 여러 라운드 반복해야 함
- 그런데 우리 팀은 이번에 **PDM-Lite 수집을 걷어내고 B2D 기성 데이터셋으로 가기로** 했습니다. DAgger를 넣는 건 그 결정을 되돌리는 것
- 3주 반에 CARLA 클로즈드루프 루프 세팅 + 다중 라운드 학습은 물리적으로 불가능

### 대신 쓸 수 있는 "DAgger의 정신을 살린 오프라인 버전"

**Loss-based hard example mining (오프라인 하드샘플 마이닝)** — Week 3에 여유 있으면 옵션으로:

1. 모델 팀이 v1으로 학습한 체크포인트를 받는다
2. 그 모델을 **학습셋 전체에 추론**시켜 프레임별 플래닝 손실(L2 오차)을 계산한다
3. 손실 상위 프레임 = "모델이 아직 못 배운 상황" → 이걸 오버샘플링해서 v2를 만든다
4. 재학습

DAgger처럼 새 상태를 만들진 않지만, **"모델이 어려워하는 곳에 데이터를 더 준다"**는 핵심 아이디어는 같고, 오프라인이라 하루면 돌아갑니다. 발표에서 "DAgger는 온라인 수집이 필요해 범위를 벗어나므로, 그 아이디어를 오프라인 능동학습으로 대체했다"고 설명하면 오히려 설계 판단력을 보여주는 포인트가 됩니다.
