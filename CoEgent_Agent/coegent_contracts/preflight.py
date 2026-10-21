"""Check a known prefix only; never predict a driver or sensor outcome."""

from coegent_contracts.semantics import check_preconditions
from coegent_contracts.state import StateContract

from .plan import PlanError


class PreflightError(PlanError):
    def __init__(self, name, path, issue):
        self.event, self.reason = issue["event"], issue["reason"]
        self.skill = name
        super().__init__(f"{path}: {name}: {self.reason}")


def preflight(plan, snapshot, config, *, prepare_skill=None):
    findings = []

    def prefix(nodes, path):
        for index, node in enumerate(nodes):
            location = f"{path}[{index}]"
            kind = node["type"]
            if kind == "condition":
                try:
                    selected = StateContract.condition(snapshot, node["condition"])
                except (KeyError, ValueError, TypeError):
                    findings.append({"path": location, "status": "deferred", "reason": "unknown_condition"})
                    return False
                branch = "then" if selected else "else"
                if not prefix(node[branch], location + "." + branch):
                    return False
            elif kind in {"sequence", "fallback"}:
                key = "nodes" if kind == "sequence" else "sequence"
                if not prefix(node[key], location + "." + key):
                    return False
            elif kind in {"skill", "retry"}:
                action = node if kind == "skill" else node["node"]
                if prepare_skill is not None:
                    prepare_skill(snapshot, action["name"], action["args"])
                issue = check_preconditions(action["name"], action["args"], snapshot, config)
                if issue:
                    raise PreflightError(action["name"], location, issue)
                findings.append({"path": location, "status": "deferred", "reason": "await_driver_result"})
                return False
            else:
                # Terminal nodes have their own safety-gated stop/return behavior.
                return False
        return True

    prefix(plan["sequence"], "sequence")
    return findings
