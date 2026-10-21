"""Canonical state and explicit, loss-detecting legacy migration."""

from copy import deepcopy
import json
import operator

from jsonschema import Draft202012Validator, ValidationError

from .registry import SCHEMAS


class StateContract:
    schema = json.loads((SCHEMAS / "state.schema.json").read_text())

    @staticmethod
    def validate(value):
        try:
            json.dumps(value, allow_nan=False)
            Draft202012Validator(StateContract.schema).validate(value)
        except (ValueError, TypeError, RecursionError, ValidationError) as exc:
            raise ValueError("state must be finite JSON with robot/world/mission/observations objects") from exc

    @staticmethod
    def resolve(snapshot, source):
        observations = snapshot.get("observations", {})
        roots = {**observations, "robot": snapshot.get("robot", {}), "world": snapshot.get("world", {}),
                 "mission": snapshot.get("mission", {}), "results": observations}
        value = roots
        for part in source.split("."):
            if not isinstance(value, dict) or part not in value:
                raise KeyError(source)
            value = value[part]
        return value

    @staticmethod
    def condition(snapshot, predicate):
        lhs = StateContract.resolve(snapshot, predicate["source"])
        rhs, op = predicate["value"], predicate["operator"]
        if lhs is None:
            raise ValueError("unknown observation")
        if op in {"in", "not_in"}:
            if not isinstance(rhs, list):
                raise ValueError("membership requires list")
            found = any(type(lhs) is type(item) and lhs == item for item in rhs)
            return found if op == "in" else not found
        numbers = type(lhs) in (int, float) and type(rhs) in (int, float)
        if type(lhs) is not type(rhs) and not numbers:
            raise ValueError("predicate type mismatch")
        if op not in {"==", "!="} and not numbers:
            raise ValueError("ordered comparisons require numbers")
        return {"==": operator.eq, "!=": operator.ne, ">": operator.gt, ">=": operator.ge,
                "<": operator.lt, "<=": operator.le}[op](lhs, rhs)


def migrate_legacy_state(flat, mapping):
    """No guessed targets, safety defaults or ignored legacy fields."""
    if not isinstance(flat, dict):
        raise ValueError("legacy state must be an object")
    if set(flat) - set(mapping):
        raise ValueError(f"unmapped legacy state keys: {sorted(set(flat) - set(mapping))}")
    result = {key: {} for key in ("robot", "world", "mission", "observations")}
    for key, value in flat.items():
        parts = mapping[key].split(".")
        if len(parts) < 2 or parts[0] not in result:
            raise ValueError(f"invalid canonical path: {mapping[key]}")
        target = result
        for part in parts[:-1]:
            target = target.setdefault(part, {})
            if not isinstance(target, dict):
                raise ValueError("conflicting mapped state paths")
        if parts[-1] in target:
            raise ValueError("conflicting mapped state paths")
        target[parts[-1]] = deepcopy(value)
    StateContract.validate(result)
    return result
