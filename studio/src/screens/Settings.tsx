import {
  Bot,
  Check,
  Copy,
  HardDrive,
  HelpCircle,
  Layers,
  Lock,
  UserRound,
  Users,
  Wrench,
  Keyboard,
  LogIn,
  Palette,
  Mic,
  Plug,
  Plus,
  RefreshCw,
  Search,
  Server,
  Settings2,
  ShieldAlert,
  Sparkles,
  Stethoscope,
  Trash2,
  X,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useSearchParams } from 'react-router';
import { Button, EmptyState, IconButton, Skeleton, Toast } from '../components';
import { getJson } from '../adapters/api';
import {
  addEndpoint,
  comboFromEvent,
  DEFAULT_KEYBINDS,
  deleteEndpoint,
  getAgentSchema,
  invalidateSettings,
  KEYBIND_LABELS,
  listEndpoints,
  loadSettings,
  refreshEndpointModels,
  saveSettings,
  probeSearch,
  searchHealth,
  testEndpoint,
  toggleEndpoint,
  type SearchHealth,
  type ModelEndpoint,
  type SchemaField,
  type SchemaGroup,
  type Settings,
} from '../adapters/settings';
import './projects.css';
import './settings.css';
import { bool, Field, fromList, list, SaveBar, Select, str, Text, Toggle, useDraft, type Opt } from './settings/fields';
import { DeviceSignIn } from './settings/DeviceSignIn';
import { ProviderConnect } from './settings/ProviderConnect';
import { AccountSection } from './settings/Account';
import { UsersSection } from './settings/Users';
import { ToolsSection } from './settings/Tools';
import { SystemExtras } from './settings/SystemExtras';
import { EffectiveConfigSection } from './settings/EffectiveConfig';
import { IntegrationsSection } from './settings/Integrations';
import { LocalModelsSection } from './settings/LocalModels';
import { AppearanceSection } from './settings/Appearance';
import { authStatus } from '../adapters/account';
import { listActiveApprovals, revokeApproval, type Approval } from '../adapters/approvals';
import { addCommandAllowlistEntry, listCommandAllowlist, removeCommandAllowlistEntry, type AllowlistEntry } from '../adapters/commandGuard';
import { t, tn } from '../i18n';

/**
 * Ajustes (the previous interface's settings modal, `/settings`).
 *
 * Model endpoints, the defaults (chat, tasks, utility, vision, research,
 * images), voice, search, reminders, the whole agent form (rendered from
 * the server's schema, the same `/api/agent/settings/schema` the previous
 * form used), the keybinds and a few system values — all over
 * `/api/auth/settings`, posting only what changed. Sections that still
 * live in the previous interface (integrations, email accounts, MCP, users,
 * account security, the theme editor, local models) are listed and open
 * there at their tab.
 */

type SectionKey = 'general' | 'models' | 'local' | 'defaults' | 'voice' | 'search' | 'reminders' | 'integrations' | 'agent' | 'tools' | 'effective_config' | 'shortcuts' | 'account' | 'users' | 'system' | 'health' | 'security';

const SECTIONS: { key: SectionKey; label: string; icon: typeof Bot; admin?: boolean }[] = [
  { key: 'general', label: 'Appearance', icon: Palette },
  { key: 'models', label: 'Models', icon: Server },
  { key: 'local', label: 'Local models', icon: HardDrive },
  // SET-04: search, models, MCP, disk, queue and safe-mode in one card,
  // reusing /api/doctor, /api/safe-mode/status and the search-health probe
  // that already lived in the Search section — never a second store for the
  // same fact.
  { key: 'health', label: 'Health', icon: Stethoscope, admin: true },
  { key: 'defaults', label: 'Default AI', icon: Sparkles },
  { key: 'voice', label: 'Voice', icon: Mic },
  { key: 'search', label: 'Search', icon: Search },
  { key: 'reminders', label: 'Reminders', icon: Check },
  { key: 'integrations', label: 'Integrations', icon: Plug },
  { key: 'agent', label: 'Agent', icon: Bot },
  { key: 'tools', label: 'Tools', icon: Wrench, admin: true },
  // ARCH-03: what actually governs a turn — global -> project -> role/preset ->
  // model -> turn — with sources, overrides and conflicts (src/effective_config.py).
  { key: 'effective_config', label: 'Effective config', icon: Layers, admin: true },
  { key: 'shortcuts', label: 'Shortcuts', icon: Keyboard },
  { key: 'account', label: 'Account', icon: UserRound },
  { key: 'users', label: 'Users', icon: Users, admin: true },
  { key: 'system', label: 'System', icon: Settings2 },
  // SEC-01 / SEC-04: the privacy profile, standing approval concessions
  // (with immediate revocation) and the command-guard allowlist — three
  // authority surfaces that had no screen at all before this lote.
  { key: 'security', label: 'Security', icon: Lock, admin: true },
];




/* ── Models: endpoints ── */

