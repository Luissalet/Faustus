import { asArray, getJson } from './api';
import { listSkills } from './skills';
import { locale, t, tn } from '../i18n';

/**
 * The slash commands that are a question to the server and an answer on the
 * screen: backups, the scorecard, the Deep Research profile, AGENTS.md, the
 * indexed folders, the endpoints, a shell command, a search.
 *
 * Each one returns **Markdown**, because the transcript now has a reader
 * that draws it (lib/markdown.ts). The previous interface built HTML by
 * hand in every handler; a table here is three lines of pipes.
 */

async function post<T>(path: string, body: unknown = {}): Promise<T> {
  const response = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return (await response.json()) as T;
}

const num = (v: unknown, fallback = 0): number => (typeof v === 'number' && Number.isFinite(v) ? v : fallback);
const str = (v: unknown): string => (typeof v === 'string' ? v : '');
const row = (cells: (string | number)[]) => `| ${cells.join(' | ')} |`;

export function bytes(value: unknown): string {
  const n = num(value);
  if (n <= 0) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
}

function age(hours: unknown): string {
  const h = num(hours, -1);
  if (h < 0) return '—';
  if (h < 1) return t('{n} min ago', { n: Math.max(1, Math.round(h * 60)) });
  if (h < 48) return t('{n} h ago', { n: Math.round(h) });
  return t('{n} days ago', { n: Math.round(h / 24) });
}

/* ── /backup ── */

interface Snapshot {
  name?: string;
  bytes?: number;
  age_hours?: number;
}

export async function backupList(): Promise<string> {
  const data = await getJson<Record<string, unknown>>('/api/backup/snapshots');
  const list = asArray<Snapshot>(data.snapshots);
  const status = (data.status ?? {}) as Record<string, unknown>;
  const where = str(status.backup_dir) || '—';
  if (!list.length) return t('No snapshots yet. They land in `{dir}`. `/backup now` takes one.', { dir: where });
  const lines = [
    t('**{n} snapshots** · newest {when} · {size} in `{dir}`', {
      n: String(list.length),
      when: age(list[0]?.age_hours),
      size: bytes(status.total_bytes),
      dir: where,
    }),
    '',
    row([t('Snapshot'), t('Size'), t('Taken')]),
    '| --- | ---: | --- |',
    ...list.slice(0, 12).map((s) => row([`\`${str(s.name)}\``, bytes(s.bytes), age(s.age_hours)])),
    '',
    t('`/backup now` takes one · `/backup verify N` checks that snapshot N would really restore. Restoring is manual and destructive: stop Faustus, then `python scripts/odysseus-backup restore <file> --yes`.'),
  ];
  return lines.join('\n');
}

export async function backupNow(): Promise<string> {
  const d = await post<Record<string, unknown>>('/api/backup/snapshot');
  const verified = (d.verified ?? {}) as Record<string, unknown>;
  if (!d.ok) {
    const problems = asArray<string>(verified.problems);
    return t('Snapshot written, but it does **not** verify: {why}', { why: problems.join('; ') || str(d.error) || t('unknown reason') });
  }
  const databases = asArray<unknown>(verified.databases).length;
  const pruned = asArray<unknown>(d.pruned).length;
  return (
    t('Snapshot `{name}` — {files} files, {size}, {seconds}s. Verified: {n} databases pass integrity_check.', {
      name: str(d.name),
      files: String(num(d.files)),
      size: bytes(d.bytes),
      seconds: String(num(d.seconds)),
      n: String(databases),
    }) + (pruned ? ` ${t('Pruned {n} old ones.', { n: String(pruned) })}` : '')
  );
}

export async function backupVerify(index: number): Promise<string> {
  const data = await getJson<Record<string, unknown>>('/api/backup/snapshots');
  const list = asArray<Snapshot>(data.snapshots);
  const target = list[Math.max(1, index) - 1];
  if (!target?.name) return t('No such snapshot. `/backup` lists them.');
  const d = await post<Record<string, unknown>>('/api/backup/verify', { name: target.name });
  const databases = asArray<{ name?: string; ok?: boolean; detail?: string }>(d.databases);
  const lines = [
    `### \`${target.name}\``,
    '',
    d.ok ? t('**Would restore** — {n} entries.', { n: String(num(d.members)) }) : t('**Would NOT restore** — {n} entries.', { n: String(num(d.members)) }),
  ];
  if (databases.length) {
    lines.push('', row([t('Database'), t('Check')]), '| --- | --- |');
    for (const db of databases) lines.push(row([`\`${str(db.name)}\``, db.ok ? 'ok' : `**${str(db.detail)}**`]));
  }
  for (const problem of asArray<string>(d.problems)) lines.push('', `- ${problem}`);
  if (str(d.restore_command)) lines.push('', `\`${str(d.restore_command)}\``);
  return lines.join('\n');
}

