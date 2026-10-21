"""Reproducible live classifier evaluation: isolated SQLite, real HTTP, no skills."""
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
from urllib.error import HTTPError
from urllib.request import urlopen

PROJECT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(PROJECT/'CoEgent_Agent'),str(PROJECT/'Executor_Adapters')]
from coegent import PlanStore,PlannerSelector
from coegent.planner import ModelServerPlanner
from coegent.planner_selector import SYSTEM
from coegent.planning import build_plan_context
from coegent.registry import SkillRegistry
from coegent.safety import SafetyPolicy
from coegent.scenarios import initial_state

HERE=Path(__file__).resolve().parent
LEGACY=json.loads((HERE/'legacy.json').read_text())

class Recorder(ModelServerPlanner):
    def __init__(self,*args,variant,**kwargs):
        super().__init__(*args,**kwargs);self.variant=variant;self.calls=[]
    async def complete(self,messages,**kwargs):
        if self.variant in ('legacy','prompt_only'):
            kwargs['schema']=deepcopy(LEGACY['schema'])
        row={'messages':deepcopy(messages),'options':deepcopy(kwargs)};started=monotonic()
        try:
            row['raw']=await super().complete(messages,**kwargs)
            return row['raw']
        except Exception as exc:
            row['error']=repr(exc)
            if isinstance(exc,HTTPError):row['http_error_body']=exc.read(8192).decode(errors='replace')
            raise
        finally:
            row['elapsed_s']=round(monotonic()-started,6);self.calls.append(row)

class EvaluatedSelector(PlannerSelector):
    def __init__(self,*args,variant,**kwargs):
        super().__init__(*args,**kwargs);self.variant=variant
    def _messages(self,payload):
        messages=[{'role':'system','content':LEGACY['system'] if self.variant=='legacy' else SYSTEM},
                  {'role':'user','content':json.dumps(payload,ensure_ascii=False,separators=(',',':'),allow_nan=False)}]
        return messages if sum(len(m['content'].encode()) for m in messages)<=self.max_prompt_bytes else None

async def run(args):
    corpus_bytes=(HERE/'cases.json').read_bytes()
    cases=[c for c in json.loads(corpus_bytes)['cases'] if c['split']==args.split and (not args.case_id or c['id'] in args.case_id)]
    variants=args.variants
    result={'model':args.model,'endpoint':args.endpoint,'split':args.split,'variants':variants,
            'repeat':args.repeat,'max_tokens':args.max_tokens,'case_filter':args.case_id,'corpus_sha256':hashlib.sha256(corpus_bytes).hexdigest(),
            'prompt_sha256':hashlib.sha256(SYSTEM.encode()).hexdigest(),
            'legacy_sha256':hashlib.sha256((HERE/'legacy.json').read_bytes()).hexdigest(),
            'scope':'selection only; real model HTTP; no generation/execution or production DB use','rows':[]}
    with urlopen(args.endpoint.rstrip('/')+'/v1/models',timeout=10) as response:
        result['server_models']=json.load(response)
    args.out.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='coegent-selector-eval-') as temp:
        for index,case in enumerate(cases):
            store=PlanStore(Path(temp)/(case['id']+'.sqlite3'));ids={}
            for entry in case['catalog']:
                ids[entry['key']]=store.add(entry['plan'],command=entry['command'],source='agent')['id']
            state=initial_state()
            context=build_plan_context(case['command'],state,SkillRegistry(),SafetyPolicy())
            retrieved=store.search(case['command'],state=state.snapshot(),limit=5)
            expected_id=ids.get(case['expected_plan']);retrieved_ids=[r['id'] for r in retrieved['candidates']]
            for iteration in range(args.repeat):
                for variant in variants:
                    model=Recorder(args.endpoint,args.model,timeout_s=90,max_tokens=args.max_tokens,variant=variant)
                    selector=EvaluatedSelector(store,model,variant=variant,max_tokens=args.max_tokens)
                    row={'id':case['id'],'category':case['category'],'command':case['command'],
                         'variant':variant,'iteration':iteration,'expected':case['expected'],
                         'expected_plan_id':expected_id,'retrieved_ids':retrieved_ids,
                         'retrieval_hit':expected_id in retrieved_ids if expected_id else None}
                    start=monotonic()
                    try:
                        chosen=await selector.select(context)
                        row.update(chosen.report());row['valid']=True
                        row['passed']=chosen.decision==case['expected'] and (chosen.decision!='reuse' or chosen.plan_id==expected_id)
                    except Exception as exc:
                        row.update(valid=False,passed=False,error=repr(exc),decision='error')
                    row['elapsed_s']=round(monotonic()-start,6);row['calls']=model.calls
                    row['wrong_reuse']=row['decision']=='reuse' and (case['expected']!='reuse' or row.get('plan_id')!=expected_id)
                    result['rows'].append(row)
                    args.out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
            print(json.dumps({'case':case['id'],'expected':case['expected'],
                'results':[(r['variant'],r['decision'],r['passed']) for r in result['rows'][-len(variants)*args.repeat:]]},ensure_ascii=False),flush=True)
    summaries={}
    for variant in variants:
        rows=[r for r in result['rows'] if r['variant']==variant]
        clarify=[r for r in rows if r['expected']=='clarify']
        confusion={e:{p:sum(r['expected']==e and r['decision']==p for r in rows)
                     for p in ('reuse','generate','clarify','error')} for e in ('reuse','generate','clarify')}
        summaries[variant]={'passed':sum(r['passed'] for r in rows),'count':len(rows),
            'valid':sum(r['valid'] for r in rows),'wrong_reuse':sum(r['wrong_reuse'] for r in rows),
            'clarify_correct':sum(r['passed'] for r in clarify),'clarify_count':len(clarify),
            'retrieval_misses':sum(r['retrieval_hit'] is False for r in rows),
            'median_s':round(statistics.median(r['elapsed_s'] for r in rows),4),'confusion':confusion}
    result['summary']=summaries
    args.out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(summaries,ensure_ascii=False),flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint',default='http://127.0.0.1:18520')
    parser.add_argument('--model',required=True)
    parser.add_argument('--split',choices=['dev','holdout'],required=True)
    parser.add_argument('--variants',nargs='+',choices=['legacy','prompt_only','improved'],default=['legacy','prompt_only','improved'])
    parser.add_argument('--repeat',type=int,default=1)
    parser.add_argument('--max-tokens',type=int,default=512)
    parser.add_argument('--case-id',action='append')
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    if not 1<=args.repeat<=3:parser.error('repeat must be 1..3')
    if not 64<=args.max_tokens<=4096:parser.error('max-tokens must be 64..4096')
    if args.case_id and args.split!='dev':parser.error('case filters are development probes only')
    asyncio.run(run(args))
