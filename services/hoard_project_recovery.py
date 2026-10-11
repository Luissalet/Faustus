"""Recover native Hoard projects from canonical identity and actual chat work."""
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path


def words(text):
    normalized = ''.join(character for character in unicodedata.normalize('NFKD', str(text)) if not unicodedata.combining(character)).casefold()
    return set(re.findall(r'[^\W_]+', normalized)) - set('el la los las un una de del que en a y con por para proyecto proyectos hoard hicimos otro dia ese esa the a an of in and project projects we made other day that'.split())


def date(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


class HoardProjectRecovery:
    def __init__(self, store=None, db_factory=None):
        from services.hoard_copilot import get_store
        from core.database import SessionLocal
        self.store = store or get_store()
        self.db_factory = db_factory or SessionLocal

    def rows(self, owner, app_id='', data_space_id=''):
        from core.database import Session, ChatMessage
        from src.owner_identity import effective_storage_owner
        owner = effective_storage_owner(owner)
        with self.store.connect() as db:
            links = db.execute('SELECT * FROM project_links WHERE owner=?', (owner or '',)).fetchall()
            refs = db.execute('SELECT session_id,project_link_id FROM conversations WHERE owner=?', (owner or '',)).fetchall()
        by_link = {}
        for ref in refs:
            by_link.setdefault(ref['project_link_id'], []).append(ref['session_id'])
        with self.db_factory() as db:
            for link in links:
                if app_id and link['app_id'] != app_id or data_space_id and link['data_space_id'] != data_space_id:
                    continue
                project = self.store.project_store().get(link['project_id'], owner)
                if not project:
                    yield {**dict(link), 'available': False, 'title': link['external_project_id'], 'conversations': [], 'evidence': [], 'activity_at': None}
                    continue
                conversations, evidence = [], []
                for sid in by_link.get(link['id'], []):
                    session = db.query(Session).filter(Session.id == sid).first()
                    if not session or owner and session.owner != owner:
                        continue
                    messages = db.query(ChatMessage).filter(ChatMessage.session_id == sid).order_by(ChatMessage.timestamp).all()
                    activity = max((message.timestamp for message in messages if message.timestamp), default=None)
                    conversations.append({'session_id': sid, 'name': session.name, 'activity_at': activity.isoformat() if activity else None})
                    evidence.extend({'session_id': sid, 'message_id': message.id, 'role': message.role, 'text': message.content,
                                     'timestamp': message.timestamp.isoformat() if message.timestamp else None} for message in messages)
                workspace = project.get('workspace_path') or ''
                evidence.sort(key=lambda item: item['timestamp'] or '')
                conversations.sort(key=lambda item: item['activity_at'] or '', reverse=True)
                yield {**dict(link), 'available': True, 'title': project.get('name') or link['external_project_id'],
                       'description': project.get('description') or '', 'folder': project.get('folder') or '',
                       'workspace': {'path': workspace, 'available': Path(workspace).exists(), 'git': (Path(workspace) / '.git').exists()} if workspace else None,
                       'memory_scope': {'project_id': project['id']}, 'conversations': conversations, 'evidence': evidence,
                       'activity_at': max((item['activity_at'] for item in conversations if item['activity_at']), default=None)}

    def search(self, owner, query='', app_id='', data_space_id='', after=None, before=None, offset=0, limit=20):
        terms = words(query)
        lower, upper = date(after), date(before)
        matches = []
        for row in self.rows(owner, app_id, data_space_id):
            activity = date(row['activity_at'])
            if lower and (not activity or activity < lower) or upper and (not activity or activity > upper):
                continue
            heading = words(' '.join(str(row.get(key, '')) for key in ('title', 'description', 'app_id', 'external_project_id')))
            evidence = row.pop('evidence')
            hits = [item for item in evidence if terms & words(item['text'])]
            matched = terms & (heading | set().union(*(words(item['text']) for item in hits)))
            if terms and not matched:
                continue
            row['matched_terms'] = sorted(matched)
            row['evidence'] = [{'session_id': hit['session_id'], 'message_id': hit['message_id'], 'timestamp': hit['timestamp'],
                                'excerpt': excerpt(hit['text'], terms)} for hit in hits[:3]]
            matches.append((len(terms & heading) * 4 + len(matched), row))
        matches.sort(key=lambda item: (item[0], item[1]['activity_at'] or ''), reverse=True)
        start, count = max(0, int(offset)), max(1, int(limit))
        return {'projects': [row for _, row in matches[start:start + count]], 'total': len(matches),
                'next_offset': start + count if start + count < len(matches) else None,
                'retrieval': 'identity, titles and canonical conversation text; dates use actual messages, not panel opening'}

    def recall(self, owner, project_link_id):
        from services.hoard_copilot import CopilotError
        row = next((row for row in self.rows(owner) if row['id'] == project_link_id), None)
        if not row:
            raise CopilotError('reference_unavailable', 'Hoard project link not found', 404)
        evidence = row.pop('evidence')
        row['recent_messages'] = [{**item, 'text': item['text'][:2000]} for item in evidence[-10:]]
        row['context'] = {'origin': {'app_id': row['app_id'], 'data_space_id': row['data_space_id'], 'surface': 'faustus'},
                          'project': {'external_project_id': row['external_project_id'], 'faustus_project_id': row['project_id'], 'title': row['title']}}
        row['next_step'] = 'Read the current native object with the origin app tools; recalled messages are historical context.'
        return row


def excerpt(text, terms):
    match = next((match for match in re.finditer(r'\S+', text) if words(match.group()) & terms), None)
    start = max(0, match.start() - 150) if match else 0
    return text[start:start + 600]
