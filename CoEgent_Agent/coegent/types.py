"""State, result, and event contracts shared by runtime components."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from time import monotonic
from typing import Any

from coegent_contracts.config import RuntimeConfig
from coegent_contracts.state import StateContract


class Event(StrEnum):
    SUCCESS = "SUCCESS"
    EXPECTED_FAILURE = "EXPECTED_FAILURE"
    REPLAN_REQUIRED = "REPLAN_REQUIRED"
    SAFETY_ABORT = "SAFETY_ABORT"


@dataclass(frozen=True)
class SkillResult:
    skill: str
    event: Event = Event.SUCCESS
    reason: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    recoverable: bool = False

    @property
    def ok(self) -> bool:
        return self.event == Event.SUCCESS

    def to_dict(self) -> dict:
        return {**asdict(self), "status": "success" if self.ok else "failed"}


@dataclass
class State:
    robot: dict[str, Any] = field(default_factory=dict)
    world: dict[str, Any] = field(default_factory=dict)
    mission: dict[str, Any] = field(default_factory=dict)
    observations: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict) -> State:
        StateContract.validate(value)
        return cls(**deepcopy(value))

    def snapshot(self) -> dict:
        return deepcopy(asdict(self))

    def resolve(self, source: str) -> Any:
        return StateContract.resolve(self.snapshot(), source)


@dataclass(frozen=True)
class Limits(RuntimeConfig):
    """Compatibility name for shared execution configuration."""


class Trace:
    def __init__(self):
        self.started = monotonic()
        self.entries: list[dict] = []

    def emit(self, kind: str, **fields):
        self.entries.append({"index": len(self.entries), "elapsed_s": monotonic() - self.started,
                             "kind": kind, **deepcopy(fields)})


class RuntimeHalt(Exception):
    def __init__(self, status: str, reason: str):
        self.status = status
        self.reason = reason
        super().__init__(reason)


async def bounded_call(coroutine, timeout_s):
    """Bound reads/planning without waiting indefinitely for cancellation acknowledgement."""
    task = asyncio.create_task(coroutine)
    task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout_s)
        if not done:
            raise TimeoutError("adapter_timeout")
        return task.result()
    finally:
        if not task.done():
            task.cancel()
