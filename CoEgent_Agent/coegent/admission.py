"""One robot owner, bounded request history and a persistent-in-process fault."""

from copy import deepcopy
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import threading
from time import monotonic, time
import weakref

from .goals import Goal
from .types import RuntimeHalt


@dataclass(frozen=True)
class MissionRequest:
    mission_id: str
    revision: int
    expires_at: float
    command: str
    plan: dict | str | None = None
    goals: tuple[Goal, ...] = ()

    def __post_init__(self):
        if not isinstance(self.mission_id, str) or not self.mission_id.strip() or len(self.mission_id) > 256:
            raise ValueError("invalid mission_id")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("revision must be a positive integer")
        if type(self.expires_at) not in (int, float) or not math.isfinite(self.expires_at):
            raise ValueError("expires_at must be finite UTC seconds")
        if not isinstance(self.command, str) or not self.command.strip():
            raise ValueError("command must be nonempty")
        if not isinstance(self.goals, tuple) or not all(isinstance(g, Goal) for g in self.goals):
            raise ValueError("goals must be a tuple of approved Goals")
        if self.plan is not None and not isinstance(self.plan, (str, dict)):
            raise ValueError("plan must be JSON text or an object")
        json.dumps(asdict(self), allow_nan=False)


_BACKENDS = weakref.WeakKeyDictionary()
_ROBOTS = weakref.WeakValueDictionary()
_REGISTRY_LOCK = threading.Lock()


def _after_fork():
    global _REGISTRY_LOCK
    _REGISTRY_LOCK = threading.Lock()


os.register_at_fork(after_in_child=_after_fork)


