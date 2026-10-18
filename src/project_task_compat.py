"""Legacy project APIs are projections of the shared ledger after adoption.

The old stores remain readable migration sources. Normal tools never write a
second copy once a project adopts the board. Original IDs stay usable aliases.
"""
import json
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
import uuid
_raw = ContextVar('legacy_task_source', default=False)

def marker(project_id):
    from src.constants import DATA_DIR
    if not str(project_id).replace('-', '').replace('_', '').isalnum():
        raise ValueError('Invalid project identity')
    return Path(DATA_DIR) / 'project-task-ledgers' / (str(project_id) + '.json')

def active(project_id):
    return not _raw.get() and marker(project_id).is_file()

def project(project_id):
    return json.loads(marker(project_id).read_text(encoding='utf-8'))

def adopt(value):
    from src.hoard_link.atomic import write_json_atomic
    write_json_atomic(marker(value['id']), {key: value.get(key, '') for key in ('id', 'name', 'workspace')})

@contextmanager
def sources():
    token = _raw.set(True)
    try:
        yield
    finally:
        _raw.reset(token)

def iso(value):
    return datetime.fromtimestamp(value or 0, timezone.utc).isoformat()

def rows(pid):
    from src import project_tasks
    return project_tasks.listing(project(pid))['tasks']

def call(pid, operation, **args):
    from src import project_tasks
    return project_tasks.command(project(pid), operation, args)

def alias(task, kind):
    original = str(task.get('origin_ref') or '').split(':', 3)
    previous = original[3] if len(original) == 4 and original[2] == kind else None
    return task.get('context', {}).get('aliases', {}).get(kind) or previous or f"T:{task['id']}"

def find(pid, key, kind='issue'):
    for task in rows(pid):
        if str(key) in (f"T:{task['id']}", alias(task, kind)):
            return task
    raise ValueError('Task not found in this project')

def locate(key, raw_get):
    old = raw_get(key)
    if old and active(old['project_id']):
        return old['project_id']
    if str(key).startswith('T:') and (not _raw.get()):
        directory = marker('probe').parent
        for path in directory.glob('*.json'):
            pid = path.stem
            if any((f"T:{row['id']}" == key for row in rows(pid))):
                return pid
    return None

def edit(pid, task, **patch):
    return call(pid, 'task_edit', task_id=task['id'], expected_etag=task['etag'], **patch)['task']

def board_value(pid, task, detail=None, aliases=None):
    context = task.get('context') or {}
    source = task.get('origin_snapshot') or {}
    key = alias(task, 'issue')
    if aliases is None:
        aliases = {row['id']: alias(row, 'issue') for row in rows(pid)}
    related = context.get('links', source.get('links', []) if ':issue:' in str(task.get('origin_ref')) else [])
    links = [{**link, 'issue_id': key} for link in related if link.get('kind') not in ('blocks', 'blocked_by')]
    links.extend({'id': f"dep:{task['id']}:{row['id']}", 'issue_id': key, 'kind': 'blocked_by',
                  'target_issue_id': aliases[row['id']], 'created_at': iso(task['created'])} for row in task.get('blocked_by', []))
    links.extend({'id': f"dep:{other}:{task['id']}", 'issue_id': key, 'kind': 'blocks',
                  'target_issue_id': aliases[other], 'created_at': iso(task['created'])} for other in task.get('dependents', []))
    state = {'claimed': 'in_progress', 'review': 'in_progress', 'approved': 'done', 'changes': 'in_progress', 'dropped': 'wontfix'}.get(task['status'], task['status'])
    return {**source, 'id': key, 'canonical_id': task['id'], 'project_id': pid, 'seq': task['id'],
            'title': task['title'], 'body_md': task.get('body') or '', 'type': task['kind'], 'status': state,
            'status_category': 'completed' if state == 'done' else 'canceled' if state == 'wontfix' else 'started' if state in ('in_progress', 'blocked') else 'unstarted',
            'priority': f"P{task['priority']}", 'assignee': task.get('owner') or '', 'labels': task.get('labels', []),
            'acceptance': (task.get('acceptance') or '').splitlines(), 'created_at': iso(task['created']),
            'updated_at': iso(task['updated']), 'closed_at': iso(task['closed_at']) if task.get('closed_at') else None,
            'created_by': task.get('created_by'),
            'blocked_by': [aliases[row['id']] for row in task.get('blocked_by', []) if row['status'] not in ('done', 'approved', 'dropped')],
            'links': links, 'refs': context.get('refs', source.get('refs', [])),
            'comments': [*source.get('comments', []), *[{'id': str(msg['id']), 'issue_id': key, 'body_md': msg['body'], 'author': msg['author'], 'created_at': iso(msg['created'])} for msg in (detail or {}).get('messages', []) if msg['kind'] == 'comment']],
            'events': [*source.get('events', []), *[{'id': str(item['revision']), 'kind': 'edit', 'actor': item['author'], 'created_at': iso(item['created']), 'payload': item} for item in (detail or {}).get('edits', [])]], 'etag': task['etag']}

