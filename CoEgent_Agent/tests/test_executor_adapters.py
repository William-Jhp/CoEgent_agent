"""An executor selection must be a real, validated execution boundary."""
import asyncio
from copy import deepcopy
import importlib
import json
import subprocess
import sys

import pytest

from coegent import CoEgent, PlanExecutor
from coegent.planner import ScriptedPlanner
from coegent.scenarios import initial_state, plan, skill
from coegent.skills import MockSkills


def adapters():
    assert importlib.util.find_spec('coegent_executor_adapters') is not None, 'missing separate adapter package'
    return importlib.import_module('coegent_executor_adapters')


def test_registry_selection_and_removal_are_explicit():
    api=adapters()
    registry=api.ExecutorRegistry()
    factory=lambda context,options: (context,options)
    registry.register('test',factory)
    assert registry.resolve('test') is factory
    with pytest.raises(ValueError,match='registered'):
        registry.register('test',factory)
    registry.unregister('test')
    with pytest.raises(ValueError,match='unknown'):
        registry.resolve('test')


def test_default_agent_executes_through_native_adapter():
    backend=MockSkills()
    report=asyncio.run(CoEgent(ScriptedPlanner([plan(skill('get_battery'))]),backend).run('battery',initial_state()))
    assert report['status']=='SUCCESS'
    assert report.get('executor')=='coegent', 'native execution bypassed adapter metadata'
    assert [e['kind'] for e in report['trace']].count('executor_prepare')==1
    assert [e['kind'] for e in report['trace']].count('executor_execute')==1
    assert backend.calls==[{'name':'get_battery','args':{}}]


def test_registered_adapter_transforms_payload_and_returns_real_execution():
    api=adapters()
    from coegent.executor_adapters.native import CoEgentExecutorAdapter
    prepared=[]

    class Wrapped(CoEgentExecutorAdapter):
        async def prepare(self,canonical):
            payload={'operations':deepcopy(canonical)}
            prepared.append(payload)
            return api.PreparedPlan(self.context.name,payload)

        async def execute(self,value):
            return await super().execute(api.PreparedPlan(self.context.name,value.payload['operations']))

    registry=api.ExecutorRegistry()
    registry.register('wrapped',lambda context,options: Wrapped(context,options))
    backend=MockSkills()
    agent=CoEgent(ScriptedPlanner([plan(skill('navigate_to',target='A'))]),backend,
                  executor='wrapped',executor_registry=registry)
    report=asyncio.run(agent.run('move',initial_state()))
    assert report['status']=='SUCCESS'
    assert report['executor']=='wrapped'
    assert report['state']['robot']['location']=='A'
    assert prepared[0]['operations']['schema_version']=='planner.v1'
    assert backend.calls==[{'name':'navigate_to','args':{'target':'A'}}]


def test_invalid_plan_does_not_reach_prepare_or_execute():
    api=adapters()
    from coegent.executor_adapters.native import CoEgentExecutorAdapter
    entered=[]

    class Guarded(CoEgentExecutorAdapter):
        async def prepare(self,value):
            entered.append('prepare')
            return await super().prepare(value)

    registry=api.ExecutorRegistry();registry.register('guarded',lambda c,o: Guarded(c,o))
    backend=MockSkills()
    report=asyncio.run(PlanExecutor(backend,executor='guarded',executor_registry=registry).execute({},initial_state()))
    assert report['status']=='INVALID_PLAN'
    assert entered==[]
    assert all(c['name']=='stop' for c in backend.calls)


def test_unknown_executor_is_rejected_before_any_robot_call():
    backend=MockSkills()
    with pytest.raises(ValueError,match='unknown'):
        CoEgent(None,backend,executor='not-installed')
    assert backend.calls==[]


def test_only_selected_entry_point_is_loaded_and_removed_plugin_stays_disabled(monkeypatch):
    api=adapters()
    loaded=[]
    class Entry:
        def __init__(self,name): self.name=name
        def load(self):
            loaded.append(self.name)
            return lambda context,options: None
    monkeypatch.setattr('coegent_executor_adapters.registry.entry_points',lambda **kw:[Entry('one'),Entry('two')])
    registry=api.ExecutorRegistry(discover_plugins=True)
    assert registry.names==('one','two')
    assert loaded==[]
    registry.resolve('one')
    assert loaded==['one']
    registry.unregister('two')
    with pytest.raises(ValueError,match='unknown'): registry.resolve('two')
    assert loaded==['one']


def test_selection_copies_options_and_survives_registration_removal():
    api=adapters()
    from coegent.executor_adapters.native import CoEgentExecutorAdapter
    seen=[]
    registry=api.ExecutorRegistry()
    def factory(context,options):
        seen.append(deepcopy(options))
        return CoEgentExecutorAdapter(context,{})
    registry.register('selected',factory)
    options={'endpoint':'local'}
    agent=CoEgent(ScriptedPlanner([plan(skill('get_pose'))]),MockSkills(),executor='selected',
                  executor_options=options,executor_registry=registry)
    options['endpoint']='changed'
    registry.unregister('selected')
    assert asyncio.run(agent.run('pose',initial_state()))['status']=='SUCCESS'
    assert seen==[{'endpoint':'local'}]


