import { Save, X } from 'lucide-react';
import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { Button } from '../../components';
import { listEndpoints, type ModelEndpoint } from '../../adapters/settings';
import {
  buildEmailTarget,
  createAutomation,
  DAYS,
  EMAIL_ACCOUNT_ACTIONS,
  emailAccountsForTasks,
  listActions,
  listEvents,
  listOutputTargets,
  localToUtc,
  parseEmailTarget,
  PERSONAS,
  promptConfig,
  saveUrgentEmailPrompt,
  updateAutomation,
  urgentEmailPrompt,
  utcToLocal,
  type ActionInfo,
  type Automation,
  type EmailAccountLite,
  type EventInfo,
  type OutputTarget,
  type Schedule,
  type TaskInput,
  type TaskType,
  type TriggerType,
} from '../../adapters/automations';
import { t } from '../../i18n';

/**
 * The recipe editor: what it does, when it fires, where it delivers. Same
 * fields the previous form had, native date and time inputs instead of
 * the hand-made pickers, and everything visible at once — no wizard.
 */

interface Draft {
  name: string;
  taskType: TaskType;
  prompt: string;
  persona: string;
  action: string;
  emailAccount: string;
  urgentRules: string;
  trigger: TriggerType;
  schedule: Schedule;
  time: string;
  weekday: number;
  monthday: number;
  date: string;
  cron: string;
  event: string;
  every: number;
  output: string;
  mailTo: string;
  mailAccount: string;
  modelPair: string;
  chain: string;
  notify: boolean;
}

function localDateInput(iso: string | null | undefined): string {
  const d = iso ? new Date(iso) : new Date();
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function draftFrom(existing: Partial<Automation> | null, taskType: TaskType, trigger: TriggerType): Draft {
  const mail = parseEmailTarget(existing?.output_target);
  const output = mail.enabled ? 'email' : existing?.output_target || 'session';
  const cfg = existing?.task_type === 'action' ? promptConfig(existing?.prompt) : {};
  return {
    name: existing?.name ?? '',
    taskType: (existing?.task_type as TaskType) || taskType,
    prompt: existing?.task_type === 'action' ? '' : (existing?.prompt ?? ''),
    persona: (existing?.character_id ?? '').toLowerCase(),
    action: existing?.action ?? '',
    emailAccount: cfg.account_id || cfg.email_account_id || '',
    urgentRules: '',
    trigger: (existing?.trigger_type as TriggerType) || trigger,
    schedule: (existing?.schedule as Schedule) || 'daily',
    time: existing?.scheduled_time ? utcToLocal(existing.scheduled_time) : '09:00',
    weekday: typeof existing?.scheduled_day === 'number' && existing.schedule === 'weekly' ? existing.scheduled_day : 0,
    monthday: typeof existing?.scheduled_day === 'number' && existing.schedule === 'monthly' ? existing.scheduled_day : 1,
    date: localDateInput(existing?.scheduled_date),
    cron: existing?.cron_expression ?? '',
    event: existing?.trigger_event ?? '',
    every: existing?.trigger_count ?? 5,
    output,
    mailTo: mail.to,
    mailAccount: mail.accountId,
    modelPair: existing?.model && existing?.endpoint_url ? `${existing.endpoint_url}::${existing.model}` : '',
    chain: existing?.then_task_id ?? '',
    notify: existing?.notifications_enabled !== false,
  };
}

function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="fs-au__field">
      <span className="fs-au__label">
        {label}
        {hint && <small>{hint}</small>}
      </span>
      {children}
    </label>
  );
}