def board_list(pid, **filters):
    tasks = rows(pid)
    aliases = {row['id']: alias(row, 'issue') for row in tasks}
    values = [board_value(pid, task, aliases=aliases) for task in tasks]
    for field in ('status', 'type', 'assignee', 'priority'):
        if filters.get(field):
            values = [v for v in values if v[field] == filters[field]]
    if filters.get('label'):
        values = [v for v in values if filters['label'] in v['labels']]
    if filters.get('q'):
        query = filters['q'].casefold()
        values = [v for v in values if query in (v['title'] + ' ' + v['body_md'] + ' ' + v['id']).casefold()]
    start = int(filters.get('cursor') or 0)
    limit = min(500, int(filters.get('limit') or 50))
    return (values[start:start + limit], str(start + limit) if start + limit < len(values) else None)

def board_get(pid, key):
    task = find(pid, key)
    detail = call(pid, 'task', task_id=task['id'])
    return board_value(pid, detail['task'], detail)

def fields(values):
    result = {new: values[old] for old, new in {'title': 'title', 'body_md': 'body', 'text': 'body', 'notes': 'body', 'type': 'kind', 'assignee': 'owner', 'labels': 'labels'}.items() if old in values}
    if 'priority' in values:
        result['priority'] = int(str(values['priority']).removeprefix('P'))
    if 'acceptance' in values:
        result['acceptance'] = '\n'.join(values['acceptance']) if isinstance(values['acceptance'], (list, tuple)) else values['acceptance']
    if 'status' in values:
        result['status'] = {'wontfix': 'dropped', 'duplicate': 'dropped', 'proposed': 'open', 'accepted': 'done', 'rejected': 'dropped', 'superseded': 'dropped'}.get(values['status'], values['status'])
    return result

def board_create(pid, key, **values):
    from src.project_board import LINK_KINDS
    related, blockers, blocked = [], [], []
    for link in values.get('links') or []:
        if not isinstance(link, dict) or link.get('kind') not in LINK_KINDS:
            raise ValueError('Invalid task link')
        target = find(pid, link.get('target') or link.get('target_issue_id'))
        kind = link['kind']
        related.append({'id': uuid.uuid4().hex, 'kind': kind, 'target_issue_id': alias(target, 'issue'), 'created_at': iso(datetime.now().timestamp())})
        if kind == 'blocked_by': blockers.append(target['id'])
        if kind == 'blocks': blocked.append(target['id'])
    values = fields(values)
    task = call(pid, 'task_add', **{k: v for k, v in values.items() if k != 'status'},
                blocked_by=blockers, blocks=blocked, context={'links': related})['task']
    return board_value(pid, task)

def board_update(pid, key, patch, **kwargs):
    task = edit(pid, find(pid, key), **fields(patch))
    return board_value(pid, task)

