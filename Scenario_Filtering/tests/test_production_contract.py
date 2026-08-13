"""Regression tests for manifest, completion, resume, and approval contracts."""

from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path

from production_contract import (
    annotation_digest,
    build_completion,
    completion_errors,
    load_approvals,
    load_manifest,
    matching_approval,
    read_completion,
    validate_source_inventory,
    write_completion,
)


class ProductionContractTest(unittest.TestCase):
    def _annotation(self, root: Path, clip: str, value: int = 1) -> Path:
        anno = root / clip / "anno"
        anno.mkdir(parents=True, exist_ok=True)
        with gzip.GzipFile(anno / "00000.json.gz", "wb", mtime=0) as file:
            file.write(json.dumps({"value": value}).encode("utf-8"))
        return anno

    def _manifest(self, root: Path) -> tuple[Path, str, str]:
        base = "Base_Town04_Route1_Weather0"
        weak = "Weak_Town10HD_Route2_Weather26"
        path = root / "split.json"
        path.write_text(
            json.dumps(
                {
                    "train": [base],
                    "val": [weak],
                    "components": {
                        "base": {"train": [base], "val": []},
                        "weak": {"train": [], "val": [weak]},
                    },
                }
            )
        )
        return path, base, weak

    def test_component_selection_preserves_explicit_split(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, base, weak = self._manifest(root)
            self.assertEqual(load_manifest(path, "base").clips, (base,))
            self.assertEqual(load_manifest(path, "weak").clips, (weak,))
            self.assertEqual(load_manifest(path, "all").clips, (base, weak))

    def test_component_overlap_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, base, _ = self._manifest(root)
            raw = json.loads(path.read_text())
            raw["components"]["weak"]["val"] = [base]
            path.write_text(json.dumps(raw))
            with self.assertRaisesRegex(ValueError, "overlap"):
                load_manifest(path, "all")

    def test_quality_filtered_manifest_allows_only_declared_source_extras(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, base, weak = self._manifest(root)
            excluded = "Excluded_Town04_Route3_Weather0"
            raw = json.loads(path.read_text())
            raw.update(
                {
                    "policy": "preserve_parent_membership_remove_excluded_no_backfill",
                    "excluded": [excluded],
                    "excluded_by_component": {"base": [excluded], "weak": []},
                }
            )
            path.write_text(json.dumps(raw))
            selection = load_manifest(path, "base")
            self.assertEqual(selection.allowed_source_extras, frozenset({excluded}))
            source = root / "source"
            self._annotation(source, base)
            self._annotation(source, excluded)
            validate_source_inventory(
                source, selection.clips, selection.allowed_source_extras
            )
            self._annotation(source, weak)
            with self.assertRaisesRegex(ValueError, "unexpected"):
                validate_source_inventory(
                    source, selection.clips, selection.allowed_source_extras
                )

    def test_completion_detects_input_and_output_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_anno = self._annotation(root / "input", "Clip")
            output_anno = self._annotation(root / "output", "Clip")
            input_hash, input_frames = annotation_digest(input_anno)
            output_hash, output_frames = annotation_digest(output_anno)
            completion = build_completion(
                clip="Clip",
                component="base",
                manifest_sha256="manifest",
                config_sha256="config",
                implementation_sha256="implementation",
                input_annotation_sha256=input_hash,
                output_annotation_sha256=output_hash,
                input_frames=input_frames,
                output_frames=output_frames,
                status="completed",
            )
            path = root / "completion.json"
            write_completion(path, completion)
            loaded = read_completion(path)
            self.assertEqual(loaded, completion)
            changed_hash, changed_frames = annotation_digest(
                self._annotation(root / "input", "Clip", value=2)
            )
            errors = completion_errors(
                loaded,
                clip="Clip",
                component="base",
                manifest_sha256="manifest",
                config_sha256="config",
                implementation_sha256="implementation",
                input_annotation_sha256=changed_hash,
                input_frames=changed_frames,
                output_annotation_sha256=output_hash,
                output_frames=output_frames,
            )
            self.assertTrue(any("input_annotation_sha256" in error for error in errors))

    def test_approval_matches_only_exact_completion_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "approvals.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "approvals": [
                            {
                                "clip": "Clip",
                                "component": "weak",
                                "completion_sha256": "abc",
                                "approved_by": "reviewer",
                                "reason": "visually checked",
                            }
                        ],
                    }
                )
            )
            approvals = load_approvals(path)
            self.assertIsNotNone(
                matching_approval(
                    approvals,
                    {
                        "clip": "Clip",
                        "component": "weak",
                        "completion_sha256": "abc",
                    },
                )
            )
            self.assertIsNone(
                matching_approval(
                    approvals,
                    {
                        "clip": "Clip",
                        "component": "weak",
                        "completion_sha256": "changed",
                    },
                )
            )


if __name__ == "__main__":
    unittest.main()
