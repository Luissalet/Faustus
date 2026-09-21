/**
 * What each answer to the "Needs your permission" card actually does.
 *
 * 20-09-2026 — the card carried four hand-written buttons, written before
 * the scopes settled, and their wording had drifted from the decisions they
 * send: «Approve» was chat-session scope, and «Approve the whole task» was
 * the NARROWEST one (this turn only). Clicking the wide-sounding button
 * therefore left every later turn asking again, which reads as a broken
 * folder grant. The server already names every scope it offers
 * (`src/tool_approvals.py`'s `public_payload`), so the card renders those,
 * narrowest first; the table below only covers history rows saved without
 * options.
 */
import { t } from '../../i18n';
import type { AskOption } from '../../adapters/chat';

export type Decision = 'approve' | 'approve_task' | 'approve_workspace' | 'deny';

export interface ApprovalChoice {
  decision: Decision;
  label: string;
  description: string;
  variant: 'primary' | 'danger' | undefined;
}

const FALLBACK: { decision: Decision; label: string; description: string }[] = [
  {
    decision: 'approve_task',
    label: 'Allow for this task',
    description: 'Runs the action and whatever else this request needs. Asks again next turn.',
  },
  {
    decision: 'approve',
    label: 'Allow for this chat',
    description: 'Stops asking at this gate for the rest of this conversation.',
  },
  {
    decision: 'approve_workspace',
    label: 'Always for this folder',
    description:
      'Remembers the answer for this workspace folder: later chats in it stop asking here. Destructive-command and desktop confirmations still apply.',
  },
  { decision: 'deny', label: 'Deny', description: 'Does not run the proposed action.' },
];

const DECISIONS = new Set<string>(FALLBACK.map((choice) => choice.decision));

export function approvalChoices(options: AskOption[] | undefined): ApprovalChoice[] {
  const served = new Map<string, AskOption>();
  for (const option of options || []) {
    const value = String(option.value || '');
    if (DECISIONS.has(value) && option.label) served.set(value, option);
  }
  return FALLBACK.map((choice) => {
    const option = served.get(choice.decision);
    return {
      decision: choice.decision,
      label: t(option?.label || choice.label),
      description: option?.description || t(choice.description),
      variant:
        choice.decision === 'deny' ? ('danger' as const)
        : choice.decision === 'approve_task' ? ('primary' as const)
        : undefined,
    };
  });
}
