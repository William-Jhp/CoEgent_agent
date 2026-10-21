"""Fixed robot checks and effects, not an expression language."""

from copy import deepcopy
from functools import lru_cache
import json
import math
from time import monotonic

from jsonschema import Draft202012Validator

from .registry import SkillRegistry


PRECONDITIONS = {
    "authority": "Deployment authority allows this skill; stop is always available.",
    "robot_ready": "robot.estop is false and robot.localization_valid is true.",
    "battery_outbound": "robot.battery_percent meets minimum_outbound_battery.",
    "battery_return": "robot.battery_percent meets minimum_return_battery.",
    "path_clear": "world.paths.<target/home> is clear; check_path can update it only by observation.",
    "distance_limit": "abs(distance) does not exceed deployment max_distance.",
    "angle_limit": "abs(angle_deg) does not exceed deployment max_angle.",
    "obstacle_clear": "world.obstacle is explicitly false.",
    "dock_at_home": "robot.location equals the configured robot.home.",
    "arm_ready": "robot.arm_ready is explicitly true.",
    "holding_at_location": "robot.holding is nonempty and place location equals robot.location.",
    "fresh_inspection": "inspection target exists and matches this target and current robot.location.",
    "empty_gripper": "robot.holding is empty.",
}
EFFECTS = {"observation.result", "observation.battery", "observation.pose", "observation.path",
           "observation.target_state", "observation.localization", "internal.invalidate_observations",
           "physical.robot_pose", "physical.arm_state", "physical.held_object", "physical.valve_state",
           "physical.stopped", "physical.charging"}


def has_physical_effect(contract):
    """Unknown/empty effects mean physical effects are possible, never read-only."""
    effects = contract.get("effects") if isinstance(contract, dict) else None
    if not isinstance(effects, list) or not effects:
        return True
    return any(not isinstance(effect, str) or effect not in EFFECTS or effect.startswith("physical.")
               for effect in effects)


@lru_cache(maxsize=1)
def registry():
    value = SkillRegistry()
    for name in value.skills:
        item = value.definition(name)
        if set(item["preconditions"]) - PRECONDITIONS.keys() or set(item["effects"]) - EFFECTS:
            raise ValueError(f"unsupported fixed semantic identifier in {name}")
    return value


