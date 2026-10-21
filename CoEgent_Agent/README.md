# CoEgent Agent v0.1

## Inspector 감시자 (2026-10-20)

선택적 `coegent.inspector` 모듈은 실행기·서브 에이전트의 완료/오류 보고를 독립된 최신 robot_state·vision과 비교합니다. 상태 비교는 코드로, 오류 해석·시각 판단은 설정한 LLM/VLM으로 수행합니다. 검증된 완료만 상위 호출자에게 확정하고 불일치/오류는 정지 확인 후 기존 예산 안에서 재계획합니다. 관측이 없으면 완료를 확정하지 않습니다.

Python의 `CoEgent(..., inspector=...)`, `PlanExecutor(..., inspector=...)` 또는 CLI `--inspector-config`로 활성화합니다. 외부 보고는 `submit_report()` / `coegent inspect`로 점검합니다. [구조·설정·A2 연결 및 사용 예제](docs/INSPECTOR.md).

`../CoEgent_Agent_Architecture.md`의 **Phase 1** 구현이다. LLM은 Mission Plan만 만들고, 실행·분기·재시도·복구는 Python runtime이 담당한다. **기본 skill은 mock이다. 실제 A2 API 실행은 별도 `a2-isaac` 프로필과 실행 Adapter를 명시적으로 선택한다.**



## PlannerSelector 자동 선택 (2026-10-15)

`command → 대상·키워드·인자 추출 → DB 후보 검색 → LLM 판단`을 추가했다.
후보의 전체 작업이 일치하면 ScriptedPlanner, 없으면 설정된 HTTP/HF 모델로 새 계획을 생성한다.
불명확하면 `NEEDS_CLARIFICATION`과 질문을 반환하며 실행하지 않는다.

```bash
./.venv/bin/python -m coegent plan --planner auto \
  --endpoint http://127.0.0.1:18200 --model planner --command "A로 이동해"
```

`run --planner auto`는 선택 뒤 기존 검증·adapter·실행 경로를 사용한다. 판단 모델은 기본적으로 생성 모델과 공유하며 별도 HTTP/HF 모델도 설정할 수 있다. 기존 직접 선택 모드와 request는 유지한다.

[구조·설정·추가 질문 처리](docs/PLANNER_SELECTOR.md)

2026-10-16: 모호성 우선 프롬프트와 후보 ID/decision별 응답 제약을 적용했다. [실모델 정확도 비교](results/planner_selector_accuracy_20261016/REPORT.md)는 개발·별도 평가 사례를 분리하며, 구조적 JSON 유효성과 의미 판단 정확도를 구분한다.

## 영속 계획 DB (2026-10-15)

`data/plans.sqlite3`에 사용자·상위 에이전트의 계획을 추가하고, 실행에 성공한 전체 계획을 자동 저장한다. 계획 전체의 정규화된 해시와 UNIQUE 제약으로 중복 행을 만들지 않는다. 제목·작성자 등은 비교에서 제외한다.

```bash
./.venv/bin/python -m coegent plans init
./.venv/bin/python -m coegent plans add --plan-file CoEgent_Agent/examples/valve_plan.json --title "밸브 점검"
./.venv/bin/python -m coegent plans list
./.venv/bin/python -m coegent run --planner db --plan-id 1 --command "저장된 계획 실행"
```

`--plan-db`로 파일을 선택한다. `ScriptedPlanner.from_store(store, [id])`는 큐를 소비해도 DB를 삭제하지 않는다. request도 `--plan-id`로 계획을 선택할 수 있다. 실행 모드의 `--no-auto-save-plans`로 자동 저장을 끌 수 있다. 목표 미달/미확인·실패·취소·재전송 및 부분 continuation은 성공 전체 계획으로 자동 등록하지 않는다. MockSkills 성공은 mock provenance로 기록한다.

[전체 사용법과 저장 기준](docs/PLAN_CATALOG.md)

## Executor adapter 선택 (2026-10-15)

모든 계획 실행은 `ExecutorSession → 선택된 ExecutorAdapter`를 거친다. 기본 `coegent` adapter는 기존 내부 MissionEngine을 감싸며 기존 호출자의 동작을 유지한다. 다른 프로세스/외부 서버는 동일한 계약을 구현하는 plugin으로 연결한다. 대상 서버별 실제 adapter 구현은 별도로 필요하다.

공통 계약과 등록은 별도 [Executor_Adapters 패키지](../Executor_Adapters/README.md)에서 관리한다. `CoEgent`와 `PlanExecutor`의 `executor`, `executor_options`, `executor_registry` 인자로 선택하며, `set_executor()`는 미션과 미완료 작업이 없을 때만 허용한다. `plan` 모드는 실행 adapter를 만들지 않는다.

```bash
./.venv/bin/python -m coegent executors
./.venv/bin/python -m coegent run --executor coegent \
  --planner file --plan-file CoEgent_Agent/examples/valve_plan.json \
  --command "밸브 확인 후 복귀" --out CoEgent_Agent/results/adapter_run.json
```

`run`/`request`에는 `--executor-options /path/options.json`으로 JSON 객체 설정을 전달할 수 있다. 등록된 이름만 선택하며 설치된 플러그인 중 선택된 모듈만 로딩한다. 미션 보고서의 `executor`와 trace의 `executor_prepare`/`executor_execute`/`executor_cancel`로 실제 경계를 확인할 수 있다.

