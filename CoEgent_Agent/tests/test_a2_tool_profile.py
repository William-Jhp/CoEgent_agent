"""Source-backed API planning does not import or dispatch ROS/robot code."""
import asyncio
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest
from coegent import PlanGenerator, PlanStore
from coegent.planner import ScriptedPlanner, ModelServerPlanner
from coegent.scenarios import plan, skill
from coegent.types import State
from coegent_contracts import RuntimeConfig

SOURCE=Path('/home/gritandgrind/dev/kts/a2-llm-isaac')

def profile():
    assert importlib.util.find_spec('coegent.tool_profiles'), 'Missing source-backed tool profile'
    from coegent.tool_profiles import A2ToolProfile
    return A2ToolProfile(SOURCE)

def generate(body):
    p=profile();client=ScriptedPlanner([body])
    result=asyncio.run(PlanGenerator(client,tool_profile=p,config=RuntimeConfig(max_repairs=0)).generate('test',State()))
    return p,client,result

def test_catalog_matches_real_source_signatures_without_imports():
    before=set(sys.modules);p=profile();catalog=p.catalog()
    names={r['name'] for r in catalog['tools']}
    assert {'RobotBackend.go_to','RobotBackend.localize','WorldState.describe','GridNavigator.plan'}<=names
    assert 'navigate_to' not in names
    assert all(r['source']['signature_verified'] for r in catalog['tools'])
    assert not any(n.startswith(('rclpy','a2sim','payload_ingest_node')) for n in set(sys.modules)-before)
    assert catalog['execution_binding']=='optional_executor:a2-isaac'

def test_profile_planning_has_actual_api_calls_and_no_mock_preflight():
    body=plan(skill('RobotBackend.go_to',x=-13.84,y=4.88))
    p,client,out=generate(body)
    assert out['status']=='PLAN_READY' and out['execution_status']=='NOT_EXECUTED'
    assert out['execution_ready'] is False and out['validation_scope']=='api_contract_only'
    assert out['api_calls'][0]['name']=='RobotBackend.go_to'
    assert out['api_calls'][0]['args']=={'x':-13.84,'y':4.88}
    assert client.contexts[0]['state']['robot']=={}
    assert all(r['status']=='deferred' for r in out['preflight'])
    assert set(client.contexts[0]['skills'])==set(p.registry.skills)

@pytest.mark.parametrize('node',[
 skill('navigate_to',target='A'),skill('RobotBackend.go_to',x='reception',y=0),
 skill('RobotBackend.go_to',x=0),skill('RobotBackend.stop_localization',grace_s=-1),
 skill('RobotBackend.observe',yaw_offset=1),
])
def test_unknown_or_incompatible_api_arguments_rejected(node):
    assert generate(plan(node))[2]['status']=='INVALID_PLAN'

def test_camera_result_binding_checked_in_dataflow_order():
    capture={**skill('WorldState.capture_views'),'save_as':'camera'}
    describe=skill('WorldState.describe',views={'$result':'camera','index':0})
    assert generate(plan(capture,describe))[2]['status']=='PLAN_READY'
    assert generate(plan(describe,capture))[2]['status']=='INVALID_PLAN'
    bad=skill('WorldState.describe',views={'$result':'camera','index':3})
    assert generate(plan(capture,bad))[2]['status']=='INVALID_PLAN'

def test_model_prompt_and_schema_both_use_profile_catalog():
    p,client,out=generate(plan(skill('RobotBackend.check_battery')))
    from coegent.planner import messages
    prompt=messages(client.contexts[0]);wire=json.dumps(prompt,ensure_ascii=False)
    assert 'RobotBackend.check_battery' in wire and 'min_battery' not in wire
    assert 'WorldState.capture_views' in wire and 'API_CONTEXT' in wire
    assert 'get_battery args=' not in wire
    assert 'reception' in wire # loaded from source annotations, not fabricated fixture

def test_profile_store_can_reuse_actual_api_plan_and_reject_mock_store(tmp_path):
    p=profile();store=PlanStore(tmp_path/'a2.sqlite3',registry=p.registry)
    body=plan(skill('RobotBackend.refresh_pose'));saved=store.add(body,command='현재 위치 확인')
    assert store.get(saved['id'])['plan']==body
    with pytest.raises(ValueError): PlanStore(store.path).get(saved['id'])

