import asyncio
from time import time
from coegent.admission import MissionRequest
from coegent.tool_profiles import A2ToolProfile
from coegent.types import State
from a2_execution_adapter.options import Options


def test_host_a2_uses_graph_and_does_not_load_planner_for_request(monkeypatch):
    from a2_execution_adapter import integration
    calls=[]
    async def execute(*args):
        calls.append(1)
        return {'status':'SUCCESS','execution_status':'SUCCESS','reason':'done','steps':1,
                'stop_confirmation':{'confirmed':True},'completed_steps':[]}
    monkeypatch.setattr(integration,'execute_plan',execute)
    plan={'schema_version':'planner.v1','goal':'battery','sequence':[{'type':'skill','name':'RobotBackend.check_battery','args':{}}],'success_criteria':['battery'],'final_outcome':'execute'}
    request=MissionRequest('graph-request',1,time()+20,'battery',plan)
    report=asyncio.run(integration.run_inspected_plan(plan,request,State(),A2ToolProfile(),Options(),None))
    assert report['status']=='SUCCESS' and calls==[1]
    assert [r['node'] for r in report['workflow_trace'] if r['kind']=='workflow_node_start']==['execute_plan','finish']


def test_worker_graph_stops_before_inspection_and_closes_after():
    from a2_execution_adapter.core import APIExecution
    from coegent.inspector import InspectionSnapshot
    order=[]
    class Runtime:
        async def call(self,name,args):order.append('execute');return .9
        async def snapshot(self):return {'robot':{'battery_percent':90}}
        async def cancel(self):order.append('stop');return {'confirmed':True,'reason':'stopped'}
        async def inspection_snapshot(self,r,**kw):
            order.append('inspect')
            return InspectionSnapshot(r.mission_id,r.revision,{'robot_state':{'battery_percent':90}},{'robot_state':time()})
        async def close(self):order.append('close')
    p={'schema_version':'planner.v1','goal':'battery','sequence':[{'type':'skill','name':'RobotBackend.check_battery','args':{}}],'success_criteria':['battery'],'final_outcome':'execute'}
    cfg={'checks':[{'id':'battery','source':'robot_state','path':'battery_percent','op':'eq','expected':90}]}
    r=asyncio.run(APIExecution(Runtime(),A2ToolProfile(),Options(inspector_config=cfg),request=MissionRequest('worker',1,time()+20,'battery',p)).run(p,State()))
    assert r['status']=='SUCCESS' and order==['execute','stop','inspect','close']
    nodes=[t['node'] for t in r['trace'] if t['kind']=='workflow_node_start']
    assert nodes==['execute_plan','stop_motion','inspect_completion','finish']
