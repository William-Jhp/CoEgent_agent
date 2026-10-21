"""Durable, planning-only clarification. Never checkpoints an execution backend."""
from contextlib import asynccontextmanager,contextmanager
from dataclasses import asdict
from copy import deepcopy
import fcntl
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import TypedDict

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import StateGraph,START,END
from langgraph.types import Command,interrupt
from coegent_contracts.registry import fingerprint
from .types import State


class PlanningState(TypedDict):
    command: str
    snapshot: dict
    answers: list
    report: dict
    contract: str
    turns: int


def json_copy(value):return json.loads(json.dumps(value,allow_nan=False))


class PlanningSession:
    """Caller configures the PlanGenerator on each process, with the same contract.

    The checkpoint DB must be separate from the plan catalog and execution journal.
    Physical restart/replay is deliberately absent. All returned plans still require
    current-state validation and admission at their eventual executor boundary.
    """
    def __init__(self,generator,checkpoint_db,*,max_turns=8):
        from .planning import PlanGenerator
        if not isinstance(generator,PlanGenerator):raise ValueError('a PlanGenerator is required')
        if type(max_turns) is not int or not 1<=max_turns<=32:raise ValueError('invalid max_turns')
        if str(checkpoint_db)==':memory:':raise ValueError('persistent checkpoint file required')
        self.generator,self.path,self.max_turns=generator,Path(checkpoint_db).expanduser().resolve(),max_turns
        from .plan_store import default_plan_db
        if self.path==default_plan_db().resolve():raise ValueError('use a dedicated checkpoint database')
        if self.path.exists():
            with sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True) as connection:
                tables={row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if (connection.execute('PRAGMA application_id').fetchone()[0]!=0 or
                        tables-{'checkpoints','writes','checkpoint_migrations','sqlite_sequence'}):
                    raise ValueError('use a dedicated checkpoint database')
        config=asdict(generator.config)
        for key,value in config.items():
            if isinstance(value,frozenset):config[key]=sorted(value)
        self.contract=fingerprint({'registry':generator.registry.fingerprint,'schema':generator.validator.schema,
            'config':config,'max_turns':max_turns,'workflow_version':1,
            'scene':getattr(generator.tool_profile,'scene_binding',None)})

    @contextmanager
    def _lock(self,thread_id):
        if not isinstance(thread_id,str) or not thread_id.strip() or len(thread_id)>256:
            raise ValueError('invalid thread_id')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        locks=self.path.parent/(self.path.name+'.locks');locks.mkdir(exist_ok=True)
        lock=(locks/(hashlib.sha256(thread_id.encode()).hexdigest()+'.lock')).open('a+')
        try:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise RuntimeError('workflow_thread_busy') from None
            yield
        finally:lock.close()

    def _graph(self,checkpointer):
        async def generate(state):
            if state['turns']>=self.max_turns:
                return {'report':{'status':'PLANNER_ERROR','reason':'clarification_limit',
                                  'execution_status':'NOT_EXECUTED','plan':None,'question':None}}
            command=state['command']
            if state['answers']:
                command+='\n추가 확인된 사용자 정보:\n'+json.dumps(state['answers'],ensure_ascii=False)
            report=await self.generator.generate(command,State.from_dict(state['snapshot']))
            return {'report':json_copy(report),'turns':state['turns']+1}

        async def clarify(state):
            answer=interrupt({'question':state['report']['question'],'command':state['command']})
            self._answer(answer)
            return {'answers':state['answers']+[{'question':state['report']['question'],'answer':answer}]}

        async def finish(state):return {}
        graph=StateGraph(PlanningState)
        graph.add_node('generate_plan',generate);graph.add_node('await_information',clarify);graph.add_node('report',finish)
        graph.add_edge(START,'generate_plan')
        graph.add_conditional_edges('generate_plan',lambda s:'await_information' if s['report']['status']=='NEEDS_CLARIFICATION' else 'report',
                                    {'await_information':'await_information','report':'report'})
        graph.add_edge('await_information','generate_plan');graph.add_edge('report',END)
        return graph.compile(checkpointer=checkpointer,name='coegent_planning_session')

    @staticmethod
    def _answer(answer):
        if not isinstance(answer,str) or not answer.strip() or len(answer)>12000:
            raise ValueError('answer must be nonempty text up to 12000 characters')

    @asynccontextmanager
    async def _open(self,thread_id):
        # A persistent checkpointer is not a same-thread concurrency lock.
        with self._lock(thread_id):
            async with AsyncSqliteSaver.from_conn_string(str(self.path)) as saver:
                graph=self._graph(saver)
                config={'configurable':{'thread_id':thread_id},'recursion_limit':8*(self.max_turns+1)}
                yield graph,config

    def _check_contract(self,snapshot):
        if snapshot.values and snapshot.values.get('contract')!=self.contract:
            raise ValueError('workflow contract changed; start a new thread')

    @staticmethod
    def _result(thread_id,snapshot):
        value=snapshot.values
        report=deepcopy(value.get('report',{}))
        paused=bool(snapshot.next and report.get('status')=='NEEDS_CLARIFICATION')
        return json_copy({'thread_id':thread_id,'orchestrator':'langgraph','status':
            'NEEDS_CLARIFICATION' if paused else report.get('status','INCOMPLETE'),
            'question':report.get('question') if paused else None,'report':report,
            'answers':value.get('answers',[]),'next':list(snapshot.next),'physical_resume':False})

    async def start(self,thread_id,command,state):
        if not isinstance(command,str) or not command.strip() or len(command)>12000:raise ValueError('invalid command')
        snapshot=json_copy(State.from_dict(state.snapshot()).snapshot())
        async with self._open(thread_id) as (graph,config):
            old=await graph.aget_state(config)
            if old.values:raise ValueError('workflow thread already exists')
            await graph.ainvoke({'command':command,'snapshot':snapshot,'answers':[],'report':{},
                'contract':self.contract,'turns':0},config)
            return self._result(thread_id,await graph.aget_state(config))

    async def resume(self,thread_id,answer):
        self._answer(answer)
        if not self.path.exists():raise ValueError('workflow thread not awaiting information')
        async with self._open(thread_id) as (graph,config):
            old=await graph.aget_state(config);self._check_contract(old)
            if not old.values or old.next!=('await_information',):
                raise ValueError('workflow thread not awaiting information')
            await graph.ainvoke(Command(resume=answer),config)
            return self._result(thread_id,await graph.aget_state(config))

    async def get_state(self,thread_id):
        if not self.path.exists():raise ValueError('workflow thread not found')
        async with self._open(thread_id) as (graph,config):
            state=await graph.aget_state(config);self._check_contract(state)
            if not state.values:raise ValueError('workflow thread not found')
            return self._result(thread_id,state)
