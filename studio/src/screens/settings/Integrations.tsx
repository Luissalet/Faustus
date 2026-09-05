import { Plus, RefreshCw, Trash2 } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { Button, IconButton, Skeleton } from '../../components';
import {
  agentKindOf,
  clearContacts,
  contactsConfig,
  deleteApiIntegration,
  deleteCalDav,
  deleteEmailAccount,
  deleteMcpServer,
  deleteToken,
  KIND_LABEL,
  listApiIntegrations,
  listCalDav,
  listContacts,
  listEmailAccounts,
  listMcpServers,
  listTokens,
  saveCardDav,
  vaultConfig,
  vaultLogout,
  type ApiIntegration,
  type CalDavAccount,
  type EmailAccount,
  type IntegrationKind,
  type McpServer,
  type ApiToken,
  type VaultConfig,
} from '../../adapters/integrations';
import { t, tn } from '../../i18n';
import { ApiForm, CalDavForm, VaultForm, AgentForm } from './IntegrationForms';
import { ContactsPanel, EmailForm, McpPanel } from './IntegrationsMore';

/**
 * Integrations: every external connection in one list, the way the previous
 * interface's unified tab had it. Each kind keeps its own form and its own
 * routes; the list is the only thing they share.
 */

export interface Item {
  kind: IntegrationKind;
  id: string;
  name: string;
  detail: string;
  enabled: boolean;
  data: unknown;
}

export type Editing = { kind: IntegrationKind; id: string | null } | null;

async function fetchAll(): Promise<Item[]> {
  const safe = <T,>(p: Promise<T>, fallback: T) => p.catch(() => fallback);
  const [api, cal, cardCfg, contacts, mail, mcp, vault, tokens] = await Promise.all([
    safe(listApiIntegrations(), [] as ApiIntegration[]),
    safe(listCalDav(), [] as CalDavAccount[]),
    safe(contactsConfig(), {}),
    safe(listContacts(), { contacts: [], count: 0 }),
    safe(listEmailAccounts(), [] as EmailAccount[]),
    safe(listMcpServers(), [] as McpServer[]),
    safe(vaultConfig(), null as VaultConfig | null),
    safe(listTokens(), [] as ApiToken[]),
  ]);
  const items: Item[] = [];
  for (const i of api) items.push({ kind: 'api', id: i.id, name: i.name || t('Unnamed'), detail: i.base_url ?? '', enabled: i.enabled !== false, data: i });
  for (const a of cal) items.push({ kind: 'caldav', id: a.id, name: a.label || t('Calendar (CalDAV)'), detail: a.url, enabled: true, data: a });
  if (contacts.count > 0) items.push({ kind: 'contacts', id: '__contacts__', name: t('Contacts'), detail: tn(contacts.count, '{n} contact', '{n} contacts'), enabled: true, data: contacts });
  const cardUrl = cardCfg.url ?? cardCfg.carddav_url;
  if (cardUrl) items.push({ kind: 'carddav', id: '__carddav__', name: t('Contacts (CardDAV)'), detail: cardUrl, enabled: true, data: cardCfg });
  for (const a of mail) items.push({ kind: 'email', id: a.id, name: a.name + (a.is_default ? ` (${t('default')})` : ''), detail: [a.from_address || a.imap_user, a.imap_host].filter(Boolean).join(' — '), enabled: a.enabled !== false, data: a });
  for (const s of mcp) {
    const detail = s.needs_oauth ? t('needs authorisation') : s.status === 'connected' ? t('{a}/{b} tools', { a: s.enabled_tool_count, b: s.tool_count }) : s.status === 'error' ? t('error') : t('disconnected');
    items.push({ kind: 'mcp', id: s.id, name: s.name || 'MCP', detail, enabled: s.is_enabled !== false, data: s });
  }
  for (const tok of tokens) {
    const kind = agentKindOf(tok);
    if (!kind) continue;
    items.push({ kind, id: tok.id, name: tok.name || KIND_LABEL[kind], detail: `${tok.token_prefix ?? 'token'}… · ${(tok.scopes ?? []).join(', ') || 'chat'}`, enabled: true, data: tok });
  }
  if (vault && (vault.server_url || vault.email || vault.logged_in || vault.unlocked)) items.push({ kind: 'vault', id: '__vault__', name: t('Vault (Bitwarden)'), detail: `${vault.email ?? ''} ${vault.unlocked ? `· ${t('unlocked')}` : `· ${t('locked')}`}`, enabled: !!vault.unlocked, data: vault });
  return items;
}

const ADDABLE: IntegrationKind[] = ['api', 'email', 'caldav', 'contacts', 'mcp', 'codex', 'claude', 'vault'];

