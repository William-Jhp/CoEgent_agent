"""Backend-free plan generation and the context shared with the legacy agent."""

import asyncio
import json
from copy import deepcopy
from dataclasses import asdict
from time import monotonic

from coegent_contracts import CONTRACT_VERSION, RuntimeConfig
from coegent_contracts.registry import fingerprint

from .preflight import PreflightError
from .registry import SkillRegistry
from .safety import SafetyPolicy
from .types import State, Trace
from .validation import PlanError, PlanValidator, walk
from .planning_dispatch import ClarificationRequired, dispatch_plan
from .workflow import stage_graph


def build_plan_context(command, state, registry, policy, *, completed_steps=(),
                       failed_step=None, remaining_plan=(), recent_events=(), feedback=None):
    """Describe planning inputs; do not construct a router, engine, or robot owner."""
    allowed = policy.allowed_skills
    schema = PlanValidator(registry).schema
    skills = {k: v for k, v in registry.contracts().items()
              if allowed is None or k in allowed or k == "stop"}
    return {"command": command, "state": state.snapshot(), "skills": skills,
            "contract_version": CONTRACT_VERSION, "contract_hash": registry.fingerprint,
            "schema_hash": fingerprint(schema), **policy.availability(registry, state),
            "schema": schema, "completed_steps": deepcopy(list(completed_steps)),
            "failed_step": deepcopy(failed_step), "remaining_plan": deepcopy(list(remaining_plan)),
            "recent_events": deepcopy(list(recent_events)), "feedback": deepcopy(feedback),
            "policy": {"minimum_outbound_battery": policy.min_battery,
                       "minimum_return_battery": policy.min_return_battery,
                       "max_distance": policy.max_distance, "max_angle": policy.max_angle},
            "instruction": "Plan remaining work only. Runtime safety policy is authoritative."}


