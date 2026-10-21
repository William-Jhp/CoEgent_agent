import asyncio
from copy import deepcopy
from time import monotonic, time

import pytest

from coegent.agent import CoEgent
from coegent.planner import ScriptedPlanner
from coegent.scenarios import condition, initial_state, plan, skill
from coegent.skills import MockSkills
from coegent.types import RuntimeHalt
from coegent_contracts import RuntimeConfig


def request(nodes=None, *, identity="m1", revision=1, goals=(), ttl=30):
    from coegent.admission import MissionRequest
    return MissionRequest(identity, revision, time() + ttl, "approved mission", nodes, goals)


def strict_config(**kwargs):
    return RuntimeConfig(state_mode="strict", max_observation_age_s=1,
                         max_inspection_age_s=1, **kwargs)


class Provider:
    def __init__(self, state=None):
        self.state = state or initial_state()
        self.age = 0
        self.broken = False

    async def snapshot(self):
        from coegent.observations import ObservationSnapshot
        if self.broken:
            raise OSError("sensor offline")
        values = self.state.snapshot()
        values.pop("mission", None)
        values.pop("observations", None)
        def fields(value, prefix=""):
            result = {}
            for key, item in value.items():
                path = f"{prefix}.{key}" if prefix else key
                if isinstance(item, dict):
                    result.update(fields(item, path))
                else:
                    result[path] = monotonic() - self.age
            return result
        return ObservationSnapshot(values, fields(values))


def test_continuation_history_survives_multiple_revisions():
    async def exercise():
        backend = MockSkills()
        agent = CoEgent(None, backend)
        for revision, angle in [(1, 30), (2, 60)]:
            report = await agent.run_request(request(plan(skill("rotate", angle_deg=angle)),
                                                     revision=revision), initial_state())
            assert report["execution_status"] == "SUCCESS"
        report = await agent.run_request(request(plan(skill("rotate", angle_deg=30)), revision=3), initial_state())
        assert report["reason"] == "continuation_repeats_completed_skill"
        assert sum(c["name"] == "rotate" for c in backend.calls) == 2
    asyncio.run(exercise())


def test_replanner_cannot_repeat_completed_side_effect():
    async def exercise():
        backend = MockSkills()
        planner = ScriptedPlanner([plan(skill("rotate", angle_deg=30),
                    {"type": "replan_required", "reason": "route_changed", "constraints": ["preserve_completed"]}, outcome="replan_required"),
                                   plan(skill("rotate", angle_deg=30))])
        agent = CoEgent(planner, backend, config=RuntimeConfig(max_replans=1))
        report = await agent.run("go", initial_state())
        assert report["execution_status"] == "REPLAN_REQUIRED"
        assert report["reason"] == "continuation_repeats_completed_skill"
        assert sum(c["name"] == "rotate" for c in backend.calls) == 1
    asyncio.run(exercise())


def test_expiry_rechecked_after_connection_probe():
    async def exercise():
        class SlowConnection:
            async def connected(self):
                await asyncio.sleep(.03)
                return True
        backend = MockSkills()
        agent = CoEgent(None, backend, connection=SlowConnection(),
                        config=RuntimeConfig(remote_required=True, connection_poll_s=1))
        report = await agent.run_request(request(plan(skill("rotate", angle_deg=30)), ttl=.01), initial_state())
        assert report["reason"] == "request_expired"
        assert not any(c["name"] == "rotate" for c in backend.calls)
    asyncio.run(exercise())


def test_dispatch_does_not_wait_for_connection_after_refresh():
    async def exercise():
        class Sensor(Provider):
            count = 0
            last_stamp = 0
            async def snapshot(self):
                self.count += 1
                value = await super().snapshot()
                self.last_stamp = monotonic()
                return value
        provider = Sensor()
        class DelayedConnection:
            async def connected(self):
                if provider.count >= 5:
                    await asyncio.sleep(.04)
                    provider.state.robot["estop"] = True
                return True
        class Driver(MockSkills):
            reads = []
            async def execute(self, name, args, snapshot):
                if name == "rotate":
                    self.reads.append((monotonic() - provider.last_stamp, provider.state.robot["estop"]))
                return await super().execute(name, args, snapshot)
        backend = Driver()
        agent = CoEgent(None, backend, state_provider=provider, connection=DelayedConnection(),
                        config=RuntimeConfig(state_mode="strict", max_observation_age_s=.01,
                                             max_inspection_age_s=1, remote_required=True, connection_poll_s=1))
        assert await agent.confirm_safe_reset()
        await agent.run_request(request(plan(skill("rotate", angle_deg=30))), initial_state())
        assert all(age <= .01 and estop is False for age, estop in backend.reads)
    asyncio.run(exercise())


