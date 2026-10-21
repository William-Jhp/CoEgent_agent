"""Trusted observation snapshots and field-level freshness, not shared mutable state."""

from copy import deepcopy
from dataclasses import dataclass
import math
from time import monotonic
from typing import Protocol

from coegent_contracts.registry import SkillRegistry
from coegent_contracts.semantics import valid_success_data
from coegent_contracts.state import StateContract

from .types import RuntimeHalt, bounded_call


@dataclass(frozen=True)
class ObservationSnapshot:
    values: dict
    observed_at: dict[str, float]


class TrustedStateProvider(Protocol):
    async def snapshot(self) -> ObservationSnapshot: ...


def required_paths(name, args, state):
    """Read existing semantic identifiers; no second rule engine."""
    rules = SkillRegistry().definition(name)["preconditions"]
    fixed = {"robot_ready": ("robot.estop", "robot.localization_valid"),
             "battery_outbound": ("robot.battery_percent",), "battery_return": ("robot.battery_percent",),
             "obstacle_clear": ("world.obstacle",), "arm_ready": ("robot.arm_ready",),
             "dock_at_home": ("robot.location", "robot.home"),
             "holding_at_location": ("robot.holding", "robot.location"), "empty_gripper": ("robot.holding",),
             "fresh_inspection": ("robot.location",)}
    paths = [path for rule in rules for path in fixed.get(rule, ())]
    if "path_clear" in rules:
        target = args.get("target") if name == "navigate_to" else state.robot.get("home")
        if name != "navigate_to":
            paths.append("robot.home")
        if target:
            paths.append(f"world.paths.{target}")
    return tuple(dict.fromkeys(paths))


def leaves(value, prefix=""):
    result = {}
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict):
            result.update(leaves(item, path))
        else:
            result[path] = item
    return result


def put(snapshot, path, value):
    parts = path.split(".")
    node = snapshot
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = deepcopy(value)