def test_executor_selection_cannot_change_during_mission_but_can_between_missions():
    api=adapters()
    from coegent.executor_adapters.native import CoEgentExecutorAdapter
    from coegent import MissionRequest
    from time import time
    async def exercise():
        registry=api.ExecutorRegistry()
        for name in ['first','second']:
            registry.register(name,lambda c,o:CoEgentExecutorAdapter(c,o))
        backend=MockSkills(delay_s=.03)
        agent=CoEgent(None,backend,executor='first',executor_registry=registry)
        request=MissionRequest('adapter-switch',1,time()+10,'pose',plan(skill('get_pose')))
        task=asyncio.create_task(agent.run_request(request,initial_state()))
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError,match='active'): agent.set_executor('second')
        first=await task
        agent.set_executor('second')
        request=MissionRequest('adapter-next',1,time()+10,'pose',plan(skill('get_pose')))
        second=await agent.run_request(request,initial_state())
        assert first['executor']=='first' and second['executor']=='second'
    asyncio.run(exercise())


@pytest.mark.parametrize('failure',['prepared','result','close','cancel'])
def test_adapter_failures_do_not_fabricate_execution_or_stop_success(failure):
    api=adapters()
    from coegent.executor_adapters.native import CoEgentExecutorAdapter
    class Broken(CoEgentExecutorAdapter):
        async def prepare(self,value):
            if failure in ('prepared','cancel'): return api.PreparedPlan('different',value)
            return await super().prepare(value)
        async def execute(self,value):
            if failure=='result': return {'status':'SUCCESS'}
            return await super().execute(value)
        async def close(self):
            if failure=='close': raise RuntimeError('connection cleanup failed')
            return await super().close()
        async def cancel(self):
            if failure=='cancel': return {'confirmed':True}
            return await super().cancel()
    registry=api.ExecutorRegistry();registry.register('broken',lambda c,o:Broken(c,o))
    backend=MockSkills()
    report=asyncio.run(PlanExecutor(backend,executor='broken',executor_registry=registry).execute(plan(skill('get_pose')),initial_state()))
    assert report['status']!='SUCCESS'
    if failure=='cancel':
        assert report['stop_confirmation']['confirmed'] is False
        assert report['robot_fault']=='invalid_executor_stop_result'
    else:
        assert any(c['name']=='stop' for c in backend.calls)


def test_cancellation_uses_adapter_cancel_and_closes_resources():
    api=adapters()
    from coegent.executor_adapters.native import CoEgentExecutorAdapter
    calls=[]
    class Tracked(CoEgentExecutorAdapter):
        async def cancel(self):
            calls.append('cancel')
            return await super().cancel()
        async def close(self):
            calls.append('close')
            return await super().close()
    async def exercise():
        registry=api.ExecutorRegistry();registry.register('tracked',lambda c,o:Tracked(c,o))
        backend=MockSkills(delay_s=.1)
        agent=CoEgent(ScriptedPlanner([plan(skill('get_pose'))]),backend,executor='tracked',executor_registry=registry)
        task=asyncio.create_task(agent.run('pose',initial_state()))
        await asyncio.sleep(.01);task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert agent.last_report['status']=='CANCELLED'
        assert agent.last_report['stop_confirmation']['confirmed'] is True
        assert calls==['cancel','close']
    asyncio.run(exercise())


