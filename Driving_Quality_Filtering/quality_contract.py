"""Immutable manifests, calibration config, decisions, and filtered split rules."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = 1
COLLISION_CATEGORIES = ("vehicle", "pedestrian", "bicycle")
CLIP_PATTERN = re.compile(
    r"^(?P<scenario>.+)_(?P<town>Town[^_]+)_Route[^_]+_(?P<weather>Weather[^_.]+)$"
)


@dataclass(frozen=True)
class ClipRecord:
    name: str
    component: str
    split: str
    scenario: str
    town: str
    weather: str


@dataclass(frozen=True)
class QualityManifest:
    path: Path
    sha256: str
    raw: Mapping[str, Any]
    clips: tuple[ClipRecord, ...]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FileNotFoundError(f"required JSON file is missing: {path}")
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid JSON in {path}: {error}") from error


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _safe_names(label: str, value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be a list of clip names")
    unsafe = [
        item
        for item in value
        if not item
        or item in {".", ".."}
        or Path(item).is_absolute()
        or "/" in item
        or "\\" in item
    ]
    if unsafe:
        raise ValueError(f"{label} contains unsafe clip names: {unsafe[:5]}")
    if len(set(value)) != len(value):
        raise ValueError(f"{label} contains duplicate clip names")
    return tuple(value)


def _parse_name(name: str) -> tuple[str, str, str]:
    match = CLIP_PATTERN.fullmatch(name)
    if match is None:
        raise ValueError(f"clip name violates the production contract: {name}")
    return match.group("scenario"), match.group("town"), match.group("weather")


def load_manifest(path: Path) -> QualityManifest:
    path = path.expanduser().resolve()
    raw = read_json(path)
    if not isinstance(raw, dict):
        raise ValueError("parent split must be a JSON object")
    top_train = _safe_names("manifest.train", raw.get("train"))
    top_val = _safe_names("manifest.val", raw.get("val"))
    if set(top_train) & set(top_val):
        raise ValueError("manifest train and val overlap")
    top_assignment = {name: "train" for name in top_train}
    top_assignment.update({name: "val" for name in top_val})

    components = raw.get("components")
    if not isinstance(components, dict) or set(components) != {"base", "weak"}:
        raise ValueError("manifest must contain exactly components.base and components.weak")
    records = []
    component_union: set[str] = set()
    for component in ("base", "weak"):
        value = components[component]
        if not isinstance(value, dict):
            raise ValueError(f"components.{component} must be an object")
        train = _safe_names(f"components.{component}.train", value.get("train"))
        val = _safe_names(f"components.{component}.val", value.get("val"))
        if set(train) & set(val):
            raise ValueError(f"components.{component} train and val overlap")
        for split, names in (("train", train), ("val", val)):
            for name in names:
                if name in component_union:
                    raise ValueError(f"manifest components overlap at clip: {name}")
                if top_assignment.get(name) != split:
                    raise ValueError(
                        f"component/top-level split mismatch for {name}: "
                        f"component={split}, top={top_assignment.get(name)}"
                    )
                scenario, town, weather = _parse_name(name)
                records.append(
                    ClipRecord(name, component, split, scenario, town, weather)
                )
                component_union.add(name)
    if component_union != set(top_assignment):
        raise ValueError("component union differs from the top-level manifest")
    by_name = {record.name: record for record in records}
    ordered = tuple(by_name[name] for name in [*top_train, *top_val])
    return QualityManifest(path, sha256_file(path), raw, ordered)


def load_config(path: Path, *, require_production: bool = False) -> dict[str, Any]:
    path = path.expanduser().resolve()
    value = read_json(path)
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("quality config must be an object with schema_version=1")
    mode = value.get("mode")
    if mode not in {"calibration_only", "production"}:
        raise ValueError("quality config mode must be calibration_only or production")
    sample_hz = value.get("sample_hz")
    if isinstance(sample_hz, bool) or not isinstance(sample_hz, (int, float)):
        raise ValueError("sample_hz must be numeric")
    if float(sample_hz) <= 0:
        raise ValueError("sample_hz must be positive")
    if value.get("sensor_validation_policy") != "inventory_and_nonzero_size_all_signature_first_middle_last":
        raise ValueError("unsupported sensor_validation_policy")
    if value.get("collision_categories") != list(COLLISION_CATEGORIES):
        raise ValueError(
            "collision_categories must be exactly vehicle, pedestrian, bicycle"
        )
    for field in ("required_sensor_keys", "required_rgb_folders", "required_depth_folders"):
        raw = value.get(field)
        if not isinstance(raw, list) or not raw or not all(
            isinstance(item, str) and item for item in raw
        ):
            raise ValueError(f"{field} must be a non-empty string list")
    if require_production and mode != "production":
        raise ValueError(
            "classification is blocked: config.mode is calibration_only; "
            "freeze and approve a versioned production config first"
        )
    if mode == "production":
        _validate_production_thresholds(value)
    return value


def _finite_number(value: Any, label: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    number = float(value)
    if not math_is_finite(number) or (minimum is not None and number < minimum):
        raise ValueError(f"{label} is outside its valid range")
    return number


def math_is_finite(value: float) -> bool:
    return value == value and value not in {float("inf"), float("-inf")}


def _validate_production_thresholds(config: Mapping[str, Any]) -> None:
    collision = config.get("collision")
    if not isinstance(collision, dict):
        raise ValueError("production config requires a collision object")
    allowed = {
        "review_minimum_penetration_m",
        "review_minimum_iou",
        "review_minimum_consecutive_frames",
        "exclude_minimum_penetration_m",
        "exclude_minimum_iou",
        "exclude_minimum_consecutive_frames",
        "exclude_severe_single_frame_penetration_m",
    }
    if set(collision) != allowed:
        raise ValueError("collision config fields do not match the v1 contract")
    numbers = {}
    for field in (
        "review_minimum_penetration_m",
        "review_minimum_iou",
        "exclude_minimum_penetration_m",
        "exclude_minimum_iou",
        "exclude_severe_single_frame_penetration_m",
    ):
        numbers[field] = _finite_number(
            collision.get(field), f"collision.{field}", minimum=0.0
        )
    runs = {}
    for field in (
        "review_minimum_consecutive_frames",
        "exclude_minimum_consecutive_frames",
    ):
        value = collision.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"collision.{field} must be a positive integer")
        runs[field] = value
    if numbers["exclude_minimum_penetration_m"] < numbers["review_minimum_penetration_m"]:
        raise ValueError("exclude penetration threshold must not be below review")
    if numbers["exclude_minimum_iou"] < numbers["review_minimum_iou"]:
        raise ValueError("exclude IoU threshold must not be below review")
    if runs["exclude_minimum_consecutive_frames"] < runs["review_minimum_consecutive_frames"]:
        raise ValueError("exclude duration threshold must not be below review")


def metrics_hash(rows: Sequence[Mapping[str, Any]]) -> str:
    ordered = sorted((dict(row) for row in rows), key=lambda row: str(row.get("clip", "")))
    return canonical_sha256(ordered)


def filtered_split(
    manifest: QualityManifest, accepted: Sequence[str], excluded: Sequence[str]
) -> dict[str, Any]:
    accepted_set = set(accepted)
    excluded_set = set(excluded)
    all_names = {record.name for record in manifest.clips}
    if accepted_set & excluded_set:
        raise ValueError("accepted and excluded clips overlap")
    if accepted_set | excluded_set != all_names:
        raise ValueError("accepted and excluded clips do not partition the parent manifest")

    def names(component: str | None, split: str) -> list[str]:
        return [
            record.name
            for record in manifest.clips
            if record.split == split
            and record.name in accepted_set
            and (component is None or record.component == component)
        ]

    train = names(None, "train")
    val = names(None, "val")
    components = {}
    excluded_by_component = {}
    for component in ("base", "weak"):
        component_train = names(component, "train")
        component_val = names(component, "val")
        components[component] = {
            "num_train": len(component_train),
            "num_val": len(component_val),
            "train": component_train,
            "val": component_val,
        }
        excluded_by_component[component] = sorted(
            record.name
            for record in manifest.clips
            if record.component == component and record.name in excluded_set
        )
    return {
        "schema_version": 1,
        "dataset": f"{manifest.raw.get('dataset', 'Bench2Drive')} quality-filtered",
        "parent_manifest": {"path": str(manifest.path), "sha256": manifest.sha256},
        "policy": "preserve_parent_membership_remove_excluded_no_backfill",
        "num_train": len(train),
        "num_val": len(val),
        "train": train,
        "val": val,
        "components": components,
        "excluded": sorted(excluded_set),
        "excluded_by_component": excluded_by_component,
        "validation": {
            "train_val_overlap": len(set(train) & set(val)),
            "accepted_excluded_overlap": len(accepted_set & excluded_set),
            "parent_total": len(all_names),
            "accepted_total": len(accepted_set),
            "excluded_total": len(excluded_set),
        },
    }
