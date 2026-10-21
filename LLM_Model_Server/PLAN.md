# Independent Local LLM Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. This task is executed inline in the user-requested new directory.

**Goal:** 별도 설정과 Python 환경에서 실행·종료·검증 가능한 독립 모델 서버를 구축한다.

**Architecture:** vLLM이 HTTP/engine/model worker를 소유한다. 표준 라이브러리 CLI가 설정 검증과 안전한 process-group 수명 관리를 소유한다. CoEgent smoke만 기존 PlanGenerator를 호출하며 server package 자체는 CoEgent에 의존하지 않는다.

**Tech Stack:** Python 3.11+, TOML, vLLM, CUDA, unittest, urllib, Linux /proc/flock.

**Spec:** [DESIGN.md](DESIGN.md)

## Global Constraints

- 사용자가 지정한 현재 workspace의 새 `LLM_Model_Server` 디렉토리에 구현한다.
- 기존 모델·adapter·CoEgent 파일은 수정하지 않는다.
- loopback 기본값과 offline 모델 로딩을 유지한다.
- 이름/인자/경로는 argv 배열로 전달하며 shell=True를 사용하지 않는다.
- 다른 PID, 다른 process start time, 이미 점유된 포트는 중단·탈취하지 않는다.
- 구현과 설치/CPU 검증/실제 GPU 추론 결과를 구분한다.
- workspace는 Git repository가 아니므로 worktree·commit은 적용할 수 없다.

## Review Focus

1. PID 재사용 또는 변조된 상태: 다른 프로세스에 signal을 보내지 않는 테스트.
2. LoRA 누락과 rank 초과: base 모델로 조용히 실행하지 않는 테스트.
3. 시작 직후 실패: 로그·명시적 실패와 상태 정리를 확인하는 테스트.
4. 준비 상태 이전과 서버 포트 충돌: ready 오인과 중복 실행을 막는 테스트.
5. schema 실패·잘린 출력: smoke가 실패로 반환하는 검증.

## Task 1: Configuration and engine launch contract

Files: `local_llm_server/config.py`, `config/server.toml`, `tests/test_config.py`.

- [x] 테스트를 먼저 작성하고 아직 모듈이 없어 실패하는 것을 확인한다.
- [x] `load_config(path) -> ServerConfig`, `engine_command(config) -> list[str]`, `engine_environment(config) -> dict[str,str]`를 구현한다.
- [x] 경로·숫자·loopback·LoRA rank·timeout/resource 설정의 오류를 거절한다.
- [x] literal argv expectation으로 alias, adapter, GPU budget, JSON options의 소비 경계를 확인한다.

## Task 2: Process lifecycle

Files: `local_llm_server/lifecycle.py`, `local_llm_server/cli.py`, `tests/test_lifecycle.py`, `tests/fixture_server.py`.

- [x] 실제 fixture 프로세스에 대한 lifecycle 테스트를 먼저 작성하고 실패를 확인한다.
- [x] `start`, `status`, `stop`, `run`, `doctor`, `command` CLI와 PID identity/lock을 구현한다.
- [x] lifecycle은 `engine_command()`로 구성한 실제 process group을 실행한다.
- [x] readiness는 health와 served model alias 모두로 판정한다.
- [x] 테스트 suite를 실행하여 start/stop/중복/PID mismatch/실패를 확인한다.

## Task 3: Setup and smoke

Files: `scripts/setup.sh`, `scripts/smoke_coegent.py`, `requirements-runtime.txt`, `README.md`, `.gitignore`.

- [x] 별도 환경에 고정 runtime을 설치하고 exact dependency lock을 생성한다.
- [x] 공통 MissionPlanSchema 요청과 PlanGenerator 계획 검증을 수행하는 smoke를 작성한다.
- [x] 실제 GPU 계획 생성: RAM 약 24 GiB 조건에서 모델·LoRA·HTTP·CoEgent CLI 검증 완료. 샘플 3건 PLAN_READY/NOT_EXECUTED. 의미 오류는 검증 기록에 별도 명시.
- [x] 전체 단위 테스트, CLI 사용법, 실제 설치/version/API smoke 결과를 기록한다.

## Execution ledger

- 2026-10-14: 신규 독립 HTTP 모델 서버 구축은 대화에서 논의한 구조에 대한 사용자의 명시적 구현 요청에 따라 진행한다.
- Ruling: package는 stdlib 관리 CLI와 vLLM runtime으로 분리한다. 모델 HTTP 서버를 새로 구현하여 중복하지 않는다.
- Ruling: 신규 디렉토리 자체를 작업 경계로 사용한다. 현재 workspace는 Git repository가 아니다.
- Ruling: 낮은 RAM과 swap 부족을 resource preflight로 기록한다. 다른 작업 종료 또는 무단 GPU 전체 예약은 하지 않는다.

- Review fixes: Gemma4ForCausalLM architecture override, pinned-runtime LoRA ranks, orphan worker ownership/cleanup, actual plan response finish_reason checks.
- Runtime contract: installed vLLM resolves text class and confirms supports_lora=True without loading weights.
- Validation: 26 CPU tests pass, including actual PlanGenerator HTTP integration; live GPU inference remains resource-blocked.

- 2026-10-14 live validation: explicit user test request; pin Transformers 5.5.4, bind positional model in runtime contract check, add lazy KV-sharing norm compatibility plugin, enforce compact xgrammar output.
- 29 tests pass. Two real server startups and shutdowns succeeded after fixes; GPU processes absent after final stop.
- Scope limitation: plan format validation succeeds, but navigation sample still has inconsistent final_outcome and incomplete success criterion. No physical skills executed.
