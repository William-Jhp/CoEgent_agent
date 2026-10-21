# PlannerSelector 검증 기록 — 2026-10-15

구현: [설계](superpowers/specs/2026-10-15-planner-selector-design.md), [사용법](PLANNER_SELECTOR.md).

## 결과

| 확인 | 결과 |
|---|---|
| CoEgent 전체 pytest | **434 passed**, 26.57s (작업 전 399개, 신규 35개) |
| LLM_Model_Server unittest | **49 tests, OK**, 6.920s |
| 기존 mock runtime 시나리오 suite | **12 / 12 passed** |
| 독립 코드 검토 | 취소 후 추론 소유권 문제 수정 후 lifecycle 7개 통과, 남은 actionable issue 없음 |
| 기본 사용자 DB | schema v1 → v2; 계획 0개/성공 이력 0개 유지, 본문·이력 digest 동일, integrity_check=ok |

실행 명령 (`local_ai_study`):

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 ./.venv/bin/python -m pytest CoEgent_Agent/tests -q
PYTHONPATH=LLM_Model_Server ./.venv/bin/python -m unittest discover -s LLM_Model_Server/tests -q
./.venv/bin/python -m coegent suite --out /tmp/coegent-selector-suite.json
```

pytest의 자동 플러그인 적재를 끈 이유는 워크스페이스 외부 ROS 플러그인의 lark 의존성 문제 때문입니다. 제품 코드의 오류를 무시하는 설정은 아닙니다. 테스트의 기본 DB는 conftest에서 테스트별 임시 파일로 분리합니다. 기본 사용자 DB에 테스트 계획을 넣지 않았습니다.

## 신규 검증 범위

- `test_plan_search.py` (5): 대상/숫자/동작 추출, 검색 순위와 전체 계획, 중복 본문에 복수 명령 연결, v1 마이그레이션에서 ID·본문·성공 횟수/이력 유지, 후보 한도와 무관 단어.
- `test_selector_models.py` (3): 실제 localhost HTTP 프로토콜의 메시지/schema/토큰 한도, 잘린 HTTP 응답 거절, fake torch/transformers를 사용하는 inprocess complete 경로.
- `test_planner_selector.py` (10): 후보 전체 계획/스냅샷, 후보 없는 추가 질문, 후보 밖 ID·중복 JSON 키·잘못된 필드 거절, continuation 시 선택 추론 생략, 초과 후보 전체 제외.
- `test_selector_dispatch.py` (6): PlanGenerator/CoEgent의 세 분기, 생성기 호출 여부, native adapter 경계, 질문 시 stop 포함 skill 0회/저장 0회, estop 검증 유지.
- `test_selector_cli.py` (4): subprocess CLI와 실제 CPU HTTP fixture를 연결한 reuse/generate/clarify, 각각 1/2/1회 HTTP 요청, 질문 stdout/JSON, 별도 HTTP 판단 모델·생성 모델 설정.
- `test_selector_lifecycle.py` (7): 취소를 삼킨 선택기의 늦은 결과가 생성/보고를 시작하지 않음, 선택기/생성기 각각의 remote hook, 기존 offline 설정 유지, timeout, 후보 밖 ID 실행 차단, 취소 후 inprocess 추론 정리 동안 다음 미션 차단.

검토에서 발견한 추론 중첩은 `CoEgent`의 dispatch task를 admission 소유 작업으로 추적하도록 수정했습니다. 선택기가 늦게 결과를 반환해도 취소된 dispatch는 새 모델을 호출하지 않습니다. `PlanGenerator`는 기존 pending task 보호를 유지합니다.

## 검증의 한계

실제 가중치를 적재하거나 GPU 추론을 하지 않았습니다. HTTP fixture의 판단 응답은 고정값이며, 이는 통신·분기·검증·실행 경계 테스트입니다. 실제 모델이 부정·대상·수치·순서·복귀 조건을 정확히 비교하는지는 검증하지 않았습니다. HF는 grammar decoder가 아닌 프롬프트와 엄격한 JSON 사후 검증을 사용합니다. HTTP 취소가 serving 프로세스의 추론 중단을 보장하지는 않습니다. 실제 로봇/외부 executor 동작은 이번 검증 범위에 포함하지 않습니다.
