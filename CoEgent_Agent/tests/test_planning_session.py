import asyncio
import json
import pytest
from coegent import PlanGenerator
from coegent.scenarios import initial_state,plan,skill
from coegent.planner import ScriptedPlanner
from coegent.planning_dispatch import ClarificationRequired


class Asking:
    calls=0
    async def plan(self,context):
        self.calls+=1
        if 'A 지점' not in context['command']:raise ClarificationRequired('어디로 갈까요?')
        return plan(skill('navigate_to',target='A'))


def test_question_survives_new_instance_and_resumes_without_executor(tmp_path):
    from coegent.planning_session import PlanningSession
    async def exercise():
        model=Asking()
        first=PlanningSession(PlanGenerator(model),tmp_path/'workflow.sqlite3')
        paused=await first.start('thread-1','그쪽으로 가',initial_state())
        assert paused['status']=='NEEDS_CLARIFICATION' and paused['question']=='어디로 갈까요?'
        assert model.calls==1
        # A new object and model; only persisted state binds the answer to the request.
        resumed=PlanningSession(PlanGenerator(Asking()),tmp_path/'workflow.sqlite3')
        result=await resumed.resume('thread-1','A 지점')
        assert result['status']=='PLAN_READY' and result['report']['plan']['sequence'][0]['args']=={'target':'A'}
        assert result['report']['execution_status']=='NOT_EXECUTED'
        json.dumps(result,allow_nan=False)
        with pytest.raises(ValueError,match='already exists'):await resumed.start('thread-1','another',initial_state())
        with pytest.raises(ValueError,match='not awaiting'):await resumed.resume('thread-1','again')
    asyncio.run(exercise())


def test_resume_rejects_wrong_contract_and_empty_answer(tmp_path):
    from coegent.planning_session import PlanningSession
    from coegent_contracts import RuntimeConfig
    async def exercise():
        path=tmp_path/'workflow.sqlite3'
        await PlanningSession(PlanGenerator(Asking()),path).start('t','go',initial_state())
        session=PlanningSession(PlanGenerator(Asking(),config=RuntimeConfig(max_steps=2)),path)
        with pytest.raises(ValueError,match='contract'):await session.resume('t','A 지점')
        normal=PlanningSession(PlanGenerator(Asking()),path)
        with pytest.raises(ValueError,match='answer'):await normal.resume('t',' ')
        assert (await normal.get_state('t'))['status']=='NEEDS_CLARIFICATION'
    asyncio.run(exercise())


def test_same_thread_cannot_be_invoked_concurrently(tmp_path):
    from coegent.planning_session import PlanningSession
    async def exercise():
        entered=asyncio.Event();release=asyncio.Event()
        class Slow:
            async def plan(self,c):entered.set();await release.wait();return plan(skill('get_pose'))
        path=tmp_path/'w.sqlite3';session=PlanningSession(PlanGenerator(Slow()),path)
        task=asyncio.create_task(session.start('t','pose',initial_state()));await entered.wait()
        try:
            with pytest.raises(RuntimeError,match='busy'):
                await PlanningSession(PlanGenerator(ScriptedPlanner([])),path).start('t','other',initial_state())
        finally:release.set();await task
    asyncio.run(exercise())


def test_checkpoint_cannot_write_into_plan_catalog(tmp_path):
    from coegent import PlanStore
    from coegent.planning_session import PlanningSession
    path=tmp_path/'plans.sqlite3';PlanStore(path).add(plan(skill('get_pose')),command='pose')
    before=path.read_bytes()
    with pytest.raises(ValueError,match='dedicated checkpoint'):
        PlanningSession(PlanGenerator(Asking()),path)
    assert path.read_bytes()==before
