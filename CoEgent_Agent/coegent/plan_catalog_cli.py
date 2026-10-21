"""Explicit catalog management, separate from planning and execution commands."""
import json
from pathlib import Path

from .plan_store import PlanStore
from .validation import strict_json
from .tool_profiles import profile_options, load_profile, profile_db


def add_catalog_parser(subs):
    parser = subs.add_parser('plans', help='manage the persistent plan catalog')
    actions = parser.add_subparsers(dest='catalog_action', required=True)
    for action in ('init', 'add', 'list', 'show', 'export'):
        command = actions.add_parser(action)
        profile_options(command)
        command.add_argument('--plan-db', help='SQLite catalog path; default COEGENT_PLAN_DB or data/plans.sqlite3')
        if action == 'add':
            command.add_argument('--plan-file', required=True, help='plan body or a report/request containing plan')
            command.add_argument('--title', default='')
            command.add_argument('--author', default='')
            command.add_argument('--source', choices=['manual', 'agent'], default='manual')
            command.add_argument('--command', default='')
        if action in {'show', 'export'}:
            command.add_argument('--plan-id', required=True, type=int)
        if action == 'export':
            command.add_argument('--out', required=True)
        if action == 'list':
            command.add_argument('--limit', type=int, default=100)
            command.add_argument('--offset', type=int, default=0)


def run_catalog(args):
    profile=load_profile(args)
    store = PlanStore(profile_db(profile,args.plan_db),registry=profile.registry) if profile else PlanStore(args.plan_db)
    if args.catalog_action == 'init':
        store.initialize()
        result = {'status': 'ready'}
    elif args.catalog_action == 'add':
        value = strict_json(Path(args.plan_file).read_text(encoding='utf-8'))
        if isinstance(value, dict) and 'plan' in value:
            value = value['plan']
        result = store.add(value, title=args.title, author=args.author, source=args.source, command=args.command)
    elif args.catalog_action == 'list':
        result = {'plans': store.list(limit=args.limit, offset=args.offset)}
    elif args.catalog_action == 'show':
        result = store.get(args.plan_id)
        result['commands'] = store.commands(args.plan_id)
        result['executions'] = store.successes(args.plan_id)
    else:
        store.export(args.plan_id, args.out)
        result = {'id': args.plan_id, 'out': str(Path(args.out).resolve())}
    print(json.dumps({'database': str(store.path), **result}, ensure_ascii=False, indent=2, allow_nan=False))
    return 0
