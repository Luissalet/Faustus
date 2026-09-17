import { Sparkles } from 'lucide-react';
import { useState } from 'react';
import { Button, Popover } from '../../components';
import { waAssist, type WaAssistTask } from '../../adapters/whatsapp';
import { t } from '../../i18n';

interface QuickAction {
  task: WaAssistTask;
  label: string;
  instruction?: string;
}

const QUICK_ACTIONS: QuickAction[] = [
  { task: 'summarize', label: 'Summarise this chat' },
  { task: 'draft_reply', label: 'Draft a reply' },
  { task: 'translate', label: 'Translate to Spanish', instruction: 'Spanish (España)' },
  { task: 'translate', label: 'Translate to English', instruction: 'English' },
];

export interface AskFaustusProps {
  chatJid: string;
  chatName: string;
  onUseAsDraft: (text: string) => void;
  say: (msg: string) => void;
}

export function AskFaustus({ chatJid, chatName, onUseAsDraft, say }: AskFaustusProps) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [question, setQuestion] = useState('');
  const [result, setResult] = useState<{ text: string; task: WaAssistTask } | null>(null);

  const run = async (task: WaAssistTask, instruction?: string) => {
    setBusy(true);
    setResult(null);
    try {
      const r = await waAssist(chatJid, task, instruction);
      setResult({ text: r.text, task });
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const openInFaustus = `/studio?draft=${encodeURIComponent(`Lee mi WhatsApp con ${chatName} (whatsapp_read, chat "${chatName}") y `)}`;

  return (
    <Popover
      open={open}
      onOpenChange={(v) => { setOpen(v); if (!v) { setResult(null); setQuestion(''); } }}
      trigger={<Button icon={Sparkles} variant="ghost" size="sm" label={t('Ask Faustus')} testId="whatsapp-assist" />}
      testId="whatsapp-assist-popover"
      className="fs-wa__assist-popover"
    >
      <div className="fs-wa__assist">
        <div className="fs-wa__assist-quick">
          {QUICK_ACTIONS.map((a) => (
            <Button
              key={a.label}
              size="sm"
              variant="secondary"
              label={t(a.label)}
              disabled={busy}
              onClick={() => void run(a.task, a.instruction)}
            />
          ))}
        </div>
        <div className="fs-wa__assist-ask">
          <textarea
            value={question}
            placeholder={t('Ask anything about this chat')}
            onChange={(e) => setQuestion(e.target.value)}
            rows={2}
            data-testid="whatsapp-assist-input"
          />
          <Button
            size="sm"
            variant="primary"
            label={t('Ask')}
            loading={busy}
            disabled={!question.trim()}
            onClick={() => void run('custom', question.trim())}
          />
        </div>

        {busy && <p className="fs-set__help">{t('Thinking…')}</p>}

        {result && !busy && (
          <div className="fs-wa__assist-result" data-testid="whatsapp-assist-result">
            <p className="fs-wa__assist-text">{result.text || t('Nothing to show.')}</p>
            <div className="fs-wa__assist-actions">
              <Button size="sm" variant="primary" label={t('Use as draft')} onClick={() => { onUseAsDraft(result.text); setOpen(false); }} />
              <Button size="sm" variant="ghost" label={t('Copy')} onClick={() => void navigator.clipboard.writeText(result.text).catch(() => {})} />
              {result.task === 'draft_reply' && (
                <Button size="sm" variant="ghost" label={t('Regenerate')} loading={busy} onClick={() => void run('draft_reply')} />
              )}
            </div>
          </div>
        )}

        <a className="fs-wa__assist-open" href={openInFaustus} data-testid="whatsapp-assist-open-faustus">
          {t('Open in Faustus')}
        </a>
      </div>
    </Popover>
  );
}
