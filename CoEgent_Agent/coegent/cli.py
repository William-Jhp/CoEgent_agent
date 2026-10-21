"""CLI with a mock local skill backend and a selectable executor adapter."""

import argparse
import asyncio
import json
import sqlite3
from pathlib import Path

from .agent import CoEgent
from .admission import MissionRequest
from .goals import Goal
from coegent_contracts import RuntimeConfig
from .planner import InprocessModelPlanner, TransformersModelPlanner, ModelServerPlanner, ScriptedPlanner
from .planner_selector import PlannerSelector
from .planning import PlanGenerator
from .execution import PlanExecutor
from .executor_adapters import default_registry
from .plan_store import PlanStore
from .plan_catalog_cli import add_catalog_parser, run_catalog
from .scenarios import initial_state, scenarios
from .skills import MockSkills
from .types import Limits, State
from .validation import strict_json
from .tool_profiles import profile_options, load_profile, profile_db
from .inspector import ReportMessage, FileEvidenceProvider, build_inspector


def write_result(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


async def run_case(name):
    case = scenarios()[name]
    backend = MockSkills(case["outcomes"])
    agent = CoEgent(ScriptedPlanner(case["plans"]), backend, auto_save_plans=False)
    report = await agent.run(case["command"], case["state"])
    actual = [c["name"] for c in backend.calls]
    state = State.from_dict(report["state"])
    checks = {key: state.resolve(key) == value for key, value in case["checks"].items()}
    return {"scenario": name, "expected_status": case["expected_status"],
            "actual_calls": actual, "expected_calls": case["expected_calls"], "state_checks": checks,
            "passed": report["status"] == case["expected_status"] and actual == case["expected_calls"] and all(checks.values()),
            "report": report}


def model_planner(kind, args, *, selector=False):
    model = args.selector_model if selector else args.model
    endpoint = args.selector_endpoint if selector else args.endpoint
    adapter = args.selector_adapter if selector else args.adapter
    tokens = args.selector_max_tokens if selector else args.max_tokens
    if not model and kind == 'http':
        raise ValueError('model name/path is required for the selected model backend')
    if kind == 'http':
        if not endpoint:
            raise ValueError('HTTP model backend requires an endpoint')
        return ModelServerPlanner(endpoint, model, args.planner_timeout, tokens,
                                  constrained=args.generation_mode != 'unconstrained')
    if kind == 'inprocess':
        return InprocessModelPlanner(model, adapter, tokens, args.planner_timeout,
                                     constrained=args.generation_mode != 'unconstrained')
    return TransformersModelPlanner(model, adapter, tokens, args.planner_timeout,
                                    constrained=args.generation_mode == 'json_schema')


def build_planners(args, *, tool_profile=None):
    store_args = {"registry": tool_profile.registry} if tool_profile else {}
    db = profile_db(tool_profile,args.plan_db) if tool_profile else args.plan_db
    if args.planner != 'db' and args.plan_id:
        raise ValueError('--plan-id requires --planner db')
    if args.planner != 'auto' and (args.selector_backend != 'same' or args.selector_model or
                                    args.selector_endpoint or args.selector_adapter):
        raise ValueError('selector settings require --planner auto')
    if args.planner == 'db':
        return ScriptedPlanner.from_store(PlanStore(db, **store_args), args.plan_id or []), None
    if args.planner == 'file':
        if not args.plan_file:
            raise ValueError('--plan-file is required for file planner')
        return ScriptedPlanner([Path(args.plan_file).read_text()]), None
    if args.planner != 'auto':
        return model_planner(args.planner, args), None
    if args.selector_backend == 'same' and (args.selector_model or args.selector_endpoint or args.selector_adapter):
        raise ValueError('selector model overrides require --selector-backend http, inprocess or hf')
    planner = model_planner(args.generation_planner, args)
    judge = planner if args.selector_backend == 'same' else model_planner(args.selector_backend, args, selector=True)
    return planner, PlannerSelector(PlanStore(db, **store_args), judge, max_candidates=args.selector_max_candidates,
                                    max_tokens=args.selector_max_tokens, max_prompt_bytes=args.selector_max_prompt_bytes)


def print_planning_summary(report):
    summary = {k: report[k] for k in ('status', 'reason', 'planner_calls', 'elapsed_s')}
    if report.get('question'):
        summary['question'] = report['question']
    print(json.dumps(summary, ensure_ascii=False))


async def main_async(args):
    if args.mode == 'graph':
        from .workflow import graph_mermaid
        diagram=graph_mermaid(args.flow)
        if args.out:
            path=Path(args.out);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(diagram+'\n')
        print(diagram)
        return 0
    if args.mode == 'inspect':
        message = ReportMessage.from_dict(strict_json(Path(args.report).read_text()))
        inspector = build_inspector(strict_json(Path(args.inspector_config).read_text()),
                                    FileEvidenceProvider(args.inspection_evidence))
        decision = await inspector.inspect(message)
        result = {**decision.to_dict(), 'completion_report': {
            'mission_id': message.mission_id, 'revision': message.revision,
            'verified': decision.verified, 'next_action': decision.decision,
            'reason': decision.reason}}
        write_result(args.out, result)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if decision.decision in {'completed', 'continue'} else 2
    if args.mode == 'plans':
        return run_catalog(args)
    if args.mode == 'tools':
        profile = load_profile(args)
        if profile is None:
            from .registry import SkillRegistry
            reg=SkillRegistry()
            data={'profile':'mock','tools':[reg.definition(n) for n in reg.skills]}
        else: data=profile.catalog()
        if args.out: write_result(args.out,data)
        print(json.dumps(data,ensure_ascii=False,indent=2))
        return 0
    if args.mode == 'executors':
        print(json.dumps({'executors': default_registry().names}))
        return 0
    if args.mode == "suite":
        rows = [await run_case(name) for name in scenarios()]
        result = {"kind": "mock_runtime_contract_suite", "llm_evaluated": False,
                  "count": len(rows), "passed": sum(row["passed"] for row in rows), "rows": rows}
        write_result(args.out, result)
        print(json.dumps({k: v for k, v in result.items() if k != "rows"}))
        return 0 if result["passed"] == result["count"] else 1
    if args.mode == "demo":
        result = await run_case(args.scenario)
        write_result(args.out, result)
        print(json.dumps({"scenario": args.scenario, "status": result["report"]["status"],
                          "passed": result["passed"], "calls": result["actual_calls"], "out": args.out}, ensure_ascii=False))
        return 0 if result["passed"] else 1
    tool_profile = load_profile(args)
    if tool_profile is not None and args.mode != 'plan':
        if getattr(args, 'executor', None) != 'a2-isaac':
            raise ValueError('a2-isaac is plan-only without explicit --executor a2-isaac')
        default_registry().resolve('a2-isaac')
        from a2_execution_adapter.integration import run_cli
        return await run_cli(args, tool_profile)
    selection = {}
    if args.mode in {'run', 'request'}:
        registry = default_registry()
        registry.resolve(args.executor)
        options = strict_json(Path(args.executor_options).read_text()) if args.executor_options else {}
        selection = {'executor': args.executor, 'executor_options': options, 'executor_registry': registry,
                     'plan_store': PlanStore(args.plan_db), 'auto_save_plans': not args.no_auto_save_plans}
        if args.inspector_config:
            if not args.inspection_evidence:
                raise ValueError('native Inspector requires an independent --inspection-evidence file')
            selection['inspector'] = build_inspector(strict_json(Path(args.inspector_config).read_text()),
                                                    FileEvidenceProvider(args.inspection_evidence))
        elif args.inspection_evidence:
            raise ValueError('--inspection-evidence requires --inspector-config')
    state = State.from_dict(strict_json(Path(args.state).read_text())) if args.state else (State() if tool_profile else initial_state())
    if args.mode == "request":
        payload = strict_json(Path(args.request_file).read_text())
        fields = {"mission_id", "revision", "expires_at", "command", "plan", "goals"}
        if not isinstance(payload, dict) or set(payload) - fields:
            raise ValueError("unsupported request fields")
        if args.plan_id is not None:
            if payload.get('plan') is not None:
                raise ValueError('cannot provide both inline plan and --plan-id')
            payload['plan'] = selection['plan_store'].get(args.plan_id)['plan']
        try:
            payload["goals"] = tuple(Goal(**value) for value in payload.get("goals", []))
            request = MissionRequest(**payload)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid mission request: {exc}") from exc
        config = RuntimeConfig(state_mode=args.state_mode,
                               max_observation_age_s=args.max_observation_age,
                               max_inspection_age_s=args.max_inspection_age)
        report = await PlanExecutor(MockSkills(), config=config, **selection).execute_request(request, state)
        report["run_metadata"] = {"backend": "MockSkills", "planner": "provided_plan", "llm_evaluated": False}
        write_result(args.out, report)
        print(json.dumps({k: report[k] for k in ("execution_status", "goal_status", "reason")}, ensure_ascii=False))
        return 0 if report["status"] == "SUCCESS" and report["goal_status"] not in {"unknown", "unmet"} else 2
    planner, selector = build_planners(args, tool_profile=tool_profile)
    if args.mode == "plan":
        config = RuntimeConfig(max_repairs=args.max_repairs, planner_timeout_s=args.planner_timeout)
        report = await PlanGenerator(planner, config=config, planner_selector=selector, tool_profile=tool_profile).generate(args.command, state)
        report["run_metadata"] = {"planner": args.planner, "model": getattr(planner, 'model_path', args.model), "adapter": args.adapter,
                                  "backend": None, "max_tokens": args.max_tokens,
                                  "state_source": "provided_snapshot" if args.state else ("unknown_snapshot" if tool_profile else "mock_fixture")}
        write_result(args.out, report)
        print_planning_summary(report)
        if tool_profile:
            print(json.dumps({"plan":report["plan"],"api_calls":report["api_calls"],"execution_ready":False},ensure_ascii=False,indent=2))
        print(f"Saved: {args.out}")
        return 0 if report["status"] == "PLAN_READY" else 2
    limits = Limits(max_replans=args.max_replans, max_repairs=args.max_repairs,
                    planner_timeout_s=args.planner_timeout, mission_timeout_s=args.mission_timeout)
    report = await CoEgent(planner, MockSkills(), limits=limits, planner_selector=selector, **selection).run(args.command, state)
    report["run_metadata"] = {"planner": args.planner, "model": getattr(planner, 'model_path', args.model), "adapter": args.adapter,
                              "backend": "MockSkills", "max_tokens": args.max_tokens}
    write_result(args.out, report)
    print_planning_summary(report)
    print(f"Saved: {args.out}")
    return 0 if report["status"] == "SUCCESS" else 2


def main():
    parser = argparse.ArgumentParser(description="CoEgent Phase 1: planner + deterministic mock runtime")
    subs = parser.add_subparsers(dest="mode", required=True)
    from .workflow import FLOWS
    graph = subs.add_parser('graph', help='export LangGraph topology without models or robot execution')
    graph.add_argument('--flow', choices=list(FLOWS), default='native')
    graph.add_argument('--out', help='optional Mermaid .mmd file')
    inspect = subs.add_parser('inspect', help='verify an external report against independent timestamped evidence')
    inspect.add_argument('--report', required=True)
    inspect.add_argument('--inspector-config', required=True)
    inspect.add_argument('--inspection-evidence', required=True)
    inspect.add_argument('--out', default='CoEgent_Agent/results/inspection.json')
    add_catalog_parser(subs)
    tools = subs.add_parser("tools", help="list planning tool contracts without loading robot SDKs")
    profile_options(tools)
    tools.add_argument("--out")
    subs.add_parser('executors', help='list adapter names without loading SDKs or executing a robot')
    suite = subs.add_parser("suite")
    suite.add_argument("--out", default="CoEgent_Agent/results/mock_suite.json")
    demo = subs.add_parser("demo")
    demo.add_argument("--scenario", choices=list(scenarios()), default="valve_open")
    demo.add_argument("--out", default="CoEgent_Agent/results/demo.json")
    request = subs.add_parser("request", help="approved request + provided plan through the selected executor")
    request.add_argument("--request-file", required=True)
    request.add_argument("--state")
    request.add_argument("--state-mode", choices=["snapshot", "strict"], default="snapshot")
    request.add_argument("--max-observation-age", type=float)
    request.add_argument("--max-inspection-age", type=float)
    request.add_argument("--out", default="CoEgent_Agent/results/mission_request.json")
    request.add_argument('--plan-id', type=int, help='load catalog plan into a metadata-only request')
    planning = subs.add_parser("plan", help="generate and validate a plan only; no skill backend")
    run = subs.add_parser("run", help="combined planning and execution through a selected adapter")
    for command in (request, run):
        command.add_argument('--inspector-config', help='deployment-owned Inspector JSON criteria/model settings')
        command.add_argument('--inspection-evidence', help='independently produced timestamped evidence JSON (native executor)')
        command.add_argument('--executor', default='coegent', help='registered executor adapter name')
        command.add_argument('--executor-options', help='path to a strict JSON options object')
        command.add_argument('--no-auto-save-plans', action='store_true', help='disable successful-plan archival')
    for command in (request, planning, run):
        profile_options(command, with_context=True)
        command.add_argument('--plan-db', help='SQLite catalog path; default COEGENT_PLAN_DB or data/plans.sqlite3')
    for command in (planning, run):
        command.add_argument("--planner", choices=["file", "db", "inprocess", "hf", "http", "auto"], default="file")
        command.add_argument('--generation-planner', choices=['http', 'inprocess', 'hf'], default='http',
                             help='new-plan backend for --planner auto')
        command.add_argument('--selector-backend', choices=['same', 'http', 'inprocess', 'hf'], default='same',
                             help='same shares the generation model; http/inprocess/hf uses separate selector settings')
        command.add_argument('--selector-model', help='inprocess GGUF path (default: E4B Q4), HF checkpoint path, or HTTP model name')
        command.add_argument('--selector-endpoint')
        command.add_argument('--selector-adapter')
        command.add_argument('--selector-max-candidates', type=int, default=5)
        command.add_argument('--selector-max-tokens', type=int, default=512)
        command.add_argument('--selector-max-prompt-bytes', type=int, default=12000)
        command.add_argument('--plan-id', type=int, action='append', help='catalog ID; repeat for scripted repair/replan responses')
        command.add_argument("--command", required=True)
        command.add_argument("--state")
        command.add_argument("--plan-file")
        command.add_argument("--model", help="inprocess GGUF path (default: E4B Q4), HF checkpoint path, or required HTTP model name")
        command.add_argument("--adapter")
        command.add_argument("--endpoint")
        command.add_argument("--generation-mode", choices=["auto", "json_schema", "unconstrained"], default="auto",
                             help="auto: HTTP and native inprocess request strict schema; hf is unconstrained")
        command.add_argument("--max-tokens", type=int, default=1024)
        command.add_argument("--max-repairs", type=int, default=1)
        command.add_argument("--planner-timeout", type=float, default=90)
    run.add_argument("--max-replans", type=int, default=1)
    run.add_argument("--mission-timeout", type=float, default=180)
    planning.add_argument("--out", default="CoEgent_Agent/results/plan.json")
    run.add_argument("--out", default="CoEgent_Agent/results/run.json")
    args = parser.parse_args()
    try:
        return asyncio.run(main_async(args))
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
