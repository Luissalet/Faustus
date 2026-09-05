import { ArrowDownToLine, ChevronDown, ChevronUp, Copy, Eye, EyeOff, GripVertical, Layers as LayersIcon, Lock, LockOpen, MoreHorizontal, Plus, Scissors, SlidersHorizontal, Trash2 } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { Button, IconButton, Menu } from '../../../components';
import { adjustmentLabel, type AdjustmentType, type Layer } from '../../../lib/pixel';
import { t } from '../../../i18n';
import type { PixelEditor } from './engine';

interface Props {
  ed: PixelEditor;
  version: number;
  onAdjust: (layerId: string, type: AdjustmentType, adjId?: string) => void;
}

/**
 * Layers, top of the stack first. Each row carries what the legacy panel
 * did — visibility, opacity, duplicate, mask, adjust, merge, delete — but
 * the rare actions live in one menu instead of a strip of tiny icons.
 */
export function LayersPane({ ed, version, onAdjust }: Props) {
  const layers = [...ed.doc.layers].reverse();
  const [dragId, setDragId] = useState<string | null>(null);
  return (
    <section className="fs-ed__section fs-ed__layers">
      <header className="fs-ed__section-head">
        <h3>{t('Layers')}</h3>
        <div className="fs-ed__row">
          <IconButton icon={ArrowDownToLine} label={t('Merge all')} size="sm" disabled={ed.doc.layers.length < 2} onClick={() => ed.mergeAll()} />
          <IconButton icon={LayersIcon} label={t('Flatten copy (keeps the originals)')} size="sm" onClick={() => ed.flattenCopy()} />
          <Button size="sm" icon={Plus} label={t('Add')} title={t('Add an empty layer (Ctrl+Alt+J)')} onClick={() => ed.addLayer()} />
        </div>
      </header>
      <ul className="fs-ed__layer-list" data-testid="layers">
        {layers.map((layer, i) => (
          <LayerRow
            key={layer.id}
            ed={ed}
            layer={layer}
            version={version}
            isTop={i === 0}
            isBottom={i === layers.length - 1}
            dragging={dragId === layer.id}
            onDragStart={() => setDragId(layer.id)}
            onDrop={() => {
              if (dragId && dragId !== layer.id) ed.moveLayer(dragId, ed.doc.layers.findIndex((l) => l.id === layer.id));
              setDragId(null);
            }}
            onDragEnd={() => setDragId(null)}
            onAdjust={onAdjust}
          />
        ))}
      </ul>
    </section>
  );
}

