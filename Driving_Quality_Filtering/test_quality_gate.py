"""Tests for read-only geometry, calibration, classification, and split contracts."""

from __future__ import annotations

import gzip
import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path
from typing import Sequence

import numpy as np

from audit_expert_driving import actor_category, audit_clip
from classify_quality import automatic_classification, load_decisions
from geometry import oriented_box_metrics
from materialize_depth_patches import materialize
from quality_contract import (
    canonical_sha256,
    filtered_split,
    load_config,
    load_manifest,
)
from summarize_calibration import metric_distribution, percentile, ranking_rows


def box(x: float, y: float, z: float = 0.0, yaw: float = 0.0) -> dict:
    return {
        "center": [x, y, z],
        "extent": [1.0, 1.0, 1.0],
        "rotation": [0.0, 0.0, yaw],
    }


def production_config() -> dict:
    return {
        "schema_version": 1,
        "mode": "production",
        "sample_hz": 10.0,
        "collision_categories": ["vehicle", "pedestrian", "bicycle"],
        "sensor_validation_policy": "inventory_and_nonzero_size_all_signature_first_middle_last",
        "required_sensor_keys": ["CAM_FRONT"],
        "required_rgb_folders": ["rgb_front"],
        "required_depth_folders": ["depth_front"],
        "collision": {
            "review_minimum_penetration_m": 0.1,
            "review_minimum_iou": 0.005,
            "review_minimum_consecutive_frames": 2,
            "exclude_minimum_penetration_m": 0.2,
            "exclude_minimum_iou": 0.01,
            "exclude_minimum_consecutive_frames": 3,
            "exclude_severe_single_frame_penetration_m": 1.0,
        },
    }


class GeometryTest(unittest.TestCase):
    def test_rotated_boxes_do_not_use_axis_aligned_overlap(self) -> None:
        metrics = oriented_box_metrics(box(0, 0, yaw=45), box(3, 0, yaw=-45))
        self.assertFalse(metrics["positive_3d_overlap"])
        self.assertGreater(metrics["bev_clearance_m"], 0.0)

    def test_touching_is_not_positive_overlap(self) -> None:
        metrics = oriented_box_metrics(box(0, 0), box(2, 0))
        self.assertFalse(metrics["positive_3d_overlap"])
        self.assertEqual(metrics["bev_penetration_m"], 0.0)

    def test_shallow_and_deep_penetration_are_ordered(self) -> None:
        shallow = oriented_box_metrics(box(0, 0), box(1.9, 0))
        deep = oriented_box_metrics(box(0, 0), box(0.5, 0))
        self.assertTrue(shallow["positive_3d_overlap"])
        self.assertGreater(deep["bev_penetration_m"], shallow["bev_penetration_m"])

    def test_z_separation_rejects_overpass_false_positive(self) -> None:
        metrics = oriented_box_metrics(box(0, 0, z=0), box(0, 0, z=3))
        self.assertGreater(metrics["bev_intersection_area_m2"], 0.0)
        self.assertFalse(metrics["positive_3d_overlap"])


