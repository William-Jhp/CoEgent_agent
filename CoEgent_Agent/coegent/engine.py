"""Bounded mission-plan interpreter; no model calls inside control flow."""

import asyncio
from time import monotonic

from coegent_contracts.state import StateContract

from .types import Event, Limits, RuntimeHalt, SkillResult, State, Trace


class MissionEngine:
    def __init__(self, router, state: State, trace: Trace, limits: Limits):
        self.router, self.state, self.trace, self.limits = router, state, trace, limits
        self.steps = 0
        self.deadline = monotonic() + limits.mission_timeout_s
        self.completed = []
        self.failed = None
        self.remaining = []
        self.stop_confirmation = None

    async def stop(self):
        if self.stop_confirmation and self.stop_confirmation["confirmed"] is False:
            return SkillResult("stop", Event.SAFETY_ABORT, self.stop_confirmation["reason"])
        task = asyncio.create_task(self.router.dispatch("stop", {}, self.state, self.trace))

        def consume(done):
            if not done.cancelled():
                done.exception()

        task.add_done_callback(consume)
        try:
            done, _ = await asyncio.wait({task}, timeout=self.limits.skill_timeout_s)
            if done:
                result = (SkillResult("stop", Event.SAFETY_ABORT, "stop_driver_cancelled")
                          if task.cancelled() else task.result())
            else:
                task.cancel()
                result = SkillResult("stop", Event.SAFETY_ABORT, "stop_timeout")
        except asyncio.CancelledError:
            task.cancel()
            raise
        except Exception as exc:
            result = SkillResult("stop", Event.SAFETY_ABORT, f"stop_error:{type(exc).__name__}")
        self.stop_confirmation = {"confirmed": result.ok, "reason": result.reason}
        if not result.ok and self.router.admission:
            self.router.admission.latch_fault(result.reason or "stop_unconfirmed")
        self.trace.emit("stop_confirmation", confirmed=result.ok, reason=result.reason)
        return result

    def budget(self):
        self.steps += 1
        if self.steps > self.limits.max_steps:
            raise RuntimeHalt("SAFETY_ABORT", "step_budget_exhausted")
        if monotonic() >= self.deadline:
            raise RuntimeHalt("SAFETY_ABORT", "mission_timeout")

    async def run(self, plan):
        self.remaining = plan["sequence"]
        result = await self.sequence(plan["sequence"])
        if result.ok and plan["final_outcome"] != "execute":
            raise RuntimeHalt("INVALID_PLAN", "terminal_outcome_not_executed")
        return result

    async def sequence(self, nodes):
        for index, node in enumerate(nodes):
            try:
                result = await self.node(node)
            except (RuntimeHalt, asyncio.CancelledError):
                self.remaining.extend(nodes[index + 1:])
                raise
            if not result.ok:
                self.remaining.extend(nodes[index + 1:])
                return result
        self.remaining = []
        return SkillResult("sequence")

    def condition(self, predicate):
        try:
            return StateContract.condition(self.state.snapshot(), predicate)
        except (KeyError, TypeError, ValueError):
            raise RuntimeHalt("REPLAN_REQUIRED", f"unknown_or_invalid_condition:{predicate['source']}") from None

    async def node(self, node):
        self.budget()
        self.remaining = [node]
        kind = node["type"]
        self.trace.emit("node", type=kind)
        if kind == "skill":
            result = await self.router.dispatch(node["name"], node["args"], self.state,
                                                self.trace, node.get("save_as"))
            if node["name"] == "stop":
                self.stop_confirmation = {"confirmed": result.ok, "reason": result.reason}
                self.trace.emit("stop_confirmation", **self.stop_confirmation)
                if not result.ok:
                    raise RuntimeHalt("SAFETY_ABORT", "stop_failed")
            if result.ok:
                self.completed.append({"name": node["name"], "args": node["args"]})
                self.state.mission["completed_count"] = len(self.completed)
            else:
                self.failed = {"node": node, "result": result.to_dict()}
            return result
        if kind == "sequence":
            return await self.sequence(node["nodes"])
        if kind == "condition":
            if self.router.checkpoint:
                await self.router.checkpoint(None, None)
            if self.router.fresh:
                source = node["condition"]["source"]
                await self.router.fresh.refresh(self.state, (source,), "condition")
                self.router.fresh.check_current(self.state, (source,))
            if self.router.dispatch_check:
                self.router.dispatch_check(None, None)
            branch = "then" if self.condition(node["condition"]) else "else"
            self.trace.emit("condition", predicate=node["condition"], branch=branch)
            return await self.sequence(node[branch])
        if kind == "fallback":
            self.trace.emit("fallback", strategy=node["strategy"])
            return await self.sequence(node["sequence"])
        if kind == "retry":
            for attempt in range(1, node["max_attempts"] + 1):
                self.trace.emit("retry_attempt", attempt=attempt)
                result = await self.node(node["node"])
                if result.ok:
                    return result
                if (result.event != Event.EXPECTED_FAILURE or not result.recoverable
                        or result.reason not in node["retry_if"]):
                    return result
            if "fallback" in node:
                return await self.node(node["fallback"])
            return result
        if kind == "abort":
            stopped = await self.stop()
            if not stopped.ok:
                raise RuntimeHalt("SAFETY_ABORT", "stop_failed")
            if node["return_home"]:
                returned = await self.node({"type": "skill", "name": "return_home", "args": {}})
                if not returned.ok:
                    raise RuntimeHalt("SAFETY_ABORT", f"abort_return_failed:{returned.reason}")
            raise RuntimeHalt("ABORTED", node["reason"])
        if kind == "replan_required":
            self.state.mission["replan_constraints"] = node["constraints"]
            raise RuntimeHalt("REPLAN_REQUIRED", node["reason"])
        raise RuntimeHalt("INVALID_PLAN", "unknown_node")
