import asyncio
from time import time

import pytest
from coegent import CoEgent, PlanExecutor, MissionRequest
from coegent.inspector import Inspector, InspectionConfig, InspectionSnapshot
from coegent.planner import ScriptedPlanner
from coegent.scenarios import initial_state, plan, skill
from coegent.skills import MockSkills
from coegent_contracts import RuntimeConfig
from coegent_executor_adapters import ExecutorRegistry
from coegent.executor_adapters.native import CoEgentExecutorAdapter


class Sensor:
    def __init__(self, locations):
        self.locations = iter(locations)
    async def snapshot(self, report):
        return InspectionSnapshot(report.mission_id, report.revision,
            {'robot_state':{'location':next(self.locations)}}, {'robot_state':time()})


def inspector(locations):
    return Inspector(InspectionConfig.from_dict({'checks':[{'id':'arrival','source':'robot_state',
        'path':'location','op':'eq','expected':'A'}]}), Sensor(locations))


class Planner(ScriptedPlanner):
    pass


def test_mismatching_completion_stops_then_replans_with_inspection_feedback():
    planner = Planner([plan(skill('get_pose')), plan(skill('get_pose'))])
    backend = MockSkills()
    report = asyncio.run(CoEgent(planner, backend, inspector=inspector(['B','A'])).run('go A', initial_state()))
    assert report['status'] == 'SUCCESS'
    assert report['mission_success'] is True
    assert report['replans'] == 1
    assert [c['name'] for c in backend.calls] == ['get_pose','stop','get_pose']
    assert planner.contexts[1]['feedback']['inspection']['decision'] == 'replan'
    assert planner.contexts[1]['feedback']['inspection']['checks'][0]['actual'] == 'B'
    assert planner.contexts[1]['feedback']['inspection']['checks'][0]['expected'] == 'A'
    assert report['completion_report']['verified'] is True
    assert [d['decision'] for d in report['inspections']] == ['replan','completed']


def test_request_never_invokes_hidden_planner_and_returns_unverified_result():
    report = asyncio.run(PlanExecutor(MockSkills(), inspector=inspector(['B'])).execute(
        plan(skill('get_pose')), initial_state()))
    assert report['status'] == 'REPLAN_REQUIRED'
    assert report['mission_success'] is False
    assert report['completion_report']['next_action'] == 'replan'
    assert report['planner_calls'] == 0


def test_error_ingress_cancels_and_drains_old_execution_before_replanning():
    order=[]
    class Reporting(CoEgentExecutorAdapter):
        count=0
        async def execute(self, prepared):
            self.count+=1
            if self.count==1:
                ack=self.context.report({'mission_id':self.context.request.mission_id,
                    'revision':self.context.request.revision,'report_id':'error-1','kind':'error',
                    'message':'Path blocked'})
                assert ack['accepted']
                try:
                    await asyncio.sleep(10)
                finally:
                    order.append('drained')
            order.append('execute')
            return await super().execute(prepared)
        async def cancel(self):
            order.append('stop')
            return await super().cancel()
    reg=ExecutorRegistry(); reg.register('reporting',lambda c,o: Reporting(c,o))
    agent=CoEgent(Planner([plan(skill('get_pose')),plan(skill('get_pose'))]),MockSkills(),
        executor='reporting',executor_registry=reg,inspector=inspector(['B','A']),
        config=RuntimeConfig(connection_poll_s=.001))
    report=asyncio.run(agent.run('go A',initial_state()))
    assert report['status']=='SUCCESS', report
    assert order==['drained','stop','execute']
    assert report['inspections'][0]['reason']=='executor_error_report'
    assert agent.submit_report({'mission_id':report['mission_id'],'revision':1,
        'report_id':'late','kind':'completed','message':'done'})['accepted'] is False