class RobotAdmission:
    def __init__(self, *, utc_now=time, monotonic_now=monotonic, capacity=256):
        if type(capacity) is not int or capacity < 1:
            raise ValueError("invalid request cache capacity")
        self.utc_now, self.monotonic_now, self.capacity = utc_now, monotonic_now, capacity
        self.robot_id = None
        self._lock = threading.RLock()
        self._file = None
        self._closed = False
        self._records = {}
        self._active = None
        self._management = False
        self._fault = None
        self._pending = set()
        self.safety_initialized = False
        self.owner_pid = os.getpid()
        self._utc_high_water = float('-inf')

    def assert_owner_process(self):
        if os.getpid() != self.owner_pid:
            raise RuntimeError("robot_process_owned_other_pid")

    def _now(self):
        self._utc_high_water = max(self._utc_high_water, self.utc_now())
        return self._utc_high_water

    @classmethod
    def bind(cls, backend, guard):
        if isinstance(guard, cls):
            guard.assert_owner_process()
        if not isinstance(guard, cls) or guard._closed:
            raise ValueError("invalid robot admission")
        with _REGISTRY_LOCK:
            current = _BACKENDS.get(backend)
            if current is not None and current is not guard:
                raise ValueError("backend already has a different execution owner")
            _BACKENDS[backend] = guard
            return guard

    @classmethod
    def for_backend(cls, backend, *, robot_id=None, lock_dir=None, capacity=256):
        with _REGISTRY_LOCK:
            existing = _BACKENDS.get(backend)
            if existing is not None:
                existing.assert_owner_process()
            if existing is not None and existing._closed:
                raise ValueError("backend execution owner was closed; cannot silently rebind")
            if existing is not None and not existing._closed:
                if robot_id is not None and existing.robot_id != robot_id:
                    raise ValueError("backend already bound to another robot identity")
                return existing
            if robot_id is not None:
                if not isinstance(robot_id, str) or not robot_id.strip() or lock_dir is None:
                    raise ValueError("robot_id requires a local lock_dir")
                existing = _ROBOTS.get(robot_id)
                if existing is not None and not existing._closed:
                    existing.assert_owner_process()
                    _BACKENDS[backend] = existing
                    return existing
            guard = cls(capacity=capacity)
            if robot_id is not None:
                root = Path(lock_dir).resolve()
                root.mkdir(parents=True, exist_ok=True)
                file = open(root / (hashlib.sha256(robot_id.encode()).hexdigest() + ".lock"), "a+")
                try:
                    fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    file.close()
                    raise RuntimeError("robot_process_owned") from None
                guard._file, guard.robot_id = file, robot_id
                guard.latch_fault("startup_safety_unconfirmed")
                _ROBOTS[robot_id] = guard
            _BACKENDS[backend] = guard
            return guard

    @property
    def fault_reason(self):
        self.assert_owner_process()
        with self._lock:
            return self._fault

    @property
    def pending(self):
        self.assert_owner_process()
        with self._lock:
            return frozenset(t for t in self._pending if not t.done())

    def owns(self, request):
        self.assert_owner_process()
        with self._lock:
            return self._active == self.key(request)

    def latch_fault(self, reason):
        self.assert_owner_process()
        with self._lock:
            self._fault = self._fault or reason

    def begin_management(self):
        self.assert_owner_process()
        with self._lock:
            if self._closed or self._active or self._management or self.pending:
                return False
            self._management = True
            return True

    def end_management(self, *, safe=False):
        self.assert_owner_process()
        with self._lock:
            if safe and self._management and not self.pending:
                self._fault = None
            self._management = False

    def track(self, task):
        self.assert_owner_process()
        with self._lock:
            self._pending.add(task)
        def done(value):
            with self._lock:
                self._pending.discard(value)
            if not value.cancelled():
                value.exception()
        task.add_done_callback(done)

    @staticmethod
    def key(request):
        return (request.mission_id, request.revision)

    def admit(self, request):
        self.assert_owner_process()
        digest = hashlib.sha256(json.dumps(asdict(request), sort_keys=True,
                    allow_nan=False, ensure_ascii=False).encode()).hexdigest()
        key = self.key(request)
        with self._lock:
            def denied(reason):
                return {"kind": "rejected", "reason": reason}
            now, mono = self._now(), self.monotonic_now()
            if self._closed:
                return denied("admission_closed")
            if request.expires_at <= now:
                return denied("request_expired")
            if any(k[0] == key[0] and k[1] > key[1] for k in self._records):
                return denied("stale_revision")
            previous = self._records.get(key)
            if previous:
                if digest != previous["digest"]:
                    return denied("request_payload_conflict")
                if mono >= previous["deadline"]:
                    return denied("request_expired")
                return {"kind": "replayed", "report": deepcopy(previous["report"])}
            if self._fault:
                return denied("robot_fault_latched")
            if self._active or self._management:
                return denied("robot_busy")
            if self.pending:
                return denied("robot_fault_latched")
            prior = [v for k, v in self._records.items() if k[0] == key[0]]
            if request.revision > 1 and not prior:
                return denied("revision_history_unavailable")
            resume = deepcopy(prior[-1]["report"]) if prior else None
            self._records = {k: v for k, v in self._records.items()
                             if v["expires_at"] > now}
            if len(self._records) >= self.capacity:
                return denied("request_cache_full")
            self._records[key] = {"digest": digest, "deadline": mono + request.expires_at - now,
                                  "expires_at": request.expires_at, "report": {"execution_status": "RUNNING"},
                                  "history": deepcopy(resume.get("mission_completed_steps", [])) if resume else []}
            self._active = key
            return {"kind": "accepted", "previous_report": resume}

    def check_active(self, request):
        self.assert_owner_process()
        with self._lock:
            record = self._records.get(self.key(request))
            if self._active != self.key(request) or record is None:
                raise RuntimeHalt("HELD", "request_no_longer_active")
            if self._fault:
                raise RuntimeHalt("SAFETY_ABORT", "robot_fault_latched")
            if self._now() >= request.expires_at or self.monotonic_now() >= record["deadline"]:
                raise RuntimeHalt("HELD", "request_expired")

    def remaining_s(self, request):
        self.assert_owner_process()
        with self._lock:
            record = self._records[self.key(request)]
            return max(0, min(request.expires_at - self._now(),
                              record["deadline"] - self.monotonic_now()))

    def finish(self, request, report):
        self.assert_owner_process()
        with self._lock:
            key = self.key(request)
            if self._active != key:
                raise RuntimeError("cannot release another mission owner")
            history = self._records[key]["history"]
            saved = deepcopy(report)
            saved["mission_completed_steps"] = deepcopy(history + report.get("completed_steps", []))
            self._records[key]["report"] = saved
            self._active = None

    def close(self):
        self.assert_owner_process()
        with self._lock:
            if self._active or self._management or self.pending:
                raise RuntimeError("robot still executing")
            self._closed = True
            if self._file:
                self._file.close()
                self._file = None


def offline_action(config, *, connected, needs_remote=False, skill=None):
    if not config.remote_required or connected:
        return "continue"
    if config.offline_policy == "continue":
        return ("continue" if not needs_remote and (skill is None or skill in (config.local_skills or ()))
                else "hold")
    return config.offline_policy
