"""Four requested driver/provider candidate cases, with real runtime dispatch."""

import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
from time import monotonic, time

import pytest

from coegent import CoEgent, MissionRequest
from coegent.scenarios import initial_state, plan, skill

from test_stale_inspection import Backend, Sensor, config


@pytest.fixture
def record(tmp_path):
    root = Path(os.environ.get("COEGENT_CANDIDATE_OUTPUT", str(tmp_path)))
    root.mkdir(parents=True, exist_ok=True)

    def save(name, **data):
        (root / f"{name}.json").write_text(json.dumps(
            {"scope": "strict CPU mock, actual inspect/pick dispatch", **data},
            indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    return save


async def candidate_mission(case, record):
    provider = Sensor(10)
    driver_observation = {}

    class SwitchAfterInspection(Backend):
        async def execute(self, name, args, snapshot):
            started_at = monotonic()
            result = await super().execute(name, args, snapshot)
            if name == "inspect_target":
                driver_observation.update({"data": deepcopy(result.data), "started_at_lower_bound": started_at})
                if case == "newer_negative":
                    provider.values["observations"]["inspection"]["target_exists"] = False
                    provider.inspection_stamp = monotonic()
                elif case == "different_target":
                    provider.values["observations"]["inspection"]["target"] = "chair"
                    provider.inspection_stamp = monotonic()
                elif case == "different_location":
                    provider.values["observations"]["inspection"]["location"] = "B"
                    provider.inspection_stamp = monotonic()
            return result

    backend = SwitchAfterInspection()
    agent = CoEgent(None, backend, config=config(), state_provider=provider)
    assert await agent.confirm_safe_reset()
    backend.entries.clear()
    backend.calls.clear()
    provider.reads.clear()
    request = MissionRequest(f"candidate-{case}", 1, time() + 30, "inspect and pick box at A",
                             plan(skill("inspect_target", target="box"), skill("pick_object", object="box")))
    report = await agent.run_request(request, initial_state())
    candidates = getattr(agent.fresh, "inspection_candidates", {})
    record(case, report=report, driver_observation=driver_observation,
           provider_candidate=deepcopy(provider.values["observations"]["inspection"]),
           provider_stamp=provider.inspection_stamp, selected_stamp=agent.fresh.inspection_stamp,
           provider_reads=provider.reads, backend_entries=backend.entries,
           pick_call_count=sum(e["name"] == "pick_object" for e in backend.entries),
           pending_after=len(agent.admission.pending),
           cached_candidates=[{"key": list(key), **deepcopy(value)} for key, value in candidates.items()])
    assert not agent.admission.pending
    assert driver_observation["data"]["target"] == "box"
    assert driver_observation["data"]["location"] == "A"
    assert driver_observation["data"]["target_exists"] is True
    if case in {"different_target", "different_location"}:
        other_key = ("chair", "A") if case == "different_target" else ("box", "B")
        assert ("box", "A") in candidates and other_key in candidates, "Both distinct keys must remain cached"
        assert candidates[("box", "A")]["data"]["target_exists"] is True
        assert candidates[other_key]["stamp"] == provider.inspection_stamp
    return report, backend, provider


def test_old_provider_new_driver_positive_preserves_driver_and_picks(record):
    async def exercise():
        report, backend, provider = await candidate_mission("older_provider", record)
        assert report["execution_status"] == "SUCCESS"
        assert [e["name"] for e in backend.entries] == ["inspect_target", "pick_object"]
        selected = backend.entries[1]["snapshot"]["observations"]
        assert selected["inspection"]["target"] == "box"
        assert selected["inspection"]["target_exists"] is True
        assert selected["_freshness"]["inspection"] > provider.inspection_stamp
    asyncio.run(exercise())


def test_new_provider_negative_old_driver_positive_blocks_pick(record):
    async def exercise():
        report, backend, provider = await candidate_mission("newer_negative", record)
        assert report["execution_status"] == "SAFETY_ABORT"
        assert report["reason"] == "target_not_verified"
        assert [e["name"] for e in backend.entries] == ["inspect_target", "stop"]
        selected = report["state"]["observations"]
        assert selected["inspection"]["target_exists"] is False
        assert selected["_freshness"]["inspection"] == provider.inspection_stamp
    asyncio.run(exercise())


def test_driver_box_provider_chair_does_not_overwrite_box_evidence(record):
    async def exercise():
        report, backend, provider = await candidate_mission("different_target", record)
        selected = report["state"]["observations"].get("inspection", {})
        assert selected.get("target") == "box", "Unrelated chair observation must not overwrite box evidence"
        assert selected["target_exists"] is True and selected["location"] == "A"
        assert provider.values["observations"]["inspection"]["target"] == "chair"
        assert [e["name"] for e in backend.entries] == ["inspect_target", "pick_object"]
    asyncio.run(exercise())


def test_same_target_different_location_keeps_evidence_separate(record):
    async def exercise():
        report, backend, provider = await candidate_mission("different_location", record)
        selected = report["state"]["observations"].get("inspection", {})
        assert (selected.get("target"), selected.get("location")) == ("box", "A"), (
            "Inspection for box/B must not replace or erase fresh box/A evidence")
        assert provider.values["observations"]["inspection"]["location"] == "B"
        picks = [e for e in backend.entries if e["name"] == "pick_object"]
        assert len(picks) == 1
        assert picks[0]["snapshot"]["observations"]["inspection"]["location"] == "A"
        assert picks[0]["snapshot"]["observations"]["_freshness"]["inspection"] != provider.inspection_stamp
    asyncio.run(exercise())
