import json
import subprocess
import sys
import time


def test_inspect_cli_compares_independent_evidence_and_emits_upstream_result(tmp_path):
    report={'mission_id':'m','revision':1,'report_id':'r','kind':'completed','message':'arrived'}
    cfg={'checks':[{'id':'arrival','source':'robot_state','path':'pose','op':'near','expected':[0,0],'tolerance':.2}]}
    evidence={'mission_id':'m','revision':1,'values':{'robot_state':{'pose':[2,0]}},'observed_at':{'robot_state':time.time()}}
    for name,value in [('report',report),('config',cfg),('evidence',evidence)]:
        (tmp_path/(name+'.json')).write_text(json.dumps(value))
    out=tmp_path/'out.json'
    p=subprocess.run([sys.executable,'-m','coegent','inspect','--report',str(tmp_path/'report.json'),
        '--inspector-config',str(tmp_path/'config.json'),'--inspection-evidence',str(tmp_path/'evidence.json'),
        '--out',str(out)],capture_output=True,text=True)
    assert p.returncode==2, p.stderr
    assert out.exists(), p.stderr
    result=json.loads(out.read_text())
    assert result['decision']=='replan'
    assert result['completion_report']['verified'] is False
    assert result['completion_report']['mission_id']=='m'