def test_expired_revision_history_cannot_repeat_completed_motion():
    async def exercise():
        from coegent.admission import RobotAdmission
        clock = {"utc": 100, "mono": 10}
        guard = RobotAdmission(utc_now=lambda: clock["utc"], monotonic_now=lambda: clock["mono"])
        backend = MockSkills()
        agent = CoEgent(None, backend, admission=guard)
        req = request(plan(skill("rotate", angle_deg=30)))
        from dataclasses import replace
        req = replace(req, expires_at=110)
        assert (await agent.run_request(req, initial_state()))["execution_status"] == "SUCCESS"
        clock.update(utc=120, mono=30)
        other = replace(req, mission_id="other", expires_at=200, plan=plan(skill("get_pose")))
        await agent.run_request(other, initial_state())
        report = await agent.run_request(replace(req, revision=2, expires_at=200), initial_state())
        assert report["execution_status"] == "REJECTED"
        assert report["reason"] == "revision_history_unavailable"
        assert sum(c["name"] == "rotate" for c in backend.calls) == 1
    asyncio.run(exercise())


def test_offline_continue_requires_an_existing_approved_plan():
    async def exercise():
        class Offline:
            async def connected(self):
                return False
        backend = MockSkills()
        agent = CoEgent(ScriptedPlanner([plan(skill("rotate", angle_deg=30))]), backend,
                        connection=Offline(), config=RuntimeConfig(remote_required=True,
                        offline_policy="continue", local_skills=frozenset({"rotate"})))
        report = await agent.run_request(request(), initial_state())
        assert report["execution_status"] == "HELD"
        assert report["planner_calls"] == 0
        assert not any(c["name"] == "rotate" for c in backend.calls)
    asyncio.run(exercise())


def test_stop_failure_blocks_new_agent():
    async def exercise():
        backend = MockSkills({"stop": [{"event": "EXPECTED_FAILURE", "reason": "no_ack"}]})
        first = CoEgent(ScriptedPlanner([plan({"type": "abort", "reason": "cancel",
                                             "return_home": False}, outcome="abort")]), backend)
        second = CoEgent(ScriptedPlanner([plan(skill("rotate", angle_deg=30))]), backend)
        failed = await first.run("cancel", initial_state())
        assert failed["stop_confirmation"]["confirmed"] is False
        denied = await second.run("rotate", initial_state())
        assert denied["reason"] == "robot_fault_latched"
        assert "rotate" not in [x["name"] for x in backend.calls]
    asyncio.run(exercise())


def test_two_agents_one_backend_cannot_overlap():
    async def exercise():
        backend = MockSkills(delay_s=0.03)
        a = CoEgent(ScriptedPlanner([plan(skill("rotate", angle_deg=30))]), backend)
        b = CoEgent(ScriptedPlanner([plan(skill("rotate", angle_deg=-30))]), backend)
        reports = await asyncio.gather(a.run("a", initial_state()), b.run("b", initial_state()))
        assert sum(r["execution_status"] == "SUCCESS" for r in reports) == 1
        assert sum(c["name"] == "rotate" for c in backend.calls) == 1
    asyncio.run(exercise())


def test_request_duplicate_and_payload_conflict():
    async def exercise():
        backend = MockSkills()
        agent = CoEgent(None, backend)
        req = request(plan(skill("rotate", angle_deg=30)))
        report = await agent.run_request(req, initial_state())
        replay = await agent.run_request(req, initial_state())
        assert replay["execution_status"] == report["execution_status"] == "SUCCESS"
        assert replay["replayed"] is True
        conflict = request(plan(skill("rotate", angle_deg=60)))
        assert (await agent.run_request(conflict, initial_state()))["reason"] == "request_payload_conflict"
        assert len(backend.calls) == 1
    asyncio.run(exercise())


