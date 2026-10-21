"""The single source for skill argument, result and semantic identifiers."""

from copy import deepcopy
from functools import lru_cache
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

SCHEMAS = Path(__file__).parent / "schemas"


def fingerprint(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


@lru_cache(maxsize=1)
def _definitions():
    values = json.loads((SCHEMAS / "skill_contract.json").read_text())
    keys = {"name", "args_schema", "preconditions", "result_schema", "effects"}
    if not isinstance(values, list) or any(set(value) != keys for value in values):
        raise ValueError("SkillContract must contain exactly five fields")
    names = [item["name"] for item in values]
    if len(names) != len(set(names)):
        raise ValueError("duplicate skill name")
    for item in values:
        Draft202012Validator.check_schema(item["args_schema"])
        Draft202012Validator.check_schema(item["result_schema"])
        from .semantics import EFFECTS, PRECONDITIONS
        if set(item["preconditions"]) - PRECONDITIONS.keys() or set(item["effects"]) - EFFECTS:
            raise ValueError(f"unsupported semantic identifier in {item['name']}")
    return {item["name"]: item for item in values}


class SkillRegistry:
    def __init__(self):
        self._definitions = _definitions()
        types = {"string": "str", "number": "number", "boolean": "bool"}
        self.skills = {name: {key: types[prop["type"]] for key, prop in item["args_schema"]["properties"].items()}
                       for name, item in self._definitions.items()}
        self.fingerprint = fingerprint({"version": "coegent.contract.v1", "skills": self._definitions,
                                        "state": json.loads((SCHEMAS / "state.schema.json").read_text()),
                                        "plan": json.loads((SCHEMAS / "mission_plan.schema.json").read_text())})

    def definition(self, name):
        return deepcopy(self._definitions[name])

    def args_schema(self, name):
        return deepcopy(self._definitions[name]["args_schema"])

    def contracts(self):
        return {name: self.args_schema(name) for name in self.skills}
