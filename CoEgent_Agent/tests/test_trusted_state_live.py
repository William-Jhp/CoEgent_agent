"""Live observations must supersede planner-time and caller safety snapshots."""

import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
from time import monotonic, time

import pytest

from coegent import CoEgent, MissionRequest
from coegent.observations import ObservationSnapshot, leaves
from coegent.scenarios import condition, initial_state, plan, skill
from coegent.skills import MockSkills
from coegent_contracts import RuntimeConfig


class LiveProvider:
    """Controlled sensor simulation, including genuinely per-field stale fixtures."""

    def __init__(self):
        source = initial_state().snapshot()
        self.values = {k: source[k] for k in ("robot", "world")}
        self.stamps = {}
        self.omit_stamps = set()
        self.broken = False
        self.reads = []

    async def snapshot(self):
        if self.broken:
            self.reads.append({"at": monotonic(), "error": "sensor_offline"})
            raise OSError("sensor_offline")
        values = deepcopy(self.values)
        stamps = {p: self.stamps.get(p, monotonic()) for p in leaves(values) if p not in self.omit_stamps}
        self.reads.append({"at": monotonic(), "values": deepcopy(values), "observed_at": deepcopy(stamps)})
        return ObservationSnapshot(values, stamps)


class WaitingPlanner:
    def __init__(self, output):
        self.output = output
        self.started, self.release = asyncio.Event(), asyncio.Event()
        self.context = None

    async def plan(self, context):
        self.context = deepcopy(context)
        self.started.set()
        await self.release.wait()
        return deepcopy(self.output)


class CountingBackend(MockSkills):
    def __init__(self, hold_skill=None):
        super().__init__()
        self.hold_skill = hold_skill
        self.started, self.release = asyncio.Event(), asyncio.Event()
        self.entries = []

    async def execute(self, name, args, snapshot):
        self.entries.append({"name": name, "args": deepcopy(args), "snapshot": deepcopy(snapshot),
                             "at": monotonic()})
        if name == self.hold_skill:
            self.started.set()
            await self.release.wait()
        return await super().execute(name, args, snapshot)


async def ready(planner, provider, backend):
    cfg = RuntimeConfig(state_mode="strict", max_observation_age_s=1, max_inspection_age_s=1,
                        max_replans=0, max_repairs=0, skill_timeout_s=2,
                        planner_timeout_s=3, mission_timeout_s=4)
    agent = CoEgent(planner, backend, state_provider=provider, config=cfg)
    refresh_log = []
    original = agent.fresh.refresh

    async def observe_refresh(state, paths=(), phase="dispatch"):
        entry = {"phase": phase, "paths": list(paths), "started_at": monotonic()}
        try:
            return await original(state, paths, phase)
        except Exception as exc:
            entry["error"] = str(exc)
            raise
        finally:
            entry.update(finished_at=monotonic(), state=state.snapshot())
            refresh_log.append(entry)

    # Forward unchanged to the real refresh; observe phases without disabling any gate.
    agent.fresh.refresh = observe_refresh
    assert await agent.confirm_safe_reset()
    assert agent.admission.fault_reason is None
    backend.entries.clear()
    backend.calls.clear()
    provider.reads.clear()
    refresh_log.clear()
    return agent, refresh_log


def request():
    return MissionRequest("trusted-live", 1, time() + 30, "inspect current safety then navigate to A")


@pytest.fixture
def record(tmp_path):
    directory = Path(os.environ.get("COEGENT_TRUSTED_STATE_OUTPUT", str(tmp_path)))
    directory.mkdir(parents=True, exist_ok=True)

    def save(name, *, agent, planner, provider, backend, refreshes, report, **extra):
        value = {"scope": "CPU sensor/planner/backend simulation; real safety and refresh code",
                 "report": report, "planner_input": planner.context,
                 "planner_output": planner.output, "provider_reads": provider.reads,
                 "refreshes": refreshes, "backend_entries": backend.entries,
                 "navigate_call_count": sum(e["name"] == "navigate_to" for e in backend.entries),
                 "stop_call_count": sum(e["name"] == "stop" for e in backend.entries),
                 "pending_after": len(agent.admission.pending), **extra}
        (directory / f"{name}.json").write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                                          allow_nan=False) + "\n")
    return save


CHANGES = [("battery", "SAFETY_ABORT", "battery_low"),
           ("estop", "SAFETY_ABORT", "estop_active_or_unknown"),
           ("path", "REPLAN_REQUIRED", "path_not_clear"),
           ("localization", "SAFETY_ABORT", "localization_invalid_or_unknown")]


def change(provider, case):
    if case == "battery":
        provider.values["robot"]["battery_percent"] = 5
    elif case == "estop":
        provider.values["robot"]["estop"] = True
    elif case == "path":
        provider.values["world"]["paths"]["A"] = "blocked"
    elif case == "localization":
        provider.values["robot"]["localization_valid"] = False
    else:
        raise AssertionError(case)


