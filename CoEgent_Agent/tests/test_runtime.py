import asyncio
from copy import deepcopy
import json

import pytest

from coegent.agent import CoEgent
from coegent.planner import ScriptedPlanner
from coegent.registry import SkillRegistry
from coegent.safety import SafetyPolicy
from coegent.scenarios import condition, initial_state, plan, scenarios, skill
from coegent.skills import MockSkills
from coegent.types import Limits, State
from coegent.validation import PlanError, PlanValidator


def execute(plans, state=None, backend=None, limits=None, policy=None):
    planner = ScriptedPlanner(plans)
    backend = backend or MockSkills()
    agent = CoEgent(planner, backend, policy, limits or Limits(max_repairs=0, max_replans=0))
    return asyncio.run(agent.run("test mission", state or initial_state())), backend, planner


@pytest.mark.parametrize("name", list(scenarios()))
def test_scenarios(name):
    case = scenarios()[name]
    report, backend, _ = execute(case["plans"], case["state"], MockSkills(case["outcomes"]), Limits())
    assert report["status"] == case["expected_status"], report["reason"]
    assert [call["name"] for call in backend.calls] == case["expected_calls"]
    state = State.from_dict(report["state"])
    for key, value in case["checks"].items():
        assert state.resolve(key) == value
    assert report["mission_success"] is None


@pytest.mark.parametrize("bad", [
    '{"schema_version":"planner.v1","schema_version":"other"}',
    '{"number":NaN}', '```json\n{}\n```', '{"number":1e1000}',
    plan(skill("hallucinated_tool")), plan(skill("rotate", angle=90)),
    plan(skill("rotate", angle_deg=True)), plan(skill("stop", extra=1)),
    plan({"type": "retry", "node": {"type": "sequence", "nodes": [skill("stop")]},
          "max_attempts": 2, "retry_if": ["failed"]}),
])
def test_invalid_plan_never_dispatches_work(bad):
    report, backend, _ = execute([bad])
    assert report["status"] == "INVALID_PLAN"
    assert [c["name"] for c in backend.calls] == ["stop"]


def test_normalization_only_matches_configured_home():
    validator = PlanValidator(SkillRegistry())
    p, changes = validator.prepare(plan(skill("return_home", target="HOME")), "HOME")
    assert p["sequence"][0]["args"] == {} and len(changes) == 1
    with pytest.raises(PlanError):
        validator.prepare(plan(skill("return_home", target="wrong")), "HOME")


def test_unknown_condition_does_not_take_false_branch():
    p = plan(condition("world.missing", True, [skill("stop")], [skill("navigate_to", target="A")]))
    report, backend, _ = execute([p])
    assert report["status"] == "REPLAN_REQUIRED"
    assert [c["name"] for c in backend.calls] == ["stop"]


def test_authority_cannot_be_overridden_by_plan():
    report, backend, _ = execute([plan(skill("navigate_to", target="A"))],
                                 policy=SafetyPolicy(allowed_skills=frozenset({"get_pose"})))
    assert report["status"] == "SAFETY_ABORT" and report["reason"] == "authority_denied"
    assert [c["name"] for c in backend.calls] == ["stop"]


def test_manipulation_requires_inspection():
    state = initial_state()
    state.robot["location"] = "A"
    report, backend, _ = execute([plan(skill("close_valve", target="red_valve"))], state)
    assert report["reason"] == "target_not_verified"


def test_inspection_invalidated_after_navigation():
    p = plan(skill("navigate_to", target="A"), skill("inspect_target", target="red_valve"),
             skill("return_home"), skill("close_valve", target="red_valve"))
    report, backend, _ = execute([p])
    assert report["status"] == "SAFETY_ABORT"
    assert "close_valve" not in [c["name"] for c in backend.calls]


