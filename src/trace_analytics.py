"""Local summaries, deterministic budgets and content-free trace exports.

Reads the existing recorder; never starts a collector, judge or network call.
OTLP JSON: https://opentelemetry.io/docs/specs/otlp/#json-protobuf-encoding
"""
from __future__ import annotations
import hashlib
import math
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from src import llm_trace

MAX_RECORDS = 10000


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def read(session_id):
    records = []
    truncated = False
    for raw in llm_trace._iter_records(session_id):
        if not isinstance(raw, dict):
            continue
        if len(records) >= MAX_RECORDS:
            truncated = True
            break
        usage = llm_trace._summary_usage(raw.get('usage'))
        records.append({'seq': raw.get('seq'), 'run_id': str(raw.get('run_id') or 'unassigned')[:200],
            'session_scope': hashlib.sha256(str(session_id).encode()).hexdigest(),
            'model': str(raw.get('model') or 'unknown')[:200], 'ts': _number(raw.get('ts')),
            'duration_ms': _number(raw.get('duration_ms')), 'error': bool(raw.get('error')),
            'phase': str(raw.get('phase') or 'model')[:30],
            'input_tokens': usage.get('input_tokens', usage.get('prompt_tokens')),
            'output_tokens': usage.get('output_tokens', usage.get('completion_tokens')),
            'usage_source': usage.get('usage_source')})
    return records, truncated


def _percentile(values, fraction):
    if not values: return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(values) * fraction) - 1)]


def summarize(records, *, truncated=False):
    durations = [r['duration_ms'] for r in records if r['duration_ms'] is not None]
    totals, coverage = {}, {}
    for key in ('input_tokens', 'output_tokens'):
        observed = [r[key] for r in records if r[key] is not None]
        totals[key] = sum(observed) if observed else None
        coverage[key] = len(observed)
    models = {}
    for r in records:
        item = models.setdefault(r['model'], {'calls': 0, 'errors': 0})
        item['calls'] += 1
        item['errors'] += int(r['error'])
    return {'calls': len(records), 'errors': sum(r['error'] for r in records),
        'error_rate': sum(r['error'] for r in records) / len(records) if records else None,
        'duration_ms': {'observed': len(durations), 'p50': _percentile(durations, .5), 'p95': _percentile(durations, .95)},
        'tokens': totals, 'token_coverage': coverage, 'models': models,
        'usage_sources': sorted({r['usage_source'] for r in records if r['usage_source']}),
        'truncated': truncated, 'scope': 'recorded_model_calls', 'content_included': False}


class TraceBudgets(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    max_calls: int | None = Field(default=None, ge=1, le=100000)
    max_error_rate: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    max_p95_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    max_input_tokens: int | None = Field(default=None, ge=0)
    max_output_tokens: int | None = Field(default=None, ge=0)


def evaluate(records, budgets: TraceBudgets, *, truncated=False):
    summary = summarize(records, truncated=truncated)
    checks = []
    metrics = {'max_calls': (summary['calls'], bool(records)),
        'max_error_rate': (summary['error_rate'], bool(records)),
        'max_p95_ms': (summary['duration_ms']['p95'], summary['duration_ms']['observed'] == len(records) and bool(records)),
        'max_input_tokens': (summary['tokens']['input_tokens'], summary['token_coverage']['input_tokens'] == len(records) and bool(records)),
        'max_output_tokens': (summary['tokens']['output_tokens'], summary['token_coverage']['output_tokens'] == len(records) and bool(records))}
    for key, bound in budgets.model_dump(exclude_none=True).items():
        actual, complete = metrics[key]
        status = 'insufficient_data' if truncated or not complete else 'pass' if actual <= bound else 'fail'
        checks.append({'metric': key, 'limit': bound, 'observed': actual, 'status': status})
    return {'summary': summary, 'checks': checks, 'passed': bool(checks) and all(c['status'] == 'pass' for c in checks),
            'method': 'deterministic_budgets', 'evaluates_answer_quality': False}


def export(records, format: Literal['chrome', 'otlp'], *, truncated=False):
    if format not in ('chrome', 'otlp'): raise ValueError('format must be chrome or otlp')
    events, spans, skipped = [], [], 0
    for index, r in enumerate(records):
        if r['ts'] is None or r['duration_ms'] is None or r['ts'] < r['duration_ms'] / 1000:
            skipped += 1
            continue
        # The recorder timestamps completion, so derive start from duration.
        end_ns = round(r['ts'] * 1e9)
        start_ns = end_ns - round(r['duration_ms'] * 1e6)
        run = hashlib.sha256((r.get('session_scope', '') + ':' + r['run_id']).encode()).hexdigest()
        meta = {k: r[k] for k in ('model', 'seq', 'phase', 'input_tokens', 'output_tokens', 'usage_source') if r[k] is not None}
        meta['failed'] = r['error']
        if format == 'chrome':
            events.append({'name': r['model'], 'cat': r['phase'], 'ph': 'X', 'ts': start_ns / 1000,
                'dur': (end_ns - start_ns) / 1000, 'pid': 1, 'tid': int(run[:7], 16), 'args': meta})
        else:
            attrs = []
            for key, val in meta.items():
                if isinstance(val, bool): encoded = {'boolValue': val}
                elif isinstance(val, int): encoded = {'intValue': str(val)}
                elif isinstance(val, float): encoded = {'doubleValue': val}
                else: encoded = {'stringValue': str(val)}
                attrs.append({'key': 'faustus.' + key, 'value': encoded})
            spans.append({'traceId': run[:32], 'spanId': hashlib.sha256(f'{run}:{r["seq"]}:{index}'.encode()).hexdigest()[:16],
                'name': r['model'], 'kind': 3, 'startTimeUnixNano': str(start_ns), 'endTimeUnixNano': str(end_ns),
                'attributes': attrs, 'status': {'code': 2 if r['error'] else 1}})
    if format == 'chrome':
        return {'traceEvents': events, 'displayTimeUnit': 'ms',
                'faustus': {'truncated': truncated, 'skipped_incomplete_timing': skipped, 'content_included': False}}
    # Keep the OTLP wire object clean; export omissions are returned in HTTP headers.
    return {'resourceSpans': [{'resource': {'attributes': [{'key': 'service.name', 'value': {'stringValue': 'faustus'}}]},
             'scopeSpans': [{'scope': {'name': 'faustus.llm_trace', 'version': '1'}, 'spans': spans}]}]}
