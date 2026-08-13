#!/usr/bin/env python3
"""
Bench2Drive anno: traffic_light bbox cyclic-permutation 복구 + affects_ego 재계산.

원인 (tools/data_collect.py, get_bounding_boxes 내 traffic_light 블록)
----------------------------------------------------------------------
actor 목록과 world.get_level_bbs(TrafficLight) 목록을 2D 최근접 greedy 로
붙인다 (`.remove()` 때문에 비복원). 그런데 CARLA 의 마스트형 신호등은
폴(=actor 원점)이 모퉁이에 있고 그 폴이 통제하는 등(head)은 교차로
'건너편'에 매달려 있다. 따라서 actor 원점에 가장 가까운 head 는 항상
옆 접근로의 것이고, 매칭이 교차로 단위로 한 칸씩 회전한다.

결과적으로 한 엔트리 안에 두 actor 의 정보가 섞인다:

    location / rotation / center / extent / road_id / lane_id / section_id
        -> level bbox (틀림, 한 칸 밀림)
    id / state / distance / trigger_volume_location / trigger_volume_extent
        -> actor (정확)
    trigger_volume_rotation = bbox.rotation + actor.trigger_volume.rotation
        -> 혼합 (오염)

복구 원리
--------
어떤 신호등의 head 는 자기 trigger volume 의 '건너편'에 있어야 한다.
교차로 중심 기준 각도로 head_angle ≈ tv_angle + 180.
이 조건으로 Hungarian 을 풀면 배정이 유일하게 확정된다.

bbox 복구가 끝난 동일한 annotation 객체에서 ego 진행 방향과 trigger volume을
이용해 affects_ego를 다시 고른다. 두 수정이 모두 끝난 뒤 프레임 파일을 한 번만
저장하므로 bbox 수정본과 affects_ego 수정본이 따로 생기지 않는다.

검증: 복구 후 facing error(=head 의 yaw 와 head->tv 방향의 각도차)가
87~93 도로 수렴해야 한다. 18 도 부근이면 아직 밀려 있는 것.

주의: `distance` 기반 재배정은 쓰면 안 된다. 수집 코드의 greedy 가
      바로 그 값을 최소화하므로 같은 오답을 재생산한다.

사용법
------
    python fix_tl_bbox_permutation.py --root /path/to/clips            # 감사만
    python fix_tl_bbox_permutation.py --root /path/to/clips --out ./fixed
    python fix_tl_bbox_permutation.py --root /path/to/clips --out ./fixed --bbox-only
    python fix_tl_bbox_permutation.py --root /path/to/clips --apply    # .bak 백업 후 덮어쓰기
"""

import argparse
import gzip
import json
import math
import os
import shutil
import re
import sys
from collections import Counter

import numpy as np
from scipy.optimize import linear_sum_assignment

# ---------------------------------------------------------------- 파라미터

JUNCTION_RADIUS = 30.0   # [m] trigger volume 이 이 안에 있으면 같은 교차로
MIN_GROUP = 3            # 이보다 적으면 배정이 불정 -> 건드리지 않음
FACE_OK_LO, FACE_OK_HI = 80.0, 100.0   # 폴백 기본값. 실제로는 타운별로 자동 산출된다
BAND_HALF = 15.0        # 자동 산출된 중앙값 +- 이 값이 정상 대역
CALIB_RESID = 10.0      # 보정 표본으로 쓸 교차로의 최대 각도 잔차 [deg]
CALIB_MIN_N = 200       # 타운별 최소 표본 수 (미달이면 전역 대역 사용)
CALIB_STRIDE = 10       # 보정 시 프레임 샘플링 간격
MAX_ANG_RESID = 35.0     # [deg] 배정 후 평균 각도 잔차가 이보다 크면 신뢰 불가

COS_MIN = 0.70           # ego 진행 방향과 신호등 통제 방향의 최소 cos
LAT_MAX = 2.5            # ego 차로 중심에서 허용할 횡방향 거리 [m]
LON_MIN = -3.0           # ego 뒤쪽 허용 범위 [m]
LON_MAX = 60.0           # ego 전방 탐색 범위 [m]
VOTE_GAP = 40.0          # 접근 구간을 나누는 ego 위치 점프 거리 [m]

BBOX_FIELDS = ["location", "rotation", "center", "extent",
               "road_id", "lane_id", "section_id"]


def wrap(a):
    return (a + 180.0) % 360.0 - 180.0


# ---------------------------------------------------------------- I/O

