"""Stop/fault acceptance cases; real Agent/Engine/Router with a controlled driver."""

import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
from time import time

import pytest

from coegent import CoEgent, MissionRequest
from coegent.scenarios import initial_state, plan, skill
from coegent.types import Event, SkillResult
from coegent_contracts import RuntimeConfig


class ControlledDriver:
    """Only the hardware boundary is simulated; no runtime decisions live here."""

    def __init__(self, mode, *, stop_ack=True):
        self.mode, self.stop_ack = mode, stop_ack
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.actual = {"stopped": True, "pose_known": True, "motion_started": False}

    async def execute_with_state(self, name, args, snapshot, execution):
        execution.record_physical_side_effect(name in {"rotate", "stop"})
        return await self.execute(name, args, snapshot)

    async def execute(self, name, args, snapshot):
        self.calls.append({"name": name, "args": deepcopy(args)})
        if name == "stop":
            if not self.stop_ack:
                self.actual["stopped"] = None
                return SkillResult(name, Event.EXPECTED_FAILURE, "motor_stop_not_acknowledged")
            self.actual["stopped"] = True
            return SkillResult(name, data={"stopped": True})
        if name == "rotate":
            self.actual.update(stopped=False, pose_known=False, motion_started=True)
            self.started.set()
            if self.mode == "actuator_exception":
                raise RuntimeError("transport_lost_after_motion_started")
            await self.release.wait()
            return SkillResult(name)
        if name == "inspect_scene":
            if self.mode == "inspect_exception":
                raise RuntimeError("camera_read_failed_without_actuation")
            if self.mode == "inspect_failure":
                return SkillResult(name, Event.EXPECTED_FAILURE, "camera_unavailable", recoverable=True)
        if name == "get_pose":
            return SkillResult(name, data={"location": snapshot["robot"]["location"]})
        raise AssertionError(f"unexpected driver dispatch: {name}")


def agent_for(driver):
    return CoEgent(None, driver, config=RuntimeConfig(max_replans=0, max_repairs=0,
                    mission_timeout_s=.12, skill_timeout_s=1, connection_poll_s=.02))


def mission(identity, name="rotate"):
    node = skill(name, angle_deg=30) if name == "rotate" else skill(name)
    return MissionRequest(identity, 1, time() + 10, "approved test mission", plan(node))


@pytest.fixture
def save_case(tmp_path):
    directory = Path(os.environ.get("COEGENT_STOP_FAULT_OUTPUT", str(tmp_path)))
    directory.mkdir(parents=True, exist_ok=True)

    def save(case, driver, first, second=None, *, delta=None, pending=None):
        value = {"case": case, "scope": "CPU mock, not physical robot evidence",
                 "mission_a": first, "mission_b": second, "backend_calls": driver.calls,
                 "mission_b_backend_execute_delta": delta, "simulated_actual_state": driver.actual,
                 "pending_driver_or_mission_tasks": pending}
        (directory / f"{case}.json").write_text(json.dumps(value, ensure_ascii=False,
                                                         indent=2, allow_nan=False) + "\n")
    return save


def test_case_a_timeout_ack_success_no_persistent_fault(save_case):
    async def exercise():
        driver = ControlledDriver("block")
        agent = agent_for(driver)
        first = await agent.run_request(mission("A"), initial_state())
        before = len(driver.calls)
        second = await agent.run_request(mission("A-next", "get_pose"), initial_state())
        save_case("A_timeout_ack_success", driver, first, second,
                  delta=len(driver.calls) - before, pending=len(agent.admission.pending))
        assert first["execution_status"] == "SAFETY_ABORT"
        assert first["reason"] == first["failure_reason"] == "mission_timeout"
        assert first["stop_confirmation"]["confirmed"] is True
        assert first["state"]["robot"]["stopped"] is True
        assert driver.actual["stopped"] is True
        assert first["robot_fault"] is None and agent.admission.fault_reason is None
        assert second["execution_status"] == "SUCCESS"
        assert [c["name"] for c in driver.calls] == ["rotate", "stop", "get_pose"]
        assert not agent.admission.pending
    asyncio.run(exercise())


