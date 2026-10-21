import asyncio
import math
from types import SimpleNamespace
import pytest
from a2_execution_adapter.options import Options

def test_pose_source_requires_explicit_known_source_and_scene():
    assert Options().pose_source=='cuvslam'
    with pytest.raises(ValueError):Options(pose_source='anything')
    with pytest.raises(ValueError):Options(pose_source='isaac_ground_truth')
    assert Options(pose_source='isaac_ground_truth',scene_dir='/scene').pose_source=='isaac_ground_truth'

def test_nonidentity_world_to_map_transform_preserves_heading_and_planar_height():
    from a2_execution_adapter.isaac_pose import map_sim_pose
    meta={'map_selection':{'map_uuid':'map'},'place_registration':{'translation_xy':[10.,20.],'yaw_rad':math.pi/2}}
    assert map_sim_pose(meta,[2.,3.,.5],[0.,0.,0.,1.])==pytest.approx((7.,22.,0.,math.pi/2))

@pytest.mark.parametrize('field,value',[('frame','map'),('child_frame','base_link'),('stamp',1.),('received',1.),('quaternion',[0.,0.,0.,0.]),('position',[float('nan'),0.,.5])])
def test_invalid_or_stale_simulator_samples_cannot_drive(field,value):
    from a2_execution_adapter.isaac_pose import checked_sim_sample
    sample={'frame':'sim_world','child_frame':'sim_base','stamp':10.,'received':20.,'position':[0.,0.,.5],'quaternion':[0.,0.,0.,1.],'velocity':[0.,0.,0.]}
    sample[field]=value
    with pytest.raises(RuntimeError):checked_sim_sample(sample,now=20.1,clock=10.1)

def test_live_office_and_stopped_native_slam_are_required():
    from a2_execution_adapter.isaac_pose import require_office
    require_office(['teamgrit_isaac_planar_sim'],{'state':'STOPPED'})
    for names,status in [([],{'state':'STOPPED'}),(['teamgrit_isaac_planar_sim','visual_slam_node'],{'state':'STOPPED'}),(['teamgrit_isaac_planar_sim'],{'state':'READY','task_id':'other'}),(['teamgrit_isaac_planar_sim'],None)]:
        with pytest.raises(RuntimeError):require_office(names,status)

def test_sim_pose_status_is_internal_and_keeps_native_status_separate():
    from a2_execution_adapter.isaac_pose import IsaacPoseRuntime
    r=IsaacPoseRuntime(Options(pose_source='isaac_ground_truth',scene_dir='/scene'))
    r.ros=SimpleNamespace(loc_status={'state':'STOPPED'},get_node_names=lambda:['teamgrit_isaac_planar_sim'])
    r.identity={'map_uuid':'map'};r.backend=SimpleNamespace(task_id='gemma-coegent-test')
    r.bridge_active=True;r.mapping_status={'state':'IDLE'}
    assert r._localization_status()['source']=='isaac_ground_truth'
    assert r.ros.loc_status=={'state':'STOPPED'}
    r.ros.loc_status={'state':'READY','task_id':'foreign'}
    with pytest.raises(RuntimeError):r._localization_status()

def test_fresh_but_old_native_tf_cannot_be_used_as_simulator_pose():
    import time
    from a2_execution_adapter.isaac_pose import IsaacPoseRuntime
    r=IsaacPoseRuntime(Options(pose_source='isaac_ground_truth',scene_dir='/scene'))
    r.ros=SimpleNamespace(loc_status={'state':'STOPPED'},pose=lambda:(0.,0.,0.,0.,0.),get_node_names=lambda:['teamgrit_isaac_planar_sim'])
    r.identity={'map_uuid':'map'};r.backend=SimpleNamespace(task_id='gemma-coegent-test')
    r.cfg={'robot_runtime':{'follow':{'tf_max_age_s':1.5}}};r.bridge_active=True;r.mapping_status={'state':'IDLE'};r.clock=10.
    r.meta={'place_registration':{'translation_xy':[10.,20.],'yaw_rad':0.}}
    r.sim_sample={'frame':'sim_world','child_frame':'sim_base','stamp':10.,'received':time.monotonic(),'position':[0.,0.,.5],'quaternion':[0.,0.,0.,1.],'velocity':[0.,0.,0.]}
    with pytest.raises(RuntimeError,match='tf.*(match|disagree)'):r._check_map()

