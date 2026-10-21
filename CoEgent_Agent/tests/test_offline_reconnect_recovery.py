"""Offline admission/recovery acceptance tests; no network or hardware required."""

import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path

import pytest

from coegent.agent import CoEgent
from coegent.goals import Goal
from coegent.planner import ScriptedPlanner
from coegent.scenarios import initial_state, plan, skill
from coegent.skills import MockSkills
from coegent_contracts import RuntimeConfig
from coegent_contracts.semantics import has_physical_effect
from test_executive_boundaries import Provider, request


class Connection:
    def __init__(self):
        self.online = True
        self.broken = False
        self.checks = 0
        self.offline_seen = asyncio.Event()

    async def connected(self):
        self.checks += 1
        if self.broken:
            raise OSError("connection probe unavailable")
        if not self.online:
            self.offline_seen.set()
        return self.online


class CountingBackend(MockSkills):
    def __init__(self, provider):
        super().__init__()
        self.provider = provider
        self.entries = []
        self.active = self.max_active = 0
        self.physical_active = self.max_physical = 0
        self.block_skill = None
        self.resist_cancel = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancel_seen = asyncio.Event()
        self.finished = asyncio.Event()
        self.after_success = None
        self.fail_stop = False

    async def execute(self, name, args, snapshot):
        from coegent.registry import SkillRegistry
        from coegent.types import Event, SkillResult

        physical = has_physical_effect(SkillRegistry().definition(name))
        self.entries.append({"name": name, "args": deepcopy(args),
                             "location": snapshot["robot"].get("location")})
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.physical_active += int(physical)
        self.max_physical = max(self.max_physical, self.physical_active)
        try:
            if name == self.block_skill:
                self.started.set()
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.cancel_seen.set()
                    if not self.resist_cancel:
                        raise
                    await self.release.wait()
            if name == "stop" and self.fail_stop:
                return SkillResult(name, Event.EXPECTED_FAILURE, "motor_stop_not_acknowledged")
            result = await super().execute(name, args, snapshot)
            if result.ok:
                for key in ("location", "holding", "stopped", "localization_valid"):
                    if key in result.data:
                        self.provider.state.robot[key] = result.data[key]
                if self.after_success is not None:
                    self.after_success(name)
            return result
        finally:
            self.active -= 1
            self.physical_active -= int(physical)
            if name == self.block_skill:
                self.finished.set()

    def count(self, name):
        return sum(entry["name"] == name for entry in self.entries)


class LatePlanner:
    requires_remote = True

    def __init__(self):
        self.started = asyncio.Event()
        self.cancel_seen = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()
        self.contexts = []

    async def plan(self, context):
        self.contexts.append(deepcopy(context))
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancel_seen.set()
            await self.release.wait()
        finally:
            self.finished.set()
        return plan(skill("rotate", angle_deg=99))


async def setup_agent(*, planner=None, policy="hold", local_skills=None, **settings):
    provider, connection = Provider(), Connection()
    backend = CountingBackend(provider)
    values = dict(state_mode="strict", max_observation_age_s=1,
                  max_inspection_age_s=2, remote_required=True, offline_policy=policy,
                  local_skills=local_skills, connection_poll_s=.005,
                  skill_timeout_s=.2, mission_timeout_s=2, planner_timeout_s=1,
                  max_replans=1, max_repairs=0)
    values.update(settings)
    agent = CoEgent(planner, backend, state_provider=provider, connection=connection,
                    config=RuntimeConfig(**values))
    assert await agent.confirm_safe_reset()
    backend.entries.clear()
    backend.calls.clear()
    backend.max_active = backend.max_physical = 0
    return agent, backend, provider, connection


@pytest.fixture
def record(tmp_path):
    root = Path(os.environ.get("COEGENT_OFFLINE_OUTPUT", tmp_path))
    root.mkdir(parents=True, exist_ok=True)

    def save(name, agent, backend, reports, **details):
        value = {"reports": reports, "backend_entries": deepcopy(backend.entries),
                 "max_concurrent_execute": backend.max_active,
                 "max_concurrent_physical": backend.max_physical,
                 "active_backend_calls": backend.active,
                 "pending_admission_tasks": len(agent.admission.pending),
                 "fault": agent.admission.fault_reason, **details}
        (root / f"{name}.json").write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    return save


