import asyncio
import pytest
from coegent.types import Trace


def test_graph_routes_repair_and_preserves_node_trace():
    from coegent.workflow import StageGraph
    calls=[];trace=Trace()
    async def generate():calls.append('generate');return 'validate'
    async def validate():calls.append('validate');return 'repair' if len(calls)==2 else 'finish'
    async def repair():calls.append('repair');return 'generate'
    async def finish():calls.append('finish');return None
    graph=StageGraph('planning',{'generate':generate,'validate':validate,'repair':repair,'finish':finish},
        {'generate':['validate'],'validate':['repair','finish'],'repair':['generate'],'finish':[]},'generate',trace=trace)
    asyncio.run(graph.run(max_transitions=12))
    assert calls==['generate','validate','repair','generate','validate','finish']
    assert [r['node'] for r in trace.entries if r['kind']=='workflow_node_start']==calls
    assert 'validate' in graph.mermaid() and 'repair' in graph.mermaid()


def test_invalid_transition_cannot_dispatch_an_unlisted_node():
    from coegent.workflow import StageGraph
    calls=[]
    async def select():return 'execute'
    async def execute():calls.append('unsafe')
    graph=StageGraph('safe',{'select':select,'execute':execute},{'select':[],'execute':[]},'select')
    with pytest.raises(ValueError,match='transition'):asyncio.run(graph.run())
    assert calls==[]


def test_graph_propagates_cancellation_without_retry():
    from coegent.workflow import StageGraph
    async def exercise():
        entered=asyncio.Event();calls=[]
        async def execute():calls.append(1);entered.set();await asyncio.Event().wait()
        g=StageGraph('cancel',{'execute':execute},{'execute':[]},'execute')
        task=asyncio.create_task(g.run());await entered.wait();task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert calls==[1]
    asyncio.run(exercise())


def test_plan_generation_repair_uses_graph_nodes_without_execution():
    from coegent import PlanGenerator
    from coegent.planner import ScriptedPlanner
    from coegent.scenarios import plan,skill,initial_state
    generator=PlanGenerator(ScriptedPlanner([{'invalid':'plan'},plan(skill('get_pose'))]))
    report=asyncio.run(generator.generate('pose',initial_state()))
    nodes=[x['node'] for x in report['trace'] if x['kind']=='workflow_node_start']
    assert report['status']=='PLAN_READY' and report['repairs']==1
    assert nodes.count('validate_plan')==2 and nodes.count('generate_plan')==2
    assert 'repair_plan' in nodes and 'select_planner' in nodes
    assert 'execute_plan' not in nodes


def test_native_inspector_replan_is_a_graph_transition_with_one_owner():
    from time import time
    from coegent import CoEgent
    from coegent.inspector import Inspector,InspectionSnapshot
    from coegent.planner import ScriptedPlanner
    from coegent.scenarios import initial_state,plan,skill
    from coegent.skills import MockSkills
    class Evidence:
        poses=iter(['B','A'])
        async def snapshot(self,report):
            return InspectionSnapshot(report.mission_id,report.revision,{'robot_state':{'location':next(self.poses)}},{'robot_state':time()})
    inspector=Inspector({'checks':[{'id':'home','source':'robot_state','path':'location','op':'eq','expected':'A'}]},Evidence())
    backend=MockSkills();planner=ScriptedPlanner([plan(skill('get_pose')),plan(skill('get_pose'))])
    r=asyncio.run(CoEgent(planner,backend,inspector=inspector,auto_save_plans=False).run('A',initial_state()))
    nodes=[t['node'] for t in r['trace'] if t['kind']=='workflow_node_start' and t['graph']=='native']
    assert r['status']=='SUCCESS' and r['replans']==1
    assert nodes.count('execute_plan')==2 and nodes.count('inspect_completion')==2
    assert nodes.index('stop_before_replan')<nodes.index('replan')
    assert [c['name'] for c in backend.calls]==['get_pose','stop','get_pose']


def test_graph_cli_is_readonly_and_does_not_construct_models(tmp_path,monkeypatch,capsys):
    import sys
    from coegent import cli
    def denied(*a,**kw):raise AssertionError('graph export must not load a model')
    monkeypatch.setattr(cli,'build_planners',denied)
    out=tmp_path/'native.mmd'
    monkeypatch.setattr(sys,'argv',['coegent','graph','--flow','native','--out',str(out)])
    assert cli.main()==0
    assert 'inspect_completion' in out.read_text() and 'stop_before_replan' in out.read_text()


def test_late_cancelled_graph_cannot_mutate_returned_plan_report():
    import json
    from coegent import PlanGenerator
    from coegent_contracts import RuntimeConfig
    from coegent.scenarios import initial_state,plan,skill
    async def exercise():
        release=asyncio.Event()
        class Slow:
            async def plan(self,context):
                try:await asyncio.Event().wait()
                except asyncio.CancelledError:await release.wait()
                return plan(skill('get_pose'))
        generator=PlanGenerator(Slow(),config=RuntimeConfig(planner_timeout_s=.02))
        report=await generator.generate('pose',initial_state())
        before=json.dumps(report,sort_keys=True)
        release.set()
        for _ in range(100):
            if not generator.pending:break
            await asyncio.sleep(.001)
        assert not generator.pending
        assert json.dumps(report,sort_keys=True)==before
    asyncio.run(exercise())
