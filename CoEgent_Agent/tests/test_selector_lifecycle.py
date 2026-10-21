"""A selector cannot extend cancelled work or bypass either remote gate."""
import asyncio
import json

import pytest

from coegent import CoEgent, PlanGenerator, PlanStore
from coegent.planner import ScriptedPlanner
from coegent.planner_selector import PlannerSelector
from coegent.planning_dispatch import dispatch_plan
from coegent.scenarios import initial_state, plan, skill
from coegent.skills import MockSkills
from coegent_contracts import RuntimeConfig
from test_planner_selector import DecisionModel, context


def decision(route='generate', plan_id=None):
    return {'decision':route,'plan_id':plan_id,'reason':'fixture','question':None}


def test_cancellation_ignoring_selector_cannot_start_new_generation(tmp_path):
    async def run():
        started, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        class LateJudge(DecisionModel):
            async def complete(self,*args,**kwargs):
                started.set()
                try: await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    await release.wait()
                return json.dumps(decision())
        fallback=ScriptedPlanner([plan(skill('get_pose'))]); reports=[]
        selector=PlannerSelector(PlanStore(tmp_path/'plans.sqlite3'),LateJudge(None))
        task=asyncio.create_task(dispatch_plan(fallback,context(),selector=selector,on_selection=reports.append))
        await asyncio.wait_for(started.wait(),1)
        task.cancel();await asyncio.wait_for(cancelled.wait(),1)
        release.set()
        try: await task
        except asyncio.CancelledError: pass
        assert fallback.contexts==[] and reports==[]
    asyncio.run(run())


@pytest.mark.parametrize('judge_remote,generator_remote,blocked_phase',[(True,False,1),(False,True,2)])
def test_remote_gate_precedes_selector_and_generator(tmp_path,judge_remote,generator_remote,blocked_phase):
    async def run():
        model=DecisionModel(decision());model.requires_remote=judge_remote
        fallback=ScriptedPlanner([plan(skill('get_pose'))]);fallback.requires_remote=generator_remote
        gates=[]
        async def gate(remote):
            gates.append(remote)
            if remote: raise RuntimeError('offline')
        selector=PlannerSelector(PlanStore(tmp_path/'plans.sqlite3'),model)
        with pytest.raises(RuntimeError,match='offline'):
            await dispatch_plan(fallback,context(),selector=selector,before_call=gate)
        assert len(gates)==blocked_phase and gates[-1] is True
        assert len(model.requests)==blocked_phase-1 and fallback.contexts==[]
    asyncio.run(run())


def test_agent_blocks_remote_selector_even_when_reuse_would_be_local(tmp_path):
    class Offline:
        async def connected(self): return False
    store=PlanStore(tmp_path/'plans.sqlite3')
    saved=store.add(plan(skill('get_pose')),command='위치 확인')
    judge=DecisionModel(decision('reuse',saved['id']));judge.requires_remote=True
    backend=MockSkills();fallback=ScriptedPlanner([])
    agent=CoEgent(fallback,backend,connection=Offline(),planner_selector=PlannerSelector(store,judge),
                 config=RuntimeConfig(remote_required=True,offline_policy='hold'),plan_store=store)
    report=asyncio.run(agent.run('위치 확인',initial_state()))
    assert report['status']=='HELD' and report['reason']=='connection_lost:hold'
    assert judge.requests==[] and fallback.contexts==[]
    assert all(call['name']=='stop' for call in backend.calls)


def test_selector_timeout_does_not_execute_or_generate(tmp_path):
    async def run():
        cancelled=asyncio.Event()
        class Slow(DecisionModel):
            async def complete(self,*args,**kwargs):
                try: await asyncio.Event().wait()
                finally: cancelled.set()
        fallback=ScriptedPlanner([])
        selector=PlannerSelector(PlanStore(tmp_path/'plans.sqlite3'),Slow(None))
        generator=PlanGenerator(fallback,planner_selector=selector,config=RuntimeConfig(planner_timeout_s=.03))
        report=await generator.generate('위치 확인',initial_state())
        await asyncio.wait_for(cancelled.wait(),1)
        assert report['status']=='PLANNER_ERROR' and report['reason']=='planner_timeout'
        assert report['plan'] is None and fallback.contexts==[]
    asyncio.run(run())


def test_invalid_selector_id_does_not_execute_candidate_or_generate(tmp_path):
    store=PlanStore(tmp_path/'plans.sqlite3');store.add(plan(skill('get_pose')),command='위치 확인')
    backend=MockSkills();fallback=ScriptedPlanner([])
    agent=CoEgent(fallback,backend,planner_selector=PlannerSelector(store,DecisionModel(decision('reuse',999))),plan_store=store)
    report=asyncio.run(agent.run('위치 확인',initial_state()))
    assert report['status']=='PLANNER_ERROR' and 'not_in_shown_candidates' in report['reason']
    assert fallback.contexts==[] and all(call['name']=='stop' for call in backend.calls)


def test_agent_keeps_cancelled_selector_owned_until_inference_cleanup(tmp_path):
    async def run():
        started, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        class Late(DecisionModel):
            async def complete(self,*args,**kwargs):
                self.requests.append(None);started.set()
                try: await release.wait()
                except asyncio.CancelledError:
                    cancelled.set();await release.wait()
                return json.dumps(decision())
        judge=Late(None);store=PlanStore(tmp_path/'plans.sqlite3');store.initialize();backend=MockSkills()
        fallback=ScriptedPlanner([plan(skill('get_pose'))])
        agent=CoEgent(fallback,backend,planner_selector=PlannerSelector(store,judge),plan_store=store,
                     config=RuntimeConfig(planner_timeout_s=.2))
        try:
            work=asyncio.create_task(agent.run('위치 확인',initial_state()))
            await asyncio.wait_for(started.wait(),1)
            first=await asyncio.wait_for(work,1)
            await asyncio.wait_for(cancelled.wait(),1)
            assert first['status']=='PLANNER_ERROR'
            assert agent.admission.pending
            second=await agent.run('위치 확인',initial_state())
            assert second['status']=='REJECTED' and len(judge.requests)==1
        finally:
            release.set()
            await asyncio.gather(*agent.admission.pending,return_exceptions=True)
        assert fallback.contexts==[] and not agent.admission.pending
    asyncio.run(run())
