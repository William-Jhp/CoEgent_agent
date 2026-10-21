"""Explicit bindings to the installed A2 APIs. ROS imports occur only in open()."""
import asyncio
from copy import deepcopy
import importlib.util
import json
import math
from pathlib import Path
import sys
import threading
import time
from uuid import uuid4

TERMINAL={'REACHED','FAILED','ABORTED','REJECTED'}

def owned_backend_type(base):
    """Prevent source localize() from adopting an unrelated gemma-* task."""
    class OwnedBackend(base):
        @property
        def task_id(self):return self._owned_task_id
        @task_id.setter
        def task_id(self,value):
            if getattr(self,'_identity_sealed',False) and value!=self._owned_task_id:
                raise RuntimeError('localization_ownership_change_refused')
            self._owned_task_id=value
        def seal_task_id(self,value):
            self.task_id=value;self._identity_sealed=True
    return OwnedBackend

def guard_localization(command,status,owner,map_uuid):
    if command.get('task_id')!=owner or command.get('map_uuid')!=map_uuid:
        raise RuntimeError('localization_command_ownership_mismatch')
    state=status.get('state')
    if command.get('command')=='START':
        if state=='STOPPING' or (state in ('STARTING','READY') and status.get('task_id')!=owner):
            raise RuntimeError('localization_ownership_conflict')
    elif command.get('command')=='STOP':
        if status.get('task_id')!=owner:raise RuntimeError('localization_stop_ownership_conflict')
    else:raise RuntimeError('unknown_localization_command')

def check_pose(pose,max_age):
    if pose is None or len(pose)!=5 or not all(math.isfinite(float(x)) for x in pose):
        raise RuntimeError('missing_or_invalid_map_pose')
    if not -.25<=pose[4]<=max_age:raise RuntimeError('stale_or_future_map_pose')
    return pose

def terminal_ack(rows,req_id,since):
    matches=[r for r in list(rows) if r.get('req_id')==req_id and r.get('_t',0)>=since and str(r.get('status','')).upper() in TERMINAL]
    return str(matches[-1]['status']).upper() if matches else None

def controller_binding(root,scene_dir,profile_path,maps_root):
    from coegent.a2_maps import native_directory
    root=Path(root).resolve();meta=json.loads((Path(scene_dir)/'scene_meta.json').read_text())
    selected=meta['map_selection'];native=native_directory(root,meta)
    def mapped(raw):
        if raw.startswith(('/repo/','/rig/')):raw=raw.split('/',2)[2]
        return (root/raw).resolve()
    profile=mapped(profile_path);maps=mapped(maps_root)
    if profile!=root/'teamgrit-sim/config'/selected['profile']:raise ValueError('live_localization_profile_mismatch')
    if maps/native.name!=native:raise ValueError('live_map_resolver_mismatch')
    matches=[p for p in maps.iterdir() if p.is_dir() and p.name.endswith(selected['map_uuid'])]
    if matches!=[native]:raise ValueError('ambiguous_or_missing_native_map_resolver')
    return {'profile':profile.name,'profile_parameter':profile_path,'maps_root_parameter':maps_root,'map_uuid':selected['map_uuid']}