def load(path):
    op = gzip.open if path.endswith(".gz") else open
    with op(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def dump(path, data):
    if path.endswith(".gz"):
        # mtime=0 으로 고정해야 내용이 같을 때 바이트도 같아진다
        with gzip.GzipFile(path, "wb", mtime=0) as gz:
            gz.write(json.dumps(data, indent=4).encode("utf-8"))
    else:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)


def walk_clips(root, clip_names=None):
    """클립(디렉토리) 단위로 프레임 경로 목록을 반환.

    ``clip_names``가 주어지면 flat Bench2Drive root 바로 아래의 명시된 clip만
    읽는다. Production pipeline이 manifest 밖 annotation을 consensus나 출력에
    섞지 않도록 하기 위한 경로다.
    """
    if clip_names is not None:
        for clip_name in clip_names:
            anno_dir = os.path.join(root, clip_name, "anno")
            try:
                names = sorted(os.listdir(anno_dir))
            except FileNotFoundError as error:
                raise FileNotFoundError(
                    f"manifest clip annotation directory is missing: {anno_dir}"
                ) from error
            frames = [
                os.path.join(anno_dir, name)
                for name in names
                if name.endswith(".json") or name.endswith(".json.gz")
            ]
            if not frames:
                raise ValueError(f"manifest clip has no annotation frames: {anno_dir}")
            yield anno_dir, frames
        return

    for dirpath, _, names in os.walk(root):
        frames = [os.path.join(dirpath, n) for n in sorted(names)
                  if n.endswith(".json") or n.endswith(".json.gz")]
        if frames:
            yield dirpath, frames


# ---------------------------------------------------------------- 핵심

def facing_error(head_xy, head_yaw, tv_xy):
    """head 의 yaw 와 head->tv 방향 사이의 각도차 [deg]."""
    v = np.asarray(tv_xy) - np.asarray(head_xy)
    if np.linalg.norm(v) < 1e-6:
        return None
    return abs(wrap(math.degrees(math.atan2(v[1], v[0])) - head_yaw))


def group_junctions(tvs):
    """trigger volume 근접성으로 교차로 단위 그룹핑."""
    n = len(tvs)
    seen = [False] * n
    groups = []
    for s in range(n):
        if seen[s]:
            continue
        grp = [j for j in range(n) if not seen[j]
               and np.linalg.norm(tvs[j] - tvs[s]) < JUNCTION_RADIUS]
        for j in grp:
            seen[j] = True
        groups.append(grp)
    return groups


def solve_group(heads, tvs, grp):
    """그룹 내에서 entry(=tv) 에 붙어야 할 head 인덱스를 반환.

    head 는 자기 tv 의 교차로 반대편에 있어야 한다 -> 각도차 180 도.
    """
    center = tvs[grp].mean(axis=0)
    ah = np.degrees(np.arctan2(*(heads[grp] - center).T[::-1]))
    at = np.degrees(np.arctan2(*(tvs[grp] - center).T[::-1]))
    cost = np.array([[abs(wrap(ah[j] - at[i] - 180.0))
                      for j in range(len(grp))] for i in range(len(grp))])
    rows, cols = linear_sum_assignment(cost)
    resid = float(cost[rows, cols].mean())
    return {grp[i]: grp[j] for i, j in zip(rows, cols)}, resid


GRID = 0.5          # [m] 월드 좌표 키 양자화
MATCH_TOL = 1.5     # [m] 목표 head 위치를 프레임 내에서 찾을 때 허용 오차


def wkey(xy):
    """월드 좌표를 격자 키로. actor id 와 달리 에피소드가 바뀌어도 불변."""
    return (round(float(xy[0]) / GRID), round(float(xy[1]) / GRID))


def town_of(path, root):
    """경로에서 타운 키를 뽑는다. 실패하면 클립 디렉토리명."""
    m = re.search(r"_(Town\w+?)_Route", path)
    if m:
        return m.group(1)
    return os.path.basename(os.path.dirname(os.path.abspath(path)))


def calib_samples(anno):
    """잘 풀린 교차로에서 '정답 배정일 때의 facing error' 표본을 뽑는다.

    180 도 규칙은 에셋 규약과 무관하게 물리적으로 성립하므로,
    이 표본의 중앙값이 곧 그 타운의 정상 facing error 다.
    """
    lights = [b for b in anno.get("bounding_boxes", [])
              if b.get("class") == "traffic_light"
              and "trigger_volume_location" in b]
    if len(lights) < MIN_GROUP:
        return []
    heads = np.array([b["location"][:2] for b in lights], dtype=float)
    tvs = np.array([b["trigger_volume_location"][:2] for b in lights], dtype=float)
    yaws = np.array([b["rotation"][2] for b in lights], dtype=float)
    out = []
    for grp in group_junctions(tvs):
        if len(grp) < 4:                 # 4갈래만 써야 배정이 유일하다
            continue
        m, resid = solve_group(heads, tvs, grp)
        if resid > CALIB_RESID:
            continue
        for i in grp:
            fe = facing_error(heads[m[i]], yaws[m[i]], tvs[i])
            if fe is not None:
                out.append(fe)
    return out