def test_failed_stop_never_calls_replanner():
    backend=MockSkills({'stop':[{'event':'SAFETY_ABORT','reason':'no_stop'}]})
    planner=Planner([plan(skill('get_pose')),plan(skill('get_pose'))])
    report=asyncio.run(CoEgent(planner,backend,inspector=inspector(['B'])).run('go A',initial_state()))
    assert report['status']=='SAFETY_ABORT'
    assert len(planner.contexts)==1
    assert not report['completion_report']['verified']


def test_suppressing_execution_cancellation_blocks_replan_and_keeps_ownership():
    async def exercise():
        release=asyncio.Event()
        class Stuck(CoEgentExecutorAdapter):
            async def execute(self,p):
                self.context.report({'mission_id':self.context.request.mission_id,'revision':1,
                    'report_id':'e','kind':'error','message':'stuck'})
                try: await release.wait()
                except asyncio.CancelledError: await release.wait()
                return await super().execute(p)
        reg=ExecutorRegistry(); reg.register('stuck',lambda c,o:Stuck(c,o))
        planner=Planner([plan(skill('get_pose')),plan(skill('get_pose'))])
        agent=CoEgent(planner,MockSkills(),executor='stuck',executor_registry=reg,
            inspector=inspector(['B']), config=RuntimeConfig(skill_timeout_s=.01,connection_poll_s=.001))
        try:
            result=await agent.run('go A',initial_state())
            assert result['status']=='SAFETY_ABORT'
            assert len(planner.contexts)==1
            assert agent.admission.pending
        finally:
            release.set()
            await asyncio.sleep(.02)
    asyncio.run(exercise())


@pytest.mark.parametrize('phase,severity,want',[('snapshot','safety','SAFETY_ABORT'),
    ('snapshot','recoverable','REPLAN_REQUIRED'),('close','safety','SAFETY_ABORT')])
def test_error_accepted_during_final_observation_or_close_prevents_completion(phase,severity,want):
    agent=None
    def send():
        req=agent._executor_session.context.request
        ack=agent.submit_report({'mission_id':req.mission_id,'revision':req.revision,
            'report_id':'late-error','kind':'error','severity':severity,'message':'late failure'})
        assert ack['accepted']
    class Observing(Sensor):
        async def snapshot(self,report):
            if phase=='snapshot':send()
            await asyncio.sleep(.001)
            return await super().snapshot(report)
    class Closing(CoEgentExecutorAdapter):
        async def close(self):
            if phase=='close':send()
            await super().close()
    reg=ExecutorRegistry();reg.register('test',lambda c,o:Closing(c,o))
    ins=inspector(['A']);ins.provider=Observing(['A','A'])
    agent=CoEgent(Planner([plan(skill('get_pose'))]),MockSkills(),inspector=ins,
        executor='test',executor_registry=reg,config=RuntimeConfig(max_replans=0))
    report=asyncio.run(agent.run('go A',initial_state()))
    assert report['status']==want
    assert report['stop_confirmation']['confirmed'] is True
    assert report['mission_success'] is False
    assert not report['completion_report']['verified']


def test_finalization_seals_report_ingress_before_awaiting_late_error_cleanup():
    acknowledgements=[]
    class ReportingClose(CoEgentExecutorAdapter):
        def send(self,report_id,severity):
            return self.context.report({'mission_id':self.context.request.mission_id,'revision':1,
                'report_id':report_id,'kind':'error','severity':severity,'message':'late failure'})
        async def close(self):
            acknowledgements.append(self.send('close','recoverable'))
            await super().close()
        async def cancel(self):
            acknowledgements.append(self.send('during-final-stop','safety'))
            return await super().cancel()
    reg=ExecutorRegistry();reg.register('test',lambda c,o:ReportingClose(c,o))
    agent=CoEgent(Planner([plan(skill('get_pose'))]),MockSkills(),inspector=inspector(['A']),
        executor='test',executor_registry=reg,auto_save_plans=False)
    report=asyncio.run(agent.run('go A',initial_state()))
    assert acknowledgements[0]['accepted'] is True
    assert acknowledgements[1]=={'accepted':False,'reason':'mission_not_active'}
    assert report['mission_success'] is False
