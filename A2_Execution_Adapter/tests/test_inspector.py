import asyncio
from time import time, monotonic
import pytest

from coegent import MissionRequest
from coegent.inspector import InspectionSnapshot
from coegent.types import State
from coegent.tool_profiles import A2ToolProfile
from a2_execution_adapter.core import APIExecution
from a2_execution_adapter.options import Options
def skill(name, **args):return {'type':'skill','name':name,'args':args}
def plan(*nodes):return {'schema_version':'planner.v1','goal':'battery','sequence':list(nodes),
    'success_criteria':['API completed'],'final_outcome':'execute'}

class Runtime:
    def __init__(self, values=None, *, stopped=True):
        self.values=values or {};self.cancelled=0;self.closed=0;self.stopped=stopped
    async def call(self,name,args):
        v=self.values.get(name,.9)
        if isinstance(v,Exception):raise v
        return v
    async def snapshot(self):return {'robot':{'battery_percent':90}}
    async def cancel(self):
        self.cancelled+=1
        return {'confirmed':self.stopped,'reason':'owned_stop'}
    async def close(self):self.closed+=1

CFG={'checks':[{'id':'battery','source':'robot_state','path':'battery_percent','op':'eq','expected':90}]}


class Measured(Runtime):
    def __init__(self, **kw):
        super().__init__(**kw); self.reads=[]
    async def inspection_snapshot(self, report, *, include_vision=False):
        assert self.closed==0, 'feedback was released before inspection'
        self.reads.append(self.cancelled)
        return InspectionSnapshot(report.mission_id,report.revision,{'robot_state':{'battery_percent':90}},
                                  {'robot_state':time()})


def run(runtime, p, cfg=CFG):
    req=MissionRequest('m',1,time()+30,'battery',p)
    return asyncio.run(APIExecution(runtime,A2ToolProfile(),Options(inspector_config=cfg),request=req).run(p,State()))


def test_a2_verifies_before_closing_feedback_and_never_treats_api_return_as_measurement():
    runtime=Measured()
    result=run(runtime,plan(skill('RobotBackend.check_battery')))
    assert result['status']=='SUCCESS' and result['mission_success'] is True
    assert result['completion_report']['verified'] is True
    assert runtime.closed==1
    # Runtime with only API success but no trusted evidence cannot prove success.
    result=run(Runtime(),plan(skill('RobotBackend.check_battery')))
    assert result['status']=='REPLAN_REQUIRED'
    assert result['mission_success'] is False


def test_a2_error_stops_before_diagnosis_and_request_returns_replan():
    runtime=Measured(values={'RobotBackend.check_battery':RuntimeError('sensor lost')})
    result=run(runtime,plan(skill('RobotBackend.check_battery')))
    assert result['status']=='REPLAN_REQUIRED'
    assert runtime.reads==[1]
    assert result['inspection']['reason']=='executor_error_report'


def test_a2_failed_stop_is_authoritative():
    result=run(Measured(stopped=False),plan(skill('RobotBackend.check_battery')))
    assert result['status']=='SAFETY_ABORT'
    assert not result['completion_report']['verified']


def test_camera_capture_preserves_original_age_and_uses_bounded_jpeg():
    from a2_execution_adapter.ros_runtime import ROSRuntime
    from PIL import Image
    class Camera:
        def capture_views(self):
            return ([('front',Image.new('RGB',(20,20)))],123.,monotonic()-20,'front_camera')
    runtime=object.__new__(ROSRuntime)
    runtime.battery_at=monotonic(); runtime.safety_at=monotonic()
    async def state():return {'robot':{'battery_percent':90},'observations':{}}
    async def camera():return Camera()
    runtime.snapshot=state;runtime._world=camera
    req=MissionRequest('m',1,time()+30,'view')
    snapshot=asyncio.run(runtime.inspection_snapshot(req,include_vision=True))
    assert time()-snapshot.observed_at['vision']>=19
    assert snapshot.values['vision']['images'][0].startswith('data:image/jpeg;base64,')


def test_a2_inspector_options_validated_before_worker_start():
    with pytest.raises(ValueError):Options(inspector_config={'max_age_s':-1})


