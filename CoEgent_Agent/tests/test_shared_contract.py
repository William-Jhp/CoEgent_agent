import asyncio
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from coegent.agent import CoEgent
from coegent.planner import ModelServerPlanner, ScriptedPlanner, messages
from coegent.registry import SkillRegistry
from coegent.scenarios import initial_state, plan, skill
from coegent.skills import MockSkills
from coegent.types import Limits, State
from coegent.validation import PlanError, PlanValidator


def test_contract_is_small_and_shared():
    from coegent_contracts import SkillRegistry as SharedRegistry
    from coegent_contracts.plan import mission_plan_schema
    from Planner_SFT_dataset_v1.planner_data.catalog import SKILLS
    registry = SharedRegistry()
    assert registry.skills == SKILLS == SkillRegistry().skills
    for name in registry.skills:
        assert set(registry.definition(name)) == {
            'name', 'args_schema', 'preconditions', 'result_schema', 'effects'}
    assert PlanValidator(registry).schema == mission_plan_schema(registry)
    assert len(registry.fingerprint) == 64
    changed = registry.definition('rotate')
    changed['args_schema']['required'] = []
    assert registry.args_schema('rotate')['required'] == ['angle_deg']


def test_state_resolve_uses_shared_contract():
    from coegent_contracts.state import StateContract
    state = initial_state().snapshot()
    state['observations']['robot'] = {'battery_percent': 0}
    assert StateContract.resolve(state, 'robot.battery_percent') == 80
    assert State.from_dict(state).resolve('robot.battery_percent') == 80
    with pytest.raises(ValueError):
        StateContract.validate({'robot': {'battery_percent': float('nan')}})
    with pytest.raises(ValueError):
        StateContract.validate({'world': {'paths': ['not', 'a', 'map']}})
    StateContract.validate({'robot': {'battery_percent': None}})


def test_one_runtime_config_controls_safety_and_budgets():
    from coegent_contracts import RuntimeConfig
    state = initial_state()
    state.robot['battery_percent'] = 20
    config = RuntimeConfig(min_battery=30, max_repairs=0, max_replans=0)
    agent = CoEgent(ScriptedPlanner([plan(skill('navigate_to', target='A'))]), MockSkills(), config=config)
    report = asyncio.run(agent.run('test', state))
    assert report['reason'] == 'battery_low'
    assert agent.limits.max_repairs == 0
    assert agent.router.policy.min_battery == 30
    with pytest.raises(ValueError, match='config'):
        CoEgent(ScriptedPlanner([]), MockSkills(), config=config, limits=Limits())


def test_effects_preserve_allowlist_and_invalidate_observations():
    from coegent_contracts.semantics import apply_effects
    before = initial_state().snapshot()
    after = apply_effects(before, 'get_battery', {}, {'battery_percent': 8, 'authority': 'all'})
    assert after['robot']['battery_percent'] == 8
    assert 'authority' not in after['robot']
    assert before['robot']['battery_percent'] == 80
    after = apply_effects(after, 'navigate_to', {'target': 'A'}, {'location': 'A'})
    assert after['observations'] == {}


def test_preconditions_match_dispatch_policy():
    from coegent_contracts.config import RuntimeConfig
    from coegent_contracts.semantics import check_preconditions
    from coegent.safety import SafetyPolicy
    state = initial_state()
    state.observations['inspection'] = {'target': 'box', 'target_exists': True, 'location': 'OTHER'}
    issue = check_preconditions('pick_object', {'object': 'box'}, state.snapshot(), RuntimeConfig())
    assert issue['reason'] == SafetyPolicy().check('pick_object', {'object': 'box'}, state).reason
    assert issue['reason'] == 'target_not_verified'


def test_preflight_rejects_uninspected_pick_but_defers_after_sensing():
    from coegent_contracts.config import RuntimeConfig
    validator = PlanValidator(SkillRegistry())
    state = initial_state()
    with pytest.raises(PlanError, match='target_not_verified'):
        validator.preflight(plan(skill('pick_object', object='box')), state, RuntimeConfig())
    before = state.snapshot()
    findings = validator.preflight(plan(skill('inspect_target', target='box'),
                                        skill('pick_object', object='box')), state, RuntimeConfig())
    assert any(item['status'] == 'deferred' for item in findings)
    assert state.snapshot() == before