function ModelsSection({ endpoints, onChanged, say }: { endpoints: ModelEndpoint[] | null; onChanged: () => void; say: (t: string) => void }) {
  const [adding, setAdding] = useState(false);
  // Copilot and a ChatGPT plan have no key to paste: they sign in the way a
  // TV app does, and the server makes the endpoint at the end.
  const [signingIn, setSigningIn] = useState(false);
  const [form, setForm] = useState({ name: '', baseUrl: '', apiKey: '', modelType: 'llm', kind: 'auto' });
  const [testing, setTesting] = useState(false);
  const [tested, setTested] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const test = async () => {
    setTesting(true);
    setTested(null);
    try {
      const r = await testEndpoint(form.baseUrl.trim(), form.apiKey);
      setTested(r.ok ? `${t('Responds')}: ${tn(r.models.length, '{n} model', '{n} models')}${r.models.length ? ` (${r.models.slice(0, 4).join(', ')}${r.models.length > 4 ? '…' : ''})` : ''}.` : `${t('Not responding')}${r.error ? `: ${r.error}` : '.'}`);
    } catch (err) {
      setTested(`${t('Not responding')}: ${(err as Error).message}`);
    } finally {
      setTesting(false);
    }
  };

  const add = async () => {
    if (!form.baseUrl.trim()) {
      say(t('The URL is missing.'));
      return;
    }
    setBusy('add');
    try {
      await addEndpoint({ ...form, baseUrl: form.baseUrl.trim(), name: form.name.trim() });
      setAdding(false);
      setForm({ name: '', baseUrl: '', apiKey: '', modelType: 'llm', kind: 'auto' });
      setTested(null);
      say(t('Endpoint added.'));
      onChanged();
    } catch (err) {
      say((err as Error).message || t('Could not add the endpoint.'));
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="fs-set__section" aria-labelledby="fs-set-models">
      <header className="fs-set__section-head">
        <div>
          <h2 id="fs-set-models" className="fs-set__title">{t('Models')}</h2>
          <p className="fs-prose">{t('Connect a cloud provider or a local model server. Choose its models in the chat picker.')}</p>
        </div>
        <div className="fs-set__row-actions">
          <Button variant="ghost" size="sm" icon={LogIn} label={t('Sign in with a subscription')} onClick={() => setSigningIn((v) => !v)} />
          <Button variant="primary" size="sm" icon={Plus} label={t('Add endpoint')} onClick={() => setAdding((v) => !v)} />
        </div>
      </header>

      {signingIn && <DeviceSignIn onDone={onChanged} onClose={() => setSigningIn(false)} say={say} />}

      <ProviderConnect onDone={onChanged} />

      {adding && (
        <div className="fs-set__card">
          <Field label={t('Name')} htmlFor="ep-name">
            <Text id="ep-name" value={form.name} onChange={(v) => setForm((f) => ({ ...f, name: v }))} placeholder={t('Optional; otherwise the host')} />
          </Field>
          <Field label={t('Base URL')} htmlFor="ep-url" help={t('With /v1 at the end for OpenAI-compatible servers: http://127.0.0.1:11434/v1')}>
            <Text id="ep-url" value={form.baseUrl} onChange={(v) => setForm((f) => ({ ...f, baseUrl: v }))} placeholder="http://…/v1" />
          </Field>
          <Field label={t('API key')} htmlFor="ep-key" help={t('Empty for local servers.')}>
            <Text id="ep-key" value={form.apiKey} onChange={(v) => setForm((f) => ({ ...f, apiKey: v }))} secret />
          </Field>
          <div className="fs-set__grid2">
            <Field label={t('Type')} htmlFor="ep-type">
              <Select id="ep-type" value={form.modelType} onChange={(v) => setForm((f) => ({ ...f, modelType: v }))} options={[{ value: 'llm', label: t('Text (LLM)') }, { value: 'image', label: t('Images') }, { value: 'embedding', label: 'Embeddings' }]} />
            </Field>
            <Field label={t('Class')} htmlFor="ep-kind">
              <Select id="ep-kind" value={form.kind} onChange={(v) => setForm((f) => ({ ...f, kind: v }))} options={[{ value: 'auto', label: t('Detect') }, { value: 'local', label: 'Local' }, { value: 'remote', label: t('Remote (API)') }]} />
            </Field>
          </div>
          {tested && <p className="fs-set__help">{tested}</p>}
          <div className="fs-set__row-actions">
            <Button variant="ghost" size="sm" label={t('Test')} loading={testing} disabled={!form.baseUrl.trim()} onClick={() => void test()} />
            <span className="fs-set__spacer" />
            <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setAdding(false)} />
            <Button variant="primary" size="sm" label={t('Add')} loading={busy === 'add'} onClick={() => void add()} />
          </div>
        </div>
      )}

      {!endpoints && <Skeleton label={t('Loading endpoints')} count={2} height="64px" />}
      {endpoints && endpoints.length === 0 && <p className="fs-set__help">{t('None yet.')}</p>}
      {endpoints && endpoints.length > 0 && (
        <div className="fs-set__list">
          {endpoints.map((ep) => (
            <article key={ep.id} className="fs-set__ep" data-off={!ep.enabled || undefined} data-online={ep.online || undefined}>
              <div className="fs-set__ep-main">
                <span className="fs-set__ep-dot" aria-hidden="true" />
                <span className="fs-set__ep-name">{ep.name || ep.baseUrl}</span>
                <span className="fs-set__ep-url">{ep.baseUrl}</span>
                <span className="fs-set__ep-meta">
                  {ep.online ? t('online') : ep.status || t('offline')} · {tn(ep.models.length, '{n} model', '{n} models')} · {ep.category || ep.kind}
                  {ep.hasKey ? t(' · with key') : ''}
                  {ep.supportsTools === false ? t(' · no tools') : ''}
                </span>
                {ep.pingError && <span className="fs-set__ep-error">{ep.pingError}</span>}
                {ep.models.length > 0 && <span className="fs-set__ep-models">{ep.models.slice(0, 8).join(' · ')}{ep.models.length > 8 ? ` · +${ep.models.length - 8}` : ''}</span>}
              </div>
              <div className="fs-set__ep-actions">
                <IconButton
                  icon={RefreshCw}
                  label={t('Reload the models')}
                  size="sm"
                  disabled={busy === ep.id}
                  onClick={() => {
                    setBusy(ep.id);
                    void refreshEndpointModels(ep.id)
                      .then((m) => {
                        say(`${m.length} modelo${m.length === 1 ? '' : 's'}.`);
                        onChanged();
                      })
                      .catch(() => say(t('Could not reload the models.')))
                      .finally(() => setBusy(null));
                  }}
                />
                <Toggle
                  id={`ep-on-${ep.id}`}
                  checked={ep.enabled}
                  onChange={() => {
                    setBusy(ep.id);
                    void toggleEndpoint(ep.id)
                      .then(onChanged)
                      .catch(() => say(t('Could not change it.')))
                      .finally(() => setBusy(null));
                  }}
                />
                <IconButton
                  icon={Trash2}
                  label={t('Remove the endpoint')}
                  size="sm"
                  disabled={busy === ep.id}
                  onClick={() => {
                    if (!window.confirm(t('Remove "{name}"? Conversations using it will be left without a model.', { name: ep.name || ep.baseUrl }))) return;
                    setBusy(ep.id);
                    void deleteEndpoint(ep.id)
                      .then(() => {
                        say(t('Endpoint removed.'));
                        onChanged();
                      })
                      .catch((err: Error) => say(err.message || t('Could not remove it.')))
                      .finally(() => setBusy(null));
                  }}
                />
              </div>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

/* ── Defaults ── */

const DEFAULT_KEYS = [
  'default_endpoint_id', 'default_model',
  'task_endpoint_id', 'task_model',
  'utility_endpoint_id', 'utility_model', 'utility_model_fallbacks',
  'vision_enabled', 'vision_model', 'vision_model_fallbacks',
  'dispatch_endpoint_id', 'dispatch_model',
  'research_endpoint_id', 'research_model', 'research_search_provider', 'research_max_tokens',
  'image_gen_enabled', 'image_model', 'image_quality',
  'teacher_enabled', 'teacher_model', 'teacher_tier2_enabled',
  'local_structured_output', 'document_writing_style',
  'chat_versions', 'chat_versions_keep', 'chat_versions_keep_hours',
];

/**
 * SET-05: what changes (privacy, cost) when the default provider/model
 * moves from one endpoint to another — `GET /api/setup/provider-change-preview`
 * (routes/diagnostics_routes.py). Mirrors that route's response shape.
 */
interface ProviderChangePreview {
  ok: boolean;
  changes: string[];
}

/**
 * Whether saving `changed` needs a provider-change preview at all, and what
 * to ask the route for. Pure on purpose (no fetch, no window.confirm) so
 * `studio/checks/l37-provider-change-preview.check.mjs` can exercise the
 * decision directly, without rendering the screen: only an ACTUAL move from
 * one endpoint to a DIFFERENT one is worth a round trip — a model swap on
 * the same endpoint, or an unrelated field changing, changes neither privacy
 * nor cost and must never prompt.
 */
export function providerChangeQuery(
  savedDefaultEndpointId: unknown,
  changed: Settings,
): { from: string; to: string } | null {
  const from = str(savedDefaultEndpointId);
  const to = typeof changed.default_endpoint_id === 'string' ? changed.default_endpoint_id : '';
  if (!to || to === from) return null;
  return { from, to };
}

/** Only a preview that both loaded AND found a real difference is worth
 * interrupting the save for — see `providerChangeQuery` above. */
export function shouldConfirmBeforeSaving(preview: ProviderChangePreview): boolean {
  return preview.ok && preview.changes.length > 0;
}

function ModelPair({ idPrefix, label, help, endpoints, draft, set, epKey, modelKey, allowEmpty }: { idPrefix: string; label: string; help?: string; endpoints: ModelEndpoint[]; draft: Settings; set: (k: string, v: unknown) => void; epKey: string; modelKey: string; allowEmpty?: string }) {
  const epId = str(draft[epKey]);
  const ep = endpoints.find((e) => e.id === epId);
  const models = ep ? ep.models : endpoints.flatMap((e) => e.models);
  return (
    <Field label={label} help={help}>
      <div className="fs-set__pair">
        <Select id={`${idPrefix}-ep`} value={epId} onChange={(v) => set(epKey, v)} allowEmpty={allowEmpty ?? t('Any endpoint')} options={endpoints.map((e) => ({ value: e.id, label: e.name || e.baseUrl }))} />
        <Select id={`${idPrefix}-model`} value={str(draft[modelKey])} onChange={(v) => set(modelKey, v)} allowEmpty={t('No model')} options={[...new Set(models)].map((m) => ({ value: m, label: m }))} />
      </div>
    </Field>
  );
}

function DefaultsSection({ settings, endpoints, onSave, say }: { settings: Settings | null; endpoints: ModelEndpoint[]; onSave: (patch: Settings) => Promise<void>; say: (t: string) => void }) {
  const { draft, set, changed, dirty } = useDraft(settings, DEFAULT_KEYS);
  const [saving, setSaving] = useState(false);
  const save = async () => {
    // SET-05: the default provider/model is a "consciously" change — before
    // it is saved, preview what moves (privacy, cost) and let the person
    // back out. Advisory: a preview that fails to load must never block the
    // save it was only meant to inform.
    const query = providerChangeQuery(settings?.default_endpoint_id, changed);
    if (query) {
      try {
        const params = new URLSearchParams({ from_endpoint_id: query.from, to_endpoint_id: query.to });
        const preview = await getJson<ProviderChangePreview>(`/api/setup/provider-change-preview?${params.toString()}`);
        if (shouldConfirmBeforeSaving(preview)) {
          const proceed = window.confirm(
            t('Changing the default model changes:\n{changes}\n\nContinue?', { changes: preview.changes.join('\n') }),
          );
          if (!proceed) return;
        }
      } catch {
        // Advisory only — see comment above.
      }
    }
    setSaving(true);
    try {
      await onSave(changed);
      say(t('Saved.'));
    } catch (err) {
      say((err as Error).message || t('Could not save.'));
    } finally {
      setSaving(false);
    }
  };
  if (!settings) return <Skeleton label={t('Loading')} count={4} height="56px" />;
  return (
    <section className="fs-set__section" aria-labelledby="fs-set-defaults">
      <header className="fs-set__section-head">
        <div>
          <h2 id="fs-set-defaults" className="fs-set__title">{t('Default AI')}</h2>
          <p className="fs-prose">{t('Which model each thing uses when you do not pick one by hand.')}</p>
        </div>
      </header>
      <ModelPair idPrefix="def" label="Chat" help={t('The one for new conversations.')} endpoints={endpoints} draft={draft} set={set} epKey="default_endpoint_id" modelKey="default_model" />
      <ModelPair idPrefix="task" label={t('Background tasks')} help={t('Automations, summaries, tidying the memory. Empty: the chat\'s.')} endpoints={endpoints} draft={draft} set={set} epKey="task_endpoint_id" modelKey="task_model" allowEmpty={t('The chat\'s')} />
      <ModelPair idPrefix="util" label={t('Utility (fast)')} help={t('Titles, calendar in your words, classifications. A small one is best.')} endpoints={endpoints} draft={draft} set={set} epKey="utility_endpoint_id" modelKey="utility_model" allowEmpty={t('The tasks\'')} />
      <Field label={t('Utility fallbacks')} htmlFor="util-fb" help={t('Models tried in order if the utility one fails; comma-separated.')}>
        <Text id="util-fb" value={list(draft.utility_model_fallbacks)} onChange={(v) => set('utility_model_fallbacks', fromList(v))} />
      </Field>
      <Field label={t('Vision')} help={t('To read attached images and screenshots.')}>
        <div className="fs-set__inline">
          <Toggle id="vision-on" checked={bool(draft.vision_enabled)} onChange={(v) => set('vision_enabled', v)} label={t('On')} />
          <Select id="vision-model" value={str(draft.vision_model)} onChange={(v) => set('vision_model', v)} allowEmpty={t('No model')} options={[...new Set(endpoints.flatMap((e) => e.models))].map((m) => ({ value: m, label: m }))} />
        </div>
      </Field>
      <Field label={t('Vision fallbacks')} htmlFor="vision-fb">
        <Text id="vision-fb" value={list(draft.vision_model_fallbacks)} onChange={(v) => set('vision_model_fallbacks', fromList(v))} />
      </Field>
      <ModelPair idPrefix="dispatch" label="Workers (dispatch)" endpoints={endpoints} draft={draft} set={set} epKey="dispatch_endpoint_id" modelKey="dispatch_model" allowEmpty={t('The chat\'s')} />
      <ModelPair idPrefix="research" label="Deep Research" endpoints={endpoints} draft={draft} set={set} epKey="research_endpoint_id" modelKey="research_model" allowEmpty={t('The chat\'s')} />
      <div className="fs-set__grid2">
        <Field label={t('Deep Research search engine')} htmlFor="research-search">
          <Select id="research-search" value={str(draft.research_search_provider)} onChange={(v) => set('research_search_provider', v)} allowEmpty={t('The web search\'s')} options={[{ value: 'firecrawl', label: 'Firecrawl' }, { value: 'searxng', label: 'SearXNG' }, { value: 'duckduckgo', label: 'DuckDuckGo' }, { value: 'tavily', label: 'Tavily' }, { value: 'brave', label: 'Brave' }, { value: 'google', label: 'Google' }, { value: 'serper', label: 'Serper' }]} />
        </Field>
        <Field label={t('Max. tokens per report')} htmlFor="research-tokens">
          <Text id="research-tokens" type="number" value={str(draft.research_max_tokens)} onChange={(v) => set('research_max_tokens', Number(v) || 0)} />
        </Field>
      </div>
      <Field label={t('Images')} help={t('Image generation from the chat.')}>
        <div className="fs-set__inline">
          <Toggle id="img-on" checked={bool(draft.image_gen_enabled)} onChange={(v) => set('image_gen_enabled', v)} label={t('On')} />
          <Text id="img-model" value={str(draft.image_model)} onChange={(v) => set('image_model', v)} placeholder={t('image model')} />
          <Select id="img-quality" value={str(draft.image_quality, 'medium')} onChange={(v) => set('image_quality', v)} options={[{ value: 'low', label: t('Low (fast)') }, { value: 'medium', label: t('Medium') }, { value: 'high', label: t('High') }]} />
        </div>
        {/* SET-06: why image generation might not actually work yet, sourced
         *  from the same doctor check the Health section reads — never a
         *  silent grey toggle. */}
        <WhatsMissing area="media" name="engines" />
      </Field>
      <Field label={t('Teacher')} help={t('A big model that reviews and teaches the small one when needed.')}>
        <div className="fs-set__inline">
          <Toggle id="teacher-on" checked={bool(draft.teacher_enabled)} onChange={(v) => set('teacher_enabled', v)} label={t('On')} />
          <Text id="teacher-model" value={str(draft.teacher_model)} onChange={(v) => set('teacher_model', v)} placeholder={t('model')} />
          <Toggle id="teacher-t2" checked={bool(draft.teacher_tier2_enabled)} onChange={(v) => set('teacher_tier2_enabled', v)} label={t('Second tier')} />
        </div>
      </Field>
      <div className="fs-set__grid2">
        <Field label={t('Structured output on local models')} htmlFor="lso">
          <Toggle id="lso" checked={bool(draft.local_structured_output)} onChange={(v) => set('local_structured_output', v)} />
        </Field>
        <Field label={t('Document style')} htmlFor="docstyle" help={t('A short instruction the writing tools receive.')}>
          <Text id="docstyle" value={str(draft.document_writing_style)} onChange={(v) => set('document_writing_style', v)} />
        </Field>
      </div>
      <Field label={t('Chat versions')} help={t('How many versions to keep when editing or regenerating, and for how long.')}>
        <div className="fs-set__inline">
          <Toggle id="cv-on" checked={bool(draft.chat_versions)} onChange={(v) => set('chat_versions', v)} label={t('Keep versions')} />
          <Text id="cv-keep" type="number" value={str(draft.chat_versions_keep)} onChange={(v) => set('chat_versions_keep', Number(v) || 0)} placeholder={t('how many')} />
          <Text id="cv-hours" type="number" value={str(draft.chat_versions_keep_hours)} onChange={(v) => set('chat_versions_keep_hours', Number(v) || 0)} placeholder={t('hours')} />
        </div>
      </Field>
      <SaveBar dirty={dirty} saving={saving} onSave={() => void save()} />
    </section>
  );
}

/* ── Generic saving section wrapper ── */

function useSaver(onSave: (patch: Settings) => Promise<void>, say: (t: string) => void) {
  const [saving, setSaving] = useState(false);
  const save = async (changed: Settings) => {
    setSaving(true);
    try {
      await onSave(changed);
      say(t('Saved.'));
    } catch (err) {
      say((err as Error).message || t('Could not save.'));
    } finally {
      setSaving(false);
    }
  };
  return { saving, save };
}

const VOICE_KEYS = ['tts_enabled', 'tts_provider', 'tts_model', 'tts_voice', 'tts_speed', 'stt_enabled', 'stt_provider', 'stt_model', 'stt_language', 'stt_device'];

function VoiceSection({ settings, endpoints, onSave, say }: { settings: Settings | null; endpoints: ModelEndpoint[]; onSave: (patch: Settings) => Promise<void>; say: (t: string) => void }) {
  const { draft, set, changed, dirty } = useDraft(settings, VOICE_KEYS);
  const { saving, save } = useSaver(onSave, say);
  if (!settings) return <Skeleton label={t('Loading')} count={3} height="56px" />;
  const apiOpts = endpoints.filter((e) => e.category !== 'local').map((e) => ({ value: `endpoint:${e.id}`, label: `${e.name || e.baseUrl} (API)` }));
  return (
    <section className="fs-set__section" aria-labelledby="fs-set-voice">
      <header className="fs-set__section-head">
        <div>
          <h2 id="fs-set-voice" className="fs-set__title">{t('Voice')}</h2>
          <p className="fs-prose">{t('Read aloud (TTS) and dictate (STT). "Browser" uses what your browser ships; "Local" a model on this machine; an endpoint, its API.')}</p>
        </div>
      </header>
      <p className="fs-prose">{t('For local voice on Windows: Whisper for input and Windows installed voices for output. The first Whisper use downloads its model. CPU leaves GPU memory free for your assistant.')}</p>
      <Button variant="ghost" size="sm" label={t('Prepare local voice on Windows')} onClick={() => {
        set('stt_enabled', true); set('stt_provider', 'local'); set('stt_model', 'base'); set('stt_device', 'cpu');
        set('tts_enabled', true); set('tts_provider', 'system'); set('tts_voice', '');
      }} />
      <Field label={t('Read aloud')}>
        <div className="fs-set__inline">
          <Toggle id="tts-on" checked={bool(draft.tts_enabled)} onChange={(v) => set('tts_enabled', v)} label={t('On')} />
          <Select id="tts-prov" value={str(draft.tts_provider, 'disabled')} onChange={(v) => set('tts_provider', v)} options={[{ value: 'disabled', label: t('Off') }, { value: 'browser', label: t('Browser') }, { value: 'system', label: t('Windows · installed voices (offline)') }, { value: 'local', label: 'Local (Kokoro)' }, ...apiOpts]} />
        </div>
      </Field>
      <div className="fs-set__grid2">
        <Field label={t('Voice model')} htmlFor="tts-model">
          <Text id="tts-model" value={str(draft.tts_model)} onChange={(v) => set('tts_model', v)} placeholder="tts-1" />
        </Field>
        <Field label='Voice' htmlFor="tts-voice" help={t('Local: af_heart and friends; API: alloy, nova…; browser: the system\'s if left empty.')}>
          <Text id="tts-voice" value={str(draft.tts_voice)} onChange={(v) => set('tts_voice', v)} />
        </Field>
      </div>
      <Field label={t('Speed')} htmlFor="tts-speed">
        <Select id="tts-speed" value={str(draft.tts_speed, '1')} onChange={(v) => set('tts_speed', v)} options={['0.5', '0.75', '1', '1.25', '1.5', '2'].map((s) => ({ value: s, label: `${s}×` }))} />
      </Field>
      <Field label={t('Dictation')}>
        <div className="fs-set__inline">
          <Toggle id="stt-on" checked={bool(draft.stt_enabled)} onChange={(v) => set('stt_enabled', v)} label={t('On')} />
          <Select id="stt-prov" value={str(draft.stt_provider, 'disabled')} onChange={(v) => set('stt_provider', v)} options={[{ value: 'disabled', label: t('Off') }, { value: 'browser', label: t('Browser') }, { value: 'local', label: 'Local (Whisper)' }, ...apiOpts]} />
        </div>
      </Field>
      <div className="fs-set__grid2">
        <Field label={t('Dictation model')} htmlFor="stt-model" help={t(t('Local: tiny, base, small, medium, large; API: whisper-1.'))}>
          <Text id="stt-model" value={str(draft.stt_model, 'base')} onChange={(v) => set('stt_model', v)} />
        </Field>
        <Field label={t('Language')} htmlFor="stt-lang" help={t('Two-letter code (es, en); empty detects.')}>
          <Text id="stt-lang" value={str(draft.stt_language)} onChange={(v) => set('stt_language', v)} placeholder="es" />
        </Field>
      </div>
      <Field label={t('Transcription device')} htmlFor="stt-device">
        <Select id="stt-device" value={str(draft.stt_device, 'auto')} onChange={v => set('stt_device', v)} options={[{ value: 'auto', label: t('Automatic') }, { value: 'cpu', label: 'CPU · int8' }, { value: 'cuda', label: 'GPU · CUDA' }]} />
      </Field>
      <SaveBar dirty={dirty} saving={saving} onSave={() => void save(changed)} />
    </section>
  );
}

const SEARCH_KEYS = ['search_provider', 'search_url', 'search_result_count', 'search_safesearch', 'search_fallback_chain', 'brave_api_key', 'serper_api_key', 'tavily_api_key', 'google_pse_key', 'google_pse_cx', 'firecrawl_url', 'firecrawl_api_key'];
const PROVIDERS: Opt[] = [
  { value: 'firecrawl', label: 'Firecrawl (self-hosted)' },
  { value: 'searxng', label: 'SearXNG (self-hosted)' },
  { value: 'duckduckgo', label: 'DuckDuckGo (no key)' },
  { value: 'brave', label: 'Brave Search' },
  { value: 'google_pse', label: 'Google PSE' },
  { value: 'tavily', label: 'Tavily' },
  { value: 'serper', label: 'Serper.dev' },
  { value: 'disabled', label: 'Disabled' },
];

/**
 * Which SearXNG engines are really answering. A result count is not health:
 * on 09-09-2026 every query "returned 10 results" and all ten came from bing,
 * which hands back the same pages for any phrasing, while the other engines
 * sat suspended — the research read nothing new and blamed the search.
 */
/* ── SET-04 / SET-06: one shared /api/doctor read, several consumers ──
 *
 * `doctor.run()` (src/doctor.py) already answers "what is worth doing about
 * this machine" per area — model, search, storage, queue/renders, GPU,
 * plugins/MCP, browser — with a state, a cause (`detail`) and an action
 * (`fix`). SET-04's health card and SET-06's inline "what's missing" text
 * are two views of the exact same findings, so both read through this one
 * cached fetch instead of each polling /api/doctor on its own.
 */
export interface DoctorFinding { area: string; name: string; state: string; detail: string; fix: string; facts: Record<string, unknown> }
export interface DoctorReport { ok: boolean; checked_at: string; worst: string; counts: Record<string, number>; findings: DoctorFinding[]; rendered?: string }

let doctorCache: { at: number; report: Promise<DoctorReport> } | null = null;
function loadDoctorReport(fresh = false): Promise<DoctorReport> {
  if (fresh || !doctorCache || Date.now() - doctorCache.at > 15000) {
    doctorCache = { at: Date.now(), report: getJson<DoctorReport>('/api/doctor?verbose=true') };
  }
  return doctorCache.report;
}

const STATE_RANK: Record<string, number> = { fail: 0, unknown: 1, warn: 2, absent: 3, ok: 4 };

/**
 * SET-06: "a disabled feature explains whether it's missing a model,
 * permission, engine or setting, instead of just sitting there as a grey
 * button." Any section can drop this next to a toggle that depends on a
 * doctor-checked area; it renders nothing once that area is fine, so it
 * never becomes a manual nobody reads.
 */
function WhatsMissing({ area, name }: { area: string; name?: string }) {
  const [findings, setFindings] = useState<DoctorFinding[] | null>(null);
  useEffect(() => {
    let live = true;
    void loadDoctorReport().then((r) => { if (live) setFindings(r.findings.filter((f) => f.area === area && (!name || f.name === name))); }).catch(() => { if (live) setFindings([]); });
    return () => { live = false; };
  }, [area, name]);
  const worst = (findings ?? []).filter((f) => f.state !== 'ok').sort((a, b) => STATE_RANK[a.state] - STATE_RANK[b.state])[0];
  if (!worst) return null;
  return (
    <p className="fs-set__help" data-tone={worst.state === 'fail' ? 'bad' : undefined} data-testid="whats-missing">
      <HelpCircle size={12} aria-hidden="true" style={{ verticalAlign: 'text-bottom' }} />{' '}
      {worst.detail}{worst.fix ? ` — ${worst.fix}` : ''}
    </p>
  );
}

function HealthSection({ say, onJump }: { say: (t: string) => void; onJump: (key: SectionKey) => void }) {
  const [report, setReport] = useState<DoctorReport | null>(null);
  const [safeMode, setSafeMode] = useState<{ active: boolean; reason: string; disabled: string[]; quarantined_mcp_servers: { id?: string; name?: string }[] } | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [rep, sm] = await Promise.all([
        loadDoctorReport(true),
        getJson<{ active: boolean; reason: string; disabled: string[]; quarantined_mcp_servers: { id?: string; name?: string }[] }>('/api/safe-mode/status'),
      ]);
      setReport(rep);
      setSafeMode(sm);
    } catch (e) {
      say((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [say]);
  useEffect(() => { void refresh(); }, [refresh]);

  const byArea = useMemo(() => {
    const groups = new Map<string, DoctorFinding[]>();
    for (const f of report?.findings ?? []) {
      const list = groups.get(f.area) ?? [];
      list.push(f);
      groups.set(f.area, list);
    }
    return [...groups.entries()].sort(([a, fa], [b, fb]) => {
      const wa = Math.min(...fa.map((f) => STATE_RANK[f.state] ?? 9));
      const wb = Math.min(...fb.map((f) => STATE_RANK[f.state] ?? 9));
      return wa - wb || a.localeCompare(b);
    });
  }, [report]);

  const copyDiagnostic = async () => {
    if (!report) return;
    try {
      await navigator.clipboard.writeText(report.rendered ?? JSON.stringify(report, null, 2));
      say(t('Diagnostic copied'));
    } catch {
      say(t('The browser refused the clipboard.'));
    }
  };

  const reactivate = async (subsystem?: string, mcpServerId?: string) => {
    const key = subsystem ?? mcpServerId ?? '';
    setBusy(key);
    try {
      await fetch('/api/safe-mode/reactivate', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(subsystem ? { subsystem } : { mcp_server_id: mcpServerId }) });
      await refresh();
      say(t('Reactivated'));
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="fs-set__section" aria-labelledby="fs-set-health">
      <div className="fs-set__row-between">
        <h2 id="fs-set-health" className="fs-set__title">{t('Health')}</h2>
        <div className="fs-set__actions">
          <Button size="sm" variant="ghost" icon={Copy} label={t('Copy sanitized diagnostic')} onClick={() => void copyDiagnostic()} disabled={!report} />
          <Button size="sm" variant="secondary" icon={RefreshCw} label={t('Recheck')} loading={loading} onClick={() => void refresh()} />
        </div>
      </div>
      <p className="fs-prose">{t('Every area is checked and shown on its own — one thing being down (ComfyUI, say) never reads as the whole application being down, and text chat keeps working regardless.')}</p>

      {safeMode?.active && (
        <div className="fs-set__card" data-tone="bad">
          <h3 className="fs-set__card-title">{t('Safe mode is on')}</h3>
          <p className="fs-set__help">{safeMode.reason}</p>
          {safeMode.disabled.length > 0 && (
            <ul className="fs-set__health-list">
              {safeMode.disabled.map((name) => (
                <li key={name}>
                  <strong>{name}</strong>
                  <Button size="sm" variant="ghost" label={t('Turn back on')} loading={busy === name} onClick={() => void reactivate(name)} />
                </li>
              ))}
            </ul>
          )}
          {safeMode.quarantined_mcp_servers.length > 0 && (
            <ul className="fs-set__health-list">
              {safeMode.quarantined_mcp_servers.map((s) => (
                <li key={s.id ?? s.name}>
                  <strong>{s.name ?? s.id}</strong> ({t('MCP server')})
                  <Button size="sm" variant="ghost" label={t('Turn back on')} loading={busy === s.id} onClick={() => void reactivate(undefined, s.id)} />
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {!report && loading && <Skeleton label={t('Checking')} count={4} height="40px" />}

      {byArea.map(([area, findings]) => {
        const worst = findings.reduce((w, f) => (STATE_RANK[f.state] < STATE_RANK[w.state] ? f : w), findings[0]);
        const tone = worst.state === 'fail' ? 'bad' : worst.state === 'warn' ? undefined : worst.state === 'ok' ? 'good' : undefined;
        return (
          <div key={area} className="fs-set__card" data-tone={tone} data-testid="health-area">
            <div className="fs-set__row-between">
              <h3 className="fs-set__card-title">{area}</h3>
              <span className="fs-set__help">{worst.state}</span>
            </div>
            <ul className="fs-set__health-list">
              {findings.map((f) => (
                <li key={f.name}>
                  <strong>{f.name}</strong>: {f.detail}
                  {f.fix && <span> — {f.fix}</span>}
                </li>
              ))}
            </ul>
            {area === 'models' && <Button size="sm" variant="ghost" label={t('Open Local models')} onClick={() => onJump('local')} />}
            {area === 'media' && <Button size="sm" variant="ghost" label={t('Open Default AI')} onClick={() => onJump('defaults')} />}
          </div>
        );
      })}

      <SearchHealthCard />
      {report && <p className="fs-set__help">{t('Last checked {time}', { time: new Date(report.checked_at).toLocaleTimeString() })}</p>}
    </section>
  );
}

function SearchHealthCard() {
  const [health, setHealth] = useState<SearchHealth | null>(null);
  const [error, setError] = useState('');
  const [probing, setProbing] = useState(false);
  const refresh = useCallback(async () => {
    try { setHealth(await searchHealth()); setError(''); } catch (e) { setError((e as Error).message); }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  const probe = async () => {
    setProbing(true);
    try {
      const out = await probeSearch('whiplash associated disorders clinical practice guideline');
      if (out.error) setError(out.error);
      await refresh();
    } finally {
      setProbing(false);
    }
  };
  const last = health?.lastCall ?? null;
  const answered = last ? Object.entries(last.answered).sort((a, b) => b[1] - a[1]) : [];
  const tone = !last ? undefined : health?.singleEngine || answered.length === 0 ? 'bad' : 'good';
  return (
    <div className="fs-set__card fs-set__search-health" data-tone={tone} data-testid="search-health">
      <div className="fs-set__row-between">
        <h3 className="fs-set__card-title">{t('Search health')}</h3>
        <Button size="sm" variant="ghost" label={probing ? t('Searching…') : t('Test the search now')} loading={probing} onClick={() => void probe()} />
      </div>
      {health && (
        <p className="fs-set__help">
          {t('Engines asked for: {list}', { list: health.configuredEngines.join(', ') || t('the instance\'s defaults') })}
        </p>
      )}
      {error && <p className="fs-set__help" data-tone="bad" role="alert">{error}</p>}
      {!last && !error && <p className="fs-set__help">{t('No search has run since the server started. Test it to see which engines answer.')}</p>}
      {last && (
        <ul className="fs-set__health-list">
          <li>
            <strong>{t('Answered')}:</strong>{' '}
            {answered.length ? answered.map(([name, n]) => `${name} (${n})`).join(', ') : t('nobody')}
            {' '}· {t('{n} results', { n: last.results })}
          </li>
          {last.unresponsive.length > 0 && (
            <li data-tone="bad">
              <strong>{t('Not answering')}:</strong>{' '}
              {last.unresponsive.map((u) => `${u.engine} — ${u.reason || t('no answer')}`).join('; ')}
            </li>
          )}
          {last.silent.length > 0 && (
            <li>
              <strong>{t('Asked but returned nothing')}:</strong> {last.silent.join(', ')}
            </li>
          )}
          {health?.singleEngine && (
            <li data-tone="bad">
              {t('Only one engine is carrying every result. It will hand back the same pages for any phrasing of a topic, so a research run finds nothing new after round one.')}
            </li>
          )}
          <li className="fs-set__help">{t('Last query: “{q}”', { q: last.query })}</li>
        </ul>
      )}
    </div>
  );
}

function SearchSection({ settings, onSave, say }: { settings: Settings | null; onSave: (patch: Settings) => Promise<void>; say: (t: string) => void }) {
  const { draft, set, changed, dirty } = useDraft(settings, SEARCH_KEYS);
  const { saving, save } = useSaver(onSave, say);
  if (!settings) return <Skeleton label={t('Loading')} count={3} height="56px" />;
  const prov = str(draft.search_provider, 'searxng');
  return (
    <section className="fs-set__section" aria-labelledby="fs-set-search">
      <header className="fs-set__section-head">
        <div>
          <h2 id="fs-set-search" className="fs-set__title">{t('Web search')}</h2>
          <p className="fs-prose">{t('The search engine behind the "Web" chip and the agent tools. Keys are stored on the server and only show here as dots.')}</p>
        </div>
      </header>
      <div className="fs-set__grid2">
        <Field label={t('Provider')} htmlFor="sp">
          <Select id="sp" value={prov} onChange={(v) => set('search_provider', v)} options={PROVIDERS.map((p) => ({ ...p, label: t(p.label) }))} />
        </Field>
        <Field label={t('Results per search')} htmlFor="src">
          <Text id="src" type="number" value={str(draft.search_result_count, '5')} onChange={(v) => set('search_result_count', Number(v) || 5)} />
        </Field>
      </div>
      {prov === 'searxng' && (
        <Field label={t('SearXNG URL')} htmlFor="surl">
          <Text id="surl" value={str(draft.search_url)} onChange={(v) => set('search_url', v)} placeholder="http://localhost:8080" />
        </Field>
      )}
      {prov === 'searxng' && <SearchHealthCard />}
      {prov === 'firecrawl' && (
        <div className="fs-set__grid2">
          <Field label={t('Firecrawl URL')} htmlFor="fcurl">
            <Text id="fcurl" value={str(draft.firecrawl_url)} onChange={(v) => set('firecrawl_url', v)} placeholder="http://localhost:3002" />
          </Field>
          <Field label={t('Firecrawl key')} htmlFor="fckey">
            <Text id="fckey" value={str(draft.firecrawl_api_key)} onChange={(v) => set('firecrawl_api_key', v)} secret />
          </Field>
        </div>
      )}
      {prov === 'brave' && (
        <Field label={t('Brave key')} htmlFor="brave">
          <Text id="brave" value={str(draft.brave_api_key)} onChange={(v) => set('brave_api_key', v)} secret />
        </Field>
      )}
      {prov === 'serper' && (
        <Field label={t('Serper key')} htmlFor="serper">
          <Text id="serper" value={str(draft.serper_api_key)} onChange={(v) => set('serper_api_key', v)} secret />
        </Field>
      )}
      {prov === 'tavily' && (
        <Field label={t('Tavily key')} htmlFor="tavily">
          <Text id="tavily" value={str(draft.tavily_api_key)} onChange={(v) => set('tavily_api_key', v)} secret />
        </Field>
      )}
      {prov === 'google_pse' && (
        <div className="fs-set__grid2">
          <Field label={t('Google PSE key')} htmlFor="gkey">
            <Text id="gkey" value={str(draft.google_pse_key)} onChange={(v) => set('google_pse_key', v)} secret />
          </Field>
          <Field label={t('Engine ID (cx)')} htmlFor="gcx">
            <Text id="gcx" value={str(draft.google_pse_cx)} onChange={(v) => set('google_pse_cx', v)} />
          </Field>
        </div>
      )}
      <div className="fs-set__grid2">
        <Field label="SafeSearch" htmlFor="ss">
          <Select id="ss" value={str(draft.search_safesearch, 'strict')} onChange={(v) => set('search_safesearch', v)} options={[{ value: 'strict', label: t('Strict') }, { value: 'moderate', label: t('Moderate') }, { value: 'off', label: t('Off') }]} />
        </Field>
        <Field label={t('Fallback chain')} htmlFor="fb" help={t('Providers tried if the main one fails, comma-separated (duckduckgo, brave…).')}>
          <Text id="fb" value={list(draft.search_fallback_chain)} onChange={(v) => set('search_fallback_chain', fromList(v))} />
        </Field>
      </div>
      <SaveBar dirty={dirty} saving={saving} onSave={() => void save(changed)} />
    </section>
  );
}

const REMINDER_KEYS = ['reminder_channel', 'reminder_email_to', 'reminder_ntfy_topic', 'reminder_llm_synthesis', 'reminder_llm_persona', 'reminder_webhook_integration_id', 'reminder_webhook_payload_template'];

function RemindersSection({ settings, onSave, say }: { settings: Settings | null; onSave: (patch: Settings) => Promise<void>; say: (t: string) => void }) {
  const { draft, set, changed, dirty } = useDraft(settings, REMINDER_KEYS);
  const { saving, save } = useSaver(onSave, say);
  if (!settings) return <Skeleton label={t('Loading')} count={3} height="56px" />;
  const ch = str(draft.reminder_channel, 'browser');
  return (
    <section className="fs-set__section" aria-labelledby="fs-set-rem">
      <header className="fs-set__section-head">
        <div>
          <h2 id="fs-set-rem" className="fs-set__title">Recordatorios</h2>
          <p className="fs-prose">{t('Where the notes and calendar alerts arrive when they are due.')}</p>
        </div>
      </header>
      <Field label={t('Channel')} htmlFor="rch">
        <Select id="rch" value={ch} onChange={(v) => set('reminder_channel', v)} options={[{ value: 'browser', label: t('Browser notification') }, { value: 'email', label: t('Mail') }, { value: 'ntfy', label: 'ntfy' }, { value: 'webhook', label: 'Webhook' }]} />
      </Field>
      {ch === 'email' && (
        <Field label={t('Send to')} htmlFor="rto" help={t('Needs an SMTP account under "Mail accounts".')}>
          <Text id="rto" value={str(draft.reminder_email_to)} onChange={(v) => set('reminder_email_to', v)} placeholder={t('you@mail')} />
        </Field>
      )}
      {ch === 'ntfy' && (
        <Field label={t('ntfy topic')} htmlFor="rntfy">
          <Text id="rntfy" value={str(draft.reminder_ntfy_topic)} onChange={(v) => set('reminder_ntfy_topic', v)} />
        </Field>
      )}
      {ch === 'webhook' && (
        <div className="fs-set__grid2">
          <Field label={t('Integration (id)')} htmlFor="rwh" help={t('Picked among the webhook integrations (previous interface).')}>
            <Text id="rwh" value={str(draft.reminder_webhook_integration_id)} onChange={(v) => set('reminder_webhook_integration_id', v)} />
          </Field>
          <Field label={t('Body template')} htmlFor="rwt">
            <Text id="rwt" value={str(draft.reminder_webhook_payload_template)} onChange={(v) => set('reminder_webhook_payload_template', v)} />
          </Field>
        </div>
      )}
      <Field label={t('AI synthesis')} help={t('The model writes the alert from the note, in the voice you give it.')}>
        <div className="fs-set__inline">
          <Toggle id="rsyn" checked={bool(draft.reminder_llm_synthesis)} onChange={(v) => set('reminder_llm_synthesis', v)} label={t('On')} />
          <Text id="rpersona" value={str(draft.reminder_llm_persona)} onChange={(v) => set('reminder_llm_persona', v)} placeholder={t('e.g. "a dry, kind butler"')} />
        </div>
      </Field>
      <SaveBar dirty={dirty} saving={saving} onSave={() => void save(changed)} />
    </section>
  );
}

const SYSTEM_KEYS = ['app_public_url', 'share_defaults_with_users', 'tool_path_extra_roots', 'urgent_email_prompt', 'gpu_placement_prefer', 'model_load_options', 'skill_max_injected', 'skill_autosave_min_confidence',
  'vram_admission', 'vram_admission_timeout_seconds', 'research_local_tokens_per_second', 'research_local_time_multiplier'];
const VRAM_ADMISSION: Opt[] = [
  { value: 'ask', label: 'Ask what to unload (recommended)' },
  { value: 'auto', label: 'Unload the least useful models on its own' },
  { value: 'off', label: 'Off — load anyway, Ollama spills to CPU/PCIe' },
];

/* -- General: the interface language -- */

function SystemSection({ settings, onSave, say, admin }: { settings: Settings | null; onSave: (patch: Settings) => Promise<void>; say: (t: string) => void; admin: boolean }) {
  const { draft, set, changed, dirty } = useDraft(settings, SYSTEM_KEYS);
  const { saving, save } = useSaver(onSave, say);
  if (!settings) return <Skeleton label={t('Loading')} count={3} height="56px" />;
  return (
    <section className="fs-set__section" aria-labelledby="fs-set-sys">
      <header className="fs-set__section-head">
        <div>
          <h2 id="fs-set-sys" className="fs-set__title">Sistema</h2>
          <p className="fs-prose">{t('Installation values; for administrators, the log, the backup and the wipes.')}</p>
        </div>
      </header>
      <Field label={t('Public URL')} htmlFor="pub" help={t('For the links in mail or webhook alerts: https://chat.example.com')}>
        <Text id="pub" value={str(draft.app_public_url)} onChange={(v) => set('app_public_url', v)} />
      </Field>
      <Field label={t('Share the defaults with the other users')} htmlFor="share">
        <Toggle id="share" checked={bool(draft.share_defaults_with_users)} onChange={(v) => set('share_defaults_with_users', v)} />
      </Field>
      <Field label={t('Extra paths allowed to the tools')} htmlFor="roots" help={t('Folders outside the workspace the agent may read; one per line or comma-separated.')}>
        <Text id="roots" value={list(draft.tool_path_extra_roots)} onChange={(v) => set('tool_path_extra_roots', fromList(v))} />
      </Field>
      <Field label={t('Mail urgency prompt')} htmlFor="urg" help={t('How the model decides a mail is urgent.')}>
        <Text id="urg" value={str(draft.urgent_email_prompt)} onChange={(v) => set('urgent_email_prompt', v)} />
      </Field>
      <div className="fs-set__grid2">
        <Field label={t('GPU preference')} htmlFor="gpu" help={t('A hint for placing models when there are several.')}>
          <Text id="gpu" value={str(draft.gpu_placement_prefer)} onChange={(v) => set('gpu_placement_prefer', v)} />
        </Field>
        <Field label={t('Model load options')} htmlFor="mlo" help={t('Passed as-is to the local server (context, GPU layers…).')}>
          <Text id="mlo" value={typeof draft.model_load_options === 'object' && draft.model_load_options ? JSON.stringify(draft.model_load_options) : str(draft.model_load_options)} onChange={(v) => set('model_load_options', v)} />
        </Field>
      </div>
      <div className="fs-set__grid2">
        <Field label={t('When a model does not fit in VRAM')} htmlFor="vadm" help={t('Ollama never says no: it loads anyway and spills to the CPU, ten times slower. Two 27B models stacked this way took the machine down on 08-09-2026.')}>
          <Select id="vadm" value={str(draft.vram_admission, 'ask')} onChange={(v) => set('vram_admission', v)} options={VRAM_ADMISSION.map((o) => ({ ...o, label: t(o.label) }))} />
        </Field>
        <Field label={t('Seconds to wait for the answer')} htmlFor="vadmt" help={t('Nobody answers → the load is cancelled, never forced.')}>
          <Text id="vadmt" type="number" value={str(draft.vram_admission_timeout_seconds, '600')} onChange={(v) => set('vram_admission_timeout_seconds', Number(v) || 600)} />
        </Field>
      </div>
      <div className="fs-set__grid2">
        <Field label={t('Local model speed for research (tokens/s)')} htmlFor="rtps" help={t('Sizes how long one research call may take: 120 s plus the tokens asked for at this speed. 8 is a 27B q4 on consumer cards.')}>
          <Text id="rtps" type="number" value={str(draft.research_local_tokens_per_second, '8')} onChange={(v) => set('research_local_tokens_per_second', Number(v) || 8)} />
        </Field>
        <Field label={t('Research wall clock × for local models')} htmlFor="rmult" help={t('The research time limit is multiplied by this when the model is local; four rounds and one report took 22 minutes on a 27B.')}>
          <Text id="rmult" type="number" value={str(draft.research_local_time_multiplier, '3')} onChange={(v) => set('research_local_time_multiplier', Number(v) || 3)} />
        </Field>
      </div>
      <div className="fs-set__grid2">
        <Field label={t('Skills injected at most')} htmlFor="skmax">
          <Text id="skmax" type="number" value={str(draft.skill_max_injected)} onChange={(v) => set('skill_max_injected', Number(v) || 0)} />
        </Field>
        <Field label={t('Minimum confidence to save a skill on its own')} htmlFor="skconf">
          <Text id="skconf" type="number" value={str(draft.skill_autosave_min_confidence)} onChange={(v) => set('skill_autosave_min_confidence', Number(v) || 0)} />
        </Field>
      </div>
      <SaveBar dirty={dirty} saving={saving} onSave={() => void save(changed)} />
      {admin && <SystemExtras say={say} />}
    </section>
  );
}

/* ── Security: privacy profile, active concessions, command-guard allowlist ── */

/** SEC-04: `src/privacy_policy.py::PROFILES` — the three values that module
 *  accepts, named exactly as it names them (`get_privacy_profile` treats
 *  anything else as the default). `privacy_profile` is a registered
 *  `DEFAULT_SETTINGS` key, so it round-trips through the same generic
 *  `POST /api/auth/settings` every other field on this screen already uses
 *  — no dedicated `/api/privacy/profile` route was needed or exists. */
const PRIVACY_PROFILE: Opt[] = [
  { value: 'local_only', label: 'Local only — every auxiliary (embeddings, the reranker, compaction) is blocked from reaching a non-local endpoint' },
  { value: 'local_preferred', label: 'Local preferred (default) — nothing blocked here; per-request safety checks still apply' },
  { value: 'cloud_allowed', label: 'Cloud allowed — remote auxiliaries are explicitly acceptable' },
];

function PrivacyProfileCard({ settings, onSave, say }: { settings: Settings | null; onSave: (patch: Settings) => Promise<void>; say: (t: string) => void }) {
  const { draft, set, changed, dirty } = useDraft(settings, ['privacy_profile']);
  const { saving, save } = useSaver(onSave, say);
  if (!settings) return <Skeleton label={t('Loading')} count={1} height="56px" />;
  return (
    <div className="fs-set__card">
      <h3 className="fs-set__card-title">{t('Privacy profile')}</h3>
      <p className="fs-set__help">{t('What every auxiliary component (not just the main chat model) is allowed to send off this machine. Wired: the custom/HTTP embedding lane, the ChromaDB vector store, the remote compaction summarizer and the reranker. OCR and telemetry call sites have not been audited against this profile yet.')}</p>
      <Field label={t('Active profile')} htmlFor="privacy-profile">
        <Select id="privacy-profile" value={str(draft.privacy_profile, 'local_preferred')} onChange={(v) => set('privacy_profile', v)} options={PRIVACY_PROFILE.map((o) => ({ ...o, label: t(o.label) }))} />
      </Field>
      <SaveBar dirty={dirty} saving={saving} onSave={() => void save(changed)} />
    </div>
  );
}

/** SEC-01: standing concessions — granted, not expired, with uses left —
 *  and immediate revocation, independent of what a model or a document
 *  claims. `GET /api/approvals/active` / `DELETE /api/approvals/{id}`. */
function ActiveApprovalsCard({ say }: { say: (t: string) => void }) {
  const [items, setItems] = useState<Approval[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(() => {
    setErr(null);
    listActiveApprovals().then(setItems).catch((e: Error) => setErr(e.message));
  }, []);
  useEffect(load, [load]);

  const revoke = (a: Approval) => {
    if (!window.confirm(t('Revoke this concession now? {action} will need a fresh approval next time.', { action: a.plan.action }))) return;
    setBusy(a.id);
    revokeApproval(a.id)
      .then(() => { say(t('Revoked.')); load(); })
      .catch((e: Error) => say(e.message))
      .finally(() => setBusy(null));
  };

  return (
    <div className="fs-set__card">
      <h3 className="fs-set__card-title fs-tools__cat">
        <span><ShieldAlert size={16} aria-hidden /> {t('Active concessions')}</span>
        <Button size="sm" variant="ghost" icon={RefreshCw} label={t('Refresh')} onClick={load} />
      </h3>
      <p className="fs-set__help">{t('Every standing yes a model or a document could currently point to and say "I have permission" — independent of what it claims, since nothing here reads its own request as authority.')}</p>
      {err && <p className="fs-set__err">{err}</p>}
      {!items && !err ? (
        <Skeleton label={t('Loading')} count={2} height="48px" />
      ) : items && items.length === 0 ? (
        <p className="fs-set__help">{t('Nothing standing right now.')}</p>
      ) : (
        <ul className="fs-wipe">
          {(items ?? []).map((a) => (
            <li key={a.id} className="fs-wipe__row">
              <span>
                <strong>{a.plan.action}</strong>{a.plan.detail ? ` — ${a.plan.detail}` : ''}
                <span className="fs-set__help">
                  {t('granted by {who} · {n} use(s) left', { who: a.decided_by || '?', n: a.uses_left })}
                  {a.expires_at ? ` · ${t('expires {when}', { when: a.expires_at })}` : ''}
                  {a.owner ? ` · ${a.owner}` : ''}
                </span>
              </span>
              <Button size="sm" variant="danger" label={t('Revoke')} loading={busy === a.id} disabled={busy !== null} onClick={() => revoke(a)} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

const ALLOWLIST_KIND: Opt[] = [
  { value: 'exact', label: 'Exact command' },
  { value: 'prefix', label: 'Prefix' },
];

/** SEC-01: `routes/command_guard_routes.py`'s allowlist, previously reachable
 *  only by calling the route directly — no Studio screen read or wrote it. */
function CommandGuardAllowlistCard({ say }: { say: (t: string) => void }) {
  const [items, setItems] = useState<AllowlistEntry[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [pattern, setPattern] = useState('');
  const [kind, setKind] = useState('exact');
  const [reason, setReason] = useState('');
  const [ttl, setTtl] = useState('');
  const [adding, setAdding] = useState(false);

  const load = useCallback(() => {
    setErr(null);
    listCommandAllowlist().then(setItems).catch((e: Error) => setErr(e.message));
  }, []);
  useEffect(load, [load]);

  const add = () => {
    if (!pattern.trim()) return;
    setAdding(true);
    addCommandAllowlistEntry({ pattern: pattern.trim(), kind, reason: reason.trim(), ttl_hours: ttl.trim() ? Number(ttl.trim()) : null })
      .then(() => { setPattern(''); setReason(''); setTtl(''); say(t('Added to the allowlist.')); load(); })
      .catch((e: Error) => say(e.message))
      .finally(() => setAdding(false));
  };
  const remove = (entry: AllowlistEntry) => {
    if (!window.confirm(t('Remove "{pattern}" from the allowlist? Faustus will classify it normally again.', { pattern: entry.pattern }))) return;
    setBusy(entry.pattern);
    removeCommandAllowlistEntry(entry.pattern)
      .then(() => { say(t('Removed.')); load(); })
      .catch((e: Error) => say(e.message))
      .finally(() => setBusy(null));
  };

  return (
    <div className="fs-set__card">
      <h3 className="fs-set__card-title fs-tools__cat">
        <span>{t('Command guard allowlist')}</span>
        <Button size="sm" variant="ghost" icon={RefreshCw} label={t('Refresh')} onClick={load} />
      </h3>
      <p className="fs-set__help">{t('A command matching one of these entries skips command-guard classification entirely — a standing authority downgrade, so keep this short-lived and specific.')}</p>
      <div className="fs-set__row">
        <input className="fs-field" placeholder={t('command or prefix')} value={pattern} onChange={(e) => setPattern(e.target.value)} aria-label={t('Pattern')} />
        <Select id="allowlist-kind" value={kind} options={ALLOWLIST_KIND.map((o) => ({ ...o, label: t(o.label) }))} onChange={setKind} />
        <input className="fs-field" placeholder={t('reason')} value={reason} onChange={(e) => setReason(e.target.value)} aria-label={t('Reason')} />
        <input className="fs-field" type="number" min={0} placeholder={t('hours (blank = never expires)')} value={ttl} onChange={(e) => setTtl(e.target.value)} aria-label={t('TTL hours')} />
        <Button size="sm" variant="secondary" icon={Plus} label={t('Add')} loading={adding} disabled={!pattern.trim()} onClick={add} />
      </div>
      {err && <p className="fs-set__err">{err}</p>}
      {!items && !err ? (
        <Skeleton label={t('Loading')} count={2} height="40px" />
      ) : items && items.length === 0 ? (
        <p className="fs-set__help">{t('The allowlist is empty.')}</p>
      ) : (
        <ul className="fs-wipe">
          {(items ?? []).map((e) => (
            <li key={e.pattern} className="fs-wipe__row">
              <span>
                <strong><code className="fs-tools__id">{e.pattern}</code></strong>
                <span className="fs-set__help">
                  {t(ALLOWLIST_KIND.find((k) => k.value === e.kind)?.label ?? e.kind)}{e.reason ? ` · ${e.reason}` : ''}{e.added_by ? ` · ${e.added_by}` : ''}
                  {e.expires_at ? ` · ${t('expires {when}', { when: e.expires_at })}` : ''}
                </span>
              </span>
              <Button size="sm" variant="danger" label={t('Remove')} loading={busy === e.pattern} disabled={busy !== null} onClick={() => remove(e)} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function SecuritySection({ settings, onSave, say }: { settings: Settings | null; onSave: (patch: Settings) => Promise<void>; say: (t: string) => void }) {
  return (
    <section className="fs-set__section" aria-labelledby="fs-set-security">
      <header className="fs-set__section-head">
        <div>
          <h2 id="fs-set-security" className="fs-set__title">{t('Security')}</h2>
          <p className="fs-prose">{t('Authority a model or a document can currently claim — visible and revocable independent of what it says: the privacy profile, standing approvals and the command-guard allowlist.')}</p>
        </div>
      </header>
      <PrivacyProfileCard settings={settings} onSave={onSave} say={say} />
      <ActiveApprovalsCard say={say} />
      <CommandGuardAllowlistCard say={say} />
    </section>
  );
}

/* ── Agent: rendered from the server's schema ── */

function SchemaControl({ field, value, onChange }: { field: SchemaField; value: unknown; onChange: (v: unknown) => void }) {
  const id = `agset-${field.key}`;
  if (field.type === 'bool') return <Toggle id={id} checked={bool(value)} onChange={onChange} />;
  if (field.type === 'select') return <Select id={id} value={str(value)} onChange={onChange} options={field.options ?? []} />;
  if (field.type === 'int' || field.type === 'float') {
    return (
      <input
        id={id}
        type="number"
        className="fs-field fs-set__num"
        value={str(value)}
        min={field.min}
        max={field.max}
        step={field.step ?? (field.type === 'float' ? 0.1 : 1)}
        onChange={(e) => {
          const t = e.target.value.trim();
          if (t === '') return onChange(value);
          let n = field.type === 'int' ? parseInt(t, 10) : parseFloat(t);
          if (Number.isNaN(n)) return;
          if (typeof field.min === 'number' && n < field.min) n = field.min;
          if (typeof field.max === 'number' && n > field.max) n = field.max;
          onChange(n);
        }}
      />
    );
  }
  if (field.type === 'list') return <Text id={id} value={list(value)} onChange={(v) => onChange(fromList(v))} />;
  return <Text id={id} value={str(value)} onChange={onChange} />;
}

function AgentSection({ settings, onSave, say }: { settings: Settings | null; onSave: (patch: Settings) => Promise<void>; say: (t: string) => void }) {
  const [schema, setSchema] = useState<{ groups: SchemaGroup[]; defaults: Settings } | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [draft, setDraft] = useState<Settings>({});
  const { saving, save } = useSaver(onSave, say);

  useEffect(() => {
    const c = new AbortController();
    getAgentSchema(c.signal)
      .then(setSchema)
      .catch((err: unknown) => {
        if ((err as { name?: string })?.name === 'AbortError') return;
        setFailed((err as { status?: number })?.status === 403 ? t('Only the administrator can see and change the agent.') : t('Could not read the agent\'s schema.'));
      });
    return () => c.abort();
  }, []);

  useEffect(() => {
    if (!settings || !schema) return;
    const next: Settings = {};
    for (const g of schema.groups) for (const f of g.fields) next[f.key] = settings[f.key] ?? schema.defaults[f.key];
    setDraft(next);
  }, [settings, schema]);

  const changed = useMemo(() => {
    const out: Settings = {};
    if (!settings || !schema) return out;
    for (const g of schema.groups) for (const f of g.fields) if (JSON.stringify(draft[f.key]) !== JSON.stringify(settings[f.key] ?? schema.defaults[f.key])) out[f.key] = draft[f.key];
    return out;
  }, [draft, settings, schema]);

  const q = query.trim().toLowerCase();
  const matches = (f: SchemaField) => !q || q.split(/\s+/).every((t) => `${f.key} ${f.label} ${f.help}`.toLowerCase().includes(t));

  if (failed) return <p className="fs-set__help">{failed}</p>;
  if (!schema || !settings) return <Skeleton label={t('Loading the agent')} count={6} height="48px" />;
  const total = schema.groups.reduce((n, g) => n + g.fields.length, 0);

  return (
    <section className="fs-set__section" aria-labelledby="fs-set-agent">
      <header className="fs-set__section-head">
        <div>
          <h2 id="fs-set-agent" className="fs-set__title">
            {t('Agent')} <span className="fs-set__count">{total}</span>
          </h2>
          <p className="fs-prose">{t('Every option of the agent, the browser and the desktop, described by the server. The grey key is the one `/settings` and the tools use.')}</p>
        </div>
        <label className="fs-set__search">
          <Search size={13} aria-hidden="true" />
          <input type="search" placeholder={t('Filter options…')} value={query} onChange={(e) => setQuery(e.target.value)} aria-label={t('Filter')} />
        </label>
      </header>
      {schema.groups.map((g) => {
        const fields = g.fields.filter(matches);
        if (!fields.length) return null;
        return (
          <details key={g.key || g.title} className="fs-set__group" open={Boolean(q) || undefined}>
            <summary className="fs-set__group-head">
              {g.title} <span className="fs-set__count">{fields.length}</span>
            </summary>
            {g.help && <p className="fs-set__help">{g.help}</p>}
            <div className="fs-set__group-body">
              {fields.map((f) => (
                <div key={f.key} className="fs-set__field fs-set__field--schema" data-changed={JSON.stringify(draft[f.key]) !== JSON.stringify(settings[f.key] ?? schema.defaults[f.key]) || undefined}>
                  <div className="fs-set__schema-text">
                    <label className="fs-set__label" htmlFor={`agset-${f.key}`}>
                      {f.label}
                      {f.restart_hint && <span className="fs-set__restart">reinicio</span>}
                    </label>
                    <code className="fs-set__key">{f.key}</code>
                    {f.help && <p className="fs-set__help">{f.help}</p>}
                  </div>
                  <div className="fs-set__control">
                    <SchemaControl field={f} value={draft[f.key]} onChange={(v) => setDraft((d) => ({ ...d, [f.key]: v }))} />
                  </div>
                </div>
              ))}
            </div>
          </details>
        );
      })}
      <SaveBar dirty={Object.keys(changed).length > 0} saving={saving} onSave={() => void save(changed)} note={t('{count} options in {groups} groups.', {count: total, groups: schema.groups.length})} />
    </section>
  );
}

/* ── Shortcuts ── */

function ShortcutsSection({ settings, onSave, say }: { settings: Settings | null; onSave: (patch: Settings) => Promise<void>; say: (t: string) => void }) {
  const [binds, setBinds] = useState<Record<string, string>>({});
  const [recording, setRecording] = useState<string | null>(null);
  const { saving, save } = useSaver(onSave, say);
  useEffect(() => {
    if (!settings) return;
    const raw = settings.keybinds && typeof settings.keybinds === 'object' ? (settings.keybinds as Record<string, unknown>) : {};
    const next = { ...DEFAULT_KEYBINDS };
    for (const [k, v] of Object.entries(raw)) if (typeof v === 'string') next[k] = v;
    setBinds(next);
  }, [settings]);

  useEffect(() => {
    if (!recording) return;
    const onKey = (e: KeyboardEvent) => {
      e.preventDefault();
      e.stopPropagation();
      if (e.key === 'Escape') {
        setRecording(null);
        return;
      }
      if (e.key === 'Backspace' || e.key === 'Delete') {
        setBinds((b) => ({ ...b, [recording]: '' }));
        setRecording(null);
        return;
      }
      const combo = comboFromEvent(e);
      if (!combo) return;
      setBinds((b) => ({ ...b, [recording]: combo }));
      setRecording(null);
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [recording]);

  if (!settings) return <Skeleton label={t('Loading')} count={4} height="40px" />;
  const saved = settings.keybinds && typeof settings.keybinds === 'object' ? (settings.keybinds as Record<string, unknown>) : {};
  const dirty = Object.keys(binds).some((k) => (binds[k] || '') !== String(saved[k] ?? DEFAULT_KEYBINDS[k] ?? ''));
  const dupes = new Map<string, number>();
  for (const v of Object.values(binds)) if (v) dupes.set(v, (dupes.get(v) ?? 0) + 1);

  return (
    <section className="fs-set__section" aria-labelledby="fs-set-keys">
      <header className="fs-set__section-head">
        <div>
          <h2 id="fs-set-keys" className="fs-set__title">{t('Keyboard shortcuts')}</h2>
          <p className="fs-prose">{t('Press "Change" and then the combination. Backspace clears it; Escape cancels. They work in Studio and in the previous interface.')}</p>
        </div>
        <Button variant="ghost" size="sm" label={t('Defaults')} onClick={() => setBinds({ ...DEFAULT_KEYBINDS })} />
      </header>
      <div className="fs-set__keys">
        {Object.keys(DEFAULT_KEYBINDS).map((k) => (
          <div key={k} className="fs-set__key-row" data-dupe={(binds[k] && (dupes.get(binds[k]) ?? 0) > 1) || undefined}>
            <span className="fs-set__key-label">{KEYBIND_LABELS[k] ? t(KEYBIND_LABELS[k]) : k}</span>
            <kbd className="fs-set__kbd" data-recording={recording === k || undefined}>
              {recording === k ? t('press a combination…') : binds[k] ? binds[k].split('+').map((p) => (p === 'ctrl' ? 'Ctrl' : p === 'alt' ? 'Alt' : p === 'shift' ? t('Shift') : p.length === 1 ? p.toUpperCase() : p)).join(' + ') : '—'}
            </kbd>
            <Button variant="ghost" size="sm" label={recording === k ? t('Cancel') : t('Change')} onClick={() => setRecording(recording === k ? null : k)} />
          </div>
        ))}
      </div>
      <SaveBar dirty={dirty} saving={saving} onSave={() => void save({ keybinds: binds }).then(() => invalidateSettings())} />
    </section>
  );
}


/* ── Screen ── */

export function SettingsScreen() {
  const [params, setParams] = useSearchParams();
  const [section, setSection] = useState<SectionKey>(() => {
    const s = params.get('s') as SectionKey | null;
    return s && SECTIONS.some((x) => x.key === s) ? s : 'general';
  });
  const [settings, setSettings] = useState<Settings | null>(null);
  const [endpoints, setEndpoints] = useState<ModelEndpoint[] | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  // ACT-06: a 401/403 here is not the same situation as the server being
  // unreachable — retrying does not fix "not signed in as an admin" the way
  // it fixes a dropped connection, and 426 (this client is below the
  // server's supported floor, src/api_version.py) never will either.
  const [failedStatus, setFailedStatus] = useState<number | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [admin, setAdmin] = useState(false);
  const epReload = useRef(0);
  useEffect(() => {
    authStatus().then((st) => setAdmin(st.is_admin === true || st.auth_enabled === false)).catch(() => {});
  }, []);

  const say = useCallback((t: string) => {
    setNotice(t);
    window.setTimeout(() => setNotice((c) => (c === t ? null : c)), 4000);
  }, []);

  useEffect(() => {
    const next = new URLSearchParams(params);
    if (next.get('s') !== section) {
      next.set('s', section);
      setParams(next, { replace: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [section]);

  useEffect(() => {
    const c = new AbortController();
    loadSettings(c.signal)
      .then(setSettings)
      .catch((err: unknown) => {
        if ((err as { name?: string })?.name === 'AbortError') return;
        const status = (err as { status?: number })?.status ?? null;
        setFailedStatus(status);
        setFailed(
          status === 401 || status === 403
            ? t('Sign in as an administrator to see and change the settings.')
            : status === 426
              ? t('This client is older than the server supports. Update it before continuing.')
              : t('Could not read the settings.'),
        );
      });
    return () => c.abort();
  }, []);

  const loadEps = useCallback(() => {
    const id = ++epReload.current;
    listEndpoints()
      .then((list) => {
        if (id === epReload.current) setEndpoints(list);
      })
      .catch(() => setEndpoints([]));
  }, []);
  useEffect(loadEps, [loadEps]);

  const onSave = async (patch: Settings) => {
    const next = await saveSettings(patch);
    setSettings(next);
    invalidateSettings();
  };

  if (failed) {
    const tone = failedStatus === 401 || failedStatus === 403 ? 'denied' : failedStatus === 426 ? 'incompatible' : 'error';
    return (
      <EmptyState
        icon={tone === 'error' ? Settings2 : undefined}
        tone={tone}
        title={failed}
        body={tone === 'error' ? t('The server did not answer /api/auth/settings.') : t('/api/auth/settings answered {status}.', { status: String(failedStatus) })}
        primaryAction={{
          label: t('Try again'),
          onClick: () => window.location.reload(),
        }}
      />
    );
  }

  return (
    <div className="fs-screen fs-set" data-testid="settings">
      <header className="fs-screen__head">
        <div>
          <h1 className="fs-screen__title">{t('Settings')}</h1>
          <p className="fs-prose" style={{ marginBlockStart: 'var(--fs-space-2)' }}>
            {t('Models, defaults, voice, search, reminders, the whole agent and the shortcuts. Saved per section, only what changes.')}
          </p>
        </div>
      </header>
      <div className="fs-set__layout">
        <nav className="fs-set__nav" aria-label={t('Sections')}>
          {SECTIONS.filter((s) => !s.admin || admin).map((s) => (
            <button key={s.key} type="button" className="fs-set__nav-item" data-on={section === s.key || undefined} onClick={() => setSection(s.key)}>
              <s.icon size={14} aria-hidden="true" />
              {t(s.label)}
            </button>
          ))}
        </nav>
        <div className="fs-set__body">
          {section === 'general' && <AppearanceSection say={say} />}
          {section === 'models' && <ModelsSection endpoints={endpoints} onChanged={loadEps} say={say} />}
          {section === 'local' && <LocalModelsSection admin={admin} say={say} />}
          {section === 'defaults' && <DefaultsSection settings={settings} endpoints={endpoints ?? []} onSave={onSave} say={say} />}
          {section === 'voice' && <VoiceSection settings={settings} endpoints={endpoints ?? []} onSave={onSave} say={say} />}
          {section === 'search' && <SearchSection settings={settings} onSave={onSave} say={say} />}
          {section === 'reminders' && <RemindersSection settings={settings} onSave={onSave} say={say} />}
          {section === 'agent' && <AgentSection settings={settings} onSave={onSave} say={say} />}
          {section === 'integrations' && <IntegrationsSection say={say} />}
          {section === 'tools' && <ToolsSection say={say} />}
          {section === 'effective_config' && <EffectiveConfigSection say={say} />}
          {section === 'shortcuts' && <ShortcutsSection settings={settings} onSave={onSave} say={say} />}
          {section === 'account' && <AccountSection say={say} />}
          {section === 'users' && <UsersSection say={say} />}
          {section === 'system' && <SystemSection settings={settings} onSave={onSave} say={say} admin={admin} />}
          {section === 'health' && <HealthSection say={say} onJump={setSection} />}
          {section === 'security' && <SecuritySection settings={settings} onSave={onSave} say={say} />}
        </div>
      </div>
      {notice && <Toast>{notice}</Toast>}
    </div>
  );
}
