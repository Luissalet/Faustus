import { useEffect, useId, useState, type ReactNode } from 'react';
import { Button } from '../../components';
import { t } from '../../i18n';
import { declaredLabels, type RawNode } from './nodeKinds';

/**
 * The per-type forms of `NodeInspector`: one for each of the node types that
 * use a model (`agent`, `classify`, `extract`, `guard`) and for `loop`. A form
 * edits a plain `config` object and hands the whole next object back; it never
 * talks to the server. The raw JSON editor in the inspector stays next to it
 * and shows the same object, so nothing a form cannot express is out of reach.
 */

export type Config = Record<string, unknown>;

export interface FormContext {
  /** Every node in the definition, so a loop can list what it may repeat. */
  nodes: RawNode[];
  /** The node being edited. */
  self: RawNode;
  /** Agent profile slugs, for the agent form's suggestion list. */
  profiles: string[];
}

interface FormProps {
  config: Config;
  onChange: (next: Config) => void;
  ctx: FormContext;
}

const isEmpty = (v: unknown): boolean =>
  v === undefined || v === null || v === '' || (Array.isArray(v) && v.length === 0);

function put(config: Config, key: string, value: unknown): Config {
  const next = { ...config };
  if (isEmpty(value)) delete next[key];
  else next[key] = value;
  return next;
}

const asText = (v: unknown): string => (typeof v === 'string' ? v : '');
const asObject = (v: unknown): Config => (v && typeof v === 'object' && !Array.isArray(v) ? (v as Config) : {});
const asList = (v: unknown): string[] => (Array.isArray(v) ? v.map((x) => String(x)) : []);
const lines = (text: string): string[] => text.split('\n').map((l) => l.trim()).filter(Boolean);

// ── small controls ────────────────────────────────────────────────────────

function Field({ label, hint, required, htmlFor, children }: { label: string; hint?: string; required?: boolean; htmlFor?: string; children: ReactNode }) {
  return (
    <div className="fs-form__field">
      <label className="fs-form__label" htmlFor={htmlFor}>
        {label}
        {required && <span className="fs-form__req"> {t('(required)')}</span>}
      </label>
      {children}
      {hint && <p className="fs-form__hint">{hint}</p>}
    </div>
  );
}

function TextArea({ label, value, onChange, rows = 3, hint, required, code, testId }: {
  label: string; value: string; onChange: (v: string) => void; rows?: number; hint?: string; required?: boolean; code?: boolean; testId: string;
}) {
  const id = useId();
  return (
    <Field label={label} hint={hint} required={required && !value.trim()} htmlFor={id}>
      <textarea id={id} className="fs-form__control" data-code={code || undefined} rows={rows} value={value} spellCheck={false}
        onChange={(e) => onChange(e.target.value)} data-testid={testId} />
    </Field>
  );
}

/** A textarea whose text is a list in disguise (one item per line). It keeps
 *  the text as typed, so a trailing newline is not eaten while the next line
 *  is being started, and hands up the parsed list on every change. */
function DraftArea<T>({ label, value, format, parse, onChange, rows = 3, hint, code, testId }: {
  label: string; value: T; format: (v: T) => string; parse: (text: string) => T; onChange: (v: T) => void;
  rows?: number; hint?: string; code?: boolean; testId: string;
}) {
  const id = useId();
  const shown = format(value);
  const [text, setText] = useState(shown);
  useEffect(() => {
    if (format(parse(text)) !== shown) setText(shown);
    // resync only when the value changed from outside
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shown]);
  return (
    <Field label={label} hint={hint} htmlFor={id}>
      <textarea id={id} className="fs-form__control" data-code={code || undefined} rows={rows} value={text} spellCheck={false}
        onChange={(e) => { setText(e.target.value); onChange(parse(e.target.value)); }} data-testid={testId} />
    </Field>
  );
}

const linesToText = (v: string[]): string => v.join('\n');

