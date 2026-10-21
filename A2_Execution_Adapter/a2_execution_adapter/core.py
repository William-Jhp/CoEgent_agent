"""A2 router + the existing CoEgent MissionEngine; no ROS imports here."""
import asyncio
from copy import deepcopy
from dataclasses import asdict, is_dataclass
import math
from time import monotonic, time
from coegent.engine import MissionEngine
from coegent.types import Event, Limits, RuntimeHalt, SkillResult, State, Trace
from coegent.validation import PlanError, walk
from coegent.workflow import stage_graph

LIVE_CONDITIONS={'robot.battery_percent','robot.localization_valid','robot.estop'}

def validate_execution(raw, profile, options):
    plan,_=profile.validator.prepare(raw)
    for n in walk(plan['sequence']):
        if n['type']=='skill' and n['name'] in ('RobotBackend.go_to','RobotBackend.localize') and not options.allow_motion:
            raise PlanError('motion_disabled: set deployment allow_motion=true explicitly')
        if n['type']=='condition' and n['condition']['source'] not in LIVE_CONDITIONS:
            raise PlanError('unsupported_live_condition_source:'+n['condition']['source'])
        if n['type']=='abort' and n['return_home']:
            raise PlanError('abort return_home has no A2 API binding')
    return plan

def summarize(v, depth=0):
    """Keep opaque buffers local; reports contain finite, bounded summaries."""
    if v is None or isinstance(v,(str,bool,int)):return v
    if isinstance(v,float):
        if not math.isfinite(v):raise ValueError('non-finite API result')
        return v
    if depth>=5:return {'python_type':type(v).__name__,'truncated':True}
    if hasattr(v,'shape') and hasattr(v,'dtype'):
        return {'python_type':type(v).__name__,'shape':list(v.shape),'dtype':str(v.dtype)}
    if hasattr(v,'size') and hasattr(v,'mode'):
        return {'python_type':type(v).__name__,'size':list(v.size),'mode':v.mode}
    if hasattr(v,'as_dict'):return summarize(v.as_dict(),depth+1)
    if hasattr(v,'data') and isinstance(v.data,dict):return summarize(v.data,depth+1)
    if is_dataclass(v):return summarize(asdict(v),depth+1)
    if isinstance(v,dict):return {str(k):summarize(x,depth+1) for k,x in list(v.items())[:256]}
    if isinstance(v,(list,tuple)):
        a=[summarize(x,depth+1) for x in v[:256]]
        return a if len(v)<=256 else {'items':a,'total_count':len(v),'truncated':True}
    return {'python_type':type(v).__name__,'opaque':True}

class APIRouter:
    admission=None
    dispatch_check=None
    fresh=None
    def __init__(self,runtime,options,emit=None):
        self.runtime,self.options,self.emit=runtime,options,emit
        self.results={};self.api_results=[]
        self.checkpoint=self.refresh_condition
        self.state=None

    async def refresh_condition(self,*unused):
        live=await self.runtime.snapshot()
        # Only explicit measured fields can control execution. Caller state is not evidence.
        self.state.robot={k:v for k,v in live.get('robot',{}).items() if 'robot.'+k in LIVE_CONDITIONS}

    def bind(self,value):
        if isinstance(value,dict) and set(value)=={'$result','index'}:
            return self.results[value['$result']][value['index']]
        if isinstance(value,dict):return {k:self.bind(v) for k,v in value.items()}
        if isinstance(value,list):return [self.bind(v) for v in value]
        return value

    async def dispatch(self,name,args,state,trace,save_as=None):
        if name=='stop':
            # Keep independent localization feedback alive through inspection.
            # Full owned-resource cleanup still happens in runtime.close().
            stop_motion=getattr(self.runtime,'stop_motion',None)
            ack=await (stop_motion() if self.options.inspector_config is not None and callable(stop_motion)
                       else self.runtime.cancel())
            return SkillResult('stop',Event.SUCCESS if ack.get('confirmed') is True else Event.SAFETY_ABORT,
                               ack.get('reason','stop_unconfirmed'),{'stopped':ack.get('confirmed') is True,'scope':'adapter_owned_work'})
        trace.emit('skill_start',skill=name,args=deepcopy(args))
        if self.emit:self.emit({'kind':'api_start','name':name,'args':deepcopy(args)})
        try:
            value=await asyncio.wait_for(self.runtime.call(name,self.bind(args)),self.options.call_timeout_s)
            if (name=='RobotBackend.go_to' and value is not True) or (name=='GridNavigator.plan' and value is None):
                result=SkillResult(name,Event.EXPECTED_FAILURE,'api_returned_false_or_no_path')
            elif name=='WorldState.capture_views' and not value[0]:
                result=SkillResult(name,Event.EXPECTED_FAILURE,'no_fresh_camera_views')
            else:
                projected=({'path_xy':value[:4096].tolist(),'point_count':len(value),'truncated':len(value)>4096}
                           if name=='GridNavigator.plan' else summarize(value))
                if save_as:self.results[save_as]=value
                result=SkillResult(name,data={'value':projected})
        except asyncio.TimeoutError:
            raise RuntimeHalt('SAFETY_ABORT','api_timeout:'+name) from None
        except asyncio.CancelledError:raise
        except RuntimeHalt:raise
        except Exception as exc:result=SkillResult(name,Event.EXPECTED_FAILURE,f'{type(exc).__name__}:{exc}')
        row={'name':name,'args':deepcopy(args),**result.to_dict()}
        self.api_results.append(row);trace.emit('skill_result',**row)
        if self.emit:self.emit({'kind':'api_result',**row})
        return result

