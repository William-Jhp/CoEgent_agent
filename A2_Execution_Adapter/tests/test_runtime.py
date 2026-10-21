import json
import asyncio
import ast
import threading
import time
from types import SimpleNamespace
from pathlib import Path
import sys
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

@pytest.mark.parametrize('pose',[None,(0,0,0,0,9),(0,0,0,0,-2),(float('nan'),0,0,0,0)])
def test_bad_pose_never_passes_freshness_check(pose):
    from a2_execution_adapter.ros_runtime import check_pose
    with pytest.raises(RuntimeError):check_pose(pose,1.5)

def test_fresh_pose_is_preserved():
    from a2_execution_adapter.ros_runtime import check_pose
    assert check_pose((1,2,0,.5,.1),1.5)==(1,2,0,.5,.1)

def test_config_uses_scene_map_identity_not_legacy_map_yaml():
    from a2_execution_adapter.ros_runtime import load_source_config
    from a2_execution_adapter.options import Options
    cfg,identity=load_source_config(Options())
    assert cfg['robot_runtime']['map_uuid']=='b64e79a3-b227-4fbc-991d-0b8601d636f9'
    assert cfg['map']['dir'].endswith('2026-10-12-00-10-46-b64e79a3-b227-4fbc-991d-0b8601d636f9')
    assert identity['map_uuid']==cfg['robot_runtime']['map_uuid']

def test_only_matching_owned_request_is_terminal_ack():
    from a2_execution_adapter.ros_runtime import terminal_ack
    rows=[{'req_id':6,'status':'ABORTED','_t':10},{'req_id':5,'status':'ACCEPTED','_t':10}]
    assert terminal_ack(rows,5,9) is None
    rows.append({'req_id':5,'status':'ABORTED','_t':8})
    assert terminal_ack(rows,5,9) is None
    rows.append({'req_id':5,'status':'ABORTED','_t':11})
    assert terminal_ack(rows,5,9)=='ABORTED'

def test_source_localize_cannot_adopt_a_late_foreign_gemma_owner():
    from a2_execution_adapter.ros_runtime import owned_backend_type
    source=Path('/home/gritandgrind/dev/kts/a2-llm-isaac/planner/a2sim/robot/backend.py')
    cls=next(n for n in ast.parse(source.read_text()).body if isinstance(n,ast.ClassDef) and n.name=='RobotBackend')
    fn=next(n for n in cls.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='localize')
    ns={'asyncio':asyncio,'time':time,'RobotError':RuntimeError,'localization_command':lambda *a:json.dumps(a)}
    exec(compile(ast.Module(body=[fn],type_ignores=[]),str(source),'exec'),ns)
    Base=type('SourceBackend',(),{'localize':ns['localize']})
    b=owned_backend_type(Base)();b.seal_task_id('gemma-coegent-owned')
    b.rc={'localization':{'ready_timeout_s':.1}};b.map_uuid='map';b.loc_started=False;b.check_battery=lambda: .9
    sent=[]
    b.ros=SimpleNamespace(loc_status={'state':'READY','task_id':'gemma-user-gui','map_uuid':'map'},
                          pub_loc=object(),publish=lambda *a:sent.append(a))
    with pytest.raises(RuntimeError,match='ownership'):
        asyncio.run(b.localize())
    assert b.task_id=='gemma-coegent-owned' and not b.loc_started and sent==[]

def test_localization_publish_guard_rejects_foreign_start_and_stop():
    from a2_execution_adapter.ros_runtime import guard_localization
    for action in ('START','STOP'):
        with pytest.raises(RuntimeError,match='ownership'):
            guard_localization({'command':action,'task_id':'gemma-coegent-owned','map_uuid':'map'},
                               {'state':'READY','task_id':'gemma-user-gui','map_uuid':'map'},'gemma-coegent-owned','map')