def board_comment(pid, key, body_md, **kwargs):
    task = edit(pid, find(pid, key), note=body_md)
    return board_get(pid, alias(task, 'issue'))['comments'][-1]

def board_context(pid, key, collection, value=None, remove=None):
    task = find(pid, key)
    context = dict(task.get('context') or {})
    existing = context.get(collection)
    if existing is None:
        existing = (call(pid, 'task', task_id=task['id'])['task'].get('origin_snapshot') or {}).get('links' if collection == 'requirement_links' else collection, [])
    context[collection] = [row for row in existing if row.get('id') != remove]
    if value:
        context[collection].append(value)
    edit(pid, task, context=context)
    return value if value else len(existing) != len(context[collection])

def board_dispatch(name, pid, *args, **kw):
    if name == 'create_issue':
        return board_create(pid, *args, **kw)
    if name in ('get', 'get_or_raise'):
        return board_get(pid, *args)
    if name == 'list_issues':
        return board_list(pid, **kw)
    if name == 'update_issue':
        return board_update(pid, *args, **kw)
    if name == 'add_comment':
        return board_comment(pid, *args, **kw)
    if name == 'claim':
        task = find(pid, args[0])
        if task.get('owner') and task['owner'] != args[1] and (task['status'] not in ('open', 'done', 'dropped')):
            raise ValueError('Task is held by ' + task['owner'])
        return board_update(pid, args[0], {'assignee': args[1], 'status': 'in_progress'})
    if name == 'delete_issue':
        board_update(pid, args[0], {'status': 'wontfix'})
        return True
    if name == 'ready_issues':
        return [v for v in board_list(pid, limit=500)[0] if v['status'] == 'open' and (not v['blocked_by'])][:kw.get('limit', 50)]
    if name == 'summary':
        values = board_list(pid, limit=500)[0]
        return {'counts': {s: sum((v['status'] == s for v in values)) for s in ('open', 'in_progress', 'blocked', 'done', 'wontfix', 'duplicate')}, 'ready': [v for v in values if v['status'] == 'open' and (not v['blocked_by'])][:8], 'in_progress': [v for v in values if v['status'] == 'in_progress'][:20], 'recent_done': [v for v in values if v['status'] == 'done'][:5]}
    if name == 'add_ref':
        value = {'id': uuid.uuid4().hex, 'issue_id': args[0], 'kind': args[1], 'value': args[2], 'label': kw.get('label', ''), 'created_at': iso(datetime.now().timestamp())}
        return board_context(pid, args[0], 'refs', value)
    if name == 'find_issue_by_ref':
        for task in rows(pid):
            value = board_get(pid, alias(task, 'issue'))
            if any((r['kind'] == args[0] and r['value'] == args[1] and (r.get('label', '') == args[2]) for r in value['refs'])):
                return value
        return None
    if name == 'add_link':
        key, kind, target = args
        task, other = (find(pid, key), find(pid, target))
        if kind in ('blocks', 'blocked_by'):
            dependent, blocker = (other, task) if kind == 'blocks' else (task, other)
            call(pid, 'task_deps', task_id=dependent['id'], add=[blocker['id']])
        value = {'id': uuid.uuid4().hex, 'issue_id': key, 'kind': kind, 'target_issue_id': target, 'created_at': iso(datetime.now().timestamp())}
        return board_context(pid, key, 'links', value)
    if name == 'remove_link':
        key, link_id = args
        task = find(pid, key)
        link = next((v for v in board_get(pid, key)['links'] if v['id'] == link_id), None)
        if link and link['kind'] in ('blocks', 'blocked_by'):
            other = find(pid, link['target_issue_id'])
            dependent, blocker = (other, task) if link['kind'] == 'blocks' else (task, other)
            call(pid, 'task_deps', task_id=dependent['id'], remove=[blocker['id']])
        removed = board_context(pid, key, 'links', remove=link_id)
        return bool(link) or removed
    if name == 'export_markdown':
        return '# ' + args[1] + '\n\n' + '\n\n'.join((f"## {v['id']} {v['title']}\n{v['status']} · {v['priority']} · {v['assignee']}\n\n{v['body_md']}\n\n" + '\n'.join(v['acceptance']) for v in board_list(pid, limit=500)[0]))
    if name == 'link_commit':
        import re
        sha, message = args
        linked = []
        closed = []
        for task in rows(pid):
            key = alias(task, 'issue')
            if not re.search('(?<!\\w)' + re.escape(key) + '(?!\\w)', message, re.I):
                continue
            value = board_get(pid, key)
            if not any((ref.get('kind') == 'commit' and ref.get('value') == sha for ref in value['refs'])):
                board_dispatch('add_ref', pid, key, 'commit', sha, label=message.splitlines()[0][:300])
            linked.append(key)
            if re.search('(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|cierra|arregla)\\s+#?' + re.escape(key) + '(?!\\w)', message, re.I) and task['status'] not in ('done', 'dropped'):
                board_update(pid, key, {'status': 'done'})
                closed.append(key)
        return {'linked': linked, 'closed': closed}
    raise ValueError('Unsupported shared board operation: ' + name)

