"""Robot admission -> shared validation -> deterministic execution -> goal evidence."""

import asyncio
import json
from copy import deepcopy
from dataclasses import asdict, replace
from time import monotonic, time
from uuid import uuid4

from coegent_contracts import CONTRACT_VERSION, RuntimeConfig
from coegent_contracts.registry import fingerprint

from .admission import MissionRequest, RobotAdmission, offline_action
from coegent_executor_adapters import ExecutorContext
from .executor_adapters import default_registry
from .executor_adapters.session import ExecutorSession
from .goals import goal_paths, mission_success, verify_goals
from .inspector import ReportInbox, ReportMessage
from .observations import FreshState
from .planning import build_plan_context
from .planning_dispatch import ClarificationRequired, dispatch_plan
from .plan_store import PlanStore
from .preflight import PreflightError
from .registry import SkillRegistry
from .safety import SafetyPolicy
from .skills import SkillRouter
from .types import Event, RuntimeHalt, State, Trace, bounded_call
from .workflow import stage_graph
from .validation import PlanError, PlanValidator


async def cancel_work(task, timeout):
    if task is not None and not task.done():
        task.cancel()
        await asyncio.wait({task}, timeout=timeout)
    if task is not None and task.done() and not task.cancelled():
        task.exception()


class CoEgent:
    def __init__(self, planner, backend, policy=None, limits=None, *, config=None,
                 admission=None, state_provider=None, connection=None,
                 executor='coegent', executor_options=None, executor_registry=None,
                 plan_store=None, auto_save_plans=True, planner_selector=None, inspector=None):
        if config is not None and (policy is not None or limits is not None):
            raise ValueError("use config OR legacy policy/limits, not both")
        values = config or RuntimeConfig()
        if limits is not None:
            fields = ("max_steps", "max_replans", "max_repairs", "skill_timeout_s", "planner_timeout_s", "mission_timeout_s")
            values = replace(values, **{key: getattr(limits, key) for key in fields})
        if policy is not None:
            fields = ("min_battery", "min_return_battery", "max_distance", "max_angle", "allowed_skills")
            values = replace(values, **{key: getattr(policy, key) for key in fields})
        self.planner, self.config, self.limits = planner, values, values
        self.planner_selector = planner_selector
        self.inspector, self._report_inbox = inspector, None
        self.executor_registry = executor_registry if executor_registry is not None else default_registry()
        self._executor_factory = self.executor_registry.resolve(executor)
        self.executor_name = executor
        self.executor_options = self._options(executor_options)
        if type(auto_save_plans) is not bool:
            raise ValueError('auto_save_plans must be a bool')
        self.plan_store = plan_store if plan_store is not None else PlanStore()
        self.auto_save_plans = auto_save_plans
        self.fresh = FreshState(state_provider, values)
        if values.remote_required and connection is None:
            raise ValueError("remote-dependent missions require a local connection probe")
        self.connection = connection
        self.admission = (RobotAdmission.bind(backend, admission) if admission is not None
                          else RobotAdmission.for_backend(backend, capacity=values.request_cache_capacity))
        if values.state_mode == "strict" and not self.admission.safety_initialized:
            self.admission.latch_fault("startup_safety_unconfirmed")
            self.admission.safety_initialized = True
        self.registry = SkillRegistry()
        self.validator = PlanValidator(self.registry)
        self.router = SkillRouter(backend, self.registry, SafetyPolicy(**asdict(values)), values.skill_timeout_s)
        self.router.admission, self.router.fresh = self.admission, self.fresh
        self._busy = False
        self.last_report = None
        self._needs_remote = False
        self._executor_session = None
        self._trace = None
        self._continuation_steps = []

    def submit_report(self, message):
        """Ingress for executor/sub-agent messages; claims never become observations.

        Call on the owning event loop (cross-thread transports should use
        loop.call_soon_threadsafe). No active configured Inspector rejects ingress.
        """
        if self._report_inbox is None:
            return {'accepted': False, 'reason': 'mission_not_active'}
        try:
            return self._report_inbox.submit(message)
        except ValueError as exc:
            return {'accepted': False, 'reason': 'invalid_report:' + str(exc)}

    def _retain_inspection_work(self):
        pending = self.inspector.pending if self.inspector is not None else ()
        for task in pending:
            self.admission.track(task)
        if pending:
            self.admission.latch_fault('inspection_cancellation_unconfirmed')
        return bool(pending)

    @staticmethod
    def _options(options):
        value = {} if options is None else deepcopy(options)
        if not isinstance(value, dict):
            raise ValueError('executor_options must be a JSON object')
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError('executor_options must be a finite JSON object') from exc
        return value

    def set_executor(self, name, *, options=None):
        """Replace a selection between missions; existing factory bindings survive unregister."""
        if self._busy or self.admission.pending:
            raise RuntimeError('cannot change executor while a mission or owned task is active')
        factory = self.executor_registry.resolve(name)
        options = self._options(options)
        self._executor_factory, self.executor_name, self.executor_options = factory, name, options

    def context(self, command, state, executor, trace, feedback):
        recent = [{k: v for k, v in entry.items() if k in
                   {"kind", "skill", "event", "reason", "branch", "status"}}
                  for entry in trace.entries if entry["kind"] in
                  {"skill_result", "safety_gate", "condition", "replan"}][-8:]
        return build_plan_context(command, state, self.registry, self.router.policy,
                                  completed_steps=executor.completed, failed_step=executor.failed,
                                  remaining_plan=executor.remaining, recent_events=recent, feedback=feedback)

    def check_dispatch(self, request, skill=None, args=None, *, returning=False):
        self.admission.check_active(request)
        if skill is not None and args is not None and not returning:
            effects = set(self.registry.definition(skill)["effects"])
            mutations = {"internal.invalidate_observations", "physical.valve_state", "physical.held_object",
                         "observation.localization", "physical.charging"}
            if effects & mutations and {"name": skill, "args": args} in self._continuation_steps:
                raise RuntimeHalt("REPLAN_REQUIRED", "continuation_repeats_completed_skill")

    async def checkpoint(self, request, skill=None, args=None, *, needs_remote=False, returning=False):
        self.check_dispatch(request, skill, args, returning=returning)
        if not self.config.remote_required:
            return
        try:
            connected = await bounded_call(self.connection.connected(), self.config.skill_timeout_s)
            connected = connected is True
        except Exception:
            connected = False
        self.admission.check_active(request)
        action = offline_action(self.config, connected=connected, needs_remote=needs_remote, skill=skill)
        if returning and action == "return_home":
            return
        if action != "continue":
            raise RuntimeHalt("HELD", "connection_lost:" + action)

    async def confirm_safe_reset(self):
        if self.fresh.provider is None or self.config.state_mode != "strict" or not self.admission.begin_management():
            return False
        safe = False
        try:
            state = State()
            await self.fresh.refresh(state, ("robot.stopped", "robot.estop"), "reset")
            if state.robot.get("stopped") is not True or state.robot.get("estop") is not False:
                return False
            ack = await self.router.dispatch("stop", {}, state, Trace())
            await asyncio.sleep(0)
            await self.fresh.refresh(state, ("robot.stopped", "robot.estop"), "reset")
            safe = (ack.ok and not self.admission.pending and state.robot.get("stopped") is True
                    and state.robot.get("estop") is False)
            return safe
        except Exception:
            return False
        finally:
            self.admission.end_management(safe=safe)

    async def run(self, command: str, state: State):
        if self._busy:
            raise RuntimeError("agent already executing a mission")
        request = MissionRequest(str(uuid4()), 1, time() + self.config.mission_timeout_s + 1, command)
        return await self.run_request(request, state)

    async def run_request(self, request: MissionRequest, state: State):
        if not isinstance(request, MissionRequest):
            raise ValueError("run_request requires a validated MissionRequest")
        request = deepcopy(request)
        state = State.from_dict(state.snapshot())
        accepted = self.admission.admit(request)
        if accepted["kind"] != "accepted":
            if accepted["kind"] == "replayed":
                return {**accepted["report"], "replayed": True}
            return {"mission_id": request.mission_id, "revision": request.revision,
                    "command": request.command, "status": "REJECTED", "execution_status": "REJECTED",
                    "reason": accepted["reason"], "goal_status": "not_evaluated", "mission_success": None,
                    "planner_calls": 0, "repairs": 0, "replans": 0, "steps": 0, "completed_steps": [],
                    "trace": [], "state": state.snapshot(), "robot_fault": self.admission.fault_reason}
        self._busy, self.last_report = True, None
        self._report_inbox = (ReportInbox(request.mission_id, request.revision,
            self.inspector.config.mailbox_capacity) if self.inspector is not None else None)
        self._executor_session, self._trace = None, None
        try:
            return await self._run_owned(request, state, accepted)
        except BaseException as exc:
            if self.admission.owns(request):
                self._retain_inspection_work()
                # Includes cancellation during final evidence reads/report construction.
                task = asyncio.create_task(self._executor_session.stop() if self._executor_session else
                                           self.router.dispatch("stop", {}, state, Trace()))
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        pass
                try:
                    ack = task.result()
                    confirmation = {"confirmed": ack.ok, "reason": ack.reason}
                except BaseException:
                    confirmation = {"confirmed": False, "reason": "cleanup_failed"}
                if self._executor_session is not None:
                    try:
                        await self._executor_session.close()
                    except BaseException:
                        self.admission.latch_fault('executor_close_failed')
                if not confirmation["confirmed"]:
                    self.admission.latch_fault(confirmation["reason"])
                status = "CANCELLED" if isinstance(exc, asyncio.CancelledError) else "SAFETY_ABORT"
                if self.admission.fault_reason:
                    status = "SAFETY_ABORT"
                fallback = {"mission_id": request.mission_id, "revision": request.revision,
                            "command": request.command, "execution_status": status, "status": status,
                            "reason": "finalization_interrupted", "report_incomplete": True,
                            "executor": self.executor_name,
                            "stop_confirmation": confirmation, "robot_fault": self.admission.fault_reason,
                            "goal_status": "unknown" if request.goals else "not_evaluated",
                            "mission_success": False if request.goals else None,
                            "state": state.snapshot(),
                            "completed_steps": deepcopy(self._executor_session.completed) if self._executor_session else [],
                            "remaining_plan": deepcopy(self._executor_session.remaining) if self._executor_session else [],
                            "trace": deepcopy(self._trace.entries) if self._trace else []}
                if self.inspector is not None:
                    fallback['mission_success'] = False
                    fallback['completion_report'] = {'mission_id':request.mission_id,'revision':request.revision,
                        'verified':False,'next_action':'halt','status':status,'reason':'finalization_interrupted'}
                self.last_report = deepcopy(fallback)
                self.admission.finish(request, fallback)
            raise
        finally:
            if self._report_inbox is not None:
                self._report_inbox.close()
            self.router.checkpoint = None
            self.router.dispatch_check = None
            self._busy = False
            self._executor_session, self._trace = None, None

    async def _run_owned(self, request, state, accepted):
        self.fresh.reset_mission()
        if self.config.state_mode == "strict":
            state.observations.clear()
        self.router.evidence = []
        self._needs_remote = False
        trace = Trace()
        context = ExecutorContext(self.executor_name, self.router, state, trace, self.limits, request,
                                  self.submit_report if self.inspector is not None else None)
        executor = ExecutorSession(self._executor_factory(context, deepcopy(self.executor_options)), context)
        self._executor_session, self._trace = executor, trace
        self.router.checkpoint = lambda name, args=None: self.checkpoint(request, name, args)
        self.router.dispatch_check = lambda name, args=None: self.check_dispatch(request, name, args)
        calls = replans = repairs = 0
        feedback, last_plan = None, None
        status, reason, failure_reason = "INVALID_PLAN", "no_plan", None
        cancelled, return_result = False, None
        selections, question = [], None
        inspections, pending_reports = [], []
        last_inspection = None

        async def inspect_message(message):
            nonlocal last_inspection
            decision = await self.inspector.inspect(message)
            if self._retain_inspection_work():
                raise RuntimeHalt('SAFETY_ABORT', 'inspection_cancellation_unconfirmed')
            last_inspection = decision.to_dict()
            inspections.append(last_inspection)
            trace.emit('inspection', **last_inspection)
            return decision

        def executor_message(kind, message):
            return ReportMessage(request.mission_id, request.revision, str(uuid4()), kind, message)

        completion_claims = []
        async def poll_reports(*, seal=False):
            if self._report_inbox is None:
                return
            # Drain every message first so a safety report cannot be hidden by
            # an earlier recoverable error in the same batch.
            messages = self._report_inbox.drain()
            if seal:
                self._report_inbox.close()
            pending_reports.extend(m for m in messages if m.kind == 'error')
            completion_claims.extend(m for m in messages if m.kind == 'completed' and m.scope == 'mission')
            for message in messages:
                trace.emit('executor_report', report=message.to_dict())
            if any(m.severity in {'fatal', 'safety'} for m in messages):
                await inspect_message(next(m for m in messages if m.severity in {'fatal','safety'}))
                raise RuntimeHalt('SAFETY_ABORT', 'executor_safety_report')
            if pending_reports:
                raise RuntimeHalt('REPLAN_REQUIRED', 'executor_error_report')

        def selected(info):
            if self._trace is trace:
                selections.append(info)
                trace.emit('planner_selection', **info)

        async def before_model_call(needs_remote):
            self._needs_remote = self.config.remote_required or needs_remote
            await self.checkpoint(request, needs_remote=self._needs_remote)
        prior = accepted.get("previous_report")
        self._continuation_steps = deepcopy(prior.get("mission_completed_steps", prior.get("completed_steps", []))) if prior else []
        if prior:
            feedback = {"kind": "approved_revision", "completed_steps": self._continuation_steps,
                        "remaining_plan": prior.get("remaining_plan", []), "instruction": "Do not repeat completed side effects."}

        async def mission():
            nonlocal calls,replans,repairs,feedback,last_plan,status,reason,failure_reason
            raw=request.plan;stage='preflight';plan=None;result=None

            async def refresh_state():
                nonlocal stage
                stage='preflight'
                await self.fresh.refresh(state,('robot.estop','robot.localization_valid'),'planning')
                return 'plan' if raw is None else 'validate_plan'

            async def plan_node():
                nonlocal raw,calls
                if self.planner is None:raise RuntimeHalt('PLANNER_ERROR','planner_not_available')
                if self.planner_selector is None:
                    await before_model_call(getattr(self.planner,'requires_remote',False))
                calls+=1
                context=self.context(request.command,state,executor,trace,feedback)
                context['request_goals']=[asdict(g) for g in request.goals]
                trace.emit('planner_start',call=calls,feedback=feedback)
                task=asyncio.create_task(dispatch_plan(self.planner,context,selector=self.planner_selector,
                    on_selection=selected,before_call=before_model_call if self.planner_selector is not None else None,
                    trace=trace,on_task=self.admission.track))
                task.add_done_callback(lambda done:done.exception() if not done.cancelled() else None)
                try:
                    done,_=await asyncio.wait({task},timeout=self.config.planner_timeout_s)
                    if not done:raise RuntimeHalt('PLANNER_ERROR','planner_timeout')
                    raw=task.result()
                except (RuntimeHalt,ClarificationRequired):raise
                except Exception as exc:raise RuntimeHalt('PLANNER_ERROR',f'{type(exc).__name__}:{exc}') from exc
                finally:
                    if not task.done():task.cancel()
                    self._needs_remote=False
                await self.checkpoint(request)
                trace.emit('planner_output',raw=raw)
                return 'validate_plan'

            async def validate_plan():
                nonlocal plan,last_plan,feedback
                await self.fresh.refresh(state,('robot.estop','robot.localization_valid'),'preflight')
                try:
                    plan,normalizations=self.validator.prepare(raw,state.robot.get('home'))
                    findings=self.validator.preflight(plan,state,self.router.policy,fresh=self.fresh)
                except PlanError as exc:
                    trace.emit('validation_error',reason=str(exc))
                    if isinstance(exc,PreflightError):
                        trace.emit('safety_gate',skill=exc.skill,reason=exc.reason,event=exc.event,stage=stage)
                        raise RuntimeHalt(exc.event,exc.reason) from exc
                    if repairs<self.config.max_repairs and self.planner is not None:
                        feedback={'kind':'validation_error','reason':str(exc),
                                  'previous_output':raw[:12000] if isinstance(raw,str) else raw}
                        return 'repair_plan'
                    raise RuntimeHalt('INVALID_PLAN',str(exc)) from exc
                last_plan=plan
                trace.emit('plan_validated',normalizations=normalizations,preflight=findings)
                return 'execute_plan'

            async def repair_plan():
                nonlocal repairs,raw
                repairs+=1;raw=None;return 'refresh_state'

            async def execute_plan():
                nonlocal result,stage
                stage='execution'
                result=await executor.run(plan,poll_reports=poll_reports if self.inspector is not None else None)
                if result.ok:return 'inspect_completion'
                raise RuntimeHalt('SAFETY_ABORT' if result.event==Event.SAFETY_ABORT else 'REPLAN_REQUIRED',result.reason)

            async def inspect_completion():
                if self.inspector is not None:
                    claim=completion_claims[-1] if completion_claims else executor_message('completed',result.reason or 'executor_completed')
                    completion_claims.clear()
                    decision=await inspect_message(claim)
                    await poll_reports()
                    if decision.decision!='completed':
                        raise RuntimeHalt('SAFETY_ABORT' if decision.decision=='halt' else 'REPLAN_REQUIRED',decision.reason)
                    await self.fresh.refresh(state,goal_paths(request.goals),'goal')
                    checked=verify_goals(request.goals,state.snapshot(),tuple(self.router.evidence))
                    if request.goals and checked['goal_status']!='achieved':
                        raise RuntimeHalt('REPLAN_REQUIRED','approved_goals_unverified')
                    await poll_reports()
                return 'complete'

            async def complete():
                nonlocal status,reason,failure_reason
                status,reason,failure_reason='SUCCESS','plan_execution_complete',None

            async def stop_before_replan():
                stopped=await executor.stop()
                if not stopped.ok:raise RuntimeHalt('SAFETY_ABORT','stop_failed_before_replan')
                return 'diagnose_error'

            async def diagnose_error():
                if self.inspector is not None:
                    if pending_reports:
                        reports,pending_reports[:]=list(pending_reports),[]
                        for message in reports:
                            decision=await inspect_message(message)
                            if decision.decision=='halt':raise RuntimeHalt('SAFETY_ABORT',decision.reason)
                    elif reason not in {'completion_contradicted','approved_goals_unverified'} and not reason.startswith('completion_unverified'):
                        await inspect_message(executor_message('error',reason))
                return 'replan'

            async def replan():
                nonlocal replans,feedback,raw
                if replans>=self.config.max_replans or self.planner is None:return None
                replans+=1
                self._continuation_steps.extend(deepcopy(executor.completed))
                feedback={'kind':'replan','reason':reason,'stage':stage}
                if last_inspection is not None:feedback['inspection']=deepcopy(last_inspection)
                trace.emit('replan',count=replans,reason=reason)
                raw=None;return 'refresh_state'

            def guarded(callback):
                async def invoke():
                    nonlocal status,reason,failure_reason
                    try:return await callback()
                    except RuntimeHalt as exc:
                        if exc.status!='REPLAN_REQUIRED':raise
                        status,reason,failure_reason=exc.status,exc.reason,exc.reason
                        return 'stop_before_replan'
                return invoke

            callbacks={name:guarded(callback) for name,callback in {
                'refresh_state':refresh_state,'plan':plan_node,'validate_plan':validate_plan,
                'execute_plan':execute_plan,'inspect_completion':inspect_completion}.items()}
            callbacks.update(repair_plan=repair_plan,complete=complete,stop_before_replan=stop_before_replan,
                             diagnose_error=diagnose_error,replan=replan)
            await stage_graph('native',callbacks,trace=trace).run(
                max_transitions=16*(self.config.max_replans+self.config.max_repairs+1)+16)

        async def monitor(*, returning=False):
            while True:
                executor.check_progress()
                await self.checkpoint(request, self.router.active_skill, needs_remote=self._needs_remote,
                                      returning=returning)
                if monotonic() >= executor.deadline:
                    raise RuntimeHalt("SAFETY_ABORT", "mission_timeout")
                if executor.steps > self.limits.max_steps:
                    raise RuntimeHalt('SAFETY_ABORT', 'step_budget_exhausted')
                await asyncio.sleep(min(self.config.connection_poll_s, self.admission.remaining_s(request),
                                        max(0, executor.deadline - monotonic())))

        work = watcher = None
        async def cleanup():
            nonlocal cancelled
            if executor.stop_confirmation and executor.stop_confirmation["confirmed"] is False:
                return
            task = asyncio.create_task(executor.stop())
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
            task.result()

        try:
            work = asyncio.create_task(mission())
            self.admission.track(work)
            watcher = asyncio.create_task(monitor())
            done, _ = await asyncio.wait({work, watcher}, timeout=self.config.mission_timeout_s,
                                         return_when=asyncio.FIRST_COMPLETED)
            if not done:
                raise RuntimeHalt("SAFETY_ABORT", "mission_timeout")
            if watcher in done:
                watcher.result()
            work.result()
        except asyncio.CancelledError:
            cancelled, status, reason = True, "CANCELLED", "mission_cancelled"
            failure_reason = reason
        except ClarificationRequired as exc:
            status, reason, question = 'NEEDS_CLARIFICATION', 'command_requires_clarification', exc.question
            trace.emit('clarification_requested', question=question)
        except RuntimeHalt as exc:
            status, reason = exc.status, exc.reason
            if reason != "stop_failed_before_replan":
                failure_reason = reason
        except Exception as exc:
            status, reason = "SAFETY_ABORT", f"runtime_error:{type(exc).__name__}:{exc}"
            failure_reason = reason
        finally:
            await cancel_work(watcher, self.config.skill_timeout_s)
            await cancel_work(work, self.config.skill_timeout_s)
            self._retain_inspection_work()
            if work is not None and not work.done():
                self.admission.track(work)
                self.admission.latch_fault("mission_cancellation_unconfirmed")
            elif work is not None and not work.cancelled():
                work.exception()
            if status not in {"SUCCESS", "REPLAN_REQUIRED"} and not (status == 'NEEDS_CLARIFICATION' and executor.steps == 0):
                await cleanup()

        if status == "HELD" and reason == "connection_lost:return_home" and not self.admission.fault_reason:
            returning = return_watcher = None
            try:
                self.router.checkpoint = lambda name, args=None: self.checkpoint(request, name, args, returning=True)
                self.router.dispatch_check = lambda name, args=None: self.check_dispatch(request, name, args, returning=True)
                returning = asyncio.create_task(self.router.dispatch("return_home", {}, state, trace))
                self.admission.track(returning)
                return_watcher = asyncio.create_task(monitor(returning=True))
                done, _ = await asyncio.wait({returning, return_watcher},
                                             timeout=max(0, executor.deadline - monotonic()),
                                             return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    raise RuntimeHalt("SAFETY_ABORT", "mission_timeout")
                if return_watcher in done:
                    return_watcher.result()
                result = returning.result()
                return_result = result.to_dict()
                if not result.ok:
                    reason = f"offline_return_failed:{result.reason}"
            except RuntimeHalt as exc:
                status, reason, failure_reason = exc.status, exc.reason, exc.reason
            finally:
                await cancel_work(return_watcher, self.config.skill_timeout_s)
                await cancel_work(returning, self.config.skill_timeout_s)
                if returning is not None and not returning.done():
                    self.admission.latch_fault("return_cancellation_unconfirmed")
                if return_result is None or return_result["status"] != "success":
                    await cleanup()
        confirmation = executor.stop_confirmation or {"confirmed": None, "reason": "not_requested"}
        if confirmation["confirmed"] is False:
            status, reason = "SAFETY_ABORT", f"stop_unconfirmed:{confirmation['reason']}"
        elif self.admission.fault_reason:
            status = "SAFETY_ABORT"
        elif cancelled:
            status, reason = "CANCELLED", "mission_cancelled"
        known = True
        try:
            await self.fresh.refresh(state, goal_paths(request.goals), "goal")
        except RuntimeHalt:
            known = False
        # Prior revisions' evidence is not imported; never infer their omissions.
        # History completeness is separate from availability of final sensor state.
        inspection_history_complete = (prior is None and status == "SUCCESS" and work is not None and work.done()
                                       and not work.cancelled() and not self.admission.pending
                                       and self.admission.fault_reason is None)
        try:
            await executor.close()
        except asyncio.CancelledError:
            cancelled, status, reason = True, 'CANCELLED', 'mission_cancelled'
            failure_reason = failure_reason or reason
            await cleanup()
        except Exception as exc:
            self.admission.latch_fault('executor_close_failed')
            status, reason = 'SAFETY_ABORT', f'executor_close_error:{type(exc).__name__}'
            failure_reason = failure_reason or reason
            await cleanup()
        try:
            executor.check_progress()
        except RuntimeHalt as exc:
            status, reason = exc.status, exc.reason
            failure_reason = failure_reason or reason
            await cleanup()
        confirmation = executor.stop_confirmation or {"confirmed": None, "reason": "not_requested"}
        if confirmation['confirmed'] is False:
            status, reason = 'SAFETY_ABORT', f"stop_unconfirmed:{confirmation['reason']}"
            failure_reason = failure_reason or reason
        elif self.admission.fault_reason:
            status = 'SAFETY_ABORT'
        elif cancelled:
            status, reason = 'CANCELLED', 'mission_cancelled'
        inspection_history_complete = (inspection_history_complete and status == 'SUCCESS'
                                       and not self.admission.pending and self.admission.fault_reason is None)
        goals = verify_goals(request.goals, state.snapshot(), tuple(self.router.evidence), state_known=known,
                             inspection_history_complete=inspection_history_complete)
        if self._report_inbox is not None:
            # Stop accepting before the final synchronous envelope is built. A
            # message accepted while final sensors/close awaited must be handled.
            try:
                await poll_reports(seal=True)
            except RuntimeHalt as exc:
                if status not in {'SAFETY_ABORT','CANCELLED'} or exc.status == 'SAFETY_ABORT':
                    status, reason = exc.status, exc.reason
                await cleanup()
            finally:
                self._report_inbox.close()
        if self.inspector is not None and status == 'SUCCESS' and request.goals and goals['goal_status'] != 'achieved':
            status, reason = 'REPLAN_REQUIRED', 'final_goals_unverified'
        if self.inspector is not None and status == 'SUCCESS' and (monotonic() >= executor.deadline or time() >= request.expires_at):
            status, reason = 'SAFETY_ABORT', 'mission_timeout'
            await cleanup()
        confirmation = executor.stop_confirmation or {"confirmed": None, "reason": "not_requested"}
        if confirmation['confirmed'] is False:
            status, reason = 'SAFETY_ABORT', 'stop_unconfirmed:' + confirmation['reason']
        elif self.admission.fault_reason:
            status = 'SAFETY_ABORT'
        inspection_history_complete = inspection_history_complete and status == 'SUCCESS'
        trace.emit("mission_end", status=status, reason=reason, stop_confirmation=confirmation)
        report = {"mission_id": request.mission_id, "revision": request.revision, "command": request.command,
                  "status": status, "execution_status": 'NOT_EXECUTED' if status == 'NEEDS_CLARIFICATION' and executor.steps == 0 else status,
                  "reason": reason, "failure_reason": failure_reason, "question": question,
                  "planner_selections": deepcopy(selections),
                  "executor": self.executor_name,
                  "stop_confirmation": confirmation, "robot_fault": self.admission.fault_reason,
                  **goals, "mission_success": mission_success(status, goals["goal_status"]),
                  "inspection_history_complete": inspection_history_complete,
                  "success_criteria_status": goals["goal_status"],
                  "success_criteria": last_plan["success_criteria"] if last_plan else [],
                  "contract_version": CONTRACT_VERSION, "contract_hash": self.registry.fingerprint,
                  "schema_hash": fingerprint(self.validator.schema), "state_mode": self.config.state_mode,
                  "ownership_mode": "process_lock" if self.admission.robot_id else "in_process_backend",
                  "generation_mode": selections[-1]['generation_mode'] if selections else getattr(self.planner, "generation_mode", "scripted_or_external"),
                  "planner_calls": calls, "repairs": repairs, "replans": replans, "orchestrator": "langgraph",
                  "steps": executor.steps, "elapsed_s": round(monotonic() - trace.started, 6),
                  "state": state.snapshot(), "completed_steps": deepcopy(executor.completed),
                  "remaining_plan": deepcopy(executor.remaining), "offline_return": return_result,
                  "evidence": [asdict(e) for e in self.router.evidence], "trace": deepcopy(trace.entries)}
        if self.inspector is not None:
            verified = (status == 'SUCCESS' and last_inspection is not None and last_inspection['verified']
                        and (not request.goals or goals['goal_status'] == 'achieved'))
            report['inspections'] = deepcopy(inspections)
            report['mission_success'] = bool(verified)
            report['completion_report'] = {'mission_id': request.mission_id, 'revision': request.revision,
                'verified': bool(verified), 'status': status, 'reason': reason,
                'next_action': 'report_complete' if verified else 'halt' if status in {'SAFETY_ABORT','CANCELLED'}
                    else last_inspection['decision'] if last_inspection and not last_inspection['verified'] else 'replan',
                'inspection': deepcopy(last_inspection)}
            if verified and not request.goals:
                report['success_criteria_status'] = 'achieved'
        if not self.auto_save_plans:
            report['plan_store'] = {'status': 'skipped', 'reason': 'disabled'}
        elif last_plan is None or self.admission.pending:
            report['plan_store'] = {'status': 'skipped', 'reason': 'no_complete_validated_plan'}
        else:
            try:
                report['plan_store'] = self.plan_store.record_success(
                    last_plan, report, backend=f'{type(self.router.backend).__module__}.{type(self.router.backend).__qualname__}')
            except Exception as exc:
                # Catalog IO must never repeat, halt, or rewrite completed physical work.
                report['plan_store'] = {'status': 'error', 'reason': f'{type(exc).__name__}:{exc}'}
        self.last_report = deepcopy(report)
        self.admission.finish(request, report)
        self.router.checkpoint = None
        self.router.dispatch_check = None
        self._busy = False
        if cancelled:
            raise asyncio.CancelledError
        return report
