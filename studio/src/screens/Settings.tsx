import {
  Bot,
  Check,
  ExternalLink,
  Keyboard,
  Mic,
  Plug,
  Plus,
  RefreshCw,
  Search,
  Server,
  Settings2,
  Sparkles,
  Trash2,
  X,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useSearchParams } from 'react-router';
import { Button, EmptyState, IconButton, Skeleton, Toast } from '../components';
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
  testEndpoint,
  toggleEndpoint,
  type ModelEndpoint,
  type SchemaField,
  type SchemaGroup,
  type Settings,
} from '../adapters/settings';
import './projects.css';
import './settings.css';

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

type SectionKey = 'models' | 'defaults' | 'voice' | 'search' | 'reminders' | 'agent' | 'shortcuts' | 'system' | 'legacy';

const SECTIONS: { key: SectionKey; label: string; icon: typeof Bot }[] = [
  { key: 'models', label: 'Modelos', icon: Server },
  { key: 'defaults', label: 'IA por defecto', icon: Sparkles },
  { key: 'voice', label: 'Voz', icon: Mic },
  { key: 'search', label: 'Búsqueda', icon: Search },
  { key: 'reminders', label: 'Recordatorios', icon: Check },
  { key: 'agent', label: 'Agente', icon: Bot },
  { key: 'shortcuts', label: 'Atajos', icon: Keyboard },
  { key: 'system', label: 'Sistema', icon: Settings2 },
  { key: 'legacy', label: 'En la interfaz anterior', icon: Plug },
];

const LEGACY_TABS: { tab: string; label: string; help: string }[] = [
  { tab: 'local-models', label: 'Modelos locales', help: 'Descargar y servir modelos en esta máquina (Ollama, llama.cpp).' },
  { tab: 'integrations', label: 'Integraciones', help: 'Claves y cuentas de servicios externos que usan el correo, los recordatorios y las herramientas.' },
  { tab: 'email', label: 'Cuentas de correo', help: 'IMAP/SMTP, Google, estilo de escritura y urgencia.' },
  { tab: 'tools', label: 'Herramientas y MCP', help: 'Servidores MCP, OAuth y sus herramientas.' },
  { tab: 'account', label: 'Cuenta', help: 'Contraseña, verificación en dos pasos, tokens de API, bóveda.' },
  { tab: 'users', label: 'Usuarios', help: 'Cuentas de la instalación y sus permisos.' },
  { tab: 'appearance', label: 'Apariencia y tema', help: 'El editor de colores de la interfaz anterior; Studio usa sus tokens y respeta los temas guardados.' },
];

function legacyHref(tab: string): string {
  return `/?shell=legacy#settings/${tab}`;
}

/* ── Field primitives ── */

type Opt = { value: string; label: string };

function Field({ label, help, htmlFor, children }: { label: string; help?: string; htmlFor?: string; children: ReactNode }) {
  return (
    <div className="fs-set__field">
      <label className="fs-set__label" htmlFor={htmlFor}>
        {label}
      </label>
      <div className="fs-set__control">{children}</div>
      {help && <p className="fs-set__help">{help}</p>}
    </div>
  );
}

function Toggle({ id, checked, onChange, label }: { id: string; checked: boolean; onChange: (v: boolean) => void; label?: string }) {
  return (
    <label className="fs-set__toggle" htmlFor={id}>
      <input id={id} type="checkbox" role="switch" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span className="fs-set__toggle-track" aria-hidden="true" />
      {label && <span>{label}</span>}
    </label>
  );
}

