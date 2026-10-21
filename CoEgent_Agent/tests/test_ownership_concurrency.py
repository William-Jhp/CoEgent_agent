"""Observe real admission ownership and unsynchronized hardware-call overlap."""

import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
from time import monotonic, time

import pytest

from coegent import CoEgent, MissionRequest, RobotAdmission
from coegent.observations import ObservationSnapshot, leaves
from coegent.scenarios import initial_state, plan, skill
from coegent.skills import MockSkills
from coegent_contracts import RuntimeConfig, SkillRegistry
from coegent_contracts.semantics import has_physical_effect


class Counter:
    """Instrumentation only: no execution lock, queue, or rejection in the fake."""

    def __init__(self):
        self.current_execute = 0
        self.max_concurrent_execute = 0
        self.current_side_effect_calls = 0
        self.max_concurrent_side_effect_calls = 0
        self.max_active_missions = 0
        self.events = []
        self.hold = False
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.owner_count = lambda: 0

    def enter(self, label, name, args, physical):
        self.started.set()
        self.current_execute += 1
        self.current_side_effect_calls += int(physical)
        self.max_concurrent_execute = max(self.max_concurrent_execute, self.current_execute)
        self.max_concurrent_side_effect_calls = max(self.max_concurrent_side_effect_calls,
                                                   self.current_side_effect_calls)
        owners = self.owner_count()
        self.max_active_missions = max(self.max_active_missions, owners)
        self.events.append({"kind": "enter", "at_monotonic": monotonic(), "adapter": label,
                            "skill": name, "args": deepcopy(args), "physical": physical,
                            "active_missions": owners, "concurrent_execute": self.current_execute,
                            "concurrent_side_effect_calls": self.current_side_effect_calls})

    def exit(self, label, name, physical):
        self.current_execute -= 1
        self.current_side_effect_calls -= int(physical)
        self.events.append({"kind": "exit", "at_monotonic": monotonic(), "adapter": label,
                            "skill": name, "concurrent_execute": self.current_execute,
                            "concurrent_side_effect_calls": self.current_side_effect_calls})

    def snapshot(self):
        return {"current_execute": self.current_execute, "max_concurrent_execute": self.max_concurrent_execute,
                "current_side_effect_calls": self.current_side_effect_calls,
                "max_concurrent_side_effect_calls": self.max_concurrent_side_effect_calls,
                "max_active_missions": self.max_active_missions, "events": deepcopy(self.events)}


class CountingBackend(MockSkills):
    def __init__(self, counter, label="shared"):
        super().__init__()
        self.counter, self.label = counter, label
        self.started = asyncio.Event()

    async def execute(self, name, args, snapshot):
        physical = has_physical_effect(SkillRegistry().definition(name))
        self.counter.enter(self.label, name, args, physical)
        try:
            self.started.set()
            if physical and self.counter.hold:
                await self.counter.release.wait()
            return await super().execute(name, args, snapshot)
        finally:
            self.counter.exit(self.label, name, physical)


class Sensor:
    async def snapshot(self):
        values = {k: v for k, v in initial_state().snapshot().items() if k in {"robot", "world"}}
        return ObservationSnapshot(values, {path: monotonic() for path in leaves(values)})


def config(**kwargs):
    return RuntimeConfig(max_replans=0, max_repairs=0, skill_timeout_s=2,
                         mission_timeout_s=3, **kwargs)


def request(identity, angle, *, destination=None, name="rotate"):
    args = {"angle_deg": angle} if name == "rotate" else {"target": "B"} if name == "navigate_to" else {}
    nodes = [skill(name, **args)]
    if destination is not None:
        nodes.append(skill("navigate_to", target=destination))
    return MissionRequest(identity, 1, time() + 10, "approved ownership probe", plan(*nodes))