def solve_frame(anno, band=None):
    """한 프레임에서 actor id 단위 배정을 추정. {id_i: id_j} 반환."""
    lights = [b for b in anno.get("bounding_boxes", [])
              if b.get("class") == "traffic_light"
              and "trigger_volume_location" in b]
    if len(lights) < MIN_GROUP:
        return {}
    heads = np.array([b["location"][:2] for b in lights], dtype=float)
    tvs = np.array([b["trigger_volume_location"][:2] for b in lights], dtype=float)
    yaws = np.array([b["rotation"][2] for b in lights], dtype=float)

    def fe_of(i, j):
        return facing_error(heads[j], yaws[j], tvs[i])

    lo, hi = band if band else (FACE_OK_LO, FACE_OK_HI)

    def band_ok(vals):
        vals = [v for v in vals if v is not None]
        return bool(vals) and all(lo <= v <= hi for v in vals)

    out = {}
    for grp in group_junctions(tvs):
        if len(grp) < MIN_GROUP:
            continue
        if band_ok([fe_of(i, i) for i in grp]):
            for i in grp:
                out[lights[i]["id"]] = lights[i]["id"]      # 이미 정상
            continue
        m, resid = solve_group(heads, tvs, grp)
        if resid > MAX_ANG_RESID:
            continue
        if not band_ok([fe_of(i, m[i]) for i in grp]):
            continue
        for i in grp:
            out[lights[i]["id"]] = lights[m[i]]["id"]
    return out


def solve_frame_world(anno, band=None):
    """월드 좌표 키 단위 배정을 추정. {tv_key: head_xy} 반환."""
    lights = [b for b in anno.get("bounding_boxes", [])
              if b.get("class") == "traffic_light"
              and "trigger_volume_location" in b]
    idx = {b["id"]: i for i, b in enumerate(lights)}
    out = {}
    for a, t in solve_frame(anno, band).items():
        i, j = idx[a], idx[t]
        out[wkey(lights[i]["trigger_volume_location"])] = \
            tuple(round(v, 3) for v in lights[j]["location"][:2])
    return out


