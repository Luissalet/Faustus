import { Download as DownloadIcon, ExternalLink, Search } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { Button, Skeleton } from '../../components';
import { hfGgufFiles, hfLatest, ollamaLibrary, useCookbookState, type HfLatestModel, type OllamaLibraryModel, type Server } from '../../adapters/cookbook';
import { ggufQuant } from '../../lib/cookbook/serve';
import { t, tn } from '../../i18n';
import { startDownload, targetFor } from './actions';
import { Field } from './parts';

/**
 * Download: a repo id, an Ollama tag or a Hugging Face URL; a GGUF repo
 * offers its quant files; below, what is new on Hugging Face and the
 * Ollama library, each one click from a download.
 */

function parseInput(raw: string): { repo: string; kind: 'hf' | 'ollama'; gguf: boolean } {
  let s = raw.trim();
  const m = s.match(/huggingface\.co\/([^/\s]+\/[^/\s?#]+)/i);
  if (m) s = m[1];
  s = s.replace(/^hf:\/\//, '').replace(/\/+$/, '');
  const kind = !s.includes('/') && (s.includes(':') || /^[a-z0-9.-]+$/i.test(s)) ? 'ollama' : 'hf';
  return { repo: s, kind, gguf: kind === 'hf' && /gguf/i.test(s) };
}

export function Download({ server, say, prefill, onStarted }: { server: Server | null; say: (m: string) => void; prefill?: string | null; onStarted: () => void }) {
  const state = useCookbookState();
  const [raw, setRaw] = useState(prefill ?? '');
  const [files, setFiles] = useState<string[] | null>(null);
  const [quant, setQuant] = useState('');
  const [busy, setBusy] = useState(false);
  const [latest, setLatest] = useState<HfLatestModel[] | null>(null);
  const [library, setLibrary] = useState<OllamaLibraryModel[] | null>(null);
  const [libQuery, setLibQuery] = useState('');
  const parsed = useMemo(() => parseInput(raw), [raw]);

  useEffect(() => {
    if (prefill) setRaw(prefill);
  }, [prefill]);

  useEffect(() => {
    if (!parsed.gguf || !parsed.repo.includes('/')) {
      setFiles(null);
      setQuant('');
      return;
    }
    const ac = new AbortController();
    const timer = window.setTimeout(() => {
      hfGgufFiles(parsed.repo, ac.signal)
        .then((list) => {
          if (ac.signal.aborted) return;
          const usable = list.filter((f) => /\.gguf$/i.test(f) && !/mmproj/i.test(f) && !/-(?!00001)\d{5}-of-\d{5}\.gguf$/i.test(f));
          setFiles(usable);
          const preferred = usable.find((f) => /Q4_K_M/i.test(f)) ?? usable[0] ?? '';
          setQuant(preferred);
        })
        .catch(() => setFiles([]));
    }, 400);
    return () => {
      ac.abort();
      window.clearTimeout(timer);
    };
  }, [parsed.repo, parsed.gguf]);

  useEffect(() => {
    const ac = new AbortController();
    hfLatest(24, ac.signal).then(setLatest).catch(() => setLatest([]));
    ollamaLibrary(ac.signal).then(setLibrary).catch(() => setLibrary([]));
    return () => ac.abort();
  }, []);

  const go = async (repo: string, kind: 'hf' | 'ollama', include?: string, display?: string) => {
    if (!repo) return;
    setBusy(true);
    try {
      const target = targetFor(state.env, server);
      const out = await startDownload({ repo, backend: kind === 'ollama' ? 'ollama' : include ? 'llamacpp' : 'hf', include, target, displayName: display });
      if ('duplicate' in out) say(t('{name} is already {state}', { name: repo.split('/').pop() || repo, state: out.duplicate.status === 'queued' ? t('queued') : t('downloading') }));
      else {
        say(out.queued ? t('Queued {name} — waiting for the current download', { name: out.task.name }) : t('Downloading {name}…', { name: out.task.name }));
        onStarted();
      }
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const libraryVisible = useMemo(() => {
    const q = libQuery.trim().toLowerCase();
    return (library ?? []).filter((m) => !q || m.name.includes(q) || m.description.toLowerCase().includes(q)).slice(0, 60);
  }, [library, libQuery]);

  return (
    <div className="fs-ck__download" data-testid="cookbook-download">
      <section className="fs-ck__panel">
        <form
          className="fs-ck__dl-form"
          onSubmit={(e) => {
            e.preventDefault();
            void go(parsed.repo, parsed.kind, files && quant ? quant : undefined);
          }}
        >
          <Field label={t('Repo, tag or URL')} wide>
            <input className="fs-field" value={raw} onChange={(e) => setRaw(e.target.value)} placeholder="org/model-name, qwen2.5:14b, or https://huggingface.co/…" data-testid="download-input" />
          </Field>
          {parsed.gguf && (
            <Field label={t('GGUF quant')} wide>
              {files === null ? (
                <Skeleton label={t('Listing the repo')} height="32px" />
              ) : files.length ? (
                <select className="fs-field" value={quant} onChange={(e) => setQuant(e.target.value)} data-testid="download-quant">
                  {files.map((f) => (
                    <option key={f} value={f}>
                      {ggufQuant(f)} · {f}
                    </option>
                  ))}
                </select>
              ) : (
                <span className="fs-muted">{t('No GGUF files listed for this repo; the whole repo is downloaded.')}</span>
              )}
            </Field>
          )}
          <div className="fs-inline">
            <Button type="submit" variant="primary" icon={DownloadIcon} label={parsed.kind === 'ollama' ? t('Pull with Ollama') : t('Download')} disabled={!parsed.repo} loading={busy} testId="download-go" />
            <span className="fs-muted">
              {parsed.kind === 'ollama' ? t('Ollama tag on {where}', { where: server && server.host ? server.host : t('this machine') }) : t('Hugging Face on {where}{dir}', { where: server && server.host ? server.host : t('this machine'), dir: server?.downloadDir ? ` → ${server.downloadDir}` : '' })}
              {!state.env.hfTokenConfigured && parsed.kind === 'hf' ? ` · ${t('no HF token set (gated repos need one, in Servers)')}` : ''}
            </span>
          </div>
        </form>
      </section>

      <div className="fs-ck__two">
        <section className="fs-ck__panel">
          <h3 className="fs-ck__h">
            {t('New on Hugging Face')} <span className="fs-muted">{t('text generation, by downloads')}</span>
          </h3>
          {!latest && <Skeleton label={t('Loading')} count={4} height="36px" />}
          {latest && !latest.length && <p className="fs-muted">{t('Hugging Face did not answer.')}</p>}
          <ul className="fs-ck__catalog">
            {(latest ?? []).map((m) => (
              <li key={m.repo_id}>
                <div className="fs-ck__catalog-main">
                  <span className="fs-ck__catalog-name">{m.repo_id}</span>
                  <span className="fs-muted">
                    {tn(m.downloads, '{n} download', '{n} downloads')} · {m.likes} ♥{m.createdAt ? ` · ${m.createdAt.slice(0, 10)}` : ''}
                  </span>
                </div>
                <a className="fs-ck__catalog-link" href={`https://huggingface.co/${m.repo_id}`} target="_blank" rel="noopener noreferrer" aria-label={t('Open on Hugging Face')}>
                  <ExternalLink size={13} aria-hidden="true" />
                </a>
                <Button size="sm" variant="ghost" icon={DownloadIcon} label={t('Get')} onClick={() => setRaw(m.repo_id)} />
              </li>
            ))}
          </ul>
        </section>
        <section className="fs-ck__panel">
          <h3 className="fs-ck__h">{t('Ollama library')}</h3>
          <label className="fs-search">
            <Search size={14} aria-hidden="true" />
            <input type="search" value={libQuery} onChange={(e) => setLibQuery(e.target.value)} placeholder={t('Filter the library')} aria-label={t('Filter the library')} />
          </label>
          {!library && <Skeleton label={t('Loading')} count={4} height="36px" />}
          {library && !library.length && <p className="fs-muted">{t('The library did not answer.')}</p>}
          <ul className="fs-ck__catalog">
            {libraryVisible.map((m) => (
              <li key={m.name}>
                <div className="fs-ck__catalog-main">
                  <span className="fs-ck__catalog-name">{m.name}</span>
                  <span className="fs-muted">{m.description}</span>
                  {m.sizes.length > 0 && (
                    <span className="fs-ck__sizes">
                      {m.sizes.slice(0, 8).map((sz) => (
                        <button key={sz} type="button" className="fs-chip" onClick={() => void go(`${m.name}:${sz}`, 'ollama')}>
                          {sz}
                        </button>
                      ))}
                    </span>
                  )}
                </div>
                <Button size="sm" variant="ghost" icon={DownloadIcon} label={t('Pull')} onClick={() => void go(m.name, 'ollama')} />
              </li>
            ))}
          </ul>
        </section>
      </div>
    </div>
  );
}
