import { AlertTriangle, Archive, ArrowLeft, Bold, Check, ChevronDown, Code, Copy, Download, Eye, FileCode2, FileText, Heading1, Heading2, Heading3, History as HistoryIcon, Italic, Link2, List, ListChecks, ListOrdered, Mail, Minus, Play, Quote, Search, Strikethrough, Table, Trash2, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { Button, Dialog, EmptyState, IconButton, Menu, Skeleton, Toast } from '../../components';
import { archiveDoc, deleteDoc, exportPdfBlob, getDoc, listDocVersions, prepareSignedReply, renameDoc, restoreDocVersion, runOnServer, saveDoc, type Doc, type DocVersion } from '../../adapters/documents';
import { relativeTime } from '../../adapters/home';
import { isPdfDoc } from '../../lib/pdfDoc';
import { locale, t, tn } from '../../i18n';
import { Rich } from '../rich';
import { leaveComposeHandoff } from '../../adapters/email';
import { DiffView } from './DiffView';
import { baseName, download, toDocx, toHtml } from './exports';
import { applyMarkdown, parseCsv, PREVIEWABLE, RUNNABLE, type MdAction } from './markdown';
import { PdfPane } from './PdfPane';
import '../documents.css';

const LANGUAGES = ['markdown', 'text', 'python', 'javascript', 'typescript', 'html', 'css', 'json', 'yaml', 'bash', 'sql', 'csv', 'rust', 'go', 'java', 'c', 'cpp', 'ruby', 'php', 'xml', 'toml', 'ini'];

/**
 * The document editor (`/documents/{id}`): the side panel's editor grown
 * into a screen. Markdown toolbar, find and replace, line numbers, preview
 * (rendered Markdown, a CSV table, live HTML), run for scripts, versions
 * with a word-level review, export to file/HTML/PDF/DOCX, send as email,
 * and for PDF-backed documents the pages with fields, annotations and
 * signatures.
 */
export function DocumentScreen() {
  const { id = '' } = useParams();
  const navigate = useNavigate();
  const [doc, setDoc] = useState<Doc | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [text, setText] = useState('');
  const [title, setTitle] = useState('');
  const [language, setLanguage] = useState('');
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<{ text: string; tone: 'ok' | 'warn' } | null>(null);
  const [view, setView] = useState<'edit' | 'preview' | 'split' | 'pdf'>('edit');
  const [find, setFind] = useState<{ open: boolean; q: string; r: string; at: number }>({ open: false, q: '', r: '', at: 0 });
  const [versions, setVersions] = useState<DocVersion[] | null>(null);
  const [compare, setCompare] = useState<DocVersion | null>(null);
  const [run, setRun] = useState<{ out: string; err: boolean; html?: string } | null>(null);
  const [running, setRunning] = useState(false);
  const [confirm, setConfirm] = useState<'delete' | 'archive' | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const editorRef = useRef<HTMLTextAreaElement>(null);
  const gutterRef = useRef<HTMLDivElement>(null);
  const noticeTimer = useRef(0);

  const say = useCallback((msg: string, tone: 'ok' | 'warn' = 'ok') => {
    setNotice({ text: msg, tone });
    window.clearTimeout(noticeTimer.current);
    noticeTimer.current = window.setTimeout(() => setNotice(null), tone === 'warn' ? 7000 : 4000);
  }, []);

  useEffect(() => {
    let cancelled = false;
    getDoc(id)
      .then((d) => {
        if (cancelled) return;
        setDoc(d);
        setText(d.content);
        setTitle(d.title);
        setLanguage(d.language);
        setView(isPdfDoc(d.content) ? 'pdf' : 'edit');
      })
      .catch((e: Error) => !cancelled && setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [id]);

  const dirty = !!doc && text !== doc.content;
  const pdf = !!doc && isPdfDoc(doc.content);
  const lang = (language || doc?.language || '').toLowerCase();

  /* ── Save ── */
  const save = useCallback(
    async (content = text, summary?: string) => {
      if (!doc) return;
      setSaving(true);
      try {
        const saved = await saveDoc(doc.id, content, summary);
        setDoc(saved);
        setText(saved.content);
        say(t('Saved as v{n}', { n: saved.versionCount }));
      } catch (e) {
        say(t('Save failed: {error}', { error: (e as Error).message }), 'warn');
      } finally {
        setSaving(false);
      }
    },
    [doc, text, say],
  );

  const rename = async () => {
    if (!doc) return;
    const next = title.trim();
    if ((!next || next === doc.title) && language === doc.language) return;
    try {
      const saved = await renameDoc(doc.id, next || doc.title, language || undefined);
      setDoc((d) => (d ? { ...d, title: saved.title, language: saved.language } : d));
      setTitle(saved.title);
      document.title = `${saved.title} — Faustus`;
    } catch (e) {
      say((e as Error).message, 'warn');
    }
  };

  useEffect(() => {
    if (doc) document.title = `${doc.title} — Faustus`;
  }, [doc]);

  useEffect(() => {
    const onLeave = (e: BeforeUnloadEvent) => {
      if (dirty) e.preventDefault();
    };
    window.addEventListener('beforeunload', onLeave);
    return () => window.removeEventListener('beforeunload', onLeave);
  }, [dirty]);

  /* ── Toolbar ── */
  const format = (action: MdAction) => {
    const ta = editorRef.current;
    if (!ta) return;
    const out = applyMarkdown(action, text, ta.selectionStart, ta.selectionEnd);
    setText(out.text);
    requestAnimationFrame(() => {
      ta.focus();
      ta.setSelectionRange(out.start, out.end);
    });
  };

  /* ── Find & replace ── */
  const matches = useMemo(() => {
    if (!find.q) return [] as number[];
    const out: number[] = [];
    const hay = text.toLowerCase(), needle = find.q.toLowerCase();
    let i = hay.indexOf(needle);
    while (i >= 0 && out.length < 5000) {
      out.push(i);
      i = hay.indexOf(needle, i + needle.length);
    }
    return out;
  }, [text, find.q]);

  const jump = (delta: number) => {
    if (!matches.length) return;
    const at = (find.at + delta + matches.length) % matches.length;
    setFind((f) => ({ ...f, at }));
    const ta = editorRef.current;
    if (ta) {
      ta.focus();
      ta.setSelectionRange(matches[at], matches[at] + find.q.length);
      const line = text.slice(0, matches[at]).split('\n').length;
      ta.scrollTop = Math.max(0, (line - 4) * lineHeight(ta));
    }
  };

  const replaceOne = () => {
    if (!matches.length) return;
    const at = matches[Math.min(find.at, matches.length - 1)];
    setText(text.slice(0, at) + find.r + text.slice(at + find.q.length));
  };

  const replaceAll = () => {
    if (!find.q) return;
    const re = new RegExp(find.q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
    const n = matches.length;
    setText(text.replace(re, find.r));
    say(tn(n, '{n} replaced', '{n} replaced'));
  };

  /* ── Run ── */
  const runIt = async () => {
    if (!text.trim()) return;
    if (lang === 'html') {
      setView('preview');
      return;
    }
    setRunning(true);
    setRun(null);
    try {
      if (lang === 'javascript' || lang === 'js') {
        setRun(await runJs(text));
      } else if (lang === 'python' || lang === 'py') {
        const r = await runOnServer(text, 'python');
        setRun({ out: [r.stderr, r.stdout].filter(Boolean).join('\n') || (r.exitCode ? t('(no output) — exit code {n}', { n: r.exitCode }) : t('(no output)')), err: !!r.stderr || r.exitCode !== 0 });
      } else if (['bash', 'sh', 'shell', 'zsh'].includes(lang)) {
        const r = await runOnServer(text, 'bash');
        setRun({ out: [r.stderr, r.stdout].filter(Boolean).join('\n') || (r.exitCode ? t('(no output) — exit code {n}', { n: r.exitCode }) : t('(no output)')), err: !!r.stderr || r.exitCode !== 0 });
      }
    } catch (e) {
      setRun({ out: (e as Error).message, err: true });
    } finally {
      setRunning(false);
    }
  };

  /* ── Export ── */
  const exportAs = async (kind: 'file' | 'html' | 'pdf' | 'docx') => {
    if (!doc) return;
    const base = baseName(title || doc.title);
    setBusy(kind);
    try {
      if (dirty) await save();
      if (kind === 'file') download(new Blob([text], { type: 'text/plain' }), `${base}${extFor(lang)}`);
      else if (kind === 'html') download(new Blob([toHtml(title, text)], { type: 'text/html' }), `${base}.html`);
      else if (kind === 'docx') download(await toDocx(text), `${base}.docx`);
      else {
        const out = await exportPdfBlob(doc.id);
        download(out.blob, out.filename || `${base}.pdf`);
      }
    } catch (e) {
      say((e as Error).message, 'warn');
    } finally {
      setBusy(null);
    }
  };

  const sendAsEmail = async () => {
    if (!doc) return;
    if (pdf && doc.sourceEmail) {
      setBusy('reply');
      try {
        if (dirty) await save();
        const r = await prepareSignedReply(doc.id);
        leaveComposeHandoff({ to: r.reply.to, subject: r.reply.subject, body: '', inReplyTo: r.reply.inReplyTo, references: r.reply.references, attachmentToken: r.attachment.token, attachmentName: r.attachment.filename });
        navigate('/email?compose=handoff');
      } catch (e) {
        say((e as Error).message, 'warn');
      } finally {
        setBusy(null);
      }
      return;
    }
    if (dirty) await save();
    leaveComposeHandoff({ subject: title, body: text });
    navigate('/email?compose=handoff');
  };

  /* ── Keyboard ── */
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const ctrl = e.ctrlKey || e.metaKey;
      if (ctrl && e.key.toLowerCase() === 's') {
        e.preventDefault();
        if (dirty) void save();
      } else if (ctrl && e.key.toLowerCase() === 'f' && view !== 'pdf') {
        e.preventDefault();
        setFind((f) => ({ ...f, open: true }));
        window.setTimeout(() => document.getElementById('doc-find')?.focus(), 30);
      } else if (e.key === 'Escape' && find.open) {
        setFind((f) => ({ ...f, open: false }));
      } else if (ctrl && e.key === 'b' && lang === 'markdown' && document.activeElement === editorRef.current) {
        e.preventDefault();
        format('bold');
      } else if (ctrl && e.key === 'i' && lang === 'markdown' && document.activeElement === editorRef.current) {
        e.preventDefault();
        format('italic');
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dirty, save, view, find.open, lang, text]);

  if (error) {
    return (
      <div className="fs-screen">
        <EmptyState icon={FileText} title={t('Could not open the document')} body={error} primaryAction={{ label: t('Back to the library'), onClick: () => navigate('/library?type=documento') }} />
      </div>
    );
  }
  if (!doc) {
    return (
      <div className="fs-docs" data-loading>
        <Skeleton label={t('Loading the document')} count={8} height="20px" />
      </div>
    );
  }

  const canPreview = PREVIEWABLE.has(lang);
  const canRun = RUNNABLE.has(lang);
  const lines = text.split('\n').length;
  const md = lang === 'markdown' || lang === '' || lang === 'md';

  return (
    <div className="fs-docs" data-testid="document" data-view={view}>
      <header className="fs-docs__top">
        <IconButton icon={ArrowLeft} label={t('Back to the library')} onClick={() => navigate(doc.sessionId ? `/studio?s=${encodeURIComponent(doc.sessionId)}` : '/library?type=documento')} />
        <input className="fs-docs__title" value={title} onChange={(e) => setTitle(e.target.value)} onBlur={() => void rename()} onKeyDown={(e) => e.key === 'Enter' && (e.target as HTMLInputElement).blur()} aria-label={t('Document title')} />
        <select className="fs-field fs-docs__lang" value={language} onChange={(e) => setLanguage(e.target.value)} onBlur={() => void rename()} aria-label={t('Language')}>
          {!LANGUAGES.includes(language) && <option value={language}>{language || t('text')}</option>}
          {LANGUAGES.map((l) => (
            <option key={l} value={l}>
              {l}
            </option>
          ))}
        </select>
        <span className="fs-docs__facts">
          v{doc.versionCount} · {tn(lines, '{n} line', '{n} lines')}
          {doc.updatedAt && ` · ${relativeTime(doc.updatedAt)}`}
          {dirty && <span className="fs-docs__dirty" title={t('Unsaved changes')} />}
        </span>
        <span className="fs-spacer" />
        {!pdf && canPreview && (
          <div className="fs-seg" role="radiogroup" aria-label={t('View')}>
            {(['edit', 'split', 'preview'] as const).map((v) => (
              <button key={v} type="button" role="radio" aria-checked={view === v} onClick={() => setView(v)}>
                {v === 'edit' ? t('Edit') : v === 'split' ? t('Both') : t('Preview')}
              </button>
            ))}
          </div>
        )}
        {pdf && (
          <div className="fs-seg" role="radiogroup" aria-label={t('View')}>
            <button type="button" role="radio" aria-checked={view === 'pdf'} onClick={() => setView('pdf')}>
              {t('Pages')}
            </button>
            <button type="button" role="radio" aria-checked={view !== 'pdf'} onClick={() => setView('edit')}>
              {t('Text')}
            </button>
          </div>
        )}
        {canRun && <Button size="sm" variant="ghost" icon={Play} label={t('Run')} loading={running} onClick={() => void runIt()} title={t('Python and bash run on the server; JavaScript runs in a sandbox here; HTML opens the preview')} />}
        {view !== 'pdf' && <IconButton icon={Search} label={t('Find and replace (Ctrl+F)')} onClick={() => { setFind((f) => ({ ...f, open: !f.open })); window.setTimeout(() => document.getElementById('doc-find')?.focus(), 30); }} />}
        <IconButton
          icon={HistoryIcon}
          label={t('Versions')}
          onClick={() => {
            if (versions) setVersions(null);
            else listDocVersions(doc.id).then(setVersions).catch((e: Error) => say(e.message, 'warn'));
          }}
        />
        <Menu
          align="end"
          trigger={<Button size="sm" variant="ghost" label={t('Export')} icon={ChevronDown} iconPosition="right" />}
          items={[
            { label: t('Download as a file'), icon: Download, onSelect: () => void exportAs('file') },
            { label: t('HTML page'), icon: FileCode2, onSelect: () => void exportAs('html') },
            { label: pdf ? t('Filled PDF') : t('PDF'), icon: FileText, onSelect: () => void exportAs('pdf') },
            { label: t('Word (.docx)'), icon: FileText, onSelect: () => void exportAs('docx') },
            null,
            { label: t('Copy the text'), icon: Copy, onSelect: () => void navigator.clipboard.writeText(text).then(() => say(t('Copied'))) },
          ]}
        />
        <Button size="sm" variant="ghost" icon={Mail} label={pdf && doc.sourceEmail ? t('Sign and reply') : t('Send as email')} loading={busy === 'reply'} onClick={() => void sendAsEmail()} />
        <Menu
          align="end"
          trigger={<Button size="sm" variant="ghost" label={t('More')} icon={ChevronDown} iconPosition="right" />}
          items={[
            ...(doc.sessionId ? [{ label: t('Open its chat'), onSelect: () => navigate(`/studio?s=${encodeURIComponent(doc.sessionId ?? '')}&doc=${encodeURIComponent(doc.id)}`) }] : []),
            { label: t('Archive'), icon: Archive, onSelect: () => setConfirm('archive') },
            { label: t('Delete'), icon: Trash2, variant: 'danger', onSelect: () => setConfirm('delete') },
          ]}
        />
        <Button size="sm" variant="primary" icon={Check} label={dirty ? t('Save') : t('Saved')} disabled={!dirty} loading={saving} onClick={() => void save()} testId="doc-save" />
      </header>

      {view !== 'pdf' && md && (
        <div className="fs-docs__tools" role="toolbar" aria-label={t('Formatting')}>
          {(
            [
              ['bold', Bold, t('Bold (Ctrl+B)')],
              ['italic', Italic, t('Italic (Ctrl+I)')],
              ['strike', Strikethrough, t('Strikethrough')],
              ['code', Code, t('Inline code')],
              ['h1', Heading1, t('Heading 1')],
              ['h2', Heading2, t('Heading 2')],
              ['h3', Heading3, t('Heading 3')],
              ['quote', Quote, t('Quote')],
              ['ul', List, t('Bulleted list')],
              ['ol', ListOrdered, t('Numbered list')],
              ['task', ListChecks, t('Task list')],
              ['codeblock', FileCode2, t('Code block')],
              ['link', Link2, t('Link')],
              ['table', Table, t('Table')],
              ['hr', Minus, t('Horizontal rule')],
            ] as [MdAction, typeof Bold, string][]
          ).map(([action, Icon, label]) => (
            <IconButton key={action} icon={Icon} label={label} size="sm" onClick={() => format(action)} />
          ))}
        </div>
      )}

      {find.open && view !== 'pdf' && (
        <div className="fs-docs__find" role="search">
          <input id="doc-find" className="fs-field" value={find.q} placeholder={t('Find')} onChange={(e) => setFind((f) => ({ ...f, q: e.target.value, at: 0 }))} onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); jump(e.shiftKey ? -1 : 1); } }} />
          <span className="fs-docs__find-n">{find.q ? (matches.length ? `${Math.min(find.at + 1, matches.length)} / ${matches.length}` : t('No matches')) : ''}</span>
          <IconButton icon={ChevronDown} label={t('Next')} size="sm" onClick={() => jump(1)} disabled={!matches.length} />
          <input className="fs-field" value={find.r} placeholder={t('Replace with')} onChange={(e) => setFind((f) => ({ ...f, r: e.target.value }))} aria-label={t('Replace with')} />
          <Button size="sm" variant="ghost" label={t('Replace')} disabled={!matches.length} onClick={replaceOne} />
          <Button size="sm" variant="ghost" label={t('Replace all')} disabled={!matches.length} onClick={replaceAll} />
          <IconButton icon={X} label={t('Close')} size="sm" onClick={() => setFind((f) => ({ ...f, open: false }))} />
        </div>
      )}

      <div className="fs-docs__body">
        {compare ? (
          <DiffView
            oldText={text}
            newText={compare.content}
            oldLabel={t('Current')}
            newLabel={`v${compare.number}`}
            onCancel={() => setCompare(null)}
            onApply={(merged) => {
              setText(merged);
              setCompare(null);
              say(t('Changes applied to the editor; save to keep them.'));
            }}
          />
        ) : view === 'pdf' ? (
          <PdfPane
            docId={doc.id}
            title={doc.title}
            content={text}
            onChange={(mdText) => {
              setText(mdText);
              void save(mdText, t('Form and annotations'));
            }}
            say={say}
          />
        ) : (
          <>
            {view !== 'preview' && (
              <div className="fs-docs__editor-wrap">
                <div ref={gutterRef} className="fs-docs__gutter" aria-hidden="true">
                  {Array.from({ length: lines }, (_, i) => (
                    <span key={i}>{i + 1}</span>
                  ))}
                </div>
                <textarea
                  ref={editorRef}
                  className="fs-docs__editor"
                  value={text}
                  onChange={(e) => setText(e.target.value)}
                  onScroll={(e) => {
                    if (gutterRef.current) gutterRef.current.scrollTop = e.currentTarget.scrollTop;
                  }}
                  spellCheck={md}
                  wrap={md ? 'soft' : 'off'}
                  aria-label={t('Document content')}
                  data-testid="doc-editor"
                />
              </div>
            )}
            {view !== 'edit' && (
              <div className="fs-docs__preview">
                {lang === 'csv' ? (
                  <CsvTable text={text} />
                ) : lang === 'html' ? (
                  <iframe className="fs-docs__html" sandbox="allow-scripts" srcDoc={text} title={t('HTML preview')} />
                ) : (
                  <div className="fs-prose">
                    <Rich text={text} />
                  </div>
                )}
              </div>
            )}
          </>
        )}

        {versions && !compare && (
          <aside className="fs-docs__versions" aria-label={t('Versions')}>
            <header>
              <h3>{t('Versions')}</h3>
              <IconButton icon={X} label={t('Close')} size="sm" onClick={() => setVersions(null)} />
            </header>
            <ul>
              {versions.map((v) => (
                <li key={v.id} data-current={v.number === doc.versionCount || undefined}>
                  <div>
                    <strong>v{v.number}</strong> · {v.source === 'user' ? t('you') : t('agent')}
                    {v.createdAt && ` · ${new Date(v.createdAt).toLocaleString(locale(), { dateStyle: 'short', timeStyle: 'short' })}`}
                    {v.summary && <p>{v.summary}</p>}
                  </div>
                  {v.number !== doc.versionCount && (
                    <div className="fs-inline">
                      <Button size="sm" variant="ghost" label={t('Compare')} onClick={() => setCompare(v)} />
                      <Button
                        size="sm"
                        variant="ghost"
                        label={t('Restore')}
                        onClick={() =>
                          restoreDocVersion(doc.id, v.number)
                            .then((d) => {
                              setDoc(d);
                              setText(d.content);
                              setVersions(null);
                              say(t('Restored v{a} as v{b}', { a: v.number, b: d.versionCount }));
                            })
                            .catch((e: Error) => say(e.message, 'warn'))
                        }
                      />
                    </div>
                  )}
                </li>
              ))}
            </ul>
          </aside>
        )}
      </div>

      {run && (
        <div className="fs-docs__run" data-error={run.err || undefined} role="status">
          <header>
            <span>{run.err ? t('Output (with errors)') : t('Output')}</span>
            <IconButton icon={X} label={t('Close the output')} size="sm" onClick={() => setRun(null)} />
          </header>
          <pre>{run.out}</pre>
        </div>
      )}

      <Dialog
        open={confirm === 'delete'}
        onOpenChange={(o) => !o && setConfirm(null)}
        title={t('Delete "{name}"?', { name: doc.title })}
        description={t('The document and every version of it are removed. This cannot be undone.')}
        testId="doc-delete"
        footer={
          <>
            <Button variant="ghost" label={t('Cancel')} onClick={() => setConfirm(null)} />
            <Button variant="danger-solid" label={t('Delete')} onClick={() => void deleteDoc(doc.id).then(() => navigate('/library?type=documento')).catch((e: Error) => say(e.message, 'warn'))} />
          </>
        }
      />
      <Dialog
        open={confirm === 'archive'}
        onOpenChange={(o) => !o && setConfirm(null)}
        title={t('Archive "{name}"?', { name: doc.title })}
        description={t('It leaves the documents list and waits in the archive, where you can restore it.')}
        testId="doc-archive"
        footer={
          <>
            <Button variant="ghost" label={t('Cancel')} onClick={() => setConfirm(null)} />
            <Button variant="primary" label={t('Archive')} onClick={() => void archiveDoc(doc.id).then(() => navigate('/library?type=archivo')).catch((e: Error) => say(e.message, 'warn'))} />
          </>
        }
      />

      {notice && (
        <Toast>
          {notice.tone === 'warn' ? <AlertTriangle size={12} aria-hidden="true" /> : <Check size={12} aria-hidden="true" />} {notice.text}
        </Toast>
      )}
    </div>
  );
}