def fix_frame(anno, forced=None, rows=None, clip="", frame="", band=None):
    """한 프레임의 traffic_light 배정을 복구.

    rows 가 주어지면 엔트리별 진단 행을 append 한다.
    (stats, changed) 반환.
    """
    lights = [b for b in anno.get("bounding_boxes", [])
              if b.get("class") == "traffic_light"
              and "trigger_volume_location" in b]
    st = Counter()
    if len(lights) < MIN_GROUP:
        return st, False

    heads = np.array([b["location"][:2] for b in lights], dtype=float)
    tvs = np.array([b["trigger_volume_location"][:2] for b in lights], dtype=float)
    yaws = np.array([b["rotation"][2] for b in lights], dtype=float)

    lo, hi = band if band else (FACE_OK_LO, FACE_OK_HI)

    # 복구 전 facing error 기록
    for i, b in enumerate(lights):
        fe = facing_error(heads[i], yaws[i], tvs[i])
        if fe is None:
            continue
        st["before_ok" if lo <= fe <= hi else "before_bad"] += 1

    def fe_of(i, j):
        """entry i 가 head j 를 가졌을 때의 facing error."""
        return facing_error(heads[j], yaws[j], tvs[i])

    def band_ok(vals):
        vals = [v for v in vals if v is not None]
        return bool(vals) and all(lo <= v <= hi for v in vals)

    idx_of = {b["id"]: i for i, b in enumerate(lights)}
    mapping = {}
    action = {}          # entry index -> 처리 결과 라벨
    gsize = {}           # entry index -> 같은 교차로로 묶인 신호등 수
    gresid = {}          # entry index -> 배정 각도 잔차 [deg]

    for grp in group_junctions(tvs):
        try:
            _, r = solve_group(heads, tvs, grp) if len(grp) >= MIN_GROUP else (None, float("nan"))
        except Exception:
            r = float("nan")
        for i in grp:
            gsize[i] = len(grp)
            gresid[i] = r

    if forced:
        # 전역(월드 좌표) 합의를 적용 -> 부분 관측 프레임과 다른 클립도 복구
        for i, b in enumerate(lights):
            tgt = forced.get(wkey(b["trigger_volume_location"]))
            if tgt is None:
                st["no_consensus"] += 1
                action[i] = "no_consensus"
                continue
            dists = np.linalg.norm(heads - np.asarray(tgt), axis=1)
            j = int(np.argmin(dists))
            if dists[j] > MATCH_TOL:
                st["target_absent_in_frame"] += 1
                action[i] = "target_absent"
                continue
            mapping[i] = j
    else:
        for grp in group_junctions(tvs):
            if len(grp) < MIN_GROUP:
                st["skipped_small_group"] += len(grp)
                for i in grp: action[i] = "small_group"
                continue
            # 이미 정상 대역이면 절대 건드리지 않는다 (가장 중요한 안전장치)
            if band_ok([fe_of(i, i) for i in grp]):
                st["already_correct"] += len(grp)
                for i in grp: action[i] = "already_ok"
                continue
            m, resid = solve_group(heads, tvs, grp)
            if resid > MAX_ANG_RESID:
                st["skipped_unreliable"] += len(grp)
                for i in grp: action[i] = "unreliable"
                continue
            if not band_ok([fe_of(i, m[i]) for i in grp]):
                st["skipped_no_improvement"] += len(grp)
                for i in grp: action[i] = "no_improvement"
                continue
            mapping.update(m)


    # 원본 스냅샷 (제자리 교환이므로 반드시 먼저 떠 둔다)
    snap = [{k: b[k] for k in BBOX_FIELDS if k in b} for b in lights]

    fe_before = [facing_error(heads[i], yaws[i], tvs[i]) for i in range(len(lights))]
    src_id = [b["id"] for b in lights]

    changed = False
    for i, b in enumerate(lights):
        j = mapping.get(i, i) if mapping else i
        if j == i:
            action.setdefault(i, "already_ok" if (fe_before[i] is not None
                              and lo <= fe_before[i] <= hi)
                              else "unchanged_bad")
            if action[i] == "already_ok":
                st["already_correct_final"] += 1
            continue
        changed = True
        action[i] = "reassigned"
        st["reassigned"] += 1

        old_head_yaw = b["rotation"][2]
        new_head_yaw = snap[j]["rotation"][2]

        # trigger_volume_rotation 은 bbox.rotation + actor.tv.rotation 이므로
        # 잘못된 bbox 성분을 빼고 올바른 것을 더해 재구성한다
        if "trigger_volume_rotation" in b:
            tvr = b["trigger_volume_rotation"]
            b["trigger_volume_rotation"] = [
                (tvr[0] - b["rotation"][0] + snap[j]["rotation"][0]) % 360,
                (tvr[1] - b["rotation"][1] + snap[j]["rotation"][1]) % 360,
                (tvr[2] - old_head_yaw + new_head_yaw) % 360,
            ]

        for k in BBOX_FIELDS:
            if k in snap[j]:
                b[k] = snap[j][k]

    # 복구 후 검증
    for i, b in enumerate(lights):
        fe = facing_error(b["location"][:2], b["rotation"][2],
                          b["trigger_volume_location"][:2])
        if fe is not None:
            st["after_ok" if lo <= fe <= hi else "after_bad"] += 1
        if rows is not None:
            j = mapping.get(i, i) if mapping else i
            rows.append({
                "clip": clip,
                "frame": frame,
                "tl_id": b["id"],
                "affects_ego_before": int(bool(b.get("affects_ego"))),
                "affects_ego_after": int(bool(b.get("affects_ego"))),
                "state": b.get("state"),
                "dist_ego": round(b.get("distance", float("nan")), 2),
                "group_size": gsize.get(i, 0),
                "ang_resid": (None if gresid.get(i) is None
                              or gresid.get(i) != gresid.get(i)
                              else round(gresid[i], 1)),
                "fac_err_before": (None if fe_before[i] is None
                                   else round(fe_before[i], 1)),
                "fac_err_after": round(fe, 1) if fe is not None else None,
                "ok_before": int(fe_before[i] is not None
                                 and lo <= fe_before[i] <= hi),
                "ok_after": int(fe is not None and FACE_OK_LO <= fe <= FACE_OK_HI),
                "action": action.get(i, "unchanged"),
                "bbox_taken_from": src_id[j] if j != i else "",
            })

    return st, changed


# ------------------------------------------------------- affects_ego 재계산

def get_ego_pose(anno):
    """(position_xy, forward_xy, left_xy) 반환."""
    ego = next((b for b in anno.get("bounding_boxes", [])
                if b.get("class") == "ego_vehicle"), None)
    if ego is None:
        return None
    pos = np.asarray(ego["location"][:2], dtype=float)
    yaw = math.radians(float(ego["rotation"][2]))
    forward = np.array([math.cos(yaw), math.sin(yaw)])
    left = np.array([-forward[1], forward[0]])
    return pos, forward, left


