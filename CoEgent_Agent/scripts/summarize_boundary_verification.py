"""Summarize CPU regression evidence without running models or rewriting old results."""

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET


def main():
    root = Path(__file__).resolve().parents[1]
    results = root / "results"
    xml = results / "mission_executive_boundaries_tests.xml"
    suite = ET.parse(xml).getroot()[0]
    mock = json.loads((results / "mission_executive_boundaries_mock_suite.json").read_text())
    value = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "CPU mock, no model inference or physical robot execution",
        "verification_commands": {
            "pytest": {
                "environment": {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
                "sys_path_prefix": [".", "CoEgent_Agent"],
                "args": ["CoEgent_Agent/tests", "Planner_SFT_dataset_v1/tests",
                         "Planner_SFT_training_v1/tests", "CoEgent_Agent_Benchmark_v1/tests",
                         "-q", "--tb=short", f"--junitxml={xml.relative_to(root.parent)}"],
            },
            "ruff": ["uv", "tool", "run", "--offline", "--from", "ruff==0.15.6", "ruff", "check",
                     "--isolated", "--select", "E4,E7,E9,F,B", "CoEgent_Agent/coegent",
                     "CoEgent_Agent/coegent_contracts", "CoEgent_Agent/tests",
                     "CoEgent_Agent/scripts/summarize_boundary_verification.py"],
            "mock_cli": "coegent suite --out CoEgent_Agent/results/mission_executive_boundaries_mock_suite.json",
        },
        "pytest": dict(suite.attrib),
        "test_classes": dict(Counter(c.attrib["classname"] for c in suite.iter("testcase"))),
        "mock_suite": {"count": mock["count"], "passed": mock["passed"]},
        "source_sha256": {
            str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for directory in ("coegent", "coegent_contracts")
            for p in sorted((root / directory).rglob("*.py"))
        },
        "artifacts": {xml.name: hashlib.sha256(xml.read_bytes()).hexdigest()},
        "unverified": ["physical stop", "real sensor adapters", "Jetson latency", "model accuracy after runtime changes"],
    }
    out = results / "mission_executive_boundaries_verification.json"
    out.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"tests": suite.attrib["tests"], "failures": suite.attrib["failures"], "out": str(out)}))


if __name__ == "__main__":
    main()
