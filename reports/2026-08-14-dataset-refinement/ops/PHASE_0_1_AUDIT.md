# Base1000 + Weak329 corrected production — Phase 0/1 audit

- Recorded at: `2026-08-13T13:07:12+09:00`
- Scope completed: Phase 0 and Phase 1 only
- No `sbatch`, `srun`, `scancel`, `git reset`, `git clean`, or `rm` was run.
- No existing checkout was switched or pulled.
- No original annotation, existing PKL, symlink, relabel output, or active training output was modified.

## Phase 0 — current state

### Active training

- Relevant job: `6691|RUNNING|team2|partition-3090-intel|3090x4-1`
- Submit checkout: `/home/ailab/AILabSSD/99_Management/2026_internship/Inhyunseo/2026-Summer-Internship`
- Submit script: `/home/ailab/AILabSSD/99_Management/2026_internship/Inhyunseo/2026-Summer-Internship/examples/vad/slurm/03_train_vad_4gpu.sbatch`
- Submit checkout HEAD: `d727f8458e05d545370e161516ea0856c5324c5b` on `team_two`
- Submit checkout is protected and dirty: two modified tracked files and three untracked files were observed. It was not touched.
- Runtime Zoo copy: `/home/ailab/AILabSSD/99_Management/2026_internship/Inhyunseo/Bench2DriveZoo` (not a Git worktree)
- Runtime train data root: `/home/ailab/AILabSSD/99_Management/2026_internship/data/base`
- Actual train annotation in the dumped job config: `data/infos/b2d_infos_small_train_1329.pkl`
  - Host path: `/home/ailab/AILabSSD/99_Management/2026_internship/data/base/infos/b2d_infos_small_train_1329.pkl`
  - SHA-256: `82c551d9227af13041df99a195d4669bb98bbb6d0b667d873864c7a76cc7b8c6`
- Actual validation annotation in the dumped job config: `data/infos/b2d_infos_val.pkl`
  - Host symlink resolves to `/home/ailab/AILabDataset/03_Shared_Repository/2026-intern/Team_two/Bench2Drive_relabel/production_1329/infos/base1000_weak329_original_v1/b2d_infos_val.pkl`
- Current output: `/home/ailab/AILabSSD/99_Management/2026_internship/Inhyunseo/bench2drive/train-6691`
- Slurm log: `/home/ailab/AILabSSD/99_Management/2026_internship/Inhyunseo/2026-Summer-Internship/slurm-team2-6691.out`
- Runtime JSON log: `/home/ailab/AILabSSD/99_Management/2026_internship/Inhyunseo/bench2drive/train-6691/20260813_115739.log.json`
- Post-Phase-1 liveness evidence: job remained `RUNNING`; log advanced to `Epoch [1][350/1568]` at `2026-08-13 13:04:08+09:00`.

### Existing original production PKLs

Directory:

`/home/ailab/AILabDataset/03_Shared_Repository/2026-intern/Team_two/Bench2Drive_relabel/production_1329/infos/base1000_weak329_original_v1`

Recorded in the existing `SHA256SUMS` file:

| File | Bytes | SHA-256 |
|---|---:|---|
| `b2d_infos_train.pkl` | 3,420,722,165 | `645fb4b6f403e17f071bd7b852444e7b5a7a405280f39cc7efecf6e103ddb38b` |
| `b2d_infos_val.pkl` | 214,882,267 | `5699246eb61f5144ebcf77dc93cad5d98421edbb10ca204bf854d8475c53510b` |
| `b2d_map_infos.pkl` | 6,296,127,061 | `f8eca34b0793195b71f76f5467944d608529516541745312bcf841e328bc5035` |

- Existing validation reports: both `status=passed`, zero errors, zero train/val overlap.
- Train records: `311323`; val records: `18451`.
- Existing split metadata: train clips `1263`, val clips `66`, annotation source `original`.
- Existing split file SHA-256: `5e62d6392f10f9e1a57d2c675f725d5da55069579f8d8d4c149c20d52e6e59fa`.

### Existing requested checkouts before Phase 1

| Repository | HEAD | Branch | Dirty/untracked | Remote |
|---|---|---|---|---|
| Bench | `dd5c4c0d27e59048c60f89925e6bea6f6d560244` | `main` | none | `https://github.com/KimMinwoo1214/Bench2Drive_dataset_manage.git` |
| Internship | `51fa1fa340b933fafde0a4b0152b557dd7786b66` | `team_two` | none | `https://github.com/ailab-hanyang/2026-Summer-Internship.git` |

