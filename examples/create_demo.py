"""Run with Blender --background --factory-startup --python this.py -- NEW.blend.

Creates a synthetic, reproducible demo; never opens or edits a user project.
"""
from pathlib import Path
import sys
import bpy

if "--" not in sys.argv or len(sys.argv[sys.argv.index("--") + 1:]) != 1:
    raise SystemExit("Supply one NEW absolute .blend filename after --")
target = Path(sys.argv[-1])
if not target.is_absolute() or target.suffix.lower() != ".blend" or not target.parent.is_dir():
    raise SystemExit("Destination must be an absolute .blend path in an existing directory")
if target.exists() or target.is_symlink():
    raise SystemExit("Destination already exists; refusing to overwrite")
if bpy.data.filepath:
    raise SystemExit("Run with --factory-startup and no input .blend file")

bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
scene = bpy.context.scene
collection = bpy.data.collections.new("Demo Platforms")
scene.collection.children.link(collection)
for index in range(120):
    bpy.ops.mesh.primitive_cube_add(size=.8, location=(index % 12, index // 12, 0))
    obj = bpy.context.object
    obj.name = f"Platform {index:03d}"
    for owner in list(obj.users_collection):
        owner.objects.unlink(obj)
    collection.objects.link(obj)
bpy.ops.mesh.primitive_uv_sphere_add(segments=128, ring_count=64, radius=2, location=(5, 4, 3))
heavy = bpy.context.object
heavy.name = "Heavy Static Scenery"
subdivision = heavy.modifiers.new("Scenery Detail", "SUBSURF")
subdivision.levels = 2
subdivision.render_levels = 2
heavy.modifiers.new("Triangles", "TRIANGULATE")
bpy.ops.mesh.primitive_cube_add(location=(0, -2, 1))
actor = bpy.context.object
actor.name = "Animated Actor - Protect Me"
actor.keyframe_insert(data_path="location", frame=1)
actor.location.x = 10
actor.keyframe_insert(data_path="location", frame=30)
scene.frame_end = 30
scene.frame_set(1)
# Save through the same atomic no-overwrite publishing helper as the plugin.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import optimizer
import uuid
temporary = target.with_name(f".blendboost-demo-{uuid.uuid4().hex}.blend")
try:
    optimizer._save_copy(str(temporary))
    optimizer._publish_new_copy(temporary, target)
finally:
    temporary.unlink(missing_ok=True)
print(f"Created synthetic BlendBoost demo: {target}")
