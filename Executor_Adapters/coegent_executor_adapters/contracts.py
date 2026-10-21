"""Transport-independent executor contracts; importing them loads no runtime."""
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ExecutorContext:
    name: str
    router: Any
    state: Any
    trace: Any
    limits: Any
    request: Any
    report: Any = None  # optional synchronous ReportMessage/dict -> admission acknowledgement


@dataclass(frozen=True)
class PreparedPlan:
    executor: str
    payload: Any


@dataclass(frozen=True)
class ExecutionProgress:
    steps: int = 0
    completed: list = field(default_factory=list)
    failed: dict | None = None
    remaining: list = field(default_factory=list)
    stop_confirmation: dict | None = None


class ExecutorAdapter(Protocol):
    """A trusted plugin must enforce per-step gates, not just serialize JSON.

    progress is cached, local and nonblocking. execute/cancel return CoEgent
    SkillResult/Event values. close releases adapter-owned resources only;
    cancel must remain callable for finalization safety even after close.
    """
    @property
    def progress(self) -> ExecutionProgress: ...
    async def prepare(self, plan: dict) -> PreparedPlan: ...
    async def execute(self, prepared: PreparedPlan) -> Any: ...
    async def status(self) -> ExecutionProgress: ...
    async def cancel(self) -> Any: ...
    async def close(self) -> None: ...