def wrap_board(namespace):
    project_first = {'create_issue', 'list_issues', 'ready_issues', 'summary', 'find_issue_by_ref', 'export_markdown', 'link_commit'}
    names = project_first | {'get', 'get_or_raise', 'update_issue', 'claim', 'add_comment', 'add_link', 'remove_link', 'add_ref', 'delete_issue'}
    raw_get = namespace['get']
    for name in names:
        original = namespace[name]

        def wrapped(*args, _name=name, _original=original, **kw):
            pid = str(args[0]) if _name in project_first else locate(str(args[0]), raw_get)
            if not pid or not active(pid):
                return _original(*args, **kw)
            try:
                return board_dispatch(_name, pid, *(args[1:] if _name in project_first else args), **kw)
            except ValueError as exc:
                from src.project_board import BoardError
                raise BoardError(str(exc)) from exc
        namespace[name] = wrapped
    original_import = namespace['import_sources']

    def import_sources(value, key, **kw):
        if not active(value['id']):
            return original_import(value, key, **kw)
        from src import project_board
        preview = project_board.Store().import_sources(value, key, **{**kw, 'dry_run': True})
        existing = {label for row in rows(value['id']) for label in row.get('labels', [])}
        candidates = [item for item in preview['preview'] if f"import:{item['source']}:{item['ext_id']}" not in existing]
        if kw.get('dry_run', True):
            return {**preview, 'preview': candidates, 'skipped': preview['skipped'] + len(preview['preview']) - len(candidates)}
        return canonical_import(value, key, kw.get('sources', []), candidates)
    namespace['import_sources'] = import_sources

def canonical_import(value, key, sources, candidates):
    from src import project_board
    pid = value['id']
    created = 0
    mapping = {}
    candidates = {(i['source'], i['ext_id']): i for i in candidates}
    base = Path(value.get('workspace') or '.')
    inputs = []
    if 'objetivos' in sources:
        inputs.extend((('objetivos', v['ext_id'], v, 'feature') for v in project_board._parse_objetivos(str(base / 'OBJETIVOS.md'))))
    if 'pendientes' in sources:
        inputs.extend((('pendientes', v['ext_id'], v, 'task') for v in project_board._parse_pendientes(str(base / 'PENDIENTES.md'))))
    backlog = project_board._parse_backlog(str(base / 'docs/spec/v2/backlog.json')) if 'backlog' in sources else []
    inputs.extend((('backlog', v['id'], v, 'feature') for v in backlog))
    for source, ext, item, kind in inputs:
        candidate = candidates.get((source, ext))
        if not candidate:
            continue
        label = f'import:{source}:{ext}'
        row = call(pid, 'task_add', title=candidate['title'], body=item.get('body', ''), kind=kind, priority=int(candidate['priority'][1:]), labels=[label], origin_ref=f'file-import:{pid}:{source}:{ext}', origin_status=candidate['status'])
        created += not row.get('existing', False)
        mapping[ext] = f"T:{row['task']['id']}"
    for item in backlog:
        if item['id'] in mapping:
            for dep in item.get('depends_on', []):
                if dep in mapping:
                    board_dispatch('add_link', pid, mapping[item['id']], 'blocked_by', mapping[dep])
    return {'created': created, 'skipped': len(inputs) - created, 'preview': list(candidates.values())}

