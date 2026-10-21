# Executor adapter verification — 2026-10-15

Implemented the authorized common execution adapter boundary. No LLM inference, server startup, or physical device execution was performed.

## Changes

- `Executor_Adapters/`: independent `coegent-executor-adapters` package with protocol, context, prepared plan, cached progress and selected-only registry/plugin discovery.
- `coegent/executor_adapters/native.py`: existing MissionEngine wrapped by CoEgentExecutorAdapter. CoEgent no longer directly constructs the engine.
- `coegent/executor_adapters/session.py`: mandatory prepare/execute/result/progress/cancel/close boundary for every admitted plan execution.
- `CoEgent` and `PlanExecutor`: executor/options/registry injection and idle-only `set_executor`.
- CLI: `executors` lists names without loading their SDKs; `run`/`request` accept `--executor` and `--executor-options`.
- Native defaults, plan validation, per-step routing/safety/freshness, goal evidence, request dedup/revisions, stop confirmation and failure diagnostics preserved.
- PlanGenerator remains execution-free; plan-mode output remains canonical planner.v1 and NOT_EXECUTED.
- LLM model-server smoke helper adds the sibling adapter source path, preserving source-based integration without changing serving or model settings.

## Evidence

| Check | Result |
|---|---|
| Existing baseline CoEgent tests | 346 passed |
| Final CoEgent suite, including 22 new adapter cases | **368 passed**, 14.08 s |
| Model-server CPU fixture/unit integration suite | **49 passed**, 6.235 s |
| CLI scenario suite | **12/12 passed** |
| CLI run with native adapter | SUCCESS; trace contains executor_prepare and executor_execute |
| Plan-only CLI using model-server Python environment | PLAN_READY, NOT_EXECUTED; no executor/skill events |
| Fresh adapter-package import | Does not import CoEgent or Torch |

Commands:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 ./.venv/bin/python -m pytest CoEgent_Agent/tests -q
PYTHONPATH=LLM_Model_Server python3 -m unittest discover -s LLM_Model_Server/tests
./.venv/bin/python -m coegent suite --out CoEgent_Agent/results/executor_adapter_suite.json
```

The host has ROS pytest plugins whose automatic import fails due to missing `lark`; product baseline and final suite use `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`. No ROS environment packages were changed.

CLI artifacts: `results/executor_adapter_cli.json`, `results/executor_adapter_plan_only.json`, `results/executor_adapter_suite.json`.

## Regression and review findings

New tests exercise actual native execution through a registered payload-transforming adapter, invalid-plan exclusion, options copying, lazy discovery/removal, unknown selection, in-flight replacement refusal, cancellation, malformed prepared/result/progress data, and close timeout ownership.

Observed failing regressions before fixes:

- Unknown CLI executor flags and names.
- Malformed progress escaping stop cleanup/report construction.
- Uncooperative timed-out close escaping admission-owned pending-task tracking.
- Plugin-reported negative stop confirmation failing to latch fault and block subsequent requests.
- A stop failure or malformed progress first appearing during close leaving final status SUCCESS.
- Stop/close escalation overwriting the original execution failure reason.

Fresh-context read-only review identified the stop latch, post-close reconciliation and sibling dependency setup issues. Each was corrected; follow-up review reported no material remaining findings.

## Operational limits

Native adapter is the supplied executor implementation. No concrete external executor API was provided, so remote/IPC implementations must be registered as deployment plugins; no fictional external service is presented as working.

Plugins are trusted code. Common plan validation is not a substitute for execution-time per-step authority, freshness, cancellation confirmation and budget enforcement. External adapters/executors must implement those checks and map actual state/evidence/results to the common contract. Safety-reset, pre-binding emergency fallback and offline-return skill operations retain their existing guarded management paths.

Adapter imports occur in the CoEgent process. The external executor may be a local process or remote service; plugins hide that transport. No automatic process supervisor or hot-unload is added. Registry removal affects future bindings; selected agents retain their resolved factory until explicitly changed. Active missions and pending cleanup prevent replacement.

Installed the lightweight local adapter package in the development and model-server virtual environments. Fresh installation requires both sibling projects, as documented in the package README.
