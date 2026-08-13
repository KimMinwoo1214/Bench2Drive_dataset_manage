"""Manifest, hash, completion, and review contracts for production relabeling."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ManifestSelection:
    """Validated clip names selected from one production split manifest."""

    path: Path
    sha256: str
    component: str
    train: tuple[str, ...]
    val: tuple[str, ...]
    all_manifest_clips: frozenset[str]

    @property
    def clips(self) -> tuple[str, ...]:
        return self.train + self.val


@dataclass(frozen=True)
class Approval:
    """Human approval tied to one immutable completion record."""

    clip: str
    component: str
    completion_sha256: str
    approved_by: str
    reason: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def annotation_files(anno_dir: Path) -> list[Path]:
    if not anno_dir.is_dir():
        return []
    return sorted(
        path
        for path in anno_dir.iterdir()
        if path.is_file()
        and (path.name.endswith(".json") or path.name.endswith(".json.gz"))
    )


def annotation_digest(anno_dir: Path) -> tuple[str, tuple[str, ...]]:
    """Hash an annotation frame set and bytes without depending on absolute paths."""
    files = annotation_files(anno_dir)
    if not files:
        raise FileNotFoundError(f"annotation directory has no frames: {anno_dir}")
    digest = hashlib.sha256()
    for path in files:
        name = path.name.encode("utf-8")
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest(), tuple(path.name for path in files)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FileNotFoundError(f"required JSON file is missing: {path}")
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid JSON in {path}: {error}") from error


def _validate_clip_list(label: str, raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list) or not all(isinstance(value, str) for value in raw):
        raise ValueError(f"{label} must be a list of clip-name strings")
    unsafe = [
        value
        for value in raw
        if not value
        or value in {".", ".."}
        or Path(value).is_absolute()
        or "/" in value
        or "\\" in value
    ]
    if unsafe:
        raise ValueError(f"{label} contains unsafe clip names: {unsafe[:5]}")
    duplicates = sorted({value for value in raw if raw.count(value) > 1})
    if duplicates:
        raise ValueError(f"{label} contains duplicate clips: {duplicates[:5]}")
    return tuple(raw)


def _split_lists(container: Mapping[str, Any], label: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    train = _validate_clip_list(f"{label}.train", container.get("train"))
    val = _validate_clip_list(f"{label}.val", container.get("val"))
    overlap = sorted(set(train) & set(val))
    if overlap:
        raise ValueError(f"{label} train/val overlap: {overlap[:5]}")
    return train, val


def load_manifest(path: Path, component: str) -> ManifestSelection:
    """Load a combined split and select base, weak, or all clips."""
    path = path.expanduser().resolve()
    raw = _read_json(path)
    if not isinstance(raw, dict):
        raise ValueError(f"manifest must be a JSON object: {path}")
    all_train, all_val = _split_lists(raw, "manifest")
    all_clips = set(all_train) | set(all_val)

    components = raw.get("components")
    if components is not None:
        if not isinstance(components, dict):
            raise ValueError("manifest.components must be an object")
        component_sets: dict[str, set[str]] = {}
        for name, value in components.items():
            if not isinstance(value, dict):
                raise ValueError(f"manifest.components.{name} must be an object")
            train, val = _split_lists(value, f"manifest.components.{name}")
            component_sets[str(name)] = set(train) | set(val)
        known_union: set[str] = set()
        for name, names in component_sets.items():
            overlap = known_union & names
            if overlap:
                raise ValueError(
                    f"manifest components overlap at {name}: {sorted(overlap)[:5]}"
                )
            known_union.update(names)
        if known_union != all_clips:
            raise ValueError(
                "manifest component union differs from top-level split: "
                f"missing={sorted(all_clips - known_union)[:5]}, "
                f"unexpected={sorted(known_union - all_clips)[:5]}"
            )

    if component == "all":
        train, val = all_train, all_val
    else:
        if not isinstance(components, dict) or component not in components:
            raise ValueError(
                f"manifest has no components.{component}; use --component all "
                "for a legacy split"
            )
        selected = components[component]
        train, val = _split_lists(selected, f"manifest.components.{component}")

    return ManifestSelection(
        path=path,
        sha256=sha256_file(path),
        component=component,
        train=train,
        val=val,
        all_manifest_clips=frozenset(all_clips),
    )


def source_inventory(root: Path) -> set[str]:
    """Return direct child clip directories that contain annotation frames."""
    if not root.is_dir():
        raise FileNotFoundError(f"source root does not exist: {root}")
    clips = set()
    for child in root.iterdir():
        if child.is_dir() and annotation_files(child / "anno"):
            clips.add(child.name)
    return clips


def validate_source_inventory(root: Path, expected: Sequence[str]) -> None:
    actual = source_inventory(root)
    expected_set = set(expected)
    missing = sorted(expected_set - actual)
    unexpected = sorted(actual - expected_set)
    if missing or unexpected:
        raise ValueError(
            f"source inventory mismatch for {root}: missing={missing[:5]} "
            f"({len(missing)}), unexpected={unexpected[:5]} ({len(unexpected)})"
        )


def implementation_hash(paths: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted((value.resolve() for value in paths), key=str):
        name = path.name.encode("utf-8")
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        digest.update(bytes.fromhex(sha256_file(path)))
    return digest.hexdigest()


def build_completion(
    *,
    clip: str,
    component: str,
    manifest_sha256: str,
    config_sha256: str,
    implementation_sha256: str,
    input_annotation_sha256: str,
    output_annotation_sha256: str | None,
    input_frames: Sequence[str],
    output_frames: Sequence[str],
    status: str,
    metrics: Mapping[str, Any] | None = None,
    error: str = "",
) -> dict[str, Any]:
    if status not in {"completed", "review", "failed"}:
        raise ValueError(f"invalid completion status: {status}")
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "clip": clip,
        "component": component,
        "manifest_sha256": manifest_sha256,
        "config_sha256": config_sha256,
        "implementation_sha256": implementation_sha256,
        "input_annotation_sha256": input_annotation_sha256,
        "output_annotation_sha256": output_annotation_sha256,
        "input_frame_count": len(input_frames),
        "output_frame_count": len(output_frames),
        "frame_set_matches": tuple(input_frames) == tuple(output_frames),
        "status": status,
        "metrics": dict(metrics or {}),
        "error": error,
    }
    payload["completion_sha256"] = canonical_sha256(payload)
    return payload


def write_completion(path: Path, completion: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(completion, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def read_completion(path: Path) -> dict[str, Any]:
    raw = _read_json(path)
    if not isinstance(raw, dict):
        raise ValueError(f"completion must be a JSON object: {path}")
    claimed = raw.get("completion_sha256")
    without_hash = dict(raw)
    without_hash.pop("completion_sha256", None)
    actual = canonical_sha256(without_hash)
    if claimed != actual:
        raise ValueError(
            f"completion SHA256 mismatch for {path}: claimed={claimed}, actual={actual}"
        )
    return raw


def completion_errors(
    completion: Mapping[str, Any],
    *,
    clip: str,
    component: str,
    manifest_sha256: str,
    config_sha256: str,
    implementation_sha256: str,
    input_annotation_sha256: str,
    input_frames: Sequence[str],
    output_annotation_sha256: str,
    output_frames: Sequence[str],
) -> list[str]:
    expected = {
        "clip": clip,
        "component": component,
        "manifest_sha256": manifest_sha256,
        "config_sha256": config_sha256,
        "implementation_sha256": implementation_sha256,
        "input_annotation_sha256": input_annotation_sha256,
        "output_annotation_sha256": output_annotation_sha256,
        "input_frame_count": len(input_frames),
        "output_frame_count": len(output_frames),
        "frame_set_matches": tuple(input_frames) == tuple(output_frames),
    }
    errors = [
        f"{field}: expected={value!r}, actual={completion.get(field)!r}"
        for field, value in expected.items()
        if completion.get(field) != value
    ]
    if tuple(input_frames) != tuple(output_frames):
        errors.append("corrected annotation frame set differs from input")
    if completion.get("status") not in {"completed", "review", "failed"}:
        errors.append(f"invalid status: {completion.get('status')!r}")
    return errors


def load_approvals(path: Path | None) -> dict[tuple[str, str, str], Approval]:
    if path is None:
        return {}
    raw = _read_json(path.expanduser().resolve())
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("approval JSON must have schema_version=1")
    entries = raw.get("approvals")
    if not isinstance(entries, list):
        raise ValueError("approval JSON must contain an approvals list")
    approvals: dict[tuple[str, str, str], Approval] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"approvals[{index}] must be an object")
        approval = Approval(
            clip=str(entry.get("clip", "")),
            component=str(entry.get("component", "")),
            completion_sha256=str(entry.get("completion_sha256", "")),
            approved_by=str(entry.get("approved_by", "")),
            reason=str(entry.get("reason", "")),
        )
        if not all(
            (
                approval.clip,
                approval.component,
                approval.completion_sha256,
                approval.approved_by,
                approval.reason,
            )
        ):
            raise ValueError(f"approvals[{index}] has empty required fields")
        key = (approval.component, approval.clip, approval.completion_sha256)
        if key in approvals:
            raise ValueError(f"duplicate approval: {key}")
        approvals[key] = approval
    return approvals


def matching_approval(
    approvals: Mapping[tuple[str, str, str], Approval],
    completion: Mapping[str, Any],
) -> Approval | None:
    key = (
        str(completion.get("component", "")),
        str(completion.get("clip", "")),
        str(completion.get("completion_sha256", "")),
    )
    return approvals.get(key)
