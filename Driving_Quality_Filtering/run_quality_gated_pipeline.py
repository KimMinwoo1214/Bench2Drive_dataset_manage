#!/usr/bin/env python3
"""Run exactly one explicitly selected stage of the quality-gated production flow."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

try:
    from .quality_contract import canonical_sha256, read_json, sha256_file, write_json_atomic
except ImportError:
    from quality_contract import canonical_sha256, read_json, sha256_file, write_json_atomic


SCRIPT_DIR = Path(__file__).resolve().parent
BENCH_ROOT = SCRIPT_DIR.parent
AUDIT = SCRIPT_DIR / "audit_expert_driving.py"
CLASSIFIER = SCRIPT_DIR / "classify_quality.py"
EVIDENCE = SCRIPT_DIR / "render_review_evidence.py"
SCENARIO_PIPELINE = BENCH_ROOT / "Scenario_Filtering" / "run_scenario_pipeline.py"
STAGES = (
    "quality-audit", "quality-classify", "quality-evidence",
    "relabel-base", "relabel-weak", "relabel-check",
    "clip-union", "clip-union-check", "pkl-original", "pkl-corrected",
    "validate-pkls",
)
# Phase 3 only has to rewrite traffic-light annotations. Review evidence is
# rendered separately in Phase 2.5-E, and the filtered PKLs are built by the
# hardened converter in Phase 5, so every rendering and PKL path stays off.
RELABEL_HEADLESS_FLAGS = (
    "--no-visualization", "--no-video", "--no-vector-map",
    "--no-vad-vector-gt", "--no-build-pkl",
)


def _release_contract(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "release": "production_1329_corrected_v1",
        "parent_split": {
            "path": str(args.manifest),
            "sha256": sha256_file(args.manifest),
        },
        "base_root": str(args.base_root),
        "weak_root": str(args.weak_root),
        "map_root": str(args.map_root),
        "bench_root": str(BENCH_ROOT),
        "internship_root": str(args.internship_root),
        "stage_policy": "one_explicit_stage_per_invocation",
    }


def _ensure_release(args: argparse.Namespace) -> None:
    release = args.release_root
    contract_path = release / "release_contract.json"
    expected = _release_contract(args)
    if release.exists():
        if not contract_path.is_file():
            raise ValueError(f"existing release root has no contract; refusing reuse: {release}")
        if read_json(contract_path) != expected:
            raise ValueError(f"existing release contract differs; refusing reuse: {contract_path}")
    else:
        release.mkdir(parents=True)
        write_json_atomic(contract_path, expected)
    for name in ("quality_gate", "relabel", "pkl", "logs"):
        (release / name).mkdir(exist_ok=True)


def _completed_classification(args: argparse.Namespace) -> Path:
    directory = args.release_root / "quality_gate" / "classification_v1"
    completion = read_json(directory / "completion.json")
    if completion.get("status") != "completed" or completion.get("unresolved") != 0:
        raise ValueError("quality classification is not complete or has unresolved REVIEW clips")
    split = directory / "filtered_train_val_split.json"
    if not split.is_file():
        raise FileNotFoundError(f"filtered split is missing: {split}")
    return split


def _run_logged(args: argparse.Namespace, command: Sequence[str], label: str, cwd: Path) -> None:
    log = args.release_root / "logs" / f"{label}.log"
    if log.exists():
        raise ValueError(f"stage log already exists; refusing overwrite: {log}")
    with log.open("w", encoding="utf-8") as file:
        file.write("COMMAND=" + json.dumps(list(command), ensure_ascii=False) + "\n")
        file.flush()
        process = subprocess.Popen(
            list(command), cwd=str(cwd), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            sys.stdout.write(line)
            file.write(line)
        return_code = process.wait()
        file.write(f"EXIT_CODE={return_code}\n")
    if return_code != 0:
        raise RuntimeError(f"stage failed with exit={return_code}; log={log}")


def _scenario_command(args: argparse.Namespace, component: str, check: bool = False) -> list[str]:
    split = _completed_classification(args)
    input_root = args.base_root if component == "base" else args.weak_root
    command = [
        sys.executable, str(SCENARIO_PIPELINE), "--input", str(input_root),
        "--output", str(args.release_root / "relabel"),
        "--manifest", str(split), "--component", component,
        "--map-root", str(args.map_root),
        *RELABEL_HEADLESS_FLAGS,
    ]
    if args.review_approvals is not None:
        command.extend(["--review-approvals", str(args.review_approvals)])
    # --check-only writes nothing and the pipeline rejects it together with
    # --resume, so the two selections are exclusive.
    command.append("--check-only" if check else "--resume")
    return command


def _relabel_label(args: argparse.Namespace, component: str, check: bool) -> str:
    """Return a log label that separates first run, approval resume, and check."""
    if check:
        return f"relabel-check-{component}"
    if args.review_approvals is not None:
        return f"relabel-{component}-approved-resume"
    return f"relabel-{component}"


def _internship_module(args: argparse.Namespace, module: str, values: Sequence[str], label: str) -> None:
    command = [sys.executable, "-m", module, *values]
    _run_logged(args, command, label, args.internship_root)


def _require_file(path: Path, label: str) -> Path:
    if not path.is_file():
        raise FileNotFoundError(f"{label} is missing: {path}")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, choices=STAGES)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--base-root", required=True, type=Path)
    parser.add_argument("--weak-root", required=True, type=Path)
    parser.add_argument("--map-root", required=True, type=Path)
    parser.add_argument("--release-root", required=True, type=Path)
    parser.add_argument("--internship-root", required=True, type=Path)
    parser.add_argument("--quality-config", type=Path)
    parser.add_argument("--calibration-version", default="calibration_v1")
    parser.add_argument("--review-decisions", type=Path)
    parser.add_argument(
        "--collision-sweep",
        type=Path,
        help="collision sweep directory; its graded contacts replace the "
             "penetration thresholds as the evidence a reviewer rules on",
    )
    parser.add_argument("--review-approvals", type=Path)
    parser.add_argument("--clip-list", type=Path)
    parser.add_argument("--runtime-data-root", type=Path)
    parser.add_argument("--workers", type=int, default=8)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error("--workers must be positive")
    if re.fullmatch(r"calibration_v[1-9][0-9]*", args.calibration_version) is None:
        parser.error("--calibration-version must match calibration_vN")
    for field in ("manifest", "base_root", "weak_root", "map_root", "internship_root", "release_root"):
        value = getattr(args, field).expanduser().resolve()
        setattr(args, field, value)
    for field in (
        "quality_config", "review_decisions", "review_approvals",
        "clip_list", "runtime_data_root", "collision_sweep",
    ):
        value = getattr(args, field)
        if value is not None:
            setattr(args, field, value.expanduser().resolve())
    for path in (args.manifest, args.base_root, args.weak_root, args.map_root, args.internship_root):
        if not path.exists():
            parser.error(f"required path is missing: {path}")
    _ensure_release(args)
    calibration = args.release_root / "quality_gate" / args.calibration_version
    classification = args.release_root / "quality_gate" / "classification_v1"
    filtered_split_path = classification / "filtered_train_val_split.json"
    union = args.release_root / "pkl" / "clip_union_filtered_v1"
    original_info = args.release_root / "pkl" / "original_filtered_v1"
    corrected_info = args.release_root / "pkl" / "corrected_filtered_v1"

    if args.stage == "quality-audit":
        command = [
            sys.executable, str(AUDIT), "--manifest", str(args.manifest),
            "--base-root", str(args.base_root), "--weak-root", str(args.weak_root),
            "--map-root", str(args.map_root), "--output-dir", str(calibration),
            "--workers", str(args.workers),
        ]
        _run_logged(
            args, command, f"quality-audit-{args.calibration_version}", BENCH_ROOT
        )
    elif args.stage == "quality-classify":
        if args.quality_config is None:
            parser.error("quality-classify requires --quality-config")
        final = args.review_decisions is not None
        classification_output = (
            classification
            if final
            else args.release_root / "quality_gate" / f"classification_candidates_{args.calibration_version}"
        )
        command = [
            sys.executable, str(CLASSIFIER), "--audit-dir", str(calibration),
            "--manifest", str(args.manifest), "--config", str(args.quality_config),
            "--output-dir", str(classification_output),
        ]
        if args.collision_sweep is not None:
            command.extend(["--sweep-dir", str(args.collision_sweep)])
        if final:
            command.extend(["--decisions", str(args.review_decisions)])
        # The candidate pass and the decision-bound final pass write different
        # outputs, so they must not share a log label either.
        _run_logged(
            args, command,
            "quality-classify-{}-{}".format(
                "final" if final else "candidates", args.calibration_version
            ),
            BENCH_ROOT,
        )
    elif args.stage == "quality-evidence":
        if args.clip_list is None:
            parser.error("quality-evidence requires --clip-list")
        command = [
            sys.executable, str(EVIDENCE), "--audit-dir", str(calibration),
            "--manifest", str(args.manifest), "--base-root", str(args.base_root),
            "--weak-root", str(args.weak_root), "--map-root", str(args.map_root),
            "--clip-list", str(args.clip_list),
            "--output-dir", str(
                args.release_root / "quality_gate" / f"review_evidence_{args.calibration_version}"
            ),
        ]
        _run_logged(
            args, command, f"quality-evidence-{args.calibration_version}", BENCH_ROOT
        )
    elif args.stage in {"relabel-base", "relabel-weak"}:
        component = args.stage.split("-", 1)[1]
        _run_logged(
            args, _scenario_command(args, component),
            _relabel_label(args, component, check=False),
            BENCH_ROOT / "Scenario_Filtering",
        )
    elif args.stage == "relabel-check":
        for component in ("base", "weak"):
            _run_logged(
                args, _scenario_command(args, component, check=True),
                _relabel_label(args, component, check=True),
                BENCH_ROOT / "Scenario_Filtering",
            )
    elif args.stage in {"clip-union", "clip-union-check"}:
        split = _completed_classification(args)
        values = [
            "--base-root", str(args.base_root), "--weak-root", str(args.weak_root),
            "--split-file", str(split), "--output-root", str(union),
        ]
        if args.stage.endswith("check"):
            values.append("--check")
        _internship_module(args, "team_code.data.prepare_clip_union", values, args.stage)
    elif args.stage in {"pkl-original", "pkl-corrected"}:
        _require_file(union / ".clip_union_manifest.json", "clip union completion manifest")
        split = _completed_classification(args)
        source = "original" if args.stage == "pkl-original" else "corrected"
        output = original_info if source == "original" else corrected_info
        values = [
            "--data-root", str(union), "--split-file", str(split),
            "--annotation-source", source, "--path-prefix", "v1",
            "--map-root", str(args.map_root), "--output-dir", str(output),
            "--workers", str(args.workers),
        ]
        if source == "corrected":
            values.extend(["--corrected-root", str(args.release_root / "relabel")])
        _internship_module(args, "team_code.data.prepare_b2d_infos", values, args.stage)
    elif args.stage == "validate-pkls":
        if args.runtime_data_root is None:
            parser.error("validate-pkls requires --runtime-data-root")
        for info in (original_info, corrected_info):
            for name in ("train", "val"):
                _require_file(info / f"b2d_infos_{name}.pkl", f"{info.name} {name} PKL")
            _require_file(info / "b2d_map_infos.pkl", f"{info.name} map PKL")
        for source, info in (("original", original_info), ("corrected", corrected_info)):
            for split, peer in (("train", "val"), ("val", "train")):
                values = [
                    "--data-root", str(args.runtime_data_root),
                    "--info-file", str(info / f"b2d_infos_{split}.pkl"),
                    "--map-file", str(info / "b2d_map_infos.pkl"),
                    "--split-peer-info-file", str(info / f"b2d_infos_{peer}.pkl"),
                    "--stopline-normalization-metres", "15", "30",
                    "--output", str(info / f"{split}_validation_report.json"),
                ]
                if source == "corrected":
                    values.extend(
                        [
                            "--original-info-file",
                            str(original_info / f"b2d_infos_{split}.pkl"),
                            "--original-map-file",
                            str(original_info / "b2d_map_infos.pkl"),
                        ]
                    )
                _internship_module(
                    args, "team_code.data.validate_pkls", values,
                    f"validate-{source}-{split}",
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