def test_bridge_stop_requires_two_new_zero_velocity_samples():
    from a2_execution_adapter.isaac_pose import IsaacPoseRuntime
    async def run():
        r=IsaacPoseRuntime(Options(pose_source='isaac_ground_truth',scene_dir='/scene',cancel_timeout_s=.2))
        r.bridge_active=r.owned_localization=True
        import time
        r.clock=10.;r.sim_sample={'frame':'sim_world','child_frame':'sim_base','stamp':10.,'received':time.monotonic(),'position':[0.,0.,.5],'quaternion':[0.,0.,0.,1.],'velocity':[0.,0.,0.]}
        async def fresh():
            await asyncio.sleep(.06);r.sim_sample={**r.sim_sample,'received':time.monotonic()}
        update=asyncio.create_task(fresh());await r._stop_localization();await update
        assert not r.bridge_active and not r.owned_localization
    asyncio.run(run())

def test_motion_or_missing_stop_measurement_cannot_claim_bridge_stopped():
    from a2_execution_adapter.isaac_pose import IsaacPoseRuntime
    async def run():
        r=IsaacPoseRuntime(Options(pose_source='isaac_ground_truth',scene_dir='/scene',cancel_timeout_s=.12))
        r.bridge_active=r.owned_localization=True
        import time
        r.clock=10.;r.sim_sample={'frame':'sim_world','child_frame':'sim_base','stamp':10.,'received':time.monotonic(),'position':[0.,0.,.5],'quaternion':[0.,0.,0.,1.],'velocity':[.1,0.,0.]}
        with pytest.raises(RuntimeError,match='stop_not_confirmed'):await r._stop_localization()
        assert r.bridge_active and r.owned_localization
    asyncio.run(run())

def test_native_mapping_start_after_initialization_is_rejected():
    from a2_execution_adapter.isaac_pose import IsaacPoseRuntime
    r=IsaacPoseRuntime(Options(pose_source='isaac_ground_truth',scene_dir='/scene'))
    r.ros=SimpleNamespace(loc_status={'state':'STOPPED'},get_node_names=lambda:['teamgrit_isaac_planar_sim','visual_slam_node'])
    r.identity={'map_uuid':'map'};r.backend=SimpleNamespace(task_id='gemma-coegent-test');r.bridge_active=True;r.mapping_status={'state':'IDLE'}
    with pytest.raises(RuntimeError):r._localization_status()

def test_latched_driving_fault_does_not_hide_fresh_zero_velocity_stop_evidence():
    import time
    from a2_execution_adapter.isaac_pose import IsaacPoseRuntime
    async def run():
        r=IsaacPoseRuntime(Options(pose_source='isaac_ground_truth',scene_dir='/scene',cancel_timeout_s=.3))
        r.bridge_active=r.owned_localization=True;r.pose_fault='tracking_conflict';r.clock=10.
        sample={'frame':'sim_world','child_frame':'sim_base','stamp':10.,'received':time.monotonic(),'position':[0.,0.,.5],'quaternion':[0.,0.,0.,1.],'velocity':[0.,0.,0.]};r.sim_sample=sample
        async def fresh():
            await asyncio.sleep(.06);r.sim_sample={**sample,'received':time.monotonic()}
        update=asyncio.create_task(fresh());await r._stop_localization();await update
        assert not r.bridge_active and r.pose_fault=='tracking_conflict'
    asyncio.run(run())

@pytest.mark.parametrize('status',[None,{'state':'STARTING'},{'state':'MAPPING'},{'state':'ENDING'},{'state':'BUILDING'},{'state':'CANCELING'},{'state':'unknown'}])
def test_mapping_session_or_unknown_state_cannot_share_sim_pose(status):
    from a2_execution_adapter.isaac_pose import require_idle_mapping
    with pytest.raises(RuntimeError):require_idle_mapping(status)

