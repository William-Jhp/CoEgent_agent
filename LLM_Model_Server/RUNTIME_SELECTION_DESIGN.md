# vLLM / llama.cpp runtime selection

## Scope

Add runtime and model selection inside the existing LLM_Model_Server management boundary. The caller selects a profile before startup. CoEgent continues using ModelServerPlanner and the HTTP chat-completions interface. One managed process group owns one active runtime/model at a time.

## Configuration and caller interface

- `runtime` is `vllm` or `llamacpp`; the existing server.toml defaults to vllm.
- `--runtime llamacpp` selects config/llamacpp.toml unless --config is supplied.
- `--config`, `--model-path`, `--model-name`, `--adapter-path` and `--no-adapter` support explicit startup selection. TOML persists a preset; command arguments override it for that invocation.
- A changed model with an existing adapter requires an explicit adapter choice; never silently attach the previous model's adapter.
- A client-config command prints ModelServerPlanner constructor settings and the selected runtime/model for agents. Selection occurs before requests, not through LLM interpretation of natural language.
- vLLM accepts supported local HF checkpoints and optional PEFT adapters. The Gemma4 text architecture override is applied only to Gemma4. llama.cpp requires GGUF model and optional GGUF LoRA files.
- Config paths resolve relative to the chosen TOML. The shared state file tracks runtime, model path, adapter path and model alias so different presets cannot be reported as ready for the wrong model.

## Native runtime ownership

Install llama-server and its project shared libraries under runtimes/llamacpp/bin. Import the already built local CUDA runtime; copy library contents rather than retaining symlinks to the benchmark tree. Include license, version, hashes and source-build provenance. Check ldd under the launch environment to ensure project dependencies resolve inside the installation. System CUDA/driver/libc remain platform prerequisites. Provide a source-build option with build output inside LLM_Model_Server for later rebuilds.

Keep upstream source and existing model files unchanged. No automatic model downloads. Example GGUF profile points to existing Gemma4 robot-SFT Q8_0 with a distinct alias; it is not silently presented as the current Planner-SFT LoRA model. Users can select another GGUF or explicitly converted GGUF adapter.

## Lifecycle and resources

Both runtime adapters construct argv/environment for the existing controller. vLLM resource checks retain its GPU budget; llama.cpp CPU mode works without a CUDA device and GPU mode checks the configured minimum VRAM margin. Health/model alias readiness, safe process identity, stop cleanup and port refusal remain shared. Runtime changes require stop then start.

## Verification

Use literal config/CLI contracts, CPU HTTP subprocess fixtures, installer dependency isolation, and both actual runtimes' health/schema/CoEgent plan-generation checks. Preserve plan quality findings separately from transport/format validity. Return to stopped state after live checks.
