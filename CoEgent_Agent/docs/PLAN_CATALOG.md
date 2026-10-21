# 계획 DB

기본 DB는 `CoEgent_Agent/data/plans.sqlite3`입니다. SQLite 파일이므로 별도 서버 프로세스나 설치가 필요하지 않습니다. `COEGENT_PLAN_DB`, CLI `--plan-db`, Python `PlanStore(path)`로 다른 파일을 선택할 수 있습니다.

## 직접 추가하고 사용하기

`local_ai_study`에서 실행합니다:

```bash
# DB 생성 / 목록
./.venv/bin/python -m coegent plans init
./.venv/bin/python -m coegent plans list

# 사용자 또는 상위 에이전트가 작성한 계획 추가
./.venv/bin/python -m coegent plans add \
  --plan-file CoEgent_Agent/examples/valve_plan.json \
  --title '밸브 점검 후 복귀' --author 'user' --source manual

# 상위 에이전트가 추가할 때는 --source agent 사용
# 저장 응답의 id로 조회 / JSON 본문 내보내기
./.venv/bin/python -m coegent plans show --plan-id 1
./.venv/bin/python -m coegent plans export --plan-id 1 --out /tmp/plan-1.json

# DB에 저장된 전체 계획을 ScriptedPlanner로 반환하고 실행
./.venv/bin/python -m coegent run --planner db --plan-id 1 \
  --command '저장된 밸브 점검 계획 실행' --out CoEgent_Agent/results/stored_run.json

# 실행 없이 계획만 읽고 검증
./.venv/bin/python -m coegent plan --planner db --plan-id 1 \
  --command '저장된 계획 확인' --out CoEgent_Agent/results/stored_plan.json
```

`1`은 예시입니다. 실제 추가 결과의 `id`를 사용하세요. 모든 관련 명령에서 `--plan-db /경로/plans.sqlite3`를 사용할 수 있습니다. add는 계획 본문뿐 아니라 `plan` 필드가 있는 plan-mode 보고서나 미션 요청 파일도 읽습니다. 이때도 수동/에이전트 등록이며 보고서에 적힌 SUCCESS를 성공 실적으로 가져오지 않습니다.

Python API:

```python
from coegent import PlanStore, PlanExecutor
from coegent.planner import ScriptedPlanner

store = PlanStore()
saved = store.add(plan_body, title="창고 이동", author="upper-agent", source="agent")
record = store.get(saved["id"])
planner = ScriptedPlanner.from_store(store, [saved["id"]])
# CoEgent(planner, backend, plan_store=store)로 실행하거나
# PlanExecutor(backend, plan_store=store).execute(record["plan"], current_state) 사용
```

`plan_body`, `backend`, `current_state`는 호출자가 제공하는 실제 객체입니다. from_store는 지정 ID의 계획들을 순서대로 읽어 기존 ScriptedPlanner 큐에 넣습니다. `pop()`으로 큐에서 꺼내도 DB에서는 삭제되지 않습니다. 여러 응답이 필요한 테스트에는 CLI에서 `--plan-id 1 --plan-id 2`처럼 순서를 지정할 수 있습니다. 자연어로 자동 선택하려면 [PlannerSelector](PLANNER_SELECTOR.md)의 `--planner auto`를 사용합니다. ScriptedPlanner 자체가 명령을 이해하는 것은 아닙니다.

## request에서 ID 선택

미션 ID·revision·만료·command·선택적 goals를 담고 plan은 생략한 요청 파일을 준비한 뒤:

```bash
./.venv/bin/python -m coegent request \
  --request-file /path/mission-metadata.json --plan-id 1
```

요청 파일에 plan을 직접 넣는 기존 방식도 유지합니다. 두 입력을 동시에 주면 거부합니다. ID를 선택해도 현재 상태·권한·안전·미션 중복 검증과 adapter 경로는 그대로 거칩니다. 이 경로는 LLM을 호출하지 않습니다.

## 중복 기준

사용자가 선택한 기준은 **정규화한 계획 전체의 동일성**입니다.

