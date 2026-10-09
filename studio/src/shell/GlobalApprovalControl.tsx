import { Shield } from 'lucide-react';
import { Popover } from '../components/Popover';
import { t, useLang } from '../i18n';
import { refreshGlobalApproval, setGlobalApprovalMode, useGlobalApprovals, type GlobalApprovalMode } from './globalApprovals';
import './global-approvals.css';

export const GLOBAL_APPROVAL_CHOICES = [
  { value: 'ask', label: 'Ask', detail: 'Confirm actions when the current tool checks require approval.' },
  { value: 'auto', label: 'Automatic', detail: 'Uses the current rules; some tools may still ask for approval.' },
  { value: 'full', label: 'Full access', detail: 'Runs tools without asking for approval.' },
] as const;

export function GlobalApprovalControl({ variant = 'shortcut' }: { variant?: 'shortcut' | 'nav' | 'settings' }) {
  useLang();
  const state = useGlobalApprovals();
  const choice = GLOBAL_APPROVAL_CHOICES.find(value => value.value === state.mode)!;
  const label = t(choice.label);
  const content = <>
    <p className="fs-global-approvals__scope">{t('Applies to every Faustus conversation. Changes are saved globally.')}</p>
    <div className="fs-global-approvals__choices" role="radiogroup" aria-label={t('Global approvals')}>
      {GLOBAL_APPROVAL_CHOICES.map(value => <button key={value.value} type="button" role="radio" aria-checked={state.mode === value.value}
        disabled={!state.ready || state.saving} data-testid={`global-approval-${value.value}`} onClick={() => void setGlobalApprovalMode(value.value as GlobalApprovalMode).catch(() => undefined)}>
        <strong>{t(value.label)}</strong><span>{t(value.detail)}</span>
      </button>)}
    </div>
    {state.error && <p role="status">{t(state.error)} <button type="button" onClick={() => void refreshGlobalApproval()}>{t('Retry')}</button></p>}
  </>;
  if (variant === 'settings') return <section className="fs-global-approvals fs-global-approvals--settings" data-testid="global-approval-settings">
    <h2>{t('Global approvals')}</h2>{content}
  </section>;
  return <Popover side={variant === 'nav' ? 'top' : undefined} placement={variant === 'shortcut' ? 'composer' : undefined}
    className="fs-global-approvals fs-global-approvals__menu" testId="global-approval-menu"
    trigger={<button type="button" className={`fs-global-approvals__trigger fs-global-approvals__trigger--${variant}`}
      disabled={!state.ready || state.saving} data-permission={state.mode} data-testid={`global-approval-${variant}`}
      aria-label={t('Global approvals: {label}', { label })} title={t('Global approvals: {label}', { label })}>
      <Shield size={15} aria-hidden="true" /><span><span className="fs-global-approvals__global">{t('Global')}</span><span>{label}</span></span>
    </button>}>
    {content}
  </Popover>;
}
