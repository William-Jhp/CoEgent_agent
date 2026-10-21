"""Plan-only APIs must not acquire/execute a robot; execution must not infer."""

import asyncio
from copy import deepcopy
import json
import subprocess
import sys
from time import time

import pytest

import coegent
from coegent import CoEgent, MissionRequest
from coegent.planner import ScriptedPlanner
from coegent.scenarios import condition, initial_state, plan, skill
from coegent.skills import MockSkills
from coegent_contracts import RuntimeConfig


def generator(planner, **config):
    assert hasattr(coegent, "PlanGenerator"), "Missing backend-free PlanGenerator API"
    return coegent.PlanGenerator(planner, config=RuntimeConfig(**config))


def executor(backend, **config):
    assert hasattr(coegent, "PlanExecutor"), "Missing LLM-free PlanExecutor API"
    return coegent.PlanExecutor(backend, config=RuntimeConfig(**config))


def test_generate_returns_plan_without_constructing_robot_runtime(monkeypatch):
    def forbid_runtime(*args, **kwargs):
        raise AssertionError("Planning must not construct a robot runtime")

    monkeypatch.setattr(CoEgent, "__init__", forbid_runtime)
    state = initial_state()
    before = state.snapshot()
    planned = plan(skill("navigate_to", target="A"))
    output = asyncio.run(generator(ScriptedPlanner([planned])).generate("Go to A", state))
    assert output["status"] == "PLAN_READY"
    assert output["execution_status"] == "NOT_EXECUTED"
    assert output["plan"] == planned
    assert output["planner_calls"] == 1
    assert state.snapshot() == before


@pytest.mark.parametrize("bad", [
    '{"schema_version":"planner.v1","schema_version":"other"}',
    plan(skill("rotate", angle=90)),
    plan(skill("unregistered_tool")),
])
def test_invalid_generation_does_not_return_executable_plan(bad):
    output = asyncio.run(generator(ScriptedPlanner([bad]), max_repairs=0).generate("test", initial_state()))
    assert output["status"] == "INVALID_PLAN"
    assert output["plan"] is None
    assert output["execution_status"] == "NOT_EXECUTED"


def test_generator_repairs_invalid_json_with_bounded_model_calls():
    planner = ScriptedPlanner(["{}", plan(skill("stop"))])
    output = asyncio.run(generator(planner, max_repairs=1).generate("Stop", initial_state()))
    assert output["status"] == "PLAN_READY"
    assert output["planner_calls"] == 2
    assert output["repairs"] == 1
    assert planner.contexts[1]["feedback"]["kind"] == "validation_error"
    bad = asyncio.run(generator(ScriptedPlanner(["{}", "{}"]), max_repairs=1).generate("test", initial_state()))
    assert bad["status"] == "INVALID_PLAN"
    assert bad["planner_calls"] == 2


def test_generator_rejects_disallowed_skill_even_after_known_prefix():
    planner = ScriptedPlanner([plan(skill("stop"), skill("rotate", angle_deg=90))])
    output = asyncio.run(generator(planner, allowed_skills=frozenset({"get_pose"})).generate("test", initial_state()))
    assert output["status"] == "SAFETY_ABORT"
    assert output["reason"] == "authority_denied"
    assert output["plan"] is None
    assert output["planner_calls"] == 1
    assert set(planner.contexts[0]["skills"]) == {"get_pose", "stop"}


def test_generator_checks_known_preconditions_without_attempting_stop():
    planner = ScriptedPlanner([plan(skill("rotate", angle_deg=200))])
    output = asyncio.run(generator(planner).generate("test", initial_state()))
    assert output["status"] == "SAFETY_ABORT"
    assert output["plan"] is None
    assert output["execution_status"] == "NOT_EXECUTED"
    assert output["planner_calls"] == 1


def test_future_observation_branch_is_deferred_during_generation():
    p = plan(skill("inspect_scene"), condition("results.inspect_scene.found", True,
             [skill("log_event", message="found")], [skill("stop")]))
    output = asyncio.run(generator(ScriptedPlanner([p])).generate("Inspect", initial_state()))
    assert output["status"] == "PLAN_READY"
    assert any(finding["status"] == "deferred" for finding in output["preflight"])


def test_planner_cannot_mutate_caller_state():
    class MutatingPlanner:
        async def plan(self, context):
            context["state"]["robot"]["battery_percent"] = 0
            return plan(skill("stop"))

    state = initial_state()
    before = state.snapshot()
    output = asyncio.run(generator(MutatingPlanner()).generate("Stop", state))
    assert output["status"] == "PLAN_READY"
    assert state.snapshot() == before


def test_planning_timeout_returns_error_without_execution():
    class SlowPlanner:
        async def plan(self, context):
            await asyncio.sleep(10)

    output = asyncio.run(generator(SlowPlanner(), planner_timeout_s=.02).generate("test", initial_state()))
    assert output["status"] == "PLANNER_ERROR"
    assert output["plan"] is None
    assert output["reason"] == "planner_timeout"


