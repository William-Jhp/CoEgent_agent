"""One MissionPlanSchema for dataset validation, inference and runtime."""

import json
import math

from jsonschema import Draft202012Validator

from .registry import SCHEMAS, SkillRegistry


class PlanError(ValueError):
    pass


def walk(nodes):
    for node in nodes:
        yield node
        kind = node["type"]
        if kind == "sequence":
            yield from walk(node["nodes"])
        elif kind == "condition":
            yield from walk(node["then"] + node["else"])
        elif kind == "fallback":
            yield from walk(node["sequence"])
        elif kind == "retry":
            yield from walk([node["node"]] + ([node["fallback"]] if "fallback" in node else []))


def validate_bounds(plan):
    count = 0

    def bound(value, depth=0):
        nonlocal count
        if depth > 32:
            raise PlanError("plan nesting exceeds limit")
        if isinstance(value, float) and not math.isfinite(value):
            raise PlanError("non-finite number")
        if isinstance(value, dict):
            if value.get("type") == "condition" and isinstance(value.get("condition"), dict):
                op = value["condition"].get("operator")
                if op not in ("==", "!=", ">", ">=", "<", "<=", "in", "not_in"):
                    raise PlanError(f"invalid condition operator {op!r}")
            if "type" in value:
                count += 1
                if count > 128:
                    raise PlanError("plan node count exceeds 128")
            for child in value.values():
                bound(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                bound(child, depth + 1)

    bound(plan)
    try:
        if len(json.dumps(plan, allow_nan=False)) > 100_000:
            raise PlanError("plan exceeds size limit")
    except (ValueError, TypeError, RecursionError) as exc:
        raise PlanError(str(exc)) from exc


def validate_plan(plan, registry=None):
    registry = registry or SkillRegistry()
    validate_bounds(plan)
    errors = list(Draft202012Validator(mission_plan_schema(registry)).iter_errors(plan))
    if errors:
        raise PlanError("; ".join(f"{e.json_path}: {e.message[:300]}" for e in errors[:5]))
    nodes = list(walk(plan["sequence"]))
    aliases = {node.get("save_as") for node in nodes if node["type"] == "skill"}
    reserved = {"robot", "world", "mission", "results", "inspection"}
    for node in nodes:
        if node["type"] == "skill" and node.get("save_as") in reserved:
            raise PlanError("save_as cannot overwrite reserved state namespaces")
        if node["type"] == "condition":
            root = node["condition"]["source"].split(".")[0]
            if root not in {*reserved, *registry.skills, *aliases}:
                raise PlanError(f"unknown state namespace {root!r}; use robot.*, world.*, results.*, inspection.* or save_as")
    direct = {node["type"] for node in plan["sequence"]}
    for terminal in ("abort", "replan_required"):
        if terminal in direct and plan["final_outcome"] != terminal:
            raise PlanError(f"direct {terminal} conflicts with final_outcome")


def mission_plan_schema(registry=None, allowed=None):
    registry = registry or SkillRegistry()
    schema = json.loads((SCHEMAS / "mission_plan.schema.json").read_text())
    schema["$defs"]["skill"] = {"oneOf": [
        {"type": "object", "additionalProperties": False, "required": ["type", "name", "args"],
         "properties": {"type": {"const": "skill"}, "name": {"const": name},
                        "args": registry.args_schema(name),
                        "save_as": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_]{0,63}$"}}}
        for name in registry.skills if allowed is None or name in allowed or name == "stop"]}
    schema["$defs"]["retry"]["properties"]["node"] = {"$ref": "#/$defs/skill"}
    return schema
