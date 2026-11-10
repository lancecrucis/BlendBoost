"""Read-only headless frame-evaluation benchmark for saved Blender scenes."""

import hashlib
import json
import math
import os
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import tempfile
import time


_MARKER = "BLENDBOOST_BENCHMARK_RESULT:"


def _error(code, message):
    return json.dumps({"ok": False, "error": {"code": code, "message": str(message)}})


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _blend_path(raw, label):
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{label} must be an absolute .blend file path.")
    path = Path(raw).expanduser()
    if not path.is_absolute() or path.suffix.lower() != ".blend" or not path.is_file():
        raise ValueError(f"{label} must name an existing absolute .blend file.")
    return path.resolve(strict=True)


def _integer(value, label, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer from {minimum} to {maximum}.")
    return value


def _validate(args):
    if not isinstance(args, dict):
        raise ValueError("Expected an argument object.")
    baseline = _blend_path(args.get("blend_file"), "blend_file")
    optimized = None
    if args.get("optimized_file") is not None:
        optimized = _blend_path(args["optimized_file"], "optimized_file")
        if optimized == baseline:
            raise ValueError("optimized_file must differ from blend_file.")
    start = _integer(args.get("frame_start"), "frame_start", -100000, 100000)
    end = _integer(args.get("frame_end"), "frame_end", -100000, 100000)
    step = _integer(args.get("frame_step", 1), "frame_step", 1, 10000)
    warmup = _integer(args.get("warmup_frames", 3), "warmup_frames", 1, 100)
    passes = _integer(args.get("passes", 3), "passes", 1, 10)
    rounds = _integer(args.get("rounds", 1), "rounds", 1, 4)
    timeout = _integer(args.get("timeout_seconds", 300), "timeout_seconds", 10, 900)
    if end < start:
        raise ValueError("frame_end must be greater than or equal to frame_start.")
    frames = list(range(start, end + 1, step))
    if len(frames) < 2:
        raise ValueError("Benchmark at least two distinct animation frames.")
    if len(frames) > 1000:
        raise ValueError("Limit a single benchmark to 1000 sampled frames.")

    scene_name = args.get("scene_name")
    view_layer_name = args.get("view_layer_name")
    for label, value in (("scene_name", scene_name), ("view_layer_name", view_layer_name)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"{label} must be a nonempty string.")

    executable = args.get("blender_executable") or os.environ.get("BLENDER_EXECUTABLE") or "blender"
    if not isinstance(executable, str):
        raise ValueError("blender_executable must be a path string.")
    executable = shutil.which(executable)
    if executable is None:
        raise FileNotFoundError("Blender executable was not found. Supply blender_executable or set BLENDER_EXECUTABLE.")

    report_file = args.get("report_file")
    if report_file is not None:
        if not isinstance(report_file, str) or not report_file.strip():
            raise ValueError("report_file must be an absolute .json path.")
        report_file = Path(report_file).expanduser()
        if not report_file.is_absolute() or report_file.suffix.lower() != ".json":
            raise ValueError("report_file must be an absolute .json path.")
        if not report_file.parent.is_dir() or report_file.exists() or report_file.is_symlink():
            raise ValueError("report_file parent must exist and the report path must be new.")
        report_file = report_file.resolve()

    return {"baseline": baseline, "optimized": optimized, "frames": frames,
            "start": start, "end": end, "step": step, "warmup": warmup,
            "passes": passes, "rounds": rounds, "timeout": timeout, "scene_name": scene_name,
            "view_layer_name": view_layer_name, "executable": executable,
            "report_file": report_file}


def _subprocess_benchmark(path, config):
    env = os.environ.copy()
    env["BLENDBOOST_BENCHMARK_REQUEST"] = json.dumps({
        "frames": config["frames"], "warmup": config["warmup"], "passes": config["passes"],
        "scene_name": config["scene_name"], "view_layer_name": config["view_layer_name"],
    }, separators=(",", ":"))
    command = [config["executable"], "--background", "--factory-startup", "--disable-autoexec",
               "--python-exit-code", "1", str(path), "--python", str(Path(__file__).resolve())]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=config["timeout"], check=False, env=env)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": {"code": "blender_timeout",
                                        "message": f"Blender exceeded {config['timeout']} seconds for {path.name}."}}
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