def test_provided_plan_no_planner_and_goal_unmet():
    async def exercise():
        from coegent.goals import Goal
        agent = CoEgent(None, MockSkills())
        report = await agent.run_request(request(plan(skill("navigate_to", target="A")),
                                                goals=(Goal("at_location", "HOME"),)), initial_state())
        assert report["planner_calls"] == 0
        assert report["execution_status"] == "SUCCESS"
        assert report["goal_status"] == "unmet" and report["mission_success"] is False
    asyncio.run(exercise())


def test_goal_inspection_evidence_survives_return():
    async def exercise():
        from coegent.goals import Goal
        agent = CoEgent(None, MockSkills())
        report = await agent.run_request(request(plan(skill("navigate_to", target="A"),
                skill("inspect_target", target="red_valve"), skill("return_home")),
                goals=(Goal("inspected_target", "red_valve"), Goal("at_location", "HOME"))), initial_state())
        assert report["goal_status"] == "achieved" and report["mission_success"] is True
        assert "inspection" not in report["state"]["observations"]
    asyncio.run(exercise())


def test_preflight_uses_replan_budget():
    async def exercise():
        state = initial_state()
        state.world["paths"]["A"] = "blocked"
        planner = ScriptedPlanner([plan(skill("navigate_to", target="A")),
                                   plan(skill("navigate_to", target="B"))])
        backend = MockSkills()
        report = await CoEgent(planner, backend).run("reroute", state)
        assert report["replans"] == 1 and report["planner_calls"] == 2
        assert report["execution_status"] == "SUCCESS"
        assert backend.calls[-1]["args"] == {"target": "B"}
    asyncio.run(exercise())


@pytest.mark.parametrize("field,value", [("estop", True), ("battery_percent", 1)])
def test_live_state_after_planner_wait_blocks_motion(field, value):
    async def exercise():
        provider = Provider()
        class Planner:
            async def plan(self, context):
                provider.state.robot[field] = value
                return plan(skill("rotate", angle_deg=30), skill("navigate_to", target="A"))
        backend = MockSkills()
        agent = CoEgent(Planner(), backend, state_provider=provider, config=strict_config())
        assert await agent.confirm_safe_reset()
        report = await agent.run("go", initial_state())
        assert report["execution_status"] == "SAFETY_ABORT"
        assert "navigate_to" not in [c["name"] for c in backend.calls]
        if field == "estop":
            assert "rotate" not in [c["name"] for c in backend.calls]
    asyncio.run(exercise())


def test_strict_requires_provider_and_safe_startup():
    with pytest.raises(ValueError):
        CoEgent(None, MockSkills(), config=strict_config())
    async def exercise():
        provider = Provider()
        agent = CoEgent(None, MockSkills(), config=strict_config(), state_provider=provider)
        report = await agent.run_request(request(plan(skill("rotate", angle_deg=10))), initial_state())
        assert report["reason"] == "robot_fault_latched"
        assert await agent.confirm_safe_reset()
        provider.age = 5
        report = await agent.run_request(request(plan(skill("rotate", angle_deg=10)), identity="fresh"), initial_state())
        assert report["execution_status"] == "SAFETY_ABORT"
        assert report["stop_confirmation"]["confirmed"] is True
    asyncio.run(exercise())


def test_fault_reset_requires_trusted_stopped_state():
    async def exercise():
        provider = Provider()
        agent = CoEgent(None, MockSkills(), config=strict_config(), state_provider=provider)
        provider.state.robot["stopped"] = False
        assert not await agent.confirm_safe_reset()
        assert agent.admission.fault_reason
        provider.state.robot["stopped"] = True
        assert await agent.confirm_safe_reset()
        assert agent.admission.fault_reason is None
    asyncio.run(exercise())


def test_stop_bypasses_failed_provider():
    async def exercise():
        provider = Provider()
        backend = MockSkills()
        agent = CoEgent(None, backend, config=strict_config(), state_provider=provider)
        assert await agent.confirm_safe_reset()
        provider.broken = True
        report = await agent.run_request(request(plan(skill("rotate", angle_deg=5))), initial_state())
        assert report["execution_status"] == "SAFETY_ABORT"
        assert report["stop_confirmation"]["confirmed"] is True
        assert backend.calls[-1]["name"] == "stop"
    asyncio.run(exercise())


