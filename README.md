# BlendBoost

BlendBoost is a Hermes Agent Python plugin for inspecting saved Blender scenes,
proposing reversible viewport changes, and comparing headless frame evaluation.
It uses Blender's local `bpy` API in a separate background process. No network
service or Python package dependency is required.

<img width="2592" height="1458" alt="BlendBoost_Img" src="https://github.com/user-attachments/assets/e60c4691-3311-48c8-966c-afed71a0c2d5" /> 
<!--<img width="1920" height="1080" alt="BlendBoost_Img2" src="https://github.com/user-attachments/assets/720c8c8e-58f6-4729-86f2-e399ff815fac" /> -->

## Tools

| Tool | What it does |
| --- | --- |
| `scan_scene` | Count scene objects, base and evaluated mesh polygons, modifiers, and the ten heaviest meshes. |
| `analyze_performance` | Rank geometry, modifier, image, collection, and saved viewport candidates with measured evidence and estimated impact. |
| `optimize_scene` | Read-only alias for an optimization preview. |
| `preview_optimizations` | Produce a plan ID and proposed viewport-only changes; writes nothing. |
| `apply_optimizations` | After Hermes human approval, save selected changes to a **new** `.blend` file. |
| `restore_optimizations` | After Hermes human approval, write another new copy with BlendBoost changes reverted. |
| `benchmark_performance` | Time headless frame evaluation on one or two saved files and return structured JSON. |

The optimizer only changes viewport subdivision levels, an already-enabled
viewport Simplify cap, or explicitly selected object/collection viewport
visibility. It never deletes scene data or changes render subdivision levels.
Global Simplify is not enabled automatically when off because it can affect
render output. Temporary hides are opt-in and may remove needed objects from
the working viewport. No action promises an FPS improvement.

## Requirements and Windows installation

- Hermes Agent with Python plugin support.
- Blender 5.1 or newer available locally; Windows testing used Blender 5.2.3.
- Saved `.blend` files. Unsaved changes in an open Blender window are not read.

For a local checkout whose root contains `plugin.yaml`, run in PowerShell:

```powershell
$source = 'C:\path\to\BlendBoost'
$target = Join-Path $env:LOCALAPPDATA 'hermes\plugins\blendboost'
New-Item -ItemType Directory -Force -Path $target | Out-Null
Copy-Item -LiteralPath (Join-Path $source 'plugin.yaml') -Destination $target -Force
Copy-Item -LiteralPath (Join-Path $source '__init__.py') -Destination $target -Force
Copy-Item -LiteralPath (Join-Path $source 'schemas.py') -Destination $target -Force
Copy-Item -LiteralPath (Join-Path $source 'tools.py') -Destination $target -Force
Copy-Item -LiteralPath (Join-Path $source 'optimizer.py') -Destination $target -Force
Copy-Item -LiteralPath (Join-Path $source 'benchmark.py') -Destination $target -Force
hermes plugins doctor $target
hermes plugins enable blendboost
hermes plugins list --plain --user | Select-String blendboost
```

Restart Hermes Desktop after updating the Python files. If Blender is not on
`PATH`, pass its full path as `blender_executable` in each tool call. Example:
`C:\Program Files\Blender Foundation\Blender 5.2\blender.exe`.

## Usage in Hermes Desktop

Ask Hermes to call `scan_scene` or `analyze_performance` with the absolute path
to a saved `.blend` file. To optimize safely:

1. Call `preview_optimizations` with `blend_file` and, optionally, a new
   `output_file` in the **same directory** as the input. Review the returned
   `plan_id` and change IDs. No changes are selected for temporary hides.
2. Call `apply_optimizations` with that exact `plan_id`, `output_file`, and a
   nonempty `selected_change_ids` list. Hermes asks the human to approve the
   write. Denial leaves the source unchanged and creates no copy.
3. To undo, call `preview_optimizations` on the optimized copy, then call
   `restore_optimizations` with its `restore_id` and a different new
   `output_file`. Hermes asks for approval again. The original remains intact.

For benchmarking, use a saved original and optimized copy:

```text
Use BlendBoost benchmark_performance with:
blend_file: C:\path\scene.blend
optimized_file: C:\path\scene_blendboost.blend
frame_start: 108
frame_end: 117
frame_step: 1
warmup_frames: 3
passes: 3
blender_executable: C:\Program Files\Blender Foundation\Blender 5.2\blender.exe
Explain average, median, P95, outliers, and limitations. Do not call this viewport FPS.
```

`benchmark_performance` returns JSON directly. An optional `report_file` must
be an absolute path to a **new** `.json` file. The report contains raw samples,
raw statistics, repeated-frame typical statistics, and comparison percentages.
It times `scene.frame_set`, evaluated dependency graph access, and visible mesh
polygon readback with `time.perf_counter()`. It does not render, draw a window,
or measure interactive viewport FPS. Warm-up and timed frame settings are
identical for both files on the same machine and Blender executable. One run
can still be affected by background load or cache effects.

## Testing

```powershell
$env:BLENDER_EXECUTABLE = 'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe'
python -m unittest discover -s .\tests -v
hermes plugins validate . --install-deps
hermes plugins doctor .
```

Run those commands from the plugin directory. Tests create temporary `.blend`
fixtures and verify invalid paths, missing Blender, corrupt files, copy and
restore behavior, preserved rig/animation/render data, and benchmark output.
The `examples/` JSON files come from generated test fixtures; they are not
claims about a user's scene.

## Windows troubleshooting

| Symptom | Check |
| --- | --- |
| Tool missing in Desktop | Run `hermes plugins list --plain --user` and `hermes plugins doctor <plugin-directory>`; ensure enabled, then restart Desktop. |
| Blender not found | Pass `blender_executable` as an absolute path. Check it with `& 'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe' --version`. |
| Corrupt or unsupported `.blend` | Open a known-good file in Blender, save a fresh copy, and retry. No output copy is published on failure. |
| Plan stale | Re-run `preview_optimizations`; a changed source file or output path changes the plan ID. |
| Output exists | Choose a new filename. BlendBoost never overwrites an existing `.blend` or JSON report. |
| Benchmark timeout | Use fewer frames or a larger `timeout_seconds` value (10–900 per Blender process). |

## Safety, limitations, and disclosure

The plugin reads the user-selected `.blend` files, launches local Blender
subprocesses, and may run for minutes on large scenes. It does not access
credentials, send telemetry, or make network requests. Apply and restore write
new `.blend` files beside the source; benchmark JSON export is optional and
also refuses to overwrite a file. All `.blend` input hashes are checked around
benchmark runs. Blender file evaluation can load linked local assets. Python
scripts embedded in `.blend` files are disabled with `--disable-autoexec`.

The optimizer's Hermes `pre_tool_call` hook requests human approval for apply
and restore. The plan ID detects stale proposals. Direct Python invocation of
the handler outside Hermes does not pass through Hermes's approval UI, so use
Hermes for human-gated operation. Restoration creates another copy; it does
not overwrite the optimized copy. Blender may serialize files differently, so
a restored file need not be byte-identical to the original even when the
changed viewport settings are restored.

Evaluated mesh counts omit generated instances. Image memory is estimated from
decoded dimensions, not measured RAM/VRAM. Benchmark timing includes Python
traversal and excludes drawing, rendering, and interactive viewport FPS.

Licensed under MIT; see `LICENSE`.
