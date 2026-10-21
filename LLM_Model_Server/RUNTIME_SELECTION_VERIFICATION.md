# Runtime / model selection verification

2026-10-14, current development PC: Linux, Python 3.12, NVIDIA RTX A6000 (49,140 MiB). Changes are confined to `LLM_Model_Server`; CoEgent and original model/adapter/upstream source files were not modified.

## Result

Runtime and model can be preselected through TOML, CLI overrides, or Python `load_config(..., overrides=...)`. Both runtime paths load real models and serve the HTTP interface used by CoEgent `ModelServerPlanner`. One managed instance is active at a time. Switching requires stop/start, and the old runtime is not silently replaced.

| Actual runtime / model | HTTP schema probe | CoEgent PlanGenerator | Tool execution |
|---|---|---|---|
| llama.cpp / existing Gemma4 robot-SFT Q8_0 GGUF | passed | PLAN_READY | NOT_EXECUTED |
| llama.cpp / explicitly selected Q4_K_M GGUF and alias | passed | PLAN_READY | NOT_EXECUTED |
| vLLM / original HF checkpoint + Planner LoRA | passed | PLAN_READY | NOT_EXECUTED |

The existing CoEgent CLI also returned PLAN_READY using the Q8_0 server, with one planner call and no repair. Requests used `현재 배터리를 확인해.`. This verifies loading, transport, schema application, output completion, and current plan validation; it is not a broad model-quality evaluation.

## Installation and configuration

- Installed a private native llama.cpp bundle at `runtimes/llamacpp`, version `0.3.0-dev`, build 1, commit `b96806d`.
- Executable and project shared libraries are copied into this server directory; library symlinks resolve inside it. License, version, hashes, and build provenance are recorded in `runtimes/llamacpp/manifest.json`.
- Installer checks `ldd` resolution and version before replacement. An ELF fixture still runs after the original source build is removed.
- Optional source build outputs into `runtimes/llamacpp-build`. Full CUDA source recompilation was not performed in this verification. The installed engine was imported from the existing local build.
- Native bundle retains system CUDA 13, driver, libc, and OpenSSL dependencies. A CPU build option is documented for hosts without those CUDA libraries.
- Existing vLLM 0.19.1 / Transformers 5.5.4 / PyTorch 2.10.0 environment remains intact; `uv pip check` found all 180 packages compatible.
- Wrong format/runtime combinations, unsupported runtime values, invalid GGUF headers, and implicit reuse of an old adapter after a model change are rejected.
- `client-config` exports the selected HTTP endpoint/model and `ModelServerPlanner` constructor kwargs. Consumers should require `server.status == "ready"` before requests.

## Lifecycle and regression checks

**49 tests passed.** Tests cover native argv and library independence, explicit adapter decisions, caller-relative CLI paths, CPU/GPU preflight, real HTTP fixture processes, runtime/model/engine-setting mismatches, duplicate starts, occupied ports, process identity, and owned-worker cleanup.

During actual switching, closed native HTTP connections left TIME_WAIT entries. The old port precheck treated these as an active listener. Added a regression test and corrected the probe with SO_REUSEADDR plus bind/listen; genuinely occupied listeners remain rejected. Verified the real transition from llama.cpp to vLLM and back to llama.cpp.

Read-only final review found two edge cases: an adapter-only preset raised KeyError when a model override was supplied, and an installer destination nested in its source build could prevent later upgrades. Both are fixed and have regression tests. Follow-up review found no material defects.

After the final smoke, stopped all managed servers. Checked all four actual server PIDs were gone, no compute processes remained on the GPU, port 18520 had no listener, and GPU free memory returned to 47,967 MiB. These are snapshots on this PC, not universal resource requirements.

## Limits

- The example GGUF files contain existing robot-SFT weights, not the current Planner-SFT PEFT adapter used by vLLM. Their outputs/timing are not a controlled runtime comparison.
- GGUF LoRA command/config support is implemented and tested with fixtures; an actual converted Planner LoRA was not available or loaded. Conversion is not included in this change.
- CPU-only preflight is tested without a GPU. Full CPU-model inference and arbitrary model families were not benchmarked.
- Existing GGUF logs warn about control-token metadata. The sample requests complete, but regeneration with a compatible converter may be needed for broader model quality.
- A valid JSON plan does not prove correct success criteria. Observed examples include a plain skill/field name as `success_criteria`; previous vLLM checks also found semantic errors. Model evaluation and stricter semantic validation remain separate work.

## Evidence

Artifacts: [results/runtime_selection_20261014_173140](results/runtime_selection_20261014_173140/).

- `llamacpp_smoke.json`, `llamacpp_final_smoke.json`: Q8 schema/plan responses, including the final revised lifecycle.
- `llamacpp_q4_smoke.json`: explicitly selected Q4 model and alias.
- `llamacpp_cli.json`: existing CoEgent CLI plan result.
- `vllm_smoke.json`, `vllm_live.json`: original model/Planner LoRA response and registered aliases.
- `llamacpp_contract.json`, `vllm_contract.json`: native options / actual HF architecture and LoRA support.
- `llamacpp_final_live.json`: private binary PID and native model metadata.
- `unittest.txt`: full 49-test result.
- `final_cleanup.json`: stopped state, absent PIDs, GPU resource release and listener check.
- `server.log`: engine logs from live validation.
