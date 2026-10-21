"""Offline preflight for the hand-authored live evaluation catalogs."""
import json
from pathlib import Path

from coegent import PlanStore


def test_all_frozen_case_catalogs_validate_and_reuse_labels_reference_a_plan(tmp_path):
    corpus=Path(__file__).resolve().parents[1]/'evaluations/planner_selector/cases.json'
    cases=json.loads(corpus.read_text())['cases']
    assert len({case['id'] for case in cases})==len(cases)
    for case in cases:
        store=PlanStore(tmp_path/(case['id']+'.sqlite3'))
        keys=set()
        for row in case['catalog']:
            store.add(row['plan'],command=row['command'])
            keys.add(row['key'])
        if case['expected']=='reuse':
            assert case['expected_plan'] in keys
        else:
            assert case['expected_plan'] is None
