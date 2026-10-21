"""One owned ROS mission. JSON protocol on stdout; all library logs on stderr."""
import asyncio
from dataclasses import asdict
import json
import signal
import sys
import time

WIRE=sys.stdout
sys.stdout=sys.stderr

def send(kind,**fields):
    WIRE.write(json.dumps({'kind':kind,**fields},ensure_ascii=False,allow_nan=False)+'\n');WIRE.flush()

def rejected(status,reason):
    return {'status':status,'execution_status':'NOT_EXECUTED','reason':reason,'goal_status':'not_evaluated',
            'stop_confirmation':{'confirmed':True,'reason':'no_dispatch'},'api_results':[],
            'steps':0,'completed_steps':[],'trace':[]}

async def process(packet):
    from coegent.admission import MissionRequest
    from coegent.tool_profiles import A2ToolProfile
    from coegent.types import State
    from coegent.validation import PlanError
    from .core import APIExecution, validate_execution
    from .binding import portable_contract, scene_binding
    from .journal import Journal
    from .options import Options
    from .ros_runtime import ROSRuntime
    from .isaac_pose import IsaacPoseRuntime
    runtime=None
    try:
        options=Options.from_dict(packet.get('options',{}))
        req=MissionRequest(**packet['request'])
        if req.goals:raise ValueError('approved goal evidence is not bound for A2')
        if req.expires_at<=time.time():return rejected('REJECTED','request_expired')
        profile=A2ToolProfile(options.source_root,scene_dir=options.scene_dir)
        if packet.get('contract_hash',portable_contract(profile))!=portable_contract(profile):
            raise ValueError('source_api_contract_mismatch')
        if packet.get('scene_binding',profile.scene_binding)!=profile.scene_binding:
            raise ValueError('scene_changed_since_prepare')
        kind=packet.get('kind')
        if kind not in ('execute','diagnose','reconcile'):raise ValueError('unknown worker request')
        plan=validate_execution(packet.get('plan'),profile,options) if kind=='execute' else None
        state=State.from_dict(packet.get('state') or State().snapshot())
        motion=bool(plan and any(n.get('name') in ('RobotBackend.go_to','RobotBackend.localize')
                               for n in __import__('coegent.validation',fromlist=['walk']).walk(plan['sequence'])))
        with Journal(options.runtime_dir,'isaac-domain72-base_link') as journal:
            if kind=='reconcile':
                old=journal.failed_localization(req.mission_id,req.revision,packet.get('legacy_report'))
                if packet['task_id']!=old['runtime_ownership']['task_id']:raise ValueError('recovery_owner_differs_from_journal')
                runtime=ROSRuntime(options)
                try:
                    await asyncio.wait_for(runtime.open(),options.startup_timeout_s)
                    evidence=await runtime.reset_failed_localization(packet['task_id'],old['map_identity']['map_uuid'])
                    journal.reconcile_localization(req.mission_id,req.revision,evidence,packet.get('legacy_report'))
                    return {'status':'SUCCESS','execution_status':'NOT_EXECUTED','reason':'failed_localization_reconciled',
                            'state':await runtime.snapshot(),'recovery':evidence,'goal_status':'not_evaluated','api_results':[],
                            'steps':0,'completed_steps':[],'trace':[],'stop_confirmation':{'confirmed':True,'reason':'verified_ERROR_to_STOPPED_reset'}}
                finally:await runtime.close()
            replay=journal.begin(req.mission_id,req.revision,packet,motion=motion)
            if replay:return replay
            runtime_type=IsaacPoseRuntime if options.pose_source=='isaac_ground_truth' else ROSRuntime
            runtime=runtime_type(options,lambda e:send('event',event=e))
            try:
                await asyncio.wait_for(runtime.open(),options.startup_timeout_s)
                if req.expires_at<=time.time():raise ValueError('request_expired_before_dispatch')
                if kind=='diagnose':
                    report={'status':'SUCCESS','execution_status':'NOT_EXECUTED','reason':'read_only_diagnostics',
                            'state':await runtime.snapshot(),'goal_status':'not_evaluated','api_results':[],
                            'steps':0,'completed_steps':[],'trace':[],
                            'stop_confirmation':{'confirmed':True,'reason':'no_adapter_owned_motion'}}
                    await runtime.close()
                else:
                    # Also cap the execution by the original request's expiry.
                    remaining=max(.001,min(options.mission_timeout_s,req.expires_at-time.time()))
                    from dataclasses import replace
                    execution=APIExecution(runtime,profile,replace(options,mission_timeout_s=remaining),lambda e:send('event',event=e), request=req)
                    report=await execution.run(plan,state)
                report.update(mission_id=req.mission_id,revision=req.revision,map_identity=runtime.identity)
            except BaseException as exc:
                try:ack=await asyncio.wait_for(runtime.cancel(),options.cancel_timeout_s)
                except BaseException:ack={'confirmed':False,'reason':'startup_cleanup_unconfirmed'}
                try:await asyncio.wait_for(runtime.close(),options.cancel_timeout_s)
                except BaseException:ack={'confirmed':False,'reason':'runtime_close_unconfirmed'}
                status='CANCELLED' if isinstance(exc,asyncio.CancelledError) and ack['confirmed'] else 'SAFETY_ABORT'
                report={'status':status,'execution_status':status,
                        'reason':type(exc).__name__+':'+str(exc),'stop_confirmation':ack,'goal_status':'not_evaluated',
                        'api_results':[],'steps':0,'completed_steps':[],'trace':[]}
            if runtime and runtime.backend:
                report['runtime_ownership']={'task_id':runtime.backend.task_id,'map_uuid':runtime.identity['map_uuid'],
                                             'path_dispatched':runtime.path_dispatched,'pose_source':options.pose_source}
            journal.finish(req.mission_id,req.revision,report)
            return report
    except (PlanError,ValueError,KeyError,TypeError) as exc:
        return rejected('INVALID_PLAN' if isinstance(exc,PlanError) else 'REJECTED',str(exc))

async def main():
    from coegent.validation import strict_json
    loop=asyncio.get_running_loop()
    reader=asyncio.StreamReader(limit=1024*1024)
    pipe,_=await loop.connect_read_pipe(lambda:asyncio.StreamReaderProtocol(reader),sys.stdin)
    try:
        first=await reader.readline()
        if not first:send('result',report=rejected('REJECTED','no_request'));return
        packet=strict_json(first.decode())
        if not isinstance(packet,dict):raise ValueError('request must be object')
        task=asyncio.create_task(process(packet))
        async def controls():
            while not task.done():
                line=await reader.readline()
                if not line or strict_json(line.decode()).get('kind')=='cancel':
                    task.cancel();return
        control=asyncio.create_task(controls())
        for sig in (signal.SIGINT,signal.SIGTERM):loop.add_signal_handler(sig,task.cancel)
        try:report=await task
        except asyncio.CancelledError:report=rejected('CANCELLED','cancelled_before_dispatch')
        finally:
            control.cancel()
            await asyncio.gather(control,return_exceptions=True)
        request=packet.get('request',{})
        report.update(mission_id=request.get('mission_id'),revision=request.get('revision'))
        send('result',report=report)
    except Exception as exc:send('result',report=rejected('REJECTED',type(exc).__name__+':'+str(exc)))
    finally:pipe.close()

if __name__=='__main__':asyncio.run(main())
