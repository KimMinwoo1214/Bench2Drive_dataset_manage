#!/usr/bin/env python3
"""Build a read-only Weak root view whose two approved depth streams are complete.

The canonical Weak clips are owned by root on a shared NFS export, so the two
approved depth streams cannot be filled in place without dataset-owner rights.
The patch root already holds a complete copy of both streams, and every file it
shares with the canonical stream is byte-identical, so a symlink tree gives the
audit the same bytes it would have read from a repaired canonical dataset while
leaving the canonical dataset untouched.

Every clip becomes a symlink to its canonical directory. The two patched clips
instead become real directories of per-entry symlinks, so only the two approved
streams are redirected to the patch root and every other stream still resolves
to the canonical file.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .quality_contract import CLIP_PATTERN, canonical_sha256, read_json, sha256_file, write_json_atomic
except ImportError:
    from quality_contract import CLIP_PATTERN, canonical_sha256, read_json, sha256_file, write_json_atomic


DEFAULT_CONTRACT = Path(__file__).resolve().parent / "depth_patch_contract_v1.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip_names(weak_root: Path) -> list[str]:
    names = sorted(
        child.name for child in weak_root.iterdir()
        if child.is_dir() and CLIP_PATTERN.fullmatch(child.name)
    )
    if not names:
        raise ValueError(f"no production clips found under {weak_root}")
    return names


def _patched_streams(contract: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    if contract.get("schema_version") != 1:
        raise ValueError("depth patch contract must have schema_version=1")
    streams = contract.get("streams")
    if not isinstance(streams, list) or not streams:
        raise ValueError("depth patch contract has no streams")
    patched: dict[str, dict[str, Any]] = {}
    for row in streams:
        clip = str(row["clip"])
        if clip in patched:
            raise ValueError(f"contract patches the same clip twice: {clip}")
        patched[clip] = {"stream": str(row["stream"]), "expected_frames": int(row["expected_frames"])}
    return patched


def _link(source: Path, destination: Path) -> None:
    """Create one symlink, refusing to replace anything already present."""
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"refusing to replace an existing entry: {destination}")
    destination.symlink_to(source)


def _build_patched_clip(
    clip: str, weak_root: Path, patch_root: Path, output_root: Path, spec: Mapping[str, Any]
) -> dict[str, Any]:
    canonical_clip = weak_root / clip
    target_clip = output_root / clip
    target_clip.mkdir(parents=True)
    stream = str(spec["stream"])
    redirected: list[str] = []
    for entry in sorted(canonical_clip.iterdir(), key=lambda path: path.name):
        if entry.name != "camera":
            _link(entry, target_clip / entry.name)
            continue
        camera = target_clip / "camera"
        camera.mkdir()
        for sensor in sorted(entry.iterdir(), key=lambda path: path.name):
            if sensor.name == stream:
                patch_dir = patch_root / clip / "camera" / stream
                if not patch_dir.is_dir():
                    raise FileNotFoundError(f"patch stream is missing: {patch_dir}")
                _link(patch_dir, camera / sensor.name)
                redirected.append(sensor.name)
            else:
                _link(sensor, camera / sensor.name)
    if redirected != [stream]:
        raise ValueError(f"{clip}: expected to redirect exactly [{stream}], got {redirected}")
    return {"clip": clip, "redirected_stream": stream, "mode": "per_entry_symlinks"}


def _verify(
    output_root: Path, clips: Sequence[str], patched: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Confirm every clip resolves and each patched stream is frame-complete."""
    checked: list[dict[str, Any]] = []
    for clip in clips:
        clip_dir = output_root / clip
        if not clip_dir.is_dir():
            raise FileNotFoundError(f"clip does not resolve in the patched root: {clip_dir}")
        if not (clip_dir / "anno").is_dir():
            raise FileNotFoundError(f"anno does not resolve: {clip_dir / 'anno'}")
        if clip not in patched:
            continue
        stream = str(patched[clip]["stream"])
        expected = int(patched[clip]["expected_frames"])
        stream_dir = clip_dir / "camera" / stream
        names = sorted(path.name for path in stream_dir.iterdir() if path.is_file())
        if len(names) != expected:
            raise ValueError(
                f"{clip}/{stream}: resolved {len(names)} frames, contract expects {expected}"
            )
        checked.append(
            {
                "clip": clip,
                "stream": stream,
                "resolved_frames": len(names),
                "expected_frames": expected,
                "resolved_target": os.path.realpath(str(stream_dir)),
            }
        )
    return checked


def build(
    weak_root: Path,
    patch_root: Path,
    contract_path: Path,
    output_root: Path,
    *,
    check: bool,
) -> dict[str, Any]:
    contract = read_json(contract_path)
    patched = _patched_streams(contract)
    clips = _clip_names(weak_root)
    missing = sorted(set(patched) - set(clips))
    if missing:
        raise ValueError(f"contract patches clips that are not in the weak root: {missing}")

    if check:
        built: list[dict[str, Any]] = []
    else:
        if output_root.exists() and any(output_root.iterdir()):
            raise FileExistsError(f"output root is not empty: {output_root}")
        output_root.mkdir(parents=True, exist_ok=True)
        built = []
        for clip in clips:
            if clip in patched:
                built.append(_build_patched_clip(clip, weak_root, patch_root, output_root, patched[clip]))
            else:
                _link(weak_root / clip, output_root / clip)
        if sorted(path.name for path in output_root.iterdir()) != clips:
            raise ValueError("patched root entries do not match the canonical clip list")

    verified = _verify(output_root, clips, patched)
    result = {
        "schema_version": 1,
        "status": "checked" if check else "built",
        "policy": "symlink_overlay_no_canonical_write",
        "built_at": _utc_now(),
        "weak_root": str(weak_root),
        "patch_root": str(patch_root),
        "output_root": str(output_root),
        "contract": {"path": str(contract_path), "sha256": sha256_file(contract_path)},
        "clip_count": len(clips),
        "patched_clips": built,
        "verified_streams": verified,
    }
    result["manifest_sha256"] = canonical_sha256(result)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weak-root", required=True, type=Path)
    parser.add_argument("--patch-root", required=True, type=Path)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--output-manifest", type=Path)
    parser.add_argument(
        "--check", action="store_true", help="verify an existing patched root without building"
    )
    args = parser.parse_args(argv)
    result = build(
        args.weak_root.expanduser().resolve(),
        args.patch_root.expanduser().resolve(),
        args.contract.expanduser().resolve(),
        args.output_root.expanduser().resolve(),
        check=args.check,
    )
    if args.output_manifest is not None:
        manifest = args.output_manifest.expanduser().resolve()
        if manifest.exists():
            raise FileExistsError(f"output manifest already exists: {manifest}")
        write_json_atomic(manifest, result)
    print(
        json.dumps(
            {
                "status": result["status"],
                "clip_count": result["clip_count"],
                "verified_streams": result["verified_streams"],
                "manifest_sha256": result["manifest_sha256"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
