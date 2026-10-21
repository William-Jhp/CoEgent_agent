"""Real HTTP protocol fixtures exercise auto routing without model inference."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import subprocess
import sys
import threading

import pytest

from coegent import PlanStore
from coegent.scenarios import plan, skill


@contextmanager
def api(decision,generated):
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])));calls.append(payload)
            schema=payload.get('response_format',{}).get('json_schema',{}).get('schema',{})
            value=decision if schema.get('title') == 'planner_selection' or 'decision' in schema.get('properties',{}) else generated
            body=json.dumps({'choices':[{'message':{'content':json.dumps(value)},'finish_reason':'stop'}]}).encode()
            self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try: yield f'http://127.0.0.1:{server.server_port}',calls
    finally: server.shutdown();server.server_close();thread.join()


@pytest.mark.parametrize('route',['reuse','generate','clarify'])
def test_auto_cli_routes_via_selector_then_native_adapter_or_question(tmp_path,route):
    db=tmp_path/'plans.sqlite3';store=PlanStore(db)
    stored=plan(skill('navigate_to',target='A'))
    plan_id=store.add(stored,command='A로 이동해')['id']
    decision={'decision':route,'plan_id':plan_id if route=='reuse' else None,
              'reason':'fixture selection','question':'어느 장소인가요?' if route=='clarify' else None}
    with api(decision,plan(skill('get_battery'))) as (url,calls):
        output=tmp_path/'report.json'
        result=subprocess.run([sys.executable,'-m','coegent','run','--planner','auto',
            '--generation-planner','http','--endpoint',url,'--model','fixture',
            '--plan-db',str(db),'--command','A로 이동해','--out',str(output)],capture_output=True,text=True)
    assert result.returncode==(2 if route=='clarify' else 0),result.stderr
    report=json.loads(output.read_text())
    assert report['planner_selections'][0]['decision']==route
    assert len(calls)==(2 if route=='generate' else 1)
    if route=='clarify':
        assert report['status']=='NEEDS_CLARIFICATION' and report['execution_status']=='NOT_EXECUTED'
        assert report['question']=='어느 장소인가요?' and '어느 장소인가요?' in result.stdout
        assert not any(entry['kind']=='skill_start' for entry in report['trace'])
    else:
        assert report['status']=='SUCCESS' and report['executor']=='coegent'


def test_separate_selector_http_model_and_generator_are_independent(tmp_path):
    decision={'decision':'generate','plan_id':None,'reason':'new plan needed','question':None}
    with api(decision,{}) as (selector_url,selection_calls), api({},plan(skill('get_pose'))) as (generator_url,generation_calls):
        output=tmp_path/'report.json'
        result=subprocess.run([sys.executable,'-m','coegent','plan','--planner','auto',
            '--endpoint',generator_url,'--model','generator','--selector-backend','http',
            '--selector-endpoint',selector_url,'--selector-model','judge',
            '--plan-db',str(tmp_path/'db.sqlite3'),'--command','위치 확인','--out',str(output)],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert selection_calls[0]['model']=='judge' and generation_calls[0]['model']=='generator'
    assert json.loads(output.read_text())['status']=='PLAN_READY'
