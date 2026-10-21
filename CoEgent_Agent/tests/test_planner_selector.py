"""LLM selection is a bounded choice, never a source of executable plan JSON."""
import asyncio
from copy import deepcopy
import importlib
import json

import pytest

from coegent import PlanStore
from coegent.scenarios import initial_state, plan, skill


class DecisionModel:
    requires_remote=False
    supports_json_schema=True
    def __init__(self,decision): self.decision=decision;self.requests=[]
    async def complete(self,messages,**kwargs):
        self.requests.append((deepcopy(messages),kwargs))
        return self.decision if isinstance(self.decision,str) else json.dumps(self.decision)


def selector(store,model,**kwargs):
    assert importlib.util.find_spec('coegent.planner_selector') is not None, 'missing PlannerSelector'
    from coegent.planner_selector import PlannerSelector
    return PlannerSelector(store,model,**kwargs)


def context(command='A로 이동해'):
    return {'command':command,'state':initial_state().snapshot(),'skills':{'navigate_to':{},'return_home':{}},
            'completed_steps':[],'feedback':None}


def test_selector_reuses_only_a_shown_full_plan_snapshot(tmp_path):
    store=PlanStore(tmp_path/'plans.sqlite3');value=plan(skill('navigate_to',target='A'))
    saved=store.add(value,command='A로 이동해')
    model=DecisionModel({'decision':'reuse','plan_id':saved['id'],'reason':'same target and actions','question':None})
    chosen=asyncio.run(selector(store,model).select(context()))
    assert chosen.decision=='reuse' and chosen.plan==value and chosen.plan_id==saved['id']
    payload=json.loads(model.requests[0][0][-1]['content'])
    assert payload['candidates'][0]['plan']==value
    assert payload['command']=='A로 이동해'
    assert model.requests[0][1]['require_complete'] is True


def test_no_candidates_still_allows_llm_clarification(tmp_path):
    store=PlanStore(tmp_path/'plans.sqlite3')
    model=DecisionModel({'decision':'clarify','plan_id':None,'reason':'missing destination','question':'어느 장소로 이동할까요?'})
    chosen=asyncio.run(selector(store,model).select(context('거기로 가줘')))
    assert chosen.decision=='clarify' and chosen.question=='어느 장소로 이동할까요?'
    assert json.loads(model.requests[0][0][-1]['content'])['candidates']==[]


@pytest.mark.parametrize('decision',[
    {'decision':'reuse','plan_id':999,'reason':'match','question':None},
    {'decision':'generate','plan_id':1,'reason':'new','question':None},
    {'decision':'clarify','plan_id':None,'reason':'missing','question':None},
    {'decision':'execute','plan_id':None,'reason':'bad','question':None},
    '{"decision":"generate","decision":"reuse","plan_id":1,"reason":"bad","question":null}',
    {'decision':'generate','plan_id':None,'reason':'new','question':None,'plan':{}},
])
def test_invalid_or_hallucinated_decision_is_rejected(tmp_path,decision):
    store=PlanStore(tmp_path/'plans.sqlite3');store.add(plan(skill('get_pose')))
    with pytest.raises(ValueError):
        asyncio.run(selector(store,DecisionModel(decision)).select(context()))


def test_repair_or_replan_context_does_not_reuse_complete_saved_plan(tmp_path):
    store=PlanStore(tmp_path/'plans.sqlite3');model=DecisionModel('must not call')
    value=context();value['completed_steps']=[{'name':'navigate_to','args':{'target':'A'}}]
    chosen=asyncio.run(selector(store,model).select(value))
    assert chosen.decision=='generate' and model.requests==[]


def test_oversized_candidate_is_omitted_whole_not_cut_into_a_different_plan(tmp_path):
    store=PlanStore(tmp_path/'plans.sqlite3')
    value=plan(*(skill('log_event',message='x'*200) for _ in range(30)))
    store.add(value,command='A로 이동해')
    model=DecisionModel({'decision':'generate','plan_id':None,'reason':'no complete candidate','question':None})
    chosen=asyncio.run(selector(store,model,max_prompt_bytes=4000).select(context()))
    assert chosen.decision=='generate'
    assert json.loads(model.requests[0][0][-1]['content'])['candidates']==[]
