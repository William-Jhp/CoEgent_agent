import asyncio
import json
from pathlib import Path
import sys
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

def packet():return {'kind':'diagnose','request':{'mission_id':'owned','revision':1}}
def complete_report(status='SUCCESS'):
    return {'status':status,'execution_status':'NOT_EXECUTED' if status=='SUCCESS' else status,
            'reason':'done','mission_id':'owned','revision':1,'steps':0,'completed_steps':[],
            'api_results':[],'trace':[],'state':{'robot':{},'world':{},'mission':{},'observations':{}},
            'stop_confirmation':{'confirmed':True,'reason':'owned_goal_ack'}}

def test_docker_command_has_only_owned_mounts_and_no_gpu_or_other_service_restart(tmp_path):
    from a2_execution_adapter.transport import docker_command
    from a2_execution_adapter.options import Options
    cmd=docker_command(Options(runtime_dir=str(tmp_path)), 'coegent-a2-test')
    assert cmd[:3]==['docker','run','--rm']
    assert '--pull=never' in cmd and '--network=host' in cmd
    assert '--gpus' not in cmd and '/var/run/docker.sock' not in ' '.join(cmd)
    assert sum(x.endswith(':/runtime:rw') for x in cmd)==1
    assert any(x.endswith(':/adapter:ro') for x in cmd)
    assert cmd[-3:]==['python3','-m','a2_execution_adapter.worker']

def test_transport_reads_events_and_complete_result_from_real_subprocess(tmp_path,monkeypatch):
    from a2_execution_adapter import transport
    from a2_execution_adapter.options import Options
    script=tmp_path/'worker.py'
    script.write_text('import sys,json\np=json.loads(sys.stdin.readline())\n'
                      'print(json.dumps({"kind":"event","event":{"kind":"api_start","name":p["name"]}}),flush=True)\n'
                      'print('+repr(json.dumps({'kind':'result','report':complete_report()}))+',flush=True)\n')
    monkeypatch.setattr(transport,'docker_command',lambda o,n:[sys.executable,str(script)])
    events=[]
    t=transport.DockerTransport(Options(runtime_dir=str(tmp_path)),events.append)
    report=asyncio.run(t.run({**packet(),'name':'RobotBackend.check_battery'}))
    assert report['status']=='SUCCESS' and events==[{'kind':'api_start','name':'RobotBackend.check_battery'}]

@pytest.mark.parametrize('output',['not json','{"kind":"result","report":{"status":"SUCCESS"}}',''])
def test_invalid_or_missing_stop_evidence_cannot_become_success(tmp_path,monkeypatch,output):
    from a2_execution_adapter import transport
    from a2_execution_adapter.options import Options
    script=tmp_path/'bad.py';script.write_text('import sys\nsys.stdin.readline()\nprint('+repr(output)+',flush=True)\n')
    monkeypatch.setattr(transport,'docker_command',lambda o,n:[sys.executable,str(script)])
    report=asyncio.run(transport.DockerTransport(Options(runtime_dir=str(tmp_path))).run(packet()))
    assert report['status']=='SAFETY_ABORT' and report['stop_confirmation']['confirmed'] is False

def test_cancellation_is_sent_to_owned_worker_and_ack_is_preserved(tmp_path,monkeypatch):
    from a2_execution_adapter import transport
    from a2_execution_adapter.options import Options
    script=tmp_path/'cancel.py'
    script.write_text('import sys,json\nsys.stdin.readline()\n'
        'p=json.loads(sys.stdin.readline())\n'
        'r='+repr(complete_report('CANCELLED'))+'\nr["stop_confirmation"]["confirmed"]=p["kind"]=="cancel"\n'
        'print(json.dumps({"kind":"result","report":r}),flush=True)\n')
    monkeypatch.setattr(transport,'docker_command',lambda o,n:[sys.executable,str(script)])
    async def run():
        t=transport.DockerTransport(Options(runtime_dir=str(tmp_path),cancel_timeout_s=1))
        task=asyncio.create_task(t.run(packet()))
        while t.process is None:await asyncio.sleep(.001)
        ack=await t.cancel();report=await task
        assert ack['confirmed'] is True and report['status']=='CANCELLED'
    asyncio.run(run())

@pytest.mark.parametrize('change',[{'mission_id':'another'},{'revision':2},{'execution_status':'NOT_EXECUTED'},{'api_results':'not a list'}])
def test_execute_report_must_match_request_identity_and_execution_schema(tmp_path,monkeypatch,change):
    from a2_execution_adapter import transport
    from a2_execution_adapter.options import Options
    report={'status':'SUCCESS','execution_status':'SUCCESS','reason':'done','mission_id':'owned','revision':1,
            'stop_confirmation':{'confirmed':True,'reason':'owned_goal_ack'},'steps':1,
            'completed_steps':[],'api_results':[],'trace':[],
            'state':{'robot':{},'world':{},'mission':{},'observations':{}}}
    report.update(change)
    script=tmp_path/'wrong.py'
    script.write_text('import sys,json\nsys.stdin.readline()\nprint('+repr(json.dumps({'kind':'result','report':report}))+',flush=True)\n')
    monkeypatch.setattr(transport,'docker_command',lambda o,n:[sys.executable,str(script)])
    r=asyncio.run(transport.DockerTransport(Options(runtime_dir=str(tmp_path))).run(
        {'kind':'execute','request':{'mission_id':'owned','revision':1}}))
    assert r['status']=='SAFETY_ABORT' and r['stop_confirmation']['confirmed'] is False
