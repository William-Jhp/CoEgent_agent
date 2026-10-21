"""Network-like retransmissions must not redispatch or preempt robot work."""

import asyncio
from copy import deepcopy
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
from time import monotonic, time

import pytest

from coegent import CoEgent, MissionRequest, RobotAdmission
from coegent.goals import Goal
from coegent.scenarios import initial_state, plan, skill
from coegent.skills import MockSkills
from coegent_contracts import RuntimeConfig


class CountingBackend(MockSkills):
    """Count entry, including unfinished calls; do not deduplicate in the mock."""

    def __init__(self, hold_skill=None):
        super().__init__()
        self.entries = []
        self.active = 0
        self.max_active = 0
        self.hold_skill = hold_skill
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, name, args, snapshot):
        self.entries.append({"name": name, "args": deepcopy(args), "at_monotonic": monotonic()})
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if name == self.hold_skill:
                self.started.set()
                await self.release.wait()
            return await super().execute(name, args, snapshot)
        finally:
            self.active -= 1

    def counts(self):
        return {"backend_execute_count": len(self.entries), "active_calls": self.active,
                "max_active_calls": self.max_active, "entries": deepcopy(self.entries),
                "completed_backend_calls": deepcopy(self.calls)}


def agent(backend, **kwargs):
    return CoEgent(None, backend, config=RuntimeConfig(
        max_replans=0, max_repairs=0, skill_timeout_s=2, mission_timeout_s=3), **kwargs)


def request():
    return MissionRequest("M1", 1, time() + 30, "rotate then go to A",
                          plan(skill("rotate", angle_deg=10), skill("navigate_to", target="A")))


def wire_copy(value):
    """Reconstruct a request after JSON serialization, not object-identity reuse."""
    body = json.loads(json.dumps(asdict(value), ensure_ascii=False))
    body["goals"] = tuple(Goal(**g) for g in body["goals"])
    return MissionRequest(**body)