def objective_state(value):
    result = {}
    edges = []
    tasks = rows(value['id'])
    aliases = {task['id']: alias(task, 'objective') for task in tasks}
    for task in tasks:
        key = alias(task, 'objective')
        result[key] = {'t': 'obj', 'id': key, 'title': task['title'], 'notes': task.get('body') or '', 'status': {'claimed': 'in_progress', 'review': 'in_progress', 'approved': 'done', 'changes': 'in_progress'}.get(task['status'], task['status']), 'priority': task['priority'] + 1, 'updated_at': iso(task['updated']), 'created_at': iso(task['created']), 'last_actor': 'user'}
        edges.extend(({'from': key, 'to': aliases[b['id']]} for b in task.get('blocked_by', [])))
    return {'objectives': result, 'edges': edges}

def objective_apply(value, deltas, actor, session_id):
    pid = value['id']
    applied = []
    conflicts = []
    for delta in deltas:
        try:
            if not isinstance(delta, dict):
                raise ValueError('Delta must be an object')
            op = delta.get('op', '').upper()
            if op not in ('ADD', 'EDIT', 'KILL'):
                raise ValueError('Unknown objective operation')
            deps = delta.get('deps', [])
            if not isinstance(deps, list) or any((not isinstance(k, str) or not k for k in deps)):
                raise ValueError('deps must be a list of task IDs')
            blockers = [find(pid, k, 'objective')['id'] for k in deps]
            if 'priority' in delta and (isinstance(delta['priority'], bool) or not isinstance(delta['priority'], int) or (not 1 <= delta['priority'] <= 4)):
                raise ValueError('priority must be 1..4')
            patch = fields({k: v for k, v in delta.items() if k in ('title', 'notes', 'status')})
            if 'priority' in delta:
                patch['priority'] = int(delta['priority']) - 1
            if 'status' in patch and patch['status'] not in ('open', 'in_progress', 'blocked', 'done', 'dropped'):
                raise ValueError('Invalid objective status')
            if op == 'ADD':
                status = patch.pop('status', 'open')
                task = call(pid, 'task_add', **patch, note=delta.get('rationale', ''))['task']
                if status != 'open':
                    task = edit(pid, task, status=status)
            else:
                task = find(pid, delta.get('id'), 'objective')
                if delta.get('base_updated_at') and delta['base_updated_at'] != iso(task['updated']):
                    raise ValueError('Objective changed since it was read')
                if op == 'KILL':
                    patch['status'] = 'dropped'
                task = edit(pid, task, **patch, note=delta.get('rationale', ''))
            if 'deps' in delta:
                call(pid, 'task_deps', task_id=task['id'], **{'set': blockers})
            applied.append({'op': op, 'id': alias(task, 'objective'), 'fields': delta, 'rationale': delta.get('rationale', '')})
        except (ValueError, TypeError) as exc:
            conflicts.append({'op': delta.get('op') if isinstance(delta, dict) else None, 'id': delta.get('id') if isinstance(delta, dict) else None, 'reason': str(exc)})
    from services.objectives import serialize_state
    return {'applied': applied, 'conflicts': conflicts, 'state': serialize_state(objective_state(value))}