export function IntegrationsSection({ say }: { say: (t: string) => void }) {
  const [items, setItems] = useState<Item[] | null>(null);
  const [editing, setEditing] = useState<Editing>(null);
  const [adding, setAdding] = useState(false);

  const reload = useCallback(() => fetchAll().then(setItems), []);
  useEffect(() => {
    void reload();
  }, [reload]);

  const remove = async (item: Item) => {
    if (!window.confirm(t('Remove "{name}"?', { name: item.name }))) return;
    try {
      if (item.kind === 'api') await deleteApiIntegration(item.id);
      else if (item.kind === 'caldav') await deleteCalDav(item.id);
      else if (item.kind === 'contacts') await clearContacts();
      else if (item.kind === 'carddav') await saveCardDav({ carddav_url: '', carddav_username: '', carddav_password: '' });
      else if (item.kind === 'email') await deleteEmailAccount(item.id);
      else if (item.kind === 'mcp') await deleteMcpServer(item.id);
      else if (item.kind === 'codex' || item.kind === 'claude') await deleteToken(item.id);
      else if (item.kind === 'vault') await vaultLogout();
      if (editing?.id === item.id) setEditing(null);
      say(t('Removed.'));
    } catch (e) {
      say((e as Error).message);
    }
    void reload();
  };

  const close = () => {
    setEditing(null);
    setAdding(false);
    void reload();
  };

  return (
    <section className="fs-set__section" aria-labelledby="fs-set-intg">
      <header className="fs-set__section-head">
        <div>
          <h2 id="fs-set-intg" className="fs-set__title">{t('Integrations')}</h2>
          <p className="fs-prose">{t('Every external connection in one place: API keys, mail accounts, CalDAV, contacts, MCP servers, the agent tokens and the vault.')}</p>
        </div>
        <div className="fs-set__row-actions">
          <IconButton icon={RefreshCw} label={t('Refresh')} size="sm" onClick={() => void reload()} />
          <Button size="sm" variant="primary" icon={Plus} label={t('Add')} onClick={() => setAdding((a) => !a)} testId="intg-add" />
        </div>
      </header>

      {adding && (
        <div className="fs-intg__kinds" role="group" aria-label={t('What to add')}>
          {ADDABLE.map((k) => (
            <button
              key={k}
              type="button"
              className="fs-chip"
              onClick={() => {
                setAdding(false);
                setEditing({ kind: k, id: null });
              }}
            >
              {t(KIND_LABEL[k])}
            </button>
          ))}
        </div>
      )}

      {editing && (
        <div className="fs-set__card fs-intg__form" data-testid="intg-form">
          <Form editing={editing} items={items ?? []} onClose={close} onChanged={() => void reload()} say={say} />
        </div>
      )}

      {items === null ? (
        <Skeleton label={t('Loading')} count={3} height="52px" />
      ) : items.length === 0 ? (
        <p className="fs-set__help">{t('Nothing connected yet. Add one above.')}</p>
      ) : (
        <ul className="fs-intg">
          {items.map((item) => (
            <li key={`${item.kind}-${item.id}`} className="fs-intg__row" data-testid={`intg-${item.kind}`}>
              <button type="button" className="fs-intg__main" onClick={() => setEditing({ kind: item.kind, id: item.id })} title={t('Open')}>
                <span className="fs-intg__kind">{t(KIND_LABEL[item.kind])}</span>
                <span className="fs-intg__text">
                  <strong>{item.name}</strong>
                  <span className="fs-set__help">{item.detail}</span>
                </span>
                <span className="fs-intg__dot" data-on={item.enabled || undefined} aria-label={item.enabled ? t('Enabled') : t('Disabled')} />
              </button>
              <IconButton icon={Trash2} label={t('Remove {name}', { name: item.name })} size="sm" onClick={() => void remove(item)} />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function Form({ editing, items, onClose, onChanged, say }: { editing: NonNullable<Editing>; items: Item[]; onClose: () => void; onChanged: () => void; say: (t: string) => void }) {
  const current = items.find((i) => i.kind === editing.kind && i.id === editing.id) ?? null;
  switch (editing.kind) {
    case 'api':
      return <ApiForm existing={current?.data as ApiIntegration | undefined} onClose={onClose} onChanged={onChanged} say={say} />;
    case 'caldav':
      return <CalDavForm existing={current?.data as CalDavAccount | undefined} onClose={onClose} onChanged={onChanged} say={say} />;
    case 'contacts':
    case 'carddav':
      return <ContactsPanel onClose={onClose} onChanged={onChanged} say={say} />;
    case 'email':
      return <EmailForm existing={current?.data as EmailAccount | undefined} onClose={onClose} onChanged={onChanged} say={say} />;
    case 'mcp':
      return <McpPanel existing={current?.data as McpServer | undefined} onClose={onClose} onChanged={onChanged} say={say} />;
    case 'codex':
    case 'claude':
      return <AgentForm kind={editing.kind} existing={current?.data as ApiToken | undefined} onClose={onClose} onChanged={onChanged} say={say} />;
    case 'vault':
      return <VaultForm onClose={onClose} onChanged={onChanged} say={say} />;
  }
}