function Segmented<T extends string>({ value, options, onChange, label }: { value: T; options: { value: T; label: string }[]; onChange: (v: T) => void; label: string }) {
  return (
    <div className="fs-au__seg" role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <button key={o.value} type="button" role="radio" aria-checked={value === o.value} className="fs-au__seg-btn" onClick={() => onChange(o.value)} data-testid={`auto-seg-${o.value}`}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

export interface FormProps {
  existing: Automation | null;
  /** A draft the server wrote from a sentence, or a preset's two choices. */
  seed?: Partial<Automation> | null;
  taskType?: TaskType;
  trigger?: TriggerType;
  others: Automation[];
  onSaved: (task: Automation | null) => void;
  onCancel: () => void;
}

export function AutomationForm({ existing, seed, taskType = 'llm', trigger = 'schedule', others, onSaved, onCancel }: FormProps) {
  const [d, setD] = useState<Draft>(() => draftFrom(existing ?? seed ?? null, taskType, trigger));
  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => setD((prev) => ({ ...prev, [k]: v }));
  const [targets, setTargets] = useState<OutputTarget[]>([]);
  const [actions, setActions] = useState<ActionInfo[]>([]);
  const [events, setEvents] = useState<EventInfo[]>([]);
  const [endpoints, setEndpoints] = useState<ModelEndpoint[]>([]);
  const [accounts, setAccounts] = useState<EmailAccountLite[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void listOutputTargets().then(setTargets);
    void listActions().then((list) => {
      setActions(list);
      setD((prev) => (prev.action || !list.length ? prev : { ...prev, action: list[0].name }));
    });
    void listEvents().then((list) => {
      setEvents(list);
      setD((prev) => (prev.event || !list.length ? prev : { ...prev, event: list[0].name }));
    });
    void listEndpoints().then(setEndpoints).catch(() => setEndpoints([]));
    void emailAccountsForTasks().then(setAccounts);
  }, []);

  // The urgent-mail rules are an account setting, loaded once the action asks for them.
  useEffect(() => {
    if (d.action !== 'check_email_urgency' || d.taskType !== 'action') return;
    void urgentEmailPrompt().then((p) => setD((prev) => (prev.urgentRules ? prev : { ...prev, urgentRules: p })));
  }, [d.action, d.taskType]);

  const modelGroups = useMemo(() => endpoints.filter((e) => e.enabled !== false).map((e) => ({ id: e.id, label: e.name || e.baseUrl, url: e.baseUrl, models: [...new Set(e.models)] })), [endpoints]);
  const modelListed = modelGroups.some((g) => g.models.some((m) => `${g.url}::${m}` === d.modelPair));

  const save = async () => {
    setError(null);
    const body: TaskInput = { task_type: d.taskType, trigger_type: d.trigger, notifications_enabled: d.notify, then_task_id: d.chain };
    if (d.name.trim()) body.name = d.name.trim();
    else if (!existing) body.name = undefined;
    if (d.modelPair) {
      const i = d.modelPair.indexOf('::');
      body.endpoint_url = d.modelPair.slice(0, i);
      body.model = d.modelPair.slice(i + 2);
    } else {
      body.endpoint_url = '';
      body.model = '';
    }
    if (d.taskType === 'action') {
      if (!d.action) return setError(t('Pick an action'));
      body.action = d.action;
      body.prompt = EMAIL_ACCOUNT_ACTIONS.has(d.action) && d.emailAccount ? JSON.stringify({ account_id: d.emailAccount }) : '';
      body.character_id = '';
    } else {
      if (!d.prompt.trim()) return setError(d.taskType === 'research' ? t('Write the research question') : t('Write the prompt'));
      body.prompt = d.prompt.trim();
      body.character_id = d.persona;
    }
    if (d.trigger === 'schedule') {
      body.schedule = d.schedule;
      if (d.schedule === 'cron') {
        if (!d.cron.trim()) return setError(t('Write the cron expression'));
        body.cron_expression = d.cron.trim();
      } else {
        body.scheduled_time = localToUtc(d.time);
        if (d.schedule === 'weekly') body.scheduled_day = d.weekday;
        if (d.schedule === 'monthly') body.scheduled_day = d.monthday;
        if (d.schedule === 'once') {
          const [y, mo, day] = d.date.split('-').map(Number);
          const [h, mi] = d.time.split(':').map(Number);
          body.scheduled_date = new Date(y, mo - 1, day, h || 0, mi || 0).toISOString();
        }
      }
    } else if (d.trigger === 'event') {
      if (!d.event) return setError(t('Pick an event'));
      body.trigger_event = d.event;
      body.trigger_count = Math.max(1, Math.min(1000, d.every || 1));
    }
    body.output_target = d.output === 'email' ? buildEmailTarget(d.mailTo, d.mailAccount) : d.output;

    setSaving(true);
    try {
      if (d.taskType === 'action' && d.action === 'check_email_urgency') await saveUrgentEmailPrompt(d.urgentRules);
      if (existing) {
        await updateAutomation(existing.id, body);
        onSaved(null);
      } else {
        onSaved(await createAutomation(body));
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const isMailAction = d.taskType === 'action' && EMAIL_ACCOUNT_ACTIONS.has(d.action);

  return (
    <form
      className="fs-au__form"
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
      data-testid="auto-form"
    >
      <Field label={t('Name')} hint={existing ? undefined : t('Left blank, the assistant names it from the prompt.')}>
        <input className="fs-field" value={d.name} onChange={(e) => set('name', e.target.value)} data-testid="auto-name" />
      </Field>

      <div className="fs-au__group">
        <span className="fs-au__label">{t('What it does')}</span>
        <Segmented
          value={d.taskType}
          label={t('Type')}
          onChange={(v) => set('taskType', v)}
          options={[
            { value: 'llm', label: t('A prompt') },
            { value: 'action', label: t('A built-in action') },
            { value: 'research', label: t('Deep research') },
          ]}
        />
        {d.taskType === 'action' ? (
          <>
            <Field label={t('Action')}>
              <select className="fs-field" value={d.action} onChange={(e) => set('action', e.target.value)} data-testid="auto-action">
                {actions.map((a) => (
                  <option key={a.name} value={a.name}>
                    {a.name.replace(/_/g, ' ')} — {a.description}
                  </option>
                ))}
              </select>
            </Field>
            {isMailAction && (
              <Field label={t('Mail account')}>
                <select className="fs-field" value={d.emailAccount} onChange={(e) => set('emailAccount', e.target.value)}>
                  <option value="">{t('Default account')}</option>
                  {accounts.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.label}
                      {a.isDefault ? ` (${t('default')})` : ''}
                    </option>
                  ))}
                </select>
              </Field>
            )}
            {d.action === 'check_email_urgency' && (
              <Field label={t('Mail triage rules')} hint={t('What counts as urgent: deadlines, people, subjects. This task tags the mail; pausing it stops the tagging.')}>
                <textarea className="fs-field fs-au__textarea" rows={4} value={d.urgentRules} onChange={(e) => set('urgentRules', e.target.value)} />
              </Field>
            )}
          </>
        ) : (
          <>
            <Field label={d.taskType === 'research' ? t('Research question') : t('Prompt')}>
              <textarea
                className="fs-field fs-au__textarea"
                rows={5}
                value={d.prompt}
                onChange={(e) => set('prompt', e.target.value)}
                placeholder={d.taskType === 'research' ? t('What should it dig into?') : t('What should it do each time?')}
                data-testid="auto-prompt"
              />
            </Field>
            <Field label={t('Persona')} hint={t('Optional; it colours the voice of the result.')}>
              <select className="fs-field" value={d.persona} onChange={(e) => set('persona', e.target.value)}>
                {PERSONAS.map((p) => (
                  <option key={p.value} value={p.value}>
                    {t(p.label)}
                  </option>
                ))}
              </select>
            </Field>
          </>
        )}
      </div>

      <div className="fs-au__group">
        <span className="fs-au__label">{t('When it fires')}</span>
        <Segmented
          value={d.trigger}
          label={t('Trigger')}
          onChange={(v) => set('trigger', v)}
          options={[
            { value: 'schedule', label: t('On a schedule') },
            { value: 'event', label: t('On an event') },
            { value: 'webhook', label: t('On a webhook') },
          ]}
        />
        {d.trigger === 'schedule' && (
          <div className="fs-au__row">
            <Field label={t('Frequency')}>
              <select className="fs-field" value={d.schedule} onChange={(e) => set('schedule', e.target.value as Schedule)} data-testid="auto-schedule">
                <option value="daily">{t('Every day')}</option>
                <option value="weekly">{t('Every week')}</option>
                <option value="monthly">{t('Every month')}</option>
                <option value="once">{t('Once')}</option>
                <option value="cron">{t('Cron')}</option>
              </select>
            </Field>
            {d.schedule === 'weekly' && (
              <Field label={t('Day of the week')}>
                <select className="fs-field" value={d.weekday} onChange={(e) => set('weekday', Number(e.target.value))}>
                  {DAYS.map((day, i) => (
                    <option key={day} value={i}>
                      {t(day)}
                    </option>
                  ))}
                </select>
              </Field>
            )}
            {d.schedule === 'monthly' && (
              <Field label={t('Day of the month')}>
                <input className="fs-field" type="number" min={1} max={31} value={d.monthday} onChange={(e) => set('monthday', Math.max(1, Math.min(31, Number(e.target.value) || 1)))} />
              </Field>
            )}
            {d.schedule === 'once' && (
              <Field label={t('Date')}>
                <input className="fs-field" type="date" value={d.date} onChange={(e) => set('date', e.target.value)} />
              </Field>
            )}
            {d.schedule === 'cron' ? (
              <Field label={t('Cron expression')} hint={t('minute hour day month weekday — "0 */2 * * *" is every two hours')}>
                <input className="fs-field" value={d.cron} onChange={(e) => set('cron', e.target.value)} placeholder="*/30 * * * *" spellCheck={false} />
              </Field>
            ) : (
              <Field label={t('Time')} hint={t('Your local time.')}>
                <input className="fs-field" type="time" value={d.time} onChange={(e) => set('time', e.target.value)} data-testid="auto-time" />
              </Field>
            )}
          </div>
        )}
        {d.trigger === 'event' && (
          <div className="fs-au__row">
            <Field label={t('Event')}>
              <select className="fs-field" value={d.event} onChange={(e) => set('event', e.target.value)}>
                {events.map((ev) => (
                  <option key={ev.name} value={ev.name}>
                    {ev.name.replace(/_/g, ' ')} — {ev.description}
                  </option>
                ))}
              </select>
            </Field>
            <Field label={t('Every N times')}>
              <input className="fs-field" type="number" min={1} max={1000} value={d.every} onChange={(e) => set('every', Number(e.target.value) || 1)} />
            </Field>
          </div>
        )}
        {d.trigger === 'webhook' && <p className="fs-au__hint">{existing?.webhook_token ? t('The URL is on the automation, with a button to copy it or issue a new one.') : t('The URL appears once it is saved.')}</p>}
      </div>

      <div className="fs-au__group">
        <span className="fs-au__label">{t('Where it delivers')}</span>
        <div className="fs-au__row">
          <Field label={t('Output')}>
            <select className="fs-field" value={d.output} onChange={(e) => set('output', e.target.value)} data-testid="auto-output">
              {targets.map((o) => (
                <option key={o.value} value={o.value} title={o.description}>
                  {o.label}
                </option>
              ))}
              <option value="none">{t('Nowhere — it just runs')}</option>
              {!targets.some((o) => o.value === d.output) && d.output !== 'none' && <option value={d.output}>{d.output}</option>}
            </select>
          </Field>
          {d.output === 'email' && (
            <>
              <Field label={t('Send from')}>
                <select className="fs-field" value={d.mailAccount} onChange={(e) => set('mailAccount', e.target.value)}>
                  <option value="">{t('Default sending account')}</option>
                  {accounts.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.label}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label={t('To')} hint={t('Blank sends it to you.')}>
                <input className="fs-field" type="email" value={d.mailTo} onChange={(e) => set('mailTo', e.target.value)} />
              </Field>
            </>
          )}
        </div>
      </div>

      <details className="fs-au__more" open={Boolean(d.modelPair || d.chain || !d.notify)}>
        <summary>{t('Model, chaining and notifications')}</summary>
        <div className="fs-au__row">
          <Field label={t('Model')} hint={t('Optional; overrides the session default.')}>
            <select className="fs-field" value={d.modelPair} onChange={(e) => set('modelPair', e.target.value)}>
              <option value="">{t('Session default')}</option>
              {modelGroups.map((g) => (
                <optgroup key={g.id} label={g.label}>
                  {g.models.map((m) => (
                    <option key={m} value={`${g.url}::${m}`}>
                      {m}
                    </option>
                  ))}
                </optgroup>
              ))}
              {d.modelPair && !modelListed && <option value={d.modelPair}>{d.modelPair.split('::')[1]} ({t('endpoint not listed')})</option>}
            </select>
          </Field>
          <Field label={t('Then run')} hint={t('Another automation, after this one succeeds.')}>
            <select className="fs-field" value={d.chain} onChange={(e) => set('chain', e.target.value)}>
              <option value="">{t('Nothing')}</option>
              {others
                .filter((o) => o.id !== existing?.id)
                .map((o) => (
                  <option key={o.id} value={o.id}>
                    {o.name}
                  </option>
                ))}
            </select>
          </Field>
        </div>
        <label className="fs-switch">
          <input type="checkbox" checked={d.notify} onChange={(e) => set('notify', e.target.checked)} />
          <span>{t('Notify me when it finishes')}</span>
        </label>
      </details>

      {error && <p className="fs-au__error" role="alert">{error}</p>}

      <div className="fs-au__actions">
        <Button type="submit" variant="primary" size="sm" icon={Save} label={existing ? t('Save changes') : t('Create')} loading={saving} testId="auto-save" />
        <Button variant="ghost" size="sm" icon={X} label={t('Cancel')} onClick={onCancel} />
      </div>
    </form>
  );
}
