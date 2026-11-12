import { useLocation } from 'react-router';
import { useChatActivity } from './activity';
import { useDisplay } from './display';
import { openSession, pulseView } from './pulseState';
import './status-pulse.css';

/** Decorative overlay; the activity screen and chat dots carry the text equivalent. */
export function StatusPulse() {
  const display = useDisplay();
  const activity = useChatActivity();
  const { pathname, search } = useLocation();
  if (!display.pulseBar) return null;
  const view = pulseView(activity, openSession(pathname, search));
  return (
    <div
      className="fs-pulse"
      data-state={view.state}
      data-waiting={view.waiting || undefined}
      data-testid="status-pulse"
      role="presentation"
      aria-hidden="true"
    />
  );
}
