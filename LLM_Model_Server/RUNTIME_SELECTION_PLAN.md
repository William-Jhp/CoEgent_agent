# Runtime Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans inline, followed by one fresh-context final review.

**Goal:** Select vLLM/HF or llama.cpp/GGUF before startup using config or CLI; expose the resulting HTTP client settings.

**Architecture:** Runtime-specific argv/validation sit behind the existing process controller. Profiles select model and adapter explicitly; both serve the HTTP protocol consumed by ModelServerPlanner.

**Tech Stack:** stdlib Python/TOML/unittest/urllib, existing isolated vLLM environment, bundled native llama-server plus shared libraries.

**Spec:** [RUNTIME_SELECTION_DESIGN.md](RUNTIME_SELECTION_DESIGN.md)

## Global Constraints

- Changes confined to LLM_Model_Server; read/build upstream source without editing it.
- Existing default vLLM model/adapter and CoEgent source stay intact.
- One shared managed state; loopback endpoint; no shell=True; no automatic downloads.
- CLI model changes cannot silently reuse an adapter. GGUF adapters must be GGUF files.
- llama.cpp project libraries resolve in server-owned runtime directory, system CUDA dependencies remain explicit.
- No Git repository exists for this workspace; directory boundary replaces worktree/commit operations.

## Review Focus

- HF checkpoint accidentally selected with llama.cpp or PEFT adapter with GGUF: reject before spawn.
- Override model but retain previous adapter: require explicit adapter choice.
- CPU-only llama.cpp on a GPU-less host: preflight must pass with enough RAM.
- Active runtime/model differs but alias/port equal: status must report config_changed and duplicate start must not replace it.
- Imported shared-library symlinks/build RPATH: ldd must resolve project libs inside the new runtime installation.

## Task 1: Configuration and runtime launch contract

Files: local_llm_server/config.py, tests/test_runtime_selection.py, config/server.toml, config/llamacpp.toml.

- [x] Write tests for default vLLM, no-adapter HF, GGUF argv/adapter/header, override protection, invalid runtime.
- [x] Observe failing tests before implementation.
- [x] Extend ServerConfig/load_config with runtime/optional adapter/llama executable/threads/GPU layers.
- [x] Build runtime-specific argv and environment while retaining the default vLLM path.
- [x] Pass those tests and the existing suite.

## Task 2: CLI and lifecycle selection

Files: local_llm_server/cli.py, lifecycle.py, tests/test_runtime_selection.py, tests/test_lifecycle.py.

- [x] Write tests for CLI preset/overrides/client settings and active-config mismatch.
- [x] Add runtime-aware preflight/version detection and shared safe lifecycle metadata.
- [x] Expose client-config with the selected HTTP endpoint/model.
- [x] Verify CPU-only llama.cpp resource checks and runtime-switch fixture behavior.

## Task 3: Native runtime installation

Files: scripts/setup_llamacpp.py, scripts/setup_llamacpp.sh, tests/test_runtime_install.py, runtimes/llamacpp.

- [x] Test copied library independence with an ELF fixture build where available.
- [x] Install local native runtime privately, write license/version/hash manifest and validate ldd resolution.
- [x] Include optional source build into server-owned output without vendor edits.
- [x] Check bundled binary version/help and exact launch options.

## Task 4: Integration, docs and final review

Files: scripts/smoke_coegent.py, scripts/check_runtime.py, README.md, RUNTIME_SELECTION_VERIFICATION.md.

- [x] Select profiles consistently in runtime contract and smoke scripts.
- [x] Live llama.cpp: GGUF load, health/models, schema probe, actual CoEgent plan route.
- [x] Live vLLM regression: original model/LoRA and planner request.
- [x] Record exact limits/model quality; cleanly stop both and verify resource release.
- [x] Final read-only review, fix important findings, update usage examples.

## Execution ledger

- Authorization: user explicitly requested adding llama.cpp and startup runtime/model selection after the comparison; implement without another permission gate.
- Ruling: retain a single managed server rather than a pool/router; minimizes simultaneous model memory and matches preselection request.
- Ruling: example GGUF profile uses existing robot-SFT Q8_0 with a distinct alias and explicit lack of Planner LoRA; arbitrary model/adapter paths remain configurable.
- Ruling: import local compiled runtime with manifest rather than recompile CUDA unnecessarily; source-build path remains available.

- Completed: 49 tests passed; live GGUF Q8/Q4 and original vLLM + Planner LoRA schema/plan checks passed. Both runtime transitions checked; server stopped and resource release recorded.
- Review: adapter-only model override and nested installer destination fixed; caller CWD, engine settings and TIME_WAIT regressions additionally covered. Follow-up read-only review found no material defects.
- Limit: imported local engine build; full CUDA source rebuild and real GGUF LoRA conversion/inference not performed.