class AuditTest(unittest.TestCase):
    @staticmethod
    def _media(path: Path, header: bytes, trailer: bytes = b"") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(header + b"test" + trailer)

    def _audit(
        self, root: Path, annotations: Sequence[dict], actions: Sequence[int] = (5, 0)
    ) -> dict:
        """Materialize a minimal readable clip and audit it read-only."""
        clip = "Scenario_Town04_Route1_Weather0"
        clip_dir = root / "base" / clip
        # Present so the unrelated MAP_MISSING review issue stays out of the way.
        (root / "maps").mkdir(parents=True, exist_ok=True)
        (root / "maps" / "Town04_HD_map.npz").touch()
        for frame, (annotation, action) in enumerate(zip(annotations, actions)):
            stem = f"{frame:05d}"
            anno = clip_dir / "anno" / f"{stem}.json.gz"
            anno.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(str(anno), "wt", encoding="utf-8") as file:
                json.dump(annotation, file)
            self._media(clip_dir / "camera" / "rgb_front" / f"{stem}.jpg", b"\xff\xd8", b"\xff\xd9")
            self._media(clip_dir / "camera" / "depth_front" / f"{stem}.png", b"\x89PNG\r\n\x1a\n")
            self._media(clip_dir / "lidar" / f"{stem}.laz", b"LASF0000")
            self._media(clip_dir / "radar" / f"{stem}.h5", b"\x89HDF\r\n\x1a\n")
            expert_stem = "-0001" if frame == 0 else f"{frame - 1:05d}"
            expert = clip_dir / "expert_assessment" / f"{expert_stem}.npz"
            expert.parent.mkdir(parents=True, exist_ok=True)
            np.savez(str(expert), arr_0=np.asarray([float(action)], dtype=np.float32))
        return audit_clip(
            {
                "record": {
                    "name": clip, "component": "base", "split": "train",
                    "scenario": "Scenario", "town": "Town04", "weather": "Weather0",
                },
                "config": {
                    "sample_hz": 10.0,
                    "sensor_validation_policy": (
                        "inventory_and_nonzero_size_all_signature_first_middle_last"
                    ),
                    "required_sensor_keys": ["CAM_FRONT"],
                    "required_rgb_folders": ["rgb_front"],
                    "required_depth_folders": ["depth_front"],
                },
                "base_root": str(root / "base"),
                "weak_root": str(root / "weak"),
                "map_root": str(root / "maps"),
            }
        )

    @staticmethod
    def _annotation(frame: int, **overrides) -> dict:
        annotation = {
            "x": float(frame), "y": 0.0,
            "theta": 3.13 if frame == 0 else -3.13,
            "sensors": {"CAM_FRONT": {"intrinsic": [[1, 0, 0], [0, 1, 0], [0, 0, 1]]}},
            "bounding_boxes": [
                {"class": "ego_vehicle", "id": "ego", **box(float(frame), 0.0)}
            ],
        }
        annotation.update(overrides)
        return annotation

    def test_previous_expert_action_alignment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self._audit(root, [self._annotation(0), self._annotation(1)])
            self.assertEqual(result["frames"][1]["x"], 1.0)
            self.assertEqual([row["expert_action_id"] for row in result["frames"]], [5, 0])

    def test_nonfinite_top_level_pose_does_not_block_collision_audit(self) -> None:
        """Approved policy: top-level x/y/theta never gate a clip.

        No quality decision reads the ego pose, so a NaN there is recorded and
        counted but must not be structurally fatal and must not stop the frame's
        bbox collision scan.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            overlapping = [
                {"class": "ego_vehicle", "id": "ego", **box(0.0, 0.0)},
                {"class": "vehicle", "id": "other", **box(0.5, 0.0)},
            ]
            result = self._audit(
                root,
                [
                    self._annotation(0, theta=float("nan"), bounding_boxes=overlapping),
                    self._annotation(1),
                ],
            )
            metrics = result["metrics"]
            self.assertEqual(metrics["structural_fatal_count"], 0)
            self.assertEqual(metrics["structural_review_count"], 0)
            self.assertEqual(metrics["nonfinite_ego_state_frames"], 1)
            self.assertIsNone(result["frames"][0]["theta_rad"])
            codes = {event["code"]: event["severity"] for event in result["events"]}
            self.assertEqual(codes["EGO_STATE_NONFINITE"], "note")
            # The whole point of the policy: the overlap is still measured.
            self.assertEqual(metrics["positive_overlap_frames"], 1)
            self.assertIn("BBOX_3D_OVERLAP", codes)

    def test_nonfinite_bounding_box_is_still_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            broken = [{"class": "ego_vehicle", "id": "ego", **box(float("nan"), 0.0)}]
            result = self._audit(
                root, [self._annotation(0, bounding_boxes=broken), self._annotation(1)]
            )
            self.assertEqual(result["metrics"]["structural_fatal_count"], 1)
            self.assertEqual(result["metrics"]["nonfinite_ego_state_frames"], 0)
            self.assertIn("ANNOTATION_NONFINITE", result["metrics"]["issue_codes"])

    def test_nonfinite_sensor_calibration_is_still_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sensors = {"CAM_FRONT": {"intrinsic": [[float("nan"), 0, 0]]}}
            result = self._audit(
                root, [self._annotation(0, sensors=sensors), self._annotation(1)]
            )
            self.assertEqual(result["metrics"]["structural_fatal_count"], 1)
            self.assertIn("ANNOTATION_NONFINITE", result["metrics"]["issue_codes"])

    def test_actor_categories_are_limited_to_three_dynamic_groups(self) -> None:
        self.assertEqual(actor_category({"class": "vehicle"}), "vehicle")
        self.assertEqual(actor_category({"class": "walker"}), "pedestrian")
        self.assertEqual(actor_category({"class": "pedestrian"}), "pedestrian")
        self.assertEqual(
            actor_category(
                {"class": "vehicle", "base_type": "bicycle", "type_id": "vehicle.bh.crossbike"}
            ),
            "bicycle",
        )
        self.assertIsNone(actor_category({"class": "traffic_sign"}))


class DepthPatchMaterializationTest(unittest.TestCase):
    @staticmethod
    def _png() -> bytes:
        return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", 1600, 900)

    def test_copy_only_missing_files_and_preserve_patch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            weak = root / "weak"
            patch = root / "patch"
            rows = []
            for index in range(2):
                clip = f"Scenario{index}_Town01_Route{index}_Weather0"
                stream = "depth_front" if index == 0 else "depth_front_right"
                for frame in range(2):
                    annotation = weak / clip / "anno" / f"{frame:05d}.json.gz"
                    annotation.parent.mkdir(parents=True, exist_ok=True)
                    annotation.write_bytes(b"annotation")
                    source = patch / clip / "camera" / stream / f"{frame:05d}.png"
                    source.parent.mkdir(parents=True, exist_ok=True)
                    source.write_bytes(self._png() + bytes([frame, index]))
                existing = weak / clip / "camera" / stream / "00000.png"
                existing.parent.mkdir(parents=True, exist_ok=True)
                existing.write_bytes((patch / clip / "camera" / stream / "00000.png").read_bytes())
                missing = "00001.png"
                rows.append(
                    {
                        "clip": clip,
                        "stream": stream,
                        "expected_frames": 2,
                        "expected_missing_before": 1,
                        "missing_names_sha256": hashlib.sha256((missing + "\n").encode("utf-8")).hexdigest(),
                    }
                )
            contract = root / "contract.json"
            contract.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "policy": "copy_missing_only_no_overwrite_preserve_patch",
                        "streams": rows,
                    }
                ),
                encoding="utf-8",
            )
            output = root / "result" / "manifest.json"
            result = materialize(weak, patch, contract, output, apply=True)
            self.assertEqual(result["copied_total"], 2)
            self.assertTrue(result["patch_preserved"])
            self.assertTrue(output.is_file())
            for row in rows:
                destination = weak / row["clip"] / "camera" / row["stream"] / "00001.png"
                source = patch / row["clip"] / "camera" / row["stream"] / "00001.png"
                self.assertEqual(destination.read_bytes(), source.read_bytes())


class CalibrationSummaryTest(unittest.TestCase):
    def test_percentiles_are_descriptive_linear_interpolation(self) -> None:
        self.assertEqual(percentile([0.0, 10.0], 0.50), 5.0)
        distribution = metric_distribution(
            [{"max_bev_penetration_m": 1.0}, {"max_bev_penetration_m": None}],
            "max_bev_penetration_m",
        )
        self.assertEqual(distribution["value_count"], 1)
        self.assertEqual(distribution["missing_count"], 1)

    def test_rankings_keep_every_available_value_without_cutoff(self) -> None:
        base = {
            "component": "base", "split": "train", "scenario": "S", "town": "Town01"
        }
        rows = [
            {**base, "clip": "low", "max_bev_penetration_m": 1.0},
            {**base, "clip": "high", "max_bev_penetration_m": 3.0},
        ]
        ranked = [
            row for row in ranking_rows(rows)
            if row["metric"] == "max_bev_penetration_m"
        ]
        self.assertEqual([row["clip"] for row in ranked], ["high", "low"])
        self.assertEqual([row["rank"] for row in ranked], [1, 2])


class ClassificationTest(unittest.TestCase):
    def result(self, **metrics: object) -> dict:
        defaults = {
            "structural_fatal_count": 0, "structural_review_count": 0,
            "positive_overlap_frames": 0,
        }
        defaults.update(metrics)
        return {"metrics": defaults, "events": []}

    def test_exclude_has_priority_over_review(self) -> None:
        status, reasons = automatic_classification(
            self.result(structural_fatal_count=1, structural_review_count=1),
            production_config(),
        )
        self.assertEqual(status, "EXCLUDE")
        self.assertIn("STRUCTURAL_FATAL", reasons)

    def test_unambiguous_data_passes(self) -> None:
        self.assertEqual(
            automatic_classification(self.result(), production_config()), ("PASS", [])
        )

    @staticmethod
    def collision_events(count: int, penetration: float, iou: float) -> list[dict]:
        return [
            {
                "event_type": "positive_3d_overlap",
                "actor_category": "vehicle",
                "actor_id": "7",
                "start_frame": frame,
                "bev_penetration_m": penetration,
                "bev_iou": iou,
            }
            for frame in range(count)
        ]

    def test_coordinate_overlap_below_review_band_passes(self) -> None:
        result = self.result(positive_overlap_frames=1)
        result["events"] = self.collision_events(1, 0.01, 0.001)
        self.assertEqual(automatic_classification(result, production_config()), ("PASS", []))

    def test_review_band_requires_manual_decision(self) -> None:
        result = self.result(positive_overlap_frames=2)
        result["events"] = self.collision_events(2, 0.15, 0.006)
        self.assertEqual(
            automatic_classification(result, production_config()),
            ("REVIEW", ["COLLISION_REVIEW_BAND"]),
        )

    def test_persistent_collision_is_excluded(self) -> None:
        result = self.result(positive_overlap_frames=3)
        result["events"] = self.collision_events(3, 0.3, 0.02)
        self.assertEqual(
            automatic_classification(result, production_config()),
            ("EXCLUDE", ["PERSISTENT_COLLISION"]),
        )

    def test_traffic_sign_event_is_ignored(self) -> None:
        result = self.result(positive_overlap_frames=1)
        result["events"] = self.collision_events(1, 2.0, 0.5)
        result["events"][0]["actor_category"] = "traffic_sign"
        self.assertEqual(automatic_classification(result, production_config()), ("PASS", []))

    def test_calibration_config_blocks_classification(self) -> None:
        path = Path(__file__).with_name("quality_config_calibration.json")
        with self.assertRaisesRegex(ValueError, "classification is blocked"):
            load_config(path, require_production=True)

    def test_stale_review_decision_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "decisions.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "metrics_sha256": "old",
                        "events_sha256": "events",
                        "decisions": [],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "stale"):
                load_decisions(path, metrics_sha256="new", events_sha256="events")


class FilteredSplitTest(unittest.TestCase):
    def test_parent_membership_is_preserved_without_backfill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "split.json"
            clips = {
                "base_train": "A_Town01_Route1_Weather0",
                "base_val": "B_Town01_Route2_Weather0",
                "weak_train": "C_Town02_Route3_Weather1",
                "weak_val": "D_Town02_Route4_Weather1",
            }
            path.write_text(
                json.dumps(
                    {
                        "dataset": "fixture",
                        "train": [clips["base_train"], clips["weak_train"]],
                        "val": [clips["base_val"], clips["weak_val"]],
                        "components": {
                            "base": {"train": [clips["base_train"]], "val": [clips["base_val"]]},
                            "weak": {"train": [clips["weak_train"]], "val": [clips["weak_val"]]},
                        },
                    }
                ), encoding="utf-8",
            )
            manifest = load_manifest(path)
            accepted = [clips["base_train"], clips["weak_train"], clips["weak_val"]]
            split = filtered_split(manifest, accepted, [clips["base_val"]])
            self.assertEqual(split["train"], [clips["base_train"], clips["weak_train"]])
            self.assertEqual(split["val"], [clips["weak_val"]])
            self.assertEqual(split["excluded_by_component"]["base"], [clips["base_val"]])


if __name__ == "__main__":
    unittest.main()