function CsvTable({ text }: { text: string }) {
  const rows = useMemo(() => parseCsv(text), [text]);
  if (!rows.length) return <p className="fs-docs__muted">{t('Empty table')}</p>;
  const [head, ...body] = rows;
  return (
    <div className="fs-docs__table">
      <table>
        <thead>
          <tr>
            {head.map((c, i) => (
              <th key={i}>{c}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {body.map((r, i) => (
            <tr key={i}>
              {r.map((c, j) => (
                <td key={j}>{c}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function lineHeight(ta: HTMLTextAreaElement): number {
  const v = parseFloat(getComputedStyle(ta).lineHeight);
  return Number.isFinite(v) ? v : 20;
}

function extFor(lang: string): string {
  const map: Record<string, string> = { markdown: '.md', md: '.md', python: '.py', javascript: '.js', typescript: '.ts', html: '.html', css: '.css', json: '.json', yaml: '.yml', bash: '.sh', sql: '.sql', csv: '.csv', rust: '.rs', go: '.go', java: '.java', c: '.c', cpp: '.cpp', ruby: '.rb', php: '.php', xml: '.xml', toml: '.toml', ini: '.ini' };
  return map[lang] ?? '.txt';
}

/** JavaScript runs in a sandboxed iframe that reports console output back. */
function runJs(code: string): Promise<{ out: string; err: boolean }> {
  return new Promise((resolve) => {
    const iframe = document.createElement('iframe');
    iframe.hidden = true;
    iframe.sandbox.add('allow-scripts');
    let settled = false;
    const done = (r: { out: string; err: boolean }) => {
      if (settled) return;
      settled = true;
      window.removeEventListener('message', onMessage);
      iframe.remove();
      resolve(r);
    };
    const onMessage = (e: MessageEvent) => {
      if (e.source !== iframe.contentWindow) return;
      const d = e.data as { error?: string; logs?: string[] };
      if (d.error) done({ out: d.error, err: true });
      else done({ out: (d.logs ?? []).join('\n') || t('(no output)'), err: false });
    };
    window.addEventListener('message', onMessage);
    window.setTimeout(() => done({ out: t('Execution timed out (10 s)'), err: true }), 12000);
    const wrapped = `<!doctype html><html><body><script>
var _logs=[];var _s=function(a){try{return typeof a==='object'?JSON.stringify(a):String(a)}catch(e){return String(a)}};
console.log=function(){_logs.push([].map.call(arguments,_s).join(' '))};console.warn=function(){_logs.push('[warn] '+[].map.call(arguments,_s).join(' '))};console.error=function(){_logs.push('[error] '+[].map.call(arguments,_s).join(' '))};
try{var _t=setTimeout(function(){parent.postMessage({error:'Execution timed out (10 s)'},'*')},10000);
${code.replace(/<\/script>/gi, '<\\/script>')}
clearTimeout(_t);parent.postMessage({logs:_logs},'*')}catch(e){parent.postMessage({error:String(e&&e.stack||e)},'*')}
<\/script></body></html>`;
    document.body.appendChild(iframe);
    iframe.srcdoc = wrapped;
  });
}
