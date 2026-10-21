import json
from pathlib import Path
import sys
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

def test_completed_request_is_replayed_and_changed_payload_is_rejected(tmp_path):
    from a2_execution_adapter.journal import Journal
    with Journal(tmp_path,'robot') as j:
        assert j.begin('mission',1,{'plan':1},motion=True) is None
        j.finish('mission',1,{'status':'SUCCESS'})
        assert j.begin('mission',1,{'plan':1},motion=True)=={'status':'SUCCESS','replayed':True}
        with pytest.raises(ValueError,match='conflict'):j.begin('mission',1,{'plan':2},motion=True)

def test_in_doubt_request_blocks_new_motion_after_process_restart(tmp_path):
    from a2_execution_adapter.journal import Journal
    with Journal(tmp_path,'robot') as j:j.begin('interrupted',1,{'plan':1},motion=True)
    with Journal(tmp_path,'robot') as j:
        with pytest.raises(ValueError,match='in_doubt'):j.begin('new',1,{'plan':2},motion=True)
        assert j.begin('readonly',1,{'plan':3},motion=False) is None

def test_robot_lock_rejects_concurrent_worker(tmp_path):
    from a2_execution_adapter.journal import Journal
    with Journal(tmp_path,'robot'):
        with pytest.raises(ValueError,match='busy'):
            with Journal(tmp_path,'robot'):pass

def test_old_revision_cannot_run_after_a_newer_revision(tmp_path):
    from a2_execution_adapter.journal import Journal
    with Journal(tmp_path,'robot') as j:
        j.begin('mission',2,{'plan':2},motion=True)
        j.finish('mission',2,{'status':'SUCCESS'})
        with pytest.raises(ValueError,match='stale_revision'):
            j.begin('mission',1,{'plan':1},motion=True)
