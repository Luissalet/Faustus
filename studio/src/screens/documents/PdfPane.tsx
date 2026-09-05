import { Check, Download, PenLine, Sparkles, Trash2, Type, Undo2, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Button, Dialog, IconButton, Skeleton } from '../../components';
import { aiFillAnnotations, exportPdfBlob, pdfPageUrl, renderPdfPages, type PdfField, type PdfPage } from '../../adapters/documents';
import { listSignatures, type Signature } from '../../adapters/signatures';
import { isSignatureField, newAnnotationId, parseAnnotations, writeAnnotations, writeFieldValues, type Annotation, type AnnotationKind, type FieldValue } from '../../lib/pdfDoc';
import { t, tn } from '../../i18n';
import { download } from './exports';
import { SignatureDialog } from './SignatureDialog';

interface Props {
  docId: string;
  title: string;
  content: string;
  /** The pane owns the PDF part of the markdown; it hands back the whole text to save. */
  onChange: (md: string) => void;
  say: (m: string, tone?: 'ok' | 'warn') => void;
}

type DropMode = AnnotationKind | null;

const DEFAULT_SIZE: Record<AnnotationKind, { w: number; h: number }> = { text: { w: 22, h: 3.2 }, check: { w: 3, h: 3 }, signature: { w: 22, h: 6 } };

/**
 * The pages of a PDF-backed document with its form fields laid over them
 * and free annotations (text, ticks, signatures) placed by clicking. Every
 * change is written back into the document's markdown, which the server
 * bakes into the exported PDF.
 */
