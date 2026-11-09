"""Read-only Blender scene scan via Blender's supported background Python CLI."""

import json
import os
from pathlib import Path
import shutil
import subprocess


_RESULT_MARKER = "BLENDBOOST_SCAN_RESULT:"
_TIMEOUT_SECONDS = 180


def _error(code, message):
    return json.dumps({"ok": False, "error": {"code": code, "message": message}})


def scan_scene(args: dict, **kwargs) -> str:
    """Hermes handler; always return JSON, including for connection failures."""
    return _run_blender(args, "scan")


def analyze_performance(args: dict, **kwargs) -> str:
    """Hermes handler for the read-only performance report."""
    return _run_blender(args, "analyze")


def optimize_scene(args: dict, **kwargs) -> str:
    """Read-only optimization entry point."""
    return preview_optimizations(args, **kwargs)


def preview_optimizations(args: dict, **kwargs) -> str:
    from . import optimizer
    return optimizer.preview(args)


def apply_optimizations(args: dict, **kwargs) -> str:
    from . import optimizer
    return optimizer.apply(args)


def restore_optimizations(args: dict, **kwargs) -> str:
    from . import optimizer
    return optimizer.restore(args)


def benchmark_performance(args: dict, **kwargs) -> str:
    """Benchmark saved scenes in separate read-only Blender processes."""
    from . import benchmark
    return benchmark.run(args)


def _run_blender(args: dict, task: str) -> str:
    try:
        if not isinstance(args, dict):
            return _error("invalid_arguments", "Expected an argument object.")

        blend_file = args.get("blend_file")
        if not isinstance(blend_file, str) or not blend_file.strip():
            return _error("invalid_blend_file", "Provide an absolute .blend file path.")
        path = Path(blend_file).expanduser()
        if not path.is_absolute() or path.suffix.lower() != ".blend":
            return _error("invalid_blend_file", "blend_file must be an absolute .blend path.")
        if not path.is_file():
            return _error("blend_file_not_found", f"Blend file does not exist: {path}")

        scene_name = args.get("scene_name")
        if scene_name is not None and (not isinstance(scene_name, str) or not scene_name.strip()):
            return _error("invalid_scene_name", "scene_name must be a nonempty string.")

        executable = args.get("blender_executable") or os.environ.get("BLENDER_EXECUTABLE") or "blender"
        if not isinstance(executable, str):
            return _error("invalid_blender_executable", "blender_executable must be a path string.")
        resolved_executable = shutil.which(executable)
        if resolved_executable is None:
            return _error(
                "blender_not_found",
                "Blender executable was not found. Supply blender_executable or set BLENDER_EXECUTABLE.",
            )

        env = os.environ.copy()
        if scene_name:
            env["BLENDBOOST_SCENE_NAME"] = scene_name
        else:
            env.pop("BLENDBOOST_SCENE_NAME", None)
        env["BLENDBOOST_TASK"] = task
        command = [
            resolved_executable,
            "--background",
            "--factory-startup",
            "--disable-autoexec",
            "--python-exit-code", "1",
            str(path),
            "--python", str(Path(__file__).resolve()),
        ]
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=_TIMEOUT_SECONDS, check=False, env=env,
            )
        except subprocess.TimeoutExpired:
            return _error("blender_timeout", f"Blender inspection exceeded {_TIMEOUT_SECONDS} seconds.")
        except OSError as exc:
            return _error("blender_connection_failed", f"Could not start Blender: {exc}")

        lines = completed.stdout.splitlines()
        results = [line[len(_RESULT_MARKER):] for line in lines if line.startswith(_RESULT_MARKER)]
        if completed.returncode != 0 or not results:
            detail = (completed.stderr or completed.stdout).strip()[-1000:]
            return _error(
                "blender_scan_failed",
                f"Blender exited with code {completed.returncode}: {detail or 'no scan result returned'}",
            )
        return results[-1]
    except Exception as exc:
        return _error("scan_failed", f"Scene scan failed: {exc}")


