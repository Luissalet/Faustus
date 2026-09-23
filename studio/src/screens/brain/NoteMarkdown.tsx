import { FileQuestion, FileText } from 'lucide-react';
import { useMemo, type ReactNode } from 'react';
import { parseMarkdown, type Block, type Inline } from '../../lib/markdown';
import { parseWikiHref, rewriteWikilinks, type TitleIndex } from '../../lib/wikilinks';
import { t } from '../../i18n';

/**
 * The vault's reading view: the shared markdown parser (`lib/markdown.ts`)
 * plus `[[wikilinks]]`, rewritten to real link/image nodes by
 * `lib/wikilinks.ts` before parsing so `lib/markdown.ts` itself never has to
 * change (its `safeHref` already lets a `#…` fragment straight through).
 * An unresolved link gets its own style and creates the note on click; an
 * embed (`![[x]]`) becomes a small card instead of an `<img>`.
 */

function WikiLink({ href, children, onOpen, onCreate }: { href: string; children: ReactNode; onOpen: (path: string) => void; onCreate: (title: string) => void }) {
  const info = parseWikiHref(href);
  if (!info || (info.kind !== 'note' && info.kind !== 'note-new')) {
    return (
      <a className="fs-link" href={href} target="_blank" rel="noreferrer">
        {children}
      </a>
    );
  }
  const unresolved = info.kind === 'note-new';
  return (
    <button
      type="button"
      className="fs-brain__wikilink"
      data-unresolved={unresolved || undefined}
      title={unresolved ? t('This note does not exist yet — click to create it') : info.path}
      onClick={() => (unresolved ? onCreate(info.path) : onOpen(info.path))}
    >
      {children}
    </button>
  );
}

function EmbedCard({ src, alt, onOpen, onCreate }: { src: string; alt: string; onOpen: (path: string) => void; onCreate: (title: string) => void }) {
  const info = parseWikiHref(src);
  if (!info) return <img className="fs-brain__img" src={src} alt={alt} loading="lazy" />;
  const unresolved = info.kind === 'embed-new';
  return (
    <button type="button" className="fs-brain__embed" data-unresolved={unresolved || undefined} onClick={() => (unresolved ? onCreate(info.path) : onOpen(info.path))}>
      {unresolved ? <FileQuestion size={14} aria-hidden="true" /> : <FileText size={14} aria-hidden="true" />}
      <span>{alt || info.path}</span>
      <span className="fs-muted">{unresolved ? t('embed — does not exist yet') : t('embed')}</span>
    </button>
  );
}

function inlines(nodes: Inline[], key: string, onOpen: (path: string) => void, onCreate: (title: string) => void): ReactNode[] {
  return nodes.map((node, i) => {
    const k = `${key}-${i}`;
    switch (node.kind) {
      case 'text':
        return node.text;
      case 'break':
        return <br key={k} />;
      case 'code':
        return <code key={k}>{node.text}</code>;
      case 'strong':
        return <strong key={k}>{inlines(node.children, k, onOpen, onCreate)}</strong>;
      case 'em':
        return <em key={k}>{inlines(node.children, k, onOpen, onCreate)}</em>;
      case 'del':
        return <del key={k}>{inlines(node.children, k, onOpen, onCreate)}</del>;
      case 'image':
        return <EmbedCard key={k} src={node.src} alt={node.alt} onOpen={onOpen} onCreate={onCreate} />;
      case 'note':
        return <sup key={k}>[{node.index}]</sup>;
      case 'link':
        return (
          <WikiLink key={k} href={node.href} onOpen={onOpen} onCreate={onCreate}>
            {inlines(node.children, k, onOpen, onCreate)}
          </WikiLink>
        );
      default:
        return null;
    }
  });
}

const HEADINGS = ['h1', 'h2', 'h3', 'h4', 'h5', 'h6'] as const;

function renderBlock(block: Block, key: string, onOpen: (path: string) => void, onCreate: (title: string) => void): ReactNode {
  switch (block.kind) {
    case 'heading': {
      const H = HEADINGS[block.level - 1];
      return <H key={key}>{inlines(block.children, key, onOpen, onCreate)}</H>;
    }
    case 'code':
      return (
        <pre key={key} className="fs-brain__code" data-lang={block.lang || undefined}>
          <code>{block.code}</code>
        </pre>
      );
    case 'rule':
      return <hr key={key} />;
    case 'quote':
      return (
        <blockquote key={key}>
          {block.blocks.map((b, i) => renderBlock(b, `${key}-${i}`, onOpen, onCreate))}
        </blockquote>
      );
    case 'list': {
      const List = block.ordered ? 'ol' : 'ul';
      return (
        <List key={key} start={block.ordered && block.start !== 1 ? block.start : undefined}>
          {block.items.map((item, i) => (
            <li key={i} className={item.task ? 'fs-brain__task' : undefined}>
              {item.task && <input type="checkbox" checked={item.done ?? false} readOnly disabled />}
              {item.blocks.map((b, bi) => renderBlock(b, `${key}-${i}-${bi}`, onOpen, onCreate))}
            </li>
          ))}
        </List>
      );
    }
    case 'table':
      return (
        <div key={key} className="fs-brain__tablewrap">
          <table>
            <thead>
              <tr>
                {block.head.map((cell, i) => (
                  <th key={i} data-align={block.align[i] ?? undefined}>
                    {inlines(cell, `${key}-h${i}`, onOpen, onCreate)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((row, r) => (
                <tr key={r}>
                  {row.map((cell, c) => (
                    <td key={c} data-align={block.align[c] ?? undefined}>
                      {inlines(cell, `${key}-r${r}c${c}`, onOpen, onCreate)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    default:
      return <p key={key}>{inlines(block.children, key, onOpen, onCreate)}</p>;
  }
}

export function NoteMarkdown({ body, index, onOpenNote, onCreateNote }: { body: string; index: TitleIndex; onOpenNote: (path: string) => void; onCreateNote: (title: string) => void }) {
  const blocks = useMemo(() => parseMarkdown(rewriteWikilinks(body, index)).blocks, [body, index]);
  if (!body.trim()) return <p className="fs-muted">{t('This note is empty.')}</p>;
  return <div className="fs-brain__prose">{blocks.map((b, i) => renderBlock(b, `b${i}`, onOpenNote, onCreateNote))}</div>;
}
