"""Execute provided plans through existing admission, safety, and goal checks."""

from time import time
from uuid import uuid4

from .admission import MissionRequest
from .agent import CoEgent


class PlanExecutor:
    """Provided-plan entry point; unknown execution state returns to the caller.

    The existing runtime is reused with planner=None, so validation failure and
    REPLAN_REQUIRED cannot trigger hidden inference or automatic plan repair.
    An explicitly supplied Inspector may use its configured model for review;
    it cannot generate or execute a replacement plan through this entry point.
    """

    def __init__(self, backend, *, config=None, admission=None, state_provider=None, connection=None,
                 executor='coegent', executor_options=None, executor_registry=None,
                 plan_store=None, auto_save_plans=True, inspector=None):
        self._runtime = CoEgent(None, backend, config=config, admission=admission,
                                state_provider=state_provider, connection=connection,
                                executor=executor, executor_options=executor_options,
                                executor_registry=executor_registry, plan_store=plan_store,
                                auto_save_plans=auto_save_plans, inspector=inspector)

    def submit_report(self, message):
        return self._runtime.submit_report(message)

    def set_executor(self, name, *, options=None):
        self._runtime.set_executor(name, options=options)

    async def execute(self, plan, state, *, command="Execute provided plan", mission_id=None,
                      revision=1, expires_at=None, goals=()):
        request = MissionRequest(
            mission_id=str(uuid4()) if mission_id is None else mission_id,
            revision=revision,
            expires_at=time() + self._runtime.config.mission_timeout_s + 1 if expires_at is None else expires_at,
            command=command, plan=plan, goals=goals)
        return await self.execute_request(request, state)

    async def execute_request(self, request, state):
        if not isinstance(request, MissionRequest) or request.plan is None:
            raise ValueError("execution requires a MissionRequest with a provided plan")
        return await self._runtime.run_request(request, state)

    async def confirm_safe_reset(self):
        return await self._runtime.confirm_safe_reset()
