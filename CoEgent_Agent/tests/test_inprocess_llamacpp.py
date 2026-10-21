"""GGUF inprocess requests use the same chat/schema contract without HTTP."""
import asyncio
from copy import deepcopy
import json
import sys
import threading
from types import SimpleNamespace

import pytest

from coegent.planner import InprocessModelPlanner, response_format
from coegent.planner_selector import PlannerSelector
from coegent.plan_store import PlanStore
from coegent.planning import build_plan_context
from coegent.registry import SkillRegistry
from coegent.safety import SafetyPolicy
from coegent.scenarios import initial_state


@pytest.fixture
def native(monkeypatch):
    seen = {'requests': [], 'instances': []}
    class Engine:
        def __init__(self, model_path, **kwargs):
            seen['instances'].append((model_path, kwargs));self.closed=False
        def request(self, route, payload=None, *, timeout_s=90, cancelled=None):
            seen['requests'].append((route, deepcopy(payload), timeout_s))
            return {'choices': [{'finish_reason':seen.get('finish','stop'), 'message': {
                'content':json.dumps({'reason':'clear new task','decision':'generate','plan_id':None,'question':None})}}]}
        def close(self):self.closed=True;seen['closed']=True
    monkeypatch.setitem(sys.modules,'coegent.llamacpp_native',SimpleNamespace(NativeLlamaEngine=Engine))
    # Fail immediately if the former Transformers implementation is still selected.
    def wrong(*args,**kwargs):raise AssertionError('Transformers used instead of native llama.cpp')
    monkeypatch.setitem(sys.modules,'torch',SimpleNamespace(bfloat16='bf16'))
    monkeypatch.setitem(sys.modules,'transformers',SimpleNamespace(
        AutoTokenizer=SimpleNamespace(from_pretrained=wrong),AutoModelForCausalLM=SimpleNamespace(from_pretrained=wrong)))
    return seen


def test_default_is_e4b_gguf_and_schema_enabled(native,tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    client=InprocessModelPlanner()
    path,options=native['instances'][0]
    assert str(path).endswith('/LLM_Model_Server/model/gguf/gemma-4-E4B_q4_0-it.gguf')
    assert options['n_ctx']==4096 and options['threads']==8 and options['gpu_layers']==-1
    assert client.supports_json_schema and client.constrained
    assert client.requires_remote is False


def test_complete_sends_identical_json_schema_payload_without_mutating_input(native):
    client=InprocessModelPlanner(max_tokens=1536)
    schema={'$defs':{'predicate':{'properties':{'value':{}}}},'type':'object'}
    original=deepcopy(schema);messages=[{'role':'user','content':'select'}]
    result=asyncio.run(client.complete(messages,schema=schema,max_tokens=512,require_complete=True))
    route,payload,timeout=native['requests'][0]
    assert route=='/v1/chat/completions'
    assert payload=={'model':client.model_name,'messages':messages,'temperature':0,'max_tokens':512,
                     'stream':False,'response_format':response_format(schema)}
    assert schema==original and json.loads(result)['decision']=='generate'


def test_real_selector_receives_constrained_schema_in_native_path(native,tmp_path):
    client=InprocessModelPlanner()
    selector=PlannerSelector(PlanStore(tmp_path/'plans.sqlite3'),client)
    context=build_plan_context('현재 위치를 확인해.',initial_state(),SkillRegistry(),SafetyPolicy())
    selected=asyncio.run(selector.select(context))
    assert selected.decision=='generate'
    wire=native['requests'][0][1]['response_format']['json_schema']['schema']
    assert [r['properties']['decision']['const'] for r in wire['anyOf']]==['generate','clarify']


def test_incomplete_native_response_is_rejected(native):
    native['finish']='length';client=InprocessModelPlanner()
    with pytest.raises(ValueError,match='model_response_incomplete'):
        asyncio.run(client.complete([{'role':'user','content':'select'}],require_complete=True))


def test_cli_inprocess_auto_enables_schema(native):
    from coegent.cli import model_planner
    args=SimpleNamespace(model=None,adapter=None,endpoint=None,max_tokens=1024,
                         planner_timeout=90,generation_mode='auto')
    client=model_planner('inprocess',args)
    assert client.constrained and client.supports_json_schema


def test_close_releases_engine_and_prevents_further_requests(native):
    client=InprocessModelPlanner();client.close();client.close()
    assert native['closed']
    with pytest.raises(RuntimeError,match='closed'):
        asyncio.run(client.complete([{'role':'user','content':'x'}]))


def test_cancellation_waits_for_native_worker_before_returning(native,monkeypatch):
    client=InprocessModelPlanner();entered=threading.Event();finished=threading.Event()
    def slow(route,payload=None,*,timeout_s=90,cancelled=None):
        entered.set();assert cancelled.wait(2);finished.set();raise TimeoutError('cancelled')
    monkeypatch.setattr(client.runtime,'request',slow)
    async def exercise():
        task=asyncio.create_task(client.complete([{'role':'user','content':'x'}]))
        await asyncio.to_thread(entered.wait,1);task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert finished.is_set()
    asyncio.run(exercise())
