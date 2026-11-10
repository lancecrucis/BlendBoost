# BlendBoost

BlendBoost helps animators work with heavy Blender scenery while retaining the
original assets for rendering. It inspects saved scenes, proposes reversible
working proxies, and tests approved copies with headless frame evaluation.
It uses Blender's local `bpy` API in a separate background process. No network
service or Python package dependency is required.

<img width="2592" height="1458" alt="BlendBoost" src="https://github.com/user-attachments/assets/e60c4691-3311-48c8-966c-afed71a0c2d5" />
<!--<img width="1920" height="1080" alt="BlendBoost_Img2" src="https://github.com/user-attachments/assets/720c8c8e-58f6-4729-86f2-e399ff815fac" /> -->

## Capabilities

- **Inspect:** count objects, base and evaluated polygons, modifiers, images,
  collection weight, saved viewport settings, visible geometry, and the ten
  heaviest meshes.
- **Explain:** rank likely bottlenecks with measured evidence, estimated impact,
  and concrete recommendations. Estimates are labeled separately from timings.
- **Optimize safely:** preview viewport subdivision changes, an existing
  Simplify cap, temporary visibility changes, static mesh proxies, and batched
  collection proxies. Unsupported or animated dependencies are excluded with a
  reason instead of being frozen or guessed safe.
- **Protect production data:** keep original meshes, rigs, actions, materials,
  render settings, and render-visible objects. Apply writes only to a new
  `.blend` file after Hermes human approval and refuses existing destinations.
- **Measure:** benchmark saved original and optimized copies under identical
  headless conditions, with warm-up frames, repeated passes, alternating rounds,
  average, median, P95, throughput estimates, and conservative verdicts.
- **Recover:** restore settings and remove only unchanged BlendBoost-owned
  proxies into another new copy. Later edits or external references cause a
  safe refusal rather than forced deletion.

BlendBoost currently targets **static local scenery in a single view layer**.
It does not simplify animated scenery, characters, simulations, arbitrary
Geometry Nodes, linked assets, or multi-view-layer projects. Benchmarks measure
headless dependency-graph evaluation, not interactive viewport FPS or GPU draw
performance.

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

Version 0.5 adds two actions to the existing tools, with no new dependencies:

- **Static mesh proxy:** cache evaluated geometry from an eligible heavy mesh,
  optionally reducing its triangle count on a generated copy. The original
  modifier stack and mesh remain intact for rendering.
- **Static collection proxy:** group at least 100 eligible small meshes into
  one viewport stand-in, retaining geometry and the separate original objects.

Both actions are opt-in. Proxies are hidden from rendering; their sources are
hidden only in the viewport. Restoration removes only owned, unedited generated
assets. These are working aids for Solid shading, not replacement production
assets. Their appearance, object picking, and culling may differ. File size and
memory usage can increase because the originals remain.
Scan and analysis expose visible mesh/polygon counts and generated-proxy counts
separately from scene totals, so retained render assets are not mistaken for
extra viewport workload. Visibility uses the first saved view layer.

Existing actions still change viewport subdivision levels, an already-enabled
viewport Simplify cap, or explicitly selected object/collection visibility.
No action deletes original scene data or changes render subdivision levels.
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
Copy-Item -LiteralPath (Join-Path $source 'proxies.py') -Destination $target -Force
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
   `plan_id`, options, exclusions, and change IDs. Proxies and temporary hides
   are not selected by default. Use `protected_objects` and
   `protected_collections` to preserve characters or scenery you are editing.
2. Call `apply_optimizations` with that exact `plan_id`, `output_file`, and a
   nonempty `selected_change_ids` list. Hermes asks the human to approve the
   write. Repeat the preview's `options` as top-level tool arguments. Denial
   leaves the source unchanged and creates no copy.
3. To undo, call `preview_optimizations` on the optimized copy, then call
   `restore_optimizations` with its `restore_id` and a different new
   `output_file`. Hermes asks for approval again. The original remains intact.

Example request (replace the paths and protected names with your scene's):

```text
Use BlendBoost to preview working proxies for C:\Projects\shot.blend.
Output: C:\Projects\shot_working.blend
Protect the collection named Characters. Use proxy_ratio 0.5.
Include benchmark_after_apply for frames 108 through 117, passes 3, rounds 2.
Explain the eligible scenery, exclusions, and visual tradeoffs. Wait for my
approval before calling apply_optimizations. Keep all original render assets.
```

`proxy_ratio` ranges from 0.1 to 1.0 and defaults to 0.5. A ratio of 1 caches
full detail. Small-object collection batches always retain full geometry.
Ratio is a triangle target, not a promised polygon count or speed increase.

An optional `benchmark_after_apply` object binds a trial budget to the preview.
The approved apply stages a copy, benchmarks source/copy in alternating order,
and publishes only after the trial completes. Failures and consistent
regressions discard the temporary copy. Inconclusive results may publish a copy
for manual viewport review; the report explicitly labels the uncertainty.
No automatic candidate search or silent quality escalation occurs.

The trial defaults to two rounds of three passes, three warm-up frames, and a
300-second limit **per subprocess**. At most four rounds are allowed; large
budgets can take several minutes. Ordinary apply remains available without a
benchmark. Standalone `benchmark_performance` accepts `rounds` too.

For safety, proxies currently require one view layer, local mesh data, one
scene/collection membership, and static supported dependencies. Animation,
drivers, shape keys, constraints, physics, particles, linked data, instancing,
referenced objects/collections, and unsupported modifier stacks are excluded.
The static modifier allowlist is Decimate, Subdivision Surface, Bevel,
Triangulate, Edge Split, and Weld. Static object-parent chains are checked.
Protected collections include descendants. Unknown protected names are errors.
The preview lists exclusions instead of guessing that geometry is static.

Restore before changing or animating the original proxy source scenery.
Restoration refuses edited or externally referenced proxies rather than
deleting later work. Already optimized files must be restored before another
optimization transaction; version 0.4 restoration records remain supported.

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
rounds: 2
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
can still be affected by background load or cache effects. Improvement/regression
verdicts require at least two rounds agreeing in raw and typical direction,
at least 5% and 0.1 ms average difference per round, and no samples above three
times that round's median. This is a conservative heuristic, not a statistical
significance test. Other results are labeled inconclusive.

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
Proxy fixtures also check exclusion rules, conflicting selections, stale
options, generated-asset cleanup, edited-proxy refusal, alternating benchmark
rounds, and sampled Cycles CPU render equivalence at two frames. Sampled renders
do not establish equivalence for all scenes or frames. Integration tests need
Blender; missing Blender skips them explicitly.
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
| No proxies proposed | Read `proxy_exclusions`. Proxies require static local meshes and one view layer; they are deliberately unavailable for unsupported dependencies. |
| Conflicting actions | Select a proxy or a setting change for the same source. Do not combine proxies with global Simplify or collection-hide actions. |
| Trial discarded | Read the benchmark error or regression report. The source is intact and the staged copy was cleaned up. |
| Restore refused | The generated proxy or source visibility was edited/referenced later. Use the untouched original or review those edits manually; restoration does not force-delete them. |

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
