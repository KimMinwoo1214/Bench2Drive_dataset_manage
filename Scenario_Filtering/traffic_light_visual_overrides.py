"""Scenario-local traffic-light identity fixes used only while rendering."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence


CAMERA_GEOMETRY_FIELDS = (
    "center",
    "extent",
    "location",
    "rotation",
    "world_cord",
    "distance",
)


def load_traffic_light_visual_map(
    path: Path | str | None,
    scenario_name: str,
) -> dict[str, str]:
    """Load one scenario's source-ID to camera-geometry-ID mapping."""
    if path is None:
        return {}

    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise FileNotFoundError(
            f"traffic-light visualization override를 찾을 수 없습니다: {config_path}"
        )
    with config_path.open("r", encoding="utf-8") as config_file:
        config = json.load(config_file)

    scenarios = config.get("scenarios")
    if not isinstance(scenarios, dict):
        raise ValueError("visualization override에는 scenarios object가 필요합니다.")
    scenario_config = scenarios.get(scenario_name)
    if scenario_config is None:
        return {}
    if not isinstance(scenario_config, dict):
        raise ValueError(f"scenario override가 object가 아닙니다: {scenario_name}")

    raw_mapping = scenario_config.get("source_to_camera_geometry")
    if not isinstance(raw_mapping, dict) or not raw_mapping:
        raise ValueError(
            "scenario override에는 비어 있지 않은 "
            "source_to_camera_geometry object가 필요합니다: "
            f"{scenario_name}"
        )
    mapping = {str(source): str(target) for source, target in raw_mapping.items()}
    if len(set(mapping.values())) != len(mapping):
        raise ValueError(
            f"camera geometry ID가 중복된 visualization override입니다: {scenario_name}"
        )
    return mapping


def remap_traffic_light_camera_geometry(
    boxes: Sequence[dict[str, Any]],
    source_to_geometry: Mapping[str, str] | None,
) -> list[dict[str, Any]]:
    """Move light metadata onto configured camera geometry without mutating input."""
    if not source_to_geometry:
        return list(boxes)

    lights_by_id = {
        str(box.get("id")): box
        for box in boxes
        if box.get("class") == "traffic_light" and box.get("id") is not None
    }
    remapped: list[dict[str, Any]] = []
    for box in boxes:
        source_id = str(box.get("id"))
        geometry_id = source_to_geometry.get(source_id)
        geometry = lights_by_id.get(str(geometry_id)) if geometry_id is not None else None
        if box.get("class") != "traffic_light" or geometry is None:
            remapped.append(box)
            continue

        display_box = dict(box)
        for field in CAMERA_GEOMETRY_FIELDS:
            if field in geometry:
                display_box[field] = geometry[field]
            else:
                display_box.pop(field, None)
        display_box["_camera_geometry_id"] = str(geometry_id)
        remapped.append(display_box)
    return remapped
