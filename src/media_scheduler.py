"""Collect already-submitted renders without an open chat or browser.

Never submits a graph. Unknown submissions reconcile with a grace period so
the scheduler cannot mistake an in-flight POST for a missing GPU job.
"""
import asyncio
import logging
import threading

from src import media_runs

logger = logging.getLogger(__name__)


class MediaScheduler:
    def __init__(self, *, page_size=20):
        self.page_size = max(1, min(int(page_size), 100))
        self.cursor = ''
        self.stopped = threading.Event()

    def candidates(self):
        from core.database import SessionLocal, MediaRunRow
        with SessionLocal() as db:
            query = db.query(MediaRunRow.id).filter(MediaRunRow.status.notin_(
                ('completed', 'failed', 'cancelled')))
            rows = query.filter(MediaRunRow.id > self.cursor).order_by(
                MediaRunRow.id).limit(self.page_size).all()
            if not rows and self.cursor:
                self.cursor = ''
                rows = query.order_by(MediaRunRow.id).limit(self.page_size).all()
            return [row[0] for row in rows]

    def tick(self):
        results = []
        for run_id in self.candidates():
            if self.stopped.is_set():
                break
            self.cursor = run_id
            try:
                record = media_runs.get(run_id)
                if not record:
                    continue
                if record['status'] in media_runs.UNSETTLED_SUBMIT:
                    media_runs.reconcile_run(run_id, grace_seconds=60, record=record)
                    record = media_runs.get(run_id) or record
                if record.get('engine_job_id'):
                    results.append(media_runs.poll(run_id))
            except Exception:
                logger.exception('Media continuation failed for %s', run_id)
        return results


async def scheduler_loop(*, interval=10, scheduler=None):
    worker = scheduler or MediaScheduler()
    try:
        while not worker.stopped.is_set():
            try:
                await asyncio.to_thread(worker.tick)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('Media continuation tick failed')
            await asyncio.sleep(max(0.1, interval))
    finally:
        worker.stopped.set()
