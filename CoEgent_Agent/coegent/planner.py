"""Planner adapters. Mock planning is explicit; it never pretends to understand text."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
from urllib.request import Request, urlopen

from coegent_contracts.prompting import planner_messages
from coegent_contracts.registry import SkillRegistry
from .schema_transport import model_server_schema


class ScriptedPlanner:
    def __init__(self, plans):
        self.plans = list(plans)
        self.contexts = []

    @classmethod
    def from_store(cls, store, plan_ids):
        """Snapshot explicit catalog IDs; consuming the queue never deletes DB rows."""
        ids = tuple(plan_ids)
        if not ids:
            raise ValueError('at least one plan ID is required')
        planner = cls([store.get(plan_id)['plan'] for plan_id in ids])
        planner.plan_ids = ids
        return planner

    async def plan(self, context):
        self.contexts.append(deepcopy(context))
        if not self.plans:
            raise ValueError("scripted plans exhausted")
        return deepcopy(self.plans.pop(0))


def messages(context):
    if context.get("tool_profile") == "a2-isaac":
        from .tool_profiles import api_messages
        return api_messages(context)
    registry = SkillRegistry()
    extra = {k: v for k, v in context.items() if k not in {"command", "state", "schema", "skills"}}
    extra["permitted_skills"] = list(context["skills"])
    return planner_messages(context["command"], context["state"], registry, extra)


def response_format(schema):
    return {"type": "json_schema",
            "json_schema": {"name": "mission_plan", "strict": True, "schema": model_server_schema(schema)}}


class ModelServerPlanner:
    """Plan through an OpenAI-compatible model server over HTTP.

    The server performs model inference; skill execution stays in CoEgent.
    """
    requires_remote = True
    supports_json_schema = True

    def __init__(self, endpoint, model, timeout_s=90, max_tokens=2048, constrained=True):
        self.endpoint, self.model = endpoint.rstrip("/"), model
        self.timeout_s, self.max_tokens = timeout_s, max_tokens
        self.constrained = constrained
        self.generation_mode = "json_schema_requested" if constrained else "unconstrained"

    async def plan(self, context):
        return await self.complete(messages(context), schema=context['schema'] if self.constrained else None)

    async def complete(self, chat_messages, *, schema=None, max_tokens=None, require_complete=False):
        token_limit = self.max_tokens if max_tokens is None else max_tokens
        chat_messages = deepcopy(chat_messages)
        def request():
            payload = {"model": self.model, "messages": chat_messages, "temperature": 0,
                       "max_tokens": token_limit, "stream": False}
            if schema is not None:
                payload["response_format"] = response_format(schema)
            req = Request(self.endpoint + "/v1/chat/completions", json.dumps(payload).encode(),
                          {"Content-Type": "application/json"})
            with urlopen(req, timeout=self.timeout_s) as response:
                data = json.load(response)
            if require_complete and data['choices'][0].get('finish_reason') != 'stop':
                raise ValueError('model_response_incomplete')
            return data["choices"][0]["message"]["content"]
        return await asyncio.to_thread(request)


class TransformersModelPlanner:
    """Local Transformers checkpoint; defaults to Gemma 4 E4B IT without LoRA."""
    supports_json_schema = False

    def __init__(self, model=None, adapter=None, max_tokens=2048, timeout_s=90, constrained=False):
        if constrained:
            raise ValueError("HF constrained decoding is not configured; use a schema-capable HTTP runtime")
        self.generation_mode = "unconstrained"
        if model is None:
            model = str(Path(__file__).resolve().parents[2] / 'LLM_Model_Server/model/hf/gemma-4-E4B-it')
        self.model_path = str(model)
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self.tokenizer = AutoTokenizer.from_pretrained(model, local_files_only=True)
        self.model = AutoModelForCausalLM.from_pretrained(
            model, dtype=torch.bfloat16, device_map="auto", local_files_only=True)
        if adapter:
            from peft import PeftModel
            self.model = PeftModel.from_pretrained(self.model, adapter, local_files_only=True)
        self.model.eval()
        self.max_tokens, self.timeout_s = max_tokens, timeout_s

    async def plan(self, context):
        return await self.complete(messages(context))

    async def complete(self, chat_messages, *, schema=None, max_tokens=None, require_complete=False):
        import threading
        from time import monotonic
        import torch
        from transformers import StoppingCriteria, StoppingCriteriaList

        cancelled = threading.Event()
        deadline = monotonic() + self.timeout_s
        token_limit = self.max_tokens if max_tokens is None else max_tokens
        chat_messages = deepcopy(chat_messages)
        if schema is not None:
            instruction = '\nReturn only JSON. JSON schema: '+json.dumps(schema, ensure_ascii=False)
            if chat_messages and chat_messages[0]['role'] == 'system':
                chat_messages[0]['content'] += instruction
            else:
                chat_messages.insert(0, {'role': 'system', 'content': instruction})

        class Stop(StoppingCriteria):
            def __call__(self, input_ids, scores, **kwargs):
                return cancelled.is_set() or monotonic() >= deadline

        def generate():
            prompt = self.tokenizer.apply_chat_template(chat_messages, tokenize=False,
                                                        add_generation_prompt=True, enable_thinking=False)
            inputs = self.tokenizer(prompt, return_tensors="pt", add_special_tokens=False).to(self.model.device)
            with torch.inference_mode():
                outputs = self.model.generate(**inputs, do_sample=False, max_new_tokens=token_limit,
                                              stopping_criteria=StoppingCriteriaList([Stop()]))
            if monotonic() >= deadline:
                raise TimeoutError("planner_generation_timeout")
            if require_complete and outputs.shape[-1] - inputs.input_ids.shape[-1] >= token_limit:
                eos = getattr(getattr(self.model, 'generation_config', None), 'eos_token_id', None)
                eos = eos if eos is not None else getattr(self.tokenizer, 'eos_token_id', None)
                endings = eos if isinstance(eos, (tuple, list, set)) else [eos]
                if outputs[0, -1].item() not in endings:
                    raise ValueError('model_response_incomplete')
            return self.tokenizer.decode(outputs[0, inputs.input_ids.shape[1]:], skip_special_tokens=True)

        worker = asyncio.create_task(asyncio.to_thread(generate))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled.set()
            try:
                await worker
            except Exception:
                pass
            raise


class InprocessModelPlanner:
    """GGUF and the installed llama.cpp core in this Python process; no HTTP server."""
    supports_json_schema = True
    requires_remote = False

    def __init__(self, model=None, adapter=None, max_tokens=2048, timeout_s=90, constrained=True,
                 *, n_ctx=4096, threads=8, gpu_layers=-1, chat_template=None,
                 model_name='coegent-e4b-gguf'):
        from .llamacpp_native import NativeLlamaEngine
        root = Path(__file__).resolve().parents[2] / 'LLM_Model_Server'
        self.model_path = str(Path(model).expanduser().resolve()) if model is not None else str(root/'model/gguf/gemma-4-E4B_q4_0-it.gguf')
        template = Path(chat_template).expanduser().resolve() if chat_template else root/'config/e4b_no_thinking.jinja'
        if Path(self.model_path).suffix.lower() != '.gguf':
            raise ValueError('InprocessModelPlanner requires a GGUF file; use --planner hf for Transformers checkpoints')
        self.model_name = model_name
        self.max_tokens, self.timeout_s = max_tokens, timeout_s
        self.constrained = constrained
        self.generation_mode = 'json_schema_requested' if constrained else 'unconstrained'
        self._closed = False
        self._completion_lock = asyncio.Lock()
        self.runtime = NativeLlamaEngine(self.model_path, chat_template=template, n_ctx=n_ctx,
            threads=threads, gpu_layers=gpu_layers, model_name=model_name, adapter=adapter)

    async def plan(self, context):
        return await self.complete(messages(context), schema=context['schema'] if self.constrained else None)

    async def complete(self, chat_messages, *, schema=None, max_tokens=None, require_complete=False):
        import threading
        async with self._completion_lock:
            if self._closed: raise RuntimeError('inprocess planner is closed')
            payload = {'model':self.model_name,'messages':deepcopy(chat_messages),'temperature':0,
                       'max_tokens':self.max_tokens if max_tokens is None else max_tokens,'stream':False}
            if schema is not None: payload['response_format'] = response_format(schema)
            cancelled = threading.Event()
            worker = asyncio.create_task(asyncio.to_thread(self.runtime.request, '/v1/chat/completions',
                payload, timeout_s=self.timeout_s, cancelled=cancelled))
            try:
                data = await asyncio.shield(worker)
            except asyncio.CancelledError:
                cancelled.set()
                try: await worker
                except Exception: pass
                raise
            if require_complete and data['choices'][0].get('finish_reason') != 'stop':
                raise ValueError('model_response_incomplete')
            return data['choices'][0]['message']['content']

    def close(self):
        if not self._closed:
            self._closed = True
            self.runtime.close()

    def __enter__(self): return self
    def __exit__(self, *args): self.close()