function TextInput({ label, value, onChange, hint, required, list, testId }: {
  label: string; value: string; onChange: (v: string) => void; hint?: string; required?: boolean; list?: string; testId: string;
}) {
  const id = useId();
  return (
    <Field label={label} hint={hint} required={required && !value.trim()} htmlFor={id}>
      <input id={id} className="fs-form__control" type="text" value={value} list={list} spellCheck={false}
        onChange={(e) => onChange(e.target.value)} data-testid={testId} />
    </Field>
  );
}

function NumberInput({ label, value, onChange, min, max, step, hint, testId }: {
  label: string; value: unknown; onChange: (v: number | undefined) => void; min?: number; max?: number; step?: number; hint?: string; testId: string;
}) {
  const id = useId();
  return (
    <Field label={label} hint={hint} htmlFor={id}>
      <input id={id} className="fs-form__control" type="number" min={min} max={max} step={step}
        value={typeof value === 'number' ? value : ''}
        onChange={(e) => onChange(e.target.value === '' ? undefined : Number(e.target.value))} data-testid={testId} />
    </Field>
  );
}

function Select({ label, value, options, onChange, hint, testId }: {
  label: string; value: string; options: { value: string; label: string }[]; onChange: (v: string) => void; hint?: string; testId: string;
}) {
  const id = useId();
  return (
    <Field label={label} hint={hint} htmlFor={id}>
      <select id={id} className="fs-form__control" value={value} onChange={(e) => onChange(e.target.value)} data-testid={testId}>
        {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    </Field>
  );
}

/** A JSON value edited as text. It keeps what is being typed, reports a parse
 *  problem in place, and only hands a value up once it parses. */
export function JsonField({ label, value, onChange, hint, rows = 6, testId, asObjectOnly = true }: {
  label: string; value: unknown; onChange: (v: unknown) => void; hint?: string; rows?: number; testId: string; asObjectOnly?: boolean;
}) {
  const id = useId();
  const shown = value === undefined ? '' : JSON.stringify(value, null, 2);
  const [text, setText] = useState(shown);
  const [error, setError] = useState('');
  useEffect(() => {
    let current: string | undefined;
    try { current = text.trim() ? JSON.stringify(JSON.parse(text), null, 2) : ''; } catch { current = undefined; }
    if (current !== shown) { setText(shown); setError(''); }
    // resync only when the value changed from outside
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shown]);
  function edit(next: string) {
    setText(next);
    if (!next.trim()) { setError(''); onChange(undefined); return; }
    try {
      const parsed = JSON.parse(next);
      if (asObjectOnly && (!parsed || typeof parsed !== 'object' || Array.isArray(parsed))) {
        setError(t('This must be a JSON object.'));
        return;
      }
      setError('');
      onChange(parsed);
    } catch {
      setError(t('This is not valid JSON yet.'));
    }
  }
  return (
    <Field label={label} hint={hint} htmlFor={id}>
      <textarea id={id} className="fs-form__control" data-code rows={rows} value={text} spellCheck={false}
        onChange={(e) => edit(e.target.value)} data-testid={testId} aria-invalid={error ? true : undefined} />
      {error && <p className="fs-form__error" role="alert">{error}</p>}
    </Field>
  );
}

// ── agent ─────────────────────────────────────────────────────────────────

type ToolsMode = 'default' | 'none' | 'listed';

export function AgentForm({ config, onChange, ctx }: FormProps) {
  const listId = useId();
  const tools = config.tools;
  const mode: ToolsMode = tools === undefined ? 'default' : Array.isArray(tools) && tools.length === 0 ? 'none' : 'listed';
  return (
    <div className="fs-form" data-testid="form-agent">
      <TextArea label={t('Prompt')} value={asText(config.prompt)} required rows={5} testId="form-agent-prompt"
        hint={t('Templates may read {{ inputs.name }} and {{ results.node.field }}.')}
        onChange={(v) => onChange(put(config, 'prompt', v))} />
      <TextArea label={t('System instructions')} value={asText(config.system)} rows={2} testId="form-agent-system"
        onChange={(v) => onChange(put(config, 'system', v))} />
      <TextInput label={t('Agent profile')} value={asText(config.agent)} list={listId} testId="form-agent-profile"
        hint={t('Optional. A profile contributes its prompt and its tool permissions.')}
        onChange={(v) => onChange(put(config, 'agent', v.trim()))} />
      <datalist id={listId}>{ctx.profiles.map((p) => <option key={p} value={p} />)}</datalist>
      <Select label={t('Tools the agent may call')} value={mode} testId="form-agent-tools-mode"
        options={[
          { value: 'default', label: t('The agent default') },
          { value: 'none', label: t('None: it only thinks') },
          { value: 'listed', label: t('Only the ones listed') },
        ]}
        onChange={(v) => {
          const next = { ...config };
          if (v === 'default') delete next.tools;
          else if (v === 'none') next.tools = [];
          else next.tools = asList(config.tools).length ? config.tools : ['read_file'];
          onChange(next);
        }} />
      {mode === 'listed' && (
        <DraftArea label={t('Allowed tools')} value={asList(tools)} format={linesToText} parse={lines} rows={3} code testId="form-agent-tools"
          hint={t('One tool name per line. Nothing outside this list can be called.')}
          onChange={(v) => onChange({ ...config, tools: v })} />
      )}
      <JsonField label={t('Output schema (JSON)')} value={config.output_schema} testId="form-agent-output-schema"
        hint={t('Optional. The reply is checked against it, with one repair attempt.')}
        onChange={(v) => onChange(put(config, 'output_schema', v))} />
      <div className="fs-form__row">
        <NumberInput label={t('Max rounds')} value={config.max_rounds} min={1} max={50} testId="form-agent-max-rounds"
          onChange={(v) => onChange(put(config, 'max_rounds', v))} />
        <NumberInput label={t('Timeout (seconds)')} value={config.timeout_s} min={5} max={3600} testId="form-agent-timeout"
          onChange={(v) => onChange(put(config, 'timeout_s', v))} />
      </div>
    </div>
  );
}

// ── classify ──────────────────────────────────────────────────────────────

function labelsToText(labels: unknown): string {
  if (!Array.isArray(labels)) return '';
  return labels.map((item) => {
    if (typeof item === 'string') return item;
    const o = asObject(item);
    return o.description ? `${asText(o.name)}: ${asText(o.description)}` : asText(o.name);
  }).join('\n');
}

function labelsFromText(text: string): unknown[] {
  return lines(text).map((line) => {
    const at = line.indexOf(':');
    if (at <= 0) return line;
    const description = line.slice(at + 1).trim();
    const name = line.slice(0, at).trim();
    return description ? { name, description } : name;
  });
}

export function ClassifyForm({ config, onChange, ctx }: FormProps) {
  const names = declaredLabels({ ...ctx.self, config });
  const fallback = asText(config.fallback);
  return (
    <div className="fs-form" data-testid="form-classify">
      <TextArea label={t('Text to classify')} value={asText(config.text)} required rows={3} testId="form-classify-text"
        hint={t('A template, for example {{ inputs.ticket }}.')}
        onChange={(v) => onChange(put(config, 'text', v))} />
      <TextInput label={t('Question')} value={asText(config.question)} testId="form-classify-question"
        onChange={(v) => onChange(put(config, 'question', v))} />
      <DraftArea label={t('Options')} value={Array.isArray(config.labels) ? config.labels : []} format={labelsToText} parse={labelsFromText} rows={4} code testId="form-classify-labels"
        hint={t('One per line, at least two. Add a description after a colon: billing: invoices and refunds.')}
        onChange={(v) => onChange({ ...config, labels: v })} />
      <NumberInput label={t('Confidence threshold')} value={config.threshold} min={0} max={1} step={0.05} testId="form-classify-threshold"
        hint={t('Below this the node is uncertain and takes the fallback.')}
        onChange={(v) => onChange(put(config, 'threshold', v))} />
      <Select label={t('Fallback option (uncertain)')} value={fallback} testId="form-classify-fallback"
        options={[{ value: '', label: t('None') }, ...names.map((n) => ({ value: n, label: n }))]}
        onChange={(v) => onChange(put(config, 'fallback', v))} />
      <Select label={t('When uncertain')} value={asText(config.on_uncertain) || (fallback ? 'fallback' : 'ask')} testId="form-classify-on-uncertain"
        options={[
          { value: 'fallback', label: t('Take the fallback option') },
          { value: 'ask', label: t('Ask the model once more') },
        ]}
        onChange={(v) => onChange(put(config, 'on_uncertain', v))} />
      <TextArea label={t('Instructions')} value={asText(config.instructions)} rows={2} testId="form-classify-instructions"
        onChange={(v) => onChange(put(config, 'instructions', v))} />
    </div>
  );
}

// ── extract ───────────────────────────────────────────────────────────────

export function ExtractForm({ config, onChange }: FormProps) {
  return (
    <div className="fs-form" data-testid="form-extract">
      <TextArea label={t('Text to read')} value={asText(config.text)} required rows={3} testId="form-extract-text"
        hint={t('A run input or an upstream output, for example {{ results.fetch.text }}.')}
        onChange={(v) => onChange(put(config, 'text', v))} />
      <JsonField label={t('Schema of the fields to extract (JSON)')} value={config.schema} rows={8} testId="form-extract-schema"
        hint={t('An object schema. A field the text does not give is left out, never invented.')}
        onChange={(v) => onChange(put(config, 'schema', v))} />
      <TextArea label={t('Instructions')} value={asText(config.instructions)} rows={2} testId="form-extract-instructions"
        onChange={(v) => onChange(put(config, 'instructions', v))} />
      <NumberInput label={t('Timeout (seconds)')} value={config.timeout_s} min={1} max={600} testId="form-extract-timeout"
        onChange={(v) => onChange(put(config, 'timeout_s', v))} />
    </div>
  );
}

// ── guard ─────────────────────────────────────────────────────────────────

const PII_KINDS = ['EMAIL', 'PHONE', 'IBAN', 'CARD', 'ID', 'IP'];
const CHECK_TYPES = ['secrets', 'injection', 'pii', 'urls', 'model'] as const;
type CheckType = (typeof CHECK_TYPES)[number];

function checkLabel(type: string): string {
  switch (type) {
    case 'secrets': return t('Secrets');
    case 'injection': return t('Prompt injection');
    case 'pii': return t('Personal data');
    case 'urls': return t('Links');
    default: return t('Model question');
  }
}

function checkObject(item: unknown): Config {
  return typeof item === 'string' ? { type: item } : asObject(item);
}

function compactCheck(check: Config): unknown {
  const type = asText(check.type);
  return (type === 'secrets' || type === 'injection') && Object.keys(check).length === 1 ? type : check;
}

export function GuardForm({ config, onChange }: FormProps) {
  const checks = (Array.isArray(config.checks) ? config.checks : []).map(checkObject);
  const write = (next: Config[]) => onChange({ ...config, checks: next.map(compactCheck) });
  const change = (i: number, patch: Config) => write(checks.map((c, at) => {
    if (at !== i) return c;
    const merged = { ...c, ...patch };
    for (const k of Object.keys(merged)) if (isEmpty(merged[k])) delete merged[k];
    return merged;
  }));
  const add = (type: CheckType) => {
    const fresh: Config = type === 'pii' ? { type, kinds: ['EMAIL', 'PHONE', 'IBAN'] } : type === 'urls' ? { type, deny: [] } : type === 'model' ? { type, question: '', threshold: 0.7 } : { type };
    write([...checks, fresh]);
  };
  return (
    <div className="fs-form" data-testid="form-guard">
      <TextArea label={t('Text to check')} value={asText(config.text)} required rows={3} testId="form-guard-text"
        hint={t('A template, for example {{ results.draft.text }}.')}
        onChange={(v) => onChange(put(config, 'text', v))} />
      <fieldset className="fs-form__group">
        <legend>{t('Checks')}</legend>
        {checks.length === 0 && <p className="fs-form__hint">{t('A guard needs at least one check.')}</p>}
        {checks.map((check, i) => {
          const type = asText(check.type);
          return (
            <div key={i} className="fs-form__check" data-testid={`form-guard-check-${i}`}>
              <div className="fs-form__check-head">
                <strong>{checkLabel(type)}</strong>
                <Button variant="ghost" size="sm" label={t('Remove')} onClick={() => write(checks.filter((_, at) => at !== i))} testId={`form-guard-check-remove-${i}`} />
              </div>
              {type === 'pii' && (
                <div className="fs-form__kinds" role="group" aria-label={t('Kinds of personal data')}>
                  {PII_KINDS.map((kind) => {
                    const on = asList(check.kinds).includes(kind);
                    return (
                      <label key={kind} className="fs-form__inline">
                        <input type="checkbox" checked={on} data-testid={`form-guard-pii-${i}-${kind}`}
                          onChange={() => change(i, { kinds: on ? asList(check.kinds).filter((k) => k !== kind) : [...asList(check.kinds), kind] })} />
                        {kind}
                      </label>
                    );
                  })}
                </div>
              )}
              {type === 'urls' && (
                <>
                  <DraftArea label={t('Allowed hosts')} value={asList(check.allow)} format={linesToText} parse={lines} rows={2} code testId={`form-guard-urls-allow-${i}`}
                    hint={t('One host per line; *.host matches subdomains only. Empty means any host not denied.')}
                    onChange={(v) => change(i, { allow: v })} />
                  <DraftArea label={t('Denied hosts')} value={asList(check.deny)} format={linesToText} parse={lines} rows={2} code testId={`form-guard-urls-deny-${i}`}
                    onChange={(v) => change(i, { deny: v })} />
                </>
              )}
              {type === 'model' && (
                <>
                  <TextInput label={t('Question (yes means the text is not acceptable)')} value={asText(check.question)} required testId={`form-guard-model-question-${i}`}
                    onChange={(v) => change(i, { question: v })} />
                  <NumberInput label={t('Confidence threshold')} value={check.threshold} min={0} max={1} step={0.05} testId={`form-guard-model-threshold-${i}`}
                    onChange={(v) => change(i, { threshold: v })} />
                </>
              )}
            </div>
          );
        })}
        <div className="fs-form__adds">
          {CHECK_TYPES.map((type) => (
            <Button key={type} variant="secondary" size="sm" label={t('Add {check}', { check: checkLabel(type) })} onClick={() => add(type)} testId={`form-guard-add-${type}`} />
          ))}
        </div>
      </fieldset>
      <Select label={t('When a check cannot be settled')} value={asText(config.on_unknown) || 'fail'} testId="form-guard-on-unknown"
        options={[{ value: 'fail', label: t('Fail the guard') }, { value: 'pass', label: t('Let it pass') }]}
        onChange={(v) => onChange(put(config, 'on_unknown', v))} />
      <NumberInput label={t('Timeout (seconds)')} value={config.timeout_s} min={1} max={600} testId="form-guard-timeout"
        onChange={(v) => onChange(put(config, 'timeout_s', v))} />
    </div>
  );
}

// ── loop ──────────────────────────────────────────────────────────────────

const LOOP_BODY_TYPES = ['agent', 'classify', 'extract', 'guard', 'condition', 'artifact_store', 'skill', 'deliver'];
const UNTIL_OPS = ['eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'contains', 'in', 'exists', 'truthy'];

function parseLoose(text: string): unknown {
  if (text.trim() === '') return '';
  try { return JSON.parse(text); } catch { return text; }
}

export function LoopForm({ config, onChange, ctx }: FormProps) {
  const budget = asObject(config.budget);
  const until = config.until && typeof config.until === 'object' ? asObject(config.until) : null;
  const body = asList(config.body);
  const takenElsewhere = new Set<string>();
  for (const n of ctx.nodes) {
    if (n.type === 'loop' && n.id !== ctx.self.id) for (const b of asList(n.config?.body)) takenElsewhere.add(b);
  }
  const candidates = ctx.nodes.filter((n) => n.id !== ctx.self.id && LOOP_BODY_TYPES.includes(n.type) && !takenElsewhere.has(n.id));
  const setBudget = (key: string, value: unknown) => {
    const next = { ...budget };
    if (isEmpty(value)) delete next[key]; else next[key] = value;
    onChange({ ...config, budget: next });
  };
  const needsRight = until && !['exists', 'truthy'].includes(asText(until.op));
  return (
    <div className="fs-form" data-testid="form-loop">
      <fieldset className="fs-form__group">
        <legend>{t('Limits')}</legend>
        <NumberInput label={t('Maximum passes')} value={budget.max_iterations} min={1} testId="form-loop-max-iterations"
          hint={t('Required. The loop never runs more often than this.')}
          onChange={(v) => setBudget('max_iterations', v)} />
        <NumberInput label={t('Maximum active seconds')} value={budget.max_seconds} min={0} testId="form-loop-max-seconds"
          hint={t('Empty or 0 means no time limit. Time spent waiting on a person does not count.')}
          onChange={(v) => setBudget('max_seconds', v)} />
        <NumberInput label={t('Maximum tool calls')} value={budget.max_tool_calls} min={0} testId="form-loop-max-tool-calls"
          onChange={(v) => setBudget('max_tool_calls', v)} />
        <Select label={t('When a limit is reached before the exit condition')} value={asText(budget.on_exhausted) || 'pause'} testId="form-loop-on-exhausted"
          options={[{ value: 'pause', label: t('Pause for a person to extend or stop it') }, { value: 'fail', label: t('Fail the loop') }]}
          onChange={(v) => setBudget('on_exhausted', v)} />
      </fieldset>
      <fieldset className="fs-form__group">
        <legend>{t('Exit condition')}</legend>
        <label className="fs-form__inline">
          <input type="checkbox" checked={!!until} data-testid="form-loop-until-on"
            onChange={(e) => {
              const next = { ...config };
              if (e.target.checked) next.until = { left: '', op: 'truthy' };
              else delete next.until;
              onChange(next);
            }} />
          {t('Stop early when a condition holds')}
        </label>
        {until && (
          <>
            <TextInput label={t('Left value')} value={typeof until.left === 'string' ? until.left : JSON.stringify(until.left ?? '')} testId="form-loop-until-left"
              hint={t('A path such as results.review.passed, or a literal.')}
              onChange={(v) => onChange({ ...config, until: { ...until, left: v } })} />
            <Select label={t('Operator')} value={asText(until.op) || 'truthy'} testId="form-loop-until-op"
              options={UNTIL_OPS.map((op) => ({ value: op, label: op }))}
              onChange={(v) => {
                const next: Config = { ...until, op: v };
                if (v === 'exists' || v === 'truthy') delete next.right;
                onChange({ ...config, until: next });
              }} />
            {needsRight && (
              <TextInput label={t('Right value')} value={typeof until.right === 'string' ? until.right : until.right === undefined ? '' : JSON.stringify(until.right)} testId="form-loop-until-right"
                onChange={(v) => onChange({ ...config, until: { ...until, right: parseLoose(v) } })} />
            )}
          </>
        )}
      </fieldset>
      <fieldset className="fs-form__group">
        <legend>{t('Nodes it repeats')}</legend>
        {candidates.length === 0 && <p className="fs-form__hint">{t('Add an agent, classify, extract or guard node first; a loop repeats nodes that already exist.')}</p>}
        {candidates.map((n) => (
          <label key={n.id} className="fs-form__inline">
            <input type="checkbox" checked={body.includes(n.id)} data-testid={`form-loop-body-${n.id}`}
              onChange={() => onChange({ ...config, body: body.includes(n.id) ? body.filter((b) => b !== n.id) : [...body, n.id] })} />
            <code>{n.id}</code> <span className="fs-form__hint">{n.type}</span>
          </label>
        ))}
        {body.length === 0 && <p className="fs-form__error" role="alert">{t('A loop needs at least one node to repeat.')}</p>}
      </fieldset>
    </div>
  );
}

export const FORM_TYPES = ['agent', 'classify', 'extract', 'guard', 'loop'] as const;

export function ConfigForm({ type, ...props }: FormProps & { type: string }) {
  switch (type) {
    case 'agent': return <AgentForm {...props} />;
    case 'classify': return <ClassifyForm {...props} />;
    case 'extract': return <ExtractForm {...props} />;
    case 'guard': return <GuardForm {...props} />;
    case 'loop': return <LoopForm {...props} />;
    default: return null;
  }
}