def requirement_value(pid, task):
    context = task.get('context') or {}
    key = alias(task, 'requirement')
    detail = call(pid, 'task', task_id=task['id'])
    state = {'done': 'accepted', 'approved': 'accepted', 'dropped': 'rejected'}.get(task['status'], 'proposed')
    previous = context.get('requirement_status')
    if previous and fields({'status': previous})['status'] == task['status']:
        state = previous
    return {'id': pid + ':' + key, 'key': key, 'project_id': pid, 'canonical_id': task['id'], 'title': task['title'], 'text': task.get('body') or '', 'acceptance': (task.get('acceptance') or '').splitlines(), 'status': state, 'source': context.get('source', 'human'), 'proposed_by': context.get('proposed_by', 'human'), 'current_revision': len(requirement_versions(detail, pid)), 'links': context.get('requirement_links', (detail['task'].get('origin_snapshot') or {}).get('links', [])), 'created_at': iso(task['created']), 'updated_at': iso(task['updated']), 'created_by': task.get('created_by')}

def requirement_dispatch(name, pid, *args, **kw):
    from src.requirements.store import SOURCES, STATUSES, PROPOSED_BY, LINK_KINDS
    values = kw if name == 'create' else args[1] if name == 'update' else {}
    for field, vocabulary in (('source', SOURCES), ('status', STATUSES), ('proposed_by', PROPOSED_BY)):
        if field in values and values[field] not in vocabulary:
            raise ValueError('Invalid requirement ' + field)
    if name == 'create':
        values = dict(kw)
        if values.get('proposed_by') == 'model':
            values['status'] = 'proposed'
        patch = fields(values)
        status = patch.pop('status', None)
        task = call(pid, 'task_add', **patch, context={'source': values.get('source', 'human'), 'proposed_by': values.get('proposed_by', 'human'), 'requirement_status': values.get('status', 'proposed')})['task']
        if status and status != 'open':
            task = edit(pid, task, status=status)
        return requirement_value(pid, task)
    if name in ('list_requirements', 'all_requirement_keys'):
        values = [requirement_value(pid, t) for t in rows(pid)]
        if name == 'all_requirement_keys':
            return [v['key'] for v in values]
        return [v for v in values if all((not kw.get(k) or v[k] == kw[k] for k in ('status', 'source'))) and (not kw.get('q') or kw['q'].casefold() in (v['title'] + ' ' + v['text']).casefold())]
    task = find(pid, args[0], 'requirement')
    if name in ('get', 'get_or_raise'):
        return requirement_value(pid, task)
    if name == 'update':
        if kw.get('by') == 'model' and args[1].get('status') in ('accepted', 'rejected'):
            raise ValueError('A model proposal cannot decide acceptance')
        patch = fields(args[1])
        context = dict(task.get('context') or {})
        if 'source' in args[1]:
            context['source'] = args[1]['source']
            patch['context'] = context
        if 'status' in args[1]:
            context['requirement_status'] = args[1]['status']
            patch['context'] = context
        task = edit(pid, task, **patch, note=kw.get('change_note', ''))
        return requirement_value(pid, task)
    if name == 'revisions':
        return requirement_versions(call(pid, 'task', task_id=task['id']), pid)
    if name == 'list_links':
        detail = call(pid, 'task', task_id=task['id'])['task']
        return detail.get('context', {}).get('requirement_links', (detail.get('origin_snapshot') or {}).get('links', []))
    if name == 'add_link':
        if kw.get('kind') not in LINK_KINDS or not str(kw.get('target') or '').strip():
            raise ValueError('Invalid requirement evidence link')
        link = {'id': uuid.uuid4().hex, **kw, 'req_id': pid + ':' + args[0], 'req_revision_at_link': requirement_value(pid, task)['current_revision'], 'state': 'linked', 'created_at': iso(datetime.now().timestamp())}
        return board_context(pid, f"T:{task['id']}", 'requirement_links', link)
    if name == 'remove_link':
        return board_context(pid, f"T:{task['id']}", 'requirement_links', remove=args[1])
    raise ValueError('Unsupported requirement operation')