## 계획 생성 / 실행 분리 API (2026-10-13)

계획 생성과 실행을 별도 공개 API로 사용할 수 있다. 공통 `planner.v1` 계약과
기존 런타임을 재사용한다. CoBiz·MCP Adapter와 실제 로봇 backend는 아직 연결하지 않았다.

```text
User / Upper Agent / CoBiz
  -> PlanGenerator.generate(command, state)
       Context Builder -> optional PlannerSelector -> selected Planner -> PlanValidator -> snapshot prefix preflight
  -> planning report [plan: planner.v1, execution_status: NOT_EXECUTED]
  -> PlanExecutor.execute(plan, current_state, selected executor adapter)
       admission -> revalidation -> safety/freshness -> ExecutorSession -> ExecutorAdapter
       -> native MissionEngine -> SkillBackend OR registered executor plugin
  -> execution report / REPLAN_REQUIRED
```

- `PlanGenerator`는 backend, robot admission, router, engine을 생성하지 않는다.
  알려진 선행조건 위반이나 잘못된 계획을 거절해도 `stop`을 포함한 Skill 호출을 하지 않는다.
- `PLAN_READY`는 구조·권한·입력 snapshot의 확정 가능한 prefix를 검사했다는 뜻이다.
  미래 관측은 `deferred`이며 실제 실행 가능성·임무 완료를 보장하지 않는다.
- `PlanExecutor`는 전달받은 계획을 실행 직전 다시 검증한다. 기존 정지·fault latch·
  중복 요청·trusted state·목표 검증을 유지하며 LLM 호출과 자동 JSON repair를 하지 않는다.
  `REPLAN_REQUIRED`는 상위 호출자에게 반환한다.
- 계획 생성 입력은 호출자가 제공하는 canonical `State`다. 실기 실행에서는
  `TrustedStateProvider`와 strict 설정 등 기존 운영 경계를 별도로 연결해야 한다.
- timeout·취소 뒤 Planner coroutine의 정리가 남아 있으면 같은 생성기의 재호출을
  거절한다. 실제 원격 추론 종료는 serving의 취소 지원에 달려 있다.
- 기존 `CoEgent.run()`/`run_request()`와 CLI `run`은 계획+실행 호환 경로로 유지한다.

Python에서 단계별 호출:

```python
from coegent import PlanGenerator, PlanExecutor
from coegent.planner import ModelServerPlanner
from coegent.scenarios import initial_state
from coegent.skills import MockSkills

planner = ModelServerPlanner("http://127.0.0.1:18200", "planner")
generator = PlanGenerator(planner)  # robot backend 불필요
output = await generator.generate("A에서 밸브를 확인하고 복귀해", initial_state())

if output["status"] == "PLAN_READY":
    # 실행 단계는 기본 native ExecutorAdapter를 반드시 거친다.
    executor = PlanExecutor(MockSkills())  # LLM 불필요, 이 예제는 mock 실행
    report = await executor.execute(output["plan"], initial_state(),
                                    command="A에서 밸브를 확인하고 복귀해")
```

위 `await` 예제는 async 함수 안에서 실행한다. `initial_state()`는 테스트용 입력이다.
실제 센서 State를 가장하지 않는다. 상위 시스템의 승인 ID·만료·목표를 유지하려면
`MissionRequest(plan=...)`를 구성해 `PlanExecutor.execute_request(request, state)`를 호출한다.
`execute()`는 ID를 생략하면 새 ID를 생성한다. 같은 작업의 재전송은 같은 ID/revision을
유지해야 하며, 현재 dedup은 프로세스 재시작 이후까지 지속되는 journal이 아니다.

CLI 계획 생성만 실행 (저장된 계획으로 경계 확인):

```bash
PYTHONPATH=CoEgent_Agent:Executor_Adapters ./.venv/bin/python -m coegent plan \
  --planner file --plan-file CoEgent_Agent/examples/valve_plan.json \
  --command "A에서 밸브를 확인하고 복귀해" \
  --state CoEgent_Agent/examples/state.json \
  --out CoEgent_Agent/results/plan_only.json
```

`--planner inprocess|hf|http`도 동일한 `plan` 진입점을 사용한다. `file`은 저장된 계획 입력이며
자연어 추론을 수행하지 않는다. 결과 파일은 report envelope이며 계획 본문은 `plan` 필드다.
`--state`를 생략하면 mock fixture를 사용하고 `run_metadata.state_source`에 기록한다.
실행만 하는 기존 CLI `request`는 이제 `PlanExecutor`를 사용한다. 기존 `MissionRequest`
형식을 유지하며 `plan` 필드는 필수다. planner 응답의 계약 hash는 추적 정보로,
인증이나 실행 허가를 대신하지 않는다.

[분리 아키텍처와 이미지](../CoEgent_Planner_Executor_Architecture_v2_20261013.md)

## 기존 통합 실행 구조

```text
Approved MissionRequest / local Command
    -> RobotAdmission (identity / expiry / ownership / stop fault)
    -> Context Builder
    -> Provided plan OR [optional PlannerSelector -> Planner (Scripted / inprocess / HTTP)]
    -> Plan Validator (planner.v1 + exact skill arguments)
    -> ExecutorSession -> selected ExecutorAdapter -> Mission Engine
         -> Trusted observation refresh + Safety/Authority Gate (매 dispatch)
         -> Skill Router -> MockSkills
         <- SkillResult + Event -> State Update
         -> continue / bounded retry / fallback / abort / bounded replan
    -> JSON execution report + approved-goal verifier + evidence
```

