import json
from pathlib import Path
from copy import deepcopy
import pytest
from grading import score

CASES=json.loads(Path(__file__).with_name('planner_cases.json').read_text())['cases']

@pytest.mark.parametrize('case',CASES,ids=lambda x:x['id'])
def test_reference_satisfies_its_semantic_rubric(case):
    assert score(case['reference'],case)['strict']

def test_wrong_target_order_and_extra_action_rejected():
    case=CASES[7];value=deepcopy(case['reference'])
    value['sequence'].reverse();assert not score(value,case)['relaxed']
    case=CASES[2];value=deepcopy(case['reference']);value['sequence'][0]['args']['target']='B'
    assert not score(value,case)['strict']
    case=CASES[15];value=deepcopy(case['reference']);value['sequence'].append({'type':'skill','name':'get_battery','args':{}})
    assert not score(value,case)['relaxed']

def test_only_unrequested_logging_gets_relaxed_credit():
    case=CASES[0];value=deepcopy(case['reference'])
    value['sequence'].append({'type':'skill','name':'log_event','args':{'message':'note'}})
    assert score(value,case)=={'strict':False,'relaxed':True,'operational':True,'reason':'trace_mismatch'}

def test_incorrect_condition_and_missing_requested_log_rejected():
    case=CASES[18];value=deepcopy(case['reference']);value['sequence'][1]['condition']['value']='closed'
    assert not score(value,case)['relaxed']
    value=deepcopy(case['reference']);value['sequence'][1]['else']=[{'type':'skill','name':'get_pose','args':{}}]
    assert not score(value,case)['relaxed']

@pytest.mark.parametrize('outcome',['abort','replan_required'])
def test_terminal_outcome_matters(outcome):
    case=CASES[0];value=deepcopy(case['reference']);value['final_outcome']=outcome
    assert not score(value,case)['strict']

def test_runtime_alias_and_strict_boolean_type():
    case=CASES[18];value=deepcopy(case['reference'])
    value['sequence'][1]['condition']['source']='results.inspection.valve_state'
    assert score(value,case)['strict']
    case=CASES[22];value=deepcopy(case['reference']);value['sequence'][1]['condition']['value']=1
    assert not score(value,case)['strict']

def test_invented_result_alias_rejected():
    case=CASES[20];value=deepcopy(case['reference'])
    value['sequence'][1]['condition']['source']='get_battery.result.battery_percent'
    assert not score(value,case)['strict']