def test_private_bridge_requires_explicit_simulator_pose_and_owns_topics():
    from a2_execution_adapter.isaac_pose import bridge_command,configure_private_topics
    with pytest.raises(ValueError):Options(private_path_bridge=True)
    o=Options(pose_source='isaac_ground_truth',scene_dir='/scene',private_path_bridge=True)
    cfg={'robot_runtime':{'topics':{'path_goal':'/path_follower/goal','path_cancel':'/path_follower/cancel','path_status':'/path_follower/status'}}}
    original=configure_private_topics(cfg,'test123')
    assert original['path_status']=='/path_follower/status'
    assert cfg['robot_runtime']['topics']['path_goal']=='/coegent/test123/goal'
    cmd=bridge_command(o,cfg,'test123')
    assert 'detour.stall_s:=30.0' in cmd and 'detour.cooldown_s:=5.0' in cmd
    assert 'goal_topic:=/coegent/test123/goal' in cmd
    assert 'cancel_topic:=/coegent/test123/cancel' in cmd
    assert 'status_topic:=/coegent/test123/status' in cmd
    assert 'detour_enabled:=true' in cmd and 'use_sim_time:=true' in cmd

def test_generated_private_ros_topic_token_never_begins_with_a_digit(monkeypatch):
    import a2_execution_adapter.isaac_pose as mod
    monkeypatch.setattr(mod,'uuid4',lambda:SimpleNamespace(hex='0123456789abcdef'))
    r=mod.IsaacPoseRuntime(Options(pose_source='isaac_ground_truth',scene_dir='/scene',private_path_bridge=True))
    assert r.path_suffix[0].isalpha()

def test_foreign_status_never_enters_private_ack_stream_and_blocks_active_drive():
    import json
    from a2_execution_adapter.isaac_pose import IsaacPoseRuntime
    r=IsaacPoseRuntime(Options(pose_source='isaac_ground_truth',scene_dir='/scene',private_path_bridge=True))
    r.ros=SimpleNamespace(path_status=[])
    r._foreign_path_status(SimpleNamespace(data=json.dumps({'req_id':1,'status':'ACCEPTED'})))
    assert r.ros.path_status==[]
    with pytest.raises(RuntimeError,match='foreign'):r._check_foreign_path()
    r._foreign_path_status(SimpleNamespace(data=json.dumps({'req_id':1,'status':'ABORTED'})))
    r._check_foreign_path()

def test_private_bridge_patch_never_cancels_all_action_server_goals():
    from a2_execution_adapter.private_bridge import patched_source
    source='void cancel() {controller_client_->async_cancel_all_goals();}\nif (cancel_requested_) { controller_client_->async_cancel_goal(goal_handle); }'
    fixed=patched_source(source)
    assert 'async_cancel_all_goals' not in fixed
    assert 'async_cancel_goal(goal_handle)' in fixed
    with pytest.raises(ValueError):patched_source('unexpected source version')

def test_current_owned_mission_contact_blocks_further_motion():
    import json
    from a2_execution_adapter.isaac_pose import IsaacPoseRuntime
    r=IsaacPoseRuntime(Options(pose_source='isaac_ground_truth',scene_dir='/scene'))
    r.clock=10.;r.active_req=2;r.bridge_active=True
    r._collision(SimpleNamespace(data=json.dumps({'event':'contact_blocked','body':'sim_base','sim_time':10.,'obstacle':'cupboard'})))
    assert r.pose_fault=='simulator_contact_blocked:cupboard'

def test_old_or_unowned_contact_is_not_applied_to_new_mission():
    import json
    from a2_execution_adapter.isaac_pose import IsaacPoseRuntime
    r=IsaacPoseRuntime(Options(pose_source='isaac_ground_truth',scene_dir='/scene'))
    r.clock=10.;r.active_req=2;r.bridge_active=True
    r._collision(SimpleNamespace(data=json.dumps({'event':'contact_blocked','body':'sim_base','sim_time':1.,'obstacle':'old'})))
    assert r.pose_fault is None

