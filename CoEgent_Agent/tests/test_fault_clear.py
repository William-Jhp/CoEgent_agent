"""Fault-clear and startup acceptance probes against the real executive boundary."""

import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
from textwrap import dedent
from time import monotonic, time

import pytest

from coegent import CoEgent, MissionRequest
from coegent.observations import ObservationSnapshot, leaves
from coegent.scenarios import initial_state, plan, skill
from coegent.skills import MockSkills
from coegent.types import Event, SkillResult
from coegent_contracts import RuntimeConfig


class Sensor:
    def __init__(self):
        self.state = initial_state()
        self.reads = []
        self.phase = "before"
        self.bad_phase = "before"
        self.bad = None

    async def snapshot(self):
        if self.phase == self.bad_phase and self.bad == "provider_error":
            raise OSError("trusted_sensor_unavailable")
        values = {k: v for k, v in self.state.snapshot().items() if k in {"robot", "world"}}
        stamps = {p: monotonic() for p in leaves(values)}
        if self.phase == self.bad_phase:
            if self.bad == "not_stopped":
                values["robot"]["stopped"] = False
            elif self.bad == "estop":
                values["robot"]["estop"] = True
            elif self.bad == "stopped_unknown":
                values["robot"].pop("stopped", None)
            elif self.bad == "estop_unknown":
                values["robot"].pop("estop", None)
            elif self.bad == "stale":
                stamps["robot.stopped"] = monotonic() - 5
            elif self.bad == "unstamped":
                stamps.pop("robot.stopped", None)
        self.reads.append({"phase": self.phase, "values": deepcopy(values), "observed_at": stamps})
        return ObservationSnapshot(values, stamps)


class Driver(MockSkills):
    def __init__(self, sensor):
        super().__init__()
        self.sensor = sensor
        self.ack = "success"
        self.block_rotate = False
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.release = asyncio.Event()
        self.stop_started = asyncio.Event()
        self.stop_release = asyncio.Event()

    async def execute(self, name, args, snapshot):
        if name == "rotate" and self.block_rotate:
            self.calls.append({"name": name, "args": deepcopy(args)})
            self.sensor.state.robot["stopped"] = False
            self.started.set()
            try:
                while not self.release.is_set():
                    try:
                        await self.release.wait()
                    except asyncio.CancelledError:
                        continue
                return SkillResult(name)
            finally:
                self.finished.set()
        if name == "stop":
            self.calls.append({"name": name, "args": deepcopy(args)})
            self.stop_started.set()
            if self.ack == "blocked":
                await self.stop_release.wait()
            if self.ack == "failure":
                return SkillResult(name, Event.EXPECTED_FAILURE, "stop_not_acknowledged")
            if self.ack == "exception":
                raise OSError("stop_driver_disconnected")
            if self.ack == "invalid":
                return SkillResult(name, data={"stopped": False})
            self.sensor.state.robot["stopped"] = True
            self.sensor.phase = "after"
            return SkillResult(name, data={"stopped": True})
        return await super().execute(name, args, snapshot)


def executive(sensor, driver):
    return CoEgent(None, driver, state_provider=sensor, config=RuntimeConfig(
        state_mode="strict", max_observation_age_s=1, max_inspection_age_s=1,
        max_replans=0, max_repairs=0, skill_timeout_s=.04, mission_timeout_s=.25))


def request(identity, name="rotate"):
    node = skill(name, angle_deg=10) if name == "rotate" else skill(name)
    return MissionRequest(identity, 1, time() + 10, "approved fault-clear probe", plan(node))


