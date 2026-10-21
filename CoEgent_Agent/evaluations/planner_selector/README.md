# 실모델 선택기 평가

[평가 규칙/개발 기록](PROTOCOL.md), [고정 사례](cases.json), [실측 결과](../../results/planner_selector_accuracy_20261016/REPORT.md).

`local_ai_study`에서 실행합니다. 이 평가기는 inference 서버를 자동 시작/교체하지 않습니다. 한 번에 한 모델을 선택해 준비 상태를 확인하세요. 모델 다운로드·학습·skill 실행·기본 계획 DB 수정은 하지 않습니다. 임시 SQLite 파일에 사례별 후보를 등록하고 실제 PlannerSelector를 통해 HTTP LLM을 호출합니다.

```bash
# 기존 Q8 Gemma 프로필
bash LLM_Model_Server/scripts/server.sh --runtime llamacpp start
./.venv/bin/python CoEgent_Agent/evaluations/planner_selector/run.py \
  --model coegent-gemma4-gguf --split holdout --variants legacy improved \
  --out /tmp/gemma-selector-comparison.json
bash LLM_Model_Server/scripts/server.sh stop

# 설치되어 있는 Qwen robot-SFT checkpoint를 비교할 때
bash LLM_Model_Server/scripts/server.sh --runtime llamacpp \
  --model-path deployment_runtime_benchmark_v0_1/models/qwen2.5-3b-sft-v0.1-Q8_0.gguf \
  --model-name coegent-qwen3b-q8 start
./.venv/bin/python CoEgent_Agent/evaluations/planner_selector/run.py \
  --model coegent-qwen3b-q8 --split dev --variants improved \
  --out /tmp/qwen-selector-dev.json
bash LLM_Model_Server/scripts/server.sh stop
```

- legacy는 작업 전 SYSTEM/schema 스냅샷입니다.
- prompt_only는 현재 SYSTEM + 이전 flat schema입니다.
- improved는 현재 SYSTEM + 실제 후보에 묶인 분기 schema입니다. 분기 제약과 reason-first 출력 순서의 묶음 효과를 측정합니다.
- `--repeat 2`로 반복합니다. 토큰512, temperature0, 동시 요청 없음. 실패도 오답으로 계산합니다.
- 실행 예산 탐색에는 `--max-tokens 1536`을 지정할 수 있습니다. `--case-id d09` 같은 필터는 개발 probe에만 허용합니다. 출력 예산이 다른 실행은 별도 구성으로 보고합니다.
- 기본 12개 dev/18개 holdout은 직접 작성한 작은 집합입니다. 동일한 명령 문자열이 서로 다른 catalog에서 다른 정답을 가질 수 있습니다. 완전히 독립적으로 수집한 운영 데이터가 아닙니다.
- 정답은 decision+reuse ID입니다. clarification 질문이 문제를 해결하는지, 실제 실행 성공이나 생성 계획 품질은 점수로 측정하지 않습니다.
- 재현 JSON에는 프롬프트/사례 해시, 서버 모델 응답, 입력/schema/raw 응답, 후보 ID, 오류, 시간과 confusion matrix가 남습니다. 파일은 매 사례 뒤 갱신합니다. 새 `--out`을 사용해 이전 기록을 보존하세요.
