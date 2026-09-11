import { useState } from 'react';
import { Button, Dialog } from '../../components';
import { createIssue, ISSUE_PRIORITIES, ISSUE_TYPES, type CreateIssueInput, type Issue, type IssuePriority, type IssueType } from '../../adapters/board';
import { t } from '../../i18n';
import { typeLabel, priorityLabel } from './IssueCard';

const toLabels = (s: string): string[] => s.split(/[,\n]/).map((x) => x.trim()).filter(Boolean);
const toBlockedBy = (s: string): { kind: 'blocked_by'; target: string }[] =>
  s.split(/[,\n]/).map((x) => x.trim()).filter(Boolean).map((target) => ({ kind: 'blocked_by' as const, target }));

/**
 * Lote 93 — "New issue". `quick` (the chat panel's own button, and used from
 * a Board-tab toolbar the same way) drops the body/labels/blocked-by rows
 * to just type + title, so logging a bug mid-conversation costs one line.
 */
export function NewIssueDialog({
  open,
  onOpenChange,
  projectId,
  quick = false,
  defaultType = 'task',
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  projectId: string;
  quick?: boolean;
  defaultType?: IssueType;
  onCreated: (issue: Issue) => void;
}) {
  const [type, setType] = useState<IssueType>(defaultType);
  const [title, setTitle] = useState('');
  const [body, setBody] = useState('');
  const [priority, setPriority] = useState<IssuePriority>('P2');
  const [labels, setLabels] = useState('');
  const [blockedBy, setBlockedBy] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reset = () => {
    setType(defaultType);
    setTitle('');
    setBody('');
    setPriority('P2');
    setLabels('');
    setBlockedBy('');
    setError(null);
  };

  const submit = async () => {
    if (!title.trim() || busy) return;
    setBusy(true);
    setError(null);
    const input: CreateIssueInput = {
      type,
      title: title.trim(),
      body_md: body.trim() || undefined,
      priority,
      labels: quick ? undefined : toLabels(labels),
      links: quick ? undefined : toBlockedBy(blockedBy),
    };
    try {
      const { issue } = await createIssue(projectId, input);
      onCreated(issue);
      reset();
      onOpenChange(false);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        if (!o) reset();
        onOpenChange(o);
      }}
      title={t('New issue')}
      testId="board-new-issue-dialog"
      footer={
        <>
          <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => onOpenChange(false)} />
          <Button
            variant="primary"
            size="sm"
            label={t('Create')}
            loading={busy}
            disabled={!title.trim()}
            onClick={() => void submit()}
            testId="board-new-issue-submit"
          />
        </>
      }
    >
      <form
        className="fs-issue-form"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <label className="fs-field-label">
          {t('Type')}
          <select className="fs-field" value={type} onChange={(e) => setType(e.target.value as IssueType)} data-testid="board-new-issue-type">
            {ISSUE_TYPES.map((ty) => (
              <option key={ty} value={ty}>{typeLabel(ty)}</option>
            ))}
          </select>
        </label>
        <label className="fs-field-label">
          {t('Title')}
          <input
            className="fs-field"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder={t('What needs doing?')}
            autoFocus
            data-testid="board-new-issue-title"
          />
        </label>
        {!quick && (
          <>
            <label className="fs-field-label">
              {t('Description (optional)')}
              <textarea
                className="fs-field fs-issue-form__body"
                rows={4}
                value={body}
                onChange={(e) => setBody(e.target.value)}
                placeholder={t('Markdown is fine.')}
                data-testid="board-new-issue-body"
              />
            </label>
            <label className="fs-field-label">
              {t('Priority')}
              <select className="fs-field" value={priority} onChange={(e) => setPriority(e.target.value as IssuePriority)}>
                {ISSUE_PRIORITIES.map((p) => (
                  <option key={p} value={p}>{priorityLabel(p)}</option>
                ))}
              </select>
            </label>
            <label className="fs-field-label">
              {t('Labels (comma-separated, optional)')}
              <input className="fs-field" value={labels} onChange={(e) => setLabels(e.target.value)} placeholder="frontend, quick-win" />
            </label>
            <label className="fs-field-label">
              {t('Blocked by (issue ids, comma-separated, optional)')}
              <input className="fs-field" value={blockedBy} onChange={(e) => setBlockedBy(e.target.value)} placeholder="FAU-9, FAU-11" />
            </label>
          </>
        )}
        {quick && (
          <label className="fs-field-label">
            {t('Priority')}
            <select className="fs-field" value={priority} onChange={(e) => setPriority(e.target.value as IssuePriority)}>
              {ISSUE_PRIORITIES.map((p) => (
                <option key={p} value={p}>{priorityLabel(p)}</option>
              ))}
            </select>
          </label>
        )}
        {error && <p className="fs-notice" data-tone="danger" role="alert">{error}</p>}
      </form>
    </Dialog>
  );
}
