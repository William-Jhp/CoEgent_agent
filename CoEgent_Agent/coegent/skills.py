"""Mock-only drivers and the enforced skill dispatch boundary."""

import asyncio
from collections import defaultdict, deque
from copy import deepcopy
import json
from time import monotonic
from typing import Protocol

from jsonschema import Draft202012Validator

from coegent_contracts.semantics import apply_effects, has_physical_effect, valid_success_data

from .registry import SkillRegistry
from .safety import SafetyPolicy
from .types import Event, SkillResult, State
from .goals import Evidence
from .observations import required_paths


class SkillBackend(Protocol):
    async def execute(self, name: str, args: dict, snapshot: dict) -> SkillResult: ...


class DriverExecutionState:
    """Per-dispatch trusted driver evidence, never populated from a plan or result."""

    def __init__(self):
        self._physical_side_effect_started = None

    @property
    def physical_side_effect_started(self):
        return self._physical_side_effect_started

    def record_physical_side_effect(self, started: bool):
        if type(started) is not bool:
            self._physical_side_effect_started = True
            raise ValueError("physical side-effect report must be a bool")
        if self._physical_side_effect_started is not True:
            self._physical_side_effect_started = started


def valid_result(result, name, args, state):
    if (not isinstance(result, SkillResult) or result.skill != name
            or not isinstance(result.event, Event) or not isinstance(result.data, dict)
            or type(result.recoverable) is not bool or not isinstance(result.reason, str)):
        return False
    try:
        json.dumps(result.data, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return False
    return not result.ok or valid_success_data(name, args, state.snapshot(), result.data)


class MockSkills:
    """Injected failures are driver outcomes, never model-supplied state updates."""

    def __init__(self, outcomes=None, delay_s=0):
        self.outcomes = defaultdict(deque, {k: deque(v) for k, v in (outcomes or {}).items()})
        self.calls = []
        self.delay_s = delay_s

    async def execute(self, name, args, snapshot):
        self.calls.append({"name": name, "args": deepcopy(args)})
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        key = f"{name}:{args.get('target', '')}"
        queue = self.outcomes[key] or self.outcomes[name]
        if queue:
            item = queue.popleft()
            return SkillResult(name, Event(item.get("event", "SUCCESS")), item.get("reason", ""),
                               deepcopy(item.get("data", {})), item.get("recoverable", False))
        robot, world = snapshot["robot"], snapshot["world"]
        data = {}
        if name == "get_battery":
            data = {"battery_percent": robot.get("battery_percent")}
        elif name == "get_pose":
            data = {"location": robot.get("location")}
        elif name == "check_path":
            data = {"target": args["target"], "path_clear": world.get("paths", {}).get(args["target"]) == "clear"}
        elif name == "inspect_scene":
            data = {"objects": [k for k, v in world.get("targets", {}).items()
                                if v.get("exists") and v.get("location") == robot.get("location")]}
        elif name == "inspect_target":
            target = world.get("targets", {}).get(args["target"], {})
            exists = bool(target.get("exists")) and target.get("location") == robot.get("location")
            data = {"target": args["target"], "target_exists": exists, "location": robot.get("location"),
                    "valve_state": target.get("valve_state") if exists else None}
        elif name in {"navigate_to", "return_home"}:
            data = {"location": args["target"] if name == "navigate_to" else robot["home"]}
        elif name in {"close_valve", "open_valve"}:
            data = {"target": args["target"], "valve_state": "closed" if name == "close_valve" else "open"}
        elif name == "pick_object":
            data = {"holding": args["object"]}
        elif name == "place_object":
            data = {"holding": None}
        elif name == "localize":
            data = {"localization_valid": True}
        elif name == "dock_for_charge":
            data = {"charging": True}
        elif name == "stop":
            data = {"stopped": True}
        return SkillResult(name, data=data)


def update_state(state: State, result: SkillResult, args: dict, save_as=None):
    if result.ok:
        updated = apply_effects(state.snapshot(), result.skill, args, result.data, save_as)
        state.robot, state.world = updated["robot"], updated["world"]
        state.mission, state.observations = updated.get("mission", {}), updated["observations"]


class SkillRouter:
    def __init__(self, backend: SkillBackend, registry=None, policy=None, timeout_s: float = 5):
        self.backend = backend
        self.registry = registry or SkillRegistry()
        self.policy = policy or SafetyPolicy()
        self.timeout_s = timeout_s
        self.admission = None
        self.fresh = None
        self.checkpoint = None
        self.dispatch_check = None
        self.evidence = []
        self.active_skill = None

    async def dispatch(self, name, args, state, trace, save_as=None):
        if self.admission:
            self.admission.assert_owner_process()
        try:
            json.dumps(args, allow_nan=False)
            finite = True
        except (TypeError, ValueError, RecursionError):
            finite = False
        if name not in self.registry.skills:
            result = SkillResult(name, Event.SAFETY_ABORT, "unknown_skill")
        elif not finite or not Draft202012Validator(self.registry.args_schema(name)).is_valid(args):
            result = SkillResult(name, Event.SAFETY_ABORT, "invalid_arguments")
        else:
            if name != "stop":
                if self.checkpoint:
                    await self.checkpoint(name, args)
                if self.fresh:
                    await self.fresh.refresh(state, required_paths(name, args, state), "dispatch")
                    current = state.snapshot()
                    self.fresh.project_for_skill(current, name, args)
                    state.observations = current["observations"]
                if self.dispatch_check:
                    self.dispatch_check(name, args)
                if self.fresh:
                    self.fresh.check_current(state, required_paths(name, args, state))
            result = self.policy.check(name, args, state)
            if result is None:
                trace.emit("skill_start", skill=name, args=args)
                started = monotonic()
                self.active_skill = name
                contract = self.registry.definition(name)
                if self.fresh and self.fresh.config.state_mode == "strict" and "internal.invalidate_observations" in contract["effects"]:
                    self.fresh.invalidate_inspections()
                execution = DriverExecutionState()

                async def execute():
                    # Legacy drivers remain compatible, but their exception outcome is unknown.
                    tracked = getattr(self.backend, "execute_with_state", None)
                    if callable(tracked):
                        return await tracked(name, deepcopy(args), state.snapshot(), execution)
                    return await self.backend.execute(name, deepcopy(args), state.snapshot())

                task = asyncio.create_task(execute())
                if self.admission:
                    self.admission.track(task)
                try:
                    done, _ = await asyncio.wait({task}, timeout=self.timeout_s)
                    if not done:
                        task.cancel()
                        if self.admission:
                            self.admission.latch_fault("skill_timeout_unknown_outcome")
                        result = SkillResult(name, Event.SAFETY_ABORT, "skill_timeout_unknown_outcome")
                    else:
                        result = task.result()
                        if not valid_result(result, name, args, state):
                            result = SkillResult(name, Event.SAFETY_ABORT, "invalid_driver_result")
                        if result.ok:
                            self.evidence.append(Evidence(name, deepcopy(args), deepcopy(result.data), monotonic()))
                except asyncio.CancelledError:
                    task.cancel()
                    # Give cooperative drivers one turn to acknowledge cancellation.
                    await asyncio.sleep(0)
                    if not task.done() and self.admission:
                        self.admission.latch_fault("driver_cancellation_unconfirmed")
                    raise
                except Exception as exc:
                    read_only = not has_physical_effect(contract) and execution.physical_side_effect_started is False
                    if read_only:
                        result = SkillResult(name, Event.EXPECTED_FAILURE,
                                             f"driver_exception:{type(exc).__name__}", recoverable=True)
                    else:
                        if self.admission:
                            self.admission.latch_fault(f"driver_error:{type(exc).__name__}")
                        result = SkillResult(name, Event.SAFETY_ABORT, f"driver_error:{type(exc).__name__}")
                    trace.emit("driver_exception", skill=name, read_only=read_only,
                               physical_side_effect_started=execution.physical_side_effect_started,
                               event=result.event, reason=result.reason)
                finally:
                    self.active_skill = None
            else:
                trace.emit("safety_gate", skill=name, reason=result.reason, event=result.event)
        update_state(state, result, args, save_as)
        if result.ok and self.fresh:
            self.fresh.record(state, name, started, save_as)
        if name == "stop" and not result.ok and self.admission:
            self.admission.latch_fault(result.reason or "stop_unconfirmed")
        trace.emit("skill_result", **result.to_dict())
        return result
