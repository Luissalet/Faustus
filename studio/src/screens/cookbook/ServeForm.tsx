import { CalendarClock, Play, Save, Sparkles } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { Button, Skeleton } from '../../components';
import { listGpus, serveCtx, serveProfiles, updateState, useCookbookState, type CachedModel, type Gpu, type Preset, type Server, type ServeProfile } from '../../adapters/cookbook';
import { BACKEND_LABEL, backendChoices, remoteWindowsDiffusers, buildServeCmd, DEFAULT_PORT, detectBackend, detectModelOptimizations, detectReasoningParser, detectToolParser, ggufFileExpr, ggufFindExpr, ggufQuant, nextFreePort, portOf, projectorGguf, runnableGguf, bytesLabel, type Backend, type ServeFields } from '../../lib/cookbook/serve';
import { t, tn } from '../../i18n';
import { launchServe, targetFor } from './actions';
import { CopyButton, Field, Switch } from './parts';

/**
 * The launch form for one model: engine, the knobs that engine takes,
 * the exact command it produces (editable before launching), presets for
 * this model, and the hardware profiles for GGUF. Ported field by field
 * from cookbookServe.js, laid out in groups instead of one long row.
 */

export interface ServeFormProps {
  model: CachedModel;
  server: Server | null;
  hwBackend: string;
  initial?: ServeFields;
  replaceTaskId?: string;
  focus?: string;
  say: (m: string) => void;
  onLaunched: () => void;
  onSchedule: (repo: string) => void;
}

const DTYPES = ['auto', 'bfloat16', 'float16', 'float32'];
const CTX_PRESETS = ['4096', '8192', '16384', '32768', '65536', '131072'];

