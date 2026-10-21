"""Selection routes through existing validators and adapters, not around them."""
import asyncio
import json

from coegent import CoEgent, PlanGenerator, PlanStore
from coegent.planner import ScriptedPlanner
from coegent.planner_selector import PlannerSelector
from coegent.scenarios import initial_state, plan, skill
from coegent.skills import MockSkills


class Judge:
    requires_remote=False
    supports_json_schema=True
    def __init__(self,decision): self.decision=decision
    async def complete(self,*args,**kwargs): return json.dumps(self.decision)


def selection(store,decision,plan_id=None):
    return PlannerSelector(store,Judge({'decision':decision,'plan_id':plan_id,
        'reason':'fixture decision','question':'어느 장소인가요?' if decision=='clarify' else None}))


def test_plan_generator_reuses_db_plan_without_generation(tmp_path):
    store=PlanStore(tmp_path/'plans.sqlite3');value=plan(skill('navigate_to',target='A'))
    saved=store.add(value,command='A로 이동해');fallback=ScriptedPlanner([])
    result=asyncio.run(PlanGenerator(fallback,planner_selector=selection(store,'reuse',saved['id'])).generate('A로 이동해',initial_state()))
    assert result['status']=='PLAN_READY' and result['plan']==value
    assert fallback.contexts==[]
    assert result['planner_selections'][0]['planner']=='ScriptedPlanner'


def test_generate_route_invokes_configured_generator(tmp_path):
    store=PlanStore(tmp_path/'plans.sqlite3');fallback=ScriptedPlanner([plan(skill('get_pose'))])
    result=asyncio.run(PlanGenerator(fallback,planner_selector=selection(store,'generate')).generate('위치 확인',initial_state()))
    assert result['status']=='PLAN_READY' and len(fallback.contexts)==1
    assert result['planner_selections'][0]['decision']=='generate'


def test_plan_generator_clarification_has_question_and_no_plan(tmp_path):
    store=PlanStore(tmp_path/'plans.sqlite3');fallback=ScriptedPlanner([])
    result=asyncio.run(PlanGenerator(fallback,planner_selector=selection(store,'clarify')).generate('거기로 가줘',initial_state()))
    assert result['status']=='NEEDS_CLARIFICATION'
    assert result['execution_status']=='NOT_EXECUTED' and result['plan'] is None
    assert result['question']=='어느 장소인가요?' and fallback.contexts==[]


def test_agent_reuse_still_passes_native_adapter_and_current_state_validation(tmp_path):
    store=PlanStore(tmp_path/'plans.sqlite3');saved=store.add(plan(skill('navigate_to',target='A')),command='A로 이동해')
    backend=MockSkills()
    agent=CoEgent(ScriptedPlanner([]),backend,planner_selector=selection(store,'reuse',saved['id']),plan_store=store)
    report=asyncio.run(agent.run('A로 이동해',initial_state()))
    assert report['status']=='SUCCESS' and report['executor']=='coegent'
    assert any(e['kind']=='executor_execute' for e in report['trace'])
    assert backend.calls==[{'name':'navigate_to','args':{'target':'A'}}]


def test_agent_clarification_executes_no_skill_or_stop_and_does_not_archive(tmp_path):
    store=PlanStore(tmp_path/'plans.sqlite3');backend=MockSkills()
    agent=CoEgent(ScriptedPlanner([]),backend,planner_selector=selection(store,'clarify'),plan_store=store)
    report=asyncio.run(agent.run('거기로 가줘',initial_state()))
    assert report['status']=='NEEDS_CLARIFICATION' and report['execution_status']=='NOT_EXECUTED'
    assert report['question']=='어느 장소인가요?' and backend.calls==[]
    assert store.list()==[]


def test_selector_reuse_cannot_bypass_authority_or_current_estop(tmp_path):
    store=PlanStore(tmp_path/'plans.sqlite3');saved=store.add(plan(skill('navigate_to',target='A')),command='A로 이동해')
    state=initial_state();state.robot['estop']=True
    backend=MockSkills()
    agent=CoEgent(ScriptedPlanner([]),backend,planner_selector=selection(store,'reuse',saved['id']),plan_store=store)
    result=asyncio.run(agent.run('A로 이동해',state))
    assert result['status']=='SAFETY_ABORT'
    assert all(call['name']=='stop' for call in backend.calls)
