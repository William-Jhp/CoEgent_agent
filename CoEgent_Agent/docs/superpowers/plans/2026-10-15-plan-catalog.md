# Persistent Plan Catalog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans inline, final fresh-context review.

**Goal:** Persist unique plans and successful outcome records; use specified IDs through ScriptedPlanner.
**Architecture:** SQLite PlanStore separated from planner, lazy execution archival, explicit CLI/Python interfaces. Full-plan canonical hash with unique constraint.
**Tech Stack:** Python stdlib sqlite3/hashlib/JSON; existing validation, pytest.
**Spec:** ../specs/2026-10-15-plan-catalog-design.md

## Global Constraints
- Deduplicate complete normalized plan, not title/author/command; preserve sequence order and different criteria/goals.
- Revalidate plans; no DB record authorizes execution or proves physical success.
- Preserve native/adapter contracts and plan-only behavior. Auto-save only complete eligible successful missions.
- DB failure does not change execution outcome or hold robot fault; diagnostics explicit, SQLite wait bounded.
- Test databases isolated; do not populate user DB from fixtures. No model loads or Git operations.

## Review Focus
- Simultaneous inserts cannot create duplicate rows.
- SUCCESS with unmet/unknown goals is not archived as successful.
- Replayed request does not increment success twice; continuation/replans not mislabeled full-plan success.
- Stored data is still revalidated; malformed/nonfinite input rejected before persistence.
- Inline request plan and DB plan ID conflict rejected before execution.

## Tasks
- [x] 1. RED/GREEN PlanStore schema/canonical identity/manual CRUD/read persistence and concurrency.
- [x] 2. RED/GREEN ScriptedPlanner.from_store and success archive integration, explicit disable/failure diagnostics; isolate test DB defaults.
- [x] 3. RED/GREEN CLI management and DB plan selection; user default empty DB, docs/CPU smoke.
- [x] 4. Full regression, fresh review/fixes and verification record.

## Ledger
- User selected complete-plan equality. SQLite file chosen for offline local storage without daemon or package installation.
- Autosave skips replanned/continued missions because final plan can be a partial suffix. Store error is diagnostic, never a reason to repeat completed side effects.

- Baseline368 passed. Task1 RED9→GREEN9; Task2 additional6RED→GREEN15. Existing suite383 passed after autosave integration; test database env fixture isolates default writes.
- CLI initial4 failures observed before adding plans management / db selector / request ID handoff; currently validating full integration.

- Catalog30 new tests pass. Reviewed and fixed initialization identity race plus destructive export target; deterministic RED→GREEN tests cover both, with DB sidecar/symlink guards.
- Real default data/plans.sqlite3 initialized empty; synthetic executions use temporary/test catalogs only.

- Follow-up ownership precheck protects unrelated SQLite journal mode; transactional recheck preserves startup race protection. RED→GREEN foreign-DB test added. Reviewer confirmed no remaining material findings and ran four protection tests.
-31 new catalog tests pass; CLI smoke persisted one plan with two successful MockSkills observations in a temporary DB, default user DB remains empty. Full results recorded in docs/PLAN_CATALOG_VERIFICATION.md.

- Final:399 CoEgent tests passed (31 new), model-server49 passed, CLI scenarios12/12, real default DB empty. Storage-only tests used temporary SQLite catalogs; no model or physical executor was started.