def test_cli_tools_and_plan_profile_output(tmp_path):
    p=profile();file=tmp_path/'input.json';file.write_text(json.dumps(plan(skill('RobotBackend.check_battery'))))
    output=tmp_path/'report.json'
    command=[sys.executable,'-m','coegent']
    listing=subprocess.run(command+['tools','--tool-profile','a2-isaac'],capture_output=True,text=True)
    assert listing.returncode==0,listing.stderr
    assert json.loads(listing.stdout)['profile']=='a2-isaac'
    r=subprocess.run(command+['plan','--tool-profile','a2-isaac','--planner','file','--plan-file',str(file),'--command','배터리 확인','--out',str(output)],capture_output=True,text=True)
    assert r.returncode==0,r.stderr
    out=json.loads(output.read_text());assert out['run_metadata']['state_source']=='unknown_snapshot'
    assert out['tool_profile']['name']=='a2-isaac' and out['execution_ready'] is False
    assert 'RobotBackend.check_battery' in r.stdout

def test_source_signature_drift_and_missing_source_fail_closed(tmp_path):
    from coegent.tool_profiles import A2ToolProfile
    with pytest.raises((OSError,ValueError)):A2ToolProfile(tmp_path)
    tree=tmp_path/'planner/a2sim/robot';tree.mkdir(parents=True)
    (tree/'backend.py').write_text('class RobotBackend:\n    def go_to(self, destination): pass\n')
    with pytest.raises(ValueError,match='signature drift'):A2ToolProfile(tmp_path)

def test_profile_catalog_cannot_mix_with_mock_catalog(tmp_path):
    p=profile();path=tmp_path/'legacy.sqlite3'
    legacy=PlanStore(path);legacy.add(plan(skill('get_battery')))
    with pytest.raises(ValueError,match='contract|profile'):
        PlanStore(path,registry=p.registry).add(plan(skill('RobotBackend.check_battery')))
    assert len(legacy.list())==1
    api=PlanStore(tmp_path/'api.sqlite3',registry=p.registry);api.initialize()
    with pytest.raises(ValueError,match='contract|profile'):
        PlanStore(api.path).add(plan(skill('get_battery')))

@pytest.mark.parametrize('route',['generate','reuse','clarify'])
def test_profile_auto_cli_selection_preserves_catalog_and_context(tmp_path,route):
    from test_selector_cli import api
    p=profile();db=tmp_path/'profile.sqlite3';body=plan(skill('RobotBackend.check_battery'))
    saved=PlanStore(db,registry=p.registry).add(body,command='배터리 확인')
    decision={'decision':route,'plan_id':saved['id'] if route=='reuse' else None,
              'reason':'fixture','question':'어디로 갈까요?' if route=='clarify' else None}
    output=tmp_path/'auto.json'
    with api(decision,body) as (endpoint,calls):
        r=subprocess.run([sys.executable,'-m','coegent','plan','--tool-profile','a2-isaac','--planner','auto',
            '--generation-planner','http','--endpoint',endpoint,'--model','fixture','--plan-db',str(db),
            '--command','배터리 확인','--out',str(output)],capture_output=True,text=True)
    assert r.returncode==(2 if route=='clarify' else 0),r.stderr
    out=json.loads(output.read_text());assert out['execution_ready'] is False
    selector_input=json.loads(calls[0]['messages'][1]['content'])
    assert selector_input['tool_profile']=='a2-isaac' and selector_input['tool_context']['places']
    assert len(calls)==(2 if route=='generate' else 1)
    assert out['status']==('NEEDS_CLARIFICATION' if route=='clarify' else 'PLAN_READY')
    assert len(PlanStore(db,registry=p.registry).list())==1

def test_cli_does_not_run_a2_profile_or_instantiate_a_model(tmp_path):
    r=subprocess.run([sys.executable,'-m','coegent','run','--tool-profile','a2-isaac','--planner','inprocess',
        '--command','이동해','--out',str(tmp_path/'out.json')],capture_output=True,text=True)
    assert r.returncode==2 and 'plan-only' in r.stderr
    assert not (tmp_path/'out.json').exists()

def test_prompt_replan_example_validates_without_constrained_decoding():
    import re
    from coegent.planner import messages
    p,client,out=generate(plan(skill('RobotBackend.check_battery')))
    text=messages(client.contexts[0])[0]['content']
    example=re.search(r'Replan node JSON: (\{[^\n]+\})\n',text)
    assert example, 'API prompt must provide a complete schema-valid replan node'
    node=json.loads(example.group(1))
    body=plan(node)
    body['final_outcome']='replan_required'
    assert p.validator.prepare(body)[0]['sequence'][0]['constraints']