def check_preconditions(name, args, snapshot, config):
    if name not in registry().skills:
        return {"decision": "denied", "reason": "unknown_skill", "event": "SAFETY_ABORT"}
    robot, world = snapshot.get("robot", {}), snapshot.get("world", {})
    for rule in registry().definition(name)["preconditions"]:
        reason, event = None, "SAFETY_ABORT"
        if rule == "authority" and config.allowed_skills is not None and name not in config.allowed_skills:
            reason = "authority_denied"
        elif rule == "robot_ready":
            if robot.get("estop") is not False:
                reason = "estop_active_or_unknown"
            elif robot.get("localization_valid") is not True:
                reason = "localization_invalid_or_unknown"
        elif rule in {"battery_outbound", "battery_return"}:
            battery = robot.get("battery_percent")
            threshold = config.min_battery if rule == "battery_outbound" else config.min_return_battery
            if type(battery) not in (int, float) or not math.isfinite(battery) or not 0 <= battery <= 100:
                reason = "battery_unknown"
            elif battery < threshold:
                reason = "battery_low"
        elif rule == "path_clear":
            target = args.get("target") if name == "navigate_to" else robot.get("home")
            if not target:
                reason = "home_unknown"
            elif world.get("paths", {}).get(target) != "clear":
                reason, event = "path_not_clear", "REPLAN_REQUIRED"
        elif rule == "distance_limit" and abs(args["distance"]) > config.max_distance:
            reason = "distance_limit"
        elif rule == "angle_limit" and abs(args["angle_deg"]) > config.max_angle:
            reason = "angle_limit"
        elif rule == "obstacle_clear" and world.get("obstacle") is not False:
            reason = "obstacle_or_unknown"
        elif rule == "dock_at_home" and (not robot.get("home") or robot.get("location") != robot["home"]):
            reason = "dock_not_at_home"
        elif rule == "arm_ready" and robot.get("arm_ready") is not True:
            reason = "arm_not_ready"
        elif rule == "holding_at_location" and (not robot.get("holding") or args["location"] != robot.get("location")):
            reason = "place_precondition"
        elif rule == "fresh_inspection":
            target = args.get("target", args.get("object"))
            observations = snapshot.get("observations", {})
            observation = observations.get("inspection", {})
            matches = (observation.get("target") == target
                       and observation.get("location") == robot.get("location"))
            if config.state_mode == "strict":
                event = "REPLAN_REQUIRED"
                invalid = observations.get("_invalidation", {}).get("inspection", {}).get("reason")
                stamp = observations.get("_freshness", {}).get("inspection")
                if not observation:
                    reason = {"stale": "inspection_stale_or_unknown",
                              "location_mismatch": "target_not_verified",
                              "target_mismatch": "target_not_verified"}.get(invalid, "inspection_unknown")
                elif not matches:
                    reason = "target_not_verified"
                elif (type(stamp) not in (int, float) or not math.isfinite(stamp)
                      or not 0 <= monotonic() - stamp <= config.max_inspection_age_s):
                    reason = "inspection_stale_or_unknown"
                elif observation.get("target_exists") is False:
                    reason, event = "target_not_verified", "SAFETY_ABORT"
                elif observation.get("target_exists") is not True:
                    reason = "inspection_unknown"
            elif not matches or observation.get("target_exists") is not True:
                reason = "target_not_verified"
        elif rule == "empty_gripper" and robot.get("holding"):
            reason = "gripper_occupied"
        if reason:
            return {"decision": "denied", "reason": reason, "event": event}
    return None


def valid_success_data(name, args, snapshot, data):
    if name not in registry().skills:
        return False
    try:
        json.dumps(data, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return False
    if not Draft202012Validator(registry().definition(name)["result_schema"]).is_valid(data):
        return False
    if name in {"navigate_to", "return_home"}:
        return data["location"] == (args["target"] if name == "navigate_to" else snapshot.get("robot", {}).get("home"))
    if name in {"check_path", "inspect_target", "close_valve", "open_valve"}:
        return data["target"] == args["target"]
    if name == "pick_object":
        return data["holding"] == args["object"]
    return True


def apply_effects(snapshot, name, args, data, save_as=None):
    """Apply only validated successful data; never copy arbitrary robot fields."""
    result = deepcopy(snapshot)
    robot = result.setdefault("robot", {})
    world = result.setdefault("world", {})
    observations = result.setdefault("observations", {})
    for effect in registry().definition(name)["effects"]:
        if effect == "observation.result":
            observations[name] = deepcopy(data)
            if save_as:
                observations[save_as] = deepcopy(data)
        elif effect == "internal.invalidate_observations":
            observations.clear()
        elif effect == "observation.target_state":
            observations["inspection"] = deepcopy(data)
        elif effect == "observation.path":
            world.setdefault("paths", {})[args["target"]] = "clear" if data["path_clear"] else "blocked"
        elif effect == "physical.valve_state":
            world.setdefault("targets", {}).setdefault(args["target"], {})["valve_state"] = data["valve_state"]
            observations.pop("inspection", None)
        elif effect == "physical.stopped":
            robot["stopped"] = True
        elif effect in {"physical.robot_pose", "physical.arm_state"}:
            # These identify actuation capability; do not invent unreported state.
            continue
        else:
            field = {"observation.battery": "battery_percent", "observation.pose": "location",
                     "physical.held_object": "holding", "observation.localization": "localization_valid",
                     "physical.charging": "charging"}[effect]
            robot[field] = data[field]
    return result