### Shared paths that must remain read-only

- Base root: `/home/ailab/AILabDataset/01_Open_Dataset/41_Bench2Drive/Bench2Drive/unzip_data`
- Weak root: `/home/ailab/AILabDataset/03_Shared_Repository/2026-intern/Team_two/Scenario`
- Map root: `/home/ailab/AILabDataset/01_Open_Dataset/41_Bench2Drive/Bench2Drive-Map`
- Existing clip wrapper: `/home/ailab/AILabDataset/03_Shared_Repository/2026-intern/Team_two/Bench2Drive_relabel/production_1329/clips`
- Existing original infos: `/home/ailab/AILabDataset/03_Shared_Repository/2026-intern/Team_two/Bench2Drive_relabel/production_1329/infos/base1000_weak329_original_v1`
- Current training workspace data links: `/home/ailab/AILabSSD/99_Management/2026_internship/data/base`

The new code worktrees do not overlap the active training checkout, Zoo copy, or output. A later corrected result must use a new versioned directory and must not reuse or overwrite any path above.

### Capacity

| Filesystem | Size | Used | Available | Use | Inodes |
|---|---:|---:|---:|---:|---|
| SSD `/dev/mapper/vg_8tb-lv_8tb` | 15T | 13T | 1.1T | 93% | 466,627,912 free; 5% used |
| NFS `10.0.0.1:/volume3/AILabDataset` | 110T | 102T | 8.3T | 93% | not reported by server (`0/0/0`) |

## Phase 1 — selected code

### Fetch and merge status

Bench:

- `git fetch --prune origin` succeeded.
- `origin/main` became `1238eac83b044cf17dc43785c66047929441c887`.
- `git merge-base --is-ancestor ed9711f origin/main` returned exit code `0`.
- Selected ref: `origin/main`.
- Selected commit: `1238eac83b044cf17dc43785c66047929441c887`.
- Merge subject: `Merge pull request #9 from KimMinwoo1214/minsung-production-1329`.
- Production commit: `ed9711f53a42308201691615b7a77d35889d747c` is the second parent of the merge.
- PR status by fetched Git history: merged. `origin/minsung-production-1329` is no longer present after prune.

Internship:

- The requested HTTPS `git fetch --prune origin` failed because no GitHub HTTPS credential was available.
- A one-command URL override still selected the configured HTTPS URL and failed; repository config was not changed.
- Fetch was completed from the same official repository using SSH and the explicit refspec:
  `git fetch --prune git@github.com:ailab-hanyang/2026-Summer-Internship.git '+refs/heads/*:refs/remotes/origin/*'`
- `origin/team_two` became `bf42647640aea186508a8b056b5572c5a2b380f0`.
- `git merge-base --is-ancestor d5621b1 origin/team_two` returned exit code `0`.
- Selected ref: `origin/team_two`.
- Selected commit: `bf42647640aea186508a8b056b5572c5a2b380f0`.
- Merge subject: `Merge pull request #6 from ailab-hanyang/data/production-1329-pkl`.
- Production commit: `d5621b1dd348228c50b196359fe65868e0ca40cf` is the second parent of the merge.
- PR status by fetched Git history: merged. `origin/data/production-1329-pkl` is no longer present after prune.

### Detached worktrees

| Repository | Path | HEAD | State |
|---|---|---|---|
| Bench | `/home/ailab/AILabSSD/99_Management/2026_internship/kimminseong/production_1329_v1_code/Bench2Drive_dataset_manage` | `1238eac83b044cf17dc43785c66047929441c887` | detached, clean, no untracked files |
| Internship | `/home/ailab/AILabSSD/99_Management/2026_internship/kimminseong/production_1329_v1_code/2026-Summer-Internship` | `bf42647640aea186508a8b056b5572c5a2b380f0` | detached, clean, no untracked files |

The original checkouts retained their original HEADs and branches. Fetch only made their local branches report behind the updated upstream (`Bench -14`, `Internship -10`); neither has working-tree changes or untracked files.

### Required files

All required files exist as regular files:

- Bench `Scenario_Filtering/production_contract.py`
- Bench `Scenario_Filtering/run_scenario_pipeline.py`
- Bench `Weak_Scenario_Mining/build_production_split.py`
- Bench `Weak_Scenario_Mining/data/bench2drive_base1000_weak329_train_val_split.json`
- Internship `team_code/data/prepare_clip_union.py`
- Internship `team_code/data/prepare_b2d_infos.py`
- Internship `team_code/data/VAD_TRAFFIC_LIGHT_PKL_GUIDE.md`

