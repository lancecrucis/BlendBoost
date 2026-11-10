"""Standard-library tests for BlendBoost's report and Blender connection."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


from plugin_loader import plugin
tools = plugin.tools


def sample_metrics():
    return {
        "blend_file": "/tmp/example.blend",
        "scene": "Scene",
        "frame": 1,
        "object_count": 1,
        "object_counts_by_type": {"MESH": 1},
        "meshes": [{"name": "Heavy", "base_polygons": 50000,
                    "evaluated_polygons": 250000, "hidden_from_viewport": False,
                    "modifier_count": 2}],
        "modifiers": [
            {"object": "Heavy", "name": "Subsurf", "type": "SUBSURF",
             "viewport_enabled": True, "viewport_levels": 3},
            {"object": "Heavy", "name": "Nodes", "type": "NODES",
             "viewport_enabled": True, "node_group": "Generate"},
        ],
        "collections": [{"path": "Scene/Heavy", "direct_object_count": 1,
                         "direct_mesh_count": 1, "direct_evaluated_polygons": 250000}],
        "images": [{"name": "Large", "width": 4096, "height": 4096,
                    "channels": 4, "is_float": False, "source": "FILE",
                    "scene_referenced": True,
                    "estimated_uncompressed_bytes": 4096 * 4096 * 4}],
        "viewports": [
            {"screen": "Layout", "area_index": 0, "screen_is_active": True,
             "shading_type": "RENDERED", "overlays_enabled": True},
            {"screen": "Shading", "area_index": 1, "screen_is_active": False,
             "shading_type": "MATERIAL", "overlays_enabled": True},
        ],
        "active_screen": "Layout",
        "simplify_enabled": False,
        "simplify_subdivision": 6,
    }


class ReportTests(unittest.TestCase):
    def test_optimizer_rejects_missing_blender_and_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.blend"
            source.write_bytes(b"fixture")
            missing = json.loads(plugin.tools.preview_optimizations({
                "blend_file": str(source),
                "blender_executable": str(Path(directory) / "missing-blender"),
            }))
            self.assertEqual(missing["error"]["code"], "blender_not_found")
            existing = json.loads(plugin.tools.preview_optimizations({
                "blend_file": str(source), "output_file": str(source),
            }))
            self.assertEqual(existing["error"]["code"], "invalid_request")
            apply_to_source = json.loads(plugin.tools.apply_optimizations({
                "blend_file": str(source), "output_file": str(source),
                "plan_id": "0" * 64, "selected_change_ids": ["x"],
            }))
            self.assertFalse(apply_to_source["ok"])
            self.assertEqual(source.read_bytes(), b"fixture")

    def test_invalid_paths_return_json(self):
        bad = "relative.blend"
        for handler, extra in (
            (plugin.tools.scan_scene, {}),
            (plugin.tools.analyze_performance, {}),
            (plugin.tools.preview_optimizations, {}),
            (plugin.tools.benchmark_performance, {"frame_start": 1, "frame_end": 2}),
        ):
            with self.subTest(handler=handler.__name__):
                result = json.loads(handler({"blend_file": bad, **extra}))
                self.assertFalse(result["ok"])

    def test_write_tools_request_human_approval(self):
        self.assertIsNone(plugin._require_optimization_approval(
            "preview_optimizations", {}, "task"))
        for name in ("apply_optimizations", "restore_optimizations"):
            decision = plugin._require_optimization_approval(
                name, {"output_file": "C:/new.blend", "plan_id": "abc"}, "task")
            self.assertEqual(decision["action"], "approve")
            self.assertIn("C:/new.blend", decision["message"])

    def test_ranks_evidence_and_ignores_inactive_viewports(self):
        report = tools._build_performance_report(sample_metrics())
        self.assertTrue(report["ok"])
        self.assertEqual(report["measured_metrics"]["evaluated_polygon_count"], 250000)
        self.assertEqual(report["estimated_metrics"]["scene_referenced_uncompressed_image_bytes"], 67108864)
        scores = [finding["severity_score"] for finding in report["findings"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        categories = {finding["category"] for finding in report["findings"]}
        self.assertTrue({"high_polygon_object", "modifier_geometry_growth",
                         "geometry_nodes_modifier", "subdivision_modifier",
                         "large_texture", "heavy_collection", "viewport_shading",
                         "viewport_simplify"}.issubset(categories))
        viewport_findings = [f for f in report["findings"] if f["category"] == "viewport_shading"]
        self.assertEqual([f["subject"] for f in viewport_findings], ["Layout area 0"])
        self.assertTrue(all("measured_evidence" in f and "estimated_impact" in f
                            and "recommendation" in f for f in report["findings"]))

    def test_disabled_modifier_and_unreferenced_image_do_not_raise_findings(self):
        metrics = sample_metrics()
        metrics["modifiers"] = [{"object": "Heavy", "name": "Nodes", "type": "NODES",
                                 "viewport_enabled": False, "node_group": "Generate"}]
        metrics["images"][0]["scene_referenced"] = False
        metrics["viewports"][0]["shading_type"] = "SOLID"
        categories = {f["category"] for f in tools._build_performance_report(metrics)["findings"]}
        self.assertNotIn("geometry_nodes_modifier", categories)
        self.assertNotIn("large_texture", categories)
        self.assertNotIn("viewport_shading", categories)

    def test_many_small_objects_are_flagged_without_claiming_measured_slowdown(self):
        metrics = sample_metrics()
        metrics["object_count"] = 500
        metrics["collections"] = [{
            "path": "Scene/Platforms", "direct_object_count": 400,
            "direct_mesh_count": 400, "direct_evaluated_polygons": 80000,
            "collection_hide_viewport": False,
        }]
        finding = next(f for f in tools._build_performance_report(metrics)["findings"]
                       if f["category"] == "high_object_count_collection")
        self.assertEqual(finding["measured_evidence"]["scene_object_share"], 0.8)
        self.assertEqual(finding["measured_evidence"]["average_evaluated_polygons_per_direct_mesh"], 200)
        self.assertIn("not benchmarked", finding["estimated_impact"])


class BlenderIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.blender = shutil.which(os.environ.get("BLENDER_EXECUTABLE", "blender"))
        if not cls.blender:
            raise unittest.SkipTest("Set BLENDER_EXECUTABLE to run Blender integration tests")

    def test_saved_file_is_unchanged_and_evaluated_polygons_are_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            blend_file = Path(directory) / "fixture.blend"
            expression = (
                "import bpy; "
                "bpy.data.objects['Cube'].modifiers.new('Subdivision', 'SUBSURF').levels = 1; "
                f"bpy.ops.wm.save_as_mainfile(filepath={str(blend_file)!r})"
            )
            created = subprocess.run(
                [self.blender, "--background", "--factory-startup", "--python-exit-code", "1",
                 "--python-expr", expression], capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            before = hashlib.sha256(blend_file.read_bytes()).hexdigest()
            report = json.loads(tools.analyze_performance({
                "blend_file": str(blend_file), "blender_executable": self.blender,
            }))
            scan = json.loads(tools.scan_scene({
                "blend_file": str(blend_file), "blender_executable": self.blender,
            }))
            after = hashlib.sha256(blend_file.read_bytes()).hexdigest()
            self.assertTrue(report["ok"], report)
            self.assertTrue(scan["ok"], scan)
            self.assertEqual(report["measured_metrics"]["base_polygon_count"], 6)
            self.assertEqual(report["measured_metrics"]["evaluated_polygon_count"], 24)
            self.assertEqual(scan["evaluated_polygon_count"], 24)
            self.assertEqual(before, after)

    def test_missing_blender_returns_json_error(self):
        with tempfile.TemporaryDirectory() as directory:
            blend_file = Path(directory) / "fixture.blend"
            blend_file.write_bytes(b"fixture")
            result = json.loads(tools.analyze_performance({
                "blend_file": str(blend_file),
                "blender_executable": str(Path(directory) / "missing-blender"),
            }))
            self.assertEqual(result["error"]["code"], "blender_not_found")

    def test_corrupted_blend_returns_errors_without_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "corrupt.blend"
            source.write_bytes(b"not a Blender file")
            inputs = {"blend_file": str(source), "blender_executable": self.blender}
            for handler, extra in (
                (plugin.tools.scan_scene, {}),
                (plugin.tools.analyze_performance, {}),
                (plugin.tools.preview_optimizations, {}),
                (plugin.tools.benchmark_performance, {"frame_start": 1, "frame_end": 2}),
            ):
                with self.subTest(handler=handler.__name__):
                    result = json.loads(handler({**inputs, **extra}))
                    self.assertFalse(result["ok"])
            self.assertEqual(source.read_bytes(), b"not a Blender file")
            self.assertEqual(list(Path(directory).glob("*_blendboost.blend")), [])

    def test_optimization_copy_and_restoration(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.blend"
            expression = (
                "import bpy; "
                "scene=bpy.context.scene; cube=bpy.data.objects['Cube']; "
                "mod=cube.modifiers.new('Subdivision','SUBSURF'); mod.levels=3; mod.render_levels=2; "
                "scene.render.use_simplify=True; scene.render.simplify_subdivision=4; "
                "scene.render.simplify_subdivision_render=5; "
                "collection=next(iter(scene.collection.children)); "
                "rig=bpy.data.objects.new('Rig',bpy.data.armatures.new('RigData')); collection.objects.link(rig); "
                "cube.location.x=0; cube.keyframe_insert(data_path='location',frame=1); "
                "cube.location.x=2; cube.keyframe_insert(data_path='location',frame=3); "
                "[(lambda o: collection.objects.link(o))(bpy.data.objects.new('Piece_%03d'%i,cube.data)) for i in range(120)]; "
                f"bpy.ops.wm.save_as_mainfile(filepath={str(source)!r})"
            )
            created = subprocess.run(
                [self.blender, "--background", "--factory-startup", "--python-exit-code", "1",
                 "--python-expr", expression], capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            preview = json.loads(plugin.tools.optimize_scene({"blend_file": str(source),
                                                              "blender_executable": self.blender}))
            self.assertTrue(preview["ok"], preview)
            kinds = {action["kind"] for action in preview["actions"]}
            self.assertTrue({"subdivision_viewport", "simplify_viewport_cap",
                             "hide_collection_viewport"}.issubset(kinds))
            selected = [action["id"] for action in preview["actions"]
                        if action["kind"] in {"subdivision_viewport", "simplify_viewport_cap",
                                              "hide_collection_viewport"}]
            invalid = json.loads(plugin.tools.apply_optimizations({
                "blend_file": str(source), "output_file": preview["output_file"],
                "blender_executable": self.blender, "plan_id": preview["plan_id"],
                "selected_change_ids": ["unknown-change"],
            }))
            self.assertFalse(invalid["ok"])
            self.assertFalse(Path(preview["output_file"]).exists())
            self.assertEqual(list(Path(directory).glob(".blendboost-*.blend")), [])
            applied = json.loads(plugin.tools.apply_optimizations({
                "blend_file": str(source), "output_file": preview["output_file"],
                "blender_executable": self.blender, "plan_id": preview["plan_id"],
                "selected_change_ids": selected,
            }))
            self.assertTrue(applied["ok"], applied)
            optimized = Path(applied["output_file"])
            self.assertTrue(optimized.is_file())
            self.assertEqual(source_hash, hashlib.sha256(source.read_bytes()).hexdigest())
            original_state = self._inspect(source)
            optimized_state = self._inspect(optimized)
            self.assertEqual(optimized_state[:7], (2, 2, True, 1, 5, True, True))
            # Hidden objects need not evaluate their animated transforms during
            # viewport playback; compare stored animation/rig/render data here.
            self.assertEqual(original_state[7:12], optimized_state[7:12])
            self.assertEqual(original_state[7], 1)  # armature datablock
            self.assertGreaterEqual(original_state[8], 1)  # animation action
            again = json.loads(plugin.tools.apply_optimizations({
                "blend_file": str(source), "output_file": preview["output_file"],
                "blender_executable": self.blender, "plan_id": preview["plan_id"],
                "selected_change_ids": selected,
            }))
            self.assertFalse(again["ok"])
            restore_output = Path(directory) / "restored.blend"
            restore_preview = json.loads(plugin.tools.preview_optimizations({
                "blend_file": str(optimized), "output_file": str(restore_output),
                "blender_executable": self.blender,
            }))
            self.assertEqual(restore_preview["mode"], "restore_preview")
            restored = json.loads(plugin.tools.restore_optimizations({
                "blend_file": str(optimized), "output_file": str(restore_output),
                "blender_executable": self.blender, "restore_id": restore_preview["restore_id"],
            }))
            self.assertTrue(restored["ok"], restored)
            restored_state = self._inspect(restore_output)
            self.assertEqual(restored_state[:7], (3, 2, True, 4, 5, False, False))
            self.assertEqual(original_state[7:], restored_state[7:])
            self.assertEqual(source_hash, hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(list(Path(directory).glob(".blendboost-*.blend")), [])

    def _inspect(self, blend_file):
        expression = (
            "import bpy; s=bpy.context.scene; o=bpy.data.objects['Cube']; "
            "c=next(iter(s.collection.children)); "
            "print('BLENDBOOST_TEST:'+repr((o.modifiers['Subdivision'].levels, "
            "o.modifiers['Subdivision'].render_levels, s.render.use_simplify, "
            "s.render.simplify_subdivision, s.render.simplify_subdivision_render, "
            "c.hide_viewport, 'blendboost_restore_v1' in s, "
            "len(bpy.data.armatures),len(bpy.data.actions),bool(o.animation_data),"
            "s.render.engine,s.render.resolution_x,"
            "(s.frame_set(1) or o.location.x),(s.frame_set(3) or o.location.x))))"
        )
        result = subprocess.run(
            [self.blender, "--background", "--factory-startup", "--disable-autoexec",
             "--python-exit-code", "1", str(blend_file), "--python-expr", expression],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        import ast
        return ast.literal_eval(next(line.split(":", 1)[1] for line in result.stdout.splitlines()
                                     if line.startswith("BLENDBOOST_TEST:")))


if __name__ == "__main__":
    unittest.main()
