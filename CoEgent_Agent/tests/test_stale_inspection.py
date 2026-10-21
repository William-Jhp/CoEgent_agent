"""Expired target evidence must never authorize picking or borrow battery age."""

import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
from time import monotonic, time

import pytest

from coegent import CoEgent, MissionRequest
from coegent.observations import FreshState, ObservationSnapshot, leaves
from coegent.scenarios import initial_state, plan, skill
from coegent.skills import MockSkills
from coegent.planner import ScriptedPlanner
from coegent_contracts import RuntimeConfig
from coegent_contracts.semantics import check_preconditions


def config():
    return RuntimeConfig(state_mode="strict", max_observation_age_s=1, max_inspection_age_s=2,
                         max_replans=0, max_repairs=0, skill_timeout_s=2, mission_timeout_s=4)


class Sensor:
    def __init__(self, age=10):
        state = initial_state().snapshot()
        state["robot"]["location"] = "A"
        state["world"]["targets"]["box"] = {"exists": True, "location": "A"}
        self.values = {k: state[k] for k in ("robot", "world")}
        self.values["observations"] = {"inspection": {
            "target": "box", "target_exists": True, "location": "A"}}
        self.inspection_stamp = None if age is None else monotonic() - age
        self.reads = []

    async def snapshot(self):
        now = monotonic()
        values = deepcopy(self.values)
        stamps = {p: now for p in leaves({k: values[k] for k in ("robot", "world")})}
        stamps["robot.battery_percent"] = now - .1
        if self.inspection_stamp is not None:
            stamps["inspection"] = self.inspection_stamp
        self.reads.append({"read_at": now, "observed_at": deepcopy(stamps), "values": deepcopy(values)})
        return ObservationSnapshot(values, stamps)


class Backend(MockSkills):
    def __init__(self, hold_pose=False):
        super().__init__()
        self.entries = []
        self.hold_pose = hold_pose
        self.started, self.release = asyncio.Event(), asyncio.Event()

    async def execute(self, name, args, snapshot):
        self.entries.append({"name": name, "args": deepcopy(args), "snapshot": deepcopy(snapshot)})
        if name == "get_pose" and self.hold_pose:
            self.started.set()
            await self.release.wait()
        return await super().execute(name, args, snapshot)


