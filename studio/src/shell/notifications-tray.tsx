import { Bell, BellOff, Check, X } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router';
import { IconButton, Popover } from '../components';
import { t } from '../i18n';
import {
  dismissNotification,
  listNotifications,
  markAllNotificationsRead,
  markNotificationRead,
  type StoredNotification,
} from './notifications';
import './notifications-tray.css';

/**
 * ACT-02, frontend half: the persistent in-app tray. Clicking a row marks it
 * read and navigates straight to the run it is about
 * (`/activity?run=<target.run>`), the "click opens the exact step" line from
 * the spec — never a generic Activity landing page.
 *
 * Desktop notifications (the other optional channel) are asked for lazily,
 * on first open, and only fired for a row whose `desktop_allowed` the server
 * already computed from this owner's per-type/channel prefs and quiet hours
 * — this component never re-derives that decision.
 */

const POLL_MS = 20000;

export function NotificationTray() {
  const [rows, setRows] = useState<StoredNotification[]>([]);
  const [unread, setUnread] = useState(0);
  const navigate = useNavigate();
  const notifiedDesktop = useRef(new Set<string>());

  const reload = async () => {
    try {
      const data = await listNotifications();
      setRows(data.notifications);
      setUnread(data.unread);
      fireDesktopNotifications(data.notifications, notifiedDesktop.current);
    } catch {
      /* the poll retries on its own timer; a single failed tick stays silent */
    }
  };

  useEffect(() => {
    void reload();
    const id = window.setInterval(() => void reload(), POLL_MS);
    return () => window.clearInterval(id);
  }, []);

  const open = async (row: StoredNotification) => {
    if (!row.read) {
      setRows((cur) => cur.map((r) => (r.id === row.id ? { ...r, read: true } : r)));
      setUnread((n) => Math.max(0, n - 1));
      try {
        await markNotificationRead(row.id);
      } catch {
        /* best-effort; next poll reconciles */
      }
    }
    if (row.target.screen === 'activity' && row.target.run) navigate(`/activity?run=${encodeURIComponent(row.target.run)}`);
  };

  const dismiss = async (row: StoredNotification, event: React.MouseEvent) => {
    event.stopPropagation();
    setRows((cur) => cur.filter((r) => r.id !== row.id));
    if (!row.read) setUnread((n) => Math.max(0, n - 1));
    try {
      await dismissNotification(row.id);
    } catch {
      void reload();
    }
  };

  const readAll = async () => {
    setRows((cur) => cur.map((r) => ({ ...r, read: true })));
    setUnread(0);
    try {
      await markAllNotificationsRead();
    } catch {
      void reload();
    }
  };

  return (
    <Popover
      side="bottom"
      align="end"
      className="fs-notif-tray"
      testId="notification-tray"
      trigger={
        <span className="fs-notif-tray__trigger">
          <IconButton icon={unread > 0 ? Bell : BellOff} label={unread > 0 ? t('{n} unread notifications', { n: unread }) : t('Notifications')} testId="notification-bell" />
          {unread > 0 && <span className="fs-notif-tray__badge" aria-hidden="true">{unread > 9 ? '9+' : unread}</span>}
        </span>
      }
    >
      <div className="fs-notif-tray__head">
        <strong>{t('Notifications')}</strong>
        {unread > 0 && <button type="button" className="fs-notif-tray__mark-all" onClick={() => void readAll()} data-testid="notification-read-all"><Check size={12} aria-hidden="true" /> {t('Mark all read')}</button>}
      </div>
      {rows.length === 0 && <p className="fs-notif-tray__empty">{t('Nothing waiting for you.')}</p>}
      <ul className="fs-notif-tray__list" role="list">
        {rows.map((row) => (
          <li key={row.id}>
            <button type="button" className="fs-notif-tray__row" data-unread={!row.read || undefined} onClick={() => void open(row)} data-testid="notification-row">
              <span className="fs-notif-tray__row-title">{row.title}</span>
              {row.detail && <span className="fs-notif-tray__row-detail">{row.detail}</span>}
            </button>
            <button type="button" className="fs-notif-tray__dismiss" aria-label={t('Dismiss')} onClick={(e) => void dismiss(row, e)}>
              <X size={11} aria-hidden="true" />
            </button>
          </li>
        ))}
      </ul>
    </Popover>
  );
}

const askedForPermission = { current: false };

function fireDesktopNotifications(rows: StoredNotification[], notified: Set<string>): void {
  if (typeof Notification === 'undefined') return;
  const wanted = rows.filter((r) => r.desktop_allowed && !r.read && !notified.has(r.id));
  if (!wanted.length) return;
  if (Notification.permission === 'default' && !askedForPermission.current) {
    askedForPermission.current = true;
    void Notification.requestPermission().then(() => fireDesktopNotifications(rows, notified));
    return;
  }
  if (Notification.permission !== 'granted') return;
  for (const row of wanted) {
    notified.add(row.id);
    // The server already stripped anything token-shaped from title/detail
    // (routes/notifications_routes.py's `_scrub`) — nothing here needs to
    // second-guess what is safe to show on a lock screen.
    new Notification(row.title, { body: row.detail || undefined, tag: row.dedupe_key });
  }
}