def test_run_cli_selects_executor_and_options(tmp_path):
    source=tmp_path/'plan.json';source.write_text(json.dumps(plan(skill('get_pose'))))
    output=tmp_path/'report.json';options=tmp_path/'options.json';options.write_text('{}')
    result=subprocess.run([sys.executable,'-m','coegent','run','--planner','file',
        '--plan-file',str(source),'--command','pose','--executor','coegent',
        '--executor-options',str(options),'--out',str(output)],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    report=json.loads(output.read_text())
    assert report['executor']=='coegent'
    assert report['status']=='SUCCESS'


def test_cli_lists_available_executors_without_robot_execution():
    result=subprocess.run([sys.executable,'-m','coegent','executors'],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert 'coegent' in json.loads(result.stdout)['executors']


def test_run_cli_rejects_unknown_executor_without_creating_output(tmp_path):
    source=tmp_path/'plan.json';source.write_text(json.dumps(plan(skill('get_pose'))))
    output=tmp_path/'report.json'
    result=subprocess.run([sys.executable,'-m','coegent','run','--planner','file',
        '--plan-file',str(source),'--command','pose','--executor','absent',
        '--out',str(output)],capture_output=True,text=True)
    assert result.returncode==2
    assert 'unknown executor' in result.stderr
    assert not output.exists()


def test_invalid_progress_cannot_break_cleanup_or_leave_request_owned():
    api=adapters()
    from coegent.executor_adapters.native import CoEgentExecutorAdapter
    class BrokenProgress(CoEgentExecutorAdapter):
        @property
        def progress(self): return {'steps':'unknown'}
    registry=api.ExecutorRegistry();registry.register('broken',lambda c,o:BrokenProgress(c,o))
    agent=CoEgent(None,MockSkills(),executor='broken',executor_registry=registry)
    from coegent import MissionRequest
    from time import time
    request=MissionRequest('bad-progress',1,time()+10,'pose',plan(skill('get_pose')))
    report=asyncio.run(agent.run_request(request,initial_state()))
    assert report['status']=='SAFETY_ABORT'
    assert report['stop_confirmation']['confirmed'] is True
    assert not agent.admission.owns(request)


def test_timed_out_close_remains_owned_and_blocks_executor_switch():
    api=adapters()
    from coegent.executor_adapters.native import CoEgentExecutorAdapter
    from coegent_contracts import RuntimeConfig
    async def exercise():
        release=asyncio.Event()
        class SlowClose(CoEgentExecutorAdapter):
            async def close(self):
                try: await release.wait()
                except asyncio.CancelledError: await release.wait()
        registry=api.ExecutorRegistry();registry.register('slow',lambda c,o:SlowClose(c,o))
        agent=CoEgent(ScriptedPlanner([plan(skill('get_pose'))]),MockSkills(),executor='slow',
                      executor_registry=registry,config=RuntimeConfig(skill_timeout_s=.02))
        try:
            report=await agent.run('pose',initial_state())
            assert report['status']=='SAFETY_ABORT'
            assert agent.admission.pending, 'adapter cleanup escaped ownership tracking'
            with pytest.raises(RuntimeError,match='active'): agent.set_executor('slow')
        finally:
            release.set()
            await asyncio.sleep(.01)
        assert not agent.admission.pending
    asyncio.run(exercise())


def test_plugin_negative_stop_confirmation_latches_fault_and_blocks_next_mission():
    api=adapters()
    from dataclasses import replace
    from coegent.executor_adapters.native import CoEgentExecutorAdapter
    class Unconfirmed(CoEgentExecutorAdapter):
        @property
        def progress(self):
            return replace(super().progress,stop_confirmation={'confirmed':False,'reason':'remote_stop_unconfirmed'})
    registry=api.ExecutorRegistry()
    registry.register('unconfirmed',lambda c,o:Unconfirmed(c,o))
    registry.register('native',lambda c,o:CoEgentExecutorAdapter(c,o))
    async def exercise():
        backend=MockSkills()
        agent=CoEgent(None,backend,executor='unconfirmed',executor_registry=registry)
        from coegent import MissionRequest
        from time import time
        request=MissionRequest('unconfirmed',1,time()+10,'pose',plan(skill('get_pose')))
        first=await agent.run_request(request,initial_state())
        assert first['status']=='SAFETY_ABORT'
        assert first['robot_fault']=='remote_stop_unconfirmed'
        agent.set_executor('native')
        request=MissionRequest('next',1,time()+10,'pose',plan(skill('get_pose')))
        second=await agent.run_request(request,initial_state())
        assert second['status']=='REJECTED'
        assert second['reason']=='robot_fault_latched'
    asyncio.run(exercise())


@pytest.mark.parametrize('invalid',['negative_stop','malformed_progress'])
def test_progress_first_changed_during_close_cannot_leave_success_report(invalid):
    api=adapters()
    from dataclasses import replace
    from coegent.executor_adapters.native import CoEgentExecutorAdapter
    class ChangesOnClose(CoEgentExecutorAdapter):
        @property
        def progress(self):
            if self._closed:
                if invalid=='malformed_progress': return {'steps':'unknown'}
                return replace(super().progress,stop_confirmation={'confirmed':False,'reason':'close_stop_unconfirmed'})
            return super().progress
    registry=api.ExecutorRegistry();registry.register('changes',lambda c,o:ChangesOnClose(c,o))
    report=asyncio.run(PlanExecutor(MockSkills(),executor='changes',executor_registry=registry).execute(plan(skill('get_pose')),initial_state()))
    assert report['status']=='SAFETY_ABORT'
    assert report['mission_success'] is not True
    if invalid=='negative_stop':
        assert report['stop_confirmation']['confirmed'] is False
        assert report['robot_fault']=='close_stop_unconfirmed'


def test_close_failure_preserves_original_execution_failure_reason():
    api=adapters()
    from coegent.executor_adapters.native import CoEgentExecutorAdapter
    class BrokenClose(CoEgentExecutorAdapter):
        async def close(self): raise RuntimeError('close failed')
    registry=api.ExecutorRegistry();registry.register('broken',lambda c,o:BrokenClose(c,o))
    backend=MockSkills({'get_pose':[{'event':'EXPECTED_FAILURE','reason':'sensor_failure'}]})
    report=asyncio.run(PlanExecutor(backend,executor='broken',executor_registry=registry).execute(plan(skill('get_pose')),initial_state()))
    assert report['status']=='SAFETY_ABORT'
    assert report['failure_reason']=='sensor_failure'
