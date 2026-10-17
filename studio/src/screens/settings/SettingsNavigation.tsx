import { Search } from 'lucide-react';
import { useState } from 'react';
import { t } from '../../i18n';
import { ContextualTools } from '../../shell/contextualTools';

export const SETTINGS_GROUPS = [
  { id: 'personal', label: 'General', sections: ['general', 'device', 'voice', 'reminders', 'shortcuts', 'account', 'users'] },
  { id: 'ai', label: 'AI and models', sections: ['models', 'defaults', 'sparks', 'local', 'openrouter', 'model_router'] },
  { id: 'connections', label: 'Connections', sections: ['integrations', 'search'] },
  { id: 'behavior', label: 'Agent behaviour', sections: ['agent', 'modes', 'repositories', 'tools'] },
  { id: 'system', label: 'System and diagnostics', sections: ['system', 'health', 'security', 'effective_config'] },
] as const;

const TERMS: Record<string, string> = {
  general: 'theme language font colour color idioma tema fuente apariencia',
  device: 'notifications install app browser notificaciones instalar movil',
  models: 'endpoint provider api key server proveedor clave servidor',
  defaults: 'temperature sampling tokens image vision research default temperatura muestreo imagen investigacion defecto',
  sparks: 'dgx backend cluster servidor', local: 'download gpu ollama llama descargar gpu',
  openrouter: 'provider privacy cost privacidad coste', model_router: 'automatic routing hardware enrutamiento',
  voice: 'speech microphone dictation piper tts stt voz microfono dictado',
  reminders: 'calendar events calendario eventos recordatorios',
  integrations: 'mcp hoards connectors plugins integrations conexiones conectores herramientas',
  search: 'web searx brave search engine buscar buscador',
  agent: 'autonomy budget reasoning rounds timeout permissions autonomia presupuesto razonamiento permisos',
  modes: 'persona tone style character personalidad tono estilo comportamiento',
  repositories: 'git commit branch push repositorio rama',
  tools: 'hooks lifecycle functions permissions herramientas funciones',
  effective_config: 'overrides sources configuration configuracion efectiva origen',
  shortcuts: 'keyboard keys atajos teclado', security: 'privacy approvals commands privacidad aprobaciones comandos',
  health: 'doctor errors services diagnostics errores servicios diagnostico', system: 'storage backup logs updates cache almacenamiento copias registros actualizaciones',
};

const normal = (value: string) => value.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();

export function settingMatches(key: string, label: string, query: string): boolean {
  const text = normal(`${label} ${t(label)} ${TERMS[key] ?? ''}`);
  return normal(query).trim().split(/\s+/).every(word => text.includes(word));
}

/** Five intentions first; only the selected family's sections are listed. */
export function SettingsNavigation<K extends string>({ sections, section, onChange }: {
  sections: { key: K; label: string }[]; section: K; onChange: (key: K) => void;
}) {
  const [query, setQuery] = useState('');
  const active = SETTINGS_GROUPS.find(group => (group.sections as readonly string[]).includes(section)) ?? SETTINGS_GROUPS[0];
  const visible = query.trim() ? sections.filter(item => settingMatches(item.key, item.label, query))
    : sections.filter(item => (active.sections as readonly string[]).includes(item.key));
  return <nav className="fs-set__nav" aria-label={t('Sections')} data-testid="settings-navigation" data-searching={Boolean(query.trim()) || undefined}>
    <label className="fs-set__find"><Search size={14} aria-hidden="true" /><input type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder={t('Find a setting')} aria-label={t('Find a setting')} /></label>
    {!query.trim() && <div className="fs-set__mobile-nav">
      <label>{t('Area')}<select className="fs-field" value={active.id} onChange={event => {
        const group = SETTINGS_GROUPS.find(item => item.id === event.target.value);
        const first = group && sections.find(item => (group.sections as readonly string[]).includes(item.key));
        if (first) onChange(first.key);
      }}>{SETTINGS_GROUPS.filter(group => sections.some(item => (group.sections as readonly string[]).includes(item.key))).map(group => <option key={group.id} value={group.id}>{t(group.label)}</option>)}</select></label>
      <label>{t('Section')}<select className="fs-field" value={section} onChange={event => onChange(event.target.value as K)}>{visible.map(item => <option key={item.key} value={item.key}>{t(item.label)}</option>)}</select></label>
    </div>}
    {!query.trim() && <div className="fs-set__groups">
      {SETTINGS_GROUPS.filter(group => sections.some(item => (group.sections as readonly string[]).includes(item.key))).map(group =>
        <button type="button" key={group.id} className="fs-set__nav-item" data-on={active.id === group.id || undefined} aria-current={active.id === group.id ? 'true' : undefined}
          onClick={() => { const first = sections.find(item => (group.sections as readonly string[]).includes(item.key)); if (first) onChange(first.key); }} data-testid={`settings-group-${group.id}`}>{t(group.label)}</button>)}
    </div>}
    <div className="fs-set__subnav" aria-label={query.trim() ? t('Search results') : t(active.label)}>
      {visible.map(item => <button type="button" key={item.key} className="fs-set__nav-item" data-on={section === item.key || undefined} aria-current={section === item.key ? 'page' : undefined}
        onClick={() => onChange(item.key)} data-testid={`settings-section-${item.key}`}>{t(item.label)}</button>)}
      {visible.length === 0 && <p className="fs-prose" role="status">{t('Nothing matches.')}</p>}
    </div>
    {active.id === 'connections' && !query.trim() && <ContextualTools context="connections" />}
  </nav>;
}
