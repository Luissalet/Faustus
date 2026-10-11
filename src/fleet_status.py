"""Publish scheduled-task outcomes and expected slots without prompts, results or credentials."""
from datetime import datetime, timezone
import re


def epoch(value):
    if not isinstance(value, datetime): return None
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).timestamp()


def label(value):
    text = " ".join(str(value or "").split())
    text = re.sub(r"(?i)\b(password|passwd|contraseña|api[_ -]?key|access[_ -]?token)\s*[:=]\s*\S+", r"\1 ***", text)
    return re.sub(r"\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{12,}", "***", text)[:120]


def build(db):
    from sqlalchemy import func
    from core.database import ScheduledTask, TaskRun
    from src.task_scheduler import compute_next_run, get_task_policy, guarded_task_ids, _resolve_task_timezone
    # Select metadata columns only; prompts, run output, error bodies and tokens stay in Faustus.
    success = dict(db.query(TaskRun.task_id, func.max(TaskRun.finished_at)).filter(
        TaskRun.status == 'success', TaskRun.finished_at.isnot(None)).group_by(TaskRun.task_id).all())
    errors = dict(db.query(TaskRun.task_id, func.max(TaskRun.finished_at)).filter(
        TaskRun.status == 'error', TaskRun.finished_at.isnot(None)).group_by(TaskRun.task_id).all())
    columns = [getattr(ScheduledTask, name) for name in ('id','name','created_at','status','trigger_type','schedule',
        'scheduled_time','scheduled_day','scheduled_date','timezone','cron_expression','crew_member_id','resumed_at')]
    tasks = db.query(*columns).order_by(ScheduledTask.id).limit(1001).all()
    guarded = guarded_task_ids()
    items = []
    for task in tasks[:1000]:
        ok, failed = epoch(success.get(task.id)), epoch(errors.get(task.id))
        created = epoch(task.created_at)
        resumed = epoch(getattr(task, 'resumed_at', None))
        recurring = task.trigger_type != 'event' and task.schedule in ('daily', 'weekly', 'monthly', 'cron')
        item = {'id': 'faustus:task:' + task.id, 'owner': 'faustus', 'kind': 'scheduled_task' if recurring else 'rule',
            'label': label(task.name), 'created_ts': created, 'enabled': task.status == 'active',
            'paused': task.status != 'active', 'last_success_ts': ok, 'last_error_ts': failed, 'resumed_ts': resumed,
            'last_status': 'error' if failed and (not ok or failed >= ok) else 'ok' if ok else None,
            'error_code': 'scheduled_run_failed' if failed and (not ok or failed >= ok) else None}
        if recurring:
            # The clock starts at the latest of: the last success, the creation and the last resume from a pause.
            values = [v for v in (ok, created, resumed) if v]
            baseline = max(values) if values else None
            try:
                after = datetime.fromtimestamp(baseline, timezone.utc).replace(tzinfo=None)
                policy = get_task_policy(task.id)
                if policy['dst_ambiguity_policy'] == 'run_all' or task.id in guarded:
                    item['cadence_unverified'] = True
                    item['expected_slots'] = []
                    items.append(item)
                    continue
                zone = _resolve_task_timezone(db, task)
                first = compute_next_run(task.schedule, task.scheduled_time, task.scheduled_day, task.scheduled_date,
                    after=after, cron_expression=task.cron_expression, tz_name=zone,
                    dst_ambiguity_policy=policy['dst_ambiguity_policy'])
                second = compute_next_run(task.schedule, task.scheduled_time, task.scheduled_day, task.scheduled_date,
                    after=first, cron_expression=task.cron_expression, tz_name=zone,
                    dst_ambiguity_policy=policy['dst_ambiguity_policy']) if first else None
                item['expected_slots'] = [epoch(first), epoch(second)] if first and second else []
                item['cadence_unverified'] = not bool(item['expected_slots'])
                item['schedule_basis'] = 'native_schedule_timezone_and_dst_policy'
            except Exception:
                item['expected_slots'] = []
                item['cadence_unverified'] = True
        items.append(item)
    return {'ok': True, 'owner': 'faustus', 'items': items,
        'coverage': {'outcomes': 'confirmed_finished_task_runs', 'truncated': len(tasks) > 1000,
                     'schedules': 'native_compute_next_run', 'event_tasks': 'no_cadence_inferred',
                     'one_off_tasks': 'no_recurring_cadence', 'run_all_dst': 'unverified',
                     'run_windows_and_period_keys': 'unverified', 'private_fields': 'not_published'}}
