"""Local persistent plan catalog, independent of planner queue and execution."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import threading

from .registry import SkillRegistry
from .validation import PlanValidator, strict_json
from .plan_features import extract_features, normalized, plan_terms

APPLICATION_ID = 0x434F4550
SCHEMA_VERSION = 2


def default_plan_db():
    return Path(os.environ.get('COEGENT_PLAN_DB', Path(__file__).resolve().parents[1]/'data/plans.sqlite3'))


def _numbers(value):
    if isinstance(value, dict):
        return {key: _numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_numbers(item) for item in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


class PlanStore:
    """One connection per operation; SQLite UNIQUE enforces cross-process identity.

    Constructing the handle has no filesystem side effects. Explicit initialize
    or a catalog operation opens/creates the database. No runtime state grants
    are stored in this catalog, and every read validates the current plan schema.
    """
    def __init__(self, path=None, *, timeout_s=0.5, registry=None):
        if path is not None and str(path) == ':memory:':
            raise ValueError('PlanStore requires a file path')
        if type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError('timeout_s must be positive and finite')
        self.path = Path(path if path is not None else default_plan_db()).expanduser().resolve()
        self.timeout_s = timeout_s
        self.registry = registry or SkillRegistry()
        self.validator = getattr(self.registry, "validator_factory", PlanValidator)(self.registry)
        self._initialized = False
        self._init_lock = threading.Lock()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=self.timeout_s)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        try:
            self._validate_profile(connection)
        except Exception:
            connection.close()
            raise
        return connection

    def _validate_profile(self, connection):
        tables={r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        profile=hasattr(self.registry,'tools')
        if 'api_catalog_identity' in tables:
            values=[r[0] for r in connection.execute('SELECT contract_hash FROM api_catalog_identity')]
            if not profile or values != [self.registry.fingerprint]:
                raise ValueError('catalog tool profile contract mismatch; use a separate database')
        elif profile and 'plans' in tables:
            values={r[0] for r in connection.execute('SELECT DISTINCT contract_hash FROM plans')}
            if values - {self.registry.fingerprint}:
                raise ValueError('catalog contains another contract/profile; use a separate database')

    @staticmethod
    def _validate_database(connection):
        application = connection.execute('PRAGMA application_id').fetchone()[0]
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        if application not in (0, APPLICATION_ID) or version not in (0, 1, SCHEMA_VERSION):
            raise ValueError('unsupported plan database format/version')
        if application == 0 and tables:
            raise ValueError('refusing to use an unrelated SQLite database')

    def initialize(self):
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as connection:
                # Reject unrelated DBs before journal configuration, and use a
                # consistent transaction snapshot for both ownership checks.
                with connection:
                    connection.execute('BEGIN IMMEDIATE')
                    self._validate_database(connection)
                    self._validate_profile(connection)
                connection.execute('PRAGMA journal_mode=WAL')
                with connection:
                    connection.execute('BEGIN IMMEDIATE')
                    self._validate_database(connection)
                    self._validate_profile(connection)
                    previous_version = connection.execute('PRAGMA user_version').fetchone()[0]
                    connection.execute('''CREATE TABLE IF NOT EXISTS plans (
                        id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE,
                        plan_json TEXT NOT NULL, title TEXT NOT NULL, author TEXT NOT NULL,
                        source TEXT NOT NULL, command TEXT NOT NULL, contract_hash TEXT NOT NULL,
                        created_at TEXT NOT NULL, success_count INTEGER NOT NULL DEFAULT 0,
                        last_success_at TEXT)''')
                    connection.execute('''CREATE TABLE IF NOT EXISTS successful_executions (
                        id INTEGER PRIMARY KEY, plan_id INTEGER NOT NULL REFERENCES plans(id),
                        mission_id TEXT NOT NULL, revision INTEGER NOT NULL, executor TEXT NOT NULL,
                        backend TEXT NOT NULL, state_mode TEXT NOT NULL, goal_status TEXT NOT NULL,
                        created_at TEXT NOT NULL, UNIQUE(mission_id, revision))''')
                    connection.execute('''CREATE TABLE IF NOT EXISTS plan_commands (
                        plan_id INTEGER NOT NULL REFERENCES plans(id), command TEXT NOT NULL,
                        normalized_command TEXT NOT NULL, PRIMARY KEY(plan_id,normalized_command))''')
                    connection.execute('''CREATE TABLE IF NOT EXISTS plan_search_terms (
                        plan_id INTEGER NOT NULL REFERENCES plans(id), term TEXT NOT NULL,
                        PRIMARY KEY(plan_id,term))''')
                    connection.execute('CREATE INDEX IF NOT EXISTS plan_search_term_idx ON plan_search_terms(term)')
                    if previous_version < 2:
                        for row in connection.execute('SELECT id,plan_json,title,command FROM plans').fetchall():
                            self._index(connection, row['id'], row['plan_json'], row['title'], row['command'])
                    if hasattr(self.registry,'tools'):
                        connection.execute('CREATE TABLE IF NOT EXISTS api_catalog_identity (contract_hash TEXT PRIMARY KEY)')
                        connection.execute('INSERT OR IGNORE INTO api_catalog_identity VALUES (?)',(self.registry.fingerprint,))
                    connection.execute(f'PRAGMA application_id={APPLICATION_ID}')
                    connection.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
            self._initialized = True

    def _canonical(self, value):
        plan, _ = self.validator.prepare(value)
        text = json.dumps(_numbers(plan), sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
        return text, hashlib.sha256(text.encode('utf-8')).hexdigest()

    @staticmethod
    def _metadata(**values):
        if any(not isinstance(value, str) or len(value) > 100_000 for value in values.values()):
            raise ValueError('plan metadata must contain bounded strings')

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    def _insert(self, connection, text, digest, *, title, author, source, command):
        cursor = connection.execute('''INSERT INTO plans
            (fingerprint,plan_json,title,author,source,command,contract_hash,created_at)
            VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(fingerprint) DO NOTHING''',
            (digest, text, title, author, source, command, self.registry.fingerprint, self._now()))
        row = connection.execute('SELECT id,plan_json,title,command FROM plans WHERE fingerprint=?', (digest,)).fetchone()
        if row['plan_json'] != text:
            raise ValueError('plan hash collision or catalog corruption')
        self._index(connection, row['id'], text, row['title'], command or row['command'])
        return {'id': row['id'], 'created': cursor.rowcount == 1, 'fingerprint': digest}

    @staticmethod
    def _index(connection, plan_id, text, title, command):
        if command.strip():
            connection.execute('INSERT OR IGNORE INTO plan_commands VALUES (?,?,?)',
                               (plan_id, command, normalized(command)))
        features = plan_terms(strict_json(text), title, command)
        connection.executemany('INSERT OR IGNORE INTO plan_search_terms VALUES (?,?)',
                               ((plan_id, term) for term in sorted(features)))

    def commands(self, plan_id):
        self.get(plan_id)
        with closing(self._connect()) as connection:
            return [row['command'] for row in connection.execute(
                'SELECT command FROM plan_commands WHERE plan_id=? ORDER BY normalized_command', (plan_id,))]

    def search(self, command, *, state=None, limit=5):
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError('candidate limit must be 1..20')
        features = extract_features(command, state)
        wanted = sorted(set(features['targets'] + features['arguments'] + features['keywords']))
        self.initialize()
        if not wanted:
            return {'features': features, 'candidates': []}
        important = features['targets'] + features['arguments'] or ['']
        placeholders = ','.join('?' for _ in wanted)
        boosts = ','.join('?' for _ in important)
        with closing(self._connect()) as connection:
            rows = connection.execute(f'''SELECT t.plan_id,
                SUM(CASE WHEN t.term IN ({boosts}) THEN 8 ELSE 1 END) AS score
                FROM plan_search_terms t JOIN plans p ON p.id=t.plan_id
                WHERE t.term IN ({placeholders}) GROUP BY t.plan_id
                ORDER BY score DESC,p.success_count DESC,t.plan_id LIMIT ?''',
                (*important, *wanted, limit)).fetchall()
        candidates = []
        for row in rows:
            record = self.get(row['plan_id'])
            record['commands'] = self.commands(record['id'])
            record['retrieval_score'] = row['score']
            candidates.append(record)
        return {'features': features, 'candidates': candidates}

    def add(self, plan, *, title='', author='', source='manual', command=''):
        self._metadata(title=title, author=author, source=source, command=command)
        if source not in {'manual', 'agent', 'execution'}:
            raise ValueError('unsupported plan source')
        text, digest = self._canonical(plan)
        self.initialize()
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            return self._insert(connection, text, digest, title=title, author=author, source=source, command=command)

    def get(self, plan_id):
        if type(plan_id) is not int or plan_id < 1:
            raise ValueError('plan ID must be a positive integer')
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute('SELECT * FROM plans WHERE id=?', (plan_id,)).fetchone()
        if row is None:
            raise ValueError(f'plan not found: {plan_id}')
        result = dict(row)
        text = result.pop('plan_json')
        if hasattr(self.registry, 'tools') and result['contract_hash'] != self.registry.fingerprint:
            raise ValueError('stored API plan contract differs from active tool profile')
        canonical, digest = self._canonical(text)
        if digest != result['fingerprint'] or canonical != text:
            raise ValueError('stored plan integrity check failed')
        result['plan'] = strict_json(text)
        return result

    def list(self, *, limit=100, offset=0):
        if type(limit) is not int or not 1 <= limit <= 1000 or type(offset) is not int or offset < 0:
            raise ValueError('invalid plan list limit/offset')
        self.initialize()
        with closing(self._connect()) as connection:
            rows = connection.execute('''SELECT id,fingerprint,title,author,source,command,contract_hash,
                created_at,success_count,last_success_at FROM plans ORDER BY id LIMIT ? OFFSET ?''',
                (limit, offset)).fetchall()
        return [dict(row) for row in rows]

    def export(self, plan_id, path):
        output = Path(path).expanduser().resolve()
        for protected in [self.path, *(Path(str(self.path)+suffix) for suffix in ('-wal', '-shm', '-journal'))]:
            if output == protected or (output.exists() and protected.exists() and os.path.samefile(output, protected)):
                raise ValueError('export cannot overwrite the plan database or its sidecar files')
        plan = self.get(plan_id)['plan']
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(plan, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')

    def successes(self, plan_id, *, limit=100):
        self.get(plan_id)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('invalid success history limit')
        with closing(self._connect()) as connection:
            rows = connection.execute('SELECT * FROM successful_executions WHERE plan_id=? ORDER BY id DESC LIMIT ?',
                                      (plan_id, limit)).fetchall()
        return [dict(row) for row in rows]

    def record_success(self, plan, report, *, backend):
        """Record actual complete success; callers cannot bless unmet/unknown goals."""
        if (report.get('status') != 'SUCCESS' or report.get('execution_status') != 'SUCCESS'
                or report.get('robot_fault') or report.get('report_incomplete')
                or report.get('goal_status') not in {'achieved', 'not_evaluated'}
                or report.get('mission_success') is False
                or report.get('stop_confirmation', {}).get('confirmed') is False):
            return {'status': 'skipped', 'reason': 'execution_not_verified_success'}
        if report.get('replayed'):
            return {'status': 'skipped', 'reason': 'replayed_request'}
        if report.get('replans', 0) or report.get('revision', 1) != 1:
            return {'status': 'skipped', 'reason': 'continuation_not_full_plan'}
        mission_id, revision = report.get('mission_id'), report.get('revision')
        if not isinstance(mission_id, str) or not mission_id or type(revision) is not int or revision < 1:
            raise ValueError('successful execution requires mission_id and revision')
        self._metadata(backend=backend, executor=report['executor'], state_mode=report['state_mode'])
        text, digest = self._canonical(plan)
        self.initialize()
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            saved = self._insert(connection, text, digest, title=strict_json(text)['goal'], author='',
                                 source='execution', command=report.get('command', ''))
            previous = connection.execute('SELECT plan_id FROM successful_executions WHERE mission_id=? AND revision=?',
                                          (mission_id, revision)).fetchone()
            if previous is not None and previous['plan_id'] != saved['id']:
                raise ValueError('mission success already recorded for a different plan')
            cursor = connection.execute('''INSERT INTO successful_executions
                (plan_id,mission_id,revision,executor,backend,state_mode,goal_status,created_at)
                VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(mission_id,revision) DO NOTHING''',
                (saved['id'], mission_id, revision, report['executor'], backend, report['state_mode'],
                 report['goal_status'], self._now()))
            if cursor.rowcount:
                connection.execute('UPDATE plans SET success_count=success_count+1,last_success_at=? WHERE id=?',
                                   (self._now(), saved['id']))
            return {**saved, 'status': 'saved' if saved['created'] else 'existing',
                    'success_recorded': cursor.rowcount == 1}
