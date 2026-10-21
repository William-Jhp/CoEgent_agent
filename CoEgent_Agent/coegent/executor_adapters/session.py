"""Enforced mission-local prepare/execute/cancel boundary and progress view."""
import asyncio
from copy import deepcopy
import json
from dataclasses import asdict
from time import monotonic

from coegent_executor_adapters import ExecutionProgress, PreparedPlan

from ..types import Event, RuntimeHalt, SkillResult


class ExecutorSession:
    def __init__(self, adapter, context):
        for method in ('prepare', 'execute', 'status', 'cancel', 'close'):
            if not callable(getattr(adapter, method, None)):
                raise ValueError(f'executor missing method: {method}')
        self.adapter, self.context = adapter, context
        self.deadline = monotonic() + context.limits.mission_timeout_s
        self._stop_confirmation = None
        self._closed = False
        self._last_progress = ExecutionProgress()
        self._progress_invalid = False

    @property
    def progress(self):
        try:
            value = self.adapter.progress
            if (not isinstance(value, ExecutionProgress) or type(value.steps) is not int or value.steps < 0
                    or not isinstance(value.completed, list) or not isinstance(value.remaining, list)
                    or (value.failed is not None and not isinstance(value.failed, dict))):
                raise ValueError('invalid progress fields')
            confirmation = value.stop_confirmation
            if confirmation is not None and (not isinstance(confirmation, dict)
                    or type(confirmation.get('confirmed')) is not bool
                    or not isinstance(confirmation.get('reason'), str)):
                raise ValueError('invalid stop confirmation')
            json.dumps(asdict(value), allow_nan=False)
            self._last_progress = deepcopy(value)
            if confirmation is not None and confirmation['confirmed'] is False:
                self.context.router.admission.latch_fault(confirmation['reason'] or 'executor_stop_unconfirmed')
        except Exception:
            # Reporting and stop cleanup must work even if a plugin breaks its cache.
            self._progress_invalid = True
        return self._last_progress

    def check_progress(self):
        self.progress
        if self._progress_invalid:
            raise RuntimeHalt('SAFETY_ABORT', 'invalid_executor_progress')

    async def _bounded(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.context.router.admission.track(task)
        try:
            done, _ = await asyncio.wait({task}, timeout=self.context.limits.skill_timeout_s)
            if not done:
                raise TimeoutError('executor_adapter_timeout')
            return task.result()
        finally:
            if not task.done():
                task.cancel()

    @property
    def steps(self): return self.progress.steps
    @property
    def completed(self): return self.progress.completed
    @property
    def failed(self): return self.progress.failed
    @property
    def remaining(self): return self.progress.remaining
    @property
    def stop_confirmation(self): return self._stop_confirmation or self.progress.stop_confirmation

    @staticmethod
    def _valid_result(value):
        if (not isinstance(value, SkillResult) or not isinstance(value.event, Event)
                or not isinstance(value.skill, str) or not isinstance(value.reason, str)
                or not isinstance(value.data, dict) or type(value.recoverable) is not bool):
            return False
        try:
            json.dumps(value.data, allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            return False
        return True

    async def run(self, plan, *, poll_reports=None):
        if self._closed:
            raise RuntimeHalt('SAFETY_ABORT', 'executor_closed')
        trace = self.context.trace
        self.check_progress()
        trace.emit('executor_prepare', executor=self.context.name)
        prepared = await self.adapter.prepare(deepcopy(plan))
        if not isinstance(prepared, PreparedPlan) or prepared.executor != self.context.name:
            raise RuntimeHalt('INVALID_PLAN', 'invalid_executor_prepared_plan')
        trace.emit('executor_execute', executor=self.context.name)
        if poll_reports is None:
            result = await self.adapter.execute(prepared)
        else:
            # An error report must interrupt an in-flight executor. A cancelled
            # coroutine is not considered drained until it actually finishes.
            task = asyncio.create_task(self.adapter.execute(prepared))
            self.context.router.admission.track(task)
            try:
                while not task.done():
                    await poll_reports()
                    await asyncio.wait({task}, timeout=self.context.limits.connection_poll_s)
                result = task.result()
                await poll_reports()
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.wait({task}, timeout=self.context.limits.skill_timeout_s)
                    if not task.done():
                        self.context.router.admission.latch_fault('executor_cancellation_unconfirmed')
                        raise RuntimeHalt('SAFETY_ABORT', 'executor_cancellation_unconfirmed')
                if task.done() and not task.cancelled():
                    task.exception()
        self.check_progress()
        if self.steps > self.context.limits.max_steps:
            raise RuntimeHalt('SAFETY_ABORT', 'step_budget_exhausted')
        if not self._valid_result(result):
            raise RuntimeHalt('SAFETY_ABORT', 'invalid_executor_result')
        return result

    async def status(self):
        value = await self._bounded(self.adapter.status())
        if not isinstance(value, ExecutionProgress):
            raise RuntimeHalt('SAFETY_ABORT', 'invalid_executor_progress')
        return value

    async def stop(self):
        if self.stop_confirmation and self.stop_confirmation['confirmed'] is False:
            return SkillResult('stop', Event.SAFETY_ABORT, self.stop_confirmation['reason'])
        try:
            result = await self._bounded(self.adapter.cancel())
            if not self._valid_result(result) or result.skill != 'stop' or (result.ok and result.data.get('stopped') is not True):
                result = SkillResult('stop', Event.SAFETY_ABORT, 'invalid_executor_stop_result')
        except Exception as exc:
            result = SkillResult('stop', Event.SAFETY_ABORT, f'executor_stop_error:{type(exc).__name__}')
        self._stop_confirmation = {'confirmed': result.ok, 'reason': result.reason}
        if not result.ok:
            self.context.router.admission.latch_fault(result.reason)
        self.context.trace.emit('executor_cancel', executor=self.context.name, **self._stop_confirmation)
        return result

    async def close(self):
        if self._closed:
            return
        task = asyncio.create_task(self._bounded(self.adapter.close()))
        cancelled = False
        try:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
            task.result()
        finally:
            self._closed = True
        if cancelled:
            raise asyncio.CancelledError