/* ── /scorecard ── */

export async function scorecard(days: number, workspace?: string): Promise<string> {
  const query = new URLSearchParams({ days: String(days), language: locale().startsWith('es') ? 'es' : 'en' });
  if (workspace) query.set('workspace', workspace);
  const d = await getJson<Record<string, unknown>>(`/api/scorecard/table?${query}`);
  const head = t(
    '**Model scorecard** — {turns} agent turns{scope} over the last {days} days. Verified = the harness confirmed the claims; asks = the model asked instead of guessing; tests and review = the project tests and the independent diff review, when they ran.',
    { turns: String(num(d.turns)), scope: workspace ? ` (\`${workspace}\`)` : '', days: String(days) },
  );
  const markdown = str(d.markdown).trim();
  return `${head}\n\n${markdown || t('No agent turns in that window yet.')}\n\n${t('`/scorecard 7` · `/scorecard here` · `/scorecard clear`.')}`;
}

export async function scorecardClear(): Promise<string> {
  const response = await fetch('/api/scorecard', { method: 'DELETE', credentials: 'same-origin' });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return t('Scorecard cleared.');
}

/* ── /researchfit ── */

export async function researchFit(): Promise<string> {
  const d = await getJson<Record<string, unknown>>('/api/research/preset');
  const gpu = str(d.gpu_name);
  const vram = d.vram_gb ? `${num(d.vram_gb)} GB VRAM` : '';
  const hardware = [gpu, vram].filter(Boolean).join(' · ') || t('GPU not detected');
  const changes = asArray<{ key?: string; from?: unknown; to?: unknown }>(d.changes);
  const blockers = asArray<{ text?: string; fix_label?: string }>(d.blockers);
  const label = (key: unknown) => str(key).replace(/^research_/, '').replace(/_seconds$/, ' (s)').replace(/_/g, ' ');
  const lines = [`### ${t('Profile "{tier}"', { tier: str(d.tier) })}`, '', hardware, '', str(d.note)];
  if (changes.length) {
    lines.push('', row([t('Setting'), t('Now'), t('Would be')]), '| --- | ---: | ---: |');
    for (const c of changes) lines.push(row([`\`${label(c.key)}\``, String(c.from), `**${String(c.to)}**`]));
    lines.push('', t('`/researchfit apply` writes them.'));
  } else {
    lines.push('', t('Settings already match this profile.'));
  }
  if (blockers.length) {
    lines.push('', `#### ${t('What would make a run come back empty')}`, '');
    for (const b of blockers) lines.push(`- ${str(b.text)}${b.fix_label ? ` — *${str(b.fix_label)}*` : ''}`);
  }
  return lines.join('\n');
}

export async function researchFitApply(includeFixes: boolean): Promise<string> {
  const d = await post<Record<string, unknown>>('/api/research/preset/apply', { include_fixes: includeFixes });
  const written = Object.keys((d.written ?? {}) as Record<string, unknown>);
  if (!written.length) return t('Nothing to change: the settings already match.');
  return t('Applied the "{tier}" profile — {n} settings updated{fixes}.', {
    tier: str(d.tier),
    n: String(written.length),
    fixes: includeFixes ? t(', search provider included') : '',
  });
}

/* ── /agentsmd ── */

