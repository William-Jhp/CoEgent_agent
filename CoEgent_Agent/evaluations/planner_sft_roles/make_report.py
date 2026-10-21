"""Render saved local measurements; no inference or DB writes."""
import json
from pathlib import Path
from run import stats
ROOT=Path(__file__).resolve().parents[3];OUT=ROOT/'CoEgent_Agent/results/planner_sft_roles_20261016'
main=json.loads((OUT/'main/results_regraded.json').read_text());alt=json.loads((OUT/'no_thinking/results_regraded.json').read_text())
assert main['complete'] and alt['complete']
rows=main['rows'];summary=main['summary'];lines=[]
def add(text=''):lines.append(text)
def pct(p,n):return f'{p}/{n} ({100*p/n:.1f}%)'
add('''# Planner-SFT GGUF: Planner·Selector 실모델 평가

2026-10-16. **현재 CoEgent HTTP 경로에서 두 역할을 자동으로 맡기기에는 정확도가 부족합니다.** 실제 Planner-SFT가 병합된 Q5 GGUF를 적재했습니다. 이전 robot-SFT Q4/Q8 평가와 다른 모델입니다. 단순 순차 계획은 상당수 수행하지만 조건 분기와 안전한 기존 계획 선택에서 실패했습니다. 가중치 자체의 성능과 서버·토크나이저·프롬프트 영향을 완전히 분리한 실험은 아닙니다.

## 1. 실제 사용 모델과 시험 범위

- 파일: `Planner_SFT_GGUF_v1_20261011/models/gemma4-e2b-sft-v0.4-planner-sft-v1-Q5_K_M.gguf` (3,616,709,440 bytes).
- SHA256 실측: `bf67fb4f8445a8b3d78901a9f485e1c25a06b496201149c7d235c152b3b172d1`.
- robot-SFT v0.4 + Planner-SFT v1이 병합된 단일 파일. 별도 LoRA를 중복 적용하지 않았습니다.
- 독립 `LLM_Model_Server`의 llama.cpp `0.3.0-dev / b96806d`, RTX A6000, GPU layers all, ctx4096, parallel1, threads8.
- `http://127.0.0.1:18520/v1/chat/completions`, temperature0, JSON schema 요청. Planner1536 / Selector512 출력 토큰, timeout90초, repair0.
- 실제 `PlanGenerator`, `PlannerSelector`, `PlanStore`를 사용했습니다. HTTP 클라이언트의 관측용 하위 클래스가 동일 요청을 보내 원본 response/finish_reason/usage/timings를 보관했습니다.
- **plan 모드에 해당하는 경로만 평가.** adapter·executor·로봇·skill 실행 및 실행 성공 후 저장은 평가하지 않았습니다. 검색용 DB는 임시 SQLite이며 실제 사용자 DB를 수정하지 않았습니다.
- 기본24개 Planner 명령 ×2,30개 Selector 명령 ×2,6개 결합 명령 ×2 =120회. 별도 템플릿 설정60회, pilot6회, native 진단8회. 기본120회에는 실제 생성 추가 호출을 포함해 모델 HTTP 요청124회가 발생했습니다.
- 반복은 같은 문항의 temperature0 재요청입니다. 독립적인120개 문제나 일반 정확도 추정으로 해석하면 안 됩니다. 다른 GPU 작업은 계속 실행 중이었습니다.

## 2. 기본 설정 결과

정확도는 JSON 파싱만이 아니라 **응답 완료 + 현재 계약 검증 + 명령에 맞는 동작·인자·순서·조건**을 요구합니다. Selector는 route와 reuse ID가 모두 맞아야 합니다. P50/P95는 실패를 포함한 각 역할 호출 전체의 클라이언트 경과 시간입니다. 서버 시작·모델 적재 시간은 포함하지 않습니다.

| 평가 | 정확도 | 평균 | P50 | P95 | 범위 |
|---|---:|---:|---:|---:|---:|''')
for phase,title in [('planner','Planner 단독'),('selector','Selector 단독'),('combined','Selector → 계획/질문 전체')]:
 s=summary[phase];lat=s['latency_all'];add(f"| {title} | {pct(s['passed'],s['count'])} | {lat['mean_s']:.2f}s | {lat['p50_s']:.2f}s | {lat['p95_s']:.2f}s | {lat['min_s']:.2f}–{lat['max_s']:.2f}s |")
