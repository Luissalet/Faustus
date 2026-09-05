import { CheckSquare, X } from 'lucide-react';
import { useCallback, useState, type ReactNode } from 'react';
import { Button } from '../../components';
import { t, tn } from '../../i18n';

/** Select-several mode shared by the library's lists. */
export function useSelection<T extends { id: string }>() {
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const toggle = useCallback((id: string) => {
    setSelected((cur) => {
      const next = new Set(cur);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);
  const all = useCallback((items: T[], on: boolean) => setSelected(on ? new Set(items.map((x) => x.id)) : new Set()), []);
  const leave = useCallback(() => {
    setSelecting(false);
    setSelected(new Set());
  }, []);
  const enter = useCallback(() => {
    setSelecting(true);
    setSelected(new Set());
  }, []);
  return { selecting, selected, toggle, all, leave, enter, setSelected };
}

export function SelectToggle({ selecting, onToggle, testId }: { selecting: boolean; onToggle: () => void; testId?: string }) {
  return <Button variant="ghost" size="sm" icon={selecting ? X : CheckSquare} label={selecting ? t('Leave selection') : t('Select several')} onClick={onToggle} testId={testId} />;
}

export function BulkBar<T extends { id: string }>({ items, selected, onAll, label, children }: { items: T[]; selected: Set<string>; onAll: (on: boolean) => void; label: string; children: ReactNode }) {
  return (
    <div className="fs-gal__bulk" role="toolbar" aria-label={label}>
      <label className="fs-switch">
        <input type="checkbox" checked={!!items.length && items.every((x) => selected.has(x.id))} onChange={(e) => onAll(e.target.checked)} />
        <span>{t('All')}</span>
      </label>
      <span className="fs-gal__muted">{tn(selected.size, '{n} selected', '{n} selected#')}</span>
      <span className="fs-gal__spacer" />
      {children}
    </div>
  );
}

export function downloadBlob(blob: Blob, name: string): void {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  window.setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}

/** Case-insensitive `<mark>` of the search text inside a title. */
export function Highlight({ text, needle }: { text: string; needle: string }) {
  const q = needle.trim();
  if (!q) return <>{text}</>;
  const idx = text.toLowerCase().indexOf(q.toLowerCase());
  if (idx < 0) return <>{text}</>;
  return (
    <>
      {text.slice(0, idx)}
      <mark>{text.slice(idx, idx + q.length)}</mark>
      {text.slice(idx + q.length)}
    </>
  );
}
