"""Tests for the read-only headless frame benchmark."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


from plugin_loader import plugin
import importlib
benchmark = importlib.import_module(plugin.__name__ + ".benchmark")


class BenchmarkUnitTests(unittest.TestCase):
    def test_statistics_and_comparison(self):
        samples = [{"elapsed_ms": value} for value in (100.0, 200.0, 300.0, 400.0)]
        stats = benchmark._summary(samples)
        self.assertEqual(stats["average_evaluation_ms"], 250.0)
        self.assertEqual(stats["median_evaluation_ms"], 250.0)
        self.assertEqual(stats["p95_evaluation_ms"], 400.0)
        self.assertEqual(stats["estimated_headless_evaluation_frames_per_second"], 4.0)
        comparison = benchmark._compare({"statistics": stats}, {"statistics": {
            **stats, "average_evaluation_ms": 100.0,
            "median_evaluation_ms": 100.0, "p95_evaluation_ms": 200.0,
            "estimated_headless_evaluation_frames_per_second": 10.0,
        }})
        self.assertEqual(comparison["average_evaluation_time_reduction_percent"], 60.0)
        self.assertEqual(comparison["estimated_headless_throughput_change_percent"], 150.0)
        self.assertIn("not interactive viewport FPS", benchmark._plain_summary(
            {"statistics": stats}, {"statistics": {**stats, "average_evaluation_ms": 100.0}},
            comparison))

    def test_timing_spike_is_visible_and_conflicting_result_is_cautioned(self):
        before_samples = [{"frame": frame, "elapsed_ms": 15.0}
                          for _ in range(3) for frame in (1, 2)]
        after_samples = [{"frame": frame, "elapsed_ms": 10.0}
                         for _ in range(3) for frame in (1, 2)]
        after_samples[1]["elapsed_ms"] = 130.0
        before = {"statistics": benchmark._summary(before_samples),
                  "typical_statistics": benchmark._typical_summary(before_samples)}
        after = {"statistics": benchmark._summary(after_samples),
                 "typical_statistics": benchmark._typical_summary(after_samples)}
        comparison = benchmark._compare(before, after)
        self.assertTrue(comparison["raw_and_typical_direction_conflict"])
        self.assertTrue(comparison["outlier_warning"])
        self.assertEqual(after["statistics"]["outlier_count_over_3x_median"], 1)
        self.assertIn("inconclusive", benchmark._plain_summary(before, after, comparison))

    def test_invalid_range_and_missing_blender(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "fixture.blend"
            source.write_bytes(b"fixture")
            invalid = json.loads(plugin.tools.benchmark_performance({
                "blend_file": str(source), "frame_start": 3, "frame_end": 2,
            }))
            self.assertEqual(invalid["error"]["code"], "benchmark_failed")
            missing = json.loads(plugin.tools.benchmark_performance({
                "blend_file": str(source), "frame_start": 1, "frame_end": 2,
                "blender_executable": str(Path(directory) / "missing-blender"),
            }))
            self.assertEqual(missing["error"]["code"], "blender_not_found")

    def test_subprocess_timeout_returns_json(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "fixture.blend"
            source.write_bytes(b"fixture")
            args = {"blend_file": str(source), "frame_start": 1, "frame_end": 2,
                    "blender_executable": sys.executable, "timeout_seconds": 10}
            with patch.object(benchmark.subprocess, "run",
                              side_effect=subprocess.TimeoutExpired("blender", 10)):
                result = json.loads(plugin.tools.benchmark_performance(args))
            self.assertEqual(result["error"]["code"], "blender_timeout")
            self.assertEqual(result["stage"], "baseline")


class BenchmarkIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.blender = shutil.which(os.environ.get("BLENDER_EXECUTABLE", "blender"))
        if not cls.blender:
            raise unittest.SkipTest("Set BLENDER_EXECUTABLE to run Blender integration tests")

    def test_pair_is_read_only_and_exports_json(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "original.blend"
            optimized = Path(directory) / "optimized.blend"
            report_file = Path(directory) / "benchmark.json"
            expression = (
                "import bpy; s=bpy.context.scene; c=next(iter(s.collection.children)); "
                "cube=bpy.data.objects['Cube']; "
                "cube.location.x=0; cube.keyframe_insert(data_path='location', frame=1); "
                "cube.location.x=3; cube.keyframe_insert(data_path='location', frame=3); "
                f"bpy.ops.wm.save_as_mainfile(filepath={str(original)!r}); "
                "c.hide_viewport=True; "
                f"bpy.ops.wm.save_as_mainfile(filepath={str(optimized)!r})"
            )
            created = subprocess.run(
                [self.blender, "--background", "--factory-startup", "--python-exit-code", "1",
                 "--python-expr", expression], capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            before = [hashlib.sha256(path.read_bytes()).hexdigest() for path in (original, optimized)]
            result = json.loads(plugin.tools.benchmark_performance({
                "blend_file": str(original), "optimized_file": str(optimized),
                "frame_start": 1, "frame_end": 3, "warmup_frames": 2,
                "passes": 2,
                "blender_executable": self.blender, "report_file": str(report_file),
            }))
            self.assertTrue(result["ok"], result)
            self.assertFalse(result["interactive_viewport_fps_measured"])
            self.assertIn("not interactive viewport FPS", result["plain_language_summary"])
            self.assertEqual(result["baseline"]["statistics"]["sample_count"], 6)
            self.assertEqual(result["optimized"]["statistics"]["sample_count"], 6)
            self.assertTrue(all(sample["visible_mesh_objects"] == 1
                                for sample in result["baseline"]["samples"]))
            self.assertTrue(all(sample["visible_mesh_objects"] == 0
                                for sample in result["optimized"]["samples"]))
            self.assertIsNotNone(result["comparison"])
            self.assertEqual(result["conditions"]["warmup_frames_per_run"], 2)
            self.assertEqual(result["conditions"]["passes_per_run"], 2)
            self.assertEqual(json.loads(report_file.read_text(encoding="utf-8"))["report_version"], 1)
            self.assertEqual(before, [hashlib.sha256(path.read_bytes()).hexdigest()
                                      for path in (original, optimized)])
            again = json.loads(plugin.tools.benchmark_performance({
                "blend_file": str(original), "frame_start": 1, "frame_end": 2,
                "blender_executable": self.blender, "report_file": str(report_file),
            }))
            self.assertFalse(again["ok"])


if __name__ == "__main__":
    unittest.main()