@pytest.mark.parametrize("mode", ["hold", "continue", "return_home"])
def test_offline_approved_plan_policy(mode, record):
    async def exercise():
        agent, backend, provider, connection = await setup_agent(
            policy=mode, local_skills=frozenset({"rotate"}) if mode == "continue" else None)
        provider.state.robot["location"] = "A"
        connection.online = False
        report = await agent.run_request(request(plan(skill("rotate", angle_deg=30)),
                                                goals=(Goal("at_location", "HOME"),)), initial_state())
        record("policy_" + mode, agent, backend, [report])
        assert report["planner_calls"] == 0
        assert backend.count("rotate") == int(mode == "continue")
        assert backend.count("return_home") == int(mode == "return_home")
        assert report["execution_status"] == ("SUCCESS" if mode == "continue" else "HELD")
        assert report["mission_success"] is False
        if mode != "continue":
            assert report["stop_confirmation"]["confirmed"] is True
        assert backend.max_physical <= 1
        assert not agent.admission.pending and backend.active == 0
    asyncio.run(exercise())


@pytest.mark.parametrize("case", ["unlisted_skill", "remote_planner", "probe_exception"])
def test_offline_continue_does_not_grant_new_authority(case, record):
    async def exercise():
        planner = ScriptedPlanner([plan(skill("rotate", angle_deg=30))])
        agent, backend, _, connection = await setup_agent(
            planner=planner, policy="continue", local_skills=frozenset({"rotate"}))
        connection.online = False
        connection.broken = case == "probe_exception"
        raw = None if case == "remote_planner" else plan(skill("navigate_to", target="A"))
        report = await agent.run_request(request(raw), initial_state())
        record("authority_" + case, agent, backend, [report], planner_contexts=planner.contexts)
        assert report["execution_status"] == "HELD"
        assert report["planner_calls"] == 0 and not planner.contexts
        assert backend.count("navigate_to") == backend.count("rotate") == 0
        assert backend.count("stop") >= 1
        assert agent.admission.fault_reason is None and not agent.admission.pending
    asyncio.run(exercise())


@pytest.mark.parametrize("unsafe", ["blocked_home", "low_battery"])
def test_offline_return_home_still_requires_safety(unsafe, record):
    async def exercise():
        agent, backend, provider, connection = await setup_agent(policy="return_home")
        provider.state.robot["location"] = "A"
        if unsafe == "blocked_home":
            provider.state.world["paths"]["HOME"] = "blocked"
        else:
            provider.state.robot["battery_percent"] = 1
        connection.online = False
        report = await agent.run_request(request(plan(skill("rotate", angle_deg=30)),
                                                goals=(Goal("at_location", "HOME"),)), initial_state())
        record("return_" + unsafe, agent, backend, [report])
        assert backend.count("return_home") == backend.count("rotate") == 0
        assert report["execution_status"] != "SUCCESS" and report["mission_success"] is False
        assert report["offline_return"]["status"] == "failed"
        assert report["stop_confirmation"]["confirmed"] is True
        assert not agent.admission.pending and backend.active == 0
    asyncio.run(exercise())


@pytest.mark.parametrize("mode", ["hold", "continue"])
def test_disconnect_during_active_skill(mode, record):
    async def exercise():
        agent, backend, _, connection = await setup_agent(
            policy=mode, local_skills=frozenset({"rotate"}) if mode == "continue" else None)
        backend.block_skill = "rotate"
        task = asyncio.create_task(agent.run_request(request(plan(skill("rotate", angle_deg=30))), initial_state()))
        try:
            await asyncio.wait_for(backend.started.wait(), 1)
            connection.online = False
            if mode == "continue":
                await asyncio.wait_for(connection.offline_seen.wait(), 1)
                await asyncio.sleep(0)
                assert not task.done() and not backend.cancel_seen.is_set()
                backend.release.set()
            report = await asyncio.wait_for(task, 1)
            record("active_" + mode, agent, backend, [report], cancelled=backend.cancel_seen.is_set())
            assert report["execution_status"] == ("HELD" if mode == "hold" else "SUCCESS")
            assert backend.cancel_seen.is_set() == (mode == "hold")
            assert backend.count("rotate") == 1 and backend.max_physical == 1
            if mode == "hold":
                assert report["remaining_plan"] and report["stop_confirmation"]["confirmed"] is True
            assert not agent.admission.pending and backend.active == 0
        finally:
            backend.release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(exercise())