def score_lights(anno):
    """bbox 복구가 끝난 신호등에 대해 ego 연관 판정 지표를 계산."""
    pose = get_ego_pose(anno)
    if pose is None:
        return None
    ego_pos, forward, left = pose
    output = []
    for box in anno.get("bounding_boxes", []):
        if box.get("class") != "traffic_light":
            continue
        if "trigger_volume_location" not in box or "location" not in box:
            continue

        pole = np.asarray(box["location"][:2], dtype=float)
        trigger = np.asarray(box["trigger_volume_location"][:2], dtype=float)
        direction = pole - trigger
        distance = float(np.linalg.norm(direction))
        if distance < 1e-3:
            continue
        travel = direction / distance
        relative = trigger - ego_pos
        output.append({
            "box": box,
            "id": box.get("id"),
            "cos": float(forward @ travel),
            "lon": float(relative @ forward),
            "lat": float(relative @ left),
            "was": bool(box.get("affects_ego", False)),
        })
    return output


def pick_affecting(candidates):
    """진행 방향, 차로, 전방 거리 조건을 만족하는 신호등 ID를 고른다."""
    valid = [c for c in candidates
             if c["cos"] >= COS_MIN
             and abs(c["lat"]) <= LAT_MAX
             and LON_MIN <= c["lon"] <= LON_MAX]
    if not valid:
        return None
    valid.sort(key=lambda c: (c["lon"], abs(c["lat"])))
    return valid[0]["id"]


def segment_frames(poses):
    """ego 위치가 크게 튀는 지점에서 클립을 접근 구간으로 분할."""
    segments, current = [], []
    previous = None
    for index, pose in enumerate(poses):
        if pose is None:
            if current:
                segments.append(current)
                current = []
            previous = None
            continue
        if previous is not None and np.linalg.norm(pose - previous) > VOTE_GAP:
            segments.append(current)
            current = []
        current.append(index)
        previous = pose
    if current:
        segments.append(current)
    return segments


def majority_vote(per_frame_ids, segments):
    """같은 접근 구간 안에서 선택된 신호등 ID를 다수결로 안정화."""
    voted = list(per_frame_ids)
    unstable = 0
    for segment in segments:
        values = [per_frame_ids[i] for i in segment
                  if per_frame_ids[i] is not None]
        if not values:
            continue
        winner, count = Counter(values).most_common(1)[0]
        if count < len(values):
            unstable += 1
        for i in segment:
            if per_frame_ids[i] is not None:
                voted[i] = winner
    return voted, unstable


def recompute_affects_ego(annotations):
    """클립 전체의 affects_ego를 제자리 수정한다.

    (통계, 프레임별 변경 여부, 프레임별 최종 {traffic_light id: bool})를 반환한다.
    """
    stats = Counter()
    scored = [score_lights(anno) if anno is not None else None
              for anno in annotations]
    poses = []
    for anno in annotations:
        pose = get_ego_pose(anno) if anno is not None else None
        poses.append(pose[0] if pose else None)

    raw_ids = [pick_affecting(c) if c else None for c in scored]
    voted_ids, unstable = majority_vote(raw_ids, segment_frames(poses))
    stats["unstable_segments"] = unstable
    changed_frames = []
    final_by_frame = []

    for candidates, new_id in zip(scored, voted_ids):
        frame_changed = False
        final = {}
        if candidates is None:
            changed_frames.append(False)
            final_by_frame.append(final)
            continue

        stats["frames"] += 1
        old = [c for c in candidates if c["was"]]
        old_id = old[0]["id"] if old else None
        if len(old) > 1:
            stats["multi_affects_ego"] += 1

        if new_id is None and old_id is None:
            stats["both_none"] += 1
        elif new_id == old_id:
            stats["agree"] += 1
        else:
            stats["disagree"] += 1
            if old_id is not None:
                previous = next(c for c in candidates if c["id"] == old_id)
                if previous["cos"] < -0.7:
                    stats["was_opposing"] += 1
                elif previous["cos"] < 0.3:
                    stats["was_perpendicular"] += 1
                elif abs(previous["lat"]) > LAT_MAX:
                    stats["was_wrong_lane"] += 1
            if new_id is not None:
                by_distance = sorted(
                    candidates,
                    key=lambda c: c["box"].get("distance", 1e9))
                rank = next((i for i, c in enumerate(by_distance, start=1)
                             if c["id"] == new_id), None)
                if rank is not None:
                    stats[f"gt_distance_rank_{rank}"] += 1

        for candidate in candidates:
            value = new_id is not None and candidate["id"] == new_id
            final[candidate["id"]] = value
            if candidate["was"] != value:
                stats["changed_entries"] += 1
                frame_changed = True
            candidate["box"]["affects_ego"] = value
        if frame_changed:
            stats["changed_frames"] += 1
        changed_frames.append(frame_changed)
        final_by_frame.append(final)

    return stats, changed_frames, final_by_frame


