"""Live two-role benchmark. Observes current CoEgent calls; never executes skills."""
import argparse
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import statistics
import sys
import tempfile
from time import monotonic
from urllib.request import Request,urlopen
from urllib.error import HTTPError

HERE=Path(__file__).resolve().parent;PROJECT=HERE.parents[2]
sys.path[:0]=[str(PROJECT/'CoEgent_Agent'),str(PROJECT/'Executor_Adapters')]
from coegent import PlanGenerator,PlannerSelector,PlanStore
from coegent.planner import ModelServerPlanner,response_format
from coegent.planning import build_plan_context
from coegent.registry import SkillRegistry
from coegent.safety import SafetyPolicy
from coegent.scenarios import initial_state
from coegent.types import State
from coegent_contracts import RuntimeConfig
from coegent.validation import PlanValidator
from coegent.planner_selector import SYSTEM
from grading import score

class ObservedModel(ModelServerPlanner):
    def __init__(self,*args,**kwargs):super().__init__(*args,**kwargs);self.calls=[]
    async def complete(self,messages,*,schema=None,max_tokens=None,require_complete=False):
        payload={'model':self.model,'messages':deepcopy(messages),'temperature':0,'stream':False,
                 'max_tokens':self.max_tokens if max_tokens is None else max_tokens}
        if schema is not None:payload['response_format']=response_format(schema)
        row={'request':payload};start=monotonic()
        def post():
            req=Request(self.endpoint+'/v1/chat/completions',json.dumps(payload).encode(),{'Content-Type':'application/json'})
            with urlopen(req,timeout=self.timeout_s) as response:
                body=response.read(2*1024*1024+1)
            if len(body)>2*1024*1024:raise ValueError('response too large')
            return json.loads(body)
        try:
            data=await asyncio.to_thread(post);row['response']=data
            choice=data['choices'][0];content=choice['message'].get('content')
            row['finished']=choice.get('finish_reason')=='stop' and isinstance(content,str) and bool(content.strip())
            if require_complete and choice.get('finish_reason')!='stop':raise ValueError('model_response_incomplete')
            return content
        except Exception as exc:
            row['error']=repr(exc)
            if isinstance(exc,HTTPError):row['http_error_body']=exc.read(8192).decode(errors='replace')
            raise
        finally:
            row['elapsed_s']=round(monotonic()-start,6);self.calls.append(row)

def stats(values):
    if not values:return {'n':0}
    ordered=sorted(values)
    def percentile(q):
        pos=(len(ordered)-1)*q;lo=int(pos);hi=min(lo+1,len(ordered)-1)
        return ordered[lo]+(ordered[hi]-ordered[lo])*(pos-lo)
    return {'n':len(values),'mean_s':round(statistics.mean(values),4),'p50_s':round(percentile(.5),4),
            'p95_s':round(percentile(.95),4),'min_s':round(min(values),4),'max_s':round(max(values),4)}

def summarize(rows):
    result={}
    for phase in sorted({r['phase'] for r in rows}):
        selected=[r for r in rows if r['phase']==phase]
        result[phase]={'count':len(selected),'passed':sum(r['passed'] for r in selected),
            'finished':sum(r['finished'] for r in selected),
            'latency_all':stats([r['elapsed_s'] for r in selected]),
            'latency_passed':stats([r['elapsed_s'] for r in selected if r['passed']])}
        if phase=='planner':
            result[phase].update(plan_ready=sum(r['report']['status']=='PLAN_READY' for r in selected),
                relaxed_passed=sum(r['relaxed_passed'] for r in selected),
                linear_passed=sum(r['passed'] for r in selected if r['case_id']<='p18'),
                conditional_passed=sum(r['passed'] for r in selected if r['case_id']>'p18'))
        if phase=='selector':
            result[phase].update(wrong_reuse=sum(r['wrong_reuse'] for r in selected),
                retrieval_misses=sum(r['retrieval_miss'] for r in selected),
                per_route={route:{'passed':sum(r['passed'] for r in selected if r['expected']==route),
                    'count':sum(r['expected']==route for r in selected)} for route in ('reuse','generate','clarify')})
        if phase=='combined':result[phase]['route_correct']=sum(r['route_correct'] for r in selected)
    return result