def load_source_config(options):
    from coegent.a2_maps import scene_path,scene_digest,native_directory
    import yaml
    root=Path(options.source_root).expanduser().resolve()
    cfg=yaml.safe_load((root/'planner/config/map.yaml').read_text())
    robot=yaml.safe_load((root/'planner/config/robot.yaml').read_text())
    def merge(a,b):
        for k,v in b.items():a[k]=merge(a.get(k,{}),v) if isinstance(v,dict) and isinstance(a.get(k,{}),dict) else deepcopy(v)
        return a
    merge(cfg,robot)
    meta=json.loads((scene_path(root,options.scene_dir)/'scene_meta.json').read_text())
    directory=native_directory(root,meta)
    if options.scene_dir:
        scene_digest(root,options.scene_dir)
        selected=meta['map_selection'];uuid=selected['map_uuid']
        required={'metadata_'+uuid+'.yaml','global_map_'+uuid+'.pcd','cuvslam_map/data.mdb'}
        if not required.issubset(selected['native_files']):raise ValueError('selected scene must bind native metadata, PCD and MDB')
        cfg['robot_runtime']['map_uuid']=uuid
    else:uuid=cfg['robot_runtime']['map_uuid']
    native=directory/('metadata_'+uuid+'.yaml')
    native_meta=yaml.safe_load(native.read_text()) if native.is_file() else {}
    if native_meta.get('map_uuid')!=uuid:
        raise ValueError('scene map and robot_runtime map_uuid differ')
    if options.scene_dir and (native_meta.get('slam_backend',{}).get('name')!='cuvslam' or
                              native_meta['slam_backend'].get('profile')!=selected['profile']):
        raise ValueError('native map backend/camera profile mismatch')
    if cfg['robot_runtime']['frames']!={'map':'prior_map','base':'base_link'}:
        raise ValueError('unsupported Isaac frames')
    cfg['map']['dir']=str(directory)
    cfg['robot_runtime']['follow']['stall_s']=options.progress_timeout_s
    if options.planning_radius_m is not None:cfg['occupancy']['robot_radius']=options.planning_radius_m
    cfg.setdefault('world_state',{})['vision_enabled']=False
    if options.vision_url:cfg['world_state']['vision_url']=options.vision_url
    if options.vision_model:cfg['planner']['model']=options.vision_model
    return cfg,{'map_uuid':uuid,'map_dir':str(directory),'frames':deepcopy(cfg['robot_runtime']['frames'])}

