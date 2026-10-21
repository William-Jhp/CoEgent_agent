"""Deployment-owned values, never model-authored policy."""

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class RuntimeConfig:
    min_battery: float = 15
    min_return_battery: float = 3
    max_distance: float = 5
    max_angle: float = 180
    allowed_skills: frozenset[str] | None = None
    max_steps: int = 128
    max_replans: int = 1
    max_repairs: int = 1
    skill_timeout_s: float = 5.0
    planner_timeout_s: float = 90.0
    mission_timeout_s: float = 180.0
    state_mode: str = "snapshot"
    max_observation_age_s: float | None = None
    max_inspection_age_s: float | None = None
    remote_required: bool = False
    offline_policy: str = "hold"
    local_skills: frozenset[str] | None = None
    connection_poll_s: float = 0.1
    request_cache_capacity: int = 256

    def __post_init__(self):
        numbers = (self.min_battery, self.min_return_battery, self.max_distance, self.max_angle,
                   self.skill_timeout_s, self.planner_timeout_s, self.mission_timeout_s)
        if any(type(x) not in (int, float) or not math.isfinite(x) for x in numbers):
            raise ValueError("runtime limits must be finite numbers")
        if not 0 < self.min_return_battery <= self.min_battery <= 100:
            raise ValueError("invalid battery limits")
        if min(numbers[2:]) <= 0:
            raise ValueError("distance, angle and timeouts must be positive")
        if (any(type(x) is not int for x in (self.max_steps, self.max_replans, self.max_repairs))
                or self.max_steps < 1 or min(self.max_replans, self.max_repairs) < 0):
            raise ValueError("invalid step/replan/repair limits")
        if self.allowed_skills is not None and (not isinstance(self.allowed_skills, frozenset) or not all(
                isinstance(x, str) and x for x in self.allowed_skills)):
            raise ValueError("allowed_skills must be a frozenset of names or None")
        if self.state_mode not in {"snapshot", "strict"} or self.offline_policy not in {"hold", "continue", "return_home"}:
            raise ValueError("invalid observation mode or offline policy")
        if type(self.remote_required) is not bool:
            raise ValueError("remote_required must be boolean")
        for age in (self.max_observation_age_s, self.max_inspection_age_s, self.connection_poll_s):
            if age is not None and (type(age) not in (int, float) or not math.isfinite(age) or age <= 0):
                raise ValueError("freshness/poll limits must be positive finite numbers")
        if self.connection_poll_s is None or (self.state_mode == "strict" and (
                self.max_observation_age_s is None or self.max_inspection_age_s is None)):
            raise ValueError("strict mode requires explicit observation age limits")
        if type(self.request_cache_capacity) is not int or self.request_cache_capacity < 1:
            raise ValueError("request cache capacity must be a positive integer")
        if self.local_skills is not None and (not isinstance(self.local_skills, frozenset)
                or not all(isinstance(s, str) and s for s in self.local_skills)):
            raise ValueError("local_skills must be a frozenset of names")
        if self.remote_required and self.offline_policy == "continue" and not self.local_skills:
            raise ValueError("offline continue requires an explicit local skill allowlist")