def test_host_replans_with_context_new_worker_identity_and_same_deadline(monkeypatch):
    from a2_execution_adapter import integration
    from coegent.planner import ScriptedPlanner
    from coegent.planning import PlanGenerator
    seen=[]
    async def execute(p,request,state,profile,options,registry):
        seen.append((request.mission_id,request.expires_at,options.max_steps))
        return {'status':'REPLAN_REQUIRED' if len(seen)==1 else 'SUCCESS','execution_status':'SUCCESS',
            'reason':'completion_contradicted','steps':1,'completed_steps':[{'name':'RobotBackend.check_battery','args':{}}],
            'remaining_plan':[],'state':{'robot':{'battery_percent':90},'world':{},'mission':{},'observations':{}},
            'stop_confirmation':{'confirmed':True,'reason':'stopped'},
            'inspection':{'decision':'replan','feedback':{'reason':'wrong pose'},'verified':False}}
    monkeypatch.setattr(integration,'execute_plan',execute)
    profile=A2ToolProfile()
    planner=ScriptedPlanner([plan(skill('RobotBackend.refresh_pose'))])
    generator=PlanGenerator(planner,tool_profile=profile)
    req=MissionRequest('root',1,time()+20,'battery',plan(skill('RobotBackend.check_battery')))
    out=asyncio.run(integration.run_inspected_plan(req.plan,req,State(),profile,
        Options(inspector_config=CFG),None,generator=generator,max_replans=1))
    assert out['status']=='SUCCESS' and out['replans']==1 and len(out['execution_attempts'])==2
    assert seen[0][0]!=seen[1][0]
    assert seen[0][1]==seen[1][1]
    assert seen[1][2]==127
    assert planner.contexts[0]['completed_steps']==[{'name':'RobotBackend.check_battery','args':{}}]
    assert planner.contexts[0]['feedback']['inspection']['decision']=='replan'


def test_host_request_no_planner_and_unconfirmed_stop_never_retry(monkeypatch):
    from a2_execution_adapter import integration
    seen=[]
    async def execute(*args):
        seen.append(True)
        return {'status':'REPLAN_REQUIRED','inspection':{'decision':'replan'},
            'stop_confirmation':{'confirmed':False,'reason':'missing'}}
    monkeypatch.setattr(integration,'execute_plan',execute)
    req=MissionRequest('m',1,time()+20,'battery',plan(skill('RobotBackend.check_battery')))
    out=asyncio.run(integration.run_inspected_plan(req.plan,req,State(),A2ToolProfile(),
        Options(inspector_config=CFG),None,max_replans=3))
    assert len(seen)==1
    assert out['status']=='SAFETY_ABORT'


def test_inspection_cannot_report_success_after_original_deadline():
    class Slow(Measured):
        async def inspection_snapshot(self,*args,**kwargs):
            await asyncio.sleep(.06)
            return await super().inspection_snapshot(*args,**kwargs)
    p=plan(skill('RobotBackend.check_battery'))
    req=MissionRequest('m',1,time()+.03,'battery',p)
    result=asyncio.run(APIExecution(Slow(),A2ToolProfile(),Options(inspector_config=CFG,mission_timeout_s=.03),
        request=req).run(p,State()))
    assert result['status']=='SAFETY_ABORT'
    assert not result['completion_report']['verified']


def test_host_cannot_accept_worker_success_after_deadline(monkeypatch):
    from a2_execution_adapter import integration
    async def slow(*args):
        await asyncio.sleep(.02)
        return {'status':'SUCCESS','execution_status':'SUCCESS','reason':'verified',
            'stop_confirmation':{'confirmed':True,'reason':'stopped'},
            'completion_report':{'verified':True}}
    monkeypatch.setattr(integration,'execute_plan',slow)
    profile=A2ToolProfile();options=Options(inspector_config=CFG)
    req=MissionRequest('m',1,time()+.01,'battery',plan(skill('RobotBackend.check_battery')))
    out=asyncio.run(integration.run_inspected_plan(req.plan,req,State(),profile,options,None))
    assert out['status']=='SAFETY_ABORT'
    assert not out['completion_report']['verified']


def test_failed_api_is_preserved_for_replanning_context():
    result=run(Measured(values={'RobotBackend.check_battery':RuntimeError('sensor lost')}),
        plan(skill('RobotBackend.check_battery')))
    assert result['failed_step']['node']['name']=='RobotBackend.check_battery'


