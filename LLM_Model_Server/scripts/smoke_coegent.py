"""Validate the live model API and generate a CoEgent plan without executing skills."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
from urllib.error import HTTPError

SERVER_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SERVER_ROOT.parent
sys.path.insert(0,str(PROJECT_ROOT/'CoEgent_Agent'))
sys.path.insert(0,str(PROJECT_ROOT/'Executor_Adapters'))
sys.path.insert(0,str(SERVER_ROOT))

from local_llm_server.cli import add_selection_arguments, selected_config
from local_llm_server.lifecycle import status
from local_llm_server.smoke import schema_probe, chat_completion


async def generate(config,command,max_tokens):
    from coegent import PlanGenerator
    from coegent.planner import ModelServerPlanner, messages, response_format
    from coegent.scenarios import initial_state
    from coegent_contracts import RuntimeConfig

    class CheckedModelServerPlanner(ModelServerPlanner):
        # Keep the application's prompt/schema, add finish_reason verification here.
        async def plan(self,context):
            payload = {'model':self.model,'messages':messages(context),'temperature':0,
                       'max_tokens':self.max_tokens,'stream':False,
                       'response_format':response_format(context['schema'])}
            data = await asyncio.to_thread(chat_completion,self.endpoint,payload,self.timeout_s)
            return data['choices'][0]['message']['content']

    planner = CheckedModelServerPlanner(config.endpoint,config.model_name,
                                 timeout_s=config.request_timeout_s,max_tokens=max_tokens,
                                 constrained=True)
    generator = PlanGenerator(planner,config=RuntimeConfig(
        max_repairs=0,planner_timeout_s=config.request_timeout_s))
    return await generator.generate(command,initial_state())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_selection_arguments(parser)
    parser.add_argument('--command',default='현재 배터리를 확인해.')
    parser.add_argument('--max-tokens',type=int,default=1024)
    parser.add_argument('--out',type=Path,default=SERVER_ROOT/'results/coegent_smoke.json')
    args = parser.parse_args()
    output = {'scope':'schema probe + plan generation only; no skills executed',
              'command':args.command,'passed':False}
    try:
        config = selected_config(args)
        info = status(config)
        if info['status']!='ready':
            raise RuntimeError('managed server is not ready: '+info['status'])
        output['server']=info
        output['schema_probe']=schema_probe(config.endpoint,config.model_name,config.request_timeout_s)
        output['planning']=asyncio.run(generate(config,args.command,args.max_tokens))
        report = output['planning']
        output['passed']=(report['status']=='PLAN_READY' and report['execution_status']=='NOT_EXECUTED'
                          and report['plan'] is not None
                          and not any(e['kind']=='skill_start' for e in report['trace']))
        if not output['passed']:
            output['error']='plan generation/validation did not succeed'
    except (ValueError,RuntimeError,OSError) as exc:
        output['error']=str(exc)
        if isinstance(exc,HTTPError):
            output['http_error_body']=exc.read(8192).decode(errors='replace')
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(output,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    print(json.dumps({'passed':output['passed'],'out':str(args.out),
                      'error':output.get('error')},ensure_ascii=False))
    return 0 if output['passed'] else 2


if __name__=='__main__': raise SystemExit(main())
