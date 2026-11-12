import type { ConnectedEvent, ResearchSource } from '../../adapters/research';

export interface SourceGroup {
  key: string;
  name: string;
  appId?: string;
}

export function sourceGroup(sourceId: string, appId?: string, name?: string): SourceGroup {
  if (sourceId === 'project_files') return { key: sourceId, name: 'Project' };
  if (appId) return { key: 'app:' + appId, name: appId.charAt(0).toUpperCase() + appId.slice(1), appId };
  return { key: sourceId, name: name || 'Sources' };
}

export function groupEntries<T>(entries: T[], groupFor: (entry: T) => SourceGroup) {
  const groups = new Map<string, SourceGroup & { entries: T[] }>();
  for (const entry of entries) {
    const group = groupFor(entry);
    let bucket = groups.get(group.key);
    if (!bucket) {
      bucket = { ...group, entries: [] };
      groups.set(group.key, bucket);
    }
    bucket.entries.push(entry);
  }
  return [...groups.values()];
}

export function sourceGroups(events: ConnectedEvent[], sources: ResearchSource[]) {
  // Older runs only recorded the connector's app id on its dockable results.
  const apps = new Map(sources.filter(s => s.appId && s.sourceId).map(s => [s.sourceId, s.appId]));
  return new Map(events.map(event => [
    event.source,
    sourceGroup(event.source, event.app_id || apps.get(event.source), event.source_name),
  ]));
}

export interface NumberedSource {
  number: number;
  source: ResearchSource;
}

export function splitConnectedBibliography(report: string, sources: ResearchSource[]) {
  const byUrl = new Map(sources.map(source => [source.url, source]));
  const headings = /^## (?:Fuentes|Sources|Quellen|Fontes|Fonti)[ \t]*\r?$/gm;
  const row = /[ \t\r\n]*(\d+)\. \[([\s\S]*?)\]\((https?:\/\/\S+?)\)(?: — [^\r\n]+)?[ \t]*(?:\r?\n|$)/y;
  for (const heading of report.matchAll(headings)) {
    const start = heading.index + heading[0].length;
    row.lastIndex = start;
    const citations: NumberedSource[] = [];
    let end = start;
    let known = false;
    let lastNumber = 0;
    // The old writer allowed line breaks and brackets inside a title. Match
    // the generated rows against their original URLs before replacing them.
    for (let match = row.exec(report); match; match = row.exec(report)) {
      const [, rawNumber, title, url] = match;
      const number = Number(rawNumber);
      if (!Number.isSafeInteger(number) || number <= lastNumber) break;
      const source = byUrl.get(url);
      known ||= !!source;
      citations.push({ number, source: source ?? { title, url } });
      lastNumber = number;
      end = row.lastIndex;
      const tail = report.slice(end).trimStart();
      if (!tail || /^#{1,2} /.test(tail)) {
        if (known) return { before: report.slice(0, heading.index).trimEnd(), after: tail, citations };
        break;
      }
      if (!/^\d+\. \[/.test(tail)) break;
    }
  }
  return { before: report, after: '', citations: [] };
}

export function connectedTitle(source: ResearchSource) {
  const [line, ...rest] = (source.title || source.url).trim().split(/\r?\n/);
  const label = line
    .replace(/!?\[([^\]]+)\]\([^)]+\)/g, '$1')
    .replace(/^\s{0,3}(?:#{1,6}|>|[-+*])\s+/, '')
    .replace(/([*_]{1,3})(\S(?:.*?\S)?)\1/g, '$2')
    .replace(/[\x60*]/g, '')
    .replace(/[\[\]]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
  const chars = Array.from(label || source.url);
  return chars.length > 96 || rest.length ? chars.slice(0, 95).join('').trimEnd() + '…' : chars.join('');
}

export function sourceLocation(source: ResearchSource) {
  const url = URL.canParse(source.url) ? new URL(source.url) : null;
  if (!url) return {};
  const number = (key: string) => {
    const value = url.searchParams.get(key);
    return value && /^\d{1,10}$/.test(value) ? value : null;
  };
  if (source.sourceId === 'project_files' || url.pathname === '/api/research/project-source') {
    return { line: number('line'), chunk: number('chunk') };
  }
  return { page: source.appId === 'kafka' ? number('page') : null };
}
