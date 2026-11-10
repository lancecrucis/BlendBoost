"""Conservative, reversible working proxies. Imported inside Blender only.

The source datablocks are retained for rendering. Unsupported dependencies are
excluded rather than frozen at one frame. Proxies are intended for Solid editing;
material/object-coordinate appearance and fine detail can differ in the viewport.
"""

import hashlib
import json
from collections import Counter

KINDS = {"static_mesh_proxy", "static_collection_proxy"}
OWNER_KEY = "blendboost_proxy_owner"
STATIC_MODIFIERS = {"DECIMATE", "SUBSURF", "BEVEL", "TRIANGULATE", "EDGE_SPLIT", "WELD"}


def _animated(data):
    animation = getattr(data, "animation_data", None)
    return bool(animation and (animation.action or animation.nla_tracks or animation.drivers))


def eligibility(scene, protected_objects, protected_collections):
    """Return every mesh's reason for exclusion, using Blender's ID user graph."""
    import bpy

    users = bpy.data.user_map()
    reasons = {}
    name_counts = Counter(obj.name for obj in scene.objects)
    collection_names = Counter(c.name for c in bpy.data.collections)
    layer = scene.view_layers[0]

    def collection_problem(collection, visited):
        if collection in visited:
            return None
        visited.add(collection)
        if collection_names[collection.name] > 1:
            return "ambiguous collection name across libraries"
        if collection.name in protected_collections:
            return "protected collection (including its descendants)"
        if collection.library or collection.override_library or _animated(collection):
            return "linked, overridden, or animated collection"
        for user in users.get(collection, ()):
            if isinstance(user, bpy.types.Collection):
                problem = collection_problem(user, visited)
                if problem:
                    return problem
            elif not isinstance(user, bpy.types.Scene):
                return "collection is instanced or referenced by another datablock"
        return None

    for obj in sorted(scene.objects, key=lambda item: item.name):
        if obj.type != "MESH":
            continue
        reason = None
        if len(scene.view_layers) != 1:
            reason = "proxy support currently requires one view layer"
        elif name_counts[obj.name] > 1:
            reason = "ambiguous object name across libraries"
        elif _animated(scene):
            reason = "animated scene settings"
        elif obj.name in protected_objects:
            reason = "protected object"
        elif obj.library or obj.override_library or obj.data.library or obj.data.override_library:
            reason = "linked or overridden data"
        elif len(obj.users_scene) != 1 or len(obj.users_collection) != 1:
            reason = "shared between scenes or collections"
        elif not obj.visible_get(view_layer=layer) or obj.hide_render:
            reason = "not visible in both viewport and render"
        elif obj.instance_type != "NONE" or obj.is_instancer:
            reason = "instancing object"
        elif _animated(obj) or _animated(obj.data) or obj.data.shape_keys:
            reason = "animation, drivers, or shape keys"
        elif obj.constraints or obj.rigid_body or obj.rigid_body_constraint or obj.particle_systems:
            reason = "constraints, physics, or particles"
        elif any(m.type not in STATIC_MODIFIERS for m in obj.modifiers):
            reason = "modifier outside the static allowlist"
        elif any(not isinstance(user, (bpy.types.Collection, bpy.types.Scene))
                 for user in users.get(obj, ())):
            reason = "referenced by another datablock (parenting, constraint, nodes, etc.)"
        else:
            for collection in obj.users_collection:
                reason = collection_problem(collection, set())
                if reason:
                    break
        parent = obj.parent
        if not reason and obj.parent and obj.parent_type != "OBJECT":
            reason = "bone or vertex parenting"
        seen = set()
        while not reason and parent:
            if parent in seen or _animated(parent) or parent.constraints or parent.modifiers or parent.rigid_body:
                reason = "animated or dependent parent chain"
                break
            if parent.library or parent.override_library or parent.parent_type != "OBJECT":
                reason = "unsupported parent chain"
                break
            seen.add(parent)
            parent = parent.parent
        if not reason and abs(obj.matrix_world.determinant()) < 1e-12:
            reason = "singular object transform"
        reasons[obj.name] = reason
    return reasons


def propose(scene, metrics, options, make_action):
    """Build stable action IDs without creating or editing any Blender data."""
    excluded = eligibility(scene, options["protected_objects"], options["protected_collections"])
    rows = {row["name"]: row for row in metrics["meshes"]}
    eligible = {name for name, reason in excluded.items() if reason is None
                and rows[name]["evaluated_polygons"] > 0}
    actions = []
    assigned = set()
    # Small objects benefit from batching, not aggressive polygon reduction.
    for row in sorted(metrics["collections"], key=lambda row: row["path"]):
        import bpy
        collection = scene.collection if row["path"] == metrics["root_collection"] else bpy.data.collections.get(row["name"])
        if collection is None:
            continue
        names = sorted(obj.name for obj in collection.objects if obj.name in eligible - assigned
                       and rows[obj.name]["evaluated_polygons"] < 50000)
        if len(names) >= 100:
            actions.append(_proposal("static_collection_proxy", scene, names, rows, 1.0,
                                     make_action, collection.name))
            assigned.update(names)
    for name in sorted(eligible - assigned):
        if rows[name]["evaluated_polygons"] >= 50000:
            actions.append(_proposal("static_mesh_proxy", scene, [name], rows,
                                     options["proxy_ratio"], make_action))
    return actions, [{"object": name, "reason": reason} for name, reason in excluded.items() if reason]