```text
CoEgent_Agent/
  coegent/
    planning.py        PlanGenerator, backend-free generation, shared planning context
    execution.py       PlanExecutor, provided-plan execution without LLM
    agent.py           legacy combined orchestration, execution safety/reporting
    admission.py       robot owner, request cache, fault latch, offline decision
    observations.py    trusted snapshots, field-level provenance/freshness
    goals.py           four fixed goal checks, separate execution evidence
    planner.py         planner adapters, HF chat-template application
    planner_selector.py bounded LLM reuse / generate / clarify decision
    plan_features.py   candidate hints: entities, action keywords, numeric arguments
    planning_dispatch.py shared guarded selector-to-planner dispatch
    plan_store.py      SQLite catalog, command aliases and indexed candidate retrieval
    validation.py      strict JSON and plan/argument validation
    engine.py          deterministic node interpreter
    safety.py          deployment-owned authority and safety policy
    skills.py          backend contract, mock driver, router, state updates
    types.py           states, limits, results, events
    registry.py        shared skill contracts
    scenarios.py       independently authored mock fixtures
    schemas/           historical Planner-SFT v1 snapshots (not runtime source)
    cli.py             plan / request / run / demo / suite
  coegent_contracts/   Dataset / Planner / Runtime shared contract
    schemas/           SkillContract / StateContract / MissionPlanSchema
    config.py          one RuntimeConfig for policy and execution budgets
    semantics.py       fixed precondition checks and validated state effects
    preflight.py       known-prefix checks; no predicted sensor results
  examples/            state.json, valve_plan.json
  tests/               runtime and safety regression tests
  scripts/smoke_hf.py  existing Gemma Planner-SFT adapter integration check
  results/             execution traces and implementation report
```

## 바로 실행

아래 명령은 `local_ai_study` 루트 기준이다. 기본 mock 실행에는 GPU/Transformers가 필요 없다.

```bash
./.venv/bin/python -m pip install -e ./Executor_Adapters -e ./CoEgent_Agent
./.venv/bin/python -m coegent demo --scenario valve_open
./.venv/bin/python -m coegent demo --scenario retry_exhausted
./.venv/bin/python -m coegent suite
```

설치 없이 실행하려면 명령 앞에 `PYTHONPATH=CoEgent_Agent:Executor_Adapters`를 붙인다. 결과는 기본적으로 `CoEgent_Agent/results/`에 저장된다. `--out`으로 변경할 수 있으며 같은 경로는 덮어쓴다.

사용자가 작성한 계획 실행:

```bash
PYTHONPATH=CoEgent_Agent:Executor_Adapters ./.venv/bin/python -m coegent run \
  --planner file \
  --command 'A에서 빨간 밸브를 확인하고 열려 있으면 닫고 복귀해.' \
  --state CoEgent_Agent/examples/state.json \
  --plan-file CoEgent_Agent/examples/valve_plan.json \
  --max-repairs 0 --max-replans 0 \
  --out CoEgent_Agent/results/file_plan_run.json
```

`file`/demo는 미리 작성한 계획을 사용하는 runtime 시험이다. 자연어 이해 능력을 흉내 내는 rule-based planner가 아니다.

## Inprocess E4B 모델 및 기존 Planner-SFT 연결

`InprocessModelPlanner()`와 CLI `--planner inprocess`는 **E4B IT QAT Q4_0 GGUF + llama.cpp**를 CoEgent 프로세스 안에 로드한다. 기본 모델은 `LLM_Model_Server/model/gguf/gemma-4-E4B_q4_0-it.gguf`이며, 외부 E4B 서버와 같은 설치 라이브러리(b96806d), thinking=false 템플릿, JSON schema 제약을 사용한다. HTTP 연결이나 별도 모델 프로세스를 만들지 않는다. 기본 ctx4096, threads8, slot1, GPU offload all이며 LoRA는 없다.

```bash
# 설치된 llama.cpp의 동일 소스/헤더에 연결하는 작은 native bridge 빌드 (최초 1회)
./.venv/bin/python LLM_Model_Server/scripts/build_inprocess.py

PYTHONPATH=CoEgent_Agent:Executor_Adapters ./.venv/bin/python -m coegent plan \
  --planner inprocess --command '현재 배터리를 확인해.' \
  --state CoEgent_Agent/examples/state.json \
  --out CoEgent_Agent/results/e4b_inprocess_plan.json
```

`--planner auto --generation-planner inprocess`도 같은 GGUF를 사용한다. 기본 `--selector-backend same`은 Selector와 Planner가 하나의 모델을 공유한다. API에서는 `with InprocessModelPlanner() as planner:` 또는 `planner.close()`로 자원을 해제한다. native bridge는 설치 runtime의 라이브러리 해시와 ABI를 검사하며, runtime을 교체하면 다시 빌드해야 한다. GGUF LoRA는 `--adapter`로 명시할 수 있다.

