"""Shared LangGraph selection -> planner dispatch with caller-owned gates."""
import asyncio
from .planner import ScriptedPlanner
from .workflow import stage_graph


class ClarificationRequired(Exception):
    def __init__(self, question):
        self.question = question
        super().__init__('command_requires_clarification')


async def dispatch_plan(planner, context, *, selector=None, on_selection=None, before_call=None, trace=None, on_task=None):
    target=planner;chosen=None;output=None
    async def gate(client):
        if before_call is not None:
            await before_call(bool(getattr(client,'requires_remote',False)))
        if asyncio.current_task().cancelling():raise asyncio.CancelledError

    async def model_call(awaitable):
        if on_task is None:return await awaitable
        # Track the actual call, rather than LangGraph's bookkeeping wrapper:
        # ownership lasts through cooperative inference cancellation/cleanup.
        task=asyncio.create_task(awaitable)
        on_task(task)
        return await task

    async def select_planner():
        nonlocal target,chosen
        if selector is None:return 'generate_plan'
        continuation=any(context.get(k) for k in ('feedback','completed_steps','failed_step','remaining_plan'))
        if not continuation:await gate(selector)
        chosen=await model_call(selector.select(context))
        if asyncio.current_task().cancelling():raise asyncio.CancelledError
        target=ScriptedPlanner([chosen.plan]) if chosen.decision=='reuse' else planner
        info={**chosen.report(),'planner':type(target).__name__ if chosen.decision!='clarify' else None,
              'generation_mode':getattr(target,'generation_mode','scripted_or_external')}
        if on_selection is not None:on_selection(info)
        return 'clarify' if chosen.decision=='clarify' else 'generate_plan'

    async def generate_plan():
        nonlocal output
        await gate(target)
        output=await model_call(target.plan(context))

    async def clarify():raise ClarificationRequired(chosen.question)
    graph=stage_graph('dispatch',{'select_planner':select_planner,'generate_plan':generate_plan,'clarify':clarify},trace=trace)
    await graph.run()
    return output