def test_condition_refresh_uses_provider_not_caller():
    async def exercise():
        provider = Provider()
        provider.state.robot["battery_percent"] = 8
        agent = CoEgent(None, MockSkills(), config=strict_config(), state_provider=provider)
        assert await agent.confirm_safe_reset()
        p = plan(condition("robot.battery_percent", 8, [skill("log_event", message="low")],
                           [skill("navigate_to", target="A")]))
        report = await agent.run_request(request(p), initial_state())
        assert report["execution_status"] == "SUCCESS"
        assert report["completed_steps"] == [{"name": "log_event", "args": {"message": "low"}}]
    asyncio.run(exercise())


class Connection:
    def __init__(self):
        self.online = True

    async def connected(self):
        return self.online


@pytest.mark.parametrize("policy", ["hold", "continue", "return_home"])
def test_offline_policy_is_deployment_owned(policy):
    async def exercise():
        connection = Connection()
        connection.online = False
        backend = MockSkills()
        cfg = RuntimeConfig(remote_required=True, offline_policy=policy,
                            local_skills=frozenset({"rotate", "return_home"}))
        agent = CoEgent(None, backend, connection=connection, config=cfg)
        report = await agent.run_request(request(plan(skill("rotate", angle_deg=30))), initial_state())
        if policy == "continue":
            assert report["execution_status"] == "SUCCESS"
            assert [c["name"] for c in backend.calls] == ["rotate"]
        else:
            assert report["execution_status"] == "HELD"
            assert "rotate" not in [c["name"] for c in backend.calls]
            assert ("return_home" in [c["name"] for c in backend.calls]) == (policy == "return_home")
    asyncio.run(exercise())


def test_disconnect_cancels_active_skill_and_no_auto_resume():
    async def exercise():
        connection = Connection()
        started = asyncio.Event()
        class Backend(MockSkills):
            async def execute(self, name, args, snapshot):
                if name == "rotate":
                    started.set()
                    await asyncio.sleep(10)
                return await super().execute(name, args, snapshot)
        backend = Backend()
        agent = CoEgent(None, backend, connection=connection,
                       config=RuntimeConfig(remote_required=True, connection_poll_s=0.002))
        req = request(plan(skill("rotate", angle_deg=30), skill("navigate_to", target="A")))
        task = asyncio.create_task(agent.run_request(req, initial_state()))
        await started.wait()
        connection.online = False
        report = await asyncio.wait_for(task, 0.5)
        assert report["execution_status"] == "HELD"
        assert report["stop_confirmation"]["confirmed"] is True
        assert len(report["remaining_plan"]) == 2
        assert report["remaining_plan"][-1] == skill("navigate_to", target="A")
        connection.online = True
        replay = await agent.run_request(req, initial_state())
        assert replay["replayed"] is True
        assert "navigate_to" not in [c["name"] for c in backend.calls]
        assert not agent.admission.pending
    asyncio.run(exercise())


def test_cancel_resistant_driver_keeps_fault_and_blocks_reset():
    async def exercise():
        release = asyncio.Event()
        class Backend(MockSkills):
            async def execute(self, name, args, snapshot):
                if name == "rotate":
                    try:
                        await asyncio.sleep(10)
                    except asyncio.CancelledError:
                        await release.wait()
                return await super().execute(name, args, snapshot)
        provider = Provider()
        backend = Backend()
        agent = CoEgent(None, backend, state_provider=provider,
                       config=strict_config(skill_timeout_s=0.01))
        assert await agent.confirm_safe_reset()
        report = await asyncio.wait_for(agent.run_request(request(plan(skill("rotate", angle_deg=3))), initial_state()), 0.5)
        assert report["execution_status"] == "SAFETY_ABORT"
        assert agent.admission.fault_reason and agent.admission.pending
        assert not await agent.confirm_safe_reset()
        release.set()
        await asyncio.sleep(0.02)
        assert await agent.confirm_safe_reset()
    asyncio.run(exercise())


