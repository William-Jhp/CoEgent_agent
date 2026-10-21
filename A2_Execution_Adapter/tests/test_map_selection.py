import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from a2_execution_adapter.options import Options
from coegent.tool_profiles import A2ToolProfile, profile_db


def bundle(tmp_path):
    p=tmp_path/'scene';p.mkdir()
    (p/'map_scene.npz').write_bytes(b'selected-grid')
    meta={'map_dir':'/rig/teamgrit-sim/runtime/maps/2026-10-16-05-20-35-9230e3f6-084f-4330-a654-945fb5ab398b',
          'map_selection':{'map_uuid':'9230e3f6-084f-4330-a654-945fb5ab398b','backend':'cuvslam',
                           'profile':'a2_sim_cuvslam360.yaml','native_files':{}},
          'annotations':{'places':[{'name':'map test point','x':1.,'y':0.}]}}
    (p/'scene_meta.json').write_text(json.dumps(meta))
    return p


def test_selected_map_places_replace_lidar_places_and_separate_catalog(tmp_path):
    selected=A2ToolProfile(scene_dir=bundle(tmp_path));default=A2ToolProfile()
    assert selected.tool_context['places']==[{'name':'map test point','x':1.,'y':0.}]
    assert selected.tool_context['map_selection']['map_uuid']=='9230e3f6-084f-4330-a654-945fb5ab398b'
    assert selected.registry.fingerprint!=default.registry.fingerprint
    assert profile_db(selected)!=profile_db(default)


def test_selected_scene_mount_and_worker_path_are_explicit(tmp_path):
    from a2_execution_adapter.transport import docker_command
    options=Options(scene_dir=str(bundle(tmp_path)))
    cmd=docker_command(options,'coegent-a2-test')
    assert str(tmp_path/'scene')+':/scene:ro' in cmd


def test_scene_binding_checks_selected_scene_not_legacy_scene(tmp_path):
    from a2_execution_adapter.binding import scene_binding
    scene=bundle(tmp_path)
    before=scene_binding(A2ToolProfile().root,scene)
    (scene/'map_scene.npz').write_bytes(b'changed-grid')
    assert scene_binding(A2ToolProfile().root,scene)!=before


def test_map_native_payload_hash_mismatch_is_rejected_before_ros(tmp_path):
    from a2_execution_adapter.ros_runtime import load_source_config
    scene=bundle(tmp_path);meta=json.loads((scene/'scene_meta.json').read_text())
    meta['map_selection']['native_files']={'cuvslam_map/data.mdb':'0'*64}
    (scene/'scene_meta.json').write_text(json.dumps(meta))
    with pytest.raises(ValueError,match='native.*(hash|changed|mismatch)'):
        load_source_config(Options(scene_dir=str(scene)))


def test_failed_localization_reset_requires_matching_error_owner_and_fresh_stopped():
    from a2_execution_adapter.ros_runtime import ROSRuntime
    async def run():
        r=ROSRuntime(Options(allow_motion=True,cancel_timeout_s=.3));sent=[]
        r.identity={'map_uuid':'map'}
        def publish(msg):
            sent.append(json.loads(msg.data))
            r._loc(SimpleNamespace(data=json.dumps({'state':'STOPPED','task_id':None,'map_uuid':None,'reason':'reset'})))
        r.ros=SimpleNamespace(loc_status={'state':'ERROR','task_id':'gemma-coegent-owned','map_uuid':'map','reason':'process_exit'},
                              pub_loc=SimpleNamespace(publish=publish))
        # Message factory avoids importing ROS during safe unit tests.
        await r.reset_failed_localization('gemma-coegent-owned','map',message_factory=lambda data:SimpleNamespace(data=data))
        assert sent==[{'command':'RESET'}]
        r.ros.loc_status={'state':'READY','task_id':'other','map_uuid':'map'}
        with pytest.raises(RuntimeError,match='ownership|error'):
            await r.reset_failed_localization('gemma-coegent-owned','map',message_factory=lambda data:SimpleNamespace(data=data))
        assert len(sent)==1
    asyncio.run(run())


def test_reconciliation_preserves_original_failure_and_releases_only_verified_localization(tmp_path):
    from a2_execution_adapter.journal import Journal
    failure={'status':'SAFETY_ABORT','stop_confirmation':{'confirmed':False},'completed_steps':[],
             'api_results':[{'name':'RobotBackend.go_to','reason':'RobotError:localization ERROR: process_exit'}],
             'runtime_ownership':{'task_id':'gemma-coegent-old','map_uuid':'map','path_dispatched':False},
             'map_identity':{'map_uuid':'map'},'adapter_trace':[], 'trace':[]}
    with Journal(tmp_path,'robot') as j:
        j.begin('old',1,{'request':'original'},motion=True);j.finish('old',1,failure)
        with pytest.raises(ValueError):j.reconcile_localization('old',1,{'confirmed':False})
        proof={'confirmed':True,'task_id':'gemma-coegent-old','map_uuid':'map',
               'before':{'state':'ERROR','reason':'process_exit','task_id':'gemma-coegent-old','map_uuid':'map'},
               'after':{'state':'STOPPED','reason':'reset','task_id':None,'map_uuid':None}}
        with pytest.raises(ValueError):j.reconcile_localization('old',1,{**proof,'task_id':'gemma-coegent-other'})
        j.reconcile_localization('old',1,proof)
        replay=j.begin('old',1,{'request':'original'},motion=True)
        assert replay['status']=='SAFETY_ABORT' and replay['stop_confirmation']['confirmed'] is False
        assert j.begin('new',1,{'request':'new'},motion=True) is None


