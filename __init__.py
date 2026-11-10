"""BlendBoost Hermes plugin registration."""

import hashlib
import json

from . import schemas, tools


def register(ctx):
    """Register inspection and approval-gated copy-only optimization tools."""
    ctx.register_tool(
        name="scan_scene",
        toolset="blendboost",
        schema=schemas.SCAN_SCENE,
        handler=tools.scan_scene,
    )
    ctx.register_tool(
        name="analyze_performance",
        toolset="blendboost",
        schema=schemas.ANALYZE_PERFORMANCE,
        handler=tools.analyze_performance,
    )
    for name, schema, handler in (
        ("optimize_scene", schemas.OPTIMIZE_SCENE, tools.optimize_scene),
        ("preview_optimizations", schemas.PREVIEW_OPTIMIZATIONS, tools.preview_optimizations),
        ("apply_optimizations", schemas.APPLY_OPTIMIZATIONS, tools.apply_optimizations),
        ("restore_optimizations", schemas.RESTORE_OPTIMIZATIONS, tools.restore_optimizations),
        ("benchmark_performance", schemas.BENCHMARK_PERFORMANCE, tools.benchmark_performance),
    ):
        ctx.register_tool(name=name, toolset="blendboost", schema=schema, handler=handler)
    ctx.register_hook("pre_tool_call", _require_optimization_approval)


def _require_optimization_approval(tool_name: str, args: dict, task_id: str = "", **kwargs):
    """Escalate every write operation to Hermes' human approval gate."""
    bare_name = tool_name.rsplit(":", 1)[-1].rsplit(".", 1)[-1]
    if bare_name not in {"apply_optimizations", "restore_optimizations"}:
        return None
    if not isinstance(args, dict):
        return {"action": "block", "message": "BlendBoost expected structured arguments."}
    destination = args.get("output_file", "(no output path)")
    plan_id = args.get("plan_id") or args.get("restore_id", "missing-plan")
    rule_id = plan_id if isinstance(plan_id, str) and len(plan_id) == 64 else "invalid-plan"
    selected = args.get("selected_change_ids")
    change_count = len(selected) if isinstance(selected, list) else "restore"
    approval_scope = hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()[:16]
    trial = " Includes the previewed benchmark trial budget." if args.get("benchmark_after_apply") else ""
    return {
        "action": "approve",
        "message": f"BlendBoost will write a NEW .blend copy at {destination} ({change_count} viewport changes).{trial} Approve only after reviewing this plan's preview.",
        "rule_key": f"blendboost:{bare_name}:{rule_id}:{approval_scope}",
    }
