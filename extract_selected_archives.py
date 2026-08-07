#!/usr/bin/env python3
"""Extract the Bench2Drive archives selected by the weak-scenario pipeline.

The extractor is restartable: a completion marker is written only after an
archive reaches the end of its gzip stream successfully.  Re-running the
script skips archives with valid completion markers and retries interrupted
ones.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tarfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT_DIR = PROJECT_ROOT / "data"
DEFAULT_SELECTED_FILE = (
    PROJECT_ROOT
    / "Weak_Scenario_Mining"
    / "outputs"
    / "experiment_001"
    / "selected_additional_files.txt"
)
DEFAULT_MANIFEST = (
    PROJECT_ROOT
    / "Weak_Scenario_Mining"
    / "data"
    / "bench2drive_full+sup_13638.json"
)

PRINT_LOCK = threading.Lock()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract selected Bench2Drive .tar.gz archives safely and "
            "resume cleanly after interruption."
        )
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help=f"Archive directory (default: {DEFAULT_INPUT_DIR})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Extraction directory (default: same as --input-dir)",
    )
    parser.add_argument(
        "--selected-file",
        type=Path,
        default=DEFAULT_SELECTED_FILE,
        help="Text file containing one selected archive name per line",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="Manifest used to validate archive byte sizes",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=2,
        help="Number of archives to extract concurrently (default: 2)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Extract again even when a valid completion marker exists",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs and show work without extracting",
    )
    return parser.parse_args()


def archive_root(filename: str) -> str:
    suffix = ".tar.gz"
    if not filename.endswith(suffix):
        raise ValueError(f"Expected a {suffix} archive, got: {filename}")
    return filename[: -len(suffix)]


def load_inputs(
    selected_file: Path,
    manifest_file: Path,
) -> tuple[list[str], dict[str, Any]]:
    selected = [
        line.strip()
        for line in selected_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(selected) != len(set(selected)):
        raise ValueError(f"Duplicate archive names in {selected_file}")

    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    missing = [name for name in selected if name not in manifest]
    if missing:
        preview = ", ".join(missing[:5])
        raise KeyError(f"{len(missing)} selected archives missing from manifest: {preview}")
    return selected, manifest


def validate_member(member: tarfile.TarInfo, expected_root: str) -> None:
    """Reject paths and entry types that can escape or mutate the output tree."""
    path = PurePosixPath(member.name)
    parts = tuple(part for part in path.parts if part not in ("", "."))

    if path.is_absolute() or not parts or ".." in parts:
        raise ValueError(f"Unsafe archive path: {member.name!r}")
    if parts[0] != expected_root:
        raise ValueError(
            f"Unexpected top-level path {parts[0]!r}; expected {expected_root!r}"
        )
    if member.issym() or member.islnk():
        raise ValueError(f"Links are not allowed in dataset archives: {member.name!r}")
    if not (member.isdir() or member.isfile()):
        raise ValueError(f"Unsupported archive entry type: {member.name!r}")


def marker_path(marker_dir: Path, filename: str) -> Path:
    return marker_dir / f"{filename}.json"


def marker_is_valid(
    marker: Path,
    archive: Path,
    extracted_root: Path,
    expected_size: int,
) -> bool:
    if not marker.is_file() or not extracted_root.is_dir():
        return False
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (
        payload.get("archive") == archive.name
        and payload.get("archive_size") == expected_size
        and archive.is_file()
        and archive.stat().st_size == expected_size
    )


def write_marker(marker: Path, filename: str, expected_size: int) -> None:
    payload = {
        "archive": filename,
        "archive_size": expected_size,
        "status": "complete",
    }
    temporary = marker.with_name(f".{marker.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(marker)


def extract_one(
    filename: str,
    expected_size: int,
    input_dir: Path,
    output_dir: Path,
    marker_dir: Path,
    force: bool,
) -> str:
    archive = input_dir / filename
    root_name = archive_root(filename)
    extracted_root = output_dir / root_name
    marker = marker_path(marker_dir, filename)

    if not archive.is_file():
        raise FileNotFoundError(f"Archive not found: {archive}")
    actual_size = archive.stat().st_size
    if actual_size != expected_size:
        raise ValueError(
            f"Archive size mismatch for {filename}: "
            f"{actual_size:,} != {expected_size:,} bytes"
        )

    if not force and marker_is_valid(
        marker,
        archive,
        extracted_root,
        expected_size,
    ):
        return "skipped"

    # Streaming mode validates and extracts in one pass instead of inflating
    # every gzip archive once for listing and a second time for extraction.
    with tarfile.open(archive, mode="r|gz") as tar:
        for member in tar:
            validate_member(member, root_name)
            tar.extract(member, path=output_dir, set_attrs=True)

    if not extracted_root.is_dir():
        raise RuntimeError(f"Expected extracted directory was not created: {extracted_root}")
    write_marker(marker, filename, expected_size)
    return "extracted"


def main() -> int:
    args = parse_args()
    if args.workers < 1:
        raise ValueError("--workers must be at least 1")

    input_dir = args.input_dir.expanduser().resolve()
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir is not None
        else input_dir
    )
    selected_file = args.selected_file.expanduser().resolve()
    manifest_file = args.manifest.expanduser().resolve()
    selected, manifest = load_inputs(selected_file, manifest_file)

    output_dir.mkdir(parents=True, exist_ok=True)
    marker_dir = output_dir / ".extract_complete"
    marker_dir.mkdir(parents=True, exist_ok=True)

    missing_or_invalid: list[str] = []
    compressed_bytes = 0
    for filename in selected:
        archive = input_dir / filename
        expected_size = int(manifest[filename]["size"])
        compressed_bytes += expected_size
        if not archive.is_file() or archive.stat().st_size != expected_size:
            missing_or_invalid.append(filename)

    if missing_or_invalid:
        preview = "\n  ".join(missing_or_invalid[:10])
        raise RuntimeError(
            f"{len(missing_or_invalid)} archives are missing or have invalid sizes:\n  {preview}"
        )

    free_bytes = shutil.disk_usage(output_dir).free
    print(f"Selected archives : {len(selected)}")
    print(f"Compressed size   : {compressed_bytes / (1024 ** 3):.2f} GiB")
    print(f"Free disk space   : {free_bytes / (1024 ** 3):.2f} GiB")
    print(f"Input directory   : {input_dir}")
    print(f"Output directory  : {output_dir}")
    print(f"Workers           : {args.workers}")

    if args.dry_run:
        pending = 0
        for filename in selected:
            expected_size = int(manifest[filename]["size"])
            if args.force or not marker_is_valid(
                marker_path(marker_dir, filename),
                input_dir / filename,
                output_dir / archive_root(filename),
                expected_size,
            ):
                pending += 1
        print(f"Dry run           : {pending} pending, {len(selected) - pending} complete")
        return 0

    counts = {"extracted": 0, "skipped": 0, "failed": 0}
    failures: list[tuple[str, Exception]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                extract_one,
                filename,
                int(manifest[filename]["size"]),
                input_dir,
                output_dir,
                marker_dir,
                args.force,
            ): filename
            for filename in selected
        }
        try:
            for completed, future in enumerate(as_completed(futures), 1):
                filename = futures[future]
                try:
                    result = future.result()
                    counts[result] += 1
                    with PRINT_LOCK:
                        print(f"[{completed}/{len(selected)}] {result:9s} {filename}")
                except Exception as exc:
                    counts["failed"] += 1
                    failures.append((filename, exc))
                    with PRINT_LOCK:
                        print(
                            f"[{completed}/{len(selected)}] FAILED    {filename}: {exc}",
                            file=sys.stderr,
                        )
        except KeyboardInterrupt:
            # Python 3.8 has no cancel_futures option on shutdown(), so cancel
            # every queued future explicitly.  At most --workers running jobs
            # are allowed to finish before the process exits.
            for future in futures:
                future.cancel()
            raise

    print(
        "Summary: "
        f"extracted={counts['extracted']}, "
        f"skipped={counts['skipped']}, "
        f"failed={counts['failed']}"
    )
    if failures:
        print("Failed archives can be retried by running the same command again.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted. Run the same command again to resume.", file=sys.stderr)
        raise SystemExit(130)
