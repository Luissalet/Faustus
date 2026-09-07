"""Validate workflow output destinations before publishing or queuing work."""


def validate_output_scope(owner, project_id='', session_id=''):
    if project_id:
        from services.projects import get_store
        project = get_store().get(project_id, owner=owner)
        if not project or not owner or str(project.get('owner') or '') != owner:
            raise ValueError('the artifact project is not owned by the workflow owner')
    if session_id:
        from core.database import SessionLocal, Session
        with SessionLocal() as db:
            chat = db.get(Session, session_id)
            if not chat or not owner or chat.owner != owner:
                raise ValueError('the artifact conversation is not owned by the workflow owner')
            if project_id and chat.project_id != project_id:
                raise ValueError('the artifact conversation belongs to a different project')