class ROSRuntime:
    def __init__(self,options,emit=None):
        self.options=options;self.emit=emit or (lambda e:None)
        self.ros=self.backend=self.executor=self.world=None
        self.thread=None;self.initialized=False;self.owned_localization=False
        self.active_req=None;self.active_since=0.;self.battery_at=0.;self.loc_at=0.
        self.path_dispatched=False
        self.safety=None;self.safety_at=0.;self.clock=None;self.clock_progress=0.
        self._stop_cache=None;self._speed_old=None
        self._effects=set()
        self.loc_start_sent=0.
        self.loc_event=({},0.)
        self.live_map_binding=None

    async def open(self):
        self.cfg,self.identity=load_source_config(self.options)
        self._configure_config()
        sys.path.insert(0,str(Path(self.options.source_root)/'planner'))
        import rclpy
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.qos import qos_profile_sensor_data, QoSProfile, DurabilityPolicy, ReliabilityPolicy
        from sensor_msgs.msg import BatteryState
        from std_msgs.msg import String
        from rosgraph_msgs.msg import Clock
        from a2sim.robot.ros_io import RosIO
        from a2sim.robot.backend import RobotBackend
        self.rclpy=rclpy
        suffix=uuid4().hex[:10]
        rclpy.init(args=['--ros-args','-p','use_sim_time:=true',
                        '-r',f'gemma_planner:__node:=coegent_a2_robot_{suffix}',
                        '-r',f'world_state:__node:=coegent_a2_world_{suffix}'])
        self.initialized=True
        self.ros=RosIO(self.cfg['robot_runtime'])
        self.backend=owned_backend_type(RobotBackend)(self.cfg,self.ros,self._backend_event)
        if self.options.scene_dir:
            from a2sim.scene.scene import Scene
            self.backend.scene=Scene.load(self.options.scene_dir)
            self.backend._navs.clear()
        self.backend.seal_task_id('gemma-coegent-'+suffix)
        publish=self.ros.publish
        def owned_publish(pub,data):
            if pub is self.ros.pub_loc:
                command=json.loads(data)
                guard_localization(command,self.ros.loc_status or {},self.backend.task_id,self.identity['map_uuid'])
                if command['command']=='START':
                    self.loc_start_sent=time.monotonic();self.owned_localization=True;self._stop_cache=None
            publish(pub,data)
        self.ros.publish=owned_publish
        self.executor=SingleThreadedExecutor();self.executor.add_node(self.ros)
        latched=QoSProfile(depth=1,reliability=ReliabilityPolicy.RELIABLE,durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.ros.create_subscription(BatteryState,self.cfg['robot_runtime']['topics']['battery'],self._battery,qos_profile_sensor_data)
        self.ros.create_subscription(String,self.cfg['robot_runtime']['topics']['localization_status'],self._loc,latched)
        self.ros.create_subscription(String,'/safety/status',self._safety,10)
        self.ros.create_subscription(Clock,'/clock',self._clock,qos_profile_sensor_data)
        self.thread=threading.Thread(target=self.executor.spin,daemon=True);self.thread.start()
        until=time.monotonic()+min(5.,self.options.startup_timeout_s)
        while time.monotonic()<until and (not self.battery_at or self.clock is None):await asyncio.sleep(.05)

    def _configure_config(self):pass

    def _battery(self,m):
        self.battery_at=time.monotonic() if math.isfinite(m.percentage) and 0<=m.percentage<=1 else 0.
    def _loc(self,m):
        try:data=json.loads(m.data)
        except (ValueError,TypeError):return
        if not isinstance(data,dict) or data.get('state') not in ('STARTING','READY','STOPPED','ERROR'):return
        self.loc_event=(data,time.monotonic());self.loc_at=self.loc_event[1]
    def _safety(self,m):self.safety=m.data;self.safety_at=time.monotonic()
    def _clock(self,m):
        v=m.clock.sec+m.clock.nanosec*1e-9
        if self.clock is None or v>self.clock:self.clock_progress=time.monotonic()
        self.clock=v

    async def _backend_event(self,event):
        if event.get('type')=='path' and 'req_id' in event:
            self.path_dispatched=True
            self.active_req=event['req_id'];self.active_since=time.time();self._stop_cache=None
        if event.get('type')=='path_result' and event.get('req_id')==self.active_req:
            if event.get('status') in TERMINAL:self.active_req=None
        self.emit({'kind':'backend_event',**event})

    def _check_battery(self):
        if time.monotonic()-self.battery_at>5:raise RuntimeError('battery_reading_missing_or_stale')
        b=self.backend.check_battery()
        if not math.isfinite(b) or not 0<=b<=1:raise RuntimeError('invalid_battery_ratio')
        return b

    def _check_clock(self):
        if self.clock is None or time.monotonic()-self.clock_progress>3:raise RuntimeError('simulation_clock_not_advancing')

    def _localization_status(self):return self.ros.loc_status or {}

    def _check_map(self):
        st=self._localization_status()
        if st.get('state')!='READY' or st.get('map_uuid')!=self.identity['map_uuid']:
            raise RuntimeError('localization_not_ready_for_scene_map')
        return check_pose(self.ros.pose(),self.cfg['robot_runtime']['follow']['tf_max_age_s'])

    def _check_safety(self):
        if time.monotonic()-self.safety_at>3 or self.safety!='NORMAL':
            raise RuntimeError('safety_not_normal:'+str(self.safety))

    async def snapshot(self):
        robot={};obs={'localization':deepcopy(self._localization_status()),'map_identity':deepcopy(self.identity),
                      'safety_status':self.safety,'clock_sim_s':self.clock}
        try:robot['battery_percent']=self._check_battery()*100
        except RuntimeError:pass
        try:
            p=self._check_map();robot['localization_valid']=True
            obs['map_pose']={'x':p[0],'y':p[1],'z':p[2],'yaw':p[3],'age_sim_s':p[4]}
        except RuntimeError:robot['localization_valid']=False
        if self.safety is not None and time.monotonic()-self.safety_at<=3:robot['estop']=self.safety!='NORMAL'
        obs['controller_state']=await asyncio.to_thread(self.ros.controller_state)
        return {'robot':robot,'world':{},'mission':{},'observations':obs}

    async def inspection_snapshot(self, report, *, include_vision=False):
        """Independent measured feedback. Preserve capture age instead of restamping frames."""
        from coegent.inspector import InspectionSnapshot
        live = await self.snapshot()
        now, mono = time.time(), time.monotonic()
        robot = deepcopy(live.get('robot', {}))
        stamps = {}
        if 'battery_percent' in robot:
            stamps['robot_state.battery_percent'] = now - (mono - self.battery_at)
        if 'estop' in robot:
            stamps['robot_state.estop'] = now - (mono - self.safety_at)
        pose = live.get('observations', {}).get('map_pose')
        if pose is not None:
            robot['pose_xy'] = [pose['x'], pose['y']]
            robot['map_pose'] = deepcopy(pose)
            stamps['robot_state.pose_xy'] = now - max(0., pose['age_sim_s'])
            stamps['robot_state.map_pose'] = stamps['robot_state.pose_xy']
        if robot.get('localization_valid') is True:
            stamps['robot_state.localization_valid'] = stamps.get('robot_state.pose_xy', now)
        if stamps:
            stamps['robot_state'] = min(stamps.values())
        values = {'robot_state': robot}
        if include_vision:
            try:
                import base64
                from io import BytesIO
                views, sim_stamp, received, source = await asyncio.to_thread((await self._world()).capture_views)
                if views and type(received) in (int, float) and math.isfinite(received) and 0 < received <= time.monotonic():
                    images = []
                    for label, frame in views[:4]:
                        image = frame.copy().convert('RGB')
                        image.thumbnail((768, 768))
                        out = BytesIO(); image.save(out, format='JPEG', quality=80)
                        images.append('data:image/jpeg;base64,' + base64.b64encode(out.getvalue()).decode('ascii'))
                    values['vision'] = {'images': images, 'camera_source': source, 'sim_stamp': sim_stamp}
                    stamps['vision'] = time.time() - (time.monotonic() - received)
            except Exception:
                pass  # No frame/model is an explicit unknown, never a fabricated view.
        return InspectionSnapshot(report.mission_id, report.revision, values, stamps)

    async def _localize(self):
        if not self.options.allow_motion:raise RuntimeError('motion_disabled')
        self._check_clock();self._check_battery()
        if self.options.scene_dir:
            from rcl_interfaces.srv import GetParameters
            client=self.ros.create_client(GetParameters,'/slam_controller/get_parameters')
            try:
                res=await asyncio.to_thread(self.ros._call,client,GetParameters.Request(names=['profile','maps_root']),5.)
                if res is None or len(res.values)!=2:raise RuntimeError('cannot_verify_live_localization_binding')
                self.live_map_binding=controller_binding(self.options.source_root,self.options.scene_dir,
                                                         res.values[0].string_value,res.values[1].string_value)
                self.emit({'kind':'localization_binding','binding':self.live_map_binding})
            finally:self.ros.destroy_client(client)
        st=self.ros.loc_status or {}
        if st.get('state') in ('STARTING','READY') and st.get('task_id')!=self.backend.task_id:
            raise RuntimeError('localization_owned_by_other_task:'+str(st.get('task_id')))
        try:return await self.backend.localize()
        finally:
            if getattr(self.backend,'loc_started',False):self.owned_localization=True

    async def _stop_localization(self,grace_s=0,timeout=None):
        if not self.owned_localization:raise RuntimeError('cannot_stop_unowned_localization')
        until=time.monotonic()+(self.options.cancel_timeout_s if timeout is None else timeout)
        # An old STOPPED latch may arrive before the queued START is processed.
        # Wait for a status belonging to this START before acknowledging or stopping it.
        while time.monotonic()<until:
            st,received=self.loc_event
            ours=st.get('task_id')==self.backend.task_id and st.get('map_uuid')==self.identity['map_uuid'] and received>=self.loc_start_sent
            if ours and st.get('state')=='STOPPED':
                self.owned_localization=False;self.backend.loc_started=False;self.loc_start_sent=0.;return None
            if ours and st.get('state')=='ERROR' and st.get('reason')=='process_exit':
                await self.reset_failed_localization(self.backend.task_id,self.identity['map_uuid'])
                self.owned_localization=False;self.backend.loc_started=False;self.loc_start_sent=0.;return None
            if ours and st.get('state') in ('STARTING','READY'):break
            if st.get('state') in ('STARTING','READY') and st.get('task_id')!=self.backend.task_id:
                raise RuntimeError('localization_ownership_lost')
            await asyncio.sleep(.05)
        else:raise RuntimeError('owned_localization_start_ack_missing')
        self.backend.stop_localization(grace_s=grace_s)
        while time.monotonic()<until:
            st,received=self.loc_event
            if (received>=self.loc_start_sent and st.get('state')=='STOPPED' and
                    st.get('task_id')==self.backend.task_id and st.get('map_uuid')==self.identity['map_uuid']):
                self.owned_localization=False;self.backend.loc_started=False;self.loc_start_sent=0.;return None
            if st.get('state') in ('STARTING','READY') and st.get('task_id')!=self.backend.task_id:
                raise RuntimeError('localization_ownership_lost')
            await asyncio.sleep(.05)
        raise RuntimeError('localization_stop_ack_missing')

    async def reset_failed_localization(self,owner,map_uuid,*,message_factory=None):
        """Reconcile only an observed, already-exited localization owned by this adapter."""
        st=self.ros.loc_status or {}
        if (not owner.startswith('gemma-coegent-') or st.get('task_id')!=owner or st.get('map_uuid')!=map_uuid or
                st.get('state')!='ERROR' or st.get('reason')!='process_exit' or self.active_req is not None):
            raise RuntimeError('failed_localization_error_ownership_mismatch')
        if message_factory is None:
            from std_msgs.msg import String
            message_factory=lambda data:String(data=data)
        since=time.monotonic()
        # RESET is not a normal plan API. Source ERROR/process_exit means its child was reaped;
        # normal STOP is deliberately a no-op there. Guard above binds this administrative reset.
        self.ros.pub_loc.publish(message_factory(json.dumps({'command':'RESET'})))
        until=since+self.options.cancel_timeout_s
        while time.monotonic()<until:
            status,received=self.loc_event
            if received>=since and status.get('state')=='STOPPED' and status.get('reason')=='reset' and status.get('task_id') is None:
                return {'confirmed':True,'task_id':owner,'map_uuid':map_uuid,'before':deepcopy(st),'after':deepcopy(status)}
            if received>=since and status.get('state') in ('STARTING','READY'):
                raise RuntimeError('localization_ownership_changed_during_recovery')
            await asyncio.sleep(.05)
        raise RuntimeError('failed_localization_reset_ack_missing')

    async def _go_to(self,args):
        if not self.options.allow_motion:raise RuntimeError('motion_disabled')
        self._check_clock();self._check_battery()
        st=self._localization_status()
        if st.get('state') in ('READY','STARTING') and st.get('task_id')!=self.backend.task_id:
            raise RuntimeError('localization_owned_by_other_task')
        if self.options.auto_initialize:
            st=self._localization_status()
            if st.get('state')!='READY':await self._localize()
            await self._effect(self.backend.ensure_nav_active)
        self._check_map();self._check_safety()
        if await asyncio.to_thread(self.ros.controller_state)!='active':raise RuntimeError('nav2_not_active')
        current={}
        for s in list(self.ros.path_status):current[s.get('req_id')]=s.get('status','').upper()
        if any(v=='ACCEPTED' for v in current.values()):raise RuntimeError('another_path_is_active')
        if self._speed_old is None:
            original=await asyncio.to_thread(self.ros.get_param,self.cfg['robot_runtime']['speed']['param'])
            if type(original) not in (float,int) or not math.isfinite(original):raise RuntimeError('cannot_read_original_speed')
            self._speed_old=original
        await self._effect(self.backend.set_speed)
        task=asyncio.create_task(self.backend.go_to(**args))
        try:
            while not task.done():
                done,_=await asyncio.wait({task},timeout=.2)
                if done:break
                self._check_clock();self._check_battery();self._check_safety()
            ok=await task
            if ok:
                p=self._check_map()
                if math.hypot(p[0]-args['x'],p[1]-args['y'])>self.options.arrival_tolerance_m:
                    raise RuntimeError('terminal_reached_but_pose_outside_goal_tolerance')
            return ok
        finally:
            if not task.done():
                self.backend.cancel.set();task.cancel()
                try:await task
                except (asyncio.CancelledError,Exception):pass

    async def _world(self):
        if self.world is None:
            path=Path(self.options.source_root)/'planner/scripts/world_state_node.py'
            spec=importlib.util.spec_from_file_location('coegent_a2_world_source',path)
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            self.world=module.WorldState(self.cfg)
            # Keep camera callbacks; disable autonomous world publishing/vision/logging.
            for timer in list(self.world.timers):self.world.destroy_timer(timer)
            self.executor.add_node(self.world)
            await asyncio.sleep(2)
        return self.world

    async def call(self,name,args):
        if name=='RobotBackend.go_to':return await self._go_to(args)
        if name=='RobotBackend.localize':return await self._localize()
        if name=='RobotBackend.stop_localization':return await self._stop_localization(args.get('grace_s'),self.options.call_timeout_s)
        if name=='RobotBackend.check_battery':return self._check_battery()
        if name=='RobotBackend.refresh_pose':
            self._check_map();check_pose(self.ros.pose(),args.get('max_age') or self.cfg['robot_runtime']['follow']['tf_max_age_s'])
            return self.backend.refresh_pose(**args)
        if name=='RobotBackend.ensure_nav_active':return await self._effect(self.backend.ensure_nav_active)
        if name=='RobotBackend.observe':return await asyncio.to_thread(self.backend.observe,**args)
        if name=='GridNavigator.plan':return await asyncio.to_thread(self.backend.nav.plan,**args)
        if name=='WorldState.capture_views':return await asyncio.to_thread((await self._world()).capture_views)
        if name=='WorldState.describe':return await (await self._world()).describe(**args)
        raise ValueError('unsupported API:'+name)

    async def stop_motion(self):
        """Stop owned paths while retaining measured feedback for Inspector."""
        if self.backend is None:return {'confirmed':True,'reason':'no_adapter_owned_work'}
        self.backend.cancel.set()
        confirmed=True;reasons=[]
        if self.active_req is not None:
            req=self.active_req;since=self.active_since
            self.ros.publish(self.ros.pub_cancel,json.dumps({'req_id':req}))
            until=time.monotonic()+self.options.cancel_timeout_s-1
            while time.monotonic()<until and terminal_ack(self.ros.path_status,req,since) is None:await asyncio.sleep(.05)
            ack=terminal_ack(self.ros.path_status,req,since)
            confirmed=ack is not None;reasons.append('owned_path_'+(ack or 'cancel_ack_missing'))
            if ack:self.active_req=None
        return {'confirmed':confirmed,'reason':';'.join(reasons) or 'no_adapter_owned_motion',
                'scope':'adapter_owned_paths'}

    async def cancel(self):
        if self._stop_cache:return self._stop_cache
        motion=await self.stop_motion()
        confirmed=motion['confirmed'];reasons=[]
        if motion['reason'] not in {'no_adapter_owned_motion','no_adapter_owned_work'}:
            reasons.append(motion['reason'])
        if self.owned_localization:
            try:await self._stop_localization(0);reasons.append('owned_localization_STOPPED')
            except Exception as exc:confirmed=False;reasons.append(str(exc))
        self._stop_cache={'confirmed':confirmed,'reason':';'.join(reasons) or 'no_adapter_owned_motion',
                          'scope':'adapter_owned_paths_and_localization'}
        return self._stop_cache

    async def _effect(self,method,*args):
        # Cancelling asyncio cannot cancel a synchronous ROS service call.
        # Retain it so cleanup cannot restore state before a late mutation.
        task=asyncio.create_task(asyncio.to_thread(method,*args));self._effects.add(task)
        def finished(t):
            self._effects.discard(t)
            if not t.cancelled():t.exception()
        task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def close(self):
        if self._effects:await asyncio.shield(asyncio.gather(*list(self._effects),return_exceptions=True))
        # Inspection has consumed the still-live feedback; release owned SLAM now.
        ack=await self.cancel()
        if ack.get('confirmed') is not True:raise RuntimeError('owned_cleanup_unconfirmed:'+ack.get('reason',''))
        if self._speed_old is not None and self.backend is not None:
            await self._effect(self.backend.restore_speed,self._speed_old)
            actual=await asyncio.to_thread(self.ros.get_param,self.cfg['robot_runtime']['speed']['param'])
            if actual is None or not math.isclose(actual,self._speed_old,rel_tol=1e-8,abs_tol=1e-8):
                raise RuntimeError('speed_restore_unconfirmed')
            self._speed_old=None
        if self.world:self.world.done.set()
        if self.executor:self.executor.shutdown(timeout_sec=2)
        if self.thread:self.thread.join(timeout=2)
        if self.world:self.world.destroy_node()
        if self.ros:self.ros.destroy_node()
        if self.initialized:self.rclpy.try_shutdown();self.initialized=False