- 공백·줄바꿈·객체 키 순서, 같은 숫자의 정수/소수 표기 차이는 제외합니다.
- goal, success_criteria, final_outcome, skill 인자와 순서는 비교합니다.
- 제목·작성자·출처·요청 command는 계획 해시에 포함하지 않습니다.
- SHA256 + DB UNIQUE 제약으로 중복 행을 차단합니다. 동시 프로세스 삽입도 같은 ID를 받습니다.
- 중복 추가 시 created=false와 기존 ID를 반환하며 최초 메타데이터를 유지합니다.
- 다른 command로 중복 추가하면 `plan_commands`에 연결 명령을 추가합니다. 성공 시 command도 연결됩니다. `plans show`의 commands에 표시하며 후보 검색에 사용합니다.

문장 의미가 비슷한 계획을 합치는 semantic dedup은 하지 않습니다. 목표 문구나 성공 조건이 다르면 별도 계획일 수 있습니다.

## 성공 후 자동 저장

`CoEgent`/`PlanExecutor`와 CLI `run`/`request`는 기본적으로 성공한 전체 계획을 저장합니다. `report.plan_store`에서 saved/existing/skipped/error와 계획 ID를 확인할 수 있습니다.

자동 저장 조건:

- 실행 상태 SUCCESS, 미확인 stop·robot fault·미완료 작업 없음
- 요청 목표가 있으면 achieved; 목표가 없으면 not_evaluated인 실행 수준의 성공
- 실패·취소·목표 unmet/unknown·report 불완전·재전송 결과는 새 성공으로 저장하지 않음
- replan 또는 revision continuation이면 마지막 계획이 미션의 일부일 수 있으므로 자동 등록을 건너뜀

계획 본문은 한 번만 보관하고, 성공 관측은 successful_executions에 따로 기록합니다. 같은 mission_id/revision은 성공 횟수를 중복 증가시키지 않습니다. 성공 횟수·마지막 성공 시각과 executor/backend/state_mode/goal_status는 `plans show`에서 확인합니다.

**현재 기본 실행 backend는 MockSkills입니다.** 이 경우 기록에도 `coegent.skills.MockSkills`가 남습니다. 이것을 실제 로봇의 성공으로 간주하지 않습니다. DB의 과거 성공은 새로운 환경에서의 실행 승인이나 성공 보장이 아닙니다.

`plan` 모드는 검증·출력만 하며 자동 성공 저장하지 않습니다. 생성 결과를 보관하려면 plans add를 명시적으로 호출하세요. `suite`/`demo`는 고정 테스트 시나리오이므로 자동 저장을 끕니다.

비활성화:

```bash
./.venv/bin/python -m coegent run ... --no-auto-save-plans
```

Python에서는 `auto_save_plans=False`를 사용합니다. DB 잠김·디스크 오류 등 저장 실패는 실행 결과를 바꾸거나 같은 작업을 다시 실행하지 않으며, report.plan_store에 error로 남깁니다. DB 쓰기는 로컬 동기 작업이고 SQLite 잠금 대기 기본값은 0.5초입니다.

## 파일과 구현

```text
CoEgent_Agent/
├── data/plans.sqlite3          영속 DB (실제 사용자 데이터, Git 제외)
├── coegent/plan_store.py       검증·정규화·중복 방지·성공 기록
├── coegent/plan_catalog_cli.py DB 관리 명령
└── coegent/planner.py          ScriptedPlanner.from_store
```

DB는 현재 planner.v1 구조로 저장·조회 시 검증하고, 실행 시 현재 상태로 다시 검증합니다. DB 저장 스키마 v1은 v2로 마이그레이션하며 기존 ID·본문·해시·성공 이력을 유지하고 명령 별칭·검색 인덱스를 만듭니다. 임의 SQLite DB나 지원하지 않는 버전은 거부합니다. export는 자기 DB와 SQLite sidecar 경로를 덮어쓰지 못하게 합니다. DB 파일에는 계획과 운영 메타데이터가 있으므로 배포 환경에서 파일 접근 권한·백업은 별도 운영 정책에 맞추세요.
