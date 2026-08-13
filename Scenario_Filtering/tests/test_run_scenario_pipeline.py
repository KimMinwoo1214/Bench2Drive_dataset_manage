import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

import run_scenario_pipeline as pipeline


class VadVectorPipelineArgumentsTest(unittest.TestCase):
    def test_parser_uses_b2d_dataset_default_pc_range(self):
        args = pipeline.build_parser().parse_args(
            ["--input", "input", "--output", "output"]
        )

        self.assertEqual(args.point_cloud_range, list(pipeline.DEFAULT_PC_RANGE))

    def test_generate_forwards_point_cloud_range(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            maps_root = root / "maps"
            maps_root.mkdir()
            (maps_root / "Town15_HD_map.npz").touch()
            anno_dir = root / "anno"
            anno_dir.mkdir()
            clip_output = root / "output"
            args = SimpleNamespace(
                maps_root=maps_root,
                vad_vector_stride=2,
                point_cloud_range=[-15.0, -30.0, -2.0, 15.0, 30.0, 2.0],
                video=False,
                fps=10.0,
            )
            captured = []

            def fake_run(command, _label):
                captured.extend(command)
                vectors = clip_output / "vad_vector_gt" / "vectors"
                images = clip_output / "vad_vector_gt" / "visualization"
                vectors.mkdir(parents=True)
                images.mkdir(parents=True)
                np.savez(vectors / "00000.npz", points=np.zeros((0, 20, 2)))
                (images / "scenario_00000.png").touch()

            with mock.patch.object(pipeline, "run_command", side_effect=fake_run):
                pipeline.generate_vad_vector_gt(
                    "Scenario_Town15_Route1_Weather1",
                    anno_dir,
                    clip_output,
                    args,
                )

            option_index = captured.index("--point-cloud-range")
            self.assertEqual(
                captured[option_index + 1:option_index + 7],
                ["-15.0", "-30.0", "-2.0", "15.0", "30.0", "2.0"],
            )


if __name__ == "__main__":
    unittest.main()
