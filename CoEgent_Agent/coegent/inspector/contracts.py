"""Transport-neutral reports and deployment-owned inspection criteria."""
from collections import deque
from copy import deepcopy
from dataclasses import asdict, dataclass, field
import json
import math


def finite_json(value, limit=262144):
    try:
        encoded = json.dumps(value, allow_nan=False)
        if len(encoded.encode()) > limit:
            raise ValueError('payload_too_large')
    except (TypeError, OverflowError, RecursionError) as exc:
        raise ValueError('finite JSON required') from exc
    return value


def number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def json_equal(actual, expected):
    """JSON equality keeps bool separate from number at every nesting level."""
    if number(actual) and number(expected):
        return actual == expected
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(json_equal(a, b) for a, b in zip(actual, expected))
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(json_equal(actual[k], v) for k, v in expected.items())
    return actual == expected


def text(value, name, limit=12000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f'invalid {name}')


@dataclass(frozen=True)
class ReportMessage:
    mission_id: str
    revision: int
    report_id: str
    kind: str
    message: str
    scope: str = 'mission'
    severity: str = 'recoverable'

    def __post_init__(self):
        for key in ('mission_id', 'report_id', 'message'):
            text(getattr(self, key), key)
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError('invalid revision')
        if self.kind not in {'progress', 'completed', 'error'} or self.scope not in {'step', 'mission'}:
            raise ValueError('invalid report kind/scope')
        if self.severity not in {'recoverable', 'fatal', 'safety'}:
            raise ValueError('invalid severity')

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict):
            raise ValueError('report must be an object')
        finite_json(value, 65536)
        try:
            return cls(**value)
        except TypeError as exc:
            raise ValueError('invalid report fields') from exc

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class InspectionConfig:
    checks: tuple = ()
    visual_assertions: tuple = ()
    max_age_s: float = 5.0
    model_timeout_s: float = 15.0
    max_image_bytes: int = 2097152
    mailbox_capacity: int = 256
    model: dict | None = None

    def __post_init__(self):
        for key in ('max_age_s', 'model_timeout_s'):
            if not number(getattr(self, key)) or getattr(self, key) <= 0:
                raise ValueError(f'invalid {key}')
        for key in ('max_image_bytes', 'mailbox_capacity'):
            if type(getattr(self, key)) is not int or getattr(self, key) < 1:
                raise ValueError(f'invalid {key}')
        ids = set()
        for check in self.checks:
            if not isinstance(check, dict) or set(check) - {'id','source','path','op','expected','tolerance'}:
                raise ValueError('invalid check fields')
            for key in ('id','path'):
                text(check.get(key), key)
            if check['id'] in ids:
                raise ValueError('duplicate check id')
            ids.add(check['id'])
            if check.get('source') not in {'robot_state','vision'} or check.get('op') not in {'eq','near'} or 'expected' not in check:
                raise ValueError('invalid check')
            finite_json(check)
            if check['op'] == 'near':
                expected = check['expected']
                vector = isinstance(expected, list) and expected and all(number(x) for x in expected)
                if not (number(expected) or vector) or not number(check.get('tolerance')) or check['tolerance'] < 0:
                    raise ValueError('near requires numeric expected and nonnegative tolerance')
        for assertion in self.visual_assertions:
            if not isinstance(assertion, dict) or set(assertion) != {'id','description'}:
                raise ValueError('invalid visual assertion')
            text(assertion['id'], 'id'); text(assertion['description'], 'description')
            if assertion['id'] in ids:
                raise ValueError('duplicate assertion id')
            ids.add(assertion['id'])
        if self.model is not None:
            if not isinstance(self.model, dict) or set(self.model) != {'endpoint','name'}:
                raise ValueError('model requires endpoint and name')
            text(self.model['endpoint'], 'endpoint'); text(self.model['name'], 'name')
        object.__setattr__(self, 'checks', tuple(deepcopy(self.checks)))
        object.__setattr__(self, 'visual_assertions', tuple(deepcopy(self.visual_assertions)))

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict):
            raise ValueError('inspection config must be an object')
        finite_json(value)
        try:
            return cls(**value)
        except TypeError as exc:
            raise ValueError('invalid inspection config fields') from exc


@dataclass(frozen=True)
class InspectionSnapshot:
    mission_id: str
    revision: int
    values: dict
    observed_at: dict  # UTC seconds, source or full source.path -> capture time

    def __post_init__(self):
        text(self.mission_id, 'mission_id')
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError('invalid snapshot revision')
        if not isinstance(self.values, dict) or not isinstance(self.observed_at, dict):
            raise ValueError('snapshot values and observed_at must be objects')


@dataclass(frozen=True)
class InspectionDecision:
    decision: str
    reason: str
    checks: list = field(default_factory=list)
    analysis: str = ''
    feedback: dict = field(default_factory=dict)

    @property
    def verified(self):
        return self.decision == 'completed'

    def to_dict(self):
        return {**asdict(self), 'verified': self.verified}


class ReportInbox:
    """Bounded per-mission ingress. Seen IDs stay seen even after consumption."""
    def __init__(self, mission_id, revision, capacity=256):
        self.mission_id, self.revision, self.capacity = mission_id, revision, capacity
        self._seen, self._pending, self._active = set(), deque(), True

    def submit(self, value):
        msg = value if isinstance(value, ReportMessage) else ReportMessage.from_dict(value)
        reason = None
        if not self._active:
            reason = 'mission_not_active'
        elif (msg.mission_id, msg.revision) != (self.mission_id, self.revision):
            reason = 'foreign_report'
        elif msg.report_id in self._seen:
            reason = 'duplicate_report'
        elif len(self._seen) >= self.capacity:
            reason = 'report_capacity_exhausted'
        if reason:
            return {'accepted':False, 'reason':reason}
        self._seen.add(msg.report_id); self._pending.append(msg)
        return {'accepted':True, 'reason':'accepted'}

    def drain(self):
        result = list(self._pending); self._pending.clear()
        return result

    def close(self):
        self._active = False
        self._pending.clear()
