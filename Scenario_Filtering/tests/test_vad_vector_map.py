import unittest

import numpy as np

import visualize_vad_gt as vad


class VadVectorMapGeometryTest(unittest.TestCase):
    def test_default_pc_range_matches_b2d_dataset_default(self):
        np.testing.assert_array_equal(
            vad.DEFAULT_PC_RANGE,
            np.array([-51.2, -51.2, -5.0, 51.2, 51.2, 3.0]),
        )

    def test_stopline_is_perpendicular_and_upstream(self):
        trigger = np.array(
            [
                [-2.0, -1.0, 0.0],
                [2.0, -1.0, 0.0],
                [2.0, 3.0, 0.0],
                [-2.0, 3.0, 0.0],
            ]
        )
        segment = vad._trigger_edge_to_stopline(trigger, np.array([0.0, 1.0]))

        self.assertIsNotNone(segment)
        actual = np.stack(segment)
        self.assertTrue(np.allclose(actual[:, 1], -1.0))
        self.assertAlmostEqual(abs(actual[1, 0] - actual[0, 0]), 4.0)

    def test_lane_crop_splits_reentry_and_clips_to_boundary(self):
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
        map_infos = {
            "Town01": {
                "lane_points": [lane],
                "lane_sample_points": [lane],
                "lane_types": ["Center"],
                "stopline_points": [],
                "stopline_types": [],
            }
        }
        info = {
            "town_name": "Town01",
            "sensors": {"LIDAR_TOP": {"world2lidar": np.eye(4)}},
        }
        pc_range = np.array([-1.0, -1.0, -1.0, 1.0, 3.0, 1.0])

        vectors = vad.map_vectors(info, map_infos, pc_range)

        self.assertEqual(len(vectors), 2)
        first, second = [points for _, points, _ in vectors]
        np.testing.assert_allclose(first[[0, -1]], [[-1.0, 0.0], [1.0, 0.0]])
        np.testing.assert_allclose(second[[0, -1]], [[1.0, 2.0], [-1.0, 2.0]])

    def test_boundary_stopline_is_clipped_and_open(self):
        map_infos = {
            "Town01": {
                "lane_points": [],
                "lane_sample_points": [],
                "lane_types": [],
                "stopline_points": [
                    np.array([[0.0, 0.0, 0.0], [2.0, 0.0, 0.0]])
                ],
                "stopline_types": ["TrafficLight"],
            }
        }
        info = {
            "town_name": "Town01",
            "sensors": {"LIDAR_TOP": {"world2lidar": np.eye(4)}},
        }
        pc_range = np.array([-1.0, -1.0, -1.0, 1.0, 1.0, 1.0])

        vectors = vad.map_vectors(info, map_infos, pc_range)

        self.assertEqual(len(vectors), 1)
        typ, points, is_closed = vectors[0]
        self.assertEqual(typ, "TrafficLight")
        self.assertFalse(is_closed)
        np.testing.assert_allclose(points, [[0.0, 0.0], [1.0, 0.0]])


if __name__ == "__main__":
    unittest.main()