def _summary(samples):
    values = [sample["elapsed_ms"] for sample in samples]
    ordered = sorted(values)
    average = statistics.fmean(values)
    p95 = ordered[math.ceil(0.95 * len(ordered)) - 1]
    median = statistics.median(values)
    return {"sample_count": len(values), "average_evaluation_ms": round(average, 4),
            "median_evaluation_ms": round(median, 4),
            "p95_evaluation_ms": round(p95, 4),
            "p95_method": "nearest_rank",
            "outlier_count_over_3x_median": sum(value > 3 * median for value in values),
            "estimated_headless_evaluation_frames_per_second": round(1000 / average, 4) if average > 0 else None}


def _typical_summary(samples):
    by_frame = {}
    for sample in samples:
        by_frame.setdefault(sample["frame"], []).append(sample["elapsed_ms"])
    frame_medians = [statistics.median(values) for _, values in sorted(by_frame.items())]
    average = statistics.fmean(frame_medians)
    return {"method": "median of repeated samples for each frame, then average across frames",
            "frame_count": len(frame_medians),
            "average_per_frame_median_ms": round(average, 4),
            "estimated_typical_headless_frames_per_second": round(1000 / average, 4) if average > 0 else None}


def _percentage_change(before, after):
    return round((after - before) / before * 100, 2) if before > 0 else None


def _compare(before, after):
    b = before["statistics"]
    a = after["statistics"]
    average_change = _percentage_change(b["average_evaluation_ms"], a["average_evaluation_ms"])
    typical_before = before.get("typical_statistics", {}).get("average_per_frame_median_ms")
    typical_after = after.get("typical_statistics", {}).get("average_per_frame_median_ms")
    typical_change = _percentage_change(typical_before, typical_after) if typical_before is not None else None
    conflict = (average_change is not None and typical_change is not None and
                (average_change < 0) != (typical_change < 0))
    outliers = b.get("outlier_count_over_3x_median", 0) + a.get("outlier_count_over_3x_median", 0)
    return {
        "average_evaluation_time_change_percent": average_change,
        "average_evaluation_time_reduction_percent": -average_change if average_change is not None else None,
        "median_evaluation_time_change_percent": _percentage_change(
            b["median_evaluation_ms"], a["median_evaluation_ms"]),
        "p95_evaluation_time_change_percent": _percentage_change(
            b["p95_evaluation_ms"], a["p95_evaluation_ms"]),
        "estimated_headless_throughput_change_percent": _percentage_change(
            b["estimated_headless_evaluation_frames_per_second"],
            a["estimated_headless_evaluation_frames_per_second"]),
        "typical_average_time_change_percent": typical_change,
        "typical_average_time_reduction_percent": -typical_change if typical_change is not None else None,
        "raw_and_typical_direction_conflict": conflict,
        "outlier_warning": bool(outliers),
        "reliability": "caution" if conflict or outliers else "single-session estimate",
        "interpretation": "lower headless evaluation time" if average_change is not None and average_change < 0
                          else "higher headless evaluation time" if average_change is not None and average_change > 0
                          else "no average-time difference detected",
    }


def _plain_summary(baseline, optimized, comparison):
    before = baseline["statistics"]["average_evaluation_ms"]
    if optimized is None:
        return (f"The saved scene averaged {before:.2f} ms per frame in this headless evaluation test. "
                "This is not interactive viewport FPS.")
    after = optimized["statistics"]["average_evaluation_ms"]
    if comparison.get("verdict") == "inconclusive" or comparison.get("raw_and_typical_direction_conflict"):
        return (f"The original averaged {before:.2f} ms and the optimized copy {after:.2f} ms per frame, "
                "but the repeated-test evidence does not meet the consistency threshold. Treat the comparison as inconclusive; "
                "inspect the per-round results and repeat the test. This is not interactive viewport FPS.")
    reduction = comparison["average_evaluation_time_reduction_percent"]
    if reduction is None:
        change = "The average-time percentage could not be calculated."
    elif reduction >= 0:
        change = f"The optimized copy took {reduction:.2f}% less average evaluation time."
    else:
        change = f"The optimized copy took {-reduction:.2f}% more average evaluation time."
    warning = " One or more timing outliers occurred; repeat the test before making a firm claim." if comparison.get("outlier_warning") else ""
    return (f"The original averaged {before:.2f} ms and the optimized copy averaged {after:.2f} ms "
            f"per frame in the same headless test. {change}{warning} This is not interactive viewport FPS.")


