"""Selected A2-profile CLI route; the legacy mock runtime remains independent."""
import asyncio
from dataclasses import asdict, replace
from copy import deepcopy
from pathlib import Path
from time import monotonic, time
from uuid import uuid4

from coegent.admission import MissionRequest
from coegent.planning import PlanGenerator
from coegent.plan_store import PlanStore
from coegent.tool_profiles import profile_db, A2ToolProfile
from coegent.types import State, Trace
from coegent.validation import PlanError, strict_json, walk
from coegent_contracts import RuntimeConfig
from coegent_executor_adapters import ExecutorContext
from .options import Options

def rejected(status,reason):
    return {'status':status,'execution_status':'NOT_EXECUTED','reason':reason,'goal_status':'not_evaluated',
            'stop_confirmation':{'confirmed':True,'reason':'no_dispatch'},'api_results':[],
            'steps':0,'completed_steps':[],'trace':[]}

async def execute_plan(plan,request,state,profile,options,registry):
    from .binding import portable_contract
    fresh=A2ToolProfile(options.source_root,scene_dir=options.scene_dir)
    if fresh.scene_binding!=profile.scene_binding or portable_contract(fresh)!=portable_contract(profile):
        return rejected('INVALID_PLAN','source_or_map_changed_since_planning')
    trace=Trace()
    limits=RuntimeConfig(mission_timeout_s=options.mission_timeout_s,skill_timeout_s=options.cancel_timeout_s)
    context=ExecutorContext('a2-isaac',None,state,trace,limits,request)
    factory=registry.resolve('a2-isaac')
    adapter=factory(context,asdict(options))
    try:
        if request.expires_at<=time():return rejected('REJECTED','request_expired')
        prepared=await adapter.prepare(plan)
        if (prepared.payload.get('scene_binding')!=profile.scene_binding or
                prepared.payload.get('contract_hash')!=portable_contract(profile)):
            raise ValueError('source_or_map_changed_during_preparation')
        trace.emit('executor_prepare',executor='a2-isaac')
        result=await adapter.execute(prepared)
        trace.emit('executor_execute',executor='a2-isaac',event=result.event.value)
        report=result.data.get('report')
        if not isinstance(report,dict):return rejected('SAFETY_ABORT','invalid_a2_executor_report')
        report={**report,'adapter_trace':trace.entries}
        return report
    except (PlanError,ValueError) as exc:return rejected('INVALID_PLAN',str(exc))
    except asyncio.CancelledError:
        cleanup=asyncio.create_task(adapter.cancel())
        try:await asyncio.shield(cleanup)
        except asyncio.CancelledError:await asyncio.shield(cleanup)
        ack=cleanup.result()
        report=adapter.report or rejected('CANCELLED','cancelled')
        report.update(status='CANCELLED' if ack.ok else 'SAFETY_ABORT',execution_status='CANCELLED' if ack.ok else 'SAFETY_ABORT',
                      stop_confirmation={'confirmed':ack.ok,'reason':ack.reason})
        return report
    finally:await adapter.close()


