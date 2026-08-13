#!/usr/bin/env python3
"""Build a ratio-driven Base + Weak production split and verify its artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "production_split_config.json"
SCHEMA_VERSION = 2
SUPPORTED_ALGORITHM = "coverage_aware_v1"

CLIP_PATTERN = re.compile(
    r"^(?P<scenario>.+)_(?P<town>Town[^_]+)_Route[^_]+_"
    r"(?P<weather>Weather[^_.]+)$"
)


@dataclass(frozen=True)
class SplitSettings:
    """All policy inputs needed to reproduce a split without fixed clip counts."""

    config_path: Path
    config_sha256: str
    algorithm: str
    seed: int
    weak_validation_ratio: float
    quota_rounding: str
    minimum_validation_per_scenario: int
    preserve_train_per_scenario: bool
    scenario_order: tuple[str, ...]
    input_paths: Mapping[str, Path]
    snapshot_expectations: Mapping[str, Any]


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FileNotFoundError(f"required JSON file is missing: {path}")
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid JSON in {path}: {error}") from error


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _display_input_path(path: Path, config_path: Path) -> str:
    """Keep repository-local provenance readable without hiding subdirectories."""
    try:
        return path.relative_to(config_path.parent).as_posix()
    except ValueError:
        return str(path)


def _clip_name(filename: str) -> str:
    return filename.removesuffix(".tar.gz")


def _parse_clip(clip: str) -> tuple[str, str, str]:
    match = CLIP_PATTERN.fullmatch(clip)
    if match is None:
        raise ValueError(f"clip name does not match the production contract: {clip}")
    return (
        match.group("scenario"),
        match.group("town"),
        match.group("weather"),
    )


def _require_unique(label: str, values: Sequence[str]) -> None:
    duplicates = sorted(name for name, count in Counter(values).items() if count > 1)
    if duplicates:
        raise ValueError(f"{label} contains duplicate clips: {duplicates[:5]}")


def _newline_hash(values: Iterable[str]) -> str:
    payload = "".join(f"{value}\n" for value in sorted(values))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_settings(config_path: Path = DEFAULT_CONFIG_PATH) -> SplitSettings:
    """Load mutable split policy separately from immutable generated artifacts."""
    config_path = config_path.expanduser().resolve()
    raw = _read_json(config_path)
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError(f"split config must have schema_version=1: {config_path}")
    algorithm = raw.get("algorithm")
    if algorithm != SUPPORTED_ALGORITHM:
        raise ValueError(
            f"unsupported split algorithm: {algorithm!r}; "
            f"expected={SUPPORTED_ALGORITHM!r}"
        )
    seed = raw.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError("split seed must be an integer")
    ratio = raw.get("weak_validation_ratio")
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)):
        raise TypeError("weak_validation_ratio must be numeric")
    ratio = float(ratio)
    if not 0.0 < ratio < 1.0:
        raise ValueError("weak_validation_ratio must be between 0 and 1")
    rounding = raw.get("quota_rounding")
    if rounding not in {"ceil", "round", "floor"}:
        raise ValueError("quota_rounding must be one of ceil, round, floor")
    minimum = raw.get("minimum_validation_per_scenario")
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0:
        raise ValueError("minimum_validation_per_scenario must be a non-negative int")
    preserve_train = raw.get("preserve_train_per_scenario")
    if not isinstance(preserve_train, bool):
        raise TypeError("preserve_train_per_scenario must be bool")
    raw_order = raw.get("scenario_order", [])
    if not isinstance(raw_order, list) or not all(
        isinstance(value, str) and value for value in raw_order
    ):
        raise ValueError("scenario_order must be a list of non-empty strings")
    _require_unique("scenario_order", raw_order)

    raw_inputs = raw.get("inputs")
    required_inputs = {"base_split", "base_stats", "weak_manifest", "weak_details"}
    if not isinstance(raw_inputs, dict) or set(raw_inputs) != required_inputs:
        raise ValueError(
            f"inputs must contain exactly {sorted(required_inputs)}"
        )
    input_paths = {}
    for name, value in raw_inputs.items():
        if not isinstance(value, str) or not value:
            raise ValueError(f"inputs.{name} must be a non-empty path string")
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = config_path.parent / path
        input_paths[name] = path.resolve()

    expectations = raw.get("snapshot_expectations", {})
    if not isinstance(expectations, dict):
        raise ValueError("snapshot_expectations must be an object when present")
    return SplitSettings(
        config_path=config_path,
        config_sha256=_sha256_file(config_path),
        algorithm=algorithm,
        seed=seed,
        weak_validation_ratio=ratio,
        quota_rounding=rounding,
        minimum_validation_per_scenario=minimum,
        preserve_train_per_scenario=preserve_train,
        scenario_order=tuple(raw_order),
        input_paths=input_paths,
        snapshot_expectations=expectations,
    )


def _weak_rows(
    manifest: Mapping[str, Any], details: Sequence[Mapping[str, Any]]
) -> list[dict[str, str]]:
    manifest_filenames = set(manifest)
    detail_filenames = [str(row.get("filename", "")) for row in details]
    _require_unique("Weak details", detail_filenames)
    if manifest_filenames != set(detail_filenames):
        missing = sorted(manifest_filenames - set(detail_filenames))
        unexpected = sorted(set(detail_filenames) - manifest_filenames)
        raise ValueError(
            "Weak manifest/details disagree: "
            f"missing_details={missing[:5]}, unexpected_details={unexpected[:5]}"
        )

    rows: list[dict[str, str]] = []
    for raw in details:
        filename = str(raw["filename"])
        clip = _clip_name(filename)
        scenario, town, weather = _parse_clip(clip)
        declared_scenario = str(raw.get("scenario", ""))
        declared_town = str(raw.get("town", ""))
        declared_weather = f"Weather{str(raw.get('weather', '')).removeprefix('Weather')}"
        if (scenario, town, weather) != (
            declared_scenario,
            declared_town,
            declared_weather,
        ):
            raise ValueError(
                f"Weak detail metadata disagrees with filename {filename}: "
                f"parsed={(scenario, town, weather)}, "
                f"declared={(declared_scenario, declared_town, declared_weather)}"
            )
        rows.append(
            {
                "filename": filename,
                "clip": clip,
                "scenario": scenario,
                "town": town,
                "weather": weather,
            }
        )
    _require_unique("Weak manifest", [row["clip"] for row in rows])
    if not rows:
        raise ValueError("Weak manifest is empty")
    return rows


def _scenario_order(rows: Sequence[Mapping[str, str]], preferred: Sequence[str]) -> tuple[str, ...]:
    available = {row["scenario"] for row in rows}
    preferred_available = [scenario for scenario in preferred if scenario in available]
    remaining = sorted(available - set(preferred_available))
    return tuple([*preferred_available, *remaining])


def _round_count(raw: float, rounding: str) -> int:
    """Round a global target deterministically (avoids bankers' rounding)."""
    if rounding == "ceil":
        return math.ceil(raw)
    if rounding == "floor":
        return math.floor(raw)
    return math.floor(raw + 0.5)


def weak_validation_target(total: int, settings: SplitSettings) -> int:
    """Return the requested global Weak validation size."""
    return _round_count(
        total * settings.weak_validation_ratio, settings.quota_rounding
    )


def _maximum_quota(count: int, settings: SplitSettings) -> int:
    if settings.preserve_train_per_scenario and count > 1:
        return count - 1
    return count


def scenario_quotas(
    rows: Sequence[Mapping[str, str]], settings: SplitSettings
) -> dict[str, int]:
    """Allocate one global ratio target across scenarios by largest deficit.

    Independent per-scenario rounding can inflate the total when scenario types
    increase. This allocator first fixes the global target, then distributes it
    while respecting minimum coverage and a retained train example when possible.
    """
    counts = Counter(row["scenario"] for row in rows)
    order = _scenario_order(rows, settings.scenario_order)
    ideals = {
        scenario: counts[scenario] * settings.weak_validation_ratio
        for scenario in order
    }
    maxima = {
        scenario: _maximum_quota(counts[scenario], settings)
        for scenario in order
    }
    minima = {
        scenario: min(settings.minimum_validation_per_scenario, maxima[scenario])
        for scenario in order
    }
    quotas = {
        scenario: min(
            maxima[scenario],
            max(minima[scenario], math.floor(ideals[scenario])),
        )
        for scenario in order
    }
    requested_target = weak_validation_target(len(rows), settings)
    feasible_target = min(
        sum(maxima.values()), max(requested_target, sum(minima.values()))
    )
    order_index = {scenario: index for index, scenario in enumerate(order)}

    while sum(quotas.values()) < feasible_target:
        candidates = [
            scenario for scenario in order if quotas[scenario] < maxima[scenario]
        ]
        if not candidates:
            break
        chosen = max(
            candidates,
            key=lambda scenario: (
                ideals[scenario] - quotas[scenario],
                counts[scenario],
                -order_index[scenario],
            ),
        )
        quotas[chosen] += 1

    while sum(quotas.values()) > feasible_target:
        candidates = [
            scenario for scenario in order if quotas[scenario] > minima[scenario]
        ]
        if not candidates:
            break
        chosen = max(
            candidates,
            key=lambda scenario: (
                quotas[scenario] - ideals[scenario],
                -counts[scenario],
                order_index[scenario],
            ),
        )
        quotas[chosen] -= 1
    return quotas


def select_weak_validation(
    rows: Sequence[Mapping[str, str]], settings: SplitSettings
) -> tuple[list[str], dict[str, int], tuple[str, ...]]:
    """Select Weak val clips using coverage_aware_v1 and ratio-derived quotas."""
    order = _scenario_order(rows, settings.scenario_order)
    quotas = scenario_quotas(rows, settings)
    town_counts = {
        scenario: Counter(
            row["town"] for row in rows if row["scenario"] == scenario
        )
        for scenario in order
    }
    weather_counts = {
        scenario: Counter(
            row["weather"] for row in rows if row["scenario"] == scenario
        )
        for scenario in order
    }

    selected: list[str] = []
    selected_names: set[str] = set()
    global_towns: set[str] = set()
    global_weathers: set[str] = set()
    selected_towns = {scenario: set() for scenario in order}
    selected_weathers = {scenario: set() for scenario in order}
    remaining = dict(quotas)

    while any(remaining.values()):
        for scenario in order:
            if remaining[scenario] == 0:
                continue
            candidates = [
                row
                for row in rows
                if row["scenario"] == scenario and row["clip"] not in selected_names
            ]
            if not candidates:
                raise ValueError(f"not enough Weak candidates for {scenario}")

            def priority(row: Mapping[str, str]) -> tuple[Any, ...]:
                tie_break = hashlib.sha256(
                    f"{settings.seed}|{scenario}|{row['clip']}".encode("utf-8")
                ).hexdigest()
                return (
                    -(row["town"] not in global_towns),
                    -(row["weather"] not in global_weathers),
                    -(row["town"] not in selected_towns[scenario]),
                    -(row["weather"] not in selected_weathers[scenario]),
                    town_counts[scenario][row["town"]],
                    weather_counts[scenario][row["weather"]],
                    tie_break,
                )

            chosen = min(candidates, key=priority)
            clip = chosen["clip"]
            selected.append(clip)
            selected_names.add(clip)
            global_towns.add(chosen["town"])
            global_weathers.add(chosen["weather"])
            selected_towns[scenario].add(chosen["town"])
            selected_weathers[scenario].add(chosen["weather"])
            remaining[scenario] -= 1
    return selected, quotas, order


def _distribution_stats(
    train: Sequence[str], val: Sequence[str], *, dataset: str
) -> dict[str, Any]:
    split_sets = {"train": set(train), "val": set(val)}
    all_clips = [*train, *val]
    parsed = {clip: _parse_clip(clip) for clip in all_clips}

    def category(index: int) -> dict[str, dict[str, int]]:
        labels = sorted({parts[index] for parts in parsed.values()})
        return {
            label: {
                "all": sum(parts[index] == label for parts in parsed.values()),
                "train": sum(
                    parsed[clip][index] == label for clip in split_sets["train"]
                ),
                "val": sum(
                    parsed[clip][index] == label for clip in split_sets["val"]
                ),
            }
            for label in labels
        }

    by_scenario = category(0)
    by_town = category(1)
    by_weather = category(2)
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset": dataset,
        "totals": {
            "clips": len(all_clips),
            "train": len(train),
            "val": len(val),
            "validation_ratio": len(val) / len(all_clips) if all_clips else 0.0,
            "scenario_types": len(by_scenario),
            "towns": len(by_town),
            "weathers": len(by_weather),
            "val_scenario_types": sum(row["val"] > 0 for row in by_scenario.values()),
            "val_towns": sum(row["val"] > 0 for row in by_town.values()),
            "val_weathers": sum(row["val"] > 0 for row in by_weather.values()),
        },
        "by_scenario": by_scenario,
        "by_town": by_town,
        "by_weather": by_weather,
    }