export function ServeForm({ model, server, hwBackend, initial, replaceTaskId, focus, say, onLaunched, onSchedule }: ServeFormProps) {
  const state = useCookbookState();
  const repo = model.repo_id;
  const ctx = useMemo(() => serveCtx(state.env, hwBackend, server), [state.env, hwBackend, server]);
  const image = model.is_diffusion || model.is_video;
  const choices = useMemo(() => backendChoices(ctx, image), [ctx, image]);
  const detected = useMemo(() => detectBackend({ ...model, is_image_gen: image }, ctx).backend, [model, ctx, image]);
  const saved = state.serveState?._byRepo[repo];
  const [f, setF] = useState<ServeFields>(() => ({ backend: choices.includes(detected) ? detected : choices[0], port: '', ctx: '8192', tp: '1', gpu_mem: '0.90', dtype: 'auto', llama_mode: 'gpu', ngl: '99', ...(saved ?? {}), ...(initial ?? {}) }));
  const backend = (f.backend as Backend) || detected;
  const [gpus, setGpus] = useState<Gpu[] | null>(null);
  const [profiles, setProfiles] = useState<ServeProfile[] | null>(null);
  const [cmdOverride, setCmdOverride] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [presetName, setPresetName] = useState('');
  const set = (k: string, v: string | boolean) => {
    setCmdOverride(null);
    setF((cur) => ({ ...cur, [k]: v }));
  };

  useEffect(() => {
    const ac = new AbortController();
    void listGpus(server, ac.signal).then((r) => setGpus(r.gpus));
    return () => ac.abort();
  }, [server]);

  useEffect(() => {
    if (backend !== 'llamacpp') return;
    const ac = new AbortController();
    setProfiles(null);
    void serveProfiles(repo, server, { modelPath: model.path, weightsGb: model.size_bytes ? model.size_bytes / 1073741824 : undefined }, ac.signal)
      .then((r) => setProfiles(r.profiles))
      .catch(() => setProfiles([]));
    return () => ac.abort();
  }, [backend, repo, server, model.path, model.size_bytes]);

  useEffect(() => {
    if (!focus) return;
    const el = document.querySelector<HTMLElement>(`[data-field="${focus}"]`);
    el?.focus();
  }, [focus]);

  const opts = useMemo(() => detectModelOptimizations(repo), [repo]);
  const ggufs = useMemo(() => runnableGguf(model.gguf_files), [model.gguf_files]);
  const projectors = useMemo(() => projectorGguf(model.gguf_files), [model.gguf_files]);
  const usedPorts = state.tasks.filter((x) => x.type === 'serve' && (x.status === 'running' || x.status === 'ready') && (x.remoteHost || '') === (server && server.host ? server.host : '')).map((x) => portOf(x.payload?._cmd || '')).filter(Boolean);
  const port = String(f.port || '') || nextFreePort(usedPorts, Number(DEFAULT_PORT[backend]));

  const built = useMemo(() => {
    const fields: ServeFields = { ...f, port };
    if (backend === 'llamacpp') {
      const chosen = ggufs.find((g) => g.rel_path === f.gguf_file) ?? null;
      fields._gguf_path = chosen ? ggufFileExpr(model, repo, chosen.rel_path) : ggufFindExpr(model, repo);
      fields._mmproj_path = projectors[0] ? ggufFileExpr(model, repo, projectors[0].rel_path) : '';
    }
    if (f.reasoning_parser === true) fields.reasoning_parser = detectReasoningParser(repo) || '';
    const modelArg = String(f.model_path || '').trim() || (model.is_local_dir && model.path ? `${model.path}/${repo}` : repo);
    let cmd = buildServeCmd(fields, modelArg, backend, ctx);
    if (String(f.extra || '').trim()) cmd += ' ' + String(f.extra).trim();
    return cmd;
  }, [f, port, backend, ggufs, projectors, model, repo, ctx]);
  const cmd = cmdOverride ?? built;

  const launch = async () => {
    setBusy(true);
    try {
      const target = targetFor(state.env, server);
      updateState((s) => ({ ...s, serveState: { _byRepo: { ...(s.serveState?._byRepo ?? {}), [repo]: { ...f, port } } } }), { push: true });
      await launchServe({ shortName: repo.split('/').pop() || repo, repo, cmd, fields: { ...f, port, backend }, target, hwBackend, replaceTaskId });
      say(t('Serving {name}…', { name: repo.split('/').pop() || repo }));
      onLaunched();
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const savePreset = () => {
    const label = presetName.trim() || `${repo.split('/').pop()} · ${BACKEND_LABEL[backend]}`;
    const preset: Preset = { id: `p-${Date.now().toString(36)}`, label, repo, backend, fields: { ...f, port, backend }, env: ctx.env, envPath: ctx.envPath, host: ctx.remoteHost, ts: Date.now() };
    updateState((s) => ({ ...s, presets: [...s.presets.filter((p) => !(p.repo === repo && p.label === label)), preset] }));
    setPresetName('');
    say(t('Preset saved'));
  };
  const presets = state.presets.filter((p) => p.repo === repo);
  const applyPreset = (p: Preset) => {
    setCmdOverride(null);
    setF((cur) => ({ ...cur, ...p.fields, backend: p.backend || cur.backend }));
  };
  const applyProfile = (p: ServeProfile) => {
    setCmdOverride(null);
    setF((cur) => ({ ...cur, ngl: String(p.n_gpu_layers), n_cpu_moe: String(p.n_cpu_moe || ''), cache_type: p.cache_type || '', ctx: String(p.ctx), llama_mode: p.n_gpu_layers > 0 ? 'gpu' : 'cpu' }));
    const match = ggufs.find((g) => ggufQuant(g.rel_path) === p.quant);
    if (match) setF((cur) => ({ ...cur, gguf_file: match.rel_path }));
  };
  const toggleGpu = (idx: number) => {
    const cur = String(f.gpus || '')
      .split(',')
      .map((x) => x.trim())
      .filter(Boolean);
    const next = cur.includes(String(idx)) ? cur.filter((x) => x !== String(idx)) : [...cur, String(idx)].sort();
    set('gpus', next.join(','));
  };
  const selectedGpus = String(f.gpus || '')
    .split(',')
    .map((x) => x.trim())
    .filter(Boolean);

  const text = (k: string, extra: Record<string, string> = {}) => <input className="fs-field" value={String(f[k] ?? '')} onChange={(e) => set(k, e.target.value)} data-field={k} {...extra} />;
  const select = (k: string, options: [string, string][]) => (
    <select className="fs-field" value={String(f[k] ?? options[0]?.[0] ?? '')} onChange={(e) => set(k, e.target.value)} data-field={k}>
      {options.map(([v, l]) => (
        <option key={v} value={v}>
          {l}
        </option>
      ))}
    </select>
  );
  const sw = (k: string, label: string, hint?: string) => <Switch label={label} checked={Boolean(f[k])} onChange={(v) => set(k, v)} hint={hint} />;

  return (
    <div className="fs-ck__serve" data-testid="serve-form">
      <div className="fs-ck__serve-top">
        <div className="fs-seg" role="radiogroup" aria-label={t('Engine')}>
          {choices.map((b) => (
            <button key={b} type="button" role="radio" aria-checked={backend === b} onClick={() => set('backend', b)} title={b === detected ? t('Detected for this model') : undefined}>
              {BACKEND_LABEL[b]}
              {b === detected && <span className="fs-ck__detected" aria-label={t('detected')} />}
            </button>
          ))}
        </div>
        {image && remoteWindowsDiffusers(ctx) && (
          <p className="fs-ck__note">{t('Diffusers does not serve on a remote Windows machine yet, so only llama.cpp is offered for this target.')}</p>
        )}
        {presets.length > 0 && (
          <div className="fs-ck__presets" role="group" aria-label={t('Presets')}>
            {presets.map((p) => (
              <button key={p.id || p.label} type="button" className="fs-chip" onClick={() => applyPreset(p)} title={p.auto ? t('Saved automatically from a launch that worked') : undefined}>
                <Sparkles size={11} aria-hidden="true" /> {p.label}
              </button>
            ))}
          </div>
        )}
      </div>

      {gpus && gpus.length > 0 && backend !== 'ollama' && (
        <div className="fs-ck__gpus" role="group" aria-label={t('GPUs')}>
          <span className="fs-ck__label">{t('GPUs')}</span>
          {gpus.map((g) => (
            <button key={g.index} type="button" className="fs-chip" data-on={selectedGpus.includes(String(g.index)) || undefined} onClick={() => toggleGpu(g.index)} title={`${g.name} · ${Math.round(g.free_mb / 1024)} GB free of ${Math.round(g.total_mb / 1024)} GB${g.busy ? ' · busy' : ''}`}>
              #{g.index} {g.name.replace(/NVIDIA GeForce |AMD /g, '')} <span className="fs-gal__n">{Math.round(g.free_mb / 1024)} GB</span>
              {g.busy ? ' ⚠' : ''}
            </button>
          ))}
          <span className="fs-muted">{selectedGpus.length ? tn(selectedGpus.length, '{n} pinned', '{n} pinned#') : t('all')}</span>
        </div>
      )}

      <div className="fs-ck__grid">
        <Field label={t('Port')}>
          <input className="fs-field" value={String(f.port || '')} placeholder={port} onChange={(e) => set('port', e.target.value)} data-field="port" inputMode="numeric" />
        </Field>
        {backend !== 'diffusers' && backend !== 'mlx_image' && (
          <Field label={backend === 'mlx' ? t('Max tokens') : t('Context')} wide>
            <div className="fs-inline">
              <span className="fs-ck__ctx-input">{text('ctx', { inputMode: 'numeric' })}</span>
              <div className="fs-ck__ctx">
                {CTX_PRESETS.map((c) => (
                  <button key={c} type="button" className="fs-chip" data-on={String(f.ctx) === c || undefined} onClick={() => set('ctx', c)}>
                    {Number(c) >= 1024 ? `${Math.round(Number(c) / 1024)}k` : c}
                  </button>
                ))}
              </div>
            </div>
          </Field>
        )}

        {(backend === 'vllm' || backend === 'sglang') && (
          <>
            <Field label={t('Tensor parallel')}>{select('tp', [['1', '1'], ['2', '2'], ['4', '4'], ['8', '8']])}</Field>
            <Field label={backend === 'vllm' ? t('GPU memory') : t('Memory fraction')}>{text('gpu_mem', { placeholder: '0.90' })}</Field>
            <Field label={t('dtype')}>{select('dtype', DTYPES.map((d) => [d, d]))}</Field>
            <Field label={t('Max sequences')}>{text('max_seqs', { placeholder: 'auto', inputMode: 'numeric' })}</Field>
            <Field label={t('Served model name')}>{text('served_model_name', { placeholder: repo.split('/').pop() ?? repo })}</Field>
            <Field label={t('Env (KEY=VAL …)')} wide>
              {text('extra_env', { placeholder: 'VLLM_USE_V1=1' })}
            </Field>
            {backend === 'vllm' && (
              <>
                <Field label={t('Swap space (GB)')}>{text('swap', { placeholder: '0' })}</Field>
                <Field label={t('Attention backend')}>{select('vllm_attn_backend', [['', 'auto'], ['FLASH_ATTN', 'FLASH_ATTN'], ['FLASHINFER', 'FLASHINFER'], ['TRITON_ATTN', 'TRITON_ATTN'], ['XFORMERS', 'XFORMERS']])}</Field>
                <Field label={t('KV cache dtype')}>{select('vllm_kv_cache_dtype', [['', 'auto'], ['fp8', 'fp8']])}</Field>
                <Field label={t('Block size')}>{text('vllm_block_size', { placeholder: 'auto', inputMode: 'numeric' })}</Field>
                <Field label={t('LoRA modules')} wide>
                  {text('vllm_lora_modules', { placeholder: 'name=path, …' })}
                </Field>
              </>
            )}
            {backend === 'sglang' && <Field label={t('Decode CUDA graph')}>{select('sglang_decode_graph', [['', 'auto'], ['bs16', 'max batch 16'], ['disabled', 'disabled']])}</Field>}
            <div className="fs-ck__switches">
              {sw('trust_remote', t('Trust remote code'))}
              {sw('auto_tool', t('Auto tool choice'), `--tool-call-parser ${detectToolParser(repo)}`)}
              {sw('prefix_cache', t('Prefix caching'))}
              {sw('enforce_eager', backend === 'vllm' ? t('Enforce eager') : t('No CUDA graph'))}
              {sw('expert_parallel', t('Expert parallel'), opts.tips.join(' · ') || undefined)}
              {sw('reasoning_parser', t('Reasoning parser'), detectReasoningParser(repo) ? `--reasoning-parser ${detectReasoningParser(repo)}` : t('No parser known for this family'))}
              {backend === 'vllm' && sw('moe_env', t('MoE env vars'), opts.envVars.join(' ') || 'VLLM_USE_DEEP_GEMM=0 VLLM_USE_FLASHINFER_MOE_FP16=1')}
              {backend === 'vllm' && sw('speculative', t('Speculative decoding'), opts.spec ? `${opts.spec.method} × ${opts.spec.tokens}` : undefined)}
              {backend === 'vllm' && sw('language_model_only', t('Language model only'))}
              {backend === 'vllm' && sw('disable_custom_all_reduce', t('Disable custom all-reduce'))}
            </div>
            {backend === 'vllm' && f.speculative && (
              <>
                <Field label={t('Speculative method')}>{text('spec_method', { placeholder: opts.spec?.method || 'mtp' })}</Field>
                <Field label={t('Speculative tokens')}>{text('spec_tokens', { placeholder: String(opts.spec?.tokens || 3), inputMode: 'numeric' })}</Field>
              </>
            )}
          </>
        )}

        {backend === 'llamacpp' && (
          <>
            {ggufs.length > 0 && (
              <Field label={t('GGUF file')} wide>
                <select className="fs-field" value={String(f.gguf_file || '')} onChange={(e) => set('gguf_file', e.target.value)} data-field="gguf_file">
                  <option value="">{t('First GGUF found')}</option>
                  {ggufs.map((g) => (
                    <option key={g.rel_path} value={g.rel_path}>
                      {ggufQuant(g.rel_path)} · {g.rel_path.split('/').pop()}
                      {g.size_bytes ? ` · ${bytesLabel(g.size_bytes)}` : ''}
                    </option>
                  ))}
                </select>
              </Field>
            )}
            <Field label={t('Inference')}>
              <div className="fs-seg" role="radiogroup" aria-label={t('Inference')}>
                {[
                  ['gpu', t('GPU')],
                  ['cpu', t('CPU')],
                  ['unified', t('Unified')],
                ].map(([v, l]) => (
                  <button key={v} type="button" role="radio" aria-checked={String(f.llama_mode || 'gpu') === v} onClick={() => set('llama_mode', v)}>
                    {l}
                  </button>
                ))}
              </div>
            </Field>
            {String(f.llama_mode || 'gpu') !== 'cpu' && <Field label={t('GPU layers')}>{text('ngl', { placeholder: '99', inputMode: 'numeric' })}</Field>}
            <Field label={t('MoE layers on CPU')}>{text('n_cpu_moe', { placeholder: '0', inputMode: 'numeric' })}</Field>
            <Field label={t('KV cache type')}>{select('cache_type', [['', 'f16'], ['q8_0', 'q8_0'], ['q4_0', 'q4_0']])}</Field>
            <Field label={t('Split mode')}>{select('llama_split_mode', [['', 'auto'], ['none', 'none'], ['layer', 'layer'], ['row', 'row'], ['tensor', 'tensor']])}</Field>
            <Field label={t('Tensor split')}>{text('llama_tensor_split', { placeholder: '1,1' })}</Field>
            <Field label={t('Main GPU')}>{text('llama_main_gpu', { placeholder: '0', inputMode: 'numeric' })}</Field>
            <Field label={t('Parallel slots')}>{text('llama_parallel', { placeholder: '1', inputMode: 'numeric' })}</Field>
            <Field label={t('Batch')}>{text('llama_batch_size', { placeholder: '2048', inputMode: 'numeric' })}</Field>
            <Field label={t('Micro-batch')}>{text('llama_ubatch_size', { placeholder: '512', inputMode: 'numeric' })}</Field>
            <Field label={t('Fit')}>{select('llama_fit', [['', 'auto'], ['on', 'on'], ['off', 'off']])}</Field>
            <div className="fs-ck__switches">
              {sw('flash_attn', t('Flash attention'))}
              {sw('llama_no_mmap', t('No mmap'))}
              {sw('llama_no_warmup', t('No warmup'))}
              {sw('llama_speculative_mtp', t('Speculative MTP'))}
              {projectors.length > 0 && sw('vision', t('Vision (mmproj)'), projectors[0].rel_path)}
            </div>
            {f.llama_speculative_mtp && <Field label={t('Draft tokens')}>{text('llama_spec_tokens', { placeholder: '3', inputMode: 'numeric' })}</Field>}
            {profiles === null && <Skeleton label={t('Reading hardware profiles')} height="32px" />}
            {profiles && profiles.length > 0 && (
              <div className="fs-ck__profiles" role="group" aria-label={t('Hardware profiles')}>
                <span className="fs-ck__label">{t('Profiles for this machine')}</span>
                {profiles.map((p) => (
                  <button key={p.key} type="button" className="fs-chip" data-tone={p.fits ? undefined : 'warning'} onClick={() => applyProfile(p)} title={`${p.note} · ${p.quant} · ${p.ctx} ctx · ~${p.est_vram_gb} GB${p.offloads ? ' · offloads' : ''}${p.fits ? '' : ' · does not fit'}`}>
                    {p.label} <span className="fs-gal__n">{p.quant}</span>
                  </button>
                ))}
              </div>
            )}
          </>
        )}

        {backend === 'ollama' && repo.includes('/') && (
          <Field label={t('GGUF file')} wide>
            {ggufs.length ? select('gguf_file', [['', t('First GGUF found')], ...ggufs.map((g): [string, string] => [g.rel_path, `${ggufQuant(g.rel_path)} · ${g.rel_path.split('/').pop()}`])]) : text('gguf_file', { placeholder: 'model-Q4_K_M.gguf' })}
          </Field>
        )}

        {(backend === 'diffusers' || backend === 'mlx_image') && (
          <>
            <Field label={t('Steps')}>{text('diff_steps', { placeholder: '28', inputMode: 'numeric' })}</Field>
            <Field label={t('Width')}>{text('diff_width', { placeholder: '1024', inputMode: 'numeric' })}</Field>
            <Field label={t('Height')}>{text('diff_height', { placeholder: '1024', inputMode: 'numeric' })}</Field>
            {backend === 'diffusers' && (
              <>
                <Field label={t('dtype')}>{select('diff_dtype', [['bfloat16', 'bfloat16'], ['float16', 'float16'], ['float32', 'float32']])}</Field>
                <Field label={t('Device map')}>{select('diff_device_map', [['balanced', 'balanced'], ['cuda', 'cuda'], ['cpu', 'cpu']])}</Field>
                <Field label={t('Guidance scale')}>{text('diff_guidance_scale', { placeholder: '3.5' })}</Field>
                <Field label={t('Negative prompt')} wide>
                  {text('diff_negative_prompt')}
                </Field>
                <Field label={t('LoRA')} wide>
                  {text('diff_lora', { placeholder: 'repo or path, …' })}
                </Field>
                <Field label={t('LoRA scale')}>{text('diff_lora_scale', { placeholder: '1.0' })}</Field>
                <Field label={t('Harmonise GPU')}>{text('diff_harmonize_gpu', { placeholder: '', inputMode: 'numeric' })}</Field>
                <div className="fs-ck__switches">
                  {sw('diff_offload', t('CPU offload'))}
                  {sw('diff_attention_slicing', t('Attention slicing'))}
                  {sw('diff_vae_slicing', t('VAE slicing'))}
                  {sw('host', t('Reachable from the network'))}
                </div>
              </>
            )}
            {backend === 'mlx_image' && (
              <>
                <Field label={t('Base model')}>{text('mlx_base_model')}</Field>
                <Field label={t('LoRA style')}>{text('mlx_lora_style')}</Field>
                <Field label={t('LoRA paths')} wide>
                  {text('mlx_lora_paths')}
                </Field>
                <Field label={t('LoRA scales')}>{text('mlx_lora_scales', { placeholder: '1.0, 0.8' })}</Field>
              </>
            )}
          </>
        )}

        {backend === 'mlx' && <div className="fs-ck__switches">{sw('host', t('Reachable from the network'))}</div>}

        <Field label={t('Extra arguments')} wide>
          {text('extra', { placeholder: '--anything-else' })}
        </Field>
        <Field label={t('Model path override')} wide hint={t('A local folder instead of the repo id')}>
          {text('model_path', { placeholder: model.is_local_dir && model.path ? `${model.path}/${repo}` : '' })}
        </Field>
      </div>

      <div className="fs-ck__cmd-box">
        <span className="fs-ck__label">{t('Command')}</span>
        <textarea className="fs-field fs-ck__cmd-edit" rows={3} value={cmd} onChange={(e) => setCmdOverride(e.target.value)} spellCheck={false} data-testid="serve-cmd" />
        <div className="fs-inline">
          {cmdOverride !== null && <Button variant="ghost" size="sm" label={t('Back to the generated command')} onClick={() => setCmdOverride(null)} />}
          <CopyButton text={cmd} say={say} />
        </div>
      </div>

      <div className="fs-ck__serve-actions">
        <Button variant="primary" icon={Play} label={replaceTaskId ? t('Relaunch') : t('Launch')} loading={busy} onClick={() => void launch()} testId="serve-launch" />
        <Button variant="ghost" size="sm" icon={CalendarClock} label={t('Schedule…')} onClick={() => onSchedule(repo)} />
        <span className="fs-spacer" />
        <input className="fs-field" value={presetName} onChange={(e) => setPresetName(e.target.value)} placeholder={t('Preset name')} aria-label={t('Preset name')} style={{ inlineSize: '180px' }} />
        <Button variant="ghost" size="sm" icon={Save} label={t('Save preset')} onClick={savePreset} />
      </div>
    </div>
  );
}