def test_late_planner_after_expiry_never_dispatches():
    async def exercise():
        class Planner:
            async def plan(self, context):
                try:
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    await asyncio.sleep(0.03)
                return plan(skill("rotate", angle_deg=30))
        backend = MockSkills()
        agent = CoEgent(Planner(), backend, config=RuntimeConfig(connection_poll_s=0.002))
        report = await asyncio.wait_for(agent.run_request(request(ttl=0.015), initial_state()), 0.5)
        await asyncio.sleep(0.05)
        assert report["reason"] == "request_expired"
        assert "rotate" not in [c["name"] for c in backend.calls]
    asyncio.run(exercise())


def test_provider_cannot_overwrite_mission_or_inspection():
    async def exercise():
        from coegent.observations import FreshState, ObservationSnapshot
        class Bad:
            async def snapshot(self):
                return ObservationSnapshot({"mission": {"completed_count": 99}}, {})
        with pytest.raises(RuntimeHalt):
            await FreshState(Bad(), strict_config()).refresh(initial_state(), (), "admission")
    asyncio.run(exercise())


def test_stale_inspection_not_refreshed_by_fresh_battery():
    async def exercise():
        from coegent.observations import FreshState
        state = initial_state()
        state.robot["location"] = "A"
        state.observations["inspection"] = {"target": "red_valve", "target_exists": True, "location": "A"}
        provider = Provider(deepcopy(state))
        fresh = FreshState(provider, strict_config())
        await fresh.refresh(state, ("robot.battery_percent",), "dispatch")
        assert "inspection" not in state.observations
    asyncio.run(exercise())


def test_stop_skill_failure_is_not_hidden_by_second_ack():
    async def exercise():
        backend = MockSkills({"stop": [{"event": "EXPECTED_FAILURE", "reason": "no_ack"}]})
        report = await CoEgent(None, backend).run_request(request(plan(skill("stop"))), initial_state())
        assert report["execution_status"] == "SAFETY_ABORT"
        assert report["stop_confirmation"]["confirmed"] is False
        assert len(backend.calls) == 1
    asyncio.run(exercise())


def test_partial_provider_does_not_leak_caller_safety_values():
    async def exercise():
        from coegent.observations import FreshState, ObservationSnapshot
        class Partial:
            async def snapshot(self):
                return ObservationSnapshot({"robot": {"battery_percent": 80}},
                                           {"robot.battery_percent": monotonic()})
        state = initial_state()
        await FreshState(Partial(), strict_config()).refresh(state, ("robot.battery_percent",), "planning")
        assert state.robot.get("estop") is None
        assert not state.world.get("paths")
    asyncio.run(exercise())


def test_cancel_during_final_goal_read_releases_owner_safely():
    async def exercise():
        from coegent.goals import Goal
        finished = asyncio.Event()
        class Sensor(Provider):
            async def snapshot(self):
                if finished.is_set():
                    await asyncio.sleep(10)
                return await super().snapshot()
        class Backend(MockSkills):
            async def execute(self, name, args, snapshot):
                result = await super().execute(name, args, snapshot)
                if name == "get_pose":
                    finished.set()
                return result
        agent = CoEgent(None, Backend(), state_provider=Sensor(), config=strict_config())
        assert await agent.confirm_safe_reset()
        task = asyncio.create_task(agent.run_request(request(plan(skill("get_pose")),
                                  goals=(Goal("at_location", "HOME"),)), initial_state()))
        await finished.wait()
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not agent._busy
        assert agent.last_report["execution_status"] == "CANCELLED"
        assert agent.last_report["stop_confirmation"]["confirmed"] is True
        assert agent.last_report["completed_steps"] == [{"name": "get_pose", "args": {}}]
    asyncio.run(exercise())


@pytest.mark.parametrize("age", [5, -5, float("nan")])
def test_invalid_sensor_timestamp_blocks_dispatch(age):
    async def exercise():
        from coegent.observations import FreshState
        provider = Provider()
        provider.age = age
        with pytest.raises(RuntimeHalt):
            await FreshState(provider, strict_config()).refresh(initial_state(), ("robot.estop",), "dispatch")
    asyncio.run(exercise())


