"""Hand-authored runtime fixtures, separate from SFT and locked holdout data."""

from copy import deepcopy

from .types import State


def initial_state():
    return State.from_dict({
        "robot": {"battery_percent": 80, "location": "HOME", "home": "HOME",
                  "localization_valid": True, "arm_ready": True, "estop": False,
                  "holding": None, "stopped": True},
        "world": {"paths": {"A": "clear", "B": "clear", "HOME": "clear"}, "obstacle": False,
                  "targets": {"red_valve": {"exists": True, "location": "A", "valve_state": "open"}}},
    })


def skill(name, **args):
    return {"type": "skill", "name": name, "args": args}


def plan(*nodes, outcome="execute", goal="Inspect valve and return"):
    return {"schema_version": "planner.v1", "goal": goal, "sequence": list(nodes),
            "success_criteria": ["Requested work verified by executor observations"], "final_outcome": outcome}


def condition(source, value, yes, no):
    return {"type": "condition", "condition": {"source": source, "operator": "==", "value": value},
            "then": yes, "else": no}


def valve_plan():
    return plan(skill("navigate_to", target="A"), skill("inspect_target", target="red_valve"),
                condition("inspection.target_exists", True,
                          [condition("inspection.valve_state", "open", [skill("close_valve", target="red_valve")],
                                     [skill("log_event", message="Already closed")])],
                          [{"type": "abort", "reason": "target_absent", "return_home": True}]),
                skill("return_home"))


def scenarios():
    cases = {}

    def add(name, plans, status, calls, state=None, outcomes=None, checks=None):
        cases[name] = {"command": "A에서 빨간 밸브를 확인하고 열려 있으면 닫고 복귀해.",
                       "state": state or initial_state(), "plans": plans, "outcomes": outcomes or {},
                       "expected_status": status, "expected_calls": calls, "checks": checks or {}}

    add("valve_open", [valve_plan()], "SUCCESS", ["navigate_to", "inspect_target", "close_valve", "return_home"],
        checks={"robot.location": "HOME", "world.targets.red_valve.valve_state": "closed"})
    state = initial_state()
    state.world["targets"]["red_valve"]["valve_state"] = "closed"
    add("valve_closed", [valve_plan()], "SUCCESS", ["navigate_to", "inspect_target", "log_event", "return_home"], state)
    state = initial_state()
    state.robot["location"], state.robot["battery_percent"] = "A", 8
    add("low_battery_abort", [plan({"type": "abort", "reason": "battery_low", "return_home": True}, outcome="abort")],
        "ABORTED", ["stop", "return_home", "stop"], state, checks={"robot.location": "HOME"})
    add("unsafe_low_battery", [valve_plan()], "SAFETY_ABORT", ["stop"], deepcopy(state))
    state = initial_state()
    state.world["targets"]["red_valve"]["exists"] = False
    add("target_absent", [valve_plan()], "ABORTED", ["navigate_to", "inspect_target", "stop", "return_home", "stop"], state)
    retry = {"type": "retry", "max_attempts": 2, "retry_if": ["temporary_unavailable"],
             "node": skill("inspect_scene"),
             "fallback": {"type": "fallback", "strategy": "request_assistance",
                          "sequence": [skill("request_assistance", reason="inspection_failed"),
                                       {"type": "abort", "reason": "inspection_failed", "return_home": False}]}}
    failure = {"event": "EXPECTED_FAILURE", "reason": "temporary_unavailable", "recoverable": True}
    add("retry_success", [plan(retry)], "SUCCESS", ["inspect_scene", "inspect_scene"], outcomes={"inspect_scene": [failure]})
    add("retry_exhausted", [plan(retry)], "ABORTED", ["inspect_scene", "inspect_scene", "request_assistance", "stop", "stop"],
        outcomes={"inspect_scene": [failure, failure]})
    add("retry_safety_stop", [plan(retry)], "SAFETY_ABORT", ["inspect_scene", "stop"],
        outcomes={"inspect_scene": [{"event": "SAFETY_ABORT", "reason": "human_detected", "recoverable": True}]})
    state = initial_state()
    state.world["paths"]["A"] = "blocked"
    reroute = plan(condition("world.paths.A", "clear", [skill("navigate_to", target="A")],
                            [{"type": "fallback", "strategy": "safe_wait_at_B",
                              "sequence": [skill("navigate_to", target="B"), skill("request_assistance", reason="A_blocked")]}]))
    add("blocked_fallback", [reroute], "SUCCESS", ["navigate_to", "request_assistance"], state,
        checks={"robot.location": "B"})
    first = plan(skill("log_event", message="preparation_complete"),
                 {"type": "replan_required", "reason": "destination_changed", "constraints": ["do_not_repeat_preparation"]},
                 outcome="replan_required")
    add("bounded_replan", [first, plan(skill("return_home"))], "SUCCESS", ["log_event", "stop", "return_home"])
    add("replan_exhausted", [first, first], "REPLAN_REQUIRED", ["log_event", "stop", "log_event", "stop"])
    add("live_battery_drop", [plan(skill("get_battery"), skill("navigate_to", target="A"))], "SAFETY_ABORT", ["get_battery", "stop"],
        outcomes={"get_battery": [{"data": {"battery_percent": 8}}]})
    return cases
