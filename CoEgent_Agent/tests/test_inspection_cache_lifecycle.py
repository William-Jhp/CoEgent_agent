"""Candidate cache is mission-local safety evidence, never mission history."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from time import monotonic, time

import pytest

from coegent import CoEgent, MissionRequest
from coegent.goals import Goal
from coegent.observations import FreshState
from coegent.registry import SkillRegistry
from coegent.scenarios import initial_state, plan, skill
from coegent.types import RuntimeHalt
from coegent.validation import PlanValidator
from coegent_contracts.semantics import check_preconditions

from test_inspection_candidate_conflicts import record as record
from test_stale_inspection import Backend, Sensor, config


def seed(fresh, state, target="box", location="A", age=.1):
    state.observations["inspection"] = {"target": target, "location": location, "target_exists": True}
    fresh.record(state, "inspect_target", monotonic() - age)


@pytest.mark.parametrize("name,args", [("pick_object", {"object": "box"}), ("close_valve", {"target": "box"})])
def test_preflight_selects_requested_key_not_last_projection(name, args, record):
    async def exercise():
        provider = Sensor(.1)
        provider.values["observations"]["inspection"]["target"] = "chair"
        state = initial_state()
        state.robot["location"] = "A"
        fresh = FreshState(provider, config())
        seed(fresh, state)
        seed(fresh, state, target="chair")
        await fresh.refresh(state)
        validator = PlanValidator(SkillRegistry())
        result = validator.preflight(plan(skill(name, **args)), state, config(), fresh=fresh)
        record(f"preflight_{name}", findings=result, cache_keys=[list(k) for k in fresh.inspection_candidates])
        assert result[0]["reason"] == "await_driver_result"
    asyncio.run(exercise())


def test_stale_cached_key_is_not_authorized_by_another_fresh_key(record):
    async def exercise():
        provider = Sensor(.1)
        provider.values["observations"]["inspection"]["target"] = "chair"
        state = initial_state()
        state.robot["location"] = "A"
        fresh = FreshState(provider, config())
        seed(fresh, state, age=10)
        await fresh.refresh(state)
        assert hasattr(fresh, "project_for_skill"), "Request-key selection is required"
        snapshot = state.snapshot()
        fresh.project_for_skill(snapshot, "pick_object", {"object": "box"})
        issue = check_preconditions("pick_object", {"object": "box"}, snapshot, config())
        record("cached_stale_key", state=snapshot, issue=issue)
        assert "inspection" not in snapshot["observations"]
        assert issue["event"] == "REPLAN_REQUIRED" and issue["reason"] == "inspection_stale_or_unknown"
        assert fresh.inspection_candidates[("box", "A")]["data"] is None
    asyncio.run(exercise())


def test_trusted_location_roundtrip_cannot_resurrect_old_provider(record):
    async def exercise():
        provider = Sensor(.1)
        state = initial_state()
        fresh = FreshState(provider, config())
        await fresh.refresh(state)
        old_stamp = provider.inspection_stamp
        provider.values["robot"]["location"] = "B"
        await fresh.refresh(state)
        provider.values["robot"]["location"] = "A"
        await fresh.refresh(state)
        record("trusted_location_roundtrip", state=state.snapshot(), old_stamp=old_stamp)
        assert "inspection" not in state.observations
        assert fresh.inspection_stamp is None
    asyncio.run(exercise())


@pytest.mark.parametrize("motion", ["rotate", "roundtrip"])
def test_actual_motion_invalidates_cache_but_preserves_goal_evidence(motion, record):
    async def exercise():
        provider = Sensor(.1)

        class MovingBackend(Backend):
            async def execute(self, name, args, snapshot):
                result = await super().execute(name, args, snapshot)
                if result.ok and name == "navigate_to":
                    provider.values["robot"]["location"] = result.data["location"]
                return result

        backend = MovingBackend()
        agent = CoEgent(None, backend, config=config(), state_provider=provider)
        assert await agent.confirm_safe_reset()
        backend.entries.clear()
        moves = ([skill("rotate", angle_deg=30)] if motion == "rotate" else
                 [skill("navigate_to", target="B"), skill("navigate_to", target="A")])
        request = MissionRequest(f"motion-{motion}", 1, time() + 30, "inspect, move, then pick box",
                                 plan(skill("inspect_target", target="box"), *moves,
                                      skill("pick_object", object="box")),
                                 goals=(Goal("inspected_target", "box"),))
        report = await agent.run_request(request, initial_state())
        record(f"motion_{motion}", report=report, backend_entries=backend.entries,
               pending_after=len(agent.admission.pending))
        assert "pick_object" not in [e["name"] for e in backend.entries]
        assert report["execution_status"] == "REPLAN_REQUIRED"
        assert report["goal_status"] == "achieved" and report["mission_success"] is False
        assert "inspection" not in report["state"]["observations"]
        assert not agent.admission.pending
    asyncio.run(exercise())


def test_completed_mission_cache_does_not_supply_next_mission(record):
    async def exercise():
        provider, backend = Sensor(10), Backend()
        agent = CoEgent(None, backend, config=config(), state_provider=provider)
        assert await agent.confirm_safe_reset()
        first = await agent.run_request(MissionRequest("cache-first", 1, time() + 30, "inspect box",
                                                       plan(skill("inspect_target", target="box"))), initial_state())
        assert first["execution_status"] == "SUCCESS"
        provider.values.pop("observations")
        backend.entries.clear()
        second = await agent.run_request(MissionRequest("cache-second", 1, time() + 30, "pick box",
                                                        plan(skill("pick_object", object="box"))), initial_state())
        record("next_mission", report=second, backend_entries=backend.entries)
        assert second["execution_status"] == "REPLAN_REQUIRED"
        assert [e["name"] for e in backend.entries] == ["stop"]
    asyncio.run(exercise())


def test_fresh_reinspection_after_motion_restores_pick(record):
    async def exercise():
        provider, backend = Sensor(10), Backend()
        agent = CoEgent(None, backend, config=config(), state_provider=provider)
        assert await agent.confirm_safe_reset()
        backend.entries.clear()
        request = MissionRequest("reinspect-after-motion", 1, time() + 30, "turn, inspect, pick",
                                 plan(skill("rotate", angle_deg=30), skill("inspect_target", target="box"),
                                      skill("pick_object", object="box")))
        report = await agent.run_request(request, initial_state())
        record("fresh_after_motion", report=report, backend_entries=backend.entries)
        assert report["execution_status"] == "SUCCESS"
        assert [e["name"] for e in backend.entries] == ["rotate", "inspect_target", "pick_object"]
    asyncio.run(exercise())


def test_non_selected_expired_candidates_lose_payloads(record):
    async def exercise():
        provider = Sensor(.005)
        state = initial_state()
        state.robot["location"] = "A"
        fresh = FreshState(provider, replace(config(), max_inspection_age_s=.03))
        seed(fresh, state, age=.005)
        seed(fresh, state, target="chair", age=.005)
        await asyncio.sleep(.04)
        fresh.prune_inspections()
        snapshot = state.snapshot()
        fresh.project_for_skill(snapshot, "pick_object", {"object": "box"})
        record("expired_non_selected", state=snapshot,
               candidates=[{"key": list(k), **v} for k, v in fresh.inspection_candidates.items()])
        assert all(v["data"] is None and v["stamp"] is None and v["reason"] == "stale"
                   for v in fresh.inspection_candidates.values())
        assert "inspection" not in snapshot["observations"]
        assert snapshot["observations"]["_invalidation"]["inspection"]["reason"] == "stale"
    asyncio.run(exercise())


def test_capacity_does_not_evict_fresh_negative_evidence(record):
    async def exercise():
        provider = Sensor(.1)
        state = initial_state()
        state.robot["location"] = "A"
        fresh = FreshState(provider, config())
        fresh.MAX_INSPECTION_CANDIDATES = 2
        state.observations["inspection"] = {"target": "box", "location": "A", "target_exists": False}
        fresh.record(state, "inspect_target", monotonic())
        seed(fresh, state, target="chair", age=0)
        provider.values["observations"]["inspection"]["target"] = "lamp"
        with pytest.raises(RuntimeHalt) as raised:
            await fresh.refresh(state)
        record("capacity_fail_closed", reason=raised.value.reason,
               candidates=[{"key": list(k), **v} for k, v in fresh.inspection_candidates.items()])
        assert raised.value.status == "REPLAN_REQUIRED" and raised.value.reason == "inspection_cache_capacity"
        assert len(fresh.inspection_candidates) == 2
        assert fresh.inspection_candidates[("box", "A")]["data"]["target_exists"] is False
    asyncio.run(exercise())


def test_valve_effect_does_not_restore_old_provider_evidence(record):
    async def exercise():
        provider, backend = Sensor(.1), Backend()
        agent = CoEgent(None, backend, config=config(), state_provider=provider)
        assert await agent.confirm_safe_reset()
        backend.entries.clear()
        request = MissionRequest("valve-cache", 1, time() + 30, "inspect, close valve, pick",
                                 plan(skill("inspect_target", target="box"), skill("close_valve", target="box"),
                                      skill("pick_object", object="box")))
        report = await agent.run_request(request, initial_state())
        record("valve_effect", report=report, backend_entries=backend.entries,
               pending_after=len(agent.admission.pending))
        assert [e["name"] for e in backend.entries] == ["inspect_target", "close_valve", "stop"]
        assert report["execution_status"] == "REPLAN_REQUIRED"
        assert "inspection" not in report["state"]["observations"]
        assert not agent.admission.pending
    asyncio.run(exercise())


def test_multikey_motion_roundtrip_does_not_resurrect_old_inspections(record):
    async def exercise():
        provider = Sensor(.1)
        provider.values["world"]["targets"]["chair"] = {"exists": True, "location": "A"}
        original_provider_stamp = provider.inspection_stamp
        before_motion = {}
        motion_checkpoints = []

        class MovingBackend(Backend):
            async def execute(self, name, args, snapshot):
                if name == "get_pose":
                    before_motion.update(deepcopy(agent.fresh.inspection_candidates))
                if name == "navigate_to":
                    motion_checkpoints.append({
                        "from": snapshot["robot"]["location"], "to": args["target"],
                        "candidates": [{"key": list(k), **deepcopy(v)}
                                       for k, v in agent.fresh.inspection_candidates.items()]})
                result = await super().execute(name, args, snapshot)
                if result.ok and name == "navigate_to":
                    provider.values["robot"]["location"] = result.data["location"]
                return result

        backend = MovingBackend()
        agent = CoEgent(None, backend, config=config(), state_provider=provider)
        assert await agent.confirm_safe_reset()
        backend.entries.clear()
        provider.reads.clear()
        request = MissionRequest(
            "multikey-motion-roundtrip", 1, time() + 30, "inspect box and chair, move B then A, pick box",
            plan(skill("inspect_target", target="box"), skill("inspect_target", target="chair"),
                 skill("get_pose"), skill("navigate_to", target="B"), skill("navigate_to", target="A"),
                 skill("pick_object", object="box")),
            goals=(Goal("inspected_target", "box"), Goal("inspected_target", "chair")))
        report = await agent.run_request(request, initial_state())
        final_candidates = agent.fresh.inspection_candidates
        picks = sum(e["name"] == "pick_object" for e in backend.entries)
        record("multikey_motion_roundtrip", report=report, backend_entries=backend.entries,
               before_motion=[{"key": list(k), **v} for k, v in before_motion.items()],
               motion_checkpoints=motion_checkpoints,
               final_candidates=[{"key": list(k), **deepcopy(v)} for k, v in final_candidates.items()],
               provider_stamp_before=original_provider_stamp, provider_stamp_after=provider.inspection_stamp,
               provider_reads=provider.reads, pick_call_count=picks, pending_after=len(agent.admission.pending))
        assert set(before_motion) == {("box", "A"), ("chair", "A")}
        assert all(v["data"]["target_exists"] is True and v["stamp"] > original_provider_stamp
                   for v in before_motion.values())
        assert [(c["from"], c["to"]) for c in motion_checkpoints] == [("A", "B"), ("B", "A")]
        assert all(not c["candidates"] for c in motion_checkpoints)
        assert all(v["data"] is None and v["stamp"] is None for v in final_candidates.values())
        assert provider.inspection_stamp == original_provider_stamp
        assert [e["name"] for e in backend.entries] == [
            "inspect_target", "inspect_target", "get_pose", "navigate_to", "navigate_to", "stop"]
        assert picks == 0 and report["state"]["robot"]["location"] == "A"
        assert report["execution_status"] == "REPLAN_REQUIRED"
        assert report["goal_status"] == "achieved" and report["mission_success"] is False
        assert "inspection" not in report["state"]["observations"]
        assert agent.fresh.inspection_stamp is None
        assert report["stop_confirmation"]["confirmed"] is True and report["robot_fault"] is None
        assert not agent.admission.pending
    asyncio.run(exercise())