def test_short_segment_tracks_route_distance_and_preserves_corner():
    import numpy as np
    from a2_execution_adapter.isaac_pose import first_route_segment
    pts=np.array([[0.,0.],[3.,0.],[3.,4.]])
    segment,done=first_route_segment(pts,4.)
    assert not done
    np.testing.assert_allclose(segment,[[0.,0.],[3.,0.],[3.,1.]])
    segment,done=first_route_segment(pts,10.)
    assert done
    np.testing.assert_allclose(segment,pts)

def test_segmented_go_to_never_claims_success_after_intermediate_failure():
    import numpy as np
    from a2_execution_adapter.isaac_pose import segmented_go_to
    calls=[]
    async def follow(path):calls.append(path);return False
    backend=SimpleNamespace(refresh_pose=lambda:SimpleNamespace(x=0.,y=0.),nav=SimpleNamespace(plan=lambda *a:np.array([[0.,0.],[10.,0.]])),follow=follow,cancel=SimpleNamespace(is_set=lambda:False),last_failure='',_segment_length=4.,emit=lambda event:asyncio.sleep(0))
    assert asyncio.run(segmented_go_to(backend,10.,0.)) is False
    assert len(calls)==1

def test_segmented_go_to_replans_remaining_path_and_finishes_only_at_final_target():
    import numpy as np
    from a2_execution_adapter.isaac_pose import segmented_go_to
    state=SimpleNamespace(x=0.,y=0.);calls=[]
    async def follow(path):state.x,state.y=path[-1];calls.append(path);return True
    backend=SimpleNamespace(refresh_pose=lambda:state,nav=SimpleNamespace(plan=lambda start,goal:np.array([start,goal])),follow=follow,cancel=SimpleNamespace(is_set=lambda:False),last_failure='',_segment_length=4.,emit=lambda event:asyncio.sleep(0))
    assert asyncio.run(segmented_go_to(backend,10.,0.)) is True
    assert len(calls)==3 and state.x==10.

def test_corner_is_a_local_goal_before_following_the_next_direction():
    import numpy as np
    from a2_execution_adapter.isaac_pose import first_route_segment
    pts=np.array([[0.,0.],[1.,0.],[2.,0.],[2.,1.],[2.,2.]])
    segment,done=first_route_segment(pts,4.,stop_at_corner=True)
    assert not done
    np.testing.assert_allclose(segment,[[0.,0.],[1.,0.],[2.,0.]])

def test_corner_inside_controller_arrival_band_stays_in_path_without_becoming_same_goal_again():
    import numpy as np
    from a2_execution_adapter.isaac_pose import first_route_segment
    # Controller accepted the previous corner within .30m; the snapped grid
    # start can be another .1m away. Keep the corner in the next polyline.
    pts=np.array([[0.,0.],[.35,0.],[.35,-1.],[.35,-5.]])
    segment,done=first_route_segment(pts,4.,stop_at_corner=True)
    assert not done
    assert segment[-1,1]<-3.
    np.testing.assert_allclose(segment[1],[.35,0.])

def test_shortcut_margin_rejects_tight_shortcuts_without_removing_narrow_route_nodes():
    from a2_execution_adapter.isaac_pose import add_shortcut_margin
    calls=[]
    graph=object()
    nav=SimpleNamespace(graph=graph,line_free=lambda a,b,margin=0,max_mult=None:calls.append((margin,max_mult)) or margin<.4)
    add_shortcut_margin(nav,.48)
    assert nav.line_free((0,0),(1,1),max_mult=2.) is False
    assert calls==[(.48,2.)] and nav.graph is graph

@pytest.mark.parametrize('value',[True,-1.,float('nan'),float('inf')])
def test_invalid_shortcut_margin_is_rejected(value):
    with pytest.raises(ValueError):Options(pose_source='isaac_ground_truth',scene_dir='/scene',shortcut_margin_m=value)

def test_shortcut_margin_is_opt_in_for_segmented_isaac_navigation():
    with pytest.raises(ValueError):Options(shortcut_margin_m=.48)
    with pytest.raises(ValueError):Options(pose_source='isaac_ground_truth',scene_dir='/scene',shortcut_margin_m=.48)
    assert Options(pose_source='isaac_ground_truth',scene_dir='/scene',path_segment_m=4.,shortcut_margin_m=.48).shortcut_margin_m==.48
