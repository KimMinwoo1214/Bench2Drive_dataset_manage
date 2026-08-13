"""Regression and scalability tests for ratio-driven production splits."""

from __future__ import annotations

import dataclasses
import unittest

from build_production_split import (
    DEFAULT_CONFIG_PATH,
    artifact_filenames,
    build_artifacts,
    load_settings,
    scenario_quotas,
    weak_validation_target,
)


class ProductionSplitTest(unittest.TestCase):
    def test_current_snapshot_remains_reproducible(self) -> None:
        settings = load_settings(DEFAULT_CONFIG_PATH)
        artifacts = build_artifacts(settings=settings)
        names = artifact_filenames(1000, 329)
        weak = artifacts[names["weak_split"]]
        combined = artifacts[names["combined_split"]]
        self.assertEqual((weak["num_train"], weak["num_val"]), (312, 17))
        self.assertEqual(weak["requested_weak_validation_count"], 17)
        self.assertEqual(weak["effective_weak_validation_count"], 17)
        self.assertEqual(
            weak["selection_sha256"],
            "d40b361637d35c8758853adbd9772eaaac2eb08b5fdae13095e06088eec06a94",
        )
        self.assertEqual(
            (combined["num_train"], combined["num_val"]), (1262, 67)
        )
        self.assertEqual(combined["validation"]["base_weak_overlap"], 0)
        self.assertEqual(combined["validation"]["train_val_overlap"], 0)

    def test_scenario_quotas_follow_ratio_when_mining_size_changes(self) -> None:
        settings = dataclasses.replace(
            load_settings(DEFAULT_CONFIG_PATH),
            weak_validation_ratio=0.10,
            scenario_order=("ScenarioA", "ScenarioB"),
            snapshot_expectations={},
        )
        rows = [
            {
                "clip": f"A{i}",
                "scenario": "ScenarioA",
                "town": "Town01",
                "weather": "Weather0",
            }
            for i in range(21)
        ] + [
            {
                "clip": f"B{i}",
                "scenario": "ScenarioB",
                "town": "Town02",
                "weather": "Weather1",
            }
            for i in range(5)
        ]
        # Global ceil((21+5)*0.10)=3. Independent per-scenario ceil would
        # incorrectly inflate this to 4.
        self.assertEqual(
            scenario_quotas(rows, settings), {"ScenarioA": 2, "ScenarioB": 1}
        )

    def test_tiny_scenario_keeps_train_member_when_possible(self) -> None:
        settings = dataclasses.replace(
            load_settings(DEFAULT_CONFIG_PATH),
            weak_validation_ratio=0.90,
            scenario_order=("ScenarioA",),
            snapshot_expectations={},
        )
        rows = [
            {
                "clip": f"A{i}",
                "scenario": "ScenarioA",
                "town": "Town01",
                "weather": "Weather0",
            }
            for i in range(3)
        ]
        self.assertEqual(scenario_quotas(rows, settings), {"ScenarioA": 2})

    def test_minimum_coverage_can_override_a_too_small_global_target(self) -> None:
        settings = dataclasses.replace(
            load_settings(DEFAULT_CONFIG_PATH),
            weak_validation_ratio=0.05,
            scenario_order=("ScenarioA", "ScenarioB"),
            snapshot_expectations={},
        )
        rows = [
            {
                "clip": scenario,
                "scenario": scenario,
                "town": "Town01",
                "weather": "Weather0",
            }
            for scenario in settings.scenario_order
        ]
        self.assertEqual(weak_validation_target(len(rows), settings), 1)
        self.assertEqual(
            scenario_quotas(rows, settings), {"ScenarioA": 1, "ScenarioB": 1}
        )

    def test_weak500_keeps_global_five_percent_target(self) -> None:
        settings = dataclasses.replace(
            load_settings(DEFAULT_CONFIG_PATH), snapshot_expectations={}
        )
        scenarios = ("ScenarioA", "ScenarioB", "ScenarioC", "ScenarioD")
        rows = [
            {
                "clip": f"{scenario}_{index}",
                "scenario": scenario,
                "town": f"Town{index % 8:02d}",
                "weather": f"Weather{index % 27}",
            }
            for scenario in scenarios
            for index in range(125)
        ]
        quotas = scenario_quotas(rows, settings)
        self.assertEqual(weak_validation_target(len(rows), settings), 25)
        self.assertEqual(sum(quotas.values()), 25)
        self.assertEqual(len(rows) - sum(quotas.values()), 475)


if __name__ == "__main__":
    unittest.main()