async def main(args):
    planners=json.loads((HERE/'planner_cases.json').read_text())['cases']
    selector_file=HERE.parent/'planner_selector/cases.json'
    selectors=json.loads(selector_file.read_text())['cases']
    output={'mode':args.mode,'model':args.model,'endpoint':args.endpoint,'repeat':args.repeat,
            'planner_max_tokens':args.planner_tokens,'selector_max_tokens':args.selector_tokens,
            'planner_corpus_sha256':hashlib.sha256((HERE/'planner_cases.json').read_bytes()).hexdigest(),
            'selector_corpus_sha256':hashlib.sha256(selector_file.read_bytes()).hexdigest(),
            'selector_prompt_sha256':hashlib.sha256(SYSTEM.encode()).hexdigest(),'rows':[]}
    args.out.mkdir(parents=True,exist_ok=True)
    for route,key in [('/v1/models','server_models'),('/props','server_props')]:
        with urlopen(args.endpoint+route,timeout=10) as response:output[key]=json.load(response)
    def save(row):
        output['rows'].append(row)
        with (args.out/'rows.jsonl').open('a') as stream:stream.write(json.dumps(row,ensure_ascii=False)+'\n')
        output['summary']=summarize(output['rows'])
        (args.out/'results.json').write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({'phase':row['phase'],'id':row['case_id'],'iteration':row['iteration'],'passed':row['passed'],
                          'elapsed_s':row['elapsed_s'],'status':row.get('report',{}).get('status'),
                          'decision':row.get('decision')},ensure_ascii=False),flush=True)
    def client():return ObservedModel(args.endpoint,args.model,timeout_s=90,max_tokens=args.planner_tokens)
    validator=PlanValidator(SkillRegistry())
    for case in planners:validator.prepare(case['reference'])
    if args.mode=='pilot':
        planners=[r for r in planners if r['id'] in ('p01','p10','p19')]
        selectors=[r for r in selectors if r['id'] in ('d01','d05','d09')]
    repeats=1 if args.mode=='pilot' else args.repeat
    for iteration in range(repeats):
        for case in planners:
            model=client();generator=PlanGenerator(model,config=RuntimeConfig(max_repairs=0,planner_timeout_s=90))
            start=monotonic();report=await generator.generate(case['command'],State.from_dict(case['state']))
            elapsed=monotonic()-start;finished=bool(model.calls) and all(r.get('finished',False) for r in model.calls)
            grade=score(report['plan'],case) if report.get('plan') else {'strict':False,'relaxed':False,'reason':'no_validated_plan'}
            assert report['execution_status']=='NOT_EXECUTED'
            save({'phase':'planner','case_id':case['id'],'command':case['command'],'iteration':iteration,
                  'passed':finished and report['status']=='PLAN_READY' and grade['strict'],
                  'relaxed_passed':finished and report['status']=='PLAN_READY' and grade['relaxed'],
                  'finished':finished,'elapsed_s':round(elapsed,6),'grading':grade,'report':report,'calls':model.calls})
    with tempfile.TemporaryDirectory(prefix='planner-sft-role-eval-') as temp:
        for iteration in range(repeats):
            for case in selectors:
                store=PlanStore(Path(temp)/(case['id']+'.sqlite3'));ids={}
                for entry in case['catalog']:ids[entry['key']]=store.add(entry['plan'],command=entry['command'])['id']
                model=client();selector=PlannerSelector(store,model,max_tokens=args.selector_tokens)
                context=build_plan_context(case['command'],initial_state(),SkillRegistry(),SafetyPolicy())
                search=store.search(case['command'],state=initial_state().snapshot());expected_id=ids.get(case['expected_plan'])
                start=monotonic();row={'phase':'selector','case_id':case['id'],'command':case['command'],
                    'iteration':iteration,'expected':case['expected'],'expected_plan_id':expected_id,
                    'retrieved_ids':[r['id'] for r in search['candidates']],
                    'retrieval_miss':bool(expected_id and expected_id not in [r['id'] for r in search['candidates']])}
                try:
                    chosen=await selector.select(context);row.update(chosen.report())
                    row['passed']=chosen.decision==case['expected'] and (chosen.decision!='reuse' or chosen.plan_id==expected_id)
                except Exception as exc:row.update(passed=False,decision='error',error=repr(exc))
                row.update(elapsed_s=round(monotonic()-start,6),finished=bool(model.calls) and all(r.get('finished',False) for r in model.calls),calls=model.calls)
                row['wrong_reuse']=row['decision']=='reuse' and (case['expected']!='reuse' or row.get('plan_id')!=expected_id)
                row['passed']=row['passed'] and row['finished'];save(row)
        if args.mode!='pilot':
            combined=[r for r in selectors if r['id'] in ('d01','d03','d05','d07','d09','d10')]
            for iteration in range(args.repeat):
                for case in combined:
                    store=PlanStore(Path(temp)/('combined_'+case['id']+'.sqlite3'));ids={}
                    for entry in case['catalog']:ids[entry['key']]=store.add(entry['plan'],command=entry['command'])['id']
                    model=client();selector=PlannerSelector(store,model,max_tokens=args.selector_tokens)
                    generator=PlanGenerator(model,planner_selector=selector,config=RuntimeConfig(max_repairs=0,planner_timeout_s=90))
                    start=monotonic();report=await generator.generate(case['command'],initial_state());elapsed=monotonic()-start
                    selected=report['planner_selections'][-1] if report['planner_selections'] else {}
                    route_correct=selected.get('decision')==case['expected'] and (case['expected']!='reuse' or selected.get('plan_id')==ids[case['expected_plan']])
                    finished=bool(model.calls) and all(r.get('finished',False) for r in model.calls)
                    grade=None
                    if case['expected']=='clarify':passed=route_correct and report['status']=='NEEDS_CLARIFICATION' and bool(report.get('question'))
                    else:
                        reference=case['catalog'][next(i for i,x in enumerate(case['catalog']) if x['key']==case['expected_plan'])]['plan'] if case['expected']=='reuse' else json.loads((HERE/'planner_cases.json').read_text())['cases'][1]['reference']
                        grading_case={'state':initial_state().snapshot(),'reference':reference,'bindings':[{}]}
                        grade=score(report['plan'],grading_case) if report.get('plan') else {'strict':False,'relaxed':False,'reason':'no_validated_plan'}
                        passed=route_correct and report['status']=='PLAN_READY' and grade['strict']
                    assert report['execution_status']=='NOT_EXECUTED'
                    assert not any(e['kind'].startswith(('skill_','executor_')) for e in report['trace'])
                    save({'phase':'combined','case_id':case['id'],'command':case['command'],'iteration':iteration,
                        'expected':case['expected'],'decision':selected.get('decision'),'route_correct':route_correct,
                        'passed':bool(passed and finished),'finished':finished,'elapsed_s':round(elapsed,6),
                        'grading':grade,'report':report,'calls':model.calls})
    output['summary']=summarize(output['rows']);(args.out/'results.json').write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'summary':output['summary']},ensure_ascii=False),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--mode',choices=['pilot','full'],required=True)
    p.add_argument('--model',default='coegent-planner-sft-q5');p.add_argument('--endpoint',default='http://127.0.0.1:18520')
    p.add_argument('--planner-tokens',type=int,default=1536);p.add_argument('--selector-tokens',type=int,default=512)
    p.add_argument('--repeat',type=int,default=2);p.add_argument('--out',type=Path,required=True)
    args=p.parse_args()
    if (args.out/'results.json').exists() or (args.out/'rows.jsonl').exists():p.error('use a fresh output folder')
    asyncio.run(main(args))