def _validation_summary(
    train: Sequence[str], val: Sequence[str], expected: set[str]
) -> dict[str, Any]:
    train_set = set(train)
    val_set = set(val)
    actual = train_set | val_set
    return {
        "train_duplicates": len(train) - len(train_set),
        "val_duplicates": len(val) - len(val_set),
        "train_val_overlap": len(train_set & val_set),
        "total_unique": len(actual),
        "expected_total": len(expected),
        "missing": sorted(expected - actual),
        "unexpected": sorted(actual - expected),
    }


def _verify_snapshot(
    settings: SplitSettings,
    input_hashes: Mapping[str, str],
    *,
    base_count: int,
    weak_count: int,
    selection_sha256: str,
) -> None:
    expectations = settings.snapshot_expectations
    if not expectations:
        return
    expected_hashes = expectations.get("input_sha256", {})
    if not isinstance(expected_hashes, dict):
        raise ValueError("snapshot_expectations.input_sha256 must be an object")
    for name, expected in expected_hashes.items():
        actual = input_hashes.get(name)
        if actual != expected:
            raise ValueError(
                f"snapshot input changed for {name}: expected={expected}, actual={actual}. "
                "새 snapshot이면 config의 snapshot_expectations를 갱신하거나 제거하세요."
            )
    checks = {
        "base_clips": base_count,
        "weak_clips": weak_count,
        "weak_validation_selection_sha256": selection_sha256,
    }
    for name, actual in checks.items():
        expected = expectations.get(name)
        if expected is not None and expected != actual:
            raise ValueError(
                f"snapshot expectation changed for {name}: "
                f"expected={expected}, actual={actual}"
            )


