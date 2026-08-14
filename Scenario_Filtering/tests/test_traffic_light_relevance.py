"""Regression tests for trigger-volume traffic-light relevance decisions."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from traffic_light_relevance import (
    read_bbox_reliability,
    read_bbox_reliability_index,
    RelevanceConfig,
    correct_affects_ego,
    segment_intersects_trigger,
    Light,
)


def annotation(
    x: float,
    y: float,
    pole_x: float,
    light_ids: tuple[str, ...],
    trigger_x: float = 0.0,
) -> dict:
    boxes: list[dict] = [
        {
            "class": "ego_vehicle",
            "id": "ego",
            "location": [x, y, 0.0],
            "rotation": [0.0, 0.0, 0.0],
        }
    ]
    for light_id in light_ids:
        boxes.append(
            {
                "class": "traffic_light",
                "id": light_id,
                "location": [pole_x, 0.0, 4.0],
                "rotation": [0.0, 0.0, 90.0],
                "trigger_volume_location": [trigger_x, 0.0, 0.0],
                "trigger_volume_rotation": [0.0, 0.0, 0.0],
                "trigger_volume_extent": [0.5, 1.5, 1.0],
                "state": 0,
                "affects_ego": False,
            }
        )
    return {"bounding_boxes": boxes, "preserved": {"value": 7}}


def write_frames(directory: Path, pole_x: float, light_ids: tuple[str, ...]) -> None:
    directory.mkdir(parents=True)
    for index, x in enumerate((-4.0, -3.0, -2.0, -1.0, 1.0, 2.0)):
        (directory / f"{index:05d}.json").write_text(
            json.dumps(annotation(x, 0.0, pole_x, light_ids)),
            encoding="utf-8",
        )


def write_bbox_report(path: Path, light_ids: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True)
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=["clip", "frame", "tl_id", "ok_after"])
        writer.writeheader()
        for frame in range(6):
            for light_id in light_ids:
                writer.writerow(
                    {
                        "clip": ".",
                        "frame": f"{frame:05d}.json",
                        "tl_id": light_id,
                        "ok_after": 1,
                    }
                )


class TriggerIntersectionTest(unittest.TestCase):
    def test_segment_crosses_rotated_trigger_rectangle(self) -> None:
        light = Light("A", 0.0, 0.0, 0.5, 1.5, 45.0, 0.0, False)
        self.assertTrue(segment_intersects_trigger((-2.0, 0.0), (2.0, 0.0), light, 0.0))
        self.assertFalse(segment_intersects_trigger((-2.0, 3.0), (2.0, 3.0), light, 0.0))


class RelevanceCorrectionTest(unittest.TestCase):
    def run_correction(self, pole_x: float, light_ids: tuple[str, ...]) -> tuple[dict, Path]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "source"
        report = root / "reports" / "bbox.csv"
        write_frames(source, pole_x, light_ids)
        write_bbox_report(report, light_ids)
        result = correct_affects_ego(
            "Synthetic_Town04_Route1_Weather0",
            source,
            root / "corrected",
            root / "reports",
            bbox_detail_csv=report,
            config=RelevanceConfig(minimum_temporal_run_frames=3),
        )
        return result, root

    def test_high_confidence_crossing_is_auto_fixed(self) -> None:
        result, root = self.run_correction(4.0, ("A",))
        self.assertEqual(result["crossing_events"], 1)
        self.assertEqual(result["auto_fix_frames"], 4)
        self.assertEqual(result["review_frames"], 0)
        for frame in range(4):
            data = json.loads((root / "corrected" / f"{frame:05d}.json").read_text())
            light = next(box for box in data["bounding_boxes"] if box["class"] == "traffic_light")
            self.assertTrue(light["affects_ego"])
            self.assertEqual(data["preserved"], {"value": 7})

    def test_heading_mismatch_is_reviewed_without_mutation(self) -> None:
        result, root = self.run_correction(-4.0, ("A",))
        self.assertEqual(result["auto_fix_frames"], 0)
        self.assertGreater(result["review_frames"], 0)
        for frame in range(6):
            data = json.loads((root / "corrected" / f"{frame:05d}.json").read_text())
            light = next(box for box in data["bounding_boxes"] if box["class"] == "traffic_light")
            self.assertFalse(light["affects_ego"])

    def test_simultaneous_distinct_trigger_candidates_are_reviewed(self) -> None:
        result, _ = self.run_correction(4.0, ("A", "B"))
        self.assertEqual(result["crossing_events"], 2)
        self.assertEqual(result["auto_fix_frames"], 0)
        self.assertGreater(result["review_frames"], 0)

    def test_affects_ego_stays_true_until_trigger_volume_exit(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "source"
        source.mkdir()
        positions = (-2.0, -1.0, -0.4, 0.0, 0.4, 0.6, 1.0)
        for index, x in enumerate(positions):
            (source / f"{index:05d}.json").write_text(
                json.dumps(annotation(x, 0.0, 4.0, ("A",))),
                encoding="utf-8",
            )
        bbox = root / "reports" / "bbox.csv"
        bbox.parent.mkdir(parents=True)
        with bbox.open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(
                file, fieldnames=["clip", "frame", "tl_id", "ok_after"]
            )
            writer.writeheader()
            for index in range(len(positions)):
                writer.writerow(
                    {
                        "clip": ".",
                        "frame": f"{index:05d}.json",
                        "tl_id": "A",
                        "ok_after": 1,
                    }
                )
        result = correct_affects_ego(
            "Synthetic_Town04_Route1_Weather0",
            source,
            root / "corrected",
            root / "reports",
            bbox_detail_csv=bbox,
        )
        self.assertEqual(result["auto_fix_frames"], 5)
        values = []
        for index in range(len(positions)):
            data = json.loads(
                (root / "corrected" / f"{index:05d}.json").read_text()
            )
            light = next(
                box
                for box in data["bounding_boxes"]
                if box["class"] == "traffic_light"
            )
            values.append(light["affects_ego"])
        self.assertEqual(values, [True, True, True, True, True, False, False])

        with (root / "reports" / "relevance_events.csv").open(
            newline="", encoding="utf-8-sig"
        ) as file:
            event = next(csv.DictReader(file))
        self.assertEqual(event["trigger_entry_frame"], "00002")
        self.assertEqual(event["trigger_center_frame"], "00003")
        self.assertEqual(event["trigger_exit_frame"], "00005")
        self.assertEqual(event["end_frame"], "00004")

    def test_later_event_starts_at_previous_trigger_exit(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "source"
        source.mkdir()
        positions = (
            -2.0,
            -1.0,
            -0.4,
            0.0,
            0.4,
            0.6,
            1.0,
            2.0,
            3.0,
            3.6,
            4.0,
            4.4,
            4.6,
            5.0,
        )
        for index, x in enumerate(positions):
            data = annotation(x, 0.0, 4.0, ("A", "B"))
            light_b = next(
                box
                for box in data["bounding_boxes"]
                if box.get("class") == "traffic_light" and box.get("id") == "B"
            )
            light_b["location"] = [8.0, 0.0, 4.0]
            light_b["trigger_volume_location"] = [4.0, 0.0, 0.0]
            (source / f"{index:05d}.json").write_text(
                json.dumps(data), encoding="utf-8"
            )

        bbox = root / "reports" / "bbox.csv"
        bbox.parent.mkdir(parents=True)
        with bbox.open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(
                file, fieldnames=["clip", "frame", "tl_id", "ok_after"]
            )
            writer.writeheader()
            for index in range(len(positions)):
                for light_id in ("A", "B"):
                    writer.writerow(
                        {
                            "clip": ".",
                            "frame": f"{index:05d}.json",
                            "tl_id": light_id,
                            "ok_after": 1,
                        }
                    )
        result = correct_affects_ego(
            "Synthetic_Town04_Route1_Weather0",
            source,
            root / "corrected",
            root / "reports",
            bbox_detail_csv=bbox,
        )
        self.assertEqual(result["crossing_events"], 2)
        self.assertEqual(result["review_frames"], 0)
        with (root / "reports" / "relevance_events.csv").open(
            newline="", encoding="utf-8-sig"
        ) as file:
            events = list(csv.DictReader(file))
        self.assertEqual(events[0]["traffic_light_id"], "A")
        self.assertEqual(events[0]["trigger_exit_frame"], "00005")
        self.assertEqual(events[0]["end_frame"], "00004")
        self.assertEqual(events[1]["traffic_light_id"], "B")
        self.assertEqual(events[1]["start_frame"], "00005")
        self.assertEqual(events[1]["trigger_exit_frame"], "00012")


if __name__ == "__main__":
    unittest.main()


class BboxReliabilityIndexTest(unittest.TestCase):
    """Grouping the CSV once must answer exactly what asking per clip did."""

    HEADER = "clip,frame,tl_id,ok_after\n"
    ROWS = [
        "clipA,00000.json,11,1\n",
        "clipA,00000.json,12,0\n",
        "clipA,00001.json,11,1\n",
        "clipB,00000.json,11,0\n",
        "clipB,00007.json,99,1\n",
    ]

    def _csv(self, directory: Path) -> Path:
        path = directory / "bbox_details.csv"
        path.write_text(self.HEADER + "".join(self.ROWS), encoding="utf-8-sig")
        return path

    def test_index_matches_the_per_clip_reader(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._csv(Path(directory))
            index = read_bbox_reliability_index(path)
            self.assertEqual(sorted(index), ["clipA", "clipB"])
            for clip in ("clipA", "clipB", "missing"):
                self.assertEqual(
                    index.get(clip, {}), read_bbox_reliability(path, clip)
                )

    def test_frame_suffix_is_stripped_and_flag_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            index = read_bbox_reliability_index(self._csv(Path(directory)))
            self.assertEqual(
                index["clipA"], {("00000", "11"): True, ("00000", "12"): False,
                                 ("00001", "11"): True},
            )

    def test_absent_csv_is_empty_not_an_error(self) -> None:
        self.assertEqual(read_bbox_reliability_index(None), {})
        self.assertEqual(
            read_bbox_reliability_index(Path("/nonexistent/bbox_details.csv")), {}
        )