def test_preflight_does_not_reject_unselected_or_unknown_branch():
    from coegent_contracts.config import RuntimeConfig
    from coegent.scenarios import condition
    validator = PlanValidator(SkillRegistry())
    state = initial_state()
    state.robot['battery_percent'] = 8
    candidate = plan(condition('robot.battery_percent', 80,
                              [skill('navigate_to', target='A')], [skill('stop')]))
    findings = validator.preflight(candidate, state, RuntimeConfig())
    assert findings[0]['path'] == 'sequence[0].else[0]'
    assert findings[0]['reason'] == 'await_driver_result'
    unknown = plan(condition('world.not_observed', True,
                            [skill('navigate_to', target='A')], [skill('stop')]))
    assert validator.preflight(unknown, state, RuntimeConfig())[0]['status'] == 'deferred'


def test_model_server_planner_preserves_the_validator_contract():
    from coegent.planner import response_format
    from jsonschema import Draft202012Validator
    schema = PlanValidator(SkillRegistry()).schema
    before = json.dumps(schema, sort_keys=True)
    value = response_format(schema)
    transported = value['json_schema']['schema']
    assert json.dumps(schema, sort_keys=True) == before
    assert transported['properties'] == schema['properties']
    assert transported['$defs']['predicate']['properties']['value'] != {}
    Draft202012Validator(transported).validate(plan(skill('get_battery')))
    assert value['json_schema']['strict'] is True


def test_shared_prompt_is_a_compact_rendering_not_a_second_schema():
    from coegent_contracts.prompting import planner_messages
    prompt = planner_messages('test', initial_state().snapshot())[0]['content']
    assert len(prompt) < 9000
    for name in SkillRegistry().skills:
        assert name in prompt
    assert 'preconditions' in prompt and 'effects' in prompt


def test_context_and_prompt_carry_shared_contract():
    agent = CoEgent(ScriptedPlanner([plan(skill('stop'))]), MockSkills())
    report = asyncio.run(agent.run('stop', initial_state()))
    context = agent.planner.contexts[0]
    assert context['contract_version'] == report['contract_version']
    assert context['contract_hash'] == report['contract_hash']
    assert 'preconditions' in messages(context)[0]['content']
    assert report['execution_status'] == report['status'] == 'SUCCESS'
    assert report['goal_status'] == 'not_evaluated'
    assert report['mission_success'] is None


@pytest.mark.parametrize('outcome', ['failure', 'timeout', 'exception'])
def test_mission_timeout_reports_stop_unconfirmed(outcome):
    class SlowPlanner:
        async def plan(self, context):
            await asyncio.sleep(1)

    class Backend(MockSkills):
        async def execute(self, name, args, snapshot):
            if name == 'stop':
                if outcome == 'timeout':
                    await asyncio.sleep(1)
                if outcome == 'exception':
                    raise OSError('stop disconnected')
            return await super().execute(name, args, snapshot)

    backend = Backend({'stop': [{'event': 'EXPECTED_FAILURE', 'reason': 'not_ack'}]})
    agent = CoEgent(SlowPlanner(), backend, limits=Limits(
        mission_timeout_s=.01, planner_timeout_s=1, skill_timeout_s=.01))
    report = asyncio.run(agent.run('test', initial_state()))
    assert report['failure_reason'] == 'mission_timeout'
    assert report['stop_confirmation']['confirmed'] is False
    assert report['status'] == 'SAFETY_ABORT'
    assert 'stop_unconfirmed' in report['reason']


def test_driver_cancelling_stop_is_unconfirmed_not_mission_cancellation():
    class Driver(MockSkills):
        async def execute(self, name, args, snapshot):
            if name == 'stop':
                raise asyncio.CancelledError
            return await super().execute(name, args, snapshot)

    agent = CoEgent(ScriptedPlanner(['invalid']), Driver(), limits=Limits(max_repairs=0))
    report = asyncio.run(agent.run('test', initial_state()))
    assert report['execution_status'] == 'SAFETY_ABORT'
    assert report['stop_confirmation']['confirmed'] is False


def test_terminal_failure_reason_does_not_leak_previous_replan():
    first = plan({'type': 'replan_required', 'reason': 'old_change', 'constraints': ['wait']},
                 outcome='replan_required')
    agent = CoEgent(ScriptedPlanner([first]), MockSkills())
    report = asyncio.run(agent.run('test', initial_state()))
    assert report['execution_status'] == 'PLANNER_ERROR'
    assert report['failure_reason'] == report['reason']


