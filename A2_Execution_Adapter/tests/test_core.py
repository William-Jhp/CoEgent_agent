import asyncio
from copy import deepcopy
from pathlib import Path
import sys

import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'A2_Execution_Adapter'),str(ROOT/'CoEgent_Agent'),str(ROOT/'Executor_Adapters')]

def skill(name, **args):return {'type':'skill','name':name,'args':args}
def plan(*nodes):return {'schema_version':'planner.v1','goal':'test','sequence':list(nodes),
                         'success_criteria':['API completed'],'final_outcome':'execute'}

class Runtime:
    def __init__(self, values=None, *, stopped=True):
        self.values=values or {};self.calls=[];self.cancelled=0;self.closed=0;self.stopped=stopped
    async def call(self,name,args):
        self.calls.append((name,args))
        v=self.values.get(name,0.9)
        if isinstance(v,Exception):raise v
        if callable(v):return await v()
        return v
    async def snapshot(self):return {'robot':{'battery_percent':90},'world':{},'mission':{},'observations':{}}
    async def cancel(self):
        self.cancelled+=1
        return {'confirmed':self.stopped,'reason':'owned_goal_terminal' if self.stopped else 'cancel_ack_missing'}
    async def close(self):self.closed+=1

def execute(p,runtime=None,options=None):
    from a2_execution_adapter.core import APIExecution
    from a2_execution_adapter.options import Options
    from coegent.tool_profiles import A2ToolProfile
    from coegent.types import State
    r=runtime or Runtime()
    out=asyncio.run(APIExecution(r,A2ToolProfile(),Options.from_dict(options or {})).run(p,State()))
    return out,r

def test_real_engine_dispatches_api_in_order_and_projects_scalar():
    out,r=execute(plan(skill('RobotBackend.check_battery'),skill('RobotBackend.refresh_pose')))
    assert out['status']=='SUCCESS'
    assert [x[0] for x in r.calls]==['RobotBackend.check_battery','RobotBackend.refresh_pose']
    assert out['api_results'][0]['data']['value']==0.9
    assert out['execution_status']=='SUCCESS' and out['goal_status']=='not_evaluated'
    assert r.closed==1

def test_image_tuple_binding_preserves_actual_python_object():
    views=[('front',object())]
    capture=skill('WorldState.capture_views');capture['save_as']='image'
    describe=skill('WorldState.describe',views={'$result':'image','index':0})
    r=Runtime({'WorldState.capture_views':(views,1.,2.,'front'),'WorldState.describe':{'summary':'office'}})
    out,r=execute(plan(capture,describe),r)
    assert out['status']=='SUCCESS'
    assert r.calls[1][1]['views'] is views
    assert out['api_results'][1]['data']['value']['summary']=='office'

@pytest.mark.parametrize('bad',[
    plan(skill('RobotBackend.go_to',x=1,y=2)),
    plan(skill('RobotBackend.check_battery'),skill('nonexistent')),
    plan(skill('RobotBackend.check_battery'),{'type':'condition','condition':{'source':'world.obstacle','operator':'==','value':False},'then':[],'else':[]}),
])
def test_entire_plan_is_rejected_before_first_api_call(bad):
    out,r=execute(bad)
    assert out['status']=='INVALID_PLAN' and r.calls==[] and r.cancelled==0

def test_false_go_to_is_failure_and_never_marks_completed():
    r=Runtime({'RobotBackend.go_to':False})
    out,r=execute(plan(skill('RobotBackend.go_to',x=1,y=2)),r,{'allow_motion':True})
    assert out['status']=='EXPECTED_FAILURE'
    assert out['completed_steps']==[] and r.cancelled==1
    assert 'false' in out['reason']

def test_none_path_is_failure_and_second_call_is_not_run():
    r=Runtime({'GridNavigator.plan':None})
    out,r=execute(plan(skill('GridNavigator.plan',start_xy=[0,0],goal_xy=[1,2]),skill('RobotBackend.check_battery')),r)
    assert out['status']=='EXPECTED_FAILURE' and len(r.calls)==1

def test_route_arrays_are_returned_as_coordinates_not_only_image_metadata():
    import numpy as np
    r=Runtime({'GridNavigator.plan':np.array([[0.,0.],[1.,2.]])})
    out,_=execute(plan(skill('GridNavigator.plan',start_xy=[0,0],goal_xy=[1,2])),r)
    assert out['status']=='SUCCESS'
    assert out['api_results'][0]['data']['value']['path_xy']==[[0.,0.],[1.,2.]]

def test_unconfirmed_stop_escalates_failure():
    r=Runtime({'RobotBackend.go_to':False},stopped=False)
    out,_=execute(plan(skill('RobotBackend.go_to',x=1,y=2)),r,{'allow_motion':True})
    assert out['status']=='SAFETY_ABORT'
    assert out['stop_confirmation']['confirmed'] is False

def test_timeout_cancels_owned_work_and_closes():
    async def hang():await asyncio.sleep(60)
    r=Runtime({'RobotBackend.go_to':hang})
    out,r=execute(plan(skill('RobotBackend.go_to',x=1,y=2)),r,
                  {'allow_motion':True,'call_timeout_s':.02,'cancel_timeout_s':.1})
    assert out['status']=='SAFETY_ABORT' and 'timeout' in out['reason']
    assert r.cancelled==1 and r.closed==1

def test_failed_resource_restore_is_reported_as_unconfirmed_cleanup():
    class RestoreFailure(Runtime):
        async def close(self):raise RuntimeError('speed_restore_failed')
    out,_=execute(plan(skill('RobotBackend.check_battery')),RestoreFailure())
    assert out['status']=='SAFETY_ABORT'
    assert out['stop_confirmation']['confirmed'] is False

def test_condition_reads_live_state_instead_of_input_snapshot():
    from a2_execution_adapter.core import APIExecution
    from a2_execution_adapter.options import Options
    from coegent.tool_profiles import A2ToolProfile
    from coegent.types import State
    p=plan({'type':'condition','condition':{'source':'robot.battery_percent','operator':'>','value':50},
            'then':[skill('RobotBackend.check_battery')],'else':[skill('RobotBackend.refresh_pose')]})
    r=Runtime()
    out=asyncio.run(APIExecution(r,A2ToolProfile(),Options()).run(p,State(robot={'battery_percent':1})))
    assert out['status']=='SUCCESS' and r.calls[0][0]=='RobotBackend.check_battery'

def test_request_for_replan_does_not_claim_success_or_call_following_api():
    p=plan({'type':'replan_required','reason':'need target','constraints':['destination']},skill('RobotBackend.check_battery'))
    p['final_outcome']='replan_required'
    out,r=execute(p)
    assert out['status']=='REPLAN_REQUIRED' and not r.calls

@pytest.mark.parametrize('value',[{'allow_motion':'false'},{'call_timeout_s':float('nan')},{'call_timeout_s':0},{'unknown':1},{'ros_domain_id':73}])
def test_options_reject_unsafe_or_unknown_settings(value):
    from a2_execution_adapter.options import Options
    with pytest.raises(ValueError):Options.from_dict(value)