def test_reconciliation_rejects_a_record_with_any_path_dispatch(tmp_path):
    from a2_execution_adapter.journal import Journal
    failure={'completed_steps':[],'api_results':[{'name':'RobotBackend.go_to','reason':'RobotError:localization ERROR: process_exit'}],
             'runtime_ownership':{'task_id':'gemma-coegent-old','map_uuid':'map','path_dispatched':True}}
    with Journal(tmp_path,'robot') as j:
        j.begin('old',1,{},motion=True);j.finish('old',1,{**failure,'stop_confirmation':{'confirmed':False}})
        with pytest.raises(ValueError,match='path|ownership'):j.failed_localization('old',1)


def test_grid_changed_after_planning_cannot_dispatch(tmp_path):
    from a2_execution_adapter.integration import execute_plan
    from coegent.admission import MissionRequest
    from coegent.types import State
    import time
    scene=bundle(tmp_path);profile=A2ToolProfile(scene_dir=scene)
    (scene/'map_scene.npz').write_bytes(b'changed-after-planning')
    report=asyncio.run(execute_plan({},MissionRequest('changed',1,time.time()+30,'test'),State(),profile,
                                   Options(scene_dir=str(scene)),None))
    assert report['status']=='INVALID_PLAN' and 'changed' in report['reason']


def test_active_localization_profile_and_resolver_must_match_selected_map(tmp_path):
    from a2_execution_adapter.ros_runtime import controller_binding
    scene=bundle(tmp_path);root=A2ToolProfile().root
    proof=controller_binding(root,scene,'/repo/teamgrit-sim/config/a2_sim_cuvslam360.yaml','/repo/teamgrit-sim/runtime/maps')
    assert proof['profile']=='a2_sim_cuvslam360.yaml'
    for profile,maps in [('/repo/teamgrit-sim/config/a2_sim_cuvslam.yaml','/repo/teamgrit-sim/runtime/maps'),
                         ('/repo/teamgrit-sim/config/a2_sim_cuvslam360.yaml','/repo/other/maps')]:
        with pytest.raises(ValueError,match='profile|resolver'):controller_binding(root,scene,profile,maps)


def test_legacy_recovery_requires_exact_original_report_and_single_owner(tmp_path):
    from a2_execution_adapter.journal import Journal
    original={'mission_id':'old','revision':1,'status':'SAFETY_ABORT','completed_steps':[],
              'api_results':[{'name':'RobotBackend.go_to','reason':'RobotError:localization ERROR: process_exit'}],
              'trace':[],'map_identity':{'map_uuid':'map'},'stop_confirmation':{'confirmed':False}}
    host={**original,'adapter_trace':[{'kind':'a2_worker','event':{'kind':'backend_event','type':'localization','state':'START',
                                                               'task_id':'gemma-coegent-old','map_uuid':'map'}}]}
    with Journal(tmp_path,'robot') as j:
        j.begin('old',1,{},motion=True);j.finish('old',1,original)
        with pytest.raises(ValueError,match='ownership'):j.failed_localization('old',1)
        with pytest.raises(ValueError,match='differs'):j.failed_localization('old',1,{**host,'mission_id':'other'})
        assert j.failed_localization('old',1,host)['runtime_ownership']['task_id']=='gemma-coegent-old'
        path=deepcopy(host);path['adapter_trace'].append({'kind':'a2_worker','event':{'type':'path','req_id':123}})
        with pytest.raises(ValueError,match='path'):j.failed_localization('old',1,path)


def test_grid_changed_during_preparation_is_not_sent_to_worker(tmp_path):
    from a2_execution_adapter.integration import execute_plan
    from a2_execution_adapter.adapter import A2ExecutorAdapter
    from coegent.admission import MissionRequest
    from coegent.types import State
    import time
    scene=bundle(tmp_path);profile=A2ToolProfile(scene_dir=scene)
    class Registry:
        def resolve(self,name):
            (scene/'map_scene.npz').write_bytes(b'changed-during-adapter-creation')
            class NoDispatch(A2ExecutorAdapter):
                async def execute(self,prepared):raise AssertionError('changed scene must never reach transport')
            return NoDispatch
    plan={'schema_version':'planner.v1','goal':'battery','sequence':[{'type':'skill','name':'RobotBackend.check_battery','args':{}}],
          'success_criteria':['battery'],'final_outcome':'execute'}
    report=asyncio.run(execute_plan(plan,MissionRequest('race',1,time.time()+30,'test'),State(),profile,
                                   Options(scene_dir=str(scene)),Registry()))
    assert report['status']=='INVALID_PLAN' and 'changed' in report['reason']