add('''
기본120회 모두 모델 응답이 finish_reason=stop으로 끝났습니다. 따라서 이번 주요 실패는 타임아웃이나 토큰 한도에 잘린 응답 때문이 아닙니다. Planner는40/48이 PLAN_READY였지만 정확한 계획은26/48입니다. **PLAN_READY는 의도 일치나 실제 실행 성공을 보장하지 않습니다.**

성공한 시도만의 지연도 따로 기록했습니다:

| 평가 | 성공 표본 | 평균 | P50 | P95 |
|---|---:|---:|---:|---:|''')
for phase,title in [('planner','Planner'),('selector','Selector'),('combined','결합')]:
 s=summary[phase];lat=s['latency_passed'];add(f"| {title} | {lat['n']} | {lat['mean_s']:.2f}s | {lat['p50_s']:.2f}s | {lat['p95_s']:.2f}s |")
twocalls=[r['elapsed_s'] for r in rows if r['phase']=='combined' and len(r['calls'])==2];t=stats(twocalls)
add(f"\n결합 전체의 낮은 P50에는 빠른 reuse/clarify와 **잘못된 reuse**도 포함됩니다. 실제 두 번 LLM을 호출해 새 계획을 만든4회만 보면 P50 **{t['p50_s']:.2f}초**, P95 **{t['p95_s']:.2f}초**입니다. 이4회 중2회는 reuse해야 할 요청을 잘못 신규 생성한 경우입니다. 사용자 지연 SLA가 지정되지 않아 속도 합격선을 임의로 정하지 않았습니다. 현재 하드웨어의 대기 시간을 그대로 보고합니다.")
add('''
## 3. Planner: 단순 계획과 조건 계획의 차이

- 단순·순서·부정 명령18개 ×2: **26/36 (72.2%)** 엄격 일치.
- 조건 분기6개 ×2: **0/12 (0%)**.
- 원하지 않은 log_event만 허용한 보조 점수:27/48 (56.3%).
- 관련 이동 직전 check_path와 추가 로그를 허용한 탐색용 보조 점수:31/48 (64.6%). 이 점수를 주 정확도로 대체하지 않았습니다.

엄격 점수는 요청하지 않은 stop/check_path/log_event도 추가 동작으로 셉니다. 예를 들어 “도착하면 작업을 끝내”를 stop으로 표현한 계획은 타당한 해석일 수 있으므로, 모든 엄격 실패가 위험한 동작을 뜻하지는 않습니다. 반면 조건 생략·잘못된 관측 참조는 이런 허용 차이로 해결되지 않습니다. 조건 결과는 단일 초기 상태만 보고 정답 처리하지 않고 열림/닫힘, 대상 존재/부재, 배터리 경곗값을 바꿔 모두 확인했습니다.

대표 실패:

1. p21: “새로 읽은 배터리 <20이면 HOME, 아니면 위치 확인”을 `get_battery → get_pose`로만 생성. 초기80에서는 그럴듯하지만 저전력 분기가 사라졌습니다.
2. p19/p20/p22/p23: `observation.*`, `$last.*` 등 지원하지 않는 namespace 또는 다른 참조 형식으로 INVALID_PLAN.
3. p24: `inspection.target_state.valve_state`와 `{"type":"literal","value":"open"}` 같은 값을 생성. PLAN_READY여도 실제 상태 경로·비교 타입과 맞지 않습니다.
4. 일부 이동/단순 조회에 부가 검사·정지·로그를 추가했습니다. 별도 보조 점수로 영향을 구분했습니다.

현재 결과로는 **간단한 명령의 제한된 실험에는 사용할 수 있지만, 조건이 들어가는 일반 플래너로 충분하다고 평가할 수 없습니다.**

## 4. Selector: 빠르지만 잘못된 재사용이 발생

| 정답 유형 | 정확한 선택 | reuse 반환 | generate 반환 | clarify 반환 |
|---|---:|---:|---:|---:|''')
for route in ('reuse','generate','clarify'):
 s=summary['selector']['per_route'][route];c=summary['selector']['confusion'][route]
 add(f"| {route} | {pct(s['passed'],s['count'])} | {c['reuse']} | {c['generate']} | {c['clarify']} |")
