"""Explicit Office simulator pose source. This does not perform cuVSLAM localization.

The scene's fixed world/map registration is used for control in this mode.
No global localization status or anchor messages are fabricated/published.
"""
import asyncio
from copy import deepcopy
import importlib.util
import json
import math
from pathlib import Path
import time
from uuid import uuid4

from .ros_runtime import ROSRuntime,check_pose


def map_sim_pose(meta,position,quaternion):
    registration=meta['place_registration']
    tx,ty=registration['translation_xy'];angle=registration['yaw_rad']
    values=[*position,*quaternion,tx,ty,angle]
    if not all(type(v) in (float,int) and math.isfinite(v) for v in values):
        raise RuntimeError('nonfinite_sim_pose_or_registration')
    qx,qy,qz,qw=quaternion
    if not math.isclose(sum(q*q for q in quaternion),1.,abs_tol=.01):raise RuntimeError('invalid_sim_quaternion')
    if abs(position[2]-.5)>.05 or abs(qx)>.01 or abs(qy)>.01:raise RuntimeError('not_planar_office_robot')
    yaw=math.atan2(2*(qw*qz+qx*qy),1-2*(qy*qy+qz*qz))+angle
    c,s=math.cos(angle),math.sin(angle);x,y,_=position
    return c*x-s*y+tx,s*x+c*y+ty,0.,math.atan2(math.sin(yaw),math.cos(yaw))


def checked_sim_sample(sample,*,now,clock):
    if not sample or sample['frame']!='sim_world' or sample['child_frame']!='sim_base':raise RuntimeError('missing_or_wrong_frame_sim_pose')
    values=[sample['stamp'],sample['received'],*sample['position'],*sample['quaternion'],*sample['velocity']]
    if not all(type(v) in (float,int) and math.isfinite(v) for v in values):raise RuntimeError('invalid_sim_pose')
    if not 0<=now-sample['received']<=2.5 or clock is None or not -.25<=clock-sample['stamp']<=1.5:
        raise RuntimeError('stale_or_future_sim_pose')
    if not math.isclose(sum(q*q for q in sample['quaternion']),1.,abs_tol=.01):raise RuntimeError('invalid_sim_quaternion')
    return sample


def require_office(names,native_status):
    if 'teamgrit_isaac_planar_sim' not in names:raise RuntimeError('office_sim_node_missing')
    if 'visual_slam_node' in names or not native_status or native_status.get('state')!='STOPPED':
        raise RuntimeError('native_localization_must_be_stopped_for_sim_pose')


def require_idle_mapping(status):
    if not status or status.get('state') not in ('IDLE','DONE','ERROR'):
        raise RuntimeError('native_mapping_must_be_idle_for_sim_pose')


def quaternion_rpy(r,p,y):
    cr,sr=math.cos(r/2),math.sin(r/2);cp,sp=math.cos(p/2),math.sin(p/2);cy,sy=math.cos(y/2),math.sin(y/2)
    return sr*cp*cy-cr*sp*sy,cr*sp*cy+sr*cp*sy,cr*cp*sy-sr*sp*cy,cr*cp*cy+sr*sp*sy


def configure_private_topics(cfg,suffix):
    topics=cfg['robot_runtime']['topics'];original={k:topics[k] for k in ('path_goal','path_cancel','path_status')}
    for key,topic in (('path_goal','goal'),('path_cancel','cancel'),('path_status','status')):
        topics[key]='/coegent/'+suffix+'/'+topic
    return original


def bridge_command(options,cfg,suffix):
    binary=Path(options.runtime_dir)/'private_bridge/nav2_path_bridge_node'
    topics=cfg['robot_runtime']['topics']
    params={'use_sim_time':True,'goal_topic':topics['path_goal'],'cancel_topic':topics['path_cancel'],
            'status_topic':topics['path_status'],'path_topic':'/coegent/'+suffix+'/nav2_path',
            'detour_enabled':True,'detour.stall_s':30.0,'detour.cooldown_s':5.0}
    cmd=[str(binary),'--ros-args','-r','__node:=coegent_path_bridge_'+suffix]
    for k,v in params.items():cmd+=['-p',k+':='+str(v).lower() if isinstance(v,bool) else k+':='+str(v)]
    return cmd