def _scan_in_blender():
    """Run only inside Blender's Python process; never call save or data mutation APIs."""
    import bpy

    scene_name = os.environ.get("BLENDBOOST_SCENE_NAME")
    scene = bpy.data.scenes.get(scene_name) if scene_name else bpy.context.scene
    if scene is None:
        raise ValueError(f"Scene not found: {scene_name}")

    # A scene override ensures evaluated geometry belongs to the requested scene.
    with bpy.context.temp_override(scene=scene):
        depsgraph = bpy.context.evaluated_depsgraph_get()
        objects = list(scene.objects)
        counts = {}
        modifiers_by_type = {}
        objects_with_modifiers = []
        meshes = []
        base_total = 0
        evaluated_total = 0

        for obj in objects:
            counts[obj.type] = counts.get(obj.type, 0) + 1
            modifiers = [{"name": mod.name, "type": mod.type} for mod in obj.modifiers]
            if modifiers:
                objects_with_modifiers.append({"object": obj.name, "modifiers": modifiers})
                for mod in modifiers:
                    kind = mod["type"]
                    modifiers_by_type[kind] = modifiers_by_type.get(kind, 0) + 1
            if obj.type != "MESH":
                continue

            base_polygons = len(obj.data.polygons)
            evaluated_obj = obj.evaluated_get(depsgraph)
            evaluated_mesh = evaluated_obj.to_mesh()
            try:
                evaluated_polygons = len(evaluated_mesh.polygons) if evaluated_mesh else 0
            finally:
                if evaluated_mesh is not None:
                    evaluated_obj.to_mesh_clear()
            base_total += base_polygons
            evaluated_total += evaluated_polygons
            meshes.append({
                "name": obj.name,
                "base_polygons": base_polygons,
                "evaluated_polygons": evaluated_polygons,
                "modifiers": modifiers,
            })

    meshes.sort(key=lambda item: (-item["evaluated_polygons"], -item["base_polygons"], item["name"]))
    objects_with_modifiers.sort(key=lambda item: item["object"])
    return {
        "ok": True,
        "blend_file": bpy.data.filepath,
        "scene": scene.name,
        "frame": scene.frame_current,
        "object_count": len(objects),
        "object_counts_by_type": dict(sorted(counts.items())),
        "mesh_count": len(meshes),
        "base_polygon_count": base_total,
        "evaluated_polygon_count": evaluated_total,
        "modifier_count": sum(modifiers_by_type.values()),
        "modifier_counts_by_type": dict(sorted(modifiers_by_type.items())),
        "objects_with_modifiers": objects_with_modifiers,
        "top_10_heaviest_meshes": meshes[:10],
        "evaluation": "viewport geometry at saved current frame; all scene mesh objects including hidden ones; instances not expanded",
    }


def _scene_images(scene):
    """Find images referenced by scene materials and world nodes, without loading pixels."""
    found = set()
    visited_trees = set()

    def visit_tree(tree):
        if tree is None or tree.as_pointer() in visited_trees:
            return
        visited_trees.add(tree.as_pointer())
        for node in tree.nodes:
            image = getattr(node, "image", None)
            if image is not None:
                found.add(image.as_pointer())
            if node.type == "GROUP":
                visit_tree(getattr(node, "node_tree", None))

    for obj in scene.objects:
        for slot in obj.material_slots:
            material = slot.material
            if material and material.use_nodes:
                visit_tree(material.node_tree)
    if scene.world and scene.world.use_nodes:
        visit_tree(scene.world.node_tree)
    return found


