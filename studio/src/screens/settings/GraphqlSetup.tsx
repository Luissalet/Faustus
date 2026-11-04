import { useState } from 'react';
import { Button } from '../../components';
import { t } from '../../i18n';
import { Field, Text, Toggle } from './fields';

export interface GraphqlPreview {
  tools: { name: string; description: string; readonly: boolean; params: { name: string; type: string; required: boolean }[] }[];
  notes: string[];
  fields: { name: string; command: string; args: string; env: string };
}

/** Reads a local GraphQL endpoint by introspection and shows the tools the assistant would get (one per
 * first-level query, mutations only when asked), then fills the MCP server form to register it through the
 * REST bridge. Nothing is saved until the form's own Add button. */
export function GraphqlSetup({ onFill }: { onFill: (fields: GraphqlPreview['fields']) => void }) {
  const [baseUrl, setBaseUrl] = useState('http://127.0.0.1:8000');
  const [path, setPath] = useState('/graphql');
  const [mutations, setMutations] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [preview, setPreview] = useState<GraphqlPreview | null>(null);

  const run = async () => {
    setBusy(true);
    setError('');
    try {
      const r = await fetch('/api/mcp/graphql/preview', {
        method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ base_url: baseUrl, path, mutations }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(typeof body.detail === 'string' ? body.detail : `HTTP ${r.status}`);
      const data = body as GraphqlPreview;
      setPreview(data);
      onFill(data.fields);
    } catch (e) {
      setPreview(null);
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fs-set__field" data-testid="graphql-setup">
      <div className="fs-set__grid2">
        <Field label={t('GraphQL server')} htmlFor="gql-base">
          <Text id="gql-base" value={baseUrl} onChange={setBaseUrl} placeholder="http://127.0.0.1:8000" />
        </Field>
        <Field label={t('GraphQL path')} htmlFor="gql-path">
          <Text id="gql-path" value={path} onChange={setPath} placeholder="/graphql" />
        </Field>
      </div>
      <div className="fs-set__inline">
        <Toggle id="gql-mut" checked={mutations} onChange={setMutations} label={t('Also expose mutations (they can change data)')} />
        <Button variant="secondary" size="sm" label={t('Read the schema')} loading={busy} onClick={() => void run()} />
      </div>
      {error && <p className="fs-set__help" role="alert" data-testid="graphql-error">{error}</p>}
      {preview && (
        <div data-testid="graphql-tools">
          <p className="fs-set__help">{t('{n} tools from this schema. The form below is filled in; press Add to register it.', { n: preview.tools.length })}</p>
          <ul>
            {preview.tools.map((tool) => (
              <li key={tool.name}><code>{tool.name}</code>{tool.readonly ? '' : ` (${t('changes data')})`}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
