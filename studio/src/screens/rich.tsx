import { t } from '../i18n';
import { Check, Copy, CornerDownLeft } from 'lucide-react';
import { useId, useMemo, useState, type ReactNode } from 'react';
import { findSensitive, getDisplay, stripEmojis, useDisplay } from '../shell/display';
import { parseMarkdown, type Block, type Footnote, type Inline } from '../lib/markdown';

/**
 * The transcript's reader. Parsing lives in lib/markdown.ts; this turns the
 * tree into React and keeps the two things only the shell knows about: the
 * blur (a secret in a reply becomes a click-to-reveal button) and the emoji
 * switch.
 */

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

function inlines(nodes: Inline[], key: string, uid: string): ReactNode[] {
  const out: ReactNode[] = [];
  nodes.forEach((node, i) => {
    const k = `${key}-${i}`;
    switch (node.kind) {
      case 'text':
        out.push(...plain(node.text, k));
        break;
      case 'break':
        out.push(<br key={k} />);
        break;
      case 'code':
        out.push(<code key={k}>{node.text}</code>);
        break;
      case 'strong':
        out.push(<strong key={k}>{inlines(node.children, k, uid)}</strong>);
        break;
      case 'em':
        out.push(<em key={k}>{inlines(node.children, k, uid)}</em>);
        break;
      case 'del':
        out.push(<del key={k}>{inlines(node.children, k, uid)}</del>);
        break;
      case 'image':
        out.push(<img key={k} className="fs-rich__img" src={node.src} alt={node.alt} loading="lazy" />);
        break;
      case 'note':
        out.push(
          <a key={k} className="fs-rich__ref" id={`${uid}-ref-${node.index}`} href={`#${uid}-note-${node.index}`} aria-label={t('Footnote {n}').replace('{n}', String(node.index))}>
            {node.index}
          </a>,
        );
        break;
      default:
        out.push(
          <a key={k} className="fs-link" href={node.href} target="_blank" rel="noreferrer">
            {inlines(node.children, k, uid)}
          </a>,
        );
    }
  });
  return out;
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

const HEADINGS = ['h1', 'h2', 'h3', 'h4', 'h5', 'h6'] as const;

function Item({ item, k, uid }: { item: { task?: boolean; done?: boolean; blocks: Block[] }; k: string; uid: string }) {
  const only = item.blocks.length === 1 && item.blocks[0].kind === 'para' ? item.blocks[0] : null;
  const body = only ? inlines(only.children, k, uid) : <Blocks blocks={item.blocks} k={k} uid={uid} />;
  if (!item.task) return <li className="fs-rich__item">{body}</li>;
  return (
    <li className="fs-rich__item fs-rich__task" data-done={item.done || undefined}>
      <input type="checkbox" checked={item.done ?? false} readOnly disabled aria-label={item.done ? t('Done') : t('Not done')} />
      <span>{body}</span>
    </li>
  );
}

function One({ block, k, uid }: { block: Block; k: string; uid: string }) {
  switch (block.kind) {
    case 'heading': {
      const H = HEADINGS[block.level - 1];
      return (
        <H className="fs-rich__h" data-level={block.level}>
          {inlines(block.children, k, uid)}
        </H>
      );
    }
    case 'code':
      return <CodeBlock lang={block.lang} code={block.code} />;
    case 'rule':
      return <hr className="fs-rich__rule" />;
    case 'quote':
      return (
        <blockquote className="fs-rich__quote">
          <Blocks blocks={block.blocks} k={k} uid={uid} />
        </blockquote>
      );
    case 'list': {
      const List = block.ordered ? 'ol' : 'ul';
      return (
        <List className="fs-rich__list" start={block.ordered && block.start !== 1 ? block.start : undefined}>
          {block.items.map((item, i) => (
            <Item key={i} item={item} k={`${k}-i${i}`} uid={uid} />
          ))}
        </List>
      );
    }
    case 'table':
      return (
        <div className="fs-rich__tablewrap" role="region" aria-label={t('Table')} tabIndex={0}>
          <table className="fs-rich__table">
            <thead>
              <tr>
                {block.head.map((cell, i) => (
                  <th key={i} data-align={block.align[i] ?? undefined} scope="col">
                    {inlines(cell, `${k}-h${i}`, uid)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((row, r) => (
                <tr key={r}>
                  {row.map((cell, c) => (
                    <td key={c} data-align={block.align[c] ?? undefined}>
                      {inlines(cell, `${k}-r${r}c${c}`, uid)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    default:
      return <p>{inlines(block.children, k, uid)}</p>;
  }
}

function Blocks({ blocks, k, uid }: { blocks: Block[]; k: string; uid: string }) {
  return (
    <>
      {blocks.map((block, i) => (
        <One key={i} block={block} k={`${k}-${i}`} uid={uid} />
      ))}
    </>
  );
}

function Notes({ notes, uid }: { notes: Footnote[]; uid: string }) {
  return (
    <section className="fs-rich__notes" aria-label={t('Footnotes')}>
      <ol>
        {notes.map((note) => (
          <li key={note.id} id={`${uid}-note-${note.index}`}>
            <Blocks blocks={note.blocks} k={`n${note.index}`} uid={uid} />
            <a className="fs-rich__back" href={`#${uid}-ref-${note.index}`} title={t('Back to the text')} aria-label={t('Back to the text')}>
              <CornerDownLeft size={12} aria-hidden="true" />
            </a>
          </li>
        ))}
      </ol>
    </section>
  );
}

export function Rich({ text: raw }: { text: string }) {
  const display = useDisplay();
  // useId gives ':r3:'; a colon in a fragment id is legal but awkward to link.
  const uid = useId().replace(/:/g, 'x');
  const text = display.emojis ? raw : stripEmojis(raw);
  const { blocks, footnotes } = useMemo(() => parseMarkdown(text), [text]);
  return (
    <div className="fs-rich">
      <Blocks blocks={blocks} k="b" uid={uid} />
      {footnotes.length > 0 && <Notes notes={footnotes} uid={uid} />}
    </div>
  );
}
