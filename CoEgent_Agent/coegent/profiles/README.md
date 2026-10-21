# a2-isaac API planning profile

`a2_isaac.json` is a curated contract for ten existing Python methods in
`/home/gritandgrind/dev/kts/a2-llm-isaac`. Loading the profile parses source ASTs
and verifies class/method names and required/optional parameter names without
importing `rclpy`, `a2sim`, or any robot code. Source paths, line numbers and
file hashes are included in `coegent tools` output. Signature checks do not
prove runtime readiness or the correctness of manually documented return types.

The `plan` mode supports **planning/output only**. No execution backend,
ROS node, model-server vision call, simulator or robot is invoked in plan mode.
`PLAN_READY` means the plan passes the API argument/dataflow contract;
reports explicitly contain `execution_ready: false`,
`validation_scope: api_contract_only`, and deferred runtime preflight checks.
`run`/`request` require the explicit optional `--executor a2-isaac` plugin from
[`A2_Execution_Adapter`](../../../A2_Execution_Adapter/README.md). The default mock
executor still rejects this profile. The A2 plugin performs real live checks and
uses an owned ROS Docker worker; installing it does not turn plan mode into execution.

## Ready-to-use commands

From local_ai_study:

```bash
# E4B GGUF / native llama.cpp, Selector + Planner, plan-only
bash CoEgent_Agent/scripts/plan_a2_isaac.sh --command 'reception으로 이동해.'

# List exact methods, arguments, return descriptions and source provenance
PYTHONPATH=CoEgent_Agent:Executor_Adapters ./.venv/bin/python -m coegent tools \
  --tool-profile a2-isaac --out CoEgent_Agent/results/a2_tools.json

# Explicit model backend (HTTP also supported by normal --planner http settings)
PYTHONPATH=CoEgent_Agent:Executor_Adapters ./.venv/bin/python -m coegent plan \
  --tool-profile a2-isaac --planner inprocess \
  --command '지도 좌표 x=-13.84, y=4.88로 이동해.' \
  --out CoEgent_Agent/results/a2_plan.json
```

The launcher selects a2-isaac / auto / inprocess and writes
`CoEgent_Agent/results/a2_isaac_plan.json`. Normal CLI invocations retain the
legacy mock profile by default. `--api-source-root` selects another checkout;
source mismatch/missing source fails before model loading.

## Tool contracts

| Method | Arguments | Python return |
|---|---|---|
| RobotBackend.go_to | x, y: number | bool |
| RobotBackend.localize | none | dict: ready_s, task_id |
| RobotBackend.stop_localization | optional grace_s: nonnegative integer/null | None; command sent, not a stop acknowledgement |
| RobotBackend.refresh_pose | optional max_age: positive number/null | RobotState object |
| RobotBackend.check_battery | none | ratio 0..1, or RobotError |
| RobotBackend.observe | optional yaw_offset=0 | tuple: RGB numpy image, None |
| RobotBackend.ensure_nav_active | none | string, or RobotError |
| GridNavigator.plan | start_xy, goal_xy: [x,y] | Nx2 numpy path or None; no movement |
| WorldState.capture_views | none | tuple: views, stamp, received, source |
| WorldState.describe | views: prior-result reference | LLM reply object with data |

Object constructors, ROS publishers/subscribers, calibration helpers, raw
motion commands and all functions in the source tree are not automatically
exposed as planning tools. In particular this catalog is not an MCP server.

`RobotBackend.go_to` handles path computation internally. A plan normally
contains only the destination coordinates, not fabricated waypoints. Named
coordinates are loaded from the source checkout's
`planner/data/scene_meta.json` → `annotations.places`. These are static map
annotations, **not** the current robot pose, localization, live battery,
reachable-path evidence, or a live map-selection acknowledgement. Their source
file hash/map artifact is included in the catalog/context. With no `--state`,
all state dictionaries are empty rather than using mock robot values.

For another map, supply explicit context with `--tool-context file.json`:

```json
{"places":[{"name":"loading bay","x":12.0,"y":3.0}],"map_artifact":"operator supplied map"}
```

Those numbers are an example format, not a measured destination. A supplied
`places` array replaces the source annotations. The model is instructed to
ask for unknown destinations instead of inventing coordinates. Auto mode uses
PlannerSelector for clarification; direct planning can represent unavailable
information with `replan_required`. Static schema validation alone does not
prove that a generated coordinate matches the user's intended destination.

## Image and conditional dataflow

Images/NumPy arrays/PIL objects cannot be embedded as literal JSON arguments.
The **planning representation** uses a symbolic prior-result reference:

```json
[
  {"type":"skill","name":"WorldState.capture_views","args":{},"save_as":"camera"},
  {"type":"skill","name":"WorldState.describe","args":{"views":{"$result":"camera","index":0}}}
]
```

`index:0` means the first item of the raw Python tuple returned by
`capture_views`. The validator verifies an earlier capture dominates the
reference and rejects forward/mismatched references. The optional A2 execution
adapter materializes this binding inside its worker. Plan mode never binds images.

Python result objects/scalars are not the existing mock observation dicts.
Conditional paths into raw results are rejected until an explicit execution
projection exists. The prompt requests sensing followed by `replan_required`
when new observations determine subsequent work. Conditions over explicitly
provided robot/world/mission snapshot values remain syntactically available;
runtime validation is deferred. This profile does not fix the earlier mock
Planner conditional benchmark or claim equivalent conditional execution support.

## Plan storage and reuse

With this profile, `--planner auto` uses a separate default database:
`CoEgent_Agent/data/a2_isaac_plans.sqlite3`. The original `plans.sqlite3` remains
for the existing mock contracts. Profile identity/contract hashes are checked;
cross-profile stores are rejected, including an empty API-marked database.
Plan mode never automatically stores or marks successful execution.

Explicit registration and retrieval use the same profile:

```bash
PYTHONPATH=CoEgent_Agent:Executor_Adapters ./.venv/bin/python -m coegent plans add \
  --tool-profile a2-isaac --plan-file CoEgent_Agent/results/a2_isaac_plan.json \
  --command 'reception으로 이동해.'
```

The next auto plan can ask Selector whether to reuse the plan body. Source
contract changes invalidate an API database identity; choose a new DB or
perform a deliberate revalidation/migration. `api_calls` in a report is a
readable list of calls in the plan, not a replacement for its control flow.
## cuVSLAM 지도 선택

`--map-scene /absolute/path/to/scene-bundle`로 기본 LiDAR scene 대신 선택한 cuVSLAM scene을 계획에 공급할 수 있습니다. bundle의 장소·격자·native 지도 UUID/해시를 계약에 묶고 기본 계획 DB를 지도별로 분리합니다. 실행 시 A2 Adapter의 `scene_dir`도 같아야 합니다. 현재 office_sim용 설정과 명령은 [A2 Adapter 문서](../../../A2_Execution_Adapter/README.md)에 있습니다. 다른 지도에 기존 reception 좌표를 그대로 복사하지 않습니다.