def test_pending_generation_is_owned_and_model_closes_only_after_drain(monkeypatch):
    from a2_execution_adapter import integration
    from coegent.planner import ScriptedPlanner
    from coegent.planning import PlanGenerator
    async def exercise():
        release=asyncio.Event();closed=[]
        class Slow(ScriptedPlanner):
            async def plan(self,context):
                try:await release.wait()
                except asyncio.CancelledError:await release.wait()
                return await super().plan(context)
            def close(self):closed.append(True)
        planner=Slow([plan(skill('RobotBackend.refresh_pose'))])
        profile=A2ToolProfile();generator=PlanGenerator(planner,tool_profile=profile)
        async def execute(*args):
            return {'status':'REPLAN_REQUIRED','reason':'mismatch','steps':1,'inspection':{'decision':'replan'},
                'stop_confirmation':{'confirmed':True},'state':State().snapshot()}
        monkeypatch.setattr(integration,'execute_plan',execute)
        req=MissionRequest('m',1,time()+1,'battery',plan(skill('RobotBackend.check_battery')))
        try:
            out=await integration.run_inspected_plan(req.plan,req,State(),profile,
                Options(inspector_config=CFG,cancel_timeout_s=.005),None,generator=generator,
                max_replans=1,deadline=monotonic()+.02)
            assert out['status']=='SAFETY_ABORT'
            assert generator.pending
            assert integration.close_models(planner,None,generator=generator) is False
            assert closed==[]
        finally:
            release.set();await asyncio.sleep(.02)
        assert not generator.pending
        assert closed==[True]
    asyncio.run(exercise())


def test_host_rejects_replaying_completed_motion_even_when_model_requests_it(monkeypatch):
    from a2_execution_adapter import integration
    from coegent.planner import ScriptedPlanner
    from coegent.planning import PlanGenerator
    seen=[]
    p=plan(skill('RobotBackend.go_to',x=1,y=2))
    async def execute(*args):
        seen.append(True)
        return {'status':'REPLAN_REQUIRED','reason':'mismatch','steps':1,
            'completed_steps':[{'name':'RobotBackend.go_to','args':{'x':1,'y':2}}],
            'inspection':{'decision':'replan'},'stop_confirmation':{'confirmed':True},'state':State().snapshot()}
    monkeypatch.setattr(integration,'execute_plan',execute)
    profile=A2ToolProfile();generator=PlanGenerator(ScriptedPlanner([p]),tool_profile=profile)
    req=MissionRequest('m',1,time()+20,'go',p)
    out=asyncio.run(integration.run_inspected_plan(p,req,State(),profile,
        Options(inspector_config=CFG,allow_motion=True),None,generator=generator,max_replans=1))
    assert len(seen)==1
    assert out['reason']=='continuation_repeats_completed_api'
    assert out['status']=='REPLAN_REQUIRED'


def test_real_ros_stop_preserves_localization_until_pose_inspection():
    """A terminal path is stopped, measured, then its owned SLAM is released."""
    from types import SimpleNamespace
    import threading
    from a2_execution_adapter.ros_runtime import ROSRuntime
    events=[]
    runtime=object.__new__(ROSRuntime)
    runtime.options=Options(inspector_config={'checks':[{
        'id':'home','source':'robot_state','path':'pose_xy','op':'near',
        'expected':[0,0],'tolerance':.75}]})
    runtime.backend=SimpleNamespace(cancel=threading.Event())
    runtime.active_req=None;runtime.owned_localization=True;runtime._stop_cache=None
    runtime._effects=set();runtime._speed_old=None;runtime.world=None
    runtime.executor=None;runtime.thread=None;runtime.initialized=False
    runtime.ros=SimpleNamespace(destroy_node=lambda:events.append('close'))
    runtime.battery_at=runtime.safety_at=monotonic()
    async def stop_localization(*args,**kwargs):
        events.append('localization_stopped');runtime.owned_localization=False
    runtime._stop_localization=stop_localization
    async def snapshot():
        events.append('measured')
        return {'robot':{'localization_valid':runtime.owned_localization},'observations':
            {'map_pose':{'x':.03,'y':-.4,'z':0,'yaw':0,'age_sim_s':0}} if runtime.owned_localization else {}}
    runtime.snapshot=snapshot
    async def call(*args):return .9
    runtime.call=call
    result=run(runtime,plan(skill('RobotBackend.check_battery')),runtime.options.inspector_config)
    assert result['status']=='SUCCESS',result.get('inspection')
    assert result['inspection']['checks'][0]['actual']==[.03,-.4]
    assert runtime.backend.cancel.is_set(), 'motion must be stopped before completion'
    assert events.index('measured')<events.index('localization_stopped')<events.index('close')
    assert runtime.owned_localization is False