def first_route_segment(path,length,stop_at_corner=False):
    import numpy as np
    points=np.asarray(path,dtype=float)
    if points.ndim!=2 or points.shape[1]!=2 or not len(points) or not np.isfinite(points).all():
        raise RuntimeError('invalid_planned_route')
    if not math.isfinite(length) or length<=0:raise ValueError('invalid_segment_length')
    segment=[points[0]];remaining=length
    travelled=0.
    for index,(a,b) in enumerate(zip(points,points[1:])):
        # The source controller accepts goals within .30m, and grid snapping
        # adds up to .14m. A nearer corner must remain inside the next polyline
        # rather than being dispatched again as an already-satisfied goal.
        if stop_at_corner and index and travelled>=.5:
            incoming=a-points[index-1];outgoing=b-a
            norm=float(np.linalg.norm(incoming)*np.linalg.norm(outgoing))
            if norm>1e-10 and float(np.dot(incoming,outgoing))/norm<math.cos(math.radians(35)):
                return np.asarray(segment),False
        distance=float(np.linalg.norm(b-a))
        if distance>remaining:
            segment.append(a+(b-a)*(remaining/distance))
            return np.asarray(segment),False
        segment.append(b);remaining-=distance;travelled+=distance
    return np.asarray(segment),True


async def segmented_go_to(backend,x,y):
    # Same API goal; only local FollowPath horizons are shortened. Replan from
    # measured pose after each acknowledgement; intermediate success is not API success.
    for index in range(64):
        if backend.cancel.is_set():return False
        pose=backend.refresh_pose();path=backend.nav.plan((pose.x,pose.y),(x,y))
        if path is None:
            backend.last_failure='no path on the prior map';return False
        segment,final=first_route_segment(path,backend._segment_length,stop_at_corner=True)
        await backend.emit({'type':'route_segment','index':index,'target':segment[-1].tolist(),
                            'final':final,'api_target':[x,y]})
        if await backend.follow(segment) is not True:return False
        if final:return True
    backend.last_failure='route_segment_limit';return False


def add_shortcut_margin(nav,margin):
    # The graph still admits mapped narrow passages. Only long line-of-sight
    # shortcuts require extra clearance, so smoothing cannot cut a wide corner
    # down to the minimum body radius. Adjacent graph edges remain the fallback.
    original=nav.line_free
    def line_free(a,b,margin=0.,max_mult=None):
        return original(a,b,margin=margin+extra,max_mult=max_mult)
    extra=margin
    nav.line_free=line_free


