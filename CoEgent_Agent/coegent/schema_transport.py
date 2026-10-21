"""Lossless model-server representation of free-form predicate JSON values."""
from copy import deepcopy


def model_server_schema(schema):
    """Keep the validator contract while avoiding object-only empty-schema decoding.

    Some JSON-schema-to-grammar servers interpret {} as object, including when
    nested under predicate.value. Explicit recursive alternatives preserve all
    JSON values. Only that schema position is adapted; prompts and literals stay
    untouched, and the caller's canonical schema is never mutated.
    """
    result = deepcopy(schema)
    if not isinstance(result, dict):
        return result
    definitions = result.get('$defs', {})
    properties = definitions.get('predicate', {}).get('properties', {})
    if properties.get('value') != {}:
        return result

    name = 'coegent_json_value'
    suffix = 0
    while name in definitions:
        suffix += 1
        name = f'coegent_json_value_{suffix}'
    reference = {'$ref': f'#/$defs/{name}'}
    definitions[name] = {'anyOf': [
        {'type': 'null'}, {'type': 'boolean'}, {'type': 'number'}, {'type': 'string'},
        {'type': 'array', 'items': reference},
        {'type': 'object', 'additionalProperties': reference},
    ]}
    properties['value'] = reference
    return result
