"""Continue explicitly started workflows without keeping a browser tab open.

This is an application worker, not a creator of recurring user tasks. Pending
drafts never start here. Human gates remain paused; due clock waits and active
runs use the same durable engine as the HTTP advance button. One bounded page
per tick and a rotating cursor prevent old paused runs starving newer ones.
"""
from __future__ import annotations

import asyncio
import logging
import threading

from src.contracts.base import now_iso
from .engine import WorkflowEngine
from .store import WorkflowStore
from .clock import due

logger = logging.getLogger(__name__)


class WorkflowScheduler:
    def __init__(self, store=None, handlers=None, *, page_size=50, max_nodes=10):
        self.store = store or WorkflowStore()
        from src.workflows.runtime import production_handlers
        self.engine = WorkflowEngine(production_handlers() if handlers is None else handlers, self.store)
        self.page_size = max(1, min(100, int(page_size)))
        self.max_nodes = max(1, min(50, int(max_nodes)))
        self.cursor = ''
        self.stopped = threading.Event()

    def candidates(self):
        from core.database import SessionLocal, WorkflowRunRow
        with SessionLocal() as db:
            query = db.query(WorkflowRunRow.id).filter(
                WorkflowRunRow.status.in_(('running', 'paused')))
            rows = query.filter(WorkflowRunRow.id > self.cursor).order_by(
                WorkflowRunRow.id).limit(self.page_size).all()
            if not rows and self.cursor:
                self.cursor = ''
                rows = query.order_by(WorkflowRunRow.id).limit(self.page_size).all()
            return [row[0] for row in rows]

    def tick(self):
        if self.stopped.is_set():
            return []
        self.store.recover_expired_node_leases()
        results = []
        for run_id in self.candidates():
            if self.stopped.is_set():
                break
            self.cursor = run_id
            try:
                loaded = self.store.get_run(run_id)
                if not loaded or loaded['run'].status not in ('running', 'paused'):
                    continue
                if loaded['run'].status == 'paused':
                    # A timer cannot answer a human approval. Only due timed
                    # nodes wake here; approvals use the existing resume route.
                    clock_ready = any(state.status == 'paused' and
                              (state.result or {}).get('wake_at') and
                              due(state.result['wake_at'], now_iso())
                              for state in self.store.node_runs(run_id).values())
                    if not clock_ready:
                        continue
                results.append(self.engine.advance(run_id, max_nodes=self.max_nodes,
                                                   should_stop=self.stopped.is_set))
            except Exception:
                # One malformed historical definition cannot stall all runs.
                logger.exception('Workflow continuation failed for %s', run_id)
        return results


async def scheduler_loop(*, interval=5, scheduler=None):
    worker = scheduler or WorkflowScheduler()
    try:
        while not worker.stopped.is_set():
            try:
                await asyncio.to_thread(worker.tick)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('Workflow continuation tick failed')
            await asyncio.sleep(max(0.1, interval))
    finally:
        # Cancelling to_thread does not kill its handler. Stop admission before
        # shutdown; the in-flight result is fenced and no successor can start.
        worker.stopped.set()
