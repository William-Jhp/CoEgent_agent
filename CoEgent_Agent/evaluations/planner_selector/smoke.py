"""Actual PlanGenerator routing smoke against the already selected external model."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile

PROJECT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(PROJECT/'CoEgent_Agent'),str(PROJECT/'Executor_Adapters')]
from coegent import PlanStore,PlanGenerator,PlannerSelector
from coegent.scenarios import initial_state
from coegent_contracts import RuntimeConfig
from run import Recorder,HERE

async def main(args):
    selected={'d01','d05','d09'}
    cases=[r for r in json.loads((HERE/'cases.json').read_text())['cases'] if r['id'] in selected]
    data={'model':args.model,'scope':'real external HTTP; plan mode only; three development flow examples','rows':[]}
    with tempfile.TemporaryDirectory(prefix='selector-flow-') as temp:
        for case in cases:
            store=PlanStore(Path(temp)/(case['id']+'.sqlite3'))
            for entry in case['catalog']:store.add(entry['plan'],command=entry['command'])
            model=Recorder(args.endpoint,args.model,timeout_s=90,max_tokens=1024,variant='improved')
            generator=PlanGenerator(model,planner_selector=PlannerSelector(store,model),config=RuntimeConfig(max_repairs=0))
            report=await generator.generate(case['command'],initial_state())
            assert report['execution_status']=='NOT_EXECUTED'
            assert not any(e['kind'].startswith(('skill_','executor_')) for e in report['trace'])
            actual=report['planner_selections'][-1]['decision'] if report['planner_selections'] else None
            row={'id':case['id'],'command':case['command'],'expected':case['expected'],'actual':actual,
                 'passed':actual==case['expected'] and report['status'] in ('PLAN_READY','NEEDS_CLARIFICATION'),
                 'report':report,'calls':model.calls}
            data['rows'].append(row)
            print(json.dumps({k:row[k] for k in ('id','expected','actual','passed')},ensure_ascii=False),flush=True)
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--endpoint',default='http://127.0.0.1:18520');p.add_argument('--model',required=True)
    p.add_argument('--out',type=Path,required=True)
    asyncio.run(main(p.parse_args()))
