"""One-shot, read-only imports of legacy room records into canonical coordination."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

TABLES = {
    'council': ('council_sessions', 'council_participants', 'council_messages', 'council_turns',
                'council_tasks', 'council_claims', 'council_objections', 'council_decisions', 'council_idempotency', 'council_controls', 'council_closes'),
    'groups': ('groups', 'group_turns', 'group_steps'),
}


def import_legacy(target: Path, sources: dict[str, str]) -> dict:
    report = {}
    with sqlite3.connect(target, timeout=15) as out:
        out.execute('BEGIN IMMEDIATE')
        out.execute('CREATE TABLE IF NOT EXISTS coordination_imports (source TEXT PRIMARY KEY, kind TEXT NOT NULL, receipt TEXT NOT NULL)')
        for kind, raw_path in sources.items():
            if kind not in TABLES:
                raise ValueError('Unknown coordination repository')
            path = Path(raw_path).resolve()
            if path == target.resolve() or not path.is_file():
                report[kind] = {'source_present': path.is_file(), 'rows': 0}
                continue
            key = kind + ':' + str(path).casefold()
            prior = out.execute('SELECT receipt FROM coordination_imports WHERE source=?', (key,)).fetchone()
            if prior:
                report[kind] = {**json.loads(prior[0]), 'already_imported': True}
                continue
            receipt = {'rows': 0, 'tables': {}, 'source_present': True}
            # URI mode=ro prevents repairing, migrating or writing the original.
            with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=15) as source:
                source.execute('BEGIN')
                available = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table in TABLES[kind]:
                    if table not in available:
                        continue
                    columns = [r[1] for r in source.execute(f'PRAGMA table_info({table})')]
                    primary = [r[1] for r in source.execute(f'PRAGMA table_info({table})') if r[5]]
                    if table == 'council_messages':
                        primary = ['id']  # message UUID is canonical across independently allocated sequences
                    if table in ('group_turns', 'council_controls'):
                        primary = ['id']
                    sequence = 'seq' if table == 'council_messages' else 'ordinal' if table in ('group_turns', 'council_controls') else ''
                    compared = [c for c in columns if c != sequence]
                    digest = hashlib.sha256()
                    count = 0
                    for row in source.execute(f'SELECT * FROM {table}' + (f' ORDER BY {sequence}' if sequence else '')):
                        data = dict(zip(columns, row))
                        digest.update(json.dumps(data, sort_keys=True, ensure_ascii=False).encode('utf-8'))
                        existing = out.execute(f'SELECT {",".join(compared)} FROM {table} WHERE '
                                               + ' AND '.join(f'{key}=?' for key in primary),
                                               tuple(data[key] for key in primary)).fetchone()
                        if existing is not None:
                            if tuple(existing) != tuple(data[c] for c in compared):
                                raise ValueError(f'Conflicting legacy identity in {table}; no records replaced')
                        else:
                            out.execute(f'INSERT INTO {table} ({",".join(compared)}) VALUES({",".join("?" for _ in compared)})',
                                        tuple(data[c] for c in compared))
                        count += 1
                    receipt['tables'][table] = {'rows': count, 'sha256': digest.hexdigest()}
                    receipt['rows'] += count
            out.execute('INSERT INTO coordination_imports(source,kind,receipt) VALUES(?,?,?)',
                        (key, kind, json.dumps(receipt, ensure_ascii=False)))
            report[kind] = receipt
    return report
