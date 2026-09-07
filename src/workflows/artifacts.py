"""Durable report outputs from this workflow's inline content and results.

Only inline UTF-8 content or a declared value from this run is accepted. This
node is not a filesystem-read capability and never accepts an arbitrary path.
"""
import tempfile
from pathlib import Path

from src import artifact_store
from src.contracts import ExecutionResult
from src.workflows.handlers import resolve, _MISSING

MAX_CONTENT_BYTES = 10 * 1024 * 1024


def save(node, context):
    config = dict(node.config)
    owner = str(context.get('owner') or '')
    run_id = str(context.get('run_id') or '')
    if not owner or not run_id:
        raise ValueError('artifact output requires a recorded run and owner')
    inputs = context.get('inputs') or {}
    project_id = str(context.get('project_id') or '')
    session_id = str(inputs.get('session_id') or '')
    source_path = config.get('content_from')
    content = resolve(source_path, context) if isinstance(source_path, str) else config.get('content', _MISSING)
    if content is _MISSING:
        raise ValueError('artifact output needs content or a valid content_from reference')
    if not isinstance(content, str):
        raise ValueError('artifact content must be text; serialize structured values before saving')
    if len(content) > MAX_CONTENT_BYTES or len(content.encode('utf-8')) > MAX_CONTENT_BYTES:
        raise ValueError('artifact content exceeds the 10 MiB limit')
    filename = config.get('filename', 'report.md')
    if not isinstance(filename, str) or not filename or len(filename) > 200 or any(
            char in filename for char in '/\\:') or filename in ('.', '..'):
        raise ValueError('artifact filename must be a bare filename, not a path')
    # The stored run supplies project identity, not arbitrary step inputs.
    from src.workflows.scope import validate_output_scope
    validate_output_scope(owner, project_id, session_id)
    with tempfile.TemporaryDirectory(prefix='faustus-workflow-artifact-') as temporary:
        Path(temporary, filename).write_text(content, encoding='utf-8')
        execution = ExecutionResult.parse({
            'run_id': run_id, 'backend': 'local', 'status': 'completed',
            'artifact_filenames': [filename],
        })
        collected = artifact_store.collect(execution, source_dir=temporary, owner=owner,
            project_id=project_id, skill_id='workflow.artifact', skill_version='1.0.0',
            provenance={'recipe': str(context.get('workflow') or ''),
                        'note': 'workflow node ' + node.id})
        if collected.skipped or not collected.artifacts:
            raise ValueError('the workflow artifact could not be collected')
        artifact_store.persist(collected.artifacts, session_id=session_id)
    artifacts = [item.to_dict() for item in collected.artifacts]
    return {'stored': True, 'artifact_ids': [item['id'] for item in artifacts],
            'artifacts': artifacts}