@pytest.fixture
def record(tmp_path):
    directory = Path(os.environ.get("COEGENT_FAULT_CLEAR_OUTPUT", str(tmp_path)))
    directory.mkdir(parents=True, exist_ok=True)

    def save(name, **value):
        (directory / f"{name}.json").write_text(json.dumps(
            {"scope": "CPU mock, not physical stop evidence", **value},
            ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    return save


def test_active_driver_prevents_clear_even_with_safe_observation_and_prior_ack(record):
    async def exercise():
        sensor = Sensor()
        driver = Driver(sensor)
        agent = executive(sensor, driver)
        assert await agent.confirm_safe_reset()
        driver.block_rotate = True
        task = asyncio.create_task(agent.run_request(request("active-A"), initial_state()))
        try:
            await asyncio.wait_for(driver.started.wait(), timeout=1)
            first = await asyncio.wait_for(task, timeout=1)
            fault = agent.admission.fault_reason
            assert fault and agent.admission.pending
            assert first["stop_confirmation"]["confirmed"] is True
            assert sensor.state.robot["stopped"] is True and sensor.state.robot["estop"] is False
            before_calls, before_reads = len(driver.calls), len(sensor.reads)
            cleared = await agent.confirm_safe_reset()
            second = await agent.run_request(request("active-B"), initial_state())
            delta = len(driver.calls) - before_calls
            record("active_driver_blocks_clear", cleared=cleared, fault_before=fault,
                   fault_after=agent.admission.fault_reason, pending=len(agent.admission.pending),
                   reset_sensor_read_delta=len(sensor.reads) - before_reads,
                   backend_execute_delta=delta, mission_a=first, mission_b=second,
                   backend_calls=driver.calls, sensor_state=sensor.state.snapshot())
            assert cleared is False and agent.admission.fault_reason == fault
            assert delta == 0 and len(sensor.reads) == before_reads
            assert second["execution_status"] == "REJECTED"
            driver.release.set()
            await asyncio.wait_for(driver.finished.wait(), timeout=1)
            await asyncio.sleep(0)
            assert not agent.admission.pending
            assert agent.admission.fault_reason == fault, "Task completion alone must not clear the fault"
            assert await agent.confirm_safe_reset()
            assert agent.admission.fault_reason is None
            third = await agent.run_request(request("active-C", "get_pose"), initial_state())
            record("active_driver_terminated_then_confirmed", fault_after=agent.admission.fault_reason,
                   pending=len(agent.admission.pending), mission_c=third, backend_calls=driver.calls,
                   observations=sensor.reads)
            assert third["execution_status"] == "SUCCESS"
        finally:
            driver.release.set()
            await asyncio.wait_for(task, timeout=1)
            await asyncio.wait_for(driver.finished.wait(), timeout=1)
            await asyncio.sleep(0)
    asyncio.run(exercise())


@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize("bad", ["not_stopped", "estop", "stopped_unknown", "estop_unknown",
                                 "stale", "unstamped", "provider_error"])
def test_clear_requires_fresh_safe_observation_before_and_after_ack(phase, bad, record):
    async def exercise():
        sensor = Sensor()
        driver = Driver(sensor)
        agent = executive(sensor, driver)
        agent.admission.latch_fault("prior_stop_unconfirmed")
        fault = agent.admission.fault_reason
        sensor.bad_phase, sensor.bad = phase, bad
        cleared = await agent.confirm_safe_reset()
        before = len(driver.calls)
        next_mission = await agent.run_request(request(f"unsafe-{phase}-{bad}"), initial_state())
        record(f"observation_{phase}_{bad}", cleared=cleared, fault_before=fault,
               fault_after=agent.admission.fault_reason, backend_calls=driver.calls,
               mission_backend_execute_delta=len(driver.calls) - before, mission=next_mission,
               observations=sensor.reads)
        assert cleared is False and agent.admission.fault_reason == fault
        assert next_mission["execution_status"] == "REJECTED" and len(driver.calls) == before
        assert [c["name"] for c in driver.calls] == ([] if phase == "before" else ["stop"])
        assert not agent.admission.pending
    asyncio.run(exercise())


@pytest.mark.parametrize("ack", ["failure", "exception", "invalid", "blocked"])
def test_safe_sensor_without_new_stop_ack_does_not_clear(ack, record):
    async def exercise():
        sensor = Sensor()
        driver = Driver(sensor)
        agent = executive(sensor, driver)
        driver.ack = ack
        fault = agent.admission.fault_reason
        try:
            cleared = await asyncio.wait_for(agent.confirm_safe_reset(), timeout=1)
            await asyncio.sleep(0)
            before = len(driver.calls)
            next_mission = await agent.run_request(request(f"ack-{ack}"), initial_state())
            record(f"new_ack_{ack}", cleared=cleared, fault_before=fault,
                   fault_after=agent.admission.fault_reason, mission=next_mission,
                   mission_backend_execute_delta=len(driver.calls) - before,
                   backend_calls=driver.calls, observations=sensor.reads)
            assert cleared is False and agent.admission.fault_reason == fault
            assert next_mission["execution_status"] == "REJECTED" and len(driver.calls) == before
            assert [c["name"] for c in driver.calls] == ["stop"]
            assert not agent.admission.pending
        finally:
            driver.stop_release.set()
    asyncio.run(exercise())


def test_real_stop_fault_clears_only_after_new_confirmation_and_task_completion(record):
    async def exercise():
        sensor = Sensor()
        driver = Driver(sensor)
        agent = executive(sensor, driver)
        assert await agent.confirm_safe_reset()
        driver.ack = "failure"
        fault_mission = await agent.run_request(request("faulting", "stop"), initial_state())
        assert fault_mission["stop_confirmation"]["confirmed"] is False
        assert agent.admission.fault_reason == "stop_not_acknowledged"
        denied = await agent.run_request(request("before-clear"), initial_state())
        driver.ack = "success"
        sensor.phase = "before"
        before = len(driver.calls)
        cleared = await agent.confirm_safe_reset()
        confirmation_calls = driver.calls[before:]
        assert not agent.admission.pending
        assert cleared is True and agent.admission.fault_reason is None
        assert confirmation_calls == [{"name": "stop", "args": {}}]
        admitted = await agent.run_request(request("after-clear"), initial_state())
        record("all_conditions_clear_real_fault", cleared=cleared, fault_mission=fault_mission,
               denied_mission=denied, admitted_mission=admitted, new_ack_calls=confirmation_calls,
               pending=len(agent.admission.pending), fault_after=agent.admission.fault_reason,
               observations=sensor.reads, backend_calls=driver.calls)
        assert denied["execution_status"] == "REJECTED"
        assert admitted["execution_status"] == "SUCCESS"
        assert admitted["completed_steps"] == [{"name": "rotate", "args": {"angle_deg": 10}}]
    asyncio.run(exercise())


def test_snapshot_without_trusted_provider_cannot_clear(record):
    async def exercise():
        driver = MockSkills()
        agent = CoEgent(None, driver)
        agent.admission.latch_fault("unconfirmed_stop")
        cleared = await agent.confirm_safe_reset()
        denied = await agent.run_request(request("snapshot-clear"), initial_state())
        record("snapshot_not_trusted_for_clear", cleared=cleared, fault=agent.admission.fault_reason,
               mission=denied, backend_calls=driver.calls)
        assert cleared is False and agent.admission.fault_reason == "unconfirmed_stop"
        assert denied["execution_status"] == "REJECTED" and driver.calls == []
    asyncio.run(exercise())


def test_cancelled_clear_keeps_fault_and_does_not_unlock_mission(record):
    async def exercise():
        sensor = Sensor()
        driver = Driver(sensor)
        driver.ack = "blocked"
        agent = executive(sensor, driver)
        fault = agent.admission.fault_reason
        task = asyncio.create_task(agent.confirm_safe_reset())
        try:
            await asyncio.wait_for(driver.stop_started.wait(), timeout=1)
            during = await agent.run_request(request("during-clear"), initial_state())
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0)
            before = len(driver.calls)
            after = await agent.run_request(request("cancelled-clear"), initial_state())
            record("cancelled_clear", fault_before=fault, fault_after=agent.admission.fault_reason,
                   during=during, after=after, backend_calls=driver.calls,
                   mission_backend_execute_delta=len(driver.calls) - before,
                   pending=len(agent.admission.pending))
            assert agent.admission.fault_reason == fault
            assert during["execution_status"] == after["execution_status"] == "REJECTED"
            assert len(driver.calls) == before and not agent.admission.pending
            driver.ack = "success"
            assert await agent.confirm_safe_reset(), "Cancelled management must release its management lease"
        finally:
            driver.stop_release.set()
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
    asyncio.run(exercise())


def test_new_process_startup_rejects_until_safe_confirmation_even_after_restart(tmp_path, record):
    code = dedent("""
        import asyncio,json,sys
        from time import monotonic,time
        from coegent import CoEgent,MissionRequest,RobotAdmission
        from coegent.observations import ObservationSnapshot,leaves
        from coegent.scenarios import initial_state,plan,skill
        from coegent.skills import MockSkills
        from coegent_contracts import RuntimeConfig
        class Sensor:
            async def snapshot(self):
                values={k:v for k,v in initial_state().snapshot().items() if k in {'robot','world'}}
                return ObservationSnapshot(values,{p:monotonic() for p in leaves(values)})
        async def main():
            backend=MockSkills()
            guard=RobotAdmission.for_backend(backend,robot_id='fault-clear-process-probe',lock_dir=sys.argv[1])
            agent=CoEgent(None,backend,admission=guard,state_provider=Sensor(),config=RuntimeConfig(
                state_mode='strict',max_observation_age_s=1,max_inspection_age_s=1))
            def request(identity):
                return MissionRequest(identity,1,time()+10,'approved startup mission',plan(skill('rotate',angle_deg=10)))
            fault_before=guard.fault_reason
            denied=await agent.run_request(request('startup'),initial_state())
            calls_before=len(backend.calls)
            cleared=await agent.confirm_safe_reset()
            admitted=await agent.run_request(request('ready'),initial_state())
            print(json.dumps({'pid':guard.owner_pid,'fault_before':fault_before,'denied':denied,
                'startup_backend_calls':calls_before,'cleared':cleared,'fault_after':guard.fault_reason,
                'admitted':admitted,'backend_calls':backend.calls,'pending':len(guard.pending)}))
            guard.close()
        asyncio.run(main())
    """)
    workspace = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": str(workspace / "CoEgent_Agent")}
    runs = []
    for _ in range(2):
        result = subprocess.run([sys.executable, "-c", code, str(tmp_path / "locks")],
                                cwd=workspace, env=env, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        runs.append(json.loads(result.stdout))
    record("process_startup_and_restart", runs=runs)
    for row in runs:
        assert row["fault_before"] == "startup_safety_unconfirmed"
        assert row["denied"]["execution_status"] == "REJECTED" and row["startup_backend_calls"] == 0
        assert row["cleared"] is True and row["fault_after"] is None
        assert row["admitted"]["execution_status"] == "SUCCESS" and row["pending"] == 0
        assert [c["name"] for c in row["backend_calls"]] == ["stop", "rotate"]
    assert runs[0]["pid"] != runs[1]["pid"]
