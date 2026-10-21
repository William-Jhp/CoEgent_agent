"""Foreground diagnostics and LLM-free execution of a provided A2 plan."""
import argparse
import asyncio
from dataclasses import asdict,replace
import json
from pathlib import Path
from time import time
from uuid import uuid4

from coegent.admission import MissionRequest
from coegent.cli import write_result
from coegent.executor_adapters import default_registry
from coegent.tool_profiles import A2ToolProfile
from coegent.types import State
from coegent.validation import strict_json
from .integration import execute_plan
from .options import Options,PACKAGE_ROOT
from .transport import DockerTransport

async def run(args):
    value=strict_json(Path(args.options).read_text()) if args.options else {}
    options=Options.from_dict(value)
    req=MissionRequest(args.mission_id or str(uuid4()),args.revision,time()+options.mission_timeout_s+options.startup_timeout_s,
                       'Read-only A2 diagnostics' if args.mode=='diagnose' else 'Execute provided A2 plan')
    if args.mode in ('diagnose','reconcile'):
        transport=DockerTransport(options,lambda e:print(json.dumps(e,ensure_ascii=False),flush=True))
        packet={'kind':args.mode,'request':{k:v for k,v in asdict(req).items() if k not in ('plan','goals')},
                'options':asdict(replace(options,source_root='/rig',runtime_dir='/runtime',scene_dir='/scene' if options.scene_dir else None))}
        if args.mode=='reconcile':
            packet['task_id']=args.task_id
            if args.evidence_report:packet['legacy_report']=strict_json(Path(args.evidence_report).read_text())
        report=await transport.run(packet)
    else:
        raw=strict_json(Path(args.plan_file).read_text())
        plan=raw.get('plan') if isinstance(raw,dict) and 'plan' in raw else raw
        report=await execute_plan(plan,req,State(),A2ToolProfile(options.source_root,scene_dir=options.scene_dir),options,default_registry())
    write_result(args.out,report)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    print('Saved: '+args.out)
    return 0 if report['status']=='SUCCESS' else 2

def main():
    p=argparse.ArgumentParser(description=__doc__)
    subs=p.add_subparsers(dest='mode',required=True)
    for name in ('diagnose','execute','reconcile'):
        s=subs.add_parser(name)
        s.add_argument('--options',help='deployment JSON; default is read-only')
        s.add_argument('--out',default=str(PACKAGE_ROOT/'results'/f'{name}.json'))
        s.add_argument('--mission-id',required=name=='reconcile');s.add_argument('--revision',type=int,default=1)
        if name=='reconcile':
            s.add_argument('--task-id',required=True)
            s.add_argument('--evidence-report',help='original operator-held host report for pre-ownership journal records only')
        if name=='execute':s.add_argument('--plan-file',required=True)
    args=p.parse_args()
    try:return asyncio.run(run(args))
    except (ValueError,OSError) as exc:p.error(str(exc))

if __name__=='__main__':raise SystemExit(main())
