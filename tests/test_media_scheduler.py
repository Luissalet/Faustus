from concurrent.futures import ThreadPoolExecutor

from src import media_runs
from src.media_scheduler import MediaScheduler
from src.media_backends import ComfyUIBackend, ComfyUIError
from tests.test_media_runs import world, ASK  # noqa: F401


def test_finished_engine_outputs_are_collected_without_a_browser(world):
    started = media_runs.start('image.product', ASK, owner='alice')
    world.finish(started['engine_job_id'])
    worker = MediaScheduler()
    assert worker.tick()[0]['status'] == 'completed'
    assert media_runs.get(started['run_id'])['artifact_ids']
    assert worker.tick() == []


def test_transient_download_failure_is_retryable_not_empty_success(world, monkeypatch):
    started = media_runs.start('image.product', ASK)
    world.finish(started['engine_job_id'])
    original = ComfyUIBackend.download
    def unavailable(*args, **kwargs):
        raise ComfyUIError('call_failed', 'temporary download failure')
    monkeypatch.setattr(ComfyUIBackend, 'download', unavailable)
    first = MediaScheduler().tick()[0]
    assert first['status'] == 'running' and first['collection_pending']
    assert 'temporary download failure' in first['reason']
    assert not first['artifact_ids']
    monkeypatch.setattr(ComfyUIBackend, 'download', original)
    done = MediaScheduler().tick()[0]
    assert done['status'] == 'completed' and done['artifact_ids']
    assert done['reason'] == ''


def test_late_poll_does_not_reverse_a_cancel(world, monkeypatch):
    started = media_runs.start('image.product', ASK)
    original = ComfyUIBackend.status
    cancelled = False
    def late(self, job):
        nonlocal cancelled
        response = original(self, job)
        if not cancelled:
            cancelled = True
            media_runs.cancel(started['run_id'])
        return response
    monkeypatch.setattr(ComfyUIBackend, 'status', late)
    result = media_runs.poll(started['run_id'])
    assert result['status'] == 'cancelled'
    assert not media_runs._update(started['run_id'], status='running')


def test_concurrent_polls_download_once_and_preserve_output_identity(world):
    started = media_runs.start('image.product', ASK)
    world.finish(started['engine_job_id'])
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: media_runs.poll(started['run_id']), range(4)))
    assert all(item['status'] == 'completed' for item in results)
    assert len({tuple(item['artifact_ids']) for item in results}) == 1
    assert len([call for call in world.calls if call[:2] == ('GET', '/view')]) == 1


def test_scheduler_and_manual_poll_do_not_condemn_an_inflight_submission(world, monkeypatch):
    original = ComfyUIBackend.submit
    observed = {}
    def inflight(self, plan, **kwargs):
        run = media_runs.recent()[0]
        assert run['status'] == 'submit_pending'
        observed['tick'] = MediaScheduler().tick()
        observed['poll'] = media_runs.poll(run['id'])
        observed['cancel'] = media_runs.cancel(run['id'])
        return original(self, plan, **kwargs)
    monkeypatch.setattr(ComfyUIBackend, 'submit', inflight)
    started = media_runs.start('image.product', ASK)
    assert started['ok']
    assert observed['tick'] == []
    assert observed['poll']['status'] == 'submit_pending'
    assert observed['cancel']['ok'] is False
    assert observed['cancel']['reason'] == 'submission_not_settled'


def test_scheduler_rotates_bounded_pages_and_stops_admitting_work(world):
    for _ in range(3):
        media_runs.start('image.product', ASK)
    worker = MediaScheduler(page_size=1)
    seen = {worker.tick()[0]['run_id'] for _ in range(3)}
    assert len(seen) == 3
    worker.stopped.set()
    assert worker.tick() == []


def test_one_unreadable_job_does_not_prevent_other_collection(world, monkeypatch):
    for _ in range(2):
        started = media_runs.start('image.product', ASK)
        world.finish(started['engine_job_id'])
    original = media_runs.poll
    first = MediaScheduler().candidates()[0]
    def broken(run_id):
        if run_id == first:
            raise ValueError('bad historical record')
        return original(run_id)
    monkeypatch.setattr(media_runs, 'poll', broken)
    results = MediaScheduler().tick()
    assert len(results) == 1 and results[0]['status'] == 'completed'


def test_reconciliation_cannot_erase_a_concurrently_recorded_submission(world, monkeypatch):
    from core.database import SessionLocal, MediaRunRow
    started = media_runs.start('image.product', ASK)
    with SessionLocal() as db:
        row = db.get(MediaRunRow, started['run_id'])
        row.status, row.engine_job_id = 'submit_unknown', None
        db.commit()
    def just_submitted(self, client_id):
        media_runs._update(started['run_id'], status='submitted', engine_job_id=started['engine_job_id'])
        return {'found': False}
    monkeypatch.setattr(ComfyUIBackend, 'find_by_client_id', just_submitted)
    reconciled = media_runs.reconcile_run(started['run_id'], grace_seconds=0)
    assert reconciled['changed'] is False
    current = media_runs.get(started['run_id'])
    assert current['status'] == 'submitted' and current['engine_job_id'] == started['engine_job_id']