# ---------------------------------------------------------------- main

DETAIL_COLS = ["clip", "frame", "tl_id", "affects_ego_before",
               "affects_ego_after", "state", "dist_ego",
               "group_size", "ang_resid", "fac_err_before", "fac_err_after",
               "ok_before", "ok_after", "band", "action", "bbox_taken_from"]


def write_csv(path, rows, affects_by_clip=None):
    """엔트리별 상세 CSV 와 클립별 요약 CSV 를 쓴다."""
    import csv
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=DETAIL_COLS)
        w.writeheader()
        w.writerows(rows)

    agg = {}
    bbox_changed_frames = {}
    for r in rows:
        a = agg.setdefault(r["clip"], Counter())
        a["entries"] += 1
        a["ok_before"] += r["ok_before"]
        a["ok_after"] += r["ok_after"]
        a["action_" + r["action"]] += 1
        if r["action"] == "reassigned":
            bbox_changed_frames.setdefault(r["clip"], set()).add(r["frame"])
        a["frames_seen"] = a["frames_seen"]
        if r["affects_ego_after"]:
            a["affects_ego_entries"] += 1
            a["affects_ego_ok_before"] += r["ok_before"]
            a["affects_ego_ok_after"] += r["ok_after"]

    affects_by_clip = affects_by_clip or {}
    for clip, stats in affects_by_clip.items():
        a = agg.setdefault(clip, Counter())
        for key, value in stats.items():
            a["affects_ego_" + key] = value

    actions = sorted({"action_" + r["action"] for r in rows}
                     | {"action_reassigned"})
    cols = (["clip", "entries", "ok_before", "ok_after", "rate_before",
             "rate_after", "bbox_changed_frames", "affects_ego_entries",
             "affects_ego_ok_before",
             "affects_ego_ok_after", "affects_ego_changed_frames",
             "affects_ego_changed_entries", "affects_ego_agree",
             "affects_ego_disagree", "affects_ego_multi_affects_ego",
             "affects_ego_unstable_segments"] + actions)
    spath = os.path.splitext(path)[0] + "_summary.csv"
    with open(spath, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for clip, a in sorted(agg.items()):
            n = max(a["entries"], 1)
            row = {"clip": clip, "entries": a["entries"],
                   "ok_before": a["ok_before"], "ok_after": a["ok_after"],
                   "rate_before": round(a["ok_before"] / n, 4),
                   "rate_after": round(a["ok_after"] / n, 4),
                   "bbox_changed_frames": len(bbox_changed_frames.get(clip, set())),
                   "affects_ego_entries": a["affects_ego_entries"],
                   "affects_ego_ok_before": a["affects_ego_ok_before"],
                   "affects_ego_ok_after": a["affects_ego_ok_after"],
                   "affects_ego_changed_frames": a["affects_ego_changed_frames"],
                   "affects_ego_changed_entries": a["affects_ego_changed_entries"],
                   "affects_ego_agree": a["affects_ego_agree"],
                   "affects_ego_disagree": a["affects_ego_disagree"],
                   "affects_ego_multi_affects_ego": a["affects_ego_multi_affects_ego"],
                   "affects_ego_unstable_segments": a["affects_ego_unstable_segments"]}
            for k in actions:
                row[k] = a[k]
            w.writerow(row)
    print(f"[csv] {path}  ({len(rows)} rows)", file=sys.stderr)
    print(f"[csv] {spath}  ({len(agg)} clips)", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", default=None, help="수정본을 쓸 별도 경로")
    ap.add_argument("--apply", action="store_true", help="원본 덮어쓰기 (.bak 백업)")
    ap.add_argument("--global", dest="glob", action="store_true", default=True,
                    help="데이터셋 전체를 월드 좌표로 합의 (기본값)")
    ap.add_argument("--per-clip", dest="glob", action="store_false",
                    help="클립 단위로만 합의")
    ap.add_argument("--csv", default=None,
                    help="엔트리별 진단 CSV 경로 (요약본은 _summary.csv 로 함께 저장)")
    ap.add_argument(
        "--bbox-only",
        action="store_true",
        help=(
            "bbox permutation과 trigger_volume_rotation만 복구하고 "
            "affects_ego는 원본 값을 유지"
        ),
    )
    ap.add_argument(
        "--clip-list",
        default=None,
        help=(
            "newline으로 구분된 flat clip 이름 목록. 지정하면 --root 전체 대신 "
            "목록에 있는 <root>/<clip>/anno만 처리"
        ),
    )
    args = ap.parse_args()
    if args.apply and args.out:
        ap.error("--apply와 --out은 동시에 사용할 수 없습니다")

    total = Counter()
    affects_total = Counter()
    frames = files_changed = bbox_changed_frames = clips = 0
    affects_by_clip = {}

    clip_names = None
    if args.clip_list:
        with open(args.clip_list, encoding="utf-8") as file:
            clip_names = [line.strip() for line in file if line.strip()]
        if not clip_names:
            ap.error("--clip-list가 비어 있습니다")
        if len(clip_names) != len(set(clip_names)):
            ap.error("--clip-list에 중복 clip이 있습니다")
        invalid = [
            name
            for name in clip_names
            if name in {".", ".."}
            or os.path.isabs(name)
            or "/" in name
            or "\\" in name
        ]
        if invalid:
            ap.error(f"--clip-list에 안전하지 않은 이름이 있습니다: {invalid[:5]}")
        clip_names.sort()

    try:
        all_clips = list(walk_clips(args.root, clip_names))
    except (OSError, ValueError) as error:
        ap.error(str(error))
    rows = [] if args.csv else None

    # ===== pass 0: 타운별 정상 facing error 대역 자동 산출 =====
    samples = {}
    for _, paths in all_clips:
        for path in paths[::CALIB_STRIDE]:
            try:
                anno = load(path)
            except Exception:
                continue
            sm = calib_samples(anno)
            if sm:
                samples.setdefault(town_of(path, args.root), []).extend(sm)
    pooled = [v for vs in samples.values() for v in vs]
    gband = ((float(np.median(pooled)) - BAND_HALF, float(np.median(pooled)) + BAND_HALF)
             if len(pooled) >= CALIB_MIN_N else (FACE_OK_LO, FACE_OK_HI))
    bands = {}
    print("[pass 0] 타운별 정상 facing error 대역", file=sys.stderr)
    for t, vs in sorted(samples.items()):
        if len(vs) >= CALIB_MIN_N:
            med = float(np.median(vs))
            bands[t] = (med - BAND_HALF, med + BAND_HALF)
            note = ""
        else:
            bands[t] = gband
            note = "  (표본 부족 -> 전역 대역)"
        print("   %-10s n=%-7d median=%5.1f  band=%.0f~%.0f%s"
              % (t, len(vs), float(np.median(vs)) if vs else float("nan"),
                 bands[t][0], bands[t][1], note), file=sys.stderr)

    def band_for(path):
        return bands.get(town_of(path, args.root), gband)


    # ===== pass 1: 데이터셋 전체를 스캔해 월드 좌표 키로 투표 =====
    votes = {}
    if args.glob:
        for _, paths in all_clips:
            for path in paths:
                try:
                    anno = load(path)
                except Exception:
                    continue
                for k, v in solve_frame_world(anno, band_for(path)).items():
                    votes.setdefault(k, Counter())[v] += 1
        consensus = {}
        for k, cnt in votes.items():
            tgt, n = cnt.most_common(1)[0]
            if n < sum(cnt.values()):
                total["disputed_ids"] += 1
            consensus[k] = tgt
        total["consensus_ids"] = len(consensus)
        print(f"[pass 1] 전역 합의 확보: {len(consensus)} junction-approach", file=sys.stderr)
    else:
        consensus = None

    # ===== pass 2: bbox 복구 -> affects_ego 재계산 -> 프레임당 한 번 저장 =====
    for clipdir, paths in all_clips:
        clips += 1
        clip_rel = os.path.relpath(clipdir, args.root)
        forced = consensus
        items = []
        for path in paths:
            try:
                items.append((path, load(path)))
            except Exception as exc:
                print(f"[skip] {path}: {exc}", file=sys.stderr)
                items.append((path, None))

        if forced is None:                       # 클립 단위 폴백
            local = {}
            for path, anno in items:
                if anno is None:
                    continue
                for k, v in solve_frame_world(anno, band_for(path)).items():
                    local.setdefault(k, Counter())[v] += 1
            forced = {k: c.most_common(1)[0][0] for k, c in local.items()}
            total["consensus_ids"] += len(forced)

        clip_row_start = len(rows) if rows is not None else 0
        bbox_changed = []
        for path, anno in items:
            if anno is None:
                bbox_changed.append(False)
                continue
            frames += 1
            st, changed = fix_frame(
                anno, forced=forced or None, rows=rows,
                clip=clip_rel,
                frame=os.path.splitext(os.path.basename(path))[0],
                band=band_for(path))
            total.update(st)
            if changed:
                bbox_changed_frames += 1
            bbox_changed.append(changed)

        if args.bbox_only:
            affects_stats = Counter()
            affects_changed = [False] * len(items)
            final_by_frame = []
            for _, anno in items:
                final = {}
                if anno is not None:
                    final = {
                        box.get("id"): bool(box.get("affects_ego", False))
                        for box in anno.get("bounding_boxes", [])
                        if box.get("class") == "traffic_light"
                    }
                final_by_frame.append(final)
        else:
            affects_stats, affects_changed, final_by_frame = recompute_affects_ego(
                [anno for _, anno in items])
        affects_total.update(affects_stats)
        affects_by_clip[clip_rel] = affects_stats

        if rows is not None:
            final_lookup = {}
            for (path, _), final in zip(items, final_by_frame):
                frame = os.path.splitext(os.path.basename(path))[0]
                for tl_id, value in final.items():
                    final_lookup[(frame, tl_id)] = int(value)
            for row in rows[clip_row_start:]:
                row["affects_ego_after"] = final_lookup.get(
                    (row["frame"], row["tl_id"]), row["affects_ego_before"])

        for (path, anno), bbox_did_change, affects_did_change in zip(
                items, bbox_changed, affects_changed):
            if anno is None:
                continue
            if bbox_did_change or affects_did_change:
                files_changed += 1
            if not (args.apply or args.out):
                continue
            if args.out:
                rel = os.path.relpath(path, args.root)
                dst = os.path.join(args.out, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
            else:
                dst = path
                if not os.path.exists(dst + ".bak"):
                    shutil.copy2(dst, dst + ".bak")
            dump(dst, anno)

    if rows is not None:
        write_csv(args.csv, rows, affects_by_clip)

    b_ok, b_bad = total["before_ok"], total["before_bad"]
    a_ok, a_bad = total["after_ok"], total["after_bad"]
    b_tot = max(b_ok + b_bad, 1)

    print("\n" + "=" * 52)
    print(f"clips                 : {clips}")
    print(f"frames                : {frames}")
    print(f"frames with any change: {files_changed}")
    print(f"  bbox changed frames : {bbox_changed_frames}")
    print(f"  affects_ego frames  : {affects_total['changed_frames']}")
    if args.bbox_only:
        print("  affects_ego mode    : preserved (--bbox-only)")
    print("-" * 52)
    print(f"facing error 정상 (복구 전) : {b_ok} / {b_ok + b_bad}"
          f"  ({100.0 * b_ok / b_tot:.1f}%)")
    if a_ok + a_bad:
        print(f"facing error 정상 (복구 후) : {a_ok} / {a_ok + a_bad}"
              f"  ({100.0 * a_ok / max(a_ok + a_bad, 1):.1f}%)")
    print("-" * 52)
    print(f"재배정된 엔트리         : {total['reassigned']}")
    print(f"원래 맞았던 엔트리      : {total['already_correct']}")
    print(f"그룹이 작아 건너뜀      : {total['skipped_small_group']}")
    print(f"각도 잔차 커서 건너뜀   : {total['skipped_unreliable']}")
    print(f"개선 안 되어 건너뜀     : {total['skipped_no_improvement']}")
    print(f"합의 없음               : {total['no_consensus']}")
    print(f"대상이 프레임에 없음    : {total['target_absent_in_frame']}")
    print(f"합의 불일치             : {total['disputed_ids']} / {total['consensus_ids']}")
    print("-" * 52)
    print(f"affects_ego 일치        : {affects_total['agree']}")
    print(f"affects_ego 둘 다 없음  : {affects_total['both_none']}")
    print(f"affects_ego 불일치      : {affects_total['disagree']}")
    print(f"affects_ego 변경 엔트리 : {affects_total['changed_entries']}")
    print(f"  직교 방향 오선택      : {affects_total['was_perpendicular']}")
    print(f"  대향 방향 오선택      : {affects_total['was_opposing']}")
    print(f"  다른 차로 오선택      : {affects_total['was_wrong_lane']}")
    print(f"affects_ego 중복        : {affects_total['multi_affects_ego']}")
    print(f"접근 구간 내 불안정     : {affects_total['unstable_segments']}")
    for key in sorted(k for k in affects_total if k.startswith("gt_distance_rank_")):
        print(f"  선택 신호가 거리 {key.split('_')[-1]}순위 : {affects_total[key]}")
    print("=" * 52)
    if not (args.apply or args.out):
        print("\n감사 모드입니다. 수정하려면 --apply 또는 --out 을 주세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
