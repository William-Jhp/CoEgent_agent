from dataclasses import replace
import subprocess
import sys
import os

import pytest


def test_request_cache_revision_and_capacity():
    from coegent.admission import MissionRequest, RobotAdmission
    guard = RobotAdmission(utc_now=lambda: 100, monotonic_now=lambda: 10, capacity=1)
    req = MissionRequest("m", 1, 120, "go")
    assert guard.admit(req)["kind"] == "accepted"
    assert guard.admit(req)["kind"] == "replayed"
    assert guard.admit(replace(req, command="other"))["reason"] == "request_payload_conflict"
    assert guard.admit(replace(req, revision=2))["reason"] == "robot_busy"
    guard.finish(req, {"execution_status": "SUCCESS"})
    assert guard.admit(replace(req, mission_id="new"))["reason"] == "request_cache_full"


def test_clock_rollback_does_not_extend_request():
    from coegent.admission import MissionRequest, RobotAdmission
    clock = {"utc": 100, "mono": 10}
    guard = RobotAdmission(utc_now=lambda: clock["utc"], monotonic_now=lambda: clock["mono"])
    req = MissionRequest("m", 1, 110, "go")
    guard.admit(req)
    clock.update(utc=90, mono=21)
    with pytest.raises(Exception, match="request_expired"):
        guard.check_active(req)


@pytest.mark.parametrize("changes", [{"revision": True}, {"revision": 0},
    {"expires_at": float("nan")}, {"expires_at": float("inf")}, {"mission_id": ""}])
def test_bad_request_rejected(changes):
    from coegent.admission import MissionRequest
    with pytest.raises(ValueError):
        MissionRequest(**{"mission_id": "m", "revision": 1, "expires_at": 120, "command": "go", **changes})


def test_expired_lower_revision_and_fault_not_erased():
    from coegent.admission import MissionRequest, RobotAdmission
    guard = RobotAdmission(utc_now=lambda: 100, monotonic_now=lambda: 10)
    req = MissionRequest("m", 1, 120, "go")
    assert guard.admit(replace(req, expires_at=99))["reason"] == "request_expired"
    guard.admit(req)
    guard.finish(req, {})
    req = replace(req, revision=2)
    guard.admit(req)
    guard.finish(req, {})
    assert guard.admit(replace(req, revision=1))["reason"] == "stale_revision"
    guard.latch_fault("no_ack")
    assert guard.admit(replace(req, revision=3))["reason"] == "robot_fault_latched"
    assert guard.fault_reason == "no_ack"


def test_file_lock_rejects_second_process(tmp_path):
    from coegent.admission import RobotAdmission
    from coegent.skills import MockSkills
    guard = RobotAdmission.for_backend(MockSkills(), robot_id="orin-1", lock_dir=tmp_path)
    source = ("from coegent.admission import RobotAdmission; from coegent.skills import MockSkills; "
              f"RobotAdmission.for_backend(MockSkills(), robot_id='orin-1', lock_dir={str(tmp_path)!r})")
    result = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True)
    assert result.returncode != 0 and "robot_process_owned" in result.stderr
    guard.close()
    result = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_clock_rollback_does_not_resurrect_pruned_request():
    from coegent.admission import MissionRequest, RobotAdmission
    clock = {"utc": 100, "mono": 10}
    guard = RobotAdmission(utc_now=lambda: clock["utc"], monotonic_now=lambda: clock["mono"])
    old = MissionRequest("old", 1, 110, "go")
    guard.admit(old)
    guard.finish(old, {})
    clock.update(utc=90, mono=21)
    other = MissionRequest("other", 1, 200, "go")
    guard.admit(other)
    guard.finish(other, {})
    assert guard.admit(old)["reason"] == "request_expired"


def test_forked_guard_cannot_admit(tmp_path):
    from coegent.admission import MissionRequest, RobotAdmission
    from coegent.skills import MockSkills
    from time import time
    backend = MockSkills()
    guard = RobotAdmission.for_backend(backend, robot_id="fork-test", lock_dir=tmp_path)
    assert guard.begin_management()
    guard.end_management(safe=True)
    child = os.fork()
    if child == 0:
        try:
            guard.admit(MissionRequest("child", 1, time() + 10, "go"))
        except RuntimeError:
            os._exit(0)
        os._exit(1)
    _, status = os.waitpid(child, 0)
    guard.close()
    assert os.waitstatus_to_exitcode(status) == 0


def test_wall_clock_rollback_cannot_revive_utc_pruned_record():
    from coegent.admission import MissionRequest, RobotAdmission
    clock = {"utc": 100, "mono": 10}
    guard = RobotAdmission(utc_now=lambda: clock["utc"], monotonic_now=lambda: clock["mono"])
    old = MissionRequest("old", 1, 110, "go")
    guard.admit(old)
    guard.finish(old, {})
    clock.update(utc=120, mono=30)
    other = MissionRequest("other", 1, 200, "go")
    guard.admit(other)
    guard.finish(other, {})
    clock.update(utc=90, mono=31)
    assert guard.admit(old)["reason"] == "request_expired"


def test_new_revision_carries_history_before_pruning_expired_record():
    from coegent.admission import MissionRequest, RobotAdmission
    clock = {"utc": 100, "mono": 10}
    guard = RobotAdmission(utc_now=lambda: clock["utc"], monotonic_now=lambda: clock["mono"], capacity=1)
    req = MissionRequest("m", 1, 110, "go")
    guard.admit(req)
    step = {"name": "rotate", "args": {"angle_deg": 30}}
    guard.finish(req, {"completed_steps": [step]})
    clock.update(utc=120, mono=30)
    second = replace(req, revision=2, expires_at=200)
    accepted = guard.admit(second)
    assert accepted["previous_report"]["mission_completed_steps"] == [step]
    guard.finish(second, {"completed_steps": []})
    assert guard.admit(second)["report"]["mission_completed_steps"] == [step]
