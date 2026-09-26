/**
 * Style check for prose (radar #339): marks the patterns that make a text
 * read as machine-written — stock openers and closers, "not only… but",
 * "it's not X, it's Y", the one-line rhetorical question, dash-heavy
 * paragraphs, stacked hedges — in Spanish and English. It only points; it
 * never rewrites. Pure and synchronous, so the editor can run it on every
 * open of the panel.
 */

export interface ProseIssue {
  rule: string;
  /** Short English label; the UI translates it. */
  label: string;
  start: number;
  end: number;
  excerpt: string;
}

interface Rule {
  id: string;
  label: string;
  pattern: RegExp;
}

const PHRASES_ES = [
  'cabe destacar', 'cabe señalar', 'cabe mencionar', 'es importante destacar', 'es importante señalar',
  'es importante mencionar', 'es importante tener en cuenta', 'es crucial', 'es fundamental destacar',
  'en el mundo actual', 'en el panorama actual', 'en la era digital', 'hoy en día', 'en conclusión',
  'en resumen,', 'sin lugar a dudas', 'sin duda alguna', 'a la hora de', 'un mundo de posibilidades',
  'sumergirnos en', 'adentrarnos en', 'profundizar en', 'desbloquear', 'juega un papel crucial',
  'juega un papel fundamental', 'en constante evolución', 'dicho esto', 'vale la pena mencionar',
  'en última instancia', 'navegar por las complejidades',
];

const PHRASES_EN = [
  "it's worth noting", 'it is worth noting', 'it is important to note', "it's important to note",
  "in today's", 'in the digital age', 'in conclusion', 'in summary,', 'at the end of the day',
  'delve into', 'delves into', 'tapestry', 'navigate the complexities', 'ever-evolving',
  'game-changer', 'game changer', 'unlock the', 'elevate your', 'seamless', 'plays a crucial role',
  'plays a pivotal role', 'a testament to', 'in the realm of', 'without a doubt', 'that being said',
  'needless to say', 'embark on',
];

function phraseRule(id: string, label: string, phrases: string[]): Rule {
  const alt = phrases
    .map((p) => p.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/'/g, "['’]"))
    .sort((a, b) => b.length - a.length)
    .join('|');
  return { id, label, pattern: new RegExp(`(?<![\\p{L}])(?:${alt})(?![\\p{L}])`, 'giu') };
}

const RULES: Rule[] = [
  phraseRule('stock-phrase', 'Stock phrase', [...PHRASES_ES, ...PHRASES_EN]),
  {
    id: 'not-only-but',
    label: '"Not only… but"',
    pattern: /\b(?:no (?:solo|sólo|solamente|únicamente)\b[^.;:\n]{1,90}?\bsino\b|not only\b[^.;:\n]{1,90}?\bbut\b)/giu,
  },
  {
    id: 'not-x-its-y',
    label: '"It\'s not X, it\'s Y"',
    pattern: /\b(?:no es (?:solo |sólo )?[^.;:\n]{1,40}?[,;—–-]\s*es\b|it['’]?s not (?:just |only )?[^.;:\n]{1,40}?[,;—–-]\s*it['’]?s\b|this isn['’]t [^.;:\n]{1,40}?[,;—–-]\s*it['’]?s\b)/giu,
  },
  {
    id: 'rhetorical-question',
    label: 'Rhetorical question answered at once',
    pattern: /(?:¿(?:El|La|Los|Las|Y el|Y la|Lo mejor|Lo peor|El resultado|La clave|La razón|El truco)[^?\n]{0,25}\?|\bThe (?:result|catch|kicker|answer|twist|reason|best part|key)\?)(?=\s+\p{Lu})/gu,
  },
  {
    id: 'opener',
    label: 'Chatty opener',
    pattern: /^(?:¡(?:Claro|Por supuesto|Excelente pregunta|Buena pregunta|Genial)[^!\n]{0,20}!|(?:Great question|Certainly|Absolutely|Of course|Sure thing)[!.])/gimu,
  },
  {
    id: 'hedge-stack',
    label: 'Stacked hedges',
    pattern: /\b(?:podría potencialmente|quizás posiblemente|tal vez podría|might potentially|could potentially|may possibly|perhaps possibly)\b/giu,
  },
];

const DASH_PARAGRAPH_MIN = 3;

function excerptOf(text: string, start: number, end: number): string {
  const lo = Math.max(0, start - 20);
  const hi = Math.min(text.length, end + 20);
  return `${lo > 0 ? '…' : ''}${text.slice(lo, hi).replace(/\s+/g, ' ').trim()}${hi < text.length ? '…' : ''}`;
}

/** Every issue, in document order. Code fences are skipped. */
export function lintProse(text: string, max = 300): ProseIssue[] {
  const src = text || '';
  const fenced: [number, number][] = [];
  const fence = /^```[^\n]*\n[\s\S]*?^```/gm;
  for (let m = fence.exec(src); m; m = fence.exec(src)) fenced.push([m.index, m.index + m[0].length]);
  const inFence = (i: number) => fenced.some(([a, b]) => i >= a && i < b);
  const out: ProseIssue[] = [];
  for (const rule of RULES) {
    rule.pattern.lastIndex = 0;
    for (let m = rule.pattern.exec(src); m; m = rule.pattern.exec(src)) {
      if (m[0].length === 0) {
        rule.pattern.lastIndex += 1;
        continue;
      }
      if (inFence(m.index)) continue;
      out.push({ rule: rule.id, label: rule.label, start: m.index, end: m.index + m[0].length, excerpt: excerptOf(src, m.index, m.index + m[0].length) });
    }
  }
  // Paragraphs leaning on dashes: one issue per paragraph, at its first dash.
  let pos = 0;
  for (const para of src.split(/\n\s*\n/)) {
    const dashes = [...para.matchAll(/ — |—/g)];
    if (dashes.length >= DASH_PARAGRAPH_MIN && !inFence(pos)) {
      const at = pos + (dashes[0].index ?? 0);
      out.push({ rule: 'dash-heavy', label: 'Many dashes in one paragraph', start: at, end: at + 1, excerpt: excerptOf(src, at, at + 1) });
    }
    pos += para.length;
    const gap = /^\n\s*\n/.exec(src.slice(pos));
    pos += gap ? gap[0].length : 0;
  }
  out.sort((a, b) => a.start - b.start);
  return out.slice(0, max);
}

/** Issue counts per label, largest first, for the panel's summary line. */
export function proseSummary(issues: ProseIssue[]): { label: string; count: number }[] {
  const by = new Map<string, number>();
  for (const i of issues) by.set(i.label, (by.get(i.label) ?? 0) + 1);
  return [...by.entries()].map(([label, count]) => ({ label, count })).sort((a, b) => b.count - a.count);
}
