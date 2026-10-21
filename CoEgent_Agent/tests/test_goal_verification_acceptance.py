"""Execution completion and approved goal fulfillment are separate outcomes."""

import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
from time import time

import pytest

from coegent import CoEgent, MissionRequest
from coegent.goals import Goal
from coegent.scenarios import initial_state, plan, skill
from coegent.skills import MockSkills
from coegent_contracts import RuntimeConfig

from test_executive_boundaries import Provider


@pytest.fixture
def record(tmp_path):
    root = Path(os.environ.get("COEGENT_GOAL_OUTPUT", str(tmp_path)))
    root.mkdir(parents=True, exist_ok=True)

    def save(name, **data):
        (root / f"{name}.json").write_text(json.dumps(
            {"scope": "strict CPU sensor/backend mock; actual agent goal finalization", **data},
            indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    return save


async def exercise(case, goals, record, *, inspect=False, lose_final_observation=False):
    class Sensor(Provider):
        def __init__(self):
            super().__init__(initial_state())
            self.reads = []

        async def snapshot(self):
            self.reads.append({"broken": self.broken, "location": self.state.robot["location"]})
            return await super().snapshot()

    provider = Sensor()

    class Backend(MockSkills):
        def __init__(self):
            super().__init__()
            self.entries = []

        async def execute(self, name, args, snapshot):
            self.entries.append({"name": name, "args": deepcopy(args), "snapshot": deepcopy(snapshot)})
            result = await super().execute(name, args, snapshot)
            if result.ok and name in {"navigate_to", "return_home"}:
                provider.state.robot["location"] = result.data["location"]
            if result.ok and name == "return_home" and lose_final_observation:
                provider.broken = True
            return result

    backend = Backend()
    config = RuntimeConfig(state_mode="strict", max_observation_age_s=1,
                           max_inspection_age_s=2, max_replans=0, max_repairs=0)
    agent = CoEgent(None, backend, config=config, state_provider=provider)
    assert await agent.confirm_safe_reset()
    backend.entries.clear()
    provider.reads.clear()
    nodes = [skill("navigate_to", target="A")]
    if inspect:
        nodes.append(skill("inspect_target", target="red_valve"))
    nodes.append(skill("return_home"))
    request = MissionRequest(case, 1, time() + 30, "Visit A, inspect red_valve and return HOME",
                             plan(*nodes), goals=goals)
    report = await agent.run_request(request, initial_state())
    record(case, report=report, backend_entries=backend.entries, provider_reads=provider.reads,
           pending_after=len(agent.admission.pending))
    assert report["execution_status"] == "SUCCESS"
    assert report["planner_calls"] == 0 and report["robot_fault"] is None
    assert not agent.admission.pending
    expected_calls = ["navigate_to", "inspect_target", "return_home"] if inspect else ["navigate_to", "return_home"]
    assert [e["name"] for e in backend.entries] == expected_calls
    return report, provider


def test_successful_execution_without_required_inspection_is_unmet(record):
    async def run():
        report, _ = await exercise("omitted_inspection", (
            Goal("inspected_target", "red_valve"), Goal("at_location", "HOME")), record)
        assert report["state"]["robot"]["location"] == "HOME"
        assert not any(e["skill"] == "inspect_target" for e in report["evidence"])
        assert report["goal_status"] == "unmet"
        assert report["mission_success"] is False
    asyncio.run(run())


def test_successful_execution_with_missing_final_observation_is_unknown(record):
    async def run():
        report, provider = await exercise("final_observation_unavailable", (
            Goal("at_location", "HOME"),), record, lose_final_observation=True)
        assert provider.reads[-1]["broken"] is True
        assert report["state"]["robot"]["location"] == "HOME"
        assert report["goal_status"] == "unknown"
        assert report["mission_success"] is None
    asyncio.run(run())


def test_successful_execution_without_goals_is_not_evaluated(record):
    async def run():
        report, _ = await exercise("no_goals", (), record)
        assert report["goal_status"] == "not_evaluated" and report["goal_results"] == []
        assert report["mission_success"] is None
    asyncio.run(run())


def test_successful_execution_with_inspection_and_home_is_achieved(record):
    async def run():
        report, _ = await exercise("all_goals_achieved", (
            Goal("inspected_target", "red_valve"), Goal("at_location", "HOME")), record, inspect=True)
        assert "inspection" not in report["state"]["observations"]
        assert any(e["skill"] == "inspect_target" and e["data"]["target_exists"] is True
                   for e in report["evidence"])
        assert report["goal_status"] == "achieved"
        assert report["mission_success"] is True
    asyncio.run(run())


def test_interrupted_execution_does_not_claim_complete_inspection_history(record):
    async def run():
        provider, backend = Provider(initial_state()), MockSkills()
        config = RuntimeConfig(state_mode="strict", max_observation_age_s=1, max_inspection_age_s=2)
        agent = CoEgent(None, backend, config=config, state_provider=provider)
        assert await agent.confirm_safe_reset()
        backend.calls.clear()
        request = MissionRequest(
            "interrupted_before_inspection", 1, time() + 30, "Inspect red_valve",
            plan({"type": "abort", "reason": "operator_hold", "return_home": False}, outcome="abort"),
            goals=(Goal("inspected_target", "red_valve"),))
        report = await agent.run_request(request, initial_state())
        record("interrupted_before_inspection", report=report, backend_calls=backend.calls,
               pending_after=len(agent.admission.pending))
        assert report["execution_status"] == "ABORTED"
        assert report["inspection_history_complete"] is False
        assert report["goal_status"] == "unknown" and report["mission_success"] is False
        assert all(c["name"] == "stop" for c in backend.calls)
        assert not agent.admission.pending
    asyncio.run(run())


def test_inspection_history_survives_final_location_observation_failure(record):
    async def run():
        report, _ = await exercise("inspection_done_final_location_unknown", (
            Goal("inspected_target", "red_valve"), Goal("at_location", "HOME")), record,
            inspect=True, lose_final_observation=True)
        assert report["inspection_history_complete"] is True
        assert [g["status"] for g in report["goal_results"]] == ["achieved", "unknown"]
        assert report["goal_status"] == "unknown" and report["mission_success"] is None
    asyncio.run(run())


def test_continuation_without_prior_evidence_does_not_claim_inspection_omission(record):
    async def run():
        provider = Provider(initial_state())

        class Backend(MockSkills):
            async def execute(self, name, args, snapshot):
                result = await super().execute(name, args, snapshot)
                if result.ok and name in {"navigate_to", "return_home"}:
                    provider.state.robot["location"] = result.data["location"]
                return result

        backend = Backend()
        config = RuntimeConfig(state_mode="strict", max_observation_age_s=1,
                               max_inspection_age_s=2, max_replans=0)
        agent = CoEgent(None, backend, config=config, state_provider=provider)
        assert await agent.confirm_safe_reset()
        backend.calls.clear()
        goals = (Goal("inspected_target", "red_valve"), Goal("at_location", "HOME"))
        first = await agent.run_request(MissionRequest(
            "inspection-continuation", 1, time() + 30, "Inspect and return HOME",
            plan(skill("navigate_to", target="A"), skill("inspect_target", target="red_valve"),
                 {"type": "replan_required", "reason": "wait_for_revision", "constraints": ["preserve_completed"]},
                 outcome="replan_required"), goals=goals), initial_state())
        previous_calls = len(backend.calls)
        second = await agent.run_request(MissionRequest(
            "inspection-continuation", 2, time() + 30, "Finish return HOME",
            plan(skill("return_home")), goals=goals), initial_state())
        record("inspection_continuation", first_report=first, report=second,
               backend_calls=backend.calls, second_revision_calls=backend.calls[previous_calls:],
               pending_after=len(agent.admission.pending))
        assert first["execution_status"] == "REPLAN_REQUIRED"
        assert first["goal_results"][0]["status"] == "achieved"
        assert second["execution_status"] == "SUCCESS"
        assert second["inspection_history_complete"] is False
        assert [g["status"] for g in second["goal_results"]] == ["unknown", "achieved"]
        assert second["goal_status"] == "unknown" and second["mission_success"] is None
        assert [c["name"] for c in backend.calls[previous_calls:]] == ["return_home"]
        assert not agent.admission.pending
    asyncio.run(run())
