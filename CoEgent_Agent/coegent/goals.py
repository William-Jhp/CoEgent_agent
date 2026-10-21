"""Approved, observable goals; never model-authored success prose."""

from dataclasses import dataclass

from coegent_contracts.state import StateContract


@dataclass(frozen=True)
class Goal:
    kind: str
    target: str
    value: str | None = None

    def __post_init__(self):
        if self.kind not in {"at_location", "inspected_target", "holding_object", "valve_state"}:
            raise ValueError("unsupported goal type")
        if not isinstance(self.target, str) or not self.target.strip() or "." in self.target:
            raise ValueError("goal target must be a nonempty state identifier")
        if ((self.kind == "valve_state" and self.value not in {"open", "closed"})
                or (self.kind != "valve_state" and self.value is not None)):
            raise ValueError("invalid goal value")


@dataclass(frozen=True)
class Evidence:
    skill: str
    args: dict
    data: dict
    completed_at: float


def goal_paths(goals):
    return tuple({{"at_location": "robot.location", "holding_object": "robot.holding",
                   "valve_state": f"world.targets.{g.target}.valve_state"}[g.kind]
                  for g in goals if g.kind != "inspected_target"})


def verify_goals(goals, state, evidence, *, state_known=True, inspection_history_complete=False):
    """Only trusted complete execution history proves an inspection was omitted."""
    results = []
    for goal in goals:
        status = "unknown"
        if goal.kind == "inspected_target":
            observations = [e.data.get("target_exists") for e in evidence
                            if e.skill == "inspect_target" and e.args.get("target") == goal.target]
            status = ("achieved" if True in observations else "unmet" if False in observations else "unknown")
            if not observations and inspection_history_complete is True:
                status = "unmet"
        elif state_known:
            path = {"at_location": "robot.location", "holding_object": "robot.holding",
                    "valve_state": f"world.targets.{goal.target}.valve_state"}[goal.kind]
            try:
                actual = StateContract.resolve(state, path)
                expected = goal.value if goal.kind == "valve_state" else goal.target
                # None is a confirmed empty gripper, not an unknown value.
                if actual is not None or goal.kind == "holding_object":
                    status = "achieved" if actual == expected else "unmet"
            except KeyError:
                pass
        results.append({"kind": goal.kind, "target": goal.target, "status": status})
    statuses = {r["status"] for r in results}
    status = ("not_evaluated" if not results else "unmet" if "unmet" in statuses
              else "unknown" if "unknown" in statuses else "achieved")
    return {"goal_status": status, "goal_results": results}


def mission_success(execution_status, goal_status):
    if goal_status == "not_evaluated":
        return None
    if execution_status != "SUCCESS" or goal_status == "unmet":
        return False
    return True if goal_status == "achieved" else None