def test_generator_rejects_reuse_while_cancelled_inference_is_cleaning_up():
    async def exercise():
        cancelled, release = asyncio.Event(), asyncio.Event()

        class CleanupPlanner:
            active = 0
            max_active = 0

            async def plan(self, context):
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    cancelled.set()
                    await release.wait()
                finally:
                    self.active -= 1
                return plan(skill("stop"))

        planner = CleanupPlanner()
        gen = generator(planner, planner_timeout_s=.02)
        try:
            result = await gen.generate("first", initial_state())
            assert result["status"] == "PLANNER_ERROR"
            await asyncio.wait_for(cancelled.wait(), .5)
            with pytest.raises(RuntimeError, match="already generating"):
                await gen.generate("overlapping", initial_state())
            assert planner.max_active == 1
        finally:
            release.set()
            for _ in range(20):
                await asyncio.sleep(0)
                if planner.active == 0:
                    break
        assert planner.active == 0
        # Cleanup completion permits reuse; its late result was never executed.
        result = await gen.generate("after cleanup", initial_state())
        assert result["status"] == "PLANNER_ERROR"
        release.set()
        await asyncio.sleep(0)
        assert planner.max_active == 1

    asyncio.run(exercise())


def test_planning_cancellation_propagates_and_generator_can_be_reused():
    async def exercise():
        entered = asyncio.Event()

        class Planner:
            calls = 0

            async def plan(self, context):
                self.calls += 1
                if self.calls == 1:
                    entered.set()
                    await asyncio.sleep(10)
                return plan(skill("stop"))

        gen = generator(Planner())
        task = asyncio.create_task(gen.generate("test", initial_state()))
        await entered.wait()
        with pytest.raises(RuntimeError, match="already generating"):
            await gen.generate("competing", initial_state())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return await gen.generate("Stop", initial_state())

    assert asyncio.run(exercise())["status"] == "PLAN_READY"


def test_generate_then_execute_has_no_implicit_inference_in_executor():
    async def exercise():
        planner = ScriptedPlanner([plan(skill("navigate_to", target="A"))])
        output = await generator(planner).generate("Go to A", initial_state())
        backend = MockSkills()
        report = await executor(backend).execute(output["plan"], initial_state(), command="Go to A")
        assert len(planner.contexts) == 1
        assert backend.calls == [{"name": "navigate_to", "args": {"target": "A"}}]
        return report

    report = asyncio.run(exercise())
    assert report["status"] == "SUCCESS"
    assert report["state"]["robot"]["location"] == "A"
    assert report["planner_calls"] == 0


def test_executor_requires_provided_plan_before_admission():
    backend = MockSkills()
    request = MissionRequest("missing-plan", 1, time()+30, "Go to A")
    with pytest.raises(ValueError, match="provided plan"):
        asyncio.run(executor(backend).execute_request(request, initial_state()))
    assert backend.calls == []


def test_executor_revalidates_handoff_plan():
    backend = MockSkills()
    raw = deepcopy(plan(skill("rotate", angle_deg=90)))
    raw["sequence"][0]["args"]["unexpected"] = True
    report = asyncio.run(executor(backend).execute(raw, initial_state()))
    assert report["status"] == "INVALID_PLAN"
    assert report["planner_calls"] == 0
    assert all(call["name"] == "stop" for call in backend.calls)


def test_executor_checks_execution_state_not_planning_snapshot():
    async def exercise():
        output = await generator(ScriptedPlanner([plan(skill("navigate_to", target="A"))])).generate("Go to A", initial_state())
        changed = initial_state()
        changed.robot["estop"] = True
        backend = MockSkills()
        report = await executor(backend).execute(output["plan"], changed)
        assert all(call["name"] == "stop" for call in backend.calls)
        return report

    assert asyncio.run(exercise())["status"] == "SAFETY_ABORT"


def test_executor_keeps_request_deduplication():
    async def exercise():
        backend = MockSkills()
        exe = executor(backend)
        request = MissionRequest("approved-plan", 1, time()+30, "Go to A", plan(skill("navigate_to", target="A")))
        first = await exe.execute_request(request, initial_state())
        second = await exe.execute_request(request, initial_state())
        assert first["status"] == "SUCCESS"
        assert second["replayed"] is True
        assert backend.calls == [{"name": "navigate_to", "args": {"target": "A"}}]

    asyncio.run(exercise())


def test_executor_returns_replan_required_without_calling_model():
    p = plan(condition("world.missing", True, [skill("stop")], [skill("navigate_to", target="A")]))
    backend = MockSkills()
    report = asyncio.run(executor(backend).execute(p, initial_state()))
    assert report["status"] == "REPLAN_REQUIRED"
    assert report["planner_calls"] == 0
    assert all(call["name"] == "stop" for call in backend.calls)


def test_invalid_nonfinite_output_still_produces_a_json_report():
    raw = plan(skill("rotate", angle_deg=float("nan")))
    output = asyncio.run(generator(ScriptedPlanner([raw]), max_repairs=0).generate("test", initial_state()))
    assert output["status"] == "INVALID_PLAN"
    assert output["plan"] is None
    json.dumps(output, allow_nan=False)


def test_plan_cli_returns_validated_plan_without_execution(tmp_path):
    p = plan(skill("navigate_to", target="A"))
    input_file, output_file = tmp_path/"input.json", tmp_path/"output.json"
    input_file.write_text(json.dumps(p))
    result = subprocess.run([sys.executable, "-m", "coegent", "plan", "--planner", "file",
                             "--plan-file", str(input_file), "--command", "Go to A",
                             "--out", str(output_file)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    report = json.loads(output_file.read_text())
    assert report["status"] == "PLAN_READY"
    assert report["execution_status"] == "NOT_EXECUTED"
    assert report["plan"] == p
    assert report["run_metadata"]["backend"] is None
    assert not any(entry["kind"] == "skill_start" for entry in report["trace"])
