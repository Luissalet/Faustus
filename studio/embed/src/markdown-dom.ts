/**
 * The agent's replies as formatted text inside the Shadow DOM: Studio's own Markdown parser (studio/src/lib/markdown.ts,
 * a pure tree with no dependencies) turned into DOM nodes. No HTML from the model is ever parsed: every node is created
 * here and every text goes in as text, links and images pass the parser's own safety filters, and links open in a new
 * tab without an opener. Unknown syntax still arrives as plain text.
 */
import { parseMarkdown, safeHref, safeSrc, type Block, type Inline } from '../../src/lib/markdown';

function inline(nodes: Inline[], into: Node): void {
  for (const node of nodes) {
    switch (node.kind) {
      case 'text': into.appendChild(document.createTextNode(node.text)); break;
      case 'break': into.appendChild(document.createElement('br')); break;
      case 'code': { const code = document.createElement('code'); code.textContent = node.text; into.appendChild(code); break; }
      case 'strong': case 'em': case 'del': {
        const tag = document.createElement(node.kind === 'strong' ? 'strong' : node.kind === 'em' ? 'em' : 'del');
        inline(node.children, tag);
        into.appendChild(tag);
        break;
      }
      case 'link': {
        // Only the web and mail: a relative path or a workspace file means nothing inside a host app's page.
        const href = safeHref(node.href);
        if (!/^(?:https?:\/\/|mailto:)/i.test(href)) { inline(node.children, into); break; }
        const a = document.createElement('a');
        a.href = href;
        a.target = '_blank';
        a.rel = 'noopener noreferrer';
        inline(node.children, a);
        into.appendChild(a);
        break;
      }
      case 'image': {
        const src = safeSrc(node.src);
        if (!/^(?:https?:\/\/|data:image\/)/i.test(src)) { into.appendChild(document.createTextNode(node.alt)); break; }
        const img = document.createElement('img');
        img.src = src;
        img.alt = node.alt;
        img.loading = 'lazy';
        into.appendChild(img);
        break;
      }
      case 'note': { const sup = document.createElement('sup'); sup.textContent = String(node.index); into.appendChild(sup); break; }
    }
  }
}

function blocks(list: Block[], into: Node): void {
  for (const block of list) {
    switch (block.kind) {
      case 'heading': {
        const h = document.createElement('div');
        h.className = 'fc-md-h';
        h.setAttribute('role', 'heading');
        h.setAttribute('aria-level', String(block.level));
        h.dataset.level = String(block.level);
        inline(block.children, h);
        into.appendChild(h);
        break;
      }
      case 'para': { const p = document.createElement('p'); inline(block.children, p); into.appendChild(p); break; }
      case 'code': {
        const pre = document.createElement('pre');
        const code = document.createElement('code');
        if (block.lang) code.dataset.lang = block.lang;
        code.textContent = block.code;
        pre.appendChild(code);
        into.appendChild(pre);
        break;
      }
      case 'list': {
        const list = document.createElement(block.ordered ? 'ol' : 'ul');
        if (block.ordered && block.start !== 1) list.setAttribute('start', String(block.start));
        for (const item of block.items) {
          const li = document.createElement('li');
          if (item.task !== undefined) {
            li.className = 'fc-md-task';
            li.appendChild(document.createTextNode(item.done ? '☑ ' : '☐ '));
          }
          blocks(item.blocks, li);
          list.appendChild(li);
        }
        into.appendChild(list);
        break;
      }
      case 'quote': { const q = document.createElement('blockquote'); blocks(block.blocks, q); into.appendChild(q); break; }
      case 'rule': into.appendChild(document.createElement('hr')); break;
      case 'table': {
        const wrap = document.createElement('div');
        wrap.className = 'fc-md-table';
        const table = document.createElement('table');
        const head = document.createElement('tr');
        block.head.forEach((cell, i) => {
          const th = document.createElement('th');
          if (block.align[i]) th.style.textAlign = block.align[i]!;
          inline(cell, th);
          head.appendChild(th);
        });
        const thead = document.createElement('thead');
        thead.appendChild(head);
        const tbody = document.createElement('tbody');
        for (const row of block.rows) {
          const tr = document.createElement('tr');
          row.forEach((cell, i) => {
            const td = document.createElement('td');
            if (block.align[i]) td.style.textAlign = block.align[i]!;
            inline(cell, td);
            tr.appendChild(td);
          });
          tbody.appendChild(tr);
        }
        table.append(thead, tbody);
        wrap.appendChild(table);
        into.appendChild(wrap);
        break;
      }
    }
  }
}

/** `text` (the model's Markdown) rendered into `target`, replacing what it held. */
export function renderMarkdownInto(target: HTMLElement, text: string): void {
  const parsed = parseMarkdown(text || '');
  const fragment = document.createDocumentFragment();
  blocks(parsed.blocks, fragment);
  if (parsed.footnotes.length) {
    const notes = document.createElement('ol');
    notes.className = 'fc-md-notes';
    for (const note of parsed.footnotes) {
      const li = document.createElement('li');
      li.value = note.index;
      blocks(note.blocks, li);
      notes.appendChild(li);
    }
    fragment.appendChild(notes);
  }
  target.replaceChildren(fragment);
}

/** A tool's name as a person reads it: `mcp__<server>__screen_status` → `screen_status`. */
export function toolLabel(name: unknown): string {
  const raw = String(name ?? '').trim();
  const match = /^mcp__[^_](?:[^_]|_(?!_))*__(.+)$/.exec(raw);
  return match ? match[1] : raw;
}
