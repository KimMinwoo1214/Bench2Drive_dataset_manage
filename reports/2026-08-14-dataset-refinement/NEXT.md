# 돌아오면 할 것

2026-08-14 퇴근 시점 기준. PKL 변환은 서버에서 계속 돌고 있고, 끝나면 결과를
스스로 기록하고 커밋까지 한다. **push 만 사람 손이 필요하다.**

---

## 1. 무엇이 끝났고 무엇이 도는 중인가

| | 상태 |
|---|---|
| 충돌 선별 → 29개 제외 → split 1,300 | **완료, push 됨** |
| relabel (bbox 복구 + `affects_ego`) | **완료, push 됨** |
| 발표 자료 (문서·수치·표·그림 78장) | **완료, push 됨** |
| PKL 2종 생성 + validator 4종 | **서버에서 진행 중** |
| PKL 품질 리포트 | 위가 끝나면 자동 생성·커밋 |

두 프로세스가 `ppid=1` 로 분리돼 있어 SSH 가 끊겨도 산다.

- `run_phase4_6_pkl.sh` — 변환 파이프라인
- `run_after_pkl.sh` — 그게 끝나기를 기다렸다가 리포트를 쓰고 커밋

---

## 2. 결과 확인 — 파일 하나만 보면 된다

```bash
cat reports/2026-08-14-dataset-refinement/STATUS.md
```

성공이든 실패든 **여기에 적힌다.** 서버에서 보려면:

```bash
W=/home/ailab/AILabSSD/99_Management/2026_internship/kimminseong/production_1329_v1_code
cat "$W/Bench2Drive_dataset_manage/reports/2026-08-14-dataset-refinement/STATUS.md"
```

없다면 아직 도는 중이다. 진행 상황은:

```bash
tail -3 "$W/phase2_5_logs/PHASE4_6_PKL_20260814_v3.log"
pgrep -af run_phase4_6_pkl.sh    # 비어 있으면 끝난 것
```

---

## 3. 성공했으면 — push 만 하면 끝

```bash
unset GIT_ASKPASS VSCODE_GIT_ASKPASS_NODE VSCODE_GIT_ASKPASS_MAIN \
      VSCODE_GIT_ASKPASS_EXTRA_ARGS VSCODE_GIT_IPC_HANDLE
export GIT_TERMINAL_PROMPT=1
W=/home/ailab/AILabSSD/99_Management/2026_internship/kimminseong/production_1329_v1_code

git -C "$W/Bench2Drive_dataset_manage" push origin production-1329-quality-gate-v1:main
```

새 PAT 이 필요하다 (이전 것은 폐기했을 테니). classic, scope `repo` 하나면 된다.
사용자명은 `minsungk02`.

`--ff-only` 병합이 필요 없는 이유: `<로컬>:<원격>` 형식이라 브랜치 전환도
병합도 없이 원격만 앞으로 간다. fast-forward 가 아니면 git 이 거부한다.

---

## 4. 실패했으면 — 재개는 한 줄

```bash
cd "$W/phase2_5_logs" && ./run_phase4_6_pkl.sh
```

로그는 절대 덮어쓰지 않으므로 `run_phase4_6_pkl.sh` 안의 `--run-label` 만
새 값으로 바꾸면 된다 (현재 `overlay-fix2`).

이미 만들어진 산출물은 그대로 두고, 실패한 단계부터 다시 하면 된다. 각 단계가
자기 출력이 이미 있으면 거부하므로, 다시 만들 단계의 출력 디렉토리만
`pkl_attempts/` 같은 곳으로 **옮겨두고**(지우지 말고) 재실행한다.

`STATUS.md` 에 어느 단계에서 멈췄는지와 오류가 적혀 있다.

---

## 5. 남은 검증 (선택)

PKL 이 나온 뒤 확인할 것 — `06_PKL.md` 에 자동으로 들어간다.

- validator 4종 `status: passed`, `errors: []`
- `annotation_comparison.unexpected_field_changes: 0`
  (신호등 7개 필드 외에는 두 PKL 이 완전히 같아야 한다)
- `ordered_key_match: true`, 두 세트 `records` 동일
- map PKL sha256 이 original == corrected
- **UV 마스킹 확인**: corrected 의 `uv_valid_frames` 가 original 보다 작고,
  그 차이가 `completion.json` 의 `uv_masked_ego_lights` (약 8,983) 와 맞아야 한다.
  그 프레임들의 `ego_tl_state` 는 변하지 않아야 한다

---

## 6. 손대지 말 것

- `production_1329/infos/base1000_weak329_original_v1/` — 기존 PKL. sha256 고정,
  `data/base/infos/` 에서 symlink 참조 중, 모델팀 handoff 완료
- `relabel_attempts/` 와 `quality_gate/calibration_v3/` — 두 결함의 유일한 1차
  기록. 발표에서 "이런 문제를 발견했다" 의 근거다
- `~/.git-credentials` — 관리자 것. `credential.helper store` 를 켜지 말 것