@pytest.fixture
def record(tmp_path):
    root = Path(os.environ.get("COEGENT_STALE_INSPECTION_OUTPUT", str(tmp_path)))
    root.mkdir(parents=True, exist_ok=True)

    def save(name, **data):
        (root / f"{name}.json").write_text(json.dumps(
            {"scope": "CPU sensor/backend mock, strict runtime", **data},
            indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    return save


async def exercise_pick(age, record, artifact, *, nodes=None, inspection_override=None):
    provider, backend = Sensor(age), Backend()
    if inspection_override:
        provider.values["observations"]["inspection"].update(inspection_override)
    agent = CoEgent(None, backend, config=config(), state_provider=provider)
    assert await agent.confirm_safe_reset()
    backend.entries.clear()
    backend.calls.clear()
    provider.reads.clear()
    req = MissionRequest("stale-box", 1, time() + 30, "pick the verified box",
                         plan(*(nodes or [skill("pick_object", object="box")])))
    report = await agent.run_request(req, initial_state())
    picks = sum(e["name"] == "pick_object" for e in backend.entries)
    record(artifact, report=report, provider_reads=provider.reads, backend_entries=backend.entries,
           pick_call_count=picks, inspection_stamp=provider.inspection_stamp,
           max_inspection_age_s=2, pending_after=len(agent.admission.pending))
    assert not agent.admission.pending
    return report, picks, backend


@pytest.mark.parametrize("age", [10, 2.5, None, -10], ids=["10s", "2.5s", "missing", "future"])
def test_stale_or_unknown_inspection_never_dispatches_pick(age, record):
    async def exercise():
        report, picks, backend = await exercise_pick(age, record, f"blocked_{age}")
        assert picks == 0 and [e["name"] for e in backend.entries] == ["stop"]
        assert report["execution_status"] != "SUCCESS"
        assert report["stop_confirmation"]["confirmed"] is True
        assert report["robot_fault"] is None
        assert "inspection" not in report["state"]["observations"]
    asyncio.run(exercise())


def test_stale_inspection_is_recoverable_replan_not_safety_abort(record):
    async def exercise():
        report, picks, _ = await exercise_pick(10, record, "requested_recovery_disposition")
        assert picks == 0
        assert report["execution_status"] == "REPLAN_REQUIRED", (
            "Expired inspection should request fresh sensing/replanning, not terminal safety abort")
    asyncio.run(exercise())


def test_battery_refresh_preserves_inspection_age_independently(record):
    async def exercise():
        provider = Sensor(10)
        original = provider.inspection_stamp
        state = initial_state()
        fresh = FreshState(provider, config())
        observations = []
        for _ in range(3):
            await fresh.refresh(state, ("robot.battery_percent",), "dispatch")
            sample = provider.reads[-1]
            battery_stamp = sample["observed_at"]["robot.battery_percent"]
            inspection_stamp = sample["observed_at"]["inspection"]
            observations.append({"battery_age_s": sample["read_at"] - battery_stamp,
                                 "inspection_age_s": sample["read_at"] - inspection_stamp,
                                 "battery_stamp": battery_stamp, "inspection_stamp": inspection_stamp,
                                 "effective_state": state.snapshot()})
            assert .09 < sample["read_at"] - battery_stamp < .11
            assert sample["read_at"] - inspection_stamp >= 10
            assert inspection_stamp == original and fresh.inspection_stamp is None
            assert state.observations["_freshness"]["robot.battery_percent"] == battery_stamp
            assert "inspection" not in state.observations
            assert "inspection" not in state.observations["_freshness"]
            provider.values["robot"]["battery_percent"] -= 1
        record("independent_battery_inspection_age", original_inspection_stamp=original,
               refresh_samples=observations, provider_reads=provider.reads)
    asyncio.run(exercise())


@pytest.mark.parametrize("age", [.1, 1.5])
def test_fresh_inspection_allows_pick_and_keeps_original_stamp(age, record):
    async def exercise():
        report, picks, backend = await exercise_pick(age, record, f"fresh_{age}")
        assert report["execution_status"] == "SUCCESS" and picks == 1
        assert [e["name"] for e in backend.entries] == ["pick_object"]
        snapshot = backend.entries[0]["snapshot"]
        assert snapshot["observations"]["inspection"]["target"] == "box"
        assert snapshot["observations"]["_freshness"]["inspection"] < monotonic()
    asyncio.run(exercise())


def test_fresh_wrong_target_does_not_authorize_box_pick(record):
    async def exercise():
        report, picks, _ = await exercise_pick(.1, record, "fresh_wrong_target",
                                              inspection_override={"target": "other_box"})
        assert picks == 0 and report["reason"] == "target_not_verified"
    asyncio.run(exercise())


def test_inspection_expires_after_preflight_before_pick_dispatch(record):
    async def exercise():
        provider, backend = Sensor(.01), Backend(hold_pose=True)
        cfg = replace(config(), max_inspection_age_s=.2)
        agent = CoEgent(None, backend, config=cfg, state_provider=provider)
        assert await agent.confirm_safe_reset()
        backend.entries.clear()
        backend.calls.clear()
        provider.reads.clear()
        req = MissionRequest("expires-before-pick", 1, time() + 30, "get pose then pick box",
                             plan(skill("get_pose"), skill("pick_object", object="box")))
        task = asyncio.create_task(agent.run_request(req, initial_state()))
        try:
            await asyncio.wait_for(backend.started.wait(), 1)
            assert backend.entries[0]["snapshot"]["observations"]["inspection"]["target"] == "box"
            # Expire the original observation, not a replacement with an older timestamp.
            await asyncio.sleep(.21)
            backend.release.set()
            report = await asyncio.wait_for(task, 1)
            record("expired_after_preflight", report=report, provider_reads=provider.reads,
                   backend_entries=backend.entries, pick_call_count=sum(
                       e["name"] == "pick_object" for e in backend.entries), pending_after=len(agent.admission.pending))
            assert [e["name"] for e in backend.entries] == ["get_pose", "stop"]
            assert report["completed_steps"] == [{"name": "get_pose", "args": {}}]
            assert any(e["kind"] == "safety_gate" and e["skill"] == "pick_object" and
                       "stage" not in e for e in report["trace"])
            assert not agent.admission.pending
        finally:
            backend.release.set()
            await task
    asyncio.run(exercise())


def test_successful_reinspection_can_restore_pick_without_stale_timestamp_reuse(record):
    async def exercise():
        report, picks, backend = await exercise_pick(10, record, "reinspection_restores_pick", nodes=[
            skill("inspect_target", target="box"), skill("pick_object", object="box")])
        assert report["execution_status"] == "SUCCESS" and picks == 1
        assert [e["name"] for e in backend.entries] == ["inspect_target", "pick_object"]
    asyncio.run(exercise())


def test_driver_reinspection_works_when_provider_has_no_old_inspection(record):
    async def exercise():
        provider, backend = Sensor(10), Backend()
        # A provider without inspection must preserve new validated driver evidence.
        provider.values.pop("observations")
        agent = CoEgent(None, backend, config=config(), state_provider=provider)
        assert await agent.confirm_safe_reset()
        backend.entries.clear()
        provider.reads.clear()
        req = MissionRequest("fresh-driver-inspection", 1, time() + 30, "inspect and pick box",
                             plan(skill("inspect_target", target="box"), skill("pick_object", object="box")))
        report = await agent.run_request(req, initial_state())
        record("driver_reinspection_without_old_provider_inspection", report=report,
               provider_reads=provider.reads, backend_entries=backend.entries,
               pick_call_count=sum(e["name"] == "pick_object" for e in backend.entries),
               pending_after=len(agent.admission.pending))
        assert report["execution_status"] == "SUCCESS"
        assert [e["name"] for e in backend.entries] == ["inspect_target", "pick_object"]
        assert not agent.admission.pending
    asyncio.run(exercise())


@pytest.mark.parametrize("runtime_age,provider_age,runtime_exists,provider_exists,selected", [
    (.1, 10, True, True, "runtime"),
    (.1, 1, True, False, "runtime"),
    (1, .1, True, True, "provider"),
    (1, .1, True, False, "provider"),
    (1, .1, False, True, "provider"),
    (.1, None, True, True, "runtime"),
    (.1, -10, True, True, "runtime"),
    (10, 5, True, True, None),
], ids=["fresh-driver-old-provider", "older-negative", "newer-positive", "newer-negative",
        "newer-positive-replaces-negative", "missing-provider-stamp", "future-provider-stamp", "both-stale"])
def test_same_key_uses_newest_trusted_observation(
        runtime_age, provider_age, runtime_exists, provider_exists, selected, record):
    async def exercise():
        provider = Sensor(provider_age)
        provider.values["observations"]["inspection"]["target_exists"] = provider_exists
        state = initial_state()
        state.robot["location"] = "A"
        state.observations["inspection"] = {"target": "box", "location": "A", "target_exists": runtime_exists}
        fresh = FreshState(provider, config())
        runtime_stamp = monotonic() - runtime_age
        fresh.record(state, "inspect_target", runtime_stamp)
        await fresh.refresh(state)
        issue = check_preconditions("pick_object", {"object": "box"}, state.snapshot(), config())
        record(f"selection_{runtime_age}_{provider_age}_{runtime_exists}_{provider_exists}",
               state=state.snapshot(), runtime_stamp=runtime_stamp, provider_stamp=provider.inspection_stamp,
               selected_stamp=fresh.inspection_stamp, issue=issue)
        if selected is None:
            assert "inspection" not in state.observations
            assert state.observations["_invalidation"]["inspection"]["reason"] == "stale"
            assert issue["event"] == "REPLAN_REQUIRED"
        else:
            expected_stamp = runtime_stamp if selected == "runtime" else provider.inspection_stamp
            expected_exists = runtime_exists if selected == "runtime" else provider_exists
            assert fresh.inspection_stamp == expected_stamp
            assert state.observations["_freshness"]["inspection"] == expected_stamp
            assert state.observations["inspection"]["target_exists"] is expected_exists
            assert "inspection" not in state.observations.get("_invalidation", {})
            if expected_exists:
                assert issue is None
            else:
                assert issue["event"] == "SAFETY_ABORT"
    asyncio.run(exercise())


@pytest.mark.parametrize("age,reason", [(10, "stale"), (None, "invalid_timestamp"), (-10, "invalid_timestamp")])
def test_invalid_inspection_keeps_reason_not_evidence(age, reason, record):
    async def exercise():
        report, picks, _ = await exercise_pick(age, record, f"invalidation_{reason}_{age}")
        observations = report["state"]["observations"]
        assert picks == 0 and report["execution_status"] == "REPLAN_REQUIRED"
        assert "inspection" not in observations and "inspection" not in observations["_freshness"]
        metadata = observations["_invalidation"]["inspection"]
        assert metadata == {"reason": reason, "target": "box", "location": "A"}
        assert report["reason"] == ("inspection_stale_or_unknown" if reason == "stale" else "inspection_unknown")
    asyncio.run(exercise())


@pytest.mark.parametrize("override", [{"target": "other_box"}, {"location": "B"}])
def test_different_keys_are_not_timestamp_merged(override, record):
    async def exercise():
        provider = Sensor(1)
        provider.values["observations"]["inspection"].update(override)
        state = initial_state()
        state.robot["location"] = "A"
        state.observations["inspection"] = {"target": "box", "location": "A", "target_exists": True}
        fresh = FreshState(provider, config())
        driver_stamp = monotonic() - .1
        fresh.record(state, "inspect_target", driver_stamp)
        await fresh.refresh(state)
        issue = check_preconditions("pick_object", {"object": "box"}, state.snapshot(), config())
        record(f"mismatch_{next(iter(override))}", state=state.snapshot(), issue=issue)
        assert issue is None
        assert state.observations["inspection"]["target"] == "box"
        assert state.observations["inspection"]["location"] == "A"
        assert state.observations["_freshness"]["inspection"] == driver_stamp
        other_key = ("box", "B") if "location" in override else ("other_box", "A")
        assert fresh.inspection_candidates[other_key]["stamp"] == provider.inspection_stamp
    asyncio.run(exercise())


def test_caller_timestamp_is_not_trusted_provenance(record):
    async def exercise():
        provider = Sensor()
        provider.values.pop("observations")
        state = initial_state()
        state.observations["inspection"] = {"target": "box", "location": "A", "target_exists": True}
        state.observations["_freshness"] = {"inspection": monotonic()}
        fresh = FreshState(provider, config())
        await fresh.refresh(state)
        record("caller_provenance_rejected", state=state.snapshot())
        assert "inspection" not in state.observations
        assert state.observations["_invalidation"]["inspection"]["reason"] == "invalid_timestamp"
    asyncio.run(exercise())


def test_equal_timestamp_conflict_does_not_hide_negative_observation(record):
    async def exercise():
        provider = Sensor(.1)
        provider.values["observations"]["inspection"]["target_exists"] = False
        state = initial_state()
        state.robot["location"] = "A"
        state.observations["inspection"] = {"target": "box", "location": "A", "target_exists": True}
        fresh = FreshState(provider, config())
        fresh.record(state, "inspect_target", provider.inspection_stamp)
        await fresh.refresh(state)
        issue = check_preconditions("pick_object", {"object": "box"}, state.snapshot(), config())
        record("equal_stamp_negative", state=state.snapshot(), issue=issue)
        assert state.observations["inspection"]["target_exists"] is False
        assert issue["event"] == "SAFETY_ABORT"
    asyncio.run(exercise())


def test_stale_replans_once_then_reinspection_restores_execution(record):
    async def exercise():
        provider, backend = Sensor(10), Backend()
        planner = ScriptedPlanner([plan(skill("inspect_target", target="box"), skill("pick_object", object="box"))])
        cfg = replace(config(), max_replans=1)
        agent = CoEgent(planner, backend, config=cfg, state_provider=provider)
        assert await agent.confirm_safe_reset()
        backend.entries.clear()
        request = MissionRequest("recover-inspection", 1, time() + 30, "pick box",
                                 plan(skill("pick_object", object="box")))
        report = await agent.run_request(request, initial_state())
        record("bounded_reinspection_recovery", report=report, contexts=planner.contexts,
               backend_entries=backend.entries, pending_after=len(agent.admission.pending))
        assert report["execution_status"] == "SUCCESS" and report["robot_fault"] is None
        assert [e["name"] for e in backend.entries] == ["stop", "inspect_target", "pick_object"]
        assert len(planner.contexts) == 1
        assert planner.contexts[0]["feedback"]["reason"] == "inspection_stale_or_unknown"
        assert "inspection" not in report["state"]["observations"].get("_invalidation", {})
        assert not agent.admission.pending
    asyncio.run(exercise())


def test_fresh_confirmed_missing_target_is_not_replanned(record):
    async def exercise():
        report, picks, backend = await exercise_pick(.1, record, "fresh_confirmed_negative",
                                                    inspection_override={"target_exists": False})
        assert picks == 0 and report["execution_status"] == "SAFETY_ABORT"
        assert report["reason"] == "target_not_verified" and report["planner_calls"] == 0
        assert [e["name"] for e in backend.entries] == ["stop"]
    asyncio.run(exercise())


def test_newer_provider_negative_after_driver_success_blocks_physical_pick(record):
    async def exercise():
        provider = Sensor(10)

        class NegativeAfterInspect(Backend):
            async def execute(self, name, args, snapshot):
                result = await super().execute(name, args, snapshot)
                if name == "inspect_target":
                    provider.values["observations"]["inspection"]["target_exists"] = False
                    provider.inspection_stamp = monotonic()
                return result

        backend = NegativeAfterInspect()
        agent = CoEgent(None, backend, config=config(), state_provider=provider)
        assert await agent.confirm_safe_reset()
        backend.entries.clear()
        request = MissionRequest("new-negative", 1, time() + 30, "inspect and pick box",
                                 plan(skill("inspect_target", target="box"), skill("pick_object", object="box")))
        report = await agent.run_request(request, initial_state())
        record("newer_negative_after_driver_success", report=report, backend_entries=backend.entries,
               provider_reads=provider.reads, pending_after=len(agent.admission.pending))
        assert [e["name"] for e in backend.entries] == ["inspect_target", "stop"]
        assert report["execution_status"] == "SAFETY_ABORT" and report["reason"] == "target_not_verified"
        assert report["state"]["observations"]["inspection"]["target_exists"] is False
        assert report["state"]["observations"]["_freshness"]["inspection"] == provider.inspection_stamp
        assert not agent.admission.pending
    asyncio.run(exercise())


def test_location_change_invalidates_even_fresh_driver_inspection(record):
    async def exercise():
        provider = Sensor()
        provider.values.pop("observations")
        provider.values["robot"]["location"] = "B"
        state = initial_state()
        state.observations["inspection"] = {"target": "box", "location": "A", "target_exists": True}
        fresh = FreshState(provider, config())
        fresh.record(state, "inspect_target", monotonic() - .1)
        await fresh.refresh(state)
        record("location_changed", state=state.snapshot(), stamp=fresh.inspection_stamp)
        assert state.robot["location"] == "B" and "inspection" not in state.observations
        assert fresh.inspection_stamp is None
        assert state.observations["_invalidation"]["inspection"]["reason"] == "location_mismatch"
    asyncio.run(exercise())


def test_stale_reason_survives_next_refresh_without_candidate(record):
    async def exercise():
        provider = Sensor(10)
        state = initial_state()
        fresh = FreshState(provider, config())
        await fresh.refresh(state)
        invalidation = deepcopy(state.observations["_invalidation"]["inspection"])
        provider.values.pop("observations")
        await fresh.refresh(state)
        record("invalidation_survives_empty_refresh", state=state.snapshot())
        assert state.observations["_invalidation"]["inspection"] == invalidation
        assert invalidation["reason"] == "stale"
        assert "inspection" not in state.observations
        assert "inspection" not in state.observations["_freshness"]
    asyncio.run(exercise())


def test_no_candidates_requests_sensing_without_invented_evidence(record):
    async def exercise():
        provider = Sensor()
        provider.values.pop("observations")
        state = initial_state()
        fresh = FreshState(provider, config())
        await fresh.refresh(state)
        issue = check_preconditions("pick_object", {"object": "box"}, state.snapshot(), config())
        record("no_candidates", state=state.snapshot(), issue=issue)
        assert state.observations["_invalidation"]["inspection"] == {"reason": "missing"}
        assert issue["event"] == "REPLAN_REQUIRED" and issue["reason"] == "inspection_unknown"
        assert fresh.inspection_stamp is None and "inspection" not in state.observations
    asyncio.run(exercise())
