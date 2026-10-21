# Persistent plan catalog verification — 2026-10-15

Implemented a local SQLite catalog at `CoEgent_Agent/data/plans.sqlite3`. The user's duplicate policy is full normalized-plan equality, excluding metadata. The real default DB is initialized and empty; test fixtures were not imported into it.

## Delivered

- `PlanStore` Python API: validated add/get/list/export, canonical full-plan SHA256 identity, SQLite UNIQUE constraint, explicit database identity/version and success observations.
- Manual/user/upper-agent additions through API and `coegent plans init/add/list/show/export`.
- `ScriptedPlanner.from_store(store, IDs)` and CLI `--planner db --plan-id ID`; queue consumption does not remove persistent rows.
- `request --plan-id ID` with a metadata-only request; inline plan plus ID rejected.
- CoEgent/PlanExecutor successful-execution auto-save enabled by default, injectable store and explicit disable.
- Separate successful-execution records with executor/backend/state-mode/goal provenance; duplicate mission observation does not increment counts twice.
- Failed/cancelled/unmet/unknown-goal outcomes and plan-only results are not automatically archived as successful.
- Replan/revision continuation suffixes are excluded from full-mission automatic registration. Existing explicit/manual addition remains available.
- Storage errors are diagnostic and never change execution success or repeat side effects.

## Results

| Verification | Result |
|---|---|
| Baseline before this change | 368 CoEgent tests passed |
| New catalog tests | 31 passed |
| Final full CoEgent suite | **399 passed**, 19.08 s |
| Model-server CPU/HTTP fixture tests | **49 passed** |
| CLI native scenario suite | **12/12 passed** |
| Temporary DB CLI integration | one unique plan, duplicate add=false, two successful observations |
| Default user DB after testing | empty |

Commands:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 ./.venv/bin/python -m pytest CoEgent_Agent/tests -q
PYTHONPATH=LLM_Model_Server python3 -m unittest discover -s LLM_Model_Server/tests
./.venv/bin/python -m coegent suite --out CoEgent_Agent/results/plan_catalog_scenario_suite.json
```

Existing host ROS pytest plugins fail automatic loading due to missing `lark`; product tests use explicit plugin isolation as in the adapter verification. No ROS packages or model weights were changed.

New tests cover persistence/reopening, independent read copies, key/whitespace/numeric canonicalization, ordered steps and criteria distinctions, malformed/nonfinite plans, thread and separate-process duplicate inserts, deterministic first-init interleaving, tampered reads, manual and automatic paths, requested goal checks, replay accounting, DB failure, disable, provenance, continuation exclusion, plan-only exclusion, CLI import/export/ID selection and request conflict handling.

## Review corrections

Fresh read-only review found an initialization identity race and destructive export target. Failing tests reproduced both before fixes. Ownership/table checks now occur in consistent write transactions, with ownership checked before journal-mode changes and rechecked before schema writes. A further regression verifies unrelated databases retain their original journal mode and data. Export rejects the active DB, same-file aliases and SQLite sidecar paths. Follow-up review ran focused tests and reported no remaining material issues.

## Evidence and limits

CLI artifacts are in `results/plan_catalog_validation_20261015/`: run/request reports and `summary.json`. A temporary file-backed DB was used across separate CLI processes. It showed one catalog row and success_count=2, both observations explicitly tagged `coegent.skills.MockSkills`, snapshot state and no requested goals. This does not prove physical robot success. No LLM inference or physical executor was used.

Deduplication is structural/full-plan, not semantic similarity. Goals, criteria and sequence order remain significant. DB success history does not authorize reuse in a new environment; load and execution apply current validation and safety policies. There is no natural-language plan lookup, persistent request execution journal, server daemon, or semantic vector index in this change.

SQLite writes are local synchronous IO with a 0.5-second default lock timeout. Catalog IO failure is recorded in `report.plan_store`; execution's result remains authoritative. User DB data is ignored by Git and retained outside tests.
