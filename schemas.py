"""Hermes tool schema for BlendBoost."""

SCAN_SCENE = {
    "name": "scan_scene",
    "description": (
        "Read a saved Blender .blend file in a separate background Blender process. "
        "Report scene object counts, base and viewport-evaluated polygon counts, "
        "modifiers, and the 10 mesh objects with the most evaluated polygons. "
        "Does not save or edit the Blender project. Unsaved changes in an open Blender window are not included."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "blend_file": {
                "type": "string",
                "description": "Absolute path to an existing .blend file to inspect.",
            },
            "scene_name": {
                "type": "string",
                "description": "Optional scene name; defaults to the scene active when the file opens.",
            },
            "blender_executable": {
                "type": "string",
                "description": (
                    "Optional absolute path to Blender executable. Defaults to "
                    "BLENDER_EXECUTABLE environment variable or blender on PATH."
                ),
            },
        },
        "required": ["blend_file"],
    },
}

ANALYZE_PERFORMANCE = {
    "name": "analyze_performance",
    "description": (
        "Read a saved Blender .blend file and produce a structured, read-only performance report. "
        "Measures base and evaluated mesh polygons, active modifiers, referenced image sizes, "
        "collection geometry, and saved viewport settings. Ranks likely bottlenecks with evidence "
        "and specific recommendations. Severity and memory impact are estimates, not benchmarks. "
        "Does not change the project; unsaved Blender-window changes are not included."
    ),
    "parameters": SCAN_SCENE["parameters"],
}

_OPTIMIZE_INPUTS = {
    "blend_file": SCAN_SCENE["parameters"]["properties"]["blend_file"],
    "blender_executable": SCAN_SCENE["parameters"]["properties"]["blender_executable"],
    "scene_name": SCAN_SCENE["parameters"]["properties"]["scene_name"],
    "output_file": {
        "type": "string",
        "description": "Absolute path for a new .blend copy in the same directory as the source. Must not already exist.",
    },
    "protected_objects": {"type": "array", "items": {"type": "string"},
                          "description": "Exact scene object names to leave untouched; default empty. Repeat preview options when applying."},
    "protected_collections": {"type": "array", "items": {"type": "string"},
                              "description": "Exact collection names; protects all descendants. Repeat preview options when applying."},
    "proxy_ratio": {"type": "number", "minimum": 0.1, "maximum": 1,
                    "description": "Triangle reduction target for heavy static mesh proxies; default 0.5. Use 1 for full-detail caching. Collection batches retain full geometry. Proxies are opt-in and may alter viewport appearance."},
    "benchmark_after_apply": {"type": ["object", "null"], "additionalProperties": False,
        "description": "Optional approved trial budget. Benchmarks the original and staged copy before publishing. Failures or consistent regressions discard the trial; inconclusive copies are labeled for manual review. Repeat these options from preview.",
        "properties": {
            "frame_start": {"type": "integer"}, "frame_end": {"type": "integer"},
            "frame_step": {"type": "integer", "minimum": 1},
            "warmup_frames": {"type": "integer", "minimum": 1, "maximum": 100},
            "passes": {"type": "integer", "minimum": 2, "maximum": 10},
            "rounds": {"type": "integer", "minimum": 2, "maximum": 4},
            "timeout_seconds": {"type": "integer", "minimum": 10, "maximum": 900},
        }, "required": ["frame_start", "frame_end"]},
}

PREVIEW_OPTIMIZATIONS = {
    "name": "preview_optimizations",
    "description": (
        "Read a saved Blender file and return a proposed viewport-only optimization plan, "
        "plan ID, opt-in static scenery proxies and collection batching, exclusions, and restore information. Does not write files. "
        "Review the proposal with the user before applying it."
    ),
    "parameters": {"type": "object", "properties": _OPTIMIZE_INPUTS, "required": ["blend_file"]},
}

OPTIMIZE_SCENE = {
    "name": "optimize_scene",
    "description": "Read-only entry point for BlendBoost optimization. Returns the same proposal as preview_optimizations; never applies changes.",
    "parameters": PREVIEW_OPTIMIZATIONS["parameters"],
}

