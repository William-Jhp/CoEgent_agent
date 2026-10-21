import json
import subprocess
import sys
from time import time

from coegent.scenarios import plan, skill


def test_cli_request_has_goals_and_no_inference(tmp_path):
    path = tmp_path / "request.json"
    out = tmp_path / "out.json"
    path.write_text(json.dumps({"mission_id": "cli-1", "revision": 1, "expires_at": time() + 30,
        "command": "check home", "plan": plan(skill("get_pose")),
        "goals": [{"kind": "at_location", "target": "HOME"}]}))
    result = subprocess.run([sys.executable, "-m", "coegent", "request", "--request-file", str(path),
                             "--out", str(out)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    report = json.loads(out.read_text())
    assert report["planner_calls"] == 0 and report["mission_success"] is True


def test_cli_request_rejects_policy_injection(tmp_path):
    path = tmp_path / "request.json"
    path.write_text(json.dumps({"mission_id": "cli-1", "revision": 1, "expires_at": time() + 30,
        "command": "unsafe", "allowed_skills": ["rotate"], "plan": plan(skill("get_pose"))}))
    result = subprocess.run([sys.executable, "-m", "coegent", "request", "--request-file", str(path)],
                             capture_output=True, text=True)
    assert result.returncode != 0
    assert "unsupported request fields" in result.stderr