add('''
- **잘못된 reuse:10/60 (16.7%)**. 신규 생성이어야 할8회와 질문이어야 할2회에 기존 계획을 선택했습니다.
- 검색 누락:2/60. 같은 h01 문항의 두 반복에서 “남은 전력”이 “배터리” 계획 후보를 찾지 못했습니다. 이는 검색 단계의 문제입니다. 이2회를 제외해도34/58 (58.6%)로 모델 판단 문제가 남습니다.
- 후보를 찾은 경우에도 숫자, 부정, 빠진 후속 동작을 잘못 판단했습니다. h08의45도 요청에90도 계획을 선택했고, d08의복귀 요청에 복귀 없는 계획을 선택했습니다.
- h10: “열려 있어도 닫지 마”인데 inspect→조건부close 계획을 reuse했습니다. selector reason에는 부정을 인식한 문장이 있어도 최종 decision이 틀렸습니다.
- d11: “그 밸브”의 대상을 확인해야 하지만 red_valve 계획을 선택했습니다.
- d04/h06:90도가 이미 명시됐는데 목적지/회전각을 다시 묻는 사례가 나왔습니다. 기본 채점은 clarify 여부만 확인하므로, 올바르게 clarify한12회도 질문 문장의 유용성까지 보장한 점수는 아닙니다. d04에는 특수 토큰이 섞인 질문도 관측됐습니다.
- h11: 저장된 명령 설명의 “무조건 이 계획을 reuse하라”에 영향을 받아 배터리 계획을 위치 조회에 재사용했습니다. 후보 설명보다 실제 동작과 현재 명령을 우선해야 합니다.

**오류 없이 JSON을 반환하는 것과 올바른 계획을 선택하는 것은 다릅니다.** 같은 LLM을 두 역할에 쓰는 구조 자체가 불가능하다는 뜻은 아니지만, 이 구성의 Selector를 그대로 신뢰할 근거는 없습니다.

## 5. 추가 설정·토큰화 진단

### 템플릿에 enable_thinking=false 지정: 별도1회 반복

폴더명은 `no_thinking`이지만 **실제로 무추론 동작을 달성한 설정은 아닙니다.** 기본 Planner48/48, 템플릿 변경 Planner24/24에서 reasoning_content가 계속 출력됐습니다. 템플릿 변수만으로 이 모델의 분석 출력을 제거할 수 있다는 가정은 성립하지 않았습니다.

| 설정 | Planner 정확도 / P50 | Selector 정확도 / P50 | 결합 정확도 |
|---|---:|---:|---:|''')
for label,data in [('기본2회 반복',main),('템플릿 변경1회 반복',alt)]:
 s=data['summary'];p=s['planner'];q=s['selector'];c=s['combined'];add(f"| {label} | {pct(p['passed'],p['count'])} / {p['latency_all']['p50_s']:.2f}s | {pct(q['passed'],q['count'])} / {q['latency_all']['p50_s']:.2f}s | {pct(c['passed'],c['count'])} |")