class APIExecution:
    def __init__(self,runtime,profile,options,emit=None,*,request=None):
        self.runtime,self.profile,self.options,self.emit=runtime,profile,options,emit
        self.request=request

    async def run(self,raw,state):
        trace=Trace();opts=self.options
        deadline=monotonic()+opts.mission_timeout_s
        if self.request is not None:
            deadline=min(deadline,monotonic()+max(0,self.request.expires_at-time()))
        try:plan=validate_execution(raw,self.profile,opts)
        except (PlanError,ValueError) as exc:
            return {'status':'INVALID_PLAN','execution_status':'NOT_EXECUTED','reason':str(exc),
                    'goal_status':'not_evaluated','api_results':[],'completed_steps':[],'trace':[]}
        state=State.from_dict(state.snapshot())
        router=APIRouter(self.runtime,opts,self.emit);router.state=state
        engine=MissionEngine(router,state,trace,Limits(max_steps=opts.max_steps,
                            mission_timeout_s=opts.mission_timeout_s,skill_timeout_s=opts.cancel_timeout_s))
        status,reason='SAFETY_ABORT','execution_incomplete'
        inspection=None
        stopped=inspected=False
        async def execute_node():
            nonlocal status,reason
            try:
                result=await asyncio.wait_for(engine.run(plan),opts.mission_timeout_s)
                status='SUCCESS' if result.ok else result.event.value
                reason=result.reason or ('api_sequence_completed' if result.ok else 'api_failed')
            except RuntimeHalt as exc:status,reason=exc.status,exc.reason
            except asyncio.TimeoutError:status,reason='SAFETY_ABORT','mission_timeout'
            except asyncio.CancelledError:status,reason='CANCELLED','execution_cancelled'
            except Exception as exc:status,reason='SAFETY_ABORT',f'{type(exc).__name__}:{exc}'
            return 'stop_motion'

        async def stop_node():
            nonlocal stopped,status,reason
            stopped=True
            cleanup=asyncio.create_task(engine.stop())
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await asyncio.shield(cleanup)
            if not engine.stop_confirmation or not engine.stop_confirmation['confirmed']:
                status,reason='SAFETY_ABORT','stop_unconfirmed'
            if engine.stop_confirmation:engine.stop_confirmation['scope']='adapter_owned_work'
            return 'inspect_completion' if opts.inspector_config is not None else 'finish'

        async def inspect_node():
            nonlocal inspected,status,reason,inspection
            inspected=True
            if opts.inspector_config is not None:
                from coegent.inspector import ReportMessage, build_inspector
                from .inspection import A2EvidenceProvider
                from uuid import uuid4
                if self.request is None:
                    status,reason='SAFETY_ABORT','inspector_request_identity_required'
                provider=A2EvidenceProvider(self.runtime,include_vision=bool(opts.inspector_config.get('visual_assertions')) or
                    any(c['source']=='vision' for c in opts.inspector_config.get('checks', [])))
                inspector=build_inspector(opts.inspector_config,provider)
                if self.request is not None:
                    message=ReportMessage(self.request.mission_id,self.request.revision,str(uuid4()),
                        'completed' if status=='SUCCESS' else 'error',reason,
                        severity='safety' if status in {'SAFETY_ABORT','CANCELLED'} else 'recoverable')
                    remaining=deadline-monotonic()
                    if remaining<=0:
                        status,reason='SAFETY_ABORT','mission_timeout'
                        inspection={'decision':'halt','verified':False,'reason':reason}
                    else:
                        task=asyncio.create_task(inspector.inspect(message))
                        try:
                            done,_=await asyncio.wait({task},timeout=remaining)
                            if not done:
                                status,reason='SAFETY_ABORT','mission_timeout'
                                inspection={'decision':'halt','verified':False,'reason':reason}
                            else:inspection=task.result().to_dict()
                        finally:
                            if not task.done():
                                task.cancel()
                                await asyncio.wait({task},timeout=min(.1,opts.cancel_timeout_s))
                            if task.done() and not task.cancelled():task.exception()
                    if inspector.pending:
                        status,reason='SAFETY_ABORT','inspection_cancellation_unconfirmed'
                    trace.emit('inspection',**inspection)
                    if status not in {'SAFETY_ABORT','CANCELLED'}:
                        status='SUCCESS' if inspection['verified'] else 'REPLAN_REQUIRED'
                        reason=inspection['reason']
                    if provider.last_snapshot is not None:
                        # Preserve independently measured values for continuation planning.
                        measured=provider.last_snapshot.values.get('robot_state', {})
                        state.robot={k:v for k,v in measured.items() if 'robot.'+k in LIVE_CONDITIONS}
                        state.observations['inspector_robot_state']=deepcopy(measured)
            return 'finish'

        async def finish_node():return None
        try:
            await stage_graph('a2_worker',{'execute_plan':execute_node,'stop_motion':stop_node,
                'inspect_completion':inspect_node,'finish':finish_node},trace=trace).run()
        except asyncio.CancelledError:status,reason='CANCELLED','execution_cancelled'
        except Exception as exc:status,reason='SAFETY_ABORT',f'{type(exc).__name__}:{exc}'
        finally:
            # Cancellation can interrupt a graph boundary. Resource release must
            # remain unconditional, outside the graph's normal transition path.
            try:
                if not stopped:await stop_node()
                if not inspected and opts.inspector_config is not None:await inspect_node()
            finally:
                try:await asyncio.wait_for(self.runtime.close(),opts.cancel_timeout_s)
                except Exception as exc:
                    status,reason='SAFETY_ABORT','runtime_close_failed:'+str(exc)
                    engine.stop_confirmation={'confirmed':False,'reason':reason}
        if opts.inspector_config is not None and status not in {'SAFETY_ABORT','CANCELLED'} and monotonic()>=deadline:
            status,reason='SAFETY_ABORT','mission_timeout'
        report={'status':status,'execution_status':status,'reason':reason,'goal_status':'not_evaluated',
                'mission_success':None,'orchestrator':'langgraph','success_criteria_verified':False,'api_results':router.api_results,
                'completed_steps':deepcopy(engine.completed),'remaining_plan':deepcopy(engine.remaining),
                'failed_step':deepcopy(engine.failed),
                'steps':engine.steps,'stop_confirmation':engine.stop_confirmation,'state':state.snapshot(),
                'trace':trace.entries,'elapsed_s':trace.entries[-1]['elapsed_s'] if trace.entries else 0}
        if opts.inspector_config is not None:
            verified=status=='SUCCESS' and inspection is not None and inspection['verified']
            report.update(inspection=inspection,mission_success=bool(verified),success_criteria_verified=bool(verified),
                completion_report={'mission_id':getattr(self.request,'mission_id',None),
                    'revision':getattr(self.request,'revision',None),'verified':bool(verified),'status':status,
                    'reason':reason,'next_action':'report_complete' if verified else
                        'halt' if status in {'SAFETY_ABORT','CANCELLED'} else inspection['decision'] if inspection else 'request_evidence'})
        return report
