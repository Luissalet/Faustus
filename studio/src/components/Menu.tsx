import * as RadixMenu from '@radix-ui/react-dropdown-menu';
import type { LucideIcon } from 'lucide-react';
import type { ReactNode } from 'react';
import { slug } from './testid';

export interface MenuItem {
  label: string;
  icon?: LucideIcon;
  variant?: 'default' | 'danger';
  disabled?: boolean;
  onSelect?: () => void;
}

export interface MenuProps {
  /** The control that opens the menu. Must be focusable on its own. */
  trigger: ReactNode;
  /** `null` marks a separator, so groups read as groups to a screen reader too. */
  items: (MenuItem | null)[];
  align?: 'start' | 'center' | 'end';
  testId?: string;
}

export function Menu({ trigger, items, align = 'start', testId = 'menu' }: MenuProps) {
  return (
    <RadixMenu.Root>
      <RadixMenu.Trigger asChild>{trigger}</RadixMenu.Trigger>
      <RadixMenu.Portal>
        <RadixMenu.Content
          className="fs-menu"
          align={align}
          sideOffset={6}
          data-testid={testId}
        >
          {items.map((item, index) =>
            item === null ? (
              <RadixMenu.Separator key={`sep-${index}`} className="fs-menu__sep" />
            ) : (
              <RadixMenu.Item
                key={item.label}
                className="fs-menu__item"
                data-variant={item.variant ?? 'default'}
                data-testid={`menu-item-${slug(item.label)}`}
                disabled={item.disabled}
                onSelect={item.onSelect}
              >
                {item.icon && <item.icon size={15} aria-hidden="true" />}
                {item.label}
              </RadixMenu.Item>
            ),
          )}
        </RadixMenu.Content>
      </RadixMenu.Portal>
    </RadixMenu.Root>
  );
}