### Fixed split verification

- File: `/home/ailab/AILabSSD/99_Management/2026_internship/kimminseong/production_1329_v1_code/Bench2Drive_dataset_manage/Weak_Scenario_Mining/data/bench2drive_base1000_weak329_train_val_split.json`
- SHA-256: `448438d80b43886cd1d7d45730ee500033ae2c44e5b190ac9fcd5862f7433c65`
- Declared and actual counts: train `1262`, val `67`.
- Unique counts: train `1262`, val `67`; overlap `0`; union `1329`.
- Composition against current roots: Base train/val `950/50`; Weak train/val `312/17`.
- Weak root contains `329` matching clip directories.
- Base root has `1000` split-matching clip directories plus one additional top-level non-split directory named `data`; the split has no missing clip and no unknown clip.
- All five input hashes embedded in the split JSON were recomputed from the selected worktree and matched exactly.

## Command record

Primary state and safety commands:

```bash
id -un
squeue -u ailab -t RUNNING,PENDING -h -o '%i|%T|%j|%P|%N|%V|%S|%M|%l|%o'
scontrol show job -dd -o 6691
sed -n '1,260p' /home/ailab/AILabSSD/99_Management/2026_internship/Inhyunseo/2026-Summer-Internship/examples/vad/slurm/03_train_vad_4gpu.sbatch
sed -n '1,220p' /home/ailab/AILabSSD/99_Management/2026_internship/Inhyunseo/2026-Summer-Internship/examples/vad/scripts/common.sh
sed -n '1,220p' /home/ailab/AILabSSD/99_Management/2026_internship/Inhyunseo/2026-Summer-Internship/examples/vad/config/cluster.env
tail -n 180 /home/ailab/AILabSSD/99_Management/2026_internship/Inhyunseo/2026-Summer-Internship/slurm-team2-6691.out
git rev-parse HEAD
git branch --show-current
GIT_OPTIONAL_LOCKS=0 git status --porcelain=v2 --branch --untracked-files=all
git remote -v
git worktree list --porcelain
df -hP <workspace> <Base> <Weak> <Map> <result-root> <active-output>
df -iP <workspace> <Base> <Weak> <Map> <result-root> <active-output>
sha256sum /home/ailab/AILabDataset/03_Shared_Repository/2026-intern/Team_two/Bench2Drive_relabel/production_1329/splits/train_val_split.json
```

Phase 1 mutation and selection commands:

```bash
# Bench checkout
git fetch --prune origin
git merge-base --is-ancestor ed9711f origin/main

# Internship checkout: requested command, which failed for HTTPS auth
git fetch --prune origin

# Successful same-origin SSH fallback
git fetch --prune git@github.com:ailab-hanyang/2026-Summer-Internship.git '+refs/heads/*:refs/remotes/origin/*'
git merge-base --is-ancestor d5621b1 origin/team_two

mkdir /home/ailab/AILabSSD/99_Management/2026_internship/kimminseong/production_1329_v1_code

# Run from the existing Bench checkout
git worktree add --detach /home/ailab/AILabSSD/99_Management/2026_internship/kimminseong/production_1329_v1_code/Bench2Drive_dataset_manage origin/main

# Run from the existing Internship checkout
git worktree add --detach /home/ailab/AILabSSD/99_Management/2026_internship/kimminseong/production_1329_v1_code/2026-Summer-Internship origin/team_two

sha256sum Weak_Scenario_Mining/data/bench2drive_base1000_weak329_train_val_split.json
```

Read-only verification additionally used `stat`, `find -maxdepth`, `rg`, `sed`, `head`, `tail`, `sha256sum`, `git for-each-ref`, `git show`, `git cat-file`, and a Python JSON-only check. `jq` was attempted but is not installed. The first `squeue --me` attempt was rejected by this older Slurm client; the equivalent `squeue -u ailab` query was used.

## Phase boundary

- Phase 0: complete.
- Phase 1: complete.
- Current training impact: none observed.
- Next phase: technically possible, but not started.
- Blocker: none for proceeding. Operational note: the Internship checkout's configured HTTPS origin still lacks credentials; future fetches need valid HTTPS credentials or the recorded SSH form.
