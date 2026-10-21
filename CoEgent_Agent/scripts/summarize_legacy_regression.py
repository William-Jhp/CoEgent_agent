"""Compare historical test identities and mock behavior, without changing expectations."""

from datetime import datetime
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "CoEgent_Agent/results"
OUT = RESULTS / "legacy_regression_20261007"


def test_summary(path):
    suites = ET.parse(path).getroot()
    cases = list(suites.iter("testcase"))
    return {
        "file": str(path.relative_to(ROOT)),
        "count": len(cases),
        "failures": sum(len(c.findall("failure")) for c in cases),
        "errors": sum(len(c.findall("error")) for c in cases),
        "skipped": sum(len(c.findall("skipped")) for c in cases),
        "duration_s": sum(float(s.attrib["time"]) for s in suites.findall("testsuite")),
    }, {(c.attrib["classname"], c.attrib["name"]) for c in cases}


def behavior(row):
    report = row["report"]
    return {
        "calls": row["actual_calls"],
        "dispatch_args": [{"skill": e["skill"], "args": e["args"]} for e in report["trace"]
                          if e["kind"] == "skill_start"],
        "robot": report["state"]["robot"],
        "world": report["state"]["world"],
        "mission": report["state"].get("mission", {}),
        "observations": report["state"].get("observations", {}),
        **{key: report.get(key) for key in
           ("status", "completed_steps", "planner_calls", "repairs", "replans", "mission_success")},
    }


def main():
    tests = []
    for baseline, current, count in [
        (ROOT / "CoEgent_Agent_Benchmark_v1/analysis/dynamic/tests.xml", OUT / "legacy71.xml", 71),
        (RESULTS / "shared_contract_v1_tests.xml", OUT / "pre_boundaries126.xml", 126),
    ]:
        old, old_ids = test_summary(baseline)
        new, new_ids = test_summary(current)
        check = {"baseline": old, "rerun": new, "same_test_ids": old_ids == new_ids,
                 "missing": sorted(old_ids - new_ids), "added": sorted(new_ids - old_ids)}
        tests.append(check)
        assert old["count"] == new["count"] == count and old_ids == new_ids, check
        assert new["failures"] == new["errors"] == new["skipped"] == 0, check

    current = json.loads((OUT / "mock_suite.json").read_text())
    assert current["count"] == current["passed"] == 12
    current_rows = {r["scenario"]: r for r in current["rows"]}
    comparisons = []
    for name in ("mock_suite.json", "shared_contract_v1_mock_suite.json"):
        baseline = json.loads((RESULTS / name).read_text())
        assert {r["scenario"] for r in baseline["rows"]} == current_rows.keys()
        rows = []
        for old in baseline["rows"]:
            before, after = behavior(old), behavior(current_rows[old["scenario"]])
            changes = [key for key in before if before[key] != after[key]]
            rows.append({"scenario": old["scenario"], "baseline_status": before["status"],
                         "unchanged": not changes, "changed_fields": changes})
        comparisons.append({"baseline": name, "rows": rows,
                            "normal_success_count": sum(r["baseline_status"] == "SUCCESS" for r in rows)})
        assert all(r["unchanged"] for r in rows), comparisons[-1]

    sources = [p for directory in ("coegent", "coegent_contracts", "tests")
               for p in (ROOT / "CoEgent_Agent" / directory).rglob("*.py")]
    summary = {
        "checked_at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
        "scope": "Existing tests only; CPU mock, no new boundary tests or model/GPU/Jetson execution",
        "tests": tests,
        "mock_suite": {"count": current["count"], "passed": current["passed"]},
        "behavior_comparisons": comparisons,
        "excluded_from_behavior_comparison": ["timestamps", "elapsed_s", "new request/goal/fault metadata"],
        "source_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in sorted(sources)},
        "limitation": "Historical test IDs verified, not historical assertion source recovery. Mock fixtures do not prove all possible mission behavior or LLM accuracy.",
    }
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"legacy": tests[0]["rerun"]["count"], "pre_boundaries": tests[1]["rerun"]["count"],
                      "unchanged_scenarios_per_baseline": len(comparisons[0]["rows"]), "out": str(OUT / "summary.json")}))


if __name__ == "__main__":
    main()
