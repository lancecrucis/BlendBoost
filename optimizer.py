"""Copy-only Blender viewport optimization with preview and restoration.

The Hermes host calls preview/apply/restore. Blender runs this same file in a
background process for bpy access. Only apply/restore may save, and only to a
temporary new file that the host publishes without replacing an existing file.
"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid


_MARKER = "BLENDBOOST_OPTIMIZER_RESULT:"
_META_KEY = "blendboost_restore_v1"
_TIMEOUT = 240


def _json_error(code, message):
    return json.dumps({"ok": False, "error": {"code": code, "message": str(message)}})


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _validate(args, operation):
    if not isinstance(args, dict):
        raise ValueError("Expected an argument object.")
    raw_source = args.get("blend_file")
    if not isinstance(raw_source, str) or not raw_source.strip():
        raise ValueError("blend_file must be an absolute .blend path.")
    source = Path(raw_source).expanduser()
    if not source.is_absolute() or source.suffix.lower() != ".blend" or not source.is_file():
        raise ValueError("blend_file must name an existing absolute .blend file.")
    source = source.resolve(strict=True)

    scene_name = args.get("scene_name")
    if scene_name is not None and (not isinstance(scene_name, str) or not scene_name.strip()):
        raise ValueError("scene_name must be a nonempty string.")

    raw_output = args.get("output_file")
    if operation in {"apply", "restore"} and not raw_output:
        raise ValueError("output_file is required for a write operation.")
    if raw_output is None:
        suffix = "_restored" if operation == "preview_restore" else "_blendboost"
        output = source.with_name(source.stem + suffix + ".blend")
    else:
        if not isinstance(raw_output, str) or not raw_output.strip():
            raise ValueError("output_file must be an absolute .blend path.")
        output = Path(raw_output).expanduser()
        if not output.is_absolute() or output.suffix.lower() != ".blend":
            raise ValueError("output_file must be an absolute .blend path.")
    if output.parent.resolve(strict=True) != source.parent:
        raise ValueError("output_file must be in the same directory as blend_file to preserve relative asset paths.")
    if output.resolve() == source or output.exists() or output.is_symlink():
        raise ValueError("output_file must be a new path; existing files are never overwritten.")

    executable = args.get("blender_executable") or os.environ.get("BLENDER_EXECUTABLE") or "blender"
    if not isinstance(executable, str):
        raise ValueError("blender_executable must be a path string.")
    executable = shutil.which(executable)
    if executable is None:
        raise FileNotFoundError("Blender executable was not found. Supply blender_executable or set BLENDER_EXECUTABLE.")
    return source, output, executable, scene_name


def _run_blender(source, executable, request):
    env = os.environ.copy()
    env["BLENDBOOST_OPT_REQUEST"] = json.dumps(request, separators=(",", ":"))
    command = [executable, "--background", "--factory-startup", "--disable-autoexec",
               "--python-exit-code", "1", str(source), "--python", str(Path(__file__).resolve())]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=_TIMEOUT, check=False, env=env)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": {"code": "blender_timeout", "message": "Blender optimization timed out."}}
    except OSError as exc:
        return {"ok": False, "error": {"code": "blender_connection_failed", "message": str(exc)}}
    lines = [line[len(_MARKER):] for line in completed.stdout.splitlines() if line.startswith(_MARKER)]
    if completed.returncode != 0 or not lines:
        detail = (completed.stderr or completed.stdout).strip()[-1000:]
        return {"ok": False, "error": {"code": "blender_failed",
                                        "message": f"Blender exited with code {completed.returncode}: {detail}"}}
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError:
        return {"ok": False, "error": {"code": "invalid_blender_result", "message": "Blender returned invalid JSON."}}


def preview(args):
    try:
        source, output, executable, scene_name = _validate(args, "preview")
        return json.dumps(_run_blender(source, executable, {
            "operation": "preview", "source_sha256": _sha256(source),
            "output_file": str(output), "scene_name": scene_name,
            "output_explicit": bool(args.get("output_file")),
        }))
    except FileNotFoundError as exc:
        return _json_error("blender_not_found", exc)
    except (ValueError, OSError) as exc:
        return _json_error("invalid_request", exc)


def _publish_new_copy(temp, output):
    if not temp.is_file() or temp.stat().st_size == 0:
        raise OSError("Blender did not produce a valid output file.")
    digest = _sha256(temp)
    # Same-directory hard link atomically creates a new name and fails if it exists.
    # This avoids os.replace, which would overwrite a concurrent user file.
    os.link(temp, output)
    return digest


def _write_copy(args, operation):
    try:
        source, output, executable, scene_name = _validate(args, operation)
        source_hash = _sha256(source)
        if operation == "apply":
            plan_id = args.get("plan_id")
            selected = args.get("selected_change_ids")
            if not isinstance(plan_id, str) or len(plan_id) != 64:
                raise ValueError("A valid plan_id from preview_optimizations is required.")
            if (not isinstance(selected, list) or not selected or
                    any(not isinstance(item, str) for item in selected) or len(set(selected)) != len(selected)):
                raise ValueError("selected_change_ids must be a nonempty list of unique change IDs.")
        else:
            plan_id = args.get("restore_id")
            selected = None
            if not isinstance(plan_id, str) or len(plan_id) != 64:
                raise ValueError("A valid restore_id from preview_optimizations is required.")

        temp = output.with_name(f".blendboost-{uuid.uuid4().hex}.blend")
        request = {
            "operation": operation, "source_sha256": source_hash,
            "output_file": str(output), "temp_file": str(temp),
            "scene_name": scene_name, "plan_id": plan_id,
            "selected_change_ids": selected,
        }
        try:
            result = _run_blender(source, executable, request)
            if not result.get("ok"):
                return json.dumps(result)
            if output.exists() or output.is_symlink():
                return _json_error("output_exists", "Output path appeared while Blender was running; no file was overwritten.")
            result["output_sha256"] = _publish_new_copy(temp, output)
            return json.dumps(result)
        finally:
            temp.unlink(missing_ok=True)
    except FileNotFoundError as exc:
        return _json_error("blender_not_found", exc)
    except (ValueError, OSError) as exc:
        return _json_error("optimization_failed", exc)


def apply(args):
    return _write_copy(args, "apply")


def restore(args):
    return _write_copy(args, "restore")


def _action(kind, target, prop, before, after, reason, default_selected=False, temporary=False):
    core = {"kind": kind, "target": target, "property": prop, "before": before, "after": after}
    return {"id": kind + ":" + _fingerprint(core)[:16], **core,
            "reason": reason, "default_selected": default_selected, "temporary": temporary}


def _propose(metrics):
    actions = []
    scene = metrics["scene"]
    for mod in metrics["modifiers"]:
        if mod["type"] == "SUBSURF" and mod["viewport_enabled"] and mod.get("viewport_levels", 0) >= 2:
            actions.append(_action(
                "subdivision_viewport", {"scene": scene, "object": mod["object"], "modifier": mod["name"]},
                "levels", mod["viewport_levels"], mod["viewport_levels"] - 1,
                "Reduce viewport subdivision by one level; render_levels stays unchanged.", True))
    if metrics["simplify_enabled"] and metrics["simplify_subdivision"] > 1:
        actions.append(_action(
            "simplify_viewport_cap", {"scene": scene}, "simplify_subdivision",
            metrics["simplify_subdivision"], 1,
            "Cap viewport subdivision while existing Simplify remains enabled; render cap stays unchanged.", True))
    total_polygons = sum(mesh["evaluated_polygons"] for mesh in metrics["meshes"])
    for collection in metrics["collections"]:
        if collection["path"] == metrics.get("root_collection") or collection["collection_hide_viewport"]:
            continue
        count = collection["direct_object_count"]
        polygons = collection["direct_evaluated_polygons"]
        many_objects = count >= 100 and count / max(1, metrics["object_count"]) >= 0.2
        many_polygons = polygons >= 200000 and polygons / max(1, total_polygons) >= 0.25
        if many_objects or many_polygons:
            actions.append(_action(
                "hide_collection_viewport", {"scene": scene, "collection": collection["name"],
                                             "path": collection["path"]}, "hide_viewport", False, True,
                "Temporary viewport isolation; only choose if this collection is not needed during the current task.",
                temporary=True))
    for mesh in metrics["meshes"]:
        if mesh["evaluated_polygons"] >= 50000 and not mesh["hidden_from_viewport"]:
            actions.append(_action(
                "hide_object_viewport", {"scene": scene, "object": mesh["name"]},
                "hide_viewport", False, True,
                "Temporary viewport isolation of a heavy object; only choose if it is not needed while working.",
                temporary=True))
    return actions


def _target_property(action, scene):
    import bpy

    target = action["target"]
    if target.get("scene") != scene.name:
        raise ValueError("Action targets a different scene.")
    kind = action["kind"]
    if kind == "simplify_viewport_cap":
        if action["property"] != "simplify_subdivision" or not scene.render.use_simplify:
            raise ValueError("Simplify setting is no longer eligible.")
        return scene.render, "simplify_subdivision"
    if kind == "subdivision_viewport":
        obj = scene.objects.get(target.get("object"))
        mod = obj.modifiers.get(target.get("modifier")) if obj else None
        if not mod or mod.type != "SUBSURF" or action["property"] != "levels":
            raise ValueError("Subdivision target changed.")
        return mod, "levels"
    if kind == "hide_collection_viewport":
        collection = bpy.data.collections.get(target.get("collection"))
        if not collection or action["property"] != "hide_viewport":
            raise ValueError("Collection target changed.")
        return collection, "hide_viewport"
    if kind == "hide_object_viewport":
        obj = scene.objects.get(target.get("object"))
        if not obj or action["property"] != "hide_viewport":
            raise ValueError("Object target changed.")
        return obj, "hide_viewport"
    raise ValueError("Unknown optimization kind.")


def _preview_in_blender(request, scene, source_hash):
    from pathlib import Path as _Path
    import bpy
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import tools

    with bpy.context.temp_override(scene=scene):
        metrics = tools._collect_performance_metrics()
    metadata = scene.get(_META_KEY)
    if metadata:
        saved = json.loads(metadata)
        changes = saved.get("changes", [])
        restore_output = _Path(request["output_file"])
        if not request.get("output_explicit") and restore_output.name.endswith("_blendboost.blend"):
            restore_output = restore_output.with_name(restore_output.stem[:-11] + "_restored.blend")
        if restore_output.exists() or restore_output.is_symlink():
            raise ValueError("Restore output path already exists; supply a new output_file.")
        restore_id = _fingerprint({"source_sha256": source_hash, "output_file": str(restore_output),
                                   "changes": changes, "scene": scene.name, "operation": "restore"})
        return {"ok": True, "mode": "restore_preview", "source_file": metrics["blend_file"],
                "output_file": str(restore_output), "restore_id": restore_id,
                "restore_changes": changes, "actions": [],
                "note": "This file already contains BlendBoost changes; restore into a new copy before planning further optimization."}
    actions = _propose(metrics)
    plan_id = _fingerprint({"source_sha256": source_hash, "output_file": request["output_file"],
                            "scene": scene.name, "actions": actions, "operation": "apply"})
    notes = ["Temporary visibility changes are opt-in and affect only the new copy's viewport."]
    if not metrics["simplify_enabled"]:
        notes.append("Global use_simplify is off. BlendBoost does not enable it automatically because it can affect render output; viewport-only caps are offered when Simplify is already on.")
    return {"ok": True, "mode": "optimization_preview", "source_file": metrics["blend_file"],
            "source_sha256": source_hash, "output_file": request["output_file"],
            "scene": scene.name, "plan_id": plan_id, "actions": actions,
            "default_selected_change_ids": [a["id"] for a in actions if a["default_selected"]],
            "approval_required": True, "notes": notes,
            "no_fps_claim": "No playback or render benchmark has been run."}


def _save_copy(path):
    import bpy

    if Path(path).exists():
        raise ValueError("Temporary output path already exists.")
    result = bpy.ops.wm.save_as_mainfile(filepath=path, check_existing=False, copy=True)
    if "FINISHED" not in result:
        raise RuntimeError("Blender did not finish saving the copy.")


def _execute_in_blender(request):
    import bpy

    source = Path(bpy.data.filepath).resolve(strict=True)
    source_hash = _sha256(source)
    if source_hash != request["source_sha256"]:
        raise ValueError("Source file changed since preview; request a new preview.")
    scene_name = request.get("scene_name")
    scene = bpy.data.scenes.get(scene_name) if scene_name else bpy.context.scene
    if scene is None:
        raise ValueError(f"Scene not found: {scene_name}")

    operation = request["operation"]
    if operation == "preview":
        return _preview_in_blender(request, scene, source_hash)

    if operation == "apply":
        with bpy.context.temp_override(scene=scene):
            preview_result = _preview_in_blender(request, scene, source_hash)
        if preview_result["mode"] != "optimization_preview" or preview_result["plan_id"] != request["plan_id"]:
            raise ValueError("Plan is stale or does not match this source and destination; preview again.")
        by_id = {action["id"]: action for action in preview_result["actions"]}
        selected_ids = request["selected_change_ids"]
        if any(change_id not in by_id for change_id in selected_ids):
            raise ValueError("Selected change ID is not in the current plan.")
        changes = [by_id[change_id] for change_id in selected_ids]
        for action in changes:
            target, prop = _target_property(action, scene)
            if getattr(target, prop) != action["before"]:
                raise ValueError(f"Target changed before apply: {action['id']}")
        for action in changes:
            target, prop = _target_property(action, scene)
            setattr(target, prop, action["after"])
        scene[_META_KEY] = json.dumps({"schema_version": 1, "source_sha256": source_hash,
                                      "changes": changes}, separators=(",", ":"))
        if _sha256(source) != source_hash:
            raise ValueError("Source changed during optimization; no copy was saved.")
        _save_copy(request["temp_file"])
        return {"ok": True, "operation": "apply", "source_file": str(source),
                "output_file": request["output_file"], "source_sha256": source_hash,
                "plan_id": request["plan_id"], "changes": changes,
                "render_specific_settings_changed": False, "fps_benchmarked": False}

    if operation == "restore":
        metadata = scene.get(_META_KEY)
        if not isinstance(metadata, str):
            raise ValueError("No BlendBoost restore metadata found in this scene.")
        saved = json.loads(metadata)
        if saved.get("schema_version") != 1 or not isinstance(saved.get("changes"), list) or not saved["changes"]:
            raise ValueError("Invalid BlendBoost restore metadata.")
        changes = saved["changes"]
        restore_id = _fingerprint({"source_sha256": source_hash, "output_file": request["output_file"],
                                   "changes": changes, "scene": scene.name, "operation": "restore"})
        if restore_id != request["plan_id"]:
            raise ValueError("Restore preview is stale or output path differs; preview again.")
        for action in changes:
            target, prop = _target_property(action, scene)
            if getattr(target, prop) != action["after"]:
                raise ValueError(f"Optimized setting was edited later; refusing to overwrite it: {action['id']}")
        for action in changes:
            target, prop = _target_property(action, scene)
            setattr(target, prop, action["before"])
        del scene[_META_KEY]
        if _sha256(source) != source_hash:
            raise ValueError("Optimized file changed during restoration; no copy was saved.")
        _save_copy(request["temp_file"])
        return {"ok": True, "operation": "restore", "source_file": str(source),
                "output_file": request["output_file"], "source_sha256": source_hash,
                "restore_id": restore_id, "restored_changes": changes,
                "render_specific_settings_changed": False, "fps_benchmarked": False}
    raise ValueError("Unknown optimization operation.")


if __name__ == "__main__":
    try:
        payload = _execute_in_blender(json.loads(os.environ["BLENDBOOST_OPT_REQUEST"]))
    except Exception as exc:
        payload = {"ok": False, "error": {"code": "blender_optimization_failed", "message": str(exc)}}
    print(_MARKER + json.dumps(payload, ensure_ascii=False))
