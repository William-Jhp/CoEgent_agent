"""Selector completion uses the configured transport, without loading weights."""
import asyncio
from contextlib import contextmanager, nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sys
import threading
from types import SimpleNamespace

import pytest

from coegent.planner import ModelServerPlanner, TransformersModelPlanner


@contextmanager
def model_api(finish='stop'):
    requests=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            body=json.dumps({'choices':[{'message':{'content':'{"decision":"generate"}'},'finish_reason':finish}]}).encode()
            self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    try: yield f'http://127.0.0.1:{server.server_port}',requests
    finally: server.shutdown();server.server_close();worker.join()


def test_http_generic_completion_sends_selector_schema_and_messages():
    with model_api() as (url,requests):
        client=ModelServerPlanner(url,'selector')
        assert hasattr(client,'complete'), 'missing generic model completion'
        schema={'type':'object','properties':{'decision':{'const':'generate'}},'required':['decision']}
        result=asyncio.run(client.complete([{'role':'user','content':'select'}],schema=schema,max_tokens=128,require_complete=True))
    assert json.loads(result)['decision']=='generate'
    assert requests[0]['response_format']['json_schema']['schema']==schema
    assert requests[0]['messages']==[{'role':'user','content':'select'}]
    assert requests[0]['max_tokens']==128


def test_http_selector_rejects_truncated_response():
    with model_api('length') as (url,_):
        client=ModelServerPlanner(url,'selector')
        assert hasattr(client,'complete'), 'missing generic model completion'
        with pytest.raises(ValueError,match='incomplete'):
            asyncio.run(client.complete([{'role':'user','content':'select'}],require_complete=True))


def test_inprocess_generic_completion_uses_local_generation_and_schema_prompt(monkeypatch):
    client=object.__new__(TransformersModelPlanner)
    assert hasattr(client,'complete'), 'missing inprocess selector completion'
    monkeypatch.setitem(sys.modules,'torch',SimpleNamespace(inference_mode=nullcontext))
    monkeypatch.setitem(sys.modules,'transformers',SimpleNamespace(StoppingCriteria=object,StoppingCriteriaList=list))
    seen={}
    class Tensor:
        shape=(1,2)
    class Inputs(dict):
        input_ids=Tensor()
        def to(self,device): return self
    class Tokenizer:
        eos_token_id=2
        def apply_chat_template(self,messages,**kwargs):
            seen['messages']=messages;seen['template_kwargs']=kwargs;return 'prompt'
        def __call__(self,*args,**kwargs): return Inputs(input_ids=Tensor())
        def decode(self,*args,**kwargs): return '{"decision":"generate"}'
    class Outputs:
        shape=(1,5)
        def __getitem__(self,key): return SimpleNamespace(item=lambda:2) if key==(0,-1) else 'generated'
    class Model:
        device='cpu'
        generation_config=SimpleNamespace(eos_token_id=2)
        def generate(self,**kwargs): seen['max_tokens']=kwargs['max_new_tokens'];return Outputs()
    client.tokenizer=Tokenizer();client.model=Model();client.timeout_s=1;client.max_tokens=256
    value=asyncio.run(client.complete([{'role':'user','content':'select'}],schema={'type':'object'},max_tokens=64,require_complete=True))
    assert json.loads(value)['decision']=='generate'
    assert seen['max_tokens']==64 and 'JSON schema' in seen['messages'][0]['content']
    assert seen['template_kwargs']['enable_thinking'] is False
