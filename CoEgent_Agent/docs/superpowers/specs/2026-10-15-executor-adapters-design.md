# Executor adapter architecture (2026-10-15)

User intent: every execution handoff follows a selected adapter; internal, local-process and remote executors are interchangeable through the same contract. Runtime location is adapter implementation detail. User explicitly authorized design and implementation of the previously proposed architecture; no repeated permission gate. Architectural change, executed inline.

## Boundaries
- New independent Executor_Adapters package: protocol, prepared plan, cached progress, explicit factory registry plus optional selected entry-point discovery. It imports no executor, SDK or CoEgent runtime on package import.
- Native CoEgentExecutorAdapter remains in CoEgent package and privately wraps MissionEngine. Only this adapter constructs the internal engine.
- CoEgent ExecutorSession enforces the boundary: validated plan -> prepare -> execute; cached progress feeds existing replanning/reporting; cancel maps to existing stop confirmation; close releases adapter-owned resources, not the injected skill backend.
- Factories bind per-mission context (router/state/trace/limits/request/selected name) and explicit options. Custom adapters are trusted deployment code and must preserve per-step safety/observation/authority checks or equivalent enforced execution-side checks. A whole-plan remote call alone is not a safety implementation.
- PlanGenerator remains backend-free. It returns canonical planner.v1; it does not create an execution adapter, robot owner or connection.
- CoEgent and PlanExecutor accept executor name/options/registry. CLI run/request accept executor selection and a strict JSON options object file; adapter loading is selection-only.
- Native is default for old callers. Every actual plan execution and mission stop goes through ExecutorSession; native node interpretation still uses SkillRouter. Existing safety reset/offline-return direct skill paths are management operations, not whole-plan handoffs, and remain guarded.
- Registration/removal affects future resolution; a running mission holds its binding. Agent selection changes are rejected while busy or while owned tasks are pending. No hot module unload or arbitrary executor processes are launched automatically.
- Local/remote plugins implement the common contract; do not ship pretend remote connections without a target service contract. Test a distinct payload-transforming registered adapter through the real orchestration and native execution, plus cancellation and unsupported-plan handling.

## Public contract
ExecutorContext stores mission binding; PreparedPlan identifies executor and opaque payload; ExecutionProgress supplies steps/completed/failed/remaining/stop_confirmation. ExecutorAdapter methods: prepare(plan), execute(prepared), status(), cancel(), close(); progress is a synchronous cached property used during monitoring. Results follow existing SkillResult/Event contracts, validated at the session boundary. Options are JSON objects copied at binding. status must not perform blocking network I/O in progress access.

## Lifecycle
Factories selected at initialization or idle set_executor; sessions constructed per admitted mission. Adapter errors cannot skip stop cleanup. Invalid result/cancel confirmation fails closed. Existing request dedup, revision/expiry, deadline, freshness, replan and evidence tests must pass. close runs bounded and cancellation-safe; cancellation/unconfirmed close cannot silently produce a successful report.

## Verification and exclusions
CPU tests only; no model loading or physical device calls. Baseline existing CoEgent suite, new boundary/plugin tests, full regression and CLI smoke. No changes to model serving or original weights; the CoEgent smoke helper may add the sibling adapter source import path for compatibility. No Git repository exists; keep change scope to Executor_Adapters + CoEgent_Agent, preserve unrelated work.
