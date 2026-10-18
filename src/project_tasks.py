"""The project's single shared task ledger, used by Faustus UI and MCP.

The existing coordination repository keeps canonical IDs and agent leases.
Old project records are imported once by stable origin, never deleted or
overwritten on a later visit. No browser-local or second task database.
"""
import json
import re
import sys

from src.shared_coordination import call


class TaskError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def rpc(operation, args=None):
    result = call('hub_agora_' + operation, args or {})
    if not isinstance(result, dict) or not result.get('ok'):
        error = (result or {}).get('error', 'Project tasks are unavailable')
        raise TaskError(error if isinstance(error, str) else str(error), (result or {}).get('status', 503))
    return result


def space(project, *, create=False):
    bound = [s for s in rpc('spaces')['spaces'] if s.get('project_id') == project['id']]
    if bound:
        if len(bound) != 1:
            raise TaskError('More than one Agora is bound to this project. Resolve the binding before editing.', 409)
        return bound[0]
    if not create:
        return None
    try:
        return rpc('space_create', {'agent': 'faustus', 'id': 'project-' + project['id'],
            'title': project['name'], 'project_id': project['id'], 'workspace': project.get('workspace') or ''})['space']
    except TaskError as exc:
        if exc.status != 409:
            raise
        # Concurrent first opening of the same project: use the committed binding.
        return space(project)


def legacy_records(project):
    from src.project_task_compat import sources
    with sources():
        yield from _legacy_records(project)


def _legacy_records(project):
    from services import objectives
    from src import project_board
    from src.requirements import store as requirements
    dashboard = objectives.dashboard_payload(project, log_limit=sys.maxsize, strict=True)
    for item in dashboard['objectives']:
        yield 'objective', item['id'], {**item,
            'history': [entry for entry in dashboard.get('log', []) if entry.get('id') == item['id']],
            'relationships': [edge for edge in dashboard.get('edges', []) if item['id'] in (edge.get('from'), edge.get('to'))]}
    for item in requirements.list_requirements(project['id']):
        yield 'requirement', item['key'], {**requirements.get_or_raise(project['id'], item['key']),
            'revisions': requirements.revisions(project['id'], item['key']),
            'links': requirements.list_links(project['id'], item['key'])}
    cursor = None
    while True:
        rows, cursor = project_board.list_issues(project['id'], limit=200, cursor=cursor)
        for item in rows:
            yield 'issue', item['id'], project_board.get_or_raise(item['id'])
        if not cursor:
            break


def prepare(project):
    sid = space(project, create=True)['id']
    imported = 0
    for kind, key, item in legacy_records(project):
        acceptance = item.get('acceptance') or item.get('acceptance_criteria') or ''
        if not isinstance(acceptance, str):
            acceptance = '\n'.join(acceptance) if isinstance(acceptance, list) and all(isinstance(line, str) for line in acceptance) else json.dumps(acceptance, ensure_ascii=False, indent=2)
        body = item.get('body_md') or item.get('text') or item.get('description') or item.get('notes') or ''
        # Retains evidence, decisions, links and old IDs even when their old
        # representation has no equivalent editable field in the shared ledger.
        source = json.dumps(item, ensure_ascii=False, indent=2)
        if len(str(body)) > 200000 or len(acceptance) > 200000 or len(source) > 600000:
            raise TaskError('An original record is too large to import without losing content: ' + str(key))
        status = item.get('status', 'open')
        status = {'accepted': 'done', 'completed': 'done', 'wontfix': 'dropped', 'duplicate': 'dropped',
                  'rejected': 'dropped', 'superseded': 'dropped',
                  'doing': 'in_progress'}.get(status, status)
        if status not in ('open', 'done', 'dropped', 'in_progress', 'blocked'):
            status = 'open'
        priority = item.get('priority', 2)
        if isinstance(priority, str):
            priority = int(priority.removeprefix('P'))
        if kind == 'objective':
            priority -= 1  # Objectives use 1..4; the shared board uses 0..3.
        owner = item.get('assignee') or item.get('owner') or ''
        if not isinstance(owner, str) or not re.fullmatch(r'[a-z][a-z0-9_.-]{1,40}', owner):
            owner = None
        result = rpc('task_add', {'agent': 'faustus', 'space_id': sid, 'title': item['title'], 'body': body,
            'acceptance': acceptance, 'priority': min(3, max(0, priority)),
            'owner': owner,
            'kind': item.get('type', 'feature'), 'labels': item.get('labels', []),
            'context': {'aliases': {kind: str(key)}, 'source': item.get('source', 'human'),
                        'proposed_by': item.get('proposed_by', 'human'),
                        'requirement_status': item.get('status') if kind=='requirement' else None},
            'repo': project['name'], 'origin_ref': f"faustus:{project['id']}:{kind}:{key}", 'origin_status': status,
            'origin_snapshot': source})
        imported += not result.get('existing', False)
    from src.project_task_compat import adopt
    adopt(project)
    return {'ok': True, 'space_id': sid, 'imported': imported}


def listing(project):
    bound = space(project)
    if not bound:
        return {'ok': True, 'space_id': None, 'tasks': [], 'agents': []}
    rows, cursor = [], None
    while True:
        result = rpc('tasks', {'space_id': bound['id'], 'limit': 500, **({'cursor': cursor} if cursor else {})})
        rows.extend(result['tasks'])
        cursor = result.get('next_cursor')
        if not cursor:
            break
    return {'ok': True, 'tasks': rows, 'space_id': bound['id']}


def command(project, operation, args=None):
    bound = space(project, create=operation == 'task_add')
    if not bound:
        raise TaskError('This project has no Agora yet.', 404)
    return rpc(operation, {**(args or {}), 'agent': 'faustus', 'space_id': bound['id']})