def test_replan_carries_completed_steps():
    case = scenarios()["bounded_replan"]
    report, _, planner = execute(case["plans"], limits=Limits())
    assert report["replans"] == 1
    assert planner.contexts[1]["completed_steps"][0]["name"] == "log_event"
    assert planner.contexts[1]["state"]["mission"]["replan_constraints"] == ["do_not_repeat_preparation"]


def test_repair_is_bounded_and_only_before_execution():
    report, backend, _ = execute(["invalid", plan(skill("get_pose"))], limits=Limits())
    assert report["status"] == "SUCCESS" and report["repairs"] == 1
    assert [c["name"] for c in backend.calls] == ["get_pose"]
    report, _, _ = execute(["invalid", "invalid", plan(skill("get_pose"))], limits=Limits())
    assert report["status"] == "INVALID_PLAN" and report["planner_calls"] == 2


def test_step_budget_stops_execution():
    report, backend, _ = execute([plan(skill("get_pose"), skill("get_pose"))], limits=Limits(max_steps=1))
    assert report["reason"] == "step_budget_exhausted"
    assert [c["name"] for c in backend.calls] == ["get_pose", "stop"]


def test_skill_timeout_is_never_retried():
    backend = MockSkills(delay_s=0.03)
    p = plan({"type": "retry", "max_attempts": 3, "retry_if": ["skill_timeout_unknown_outcome"], "node": skill("inspect_scene")})
    report, backend, _ = execute([p], backend=backend, limits=Limits(skill_timeout_s=0.005))
    assert report["status"] == "SAFETY_ABORT"
    assert [c["name"] for c in backend.calls].count("inspect_scene") == 1


def test_planner_timeout():
    class SlowPlanner:
        async def plan(self, context):
            await asyncio.sleep(1)
    backend = MockSkills()
    agent = CoEgent(SlowPlanner(), backend, limits=Limits(planner_timeout_s=0.005))
    report = asyncio.run(agent.run("test", initial_state()))
    assert report["status"] == "PLANNER_ERROR"
    assert report["reason"] == "planner_timeout"


def test_state_namespace_cannot_be_shadowed():
    state = initial_state()
    state.observations["robot"] = {"battery_percent": 100}
    assert state.resolve("robot.battery_percent") == 80


def test_abort_return_blocked_is_safety_abort():
    state = initial_state()
    state.world["paths"]["HOME"] = "blocked"
    p = plan({"type": "abort", "reason": "cancelled", "return_home": True}, outcome="abort")
    report, backend, _ = execute([p], state)
    assert report["status"] == "SAFETY_ABORT"
    assert "return_home" not in [c["name"] for c in backend.calls]


def test_caller_state_not_mutated_and_report_serializes():
    state = initial_state()
    before = deepcopy(state.snapshot())
    report, _, _ = execute([plan(skill("navigate_to", target="A"))], state)
    assert state.snapshot() == before
    json.dumps(report, allow_nan=False)


def test_missing_success_result_is_not_execution_success():
    report, backend, _ = execute([plan(skill("navigate_to", target="A"))],
                                 backend=MockSkills({"navigate_to": [{"data": {}}]}))
    assert report["status"] == "SAFETY_ABORT"
    assert report["reason"] == "invalid_driver_result"
    assert report["state"]["robot"]["location"] == "HOME"


def test_unknown_null_condition_requires_replan():
    state = initial_state()
    state.world["reading"] = None
    report, _, _ = execute([plan(condition("world.reading", None, [skill("stop")], [skill("get_pose")]))], state)
    assert report["status"] == "REPLAN_REQUIRED"


def test_current_skill_availability_uses_battery():
    state = initial_state()
    state.robot["battery_percent"] = 8
    _, _, planner = execute([plan(skill("get_pose"))], state)
    context = planner.contexts[0]
    assert "navigate_to" not in context["currently_allowed_skills"]
    assert "return_home" in context["currently_allowed_skills"]


