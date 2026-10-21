"""Catalog CLI and explicit ID selection use the same persistent store."""
import json
import subprocess
import sys
from time import time

from coegent.scenarios import plan, skill


def cli(*args):
    return subprocess.run([sys.executable,'-m','coegent',*map(str,args)],capture_output=True,text=True)


def test_add_list_show_export_and_dedup(tmp_path):
    db=tmp_path/'catalog.sqlite3';source=tmp_path/'plan.json';source.write_text(json.dumps(plan(skill('get_pose'))))
    first=cli('plans','add','--plan-db',db,'--plan-file',source,'--title','pose','--author','agent')
    assert first.returncode==0,first.stderr
    saved=json.loads(first.stdout);plan_id=saved['id']
    second=cli('plans','add','--plan-db',db,'--plan-file',source)
    assert second.returncode==0,second.stderr
    assert json.loads(second.stdout)['created'] is False
    listed=cli('plans','list','--plan-db',db)
    assert len(json.loads(listed.stdout)['plans'])==1
    shown=cli('plans','show','--plan-db',db,'--plan-id',plan_id)
    assert json.loads(shown.stdout)['plan']==plan(skill('get_pose'))
    output=tmp_path/'export.json'
    exported=cli('plans','export','--plan-db',db,'--plan-id',plan_id,'--out',output)
    assert exported.returncode==0,exported.stderr
    assert json.loads(output.read_text())==plan(skill('get_pose'))


def test_database_planner_and_run_reuse_the_same_plan_id(tmp_path):
    from coegent import PlanStore
    db=tmp_path/'plans.sqlite3';store=PlanStore(db)
    plan_id=store.add(plan(skill('get_pose')))['id']
    output=tmp_path/'report.json'
    result=cli('run','--planner','db','--plan-db',db,'--plan-id',plan_id,'--command','pose','--out',output)
    assert result.returncode==0,result.stderr
    report=json.loads(output.read_text())
    assert report['status']=='SUCCESS' and report['plan_store']['id']==plan_id
    assert report['plan_store']['created'] is False
    assert len(store.list())==1 and store.get(plan_id)['success_count']==1


def test_request_can_select_stored_plan_without_inline_plan(tmp_path):
    from coegent import PlanStore
    db=tmp_path/'plans.sqlite3';store=PlanStore(db)
    plan_id=store.add(plan(skill('get_pose')))['id']
    request=tmp_path/'request.json'
    request.write_text(json.dumps({'mission_id':'db-request','revision':1,'expires_at':time()+60,'command':'pose'}))
    output=tmp_path/'report.json'
    result=cli('request','--plan-db',db,'--plan-id',plan_id,'--request-file',request,'--out',output)
    assert result.returncode==0,result.stderr
    report=json.loads(output.read_text())
    assert report['status']=='SUCCESS' and report['planner_calls']==0
    assert report['plan_store']['id']==plan_id


def test_request_rejects_inline_plan_and_db_id_conflict(tmp_path):
    from coegent import PlanStore
    db=tmp_path/'plans.sqlite3';plan_id=PlanStore(db).add(plan(skill('get_pose')))['id']
    request=tmp_path/'request.json'
    request.write_text(json.dumps({'mission_id':'conflict','revision':1,'expires_at':time()+60,'command':'pose','plan':plan(skill('get_battery'))}))
    result=cli('request','--plan-db',db,'--plan-id',plan_id,'--request-file',request)
    assert result.returncode==2
    assert 'both' in result.stderr