def _collect_performance_metrics():
    """Gather observations with bpy. No save, assignment, or operator calls."""
    import bpy

    scene_name = os.environ.get("BLENDBOOST_SCENE_NAME")
    scene = bpy.data.scenes.get(scene_name) if scene_name else bpy.context.scene
    if scene is None:
        raise ValueError(f"Scene not found: {scene_name}")
    active_screen = bpy.context.screen.name if bpy.context.screen else None

    with bpy.context.temp_override(scene=scene):
        depsgraph = bpy.context.evaluated_depsgraph_get()
        objects = list(scene.objects)
        mesh_rows = []
        modifier_rows = []
        object_counts = {}
        for obj in objects:
            object_counts[obj.type] = object_counts.get(obj.type, 0) + 1
            modifiers = []
            for mod in obj.modifiers:
                row = {
                    "object": obj.name,
                    "name": mod.name,
                    "type": mod.type,
                    "viewport_enabled": bool(mod.show_viewport),
                }
                if mod.type in {"SUBSURF", "MULTIRES"}:
                    row["viewport_levels"] = int(getattr(mod, "levels", getattr(mod, "sculpt_levels", 0)))
                if mod.type == "SUBSURF":
                    row["render_levels"] = int(mod.render_levels)
                if mod.type == "NODES":
                    group = getattr(mod, "node_group", None)
                    row["node_group"] = group.name if group else None
                modifier_rows.append(row)
                modifiers.append(row)
            if obj.type != "MESH":
                continue
            base = len(obj.data.polygons)
            evaluated_obj = obj.evaluated_get(depsgraph)
            evaluated_mesh = evaluated_obj.to_mesh()
            try:
                evaluated = len(evaluated_mesh.polygons) if evaluated_mesh else 0
            finally:
                if evaluated_mesh is not None:
                    evaluated_obj.to_mesh_clear()
            mesh_rows.append({
                "name": obj.name,
                "base_polygons": base,
                "evaluated_polygons": evaluated,
                "hidden_from_viewport": bool(obj.hide_viewport or obj.hide_get()),
                "modifier_count": len(modifiers),
            })

        mesh_by_name = {row["name"]: row for row in mesh_rows}
        collection_rows = []

        def visit_collection(collection, parent):
            path = f"{parent}/{collection.name}" if parent else collection.name
            direct = [obj for obj in collection.objects if obj.name in mesh_by_name]
            collection_rows.append({
                "name": collection.name,
                "path": path,
                "direct_object_count": len(collection.objects),
                "direct_mesh_count": len(direct),
                "direct_evaluated_polygons": sum(mesh_by_name[obj.name]["evaluated_polygons"] for obj in direct),
                "collection_hide_viewport": bool(collection.hide_viewport),
            })
            for child in collection.children:
                visit_collection(child, path)

        visit_collection(scene.collection, "")

        referenced_images = _scene_images(scene)
        image_rows = []
        for image in bpy.data.images:
            width, height = map(int, image.size)
            if width <= 0 or height <= 0:
                continue
            channels = max(1, int(image.channels))
            # One decoded buffer, not file size or measured GPU allocation.
            bytes_per_channel = 4 if image.is_float else 1
            image_rows.append({
                "name": image.name,
                "width": width,
                "height": height,
                "channels": channels,
                "is_float": bool(image.is_float),
                "source": image.source,
                "scene_referenced": image.as_pointer() in referenced_images,
                "estimated_uncompressed_bytes": width * height * channels * bytes_per_channel,
            })

        viewports = []
        for screen in bpy.data.screens:
            for index, area in enumerate(screen.areas):
                if area.type != "VIEW_3D":
                    continue
                space = area.spaces.active
                viewports.append({
                    "screen": screen.name,
                    "area_index": index,
                    "screen_is_active": screen.name == active_screen,
                    "shading_type": space.shading.type,
                    "overlays_enabled": bool(space.overlay.show_overlays),
                    "cavity_enabled": bool(getattr(space.shading, "show_cavity", False)),
                    "shadows_enabled": bool(getattr(space.shading, "show_shadows", False)),
                })

    return {
        "blend_file": bpy.data.filepath,
        "scene": scene.name,
        "frame": scene.frame_current,
        "object_count": len(objects),
        "object_counts_by_type": dict(sorted(object_counts.items())),
        "meshes": mesh_rows,
        "modifiers": modifier_rows,
        "collections": collection_rows,
        "root_collection": scene.collection.name,
        "images": image_rows,
        "viewports": viewports,
        "active_screen": active_screen,
        "simplify_enabled": bool(scene.render.use_simplify),
        "simplify_subdivision": int(scene.render.simplify_subdivision),
    }


