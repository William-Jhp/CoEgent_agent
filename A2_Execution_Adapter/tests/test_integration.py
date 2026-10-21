import asyncio
import json
from pathlib import Path
import sys
import time

import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'A2_Execution_Adapter'),str(ROOT/'CoEgent_Agent'),str(ROOT/'Executor_Adapters')]

def test_importing_host_integration_keeps_stdout_and_does_not_import_ros():
    import os,subprocess
    env=dict(os.environ,PYTHONPATH=':'.join([str(ROOT/'A2_Execution_Adapter'),str(ROOT/'CoEgent_Agent'),str(ROOT/'Executor_Adapters')]))
    r=subprocess.run([sys.executable,'-c','import sys; import a2_execution_adapter.integration; print("HOST_STDOUT_OK"); assert "rclpy" not in sys.modules'],env=env,text=True,capture_output=True)
    assert r.returncode==0 and r.stdout.strip()=='HOST_STDOUT_OK'

def battery_plan():
    return {'schema_version':'planner.v1','goal':'battery','sequence':[{'type':'skill','name':'RobotBackend.check_battery','args':{}}],
            'success_criteria':['API completed'],'final_outcome':'execute'}

class FakeROS:
    async def call(self,name,args):
        assert name=='RobotBackend.check_battery' and args=={}
        return .9
    async def snapshot(self):return {'robot':{'battery_percent':90}}
    async def cancel(self):return {'confirmed':True,'reason':'no_owned_motion'}
    async def close(self):pass

def bind(monkeypatch):
    from a2_execution_adapter import adapter
    from a2_execution_adapter.core import APIExecution
    from a2_execution_adapter.options import Options
    from coegent.tool_profiles import A2ToolProfile
    from coegent.types import State
    packets=[]
    class Transport:
        final=None
        def __init__(self,options,emit):self.options=options;self.emit=emit;self.done=asyncio.Event()
        async def run(self,packet):
            packets.append(packet)
            self.final=await APIExecution(FakeROS(),A2ToolProfile(),self.options,self.emit).run(packet['plan'],State())
            self.done.set();return self.final
        async def cancel(self):return {'confirmed':True,'reason':'no_owned_motion'}
    monkeypatch.setattr(adapter,'DockerTransport',Transport)
    return packets

def test_explicit_coegent_run_uses_selected_a2_adapter(tmp_path,monkeypatch):
    from coegent import cli
    packets=bind(monkeypatch)
    p=tmp_path/'plan.json';p.write_text(json.dumps(battery_plan()))
    out=tmp_path/'result.json'
    monkeypatch.setattr(sys,'argv',['coegent','run','--tool-profile','a2-isaac','--executor','a2-isaac',
        '--planner','file','--plan-file',str(p),'--command','battery','--out',str(out)])
    assert cli.main()==0
    r=json.loads(out.read_text())
    assert r['status']=='SUCCESS' and r['executor']=='a2-isaac'
    assert r['api_results'][0]['data']['value']==.9 and len(packets)==1
    assert r['goal_status']=='not_evaluated' and r['auto_saved_plan'] is None

def test_provided_request_does_not_build_or_call_any_planner(tmp_path,monkeypatch):
    from coegent import cli
    packets=bind(monkeypatch)
    def no_planner(*a,**kw):raise AssertionError('provided request must not load a planner')
    monkeypatch.setattr(cli,'build_planners',no_planner)
    request={'mission_id':'provided','revision':1,'expires_at':time.time()+30,'command':'battery','plan':battery_plan()}
    p=tmp_path/'request.json';p.write_text(json.dumps(request));out=tmp_path/'result.json'
    monkeypatch.setattr(sys,'argv',['coegent','request','--tool-profile','a2-isaac','--executor','a2-isaac',
        '--request-file',str(p),'--out',str(out)])
    assert cli.main()==0 and packets[0]['request']['mission_id']=='provided'
    assert json.loads(out.read_text())['planner_calls']==0

def test_clarification_never_starts_a_worker(tmp_path,monkeypatch):
    from coegent import cli
    from coegent.planner import ScriptedPlanner
    from coegent.planner_selector import PlannerSelection
    packets=bind(monkeypatch)
    class Selector:
        requires_remote=False
        async def select(self,context):return PlannerSelection('clarify',None,'missing','어느 장소인가요?')
    monkeypatch.setattr(cli,'build_planners',lambda *a,**kw:(ScriptedPlanner([]),Selector()))
    out=tmp_path/'result.json'
    monkeypatch.setattr(sys,'argv',['coegent','run','--tool-profile','a2-isaac','--executor','a2-isaac',
        '--planner','auto','--generation-planner','inprocess','--command','거기로 가','--out',str(out)])
    assert cli.main()==2 and packets==[]
    assert json.loads(out.read_text())['status']=='NEEDS_CLARIFICATION'

def test_legacy_strict_flags_are_not_silently_ignored_for_a2_requests(tmp_path,monkeypatch):
    from coegent import cli
    packets=bind(monkeypatch)
    request={'mission_id':'strict','revision':1,'expires_at':time.time()+30,'command':'battery','plan':battery_plan()}
    p=tmp_path/'request.json';p.write_text(json.dumps(request))
    monkeypatch.setattr(sys,'argv',['coegent','request','--tool-profile','a2-isaac','--executor','a2-isaac',
        '--state-mode','strict','--request-file',str(p),'--out',str(tmp_path/'result.json')])
    with pytest.raises(SystemExit) as exc:cli.main()
    assert exc.value.code==2 and packets==[]
