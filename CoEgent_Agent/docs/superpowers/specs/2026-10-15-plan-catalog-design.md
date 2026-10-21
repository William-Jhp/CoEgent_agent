# Persistent plan catalog design — 2026-10-15

## Intent and authorization
User requested a separate persistent DB for ScriptedPlanner plans, manual/upper-agent additions, successful-execution auto-save and no duplicate plan rows. User explicitly selected full-plan deduplication excluding metadata; whitespace and object key order do not affect identity. Architectural addition implemented inline under the authorized request; no extra permission prompt.

## Storage and identity
Use Python stdlib SQLite, no service/dependency. Default path CoEgent_Agent/data/plans.sqlite3, override via COEGENT_PLAN_DB, CLI --plan-db or explicit PlanStore(path). File-backed persistent catalog lives separately from planner memory. Validate planner.v1 before insertion; normalize JSON object keys/whitespace and integral numeric representations, hash the full normalized plan including goal, criteria and ordered sequence. Metadata (title/author/source/command) is excluded. UNIQUE hash enforces dedup transactionally across processes; first plan metadata stays stable. List/get IDs, persist original source and contract hash, export canonical body. Load revalidates structural contract; execute always applies current state/authority again. No semantic natural-language lookup.

Plans table: ID, hash, canonical JSON, title/author/source/command, contract fingerprint, creation time, success_count/last_success_at. Successful execution observations separate plan rows; mission_id/revision idempotency prevents duplicate history count on replay. Store executor/backend/state_mode/goal status provenance. Do not promote a MockSkills outcome to proof of physical robot execution.

## Automatic persistence
CoEgent and PlanExecutor default auto-save successful eligible missions through a lazy default store, explicit plan_store injectable and auto_save_plans=False available. Save only final SUCCESS without robot fault/incomplete report/pending tasks and with goals achieved or not_evaluated. Unknown/unmet goals, failure/cancellation/replayed requests/plan-only outputs are excluded. After replan or approved continuation, the last plan may be only a suffix: skip automatic archival rather than claim it is a full successful mission; return reason. Users may explicitly save any structurally valid plan.

Plan DB write is small synchronous local IO with bounded SQLite busy timeout after execution finalization. Catalog failure is reported as plan_store error without changing completed execution status or triggering stop/replay. Initial snapshot and actual completed outcome remain trace/runtime evidence, not planning authority. Do not infer reusable correctness in a new state.

## Caller surface
PlanStore.add/get/list/export as Python API, ScriptedPlanner.from_store(store, plan_ids) snapshots specified full plans into its existing ordered queue. Reading/consuming does not delete DB rows. CLI plans init/add/list/show/export; --planner db plus repeatable --plan-id for plan/run. request may select --plan-id with metadata-only request JSON; inline plan and ID together are rejected. --plan-db selects catalog, run/request support --no-auto-save-plans. Do not turn a command like 'mission 3' into autonomous lookup.

## Verification and isolation
Temporary SQLite per test (existing suite defaults isolated via COEGENT_PLAN_DB autouse fixture); concurrency, malformed JSON, canonical duplicate, restart, manual add, success/goal failure/autosave failure/replay/continuation, DB-backed planner and CLI integration. No inference/physical devices, no importing historical reports as success. Initialize a real empty default DB for user without test plans. Preserve adapter/executor guard regressions and model-server fixtures.

No Git repo/worktree exists. Scope CoEgent_Agent only, aside from CPU model-server regression verification. Keep ledger/docs.
