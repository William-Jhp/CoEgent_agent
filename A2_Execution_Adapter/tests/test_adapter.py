import asyncio
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'A2_Execution_Adapter'),str(ROOT/'CoEgent_Agent'),str(ROOT/'Executor_Adapters')]

def test_prepare_checks_whole_plan_without_starting_worker(monkeypatch):
    from a2_execution_adapter.adapter import A2ExecutorAdapter
    from coegent.types import State,Trace,Limits
    from coegent.admission import MissionRequest
    from coegent_executor_adapters import ExecutorContext
    from time import time
    ctx=ExecutorContext('a2-isaac',None,State(),Trace(),Limits(),MissionRequest('test',1,time()+60,'battery'))
    adapter=A2ExecutorAdapter(ctx,{})
    plan={'schema_version':'planner.v1','goal':'battery','sequence':[{'type':'skill','name':'RobotBackend.check_battery','args':{}}],
          'success_criteria':['completed'],'final_outcome':'execute'}
    p=asyncio.run(adapter.prepare(plan))
    assert p.executor=='a2-isaac' and adapter.transport is None
    plan['sequence'][0]['name']='RobotBackend.go_to'
    with pytest.raises(ValueError):asyncio.run(adapter.prepare(plan))

def test_worker_rejects_invalid_plan_before_ros_import(tmp_path):
    import json,os,subprocess,time
    from a2_execution_adapter.options import Options
    from dataclasses import asdict
    packet={'kind':'execute','plan':{},'state':{},'options':asdict(Options(runtime_dir=str(tmp_path))),
            'request':{'mission_id':'invalid','revision':1,'expires_at':time.time()+60,'command':'invalid'}}
    env=dict(os.environ,PYTHONPATH=':'.join([str(ROOT/'A2_Execution_Adapter'),str(ROOT/'CoEgent_Agent'),str(ROOT/'Executor_Adapters')]))
    r=subprocess.run([sys.executable,'-m','a2_execution_adapter.worker'],input=json.dumps(packet)+'\n',env=env,
                     text=True,capture_output=True,timeout=10)
    assert r.returncode==0, r.stderr
    reply=json.loads(r.stdout)
    assert reply['report']['status']=='INVALID_PLAN'
    assert reply['report']['stop_confirmation']['reason']=='no_dispatch'

def test_worker_expired_request_has_no_ros_startup(tmp_path):
    import json,os,subprocess,time
    from a2_execution_adapter.options import Options
    from dataclasses import asdict
    packet={'kind':'diagnose','options':asdict(Options(runtime_dir=str(tmp_path))),
            'request':{'mission_id':'expired','revision':1,'expires_at':time.time()-1,'command':'diagnose'}}
    env=dict(os.environ,PYTHONPATH=':'.join([str(ROOT/'A2_Execution_Adapter'),str(ROOT/'CoEgent_Agent'),str(ROOT/'Executor_Adapters')]))
    r=subprocess.run([sys.executable,'-m','a2_execution_adapter.worker'],input=json.dumps(packet)+'\n',env=env,
                     text=True,capture_output=True,timeout=10)
    assert r.returncode==0,r.stderr
    report=json.loads(r.stdout)['report']
    assert report['status']=='REJECTED' and report['reason']=='request_expired'
