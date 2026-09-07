"""Owner-scoped, human-configured teams pinned for each chat turn."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
import threading
from core.atomic_io import atomic_write_text
from src.constants import DATA_DIR

_LOCK = threading.RLock()
DEFAULT = {'enabled': False, 'members': [], 'max_parallel': 2, 'max_rounds': 12, 'timeout_s': 600}


def validate(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULT):
        raise ValueError('Invalid chat team fields')
    team = {**DEFAULT, **value}
    if type(team['enabled']) is not bool:
        raise ValueError('enabled must be a boolean')
    for key, low, high in [('max_parallel', 1, 8), ('max_rounds', 3, 40), ('timeout_s', 60, 7200)]:
        if type(team[key]) is not int or not low <= team[key] <= high:
            raise ValueError(f'{key} must be between {low} and {high}')
    if not isinstance(team['members'], list) or len(team['members']) > 8:
        raise ValueError('Choose at most eight team members')
    members, ids = [], set()
    for raw in team['members']:
        if not isinstance(raw, dict) or set(raw) - {'id', 'name', 'role', 'model', 'endpoint_id', 'tools', 'write', 'files'}:
            raise ValueError('Invalid team member fields')
        row = {key: raw.get(key, '') for key in ('id', 'name', 'role', 'model', 'endpoint_id')}
        for key, limit in [('id', 60), ('name', 80), ('role', 2000), ('model', 200), ('endpoint_id', 160)]:
            if not isinstance(row[key], str) or len(row[key]) > limit:
                raise ValueError(f'Invalid member {key}')
            row[key] = row[key].strip()
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', row['id']) or row['id'] in ids or not row['name']:
            raise ValueError('Each member needs a unique ID and a name')
        ids.add(row['id'])
        if bool(row['model']) != bool(row['endpoint_id']):
            raise ValueError('Choose both a model and its endpoint, or inherit both')
        row['write'] = raw.get('write', True)
        if type(row['write']) is not bool:
            raise ValueError('write must be a boolean')
        for key in ('tools', 'files'):
            values = raw.get(key, [])
            if not isinstance(values, list) or len(values) > 40 or any(not isinstance(v, str) or not v.strip() or len(v) > 300 for v in values):
                raise ValueError(f'Invalid member {key}')
            row[key] = list(dict.fromkeys(v.strip() for v in values))
        if row['tools']:
            from src.agent_defs import known_tools
            if set(row['tools']) - set(known_tools()):
                raise ValueError('The team contains an unknown tool')
        members.append(row)
    if team['enabled'] and not members:
        raise ValueError('Add a team member before enabling orchestration')
    return {**team, 'members': members}


def _path(session_id, owner):
    if not session_id:
        raise ValueError('A saved conversation is required')
    key = hashlib.sha256(json.dumps([str(owner or ''), str(session_id)]).encode()).hexdigest()
    return Path(DATA_DIR) / 'chat_teams' / (key + '.json')


def load(session_id, owner):
    with _LOCK:
        try:
            raw = json.loads(_path(session_id, owner).read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {'revision': 0, 'team': validate(DEFAULT)}
        if not isinstance(raw, dict) or type(raw.get('revision')) is not int:
            raise ValueError('Chat team storage is invalid; no configuration was overwritten')
        return {'revision': raw['revision'], 'team': validate(raw.get('team'))}


def save(session_id, owner, team, expected_revision):
    team = validate(team)
    with _LOCK:
        current = load(session_id, owner)
        if type(expected_revision) is not int or current['revision'] != expected_revision:
            raise ValueError('Team changed elsewhere. Reload before saving.')
        result = {'revision': current['revision'] + 1, 'team': team}
        path = _path(session_id, owner)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(result, ensure_ascii=False))
        return result


def instruction(team):
    if not team or not team.get('enabled'):
        return ''
    return ('The user configured you as this chat\'s coordinator. Delegate bounded tasks, '
            'combine results and resolve disagreements. Use delegate_agents with task field team_member set '
            'to a member ID below (or its exact name). Do not invent members or override their routes/policies. '
            'Avoid overlapping edits; file locks and inherited permissions still apply. '
            'Explain progress and answer in the user\'s language. A roster is not an instruction to run '
            'every member on every message. Team: ' + json.dumps(team, ensure_ascii=False))


def bind_tasks(args, team, owner):
    """Apply the trusted turn snapshot after parsing model-authored tasks."""
    if not team or not team.get('enabled'):
        return args
    members = {m['id']: m for m in team['members']}
    for task in args['tasks']:
        ident = task.get('team_member') or task.get('name')
        member = members.get(ident)
        if member is None:
            matches = [m for m in members.values() if m['name'] == ident]
            member = matches[0] if len(matches) == 1 else None
        if member is None:
            raise ValueError('Choose a configured team_member: ' + ', '.join(members))
        if member['endpoint_id']:
            from src.endpoint_resolver import resolve_endpoint_by_id
            if not resolve_endpoint_by_id(member['endpoint_id'], member['model'], owner=owner, require_exact_model=True):
                raise ValueError('A selected team model is unavailable. No fallback was used.')
        definition = {'slug': 'chat-' + member['id'], 'name': member['name'], 'mode': 'worker',
                      'tools': member['tools'], 'permission': [] if member['write'] else ['deny write **'],
                      'prompt': member['role'], 'max_rounds': team['max_rounds'], 'timeout_s': team['timeout_s']}
        for key in ('resume', 'agent', 'runner'):
            task.pop(key, None)
        task.update(name=member['name'], model=member['model'], endpoint_id=member['endpoint_id'],
                    files=member['files'] or task.get('files', []), agent_def=definition,
                    system_prompt=member['role'], max_rounds=team['max_rounds'], timeout_s=team['timeout_s'], team_bound=True)
    args.update(max_rounds=min(args['max_rounds'], team['max_rounds']),
                timeout_s=min(args['timeout_s'], team['timeout_s']), reviewer=False)
    return args
