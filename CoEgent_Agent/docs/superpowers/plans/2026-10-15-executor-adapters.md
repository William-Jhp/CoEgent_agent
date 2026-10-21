# Executor Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans inline; one fresh-context final review.

**Goal:** All execution handoffs use replaceable executor adapters.
**Architecture:** Independent plugin contract/registry, native engine wrapper and an enforcing CoEgent session boundary; selected factories bind once per admitted mission.
**Tech Stack:** Python stdlib, existing asyncio/pytest/jsonschema. No inference.
**Spec:** ../specs/2026-10-15-executor-adapters-design.md

## Global Constraints
- Preserve native defaults, admission and per-skill safety, backend-free plan mode, existing reports and request idempotency.
- Separate package, selected-only imports, copied JSON options; unknown/duplicate registrations rejected.
- No active executor swapping, no fictitious external service, no heavy dependencies or model execution.
- No Git repo: directory-scoped implementation, no commits/worktree possible.

## Review Focus
- Invalid plans never reach adapter prepare/execute.
- Cancel acknowledgement and close failure do not fabricate success.
- Selected adapter progress must remain tied to current mission, not stale state.
- Removed/unselected plugins are not imported or executed.
- CLI plan remains entirely execution-free.

## Tasks
- [x] 1. RED/GREEN independent contract + registry and lifecycle tests; install local editable lightweight package.
- [x] 2. RED/GREEN session/native bridge and CoEgent integration; full native safety regression.
- [x] 3. RED/GREEN CLI/PlanExecutor selection; docs, custom payload adapter example, full tests and fixture CLI checks.
- [x] 4. Fresh-context review, correct findings with tests, final evidence.

## Ledger
- Design follows explicit user approval of prior interface proposal. Inline execution; no additional permission prompt.
- External endpoint-specific implementations remain plugin responsibilities because no external executor contract was provided. The common boundary and actual native/custom-plugin flow are implemented and tested.

- Baseline: 346 tests passed with PYTEST_DISABLE_PLUGIN_AUTOLOAD=1; host ROS pytest plugin auto-discovery requires missing lark, unrelated to product. No ROS installation changes.
- Added native/session/registry and CLI tests (22 currently). Registry and initial native handoff tests observed RED before implementation. Follow-up regressions observed RED for caller CLI selection, invalid-progress cleanup, timeout ownership, negative stop latch, and post-close state reconciliation.
- Review findings fixed: negative stop fault latch, post-close final status reconciliation, new-package setup docs. Reviewer follow-up found no material remaining findings.
- Integration scope clarification: LLM_Model_Server/scripts/smoke_coegent.py adds Executor_Adapters source path, preserving existing system-Python fixture tests. Server runtime/config/weights unchanged.
- Native regression preserves original failure_reason when stop failure escalates final status; six existing tests caught accidental overwrite and drove the fix.

- Final native compatibility: original execution failure_reason is retained when close/stop errors escalate status. Added RED→GREEN close-failure regression.

- Final evidence: CoEgent368 passed (22 added), model-server49 passed, CLI suite12/12, native adapter CLI SUCCESS and plan-only PLAN_READY/NOT_EXECUTED. No inference or physical execution. Details in docs/EXECUTOR_ADAPTER_VERIFICATION.md.
