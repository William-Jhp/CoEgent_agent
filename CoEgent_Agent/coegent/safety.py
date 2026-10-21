"""Deployment configuration wrapper around shared skill preconditions."""

from dataclasses import dataclass

from coegent_contracts.config import RuntimeConfig
from coegent_contracts.semantics import check_preconditions

from .types import Event, SkillResult, State


@dataclass(frozen=True)
class SafetyPolicy(RuntimeConfig):
    def availability(self, registry, state):
        available, blocked = [], {}
        for name, arguments in registry.skills.items():
            args = {key: 0 if kind == "number" else "__unbound__" for key, kind in arguments.items()}
            result = self.check(name, args, state)
            if result is None or result.reason in {"path_not_clear", "target_not_verified", "place_precondition"}:
                available.append(name)
            else:
                blocked[name] = result.reason
        return {"currently_allowed_skills": available, "blocked_skills": blocked,
                "note": "Argument-dependent checks still apply; successful sensing can change availability."}

    def check(self, name: str, args: dict, state: State) -> SkillResult | None:
        issue = check_preconditions(name, args, state.snapshot(), self)
        return SkillResult(name, Event(issue["event"]), issue["reason"]) if issue else None