def _build_performance_report(metrics):
    """Rank evidence-based candidates; scores are heuristics, not timed results."""
    meshes = metrics["meshes"]
    modifiers = metrics["modifiers"]
    images = metrics["images"]
    total_base = sum(row["base_polygons"] for row in meshes)
    total_eval = sum(row["evaluated_polygons"] for row in meshes)
    findings = []

    def add(category, subject, score, measured, explanation, recommendation, impact, estimated=None):
        finding = {
            "category": category,
            "subject": subject,
            "severity": "critical" if score >= 90 else "high" if score >= 70 else "medium" if score >= 40 else "low",
            "severity_score": score,
            "measured_evidence": measured,
            "estimated_impact": impact,
            "explanation": explanation,
            "recommendation": recommendation,
        }
        if estimated is not None:
            finding["estimated_evidence"] = estimated
        findings.append(finding)

    for mesh in meshes:
        evaluated = mesh["evaluated_polygons"]
        base = mesh["base_polygons"]
        if evaluated >= 50000:
            score = 95 if evaluated >= 500000 else 80 if evaluated >= 200000 else 60 if evaluated >= 100000 else 42
            add("high_polygon_object", mesh["name"], score,
                {"base_polygons": base, "evaluated_polygons": evaluated,
                 "hidden_from_viewport": mesh["hidden_from_viewport"],
                 "scene_evaluated_share": round(evaluated / total_eval, 4) if total_eval else 0},
                "This mesh contributes substantial viewport geometry; actual frame cost depends on visibility, materials, and hardware.",
                "Inspect silhouette and deformation needs; consider a lower-detail viewport representation, a lower subdivision level, or simplifying source geometry.",
                "Potential viewport draw and dependency-graph cost; not benchmarked.")
        if base > 0 and evaluated >= 50000 and evaluated / base >= 2:
            ratio = round(evaluated / base, 2)
            add("modifier_geometry_growth", mesh["name"], 78 if ratio >= 4 else 52,
                {"base_polygons": base, "evaluated_polygons": evaluated, "growth_ratio": ratio},
                "Evaluated geometry is substantially larger than the base mesh.",
                "Review viewport modifier levels and generated geometry before changing render quality.",
                "Potential geometry evaluation and draw cost; not benchmarked.")
        if mesh["hidden_from_viewport"] and evaluated >= 50000:
            add("heavy_hidden_object", mesh["name"], 45,
                {"evaluated_polygons": evaluated, "hidden_from_viewport": True},
                "A heavy mesh is hidden in the saved viewport state; this is an inspection candidate, not proof of playback cost.",
                "If it is no longer needed in this scene, consider moving it to an archival file or collection after checking dependencies.",
                "Possible project memory or management cost; viewport impact is uncertain.")

    for mod in modifiers:
        if not mod["viewport_enabled"]:
            continue
        if mod["type"] == "NODES":
            add("geometry_nodes_modifier", mod["object"], 72,
                {"modifier": mod["name"], "type": "NODES", "node_group": mod.get("node_group"),
                 "viewport_enabled": True},
                "Geometry Nodes can require expensive evaluation even when polygon count changes little; node execution time was not measured.",
                "Inspect the node group for repeated geometry generation or simulation; consider a lower-detail viewport branch if appropriate.",
                "Potential dependency-graph cost; node runtime is unknown.")
        elif mod["type"] in {"SUBSURF", "MULTIRES"} and mod.get("viewport_levels", 0) >= 2:
            levels = mod["viewport_levels"]
            add("subdivision_modifier", mod["object"], 78 if levels >= 3 else 55,
                {"modifier": mod["name"], "type": mod["type"], "viewport_levels": levels,
                 "viewport_enabled": True},
                "Multiple viewport subdivision levels can multiply geometry and evaluation work.",
                "Try one fewer viewport subdivision level while keeping the render level unchanged; compare appearance and playback.",
                "Potential geometry evaluation cost; not benchmarked.")

    referenced = [image for image in images if image["scene_referenced"]]
    referenced_bytes = sum(image["estimated_uncompressed_bytes"] for image in referenced)
    for image in referenced:
        size = image["estimated_uncompressed_bytes"]
        if size >= 16 * 1024**2:
            score = 90 if size >= 256 * 1024**2 else 72 if size >= 64 * 1024**2 else 44
            add("large_texture", image["name"], score,
                {"dimensions": [image["width"], image["height"]], "channels": image["channels"],
                 "is_float": image["is_float"]},
                "The image's decoded buffer may consume substantial memory. This estimate excludes mipmaps, copies, compression, and GPU allocation.",
                "Check whether the material needs this resolution; use a smaller source or viewport proxy only after visual review.",
                "Estimated RAM/VRAM pressure; actual allocation was not measured.",
                {"uncompressed_bytes": size})
    if referenced_bytes >= 512 * 1024**2:
        add("texture_memory_total", metrics["scene"], 78 if referenced_bytes >= 1024**3 else 54,
            {"scene_referenced_image_count": len(referenced)},
            "Scene-referenced decoded image buffers add up; actual residency and sharing are unknown.",
            "Review the largest textures first and check texture resolution against final output needs.",
            "Estimated memory pressure; not measured RAM or VRAM usage.",
            {"uncompressed_bytes": referenced_bytes})

    for collection in metrics["collections"]:
        polygons = collection["direct_evaluated_polygons"]
        if total_eval and polygons >= 200000 and polygons / total_eval >= 0.25:
            add("heavy_collection", collection["path"], 70 if polygons / total_eval >= 0.5 else 50,
                {"direct_evaluated_polygons": polygons,
                 "direct_mesh_count": collection["direct_mesh_count"],
                 "scene_evaluated_share": round(polygons / total_eval, 4)},
                "Direct members of this collection hold a large share of scene geometry. Nested collections and shared memberships are counted separately.",
                "Check whether every object needs to be visible during animation; consider collection-level viewport visibility or proxies while working.",
                "Potential viewport draw cost when visible; not benchmarked.")
        direct_count = collection["direct_object_count"]
        object_share = direct_count / metrics["object_count"] if metrics["object_count"] else 0
        if direct_count >= 100 and object_share >= 0.2:
            add("high_object_count_collection", collection["path"],
                40 if collection.get("collection_hide_viewport", False) else 65 if direct_count >= 400 else 48,
                {"direct_object_count": direct_count,
                 "direct_mesh_count": collection["direct_mesh_count"],
                 "direct_evaluated_polygons": polygons,
                 "scene_object_share": round(object_share, 4),
                 "average_evaluated_polygons_per_direct_mesh": round(
                     polygons / collection["direct_mesh_count"], 1
                 ) if collection["direct_mesh_count"] else None,
                 "collection_hide_viewport": collection.get("collection_hide_viewport", False)},
                "Many separate objects may add dependency-graph and draw-submission overhead even when each mesh is small. This cost was not timed.",
                "During playback, isolate this collection to compare FPS. For repeated static pieces, consider instancing or batching only after checking materials, culling, and editability.",
                "Potential per-object viewport overhead when visible; not benchmarked.")

    for viewport in metrics["viewports"]:
        if not viewport["screen_is_active"]:
            continue
        mode = viewport["shading_type"]
        if mode in {"RENDERED", "MATERIAL"}:
            add("viewport_shading", f"{viewport['screen']} area {viewport['area_index']}",
                72 if mode == "RENDERED" else 48,
                {"shading_type": mode, "overlays_enabled": viewport["overlays_enabled"]},
                "The saved 3D View uses a shading mode that may cost more during playback than Solid mode.",
                "Use Solid shading while animating, then switch back for look development.",
                "Potential viewport shading cost; not benchmarked.")
    if not metrics["simplify_enabled"] and any(
        mod["viewport_enabled"] and mod["type"] in {"SUBSURF", "MULTIRES"} for mod in modifiers
    ):
        add("viewport_simplify", metrics["scene"], 42,
            {"simplify_enabled": False, "active_subdivision_modifiers": sum(
                mod["viewport_enabled"] and mod["type"] in {"SUBSURF", "MULTIRES"} for mod in modifiers)},
            "Render Simplify is off while viewport subdivision modifiers are active.",
            "Consider enabling Simplify for playback and setting a conservative viewport subdivision cap; restore it for quality checks.",
            "Potential viewport evaluation reduction; not benchmarked.")

    findings.sort(key=lambda row: (-row["severity_score"], row["category"], row["subject"]))
    modifier_counts = {}
    for mod in modifiers:
        modifier_counts[mod["type"]] = modifier_counts.get(mod["type"], 0) + 1
    heaviest = sorted(meshes, key=lambda row: (-row["evaluated_polygons"], row["name"]))[:10]
    largest_images = sorted(referenced, key=lambda row: (-row["estimated_uncompressed_bytes"], row["name"]))[:10]
    return {
        "ok": True,
        "report_version": 1,
        "source": {"blend_file": metrics["blend_file"], "scene": metrics["scene"], "saved_frame": metrics["frame"]},
        "measured_metrics": {
            "object_count": metrics["object_count"],
            "object_counts_by_type": metrics["object_counts_by_type"],
            "mesh_count": len(meshes),
            "base_polygon_count": total_base,
            "evaluated_polygon_count": total_eval,
            "modifier_counts_by_type": dict(sorted(modifier_counts.items())),
            "heaviest_meshes": heaviest,
            "collections": metrics["collections"],
            "viewports": metrics["viewports"],
            "active_screen": metrics["active_screen"],
            "simplify_enabled": metrics["simplify_enabled"],
            "simplify_subdivision": metrics["simplify_subdivision"],
        },
        "estimated_metrics": {
            "scene_referenced_image_count": len(referenced),
            "scene_referenced_uncompressed_image_bytes": referenced_bytes,
            "largest_scene_referenced_images": largest_images,
            "image_estimate_method": "width × height × channels × (4 bytes if float, otherwise 1 byte); one decoded image buffer only",
        },
        "findings": findings[:50],
        "omitted_finding_count": max(0, len(findings) - 50),
        "severity_method": {
            "type": "rule_based_estimate",
            "score_ranges": {"critical": "90-100", "high": "70-89", "medium": "40-69", "low": "0-39"},
            "examples": [
                "Mesh polygon thresholds: 50k, 100k, 200k, 500k evaluated polygons.",
                "Decoded image estimate thresholds: 16, 64, 256 MiB per scene-referenced image.",
                "Collection flag: at least 200k direct evaluated polygons and 25% of scene total.",
            "Object-count collection flag: at least 100 direct objects and 20% of scene objects.",
            ],
        },
        "assessment_limits": [
            "Severity is a threshold heuristic, not a measured frame-time, RAM, or VRAM benchmark.",
            "Evaluated polygons use the saved frame's viewport dependency graph for mesh objects; generated instances are not expanded.",
            "Polygon totals include hidden scene mesh objects; hiding alone does not remove their polygons from these totals.",
            "Image estimates exclude mipmaps, multiple buffers, tiled or animated frames, and actual GPU residency.",
            "Only saved 3D View settings are inspected; unsaved UI changes are not included.",
            "Heavy or hidden geometry is an optimization candidate, not proof it is unnecessary.",
        ],
    }


def _analyze_in_blender():
    return _build_performance_report(_collect_performance_metrics())


if __name__ == "__main__":
    try:
        payload = _analyze_in_blender() if os.environ.get("BLENDBOOST_TASK") == "analyze" else _scan_in_blender()
    except Exception as exc:
        payload = {"ok": False, "error": {"code": "scene_scan_failed", "message": str(exc)}}
    print(_RESULT_MARKER + json.dumps(payload, ensure_ascii=False))