기존 HF BF16 경로는 `TransformersModelPlanner` / `--planner hf`로 명시적으로 유지한다. 이 경로는 torch/transformers/accelerate가 필요하며 GGUF/llama.cpp와 다른 엔진이다. HTTP 모델명은 명시해야 한다. 외부에서 동일한 E4B GGUF를 쓰려면 `LLM_Model_Server/config/e4b.toml`을 사용한다.

기존 **v0.4 merged BF16 + Planner-SFT v1 LoRA**도 `--model`과 `--adapter`로 명시적으로 선택할 수 있다(peft 필요). E2B용 adapter를 E4B에 사용하면 안 된다.

```bash
PYTHONPATH=CoEgent_Agent:Executor_Adapters ./.venv/bin/python -m coegent run \
  --planner hf \
  --model models/robot-agent-sft-merged/gemma4-e2b-robot-sft-v0.4-merged \
  --adapter Planner_SFT_training_v1/models/gemma4-e2b-sft-v0.4_planner-sft-v1_attention-mlp_r16_a32_lr1e-4_ep3_bs2x8/final_adapter \
  --command '현재 배터리를 확인하고 현재 위치를 확인해.' \
  --state CoEgent_Agent/examples/state.json \
  --out CoEgent_Agent/results/planner_run.json
```

HF는 `apply_chat_template()` 후 `add_special_tokens=False`로 중복 BOS를 피하고 greedy decoding을 사용한다. 원래 학습의 `COMMAND / STATE` 표면을 유지하되, runtime용 중첩 state와 압축된 노드/skill 계약을 추가했다. **학습 입력과 완전히 동일한 분포는 아니며, 일반화 성능은 별도 평가 대상**이다. 원본 응답과 검증/정규화 기록은 trace에 남긴다.

HTTP planner도 선택 가능하다:

```bash
PYTHONPATH=CoEgent_Agent:Executor_Adapters ./.venv/bin/python -m coegent run \
  --planner http --endpoint http://127.0.0.1:18200 --model planner \
  --command '현재 위치를 확인해.' --state CoEgent_Agent/examples/state.json
```

HTTP는 기본적으로 runtime validator와 같은 MissionPlanSchema를 `response_format.json_schema`에 `strict=true`로 요청한다. 서버가 이를 거부하면 실패로 보고하며 자유 생성으로 자동 재시도하지 않는다. 서버가 실제로 제약을 적용하는지는 별도 검증해야 하므로 report에는 `generation_mode=json_schema_requested`로 기록한다.

CLI `--generation-mode auto|json_schema|unconstrained`에서 auto는 HTTP에 schema 요청, HF에는 자유 생성을 사용한다. **HF grammar decoder는 아직 연결하지 않았으며**, HF에 `json_schema`를 요청하면 모델 적재 전에 명시적으로 거부한다. HTTP에 `unconstrained`를 명시하면 schema 요청을 끌 수 있다. Gemma GGUF의 HF token-ID `/completion` 경로와 Jetson 성능은 별도 검증 대상이다.

## 공유 계약 v1

규칙의 단일 원본은 `coegent_contracts`다. skill마다 정확히 다음 다섯 필드만 사용하며 표현식 실행기나 범용 DSL은 없다.

- SkillContract: `name`, `args_schema`, `preconditions`, `result_schema`, `effects`
- StateContract: `robot/world/mission/observations` 검사와 조건 조회
- MissionPlanSchema: 기존 `planner.v1` 문법 + SkillContract에서 만든 exact argument schema
- RuntimeConfig: 배포 정책과 실행 예산. Planner가 변경할 수 없음

Dataset의 신규 canonical 입력, Planner prompt, validator, preflight, dispatch 및 state update가 같은 정의/검사 함수를 사용한다. 선행조건과 effects는 고정 Python identifier이며, driver 실행이나 orchestration은 계약 패키지에 넣지 않았다. report/context에 contract version/hash와 schema hash를 기록한다.

```python
from coegent.agent import CoEgent
from coegent_contracts import RuntimeConfig

agent = CoEgent(planner, backend, config=RuntimeConfig(
    min_battery=15, max_repairs=1, max_replans=1, skill_timeout_s=5))
```

기존 `policy`/`limits` 인자는 호환 wrapper로 유지하되 `config`와 함께 넘길 수 없다. 정적 plan bound(128노드/깊이32/100,000자)는 배포 예산과 별도의 입력 방어 제한이다.

**기존 Planner-SFT 2,000개는 flat State인 과거 자료다.** 원본을 자동 변환하거나 재학습하지 않았다. 신규 데이터는 `Planner_SFT_dataset_v1.planner_data.shared_contract.prepare_record()`로 canonical State/공통 plan 검사/preflight/prompt를 만든다. legacy 변환에는 명시적인 key mapping이 필요하며 누락된 배터리·경로·inspection 정보를 추측하지 않는다. 과거 검증은 `legacy=True`, 과거 학습 재현은 `--allow-legacy-contract`를 명시해야 한다. 이 옵션은 새 계약 준수 인증이 아니다.

[구현·검증 리포트](results/shared_contract_v1_implementation_report.md)

## Mission Plan 계약

학습된 모델의 `planner.v1`을 기준으로 한다. 원본 아키텍처 문서의 `navigate(destination)`, `steps`, `if/var/op`는 개념 예시이며 실제 계약은 `navigate_to(target)`, `sequence`, `condition/source/operator`다.