def _proposal(kind, scene, names, rows, ratio, make_action, collection=None):
    target = {"scene": scene.name, "objects": names}
    if collection:
        target["collection"] = collection
    polygons = sum(rows[name]["evaluated_polygons"] for name in names)
    action = make_action(kind, target, "working_proxy", None, {"ratio": ratio},
                         "Create a cached viewport stand-in; retain originals for rendering.", temporary=True)
    action.update({
        "measured_evidence": {"source_mesh_objects": len(names), "source_evaluated_polygons": polygons,
                              "source_modifiers": sum(rows[name]["modifier_count"] for name in names)},
        "estimated_impact": "Fewer visible objects and/or less geometry evaluation; speed is unmeasured until benchmarked.",
        "tradeoffs": ["Use for Solid viewport work; material appearance and object picking can differ.",
                      "Originals remain in the file, so disk and memory usage may increase.",
                      "Proxy geometry is frozen. Restore before editing or animating the source scenery.",
                      "Reduction ratio targets triangles; actual polygon count is measured after creation."],
    })
    return action


def check_conflicts(changes):
    proxies = [a for a in changes if a["kind"] in KINDS]
    names = [name for a in proxies for name in a["target"]["objects"]]
    if len(names) != len(set(names)):
        raise ValueError("Two proxy actions target the same object.")
    for action in changes:
        if action["kind"] in KINDS:
            continue
        if action["kind"] in {"hide_collection_viewport", "simplify_viewport_cap"} and proxies:
            raise ValueError("Choose proxies separately from collection hides or global Simplify changes.")
        if action["target"].get("object") in names:
            raise ValueError("Choose either a proxy or a setting change for the same object.")


def _mesh_digest(mesh):
    from array import array
    digest = hashlib.sha256()
    for collection, prop, size, code in ((mesh.vertices, "co", 3, "f"),
                                        (mesh.edges, "vertices", 2, "i"),
                                        (mesh.loops, "vertex_index", 1, "i"),
                                        (mesh.polygons, "loop_total", 1, "i"),
                                        (mesh.polygons, "material_index", 1, "i")):
        values = array(code, [0]) * (len(collection) * size)
        collection.foreach_get(prop, values)
        digest.update(values.tobytes())
    for layer in mesh.uv_layers:
        values = array("f", [0]) * (len(layer.data) * 2)
        layer.data.foreach_get("uv", values)
        digest.update(layer.name.encode())
        digest.update(values.tobytes())
    # Include color/weight/custom attributes and face shading flags so restore
    # detects work on a generated mesh, not only moved vertices.
    for attribute in sorted(mesh.attributes, key=lambda item: item.name):
        if attribute.name in {"position", ".corner_vert", ".corner_edge", ".edge_verts"}:
            continue  # Already hashed efficiently above.
        digest.update(f"{attribute.name}:{attribute.domain}:{attribute.data_type}".encode())
        if not len(attribute.data):
            continue
        for prop in attribute.data[0].bl_rna.properties:
            if prop.identifier == "rna_type" or prop.type not in {"FLOAT", "INT", "BOOLEAN", "STRING"}:
                continue
            values = [getattr(item, prop.identifier) for item in attribute.data]
            values = [list(value) if getattr(prop, "is_array", False) else value for value in values]
            digest.update(json.dumps(values, separators=(",", ":")).encode())
    digest.update(bytes(p.use_smooth for p in mesh.polygons))
    digest.update(json.dumps([m.name if m else None for m in mesh.materials]).encode())
    return digest.hexdigest()


def _proxy_state(obj):
    return {"mesh": obj.data.name, "geometry": _mesh_digest(obj.data),
            "matrix": [list(row) for row in obj.matrix_world],
            "hide_render": obj.hide_render, "hide_viewport": obj.hide_viewport,
            "collections": sorted(c.name for c in obj.users_collection),
            "modifiers": len(obj.modifiers), "animated": _animated(obj),
            "parent": obj.parent.name if obj.parent else None,
            "color": list(obj.color), "display_type": obj.display_type,
            "fake_user": obj.use_fake_user,
            "vertex_groups": [g.name for g in obj.vertex_groups],
            "custom_properties": {key: repr(obj[key]) for key in obj.keys()},
            "mesh_custom_properties": {key: repr(obj.data[key]) for key in obj.data.keys()},
            "materials": [(s.link, s.material.name if s.material else None) for s in obj.material_slots],
            "shape_keys": bool(obj.data.shape_keys), "mesh_animation": _animated(obj.data),
            "physics": bool(obj.rigid_body or obj.rigid_body_constraint)}