def artifact_filenames(base_count: int, weak_count: int) -> dict[str, str]:
    return {
        "weak_split": f"bench2drive_weak{weak_count}_train_val_split.json",
        "weak_stats": f"bench2drive_weak{weak_count}_split_stats.json",
        "combined_split": (
            f"bench2drive_base{base_count}_weak{weak_count}_train_val_split.json"
        ),
        "combined_stats": (
            f"bench2drive_base{base_count}_weak{weak_count}_split_stats.json"
        ),
    }


def build_artifacts(
    data_dir: Path | None = None,
    settings: SplitSettings | None = None,
) -> dict[str, Any]:
    """Build artifacts for arbitrary Base/Weak sizes using the configured ratio."""
    settings = settings or load_settings(DEFAULT_CONFIG_PATH)
    if data_dir is not None:
        # Backward-compatible test/helper override: keep filenames from config,
        # but read them from the requested fixture directory.
        data_dir = data_dir.resolve()
        input_paths = {
            name: data_dir / path.name for name, path in settings.input_paths.items()
        }
    else:
        input_paths = dict(settings.input_paths)

    input_hashes = {name: _sha256_file(path) for name, path in input_paths.items()}
    base = _read_json(input_paths["base_split"])
    if not isinstance(base, dict):
        raise ValueError("Base split must be an object")
    base_train = [str(value) for value in base.get("train", [])]
    base_val = [str(value) for value in base.get("val", [])]
    _require_unique("Base train", base_train)
    _require_unique("Base val", base_val)
    if set(base_train) & set(base_val):
        raise ValueError("Base train and val overlap")
    if "num_train" in base and base["num_train"] != len(base_train):
        raise ValueError("Base num_train does not match its train list")
    if "num_val" in base and base["num_val"] != len(base_val):
        raise ValueError("Base num_val does not match its val list")

    weak_manifest = _read_json(input_paths["weak_manifest"])
    weak_details = _read_json(input_paths["weak_details"])
    if not isinstance(weak_manifest, dict) or not isinstance(weak_details, list):
        raise ValueError("Weak manifest must be an object and details must be a list")
    rows = _weak_rows(weak_manifest, weak_details)
    weak_all = {row["clip"] for row in rows}
    base_all = set(base_train) | set(base_val)
    overlap = sorted(base_all & weak_all)
    if overlap:
        raise ValueError(f"Base and Weak overlap: {overlap[:5]}")

    selection_order, quotas, scenario_order = select_weak_validation(rows, settings)
    weak_val = sorted(selection_order)
    weak_train = sorted(weak_all - set(weak_val))
    selection_sha256 = _newline_hash(weak_val)
    _verify_snapshot(
        settings,
        input_hashes,
        base_count=len(base_all),
        weak_count=len(weak_all),
        selection_sha256=selection_sha256,
    )

    policy = {
        "algorithm": settings.algorithm,
        "seed": settings.seed,
        "weak_validation_ratio": settings.weak_validation_ratio,
        "quota_rounding": settings.quota_rounding,
        "minimum_validation_per_scenario": settings.minimum_validation_per_scenario,
        "preserve_train_per_scenario": settings.preserve_train_per_scenario,
        "requested_weak_validation_count": weak_validation_target(
            len(weak_all), settings
        ),
        "effective_weak_validation_count": len(weak_val),
        "scenario_order": list(scenario_order),
        "scenario_quotas": quotas,
    }
    inputs = {
        name: {
            "path": _display_input_path(path, settings.config_path),
            "sha256": input_hashes[name],
        }
        for name, path in input_paths.items()
    }
    weak_validation = _validation_summary(weak_train, weak_val, weak_all)
    weak_split = {
        "schema_version": SCHEMA_VERSION,
        "dataset": f"Bench2Drive Weak{len(weak_all)}",
        "config": {
            "path": settings.config_path.name,
            "sha256": settings.config_sha256,
        },
        **policy,
        "inputs": inputs,
        "selection_sha256": selection_sha256,
        "num_train": len(weak_train),
        "num_val": len(weak_val),
        "train": weak_train,
        "val": weak_val,
        "validation": weak_validation,
    }

    combined_train = [*base_train, *weak_train]
    combined_val = [*base_val, *weak_val]
    combined_expected = base_all | weak_all
    combined_validation = _validation_summary(
        combined_train, combined_val, combined_expected
    )
    combined_validation.update(
        {
            "base_weak_overlap": len(base_all & weak_all),
            "base": _validation_summary(base_train, base_val, base_all),
            "weak": weak_validation,
        }
    )
    combined_split = {
        "schema_version": SCHEMA_VERSION,
        "dataset": f"Bench2Drive Base{len(base_all)} + Weak{len(weak_all)}",
        "config": {
            "path": settings.config_path.name,
            "sha256": settings.config_sha256,
        },
        **policy,
        "inputs": inputs,
        "num_train": len(combined_train),
        "num_val": len(combined_val),
        "train": combined_train,
        "val": combined_val,
        "components": {
            "base": {
                "num_train": len(base_train),
                "num_val": len(base_val),
                "train": base_train,
                "val": base_val,
            },
            "weak": {
                "num_train": len(weak_train),
                "num_val": len(weak_val),
                "train": weak_train,
                "val": weak_val,
            },
        },
        "validation": combined_validation,
    }

    weak_stats = _distribution_stats(
        weak_train, weak_val, dataset=f"Bench2Drive Weak{len(weak_all)}"
    )
    weak_stats.update({**policy, "selection_sha256": selection_sha256})
    combined_stats = _distribution_stats(
        combined_train,
        combined_val,
        dataset=f"Bench2Drive Base{len(base_all)} + Weak{len(weak_all)}",
    )
    combined_stats.update({**policy, "selection_sha256": selection_sha256})
    combined_stats["components"] = {
        "base": {
            "train": len(base_train),
            "val": len(base_val),
            "all": len(base_all),
        },
        "weak": {
            "train": len(weak_train),
            "val": len(weak_val),
            "all": len(weak_all),
        },
    }

    filenames = artifact_filenames(len(base_all), len(weak_all))
    return {
        filenames["weak_split"]: weak_split,
        filenames["weak_stats"]: weak_stats,
        filenames["combined_split"]: combined_split,
        filenames["combined_stats"]: combined_stats,
    }


