# 실제 LLM 서버 검증 기록

검증일: 2026-10-14. 실제 GPU 로딩과 HTTP 호출을 수행했습니다. 테스트 시작 시 RAM 약 24 GiB / GPU 여유 약 47 GiB였으며, 테스트 후 관리 서버를 종료했습니다.

**서버의 모델·LoRA 로딩과 HTTP 추론 및 CoEgent 연결은 확인했습니다. 모델이 생성하는 모든 계획의 의미 정확성은 확인되지 않았고, 실제 오류 사례가 있습니다.**

## 결과

| 검증 항목 | 결과 |
|---|---|
| 단위·HTTP fixture·호환성 경계 테스트 | 29개 통과, 실패·오류 0개 |
| 의존성 검사 | 180개 패키지 호환 |
| 실제 Gemma4 GPU 로딩 | 성공, 로그상 모델 로딩 메모리 8.91 GiB |
| 실제 Planner LoRA 로딩 | coegent-planner 등록 및 해당 별칭으로 추론 성공 |
| HTTP /health, /v1/models | 200 OK, base 및 adapter 별칭 확인 |
| JSON schema 제약 probe | 명령과 다른 const schema를 요청하여 schema 적용 확인 |
| 실제 CoEgent CLI 배터리 계획 | PLAN_READY / NOT_EXECUTED, get_battery 단계 생성 |
| 실제 이동 계획 smoke | PLAN_READY / NOT_EXECUTED |
| 명시적 배터리 확인 smoke | PLAN_READY / NOT_EXECUTED, get_battery 단계 생성 |
| 종료·재시작·최종 종료 | 성공, 최종 GPU 작업 프로세스 없음 |
| 물리적 skill 실행 | 없음 |

최종 CoEgent 계획 생성 시간은 배터리 요청 7.60초, 이동 요청 10.75초, 명시적 배터리 요청 6.35초였습니다. 서로 다른 prompt와 초기화 상태가 포함된 소수 샘플이므로 성능 benchmark로 해석하지 않습니다.

증거: [실제 결과 요약](results/live_validation_20261014_164329/summary.json), [개별 요청 실행 결과](results/live_validation_20261014_164329/final_cases.json), [서버 로그](results/live_validation_20261014_164329/server.log), [실제 모델 경로를 확인한 runtime 계약](results/live_validation_20261014_164329/runtime_contract.json).

## 실제 테스트에서 수정한 문제

1. **Gemma4 설정 버전 불일치:** Transformers 5.15.1은 head_dim 등을 레이어별 설정으로 처리하며 vLLM 0.19.1이 요구하는 global 접근이 실패했습니다. 서버 전용 환경을 Transformers 5.5.4로 고정했습니다.
2. **사전 점검 도구의 잘못된 모델 검사:** serve의 positional model_tag를 EngineArgs.model에 반영하지 않아 기본 모델 설정으로 검사했습니다. 실제 serve와 동일하게 model을 지정하고 경로 및 model_type 검증을 추가했습니다. 이전 사전 점검만으로 checkpoint 호환성을 확인했다는 결론은 유효하지 않았습니다.
3. **사용하지 않는 공유 KV 레이어 norm 요구:** checkpoint에는 공유 레이어 15–34의 k_norm 가중치가 없지만 vLLM 기본 클래스는 이를 만들어 strict loader가 실패했습니다. 고정 버전의 forward는 해당 레이어의 k_norm을 사용하지 않습니다. 서버의 lazy general plugin이 그 파라미터만 제거하고, 기존 checkpoint에 남아 있을 경우 해당 미사용 가중치만 건너뜁니다. 활성 레이어와 다른 가중치는 유지합니다. 원본 checkpoint·adapter 및 vLLM 설치 소스는 변경하지 않았습니다.
4. **JSON 공백 반복:** 초기 CLI 요청에서 모델이 JSON 사이 줄바꿈을 반복해 출력 예산을 소진했고 INVALID_PLAN이 반환됐습니다. xgrammar compact JSON 설정 후 샘플 재검증이 통과했습니다.

재현 가능한 설치는 scripts/setup.sh와 고정 dependency lock 및 plugin entry point를 사용합니다. 호환성 plugin은 vLLM 0.19.1에 제한되므로 runtime 업그레이드 시 재검증해야 합니다. 관련 upstream 근거: [고정 버전 Gemma4 attention forward](https://github.com/vllm-project/vllm/blob/v0.19.1/vllm/model_executor/models/gemma4.py), [plugin 등록 방식](https://docs.vllm.ai/en/stable/design/plugin_system/).

## 생성 계획의 의미 오류

초기 배터리 계획은 배터리가 10보다 클 때 abort하는 반대 조건을 생성했습니다. 구조 검증은 통과했으나 명령 의도에는 맞지 않았습니다. compact 설정 후 배터리 계획 샘플은 get_battery 호출로 생성되었습니다.

최종 이동 계획은 현재 상태에서 navigate_to(A)를 선택하는 조건을 생성했지만, 성공 조건이 `location == `로 미완성이고 `final_outcome`은 abort로 작성되어 모순이 있습니다. [해당 응답](results/live_validation_20261014_164329/navigate_compact.json)을 보존했습니다. 이 의미 오류는 서버의 HTTP·모델 로딩 성공으로 해결된 것으로 처리하지 않습니다. 모델·prompt 및 의미 검증 개선이 별도로 필요합니다.

vLLM xgrammar 로그에는 일부 oneOf를 anyOf로 처리하는 경고도 있습니다. JSON schema probe 성공은 모든 MissionPlanSchema 제약과 명령 의도를 runtime만으로 보장한다는 의미가 아니며, CoEgent validator는 계속 적용해야 합니다.

## 최종 상태

서버 상태는 **stopped**입니다. 두 차례 실제 종료에서 관리 프로세스 그룹 정리를 확인했고, 마지막 nvidia-smi compute-apps 결과는 비어 있었습니다. 다시 사용하려면 README의 doctor → start 명령을 실행하면 됩니다.
