"""Posthoc contract-corrected scoring. Never edits saved raw results or calls a model."""
from copy import deepcopy
import hashlib,json,sys
from pathlib import Path
from run import summarize,stats
from grading import score
HERE=Path(__file__).resolve().parent

def main(path):
    data=json.loads(path.read_text());rows=deepcopy(data['rows'])
    cases={r['id']:r for r in json.loads((HERE/'planner_cases.json').read_text())['cases']}
    selectors={r['id']:r for r in json.loads((HERE.parent/'planner_selector/cases.json').read_text())['cases']}
    for row in rows:
        if row['phase']=='selector':continue
        if row['phase']=='planner':case=cases[row['case_id']]
        else:
            raw=selectors[row['case_id']]
            if raw['expected']=='clarify':continue
            reference=next(r['plan'] for r in raw['catalog'] if r['key']==raw['expected_plan']) if raw['expected']=='reuse' else cases['p02']['reference']
            from coegent.scenarios import initial_state
            case={'state':initial_state().snapshot(),'bindings':[{}],'reference':reference}
        plan=row['report'].get('plan')
        grade=score(plan,case) if plan else {'strict':False,'relaxed':False,'operational':False,'reason':'no_validated_plan'}
        row['grading']=grade
        gate=row['finished'] and row['report']['status']=='PLAN_READY' and (row.get('route_correct',True))
        row['passed']=bool(gate and grade['strict']);row['relaxed_passed']=bool(gate and grade['relaxed']);row['operational_passed']=bool(gate and grade['operational'])
    summary=summarize(rows)
    for phase in summary:
        selected=[r for r in rows if r['phase']==phase]
        summary[phase]['unique_cases']=len({r['case_id'] for r in selected})
        if phase=='planner':
            summary[phase]['operational_passed']=sum(r.get('operational_passed',False) for r in selected)
        if phase=='selector':
            summary[phase]['confusion']={expected:{actual:sum(r['expected']==expected and r['decision']==actual for r in selected) for actual in ('reuse','generate','clarify','error')} for expected in ('reuse','generate','clarify')}
        if phase=='combined':
            summary[phase]['per_expected_route']={route:{'count':sum(r['expected']==route for r in selected),'passed':sum(r['passed'] for r in selected if r['expected']==route),'latency_all':stats([r['elapsed_s'] for r in selected if r['expected']==route])} for route in ('reuse','generate','clarify')}
    counts={k:summary[k]['count'] for k in summary};expected={'planner':24*data['repeat'],'selector':30*data['repeat'],'combined':6*data['repeat']}
    complete=counts==expected if data['mode']=='full' else len(rows)==6
    output={k:v for k,v in data.items() if k not in ('rows','summary')}
    output.update(raw_source=str(path),raw_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),rubric_version=2,
        grading_corpus_sha256=hashlib.sha256((HERE/'planner_cases.json').read_bytes()).hexdigest(),
        rubric_correction='StateContract.condition/apply_effects; no invented .result aliases; strict predicate types; terminal final_outcome checked; p21/p22 reference paths corrected. Commands/state/model inputs unchanged.',
        exploratory_metric='operational additionally permits unrequested logs and check_path for following requested move; not primary accuracy',
        complete=complete,rows=rows,summary=summary)
    target=path.with_name('results_regraded.json');target.write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n')
    print(path.parent.name,complete,json.dumps(summary,ensure_ascii=False))

if __name__=='__main__':
    for value in sys.argv[1:]:main(Path(value))
