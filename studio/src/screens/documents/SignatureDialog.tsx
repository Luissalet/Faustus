import { Trash2, Upload } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { Button, Dialog, IconButton, Skeleton } from '../../components';
import { createSignature, deleteSignature, listSignatures, rememberSignature, type Signature } from '../../adapters/signatures';
import { t } from '../../i18n';
import { SIGNATURE_INK } from './markdown';

/**
 * Pick a saved signature, draw a new one, or upload a PNG. Drawing smooths
 * the pen with a running average so a mouse still produces a clean line.
 */
export function SignatureDialog({ open, onClose, onPick }: { open: boolean; onClose: () => void; onPick: (sig: Signature) => void }) {
  const [list, setList] = useState<Signature[] | null>(null);
  const [name, setName] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const strokes = useRef<{ x: number; y: number }[][]>([]);
  const drawing = useRef(false);
  const smooth = useRef<{ x: number; y: number } | null>(null);

  useEffect(() => {
    if (!open) return;
    setList(null);
    listSignatures()
      .then(setList)
      .catch((e: Error) => setError(e.message));
    strokes.current = [];
  }, [open]);

  const redraw = () => {
    const c = canvasRef.current;
    if (!c) return;
    const ctx = c.getContext('2d');
    if (!ctx) return;
    ctx.clearRect(0, 0, c.width, c.height);
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.lineWidth = 2.6;
    ctx.strokeStyle = SIGNATURE_INK;
    for (const s of strokes.current) {
      if (s.length < 2) continue;
      ctx.beginPath();
      ctx.moveTo(s[0].x, s[0].y);
      for (let i = 1; i < s.length - 1; i++) {
        const mx = (s[i].x + s[i + 1].x) / 2, my = (s[i].y + s[i + 1].y) / 2;
        ctx.quadraticCurveTo(s[i].x, s[i].y, mx, my);
      }
      ctx.lineTo(s[s.length - 1].x, s[s.length - 1].y);
      ctx.stroke();
    }
  };

  const point = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const c = canvasRef.current!;
    const r = c.getBoundingClientRect();
    const raw = { x: ((e.clientX - r.left) / r.width) * c.width, y: ((e.clientY - r.top) / r.height) * c.height };
    const prev = smooth.current;
    const p = prev ? { x: prev.x + (raw.x - prev.x) * 0.45, y: prev.y + (raw.y - prev.y) * 0.45 } : raw;
    smooth.current = p;
    return p;
  };

  const trimmed = (): { dataUrl: string; width: number; height: number } | null => {
    const c = canvasRef.current;
    if (!c || !strokes.current.some((s) => s.length > 1)) return null;
    const ctx = c.getContext('2d')!;
    const d = ctx.getImageData(0, 0, c.width, c.height).data;
    let minX = c.width, minY = c.height, maxX = 0, maxY = 0;
    for (let y = 0; y < c.height; y++)
      for (let x = 0; x < c.width; x++)
        if (d[(y * c.width + x) * 4 + 3] > 0) {
          if (x < minX) minX = x;
          if (x > maxX) maxX = x;
          if (y < minY) minY = y;
          if (y > maxY) maxY = y;
        }
    const pad = 8;
    const w = maxX - minX + 1 + pad * 2, h = maxY - minY + 1 + pad * 2;
    const out = document.createElement('canvas');
    out.width = w;
    out.height = h;
    out.getContext('2d')!.drawImage(c, minX - pad, minY - pad, w, h, 0, 0, w, h);
    return { dataUrl: out.toDataURL('image/png'), width: w, height: h };
  };

  const save = async () => {
    const img = trimmed();
    if (!img) {
      setError(t('Draw something first.'));
      return;
    }
    setSaving(true);
    try {
      const sig = await createSignature({ ...img, name: name.trim() });
      rememberSignature(sig.id);
      onPick(sig);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const upload = async (file: File) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = async () => {
      URL.revokeObjectURL(url);
      const c = document.createElement('canvas');
      c.width = img.width;
      c.height = img.height;
      c.getContext('2d')!.drawImage(img, 0, 0);
      setSaving(true);
      try {
        const sig = await createSignature({ dataUrl: c.toDataURL('image/png'), width: c.width, height: c.height, name: name.trim() || file.name.replace(/\.[^.]+$/, '') });
        rememberSignature(sig.id);
        onPick(sig);
      } catch (e) {
        setError((e as Error).message);
      } finally {
        setSaving(false);
      }
    };
    img.onerror = () => setError(t('That file is not an image.'));
    img.src = url;
  };

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()} title={t('Signature')} description={t('Pick a saved one, or draw a new one. Signatures stay on this server.')} testId="signature">
      {error && (
        <p className="fs-notice" data-tone="warning">
          {error}
        </p>
      )}
      <div className="fs-docs__sigs">
        {!list && !error && <Skeleton label={t('Loading signatures')} count={2} height="48px" />}
        {list?.map((s) => (
          <div key={s.id} className="fs-docs__sig">
            <button
              type="button"
              className="fs-docs__sig-pick"
              onClick={() => {
                rememberSignature(s.id);
                onPick(s);
              }}
              title={s.name || t('Use this signature')}
            >
              <img src={s.dataUrl} alt={s.name || t('Signature')} />
            </button>
            <span>{s.name || t('Unnamed')}</span>
            <IconButton icon={Trash2} label={t('Delete signature')} size="sm" onClick={() => void deleteSignature(s.id).then(() => setList((cur) => (cur ?? []).filter((x) => x.id !== s.id)))} />
          </div>
        ))}
        {list && !list.length && <p className="fs-docs__muted">{t('No saved signatures yet — draw one below.')}</p>}
      </div>
      <p className="fs-docs__kicker">{t('Draw a new one')}</p>
      <canvas
        ref={canvasRef}
        className="fs-docs__sig-canvas"
        width={720}
        height={240}
        aria-label={t('Signature pad')}
        onPointerDown={(e) => {
          e.currentTarget.setPointerCapture(e.pointerId);
          drawing.current = true;
          smooth.current = null;
          strokes.current.push([point(e)]);
        }}
        onPointerMove={(e) => {
          if (!drawing.current) return;
          strokes.current[strokes.current.length - 1].push(point(e));
          redraw();
        }}
        onPointerUp={() => {
          drawing.current = false;
          redraw();
        }}
      />
      <div className="fs-inline">
        <input className="fs-field" value={name} onChange={(e) => setName(e.target.value)} placeholder={t('Name (optional): full, initials…')} aria-label={t('Signature name')} />
        <Button size="sm" variant="ghost" label={t('Undo stroke')} onClick={() => { strokes.current.pop(); redraw(); }} />
        <Button size="sm" variant="ghost" label={t('Clear')} onClick={() => { strokes.current = []; redraw(); }} />
        <Button size="sm" variant="ghost" icon={Upload} label={t('Upload PNG')} onClick={() => fileRef.current?.click()} />
        <input ref={fileRef} type="file" accept="image/*" hidden onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ''; if (f) void upload(f); }} />
        <Button size="sm" variant="primary" label={t('Save and use')} loading={saving} onClick={() => void save()} />
      </div>
    </Dialog>
  );
}