| Node | Runtime 의미 |
|---|---|
| `sequence` | `nodes`를 순서대로 실행, 실패 시 나머지 중단 |
| `skill` | exact argument 검증 → 현재 상태 safety 검사 → backend 호출 |
| `condition` | dotted state path 비교, 선택된 branch만 실행 |
| `retry` | 단일 skill만 최대 1~3회 실행, 전체 시도 횟수 기준 |
| `fallback` | 명시된 복구 `sequence` 실행 |
| `abort` | 정지 → 선택적 안전 복귀 → ABORTED, 뒤의 노드 실행 안 함 |
| `replan_required` | 정지 확인 → 남은 작업 context로 제한된 재계획 |

- `retry`는 `EXPECTED_FAILURE`, `recoverable=true`, `retry_if` 이유 일치 때만 반복한다. `recoverable`은 driver가 재실행 안전성을 보장한다는 계약이다.
- retry exhaustion에만 연결된 fallback을 실행한다. `SAFETY_ABORT`는 retry/fallback/replan으로 우회할 수 없다.
- `fallback`은 자동 route 탐색기가 아니다. 실제 우회 경로 실행은 해당 경로를 지원하는 navigation skill/driver의 책임이다.
- 조건에 없는 경로, `null`, 비교 타입 오류가 있으면 false로 간주하지 않고 `REPLAN_REQUIRED`로 중단한다.
- `skill.save_as`는 선택적 runtime 확장이다. 검사 결과는 기본적으로 `inspection.*`, 각 skill명 또는 `results.*`로 참조한다. 이동 후 이전 관측은 무효화한다.
- retry 안의 복합 sequence는 완료한 부작용을 중복 실행할 위험 때문에 v0.1에서는 거부한다.

### 검증 및 정규화

JSON 중복 키, NaN/Infinity, 허용되지 않은 skill/argument, 과도한 깊이/길이를 거부한다. 의미를 추정하는 JSON 복구는 하지 않는다.

자동 정규화는 두 가지뿐이며 전후 값이 trace에 기록된다:

1. 응답 전체가 단 하나의 JSON Markdown 코드펜스로 감싸진 경우 외곽 fence 제거. 앞뒤 해설/다중 객체는 거부.
2. `return_home(target=...)`의 target이 **설정된 home과 정확히 일치**할 때만 `return_home({})`로 변환.

정규화 후에도 schema/argument/safety 검증을 모두 수행한다. 모델 출력의 raw JSON 준수율과 정규화 후 실행 가능률은 구분해야 한다.

최소 preflight는 현재 상태로 확정되는 branch/prefix만 검사한다. 첫 skill의 결과나 미래 관측이 필요하면 `deferred`로 남긴다. 확정된 안전 위반은 schema repair로 보내지 않는다. `SAFETY_ABORT`는 중단하고, 복구 가능한 `REPLAN_REQUIRED`만 실행 중 실패와 같은 stop-confirm/replan 예산으로 처리한다. 이 검사는 전체 계획의 실행 가능성 증명이 아니며 매 dispatch safety를 대체하지 않는다.

## Safety 및 State

- 출발/조작 배터리 기본 하한 15%, 복귀/충전 3%. 데모용 정책이며 실제 로봇 요구사항으로 교체해야 한다.
- estop/위치추정/배터리가 불명확하면 이동과 조작 금지.
- 경로가 clear가 아니면 navigation을 실행하지 않고 재계획 요청.
- 조작은 현재 위치에서 대상 존재를 관측한 뒤에만 허용. 그리퍼 점유, arm readiness, 놓을 위치도 확인.
- authority whitelist는 deployment 코드가 결정한다. Planner는 변경할 수 없다. `stop`은 항상 허용.
- `currently_allowed_skills`는 현재 상태 기준 advisory이며, 실제 args와 현재 상태는 dispatch 때 다시 확인한다.
- driver의 성공 결과도 필수 필드/타입을 검증한다. 알려진 필드만 state에 반영하고 반환된 임의 권한/정책 필드를 적용하지 않는다.
- 같은 backend를 공유하는 agent는 RobotAdmission 하나를 공유하여 mission 실행을 겹치지 않는다. caller의 state는 복사해 사용하고 최종 state를 반환한다.

기본 제한: 노드 실행 128회, replan 1회, JSON repair 요청 1회, skill 5초, planner 90초, mission 180초. 안전 정지 확인은 mission deadline 이후에도 별도 skill timeout만큼 시도하므로 전체 응답은 deadline을 조금 초과할 수 있다. HF 취소는 다음 generation step에서 협력적으로 적용된다.

timeout/예외/취소/replan 종료는 공통 `engine.stop()` 확인 경로를 사용한다. 정지 미확인은 `stop_confirmation.confirmed=false`, `execution_status=SAFETY_ABORT`로 승격하고 원래 원인은 `failure_reason`에 보존한다. 실패한 확인을 이후 ACK로 덮어쓰거나 그 상태에서 재계획하지 않는다. 취소는 cleanup 후 `CancelledError`를 다시 전달하며 `agent.last_report`로 안전 결과를 확인할 수 있다. 비협력 driver task를 취소해도 하드웨어가 멈췄다는 보장은 없으므로 watchdog은 별도로 필요하다.

## 결과 해석