@pytest.mark.parametrize("fault_case", ["stop_ack_failed", "driver_still_active"])
def test_disconnect_fault_blocks_next_mission_without_backend_entry(fault_case, record):
    async def exercise():
        agent, backend, _, connection = await setup_agent()
        backend.block_skill = "rotate"
        backend.fail_stop = fault_case == "stop_ack_failed"
        backend.resist_cancel = fault_case == "driver_still_active"
        task = asyncio.create_task(agent.run_request(request(plan(skill("rotate", angle_deg=30))), initial_state()))
        try:
            await asyncio.wait_for(backend.started.wait(), 1)
            connection.online = False
            first = await asyncio.wait_for(task, 1)
            connection.online = True
            count = len(backend.entries)
            second = await agent.run_request(request(plan(skill("rotate", angle_deg=60)), identity="m2"), initial_state())
            cleared = await agent.confirm_safe_reset() if backend.resist_cancel else None
            record("disconnect_fault_" + fault_case, agent, backend, [first, second],
                   next_backend_delta=len(backend.entries) - count, clear_while_pending=cleared)
            assert first["execution_status"] == "SAFETY_ABORT" and first["robot_fault"]
            assert second["execution_status"] == "REJECTED" and second["reason"] == "robot_fault_latched"
            assert len(backend.entries) == count
            if backend.resist_cancel:
                assert cleared is False and agent.admission.pending
            else:
                assert first["reason"].startswith("stop_unconfirmed:")
        finally:
            backend.release.set()
            await asyncio.gather(task, *agent.admission.pending, return_exceptions=True)
            assert backend.active == 0 and not agent.admission.pending
            record("disconnect_fault_" + fault_case + "_cleanup", agent, backend, [],
                   released_driver=True, fault_preserved=agent.admission.fault_reason is not None)
    asyncio.run(exercise())


def test_reconnect_replay_and_explicit_revision_do_not_repeat_completed_motion(record):
    async def exercise():
        agent, backend, _, connection = await setup_agent()
        backend.block_skill = "get_pose"
        req = request(plan(skill("rotate", angle_deg=30), skill("get_pose"), skill("navigate_to", target="A")))
        task = asyncio.create_task(agent.run_request(req, initial_state()))
        try:
            await asyncio.wait_for(backend.started.wait(), 1)
            connection.online = False
            first = await asyncio.wait_for(task, 1)
            connection.online = True
            count = len(backend.entries)
            replay = await agent.run_request(req, initial_state())
            replay_delta = len(backend.entries) - count
            assert replay_delta == 0
            backend.block_skill = None
            revised = replace(req, revision=2, plan=plan(skill("navigate_to", target="A")))
            continued = await agent.run_request(revised, initial_state())
            count = len(backend.entries)
            second_replay = await agent.run_request(revised, initial_state())
            record("reconnect_revision", agent, backend, [first, replay, continued, second_replay],
                   first_replay_backend_delta=replay_delta, revision_replay_backend_delta=len(backend.entries) - count)
            assert first["execution_status"] == replay["execution_status"] == "HELD"
            assert replay["replayed"] and second_replay["replayed"]
            assert continued["execution_status"] == "SUCCESS"
            assert backend.count("rotate") == backend.count("navigate_to") == 1
            assert len(backend.entries) == count and backend.max_physical == 1
            assert {"name": "rotate", "args": {"angle_deg": 30}} in second_replay["mission_completed_steps"]
            assert not agent.admission.pending
        finally:
            backend.release.set()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(exercise())


@pytest.mark.parametrize("cause", ["disconnect", "expiry"])
def test_late_planner_result_after_hold_is_never_dispatched(cause, record):
    async def exercise():
        planner = LatePlanner()
        agent, backend, _, connection = await setup_agent(planner=planner)
        task = asyncio.create_task(agent.run_request(request(ttl=.08 if cause == "expiry" else 30), initial_state()))
        try:
            await asyncio.wait_for(planner.started.wait(), 1)
            if cause == "disconnect":
                connection.online = False
            report = await asyncio.wait_for(task, 1)
            await asyncio.wait_for(planner.cancel_seen.wait(), 1)
            count = len(backend.entries)
            connection.online = True
            planner.release.set()
            await asyncio.wait_for(planner.finished.wait(), 1)
            await asyncio.sleep(0)
            record("late_planner_" + cause, agent, backend, [report],
                   backend_delta_after_late_response=len(backend.entries) - count,
                   planner_finished=planner.finished.is_set(), planner_cancelled=planner.cancel_seen.is_set())
            assert report["execution_status"] == "HELD"
            assert report["reason"] == ("request_expired" if cause == "expiry" else "connection_lost:hold")
            assert backend.count("rotate") == 0 and len(backend.entries) == count
            assert not any(e["kind"] == "planner_output" for e in report["trace"])
            assert not agent.admission.pending
        finally:
            planner.release.set()
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.wait_for(planner.finished.wait(), 1)
    asyncio.run(exercise())