async def run_inspected_plan(plan,request,state,profile,options,registry,*,generator=None,max_replans=0,deadline=None):
    """LangGraph-owned serial continuations; provided requests have no hidden planner."""
    from coegent.workflow import stage_graph
    trace=Trace()
    deadline=min(deadline or monotonic()+options.mission_timeout_s,
                 monotonic()+max(0,request.expires_at-time()))
    attempts=[];planning_reports=[];completed=[];steps=0;replans=0
    current=request;report=None;candidate=None

    async def execute_node():
        nonlocal report,steps
        if options.inspector_config is None:
            report=await execute_plan(plan,request,state,profile,options,registry)
            return 'finish'
        remaining=deadline-monotonic()
        if remaining<=0 or steps>=options.max_steps:
            report=rejected('SAFETY_ABORT','mission_timeout' if remaining<=0 else 'step_budget_exhausted')
            return 'finish'
        attempt_options=replace(options,mission_timeout_s=remaining,max_steps=options.max_steps-steps)
        report=await execute_plan(plan,current,state,profile,attempt_options,registry)
        attempts.append(deepcopy(report));steps+=report.get('steps',0)
        completed.extend(deepcopy(report.get('completed_steps',[])))
        return 'evaluate_result'

    async def evaluate_result():
        if monotonic()>=deadline or time()>=request.expires_at:
            report.update(status='SAFETY_ABORT',execution_status='SAFETY_ABORT',reason='mission_timeout',mission_success=False)
            return 'finish'
        if report.get('stop_confirmation',{}).get('confirmed') is not True:
            report.update(status='SAFETY_ABORT',execution_status='SAFETY_ABORT',reason='stop_unconfirmed',mission_success=False)
            return 'finish'
        decision=report.get('inspection') or {}
        if (report['status']!='REPLAN_REQUIRED' or decision.get('decision') not in {'replan','request_evidence'}
                or generator is None or replans>=max_replans):return 'finish'
        return 'plan_remaining'

    async def plan_remaining():
        nonlocal state,candidate
        remaining=deadline-monotonic()
        if remaining<=0:
            report.update(status='SAFETY_ABORT',execution_status='SAFETY_ABORT',reason='mission_timeout',mission_success=False)
            return 'finish'
        state=State.from_dict(report.get('state',State().snapshot()))
        feedback={'kind':'replan','reason':report['reason'],'inspection':report.get('inspection') or {},
                  'instruction':'Plan remaining work only; never repeat completed side effects.'}
        task=asyncio.create_task(generator.generate(request.command,state,feedback=feedback,
            completed_steps=completed,failed_step=report.get('failed_step'),remaining_plan=report.get('remaining_plan',[])))
        generator.track(task)
        try:
            done,_=await asyncio.wait({task},timeout=remaining)
            if not done:
                report.update(status='SAFETY_ABORT',execution_status='SAFETY_ABORT',reason='mission_timeout',mission_success=False)
                return 'finish'
            planning=task.result()
        finally:
            if not task.done():
                task.cancel();await asyncio.wait({task},timeout=options.cancel_timeout_s)
        planning_reports.append(planning)
        if planning['status']!='PLAN_READY':
            report.update(status=planning['status'],reason=planning['reason'],question=planning.get('question'),mission_success=False)
            return 'finish'
        candidate=planning['plan'];return 'validate_continuation'

    async def validate_continuation():
        nonlocal replans,plan,current
        repeated=any(n['type']=='skill' and profile.registry.tools[n['name']]['kind'] in
            {'motion','localization','lifecycle'} and {'name':n['name'],'args':n['args']} in completed
            for n in walk(candidate['sequence']))
        if repeated:
            report.update(status='REPLAN_REQUIRED',execution_status='REPLAN_REQUIRED',
                          reason='continuation_repeats_completed_api',mission_success=False)
            return 'finish'
        replans+=1;plan=candidate;current=replace(request,mission_id=str(uuid4()),plan=plan)
        return 'execute_plan'

    async def finish():
        if options.inspector_config is None:return None
        report.update(execution_attempts=attempts,continuation_planning_reports=planning_reports,
            replans=replans,steps=steps,mission_completed_steps=completed,plan=plan,
            mission_id=request.mission_id,revision=request.revision)
        report['generation_cleanup_pending']=bool(generator is not None and generator.pending)
        if report.get('completion_report') is not None:
            verified=report['status']=='SUCCESS' and report['completion_report'].get('verified') is True
            report['completion_report'].update(mission_id=request.mission_id,revision=request.revision,
                verified=verified,status=report['status'],reason=report['reason'],
                next_action='report_complete' if verified else 'halt' if report['status']=='SAFETY_ABORT' else 'replan')
        else:
            report['mission_success']=False
            report['completion_report']={'mission_id':request.mission_id,'revision':request.revision,
                'verified':False,'status':report['status'],'reason':report['reason'],
                'next_action':'halt' if report['status'] in {'SAFETY_ABORT','CANCELLED'} else 'replan'}

    await stage_graph('a2_continuation',{'execute_plan':execute_node,'evaluate_result':evaluate_result,
        'plan_remaining':plan_remaining,'validate_continuation':validate_continuation,'finish':finish},trace=trace).run(
            max_transitions=6*(max_replans+1)+5)
    report.update(orchestrator='langgraph',workflow_trace=trace.entries)
    return report

