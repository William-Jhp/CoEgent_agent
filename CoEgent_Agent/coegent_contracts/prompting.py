"""The same schema and skill semantics for new dataset examples and inference."""

import json

from . import CONTRACT_VERSION
from .plan import mission_plan_schema
from .registry import SkillRegistry
from .semantics import PRECONDITIONS
from .state import StateContract


def _shape(schema):
    if "const" in schema:
        return json.dumps(schema["const"])
    if "enum" in schema:
        return "|".join(str(x) for x in schema["enum"])
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]
    if schema.get("type") == "array":
        return "[" + _shape(schema["items"]) + "]"
    if schema.get("type") == "object":
        required = schema.get("required", [])
        return "{" + ",".join(key + ("" if key in required else "?") + ":" + _shape(value)
                               for key, value in schema.get("properties", {}).items()) + "}"
    kind = schema.get("type", "JSON")
    if "minimum" in schema or "maximum" in schema:
        kind += f"({schema.get('minimum', '-inf')}..{schema.get('maximum', 'inf')})"
    return kind


def planner_messages(command, state, registry=None, runtime_context=None, allowed=None):
    registry = registry or SkillRegistry()
    StateContract.validate(state)
    schema = mission_plan_schema(registry)
    definitions = [registry.definition(name) for name in registry.skills
                   if allowed is None or name in allowed or name == "stop"]
    system = (
        "You are a robot mission planner, not an executor. Return only one planner.v1 JSON object. "
        "No markdown. Never claim execution success. Use registered skills and exact argument keys. "
        "State roots: robot, world, mission, results (observations). "
        "inspection is the latest inspect_target observation; motion invalidates it. "
        "Unknown state requires sensing or replan_required, never assume false or success. "
        "A retry wraps one skill; never replay completed side effects. Abort is terminal; return-home is safety-gated.\n"
        "CONTRACT_VERSION: " + CONTRACT_VERSION + "\n"
        "SCHEMA (rendered from MissionPlanSchema; no extra keys): " + _shape(schema) + "\n"
        + "\n".join(key + ": " + _shape(value) for key, value in schema["$defs"].items()
                      if key not in {"node", "skill"}) + "\n"
        "skill: {type:skill,name:registered_name,args:exact_args,save_as?:string}.\n"
        "SKILL_CONTRACTS:\n" + "\n".join(
            item["name"] + " args=" + _shape(item["args_schema"]) + " result=" + _shape(item["result_schema"])
            + " preconditions=" + ",".join(item["preconditions"]) + " effects=" + ",".join(item["effects"])
            for item in definitions) + "\n"
        "PRECONDITIONS: " + json.dumps(PRECONDITIONS, ensure_ascii=False, separators=(",", ":")))
    user = "COMMAND: " + command + "\nSTATE: " + json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if runtime_context:
        user += "\nRUNTIME_CONTEXT: " + json.dumps(runtime_context, ensure_ascii=False, separators=(",", ":"))
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