def create(scene, action):
    """Bake only generated meshes. The original meshes/modifiers are never edited."""
    import bpy
    from mathutils import Matrix

    token = action["id"]
    collection = bpy.data.collections.new("BlendBoost Working Proxies")
    collection[OWNER_KEY] = token
    collection.hide_render = True
    scene.collection.children.link(collection)
    originals = [scene.objects[name] for name in action["target"]["objects"]]
    generated = []
    ratio = action["after"]["ratio"]
    layer = scene.view_layers[0]
    with bpy.context.temp_override(scene=scene, view_layer=layer):
        depsgraph = bpy.context.evaluated_depsgraph_get()
        for original in originals:
            evaluated = original.evaluated_get(depsgraph)
            mesh = bpy.data.meshes.new_from_object(evaluated, preserve_all_data_layers=True, depsgraph=depsgraph)
            mesh.name = "BB Proxy " + original.name
            # Resolve object-level material overrides into the new mesh slots.
            mesh.materials.clear()
            for slot in original.material_slots:
                mesh.materials.append(slot.material)
            proxy = bpy.data.objects.new("BB Proxy " + original.name, mesh)
            collection.objects.link(proxy)
            proxy.matrix_world = original.matrix_world.copy()
            proxy.color = original.color
            proxy.hide_render = True
            if ratio < 1:
                modifier = proxy.modifiers.new("BlendBoost temporary reduction", "DECIMATE")
                modifier.ratio = ratio
                layer.update()
                reduced = bpy.data.meshes.new_from_object(proxy.evaluated_get(depsgraph),
                                                         preserve_all_data_layers=True, depsgraph=depsgraph)
                proxy.modifiers.clear()
                proxy.data = reduced
                bpy.data.meshes.remove(mesh)
                mesh = reduced
            # World-space mesh permits grouping objects with different transforms.
            mesh.transform(proxy.matrix_world)
            if proxy.matrix_world.determinant() < 0:
                mesh.flip_normals()
            proxy.matrix_world = Matrix.Identity(4)
            generated.append(proxy)
        if len(generated) > 1:
            obsolete_meshes = [o.data for o in generated[1:]]
            with bpy.context.temp_override(active_object=generated[0], object=generated[0],
                                           selected_objects=generated, selected_editable_objects=generated):
                if "FINISHED" not in bpy.ops.object.join():
                    raise RuntimeError("Blender could not group the proxy objects.")
            generated = generated[:1]
            for mesh in obsolete_meshes:
                if mesh.users == 0:
                    bpy.data.meshes.remove(mesh)
        proxy = generated[0]
        proxy[OWNER_KEY] = token
        proxy.data[OWNER_KEY] = token
        layer.update()
        if not proxy.visible_get(view_layer=layer):
            raise RuntimeError("Generated proxy is not visible; refusing to hide its sources.")
        if not len(proxy.data.polygons):
            raise RuntimeError("Generated proxy is empty.")
        before = [{"object": obj.name, "hide_viewport": obj.hide_viewport} for obj in originals]
        for obj in originals:
            obj.hide_viewport = True
        return {**action, "created": {"collection": collection.name, "object": proxy.name,
                                      "state": _proxy_state(proxy)}, "original_visibility": before,
                "measured_result": {"visible_source_objects_before": len(originals),
                                    "visible_proxy_objects_after": 1,
                                    "proxy_polygons": len(proxy.data.polygons),
                                    "original_meshes_modified": False}}


def validate_restore(scene, action):
    import bpy
    created = action["created"]
    obj = bpy.data.objects.get(created["object"])
    collection = bpy.data.collections.get(created["collection"])
    if (not obj or not collection or obj.type != "MESH" or obj.name not in scene.objects
            or obj.get(OWNER_KEY) != action["id"] or obj.data.get(OWNER_KEY) != action["id"]
            or collection.get(OWNER_KEY) != action["id"] or not collection.hide_render or collection.hide_viewport
            or set(collection.objects) != {obj} or collection.children
            or obj.data.users != 1 or obj.constraints or obj.particle_systems
            or json.dumps(_proxy_state(obj), sort_keys=True) != json.dumps(created["state"], sort_keys=True)):
        raise ValueError("Generated proxy was edited or ownership changed; restore refused to protect later work.")
    users = bpy.data.user_map()
    if (any(user not in {collection, scene} for user in users.get(obj, ()))
            or any(user not in {scene, scene.collection} for user in users.get(collection, ()))):
        references = [f"{type(u).__name__}:{u.name}" for u in set(users.get(obj, ())) | set(users.get(collection, ()))]
        raise ValueError(f"Generated proxy is referenced elsewhere; restore refused: {references}")
    for record in action["original_visibility"]:
        original = scene.objects.get(record["object"])
        if original is None or not original.hide_viewport:
            raise ValueError("Original visibility changed after optimization; restore refused.")


def restore(scene, action):
    import bpy
    for record in action["original_visibility"]:
        scene.objects[record["object"]].hide_viewport = record["hide_viewport"]
    obj = bpy.data.objects[action["created"]["object"]]
    mesh = obj.data
    bpy.data.objects.remove(obj, do_unlink=True)
    bpy.data.meshes.remove(mesh)
    bpy.data.collections.remove(bpy.data.collections[action["created"]["collection"]])