@pytest.fixture
def record(tmp_path):
    directory = Path(os.environ.get("COEGENT_DEDUP_OUTPUT", str(tmp_path)))
    directory.mkdir(parents=True, exist_ok=True)

    def save(name, **values):
        (directory / f"{name}.json").write_text(json.dumps(
            {"scope": "CPU mock / validated MissionRequest, not a network transport test", **values},
            ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    return save


def assert_denied(report, reason, backend, before):
    assert report["execution_status"] == "REJECTED" and report["reason"] == reason
    assert report["steps"] == report["planner_calls"] == 0
    assert len(backend.entries) == before, "Denied request must not dispatch even stop"


@pytest.mark.parametrize("new_agent", [False, True], ids=["same-agent", "new-agent"])
def test_completed_duplicate_returns_saved_report_without_backend_calls(new_agent, record):
    async def exercise():
        backend = CountingBackend()
        first_agent = agent(backend)
        req = request()
        first = await first_agent.run_request(req, initial_state())
        receiver = agent(backend) if new_agent else first_agent
        before = len(backend.entries)
        replies = [await receiver.run_request(wire_copy(req), initial_state()) for _ in range(10)]
        saved = deepcopy(replies[0])
        record(f"completed_duplicate_{new_agent}", original=first, replay=saved,
               repeat_count=10, backend_execute_delta=len(backend.entries) - before, counter=backend.counts())
        assert first["execution_status"] == "SUCCESS"
        assert before == 2 and len(backend.entries) == 2
        assert backend.calls == [{"name": "rotate", "args": {"angle_deg": 10}},
                                 {"name": "navigate_to", "args": {"target": "A"}}]
        assert all(r == saved and r["replayed"] is True for r in replies)
        assert {k: saved[k] for k in first} == first
        assert saved["mission_completed_steps"] == first["completed_steps"]
        replies[0]["state"]["robot"]["location"] = "forged"
        replies[0]["completed_steps"].clear()
        assert await receiver.run_request(wire_copy(req), initial_state()) == saved
        assert len(backend.entries) == before and not first_agent.admission.pending
    asyncio.run(exercise())


@pytest.mark.parametrize("field", ["command", "plan", "expires_at", "goals"])
def test_same_key_different_payload_rejected_without_execution(field, record):
    async def exercise():
        backend = CountingBackend()
        receiver = agent(backend)
        req = request()
        original = await receiver.run_request(req, initial_state())
        changes = {"command": "different approved mission",
                   "plan": plan(skill("rotate", angle_deg=60)),
                   "expires_at": req.expires_at + 10,
                   "goals": (Goal("at_location", "HOME"),)}
        before = len(backend.entries)
        conflict = await receiver.run_request(replace(req, **{field: changes[field]}), initial_state())
        replay = await receiver.run_request(wire_copy(req), initial_state())
        record(f"payload_conflict_{field}", conflict=conflict, original=original, replay=replay,
               backend_execute_delta=len(backend.entries) - before, counter=backend.counts())
        assert_denied(conflict, "request_payload_conflict", backend, before)
        assert replay["replayed"] is True and replay["completed_steps"] == original["completed_steps"]
    asyncio.run(exercise())


@pytest.mark.parametrize("new_agent", [False, True], ids=["same-agent", "new-agent"])
def test_inflight_duplicate_returns_running_without_redispatch_or_cancellation(new_agent, record):
    async def exercise():
        backend = CountingBackend("navigate_to")
        a = agent(backend)
        b = agent(backend) if new_agent else a
        req = request()
        task = asyncio.create_task(a.run_request(req, initial_state()))
        try:
            await asyncio.wait_for(backend.started.wait(), 1)
            before = len(backend.entries)
            replies = [await b.run_request(wire_copy(req), initial_state()) for _ in range(10)]
            record(f"inflight_duplicate_{new_agent}", repeat_count=10, replies=replies,
                   owner_preserved=a.admission.owns(req), backend_execute_delta=len(backend.entries) - before,
                   original_task_done=task.done(), counter=backend.counts())
            assert before == 2 and backend.calls == [{"name": "rotate", "args": {"angle_deg": 10}}]
            assert all(r == {"execution_status": "RUNNING", "replayed": True} for r in replies)
            assert len(backend.entries) == before and backend.active == backend.max_active == 1
            assert a.admission.owns(req) and not task.done()
            backend.release.set()
            final = await asyncio.wait_for(task, 1)
            replay = await b.run_request(wire_copy(req), initial_state())
            record(f"inflight_duplicate_{new_agent}_complete", original=final, replay=replay,
                   counter=backend.counts(), active_owner=a.admission.owns(req))
            assert final["execution_status"] == "SUCCESS" and replay["execution_status"] == "SUCCESS"
            assert replay["completed_steps"] == final["completed_steps"] and len(backend.entries) == 2
            assert not a.admission.owns(req) and not a.admission.pending
        finally:
            backend.release.set()
            await task
    asyncio.run(exercise())


@pytest.mark.parametrize("kind,reason", [("conflict", "request_payload_conflict"),
                                        ("higher", "robot_busy")])
def test_conflict_or_higher_revision_cannot_preempt_running_original(kind, reason, record):
    async def exercise():
        backend = CountingBackend("rotate")
        a, b = agent(backend), agent(backend)
        req = request()
        incoming = replace(req, command="new approved payload", plan=plan(skill("rotate", angle_deg=20)),
                           revision=1 if kind == "conflict" else 2)
        task = asyncio.create_task(a.run_request(req, initial_state()))
        try:
            await asyncio.wait_for(backend.started.wait(), 1)
            before = len(backend.entries)
            denied = await b.run_request(incoming, initial_state())
            record(f"active_{kind}_denied", report=denied, owner_is_rev1=a.admission.owns(req),
                   owner_is_incoming=a.admission.owns(incoming), original_task_done=task.done(),
                   backend_execute_delta=len(backend.entries) - before, counter=backend.counts())
            assert_denied(denied, reason, backend, before)
            assert a.admission.owns(req) and not task.done() and backend.active == 1
            if kind == "higher":
                assert not a.admission.owns(incoming)
            backend.release.set()
            final = await asyncio.wait_for(task, 1)
            assert final["execution_status"] == "SUCCESS" and len(backend.entries) == 2
            assert backend.calls == [{"name": "rotate", "args": {"angle_deg": 10}},
                                     {"name": "navigate_to", "args": {"target": "A"}}]
            if kind == "higher":
                accepted = await b.run_request(incoming, initial_state())
                after = len(backend.entries)
                replay = await b.run_request(wire_copy(incoming), initial_state())
                record("higher_revision_after_original_finished", rev1=final, rev2=accepted, replay=replay,
                       replay_execute_delta=len(backend.entries) - after, counter=backend.counts())
                assert accepted["execution_status"] == "SUCCESS" and after == 3
                assert backend.calls[-1] == {"name": "rotate", "args": {"angle_deg": 20}}
                assert replay["mission_completed_steps"] == [
                    {"name": "rotate", "args": {"angle_deg": 10}},
                    {"name": "navigate_to", "args": {"target": "A"}},
                    {"name": "rotate", "args": {"angle_deg": 20}}]
                assert replay["replayed"] is True and len(backend.entries) == after
            assert not a.admission.pending and not a.admission.owns(req)
        finally:
            backend.release.set()
            await task
    asyncio.run(exercise())


def test_revision_zero_rejected_at_request_boundary_without_backend_calls(record):
    async def exercise():
        backend = CountingBackend()
        receiver = agent(backend)
        req = request()
        assert (await receiver.run_request(req, initial_state()))["execution_status"] == "SUCCESS"
        before = len(backend.entries)
        body = asdict(req)
        body["revision"] = 0
        with pytest.raises(ValueError, match="revision must be a positive integer") as caught:
            await receiver.run_request(MissionRequest(**body), initial_state())
        record("revision_zero_invalid", rejection_stage="MissionRequest validation", error=str(caught.value),
               backend_execute_delta=len(backend.entries) - before, counter=backend.counts())
        assert len(backend.entries) == before == 2
    asyncio.run(exercise())


def test_lower_positive_revision_rejected_after_newer_revision(record):
    async def exercise():
        backend = CountingBackend()
        receiver = agent(backend)
        req = request()
        await receiver.run_request(req, initial_state())
        second = replace(req, revision=2, plan=plan(skill("rotate", angle_deg=20)))
        assert (await receiver.run_request(second, initial_state()))["execution_status"] == "SUCCESS"
        before = len(backend.entries)
        old = await receiver.run_request(wire_copy(req), initial_state())
        replay = await receiver.run_request(wire_copy(second), initial_state())
        record("lower_positive_revision_stale", stale=old, current_replay=replay,
               backend_execute_delta=len(backend.entries) - before, counter=backend.counts())
        assert_denied(old, "stale_revision", backend, before)
        assert replay["revision"] == 2 and replay["replayed"] is True
    asyncio.run(exercise())


def test_json_object_key_order_does_not_create_payload_collision(record):
    async def exercise():
        backend = CountingBackend()
        receiver = agent(backend)
        req = request()
        first = await receiver.run_request(req, initial_state())
        reordered = json.loads(json.dumps(req.plan))
        reordered = dict(reversed(list(reordered.items())))
        before = len(backend.entries)
        replay = await receiver.run_request(replace(req, plan=reordered), initial_state())
        record("reordered_json_keys", replay=replay, backend_execute_delta=len(backend.entries) - before)
        assert replay["replayed"] is True and replay["completed_steps"] == first["completed_steps"]
        assert len(backend.entries) == before
    asyncio.run(exercise())


def test_same_command_new_identity_is_a_new_mission_not_a_duplicate(record):
    async def exercise():
        backend = CountingBackend()
        receiver = agent(backend)
        req = request()
        await receiver.run_request(req, initial_state())
        before = len(backend.entries)
        other = await receiver.run_request(replace(req, mission_id="M2"), initial_state())
        record("same_command_new_identity", report=other, backend_execute_delta=len(backend.entries) - before,
               counter=backend.counts())
        assert other["execution_status"] == "SUCCESS" and not other.get("replayed", False)
        assert len(backend.entries) - before == 2
    asyncio.run(exercise())


def test_expired_duplicate_rejected_without_reexecution(record):
    async def exercise():
        clock = {"utc": 100.0, "mono": 10.0}
        guard = RobotAdmission(utc_now=lambda: clock["utc"], monotonic_now=lambda: clock["mono"])
        backend = CountingBackend()
        receiver = agent(backend, admission=guard)
        req = replace(request(), expires_at=120.0)
        await receiver.run_request(req, initial_state())
        clock.update(utc=121.0, mono=31.0)
        before = len(backend.entries)
        expired = await receiver.run_request(wire_copy(req), initial_state())
        record("expired_duplicate", report=expired, backend_execute_delta=len(backend.entries) - before)
        assert_denied(expired, "request_expired", backend, before)
    asyncio.run(exercise())


def test_full_cache_does_not_evict_live_dedup_record_or_execute_new_request(record):
    async def exercise():
        backend = CountingBackend()
        receiver = agent(backend, admission=RobotAdmission(capacity=1))
        req = request()
        original = await receiver.run_request(req, initial_state())
        before = len(backend.entries)
        denied = await receiver.run_request(replace(req, mission_id="M2"), initial_state())
        replay = await receiver.run_request(wire_copy(req), initial_state())
        record("full_cache_preserves_dedup", denial=denied, original=original, replay=replay,
               backend_execute_delta=len(backend.entries) - before, counter=backend.counts())
        assert_denied(denied, "request_cache_full", backend, before)
        assert replay["replayed"] is True and replay["completed_steps"] == original["completed_steps"]
    asyncio.run(exercise())


def test_failed_terminal_report_is_replayed_not_retried(record):
    async def exercise():
        backend = CountingBackend()
        receiver = agent(backend)
        req = replace(request(), plan={"invalid_plan": True})
        first = await receiver.run_request(req, initial_state())
        before = len(backend.entries)
        replay = await receiver.run_request(wire_copy(req), initial_state())
        record("failed_report_replayed", original=first, replay=replay,
               backend_execute_delta=len(backend.entries) - before, counter=backend.counts())
        assert first["execution_status"] == "INVALID_PLAN"
        assert before == 1 and backend.calls == [{"name": "stop", "args": {}}]
        assert replay["replayed"] is True and {k: replay[k] for k in first} == first
        assert len(backend.entries) == before
    asyncio.run(exercise())
