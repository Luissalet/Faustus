import { useEffect, useMemo, useState } from 'react';
import { Beaker, Check, Plus, Star, Theater, Trash2 } from 'lucide-react';
import { Button, EmptyState, Skeleton } from '../../components';
import { t, useLang } from '../../i18n';
import {
  checkText,
  deleteMode,
  listModes,
  modeDescription,
  modeLabel,
  saveMode,
  setDefaultMode,
  type BehaviorMode,
  type BehaviorModeChecks,
  type ModeCheckResult,
} from '../../adapters/behaviorModes';
import { Toggle } from './fields';

/**
 * CONTRATO_MODOS Lote B — "Behaviour modes": every mode the caller can see
 * (built-in, read-only, plus their own), a create/edit form for their own,
 * a "Try it" preview against `/api/behavior-modes/check`, and (admin only)
 * which built-in id the server falls back to for everyone.
 *
 * Mirrors `Tools.tsx`'s shape (a plain list section plus a detail panel)
 * rather than inventing a new one: `listModes()` -> render -> `reload()`
 * after every write, same as `ToolsSection`'s own `reload`.
 */

const MODE_ID_RE = /^[a-z][a-z0-9_]{1,32}$/;

interface Draft {
  id: string;
  nameEn: string;
  nameEs: string;
  descEn: string;
  descEs: string;
  prompt: string;
  confidenceTags: boolean;
  firstSentenceChallenge: boolean;
  endsWithQuestion: boolean;
  maxQuestions: string;
  maxWords: string;
  forbiddenText: string;
}

function blankDraft(): Draft {
  return {
    id: '', nameEn: '', nameEs: '', descEn: '', descEs: '', prompt: '',
    confidenceTags: false, firstSentenceChallenge: false, endsWithQuestion: false,
    maxQuestions: '', maxWords: '', forbiddenText: '',
  };
}

function draftFromMode(mode: BehaviorMode): Draft {
  const checks = mode.checks;
  return {
    id: mode.id, nameEn: mode.name.en, nameEs: mode.name.es,
    descEn: mode.description.en, descEs: mode.description.es, prompt: mode.prompt,
    confidenceTags: Boolean(checks.confidence_tags),
    firstSentenceChallenge: checks.first_sentence === 'challenge',
    endsWithQuestion: Boolean(checks.ends_with_question),
    maxQuestions: checks.max_questions != null ? String(checks.max_questions) : '',
    maxWords: checks.max_words != null ? String(checks.max_words) : '',
    forbiddenText: (checks.forbidden_phrases ?? []).join('\n'),
  };
}

function checksFromDraft(d: Draft): BehaviorModeChecks {
  const checks: BehaviorModeChecks = {};
  if (d.confidenceTags) checks.confidence_tags = true;
  if (d.firstSentenceChallenge) checks.first_sentence = 'challenge';
  if (d.endsWithQuestion) checks.ends_with_question = true;
  const phrases = d.forbiddenText.split('\n').map((p) => p.trim()).filter(Boolean);
  if (phrases.length) checks.forbidden_phrases = phrases;
  const maxQ = d.maxQuestions.trim();
  if (maxQ && /^\d+$/.test(maxQ)) checks.max_questions = Number(maxQ);
  const maxW = d.maxWords.trim();
  if (maxW && /^\d+$/.test(maxW)) checks.max_words = Number(maxW);
  return checks;
}

