import unittest

import numpy as np

import visualize_vad_gt as vad


class VadVectorMapGeometryTest(unittest.TestCase):
    def test_default_pc_range_matches_b2d_dataset_default(self):
        np.testing.assert_array_equal(
            vad.DEFAULT_PC_RANGE,
            np.array([-51.2, -51.2, -5.0, 51.2, 51.2, 3.0]),
        )

    # map_vectors must reproduce B2D_VAD_Dataset.get_map_info() exactly, so these
    # tests pin the loader's semantics rather than a nicer-looking geometry:
    # lane points outside the range are dropped (never clipped or split), and a
    # trigger volume survives only if every corner is in range.
    @staticmethod
    def _map_infos(lanes=(), lane_types=(), triggers=(), trigger_types=()):
        return {
            "Town01": {
                "lane_points": list(lanes),
                "lane_sample_points": list(lanes),
                "lane_types": list(lane_types),
                "trigger_volumes_points": list(triggers),
                "trigger_volumes_types": list(trigger_types),
            }
        }

    @staticmethod
    def _info():
        return {
            "town_name": "Town01",
            "sensors": {"LIDAR_TOP": {"world2lidar": np.eye(4)}},
        }

    def test_out_of_range_lane_points_are_dropped_not_clipped(self):
        lane = np.array(
            [
                [-2.0, 0.0, 0.0],
                [0.0, 0.0, 0.0],
                [2.0, 0.0, 0.0],
                [2.0, 2.0, 0.0],
                [0.0, 2.0, 0.0],
                [-2.0, 2.0, 0.0],
            ]
        )
        pc_range = np.array([-1.0, -1.0, -1.0, 1.0, 3.0, 1.0])

        vectors = vad.map_vectors(
            self._info(), self._map_infos([lane], ["Center"]), pc_range
        )

        # The two surviving points are connected into one polyline; the loader
        # never interpolates a boundary point and never splits on re-entry.
        self.assertEqual(len(vectors), 1)
        typ, points, is_closed = vectors[0]
        self.assertEqual(typ, "Center")
        self.assertFalse(is_closed)
        np.testing.assert_allclose(points, [[0.0, 0.0], [0.0, 2.0]])

    def test_fully_contained_trigger_volume_stays_a_closed_polygon(self):
        trigger = np.array(
            [
                [-0.5, -0.5, 0.0],
                [0.5, -0.5, 0.0],
                [0.5, 0.5, 0.0],
                [-0.5, 0.5, 0.0],
            ]
        )
        pc_range = np.array([-1.0, -1.0, -1.0, 1.0, 1.0, 1.0])

        vectors = vad.map_vectors(
            self._info(),
            self._map_infos(triggers=[trigger], trigger_types=["TrafficLight"]),
            pc_range,
        )

        self.assertEqual(len(vectors), 1)
        typ, points, _ = vectors[0]
        self.assertEqual(typ, "TrafficLight")
        self.assertEqual(len(points), 5)
        np.testing.assert_allclose(points[0], points[-1])

    def test_partially_out_of_range_trigger_volume_is_dropped(self):
        trigger = np.array(
            [
                [-2.0, -0.5, 0.0],
                [0.5, -0.5, 0.0],
                [0.5, 0.5, 0.0],
                [-2.0, 0.5, 0.0],
            ]
        )
        pc_range = np.array([-1.0, -1.0, -1.0, 1.0, 1.0, 1.0])

        vectors = vad.map_vectors(
            self._info(),
            self._map_infos(triggers=[trigger], trigger_types=["TrafficLight"]),
            pc_range,
        )

        self.assertEqual(vectors, [])


if __name__ == "__main__":
    unittest.main()