class PlanGenerator:
    """Return validated planner.v1 data without dispatching even a stop command.

    State is a caller-supplied snapshot. Prefix preflight is advisory with respect
    to future execution: an executor must validate again using its current state.
    """

    def __init__(self, planner, *, config=None, planner_selector=None, tool_profile=None):
        self.planner = planner
        self.planner_selector = planner_selector
        self.config = config or RuntimeConfig()
        self.tool_profile = tool_profile
        self.registry = tool_profile.registry if tool_profile else SkillRegistry()
        self.validator = tool_profile.validator if tool_profile else PlanValidator(self.registry)
        self.policy = SafetyPolicy(**asdict(self.config))
        self._busy = False
        self._pending = None
        self._owned_tasks = set()

    @property
    def pending(self):
        tasks = set(self._owned_tasks)
        if self._pending is not None:
            tasks.add(self._pending)
        return tuple(task for task in tasks if not task.done())

    def track(self, task):
        """Retain caller-owned generation cleanup until its coroutine has drained."""
        self._owned_tasks.add(task)
        def finished(done):
            self._owned_tasks.discard(done)
            if not done.cancelled():done.exception()
        task.add_done_callback(finished)

    async def _call_planner(self, context, on_selection=None, *, trace=None):
        task = asyncio.create_task(dispatch_plan(self.planner, context, selector=self.planner_selector,
                                                 on_selection=on_selection, trace=trace))
        self._pending = task
        task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        try:
            done, _ = await asyncio.wait({task}, timeout=self.config.planner_timeout_s)
            if not done:
                raise TimeoutError("planner_timeout")
            return task.result()
        finally:
            # Retain the task while HF/cooperative adapters finish cancellation.
            # A second cancellation could interrupt cleanup prematurely.
            if not task.done() and not task.cancelling():
                task.cancel()

    async def generate(self, command, state, *, feedback=None, completed_steps=(), failed_step=None, remaining_plan=()):
        draining=tuple(t for t in self.pending if t is not asyncio.current_task())
        if not self._busy and draining:
            # LangGraph needs a few loop turns to unwind cancelled node tasks
            # after model cleanup. Keep ownership, with a bounded drain grace.
            await asyncio.wait(draining,timeout=.01)
        if self._busy or any(task is not asyncio.current_task() for task in self.pending):
            raise RuntimeError("already generating a plan")
        state = State.from_dict(state.snapshot())
        self._busy = True
        trace = Trace()
        calls = repairs = 0
        generated, normalizations, findings = None, [], []
        selections, question = [], None
        accepting_selections = True
        def selected(info):
            if accepting_selections:
                selections.append(info)
                trace.emit('planner_selection', **info)
        status, reason = "PLANNER_ERROR", "planner_not_available"
        raw=logged_raw=context=None
        async def prepare_context():
            nonlocal context,calls
            if self.planner is None:return 'report'
            continuation=dict(feedback=feedback,completed_steps=deepcopy(list(completed_steps)),
                              failed_step=deepcopy(failed_step),remaining_plan=deepcopy(list(remaining_plan)))
            context=(self.tool_profile.context(command,state,**continuation) if self.tool_profile
                     else build_plan_context(command,state,self.registry,self.policy,**continuation))
            calls+=1;trace.emit('planner_start',call=calls,feedback=feedback)
            return 'call_planner'

        async def call_planner():
            nonlocal raw,logged_raw,status,reason,question
            try:raw=await self._call_planner(context,selected,trace=trace)
            except ClarificationRequired as exc:
                status,reason,question='NEEDS_CLARIFICATION','command_requires_clarification',exc.question
                trace.emit('clarification_requested',question=question);return 'report'
            except TimeoutError:
                reason='planner_timeout';return 'report'
            except Exception as exc:
                reason=f'{type(exc).__name__}:{exc}';return 'report'
            try:json.dumps(raw,allow_nan=False);logged_raw=raw
            except (ValueError,TypeError,RecursionError):logged_raw=repr(raw)[:12000]
            trace.emit('planner_output',raw=logged_raw)
            return 'validate_plan'

        async def validate_plan():
            nonlocal generated,normalizations,findings,status,reason,feedback
            try:
                candidate,normalizations=self.validator.prepare(raw,state.robot.get('home'))
                allowed=self.policy.allowed_skills
                if allowed is not None and any(n['type']=='skill' and n['name'] not in allowed and n['name']!='stop'
                                               for n in walk(candidate['sequence'])):
                    status,reason='SAFETY_ABORT','authority_denied';return 'report'
                findings=(self.tool_profile.preflight(candidate,state,self.policy) if self.tool_profile
                          else self.validator.preflight(candidate,state,self.policy))
            except PreflightError as exc:
                status,reason=exc.event,exc.reason
                trace.emit('preflight_rejected',skill=exc.skill,reason=reason,event=status);return 'report'
            except PlanError as exc:
                trace.emit('validation_error',reason=str(exc))
                if repairs<self.config.max_repairs:
                    feedback={'kind':'validation_error','reason':str(exc),
                              'previous_output':logged_raw[:12000] if isinstance(logged_raw,str) else logged_raw}
                    return 'repair_plan'
                status,reason='INVALID_PLAN',str(exc);return 'report'
            generated=candidate;status,reason='PLAN_READY','plan_validated_not_executed'
            trace.emit('plan_validated',normalizations=normalizations,preflight=findings)
            return 'report'

        async def repair_plan():
            nonlocal repairs
            repairs+=1;return 'prepare_context'

        async def report_node():return None
        try:
            graph=stage_graph('planning',{'prepare_context':prepare_context,'call_planner':call_planner,
                'validate_plan':validate_plan,'repair_plan':repair_plan,'report':report_node},trace=trace)
            await graph.run(max_transitions=5*(self.config.max_repairs+1)+5)
            return {"status": status, "reason": reason, "execution_status": "NOT_EXECUTED",
                    "plan": deepcopy(generated), "normalizations": normalizations, "preflight": findings,
                    "planner_selections": deepcopy(selections), "question": question,
                    "planner_calls": calls, "repairs": repairs, "orchestrator": "langgraph",
                    "contract_version": CONTRACT_VERSION, "contract_hash": self.registry.fingerprint,
                    "schema_hash": fingerprint(self.validator.schema),
                    "generation_mode": selections[-1]['generation_mode'] if selections else getattr(self.planner, "generation_mode", "scripted_or_external"),
                    "elapsed_s": round(monotonic() - trace.started, 6), "trace": deepcopy(trace.entries),
                    **(self.tool_profile.report(generated) if self.tool_profile else {})}
        finally:
            accepting_selections = False
            self._busy = False
