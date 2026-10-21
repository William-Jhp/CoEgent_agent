"""Explicit LangGraph control flow; live resource ownership stays with the caller.

These execution graphs deliberately have no checkpointer or automatic retries.
Physical resume must reconcile the execution journal and current robot state.
"""
import asyncio
from typing import TypedDict
from langgraph.graph import StateGraph, START, END


class RouteState(TypedDict):
    next: str


FLOWS = {
    'planning_session': ('generate_plan', {
        'generate_plan':['await_information','report'], 'await_information':['generate_plan'], 'report':[]}),
    'dispatch': ('select_planner', {
        'select_planner':['generate_plan','clarify'], 'generate_plan':[], 'clarify':[]}),
    'planning': ('prepare_context', {
        'prepare_context':['call_planner','report'], 'call_planner':['validate_plan','report'],
        'validate_plan':['repair_plan','report'], 'repair_plan':['prepare_context'], 'report':[]}),
    'native': ('refresh_state', {
        'refresh_state':['plan','validate_plan','stop_before_replan'],
        'plan':['validate_plan','stop_before_replan'],
        'validate_plan':['execute_plan','repair_plan','stop_before_replan'],
        'repair_plan':['refresh_state'], 'execute_plan':['inspect_completion','stop_before_replan'],
        'inspect_completion':['complete','stop_before_replan'], 'complete':[],
        'stop_before_replan':['diagnose_error'], 'diagnose_error':['replan'],
        'replan':['refresh_state',END]}),
    'a2_continuation': ('execute_plan', {
        'execute_plan':['evaluate_result','finish'], 'evaluate_result':['plan_remaining','finish'],
        'plan_remaining':['validate_continuation','finish'],
        'validate_continuation':['execute_plan','finish'], 'finish':[]}),
    'a2_worker': ('execute_plan', {
        'execute_plan':['stop_motion'], 'stop_motion':['inspect_completion','finish'],
        'inspect_completion':['finish'], 'finish':[]}),
}


class StageGraph:
    def __init__(self, name, callbacks, transitions, entry, *, trace=None):
        if set(callbacks)!=set(transitions) or entry not in callbacks:
            raise ValueError('graph nodes and transitions differ')
        self.name,self.trace=name,trace
        builder=StateGraph(RouteState)
        for node, callback in callbacks.items():
            allowed=set(transitions[node]) or {END}
            if not allowed <= set(callbacks)|{END}:
                raise ValueError('unknown graph transition')
            async def invoke(state, _node=node, _callback=callback, _allowed=allowed):
                if asyncio.current_task().cancelling():raise asyncio.CancelledError
                if self.trace:self.trace.emit('workflow_node_start',graph=name,node=_node)
                try:
                    route=await _callback()
                    if asyncio.current_task().cancelling():raise asyncio.CancelledError
                    target=END if route is None else route
                    if target not in _allowed:raise ValueError('invalid graph transition: '+_node+' -> '+str(target))
                except BaseException as exc:
                    if self.trace:self.trace.emit('workflow_node_error',graph=name,node=_node,error=type(exc).__name__)
                    raise
                if self.trace:self.trace.emit('workflow_node_end',graph=name,node=_node,next=target)
                return {'next':target}
            builder.add_node(node,invoke)
            builder.add_conditional_edges(node,lambda state:state['next'],{v:v for v in allowed})
        builder.add_edge(START,entry)
        self.compiled=builder.compile(name='coegent_'+name)

    async def run(self, *, max_transitions=1024):
        return await self.compiled.ainvoke({'next':''},{'recursion_limit':max_transitions})

    def mermaid(self):return self.compiled.get_graph().draw_mermaid()


def stage_graph(name, callbacks, *, trace=None):
    entry,transitions=FLOWS[name]
    return StageGraph(name,callbacks,transitions,entry,trace=trace)


def graph_mermaid(name):
    async def unused():return None
    return stage_graph(name,{node:unused for node in FLOWS[name][1]}).mermaid()
