"""Real Blender fixtures for proxy safety, restore, render checks, and trials."""
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from plugin_loader import plugin

optimizer = importlib.import_module(plugin.__name__ + ".optimizer")
benchmark = importlib.import_module(plugin.__name__ + ".benchmark")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ProxyUnitTests(unittest.TestCase):
    def test_options_and_trial_budget_validation(self):
        for args in ({"proxy_ratio": True}, {"proxy_ratio": float("nan")}, {"proxy_ratio": 0},
                     {"protected_objects": "Rig"}, {"benchmark_after_apply": {}},
                     {"benchmark_after_apply": {"frame_start": 1, "frame_end": 1}},
                     {"benchmark_after_apply": {"frame_start": 1, "frame_end": 3, "report_file": "x"}}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                optimizer._options(args)

    def test_trial_verdict_requires_consistent_evidence(self):
        def run(value):
            samples = [{"frame": i, "elapsed_ms": value} for i in range(3)]
            return {"statistics": benchmark._summary(samples), "typical_statistics": benchmark._typical_summary(samples)}
        self.assertEqual(benchmark._trial_verdict([(run(10), run(5))]), "inconclusive")
        self.assertEqual(benchmark._trial_verdict([(run(10), run(5))] * 2), "improvement")
        self.assertEqual(benchmark._trial_verdict([(run(10), run(15))] * 2), "regression")
        self.assertEqual(benchmark._trial_verdict([(run(10), run(5)), (run(10), run(15))]), "inconclusive")
        self.assertEqual(benchmark._trial_verdict([(run(10), run(9.9))] * 2), "inconclusive")

    def test_failed_or_regressed_trials_never_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.blend"
            source.write_bytes(b"original")
            output = Path(directory) / "copy.blend"
            args = {"blend_file": str(source), "output_file": str(output), "plan_id": "0" * 64,
                    "selected_change_ids": ["x"], "benchmark_after_apply": {"frame_start": 1, "frame_end": 3}}
            def stage(source, executable, request):
                Path(request["temp_file"]).write_bytes(b"staged")
                return {"ok": True, "changes": []}
            for result, code in (({"ok": False}, "trial_benchmark_failed"),
                                 ({"ok": True, "optimized": {}, "comparison": {"verdict": "regression"}}, "trial_regression")):
                with patch.object(optimizer, "_validate", return_value=(source, output, "blender", None)), \
                        patch.object(optimizer, "_run_blender", side_effect=stage), \
                        patch.object(benchmark, "run", return_value=json.dumps(result)):
                    response = json.loads(optimizer.apply(args))
                    self.assertEqual(response["error"]["code"], code)
                    self.assertFalse(output.exists())
                    self.assertEqual(list(Path(directory).glob(".blendboost-*")), [])
                    self.assertEqual(source.read_bytes(), b"original")

    def test_registration_schema_and_approval_gate(self):
        class Context:
            def __init__(self):
                self.tools, self.hooks = [], []
            def register_tool(self, **entry):
                self.tools.append(entry)
            def register_hook(self, *entry):
                self.hooks.append(entry)
        ctx = Context()
        plugin.register(ctx)
        self.assertEqual(len(ctx.tools), 7)
        self.assertEqual(len(ctx.hooks), 1)
        for tool in ctx.tools:
            self.assertEqual(tool["name"], tool["schema"]["name"])
            self.assertTrue(callable(tool["handler"]))
        self.assertEqual(plugin._require_optimization_approval("apply_optimizations", {})["action"], "approve")
        first = plugin._require_optimization_approval("apply_optimizations", {"selected_change_ids": ["a"]})
        second = plugin._require_optimization_approval("apply_optimizations", {"selected_change_ids": ["b"]})
        self.assertNotEqual(first["rule_key"], second["rule_key"])

    def test_publish_race_keeps_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory) / "staged.blend"
            destination = Path(directory) / "existing.blend"
            temporary.write_bytes(b"trial")
            destination.write_bytes(b"user data")
            with self.assertRaises(FileExistsError):
                optimizer._publish_new_copy(temporary, destination)
            self.assertEqual(destination.read_bytes(), b"user data")

    def test_optimizer_timeout_is_structured(self):
        with patch.object(optimizer.subprocess, "run", side_effect=subprocess.TimeoutExpired("blender", 240)):
            result = optimizer._run_blender(Path("fixture.blend"), "blender", {})
        self.assertEqual(result["error"]["code"], "blender_timeout")


class ProxyBlenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.blender = shutil.which(os.environ.get("BLENDER_EXECUTABLE", "blender"))
        if not cls.blender:
            raise unittest.SkipTest("Set BLENDER_EXECUTABLE to run Blender proxy tests")
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        cls.source = cls.root / "scenery.blend"
        script = r'''
import bpy
bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete(use_global=False)
s = bpy.context.scene
pieces = bpy.data.collections.new('Platforms')
s.collection.children.link(pieces)
for i in range(105):
    bpy.ops.mesh.primitive_cube_add(size=.15, location=(i % 15 * .2 - 1.4, i // 15 * .2, 0))
    o = bpy.context.object
    o.name = 'Platform_%03d' % i
    for c in list(o.users_collection): c.objects.unlink(o)
    pieces.objects.link(o)
    if i == 0: o.scale.x = -1
bpy.ops.mesh.primitive_uv_sphere_add(segments=128, ring_count=64, radius=.6, location=(0, 0, 1))
heavy = bpy.context.object
heavy.name = 'HeavyScenery'
mod = heavy.modifiers.new('StaticSubdivision', 'SUBSURF')
mod.levels = 2
mod.render_levels = 2
heavy.modifiers.new('Triangles', 'TRIANGULATE')
material = bpy.data.materials.new('SceneryMaterial')
heavy.data.materials.append(material)
for name in ('Animated', 'Driven', 'Constrained', 'ShapeKeys', 'Nodes', 'Referenced', 'ChildOfAnimated', 'Protected'):
    o = bpy.data.objects.new(name, bpy.data.objects['Platform_000'].data.copy())
    s.collection.objects.link(o)
    o.location = (3, 3, 3)
    if name == 'Animated': o.keyframe_insert(data_path='location', frame=1)
    if name == 'Driven': o.driver_add('location', 0).driver.expression = 'frame / 10'
    if name == 'Constrained': o.constraints.new('LIMIT_LOCATION')
    if name == 'ShapeKeys': o.shape_key_add(name='Basis')
    if name == 'Nodes': o.modifiers.new('GeometryNodes', 'NODES')
bpy.data.objects['Constrained'].constraints.new('COPY_LOCATION').target = bpy.data.objects['Referenced']
bpy.data.objects['ChildOfAnimated'].parent = bpy.data.objects['Animated']
rig = bpy.data.objects.new('CharacterRig', bpy.data.armatures.new('RigData'))
s.collection.objects.link(rig)
bpy.ops.object.camera_add(location=(4, -6, 4))
camera = bpy.context.object
from mathutils import Vector
camera.rotation_euler = (Vector((0, 0, .5)) - camera.location).to_track_quat('-Z', 'Y').to_euler()
s.camera = camera
camera.keyframe_insert(data_path='location', frame=1)
camera.location.x += .05
camera.keyframe_insert(data_path='location', frame=3)
s.render.engine = 'CYCLES'
s.cycles.device = 'CPU'
s.cycles.samples = 4
s.cycles.use_denoising = False
s.cycles.seed = 42
s.render.resolution_x = 32
s.render.resolution_y = 32
s.render.resolution_percentage = 100
s.frame_set(1)
'''
        cls._blender(script + f"\nbpy.ops.wm.save_as_mainfile(filepath={str(cls.source)!r})")

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    @classmethod
    def _blender(cls, code, source=None):
        script = cls.root / "fixture_script.py"
        script.write_text(code, encoding="utf-8")
        args = [cls.blender, "--background", "--factory-startup", "--disable-autoexec", "--python-exit-code", "1"]
        if source:
            args.append(str(source))
        result = subprocess.run([*args, "--python", str(script)], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=180)
        if result.returncode:
            raise AssertionError(result.stdout[-5000:] + result.stderr[-2000:])
        lines = [line[8:] for line in result.stdout.splitlines() if line.startswith("BB_TEST:")]
        return json.loads(lines[-1]) if lines else None

    def preview(self, stem, **extra):
        args = {"blend_file": str(self.source), "output_file": str(self.root / (stem + ".blend")),
                "blender_executable": self.blender, **extra}
        preview = json.loads(plugin.tools.preview_optimizations(args))
        self.assertTrue(preview["ok"], preview)
        return args, preview

    def test_proxy_roundtrip_and_sampled_render_equivalence(self):
        before = digest(self.source)
        args, preview = self.preview("optimized", protected_objects=["Protected"])
        proxy_actions = [a for a in preview["actions"] if a["kind"] in optimizer.proxies.KINDS]
        self.assertEqual({a["kind"] for a in proxy_actions}, optimizer.proxies.KINDS)
        exclusions = {row["object"] for row in preview["proxy_exclusions"]}
        self.assertTrue({"Animated", "Driven", "Constrained", "ShapeKeys", "Nodes", "Referenced", "ChildOfAnimated", "Protected"} <= exclusions)
        self.assertTrue(all(not a["default_selected"] for a in proxy_actions))
        selected = [a["id"] for a in proxy_actions]
        applied = json.loads(plugin.tools.apply_optimizations({**args, "plan_id": preview["plan_id"],
                                                              "selected_change_ids": selected}))
        self.assertTrue(applied["ok"], applied)
        self.assertEqual(before, digest(self.source))
        self.assertTrue(applied["validation"]["original_geometry_and_render_structure_preserved"])
        batch = next(a for a in applied["changes"] if a["kind"] == "static_collection_proxy")
        self.assertEqual(batch["measured_result"]["visible_source_objects_before"], 105)
        self.assertEqual(batch["measured_result"]["proxy_polygons"], 630)
        heavy = next(a for a in applied["changes"] if a["kind"] == "static_mesh_proxy")
        self.assertLess(heavy["measured_result"]["proxy_polygons"], heavy["measured_evidence"]["source_evaluated_polygons"])
        original_scan = json.loads(plugin.tools.scan_scene({"blend_file": str(self.source), "blender_executable": self.blender}))
        proxy_scan = json.loads(plugin.tools.scan_scene({"blend_file": args["output_file"], "blender_executable": self.blender}))
        self.assertTrue(original_scan["ok"], original_scan)
        self.assertTrue(proxy_scan["ok"], proxy_scan)
        self.assertEqual(proxy_scan["generated_proxy_count"], 2)
        self.assertEqual(proxy_scan["visible_mesh_count"], original_scan["visible_mesh_count"] - 104)
        self.assertEqual(proxy_scan["mesh_count"], original_scan["mesh_count"] + 2)
        self.assertLess(proxy_scan["visible_evaluated_polygon_count"], original_scan["visible_evaluated_polygon_count"])
        restore_args = {"blend_file": args["output_file"], "output_file": str(self.root / "restored.blend"),
                        "blender_executable": self.blender}
        restore_preview = json.loads(plugin.tools.preview_optimizations(restore_args))
        self.assertTrue(restore_preview["ok"], restore_preview)
        restored = json.loads(plugin.tools.restore_optimizations({**restore_args, "restore_id": restore_preview["restore_id"]}))
        self.assertTrue(restored["ok"], restored)
        result = self._blender(f'''
import bpy, json
paths = {[str(self.source), args['output_file'], restore_args['output_file']]!r}
renders = []
states = []
for path in paths:
    bpy.ops.wm.open_mainfile(filepath=path)
    states.append({{'objects': len(bpy.data.objects), 'meshes': len(bpy.data.meshes),
                   'actions': len(bpy.data.actions), 'rigs': len(bpy.data.armatures),
                   'hidden': sorted(o.name for o in bpy.data.objects if o.hide_viewport)}})
    frames = []
    for frame in (1, 3):
        bpy.context.scene.frame_set(frame)
        bpy.ops.render.render()
        # Render Result's pixels may be empty in background; save and reload
        # a temporary float EXR to check actual sampled render values.
        bpy.context.scene.render.image_settings.file_format = 'OPEN_EXR'
        image_path = {str(self.root / 'check.exr')!r}
        bpy.data.images['Render Result'].save_render(image_path)
        im = bpy.data.images.load(image_path, check_existing=False)
        frames.append(list(im.pixels))
        bpy.data.images.remove(im)
    renders.append(frames)
differences = [max(abs(x-y) for bf, af in zip(renders[0], other) for x,y in zip(bf,af)) for other in renders[1:]]
print('BB_TEST:' + json.dumps({{'states': states, 'differences': differences, 'pixel_count': len(renders[0][0])}}))
''')
        self.assertGreater(result["pixel_count"], 0)
        self.assertTrue(all(difference < 1e-5 for difference in result["differences"]), result)
        self.assertEqual(result["states"][0], result["states"][2])
        self.assertEqual(before, digest(self.source))

    def test_conflicts_stale_options_and_protection(self):
        args, preview = self.preview("conflicts")
        proxy = next(a for a in preview["actions"] if a["kind"] == "static_collection_proxy")
        hide = next(a for a in preview["actions"] if a["kind"] == "hide_collection_viewport")
        for extra, selected in (({}, [proxy["id"], hide["id"]]), ({"proxy_ratio": .25}, [proxy["id"]])):
            result = json.loads(plugin.tools.apply_optimizations({**args, **extra, "plan_id": preview["plan_id"],
                                                                 "selected_change_ids": selected}))
            self.assertFalse(result["ok"])
            self.assertFalse(Path(args["output_file"]).exists())
        _, protected = self.preview("protected", protected_collections=["Platforms"], protected_objects=["HeavyScenery"])
        self.assertFalse(any(a["kind"] in optimizer.proxies.KINDS for a in protected["actions"]))
        self.assertEqual(list(self.root.glob(".blendboost-*.blend")), [])

    def test_edited_proxy_restore_is_refused(self):
        args, preview = self.preview("editable")
        action = next(a for a in preview["actions"] if a["kind"] == "static_collection_proxy")
        applied = json.loads(plugin.tools.apply_optimizations({**args, "plan_id": preview["plan_id"],
                                                              "selected_change_ids": [action["id"]]}))
        self.assertTrue(applied["ok"], applied)
        edited = self.root / "edited.blend"
        self._blender(f"import bpy\no=next(o for o in bpy.data.objects if o.get('blendboost_proxy_owner'))\no.data.vertices[0].co.x += 1\nbpy.ops.wm.save_as_mainfile(filepath={str(edited)!r})", args["output_file"])
        restore_args = {"blend_file": str(edited), "output_file": str(self.root / "refused_restore.blend"),
                        "blender_executable": self.blender}
        plan = json.loads(plugin.tools.preview_optimizations(restore_args))
        result = json.loads(plugin.tools.restore_optimizations({**restore_args, "restore_id": plan["restore_id"]}))
        self.assertFalse(result["ok"], result)
        self.assertIn("edited", result["error"]["message"])
        self.assertFalse(Path(restore_args["output_file"]).exists())

    def test_approved_trial_uses_alternating_rounds(self):
        args, preview = self.preview("trial", benchmark_after_apply={"frame_start": 1, "frame_end": 3, "passes": 2})
        action = next(a for a in preview["actions"] if a["kind"] == "static_collection_proxy")
        result = json.loads(plugin.tools.apply_optimizations({**args, "plan_id": preview["plan_id"],
                                                             "selected_change_ids": [action["id"]]}))
        if not result["ok"]:
            self.assertEqual(result["error"]["code"], "trial_regression", result)
        report = result["benchmark"]
        self.assertEqual(report["conditions"]["run_order"], [["baseline", "optimized"], ["optimized", "baseline"]])
        self.assertEqual(report["baseline"]["statistics"]["sample_count"], 12)
        self.assertFalse(report["interactive_viewport_fps_measured"])
        self.assertEqual(list(self.root.glob(".blendboost-*.blend")), [])

    def test_collection_dependencies_layers_and_linked_data_are_excluded(self):
        result = self._blender(f'''
import bpy,sys,json
sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
import proxies
s=bpy.context.scene
collection=bpy.data.collections['Platforms']
instancer=bpy.data.objects.new('CollectionInstance',None)
s.collection.objects.link(instancer)
instancer.instance_type='COLLECTION'
instancer.instance_collection=collection
instanced=proxies.eligibility(s,[],[])['Platform_000']
bpy.data.objects.remove(instancer,do_unlink=True)
parent=bpy.data.collections.new('Protected Parent')
s.collection.children.link(parent)
s.collection.children.unlink(collection)
parent.children.link(collection)
protected=proxies.eligibility(s,[],['Protected Parent'])['Platform_000']
s.view_layers.new('Second Layer')
layer=proxies.eligibility(s,[],[])['Platform_000']
s.view_layers.remove(s.view_layers['Second Layer'])
library_path={str(self.root / 'linked_library.blend')!r}
local=bpy.data.objects['HeavyScenery']
local.name='LibraryOnlyScenery'
bpy.data.libraries.write(library_path,{{local}})
local.name='HeavyScenery'
with bpy.data.libraries.load(library_path,link=True) as (available,loaded):
    loaded.objects=['LibraryOnlyScenery']
linked=loaded.objects[0]
s.collection.objects.link(linked)
linked_reason=proxies.eligibility(s,[],[])[linked.name]
local.name=linked.name
ambiguous=proxies.eligibility(s,[],[])[linked.name]
print('BB_TEST:'+json.dumps({{'instanced':instanced,'protected':protected,'layer':layer,'linked':linked_reason,'ambiguous':ambiguous}}))
''', self.source)
        self.assertIn("instanced", result["instanced"])
        self.assertIn("protected", result["protected"])
        self.assertIn("one view layer", result["layer"])
        self.assertIn("linked", result["linked"])
        self.assertIn("ambiguous", result["ambiguous"])


if __name__ == "__main__":
    unittest.main()