`SUCCESS`는 **주어진 계획의 노드 실행 완료**다. 자유문장 `success_criteria`를 운영 성공 판정에 사용하지 않는다. 승인된 `MissionRequest.goals`를 지정해야 고정된 goal verifier가 판정한다. 기존 `run(command, state)`와 mock suite처럼 목표를 지정하지 않은 호출은 `mission_success=null`, `goal_status=not_evaluated`를 유지한다.

`execution_status`와 `goal_status`를 분리한다. `status`는 execution_status 호환 alias다. goal_status는 achieved/unmet/unknown/not_evaluated이며, 정상 SUCCESS와 achieved가 함께 있을 때만 mission_success=true다. 최종 상태 증거가 없거나 오래됐으면 unknown이다. snapshot mock에서 얻은 achieved는 실제 센서/물리 미션 검증이 아니다.

inspection 미수행은 정상 실행이 완료되고 미확인 작업/fault가 없을 때 `unmet`으로 판정한다. Agent가 기록한 `inspection_history_complete`는 이번 실행의 inspection 이력에 관한 값이며 최종 센서 상태의 신뢰도와 별개다. 중단된 실행이나 순수 `verify_goals()` 호출의 기본값처럼 완전한 이력을 확인할 수 없는 경우, inspection 증거 부재는 `unknown`을 유지한다. 승인된 상위 revision의 continuation도 이전 revision의 Goal evidence를 현재 호출로 가져오지 않으므로 이력을 완전하다고 선언하지 않는다. 이 값은 LLM/요청이 지정하는 정책이 아니다.

종료 상태: `SUCCESS`, `ABORTED`, `REPLAN_REQUIRED`, `SAFETY_ABORT`, `INVALID_PLAN`, `PLANNER_ERROR`, `CANCELLED`, `HELD`, `REJECTED`. CLI `run`은 SUCCESS이면 0, 그 외 완료 상태이면 2를 반환한다. CLI `request`는 실행 SUCCESS라도 명시한 목표가 unmet/unknown이면 2다. demo/suite는 기대했던 abort까지 올바르게 처리하면 0이다.

## Mission Executive 운영 경계

```python
from time import time
from coegent import CoEgent, Goal, MissionRequest

agent = CoEgent(None, backend)  # snapshot mock; no inference for a provided plan
request = MissionRequest(
    mission_id="operator-approved-001", revision=1, expires_at=time() + 60,
    command="A에서 밸브를 확인하고 복귀해", plan=approved_plan,
    goals=(Goal("inspected_target", "red_valve"), Goal("at_location", "HOME")),
)
report = await agent.run_request(request, state)
```

goals는 at_location/inspected_target/holding_object/valve_state 네 유형뿐이다. 누적 성공 inspection evidence는 이동 후 현재 observation이 지워져도 유지한다. LLM이 goals/권한/정지 latch를 수정하지 않는다. 제공된 plan도 공통 validator/preflight/safety를 통과한다. Planner가 없고 재계획이 필요하면 안전 정지 후 REPLAN_REQUIRED로 종료한다.

`coegent request --request-file <JSON> --state <JSON> --out <JSON>`는 이 envelope를 접수하는 **mock CLI**다. 필드는 mission_id/revision/expires_at(UTC epoch seconds)/command/plan/goals만 허용하며 policy injection은 거부한다. CLI에는 실제 sensor adapter가 없으므로 `--state-mode strict`로 실기 기능을 가장하지 않고 설정 오류로 거부한다.

### 소유권ㆍ정지 faultㆍ중복 요청

- stop 미확인, 물리 side-effect 가능/불확실한 driver 예외, 실행 timeout, 취소 미확인은 robot fault를 다음 mission까지 유지한다. 새 agent/State/계획/재연결로 해제하지 않는다. 공통 effects가 non-physical이고 호출별 driver 증거가 physical_side_effect_started=false인 종료된 예외만 recoverable EXPECTED_FAILURE로 처리한다.
- 같은 backend 객체는 기본적으로 같은 guard를 쓴다. 같은 물리 장치를 다른 adapter로 표현하는 경우 동일한 명시적 robot_id/guard를 사용해야 한다.
- 프로세스 간 배타성이 필요하면 `RobotAdmission.for_backend(backend, robot_id="orin-nx-1", lock_dir="/shared/robot-locks")`를 만들고 `CoEgent(..., admission=guard)`에 주입한다. 모든 소유 프로세스/컨테이너는 **같은 lock directory를 공유**해야 한다. 다중 host/서로 다른 lock directory를 보호하는 분산 잠금은 아니다.
- process-lock owner 및 strict state mode는 startup 안전 확인을 요구한다. `confirm_safe_reset()`은 ACK 전후 trusted stopped=true/estop=false, pending driver 없음, 새로운 stop ACK 성공을 모두 요구한다. 명령 요청에는 reset/clear-fault 권한이 없다. fork된 자식은 상속한 guard로 실행할 수 없고 소유 프로세스 PID를 다시 확인한다.
- 동일 ID/revision/payload는 재실행하지 않고 진행 상태/저장 report를 반환한다. payload 충돌, 낮은 revision, 만료, 실행 중 높은 revision은 거부한다. 최초 접수는 revision=1이며, 상위 revision은 보관된 continuation 이력을 요구한다. 이력이 만료ㆍ퇴출ㆍ재시작으로 사라졌으면 revision_history_unavailable로 거부한다. 상위 시스템이 완료 작업을 재확인한 뒤 새 mission_id로 잔여 작업을 승인해야 한다. command 문자열 dedup은 하지 않으며 오래된 mission_id를 새 미션에 재사용하지 않는다.
- 요청 기록은 유효기간과 cache capacity로 제한한다. 유효한 기록을 지워 새 요청을 받지 않고 포화 시 거부한다. UTC high-water와 monotonic deadline으로 시계 rollback 후 만료 요청의 부활을 막는다. 센서/연결 응답을 기다린 뒤 dispatch 직전에도 만료를 다시 검사한다. **dedup은 프로세스 생존 기간ㆍ기록 유효기간 내 보장**이며 재시작 자동 resume/exactly-once는 구현하지 않는다. CLI를 새 프로세스로 반복 실행하는 것은 지속적인 dedup 서비스가 아니다.

