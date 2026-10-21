"""Paired native string vs HF-ID diagnosis. Separate from production chat evaluation."""
import json,sys,time
from pathlib import Path
from urllib.request import Request,urlopen
from transformers import AutoTokenizer
from grading import score
from coegent.validation import PlanValidator
from coegent.registry import SkillRegistry
ROOT=Path(__file__).resolve().parents[3];OUT=ROOT/'CoEgent_Agent/results/planner_sft_roles_20261016'

def post(route,payload):
    with urlopen(Request('http://127.0.0.1:18520'+route,json.dumps(payload).encode(),{'Content-Type':'application/json'}),timeout=90) as response:return json.load(response)

def main():
    tok=AutoTokenizer.from_pretrained(ROOT/'models/robot-agent-sft-merged/gemma4-e2b-robot-sft-v0.4-merged',local_files_only=True)
    saved=json.loads((OUT/'main/results.json').read_text())
    refs={r['id']:r for r in json.loads(Path(__file__).with_name('planner_cases.json').read_text())['cases']}
    output={'scope':'exploratory paired native /completion diagnostic, not CoEgent production chat or full pipeline accuracy','rows':[]}
    for phase,cid in [('planner','p19'),('planner','p21'),('selector','d04'),('selector','h10')]:
        original=next(r for r in saved['rows'] if r['phase']==phase and r['case_id']==cid and r['iteration']==0)
        call=original['calls'][0]['request'];prompt=tok.apply_chat_template(call['messages'],tokenize=False,add_generation_prompt=True,enable_thinking=False)
        ids=tok(prompt,add_special_tokens=False)['input_ids']
        strings=post('/tokenize',{'content':prompt,'add_special':False,'parse_special':True})['tokens']
        for mode,value in [('string',prompt),('hf_ids',ids)]:
            payload={'prompt':value,'temperature':0,'n_predict':call['max_tokens'],'cache_prompt':False,'json_schema':call['response_format']['json_schema']['schema']}
            row={'phase':phase,'case_id':cid,'input_mode':mode,'hf_tokens':len(ids),'string_tokens':len(strings),'token_parity':ids==strings,'payload':payload}
            started=time.monotonic()
            try:
                data=post('/completion',payload);row['response']=data
                row['finished']=data.get('stop') is True and data.get('stop_type')=='eos' and not data.get('truncated')
                content=json.loads(data['content']);row['parsed']=content
                if phase=='planner':
                    plan,_=PlanValidator(SkillRegistry()).prepare(content);row['grading']=score(plan,refs[cid]);row['passed']=row['finished'] and row['grading']['strict']
                else:row['passed']=row['finished'] and content.get('decision')==original['expected'] and (content.get('decision')!='reuse' or content.get('plan_id')==original['expected_plan_id'])
            except Exception as exc:row.update(passed=False,error=repr(exc))
            row['elapsed_s']=time.monotonic()-started;output['rows'].append(row)
            (OUT/'native_token_diagnostic.json').write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n')
            print(json.dumps({k:v for k,v in row.items() if k not in ('payload','response','parsed')},ensure_ascii=False),flush=True)
if __name__=='__main__':main()