def assert_blocked(report, backend, status, reason):
    assert report["execution_status"] == status and report["reason"] == reason
    assert not any(e["name"] == "navigate_to" for e in backend.entries)
    assert [e["name"] for e in backend.entries].count("stop") == 1
    assert report["stop_confirmation"]["confirmed"] is True
    assert report["robot_fault"] is None


@pytest.mark.parametrize("case,status,reason", CHANGES, ids=[v[0] for v in CHANGES])
def test_state_changed_while_planner_waits_blocks_navigation(case, status, reason, record):
    async def exercise():
        provider, backend = LiveProvider(), CountingBackend()
        planner = WaitingPlanner(plan(skill("navigate_to", target="A")))
        agent, refreshes = await ready(planner, provider, backend)
        caller = initial_state()
        before = caller.snapshot()
        task = asyncio.create_task(agent.run_request(request(), caller))
        try:
            await asyncio.wait_for(planner.started.wait(), 1)
            assert planner.context["state"]["robot"]["battery_percent"] == 80
            assert planner.context["state"]["world"]["paths"]["A"] == "clear"
            assert planner.context["state"]["robot"]["estop"] is False
            change(provider, case)
            changed_at = monotonic()
            planner.release.set()
            report = await asyncio.wait_for(task, 1)
            record(f"planner_wait_{case}", agent=agent, planner=planner, provider=provider,
                   backend=backend, refreshes=refreshes, report=report, changed_at=changed_at,
                   caller_before=before, caller_after=caller.snapshot())
            assert_blocked(report, backend, status, reason)
            assert any(r["phase"] == "preflight" and r["started_at"] >= changed_at for r in refreshes)
            if case == "battery":
                assert report["state"]["robot"]["battery_percent"] == 5
                assert any(r["phase"] == "preflight" and r["state"]["robot"]["battery_percent"] == 5
                           for r in refreshes)
            assert caller.snapshot() == before and not agent.admission.pending
        finally:
            planner.release.set()
            await task
    asyncio.run(exercise())


@pytest.mark.parametrize("case,status,reason", CHANGES, ids=[v[0] for v in CHANGES])
def test_dispatch_refresh_blocks_change_after_preflight_passed(case, status, reason, record):
    async def exercise():
        provider, backend = LiveProvider(), CountingBackend("get_pose")
        planner = WaitingPlanner(plan(skill("get_pose"), skill("navigate_to", target="A")))
        agent, refreshes = await ready(planner, provider, backend)
        task = asyncio.create_task(agent.run_request(request(), initial_state()))
        try:
            await asyncio.wait_for(planner.started.wait(), 1)
            planner.release.set()
            await asyncio.wait_for(backend.started.wait(), 1)
            assert backend.entries[0]["name"] == "get_pose"
            assert any(r["phase"] == "preflight" and r["state"]["robot"]["battery_percent"] == 80
                       for r in refreshes)
            change(provider, case)
            changed_at = monotonic()
            backend.release.set()
            report = await asyncio.wait_for(task, 1)
            record(f"after_preflight_{case}", agent=agent, planner=planner, provider=provider,
                   backend=backend, refreshes=refreshes, report=report, changed_at=changed_at)
            assert_blocked(report, backend, status, reason)
            assert report["completed_steps"] == [{"name": "get_pose", "args": {}}]
            gates = [e for e in report["trace"] if e["kind"] == "safety_gate" and e["skill"] == "navigate_to"]
            assert gates and gates[-1]["reason"] == reason and "stage" not in gates[-1]
            assert any(r["phase"] == "dispatch" and "world.paths.A" in r["paths"]
                       and r["started_at"] >= changed_at for r in refreshes)
            if case == "battery":
                assert any(r["phase"] == "dispatch" and r["state"]["robot"]["battery_percent"] == 5
                           for r in refreshes)
            assert [e["name"] for e in backend.entries] == ["get_pose", "stop"]
            assert not agent.admission.pending
        finally:
            planner.release.set()
            backend.release.set()
            await task
    asyncio.run(exercise())


def test_healthy_trusted_state_allows_navigation_and_replaces_caller_values(record):
    async def exercise():
        provider, backend = LiveProvider(), CountingBackend()
        planner = WaitingPlanner(plan(skill("navigate_to", target="A")))
        agent, refreshes = await ready(planner, provider, backend)
        caller = initial_state()
        caller.robot["battery_percent"] = 99
        task = asyncio.create_task(agent.run_request(request(), caller))
        try:
            await asyncio.wait_for(planner.started.wait(), 1)
            planner.release.set()
            report = await asyncio.wait_for(task, 1)
            record("healthy_control", agent=agent, planner=planner, provider=provider,
                   backend=backend, refreshes=refreshes, report=report, caller_battery=99)
            assert report["execution_status"] == "SUCCESS"
            assert planner.context["state"]["robot"]["battery_percent"] == 80
            assert len(backend.entries) == 1 and backend.entries[0]["name"] == "navigate_to"
            assert backend.entries[0]["snapshot"]["robot"]["battery_percent"] == 80
            assert caller.robot["battery_percent"] == 99 and not agent.admission.pending
        finally:
            planner.release.set()
            await task
    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["provider_error", "battery_stamp_missing", "battery_stamp_stale"])
