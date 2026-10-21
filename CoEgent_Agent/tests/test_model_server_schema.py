"""The model server must allow the same predicate values as the validator."""
from copy import deepcopy

from jsonschema import Draft202012Validator
import pytest

from coegent.planner import response_format
from coegent.validation import PlanValidator
from coegent.registry import SkillRegistry


def predicate_schema(schema):
    return {'$ref': '#/$defs/predicate', '$defs': schema['$defs']}


def test_free_predicate_value_is_explicit_for_grammar_servers():
    schema = PlanValidator(SkillRegistry()).schema
    before = deepcopy(schema)
    transported = response_format(schema)['json_schema']['schema']
    # The deployed decoder translates an empty schema to object-only grammar.
    assert transported['$defs']['predicate']['properties']['value'] != {}
    assert schema == before


@pytest.mark.parametrize('value', [20, -0.5, 'open', True, False, None,
                                  [], [20, 'open', False, None], {},
                                  {'nested': [{'value': 20}, ['open', None]]}])
def test_transport_preserves_scalar_and_recursive_json_values(value):
    original = PlanValidator(SkillRegistry()).schema
    transported = response_format(original)['json_schema']['schema']
    predicate = {'source': 'battery_check.battery_percent', 'operator': '<', 'value': value}
    assert Draft202012Validator(predicate_schema(original)).is_valid(predicate)
    assert Draft202012Validator(predicate_schema(transported)).is_valid(predicate)


def test_transport_does_not_relax_predicate_structure_or_operator():
    schema = response_format(PlanValidator(SkillRegistry()).schema)['json_schema']['schema']
    validator = Draft202012Validator(predicate_schema(schema))
    assert not validator.is_valid({'source': 'x', 'operator': 'unknown', 'value': 20})
    assert not validator.is_valid({'source': '', 'operator': '<', 'value': 20})
    assert not validator.is_valid({'source': 'x', 'operator': '<'})
    assert not validator.is_valid({'source': 'x', 'operator': '<', 'value': 20, 'extra': True})


def test_schema_without_free_predicate_is_unchanged_and_copied():
    schema = {'type': 'object', 'properties': {'value': {'type': 'number'}},
              'default': {}, 'enum': [{}], 'examples': [{'value': {}}]}
    result = response_format(schema)['json_schema']['schema']
    assert result == schema
    result['properties']['value']['type'] = 'string'
    assert schema['properties']['value']['type'] == 'number'


@pytest.mark.parametrize('schema', [True, False])
def test_boolean_schemas_are_preserved(schema):
    assert response_format(schema)['json_schema']['schema'] is schema


def test_explicit_predicate_constraint_remains_number_only():
    schema = deepcopy(PlanValidator(SkillRegistry()).schema)
    schema['$defs']['predicate']['properties']['value'] = {'type': 'number'}
    result = response_format(schema)['json_schema']['schema']
    assert result == schema
    validator = Draft202012Validator(predicate_schema(result))
    assert validator.is_valid({'source': 'x', 'operator': '<', 'value': 20})
    assert not validator.is_valid({'source': 'x', 'operator': '<', 'value': '20'})


def test_existing_definitions_are_not_overwritten():
    schema = deepcopy(PlanValidator(SkillRegistry()).schema)
    schema['$defs']['coegent_json_value'] = {'const': 'keep'}
    result = response_format(schema)['json_schema']['schema']
    assert result['$defs']['coegent_json_value'] == {'const': 'keep'}
    assert result['$defs']['predicate']['properties']['value'] != {}
    assert Draft202012Validator(predicate_schema(result)).is_valid(
        {'source': 'x', 'operator': '==', 'value': {'nested': [True, 'open', 20]}})
