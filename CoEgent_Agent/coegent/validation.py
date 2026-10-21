"""Strict plan validation with a small, auditable normalization rule."""

from copy import deepcopy
import json

from jsonschema import Draft202012Validator

from coegent_contracts.plan import PlanError, mission_plan_schema, validate_bounds, validate_plan, walk

from .registry import SkillRegistry

__all__ = ["PlanError", "PlanValidator", "strict_json", "walk"]


def strict_json(raw: str) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise PlanError(f"duplicate key: {key}")
            result[key] = value
        return result

    def reject(value):
        raise PlanError(f"non-finite value: {value}")

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=reject)
    except (ValueError, RecursionError) as exc:
        raise PlanError(str(exc)) from exc


class PlanValidator:
    def __init__(self, registry: SkillRegistry):
        self.registry = registry
        self.schema = mission_plan_schema(registry)
        self.validator = Draft202012Validator(self.schema)

    def preflight(self, plan, state, config, *, fresh=None):
        from .preflight import preflight
        return preflight(plan, state.snapshot(), config,
                         prepare_skill=fresh.project_for_skill if fresh is not None else None)

    def prepare(self, raw, home: str | None = None) -> tuple[dict, list[dict]]:
        normalizations = []
        if isinstance(raw, str):
            if len(raw) > 100_000:
                raise PlanError("plan exceeds size limit")
            stripped = raw.strip()
            lines = stripped.splitlines()
            if len(lines) >= 3 and lines[0] in {"```json", "```"} and lines[-1] == "```":
                raw = "\n".join(lines[1:-1])
                normalizations.append({"rule": "single_outer_json_code_fence", "before": stripped, "after": raw})
            plan = strict_json(raw)
        else:
            try:
                encoded = json.dumps(raw, allow_nan=False)
            except (TypeError, ValueError, RecursionError) as exc:
                raise PlanError("plan is not finite JSON") from exc
            if len(encoded) > 100_000:
                raise PlanError("plan exceeds size limit")
            plan = deepcopy(raw)
        validate_bounds(plan)

        def normalize(value):
            if isinstance(value, dict):
                if value.get("type") == "skill" and value.get("name") == "return_home":
                    if home and value.get("args") == {"target": home}:
                        normalizations.append({"rule": "return_home_matching_configured_home",
                                               "before": deepcopy(value["args"]), "after": {}})
                        value["args"] = {}
                for child in value.values():
                    normalize(child)
            elif isinstance(value, list):
                for child in value:
                    normalize(child)

        normalize(plan)
        validate_plan(plan, self.registry)
        return plan, normalizations