export function BehaviorModesSection({ say, admin }: { say: (t: string) => void; admin: boolean }) {
  const lang = useLang();
  const [modes, setModes] = useState<BehaviorMode[] | null>(null);
  const [defaultId, setDefaultIdState] = useState('default');
  const [failed, setFailed] = useState(false);
  const [editing, setEditing] = useState<Draft | null>(null);
  const [saving, setSaving] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [tryFor, setTryFor] = useState<string | null>(null);
  const [tryText, setTryText] = useState('');
  const [tryBusy, setTryBusy] = useState(false);
  const [tryResult, setTryResult] = useState<ModeCheckResult | null>(null);
  const [settingDefault, setSettingDefault] = useState<string | null>(null);

  const reload = () => {
    listModes()
      .then((r) => { setModes(r.modes); setDefaultIdState(r.default); setFailed(false); })
      .catch(() => { setModes([]); setFailed(true); });
  };
  useEffect(reload, []);

  const builtins = useMemo(() => (modes ?? []).filter((m) => m.builtin), [modes]);
  const own = useMemo(() => (modes ?? []).filter((m) => !m.builtin), [modes]);

  const submit = async () => {
    if (!editing) return;
    const id = editing.id.trim();
    if (!MODE_ID_RE.test(id)) {
      say(t('The id must start with a letter and use only lowercase letters, digits and _ (2-32 characters).'));
      return;
    }
    if (!editing.nameEn.trim() || !editing.nameEs.trim()) {
      say(t('Name (EN) and Name (ES) are both required.'));
      return;
    }
    setSaving(true);
    try {
      await saveMode({
        id,
        name: { en: editing.nameEn.trim(), es: editing.nameEs.trim() },
        description: { en: editing.descEn.trim(), es: editing.descEs.trim() },
        prompt: editing.prompt,
        checks: checksFromDraft(editing),
      });
      setEditing(null);
      reload();
      say(t('Mode saved.'));
    } catch (e) {
      say((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const remove = async (id: string) => {
    try {
      await deleteMode(id);
      reload();
      say(t('Mode deleted.'));
    } catch (e) {
      say((e as Error).message);
    } finally {
      setConfirmDelete(null);
    }
  };

  const runTry = async (id: string) => {
    setTryBusy(true);
    setTryResult(null);
    try {
      setTryResult(await checkText(id, tryText));
    } catch (e) {
      say((e as Error).message);
    } finally {
      setTryBusy(false);
    }
  };

  const makeDefault = async (id: string) => {
    setSettingDefault(id);
    try {
      await setDefaultMode(id);
      setDefaultIdState(id);
      say(t('Default behaviour mode: {name}', { name: modeLabel(builtins.find((m) => m.id === id), lang) }));
    } catch (e) {
      say((e as Error).message);
    } finally {
      setSettingDefault(null);
    }
  };

  if (failed) {
    return (
      <EmptyState
        icon={Theater}
        tone="error"
        title={t('Could not read the behaviour modes.')}
        body={t('GET /api/behavior-modes failed.')}
        primaryAction={{ label: t('Try again'), onClick: reload }}
      />
    );
  }

  return (
    <section className="fs-set__section" aria-labelledby="fs-set-modes">
      <header className="fs-set__section-head">
        <div>
          <h2 id="fs-set-modes" className="fs-set__title">{t('Behaviour modes')}</h2>
          <p className="fs-prose">
            {t('A behaviour mode changes HOW Faustus argues — never what it is allowed to do. The content policy, the agent rules and your own instructions in the chat always win.')}
          </p>
        </div>
        <Button icon={Plus} label={t('New mode')} onClick={() => setEditing(blankDraft())} testId="modes-new" />
      </header>

      {modes === null ? (
        <Skeleton label={t('Loading')} count={4} height="52px" />
      ) : (
        <div className="fs-modes__list" data-testid="modes-list">
          {[...builtins, ...own].map((mode) => (
            <ModeRow
              key={mode.id}
              mode={mode}
              lang={lang}
              isDefault={mode.id === defaultId}
              admin={admin}
              onEdit={!mode.builtin ? () => setEditing(draftFromMode(mode)) : undefined}
              onDelete={!mode.builtin ? () => setConfirmDelete(mode.id) : undefined}
              confirmingDelete={confirmDelete === mode.id}
              onConfirmDelete={() => void remove(mode.id)}
              onCancelDelete={() => setConfirmDelete(null)}
              onMakeDefault={mode.builtin && admin ? () => void makeDefault(mode.id) : undefined}
              settingDefault={settingDefault === mode.id}
              tryOpen={tryFor === mode.id}
              onToggleTry={() => { setTryFor(tryFor === mode.id ? null : mode.id); setTryResult(null); setTryText(''); }}
              tryText={tryText}
              onTryTextChange={setTryText}
              onRunTry={() => void runTry(mode.id)}
              tryBusy={tryBusy}
              tryResult={tryResult}
            />
          ))}
        </div>
      )}

      {editing && (
        <ModeForm
          draft={editing}
          isNew={!own.some((m) => m.id === editing.id)}
          onChange={setEditing}
          onCancel={() => setEditing(null)}
          onSave={() => void submit()}
          saving={saving}
        />
      )}
    </section>
  );
}

function ModeRow({
  mode, lang, isDefault, admin, onEdit, onDelete, confirmingDelete, onConfirmDelete, onCancelDelete,
  onMakeDefault, settingDefault, tryOpen, onToggleTry, tryText, onTryTextChange, onRunTry, tryBusy, tryResult,
}: {
  mode: BehaviorMode;
  lang: 'en' | 'es';
  isDefault: boolean;
  admin: boolean;
  onEdit?: () => void;
  onDelete?: () => void;
  confirmingDelete: boolean;
  onConfirmDelete: () => void;
  onCancelDelete: () => void;
  onMakeDefault?: () => void;
  settingDefault: boolean;
  tryOpen: boolean;
  onToggleTry: () => void;
  tryText: string;
  onTryTextChange: (v: string) => void;
  onRunTry: () => void;
  tryBusy: boolean;
  tryResult: ModeCheckResult | null;
}) {
  const [showPrompt, setShowPrompt] = useState(false);
  return (
    <div className="fs-set__card fs-modes__row" data-testid={`modes-row-${mode.id}`}>
      <div className="fs-tools__row">
        <span className="fs-tools__text" style={{ flex: 1 }}>
          <strong>
            {modeLabel(mode, lang)} <code className="fs-tools__id">{mode.id}</code>
            {isDefault && <span className="fs-st__badge" data-tone="ok"> {t('global default')}</span>}
            {!mode.builtin && <span className="fs-st__badge"> {t('yours')}</span>}
          </strong>
          <span className="fs-set__help">{modeDescription(mode, lang) || t('No description.')}</span>
        </span>
        <Button variant="ghost" size="sm" icon={Beaker} label={t('Try it')} onClick={onToggleTry} testId="modes-try" />
        {onMakeDefault && (
          <Button
            variant={isDefault ? 'primary' : 'ghost'}
            size="sm"
            icon={Star}
            label={isDefault ? t('Default') : t('Set as default')}
            onClick={onMakeDefault}
            loading={settingDefault}
            disabled={isDefault}
            testId="modes-default"
          />
        )}
        {onEdit && <Button variant="ghost" size="sm" label={t('Edit')} onClick={onEdit} />}
        {onDelete && !confirmingDelete && <Button variant="danger" size="sm" icon={Trash2} label={t('Delete')} onClick={onDelete} />}
        {confirmingDelete && (
          <span className="fs-modes__confirm">
            <span className="fs-set__help" data-tone="bad">{t('Delete for good?')}</span>
            <Button variant="danger-solid" size="sm" label={t('Delete')} onClick={onConfirmDelete} />
            <Button variant="ghost" size="sm" label={t('Cancel')} onClick={onCancelDelete} />
          </span>
        )}
      </div>
      {mode.prompt && (
        <details className="fs-modes__prompt" open={showPrompt} onToggle={(e) => setShowPrompt((e.target as HTMLDetailsElement).open)}>
          <summary>{t('Prompt')}</summary>
          <pre className="fs-set__pre">{mode.prompt}</pre>
        </details>
      )}
      {tryOpen && (
        <div className="fs-modes__try" data-testid="modes-try-panel">
          <textarea
            className="fs-field"
            rows={3}
            value={tryText}
            onChange={(e) => onTryTextChange(e.target.value)}
            placeholder={t('Paste a reply to check it against this mode…')}
            aria-label={t('Text to check')}
          />
          <Button variant="primary" size="sm" label={t('Check')} onClick={onRunTry} loading={tryBusy} disabled={tryBusy || !tryText.trim()} />
          {tryResult && (
            <p className="fs-set__help" data-tone={tryResult.violations.length ? 'bad' : 'ok'} data-testid="modes-try-result">
              {tryResult.checked.length === 0
                ? t('This mode has no checks configured.')
                : tryResult.violations.length === 0
                  ? t('Every configured check passed.')
                  : tryResult.violations.map((v) => v.detail).join(' · ')}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function ModeForm({
  draft, isNew, onChange, onCancel, onSave, saving,
}: {
  draft: Draft;
  isNew: boolean;
  onChange: (d: Draft) => void;
  onCancel: () => void;
  onSave: () => void;
  saving: boolean;
}) {
  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => onChange({ ...draft, [key]: value });
  return (
    <div className="fs-set__card fs-modes__form" data-testid="modes-form">
      <h3 className="fs-set__card-title">{isNew ? t('New mode') : t('Edit mode')}</h3>
      <div className="fs-tools">
        <label className="fs-tools__row">
          <span className="fs-tools__text">{t('Id (slug, e.g. "socratic")')}</span>
          <input className="fs-field" value={draft.id} disabled={!isNew} onChange={(e) => set('id', e.target.value.trim().toLowerCase())} />
        </label>
        <label className="fs-tools__row">
          <span className="fs-tools__text">{t('Name (EN)')}</span>
          <input className="fs-field" value={draft.nameEn} onChange={(e) => set('nameEn', e.target.value)} />
        </label>
        <label className="fs-tools__row">
          <span className="fs-tools__text">{t('Name (ES)')}</span>
          <input className="fs-field" value={draft.nameEs} onChange={(e) => set('nameEs', e.target.value)} />
        </label>
        <label className="fs-tools__row">
          <span className="fs-tools__text">{t('Description (EN)')}</span>
          <input className="fs-field" value={draft.descEn} onChange={(e) => set('descEn', e.target.value)} />
        </label>
        <label className="fs-tools__row">
          <span className="fs-tools__text">{t('Description (ES)')}</span>
          <input className="fs-field" value={draft.descEs} onChange={(e) => set('descEs', e.target.value)} />
        </label>
      </div>
      <label className="fs-modes__prompt-field">
        <span>{t('Prompt (in English, for the model — empty means no extra block at all)')}</span>
        <textarea className="fs-field fs-set__pre" rows={6} value={draft.prompt} onChange={(e) => set('prompt', e.target.value)} maxLength={4000} />
      </label>
      <h4 className="fs-set__card-title">{t('Checks (heuristic — a detector, not a judge)')}</h4>
      <div className="fs-tools">
        <div className="fs-tools__row">
          <Toggle id="mode-confidence" checked={draft.confidenceTags} onChange={(v) => set('confidenceTags', v)} label={t('Require a confidence tag ([Certain]/[Likely]/[Guessing])')} />
        </div>
        <div className="fs-tools__row">
          <Toggle id="mode-first-sentence" checked={draft.firstSentenceChallenge} onChange={(v) => set('firstSentenceChallenge', v)} label={t('First sentence must not open with agreement')} />
        </div>
        <div className="fs-tools__row">
          <Toggle id="mode-ends-question" checked={draft.endsWithQuestion} onChange={(v) => set('endsWithQuestion', v)} label={t('Must end with a question')} />
        </div>
        <label className="fs-tools__row">
          <span className="fs-tools__text">{t('Max questions (empty = no limit)')}</span>
          <input className="fs-field" type="number" min={0} value={draft.maxQuestions} onChange={(e) => set('maxQuestions', e.target.value)} />
        </label>
        <label className="fs-tools__row">
          <span className="fs-tools__text">{t('Max words (empty = no limit)')}</span>
          <input className="fs-field" type="number" min={0} value={draft.maxWords} onChange={(e) => set('maxWords', e.target.value)} />
        </label>
      </div>
      <label className="fs-modes__prompt-field">
        <span>{t('Forbidden phrases (one per line)')}</span>
        <textarea className="fs-field fs-set__pre" rows={3} value={draft.forbiddenText} onChange={(e) => set('forbiddenText', e.target.value)} />
      </label>
      <div className="fs-set__save">
        <span className="fs-set__save-note" />
        <Button variant="ghost" label={t('Cancel')} onClick={onCancel} disabled={saving} />
        <Button variant="primary" icon={Check} label={t('Save')} onClick={onSave} loading={saving} testId="modes-save" />
      </div>
    </div>
  );
}
