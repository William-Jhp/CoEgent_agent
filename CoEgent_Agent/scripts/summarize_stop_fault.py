"""Record actual stop/fault test results, including failures; do not change policy."""

from datetime import datetime
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results/stop_fault_20261007"


def read_tests(path):
    document = ET.parse(path).getroot()
    rows = []
    for case in document.iter("testcase"):
        problem = case.find("failure")
        if problem is None:
            problem = case.find("error")
        rows.append({"class": case.attrib["classname"], "name": case.attrib["name"],
                     "status": "failed" if problem is not None else "skipped" if case.find("skipped") is not None else "passed",
                     "failure": problem.attrib.get("message") if problem is not None else None})
    return {"count": len(rows), "passed": sum(r["status"] == "passed" for r in rows),
            "failed": sum(r["status"] == "failed" for r in rows),
            "skipped": sum(r["status"] == "skipped" for r in rows), "tests": rows}


def main():
    initial = read_tests(OUT / "initial/tests.xml")
    full = read_tests(OUT / "full_regression/tests.xml")
    previous = json.loads((ROOT / "results/mission_executive_boundaries_verification.json").read_text())
    sources = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
               for directory in ("coegent", "coegent_contracts")
               for p in sorted((ROOT / directory).rglob("*.py"))}
    changed = [name for name in sources.keys() | previous["source_sha256"].keys()
               if sources.get(name) != previous["source_sha256"].get(name)]
    cases = []
    for path in sorted((OUT / "initial").glob("*.json")):
        value = json.loads(path.read_text())
        first, second = value["mission_a"], value["mission_b"]
        cases.append({"case": value["case"], "trace_file": str(path.relative_to(OUT)),
                      "execution_status": first["execution_status"], "reason": first["reason"],
                      "failure_reason": first["failure_reason"], "stop_confirmation": first["stop_confirmation"],
                      "fault": first["robot_fault"], "next_status": second["execution_status"],
                      "next_backend_execute_calls": value["mission_b_backend_execute_delta"],
                      "simulated_actual_state": value["simulated_actual_state"],
                      "pending_tasks": value["pending_driver_or_mission_tasks"]})
    legacy_xml = ROOT.parent / "CoEgent_Agent_Benchmark_v1/analysis/dynamic/tests.xml"
    legacy = read_tests(legacy_xml)
    full_by_id = {(r["class"], r["name"]): r for r in full["tests"]}
    legacy_rechecks = [full_by_id.get((r["class"], r["name"])) for r in legacy["tests"]]
    current_stop = [r for r in full["tests"] if r["class"].endswith("test_stop_fault")]
    value = {
        "checked_at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
        "scope": "Stop/fault group 1; CPU driver simulation, real runtime classes; no physical/GPU/LLM execution",
        "gate": "failed" if initial["failed"] or initial["skipped"] else "passed",
        "initial_group": initial,
        "full_regression": full,
        "old_71_still_pass": len(legacy_rechecks) == 71 and all(r and r["status"] == "passed" for r in legacy_rechecks),
        "previous_183_still_pass": len(full["tests"]) - len(current_stop) == 183 and
                                  all(r["status"] == "passed" for r in full["tests"] if r not in current_stop),
        "case_observations": cases,
        "product_sources_unchanged": not changed,
        "changed_product_sources": sorted(changed), "source_sha256": sources,
        "test_sha256": hashlib.sha256((ROOT / "tests/test_stop_fault.py").read_bytes()).hexdigest(),
        "commands": {
            "environment": {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
            "sys_path_prefix": [".", "CoEgent_Agent"],
            "initial_pytest_args": ["CoEgent_Agent/tests/test_stop_fault.py", "-v", "--tb=short"],
            "full_pytest_args": ["CoEgent_Agent/tests", "Planner_SFT_dataset_v1/tests",
                                 "Planner_SFT_training_v1/tests", "CoEgent_Agent_Benchmark_v1/tests", "-q", "--tb=short"],
            "artifact_dirs": {"initial": "CoEgent_Agent/results/stop_fault_20261007/initial",
                              "full": "CoEgent_Agent/results/stop_fault_20261007/full_regression"},
        },
        "limitations": ["Snapshot report state is not live physical evidence", "Only cooperative driver cancellation simulated",
                        "Read-only driver exception requirement currently fails; not skipped or xfailed"],
    }
    (OUT / "summary.json").write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"gate": value["gate"], "group_passed": initial["passed"], "group_failed": initial["failed"],
                      "full_passed": full["passed"], "full_failed": full["failed"],
                      "product_unchanged": value["product_sources_unchanged"]}))


if __name__ == "__main__":
    main()
