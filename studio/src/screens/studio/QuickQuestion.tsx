import { useEffect, type RefObject } from 'react';
import { ExternalLink, X } from 'lucide-react';
import { Button, IconButton } from '../../components';
import { t } from '../../i18n';
import './quick-question.css';

declare global {
  interface Window {
    faustusQuick?: {
      isQuick: boolean;
      openStudio(id: string): Promise<unknown>;
      hide(): Promise<unknown>;
      onFocus(callback: () => void): () => void;
    };
  }
}

export function QuickQuestion({ input, onContinue }: {
  input: RefObject<HTMLTextAreaElement | null>; onContinue: () => Promise<void>;
}) {
  useEffect(() => {
    const focus = () => input.current?.focus();
    const timer = window.setTimeout(focus, 150);
    const unsubscribe = window.faustusQuick?.onFocus(focus);
    return () => { clearTimeout(timer); unsubscribe?.(); };
  }, [input]);
  return <header className="fs-quick" data-testid="quick-question">
    <h1>{t('Quick question')}</h1>
    <Button variant="ghost" size="sm" icon={ExternalLink} label={t('Continue in Studio')} onClick={() => void onContinue()} />
    {window.faustusQuick && <IconButton icon={X} label={t('Close window')} size="sm" onClick={() => void window.faustusQuick?.hide()} />}
  </header>;
}
