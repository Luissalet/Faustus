import type { LucideIcon } from 'lucide-react';
import { useEffect, useId, useRef, useState } from 'react';
import { IconButton } from './IconButton';
import { slug } from './testid';

/**
 * A dependency-free dropdown for the eager bundle. `Menu` (Radix) pulls
 * floating-ui in — 80 KB the Studio budget cannot afford on a screen that
 * loads on every visit — so the sidebar's small menus use this: a
 * positioned list under an icon button, closed by a click outside, Escape,
 * or a choice. Keyboard: arrows move, Enter chooses.
 */

export interface QuickMenuItem {
  label: string;
  icon?: LucideIcon;
  variant?: 'default' | 'danger';
  disabled?: boolean;
  onSelect: () => void;
}

export interface QuickMenuProps {
  label: string;
  icon: LucideIcon;
  items: (QuickMenuItem | null)[];
  align?: 'start' | 'end';
  testId?: string;
}

export function QuickMenu({ label, icon, items, align = 'end', testId = 'quick-menu' }: QuickMenuProps) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const root = useRef<HTMLDivElement>(null);
  const id = useId();
  const real = items.filter((i): i is QuickMenuItem => i !== null && !i.disabled);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (root.current && !root.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
      else if (e.key === 'ArrowDown') {
        e.preventDefault();
        setActive((a) => (a + 1) % Math.max(1, real.length));
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        setActive((a) => (a - 1 + real.length) % Math.max(1, real.length));
      } else if (e.key === 'Enter') {
        e.preventDefault();
        real[active]?.onSelect();
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open, active, real]);

  return (
    <div className="fs-qmenu" ref={root}>
      <IconButton icon={icon} label={label} size="sm" onClick={() => setOpen((v) => !v)} testId={testId} />
      {open && (
        <div className="fs-menu fs-qmenu__list" role="menu" id={id} data-align={align} data-testid={`${testId}-list`}>
          {items.map((item, index) =>
            item === null ? (
              <div key={`sep-${index}`} className="fs-menu__sep" role="separator" />
            ) : (
              <button
                key={item.label}
                type="button"
                role="menuitem"
                className="fs-menu__item"
                data-variant={item.variant ?? 'default'}
                data-active={real[active] === item || undefined}
                data-testid={`menu-item-${slug(item.label)}`}
                disabled={item.disabled}
                onMouseEnter={() => setActive(Math.max(0, real.indexOf(item)))}
                onClick={() => {
                  item.onSelect();
                  setOpen(false);
                }}
              >
                {item.icon && <item.icon size={15} aria-hidden="true" />}
                {item.label}
              </button>
            ),
          )}
        </div>
      )}
    </div>
  );
}