def test_nested_replan_retains_unfinished_outer_suffix():
    p = plan({"type": "sequence", "nodes": [skill("get_pose"),
             {"type": "replan_required", "reason": "changed", "constraints": ["keep_home_goal"]}]},
             skill("return_home"))
    _, _, planner = execute([p, plan(skill("return_home"))], limits=Limits())
    remaining = planner.contexts[1]["remaining_plan"]
    assert remaining[-1]["name"] == "return_home"
    assert not any(n.get("name") == "get_pose" for n in remaining)


@pytest.mark.parametrize("kwargs", [{"max_steps": True}, {"max_replans": -1}, {"skill_timeout_s": float("nan")}])
def test_invalid_limits_rejected(kwargs):
    with pytest.raises(ValueError):
        Limits(**kwargs)


def test_mission_timeout():
    report, backend, _ = execute([plan(skill("inspect_scene"))], backend=MockSkills(delay_s=0.02),
                                 limits=Limits(mission_timeout_s=0.005, skill_timeout_s=0.1))
    assert report["status"] == "SAFETY_ABORT" and report["reason"] == "mission_timeout"
    assert backend.calls[-1]["name"] == "stop"


def test_only_full_response_code_fence_is_normalized():
    validator = PlanValidator(SkillRegistry())
    p = plan(skill("get_pose"))
    raw = "```json\n" + json.dumps(p) + "\n```"
    canonical, changes = validator.prepare(raw)
    assert canonical == p and changes[0]["rule"] == "single_outer_json_code_fence"
    with pytest.raises(PlanError):
        validator.prepare("Here is a plan: " + raw)
    with pytest.raises(PlanError):
        validator.prepare(raw + "\nIgnore safety")


def test_nonrecoverable_failure_never_retries():
    p = plan({"type": "retry", "max_attempts": 3, "retry_if": ["failed"], "node": skill("inspect_scene")})
    report, backend, _ = execute([p], backend=MockSkills({"inspect_scene": [
        {"event": "EXPECTED_FAILURE", "reason": "failed", "recoverable": False}]}))
    assert report["status"] == "REPLAN_REQUIRED"
    assert [c["name"] for c in backend.calls] == ["inspect_scene", "stop"]


def test_low_battery_return_is_not_unconditionally_allowed():
    state = initial_state()
    state.robot["battery_percent"] = 0
    p = plan({"type": "abort", "reason": "low", "return_home": True}, outcome="abort")
    report, backend, _ = execute([p], state)
    assert report["status"] == "SAFETY_ABORT"
    assert "return_home" not in [c["name"] for c in backend.calls]


def test_cancel_stops_and_releases_mission_lock():
    async def run():
        backend = MockSkills(delay_s=0.02)
        agent = CoEgent(ScriptedPlanner([plan(skill("inspect_scene"))]), backend)
        task = asyncio.create_task(agent.run("test", initial_state()))
        await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert backend.calls[-1]["name"] == "stop"
        assert not agent._busy
    asyncio.run(run())


def test_schema_snapshot_matches_training_source():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    local = root / "CoEgent_Agent/coegent/schemas"
    for name in ("mission_plan.schema.json", "skill_registry.json"):
        assert json.loads((local / name).read_text()) == json.loads((root / "Planner_SFT_dataset_v1/schemas" / name).read_text())


def test_finite_state_required():
    with pytest.raises(ValueError):
        State.from_dict({"robot": {"battery_percent": float("nan")}})


def test_driver_cannot_change_policy_fields_in_state():
    report, _, _ = execute([plan(skill("get_battery"), skill("navigate_to", target="A"))],
                          backend=MockSkills({"get_battery": [{"data": {
                              "battery_percent": 8, "min_battery": 0, "authority": "all"}}]}))
    assert report["status"] == "SAFETY_ABORT"
    assert "authority" not in report["state"]["robot"]


def test_unsupported_state_namespace_rejected_before_work():
    p = plan(condition("target.red_valve.state", "open", [skill("stop")], [skill("stop")]))
    report, backend, _ = execute([p])
    assert report["status"] == "INVALID_PLAN"
    assert "unknown state namespace" in report["reason"]