class FreshState:
    MAX_INSPECTION_CANDIDATES = 32

    def __init__(self, provider, config, *, clock=monotonic):
        if config.state_mode == "strict" and provider is None:
            raise ValueError("strict state mode requires a trusted state provider")
        self.provider, self.config, self.clock = provider, config, clock
        self.inspection_candidates = {}
        self.inspection_stamp = None
        self.inspection_not_before = None
        self.last_location = None
        self.inspection_invalidation_reason = None

    def reset_mission(self):
        self.inspection_candidates.clear()
        self.inspection_stamp = None
        self.result_stamps = {}
        self.inspection_invalidation_reason = None
        # Keep the motion cutoff: a new mission must not revive pre-motion data.

    def invalidate_inspections(self):
        self.inspection_candidates.clear()
        self.inspection_stamp = None
        self.inspection_not_before = self.clock()
        self.inspection_invalidation_reason = "motion_invalidated"

    async def refresh(self, state, paths=(), phase="dispatch"):
        if self.config.state_mode != "strict":
            return
        try:
            observed = await bounded_call(self.provider.snapshot(), self.config.skill_timeout_s)
            if not isinstance(observed, ObservationSnapshot) or not isinstance(observed.observed_at, dict):
                raise ValueError("invalid observation envelope")
            values = deepcopy(observed.values)
            if set(values) - {"robot", "world", "observations"}:
                raise ValueError("provider cannot overwrite mission/authority")
            if set(values.get("robot", {})) - StateContract.schema["properties"]["robot"]["properties"].keys():
                raise ValueError("unknown robot observation field")
            if set(values.get("world", {})) - StateContract.schema["properties"]["world"]["properties"].keys():
                raise ValueError("unknown world observation field")
            if set(values.get("observations", {})) - {"inspection"}:
                raise ValueError("provider cannot supply planner results/provenance")
            StateContract.validate({**values, "mission": {}})
            current = state.snapshot()
            current["robot"], current["world"] = {}, {}
            fresh = current["observations"].setdefault("_freshness", {})
            # Remove caller-supplied provenance; only the live adapter's timestamps apply.
            updated = leaves({k: v for k, v in values.items() if k != "observations"})
            fresh.clear()
            for path, value in updated.items():
                stamp = observed.observed_at.get(path)
                if self.valid_stamp(stamp, self.config.max_observation_age_s):
                    put(current, path, value)
                    fresh[path] = stamp
                else:
                    put(current, path, None)
            for path in paths:
                if path.startswith(("robot.", "world.")) and path not in fresh:
                    put(current, path, None)
                    raise ValueError(f"missing/stale observation:{path}")
                if not path.startswith(("robot.", "world.", "mission.")):
                    root = path.split(".")[1] if path.startswith("results.") else path.split(".")[0]
                    stamp = (getattr(self, "inspection_stamp", None) if root == "inspection"
                             else getattr(self, "result_stamps", {}).get(root))
                    limit = (self.config.max_inspection_age_s if root == "inspection"
                             else self.config.max_observation_age_s)
                    # The adapter's inspection is handled below, with its own timestamp.
                    if root != "inspection" and not self.valid_stamp(stamp, limit):
                        raise RuntimeHalt("REPLAN_REQUIRED", "condition_observation_stale_or_unknown")
            location = current["robot"].get("location")
            if self.last_location is not None and location != self.last_location:
                self.invalidate_inspections()
            self.last_location = location
            self.refresh_inspection(current, values.get("observations", {}).get("inspection"),
                                    observed.observed_at.get("inspection"))
            state.robot, state.world = current["robot"], current["world"]
            state.observations = current["observations"]
        except RuntimeHalt:
            raise
        except Exception as exc:
            raise RuntimeHalt("SAFETY_ABORT", f"observation_unavailable:{phase}:{exc}") from exc

    def refresh_inspection(self, current, provided, provider_stamp):
        observations = current["observations"]
        inspection = observations.get("inspection")
        for candidate in (inspection, provided):
            if candidate is not None and (not isinstance(candidate, dict) or not valid_success_data(
                    "inspect_target", {"target": candidate.get("target")}, current, candidate)):
                raise ValueError("invalid trusted inspection")
        key = (inspection["target"], inspection["location"]) if inspection is not None else None
        if key is not None and key not in self.inspection_candidates:
            # Caller state is not a trusted source, even if it includes _freshness.
            self.remember_inspection(inspection, None)
        if provided is not None:
            self.remember_inspection(provided, provider_stamp)
            if key is None or key[1] != current["robot"].get("location"):
                key = (provided["target"], provided["location"])
        self.project_inspection(current, key)

    def prune_inspections(self):
        for entry in self.inspection_candidates.values():
            if entry["data"] is not None and not self.valid_stamp(entry["stamp"], self.config.max_inspection_age_s):
                entry.update(data=None, stamp=None, reason="stale")

    def remember_inspection(self, data, stamp):
        self.prune_inspections()
        key = (data["target"], data["location"])
        reason = None
        if not self.valid_stamp(stamp, math.inf):
            reason = "invalid_timestamp"
        elif self.inspection_not_before is not None and stamp <= self.inspection_not_before:
            reason = "motion_invalidated"
        elif not self.valid_stamp(stamp, self.config.max_inspection_age_s):
            reason = "stale"
        previous = self.inspection_candidates.get(key)
        if previous is not None and previous["stamp"] is not None:
            if reason or previous["stamp"] > stamp or (previous["stamp"] == stamp and (
                    previous["data"] is None or previous["data"]["target_exists"] is False)):
                return
        if key not in self.inspection_candidates and len(self.inspection_candidates) >= self.MAX_INSPECTION_CANDIDATES:
            expired = [k for k, v in self.inspection_candidates.items()
                       if not self.valid_stamp(v["stamp"], self.config.max_inspection_age_s)]
            for expired_key in expired:
                del self.inspection_candidates[expired_key]
            if len(self.inspection_candidates) >= self.MAX_INSPECTION_CANDIDATES:
                raise RuntimeHalt("REPLAN_REQUIRED", "inspection_cache_capacity")
        self.inspection_candidates[key] = {"data": deepcopy(data) if reason is None else None,
                                           "stamp": stamp if reason is None else None, "reason": reason}

    def project_inspection(self, current, key):
        self.prune_inspections()
        observations = current["observations"]
        entry = self.inspection_candidates.get(key)
        reason = entry["reason"] if entry else self.inspection_invalidation_reason or "missing"
        if key is not None and key[1] != current["robot"].get("location"):
            reason = "location_mismatch"
        invalidation = observations.setdefault("_invalidation", {})
        if reason:
            if key is not None or "inspection" not in invalidation:
                invalidation["inspection"] = {"reason": reason}
                if key is not None:
                    invalidation["inspection"].update(target=key[0], location=key[1])
            observations.pop("inspection", None)
            observations["_freshness"].pop("inspection", None)
            self.inspection_stamp = None
        else:
            observations["inspection"] = deepcopy(entry["data"])
            observations.setdefault("_freshness", {})["inspection"] = entry["stamp"]
            self.inspection_stamp = entry["stamp"]
            invalidation.pop("inspection", None)

    def project_for_skill(self, current, name, args):
        if self.config.state_mode != "strict" or name not in SkillRegistry().skills:
            return
        if "fresh_inspection" not in SkillRegistry().definition(name)["preconditions"]:
            return
        key = (args.get("target", args.get("object")), current["robot"].get("location"))
        self.project_inspection(current, key)
        if key not in self.inspection_candidates and self.inspection_candidates:
            current["observations"]["_invalidation"]["inspection"]["reason"] = "target_mismatch"

    def valid_stamp(self, stamp, limit):
        return (type(stamp) in (int, float) and math.isfinite(stamp)
                and limit is not None and 0 <= self.clock() - stamp <= limit)

    def check_current(self, state, paths):
        if self.config.state_mode != "strict":
            return
        fresh = state.observations.get("_freshness", {})
        for path in paths:
            if path.startswith(("robot.", "world.")):
                if not self.valid_stamp(fresh.get(path), self.config.max_observation_age_s):
                    raise RuntimeHalt("SAFETY_ABORT", f"observation_stale_before_dispatch:{path}")
            elif not path.startswith("mission."):
                root = path.split(".")[1] if path.startswith("results.") else path.split(".")[0]
                stamp = (getattr(self, "inspection_stamp", None) if root == "inspection"
                         else getattr(self, "result_stamps", {}).get(root))
                limit = (self.config.max_inspection_age_s if root == "inspection"
                         else self.config.max_observation_age_s)
                if not self.valid_stamp(stamp, limit):
                    raise RuntimeHalt("REPLAN_REQUIRED", "condition_observation_stale_or_unknown")

    def record(self, state, name, started_at, save_as=None):
        effects = SkillRegistry().definition(name)["effects"]
        if self.config.state_mode == "strict":
            location = state.robot.get("location")
            if "internal.invalidate_observations" in effects or (
                    self.last_location is not None and location != self.last_location):
                self.invalidate_inspections()
            self.last_location = location
            if "physical.valve_state" in effects:
                inspection = state.observations.get(name, {})
                key = (inspection.get("target"), location)
                self.inspection_candidates[key] = {"data": None, "stamp": self.clock(), "reason": "effect_invalidated"}
        if "observation.result" in SkillRegistry().definition(name)["effects"]:
            if not hasattr(self, "result_stamps"):
                self.result_stamps = {}
            self.result_stamps[name] = started_at
            if save_as:
                self.result_stamps[save_as] = started_at
        if name == "inspect_target":
            if self.config.state_mode == "strict":
                self.remember_inspection(state.observations["inspection"], started_at)
            self.inspection_stamp = started_at
            state.observations.setdefault("_freshness", {})["inspection"] = started_at
            state.observations.get("_invalidation", {}).pop("inspection", None)