async def run_cli(args,profile):
    from coegent import cli
    start=monotonic()
    if args.mode=='request' and (args.state_mode!='snapshot' or args.max_observation_age is not None or args.max_inspection_age is not None):
        raise ValueError('legacy strict-state flags are not bound to A2; the adapter enforces live ROS checks')
    raw_options=strict_json(Path(args.executor_options).read_text()) if args.executor_options else {}
    if getattr(args,'inspection_evidence',None):
        raise ValueError('A2 Inspector uses independent live ROS/camera evidence; side-file evidence is for native/offline inspection')
    if getattr(args,'inspector_config',None):
        if 'inspector_config' in raw_options:
            raise ValueError('provide inspector_config in executor options OR --inspector-config')
        raw_options['inspector_config']=strict_json(Path(args.inspector_config).read_text())
    raw_options.setdefault('source_root',str(profile.root))
    if profile.selected_scene:raw_options.setdefault('scene_dir',str(profile.scene_dir))
    options=Options.from_dict(raw_options)
    if Path(options.source_root).resolve()!=profile.root:raise ValueError('executor source_root differs from planning API source')
    from coegent.a2_maps import scene_path
    if scene_path(profile.root,options.scene_dir)!=profile.scene_dir:
        raise ValueError('executor scene differs from planning map; select matching --map-scene')
    registry=cli.default_registry();registry.resolve(args.executor)
    state=State.from_dict(strict_json(Path(args.state).read_text())) if args.state else State()
    planning=None;model_metadata={'planner':'provided_plan','model':None}
    planner=selector=generator=None
    deadline=start+min(options.mission_timeout_s,getattr(args,'mission_timeout',options.mission_timeout_s))
    if args.mode=='request':
        payload=strict_json(Path(args.request_file).read_text())
        if not isinstance(payload,dict) or set(payload)-{'mission_id','revision','expires_at','command','plan','goals'}:
            raise ValueError('unsupported request fields')
        if payload.get('goals'):raise ValueError('approved goal evidence is not bound for A2')
        payload.pop('goals',None)
        if args.plan_id is not None:
            if payload.get('plan') is not None:raise ValueError('cannot provide both inline plan and --plan-id')
            payload['plan']=PlanStore(profile_db(profile,args.plan_db),registry=profile.registry).get(args.plan_id)['plan']
        request=MissionRequest(**payload)
        if request.plan is None:raise ValueError('A2 request requires a provided plan')
        plan=request.plan
    else:
        planner,selector=cli.build_planners(args,tool_profile=profile)
        model_metadata={'planner':type(planner).__name__,'model':getattr(planner,'model_path',getattr(args,'model',None))}
        generator=PlanGenerator(planner,tool_profile=profile,planner_selector=selector,
            config=RuntimeConfig(max_repairs=args.max_repairs,
                                 planner_timeout_s=min(args.planner_timeout,max(.001,deadline-monotonic()))))
        try:
            planning=await generator.generate(args.command,state)
        except BaseException:
            close_models(planner,selector,generator=generator)
            raise
        if planning['status']!='PLAN_READY':
            close_models(planner,selector,generator=generator)
            cli.write_result(args.out,planning);cli.print_planning_summary(planning)
            return 2
        plan=planning['plan']
        request=MissionRequest(str(uuid4()),1,time()+max(0,deadline-monotonic()),args.command,plan)
    models_closed=False
    if options.inspector_config is None:
        models_closed=close_models(planner,selector,generator=generator)
    try:
        if A2ToolProfile(profile.root,scene_dir=options.scene_dir).scene_binding!=profile.scene_binding:
            raise ValueError('map changed during planning')
        report=await run_inspected_plan(plan,request,state,profile,options,registry,generator=generator,
            max_replans=getattr(args,'max_replans',0),deadline=deadline)
    finally:
        if not models_closed:close_models(planner,selector,generator=generator)
    continuations=report.get('continuation_planning_reports',[])
    report.update(executor='a2-isaac',tool_profile={'name':profile.name,'source_root':str(profile.root),
                  'contract_hash':profile.registry.fingerprint},plan=report.get('plan',plan),
                  planning_report=planning,planner_calls=(planning['planner_calls'] if planning else 0)+sum(p['planner_calls'] for p in continuations),
                  auto_saved_plan=None,auto_save_reason='goal_evidence_not_bound',
                  planning_elapsed_s=planning['elapsed_s'] if planning else 0,
                  elapsed_s=round(monotonic()-start,6),execution_binding='a2-isaac',
                  run_metadata={**model_metadata,
                                'backend':'A2 RobotBackend / ROS2 Docker worker'})
    cli.write_result(args.out,report)
    print(__import__('json').dumps({k:report.get(k) for k in ('status','execution_status','reason','executor','elapsed_s','stop_confirmation')},ensure_ascii=False))
    print('Saved: '+args.out)
    return 0 if report['status']=='SUCCESS' else (130 if report['status']=='CANCELLED' else 2)


def close_models(planner,selector,*,generator=None):
    pending=generator.pending if generator is not None else ()
    if pending:
        # In-process native inference must not lose its model/engine while its
        # cancellation handler is still draining. Deferred callbacks own cleanup.
        released=False
        def retry(done):
            nonlocal released
            if not released and not generator.pending:
                released=True
                close_models(planner,selector)
        for task in pending:task.add_done_callback(retry)
        return False
    seen=set()
    for model in (planner,getattr(selector,'llm',None)):
        if model is not None and id(model) not in seen:
            seen.add(id(model))
            if callable(getattr(model,'close',None)):model.close()
    return True
