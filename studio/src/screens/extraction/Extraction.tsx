import { FileJson2, Play, Plus, Trash2 } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Button, EmptyState, Skeleton, Toast } from '../../components';
import * as api from '../../adapters/extraction';
import { t } from '../../i18n';
import './extraction.css';

/**
 * Extracción: run a user JSON Schema over a document or pasted text.
 *
 * Dropped values are never drawn in the data table. Limits are a warning strip,
 * not a claim that a value belongs to its field. Reasoning lives in
 * ``adapters/extraction.ts`` (checked by ``studio/checks/extraction.check.mjs``).
 */

function valueText(value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

export function ExtractionScreen() {
  const [schemas, setSchemas] = useState<api.SavedSchema[]>([]);
  const [name, setName] = useState('');
  const [schemaText, setSchemaText] = useState('{\n  "type": "object",\n  "properties": {}\n}');
  const [profile, setProfile] = useState<api.SchemaProfile | null>(null);
  const [sourceText, setSourceText] = useState('');
  const [sourcePath, setSourcePath] = useState('');
  const [result, setResult] = useState<api.ExtractResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [loadingList, setLoadingList] = useState(true);

  const refresh = useCallback(async () => {
    setLoadingList(true);
    try {
      setSchemas(await api.listSchemas());
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setLoadingList(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const parsed = useMemo(() => api.parseSchemaText(schemaText), [schemaText]);

  const onProfile = async () => {
    if (!parsed.ok) {
      setError(parsed.error);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      setProfile(await api.fetchProfile(parsed.schema));
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  };

  const onSave = async () => {
    if (!parsed.ok) {
      setError(parsed.error);
      return;
    }
    if (!api.schemaNameOk(name)) {
      setError(t('Schema name must match A-Za-z0-9_- (max 64)'));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.saveSchema(name, parsed.schema);
      setToast(t('Schema saved'));
      await refresh();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  };

  const onDelete = async (target: string) => {
    setBusy(true);
    setError(null);
    try {
      await api.deleteSchema(target);
      if (name === target) setName('');
      await refresh();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  };

  const onLoad = async (saved: api.SavedSchema) => {
    setBusy(true);
    setError(null);
    setResult(null);
    setProfile(null);
    setName(saved.name);
    try {
      const full = await api.getSchema(saved.name);
      setSchemaText(JSON.stringify(full.schema ?? {}, null, 2));
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  };

  const onRun = async () => {
    if (!parsed.ok) {
      setError(parsed.error);
      return;
    }
    if (!sourceText.trim() && !sourcePath.trim()) {
      setError(t('Paste text or give a workspace path'));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const body: Record<string, unknown> = { schema: parsed.schema };
      if (sourcePath.trim()) body.path = sourcePath.trim();
      else body.text = sourceText;
      setResult(await api.runExtract(body));
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  };

  const dataRows = result ? api.displayDataRows(result) : [];

  return (
    <div className="fs-extract" data-screen="extraction">
      <header className="fs-extract__head">
        <div>
          <h1>{t('Extraction')}</h1>
          <p>{t('Fill a JSON Schema from a document. Dropped values stay dropped.')}</p>
        </div>
        <Button label={t('Extract')} onClick={() => void onRun()} disabled={busy} icon={Play} />
      </header>

      {error ? <div className="fs-extract__error" role="alert">{error}</div> : null}

      <div className="fs-extract__grid">
        <section className="fs-extract__panel" aria-labelledby="extract-schemas">
          <h2 id="extract-schemas">{t('Saved schemas')}</h2>
          {loadingList ? <Skeleton label={t('Loading schemas')} count={3} height="32px" /> : null}
          {!loadingList && schemas.length === 0 ? (
            <EmptyState icon={FileJson2} title={t('No saved schemas yet')} body={t('Save a schema to reuse it.')} />
          ) : null}
          <ul className="fs-extract__list">
            {schemas.map((s) => (
              <li key={s.name}>
                <button type="button" className="fs-extract__link" onClick={() => void onLoad(s)}>
                  {s.name}
                </button>
                <button
                  type="button"
                  className="fs-extract__icon"
                  aria-label={t('Delete schema')}
                  onClick={() => void onDelete(s.name)}
                >
                  <Trash2 size={14} />
                </button>
              </li>
            ))}
          </ul>
          <label className="fs-extract__label">
            {t('Name')}
            <input value={name} onChange={(e) => setName(e.target.value)} spellCheck={false} />
          </label>
          <div className="fs-extract__row">
            <Button variant="secondary" label={t('Save schema')} onClick={() => void onSave()} disabled={busy} icon={Plus} />
            <Button variant="secondary" label={t('Profile')} onClick={() => void onProfile()} disabled={busy} />
          </div>
          {profile ? (
            <p className="fs-extract__profile">
              {profile.summary || `${profile.complexity} · ${profile.route}`}
            </p>
          ) : null}
          <label className="fs-extract__label">
            {t('Schema JSON')}
            <textarea
              value={schemaText}
              onChange={(e) => setSchemaText(e.target.value)}
              spellCheck={false}
              rows={14}
              aria-invalid={parsed.ok ? undefined : true}
            />
          </label>
          {!parsed.ok ? <p className="fs-extract__hint">{parsed.error}</p> : null}
        </section>

        <section className="fs-extract__panel" aria-labelledby="extract-source">
          <h2 id="extract-source">{t('Source')}</h2>
          <label className="fs-extract__label">
            {t('Workspace path')}
            <input
              value={sourcePath}
              onChange={(e) => setSourcePath(e.target.value)}
              placeholder="docs/invoice.pdf"
              spellCheck={false}
            />
          </label>
          <label className="fs-extract__label">
            {t('Or paste text')}
            <textarea
              value={sourceText}
              onChange={(e) => setSourceText(e.target.value)}
              rows={16}
            />
          </label>
        </section>

        <section className="fs-extract__panel fs-extract__panel--wide" aria-labelledby="extract-result">
          <h2 id="extract-result">{t('Result')}</h2>
          {!result ? (
            <EmptyState
              icon={FileJson2}
              title={t('Run an extraction to see data and evidence')}
              body={t('Valid fields appear with quotes; dropped values stay in their own list.')}
            />
          ) : (
            <>
              <p className="fs-extract__meta">
                {result.route ? `${t('Route')}: ${result.route}` : null}
                {result.schemaValid ? ` · ${t('schema valid')}` : ` · ${t('schema invalid')}`}
              </p>
              {result.limits ? (
                <div className="fs-extract__limits" role="note">
                  <strong>{result.limits.message}</strong>
                  {result.limits.items.length ? (
                    <ul>
                      {result.limits.items.map((item) => (
                        <li key={`${item.code}:${item.note}`}>
                          {item.note || item.code}
                          {item.code ? <span className="fs-extract__badge">{item.code}</span> : null}
                        </li>
                      ))}
                    </ul>
                  ) : null}
                  {result.limits.detail && !result.limits.items.length ? <span>{result.limits.detail}</span> : null}
                  <em>{t('Lexical check only — does not prove the value belongs to the field.')}</em>
                </div>
              ) : null}

              {result.errors.length ? (
                <>
                  <h3>{t('Errors')}</h3>
                  <ul className="fs-extract__errors">{result.errors.map((err) => <li key={err}>{err}</li>)}</ul>
                </>
              ) : null}

              {result.conflicts.length ? (
                <>
                  <h3>{t('Conflicts')}</h3>
                  <ul>
                    {result.conflicts.map((c, i) => (
                      <li key={i}><code>{typeof c === 'string' ? c : JSON.stringify(c)}</code></li>
                    ))}
                  </ul>
                </>
              ) : null}

              <h3>{t('Data')}</h3>
              {dataRows.length === 0 ? <p className="fs-extract__hint">{t('No valid fields')}</p> : (
                <table className="fs-extract__table">
                  <thead>
                    <tr>
                      <th scope="col">{t('Path')}</th>
                      <th scope="col">{t('Value')}</th>
                      <th scope="col">{t('Evidence')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {dataRows.map((row) => (
                      <tr key={row.path} data-inferred={row.inferred ? 'yes' : undefined}>
                        <td>
                          <code>{row.path}</code>
                          {row.inferred ? <span className="fs-extract__badge">{t('inferred')}</span> : null}
                        </td>
                        <td>{valueText(row.value)}</td>
                        <td>
                          {row.evidence ? (
                            <>
                              <q>{row.evidence.quote}</q>
                              {row.evidence.unit ? <span className="fs-extract__unit">{row.evidence.unit}</span> : null}
                            </>
                          ) : '—'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}

              <h3>{t('Dropped')}</h3>
              {result.dropped.length === 0 ? <p className="fs-extract__hint">{t('Nothing dropped')}</p> : (
                <table className="fs-extract__table fs-extract__table--dropped">
                  <thead>
                    <tr>
                      <th scope="col">{t('Path')}</th>
                      <th scope="col">{t('Value')}</th>
                      <th scope="col">{t('Why')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.dropped.map((row) => (
                      <tr key={`${row.path}:${row.code}`}>
                        <td><code>{row.path}</code></td>
                        <td>{valueText(row.value)}</td>
                        <td>
                          {row.why || row.code}
                          {row.code ? <span className="fs-extract__badge">{row.code}</span> : null}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}

              {result.missingRequired.length ? (
                <>
                  <h3>{t('Missing required')}</h3>
                  <ul>{result.missingRequired.map((p) => <li key={p}><code>{p}</code></li>)}</ul>
                </>
              ) : null}
            </>
          )}
        </section>
      </div>

      {toast ? <Toast>{toast}</Toast> : null}
    </div>
  );
}

export default ExtractionScreen;