function LayerRow({ ed, layer, version, isTop, isBottom, dragging, onDragStart, onDrop, onDragEnd, onAdjust }: { ed: PixelEditor; layer: Layer; version: number; isTop: boolean; isBottom: boolean; dragging: boolean; onDragStart: () => void; onDrop: () => void; onDragEnd: () => void; onAdjust: Props['onAdjust'] }) {
  const active = ed.doc.activeLayerId === layer.id;
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState(layer.name);
  const idx = ed.doc.layers.indexOf(layer);
  useEffect(() => setName(layer.name), [layer.name]);
  const adjustItems = (['brightness-contrast', 'hue-saturation', 'levels', 'color-balance'] as AdjustmentType[]).map((type) => ({ label: t(adjustmentLabel(type)), icon: SlidersHorizontal, onSelect: () => onAdjust(layer.id, type) }));
  return (
    <li
      className="fs-ed__layer"
      data-active={active || undefined}
      data-hidden={!layer.visible || undefined}
      data-dragging={dragging || undefined}
      draggable
      onDragStart={(e) => {
        e.dataTransfer.effectAllowed = 'move';
        onDragStart();
      }}
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => {
        e.preventDefault();
        onDrop();
      }}
      onDragEnd={onDragEnd}
    >
      <div className="fs-ed__layer-main">
        <span className="fs-ed__grip" aria-hidden="true">
          <GripVertical size={12} />
        </span>
        <IconButton icon={layer.visible ? Eye : EyeOff} label={layer.visible ? t('Hide layer') : t('Show layer')} size="sm" onClick={() => ed.updateLayer(layer.id, { visible: !layer.visible }, false)} />
        <button type="button" className="fs-ed__layer-pick" aria-pressed={active} onClick={() => ed.setActive(layer.id)} onDoubleClick={() => setRenaming(true)} title={t('Double-click to rename')}>
          <Thumb layer={layer} version={version} />
          {renaming ? (
            <input
              className="fs-field"
              value={name}
              autoFocus
              onChange={(e) => setName(e.target.value)}
              onClick={(e) => e.stopPropagation()}
              onBlur={() => {
                setRenaming(false);
                if (name.trim() && name !== layer.name) ed.updateLayer(layer.id, { name: name.trim() });
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter') (e.target as HTMLInputElement).blur();
                if (e.key === 'Escape') {
                  setName(layer.name);
                  setRenaming(false);
                }
              }}
            />
          ) : (
            <span className="fs-ed__layer-name">
              {layer.name}
              {layer.locked && <Lock size={10} aria-label={t('Locked')} />}
            </span>
          )}
        </button>
        <input className="fs-ed__opacity" type="range" min={0} max={100} value={Math.round(layer.opacity * 100)} aria-label={t('Opacity')} title={`${Math.round(layer.opacity * 100)}%`} onChange={(e) => ed.updateLayer(layer.id, { opacity: Number(e.target.value) / 100 }, false)} />
        <Menu
          align="end"
          trigger={<IconButton icon={MoreHorizontal} label={t('Layer actions')} size="sm" />}
          items={[
            { label: t('Rename'), onSelect: () => setRenaming(true) },
            { label: t('Duplicate'), icon: Copy, onSelect: () => ed.duplicateLayer(layer.id) },
            { label: layer.locked ? t('Unlock') : t('Lock'), icon: layer.locked ? LockOpen : Lock, onSelect: () => ed.updateLayer(layer.id, { locked: !layer.locked }) },
            null,
            { label: t('Move up'), icon: ChevronUp, disabled: isTop, onSelect: () => ed.moveLayer(layer.id, idx + 1) },
            { label: t('Move down'), icon: ChevronDown, disabled: isBottom, onSelect: () => ed.moveLayer(layer.id, idx - 1) },
            { label: t('Merge down'), icon: ArrowDownToLine, disabled: isBottom, onSelect: () => ed.mergeDown(layer.id) },
            null,
            { label: ed.hasSelection() ? t('Add mask from selection') : t('Add mask'), icon: Scissors, onSelect: () => ed.addMask(layer.id, ed.hasSelection()) },
            ...adjustItems,
            null,
            { label: layer.isBase ? t('Delete original layer') : t('Delete layer'), icon: Trash2, variant: 'danger', onSelect: () => ed.deleteLayer(layer.id) },
          ]}
        />
      </div>
      {(layer.adjustments.length > 0 || layer.masks.length > 0) && (
        <ul className="fs-ed__sub">
          {layer.adjustments.map((adj) => (
            <li key={adj.id} className="fs-ed__subrow" data-hidden={!adj.visible || undefined}>
              <IconButton icon={adj.visible ? Eye : EyeOff} label={adj.visible ? t('Hide adjustment') : t('Show adjustment')} size="sm" onClick={() => ed.updateAdjustment(layer.id, adj.id, { visible: !adj.visible })} />
              <button type="button" className="fs-ed__subname" onClick={() => onAdjust(layer.id, adj.type, adj.id)} title={t('Edit adjustment')}>
                <SlidersHorizontal size={11} aria-hidden="true" /> {adj.name}
              </button>
              <input className="fs-ed__opacity" type="range" min={0} max={100} value={Math.round(adj.opacity * 100)} aria-label={t('Adjustment opacity')} onChange={(e) => ed.updateAdjustment(layer.id, adj.id, { opacity: Number(e.target.value) / 100 })} />
              <IconButton icon={ArrowDownToLine} label={t('Bake into the layer')} size="sm" onClick={() => ed.bakeAdjustment(layer.id, adj.id)} />
              <IconButton icon={Trash2} label={t('Delete adjustment')} size="sm" onClick={() => ed.deleteAdjustment(layer.id, adj.id)} />
            </li>
          ))}
          {layer.masks.map((mask, mi) => (
            <li key={mask.id} className="fs-ed__subrow" data-hidden={!mask.visible || undefined} data-active={(active && layer.activeMaskId === mask.id) || undefined}>
              <IconButton icon={mask.visible ? Eye : EyeOff} label={mask.visible ? t('Hide mask') : t('Show mask')} size="sm" onClick={() => ed.updateMask(layer.id, mask.id, { visible: !mask.visible })} />
              <button type="button" className="fs-ed__subname" aria-pressed={layer.activeMaskId === mask.id} onClick={() => ed.setActiveMask(layer.id, layer.activeMaskId === mask.id ? null : mask.id)} title={t('Paint this mask with the brush and eraser')}>
                <Scissors size={11} aria-hidden="true" /> {mask.name}
              </button>
              {mi > 0 && <IconButton icon={ChevronUp} label={t('Merge into the mask above')} size="sm" onClick={() => ed.mergeMaskUp(layer.id, mask.id)} />}
              <IconButton icon={Trash2} label={t('Delete mask')} size="sm" onClick={() => ed.deleteMask(layer.id, mask.id)} />
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

function Thumb({ layer, version }: { layer: Layer; version: number }) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const c = ref.current;
    if (!c) return;
    const ctx = c.getContext('2d');
    if (!ctx) return;
    ctx.clearRect(0, 0, c.width, c.height);
    const scale = Math.min(c.width / Math.max(1, layer.canvas.width), c.height / Math.max(1, layer.canvas.height));
    const w = layer.canvas.width * scale, h = layer.canvas.height * scale;
    ctx.drawImage(layer.canvas, (c.width - w) / 2, (c.height - h) / 2, w, h);
  }, [layer, version]);
  return <canvas ref={ref} width={40} height={30} className="fs-ed__thumb" aria-hidden="true" />;
}
