"""Deterministic retrieval narrows candidates; it never decides equivalence."""
import importlib
import json
import sqlite3

from coegent import PlanStore
from coegent.scenarios import initial_state, plan, skill


def test_extracts_known_entity_keywords_and_numeric_arguments():
    assert importlib.util.find_spec('coegent.plan_features') is not None, 'missing programmatic extraction'
    from coegent.plan_features import extract_features
    features=extract_features('A로 이동하고 90도 회전해. B에는 가지 마.',initial_state().snapshot())
    assert 'a' in features['targets'] and 'b' in features['targets']
    assert '90' in features['arguments']
    assert 'navigate_to' in features['keywords']


def test_search_returns_targeted_plans_and_preserves_full_conditions(tmp_path):
    store=PlanStore(tmp_path/'plans.sqlite3')
    a=store.add(plan(skill('navigate_to',target='A')),command='A로 이동해')['id']
    store.add(plan(skill('navigate_to',target='B')),command='B로 이동해')
    store.add(plan(skill('get_battery')),command='배터리 잔량 확인')
    assert hasattr(store,'search'), 'missing indexed search'
    result=store.search('A로 이동해',state=initial_state().snapshot(),limit=1)
    assert result['candidates'][0]['id']==a
    assert result['candidates'][0]['plan']['sequence']==[skill('navigate_to',target='A')]


def test_duplicate_commands_remain_searchable_without_duplicate_plans(tmp_path):
    store=PlanStore(tmp_path/'plans.sqlite3');value=plan(skill('navigate_to',target='A'))
    first=store.add(value,command='창고로 이동해')
    store.add(value,command='물류동에 방문해')
    assert hasattr(store,'commands'), 'missing command aliases'
    assert set(store.commands(first['id']))=={'창고로 이동해','물류동에 방문해'}
    assert store.search('물류동')['candidates'][0]['id']==first['id']
    assert len(store.list())==1


def test_version_one_migration_preserves_id_plan_and_success_history(tmp_path):
    path=tmp_path/'v1.sqlite3';store=PlanStore(path)
    saved=store.add(plan(skill('get_pose')),command='위치 확인')
    store.record_success(plan(skill('get_pose')), {
        'status':'SUCCESS','execution_status':'SUCCESS','goal_status':'not_evaluated',
        'mission_id':'migration-check','revision':1,'executor':'coegent','state_mode':'snapshot',
        'command':'좌표 알려줘'},backend='fixture')
    history=store.successes(saved['id'])
    with sqlite3.connect(path) as connection:
        connection.execute('DROP TABLE IF EXISTS plan_search_terms')
        connection.execute('DROP TABLE IF EXISTS plan_commands')
        connection.execute('PRAGMA user_version=1')
    reopened=PlanStore(path);reopened.initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0]==2, 'missing v1 migration'
    assert reopened.get(saved['id'])['plan']==plan(skill('get_pose'))
    assert reopened.get(saved['id'])['success_count']==1
    assert reopened.successes(saved['id'])==history
    assert reopened.search('위치')['candidates'][0]['id']==saved['id']


def test_candidate_limit_is_bounded_and_unknown_words_do_not_pick_anything(tmp_path):
    store=PlanStore(tmp_path/'plans.sqlite3')
    for index in range(8): store.add(plan(skill('get_pose'),goal=f'pose {index}'),command='위치 확인')
    assert hasattr(store,'search'), 'missing indexed search'
    assert len(store.search('위치 확인',limit=3)['candidates'])==3
    assert store.search('무관한새로운단어')['candidates']==[]