@pytest.fixture
def record(tmp_path):
    directory = Path(os.environ.get("COEGENT_OWNERSHIP_OUTPUT", str(tmp_path)))
    directory.mkdir(parents=True, exist_ok=True)

    def save(name, **value):
        (directory / f"{name}.json").write_text(json.dumps(
            {"scope": "CPU mock concurrency, not physical hardware proof", **value},
            ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    return save


async def race(agents, requests, counter, launch_order, save):
    gate, arrived = asyncio.Event(), asyncio.Event()
    waiting = []

    async def submit(role):
        waiting.append(role)
        if len(waiting) == 2:
            arrived.set()
        await gate.wait()
        return await agents[role].run_request(requests[role], initial_state())

    counter.owner_count = lambda: sum(agents[r].admission.owns(requests[r]) for r in ("A", "B"))
    counter.hold = True
    counter.started.clear()
    tasks = {r: asyncio.create_task(submit(r)) for r in launch_order}
    try:
        await asyncio.wait_for(arrived.wait(), timeout=1)
        gate.set()
        await asyncio.wait_for(counter.started.wait(), timeout=1)
        done, _ = await asyncio.wait(set(tasks.values()), timeout=.5, return_when=asyncio.FIRST_COMPLETED)
        during = {"active_missions": counter.owner_count(), "completed_submitters": len(done),
                  "counter": counter.snapshot()}
        save("during", during=during)
        assert len(done) == 1, "One loser must return while the winner's physical call is still held"
        loser = next(r for r, task in tasks.items() if task in done)
        winner = "B" if loser == "A" else "A"
        rejected = tasks[loser].result()
        assert rejected["execution_status"] == "REJECTED" and rejected["reason"] == "robot_busy"
        assert rejected["planner_calls"] == rejected["steps"] == 0
        assert not tasks[winner].done()
        assert during["active_missions"] == 1 and counter.current_execute == 1
        assert counter.current_side_effect_calls == 1
        counter.release.set()
        reports = {r: await asyncio.wait_for(task, timeout=1) for r, task in tasks.items()}
        save("complete", during=during, winner=winner, loser=loser, reports=reports,
             counter=counter.snapshot(), final_active_missions=counter.owner_count())
        assert reports[winner]["execution_status"] == "SUCCESS"
        assert reports[loser]["execution_status"] == "REJECTED"
        assert counter.max_active_missions == 1
        assert counter.max_concurrent_execute == counter.max_concurrent_side_effect_calls == 1
        assert counter.current_execute == counter.current_side_effect_calls == counter.owner_count() == 0
        assert all(not agent.admission.pending for agent in agents.values())
        return winner, loser, reports
    finally:
        counter.release.set()
        await asyncio.gather(*tasks.values(), return_exceptions=True)


@pytest.mark.parametrize("launch_order", [("A", "B"), ("B", "A")], ids=["A-first", "B-first"])
def test_two_agents_same_backend_simultaneous_start_never_overlap(launch_order, record):
    async def exercise():
        for round_no in range(10):
            counter = Counter()
            backend = CountingBackend(counter)
            agents = {r: CoEgent(None, backend, config=config()) for r in ("A", "B")}
            requests = {"A": request(f"A-{round_no}", 10, destination="A"),
                        "B": request(f"B-{round_no}", 20, destination="B")}
            prefix = f"race_{launch_order[0]}_{round_no:02d}"

            def save(stage, prefix=prefix, **value):
                record(prefix + "_" + stage, **value)

            winner, _, _ = await race(agents, requests, counter, launch_order, save)
            angle, target = (10, "A") if winner == "A" else (20, "B")
            assert backend.calls == [{"name": "rotate", "args": {"angle_deg": angle}},
                                     {"name": "navigate_to", "args": {"target": target}}]
    asyncio.run(exercise())


@pytest.mark.parametrize("loser_skill", ["rotate", "navigate_to", "get_pose", "stop"])
def test_b_rejected_while_a_active_without_even_stop_dispatch(loser_skill, record):
    async def exercise():
        counter = Counter()
        counter.hold = True
        backend = CountingBackend(counter)
        a, b = CoEgent(None, backend, config=config()), CoEgent(None, backend, config=config())
        req_a, req_b = request("running-A", 10), request("busy-B", 20, name=loser_skill)
        counter.owner_count = lambda: int(a.admission.owns(req_a)) + int(b.admission.owns(req_b))
        task = asyncio.create_task(a.run_request(req_a, initial_state()))
        try:
            await asyncio.wait_for(backend.started.wait(), timeout=1)
            before = len(counter.events)
            denied = await b.run_request(req_b, initial_state())
            entered = sum(e["kind"] == "enter" for e in counter.events[before:])
            record(f"A_running_B_{loser_skill}_denied", mission_b=denied, backend_execute_delta=entered,
                   active_missions=counter.owner_count(), counter=counter.snapshot())
            assert entered == 0, "Rejected Mission B must not execute even a stop"
            assert denied["execution_status"] == "REJECTED" and denied["reason"] == "robot_busy"
            assert counter.owner_count() == 1 and not task.done()
            assert counter.max_concurrent_execute == counter.max_concurrent_side_effect_calls == 1
            counter.release.set()
            completed = await asyncio.wait_for(task, timeout=1)
            assert completed["execution_status"] == "SUCCESS"
            assert backend.calls == [{"name": "rotate", "args": {"angle_deg": 10}}]
            assert counter.owner_count() == counter.current_execute == 0
            allowed = await b.run_request(request("new-approved-B", 20, name=loser_skill), initial_state())
            record(f"A_finished_B_{loser_skill}_admitted", mission_a=completed, mission_b=allowed,
                   counter=counter.snapshot(), backend_calls=backend.calls)
            assert allowed["execution_status"] == "SUCCESS"
            assert counter.max_concurrent_side_effect_calls == 1
            assert not a.admission.pending and not b.admission.pending
        finally:
            counter.release.set()
            await task
    asyncio.run(exercise())


def test_different_adapters_same_robot_share_one_execution_owner(tmp_path, record):
    async def exercise():
        counter = Counter()
        backends = {r: CountingBackend(counter, r) for r in ("A", "B")}
        guards = {r: RobotAdmission.for_backend(backends[r], robot_id="same-physical-robot",
                                               lock_dir=tmp_path / "locks") for r in ("A", "B")}
        agents = {r: CoEgent(None, backends[r], admission=guards[r], state_provider=Sensor(), config=config(
            state_mode="strict", max_observation_age_s=1, max_inspection_age_s=1)) for r in ("A", "B")}
        requests = {"A": request("adapter-A", 10), "B": request("adapter-B", 20)}
        try:
            assert await agents["A"].confirm_safe_reset()
            counter.hold = True

            def save(stage, **value):
                record("same_robot_adapters_" + stage, **value)

            winner, loser, _ = await race(agents, requests, counter, ("A", "B"), save)
            assert len(backends[loser].calls) == (1 if loser == "A" else 0)
            assert {c["name"] for c in backends[winner].calls} <= {"stop", "rotate"}
            assert sum(c["name"] == "rotate" for b in backends.values() for c in b.calls) == 1
        finally:
            counter.release.set()
            guards["A"].close()
    asyncio.run(exercise())


def test_independent_robots_can_overlap_as_instrumentation_control(record):
    async def exercise():
        counter = Counter()
        counter.hold = True
        backends = {r: CountingBackend(counter, r) for r in ("A", "B")}
        agents = {r: CoEgent(None, backends[r], config=config()) for r in ("A", "B")}
        requests = {"A": request("independent-A", 10), "B": request("independent-B", 20)}
        counter.owner_count = lambda: sum(agents[r].admission.owns(requests[r]) for r in ("A", "B"))
        tasks = {r: asyncio.create_task(agents[r].run_request(requests[r], initial_state())) for r in ("A", "B")}
        try:
            await asyncio.wait_for(asyncio.gather(*(b.started.wait() for b in backends.values())), timeout=1)
            during = counter.snapshot()
            record("independent_robots_during", counter=during, active_missions=counter.owner_count())
            assert counter.owner_count() == 2
            assert counter.current_execute == counter.max_concurrent_execute == 2
            assert counter.current_side_effect_calls == counter.max_concurrent_side_effect_calls == 2
            counter.release.set()
            reports = {r: await asyncio.wait_for(t, timeout=1) for r, t in tasks.items()}
            record("independent_robots_complete", reports=reports, counter=counter.snapshot())
            assert all(r["execution_status"] == "SUCCESS" for r in reports.values())
            assert counter.current_execute == counter.current_side_effect_calls == counter.owner_count() == 0
            assert all(not a.admission.pending for a in agents.values())
        finally:
            counter.release.set()
            await asyncio.gather(*tasks.values(), return_exceptions=True)
    asyncio.run(exercise())