def test_cancel_twice_still_cleans_up_and_records_confirmation():
    async def run():
        backend = MockSkills(delay_s=.03)
        agent = CoEgent(ScriptedPlanner([plan(skill('inspect_scene'))]), backend)
        task = asyncio.create_task(agent.run('test', initial_state()))
        await asyncio.sleep(.005)
        task.cancel()
        await asyncio.sleep(.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not agent._busy
        assert agent.last_report['stop_confirmation']['confirmed'] is True
        assert agent.last_report['execution_status'] == 'CANCELLED'
        assert backend.calls[-1]['name'] == 'stop'
    asyncio.run(run())


def test_bad_state_does_not_leave_agent_busy():
    agent = CoEgent(ScriptedPlanner([plan(skill('stop'))]), MockSkills())
    state = initial_state()
    state.robot['battery_percent'] = float('nan')
    with pytest.raises(ValueError):
        asyncio.run(agent.run('test', state))
    assert agent._busy is False


def test_legacy_state_migration_never_invents_safety_fields():
    from coegent_contracts.state import migrate_legacy_state
    value = migrate_legacy_state({'battery_percent': 8}, {'battery_percent': 'robot.battery_percent'})
    assert value['robot'] == {'battery_percent': 8}
    with pytest.raises(ValueError, match='unmapped'):
        migrate_legacy_state({'path_status': 'clear'}, {})


def test_shared_prompt_and_dataset_adapter_use_same_schema():
    from Planner_SFT_dataset_v1.planner_data.shared_contract import prepare_record
    example = {'id': 'test', 'user': '배터리 확인', 'state': initial_state().snapshot(),
               'mission_plan': plan(skill('get_battery'))}
    before = deepcopy(example)
    converted = prepare_record(example)
    assert example == before
    assert converted['contract_hash'] == SkillRegistry().fingerprint
    assert converted['messages'][0]['content'] == messages({
        'command': example['user'], 'state': example['state'],
        'skills': SkillRegistry().contracts(), 'schema': PlanValidator(SkillRegistry()).schema})[0]['content']
    assert json.loads(converted['messages'][-1]['content']) == example['mission_plan']


def test_dataset_canonical_adapter_rejects_runtime_precondition_failure():
    from Planner_SFT_dataset_v1.planner_data.shared_contract import prepare_record
    example = {'id': 'bad', 'user': '집어', 'state': initial_state().snapshot(),
               'mission_plan': plan(skill('pick_object', object='box'))}
    with pytest.raises(ValueError, match='target_not_verified'):
        prepare_record(example)


@pytest.mark.parametrize('node', [
    {**skill('get_pose'), 'save_as': 'robot'},
    {'type': 'condition', 'condition': {'source': 'invented.value', 'operator': '==', 'value': True},
     'then': [skill('stop')], 'else': [skill('stop')]},
    {'type': 'abort', 'reason': 'unsafe', 'return_home': False},
])
def test_dataset_and_runtime_share_plan_semantic_rejections(node):
    from Planner_SFT_dataset_v1.planner_data.shared_contract import prepare_record
    candidate = plan(node)
    with pytest.raises(PlanError):
        PlanValidator(SkillRegistry()).prepare(candidate)
    with pytest.raises(ValueError):
        prepare_record({'user': 'test', 'state': initial_state().snapshot(), 'mission_plan': candidate})


def test_default_dataset_validation_requires_shared_contract():
    from Planner_SFT_dataset_v1.planner_data.validator import validate_record
    example = {'id': 'legacy', 'user': '조회', 'state': {'battery_percent': 8},
               'mission_plan': plan(skill('get_battery'))}
    assert any('contract' in error for error in validate_record(example))


def test_training_does_not_silently_reuse_flat_legacy_state():
    from Planner_SFT_training_v1.training_data import to_prompt_completion
    example = {'state': {'battery_percent': 80}, 'messages': [
        {'role': 'user', 'content': 'legacy'}, {'role': 'assistant', 'content': '{}'}]}
    with pytest.raises(ValueError, match='legacy'):
        to_prompt_completion(example)
    assert to_prompt_completion(example, allow_legacy=True)['completion'] == [example['messages'][-1]]


def test_contract_package_has_no_model_or_runtime_imports():
    root = Path(__file__).resolve().parents[1]
    command = [sys.executable, '-c',
               'import sys; sys.path.insert(0,sys.argv[1]); import coegent_contracts; '
               'r=coegent_contracts.SkillRegistry(); assert len(r.skills)==18; '
               'assert not any(k in sys.modules for k in ("torch","transformers","coegent.agent"))', str(root)]
    subprocess.run(command, check=True, capture_output=True, text=True)


def test_check_path_effect_uses_verified_result():
    from coegent_contracts.semantics import apply_effects
    before = initial_state().snapshot()
    before['world']['paths']['A'] = 'blocked'
    after = apply_effects(before, 'check_path', {'target': 'A'}, {'target': 'A', 'path_clear': True})
    assert after['world']['paths']['A'] == 'clear'
    assert before['world']['paths']['A'] == 'blocked'


def test_contract_definition_rejects_unknown_semantic_identifier(tmp_path, monkeypatch):
    from coegent_contracts import registry as module
    path = tmp_path / 'skill_contract.json'
    value = [SkillRegistry().definition('stop')]
    value[0]['effects'].append('arbitrary_script')
    path.write_text(json.dumps(value))
    monkeypatch.setattr(module, 'SCHEMAS', tmp_path)
    module._definitions.cache_clear()
    try:
        with pytest.raises(ValueError, match='identifier'):
            module._definitions()
    finally:
        module._definitions.cache_clear()


def test_model_server_planner_sends_shared_schema_and_does_not_fallback(monkeypatch):
    from urllib.error import HTTPError
    from coegent import planner as module
    captured = []

    def reject(request, timeout):
        captured.append(json.loads(request.data))
        raise HTTPError(request.full_url, 400, 'schema unsupported', {}, None)

    monkeypatch.setattr(module, 'urlopen', reject)
    context = {'command': '정지', 'state': initial_state().snapshot(),
               'skills': SkillRegistry().contracts(), 'schema': PlanValidator(SkillRegistry()).schema}
    with pytest.raises(HTTPError):
        asyncio.run(ModelServerPlanner('http://localhost:1', 'local').plan(context))
    assert len(captured) == 1
    transported = captured[0]['response_format']['json_schema']['schema']
    assert transported['$defs']['predicate']['properties']['value'] != {}
    assert context['schema']['$defs']['predicate']['properties']['value'] == {}
    assert transported['properties'] == context['schema']['properties']


def test_stop_failure_before_replan_is_latched():
    first = plan({'type': 'replan_required', 'reason': 'changed', 'constraints': ['wait']},
                 outcome='replan_required')
    backend = MockSkills({'stop': [{'event': 'EXPECTED_FAILURE', 'reason': 'not_ack'}]})
    planner = ScriptedPlanner([first, plan(skill('navigate_to', target='A'))])
    report = asyncio.run(CoEgent(planner, backend).run('test', initial_state()))
    assert report['stop_confirmation']['confirmed'] is False
    assert report['failure_reason'] == 'changed'
    assert report['planner_calls'] == 1
    assert [call['name'] for call in backend.calls] == ['stop']


def test_mission_timeout_during_replan_stop_is_not_external_cancellation():
    first = plan({'type': 'replan_required', 'reason': 'changed', 'constraints': ['wait']},
                 outcome='replan_required')
    agent = CoEgent(ScriptedPlanner([first]), MockSkills(delay_s=.04),
                    limits=Limits(mission_timeout_s=.01, skill_timeout_s=.1))
    report = asyncio.run(agent.run('test', initial_state()))
    assert report['failure_reason'] == 'mission_timeout'
    assert report['execution_status'] == 'SAFETY_ABORT'


def test_stop_rejects_driver_ignoring_cancellation_without_hanging():
    async def run():
        release = asyncio.Event()

        class ResistantDriver(MockSkills):
            async def execute(self, name, args, snapshot):
                if name == 'stop':
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        await release.wait()
                return await super().execute(name, args, snapshot)

        agent = CoEgent(ScriptedPlanner(['invalid']), ResistantDriver(),
                        limits=Limits(max_repairs=0, skill_timeout_s=.01))
        task = asyncio.create_task(agent.run('test', initial_state()))
        try:
            done, _ = await asyncio.wait({task}, timeout=.1)
            assert done, 'stop cleanup exceeded its own deadline'
            report = task.result()
            assert report['stop_confirmation']['confirmed'] is False
            assert report['execution_status'] == 'SAFETY_ABORT'
        finally:
            release.set()
            await task
            await asyncio.sleep(.01)
    asyncio.run(run())
