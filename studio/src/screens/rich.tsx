import { t } from '../i18n';
import { Check, Copy } from 'lucide-react';
import { Fragment, useState, type ReactNode } from 'react';
import { findSensitive, getDisplay, stripEmojis, useDisplay } from '../shell/display';

/**
 * A deliberately small reader for what models actually write: fenced
 * code, inline code, bold, headings, lists, links and paragraphs.
 *
 * Not a Markdown implementation. A full one costs 40–90 KB of the 350 KB
 * budget (DECISIONES_UI.md) to handle tables and footnotes that a chat
 * reply almost never contains. Everything unknown falls through as text,
 * which is the correct failure for a transcript: nothing is ever hidden.
 */

const INLINE = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\[[^\]\n]+\]\((https?:\/\/[^\s)]+)\))/g;

function inline(text: string, keyPrefix: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let index = 0;
  for (const match of text.matchAll(INLINE)) {
    const start = match.index ?? 0;
    if (start > last) out.push(...plain(text.slice(last, start), `${keyPrefix}-p${index}`));
    const token = match[0];
    const key = `${keyPrefix}-${index++}`;
    if (token.startsWith('`')) {
      out.push(<code key={key}>{token.slice(1, -1)}</code>);
    } else if (token.startsWith('**')) {
      out.push(<strong key={key}>{token.slice(2, -2)}</strong>);
    } else {
      const label = token.slice(1, token.indexOf(']('));
      out.push(
        <a key={key} className="fs-link" href={match[2]} target="_blank" rel="noreferrer">
          {label}
        </a>,
      );
    }
    last = start + token.length;
  }
  if (last < text.length) out.push(...plain(text.slice(last), `${keyPrefix}-pend`));
  return out;
}

/** A run of plain text; with the blur on, its secrets become reveal-on-click buttons. */
function plain(text: string, key: string): ReactNode[] {
  if (!getDisplay().blur) return [text];
  return findSensitive(text).map((run, i) =>
    run.sensitive ? <Censored key={`${key}-${i}`} text={run.text} /> : run.text,
  );
}

function Censored({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <button type="button" className="fs-censor" data-open={open || undefined} title={open ? t('Hide again') : t('Blurred: click to reveal')} onClick={() => setOpen((o) => !o)}>
      {text}
    </button>
  );
}

function prose(block: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const lines = block.split('\n');
  let i = 0;
  let k = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.trim() === '') {
      i++;
      continue;
    }
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      nodes.push(
        <p key={`${keyPrefix}-h${k++}`} className="fs-rich__heading">
          {inline(heading[2], `${keyPrefix}-hi${k}`)}
        </p>,
      );
      i++;
      continue;
    }
    const bullet = /^\s*(?:[-*•]|\d+[.)])\s+/;
    if (bullet.test(line)) {
      const items: string[] = [];
      const ordered = /^\s*\d+[.)]/.test(line);
      while (i < lines.length && bullet.test(lines[i])) {
        items.push(lines[i].replace(bullet, ''));
        i++;
      }
      const List = ordered ? 'ol' : 'ul';
      nodes.push(
        <List key={`${keyPrefix}-l${k++}`} className="fs-rich__list">
          {items.map((item, j) => (
            <li key={j}>{inline(item, `${keyPrefix}-li${k}-${j}`)}</li>
          ))}
        </List>,
      );
      continue;
    }
    const para: string[] = [];
    while (i < lines.length && lines[i].trim() !== '' && !bullet.test(lines[i]) && !/^#{1,6}\s/.test(lines[i])) {
      para.push(lines[i]);
      i++;
    }
    nodes.push(
      <p key={`${keyPrefix}-p${k++}`}>
        {para.map((text, j) => (
          <Fragment key={j}>
            {j > 0 && <br />}
            {inline(text, `${keyPrefix}-pi${k}-${j}`)}
          </Fragment>
        ))}
      </p>,
    );
  }
  return nodes;
}

function CodeBlock({ lang, code }: { lang: string; code: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="fs-rich__codewrap">
      <pre className="fs-rich__code" data-lang={lang || undefined}>
        <code>{code}</code>
      </pre>
      <button
        type="button"
        className="fs-rich__copy"
        aria-label={copied ? t('Copied') : t('Copy code')}
        title={copied ? t('Copied') : t('Copy code')}
        onClick={() => {
          navigator.clipboard
            .writeText(code)
            .then(() => {
              setCopied(true);
              setTimeout(() => setCopied(false), 1400);
            })
            .catch(() => undefined);
        }}
      >
        {copied ? <Check size={13} aria-hidden="true" /> : <Copy size={13} aria-hidden="true" />}
      </button>
    </div>
  );
}

export function Rich({ text: raw }: { text: string }) {
  const display = useDisplay();
  const text = display.emojis ? raw : stripEmojis(raw);
  const parts = text.split(/```/);
  return (
    <div className="fs-rich">
      {parts.map((part, index) => {
        if (index % 2 === 1) {
          const firstBreak = part.indexOf('\n');
          const lang = firstBreak === -1 ? '' : part.slice(0, firstBreak).trim();
          const code = firstBreak === -1 ? part : part.slice(firstBreak + 1);
          return <CodeBlock key={index} lang={lang} code={code.replace(/\n$/, '')} />;
        }
        return <Fragment key={index}>{prose(part, `b${index}`)}</Fragment>;
      })}
    </div>
  );
}
