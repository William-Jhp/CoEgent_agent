"""The internal interpreter participates in the same adapter boundary."""
from copy import deepcopy

from coegent_executor_adapters import ExecutionProgress, PreparedPlan

from ..engine import MissionEngine


class CoEgentExecutorAdapter:
    def __init__(self, context, options):
        if options:
            raise ValueError('coegent executor does not accept options')
        self.context = context
        self._engine = MissionEngine(context.router, context.state, context.trace, context.limits)
        self._closed = False

    @property
    def progress(self):
        engine = self._engine
        return ExecutionProgress(engine.steps, deepcopy(engine.completed), deepcopy(engine.failed),
                                 deepcopy(engine.remaining), deepcopy(engine.stop_confirmation))

    async def prepare(self, plan):
        return PreparedPlan(self.context.name, deepcopy(plan))

    async def execute(self, prepared):
        if self._closed:
            raise RuntimeError('executor is closed')
        return await self._engine.run(prepared.payload)

    async def status(self):
        return self.progress

    async def cancel(self):
        return await self._engine.stop()

    async def close(self):
        # The injected skill backend belongs to the agent and can be reused.
        self._closed = True
