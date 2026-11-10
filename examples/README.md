# Fixture-derived JSON examples

The original scan, diagnostic, and single-file benchmark JSON examples were
generated against one cube with a viewport subdivision modifier, plus the
startup camera and light, using Blender 5.2.3 on Windows. File paths are
anonymized; measured counts and timing samples were not invented.

The benchmark example is intentionally a single-file run. Its tiny-scene throughput is not representative of a real project and is not interactive viewport FPS. For a meaningful before/after comparison, benchmark an original scene and its approved optimized copy over the same frame range and settings.

## Working-proxy demo (0.5)

`proxy-preview.json`, `proxy-apply.json`, `proxy-restore.json`, and
`proxy-benchmark.json` come from an actual generated demo run on Windows with
Blender 5.2.3. Only paths were anonymized. Example plan IDs are historical;
always generate a new preview before applying anything.

The demo contains 120 static cubes, one subdivided/triangulated static sphere,
and one animated actor. The completed run grouped 120 cubes (720 polygons)
into one viewport proxy and reduced the sphere proxy from 260,096 to 130,048
polygons. Original meshes and the animated actor were retained. Restoration
completed into a third file. Timing applies only to this synthetic scene and
that machine/session, not to a user's project or interactive viewport FPS.

Create your own fresh demo in PowerShell, from the plugin directory:

```powershell
$blender = 'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe'
$demoDir = Join-Path $env:USERPROFILE ('Desktop\BlendBoostDemo-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $demoDir | Out-Null
$demo = Join-Path $demoDir 'demo.blend'
& $blender --background --factory-startup --disable-autoexec --python-exit-code 1 --python '.\examples\create_demo.py' -- $demo
```

The generator refuses existing destinations and refuses to run on an opened
project. Then ask Hermes:

```text
Use BlendBoost to preview the demo.blend file at <paste the full path>.
Protect "Animated Actor - Protect Me". Show the static collection proxy and
static mesh proxy proposals, exclusions, and quality tradeoffs. Wait for my approval.
```

After reviewing the preview, approve those two proxy actions. Do not also
select subdivision or hide actions for the same sources. Open the new copy in
Blender's Solid viewport and confirm that the actor still moves. Use
`benchmark_performance` with the original/copy, frames 1–10, passes 3, rounds 2.
Finally preview restoration on the optimized copy and approve a new restored
filename. For one combined trial, include `benchmark_after_apply` in the
original preview; no separate benchmarking tool is added.
