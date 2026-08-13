#!/usr/bin/env python3
"""Safely fill two approved Weak depth streams from the preserved patch root."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .quality_contract import canonical_sha256, read_json, sha256_file, write_json_atomic
except ImportError:
    from quality_contract import canonical_sha256, read_json, sha256_file, write_json_atomic


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONTRACT = SCRIPT_DIR / "depth_patch_contract_v1.json"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _names_sha256(names: Sequence[str]) -> str:
    payload = "" if not names else "\n".join(sorted(names)) + "\n"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _annotation_frame_names(clip_dir: Path) -> list[str]:
    annotation_dir = clip_dir / "anno"
    if not annotation_dir.is_dir():
        raise ValueError(f"annotation directory is missing: {annotation_dir}")
    numbers = []
    for path in annotation_dir.iterdir():
        if not path.is_file():
            continue
        name = path.name
        stem = name[:-8] if name.endswith(".json.gz") else name[:-5] if name.endswith(".json") else ""
        if stem.isdigit():
            numbers.append(int(stem))
    numbers.sort()
    if numbers != list(range(len(numbers))):
        raise ValueError(f"annotation frames are not contiguous in {annotation_dir}")
    return [f"{number:05d}.png" for number in numbers]


def _png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as file:
        header = file.read(24)
    if len(header) != 24 or header[:8] != PNG_SIGNATURE or header[12:16] != b"IHDR":
        raise ValueError(f"invalid PNG header: {path}")
    width, height = struct.unpack(">II", header[16:24])
    if width <= 0 or height <= 0:
        raise ValueError(f"invalid PNG dimensions: {path}")
    return width, height


def _stream_preflight(
    weak_root: Path, patch_root: Path, row: Mapping[str, Any]
) -> dict[str, Any]:
    clip = str(row["clip"])
    stream = str(row["stream"])
    canonical_dir = weak_root / clip / "camera" / stream
    patch_dir = patch_root / clip / "camera" / stream
    if not canonical_dir.is_dir() or not patch_dir.is_dir():
        raise ValueError(f"canonical or patch stream directory is missing: {clip}/{stream}")
    expected = _annotation_frame_names(weak_root / clip)
    if len(expected) != int(row["expected_frames"]):
        raise ValueError(f"annotation count differs from contract: {clip}/{stream}")
    patch_names = sorted(path.name for path in patch_dir.iterdir() if path.is_file())
    canonical_names = sorted(path.name for path in canonical_dir.iterdir() if path.is_file())
    expected_set = set(expected)
    if set(patch_names) != expected_set:
        raise ValueError(f"patch inventory differs from annotation: {clip}/{stream}")
    if not set(canonical_names) <= expected_set:
        raise ValueError(f"canonical stream contains unexpected files: {clip}/{stream}")
    missing = sorted(expected_set - set(canonical_names))
    if len(missing) not in {0, int(row["expected_missing_before"])}:
        raise ValueError(f"canonical missing count is neither pre- nor post-state: {clip}/{stream}")
    if missing and _names_sha256(missing) != row["missing_names_sha256"]:
        raise ValueError(f"canonical missing-name hash differs from contract: {clip}/{stream}")
    files = []
    for name in expected:
        patch_path = patch_dir / name
        width, height = _png_dimensions(patch_path)
        patch_sha = sha256_file(patch_path)
        canonical_path = canonical_dir / name
        canonical_sha = sha256_file(canonical_path) if canonical_path.is_file() else None
        if canonical_sha is not None and canonical_sha != patch_sha:
            raise ValueError(f"existing canonical file differs from patch: {canonical_path}")
        files.append(
            {
                "name": name,
                "patch_sha256": patch_sha,
                "canonical_sha256_before": canonical_sha,
                "width": width,
                "height": height,
            }
        )
    dimensions = sorted({(item["width"], item["height"]) for item in files})
    return {
        "clip": clip,
        "stream": stream,
        "canonical_dir": str(canonical_dir),
        "patch_dir": str(patch_dir),
        "expected_frames": len(expected),
        "canonical_frames_before": len(canonical_names),
        "missing_before": len(missing),
        "missing_names_sha256": _names_sha256(missing) if missing else None,
        "dimensions": [list(value) for value in dimensions],
        "files": files,
    }


def _copy_exclusive(source: Path, destination: Path) -> None:
    with source.open("rb") as source_file, destination.open("xb") as destination_file:
        while True:
            chunk = source_file.read(1024 * 1024)
            if not chunk:
                break
            destination_file.write(chunk)
        destination_file.flush()
        os.fsync(destination_file.fileno())


def materialize(
    weak_root: Path,
    patch_root: Path,
    contract_path: Path,
    output_manifest: Path | None,
    *,
    apply: bool,
) -> dict[str, Any]:
    contract = read_json(contract_path)
    if not isinstance(contract, dict) or contract.get("schema_version") != 1:
        raise ValueError("depth patch contract must have schema_version=1")
    if contract.get("policy") != "copy_missing_only_no_overwrite_preserve_patch":
        raise ValueError("unsupported depth patch policy")
    streams = contract.get("streams")
    if not isinstance(streams, list) or len(streams) != 2:
        raise ValueError("depth patch v1 contract must contain exactly two streams")
    preflight = [_stream_preflight(weak_root, patch_root, row) for row in streams]
    missing_total = sum(int(row["missing_before"]) for row in preflight)
    if apply:
        if output_manifest is None:
            raise ValueError("--output-manifest is required with --apply")
        if output_manifest.exists():
            raise FileExistsError(f"output manifest already exists: {output_manifest}")
        unwritable = [
            row["canonical_dir"]
            for row in preflight
            if int(row["missing_before"]) > 0
            and not os.access(row["canonical_dir"], os.W_OK)
        ]
        if unwritable:
            raise PermissionError(
                "canonical stream is not writable; no files were copied: "
                + ", ".join(unwritable)
            )
        copied = 0
        for stream in preflight:
            canonical_dir = Path(stream["canonical_dir"])
            patch_dir = Path(stream["patch_dir"])
            for file_row in stream["files"]:
                if file_row["canonical_sha256_before"] is not None:
                    file_row["action"] = "existing_identical"
                    continue
                source = patch_dir / file_row["name"]
                destination = canonical_dir / file_row["name"]
                _copy_exclusive(source, destination)
                copied_sha = sha256_file(destination)
                if copied_sha != file_row["patch_sha256"]:
                    raise OSError(f"copied file hash mismatch: {destination}")
                file_row["action"] = "copied_missing"
                file_row["canonical_sha256_after"] = copied_sha
                copied += 1
        verified = [_stream_preflight(weak_root, patch_root, row) for row in streams]
        if any(int(row["missing_before"]) != 0 for row in verified):
            raise OSError("post-copy verification still reports missing files")
        status = "already_complete" if copied == 0 else "materialized"
    else:
        copied = 0
        verified = []
        status = "check_only"
    result = {
        "schema_version": 1,
        "status": status,
        "policy": contract["policy"],
        "checked_at": _utc_now(),
        "weak_root": str(weak_root),
        "patch_root": str(patch_root),
        "patch_preserved": True,
        "contract": {"path": str(contract_path), "sha256": sha256_file(contract_path)},
        "preflight": preflight,
        "missing_total_before": missing_total,
        "copied_total": copied,
        "postflight": verified,
    }
    result["manifest_sha256"] = canonical_sha256(result)
    if apply:
        assert output_manifest is not None
        write_json_atomic(output_manifest, result)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weak-root", required=True, type=Path)
    parser.add_argument("--patch-root", required=True, type=Path)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--output-manifest", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    result = materialize(
        args.weak_root.expanduser().resolve(),
        args.patch_root.expanduser().resolve(),
        args.contract.expanduser().resolve(),
        args.output_manifest.expanduser().resolve() if args.output_manifest else None,
        apply=args.apply,
    )
    print(
        json.dumps(
            {
                "status": result["status"],
                "missing_total_before": result["missing_total_before"],
                "copied_total": result["copied_total"],
                "manifest_sha256": result["manifest_sha256"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