APPLY_OPTIMIZATIONS = {
    "name": "apply_optimizations",
    "description": (
        "After the user reviews a preview, request Hermes human approval and write selected "
        "viewport-only changes to a NEW .blend copy. Requires the preview's plan_id and selected_change_ids. "
        "Repeat the preview's options exactly. Can create viewport-only proxies retaining originals for rendering, "
        "and optionally benchmark a staged copy. Never overwrites a file or alters the original. No viewport FPS improvement is claimed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            **_OPTIMIZE_INPUTS,
            "plan_id": {"type": "string", "description": "Exact plan_id from the latest preview for this source and destination."},
            "selected_change_ids": {"type": "array", "items": {"type": "string"},
                                    "description": "Nonempty list of change IDs explicitly approved by the user."},
        },
        "required": ["blend_file", "output_file", "plan_id", "selected_change_ids"],
    },
}

RESTORE_OPTIMIZATIONS = {
    "name": "restore_optimizations",
    "description": (
        "Request Hermes human approval and restore BlendBoost-changed viewport settings "
        "and remove only unedited BlendBoost-generated proxies from an optimized copy into a NEW .blend copy. Never overwrites the optimized copy."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "blend_file": {"type": "string", "description": "Absolute path to a BlendBoost-optimized .blend file."},
            "output_file": _OPTIMIZE_INPUTS["output_file"],
            "blender_executable": _OPTIMIZE_INPUTS["blender_executable"],
            "scene_name": _OPTIMIZE_INPUTS["scene_name"],
            "restore_id": {"type": "string", "description": "Exact restore_id from preview_optimizations on the optimized file."},
        },
        "required": ["blend_file", "output_file", "restore_id"],
    },
}

BENCHMARK_PERFORMANCE = {
    "name": "benchmark_performance",
    "description": (
        "Measure headless Blender frame evaluation for a saved .blend file and optionally "
        "compare it with an optimized .blend file. Uses identical frame range, step, warm-up, "
        "Blender executable, and hardware for both runs. Returns average, median, P95, and "
        "estimated headless evaluation throughput plus percentage changes. Explain results simply, "
        "with measured timing and limitations. This is NOT actual "
        "interactive viewport FPS; no render or project save occurs. Optionally export JSON to a new file."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "blend_file": SCAN_SCENE["parameters"]["properties"]["blend_file"],
            "optimized_file": {
                "type": "string",
                "description": "Optional absolute path to an optimized .blend file for a before-and-after comparison.",
            },
            "scene_name": SCAN_SCENE["parameters"]["properties"]["scene_name"],
            "view_layer_name": {"type": "string", "description": "Optional saved view layer name; defaults to the first view layer in each file."},
            "blender_executable": SCAN_SCENE["parameters"]["properties"]["blender_executable"],
            "frame_start": {"type": "integer", "description": "First animation frame to time, inclusive."},
            "frame_end": {"type": "integer", "description": "Last animation frame to time, inclusive."},
            "frame_step": {"type": "integer", "minimum": 1, "description": "Frame interval; defaults to 1."},
            "warmup_frames": {"type": "integer", "minimum": 1,
                              "description": "Number of untimed frame evaluations before measurements; defaults to 3."},
            "passes": {"type": "integer", "minimum": 1, "maximum": 10,
                       "description": "Complete timed passes over the frame range; defaults to 3 for outlier visibility."},
            "rounds": {"type": "integer", "minimum": 1, "maximum": 4,
                       "description": "Fresh-process rounds, alternating original/copy order. Default 1; use at least 2 for an improvement/regression verdict."},
            "timeout_seconds": {"type": "integer", "minimum": 10, "maximum": 900,
                                "description": "Maximum seconds per Blender subprocess; defaults to 300."},
            "report_file": {"type": "string", "description": "Optional absolute path to a NEW .json report file; existing files are never overwritten."},
        },
        "required": ["blend_file", "frame_start", "frame_end"],
    },
}