def test_observation_failure_after_planning_does_not_fall_back_to_old_battery(failure, record):
    async def exercise():
        provider, backend = LiveProvider(), CountingBackend()
        planner = WaitingPlanner(plan(skill("navigate_to", target="A")))
        agent, refreshes = await ready(planner, provider, backend)
        task = asyncio.create_task(agent.run_request(request(), initial_state()))
        try:
            await asyncio.wait_for(planner.started.wait(), 1)
            assert planner.context["state"]["robot"]["battery_percent"] == 80
            if failure == "provider_error":
                provider.broken = True
            elif failure == "battery_stamp_missing":
                provider.omit_stamps.add("robot.battery_percent")
            else:
                provider.stamps["robot.battery_percent"] = monotonic() - 10
            planner.release.set()
            report = await asyncio.wait_for(task, 1)
            record(failure, agent=agent, planner=planner, provider=provider,
                   backend=backend, refreshes=refreshes, report=report)
            assert report["execution_status"] == "SAFETY_ABORT"
            assert report["reason"].startswith("observation_unavailable:preflight:") if failure == "provider_error" \
                else report["reason"] == "battery_unknown"
            assert [e["name"] for e in backend.entries] == ["stop"]
            assert report["stop_confirmation"]["confirmed"] is True and not agent.admission.pending
        finally:
            planner.release.set()
            await task
    asyncio.run(exercise())


def test_fresh_battery_does_not_refresh_stale_path_before_navigation(record):
    async def exercise():
        provider, backend = LiveProvider(), CountingBackend("get_pose")
        planner = WaitingPlanner(plan(skill("get_pose"), skill("navigate_to", target="A")))
        agent, refreshes = await ready(planner, provider, backend)
        task = asyncio.create_task(agent.run_request(request(), initial_state()))
        try:
            await asyncio.wait_for(planner.started.wait(), 1)
            planner.release.set()
            await asyncio.wait_for(backend.started.wait(), 1)
            provider.values["robot"]["battery_percent"] = 79
            provider.stamps["world.paths.A"] = monotonic() - 10
            backend.release.set()
            report = await asyncio.wait_for(task, 1)
            record("fresh_battery_stale_path", agent=agent, planner=planner, provider=provider,
                   backend=backend, refreshes=refreshes, report=report)
            assert report["execution_status"] == "SAFETY_ABORT"
            assert report["reason"].startswith("observation_unavailable:dispatch:")
            assert "missing/stale observation:world.paths.A" in report["reason"]
            assert [e["name"] for e in backend.entries] == ["get_pose", "stop"]
            latest = provider.reads[-1]
            assert latest["observed_at"]["robot.battery_percent"] > latest["observed_at"]["world.paths.A"]
            assert not agent.admission.pending
        finally:
            planner.release.set()
            backend.release.set()
            await task
    asyncio.run(exercise())


def test_condition_uses_latest_battery_after_read_only_wait(record):
    async def exercise():
        provider, backend = LiveProvider(), CountingBackend("get_pose")
        planner = WaitingPlanner(plan(skill("get_pose"), condition("robot.battery_percent", 5,
            [skill("log_event", message="low_battery_branch")], [skill("log_event", message="old_state_branch")])))
        agent, refreshes = await ready(planner, provider, backend)
        task = asyncio.create_task(agent.run_request(request(), initial_state()))
        try:
            await asyncio.wait_for(planner.started.wait(), 1)
            planner.release.set()
            await asyncio.wait_for(backend.started.wait(), 1)
            provider.values["robot"]["battery_percent"] = 5
            backend.release.set()
            report = await asyncio.wait_for(task, 1)
            record("condition_latest_battery", agent=agent, planner=planner, provider=provider,
                   backend=backend, refreshes=refreshes, report=report)
            assert report["execution_status"] == "SUCCESS"
            assert backend.calls == [{"name": "get_pose", "args": {}},
                                     {"name": "log_event", "args": {"message": "low_battery_branch"}}]
            assert any(e["kind"] == "condition" and e["branch"] == "then" for e in report["trace"])
            assert any(r["phase"] == "condition" and r["state"]["robot"]["battery_percent"] == 5
                       for r in refreshes)
            assert not agent.admission.pending
        finally:
            planner.release.set()
            backend.release.set()
            await task
    asyncio.run(exercise())