def test_preflight_recovery_uses_one_budget_and_approved_goals(record):
    async def exercise():
        planner = ScriptedPlanner([plan(skill("navigate_to", target="B"))])
        agent, backend, provider, _ = await setup_agent(planner=planner)
        provider.state.world["paths"]["A"] = "blocked"
        req = request(plan(skill("navigate_to", target="A")), goals=(Goal("at_location", "B"),))
        report = await agent.run_request(req, initial_state())
        record("preflight_recovery", agent, backend, [report], planner_contexts=planner.contexts)
        assert report["execution_status"] == "SUCCESS" and report["mission_success"] is True
        assert report["replans"] == report["planner_calls"] == 1 and report["repairs"] == 0
        assert planner.contexts[0]["feedback"]["stage"] == "preflight"
        assert planner.contexts[0]["request_goals"] == [{"kind": "at_location", "target": "B", "value": None}]
        assert [e["args"] for e in backend.entries if e["name"] == "navigate_to"] == [{"target": "B"}]
        assert not agent.admission.pending
    asyncio.run(exercise())


@pytest.mark.parametrize("exhausted", [False, True])
def test_execution_recovery_preserves_goals_and_completed_steps(exhausted, record):
    async def exercise():
        replacement = plan(skill("inspect_scene")) if exhausted else plan(
            skill("inspect_target", target="red_valve"), skill("return_home"))
        planner = ScriptedPlanner([replacement])
        agent, backend, _, _ = await setup_agent(planner=planner)
        backend.outcomes["inspect_scene"].extend([
            {"event": "EXPECTED_FAILURE", "reason": "camera_unavailable", "recoverable": True}
            for _ in range(2 if exhausted else 1)])
        goals = (Goal("inspected_target", "red_valve"), Goal("at_location", "HOME"))
        req = request(plan(skill("navigate_to", target="A"), skill("inspect_scene")), goals=goals)
        report = await agent.run_request(req, initial_state())
        record("execution_recovery_" + str(exhausted).lower(), agent, backend, [report], planner_contexts=planner.contexts)
        assert report["replans"] == report["planner_calls"] == 1 and report["repairs"] == 0
        assert planner.contexts[0]["feedback"]["stage"] == "execution"
        assert planner.contexts[0]["completed_steps"] == [{"name": "navigate_to", "args": {"target": "A"}}]
        assert [g["target"] for g in planner.contexts[0]["request_goals"]] == ["red_valve", "HOME"]
        assert backend.count("navigate_to") == 1 and not agent.admission.pending
        if exhausted:
            assert report["execution_status"] == "REPLAN_REQUIRED" and report["mission_success"] is False
            assert backend.count("inspect_scene") == 2
        else:
            assert report["execution_status"] == "SUCCESS" and report["mission_success"] is True
            assert report["goal_status"] == "achieved" and backend.count("return_home") == 1
    asyncio.run(exercise())


def test_safety_abort_is_not_repaired_or_replanned(record):
    async def exercise():
        planner = ScriptedPlanner([plan(skill("navigate_to", target="A"))])
        agent, backend, provider, _ = await setup_agent(planner=planner)
        provider.state.robot["battery_percent"] = 5
        report = await agent.run_request(request(plan(skill("navigate_to", target="A"))), initial_state())
        record("safety_abort_no_bypass", agent, backend, [report], planner_contexts=planner.contexts)
        assert report["execution_status"] == "SAFETY_ABORT"
        assert report["replans"] == report["repairs"] == report["planner_calls"] == 0
        assert backend.count("navigate_to") == 0 and backend.count("stop") == 1
        assert not planner.contexts and not agent.admission.pending
    asyncio.run(exercise())