class IsaacPoseRuntime(ROSRuntime):
    def __init__(self,options,emit=None):
        super().__init__(options,emit)
        self.bridge_active=False;self.sim_sample=None;self.pose_fault=None
        self.mapping_status=None
        self.depth_node=None;self.tf_pub=self.static_pub=self.odom_pub=None
        self.path_suffix='t'+uuid4().hex[:12];self.path_bridge=None;self.path_log=None
        self.foreign_paths={};self.action_status=None

    def _configure_config(self):
        if self.options.private_path_bridge:
            from .private_bridge import verify
            self.bridge_build=verify(self.options.source_root,Path(self.options.runtime_dir)/'private_bridge')
            self.original_path_topics=configure_private_topics(self.cfg,self.path_suffix)

    async def open(self):
        await super().open()
        if self.options.path_segment_m is not None:
            from types import MethodType
            self.backend._segment_length=self.options.path_segment_m
            self.backend.go_to=MethodType(segmented_go_to,self.backend)
        if self.options.shortcut_margin_m is not None:
            add_shortcut_margin(self.backend.nav,self.options.shortcut_margin_m)
        from nav_msgs.msg import Odometry
        from rclpy.qos import qos_profile_sensor_data
        from rclpy.qos import QoSProfile,ReliabilityPolicy,DurabilityPolicy
        from std_msgs.msg import String
        self.meta=json.loads((Path(self.options.scene_dir)/'scene_meta.json').read_text())
        if self.meta['map_selection']['map_uuid']!=self.identity['map_uuid']:raise RuntimeError('sim_registration_map_mismatch')
        # Validate registration before subscribing/publishing. Z=0 is this planar rig's base height in the saved map.
        map_sim_pose(self.meta,[0.,0.,.5],[0.,0.,0.,1.])
        self.identity['pose_source']='isaac_ground_truth'
        self.ros.create_subscription(Odometry,'/evaluation/ground_truth',self._sim_pose,qos_profile_sensor_data)
        latched=QoSProfile(depth=1,reliability=ReliabilityPolicy.RELIABLE,durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.ros.create_subscription(String,'/grit_slam/mapping/status',self._mapping,latched)
        self.ros.create_subscription(String,'/evaluation/collision',self._collision,10)
        until=time.monotonic()+8
        while (self.sim_sample is None or self.mapping_status is None) and time.monotonic()<until:await asyncio.sleep(.05)
        self._sample()
        if self.options.private_path_bridge:
            # Also watch the shared bridge for competing accepted goals.
            self.ros.create_subscription(String,self.original_path_topics['path_status'],self._foreign_path_status,10)
            from action_msgs.msg import GoalStatusArray
            self.ros.create_subscription(GoalStatusArray,'/follow_path/_action/status',self._action_status,latched)
            self.path_log_path=Path(self.options.runtime_dir)/(self.backend.task_id+'.path_bridge.log')
            self.path_log=self.path_log_path.open('wb')
            cmd=bridge_command(self.options,self.cfg,self.path_suffix)
            self.path_bridge=await asyncio.create_subprocess_exec(*cmd,stdout=self.path_log,stderr=self.path_log)
            until=time.monotonic()+8
            while self.path_bridge.returncode is None and time.monotonic()<until:
                if self.ros.pub_goal.get_subscription_count()>0:break
                await asyncio.sleep(.1)
            else:raise RuntimeError('private_path_bridge_not_ready')
            self.emit({'kind':'private_path_bridge','node':'coegent_path_bridge_'+self.path_suffix,
                       'topics':self.cfg['robot_runtime']['topics'],'log_path':str(self.path_log_path),
                       'detour_stall_wall_s':30.,'detour_cooldown_wall_s':5.,'build':self.bridge_build})

    def _mapping(self,m):
        try:self.mapping_status=json.loads(m.data)
        except (ValueError,TypeError):self.mapping_status=None

    def _collision(self,m):
        if not self.bridge_active or self.active_req is None:return
        try:
            event=json.loads(m.data);stamp=float(event['sim_time'])
            if (event.get('event')=='contact_blocked' and event.get('body')=='sim_base' and
                    self.clock is not None and -.25<=self.clock-stamp<=1.5):
                self.pose_fault='simulator_contact_blocked:'+str(event.get('obstacle','unknown'))
                self.emit({'kind':'simulator_contact','event':event})
        except (ValueError,TypeError,KeyError):return

    def _foreign_path_status(self,m):
        try:
            value=json.loads(m.data)
            self.foreign_paths[value['req_id']]=str(value['status']).upper()
        except (ValueError,TypeError,KeyError):return

    def _action_status(self,m):self.action_status=[s.status for s in m.status_list]

    def _check_foreign_path(self):
        if any(v=='ACCEPTED' for v in self.foreign_paths.values()):
            raise RuntimeError('foreign_path_accepted_during_private_mission')

    async def _go_to(self,args):
        if self.options.private_path_bridge:
            self._check_foreign_path()
            # Status is transient-local, unlike the bridge's volatile ACCEPTED topic.
            until=time.monotonic()+5
            while self.action_status is None and time.monotonic()<until:await asyncio.sleep(.05)
            if self.action_status is None or any(s in (1,2,3) for s in self.action_status):
                raise RuntimeError('follow_path_action_missing_or_already_active')
        return await super()._go_to(args)

    def _sample(self):
        if self.pose_fault:raise RuntimeError(self.pose_fault)
        return checked_sim_sample(self.sim_sample,now=time.monotonic(),clock=self.clock)

    def _sim_pose(self,m):
        p=m.pose.pose.position;q=m.pose.pose.orientation;v=m.twist.twist
        sample={'frame':m.header.frame_id,'child_frame':m.child_frame_id,
                'stamp':m.header.stamp.sec+m.header.stamp.nanosec*1e-9,'received':time.monotonic(),
                'position':[p.x,p.y,p.z],'quaternion':[q.x,q.y,q.z,q.w],
                'velocity':[v.linear.x,v.linear.y,v.angular.z]}
        self.sim_sample=sample
        if not self.bridge_active:return
        try:
            self._localization_status();self._sample()
            x,y,z,yaw=map_sim_pose(self.meta,sample['position'],sample['quaternion'])
            from geometry_msgs.msg import TransformStamped
            t=TransformStamped();t.header.stamp=m.header.stamp;t.header.frame_id='odom';t.child_frame_id='base_link'
            t.transform.translation.x=x;t.transform.translation.y=y;t.transform.translation.z=z
            t.transform.rotation.z=math.sin(yaw/2);t.transform.rotation.w=math.cos(yaw/2)
            self.tf_pub.sendTransform(t)
            odom=deepcopy(m);odom.header.frame_id='odom';odom.child_frame_id='base_link'
            odom.pose.pose.position.x=x;odom.pose.pose.position.y=y;odom.pose.pose.position.z=z
            odom.pose.pose.orientation=t.transform.rotation
            self.odom_pub.publish(odom)
        except Exception as exc:self.pose_fault=type(exc).__name__+':'+str(exc)

    def _localization_status(self):
        native=self.ros.loc_status or {}
        if native.get('state') in ('STARTING','READY','STOPPING','ERROR'):
            raise RuntimeError('native_localization_conflicts_with_sim_pose')
        if self.bridge_active:
            require_office(self.ros.get_node_names(),native)
            require_idle_mapping(self.mapping_status)
        return {'state':'READY' if self.bridge_active else 'STOPPED','task_id':self.backend.task_id,
                'map_uuid':self.identity['map_uuid'],'source':'isaac_ground_truth'}

    def _check_map(self):
        sample=self._sample()
        pose=super()._check_map()
        expected=map_sim_pose(self.meta,sample['position'],sample['quaternion'])
        yaw_error=math.atan2(math.sin(pose[3]-expected[3]),math.cos(pose[3]-expected[3]))
        if math.dist(pose[:3],expected[:3])>.15 or abs(yaw_error)>.15:
            raise RuntimeError('sim_pose_tf_disagrees_with_sample')
        return pose

    def _check_safety(self):
        self._check_foreign_path()
        if self.options.private_path_bridge and (self.path_bridge is None or self.path_bridge.returncode is not None):
            raise RuntimeError('private_path_bridge_exited')
        super()._check_safety()
        self._check_map()

    async def _localize(self):
        if not self.options.allow_motion:raise RuntimeError('motion_disabled')
        self._check_clock();self._check_battery();self._sample()
        require_office(self.ros.get_node_names(),self.ros.loc_status)
        require_idle_mapping(self.mapping_status)
        from nav_msgs.msg import Odometry
        from tf2_ros import TransformBroadcaster,StaticTransformBroadcaster
        from geometry_msgs.msg import TransformStamped
        import yaml
        self.tf_pub=TransformBroadcaster(self.ros);self.static_pub=StaticTransformBroadcaster(self.ros)
        self.odom_pub=self.ros.create_publisher(Odometry,'/grit_slam/odometry',10)
        transforms=[]
        def add(parent,child,xyz=(0.,0.,0.),rpy=(0.,0.,0.)):
            t=TransformStamped();t.header.stamp=self.ros.get_clock().now().to_msg();t.header.frame_id=parent;t.child_frame_id=child
            t.transform.translation.x,t.transform.translation.y,t.transform.translation.z=map(float,xyz)
            t.transform.rotation.x,t.transform.rotation.y,t.transform.rotation.z,t.transform.rotation.w=quaternion_rpy(*rpy)
            transforms.append(t)
        add('earth','prior_map');add('prior_map','map');add('map','odom')
        profile=yaml.safe_load((Path(self.options.source_root)/'teamgrit-sim/config'/self.meta['map_selection']['profile']).read_text())
        ext=profile['extrinsics'];camera=ext['T_base_camera'];add('base_link',profile['frames']['camera_link'],camera['translation'],[math.radians(v) for v in camera['rpy_deg']])
        for cf in ext.get('camera_frames',[]):add(cf['parent'],cf['child'],cf.get('translation',[0,0,0]),[math.radians(v) for v in cf.get('rpy_deg',[0,0,0])])
        self.static_pub.sendTransform(transforms)
        path=Path(self.options.source_root)/'teamgrit-slam/slam/scripts/cuvslam/depth_cloud.py'
        spec=importlib.util.spec_from_file_location('coegent_isaac_depth',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.depth_node=module.DepthCloud();self.executor.add_node(self.depth_node)
        self.owned_localization=True;self.bridge_active=True;self._stop_cache=None
        self.emit({'kind':'pose_bridge','state':'START','source':'isaac_ground_truth','registration':self.meta['place_registration']})
        until=time.monotonic()+12
        while time.monotonic()<until:
            try:self._check_map();self._check_safety();break
            except RuntimeError:await asyncio.sleep(.1)
        else:raise RuntimeError('sim_pose_tf_or_safety_not_ready')
        p=self._check_map();self.emit({'kind':'pose_bridge','state':'READY','source':'isaac_ground_truth','pose':list(p)})
        return {'source':'isaac_ground_truth','pose':list(p)}

    async def _stop_localization(self,grace_s=0,timeout=None):
        if not self.owned_localization:raise RuntimeError('cannot_stop_unowned_sim_pose')
        # Require two fresh zero-velocity simulator observations before removing feedback.
        until=time.monotonic()+(timeout or self.options.cancel_timeout_s);count=0;last=None
        while time.monotonic()<until:
            sample=checked_sim_sample(self.sim_sample,now=time.monotonic(),clock=self.clock)
            if sample['received']!=last:
                last=sample['received'];count=count+1 if max(abs(v) for v in sample['velocity'])<.01 else 0
                if count>=2:break
            await asyncio.sleep(.05)
        else:raise RuntimeError('simulator_stop_not_confirmed')
        self.bridge_active=False;self.owned_localization=False
        self.emit({'kind':'pose_bridge','state':'STOPPED','source':'isaac_ground_truth','zero_velocity_samples':count})

    async def cancel(self):
        result=await super().cancel()
        result['reason']=result['reason'].replace('owned_localization_STOPPED','owned_isaac_pose_bridge_STOPPED')
        return result

    async def close(self):
        try:
            if self.depth_node:
                self.executor.remove_node(self.depth_node);self.depth_node.destroy_node();self.depth_node=None
            await super().close()
        finally:
            if self.path_bridge and self.path_bridge.returncode is None:
                self.path_bridge.terminate()
                try:await asyncio.wait_for(self.path_bridge.wait(),3)
                except asyncio.TimeoutError:self.path_bridge.kill();await self.path_bridge.wait()
            if self.path_log:self.path_log.close();self.path_log=None
