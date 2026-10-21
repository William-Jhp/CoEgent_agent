"""The decoding contract must prevent the invalid combinations seen live."""
import asyncio
import json

import pytest
from jsonschema import Draft202012Validator

from coegent import PlanStore, PlannerSelector
from coegent.scenarios import plan,skill
from test_planner_selector import DecisionModel,context


def emitted_schema(tmp_path,has_candidate=True):
    store=PlanStore(tmp_path/'plans.sqlite3')
    if has_candidate: store.add(plan(skill('navigate_to',target='A')),command='A로 이동해')
    model=DecisionModel({'reason':'new','decision':'generate','plan_id':None,'question':None})
    asyncio.run(PlannerSelector(store,model).select(context()))
    return model.requests[0][1]['schema']


@pytest.mark.parametrize('invalid',[
    {'decision':'generate','plan_id':None,'reason':'new','question':'목적지는?'},
    {'decision':'clarify','plan_id':None,'reason':'missing','question':None},
    {'decision':'reuse','plan_id':None,'reason':'match','question':None},
    {'decision':'reuse','plan_id':999,'reason':'match','question':None},
])
def test_wire_schema_rejects_cross_field_or_unshown_id(tmp_path,invalid):
    schema=emitted_schema(tmp_path)
    assert not Draft202012Validator(schema).is_valid(invalid)


def test_empty_candidates_do_not_offer_reuse_in_decoding(tmp_path):
    schema=emitted_schema(tmp_path,False)
    assert not Draft202012Validator(schema).is_valid({'decision':'reuse','plan_id':1,'reason':'match','question':None})
    assert Draft202012Validator(schema).is_valid({'decision':'clarify','plan_id':None,'reason':'missing','question':'어디로 갈까요?'})


def test_valid_all_routes_have_unchanged_public_shape(tmp_path):
    schema=emitted_schema(tmp_path)
    rows=[{'reason':'match','decision':'reuse','plan_id':1,'question':None},
          {'reason':'new','decision':'generate','plan_id':None,'question':None},
          {'reason':'missing','decision':'clarify','plan_id':None,'question':'목적지는 어디인가요?'}]
    assert all(Draft202012Validator(schema).is_valid(row) for row in rows)
