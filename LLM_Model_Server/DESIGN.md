# 독립 로컬 LLM 서버 설계

기준일: 2026-10-14. 사용자는 개발 PC에서 CoEgent와 독립된 모델 서버를 새 디렉토리에 구축하도록 요청했다.

## 목표와 경계

별도 Python 환경의 vLLM OpenAI-compatible server를 상시 프로세스로 실행한다. CoEgent는 기존 ModelServerPlanner로 `POST /v1/chat/completions`를 호출한다. 이 프로젝트는 CoEgent runtime을 import하거나 로봇 동작을 실행하지 않는다. 모델 가중치를 복사하지 않고 기존 local checkpoint와 Planner-SFT adapter 경로를 설정으로 연결한다.

서버의 HTTP frontend, engine 작업 관리, 모델 worker, structured decoding은 vLLM을 사용한다. 이 프로젝트가 제공하는 구성 검증·시작·상태·종료 CLI는 표준 라이브러리로 구현하여 무거운 모델 환경 없이 관리·테스트할 수 있게 한다.

## 실행 구성

- 새 디렉토리: `LLM_Model_Server`.
- loopback HTTP: `127.0.0.1:18520`.
- 런타임: Gemma4 지원이 확인되는 vLLM release를 전용 `.venv`에 고정 설치.
- 모델: Gemma4 E2B robot-SFT v0.4 merged, 별칭 `coegent-base`.
- adapter: Planner-SFT v1 final_adapter, 별칭 `coegent-planner`, rank 16.
- 고정 버전의 conditional wrapper는 LoRA를 지원하지 않아 HF architecture override로 Gemma4ForCausalLM 텍스트 경로를 선택한다. 원본 checkpoint는 변경하지 않는다.
- precision: BF16, GPU 하나, 동시 추론 sequence 하나, context 4096부터 확인.
- GPU 메모리 비율: 전체의 0.40; CLI의 명시적 설정으로 변경 가능.
- 그래프 compilation 대신 eager 초기 설정으로 첫 호환성 확인 비용을 줄인다.
- 다운로드 방지를 위해 HF offline 환경을 사용하고 모델·adapter 경로를 시작 전에 검사한다.
- 모델 작업은 독립 process group으로 실행하고 PID 및 `/proc` 시작 시각을 기록한다. 종료할 때 이 프로젝트가 시작한 동일 프로세스임을 재확인한다.
- 포트 사용 중, 모델/adapter 누락, 자원 부족, runtime 미설치, 시작 실패를 명시적 오류로 반환한다. 다른 서비스나 작업을 종료하지 않는다.

## API와 호환성

`/health`, `/v1/models`, `/v1/chat/completions`는 runtime의 API를 그대로 사용한다. CoEgent의 model 필드는 `coegent-planner`로 설정한다. JSON schema response_format을 전송하고 생성 결과를 CoEgent의 validator에서 다시 검증하는 smoke 도구를 제공한다. 응답이 잘렸거나 schema를 만족하지 않으면 성공으로 기록하지 않는다.

HF 모델·LoRA의 지원은 실제 로딩과 공통 MissionPlanSchema smoke를 통해 확인한다. 검증 없이 LoRA를 제거하거나 unconstrained 생성으로 자동 전환하지 않는다. 현재 host RAM 부족이 실제 모델 실행을 막으면 상태와 진단을 기록하고 설치·소스·단위 검증과 실제 추론 검증을 구분한다.

## 관리 수명

start: 설정·경로·포트·자원 검사 → 독립 server 실행 → PID 기록 → health/models 확인 → ready.

status: PID identity와 health를 확인하여 stopped/starting/ready/unhealthy를 구분한다.

stop: 관리 대상 identity 확인 → process group SIGTERM → 제한 시간 대기 → 필요 시 해당 group만 SIGKILL → 상태 파일 정리.

run: foreground에서 동일 설정으로 server를 exec하여 외부 supervisor에서도 관리할 수 있게 한다.

## 검증

설정·인자·LoRA 별칭·offline 환경·잘못된 경로·resource boundary를 단위 테스트한다. 실제 CPU HTTP fixture 프로세스로 시작/ready/중복 시작/종료/PID 충돌/시작 실패를 테스트한다. fixture 테스트는 모델 추론 검증으로 표현하지 않는다. 실제 GPU 가능 조건에서는 vLLM과 CoEgent PlanGenerator를 연결하여 계획만 생성·검증하고 장비 backend는 호출하지 않는다.

## 실제 검증 후 확정한 호환성 설정

- vLLM 0.19.1 / Transformers 5.5.4 / Torch 2.10.0.
- 서버 패키지의 일반 plugin이 Gemma4 텍스트 클래스를 등록하며, 실제 forward에서 사용하지 않는 KV 공유 레이어의 k_norm만 제거한다. 활성 레이어 norm 및 원본 checkpoint는 보존한다.
- xgrammar compact JSON 생성으로 JSON 필드 사이 무제한 공백 반복을 제한한다.
- 실제 모델·LoRA·HTTP·CoEgent 계획 경로가 샘플에서 확인되었다. 생성 계획의 의미 정확성은 구조 검증과 별개이며, 실제 발견된 모순은 VERIFICATION.md에 기록한다.