### Trusted State와 offline

```python
from coegent import RobotAdmission
from coegent_contracts import RuntimeConfig

guard = RobotAdmission.for_backend(backend, robot_id="orin-nx-1", lock_dir="/shared/robot-locks")
agent = CoEgent(planner, backend, admission=guard, state_provider=trusted_sensors,
    config=RuntimeConfig(state_mode="strict", max_observation_age_s=1.0,
                         max_inspection_age_s=2.0))
safe = await agent.confirm_safe_reset()
```

위 age 숫자는 예시이며 장치/센서 주기별로 검증해야 한다. `TrustedStateProvider.snapshot()`은 ObservationSnapshot(values, observed_at)를 반환한다. values는 robot/world 및 검증된 inspection만 제공하고, observed_at은 `robot.estop`, `world.paths.A` 같은 필드별 **로컬 monotonic 관측 시각**이다. inspection은 `inspection` 키의 시각을 사용한다. 수신 시각을 새로 찍어 오래된 관측을 최신으로 위장하면 안 된다. 누락/만료 필드는 caller의 예전 값으로 대체하지 않는다. driver inspection evidence는 보수적으로 skill 시작 시각을 기준으로 age를 계산한다. 연결 검사 등 기다리는 작업을 마친 뒤 센서를 갱신하고, dispatch 직전에는 await 없는 admission/age/safety 검사를 적용한다.

strict state 모드, robot process-lock, trusted safety reset을 모두 연결해야 실기 운영 경계로 사용할 수 있다. `state_mode=strict`만 지정하면 센서 경계만 엄격해질 뿐 다른 프로세스를 자동 탐지하는 것은 아니다. snapshot 모드는 과거 mock/평가 호환용이며 실기 안전 기능으로 간주하지 않는다.

remote 의존 실행은 `remote_required=True`와 로컬 `connection.connected()` async adapter를 함께 설정한다. 기본 offline_policy는 hold다. continue는 이미 승인한 계획의 local_skills allowlist 안에서만 동작하며, 추가 remote planning/승인이 필요하면 hold한다. return_home은 로컬 설정으로 승인한 경우만 시도하고 기존 배터리/경로/위치추정/권한 검사를 우회하지 않는다. active skill 단절은 취소/stop ACK를 거치며 비협력 driver가 남으면 fault를 latch한다.

재연결 자체는 자동 resume가 아니다. 동일 요청 재전송은 기존 report를 반환하며, 새 승인 revision은 이전 누적 completed/remaining을 Planner feedback으로 전달한다. 재계획/후속 revision의 dispatch는 공유 effects로 식별한 물리 변경 skill이 완료 이력의 동일 name/args와 일치하면 REPLAN_REQUIRED로 차단한다. 관측/log/stop은 반복할 수 있다. 의도적으로 같은 물리 동작을 새로 수행하려면 별도 승인된 mission_id를 사용한다. 의미상 다른 skill 간 동등성이나 crash 이후 자동 재실행은 추론하지 않는다. 상위 요청자는 잔여 작업을 승인해야 한다. 단절 복귀도 원래 mission deadline/요청 만료/연결 감시 안에서 수행하며 안전 정지 cleanup만 deadline 이후 별도 timeout을 갖는다. HTTP 오류를 곧바로 전체 네트워크 단절로 해석하지 않는다.

[운영 경계 구현 결과](results/mission_executive_boundaries_implementation_report.md)

## 테스트

```bash
PYTHONPATH=CoEgent_Agent:Executor_Adapters PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  ./.venv/bin/python -m pytest CoEgent_Agent/tests -q
./.venv/bin/python CoEgent_Agent/scripts/smoke_hf.py
```

기존 SFT/locked holdout을 재사용하지 않은 신규 runtime fixture다. 시험지 점수나 Jetson 지연시간 목표를 달성했다고 해석하면 안 된다.

## 다음 연결 지점

기본 skill runtime의 연결 지점은 `SkillBackend.execute(name,args,snapshot) -> SkillResult`를 구현한 adapter를 `SkillRouter`에 주입하는 것이다. 별도 A2 실행 Adapter의 ROS2·Nav2·카메라 연결은 아래 절을 참고한다. 기본 runtime의 실기 연결 전 필수 작업:

- 구현된 TrustedStateProvider에 실제 센서 adapter 연결, 장치별 freshness 한계 검증, 이동 중 독립적인 collision/estop 제어
- 취소 확인, idempotency, skill 실패 시 부분 실행 결과/재시도 안전성 계약
- 네 가지 goal 외 실제 배달/물체 배치 등 필요한 목표의 관측 근거 설계, 지속 journal과 재시작 복구
- 인증된 transport와 planner endpoint, Jetson runtime prompt 호환 및 end-to-end latency 측정

현재 Python safety gate는 로봇의 인증된 emergency-stop controller를 대체하지 않는다.

### Driver 예외의 물리 side-effect 경계

SkillContract는 다섯 필드를 그대로 유지한다. effects는 고정된 `observation.*`, `physical.*`, `internal.*` 식별자를 사용한다. `coegent_contracts.semantics.has_physical_effect(contract)`가 공통 판정 함수이며, 알 수 없거나 비어 있는 effects도 물리 영향 가능으로 판정한다. 새 effect는 임의 문자열로 등록할 수 없다.

- inspect_target: observation.target_state; get_pose: observation.pose
- navigate_to / rotate: physical.robot_pose
- pick_object: physical.arm_state + physical.held_object
- physical effect는 동작 가능성이지 실행 완료 증거가 아니다. 상태 갱신에는 검증된 성공 결과만 사용한다.

기존 3인자 execute는 호환 유지된다. 호출별 증거를 지원하려면 로컬의 신뢰된 driver에 선택적으로 아래 메서드를 구현한다. Router는 지원 시 이 메서드를 호출하고 매 호출 새로운 DriverExecutionState를 전달한다.

```python
async def execute_with_state(self, name, args, snapshot, execution):
    # An actually sensor-only implementation may confirm no actuation.
    execution.record_physical_side_effect(False)
    return await self.read_sensor(name, args)
```

actuator 호출을 시작할 가능성이 있는 지점에서는 **명령 전송 전에** `execution.record_physical_side_effect(True)`로 기록해야 한다. 한번 True이면 False로 되돌릴 수 없다. None은 미확인이며, 0/문자열 등 잘못된 보고도 안전 분기로 처리한다. plan/LLM/SkillResult.data의 필드를 이 증거로 채택하지 않는다. 이름이 inspect라고 자동 False로 기록하거나 계약만 보고 실제 실행을 확인했다고 주장하면 안 된다.

종료된 driver exception은 non-physical contract + 명시적 False일 때만 recoverable EXPECTED_FAILURE다. 나머지는 SAFETY_ABORT와 persistent fault다. 3인자 legacy driver의 예외는 실행 증거가 없으므로 계속 fault를 유지한다. RuntimeError/OSError/TimeoutError/ValueError 종류에 따른 안전 분류는 없다. runtime 자체 deadline 초과, 취소 미확인, stop ACK 실패의 기존 fail-closed 처리는 유지한다.

[Effects 기반 stop/fault 수정 및 검증](results/stop_fault_effects_20261007/stop_fault_effects_report.md)

## a2-llm-isaac 실제 API로 계획 생성

별도 `a2-isaac` 툴 프로필을 사용하면 기존 mock 스킬 대신 실제 소스에서 확인한
`RobotBackend.go_to`, `localize`, `refresh_pose`, `check_battery`,
`WorldState.capture_views` / `describe` 등의 API로 계획을 생성한다.

```bash
bash CoEgent_Agent/scripts/plan_a2_isaac.sh --command 'reception으로 이동해.'
```

이 명령은 E4B GGUF + native llama.cpp에서 Selector와 Planner를 공유하고,
`CoEgent_Agent/results/a2_isaac_plan.json` 및 터미널에 계획을 출력한다.
위 `plan_a2_isaac.sh` 명령은 **계획/출력만 수행**하며 ROS·시뮬레이터·로봇 API를 실행하지 않는다.
`PLAN_READY`는 API 계약 검증 결과이며 `execution_ready=false`로 표시한다.
실행은 아래 별도 Adapter로 연결한다. 영상 반환값 참조는 지원하지만 임의의 Python 반환값을 조건으로 쓰는 기능은 지원하지 않는다.

도구 목록, 인자, 지도 장소 입력, 카메라 결과 참조, 저장/재사용 명령과 제한은
[프로필 사용법](coegent/profiles/README.md)에 설명한다. 일반 CLI의 기본 mock 프로필은 유지한다.
## Optional A2 Isaac execution

The API profile can execute through the separate `A2_Execution_Adapter` package:

```bash
bash A2_Execution_Adapter/scripts/run_a2_isaac.sh --command '현재 배터리 잔량만 확인해.'
```

This explicitly selects `--tool-profile a2-isaac --executor a2-isaac`. Default
settings prohibit motion. See [adapter setup, simulation options and supported
runtime checks](../A2_Execution_Adapter/README.md). Plan mode remains side-effect free.


## LangGraph orchestration

`plan`, `run`, `request`와 A2 실행 경로는 LangGraph 노드와 조건 분기로 동작합니다. 기존 planner/adapter/Inspector와 안전·중복 실행 방지 계약은 유지합니다. `python -m coegent graph --flow native`로 모델·로봇 실행 없이 그래프를 확인할 수 있습니다. `PlanningSession`은 SQLite에 추가 질문을 저장하고 답변으로 재개하는 계획 전용 API입니다. 실제 로봇의 재시작 후 자동 주행 재개는 제공하지 않습니다. [설정·노드 구조·API와 복구 범위](docs/LANGGRAPH.md).