def _trial_verdict(rounds):
    """Conservative practical threshold, not a statistical significance test."""
    if len(rounds) < 2:
        return "inconclusive"
    signs = []
    for before, after in rounds:
        comparison = _compare(before, after)
        reduction = comparison["average_evaluation_time_reduction_percent"]
        typical = comparison["typical_average_time_reduction_percent"]
        difference = before["statistics"]["average_evaluation_ms"] - after["statistics"]["average_evaluation_ms"]
        if (reduction is None or typical is None or comparison["outlier_warning"]
                or abs(difference) < 0.1 or abs(reduction) < 5 or abs(typical) < 5
                or (reduction > 0) != (typical > 0)):
            return "inconclusive"
        signs.append(reduction > 0)
    return "improvement" if all(signs) else "regression" if not any(signs) else "inconclusive"


def _export_new_json(report, path):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json",
                                         prefix=".blendboost-benchmark-", dir=path.parent,
                                         delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(report, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        # Atomic new name; never replace an existing report.
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run(args):
    """Hermes handler. Returns JSON for success and every expected failure."""
    try:
        config = _validate(args)
        source_hashes = {"baseline": _sha256(config["baseline"])}
        if config["optimized"]:
            source_hashes["optimized"] = _sha256(config["optimized"])

        runs = {"baseline": [], "optimized": []}
        run_order = []
        identity = None
        for round_index in range(config["rounds"]):
            order = ["baseline", "optimized"] if config["optimized"] else ["baseline"]
            if round_index % 2:
                order.reverse()
            run_order.append(order)
            for label in order:
                result = _subprocess_benchmark(config[label], config)
                if not result.get("ok"):
                    result["stage"] = label
                    return json.dumps(result)
                current = (result["blender_version"], result["scene"], result["view_layer"])
                if identity is not None and current != identity:
                    return _error("incomparable_scenes", "Blender version, scene, or view layer differed between runs.")
                identity = current
                for sample in result["samples"]:
                    sample["round_index"] = round_index + 1
                result["statistics"] = _summary(result["samples"])
                result["typical_statistics"] = _typical_summary(result["samples"])
                runs[label].append(result)
        baseline = {**runs["baseline"][0], "samples": [s for r in runs["baseline"] for s in r["samples"]]}
        optimized = ({**runs["optimized"][0], "samples": [s for r in runs["optimized"] for s in r["samples"]]}
                     if config["optimized"] else None)

        if _sha256(config["baseline"]) != source_hashes["baseline"] or (
            config["optimized"] and _sha256(config["optimized"]) != source_hashes["optimized"]
        ):
            return _error("input_changed", "A .blend file changed during benchmarking; results were discarded.")

        for label, result in (("baseline", baseline), ("optimized", optimized)):
            if result is not None:
                result["file_sha256"] = source_hashes[label]
                result["statistics"] = _summary(result["samples"])
                result["typical_statistics"] = _typical_summary(result["samples"])

        comparison = _compare(baseline, optimized) if optimized else None
        if comparison is not None:
            pairs = list(zip(runs["baseline"], runs["optimized"]))
            comparison["verdict"] = _trial_verdict(pairs)
            comparison["verdict_method"] = "At least two rounds, each agreeing in raw and typical direction, >=5% and >=0.1ms mean difference, no >3x-median outliers. Heuristic, not statistical significance."
            comparison["per_round"] = [_compare(b, a) for b, a in pairs]
        report = {
            "ok": True,
            "report_version": 1,
            "measurement_type": "headless_frame_evaluation",
            "interactive_viewport_fps_measured": False,
            "conditions": {
                "frame_start": config["start"], "frame_end": config["end"],
                "frame_step": config["step"], "sampled_frames": config["frames"],
                "warmup_frames_per_run": config["warmup"],
                "passes_per_run": config["passes"],
                "rounds": config["rounds"], "run_order": run_order,
                "blender_executable": config["executable"],
                "same_host_and_executable": True,
                "host_system": platform.system(), "cpu_count": os.cpu_count(),
                "method": "time.perf_counter around scene.frame_set, evaluated dependency graph access, and visible mesh polygon readback",
            },
            "baseline": baseline,
            "optimized": optimized,
            "comparison": comparison,
            "plain_language_summary": _plain_summary(baseline, optimized, comparison),
            "limits": [
                "Estimated throughput is 1000 / average headless evaluation milliseconds; it is not interactive viewport FPS.",
                "Python object traversal and polygon readback are included in each timed sample; no display drawing or rendering is measured.",
                "Multiple rounds alternate original/copy order to reduce order bias; cache, thermal, and background-load variation remain possible.",
                "Raw statistics retain timing spikes. Typical statistics use the median of repeated samples per frame; neither is viewport FPS.",
                "Only visible mesh objects in the chosen view layer are read back; generated instances and non-mesh drawing are not counted.",
                "Frame changes occur only in memory; input .blend files are never saved.",
            ],
        }
        if config["report_file"]:
            report["report_file"] = str(config["report_file"])
            _export_new_json(report, config["report_file"])
        return json.dumps(report, ensure_ascii=False)
    except FileNotFoundError as exc:
        return _error("blender_not_found", exc)
    except (ValueError, OSError, KeyError, TypeError, ZeroDivisionError) as exc:
        return _error("benchmark_failed", exc)


def _evaluate_frame(scene, view_layer, frame):
    import bpy

    scene.frame_set(frame)
    depsgraph = bpy.context.evaluated_depsgraph_get()
    visible_count = 0
    polygons = 0
    for obj in scene.objects:
        if obj.type != "MESH" or not obj.visible_get(view_layer=view_layer):
            continue
        visible_count += 1
        evaluated = obj.evaluated_get(depsgraph)
        if evaluated.data and hasattr(evaluated.data, "polygons"):
            polygons += len(evaluated.data.polygons)
    return visible_count, polygons


def _benchmark_in_blender(request):
    import bpy

    scene_name = request.get("scene_name")
    scene = bpy.data.scenes.get(scene_name) if scene_name else bpy.context.scene
    if scene is None:
        raise ValueError(f"Scene not found: {scene_name}")
    layer_name = request.get("view_layer_name")
    view_layer = scene.view_layers.get(layer_name) if layer_name else scene.view_layers[0]
    if view_layer is None:
        raise ValueError(f"View layer not found: {layer_name}")
    frames = request["frames"]
    samples = []
    with bpy.context.temp_override(scene=scene, view_layer=view_layer):
        for index in range(request["warmup"]):
            _evaluate_frame(scene, view_layer, frames[index % len(frames)])
        for pass_index in range(request["passes"]):
            # Ensure the first timed sample is a real frame change, even when
            # warm-up ended on the first frame. Not included in the timing.
            _evaluate_frame(scene, view_layer, frames[-1])
            for frame in frames:
                start = time.perf_counter()
                visible_count, polygons = _evaluate_frame(scene, view_layer, frame)
                elapsed_ms = (time.perf_counter() - start) * 1000
                samples.append({"pass_index": pass_index + 1, "frame": frame,
                                "elapsed_ms": elapsed_ms,
                                "visible_mesh_objects": visible_count,
                                "visible_evaluated_polygons": polygons})
    return {"ok": True, "file": bpy.data.filepath, "scene": scene.name,
            "view_layer": view_layer.name, "blender_version": bpy.app.version_string,
            "samples": samples}


if __name__ == "__main__":
    try:
        payload = _benchmark_in_blender(json.loads(os.environ["BLENDBOOST_BENCHMARK_REQUEST"]))
    except Exception as exc:
        payload = {"ok": False, "error": {"code": "blender_benchmark_failed", "message": str(exc)}}
    print(_MARKER + json.dumps(payload, ensure_ascii=False))