def speed_runtime(delay=0):
    from a2_execution_adapter.ros_runtime import ROSRuntime
    from a2_execution_adapter.options import Options
    r=ROSRuntime(Options(allow_motion=True));started=threading.Event()
    class Backend:
        speed=.25;task_id='gemma-coegent-owned';cancel=asyncio.Event()
        def set_speed(self):
            old=self.speed;started.set();time.sleep(delay);self.speed=.6;return old
        def restore_speed(self,old):self.speed=old
        async def go_to(self,**kw):return True
    r.backend=Backend()
    r.ros=SimpleNamespace(controller_state=lambda:'active',path_status=[],
        loc_status={'state':'READY','task_id':r.backend.task_id},get_param=lambda name:r.backend.speed,destroy_node=lambda:None)
    r.cfg={'robot_runtime':{'speed':{'param':'FollowPath.vx_max'}}}
    r._check_clock=r._check_battery=r._check_safety=lambda:None
    r._check_map=lambda:(0,0,0,0,0)
    return r,started

def test_two_moves_restore_the_original_speed_once():
    async def run():
        r,_=speed_runtime()
        await r._go_to({'x':0,'y':0});await r._go_to({'x':0,'y':0});await r.close()
        assert r.backend.speed==.25
    asyncio.run(run())

def test_cancel_during_sync_speed_change_waits_then_restores():
    async def run():
        r,started=speed_runtime(.05)
        task=asyncio.create_task(r._go_to({'x':0,'y':0}))
        while not started.is_set():await asyncio.sleep(.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        await r.close()
        await asyncio.sleep(.08)  # Observe late completion of the original blocking ROS call.
        assert r.backend.speed==.25
    asyncio.run(run())

def test_cached_stopped_status_cannot_acknowledge_a_queued_start():
    from a2_execution_adapter.ros_runtime import ROSRuntime
    from a2_execution_adapter.options import Options
    async def run():
        r=ROSRuntime(Options(allow_motion=True,cancel_timeout_s=.3))
        sent=[];owner='gemma-coegent-owned'
        r.identity={'map_uuid':'map'};r.owned_localization=True
        r.ros=SimpleNamespace(loc_status={'state':'STOPPED','task_id':None,'map_uuid':None})
        r._loc(SimpleNamespace(data=json.dumps(r.ros.loc_status)))
        r.loc_start_sent=time.monotonic()
        r.backend=SimpleNamespace(task_id=owner,loc_started=True,stop_localization=lambda **kw:sent.append(kw))
        task=asyncio.create_task(r._stop_localization(0))
        await asyncio.sleep(.02)
        assert not task.done() and sent==[]
        r.ros.loc_status={'state':'STARTING','task_id':owner,'map_uuid':'map'};r._loc(SimpleNamespace(data=json.dumps(r.ros.loc_status)))
        while not sent:await asyncio.sleep(.001)
        r.ros.loc_status={'state':'STOPPED','task_id':owner,'map_uuid':'map'};r._loc(SimpleNamespace(data=json.dumps(r.ros.loc_status)))
        await task
        assert r.owned_localization is False and r.backend.loc_started is False
    asyncio.run(run())

def test_invalid_new_battery_message_does_not_refresh_cached_valid_measurement():
    from a2_execution_adapter.ros_runtime import ROSRuntime
    from a2_execution_adapter.options import Options
    r=ROSRuntime(Options());r.battery_at=time.monotonic()
    r._battery(SimpleNamespace(percentage=float('nan')))
    assert r.battery_at==0

def test_localization_ack_is_bound_to_the_actual_received_status_not_an_old_cache():
    from a2_execution_adapter.ros_runtime import ROSRuntime
    from a2_execution_adapter.options import Options
    r=ROSRuntime(Options())
    r._loc(SimpleNamespace(data='{"state":"STOPPED","task_id":"owned","map_uuid":"map"}'))
    original=r.loc_event
    r._loc(SimpleNamespace(data='invalid json'))
    assert r.loc_event==original

def test_explicit_progress_timeout_applies_only_to_worker_config():
    from a2_execution_adapter.ros_runtime import load_source_config
    from a2_execution_adapter.options import Options
    cfg,_=load_source_config(Options(progress_timeout_s=180.))
    assert cfg['robot_runtime']['follow']['stall_s']==180.
    original,_=load_source_config(Options())
    assert original['robot_runtime']['follow']['stall_s']==30.

def test_selected_planning_radius_is_isolated_from_original_source_config():
    from a2_execution_adapter.ros_runtime import load_source_config
    from a2_execution_adapter.options import Options
    cfg,_=load_source_config(Options(planning_radius_m=.52))
    assert cfg['occupancy']['robot_radius']==.52
    original,_=load_source_config(Options())
    assert original['occupancy']['robot_radius']==.28
