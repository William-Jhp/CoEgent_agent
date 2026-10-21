"""Owned Docker worker with bounded JSON stdio and an explicit cancel channel."""
import asyncio
from collections import deque
import json
import os
from pathlib import Path
from uuid import uuid4
from coegent.validation import strict_json
from coegent.types import State
from .options import PACKAGE_ROOT

def docker_command(options,name):
    import coegent, coegent_executor_adapters
    root=Path(options.source_root).expanduser().resolve()
    runtime=Path(options.runtime_dir).expanduser().resolve()
    runtime.mkdir(parents=True,exist_ok=True)
    mounts=[(root,'/rig','ro'),(PACKAGE_ROOT,'/adapter','ro'),
            (Path(coegent.__file__).resolve().parents[1],'/coegent','ro'),
            (Path(coegent_executor_adapters.__file__).resolve().parents[1],'/executor_adapters','ro'),
            (runtime,'/runtime','rw')]
    if options.scene_dir:mounts.append((Path(options.scene_dir).expanduser().resolve(),'/scene','ro'))
    cmd=['docker','run','--rm','--pull=never','--init','-i','--name',name,'--network=host',
         '--user',f'{os.getuid()}:{os.getgid()}','--read-only','--tmpfs','/tmp:rw,nosuid,size=128m']
    for source,target,mode in mounts:cmd+=['--volume',f'{source}:{target}:{mode}']
    return cmd+[options.image,'python3','-m','a2_execution_adapter.worker']

def fault(reason,stderr=''):
    return {'status':'SAFETY_ABORT','execution_status':'SAFETY_ABORT','reason':reason,
            'stop_confirmation':{'confirmed':False,'reason':'worker_stop_unconfirmed'},
            'goal_status':'not_evaluated','api_results':[],'trace':[],'completed_steps':[],
            'worker_stderr':stderr[-8000:]}

class DockerTransport:
    def __init__(self,options,emit=None):
        self.options,self.emit=options,emit
        self.name='coegent-a2-'+uuid4().hex[:16]
        self.process=None;self.final=None;self.done=asyncio.Event()
        self.stderr=deque(maxlen=30);self._cancel_sent=False
        self.packet=None

    def _validate_report(self,report):
        req=self.packet['request']
        if report.get('mission_id')!=req['mission_id'] or type(report.get('revision')) is not int or report['revision']!=req['revision']:
            raise ValueError('worker_response_identity_mismatch')
        status=report['status'];execution=report.get('execution_status')
        wanted='NOT_EXECUTED' if status in ('INVALID_PLAN','REJECTED') or (status=='SUCCESS' and self.packet['kind'] in ('diagnose','reconcile')) else status
        if execution!=wanted or not isinstance(report.get('reason'),str):raise ValueError('inconsistent_execution_status')
        if type(report.get('steps')) is not int or report['steps']<0:raise ValueError('invalid_step_count')
        for key in ('completed_steps','api_results','trace'):
            if not isinstance(report.get(key),list):raise ValueError('invalid_result_'+key)
        if len(report['completed_steps'])>report['steps']:raise ValueError('completed_count_exceeds_steps')
        if status=='SUCCESS':State.from_dict(report.get('state'))

    async def _stderr(self):
        while True:
            line=await self.process.stderr.readline()
            if not line:break
            self.stderr.append(line.decode(errors='replace')[:2000])

    async def _read(self):
        while True:
            line=await self.process.stdout.readline()
            if not line:break
            value=strict_json(line.decode())
            if not isinstance(value,dict):raise ValueError('worker message is not an object')
            if value.get('kind')=='event':
                if not isinstance(value.get('event'),dict):raise ValueError('invalid event')
                if self.emit:self.emit(value['event'])
            elif value.get('kind')=='result':
                report=value.get('report')
                if not isinstance(report,dict) or report.get('status') not in {
                    'SUCCESS','REJECTED','INVALID_PLAN','EXPECTED_FAILURE','REPLAN_REQUIRED','SAFETY_ABORT','CANCELLED','ABORTED'}:
                    raise ValueError('invalid result')
                ack=report.get('stop_confirmation')
                if (not isinstance(ack,dict) or type(ack.get('confirmed')) is not bool or not isinstance(ack.get('reason'),str)):
                    raise ValueError('missing stop evidence')
                if report['status']=='SUCCESS' and ack['confirmed'] is not True:raise ValueError('success without stop evidence')
                self._validate_report(report)
                if self.final is not None:raise ValueError('duplicate result')
                self.final=report
            else:raise ValueError('unknown worker message')
        await asyncio.wait_for(self.process.wait(),self.options.cancel_timeout_s)
        if self.process.returncode!=0:raise ValueError('worker_exit:'+str(self.process.returncode))
        if self.final is None:raise ValueError('worker_eof_without_result')
        return self.final

    async def run(self,packet):
        err=None
        try:
            if packet.get('kind') not in ('execute','diagnose','reconcile') or not isinstance(packet.get('request'),dict):
                raise ValueError('invalid_worker_request')
            self.packet=packet
            self.process=await asyncio.create_subprocess_exec(*docker_command(self.options,self.name),
                stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,limit=1024*1024)
            err=asyncio.create_task(self._stderr())
            self.process.stdin.write((json.dumps(packet,ensure_ascii=False,allow_nan=False)+'\n').encode())
            await self.process.stdin.drain()
            return await asyncio.wait_for(self._read(),self.options.startup_timeout_s+self.options.mission_timeout_s)
        except asyncio.CancelledError:
            await self._abort_and_drain()
            raise
        except Exception as exc:
            # A protocol/transport failure cannot retain a previous SUCCESS.
            self.final=None
            await self._abort_and_drain()
            self.final=fault(type(exc).__name__+':'+str(exc),''.join(self.stderr))
            return self.final
        finally:
            if self.process and self.process.stdin:self.process.stdin.close()
            if err:
                try:await asyncio.wait_for(err,2)
                except (asyncio.TimeoutError,asyncio.CancelledError):err.cancel()
            self.done.set()

    async def _send_cancel(self):
        if self.process and self.process.returncode is None and not self._cancel_sent:
            self._cancel_sent=True
            try:
                self.process.stdin.write(b'{"kind":"cancel"}\n');await self.process.stdin.drain()
            except (BrokenPipeError,ConnectionResetError):pass

    async def _abort_and_drain(self):
        if not self.process or self.process.returncode is not None:return
        await self._send_cancel()
        try:await asyncio.wait_for(self._read(),self.options.cancel_timeout_s)
        except Exception:
            # Only this adapter's named container can be removed. Exit is NOT stop proof.
            p=await asyncio.create_subprocess_exec('docker','rm','-f',self.name,
                stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
            try:await asyncio.wait_for(p.wait(),5)
            except asyncio.TimeoutError:p.kill();await p.wait()
            if self.process.returncode is None:
                self.process.kill();await self.process.wait()

    async def cancel(self):
        if self.process is None:return {'confirmed':True,'reason':'no_adapter_owned_work'}
        await self._send_cancel()
        try:await asyncio.wait_for(self.done.wait(),self.options.cancel_timeout_s+2)
        except asyncio.TimeoutError:return {'confirmed':False,'reason':'worker_cancel_timeout'}
        return (self.final or {}).get('stop_confirmation',{'confirmed':False,'reason':'worker_stop_unconfirmed'})
