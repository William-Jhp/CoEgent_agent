"""A persistent catalog deduplicates complete plans, not caller labels."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import importlib
import json
import asyncio
import sqlite3
import subprocess
import sys

import pytest

from coegent.scenarios import plan, skill


def store(path):
    assert importlib.util.find_spec('coegent.plan_store') is not None, 'persistent PlanStore missing'
    return importlib.import_module('coegent.plan_store').PlanStore(path)


def test_manual_add_survives_reopening_and_returns_copies(tmp_path):
    path=tmp_path/'plans.sqlite3';db=store(path)
    value=plan(skill('navigate_to',target='A'))
    saved=db.add(value,title='warehouse',author='upper-agent',source='agent')
    assert saved['created'] is True
    record=store(path).get(saved['id'])
    assert record['plan']==value and record['author']=='upper-agent'
    record['plan']['sequence'].clear()
    assert db.get(saved['id'])['plan']==value


def test_key_order_whitespace_and_metadata_do_not_create_duplicate_plan(tmp_path):
    db=store(tmp_path/'plans.sqlite3');value=plan(skill('get_pose'))
    first=db.add(value,title='first')
    changed={key:deepcopy(value[key]) for key in reversed(value)}
    second=db.add(json.dumps(changed,indent=4),title='different',author='agent')
    assert second['created'] is False and second['id']==first['id']
    assert len(db.list())==1
    assert db.get(first['id'])['title']=='first'


def test_full_plan_identity_preserves_step_order_and_success_criteria(tmp_path):
    db=store(tmp_path/'plans.sqlite3')
    one=plan(skill('get_pose'),skill('get_battery'))
    two=deepcopy(one);two['sequence'].reverse()
    three=deepcopy(one);three['success_criteria']=['different criterion']
    ids={db.add(value)['id'] for value in [one,two,three]}
    assert len(ids)==3


@pytest.mark.parametrize('value',['{}','{"a":1,"a":2}',plan(skill('missing_skill')),
                               plan(skill('rotate',angle_deg=float('nan')))])
def test_invalid_plan_is_rejected_without_a_row(tmp_path,value):
    db=store(tmp_path/'plans.sqlite3')
    with pytest.raises(ValueError): db.add(value)
    assert db.list()==[]


def test_concurrent_duplicate_inserts_produce_one_row(tmp_path):
    db=store(tmp_path/'plans.sqlite3');value=plan(skill('get_pose'))
    with ThreadPoolExecutor(max_workers=6) as workers:
        results=list(workers.map(lambda _:db.add(value),range(12)))
    assert len({r['id'] for r in results})==1
    assert sum(r['created'] for r in results)==1
    assert len(db.list())==1


def test_unknown_plan_id_fails_and_does_not_execute(tmp_path):
    db=store(tmp_path/'plans.sqlite3')
    with pytest.raises(ValueError,match='not found'): db.get(999)


def test_scripted_planner_snapshots_selected_ids_without_deleting_saved_rows(tmp_path):
    from coegent.planner import ScriptedPlanner
    db=store(tmp_path/'plans.sqlite3')
    values=[plan(skill('get_pose')),plan(skill('get_battery'))]
    ids=[db.add(value)['id'] for value in values]
    assert hasattr(ScriptedPlanner,'from_store'), 'missing catalog-backed scripted planner'
    planner=ScriptedPlanner.from_store(db,ids)
    async def exercise():
        assert await planner.plan({})==values[0]
        assert await planner.plan({})==values[1]
        with pytest.raises(ValueError,match='exhausted'): await planner.plan({})
    asyncio.run(exercise())
    assert len(db.list())==2


def test_successful_execution_is_archived_once_and_remains_reusable(tmp_path):
    from coegent import PlanExecutor, MissionRequest
    from coegent.skills import MockSkills
    from coegent.scenarios import initial_state
    from time import time
    db=store(tmp_path/'plans.sqlite3')
    async def exercise():
        backend=MockSkills()
        executor=PlanExecutor(backend,plan_store=db)
        request=MissionRequest('saved-mission',1,time()+10,'pose',plan(skill('get_pose')))
        first=await executor.execute_request(request,initial_state())
        second=await executor.execute_request(request,initial_state())
        assert first['status']=='SUCCESS' and first['plan_store']['status']=='saved'
        assert second['replayed'] is True
        assert db.get(first['plan_store']['id'])['success_count']==1
    asyncio.run(exercise())


@pytest.mark.parametrize('goal_target',['B','A'])
def test_only_achieved_requested_goals_are_archived(tmp_path,goal_target):
    from coegent import PlanExecutor, Goal
    from coegent.skills import MockSkills
    from coegent.scenarios import initial_state
    db=store(tmp_path/'plans.sqlite3')
    report=asyncio.run(PlanExecutor(MockSkills(),plan_store=db).execute(
        plan(skill('navigate_to',target='A')),initial_state(),goals=(Goal('at_location',goal_target),)))
    assert report['status']=='SUCCESS'
    assert len(db.list())==(1 if goal_target=='A' else 0)


def test_archive_failure_does_not_change_success_or_repeat_actions():
    from coegent import PlanExecutor
    from coegent.skills import MockSkills
    from coegent.scenarios import initial_state
    class BrokenStore:
        def record_success(self,*args,**kwargs): raise OSError('disk unavailable')
    backend=MockSkills()
    report=asyncio.run(PlanExecutor(backend,plan_store=BrokenStore()).execute(plan(skill('get_pose')),initial_state()))
    assert report['status']=='SUCCESS'
    assert report['plan_store']['status']=='error'
    assert backend.calls==[{'name':'get_pose','args':{}}]


def test_auto_archive_can_be_disabled(tmp_path):
    from coegent import PlanExecutor
    from coegent.skills import MockSkills
    from coegent.scenarios import initial_state
    db=store(tmp_path/'plans.sqlite3')
    report=asyncio.run(PlanExecutor(MockSkills(),plan_store=db,auto_save_plans=False).execute(plan(skill('get_pose')),initial_state()))
    assert report['status']=='SUCCESS' and report['plan_store']['reason']=='disabled'
    assert not db.path.exists()


def test_numeric_format_differences_do_not_duplicate_identical_plan(tmp_path):
    db=store(tmp_path/'plans.sqlite3')
    first=db.add(plan(skill('rotate',angle_deg=90)))
    second=db.add(plan(skill('rotate',angle_deg=90.0)))
    assert second['id']==first['id'] and second['created'] is False


def test_database_tampering_is_rejected_on_read(tmp_path):
    db=store(tmp_path/'plans.sqlite3');plan_id=db.add(plan(skill('get_pose')))['id']
    with sqlite3.connect(db.path) as connection:
        connection.execute('UPDATE plans SET plan_json=? WHERE id=?',(json.dumps(plan(skill('get_battery'))),plan_id))
    with pytest.raises(ValueError,match='integrity'): db.get(plan_id)


def test_simultaneous_process_inserts_do_not_duplicate_plan(tmp_path):
    db=store(tmp_path/'plans.sqlite3');db.initialize()
    script='''import json,sys
from coegent import PlanStore
value=json.loads(sys.stdin.read())
print(json.dumps(PlanStore(sys.argv[1],timeout_s=2).add(value)))
'''
    with ThreadPoolExecutor(max_workers=4) as workers:
        def add(_):
            result=subprocess.run([sys.executable,'-c',script,str(db.path)],
                                  input=json.dumps(plan(skill('get_pose'))),capture_output=True,text=True)
            assert result.returncode==0,result.stderr
            return json.loads(result.stdout)
        results=list(workers.map(add,range(4)))
    assert len({result['id'] for result in results})==1
    assert sum(result['created'] for result in results)==1


def test_failed_execution_is_not_archived(tmp_path):
    from coegent import PlanExecutor
    from coegent.skills import MockSkills
    from coegent.scenarios import initial_state
    db=store(tmp_path/'plans.sqlite3')
    backend=MockSkills({'get_pose':[{'event':'EXPECTED_FAILURE','reason':'unavailable'}]})
    report=asyncio.run(PlanExecutor(backend,plan_store=db).execute(plan(skill('get_pose')),initial_state()))
    assert report['status']=='REPLAN_REQUIRED'
    assert report['plan_store']['status']=='skipped' and db.list()==[]


def test_plan_only_does_not_create_or_archive_catalog(tmp_path):
    from coegent import PlanGenerator
    from coegent.planner import ScriptedPlanner
    from coegent.scenarios import initial_state
    from coegent.plan_store import default_plan_db
    path=default_plan_db()
    report=asyncio.run(PlanGenerator(ScriptedPlanner([plan(skill('get_pose'))])).generate('pose',initial_state()))
    assert report['status']=='PLAN_READY'
    assert not path.exists()


def test_success_history_preserves_mock_provenance(tmp_path):
    from coegent import PlanExecutor
    from coegent.skills import MockSkills
    from coegent.scenarios import initial_state
    db=store(tmp_path/'plans.sqlite3')
    report=asyncio.run(PlanExecutor(MockSkills(),plan_store=db).execute(plan(skill('get_pose')),initial_state()))
    records=db.successes(report['plan_store']['id'])
    assert records[0]['backend']=='coegent.skills.MockSkills'
    assert records[0]['goal_status']=='not_evaluated' and records[0]['executor']=='coegent'


def test_export_cannot_overwrite_its_own_database(tmp_path):
    db=store(tmp_path/'plans.sqlite3');plan_id=db.add(plan(skill('get_pose')))['id']
    with pytest.raises(ValueError,match='database'): db.export(plan_id,db.path)
    assert db.get(plan_id)['plan']==plan(skill('get_pose'))


def test_concurrent_first_initialization_uses_consistent_database_identity(tmp_path):
    import threading
    from concurrent.futures import TimeoutError as FutureTimeout
    path=tmp_path/'fresh.sqlite3';first=store(path);second=store(path)
    observed=threading.Event();release=threading.Event()
    class CapturedRow:
        def __init__(self,row): self.row=row
        def fetchone(self): return self.row
    class PausedConnection(sqlite3.Connection):
        def execute(self,query,*args,**kwargs):
            cursor=super().execute(query,*args,**kwargs)
            if query=='PRAGMA user_version':
                row=cursor.fetchone()
                observed.set();release.wait(2)
                return CapturedRow(row)
            return cursor
    def connect():
        connection=sqlite3.connect(path,timeout=2,factory=PausedConnection)
        connection.row_factory=sqlite3.Row
        return connection
    first._connect=connect
    with ThreadPoolExecutor(max_workers=2) as workers:
        a=workers.submit(first.initialize)
        assert observed.wait(2)
        b=workers.submit(second.initialize)
        try:
            try: b.result(timeout=.15)
            except FutureTimeout: pass
        finally: release.set()
        a.result(timeout=3);b.result(timeout=3)
    assert first.list()==[] and second.list()==[]


def test_replanned_success_is_not_archived_as_a_complete_mission_plan(tmp_path):
    from coegent import CoEgent
    from coegent.planner import ScriptedPlanner
    from coegent.skills import MockSkills
    from coegent.scenarios import scenarios
    db=store(tmp_path/'plans.sqlite3');case=scenarios()['bounded_replan']
    report=asyncio.run(CoEgent(ScriptedPlanner(case['plans']),MockSkills(),plan_store=db).run(case['command'],case['state']))
    assert report['status']=='SUCCESS' and report['replans']==1
    assert report['plan_store']['reason']=='continuation_not_full_plan'
    assert not db.path.exists()


def test_unknown_goal_and_duplicate_outcome_do_not_inflate_success_history(tmp_path):
    from coegent import PlanExecutor
    from coegent.skills import MockSkills
    from coegent.scenarios import initial_state
    db=store(tmp_path/'plans.sqlite3');value=plan(skill('get_pose'))
    report=asyncio.run(PlanExecutor(MockSkills(),auto_save_plans=False).execute(value,initial_state()))
    unverified={**report,'goal_status':'unknown','mission_success':None}
    assert db.record_success(value,unverified,backend='mock')['status']=='skipped'
    assert not db.path.exists()
    first=db.record_success(value,report,backend='coegent.skills.MockSkills')
    second=db.record_success(value,report,backend='coegent.skills.MockSkills')
    assert first['success_recorded'] is True and second['success_recorded'] is False
    assert db.get(first['id'])['success_count']==1


def test_export_cannot_overwrite_database_alias_or_sidecar(tmp_path):
    db=store(tmp_path/'plans.sqlite3');plan_id=db.add(plan(skill('get_pose')))['id']
    alias=tmp_path/'alias.json';alias.symlink_to(db.path)
    for output in [alias,str(db.path)+'-wal',str(db.path)+'-shm',str(db.path)+'-journal']:
        with pytest.raises(ValueError,match='database'): db.export(plan_id,output)
    assert db.get(plan_id)['success_count']==0


def test_unrelated_database_is_rejected_without_changing_journal_mode(tmp_path):
    path=tmp_path/'other.sqlite3'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE unrelated (value TEXT)')
        connection.execute("INSERT INTO unrelated VALUES ('keep')")
        assert connection.execute('PRAGMA journal_mode').fetchone()[0]=='delete'
    with pytest.raises(ValueError,match='unrelated'): store(path).initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute('PRAGMA journal_mode').fetchone()[0]=='delete'
        assert connection.execute('SELECT value FROM unrelated').fetchone()[0]=='keep'
