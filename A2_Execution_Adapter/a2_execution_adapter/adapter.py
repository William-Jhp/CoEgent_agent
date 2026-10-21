"""Host-side selected-only implementation of ExecutorAdapter."""
from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path

from coegent_executor_adapters import ExecutionProgress, PreparedPlan
from coegent.tool_profiles import A2ToolProfile
from coegent.types import Event, SkillResult
from .core import validate_execution
from .binding import portable_contract, scene_binding
from .options import Options
from .transport import DockerTransport

class A2ExecutorAdapter:
    def __init__(self,context,options):
        self.context=context;self.options=Options.from_dict(options)
        self.profile=A2ToolProfile(self.options.source_root,scene_dir=self.options.scene_dir)
        self.transport=None;self.report=None;self._prepared=None;self._closed=False
        self._completed=[];self._failed=None

    @property
    def progress(self):
        r=self.report or {}
        return ExecutionProgress(r.get('steps',len(self._completed)),deepcopy(r.get('completed_steps',self._completed)),
            deepcopy(self._failed),deepcopy(r.get('remaining_plan',[])),deepcopy(r.get('stop_confirmation')))

    async def prepare(self,plan):
        if self._closed:raise ValueError('executor is closed')
        canonical=validate_execution(plan,self.profile,self.options)
        payload={'plan':canonical,'contract_hash':portable_contract(self.profile),'scene_binding':scene_binding(self.profile.root,self.options.scene_dir)}
        self._prepared=deepcopy(payload)
        return PreparedPlan(self.context.name,deepcopy(payload))

    def _event(self,event):
        self.context.trace.emit('a2_worker',event=event)
        if self.options.stream_events:
            shown=deepcopy(event)
            if 'points' in shown:shown['points_count']=len(shown.pop('points'))
            print(json.dumps(shown,ensure_ascii=False),flush=True)
        if event.get('kind')=='api_result':
            if event.get('status')=='success':self._completed.append({'name':event['name'],'args':deepcopy(event.get('args',{}))})
            else:self._failed=deepcopy(event)

    async def execute(self,prepared):
        if self._closed or prepared.executor!=self.context.name or prepared.payload!=self._prepared or self._prepared is None:
            raise ValueError('invalid or unprepared A2 plan')
        if self.transport is not None:raise ValueError('adapter instances execute one mission only')
        self.transport=DockerTransport(self.options,self._event)
        worker_options=asdict(replace(self.options,source_root='/rig',runtime_dir='/runtime',scene_dir='/scene' if self.options.scene_dir else None))
        packet={'kind':'execute',**deepcopy(self._prepared),'options':worker_options,
                'state':self.context.state.snapshot(),
                'request':{k:v for k,v in asdict(self.context.request).items() if k!='plan'}}
        # JSON tuple/list mismatch is avoided; explicit goals are unsupported and rejected.
        if packet['request'].get('goals'):
            raise ValueError('approved goal evidence is not bound for A2')
        packet['request'].pop('goals',None)
        try:self.report=await self.transport.run(packet)
        except BaseException:
            self.report=self.transport.final
            raise
        status=self.report['status']
        event={'SUCCESS':Event.SUCCESS,'REPLAN_REQUIRED':Event.REPLAN_REQUIRED,
               'EXPECTED_FAILURE':Event.EXPECTED_FAILURE}.get(status,Event.SAFETY_ABORT)
        return SkillResult(self.context.name,event,self.report.get('reason',''),{'report':deepcopy(self.report)})

    async def status(self):return self.progress

    async def cancel(self):
        ack=await self.transport.cancel() if self.transport else {'confirmed':True,'reason':'no_adapter_owned_work'}
        return SkillResult('stop',Event.SUCCESS if ack.get('confirmed') is True else Event.SAFETY_ABORT,
                           ack.get('reason','stop_unconfirmed'),{'stopped':ack.get('confirmed') is True,'scope':'adapter_owned_work'})

    async def close(self):
        if self.transport and not self.transport.done.is_set():await self.cancel()
        self._closed=True
