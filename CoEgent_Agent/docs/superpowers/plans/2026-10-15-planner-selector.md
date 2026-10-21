# PlannerSelector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans inline; one fresh-context final review.

**Goal:** Automatically route a command to a saved exact-match plan, new model planning, or clarification.
**Architecture:** Indexed candidate search + schema-validated LLM selector + shared guarded dispatch before existing planner adapters.
**Tech Stack:** stdlib regex/sqlite/asyncio/urllib, existing jsonschema/pytest; no new model dependencies.
**Spec:** ../specs/2026-10-15-planner-selector-design.md

## Global Constraints
- Existing file/db/http/hf and request semantics remain; auto is explicit.
- Preserve schema/state/authority/adapter checks, bounded repair/replan, cancellation and remote policy.
- No executing selector-produced plans or IDs outside candidates; full candidate snapshots, bounded prompts.
- Migration retains existing catalog data and dedup; aliases do not duplicate plans.
- Clarification with no side effects issues no skills, no stop, no success archival.
- No real model/device execution; no Git operations possible.

## Review Focus
- Negation, extra return step, changed target/numbers must not be treated as text equality.
- Malformed/truncated decision or hallucinated ID fails closed.
- Repair/replan must not replay a complete saved plan after completed side effects.
- Remote gates protect both selector and chosen generator; local reuse does not hide remote selector calls.
- Oversized plans omitted whole; migration/search do not corrupt body/hash/observations.

## Tasks
- [x] 1. RED/GREEN feature extraction + indexed catalog migration/aliases/search.
- [x] 2. RED/GREEN generic model completion and selector decision/schema/prompt tests.
- [x] 3. RED/GREEN shared dispatch, CoEgent/PlanGenerator clarification/reporting and remote hooks.
- [x] 4. RED/GREEN CLI auto/backend config; CPU HTTP integration, full regression, docs and fresh review.

## Ledger
- Initial authorization is explicit implementation of discussed design. Auto opt-in preserves existing CLI/defaults.
- Full-plan reuse disabled for continuation/repair; generation backend selected by application configuration, not model output.
- RED/GREEN: search 5, completion 3, selection 10, dispatch 6, CLI 4; lifecycle 7 (including late cancellation and remote gates). Baseline 399 -> final 434 pytest tests.
- Fresh reviewer found untracked cancelled model dispatch could overlap the next mission. Added admission tracking and failing-then-passing ownership test; reviewer rechecked all 7 lifecycle tests, no remaining actionable issues.
- Final validation: CoEgent 434 passed; model server 49 OK; scenario suite 12/12. Default catalog transactionally migrated v1->v2, original plan/history digest unchanged, integrity_check=ok.
- CPU fixtures/fake HF only, no actual model inference. Real deployed-model semantic comparison quality remains unevaluated; documented in PLANNER_SELECTOR.md and PLANNER_SELECTOR_VERIFICATION.md.
