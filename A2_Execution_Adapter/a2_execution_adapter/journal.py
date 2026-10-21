"""Worker ownership and persistent replay protection, independent of the plan DB."""
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3

class Journal:
    def __init__(self,root,robot):self.root=Path(root);self.robot=robot;self.lock=None;self.db=None
    def __enter__(self):
        self.root.mkdir(parents=True,exist_ok=True)
        key=hashlib.sha256(self.robot.encode()).hexdigest()[:16]
        self.lock=open(self.root/(key+'.lock'),'a+')
        try:fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close();raise ValueError('robot_busy') from None
        self.db=sqlite3.connect(self.root/(key+'.sqlite3'))
        self.db.execute('CREATE TABLE IF NOT EXISTS missions (id TEXT, revision INTEGER, fingerprint TEXT, motion INTEGER, phase TEXT, report TEXT, PRIMARY KEY(id,revision))')
        self.db.execute('CREATE TABLE IF NOT EXISTS recoveries (id TEXT, revision INTEGER, evidence TEXT, PRIMARY KEY(id,revision))')
        self.db.commit();return self
    def begin(self,mission_id,revision,payload,*,motion):
        digest=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
        row=self.db.execute('SELECT fingerprint,phase,report FROM missions WHERE id=? AND revision=?',(mission_id,revision)).fetchone()
        if row:
            if row[0]!=digest:raise ValueError('request_identity_conflict')
            if row[1]=='done':return {**json.loads(row[2]),'replayed':True}
            raise ValueError('request_in_doubt')
        newest=self.db.execute('SELECT MAX(revision) FROM missions WHERE id=?',(mission_id,)).fetchone()[0]
        if newest is not None and revision<newest:raise ValueError('stale_revision')
        if motion and self.db.execute("SELECT 1 FROM missions WHERE motion=1 AND phase!='done' LIMIT 1").fetchone():
            raise ValueError('prior_motion_in_doubt')
        with self.db:self.db.execute('INSERT INTO missions VALUES (?,?,?,?,?,NULL)',(mission_id,revision,digest,int(motion),'running'))
    def finish(self,mission_id,revision,report):
        phase='in_doubt' if report.get('stop_confirmation',{}).get('confirmed') is False else 'done'
        with self.db:self.db.execute('UPDATE missions SET phase=?,report=? WHERE id=? AND revision=?',
                                    (phase,json.dumps(report,ensure_ascii=False,allow_nan=False),mission_id,revision))
    def failed_localization(self,mission_id,revision,legacy_report=None):
        row=self.db.execute('SELECT phase,report FROM missions WHERE id=? AND revision=?',(mission_id,revision)).fetchone()
        if not row or row[0]!='in_doubt' or not row[1]:raise ValueError('no_recorded_localization_failure')
        report=json.loads(row[1]);calls=report.get('api_results',[])
        if (report.get('completed_steps') or len(calls)!=1 or calls[0].get('name') not in ('RobotBackend.go_to','RobotBackend.localize') or
                calls[0].get('reason')!='RobotError:localization ERROR: process_exit'):
            raise ValueError('recovery_requires_localization_only_failure_before_path_dispatch')
        ownership=report.get('runtime_ownership')
        if ownership is None and legacy_report is not None:
            for key in ('mission_id','revision','map_identity','api_results','trace','status','completed_steps'):
                if legacy_report.get(key)!=report.get(key):raise ValueError('legacy_report_differs_from_journal')
            events=[t.get('event',{}) for t in legacy_report.get('adapter_trace',[]) if t.get('kind')=='a2_worker']
            starts=[e for e in events if e.get('kind')=='backend_event' and e.get('type')=='localization' and e.get('state')=='START']
            if len(starts)!=1 or any(e.get('type') in ('path','path_result') for e in events):raise ValueError('legacy_report_path_or_ownership_unproven')
            ownership={'task_id':starts[0]['task_id'],'map_uuid':starts[0]['map_uuid'],'path_dispatched':False}
        if (not ownership or ownership.get('path_dispatched') is not False or
                not str(ownership.get('task_id','')).startswith('gemma-coegent-') or
                ownership.get('map_uuid')!=report.get('map_identity',{}).get('map_uuid')):
            raise ValueError('recorded_localization_ownership_or_no_path_evidence_missing')
        report['runtime_ownership']=ownership
        return report

    def reconcile_localization(self,mission_id,revision,evidence,legacy_report=None):
        original=self.failed_localization(mission_id,revision,legacy_report)
        if (evidence.get('confirmed') is not True or not str(evidence.get('task_id','')).startswith('gemma-coegent-') or
                evidence.get('map_uuid')!=original.get('map_identity',{}).get('map_uuid') or
                evidence.get('before',{}).get('state')!='ERROR' or evidence['before'].get('reason')!='process_exit' or
                evidence.get('after',{}).get('state')!='STOPPED' or evidence['after'].get('reason')!='reset'):
            raise ValueError('verified_failed_localization_reset_required')
        owner=original['runtime_ownership']['task_id']
        if (evidence['task_id']!=owner or evidence['before'].get('task_id')!=owner or
                evidence['before'].get('map_uuid')!=evidence['map_uuid'] or evidence['after'].get('task_id') is not None):
            raise ValueError('recovery_owner_differs_from_recorded_owner')
        if legacy_report is not None:
            evidence={**evidence,'legacy_report_sha256':hashlib.sha256(json.dumps(legacy_report,sort_keys=True).encode()).hexdigest()}
        with self.db:
            self.db.execute('INSERT INTO recoveries VALUES (?,?,?)',(mission_id,revision,json.dumps(evidence,allow_nan=False)))
            self.db.execute("UPDATE missions SET phase='done' WHERE id=? AND revision=?",(mission_id,revision))
    def __exit__(self,*unused):
        if self.db:self.db.close()
        if self.lock:self.lock.close()
