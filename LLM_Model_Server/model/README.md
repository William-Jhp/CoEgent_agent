# 로컬 모델 배치

다른 PC에서 저장소를 clone한 뒤 사용할 모델을 이 디렉토리에 넣습니다.
Git에는 안내 파일과 `.gitkeep`만 포함되며, 모델은 자동 다운로드되지 않습니다.
빈 디렉토리를 유지하는 용도이므로 실제 디스크 용량을 미리 할당하지 않습니다.

```text
model/
├── gguf/       llama.cpp용 GGUF 모델과 GGUF LoRA
├── hf/         Hugging Face 모델: 가중치, config, tokenizer 등 전체 파일
├── adapters/   HF 모델에 적용할 PEFT / LoRA 어댑터
└── original/   원본 모델 보관용 (선택)
```

## 기본 E4B 모델

외부 E4B 서버와 CoEgent의 `InprocessModelPlanner` 기본값은 같은 파일을 사용합니다.

```text
LLM_Model_Server/model/gguf/gemma-4-E4B_q4_0-it.gguf
```

모델을 해당 경로에 복사하고 llama.cpp 런타임을 별도로 설치한 뒤,
저장소 루트에서 외부 서버를 시작합니다.

```bash
bash LLM_Model_Server/scripts/server.sh --config LLM_Model_Server/config/e4b.toml doctor
bash LLM_Model_Server/scripts/server.sh --config LLM_Model_Server/config/e4b.toml start
```

내부 추론은 추가로 native bridge 빌드가 필요합니다.
[서버 설치 및 내부 추론 안내](../README.md)를 참고하세요.

## 다른 모델 선택

다른 GGUF 파일을 넣은 경우 CLI에서 경로와 서버 모델 별칭을 지정할 수 있습니다.

```bash
bash LLM_Model_Server/scripts/server.sh --runtime llamacpp \
  --model-path LLM_Model_Server/model/gguf/your-model.gguf \
  --model-name your-model --no-adapter doctor
```

`doctor`는 점검만 수행합니다. 점검 결과를 확인한 뒤 같은 선택 옵션에서
마지막 동작을 `start`로 바꾸면 서버를 시작합니다. 모델에 맞는 chat template과
문맥 길이 등은 별도 TOML 프로필로 설정하세요. CoEgent의 내부 플래너 모델 선택은
[CoEgent 안내](../../CoEgent_Agent/README.md)를 참고하세요.

HF 모델은 `hf/<model-name>/`에 전체 checkpoint를, PEFT 어댑터는
`adapters/<adapter-name>/`에 배치합니다. `config/server.toml`의 경로를 수정하거나
`--model-path`와 `--adapter-path`로 선택합니다. 어댑터 없이 실행하려면
`--no-adapter`를 명시하세요. 어댑터는 기반 모델 및 런타임과 호환되어야 합니다.

TOML 내부 상대 경로는 TOML 파일의 디렉토리가 기준이고, CLI로 넘기는 상대 경로는
현재 작업 디렉토리가 기준입니다. 모델의 출처, revision, 라이선스와 SHA256은
로컬에 별도로 기록하는 것을 권장합니다. 기존 개발 PC의 `COPY_MANIFEST.json`과
`*.source.json` 등 로컬 기록도 Git에서는 제외됩니다.