def test_inspection_expires_in_same_location():
    async def exercise():
        provider = Provider()
        provider.state.robot["location"] = "A"
        class Backend(MockSkills):
            async def execute(self, name, args, snapshot):
                if name == "log_event":
                    await asyncio.sleep(0.02)
                return await super().execute(name, args, snapshot)
        backend = Backend()
        cfg = RuntimeConfig(state_mode="strict", max_observation_age_s=1, max_inspection_age_s=0.005,
                            max_replans=0)
        agent = CoEgent(None, backend, state_provider=provider, config=cfg)
        assert await agent.confirm_safe_reset()
        report = await agent.run_request(request(plan(skill("inspect_target", target="red_valve"),
            skill("log_event", message="delay"), skill("close_valve", target="red_valve"))), initial_state())
        assert report["execution_status"] == "REPLAN_REQUIRED"
        assert "close_valve" not in [c["name"] for c in backend.calls]
    asyncio.run(exercise())


def test_offline_return_never_bypasses_blocked_home():
    async def exercise():
        connection = Connection()
        connection.online = False
        state = initial_state()
        state.world["paths"]["HOME"] = "blocked"
        backend = MockSkills()
        agent = CoEgent(None, backend, connection=connection,
                       config=RuntimeConfig(remote_required=True, offline_policy="return_home"))
        report = await agent.run_request(request(plan(skill("get_pose"))), state)
        assert report["execution_status"] == "HELD"
        assert report["reason"].startswith("offline_return_failed")
        assert "return_home" not in [c["name"] for c in backend.calls]
    asyncio.run(exercise())


def test_expiry_rechecked_after_sensor_wait():
    async def exercise():
        class Sensor(Provider):
            async def snapshot(self):
                self.reads = getattr(self, "reads", 0) + 1
                if self.reads >= 5:
                    await asyncio.sleep(0.04)
                return await super().snapshot()
        backend = MockSkills()
        agent = CoEgent(None, backend, state_provider=Sensor(), config=strict_config(connection_poll_s=1))
        assert await agent.confirm_safe_reset()
        report = await agent.run_request(request(plan(skill("rotate", angle_deg=30)), ttl=0.02), initial_state())
        assert report["reason"] == "request_expired"
        assert "rotate" not in [c["name"] for c in backend.calls]
    asyncio.run(exercise())


def test_reset_reobserves_after_ack():
    async def exercise():
        provider = Provider()
        class Backend(MockSkills):
            async def execute(self, name, args, snapshot):
                if name == "stop":
                    provider.state.robot["estop"] = True
                    await asyncio.sleep(0.02)
                return await super().execute(name, args, snapshot)
        agent = CoEgent(None, Backend(), state_provider=provider, config=strict_config())
        assert not await agent.confirm_safe_reset()
        assert agent.admission.fault_reason
    asyncio.run(exercise())


def test_revision_cannot_repeat_completed_motion():
    async def exercise():
        backend = MockSkills()
        agent = CoEgent(None, backend)
        p = plan(skill("rotate", angle_deg=30))
        assert (await agent.run_request(request(p), initial_state()))["status"] == "SUCCESS"
        report = await agent.run_request(request(p, revision=2), initial_state())
        assert report["execution_status"] == "REPLAN_REQUIRED"
        assert report["reason"] == "continuation_repeats_completed_skill"
        assert sum(c["name"] == "rotate" for c in backend.calls) == 1
    asyncio.run(exercise())


def test_offline_return_stays_under_mission_deadline():
    async def exercise():
        connection = Connection()
        connection.online = False
        completed_return = []
        class Backend(MockSkills):
            async def execute(self, name, args, snapshot):
                if name == "return_home":
                    await asyncio.sleep(0.08)
                    completed_return.append(True)
                return await super().execute(name, args, snapshot)
        backend = Backend()
        agent = CoEgent(None, backend, connection=connection,
                config=RuntimeConfig(remote_required=True, offline_policy="return_home", mission_timeout_s=0.025))
        report = await agent.run_request(request(plan(skill("get_pose")), ttl=0.03), initial_state())
        assert report["execution_status"] == "SAFETY_ABORT"
        assert "mission_timeout" in report["reason"]
        assert not completed_return
    asyncio.run(exercise())