export async function agentsMd(workspace: string, write: boolean): Promise<string> {
  const d = await post<Record<string, unknown>>('/api/workspace/instructions/draft', {
    workspace,
    write,
    language: locale().startsWith('es') ? 'es' : 'en',
  });
  const text = str(d.text);
  let head: string;
  if (d.written) head = t('**AGENTS.md written** to `{path}`. It goes into every agent turn in this folder from now on; edit it freely.', { path: str(d.path) });
  else if (d.exists) head = t('This folder already has an instructions file (`{existing}`). Here is a fresh draft for reference; nothing was written.', { existing: str(d.existing) });
  else head = t('Draft AGENTS.md for `{ws}`. `/agentsmd write` saves it.', { ws: workspace });
  return `${head}\n\n\`\`\`markdown\n${text}\n\`\`\``;
}

/* ── /rag ── */

export async function ragList(): Promise<string> {
  const d = await getJson<Record<string, unknown>>('/api/personal');
  const dirs = asArray<unknown>(d.directories).map((x) => (typeof x === 'string' ? x : str((x as Record<string, unknown>).path)));
  const files = asArray<Record<string, unknown>>(d.files);
  if (!dirs.length && !files.length) return t('Nothing indexed yet. `/rag add /path` indexes a folder.');
  const lines: string[] = [];
  if (dirs.length) {
    lines.push(`#### ${tn(dirs.length, '{n} folder', '{n} folders')}`, '');
    for (const dir of dirs) lines.push(`- \`${dir}\``);
  }
  if (files.length) {
    lines.push('', `#### ${tn(files.length, '{n} file', '{n} files')}`, '');
    for (const f of files.slice(0, 30)) lines.push(`- ${str(f.name) || str(f.path)}`);
    if (files.length > 30) lines.push(t('…and {n} more.', { n: String(files.length - 30) }));
  }
  return lines.join('\n');
}

export async function ragAdd(directory: string): Promise<string> {
  const d = await post<Record<string, unknown>>('/api/personal/add_directory', { directory });
  return t('Indexed `{dir}` ({n} files).', { dir: directory, n: String(num(d.indexed_count)) });
}

