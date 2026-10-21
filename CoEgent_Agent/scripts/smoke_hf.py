"""Two new mock-world integration examples; not a planner benchmark."""

import asyncio
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coegent.agent import CoEgent
from coegent.cli import write_result
from coegent.planner import TransformersModelPlanner
from coegent.scenarios import initial_state
from coegent.skills import MockSkills
from coegent.types import Limits


ROOT = Path(__file__).resolve().parents[2]


async def main(out):
    model = ROOT / "models/robot-agent-sft-merged/gemma4-e2b-robot-sft-v0.4-merged"
    adapter = ROOT / "Planner_SFT_training_v1/models/gemma4-e2b-sft-v0.4_planner-sft-v1_attention-mlp_r16_a32_lr1e-4_ep3_bs2x8/final_adapter"
    planner = TransformersModelPlanner(str(model), str(adapter), max_tokens=1024)
    rows = []
    commands = ["현재 배터리를 확인하고 현재 위치를 확인해.",
                "A로 가서 red_valve를 검사하고 열려 있으면 닫은 다음 HOME으로 복귀해."]
    for command in commands:
        backend = MockSkills()
        report = await CoEgent(planner, backend, limits=Limits(max_replans=0, max_repairs=1)).run(command, initial_state())
        report["actual_calls"] = backend.calls
        rows.append(report)
        write_result(out,
                     {"model": str(model), "adapter": str(adapter), "kind": "integration_smoke_not_benchmark",
                      "backend": "MockSkills", "rows": rows})
        print(command, report["status"], report["reason"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=ROOT / "CoEgent_Agent/results/hf_integration_smoke.json")
    asyncio.run(main(parser.parse_args().out))