def requirement_versions(detail, pid):
    old = (detail['task'].get('origin_snapshot') or {}).get('revisions', [])
    result = list(old)
    keys = ('title', 'body', 'acceptance', 'status')
    for item in detail.get('edits', []):
        if old and item['kind'] in ('created', 'baseline'):
            continue
        before = item['before']
        if before and all((before.get(k) == item['after'].get(k) for k in keys)) and all(before.get('context', {}).get(k) == item['after'].get('context', {}).get(k) for k in ('source', 'requirement_status')):
            continue
        snapshot=item['after'];context=snapshot.get('context') or {}
        status={'done':'accepted','approved':'accepted','dropped':'rejected'}.get(snapshot.get('status'),'proposed')
        previous=context.get('requirement_status')
        if previous and fields({'status':previous})['status']==snapshot.get('status'): status=previous
        result.append({'id':f"task-edit:{detail['task']['id']}:{item['revision']}", 'req_id':pid+':'+alias(detail['task'],'requirement'),
            'revision':len(result)+1,'created_at':iso(item['created']),'changed_by':item['author'],'change_note':'',
            'title':snapshot.get('title',''),'text':snapshot.get('body',''),'acceptance':(snapshot.get('acceptance') or '').splitlines(),
            'source':context.get('source','human'),'status':status})
    return result

def wrap_requirements(namespace):
    for name in ('create', 'get', 'get_or_raise', 'list_requirements', 'revisions', 'update', 'add_link', 'list_links', 'remove_link', 'all_requirement_keys'):
        original = namespace[name]

        def wrapped(pid, *args, _name=name, _original=original, **kw):
            if not active(pid):
                return _original(pid, *args, **kw)
            try:
                return requirement_dispatch(_name, pid, *args, **kw)
            except ValueError as exc:
                from src.requirements.store import RequirementsError
                if _name == 'get':
                    return None
                raise RequirementsError(str(exc)) from exc
        namespace[name] = wrapped

    def set_link_state(link_id, state):
        from src.requirements.store import Store, LINK_STATES, RequirementsError
        if state not in LINK_STATES:
            raise RequirementsError('Invalid link state')
        if not _raw.get():
            for path in marker('probe').parent.glob('*.json'):
                for task in rows(path.stem):
                    links = task.get('context', {}).get('requirement_links')
                    if links is None and ':requirement:' in str(task.get('origin_ref')):
                        links = (call(path.stem, 'task', task_id=task['id'])['task'].get('origin_snapshot') or {}).get('links', [])
                    links = links or []
                    if any((link['id'] == link_id for link in links)):
                        context = {**task['context'], 'requirement_links': [{**link, 'state': state} if link['id'] == link_id else link for link in links]}
                        edit(path.stem, task, context=context)
                        return
        return Store().set_link_state(link_id, state)
    namespace['set_link_state'] = set_link_state

def objective_log(value, record=None, limit=50):
    pid = value['id']
    if record is not None:
        edit(pid, find(pid, record['id'], 'objective'), note=json.dumps(record, ensure_ascii=False))
        return True
    result = []
    for task in rows(pid):
        detail = call(pid, 'task', task_id=task['id'])
        result.extend((detail['task'].get('origin_snapshot') or {}).get('history', []))
        result.extend(({'id': alias(task, 'objective'), 'kind': 'edit', 'actor': e['author'], 'ts': iso(e['created']), 'fields': e['after'], 'before': e['before']} for e in detail.get('edits', [])))
        for msg in detail.get('messages', []):
            if msg['kind'] != 'comment':
                continue
            try:
                item = json.loads(msg['body'])
            except ValueError:
                item = {'id': alias(task, 'objective'), 'kind': 'comment', 'actor': msg['author'], 'ts': iso(msg['created']), 'note': msg['body']}
            result.append(item if isinstance(item, dict) else {'note': str(item)})
    return sorted(result, key=lambda x: str(x.get('ts', '')))[-max(1, int(limit)):]