def test_case_b_timeout_ack_fail_latches_and_escalates_report(save_case):
    async def exercise():
        driver = ControlledDriver("block", stop_ack=False)
        agent = agent_for(driver)
        first = await agent.run_request(mission("B"), initial_state())
        before = len(driver.calls)
        second = await agent.run_request(mission("B-next"), initial_state())
        delta = len(driver.calls) - before
        save_case("B_timeout_ack_fail", driver, first, second, delta=delta,
                  pending=len(agent.admission.pending))
        assert first["stop_confirmation"]["confirmed"] is False
        assert first["execution_status"] == "SAFETY_ABORT"
        assert first["reason"] == "stop_unconfirmed:motor_stop_not_acknowledged"
        assert first["failure_reason"] == "mission_timeout"
        assert first["robot_fault"] and agent.admission.fault_reason
        assert delta == 0, "Rejected mission must not dispatch even another stop"
        assert second["execution_status"] == "REJECTED" and second["reason"] == "robot_fault_latched"
        assert [c["name"] for c in driver.calls] == ["rotate", "stop"]
        assert not agent.admission.pending
    asyncio.run(exercise())


def test_case_c_cancel_ack_fail_latches_and_escalates_report(save_case):
    async def exercise():
        driver = ControlledDriver("block", stop_ack=False)
        agent = agent_for(driver)
        task = asyncio.create_task(agent.run_request(mission("C"), initial_state()))
        await asyncio.wait_for(driver.started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        first = deepcopy(agent.last_report)
        before = len(driver.calls)
        second = await agent.run_request(mission("C-next"), initial_state())
        delta = len(driver.calls) - before
        save_case("C_cancel_ack_fail", driver, first, second, delta=delta,
                  pending=len(agent.admission.pending))
        assert first["stop_confirmation"]["confirmed"] is False
        assert first["execution_status"] == "SAFETY_ABORT"
        assert first["reason"] == "stop_unconfirmed:motor_stop_not_acknowledged"
        assert first["failure_reason"] == "mission_cancelled"
        assert first["robot_fault"] and agent.admission.fault_reason
        assert delta == 0 and second["execution_status"] == "REJECTED"
        assert [c["name"] for c in driver.calls] == ["rotate", "stop"]
        assert not agent.admission.pending
    asyncio.run(exercise())


def test_case_d_exception_after_actuation_keeps_unknown_state_fault(save_case):
    async def exercise():
        driver = ControlledDriver("actuator_exception")
        agent = agent_for(driver)
        first = await agent.run_request(mission("D"), initial_state())
        before = len(driver.calls)
        second = await agent.run_request(mission("D-next"), initial_state())
        delta = len(driver.calls) - before
        save_case("D_actuator_exception", driver, first, second, delta=delta,
                  pending=len(agent.admission.pending))
        assert driver.actual["motion_started"] and driver.actual["pose_known"] is False
        assert first["execution_status"] == "SAFETY_ABORT"
        assert first["robot_fault"] == agent.admission.fault_reason == "driver_error:RuntimeError"
        assert first["stop_confirmation"]["confirmed"] is True
        assert delta == 0 and second["execution_status"] == "REJECTED"
        assert [c["name"] for c in driver.calls] == ["rotate", "stop"]
        assert not agent.admission.pending
    asyncio.run(exercise())


@pytest.mark.parametrize("mode", ["inspect_failure", "inspect_exception"], ids=["E-result", "E-exception"])
def test_case_e_read_only_failure_does_not_latch_physical_fault(mode, save_case):
    async def exercise():
        driver = ControlledDriver(mode)
        agent = agent_for(driver)
        first = await agent.run_request(mission("E", "inspect_scene"), initial_state())
        before = len(driver.calls)
        second = await agent.run_request(mission("E-next", "get_pose"), initial_state())
        save_case(f"E_{mode}", driver, first, second, delta=len(driver.calls) - before,
                  pending=len(agent.admission.pending))
        assert driver.actual["motion_started"] is False and driver.actual["pose_known"] is True
        assert first["execution_status"] != "SUCCESS"
        assert first["stop_confirmation"]["confirmed"] is True
        assert first["robot_fault"] is None, "A read-only camera failure is not unknown actuator state"
        assert first["execution_status"] == "REPLAN_REQUIRED"
        if mode == "inspect_exception":
            failures = [e for e in first["trace"] if e["kind"] == "skill_result"
                        and e["skill"] == "inspect_scene"]
            assert failures[0]["event"] == "EXPECTED_FAILURE" and failures[0]["recoverable"] is True
            assert failures[0]["reason"] == "driver_exception:RuntimeError"
        assert agent.admission.fault_reason is None
        assert second["execution_status"] == "SUCCESS"
        assert [c["name"] for c in driver.calls] == ["inspect_scene", "stop", "get_pose"]
        assert not agent.admission.pending
    asyncio.run(exercise())


@pytest.mark.parametrize("name,physical", [
    ("inspect_target", False), ("get_pose", False), ("get_battery", False),
    ("check_path", False), ("inspect_scene", False), ("localize", False),
    ("navigate_to", True), ("rotate", True), ("move_relative", True),
    ("pick_object", True), ("place_object", True), ("stop", True),
    ("return_home", True), ("close_valve", True), ("open_valve", True),
    ("dock_for_charge", True),
])
def test_physical_effect_classification_uses_shared_contract(name, physical):
    from coegent_contracts.registry import SkillRegistry
    from coegent_contracts.semantics import has_physical_effect
    assert has_physical_effect(SkillRegistry().definition(name)) is physical


@pytest.mark.parametrize("effects", [[], None, "observation.pose", ["unknown.effect"],
                                         ["observation.unregistered"], [0], ["pose"]])
def test_missing_or_unknown_effects_are_not_read_only(effects):
    from coegent_contracts.semantics import has_physical_effect
    assert has_physical_effect({"effects": effects}) is True


@pytest.mark.parametrize("signal", [True, None, "false", 0], ids=["started", "unknown", "string", "integer"])
def test_read_only_contract_cannot_override_actuation_or_unknown_execution(signal):
    class Driver(ControlledDriver):
        async def execute_with_state(self, name, args, snapshot, execution):
            if name == "inspect_scene":
                if signal is not None:
                    execution.record_physical_side_effect(signal)
                if signal is True:
                    execution.record_physical_side_effect(False)
                raise RuntimeError("camera_or_actuator_error")
            return await super().execute_with_state(name, args, snapshot, execution)

    async def exercise():
        driver = Driver("inspect_exception")
        agent = agent_for(driver)
        first = await agent.run_request(mission("uncertain-A", "inspect_scene"), initial_state())
        before = len(driver.calls)
        second = await agent.run_request(mission("uncertain-B"), initial_state())
        assert first["execution_status"] == "SAFETY_ABORT" and first["robot_fault"]
        assert second["execution_status"] == "REJECTED" and len(driver.calls) == before
        assert not agent.admission.pending
    asyncio.run(exercise())


def test_physical_contract_faults_even_when_driver_reports_not_started():
    class Driver(ControlledDriver):
        async def execute_with_state(self, name, args, snapshot, execution):
            if name == "rotate":
                execution.record_physical_side_effect(False)
                raise RuntimeError("motor_status_uncertain")
            return await super().execute_with_state(name, args, snapshot, execution)

    async def exercise():
        agent = agent_for(Driver("actuator_exception"))
        first = await agent.run_request(mission("physical"), initial_state())
        assert first["execution_status"] == "SAFETY_ABORT" and first["robot_fault"]
    asyncio.run(exercise())


def test_legacy_driver_without_per_call_state_keeps_unknown_exception_fault():
    class LegacyDriver:
        def __init__(self):
            self.driver = ControlledDriver("inspect_exception")

        async def execute(self, name, args, snapshot):
            return await self.driver.execute(name, args, snapshot)

    async def exercise():
        driver = LegacyDriver()
        agent = agent_for(driver)
        first = await agent.run_request(mission("legacy-A", "inspect_scene"), initial_state())
        before = len(driver.driver.calls)
        second = await agent.run_request(mission("legacy-B"), initial_state())
        assert first["robot_fault"] and first["execution_status"] == "SAFETY_ABORT"
        assert second["execution_status"] == "REJECTED" and len(driver.driver.calls) == before
    asyncio.run(exercise())


def test_execution_report_is_scoped_to_each_call():
    from coegent.skills import DriverExecutionState
    from coegent.types import Trace

    class Driver(ControlledDriver):
        async def execute_with_state(self, name, args, snapshot, execution):
            execution.record_physical_side_effect(name == "rotate")
            if name == "rotate":
                return SkillResult(name)
            raise RuntimeError("camera_read_error")

    async def exercise():
        driver = Driver("inspect_exception")
        agent = agent_for(driver)
        assert (await agent.router.dispatch("rotate", {"angle_deg": 30}, initial_state(), Trace())).ok
        result = await agent.router.dispatch("inspect_scene", {}, initial_state(), Trace())
        assert result.event == Event.EXPECTED_FAILURE and result.recoverable
        assert agent.admission.fault_reason is None
        recorded = DriverExecutionState()
        recorded.record_physical_side_effect(True)
        recorded.record_physical_side_effect(False)
        assert recorded.physical_side_effect_started is True
    asyncio.run(exercise())


@pytest.mark.parametrize("error_type", [RuntimeError, OSError, TimeoutError, ValueError])
@pytest.mark.parametrize("name", ["inspect_scene", "rotate"])
def test_driver_exception_type_does_not_determine_fault_policy(error_type, name):
    class Driver(ControlledDriver):
        async def execute_with_state(self, selected, args, snapshot, execution):
            if selected == name:
                self.calls.append({"name": selected, "args": deepcopy(args)})
                execution.record_physical_side_effect(False)
                raise error_type("driver_failure")
            return await super().execute_with_state(selected, args, snapshot, execution)

    async def exercise():
        driver = Driver("inspect_exception")
        agent = agent_for(driver)
        first = await agent.run_request(mission("typed-A", name), initial_state())
        before = len(driver.calls)
        second = await agent.run_request(mission("typed-B", "get_pose"), initial_state())
        if name == "inspect_scene":
            assert first["execution_status"] == "REPLAN_REQUIRED"
            assert first["failure_reason"] == f"driver_exception:{error_type.__name__}"
            assert first["robot_fault"] is None
            assert second["execution_status"] == "SUCCESS"
        else:
            assert first["execution_status"] == "SAFETY_ABORT"
            assert first["robot_fault"] == f"driver_error:{error_type.__name__}"
            assert second["execution_status"] == "REJECTED" and len(driver.calls) == before
    asyncio.run(exercise())


@pytest.mark.parametrize("new_agent", [False, True], ids=["same-agent", "new-agent-same-backend"])
def test_stop_unconfirmed_next_mission_never_calls_backend_execute(new_agent, save_case):
    async def exercise():
        driver = ControlledDriver("block", stop_ack=False)
        first_agent = agent_for(driver)
        first = await first_agent.run_request(mission("critical-A"), initial_state())
        assert first["stop_confirmation"]["confirmed"] is False
        next_agent = agent_for(driver) if new_agent else first_agent
        before = len(driver.calls)
        second = await next_agent.run_request(mission("critical-B"), initial_state())
        await asyncio.sleep(0)
        delta = len(driver.calls) - before
        save_case("critical_new_agent" if new_agent else "critical_same_agent", driver,
                  first, second, delta=delta, pending=len(next_agent.admission.pending))
        assert delta == 0, "Fault rejection must happen before every backend.execute, including stop"
        assert second["execution_status"] == "REJECTED" and second["reason"] == "robot_fault_latched"
        assert second["planner_calls"] == 0 and second["steps"] == 0
        assert second["robot_fault"]
        assert not next_agent.admission.pending
    asyncio.run(exercise())