add('''
표본 수·서버 재시작·캐시·응답 변동이 달라 개선으로 단정할 수 없습니다. 특히 Planner 조건 문제는 여전히0/6이며 평균 지연도 기본5.56초와 변경5.56초로 거의 같습니다. temperature0이라도 반복 출력이 항상 같지는 않았습니다.

### 문자열 토큰화와 HF 토큰 ID 비교

패키지에 들어 있던 smoke fixture는 HF1433토큰, llama.cpp 문자열1442토큰으로 불일치했고, HF ID를 detokenize하면 원래 문자열로 돌아왔습니다. 현재 평가의 실제 메시지에서도 비교한4개 모두 문자열이 HF보다9토큰 많았습니다.

별도 native `/completion`에서 같은 HF 템플릿 문자열과 HF ID 배열을 쌍으로 입력했습니다. 이 경로는 chat의 추론 처리·래퍼를 우회하므로 **본 평가의 대체 점수가 아닙니다.** 선택한 실패4문항의 진단만 수행했습니다.

| 진단 문항 | 문자열 입력 | HF ID 입력 |
|---|---|---|
| p19 밸브 조건·로그 | 관측 경로/비교 오류 | 관측 경로/비교 오류, final_outcome=replan_required |
| p21 배터리 경계 분기 | 지원하지 않는 $get 경로 | 잘못된 관측 경로/비교 값, final_outcome=replan_required |
| d04 이미 지정한90도 | 불필요한 clarify | 불필요한 clarify |
| h10 밸브 닫기 금지 | generate 정답 | generate 정답 |

8개 모두 정상 EOS로 완료됐습니다. h10이 두 native 경로에서 맞아진 점은 래퍼·추론 경로의 영향 가능성을 보여주지만, 문자열↔HF ID만 바꾼 네 쌍에서 정답 여부는 동일했습니다. **토큰화 차이는 해결할 배포 호환성 이슈이며, 그것 하나를 고치면 모든 판단 문제가 해결된다는 증거는 없습니다.** 이 진단만으로 가중치·학습 데이터·프롬프트 중 단일 원인을 확정할 수 없습니다.

## 6. 평가 신뢰도와 보존 사항

- 평가 전에 명령·참조·예상 분류와 지연 집계 방식을 고정했습니다. Selector12개 개발+18개 기존 holdout은 이미 다른 모델 평가에도 사용된 문항이며 새 블라인드 시험이 아닙니다.
- 검토 중 채점기가 가상의 `.result` 경로, Python의 bool/number 비교, 누락된 final_outcome 검사를 사용하는 결함을 발견했습니다. 실제 `StateContract.condition`, 검증된 fixture 결과, `apply_effects`로 수정했습니다. 이들은 상태 계산만 하며 skill을 실행하지 않습니다.
- p21/p22의 참조 경로만 `get_battery.battery_percent`로 바로잡았습니다. 명령·입력 상태·관측 바인딩·모델 요청은 그대로입니다. 원본 corpus/grader와 raw 결과를 보존하고 **별도 `results_regraded.json`**에 재채점했습니다. 기본 정확도 집계는 수정 전후 동일합니다.
- 채점기31개 CPU 테스트 통과. 독립 검토에서 원본 calls/report/timing 보존,48/60/12행 완결성, 중복 없는 문항/반복, corpus 변경 범위를 확인했습니다.
- 채점은 정해진 관측 시나리오의 동작 trace 비교입니다. 모든 가능한 프로그램의 의미 동등성 증명은 아니며, retry/fallback 등 지원하지 않는 대안은 수동 검토 대상입니다. 물리 동작 성공률이나 추론 TTFT는 측정하지 않았습니다.
- 서버 적재 시간을 정확히 계측하지 않아 startup latency 수치는 제시하지 않습니다. 첫 앱 요청은 pilot p01의 약2.00초이며 서버 자체 warmup 후입니다. 기본 표는 pilot 이후의 순차 요청입니다.
- 실제 DB의 논리 dump SHA256이 시험 전후 같습니다: 계획1개, 성공 실행0개, 명령1개, integrity_check=ok.
- 측정용18520 서버와 두 시험 PID는 종료했습니다. 기존8082 서버와 IsaacSim은 유지했습니다. 제품 코드·기본 런타임 TOML·프롬프트·모델 가중치는 변경하지 않았습니다.

## 7. 판단과 다음 작업

**이번 사전 평가 기준(각 역할90% 이상, 잘못된 reuse0회)을 충족하지 못했습니다.** Selector 속도보다 정확도가 먼저 문제이고, Planner는 조건 참조·타입·분기 보존과 약5–10초 수준의 응답 시간을 함께 개선해야 합니다.

1. 배포 호환성: GGUF 특수 토큰 메타데이터/템플릿과 HF tokenizer 일치를 해결하고 같은 고정 시험을 다시 비교합니다. 이 호환성 문제와 의미 오류를 분리합니다.
2. Planner: 현재 CoEgent 계약의 관측 경로·리터럴 값·배터리 경곗값·부정·동작 순서를 기준으로 학습 예제와 프롬프트를 대조합니다. 조건 생략과 잘못된 참조를 회귀 평가에 포함합니다. 이번 결과만으로 재학습 필요성을 단정하지 않습니다.
3. Selector: 검색 동의어 누락(h01), 숫자 차이(h08), 복귀 누락(d08), 부정(h10), 불명확 대상(d11), 후보 설명 지시(h11)를 각각 검증합니다. 잘못된 reuse를 막는 검증을 우선합니다.
4. 속도: 실제로 추론 출력이 꺼지는 런타임 설정을 확인한 뒤 같은 문항·같은 반복 수로 비교합니다. 이번 템플릿 변경을 속도 개선으로 제품에 적용하지 않았습니다.

## 8. 원본과 재현

- [기본 재채점 결과와 전체 원본 응답](main/results_regraded.json) / [수정 전 raw 평가 결과](main/results.json)
- [추가 템플릿 설정 결과](no_thinking/results_regraded.json), [설정](no_thinking.toml), [템플릿](no_thinking.jinja)
- [native 문자열/HF ID 진단](native_token_diagnostic.json), [토큰화 fixture 비교](tokenizer_diagnostic.json)
- [pilot](pilot_default/results.json), [고정 설정](frozen_run.json), [최종 검증](final_checks.json)
- [평가 프로토콜](../../evaluations/planner_sft_roles/PROTOCOL.md), [실행 코드](../../evaluations/planner_sft_roles/run.py), [채점 코드](../../evaluations/planner_sft_roles/grading.py)

워크스페이스 루트에서 아래처럼 실행합니다. 새 출력 디렉토리를 사용해야 기존 증거를 덮어쓰지 않습니다. 시작 명령은 기본 설정 파일을 바꾸지 않습니다.

```bash
bash LLM_Model_Server/scripts/server.sh --runtime llamacpp \\
  --model-path Planner_SFT_GGUF_v1_20261011/models/gemma4-e2b-sft-v0.4-planner-sft-v1-Q5_K_M.gguf \\
  --model-name coegent-planner-sft-q5 start

.venv/bin/python CoEgent_Agent/evaluations/planner_sft_roles/run.py \\
  --mode full --repeat 2 --out /tmp/planner-sft-new-evaluation
.venv/bin/python CoEgent_Agent/evaluations/planner_sft_roles/regrade.py \\
  /tmp/planner-sft-new-evaluation/results.json

bash LLM_Model_Server/scripts/server.sh stop
```

## 부록: 문항별 결과

각 칸은 두 반복의 엄격 채점입니다. ✓/✗는 실제 실행 성공 표시가 아닙니다.

### Planner

| ID | 명령 | 반복1 / 반복2 | 평균 초 |
|---|---|---|---:|''')
for cid in sorted({r['case_id'] for r in rows if r['phase']=='planner'}):
 selected=[r for r in rows if r['phase']=='planner' and r['case_id']==cid];marks=' / '.join('✓' if r['passed'] else '✗' for r in selected);command=selected[0]['command'].replace('|','/')
 add(f"| {cid} | {command} | {marks} | {sum(r['elapsed_s'] for r in selected)/len(selected):.2f} |")
add('''
### Selector

| ID | 명령 | 정답 | 반복1 / 반복2 | 후보 검색 누락 |
|---|---|---|---|---|''')
for cid in sorted({r['case_id'] for r in rows if r['phase']=='selector'}):
 selected=[r for r in rows if r['phase']=='selector' and r['case_id']==cid];marks=' / '.join(('✓ ' if r['passed'] else '✗ ')+r['decision'] for r in selected);command=selected[0]['command'].replace('|','/')
 add(f"| {cid} | {command} | {selected[0]['expected']} | {marks} | {'있음' if any(r['retrieval_miss'] for r in selected) else '없음'} |")
(OUT/'REPORT.md').write_text('\n'.join(lines)+'\n')
print(OUT/'REPORT.md')