export function PdfPane({ docId, title, content, onChange, say }: Props) {
  const [pages, setPages] = useState<PdfPage[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [values, setValues] = useState<Record<string, FieldValue>>({});
  const [annotations, setAnnotations] = useState<Annotation[]>(() => parseAnnotations(content));
  const [mode, setMode] = useState<DropMode>(null);
  const [sigs, setSigs] = useState<Record<string, Signature>>({});
  const [sigTarget, setSigTarget] = useState<{ kind: 'field'; name: string } | { kind: 'annotation'; id: string } | null>(null);
  const [aiOpen, setAiOpen] = useState(false);
  const [aiText, setAiText] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const [focusId, setFocusId] = useState<string | null>(null);
  const undo = useRef<{ values: Record<string, FieldValue>; annotations: Annotation[] }[]>([]);
  const contentRef = useRef(content);
  contentRef.current = content;

  useEffect(() => {
    let cancelled = false;
    renderPdfPages(docId)
      .then((out) => {
        if (cancelled) return;
        setPages(out.pages);
        const vals: Record<string, FieldValue> = {};
        for (const p of out.pages)
          for (const f of p.fields) {
            const sig = isSignatureField(f);
            vals[f.name] = sig ? { name: f.name, type: 'signature', value: '', signatureId: f.value.startsWith('signature:') ? f.value.slice('signature:'.length).trim() : undefined } : f.type === 'checkbox' ? { name: f.name, type: 'checkbox', value: f.value === 'true' || f.value === 'on' || f.value === 'x' } : { name: f.name, type: f.type, value: f.value };
          }
        setValues(vals);
      })
      .catch((e: Error) => !cancelled && setError(e.message));
    listSignatures()
      .then((list) => {
        if (cancelled) return;
        const map: Record<string, Signature> = {};
        for (const s of list) map[s.id] = s;
        setSigs(map);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [docId]);

  const pushUndo = () => {
    undo.current.push({ values, annotations });
    if (undo.current.length > 40) undo.current.shift();
  };

  // Every change is written back into the markdown after a short pause.
  const dirty = useRef(false);
  const timer = useRef<number>(0);
  const commit = useCallback(
    (vals: Record<string, FieldValue>, anns: Annotation[]) => {
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => {
        const md = writeAnnotations(writeFieldValues(contentRef.current, Object.values(vals)), anns);
        if (md !== contentRef.current) onChange(md);
        dirty.current = false;
      }, 500);
    },
    [onChange],
  );

  const setValue = (name: string, patch: Partial<FieldValue>) => {
    setValues((cur) => {
      const next = { ...cur, [name]: { ...cur[name], ...patch } };
      commit(next, annotations);
      return next;
    });
  };

  const setAnns = (fn: (cur: Annotation[]) => Annotation[]) => {
    setAnnotations((cur) => {
      const next = fn(cur);
      commit(values, next);
      return next;
    });
  };

  const addAnnotation = (page: PdfPage, e: React.MouseEvent<HTMLButtonElement>) => {
    if (!mode) return;
    const r = e.currentTarget.getBoundingClientRect();
    const size = DEFAULT_SIZE[mode];
    // Keyboard activation (Enter/Space on the page) has no pointer: place it in the middle.
    const px = e.detail === 0 ? r.left + r.width / 2 : e.clientX;
    const py = e.detail === 0 ? r.top + r.height / 2 : e.clientY;
    const x = Math.max(0, Math.min(100 - size.w, ((px - r.left) / r.width) * 100));
    const y = Math.max(0, Math.min(100 - size.h, ((py - r.top) / r.height) * 100 - size.h / 2));
    pushUndo();
    const ann: Annotation = { id: newAnnotationId(), page: page.page, x, y, w: size.w, h: size.h, kind: mode, lineHeight: 1.3, value: mode === 'check' ? '✓' : '' };
    setAnns((cur) => [...cur, ann]);
    setFocusId(ann.id);
    if (mode === 'signature') setSigTarget({ kind: 'annotation', id: ann.id });
    else if (mode === 'text') setMode(null);
  };

  const removeAnnotation = (id: string) => {
    pushUndo();
    setAnns((cur) => cur.filter((a) => a.id !== id));
  };

  const undoLast = () => {
    const prev = undo.current.pop();
    if (!prev) return;
    setValues(prev.values);
    setAnnotations(prev.annotations);
    commit(prev.values, prev.annotations);
  };

  const pickSignature = (sig: Signature) => {
    setSigs((cur) => ({ ...cur, [sig.id]: sig }));
    if (sigTarget?.kind === 'field') setValue(sigTarget.name, { signatureId: sig.id });
    else if (sigTarget?.kind === 'annotation') setAnns((cur) => cur.map((a) => (a.id === sigTarget.id ? { ...a, value: `signature:${sig.id}` } : a)));
    setSigTarget(null);
  };

  const aiFill = async () => {
    const instruction = aiText.trim();
    if (!instruction) return;
    setBusy('ai');
    try {
      const found = await aiFillAnnotations(docId, instruction);
      if (!found.length) say(t('The model found nothing to fill.'), 'warn');
      else {
        pushUndo();
        setAnns((cur) => [...cur, ...found.map((f) => ({ id: newAnnotationId(), page: f.page, x: f.x, y: f.y, w: f.w, h: f.h, kind: 'text' as const, lineHeight: 1.3, value: f.value }))]);
        say(tn(found.length, '{n} field filled', '{n} fields filled'));
      }
      setAiOpen(false);
    } catch (e) {
      say((e as Error).message, 'warn');
    } finally {
      setBusy(null);
    }
  };

  const downloadPdf = async () => {
    setBusy('pdf');
    try {
      const out = await exportPdfBlob(docId);
      download(out.blob, out.filename || `${title.replace(/\.pdf$/i, '') || 'form'}_annotated.pdf`);
    } catch (e) {
      say((e as Error).message, 'warn');
    } finally {
      setBusy(null);
    }
  };

  const modes: { id: AnnotationKind; icon: typeof Type; label: string }[] = useMemo(
    () => [
      { id: 'text', icon: Type, label: t('Text') },
      { id: 'check', icon: Check, label: t('Tick') },
      { id: 'signature', icon: PenLine, label: t('Signature') },
    ],
    [],
  );

  const fieldCount = pages?.reduce((n, p) => n + p.fields.length, 0) ?? 0;

  return (
    <div className="fs-docs__pdf" data-testid="pdf-pane" data-mode={mode ?? undefined}>
      <div className="fs-docs__pdf-bar" role="toolbar" aria-label={t('PDF tools')}>
        <span className="fs-docs__kicker">{t('Place')}</span>
        {modes.map((m) => (
          <Button key={m.id} size="sm" variant={mode === m.id ? 'primary' : 'ghost'} icon={m.icon} label={m.label} title={t('Then click the page where it goes')} onClick={() => setMode(mode === m.id ? null : m.id)} />
        ))}
        {mode && <IconButton icon={X} label={t('Stop placing')} size="sm" onClick={() => setMode(null)} />}
        <span className="fs-spacer" />
        <IconButton icon={Undo2} label={t('Undo the last change on the pages')} size="sm" disabled={!undo.current.length} onClick={undoLast} />
        <Button size="sm" variant="ghost" icon={Sparkles} label={t('Fill with AI')} title={t('Describe what to write and a vision model finds the blanks')} onClick={() => setAiOpen(true)} />
        <Button size="sm" variant="secondary" icon={Download} label={t('Download filled PDF')} loading={busy === 'pdf'} onClick={() => void downloadPdf()} />
      </div>
      {pages && (
        <p className="fs-docs__pdf-facts">
          {tn(pages.length, '{n} page', '{n} pages')}
          {fieldCount > 0 && ` · ${tn(fieldCount, '{n} form field', '{n} form fields')}`}
          {annotations.length > 0 && ` · ${tn(annotations.length, '{n} annotation', '{n} annotations')}`}
          {mode && ` · ${t('Click a page to place it')}`}
        </p>
      )}
      {error && (
        <p className="fs-notice" data-tone="warning">
          {error}
        </p>
      )}
      {!pages && !error && <Skeleton label={t('Rendering the pages')} count={2} height="360px" radius="preview" />}
      {pages?.map((page) => (
        <div key={page.page} className="fs-docs__page" style={{ aspectRatio: `${page.width} / ${page.height}` }}>
          <img src={pdfPageUrl(docId, page.page)} alt={t('Page {n}', { n: page.page })} width={page.width} height={page.height} loading="lazy" draggable={false} />
          {mode && <button type="button" className="fs-docs__page-hit" aria-label={t('Page {n}: click to place', { n: page.page })} onClick={(e) => addAnnotation(page, e)} />}
          {page.fields.map((f) => (
            <FieldBox key={f.name} field={f} page={page} value={values[f.name]} sigs={sigs} onChange={(patch) => setValue(f.name, patch)} onSign={() => setSigTarget({ kind: 'field', name: f.name })} />
          ))}
          {annotations
            .filter((a) => a.page === page.page)
            .map((a) => (
              <div key={a.id} className="fs-docs__ann" data-kind={a.kind} style={{ left: `${a.x}%`, top: `${a.y}%`, width: `${a.w}%`, height: a.kind === 'text' ? undefined : `${a.h}%`, minHeight: `${a.h}%` }}>
                {a.kind === 'text' && <textarea value={a.value} rows={1} autoFocus={focusId === a.id} aria-label={t('Annotation text')} style={{ lineHeight: a.lineHeight }} onChange={(e) => setAnns((cur) => cur.map((x) => (x.id === a.id ? { ...x, value: e.target.value } : x)))} />}
                {a.kind === 'check' && <span aria-hidden="true">✓</span>}
                {a.kind === 'signature' && (
                  <button type="button" className="fs-docs__ann-sig" onClick={() => setSigTarget({ kind: 'annotation', id: a.id })} title={t('Choose the signature')}>
                    {a.value.startsWith('signature:') && sigs[a.value.slice('signature:'.length)] ? <img src={sigs[a.value.slice('signature:'.length)].dataUrl} alt={t('Signature')} /> : <span>{t('Sign here')}</span>}
                  </button>
                )}
                <IconButton icon={Trash2} label={t('Remove annotation')} size="sm" onClick={() => removeAnnotation(a.id)} />
              </div>
            ))}
        </div>
      ))}

      <SignatureDialog open={!!sigTarget} onClose={() => setSigTarget(null)} onPick={pickSignature} />

      <Dialog
        open={aiOpen}
        onOpenChange={setAiOpen}
        title={t('Fill with AI')}
        description={t('Say what to write — "my name is Ana López, today\'s date, tick the first box" — and a vision model places the text where the blanks are. You can move or delete anything afterwards.')}
        testId="ai-fill"
        footer={
          <>
            <Button variant="ghost" label={t('Cancel')} onClick={() => setAiOpen(false)} />
            <Button variant="primary" icon={Sparkles} label={busy === 'ai' ? t('Filling…') : t('Fill')} loading={busy === 'ai'} disabled={!aiText.trim()} onClick={() => void aiFill()} />
          </>
        }
      >
        <textarea className="fs-field" rows={4} value={aiText} onChange={(e) => setAiText(e.target.value)} placeholder={t('What should go in the form')} aria-label={t('Instruction')} />
      </Dialog>
    </div>
  );
}

function FieldBox({ field, page, value, sigs, onChange, onSign }: { field: PdfField; page: PdfPage; value: FieldValue | undefined; sigs: Record<string, Signature>; onChange: (patch: Partial<FieldValue>) => void; onSign: () => void }) {
  const [x0, y0, x1, y1] = field.rect;
  const style = { left: `${(x0 / page.width) * 100}%`, top: `${(y0 / page.height) * 100}%`, width: `${((x1 - x0) / page.width) * 100}%`, height: `${((y1 - y0) / page.height) * 100}%` };
  if (!value) return null;
  if (value.type === 'signature') {
    const sig = value.signatureId ? sigs[value.signatureId] : undefined;
    return (
      <div className="fs-docs__field" data-type="signature" style={style}>
        <button type="button" className="fs-docs__ann-sig" onClick={onSign} title={field.label || field.name}>
          {sig ? <img src={sig.dataUrl} alt={t('Signature')} /> : <span>{t('Sign here')}</span>}
        </button>
        {sig && <IconButton icon={X} label={t('Remove the signature')} size="sm" onClick={() => onChange({ signatureId: undefined })} />}
      </div>
    );
  }
  if (value.type === 'checkbox') {
    return (
      <label className="fs-docs__field" data-type="checkbox" style={style}>
        <input type="checkbox" checked={!!value.value} onChange={(e) => onChange({ value: e.target.checked })} aria-label={field.label || field.name} />
      </label>
    );
  }
  if (value.type === 'choice') {
    return (
      <div className="fs-docs__field" data-type="choice" style={style}>
        <select value={value.value as string} onChange={(e) => onChange({ value: e.target.value })} aria-label={field.label || field.name}>
          <option value="">—</option>
          {field.options.map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </select>
      </div>
    );
  }
  const isDate = /date|fecha/i.test(`${field.name} ${field.label}`);
  return (
    <div className="fs-docs__field" data-type="text" style={style}>
      <input value={value.value as string} onChange={(e) => onChange({ value: e.target.value })} aria-label={field.label || field.name} placeholder={field.label} />
      {isDate && !value.value && <button type="button" className="fs-docs__today" onClick={() => onChange({ value: new Date().toLocaleDateString() })}>{t('Today')}</button>}
    </div>
  );
}
