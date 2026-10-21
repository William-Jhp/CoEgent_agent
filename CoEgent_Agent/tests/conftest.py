"""Keep automatic catalog writes out of the user's persistent database."""
import pytest


@pytest.fixture(autouse=True)
def isolated_plan_database(tmp_path, monkeypatch):
    monkeypatch.setenv('COEGENT_PLAN_DB', str(tmp_path/'default-plans.sqlite3'))