def _verify_zero_error_validation(name: str, value: Mapping[str, Any]) -> None:
    for field in ("train_duplicates", "val_duplicates", "train_val_overlap"):
        if value[field] != 0:
            raise ValueError(f"{name} validation failed: {field}={value[field]}")
    if value["total_unique"] != value["expected_total"]:
        raise ValueError(
            f"{name} validation failed: unique={value['total_unique']}, "
            f"expected={value['expected_total']}"
        )
    if value["missing"] or value["unexpected"]:
        raise ValueError(
            f"{name} validation failed: missing={value['missing'][:5]}, "
            f"unexpected={value['unexpected'][:5]}"
        )


def _validate_artifacts(artifacts: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    split_artifacts = [
        value for filename, value in artifacts.items() if filename.endswith("train_val_split.json")
    ]
    weak = next(value for value in split_artifacts if "components" not in value)
    combined = next(value for value in split_artifacts if "components" in value)
    _verify_zero_error_validation("Weak", weak["validation"])
    _verify_zero_error_validation("combined", combined["validation"])
    if combined["validation"]["base_weak_overlap"] != 0:
        raise ValueError("Base and Weak components overlap")
    return weak, combined


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="ratio, seed, input paths, and optional snapshot expectations",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="generated artifact directory; default is the base split's directory",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify generated outputs without modifying files",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings(args.config)
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else settings.input_paths["base_split"].parent
    )
    artifacts = build_artifacts(settings=settings)
    weak, combined = _validate_artifacts(artifacts)

    if args.check:
        for filename, expected in artifacts.items():
            path = output_dir / filename
            actual = _read_json(path)
            if actual != expected:
                raise ValueError(f"generated split artifact is stale: {path}")
        print(
            "production split check passed: "
            f"Base={len(combined['components']['base']['train'])}/"
            f"{len(combined['components']['base']['val'])}, "
            f"Weak={weak['num_train']}/{weak['num_val']}, "
            f"combined={combined['num_train']}/{combined['num_val']}"
        )
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)
    for filename, value in artifacts.items():
        _write_json(output_dir / filename, value)
    print(
        "wrote production splits: "
        f"Base={len(combined['components']['base']['train'])}/"
        f"{len(combined['components']['base']['val'])}, "
        f"Weak={weak['num_train']}/{weak['num_val']}, "
        f"combined={combined['num_train']}/{combined['num_val']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
