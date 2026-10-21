# CoEgent Agent

자연어 명령을 계획으로 변환하고, 실행 어댑터를 통해 임무를 수행하며,
Inspector로 결과를 점검하는 에이전트입니다. LangGraph가 계획 생성,
실행, 검사 및 재계획 흐름을 연결합니다.

## 구성

- [CoEgent_Agent](CoEgent_Agent/README.md): PlannerSelector, 플래너, Plan DB,
  Inspector 및 LangGraph 워크플로우.
- [Executor_Adapters](Executor_Adapters/README.md): 공통 실행 계약과 어댑터 등록.
- [A2_Execution_Adapter](A2_Execution_Adapter/README.md): A2 / Isaac Sim 실행 연동.
- [LLM_Model_Server](LLM_Model_Server/README.md): 독립 모델 서버와 llama.cpp 런타임 설정.

## 기본 설치

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ./Executor_Adapters -e './CoEgent_Agent[test]'
.venv/bin/python -m coegent --help
```

모델 서버, A2 어댑터와 ROS / Isaac Sim 연동에는 각 디렉토리의 별도 설치가
필요합니다. 모델 가중치, 빌드된 런타임, 시뮬레이션 지도, 실행 DB 및 로그는
이 저장소에 포함하지 않습니다. 로컬 경로와 장비 설정은 사용할 환경에
맞게 지정해야 합니다.

자세한 구조는 [LangGraph](CoEgent_Agent/docs/LANGGRAPH.md),
[Inspector](CoEgent_Agent/docs/INSPECTOR.md),
[PlannerSelector](CoEgent_Agent/docs/PLANNER_SELECTOR.md) 문서를 참고하세요.
