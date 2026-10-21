# Executor adapters

CoEgent가 사용하는 executor 공통 계약과 등록 기능을 별도 패키지로 관리합니다. 패키지 자체는 SDK·모델·실행 엔진을 import하지 않습니다. 선택된 plugin만 로딩합니다.

## 설치

```bash
uv pip install --python .venv/bin/python -e Executor_Adapters -e CoEgent_Agent
# 설치 없이 source를 사용할 때
PYTHONPATH=CoEgent_Agent:Executor_Adapters ./.venv/bin/python -m coegent executors
```

## 현재 실행 흐름

```text
raw planner output
 → CoEgent 공통 JSON/스키마/실행 상태 검증
 → ExecutorSession
    → 선택된 adapter.prepare(validated_plan)
    → adapter.execute(PreparedPlan)
    ← SkillResult/Event + cached ExecutionProgress
 → 공통 미션 보고서
```

기본 `coegent` adapter는 `CoEgent_Agent/coegent/executor_adapters/native.py`에서 기존 MissionEngine을 감쌉니다. 조건·재시도·중단, SkillRouter의 권한·안전·관측 검증은 그대로 유지합니다. 계획 전체를 실행하는 모든 경로는 session을 거칩니다. 안전 리셋/비상 fallback/오프라인 복귀의 직접 skill 경로는 기존 관리 동작으로 유지합니다.

`PlanGenerator`/CLI `plan`은 실행기를 생성하거나 연결하지 않습니다. 반환된 canonical 계획은 실행 단계에서 선택된 adapter를 반드시 거칩니다.

## 등록·선택·교체·해제

```python
from coegent import CoEgent
from coegent.executor_adapters import default_registry

registry = default_registry()
registry.register("robot_a", robot_a_factory)
# factory(context, options) -> ExecutorAdapter
agent = CoEgent(planner, backend, executor="robot_a",
                executor_options={"endpoint": "http://127.0.0.1:19000"},
                executor_registry=registry)

# 미션이 끝나고 미완료 작업이 없을 때만 교체 가능
agent.set_executor("coegent")
registry.unregister("robot_a")
```

`robot_a_factory`, `planner`, `backend`는 사용자가 구성하는 객체입니다. 위 코드는 특정 로봇 서버가 이미 구현되어 있다고 가정하지 않습니다. `PlanExecutor`도 같은 executor/options/registry 인자를 받으며 `set_executor()`를 제공합니다.

등록 해제는 신규 선택을 차단합니다. 이미 선택된 agent는 잡고 있는 factory를 유지하므로 미션 중 모듈 삭제로 경로가 바뀌지 않습니다. 완전히 떼려면 미션 종료 → 다른 executor 선택 → 등록 해제 → 패키지 제거 순서를 사용하세요. 자동 hot-unload는 하지 않습니다.

CLI:

```bash
./.venv/bin/python -m coegent executors
./.venv/bin/python -m coegent run --executor coegent \
  --planner file --plan-file CoEgent_Agent/examples/valve_plan.json \
  --command '밸브 확인 후 복귀' --out CoEgent_Agent/results/adapter_run.json
# executor 전용 설정이 필요하면 JSON 객체 파일을 지정
# --executor robot_a --executor-options /path/to/robot_a.json
```

`run`, `request`에서 선택합니다. 기본 native adapter의 options는 빈 객체여야 합니다. 알 수 없는 이름은 모델 로딩·robot admission 전에 거부합니다.

## 계약

| 메서드/속성 | 의미 |
|---|---|
| `prepare(plan)` | 검증된 planner.v1을 실행기 형식으로 변환. 실제 실행/원격 제출 금지 |
| `execute(prepared)` | 실제 실행. canonical 상태·관측·증거 및 cached progress 갱신 |
| `progress` | 동기, 비차단 cached ExecutionProgress. 네트워크 호출 금지 |
| `status()` | 비동기 상태 갱신/조회. ExecutionProgress 반환 |
| `cancel()` | 중단 요청·확인. 실제 확인에 근거한 SkillResult('stop', ..., data={'stopped': True}) |
| `close()` | adapter 소유 자원 해제. 공통 skill backend를 닫지 않음. 이후 비상 cancel은 여전히 가능해야 함 |

`PreparedPlan.executor`는 context.name과 일치해야 합니다. payload는 adapter가 소유하는 형식입니다. execute/cancel 결과는 `coegent.types.SkillResult`와 `Event` 계약을 따릅니다. 잘못된 결과, 음수/비정상 progress, 불확실한 stop은 성공으로 취급하지 않습니다. stop 미확인은 fault latch를 유지해 다음 미션을 차단합니다.

factory는 부작용 없는 구성 작업을 수행해야 합니다. 실제 수행은 execute에서만 시작합니다. context의 router/state/trace/limits/request는 미션 단위이며 다른 미션에 재사용하지 않습니다. options는 JSON 객체이며 선택·생성 시 복사합니다.

외부 서버 연결 adapter는 prepare에서 서버의 필드·명령 형식으로 변환하고 execute/status/cancel에서 HTTP·IPC를 사용하면 됩니다. adapter의 코드 관리 위치와 실행기의 프로세스 위치는 독립입니다. 대상 서버 API가 정해지지 않았으므로 가상의 Local/Remote 서버 연결은 제공하지 않습니다.

**안전 계약:** plugins는 신뢰된 배포 코드입니다. 공통 preflight는 미래 관측을 포함한 모든 실행 단계의 안전을 대신하지 않습니다. 내부 router를 사용하거나 외부 executor에서 동등한 per-step 권한·freshness·중단·예산 검증을 실제로 보장해야 합니다. 성공·실패·관측·증거는 실행 결과에 근거해야 하며 LLM 출력이나 제출 성공으로 채우면 안 됩니다.

## 개별 설치 플러그인

다른 패키지의 pyproject.toml에 factory entry point를 등록할 수 있습니다:

```toml
[project.entry-points."coegent.executors"]
robot_a = "robot_a_adapter:create_adapter"
```

default_registry가 설치된 이름을 발견하고 선택된 factory만 로딩합니다. 중복 이름은 거부합니다. 사용하지 않는 adapter의 SDK를 import하지 않습니다. Python에서 명시적 registry만 쓰려면 `ExecutorRegistry()`에 register하세요.

## 검증

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 ./.venv/bin/python -m pytest CoEgent_Agent/tests -q
```

새 테스트는 native 경계, 다른 payload로 변환하는 등록형 adapter의 실제 실행, invalid plan 차단, 선택/제거, lazy discovery, 미션 중 교체 차단, 취소/close, 잘못된 결과·progress, 중단 미확인, 미완료 close 소유권, CLI 선택을 검사합니다. LLM 추론이나 실제 로봇 실행은 하지 않습니다.