export async function ragRemove(directory: string): Promise<string> {
  const response = await fetch(`/api/personal/remove_directory?directory=${encodeURIComponent(directory)}`, {
    method: 'DELETE',
    credentials: 'same-origin',
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return t('`{dir}` is no longer indexed.', { dir: directory });
}

/* ── /ping and /probe ── */

export async function ping(): Promise<string> {
  const d = await getJson<Record<string, unknown>>('/api/ping');
  const endpoints = asArray<Record<string, unknown>>(d.endpoints);
  if (!endpoints.length) return t('No model endpoints configured. `/setup local URL` adds one.');
  const lines = [row([t('Endpoint'), t('State'), t('Latency'), t('Models')]), '| --- | --- | ---: | ---: |'];
  for (const e of endpoints) {
    const up = str(e.status) === 'online';
    const latency = e.latency_ms == null ? '—' : `${num(e.latency_ms)} ms`;
    const detail = str(e.error) ? ` — ${str(e.error).slice(0, 60)}` : '';
    lines.push(row([str(e.name), (up ? t('up') : t('down')) + detail, latency, String(num(e.model_count))]));
  }
  return lines.join('\n');
}

export interface ProbeRow {
  endpoint: string;
  model: string;
  ok: boolean;
  detail: string;
}

/** `/api/probe` streams NDJSON, one line per model. */
export async function probe(endpointId?: string, onRow?: (rows: ProbeRow[]) => void): Promise<ProbeRow[]> {
  const url = endpointId ? `/api/probe?endpoint_id=${encodeURIComponent(endpointId)}` : '/api/probe';
  const response = await fetch(url, { credentials: 'same-origin' });
  if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const rows: ProbeRow[] = [];
  let buffer = '';
  let endpoint = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() ?? '';
    for (const line of lines) {
      if (!line.trim()) continue;
      let event: Record<string, unknown>;
      try {
        event = JSON.parse(line) as Record<string, unknown>;
      } catch {
        continue;
      }
      if (str(event.endpoint)) endpoint = str(event.endpoint);
      if (!str(event.model)) continue;
      rows.push({
        endpoint,
        model: str(event.model),
        ok: event.ok === true || str(event.status) === 'ok',
        detail: str(event.error) || str(event.detail) || (event.latency_ms != null ? `${num(event.latency_ms)} ms` : ''),
      });
      onRow?.(rows);
    }
  }
  return rows;
}

export function probeMarkdown(rows: ProbeRow[], done: boolean): string {
  if (!rows.length) return done ? t('No model answered.') : t('Asking each model for one token…');
  const ok = rows.filter((r) => r.ok).length;
  const lines = [
    done ? t('**{ok} of {n} models answered.**', { ok: String(ok), n: String(rows.length) }) : t('Asking each model for one token… ({n} so far)', { n: String(rows.length) }),
    '',
    row([t('Endpoint'), t('Model'), t('Answers')]),
    '| --- | --- | --- |',
    ...rows.map((r) => row([r.endpoint || '—', `\`${r.model}\``, r.ok ? `${t('yes')}${r.detail ? ` · ${r.detail}` : ''}` : `${t('no')}${r.detail ? ` · ${r.detail}` : ''}`])),
  ];
  return lines.join('\n');
}

export async function endpointIdByName(query: string): Promise<string | null> {
  const list = asArray<Record<string, unknown>>(await getJson<unknown>('/api/model-endpoints'));
  const needle = query.toLowerCase();
  const hit = list.find((e) => str(e.name).toLowerCase() === needle) ?? list.find((e) => str(e.name).toLowerCase().includes(needle));
  return hit ? str(hit.id) : null;
}

/* ── /sh ── */

export async function shellExec(command: string): Promise<string> {
  const d = await post<Record<string, unknown>>('/api/shell/exec', { command });
  const out = [str(d.stdout), str(d.stderr)].filter(Boolean).join('\n') || t('(no output)');
  const code = d.exit_code == null ? '?' : String(num(d.exit_code));
  return `\`\`\`\n$ ${command}\n${out}\n[exit ${code}]\n\`\`\``;
}

/* ── /find and /stats ── */

export async function findInChats(query: string): Promise<string> {
  const d = await getJson<Record<string, unknown>>(`/api/search?q=${encodeURIComponent(query)}&limit=20`);
  const hits = asArray<Record<string, unknown>>(d.results ?? d);
  if (!hits.length) return t('Nothing matches "{q}".', { q: query });
  const lines = [tn(hits.length, '{n} match', '{n} matches'), ''];
  for (const hit of hits) {
    const id = str(hit.session_id) || str(hit.id);
    const title = str(hit.session_name) || str(hit.title) || t('Untitled');
    const snippet = str(hit.content_snippet) || str(hit.snippet) || str(hit.content) || str(hit.text);
    const who = str(hit.role) === 'assistant' ? t('the model') : t('you');
    lines.push(`- [${title}](/studio?s=${encodeURIComponent(id)}) — *${who}*: ${snippet.replace(/\s+/g, ' ').slice(0, 160)}`);
  }
  return lines.join('\n');
}

export async function dbStats(): Promise<string> {
  const d = await getJson<Record<string, unknown>>('/api/db/stats');
  const entries = Object.entries(d).filter(([, v]) => typeof v === 'number' || typeof v === 'string');
  if (!entries.length) return '';
  return [row([t('In the database'), t('Count')]), '| --- | ---: |', ...entries.map(([k, v]) => row([k.replace(/_/g, ' '), String(v)]))].join('\n');
}

/* ── /skills ── */

export async function skillsMarkdown(query: string): Promise<string> {
  if (query) {
    const d = await post<Record<string, unknown>>('/api/skills/search', { query });
    const found = asArray<Record<string, unknown>>(d.skills);
    if (!found.length) return t('No skills match "{q}".', { q: query });
    return [row([t('Skill'), t('What it does')]), '| --- | --- |', ...found.map((s) => row([`\`${str(s.name) || str(s.id)}\``, str(s.description)]))].join('\n');
  }
  const list = await listSkills();
  if (!list.length) return t('No skills yet. [Skills](/skills) is where they live.');
  const lines = [row([t('Skill'), t('State'), t('What it does')]), '| --- | --- | --- |'];
  for (const s of list) lines.push(row([`\`${s.name}\``, s.status || '—', (s.description ?? '').slice(0, 120)]));
  lines.push('', t('`/skills query` searches them; [Skills](/skills) is the screen.'));
  return lines.join('\n');
}

export async function reloadSkills(): Promise<string> {
  const list = await listSkills();
  return tn(list.length, 'Reloaded: {n} skill.', 'Reloaded: {n} skills.');
}