function Select({ id, value, options, onChange, allowEmpty }: { id: string; value: string; options: Opt[]; onChange: (v: string) => void; allowEmpty?: string }) {
  const known = options.some((o) => o.value === value);
  return (
    <select id={id} className="fs-field" value={value} onChange={(e) => onChange(e.target.value)}>
      {allowEmpty !== undefined && <option value="">{allowEmpty}</option>}
      {!known && value && <option value={value}>{value}</option>}
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}

function Text({ id, value, onChange, type = 'text', placeholder, secret }: { id: string; value: string; onChange: (v: string) => void; type?: string; placeholder?: string; secret?: boolean }) {
  return <input id={id} type={secret ? 'password' : type} className="fs-field" value={value} placeholder={placeholder} onChange={(e) => onChange(e.target.value)} autoComplete={secret ? 'new-password' : 'off'} />;
}

/* A section with its own draft, dirty flag and Save. */
function useDraft(settings: Settings | null, keys: string[]) {
  const [draft, setDraft] = useState<Settings>({});
  useEffect(() => {
    if (!settings) return;
    const next: Settings = {};
    for (const k of keys) next[k] = settings[k];
    setDraft(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settings]);
  const set = (k: string, v: unknown) => setDraft((d) => ({ ...d, [k]: v }));
  const changed = useMemo(() => {
    const out: Settings = {};
    if (!settings) return out;
    for (const k of keys) if (JSON.stringify(draft[k]) !== JSON.stringify(settings[k])) out[k] = draft[k];
    return out;
  }, [draft, settings, keys]);
  return { draft, set, changed, dirty: Object.keys(changed).length > 0 };
}

function str(v: unknown, fallback = ''): string {
  return v === null || v === undefined ? fallback : String(v);
}
function bool(v: unknown): boolean {
  return v === true || v === 'true' || v === 1;
}
function list(v: unknown): string {
  return Array.isArray(v) ? v.join(', ') : str(v);
}
function fromList(s: string): string[] {
  return s.split(/[,\n]/).map((x) => x.trim()).filter(Boolean);
}

function SaveBar({ dirty, saving, onSave, note }: { dirty: boolean; saving: boolean; onSave: () => void; note?: string }) {
  return (
    <div className="fs-set__save" data-dirty={dirty || undefined}>
      <span className="fs-set__save-note">{dirty ? 'Hay cambios sin guardar.' : note ?? 'Sin cambios.'}</span>
      <Button variant="primary" size="sm" label="Guardar" disabled={!dirty} loading={saving} onClick={onSave} testId="settings-save" />
    </div>
  );
}

/* ── Models: endpoints ── */

function ModelsSection({ endpoints, onChanged, say }: { endpoints: ModelEndpoint[] | null; onChanged: () => void; say: (t: string) => void }) {
  const [adding, setAdding] = useState(false);
  const [form, setForm] = useState({ name: '', baseUrl: '', apiKey: '', modelType: 'llm', kind: 'auto' });
  const [testing, setTesting] = useState(false);
  const [tested, setTested] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const test = async () => {
    setTesting(true);
    setTested(null);
    try {
      const r = await testEndpoint(form.baseUrl.trim(), form.apiKey);
      setTested(r.ok ? `Responde: ${r.models.length} modelo${r.models.length === 1 ? '' : 's'}${r.models.length ? ` (${r.models.slice(0, 4).join(', ')}${r.models.length > 4 ? '…' : ''})` : ''}.` : `No responde${r.error ? `: ${r.error}` : '.'}`);
    } catch (err) {
      setTested(`No responde: ${(err as Error).message}`);
    } finally {
      setTesting(false);
    }
  };

  const add = async () => {
    if (!form.baseUrl.trim()) {
      say('Falta la URL.');
      return;
    }
    setBusy('add');
    try {
      await addEndpoint({ ...form, baseUrl: form.baseUrl.trim(), name: form.name.trim() });
      setAdding(false);
      setForm({ name: '', baseUrl: '', apiKey: '', modelType: 'llm', kind: 'auto' });
      setTested(null);
      say('Endpoint añadido.');
      onChanged();
    } catch (err) {
      say((err as Error).message || 'No he podido añadir el endpoint.');
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="fs-set__section" aria-labelledby="fs-set-models">
      <header className="fs-set__section-head">
        <div>
          <h2 id="fs-set-models" className="fs-set__title">Modelos</h2>
          <p className="fs-prose">Cada endpoint es un servidor compatible con la API de OpenAI (Ollama, llama.cpp, vLLM, OpenAI, Anthropic vía proxy…). Sus modelos aparecen en el selector de Studio.</p>
        </div>
        <Button variant="primary" size="sm" icon={Plus} label="Añadir endpoint" onClick={() => setAdding((v) => !v)} />
      </header>

      {adding && (
        <div className="fs-set__card">
          <Field label="Nombre" htmlFor="ep-name">
            <Text id="ep-name" value={form.name} onChange={(v) => setForm((f) => ({ ...f, name: v }))} placeholder="Opcional; si no, el host" />
          </Field>
          <Field label="URL base" htmlFor="ep-url" help="Con /v1 al final para servidores compatibles con OpenAI: http://127.0.0.1:11434/v1">
            <Text id="ep-url" value={form.baseUrl} onChange={(v) => setForm((f) => ({ ...f, baseUrl: v }))} placeholder="http://…/v1" />
          </Field>
          <Field label="Clave de API" htmlFor="ep-key" help="Vacía para servidores locales.">
            <Text id="ep-key" value={form.apiKey} onChange={(v) => setForm((f) => ({ ...f, apiKey: v }))} secret />
          </Field>
          <div className="fs-set__grid2">
            <Field label="Tipo" htmlFor="ep-type">
              <Select id="ep-type" value={form.modelType} onChange={(v) => setForm((f) => ({ ...f, modelType: v }))} options={[{ value: 'llm', label: 'Texto (LLM)' }, { value: 'image', label: 'Imágenes' }, { value: 'embedding', label: 'Embeddings' }]} />
            </Field>
            <Field label="Clase" htmlFor="ep-kind">
              <Select id="ep-kind" value={form.kind} onChange={(v) => setForm((f) => ({ ...f, kind: v }))} options={[{ value: 'auto', label: 'Detectar' }, { value: 'local', label: 'Local' }, { value: 'remote', label: 'Remoto (API)' }]} />
            </Field>
          </div>
          {tested && <p className="fs-set__help">{tested}</p>}
          <div className="fs-set__row-actions">
            <Button variant="ghost" size="sm" label="Probar" loading={testing} disabled={!form.baseUrl.trim()} onClick={() => void test()} />
            <span className="fs-set__spacer" />
            <Button variant="ghost" size="sm" label="Cancelar" onClick={() => setAdding(false)} />
            <Button variant="primary" size="sm" label="Añadir" loading={busy === 'add'} onClick={() => void add()} />
          </div>
        </div>
      )}

      {!endpoints && <Skeleton label="Cargando endpoints" count={2} height="64px" />}
      {endpoints && endpoints.length === 0 && <p className="fs-set__help">Todavía no hay ninguno.</p>}
      {endpoints && endpoints.length > 0 && (
        <div className="fs-set__list">
          {endpoints.map((ep) => (
            <article key={ep.id} className="fs-set__ep" data-off={!ep.enabled || undefined} data-online={ep.online || undefined}>
              <div className="fs-set__ep-main">
                <span className="fs-set__ep-dot" aria-hidden="true" />
                <span className="fs-set__ep-name">{ep.name || ep.baseUrl}</span>
                <span className="fs-set__ep-url">{ep.baseUrl}</span>
                <span className="fs-set__ep-meta">
                  {ep.online ? 'en línea' : ep.status || 'sin conexión'} · {ep.models.length} modelo{ep.models.length === 1 ? '' : 's'} · {ep.category || ep.kind}
                  {ep.hasKey ? ' · con clave' : ''}
                  {ep.supportsTools === false ? ' · sin herramientas' : ''}
                </span>
                {ep.pingError && <span className="fs-set__ep-error">{ep.pingError}</span>}
                {ep.models.length > 0 && <span className="fs-set__ep-models">{ep.models.slice(0, 8).join(' · ')}{ep.models.length > 8 ? ` · +${ep.models.length - 8}` : ''}</span>}
              </div>
              <div className="fs-set__ep-actions">
                <IconButton
                  icon={RefreshCw}
                  label="Releer los modelos"
                  size="sm"
                  disabled={busy === ep.id}
                  onClick={() => {
                    setBusy(ep.id);
                    void refreshEndpointModels(ep.id)
                      .then((m) => {
                        say(`${m.length} modelo${m.length === 1 ? '' : 's'}.`);
                        onChanged();
                      })
                      .catch(() => say('No he podido releer los modelos.'))
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
                      .catch(() => say('No he podido cambiarlo.'))
                      .finally(() => setBusy(null));
                  }}
                />
                <IconButton
                  icon={Trash2}
                  label="Quitar el endpoint"
                  size="sm"
                  disabled={busy === ep.id}
                  onClick={() => {
                    if (!window.confirm(`¿Quitar «${ep.name || ep.baseUrl}»? Las conversaciones que lo usen quedarán sin modelo.`)) return;
                    setBusy(ep.id);
                    void deleteEndpoint(ep.id)
                      .then(() => {
                        say('Endpoint quitado.');
                        onChanged();
                      })
                      .catch((err: Error) => say(err.message || 'No he podido quitarlo.'))
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

function ModelPair({ idPrefix, label, help, endpoints, draft, set, epKey, modelKey, allowEmpty }: { idPrefix: string; label: string; help?: string; endpoints: ModelEndpoint[]; draft: Settings; set: (k: string, v: unknown) => void; epKey: string; modelKey: string; allowEmpty?: string }) {
  const epId = str(draft[epKey]);
  const ep = endpoints.find((e) => e.id === epId);
  const models = ep ? ep.models : endpoints.flatMap((e) => e.models);
  return (
    <Field label={label} help={help}>
      <div className="fs-set__pair">
        <Select id={`${idPrefix}-ep`} value={epId} onChange={(v) => set(epKey, v)} allowEmpty={allowEmpty ?? 'Cualquier endpoint'} options={endpoints.map((e) => ({ value: e.id, label: e.name || e.baseUrl }))} />
        <Select id={`${idPrefix}-model`} value={str(draft[modelKey])} onChange={(v) => set(modelKey, v)} allowEmpty="Sin modelo" options={[...new Set(models)].map((m) => ({ value: m, label: m }))} />
      </div>
    </Field>
  );
}

function DefaultsSection({ settings, endpoints, onSave, say }: { settings: Settings | null; endpoints: ModelEndpoint[]; onSave: (patch: Settings) => Promise<void>; say: (t: string) => void }) {
  const { draft, set, changed, dirty } = useDraft(settings, DEFAULT_KEYS);
  const [saving, setSaving] = useState(false);
  const save = async () => {
    setSaving(true);
    try {
      await onSave(changed);
      say('Guardado.');
    } catch (err) {
      say((err as Error).message || 'No he podido guardar.');
    } finally {
      setSaving(false);
    }
  };
  if (!settings) return <Skeleton label="Cargando" count={4} height="56px" />;
  return (
    <section className="fs-set__section" aria-labelledby="fs-set-defaults">
      <header className="fs-set__section-head">
        <div>
          <h2 id="fs-set-defaults" className="fs-set__title">IA por defecto</h2>
          <p className="fs-prose">Qué modelo usa cada cosa cuando no eliges uno a mano.</p>
        </div>
      </header>
      <ModelPair idPrefix="def" label="Chat" help="El de las conversaciones nuevas." endpoints={endpoints} draft={draft} set={set} epKey="default_endpoint_id" modelKey="default_model" />
      <ModelPair idPrefix="task" label="Tareas de fondo" help="Automatizaciones, resúmenes, ordenar la memoria. Vacío: el del chat." endpoints={endpoints} draft={draft} set={set} epKey="task_endpoint_id" modelKey="task_model" allowEmpty="El del chat" />
      <ModelPair idPrefix="util" label="Utilidad (rápido)" help="Títulos, calendario en tus palabras, clasificaciones. Conviene uno pequeño." endpoints={endpoints} draft={draft} set={set} epKey="utility_endpoint_id" modelKey="utility_model" allowEmpty="El de tareas" />
      <Field label="Alternativas de utilidad" htmlFor="util-fb" help="Modelos que se prueban en orden si el de utilidad falla; separados por comas.">
        <Text id="util-fb" value={list(draft.utility_model_fallbacks)} onChange={(v) => set('utility_model_fallbacks', fromList(v))} />
      </Field>
      <Field label="Visión" help="Para leer imágenes adjuntas y capturas.">
        <div className="fs-set__inline">
          <Toggle id="vision-on" checked={bool(draft.vision_enabled)} onChange={(v) => set('vision_enabled', v)} label="Activa" />
          <Select id="vision-model" value={str(draft.vision_model)} onChange={(v) => set('vision_model', v)} allowEmpty="Sin modelo" options={[...new Set(endpoints.flatMap((e) => e.models))].map((m) => ({ value: m, label: m }))} />
        </div>
      </Field>
      <Field label="Alternativas de visión" htmlFor="vision-fb">
        <Text id="vision-fb" value={list(draft.vision_model_fallbacks)} onChange={(v) => set('vision_model_fallbacks', fromList(v))} />
      </Field>
      <ModelPair idPrefix="dispatch" label="Workers (dispatch)" endpoints={endpoints} draft={draft} set={set} epKey="dispatch_endpoint_id" modelKey="dispatch_model" allowEmpty="El del chat" />
      <ModelPair idPrefix="research" label="Deep Research" endpoints={endpoints} draft={draft} set={set} epKey="research_endpoint_id" modelKey="research_model" allowEmpty="El del chat" />
      <div className="fs-set__grid2">
        <Field label="Buscador de Deep Research" htmlFor="research-search">
          <Select id="research-search" value={str(draft.research_search_provider)} onChange={(v) => set('research_search_provider', v)} allowEmpty="El de la búsqueda web" options={[{ value: 'firecrawl', label: 'Firecrawl' }, { value: 'searxng', label: 'SearXNG' }, { value: 'duckduckgo', label: 'DuckDuckGo' }, { value: 'tavily', label: 'Tavily' }, { value: 'brave', label: 'Brave' }, { value: 'google', label: 'Google' }, { value: 'serper', label: 'Serper' }]} />
        </Field>
        <Field label="Máx. tokens por informe" htmlFor="research-tokens">
          <Text id="research-tokens" type="number" value={str(draft.research_max_tokens)} onChange={(v) => set('research_max_tokens', Number(v) || 0)} />
        </Field>
      </div>
      <Field label="Imágenes" help="Generación de imágenes desde el chat.">
        <div className="fs-set__inline">
          <Toggle id="img-on" checked={bool(draft.image_gen_enabled)} onChange={(v) => set('image_gen_enabled', v)} label="Activa" />
          <Text id="img-model" value={str(draft.image_model)} onChange={(v) => set('image_model', v)} placeholder="modelo de imágenes" />
          <Select id="img-quality" value={str(draft.image_quality, 'medium')} onChange={(v) => set('image_quality', v)} options={[{ value: 'low', label: 'Baja (rápida)' }, { value: 'medium', label: 'Media' }, { value: 'high', label: 'Alta' }]} />
        </div>
      </Field>
      <Field label="Profesor (teacher)" help="Un modelo grande que revisa y enseña al pequeño cuando hace falta.">
        <div className="fs-set__inline">
          <Toggle id="teacher-on" checked={bool(draft.teacher_enabled)} onChange={(v) => set('teacher_enabled', v)} label="Activo" />
          <Text id="teacher-model" value={str(draft.teacher_model)} onChange={(v) => set('teacher_model', v)} placeholder="modelo" />
          <Toggle id="teacher-t2" checked={bool(draft.teacher_tier2_enabled)} onChange={(v) => set('teacher_tier2_enabled', v)} label="Segundo nivel" />
        </div>
      </Field>
      <div className="fs-set__grid2">
        <Field label="Salida estructurada en modelos locales" htmlFor="lso">
          <Toggle id="lso" checked={bool(draft.local_structured_output)} onChange={(v) => set('local_structured_output', v)} />
        </Field>
        <Field label="Estilo de los documentos" htmlFor="docstyle" help="Instrucción breve que reciben las herramientas de escritura.">
          <Text id="docstyle" value={str(draft.document_writing_style)} onChange={(v) => set('document_writing_style', v)} />
        </Field>
      </div>
      <Field label="Versiones del chat" help="Cuántas versiones guardar al editar o regenerar y durante cuánto tiempo.">
        <div className="fs-set__inline">
          <Toggle id="cv-on" checked={bool(draft.chat_versions)} onChange={(v) => set('chat_versions', v)} label="Guardar versiones" />
          <Text id="cv-keep" type="number" value={str(draft.chat_versions_keep)} onChange={(v) => set('chat_versions_keep', Number(v) || 0)} placeholder="cuántas" />
          <Text id="cv-hours" type="number" value={str(draft.chat_versions_keep_hours)} onChange={(v) => set('chat_versions_keep_hours', Number(v) || 0)} placeholder="horas" />
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
      say('Guardado.');
    } catch (err) {
      say((err as Error).message || 'No he podido guardar.');
    } finally {
      setSaving(false);
    }
  };
  return { saving, save };
}

const VOICE_KEYS = ['tts_enabled', 'tts_provider', 'tts_model', 'tts_voice', 'tts_speed', 'stt_enabled', 'stt_provider', 'stt_model', 'stt_language'];

function VoiceSection({ settings, endpoints, onSave, say }: { settings: Settings | null; endpoints: ModelEndpoint[]; onSave: (patch: Settings) => Promise<void>; say: (t: string) => void }) {
  const { draft, set, changed, dirty } = useDraft(settings, VOICE_KEYS);
  const { saving, save } = useSaver(onSave, say);
  if (!settings) return <Skeleton label="Cargando" count={3} height="56px" />;
  const apiOpts = endpoints.filter((e) => e.category !== 'local').map((e) => ({ value: `endpoint:${e.id}`, label: `${e.name || e.baseUrl} (API)` }));
  return (
    <section className="fs-set__section" aria-labelledby="fs-set-voice">
      <header className="fs-set__section-head">
        <div>
          <h2 id="fs-set-voice" className="fs-set__title">Voz</h2>
          <p className="fs-prose">Leer en voz alta (TTS) y dictar (STT). «Navegador» usa lo que trae tu navegador; «Local» un modelo en esta máquina; un endpoint, su API.</p>
        </div>
      </header>
      <Field label="Leer en voz alta">
        <div className="fs-set__inline">
          <Toggle id="tts-on" checked={bool(draft.tts_enabled)} onChange={(v) => set('tts_enabled', v)} label="Activo" />
          <Select id="tts-prov" value={str(draft.tts_provider, 'disabled')} onChange={(v) => set('tts_provider', v)} options={[{ value: 'disabled', label: 'Desactivado' }, { value: 'browser', label: 'Navegador' }, { value: 'local', label: 'Local (Kokoro)' }, ...apiOpts]} />
        </div>
      </Field>
      <div className="fs-set__grid2">
        <Field label="Modelo de voz" htmlFor="tts-model">
          <Text id="tts-model" value={str(draft.tts_model)} onChange={(v) => set('tts_model', v)} placeholder="tts-1" />
        </Field>
        <Field label="Voz" htmlFor="tts-voice" help="Local: af_heart y compañía; API: alloy, nova…; navegador: la del sistema si se deja vacío.">
          <Text id="tts-voice" value={str(draft.tts_voice)} onChange={(v) => set('tts_voice', v)} />
        </Field>
      </div>
      <Field label="Velocidad" htmlFor="tts-speed">
        <Select id="tts-speed" value={str(draft.tts_speed, '1')} onChange={(v) => set('tts_speed', v)} options={['0.5', '0.75', '1', '1.25', '1.5', '2'].map((s) => ({ value: s, label: `${s}×` }))} />
      </Field>
      <Field label="Dictado">
        <div className="fs-set__inline">
          <Toggle id="stt-on" checked={bool(draft.stt_enabled)} onChange={(v) => set('stt_enabled', v)} label="Activo" />
          <Select id="stt-prov" value={str(draft.stt_provider, 'disabled')} onChange={(v) => set('stt_provider', v)} options={[{ value: 'disabled', label: 'Desactivado' }, { value: 'browser', label: 'Navegador' }, { value: 'local', label: 'Local (Whisper)' }, ...apiOpts]} />
        </div>
      </Field>
      <div className="fs-set__grid2">
        <Field label="Modelo de dictado" htmlFor="stt-model" help="Local: tiny, base, small, medium, large; API: whisper-1.">
          <Text id="stt-model" value={str(draft.stt_model, 'base')} onChange={(v) => set('stt_model', v)} />
        </Field>
        <Field label="Idioma" htmlFor="stt-lang" help="Código de dos letras (es, en); vacío detecta.">
          <Text id="stt-lang" value={str(draft.stt_language)} onChange={(v) => set('stt_language', v)} placeholder="es" />
        </Field>
      </div>
      <SaveBar dirty={dirty} saving={saving} onSave={() => void save(changed)} />
    </section>
  );
}

const SEARCH_KEYS = ['search_provider', 'search_url', 'search_result_count', 'search_safesearch', 'search_fallback_chain', 'brave_api_key', 'serper_api_key', 'tavily_api_key', 'google_pse_key', 'google_pse_cx', 'firecrawl_url', 'firecrawl_api_key'];
const PROVIDERS: Opt[] = [
  { value: 'firecrawl', label: 'Firecrawl (propio)' },
  { value: 'searxng', label: 'SearXNG (propio)' },
  { value: 'duckduckgo', label: 'DuckDuckGo (sin clave)' },
  { value: 'brave', label: 'Brave Search' },
  { value: 'google_pse', label: 'Google PSE' },
  { value: 'tavily', label: 'Tavily' },
  { value: 'serper', label: 'Serper.dev' },
  { value: 'disabled', label: 'Desactivada' },
];

function SearchSection({ settings, onSave, say }: { settings: Settings | null; onSave: (patch: Settings) => Promise<void>; say: (t: string) => void }) {
  const { draft, set, changed, dirty } = useDraft(settings, SEARCH_KEYS);
  const { saving, save } = useSaver(onSave, say);
  if (!settings) return <Skeleton label="Cargando" count={3} height="56px" />;
  const prov = str(draft.search_provider, 'searxng');
  return (
    <section className="fs-set__section" aria-labelledby="fs-set-search">
      <header className="fs-set__section-head">
        <div>
          <h2 id="fs-set-search" className="fs-set__title">Búsqueda web</h2>
          <p className="fs-prose">El buscador que usa el chip «Web» y las herramientas del agente. Las claves se guardan en el servidor y solo se ven aquí como puntos.</p>
        </div>
      </header>
      <div className="fs-set__grid2">
        <Field label="Proveedor" htmlFor="sp">
          <Select id="sp" value={prov} onChange={(v) => set('search_provider', v)} options={PROVIDERS} />
        </Field>
        <Field label="Resultados por búsqueda" htmlFor="src">
          <Text id="src" type="number" value={str(draft.search_result_count, '5')} onChange={(v) => set('search_result_count', Number(v) || 5)} />
        </Field>
      </div>
      {prov === 'searxng' && (
        <Field label="URL de SearXNG" htmlFor="surl">
          <Text id="surl" value={str(draft.search_url)} onChange={(v) => set('search_url', v)} placeholder="http://localhost:8080" />
        </Field>
      )}
      {prov === 'firecrawl' && (
        <div className="fs-set__grid2">
          <Field label="URL de Firecrawl" htmlFor="fcurl">
            <Text id="fcurl" value={str(draft.firecrawl_url)} onChange={(v) => set('firecrawl_url', v)} placeholder="http://localhost:3002" />
          </Field>
          <Field label="Clave de Firecrawl" htmlFor="fckey">
            <Text id="fckey" value={str(draft.firecrawl_api_key)} onChange={(v) => set('firecrawl_api_key', v)} secret />
          </Field>
        </div>
      )}
      {prov === 'brave' && (
        <Field label="Clave de Brave" htmlFor="brave">
          <Text id="brave" value={str(draft.brave_api_key)} onChange={(v) => set('brave_api_key', v)} secret />
        </Field>
      )}
      {prov === 'serper' && (
        <Field label="Clave de Serper" htmlFor="serper">
          <Text id="serper" value={str(draft.serper_api_key)} onChange={(v) => set('serper_api_key', v)} secret />
        </Field>
      )}
      {prov === 'tavily' && (
        <Field label="Clave de Tavily" htmlFor="tavily">
          <Text id="tavily" value={str(draft.tavily_api_key)} onChange={(v) => set('tavily_api_key', v)} secret />
        </Field>
      )}
      {prov === 'google_pse' && (
        <div className="fs-set__grid2">
          <Field label="Clave de Google PSE" htmlFor="gkey">
            <Text id="gkey" value={str(draft.google_pse_key)} onChange={(v) => set('google_pse_key', v)} secret />
          </Field>
          <Field label="ID del motor (cx)" htmlFor="gcx">
            <Text id="gcx" value={str(draft.google_pse_cx)} onChange={(v) => set('google_pse_cx', v)} />
          </Field>
        </div>
      )}
      <div className="fs-set__grid2">
        <Field label="SafeSearch" htmlFor="ss">
          <Select id="ss" value={str(draft.search_safesearch, 'strict')} onChange={(v) => set('search_safesearch', v)} options={[{ value: 'strict', label: 'Estricto' }, { value: 'moderate', label: 'Moderado' }, { value: 'off', label: 'Apagado' }]} />
        </Field>
        <Field label="Cadena de respaldo" htmlFor="fb" help="Proveedores que se prueban si el principal falla, separados por comas (duckduckgo, brave…).">
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
  if (!settings) return <Skeleton label="Cargando" count={3} height="56px" />;
  const ch = str(draft.reminder_channel, 'browser');
  return (
    <section className="fs-set__section" aria-labelledby="fs-set-rem">
      <header className="fs-set__section-head">
        <div>
          <h2 id="fs-set-rem" className="fs-set__title">Recordatorios</h2>
          <p className="fs-prose">Por dónde llegan los avisos de las notas y del calendario cuando toca.</p>
        </div>
      </header>
      <Field label="Canal" htmlFor="rch">
        <Select id="rch" value={ch} onChange={(v) => set('reminder_channel', v)} options={[{ value: 'browser', label: 'Aviso del navegador' }, { value: 'email', label: 'Correo' }, { value: 'ntfy', label: 'ntfy' }, { value: 'webhook', label: 'Webhook' }]} />
      </Field>
      {ch === 'email' && (
        <Field label="Enviar a" htmlFor="rto" help="Necesita una cuenta SMTP en «Cuentas de correo».">
          <Text id="rto" value={str(draft.reminder_email_to)} onChange={(v) => set('reminder_email_to', v)} placeholder="tu@correo" />
        </Field>
      )}
      {ch === 'ntfy' && (
        <Field label="Tema de ntfy" htmlFor="rntfy">
          <Text id="rntfy" value={str(draft.reminder_ntfy_topic)} onChange={(v) => set('reminder_ntfy_topic', v)} />
        </Field>
      )}
      {ch === 'webhook' && (
        <div className="fs-set__grid2">
          <Field label="Integración (id)" htmlFor="rwh" help="Se elige entre las integraciones de tipo webhook (interfaz anterior).">
            <Text id="rwh" value={str(draft.reminder_webhook_integration_id)} onChange={(v) => set('reminder_webhook_integration_id', v)} />
          </Field>
          <Field label="Plantilla del cuerpo" htmlFor="rwt">
            <Text id="rwt" value={str(draft.reminder_webhook_payload_template)} onChange={(v) => set('reminder_webhook_payload_template', v)} />
          </Field>
        </div>
      )}
      <Field label="Síntesis con IA" help="El modelo redacta el aviso a partir de la nota, con la voz que le digas.">
        <div className="fs-set__inline">
          <Toggle id="rsyn" checked={bool(draft.reminder_llm_synthesis)} onChange={(v) => set('reminder_llm_synthesis', v)} label="Activa" />
          <Text id="rpersona" value={str(draft.reminder_llm_persona)} onChange={(v) => set('reminder_llm_persona', v)} placeholder="p. ej. «un mayordomo seco y amable»" />
        </div>
      </Field>
      <SaveBar dirty={dirty} saving={saving} onSave={() => void save(changed)} />
    </section>
  );
}

const SYSTEM_KEYS = ['app_public_url', 'share_defaults_with_users', 'tool_path_extra_roots', 'urgent_email_prompt', 'gpu_placement_prefer', 'model_load_options', 'skill_max_injected', 'skill_autosave_min_confidence'];

function SystemSection({ settings, onSave, say }: { settings: Settings | null; onSave: (patch: Settings) => Promise<void>; say: (t: string) => void }) {
  const { draft, set, changed, dirty } = useDraft(settings, SYSTEM_KEYS);
  const { saving, save } = useSaver(onSave, say);
  if (!settings) return <Skeleton label="Cargando" count={3} height="56px" />;
  return (
    <section className="fs-set__section" aria-labelledby="fs-set-sys">
      <header className="fs-set__section-head">
        <div>
          <h2 id="fs-set-sys" className="fs-set__title">Sistema</h2>
          <p className="fs-prose">Valores de la instalación. Lo que no está aquí (usuarios, tokens, bóveda, 2FA) sigue en la interfaz anterior.</p>
        </div>
      </header>
      <Field label="URL pública" htmlFor="pub" help="Para los enlaces de los avisos por correo o webhook: https://chat.ejemplo.com">
        <Text id="pub" value={str(draft.app_public_url)} onChange={(v) => set('app_public_url', v)} />
      </Field>
      <Field label="Compartir los valores por defecto con los demás usuarios" htmlFor="share">
        <Toggle id="share" checked={bool(draft.share_defaults_with_users)} onChange={(v) => set('share_defaults_with_users', v)} />
      </Field>
      <Field label="Rutas extra permitidas a las herramientas" htmlFor="roots" help="Carpetas fuera del workspace que el agente puede leer; una por línea o separadas por comas.">
        <Text id="roots" value={list(draft.tool_path_extra_roots)} onChange={(v) => set('tool_path_extra_roots', fromList(v))} />
      </Field>
      <Field label="Prompt de urgencia del correo" htmlFor="urg" help="Cómo decide el modelo que un correo es urgente.">
        <Text id="urg" value={str(draft.urgent_email_prompt)} onChange={(v) => set('urgent_email_prompt', v)} />
      </Field>
      <div className="fs-set__grid2">
        <Field label="Preferencia de GPU" htmlFor="gpu" help="Pista para colocar modelos cuando hay varias.">
          <Text id="gpu" value={str(draft.gpu_placement_prefer)} onChange={(v) => set('gpu_placement_prefer', v)} />
        </Field>
        <Field label="Opciones de carga del modelo" htmlFor="mlo" help="Se pasan tal cual al servidor local (contexto, capas en GPU…).">
          <Text id="mlo" value={typeof draft.model_load_options === 'object' && draft.model_load_options ? JSON.stringify(draft.model_load_options) : str(draft.model_load_options)} onChange={(v) => set('model_load_options', v)} />
        </Field>
      </div>
      <div className="fs-set__grid2">
        <Field label="Skills inyectadas como máximo" htmlFor="skmax">
          <Text id="skmax" type="number" value={str(draft.skill_max_injected)} onChange={(v) => set('skill_max_injected', Number(v) || 0)} />
        </Field>
        <Field label="Confianza mínima para guardar una skill sola" htmlFor="skconf">
          <Text id="skconf" type="number" value={str(draft.skill_autosave_min_confidence)} onChange={(v) => set('skill_autosave_min_confidence', Number(v) || 0)} />
        </Field>
      </div>
      <SaveBar dirty={dirty} saving={saving} onSave={() => void save(changed)} />
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
        setFailed((err as { status?: number })?.status === 403 ? 'Solo el administrador puede ver y cambiar el agente.' : 'No he podido leer el esquema del agente.');
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
  if (!schema || !settings) return <Skeleton label="Cargando el agente" count={6} height="48px" />;
  const total = schema.groups.reduce((n, g) => n + g.fields.length, 0);

  return (
    <section className="fs-set__section" aria-labelledby="fs-set-agent">
      <header className="fs-set__section-head">
        <div>
          <h2 id="fs-set-agent" className="fs-set__title">
            Agente <span className="fs-set__count">{total}</span>
          </h2>
          <p className="fs-prose">Todas las opciones del agente, el navegador y el escritorio, descritas por el servidor. La clave en gris es la que usan `/settings` y las herramientas.</p>
        </div>
        <label className="fs-set__search">
          <Search size={13} aria-hidden="true" />
          <input type="search" placeholder="Filtrar opciones…" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Filtrar" />
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
      <SaveBar dirty={Object.keys(changed).length > 0} saving={saving} onSave={() => void save(changed)} note={`${total} opciones en ${schema.groups.length} grupos.`} />
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

  if (!settings) return <Skeleton label="Cargando" count={4} height="40px" />;
  const saved = settings.keybinds && typeof settings.keybinds === 'object' ? (settings.keybinds as Record<string, unknown>) : {};
  const dirty = Object.keys(binds).some((k) => (binds[k] || '') !== String(saved[k] ?? DEFAULT_KEYBINDS[k] ?? ''));
  const dupes = new Map<string, number>();
  for (const v of Object.values(binds)) if (v) dupes.set(v, (dupes.get(v) ?? 0) + 1);

  return (
    <section className="fs-set__section" aria-labelledby="fs-set-keys">
      <header className="fs-set__section-head">
        <div>
          <h2 id="fs-set-keys" className="fs-set__title">Atajos de teclado</h2>
          <p className="fs-prose">Pulsa «Cambiar» y luego la combinación. Retroceso la deja sin atajo; Escape cancela. Valen en Studio y en la interfaz anterior.</p>
        </div>
        <Button variant="ghost" size="sm" label="Valores por defecto" onClick={() => setBinds({ ...DEFAULT_KEYBINDS })} />
      </header>
      <div className="fs-set__keys">
        {Object.keys(DEFAULT_KEYBINDS).map((k) => (
          <div key={k} className="fs-set__key-row" data-dupe={(binds[k] && (dupes.get(binds[k]) ?? 0) > 1) || undefined}>
            <span className="fs-set__key-label">{KEYBIND_LABELS[k] ?? k}</span>
            <kbd className="fs-set__kbd" data-recording={recording === k || undefined}>
              {recording === k ? 'pulsa una combinación…' : binds[k] ? binds[k].split('+').map((p) => (p === 'ctrl' ? 'Ctrl' : p === 'alt' ? 'Alt' : p === 'shift' ? 'Mayús' : p.length === 1 ? p.toUpperCase() : p)).join(' + ') : '—'}
            </kbd>
            <Button variant="ghost" size="sm" label={recording === k ? 'Cancelar' : 'Cambiar'} onClick={() => setRecording(recording === k ? null : k)} />
          </div>
        ))}
      </div>
      <SaveBar dirty={dirty} saving={saving} onSave={() => void save({ keybinds: binds }).then(() => invalidateSettings())} />
    </section>
  );
}

/* ── Legacy links ── */

function LegacySection() {
  return (
    <section className="fs-set__section" aria-labelledby="fs-set-legacy">
      <header className="fs-set__section-head">
        <div>
          <h2 id="fs-set-legacy" className="fs-set__title">En la interfaz anterior</h2>
          <p className="fs-prose">Estas secciones todavía se editan allí. Cada enlace abre la pestaña justa y vuelves con «Studio».</p>
        </div>
      </header>
      <div className="fs-set__legacy">
        {LEGACY_TABS.map((t) => (
          <a key={t.tab} className="fs-set__legacy-card" href={legacyHref(t.tab)}>
            <span className="fs-set__legacy-title">
              {t.label} <ExternalLink size={12} aria-hidden="true" />
            </span>
            <span className="fs-set__help">{t.help}</span>
          </a>
        ))}
      </div>
    </section>
  );
}

/* ── Screen ── */

export function SettingsScreen() {
  const [params, setParams] = useSearchParams();
  const [section, setSection] = useState<SectionKey>(() => {
    const s = params.get('s') as SectionKey | null;
    return s && SECTIONS.some((x) => x.key === s) ? s : 'models';
  });
  const [settings, setSettings] = useState<Settings | null>(null);
  const [endpoints, setEndpoints] = useState<ModelEndpoint[] | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const epReload = useRef(0);

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
        if ((err as { name?: string })?.name !== 'AbortError') setFailed('No he podido leer los ajustes.');
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
    return (
      <EmptyState
        icon={Settings2}
        title={failed}
        body="La interfaz anterior no depende de esta pantalla."
        primaryAction={{
          label: 'Abrir los ajustes de la interfaz anterior',
          onClick: () => {
            window.location.href = legacyHref('services');
          },
        }}
      />
    );
  }

  return (
    <div className="fs-screen fs-set" data-testid="settings">
      <header className="fs-screen__head">
        <div>
          <h1 className="fs-screen__title">Ajustes</h1>
          <p className="fs-prose" style={{ marginBlockStart: 'var(--fs-space-2)' }}>
            Modelos, valores por defecto, voz, búsqueda, recordatorios, el agente entero y los atajos. Se guarda por sección, solo lo que cambia.
          </p>
        </div>
      </header>
      <div className="fs-set__layout">
        <nav className="fs-set__nav" aria-label="Secciones">
          {SECTIONS.map((s) => (
            <button key={s.key} type="button" className="fs-set__nav-item" data-on={section === s.key || undefined} onClick={() => setSection(s.key)}>
              <s.icon size={14} aria-hidden="true" />
              {s.label}
            </button>
          ))}
        </nav>
        <div className="fs-set__body">
          {section === 'models' && <ModelsSection endpoints={endpoints} onChanged={loadEps} say={say} />}
          {section === 'defaults' && <DefaultsSection settings={settings} endpoints={endpoints ?? []} onSave={onSave} say={say} />}
          {section === 'voice' && <VoiceSection settings={settings} endpoints={endpoints ?? []} onSave={onSave} say={say} />}
          {section === 'search' && <SearchSection settings={settings} onSave={onSave} say={say} />}
          {section === 'reminders' && <RemindersSection settings={settings} onSave={onSave} say={say} />}
          {section === 'agent' && <AgentSection settings={settings} onSave={onSave} say={say} />}
          {section === 'shortcuts' && <ShortcutsSection settings={settings} onSave={onSave} say={say} />}
          {section === 'system' && <SystemSection settings={settings} onSave={onSave} say={say} />}
          {section === 'legacy' && <LegacySection />}
        </div>
      </div>
      {notice && <Toast>{notice}</Toast>}
    </div>
  );
}
