import { getSettings, saveSettings } from '../../adapters/settings';
import { t } from '../../i18n';
import {
  ArrowUp,
  AudioLines,
  Bot,
  BookOpen,
  Brain,
  Database,
  EyeOff,
  FileText,
  FolderOpen,
  Gauge,
  Globe,
  Layers,
  Telescope,
  ListTodo,
  MessageSquare,
  Mic,
  MicOff,
  Paperclip,
  Plus,
  Shield,
  RefreshCw,
  SlidersHorizontal,
  Square,
  Terminal,
  X,
} from 'lucide-react';
import type { Dictation } from '../../adapters/speech';
import {
  memo,
  useCallback,
  useEffect,
  useRef,
  useMemo,
  useState,
  type ChangeEvent,
  type ClipboardEvent,
  type DragEvent,
  type KeyboardEvent,
  type ReactNode,
  type RefObject,
} from 'react';
import { IconButton,Popover } from '../../components';
import {
  loadStrategyProfile,
  saveStrategyProfile,
  loadRecipes,
  STRATEGY_PROFILES,
  type StrategyProfile,
  type Recipe,
} from '../../adapters/strategy';
import {
  attachmentUrl,
  basename,
  describeGen,
  isImage,
  searchWorkspaceFiles,
  uploadFiles,
  type Attachment,
  type GenOverrides,
  type WorkspaceFile,
} from '../../adapters/composer';
import type { Suggestion } from './commands';
import { capMentionItems, resolveSuggestionIntent } from './composer-suggest';
import { frameBatcher } from '../../lib/frame-batch';
import { clipboardFiles, insertPastedText } from '../../lib/clipboard-attachments';
import {REFERENCE_ROLES} from '../../lib/image-references';
import {MediaRecipes} from './MediaRecipes';
import { createAttachmentUploads, type PendingAttachment } from '../../lib/attachment-uploads';
import type { ContextOverrides } from '../../adapters/chat';
import { ContextPanel, pruneOverrides } from './ContextPanel';

export type Mode = 'chat' | 'agent';

export interface Knobs {
  mode: Mode;
  web: boolean;
  bash: boolean;
  plan: boolean;
  rag: boolean;
  /** Skip automatic personal-memory retrieval, not chat history or project sources. */
  noMemory?: boolean;
  noSkills?: boolean;
  inputTokenBudget?: number;
  /** Nobody mode: the conversation is not saved and memory stays closed. */
  incognito: boolean;
  /** Deep Research before the next answer; switches itself off after the turn. */
  research: boolean;
  /** TASK-06: how far one agent turn may go before it must stop and check in
   *  (src/autonomy_budget.py). Undefined behaves exactly like 'supervised' —
   *  the server's own default when the field is omitted entirely. */
  autonomyPreset?: AutonomyPreset;
  /** UX-07: per-turn pins/exclusions from ContextPanel, forwarded to
   *  sendTurn() as `context_overrides` exactly like autonomyPreset travels
   *  as `autonomy_preset` — never written back to any global setting. */
  contextOverrides?: ContextOverrides;
}

export type AutonomyPreset = 'supervised' | 'bounded_autonomous' | 'read_only';

export interface ComposerProps {
  draft: string;
  setDraft: (value: string) => void;
  busy: boolean;
  pending: boolean;
  preparing?: boolean;
  knobs: Knobs;
  setKnobs: (update: (k: Knobs) => Knobs) => void;
  workspace: string;
  onPickWorkspace: () => void;
  onClearWorkspace: () => void;
  gen: GenOverrides;
  onClearGen: () => void;
  attachments: Attachment[];
  setAttachments: (update: (list: Attachment[]) => Attachment[]) => void;
  sessionId: string | null;
  onSend: (text: string) => void;
  onStop: () => void;
  /** UX-04: the three Stop scopes and the two ways to add to a live turn
   *  without cancelling it. Optional — a caller that omits them gets the
   *  plain Stop button exactly as before (see the `busy` branch below). */
  onPauseGeneration?: () => void;
  onCancelTask?: () => void;
  onCancelWork?: () => void;
  onSteer?: (text: string) => void;
  onQueueSend?: (text: string) => void;
  onVoice?: () => void;
  voiceActive?: boolean;
  onNotice: (text: string, tone?: 'info' | 'warning' | 'danger') => void;
  modelPicker: ReactNode;
  /** The preset chip (picker + clear), rendered by the screen. */
  presetChip?: ReactNode;
  extraControls?: ReactNode;
  /** ↑ on an empty composer brings back the last thing you sent. */
  lastSent?: string;
  textareaRef: RefObject<HTMLTextAreaElement | null>;
}

const MENTION = /(^|\s)@([^\s@]*)$/;

/**
 * UX-06: an attachment that cannot possibly be used must say so before it is
 * ever uploaded — not fail during inference once the model tries to use it.
 * `/api/media/capabilities` (MEDIA-01, `src/media_capabilities.py`) is a
 * real probe (ffmpeg present AND answering, a transcription backend
 * actually loadable), never a guess from a file extension; this reads it
 * once, cached for the session, and checks only the two kinds that DEPEND
 * on an optional backend — audio needs a working transcriber, video needs
 * ffmpeg. Everything else (images, text, PDF, code) has no such dependency
 * and is never blocked here.
 *
 * The endpoint is admin-gated (`routes/local_video_routes.py`'s
 * `Depends(require_admin)`) — a non-admin session's fetch answers 403, and
 * that failure is read the same as "unknown": never blocks a normal send,
 * only adds an upfront reason when the check actually succeeds.
 */
interface MediaCapabilities {
  audio: boolean;
  video: boolean;
}
let mediaCapsPromise: Promise<MediaCapabilities | null> | null = null;
function mediaCapabilities(): Promise<MediaCapabilities | null> {
  if (!mediaCapsPromise) {
    mediaCapsPromise = fetch('/api/media/capabilities', { credentials: 'same-origin' })
      .then((r) => (r.ok ? r.json() : null))
      .then((data: unknown) => {
        if (!data || typeof data !== 'object') return null;
        const backends = (data as Record<string, unknown>).backends;
        if (!backends || typeof backends !== 'object') return null;
        const installed = (key: string) => {
          const entry = (backends as Record<string, unknown>)[key];
          return Boolean(entry && typeof entry === 'object' && (entry as Record<string, unknown>).installed);
        };
        return { audio: installed('stt'), video: installed('ffmpeg') };
      })
      .catch(() => null);
  }
  return mediaCapsPromise;
}

/** Pasting/dropping a very large file must never stall the composer while
 *  it is merely being queued — the actual read happens off the main thread
 *  either way (`URL.createObjectURL`/`FormData`, never `FileReader.
 *  readAsDataURL`), but a file with nothing usable behind it is still worth
 *  refusing outright rather than spending an upload attempt on it. */
const MAX_ATTACHMENT_BYTES = 200 * 1024 * 1024; // 200MB

/** Reason a file cannot be attached, or `null` when it is fine — checked
 *  before it is ever queued, so nothing is uploaded (and no inference is
 *  ever asked to use it) for a file this can already rule out locally. */
async function incompatibilityReason(file: File): Promise<string | null> {
  if (file.size > MAX_ATTACHMENT_BYTES) {
    return t('{name} is too large ({size} MB) to attach.', { name: file.name, size: Math.round(file.size / 1024 / 1024) });
  }
  const caps = await mediaCapabilities();
  if (!caps) return null; // unknown (not an admin session, or the probe failed): never block on a guess
  if (file.type.startsWith('audio/') && !caps.audio) {
    return t('{name} is audio, but no transcription backend is installed — it would not be usable.', { name: file.name });
  }
  if (file.type.startsWith('video/') && !caps.video) {
    return t('{name} is video, but ffmpeg is not installed — it would not be usable.', { name: file.name });
  }
  return null;
}

/**
 * The composer. Everything the old input bar did — attachments, `@` files,
 * `#` rules, `/` commands, the mode and tool toggles, the folder — in one
 * slab, with the two pickers (files, commands) drawn as a strip above the
 * text instead of a floating menu.
 */
export function Composer({
  draft,
  setDraft,
  busy,
  pending,
  preparing = false,
  knobs,
  setKnobs,
  workspace,
  onPickWorkspace,
  onClearWorkspace,
  gen,
  onClearGen,
  attachments,
  setAttachments,
  sessionId,
  onSend,
  onStop,
  onPauseGeneration,
  onCancelTask,
  onCancelWork,
  onSteer,
  onQueueSend,
  onVoice,
  voiceActive,
  onNotice,
  modelPicker,
  presetChip,
  extraControls,
  lastSent,
  textareaRef,
}: ComposerProps) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [pendingFiles, setPendingFiles] = useState<PendingAttachment[]>([]);
  const attachmentTarget = useRef({sessionId, setAttachments});
  attachmentTarget.current = {sessionId, setAttachments};
  const uploads = useMemo(() => createAttachmentUploads<Attachment>({
    upload: (file, signal) => uploadFiles([file], sessionId, signal),
    ready: (uploaded) => {
      if (attachmentTarget.current.sessionId !== sessionId) return;
      attachmentTarget.current.setAttachments((list) => [...list, ...uploaded.filter((u) => !list.some((a) => a.id === u.id))]);
    },
    change: (entries) => { if (attachmentTarget.current.sessionId === sessionId) setPendingFiles(entries); },
    preview: (file) => isImage(file.type) ? URL.createObjectURL(file) : '',
    revoke: (url) => URL.revokeObjectURL(url),
    timeoutMessage: () => t('Upload timed out. Retry or remove this attachment.'),
    emptyMessage: () => t('The server returned no attachment. Retry the upload.'),
  }), [sessionId]);
  useEffect(() => { uploads.resume(); return () => uploads.dispose(); }, [uploads]);
  const uploading = pendingFiles.some((file) => file.state !== 'failed');
  const [dragging, setDragging] = useState(false);

  /* ── CMP-09/CMP-12: strategy profile + recipe, persisted server-side per
     owner (optionally scoped to this session) — see adapters/strategy.ts's
     module docstring for why this reads/writes the endpoint directly
     instead of travelling with sendTurn like autonomyPreset does. */
  const [strategyProfile, setStrategyProfile] = useState<StrategyProfile>('balanced');
  const [activeRecipeId, setActiveRecipeId] = useState<string | null>(null);
  const [recipes, setRecipes] = useState<Recipe[]>([]);
  useEffect(() => {
    let cancelled = false;
    loadStrategyProfile(sessionId).then((active) => {
      if (cancelled) return;
      setStrategyProfile(active.profile);
      setActiveRecipeId(active.recipeId);
    }).catch(() => { /* best-effort — keeps the last known profile on screen */ });
    return () => { cancelled = true; };
  }, [sessionId]);
  useEffect(() => {
    let cancelled = false;
    loadRecipes().then((list) => { if (!cancelled) setRecipes(list); }).catch(() => {});
    return () => { cancelled = true; };
  }, []);
  const pickStrategyProfile = useCallback((profile: StrategyProfile) => {
    setStrategyProfile(profile);
    saveStrategyProfile({ profile }, sessionId).catch(() => {
      onNotice(t('Could not save the strategy profile.'), 'warning');
    });
  }, [sessionId, onNotice]);
  const pickRecipe = useCallback((recipeId: string | null) => {
    setActiveRecipeId(recipeId);
    saveStrategyProfile({ recipeId }, sessionId).catch(() => {
      onNotice(t('Could not save the active recipe.'), 'warning');
    });
  }, [sessionId, onNotice]);

  /* ── Dictation ── */
  const [dictation, setDictation] = useState<Dictation | null>(null);
  const [transcribing, setTranscribing] = useState(false);
  const dictationController = useRef<AbortController | null>(null);
  useEffect(() => () => { dictationController.current?.abort(); }, [sessionId]);
  useEffect(() => {
    if (voiceActive) { dictationController.current?.abort(); setDictation(null); setTranscribing(false); }
  }, [voiceActive]);
  const toggleDictation = async () => {
    if (dictation) {
      dictation.stop();
      setTranscribing(true);
      return;
    }
    try {
      // The speech adapter (recorder + browser fallbacks) loads on first use.
      const { startDictation } = await import('../../adapters/speech');
      const controller = new AbortController();
      dictationController.current?.abort(); dictationController.current = controller;
      const d = await startDictation(undefined, controller.signal);
      if (controller.signal.aborted) { d.cancel(); return; }
      setDictation(d);
      d.done
        .then((text) => {
          if (controller.signal.aborted) return;
          const current = textareaRef.current?.value ?? '';
          if (text) setDraft(current ? `${current.trimEnd()} ${text}` : text);
          else onNotice(t('I did not hear anything.'), 'warning');
        })
        .catch((e: Error) => { if (!controller.signal.aborted) onNotice(`${t('Dictation')}: ${e.message}`, 'danger'); })
        .finally(() => {
          setDictation(null);
          setTranscribing(false);
          requestAnimationFrame(() => textareaRef.current?.focus());
        });
    } catch (e) {
      onNotice((e as Error).message, 'danger');
    }
  };

  /* ── Suggestions: `@` files or `/` commands ──
   * UX-06: the actual matching (resolveSuggestionIntent, composer-suggest.ts)
   * runs behind a frameBatcher (studio/src/lib/frame-batch.ts — the same
   * coalescing Transcript.tsx already uses to cap streaming repaints at one
   * per frame) instead of directly on every keystroke, so several keystrokes
   * landing in one animation frame run the match pass once, not once each —
   * what keeps a long draft under a 16ms-per-keystroke budget. */
  const [mention, setMention] = useState<{ query: string; items: WorkspaceFile[] } | null>(null);
  const [commands, setCommands] = useState<Suggestion[] | null>(null);
  const [active, setActive] = useState(0);
  const mentionAbort = useRef<AbortController | null>(null);
  const suggestionCtx = useRef({ workspace });
  suggestionCtx.current = { workspace };

  const applySuggestionIntent = useCallback((value: string, caret: number) => {
    const intent = resolveSuggestionIntent(value, caret);
    if (intent.kind === 'mention' && suggestionCtx.current.workspace) {
      mentionAbort.current?.abort();
      const controller = new AbortController();
      mentionAbort.current = controller;
      searchWorkspaceFiles(suggestionCtx.current.workspace, intent.query, controller.signal)
        .then((items) => {
          if (controller.signal.aborted) return;
          setMention({ query: intent.query, items: capMentionItems(items) });
          setActive(0);
        })
        .catch(() => undefined);
      setCommands(null);
      return;
    }
    setMention(null);
    if (intent.kind === 'commands') {
      setCommands(intent.items);
      setActive(0);
      return;
    }
    setCommands(null);
  }, []);

  const suggestBatcher = useRef<ReturnType<typeof frameBatcher<{ value: string; caret: number }>> | null>(null);
  if (!suggestBatcher.current) {
    suggestBatcher.current = frameBatcher(({ value, caret }) => applySuggestionIntent(value, caret));
  }
  useEffect(() => () => suggestBatcher.current?.cancel(), []);

  const refreshSuggestions = useCallback((value: string, caret: number) => {
    suggestBatcher.current?.push({ value, caret });
  }, []);

  const onChange = (event: ChangeEvent<HTMLTextAreaElement>) => {
    const el = event.target;
    setDraft(el.value);
    el.style.blockSize = 'auto';
    el.style.blockSize = `${Math.min(el.scrollHeight, 220)}px`;
    refreshSuggestions(el.value, el.selectionStart ?? el.value.length);
  };

  const pickMention = (item: WorkspaceFile) => {
    const el = textareaRef.current;
    const caret = el?.selectionStart ?? draft.length;
    const before = draft.slice(0, caret).replace(MENTION, (_all, lead: string) => `${lead}@${item.path} `);
    const next = before + draft.slice(caret);
    setDraft(next);
    setMention(null);
    requestAnimationFrame(() => {
      if (!el) return;
      el.focus();
      el.setSelectionRange(before.length, before.length);
    });
  };

  const pickCommand = (command: Suggestion) => {
    setDraft(`/${command.insert.trimEnd()} `);
    setCommands(null);
    requestAnimationFrame(() => textareaRef.current?.focus());
  };

  /* ── Attachments ── */
  // UX-06: filtered before anything is queued — see `incompatibilityReason`'s
  // doc comment. `void` on purpose: the caller (paste/drop/file-input) never
  // waits on this, so a large batch never blocks the keystroke or drop event
  // that triggered it.
  const addFiles = (files: File[]) => {
    void (async () => {
      const accepted: File[] = [];
      for (const file of files) {
        const reason = await incompatibilityReason(file);
        if (reason) onNotice(reason, 'warning');
        else accepted.push(file);
      }
      if (accepted.length) uploads.add(accepted);
    })();
  };

  const onPaste = (event: ClipboardEvent<HTMLTextAreaElement>) => {
    const files = clipboardFiles(event.clipboardData);
    if (files.length) {
      event.preventDefault();
      const text = event.clipboardData.getData('text/plain');
      if (text) {
        const input = event.currentTarget;
        const pasted = insertPastedText(input.value, input.selectionStart, input.selectionEnd, text);
        setDraft(pasted.value);
        requestAnimationFrame(() => { input.focus(); input.setSelectionRange(pasted.caret, pasted.caret); });
      }
      addFiles(files);
    }
  };

  const onDrop = (event: DragEvent<HTMLFormElement>) => {
    event.preventDefault();
    setDragging(false);
    void addFiles(Array.from(event.dataTransfer.files ?? []));
  };

  /* ── Keys ── */
  const onKey = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    const list = mention?.items ?? commands;
    if (list && list.length) {
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        setActive((i) => (i + 1) % list.length);
        return;
      }
      if (event.key === 'ArrowUp') {
        event.preventDefault();
        setActive((i) => (i - 1 + list.length) % list.length);
        return;
      }
      if (event.key === 'Tab' || (event.key === 'Enter' && !event.shiftKey)) {
        event.preventDefault();
        if (mention) pickMention(mention.items[active]);
        else if (commands) pickCommand(commands[active]);
        return;
      }
      if (event.key === 'Escape') {
        setMention(null);
        setCommands(null);
        return;
      }
    }
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      if (!uploads.hasPending()) onSend(draft);
      return;
    }
    if (event.key === 'ArrowUp' && !draft && lastSent) {
      // Empty composer: bring back the last message, caret at the end.
      event.preventDefault();
      setDraft(lastSent);
      requestAnimationFrame(() => {
        const el = textareaRef.current;
        if (el) el.setSelectionRange(el.value.length, el.value.length);
      });
      return;
    }
    // UX-04: the shortcut now names the least destructive action — stop the
    // generation, leave the turn resumable — same key as always; the harsher
    // "cancel the task"/"cancel all work" scopes only live in the Stop menu.
    if (event.key === 'Escape' && busy) (onPauseGeneration ?? onStop)();
  };

  useEffect(() => {
    if (!draft) {
      setMention(null);
      setCommands(null);
    }
    // Text can land without a keystroke (a quote, ↑, a dictation, ?draft=):
    // the box still has to grow to fit it.
    const el = textareaRef.current;
    if (el) {
      el.style.blockSize = 'auto';
      el.style.blockSize = `${Math.min(el.scrollHeight, 220)}px`;
    }
  }, [draft, textareaRef]);

  useEffect(() => {
    // A removed `@file` must not leave a dangling exclusion/pin behind it —
    // the chip that explained the override is gone, so the override itself
    // must go too, not linger invisibly into the next send.
    if (!knobs.contextOverrides) return;
    const pruned = pruneOverrides(knobs.contextOverrides, draft);
    const changed = (pruned.excludeSources?.length ?? 0) !== (knobs.contextOverrides.excludeSources?.length ?? 0)
      || (pruned.pinSources?.length ?? 0) !== (knobs.contextOverrides.pinSources?.length ?? 0);
    if (changed) setKnobs((k) => ({ ...k, contextOverrides: pruned }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft]);

  const genLabel = describeGen(gen);
  const canSend = (draft.trim().length > 0 || attachments.length > 0) && pendingFiles.length === 0;

  return (
    <form
      className="fs-studio__composer fs-panel"
      data-dragging={dragging || undefined}
      onSubmit={(event) => {
        event.preventDefault();
        if (!uploads.hasPending()) onSend(draft);
      }}
      onDragOver={(event) => {
        event.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={onDrop}
      data-testid="studio-composer"
    >
      {mention && mention.items.length > 0 && (
        <ul className="fs-studio__suggest" role="listbox" aria-label={t('Workspace files')} data-testid="studio-mentions">
          {mention.items.map((item, i) => (
            <li
              key={item.path}
              role="option"
              aria-selected={i === active}
              className="fs-studio__suggest-item"
              onMouseDown={(event) => {
                event.preventDefault();
                pickMention(item);
              }}
            >
              <FileText size={13} aria-hidden="true" />
              <span className="fs-studio__suggest-main">{item.path}</span>
            </li>
          ))}
        </ul>
      )}
      {commands && commands.length > 0 && (
        <ul className="fs-studio__suggest" role="listbox" aria-label={t('Commands')} data-testid="studio-commands">
          {commands.map((command, i) => (
            <li
              key={command.insert}
              role="option"
              aria-selected={i === active}
              className="fs-studio__suggest-item"
              onMouseDown={(event) => {
                event.preventDefault();
                pickCommand(command);
              }}
            >
              <span className="fs-studio__suggest-main">
                <code>{t(command.usage)}</code>
              </span>
              <span className="fs-studio__suggest-cat">{t(command.category)}</span>
              <span className="fs-studio__suggest-help">{t(command.help)}</span>
            </li>
          ))}
        </ul>
      )}

      {(attachments.length > 0 || pendingFiles.length > 0) && (
        <AttachmentList
          pendingFiles={pendingFiles}
          attachments={attachments}
          sessionId={sessionId}
          onRetry={uploads.retry}
          onRemovePending={uploads.remove}
          onSetReferenceRole={(id, role) => setAttachments((list) => list.map((item) => (item.id === id ? { ...item, referenceRole: role } : item)))}
          onRemoveAttachment={(id) => setAttachments((list) => list.filter((x) => x.id !== id))}
        />
      )}

      <ContextPanel
        draft={draft}
        overrides={knobs.contextOverrides ?? {}}
        onChange={(next) => setKnobs((k) => ({ ...k, contextOverrides: next }))}
        tokenBudget={knobs.inputTokenBudget}
      />

      {preparing && <p className="fs-studio__paste-hint" role="status">{t('Creating the conversation… Your draft is kept until it is ready.')}</p>}

      <textarea
        ref={textareaRef}
        className="fs-studio__input"
        rows={1}
        value={draft}
        placeholder={
          pending
            ? t('Answer above, or type to go on…')
            : knobs.mode === 'agent'
              ? t('Tell me what you want done…  @file · #rule · /command')
              : t('Write a message…  /command')
        }
        aria-label={t('Message')}
        onChange={onChange}
        onKeyDown={onKey}
        onPaste={onPaste}
        onClick={(event) => refreshSuggestions(draft, event.currentTarget.selectionStart ?? draft.length)}
        data-testid="studio-input"
      />
      <p className="fs-studio__paste-hint">{t('Paste a screenshot with Ctrl+V, or drop a file here.')}</p>

      <div className="fs-studio__bar">
        <Popover side="top" className="fs-studio__add-menu" testId="studio-add-menu" trigger={<IconButton icon={Plus} label={t('Add files and tools')} testId="studio-add" />}>
          <div className="fs-studio__add-content">


        <div className="fs-studio__add-options">
          <MediaRecipes onInsert={text=>setDraft(draft.trim()?`${draft.trimEnd()}\n\n${text}`:text)}/>
          {extraControls}
          <input
            ref={fileInputRef}
            type="file"
            multiple
            hidden
            onChange={(event) => {
              void addFiles(Array.from(event.target.files ?? []));
              event.target.value = '';
            }}
            data-testid="studio-file-input"
          />
          <button type="button" className="fs-studio__chip" disabled={uploading} onClick={() => fileInputRef.current?.click()} data-testid="studio-attach"><Paperclip size={14} />{uploading ? t('Uploading…') : t('Attach files')}</button>

          <button
            type="button"
            className="fs-studio__chip"
            aria-pressed={knobs.web}
            onClick={() => setKnobs((k) => ({ ...k, web: !k.web }))}
            data-testid="studio-knob-web"
          >
            <Globe size={13} aria-hidden="true" /> {t('Web')}
          </button>
          <button
            type="button"
            className="fs-studio__chip"
            aria-pressed={knobs.research}
            title={t('Deep Research for the next message: several rounds of search and reading, then the answer with sources')}
            onClick={() => setKnobs((k) => ({ ...k, research: !k.research }))}
            data-testid="studio-knob-research"
          >
            <Telescope size={13} aria-hidden="true" /> {t('Research')}
          </button>
          <button
            type="button"
            className="fs-studio__chip"
            aria-pressed={knobs.rag}
            title={t('Search your indexed documents (RAG)')}
            onClick={() => setKnobs((k) => ({ ...k, rag: !k.rag }))}
            data-testid="studio-knob-rag"
          >
            <Database size={13} aria-hidden="true" /> {t('Docs')}
          </button>
          <button
            type="button"
            className="fs-studio__chip"
            aria-pressed={Boolean(knobs.noMemory || knobs.incognito)}
            disabled={knobs.incognito}
            title={t('Skip automatic personal-memory recall. Chat history and project context stay available; this is not incognito mode.')}
            onClick={() => setKnobs((k) => ({ ...k, noMemory: !k.noMemory }))}
            data-testid="studio-knob-no-memory"
          >
            <Brain size={13} aria-hidden="true" /> {t('Skip memory recall')}
          </button>
          {knobs.mode === 'agent' && (
            <>
              <Popover side="top" className="fs-media-recipes" trigger={<button type="button" className="fs-studio__chip" aria-pressed={Boolean(knobs.noSkills||knobs.inputTokenBudget)}><SlidersHorizontal size={13}/>{t('Agent context')}</button>}>
                <section aria-label={t('Agent context')}>
                  <h3>{t('Agent context')}</h3>
                  <label><span><input type="checkbox" checked={Boolean(knobs.noSkills)} onChange={event=>setKnobs(k=>({...k,noSkills:event.target.checked}))}/>{t('Skip automatic skills')}</span></label>
                  <p>{t('Skips skill suggestions and their automatic tool selection. Explicit tool requests and project instructions remain available.')}</p>
                  <label>{t('Soft input budget')}<select value={knobs.inputTokenBudget??''} onChange={event=>setKnobs(k=>({...k,inputTokenBudget:event.target.value?Number(event.target.value):undefined}))}>
                    <option value="">{t('Automatic (model and settings)')}</option>{[4096,8192,16384,32768,65536,131072,200000].map(value=><option key={value} value={value}>{value.toLocaleString()} tokens</option>)}
                  </select></label>
                  <p>{t('Applies to the next agent request and its tool rounds. The model window still limits it; token counts are estimates, not billing limits.')}</p>
                  <p>{t('Use @file for explicit references and Docs for indexed documents. Project context stays attached to the project.')}</p>
                </section>
              </Popover>
              <button
                type="button"
                className="fs-studio__chip"
                aria-pressed={knobs.bash}
                onClick={() => setKnobs((k) => ({ ...k, bash: !k.bash }))}
                data-testid="studio-knob-bash"
              >
                <Terminal size={13} aria-hidden="true" /> {t('Terminal')}
              </button>
              <button
                type="button"
                className="fs-studio__chip"
                aria-pressed={knobs.plan}
                onClick={() => setKnobs((k) => ({ ...k, plan: !k.plan }))}
                data-testid="studio-knob-plan"
              >
                <ListTodo size={13} aria-hidden="true" /> {t('Plan')}
              </button>
              <span className="fs-studio__chipgroup">
                <button
                  type="button"
                  className="fs-studio__chip fs-studio__chip--folder"
                  aria-pressed={Boolean(workspace)}
                  title={workspace ? `${t('Folder')}: ${workspace}` : t('No folder: the agent cannot read or edit files')}
                  onClick={onPickWorkspace}
                  data-testid="studio-workspace"
                >
                  <FolderOpen size={13} aria-hidden="true" />
                  <span>{workspace ? basename(workspace) : t('Choose folder')}</span>
                </button>
                {workspace && (
                  <button
                    type="button"
                    className="fs-studio__chip-x"
                    aria-label={t('Remove the folder')}
                    onClick={onClearWorkspace}
                  >
                    <X size={11} aria-hidden="true" />
                  </button>
                )}
              </span>
            </>
          )}
          <button
            type="button"
            className="fs-studio__chip fs-studio__chip--incognito"
            aria-pressed={knobs.incognito}
            title={t('Nobody mode: nothing from this conversation is saved and the memory stays closed')}
            onClick={() => setKnobs((k) => ({ ...k, incognito: !k.incognito }))}
            data-testid="studio-knob-incognito"
          >
            <EyeOff size={13} aria-hidden="true" /> {t('Incognito')}
          </button>
          {presetChip}
          {genLabel && (
            <span className="fs-studio__chipgroup">
              <span className="fs-studio__chip" aria-pressed="true" title={t('Generation settings of this chat (/temp, /maxtokens, /topp, /think, /gen)')}>
                <SlidersHorizontal size={13} aria-hidden="true" /> {genLabel}
              </span>
              <button type="button" className="fs-studio__chip-x" aria-label={t('Remove the generation settings')} onClick={onClearGen}>
                <X size={11} aria-hidden="true" />
              </button>
            </span>
          )}
        </div>
          </div>
        </Popover>
        <div className="fs-studio__seg" role="radiogroup" aria-label={t('Mode')}>
          <span className="fs-studio__seg-thumb" data-mode={knobs.mode} aria-hidden="true" />
          <button
            type="button"
            role="radio"
            aria-checked={knobs.mode === 'chat'}
            onClick={() => setKnobs((k) => ({ ...k, mode: 'chat' }))}
            data-testid="studio-mode-chat"
          >
            <MessageSquare size={13} aria-hidden="true" /> {t('Chat')}
          </button>
          <button
            type="button"
            role="radio"
            aria-checked={knobs.mode === 'agent'}
            onClick={() => setKnobs((k) => ({ ...k, mode: 'agent' }))}
            data-testid="studio-mode-agent"
          >
            <Bot size={13} aria-hidden="true" /> {t('Agent')}
          </button>
        </div>
        {knobs.mode === 'agent' && (
          <AutonomyPresetSelector
            preset={knobs.autonomyPreset ?? 'supervised'}
            onPick={(value) => setKnobs((k) => ({ ...k, autonomyPreset: value }))}
          />
        )}
        <ApprovalSelector disabled={busy} onNotice={onNotice} />
        <StrategyProfileSelector profile={strategyProfile} onPick={pickStrategyProfile} />
        <RecipeSelector recipeId={activeRecipeId} recipes={recipes} onPick={pickRecipe} />
        <div className="fs-studio__model-control">{modelPicker}</div>
          <span className="fs-studio__mic" data-recording={dictation ? true : undefined}>
            <IconButton
              icon={dictation ? MicOff : Mic}
              label={dictation ? t('Stop dictating') : transcribing ? t('Transcribing…') : t('Dictate')}
              size="sm"
              disabled={transcribing || voiceActive}
              onClick={() => void toggleDictation()}
              testId="studio-mic"
            />
          </span>
        <div className="fs-studio__send">
          {onVoice && <IconButton icon={AudioLines} label={t(voiceActive ? 'Close voice mode' : 'Talk to Faustus')} onClick={onVoice} testId="studio-voice" />}
          {busy ? (
            <StopMenu
              onStop={onStop}
              onPauseGeneration={onPauseGeneration}
              onCancelTask={onCancelTask}
              onCancelWork={onCancelWork}
              onSteer={onSteer}
              onQueueSend={onQueueSend}
            />
          ) : (
            <button type="submit" className="fs-studio__go" disabled={!canSend || preparing} aria-label={t(preparing ? 'Creating…' : 'Send')} data-testid="studio-send">
              <ArrowUp size={18} aria-hidden="true" />
            </button>
          )}
        </div>
      </div>
    </form>
  );
}

/**
 * UX-06: the attachment strip, split out and `memo`-ed so it only
 * re-renders when an attachment actually changes — not on every keystroke.
 * `Composer` re-renders on every `draft` change (a controlled textarea has
 * no other way to work); with 200 attachments the old inline `.map()` sat
 * in that same render and reconciled 200 `<li>`s per keystroke for no
 * reason, since none of them depend on the draft text. `pendingFiles` and
 * `attachments` only change on an actual upload event, so `memo`'s default
 * shallow-prop comparison skips this subtree on every other render.
 */
const AttachmentList = memo(function AttachmentList({
  pendingFiles,
  attachments,
  sessionId,
  onRetry,
  onRemovePending,
  onSetReferenceRole,
  onRemoveAttachment,
}: {
  pendingFiles: PendingAttachment[];
  attachments: Attachment[];
  sessionId: string | null;
  onRetry: (id: string) => void;
  onRemovePending: (id: string) => void;
  onSetReferenceRole: (id: string, role: Attachment['referenceRole']) => void;
  onRemoveAttachment: (id: string) => void;
}) {
  return (
    <ul className="fs-studio__attachments" aria-label={t('Attachments')}>
      {pendingFiles.map((entry) => (
        <li key={entry.id} className="fs-studio__attachment" data-state={entry.state} data-testid="studio-pending-attachment">
          {entry.preview ? <img src={entry.preview} alt="" width={36} height={36} /> : <FileText size={16} aria-hidden="true" />}
          <span className="fs-studio__attachment-info">
            <span className="fs-studio__attachment-name" title={entry.file.name}>{entry.file.name || t('Screenshot')}</span>
            <span role={entry.state === 'failed' ? 'alert' : 'status'} className="fs-studio__attachment-status">
              {entry.state === 'failed' ? entry.error : entry.state === 'queued' ? t('Waiting to upload…') : t('Uploading…')}
            </span>
          </span>
          {entry.state === 'failed' && <button type="button" className="fs-studio__attachment-x" aria-label={t('Retry {name}', {name:entry.file.name})} onClick={() => onRetry(entry.id)}><RefreshCw size={13} aria-hidden="true" /></button>}
          <button type="button" className="fs-studio__attachment-x" aria-label={t('Remove {name}', {name:entry.file.name})} onClick={() => onRemovePending(entry.id)}><X size={12} aria-hidden="true" /></button>
        </li>
      ))}
      {attachments.map((a) => (
        <li key={a.id} className="fs-studio__attachment" data-image={isImage(a.mime)||undefined} data-partial={a.partial||undefined} data-testid="studio-attachment">
          {isImage(a.mime) ? (
            <a href={`/library/edit?attachment=${encodeURIComponent(a.id)}&name=${encodeURIComponent(a.name)}${sessionId ? `&chat=${encodeURIComponent(sessionId)}` : ''}`} target="_blank" rel="noopener noreferrer"
              className="fs-studio__attachment-edit" aria-label={t('Edit image and masks: {name}', {name:a.name})}
              title={t('Open the image editor in another tab. Your chat draft stays here.')}>
              <img src={attachmentUrl(a.id)} alt="" width={36} height={36} />
            </a>
          ) : (
            <FileText size={16} aria-hidden="true" />
          )}
          <span className="fs-studio__attachment-info">
            <span className="fs-studio__attachment-name" title={a.name}>{a.name}</span>
            {a.partial && (
              <span className="fs-studio__attachment-status" data-testid="studio-attachment-partial">
                {a.partialReason === 'scanned' ? t('Scanned PDF — only the pages, not searchable text, were extracted.')
                  : a.partialReason === 'cover_only' ? t('Only the cover page could be extracted.')
                  : t('Only partially extracted.')}
              </span>
            )}
            {isImage(a.mime) && <select className="fs-studio__reference-role"
              aria-label={t('Reference role for {name}', {name:a.name})}
              title={t('Adds visible guidance to your message. The image model determines how closely it can follow it.')}
              value={a.referenceRole || ''}
              onChange={event=>{const role=REFERENCE_ROLES.find(role=>role.value===event.target.value)?.value;
                onSetReferenceRole(a.id, role);}}
            ><option value="">{t('Attachment only')}</option>{REFERENCE_ROLES.map(role=><option key={role.value} value={role.value}>{t(role.label)}</option>)}</select>}
          </span>
          <button
            type="button"
            className="fs-studio__attachment-x"
            aria-label={t('Remove {name}', { name: a.name })}
            onClick={() => onRemoveAttachment(a.id)}
          >
            <X size={12} aria-hidden="true" />
          </button>
        </li>
      ))}
    </ul>
  );
});

// TASK-06: per-turn autonomy budget preset (src/autonomy_budget.py). Unlike
// ApprovalSelector this is NOT a saved setting — it travels with this one
// turn via `Knobs.autonomyPreset` / `SendOptions.autonomyPreset`, exactly
// like `plan`/`bash` above, so switching it never affects another chat.
export const AUTONOMY_PRESET_CHOICES: { value: AutonomyPreset; label: string; detail: string }[] = [
  { value: 'supervised', label: t('Supervised'),
    detail: t('Asks before anything with an external or destructive effect; medium budgets.') },
  { value: 'bounded_autonomous', label: t('Bounded autonomous'),
    detail: t('Runs without asking except for irreversible effects; higher budgets.') },
  { value: 'read_only', label: t('Read only'),
    detail: t('Only read tools are offered — nothing changes on disk or anywhere else; low budgets.') },
];

function AutonomyPresetSelector({ preset, onPick }: { preset: AutonomyPreset; onPick: (value: AutonomyPreset) => void }) {
  const current = AUTONOMY_PRESET_CHOICES.find((c) => c.value === preset) ?? AUTONOMY_PRESET_CHOICES[0];
  return (
    <Popover
      side="top"
      className="fs-studio__permission-menu"
      trigger={
        <button type="button" className="fs-studio__chip" data-autonomy={preset} data-testid="studio-autonomy-preset">
          <Gauge size={14} aria-hidden="true" /> {current.label}
        </button>
      }
    >
      <p>{t('How far this turn may go before it must stop and check in.')}</p>
      <div role="radiogroup" aria-label={t('Autonomy')}>
        {AUTONOMY_PRESET_CHOICES.map((choice) => (
          <button
            key={choice.value}
            type="button"
            role="radio"
            aria-checked={preset === choice.value}
            onClick={() => onPick(choice.value)}
            data-testid={`studio-autonomy-preset-${choice.value}`}
          >
            <strong>{choice.label}</strong>
            <span>{choice.detail}</span>
          </button>
        ))}
      </div>
    </Popover>
  );
}

// CMP-09: fast/balanced/deep_review — src/strategy_policy.py's own
// PROFILES/_PROFILE_MULTIPLIERS. Same shape as AUTONOMY_PRESET_CHOICES
// right above: label + a one-line, user-facing consequence, not a
// number. Persisted server-side (see adapters/strategy.ts), so switching
// here changes the NEXT turn's strategy without touching this one.
export const STRATEGY_PROFILE_CHOICES: { value: StrategyProfile; label: string; detail: string }[] = [
  { value: 'fast', label: t('Fast'),
    detail: t('Fewer steps, smaller budget — for small, well-defined tasks.') },
  { value: 'balanced', label: t('Balanced'),
    detail: t('The default: a normal plan/verify cycle.') },
  { value: 'deep_review', label: t('Deep review'),
    detail: t('Adds an explicit review step and a larger budget — for anything worth double-checking.') },
];

function StrategyProfileSelector({ profile, onPick }: { profile: StrategyProfile; onPick: (value: StrategyProfile) => void }) {
  const current = STRATEGY_PROFILE_CHOICES.find((c) => c.value === profile) ?? STRATEGY_PROFILE_CHOICES[1];
  return (
    <Popover
      side="top"
      className="fs-studio__permission-menu"
      trigger={
        <button type="button" className="fs-studio__chip" data-strategy-profile={profile} data-testid="studio-strategy-profile">
          <Layers size={14} aria-hidden="true" /> {current.label}
        </button>
      }
    >
      <p>{t('How carefully the agent works this turn. Never changes WHAT kind of task it thinks this is — only how much budget and review it gets.')}</p>
      <div role="radiogroup" aria-label={t('Strategy profile')}>
        {STRATEGY_PROFILE_CHOICES.map((choice) => (
          <button
            key={choice.value}
            type="button"
            role="radio"
            aria-checked={profile === choice.value}
            onClick={() => onPick(choice.value)}
            data-testid={`studio-strategy-profile-${choice.value}`}
          >
            <strong>{choice.label}</strong>
            <span>{choice.detail}</span>
          </button>
        ))}
      </div>
    </Popover>
  );
}

// CMP-12: which recipe (src/recipes.py) injects its structured procedure
// into this turn, if any. `null` means "no recipe — the strategy's generic
// steps for whatever method gets chosen".
function RecipeSelector({
  recipeId, recipes, onPick,
}: { recipeId: string | null; recipes: Recipe[]; onPick: (value: string | null) => void }) {
  const current = recipes.find((r) => r.id === recipeId) ?? null;
  return (
    <Popover
      side="top"
      className="fs-studio__permission-menu"
      trigger={
        <button type="button" className="fs-studio__chip" data-recipe={recipeId ?? ''} data-testid="studio-recipe-selector">
          <BookOpen size={14} aria-hidden="true" /> {current ? current.title : t('No recipe')}
        </button>
      }
    >
      <p>{t('A recipe injects a fixed procedure for this turn instead of the default strategy.')}</p>
      <div role="radiogroup" aria-label={t('Recipe')}>
        <button
          type="button"
          role="radio"
          aria-checked={!recipeId}
          onClick={() => onPick(null)}
          data-testid="studio-recipe-none"
        >
          <strong>{t('No recipe')}</strong>
          <span>{t('Use the default strategy for this task.')}</span>
        </button>
        {recipes.map((recipe) => (
          <button
            key={recipe.id}
            type="button"
            role="radio"
            aria-checked={recipeId === recipe.id}
            onClick={() => onPick(recipe.id)}
            data-testid={`studio-recipe-${recipe.id}`}
          >
            <strong>{recipe.title}{recipe.status === 'draft' ? ` (${t('draft')})` : ''}</strong>
            <span>{recipe.steps.slice(0, 2).join(' · ') || t('No steps recorded.')}</span>
          </button>
        ))}
      </div>
    </Popover>
  );
}

/**
 * TASK-04/UX-04: the busy-state Stop button becomes a menu — three scopes,
 * each with a one-line consequence, plus "Dirigir…" (steer the live turn)
 * and "Enviar después" (queue for once it ends, never touching the live
 * turn). Any of the five callbacks being undefined only removes that one
 * row, so a caller that still passes just `onStop` gets a menu with a
 * single, familiar action rather than a crash.
 */
function StopMenu({
  onStop,
  onPauseGeneration,
  onCancelTask,
  onCancelWork,
  onSteer,
  onQueueSend,
}: {
  onStop: () => void;
  onPauseGeneration?: () => void;
  onCancelTask?: () => void;
  onCancelWork?: () => void;
  onSteer?: (text: string) => void;
  onQueueSend?: (text: string) => void;
}) {
  const [note, setNote] = useState('');
  const [noteMode, setNoteMode] = useState<'steer' | 'queue' | null>(null);
  const submitNote = () => {
    const text = note.trim();
    if (!text || !noteMode) return;
    if (noteMode === 'steer') onSteer?.(text);
    else onQueueSend?.(text);
    setNote('');
    setNoteMode(null);
  };
  return (
    <Popover
      side="top"
      className="fs-studio__stop-menu"
      testId="studio-stop-menu"
      trigger={<IconButton icon={Square} label={t('Stop')} testId="studio-stop" />}
    >
      <div role="menu" aria-label={t('Stop')}>
        <button type="button" role="menuitem" onClick={() => (onPauseGeneration ?? onStop)()}>
          <strong>{t('Stop generation')}</strong>
          <span>{t('Ends the current reply; the turn stays paused and you can continue it.')}</span>
        </button>
        <button type="button" role="menuitem" onClick={() => (onCancelTask ?? onStop)()}>
          <strong>{t('Cancel task')}</strong>
          <span>{t('Ends the turn for good and stops any sub-agents it started.')}</span>
        </button>
        <button type="button" role="menuitem" onClick={() => (onCancelWork ?? onStop)()}>
          <strong>{t('Cancel all work')}</strong>
          <span>{t('Cancels the task and this chat’s background jobs too.')}</span>
        </button>
      </div>
      {(onSteer || onQueueSend) && (
        <div className="fs-studio__stop-menu-note">
          {noteMode ? (
            <>
              <textarea
                autoFocus
                rows={2}
                value={note}
                onChange={(event) => setNote(event.target.value)}
                placeholder={noteMode === 'steer' ? t('Tell it something while it keeps working…') : t('Send once this turn is done…')}
                data-testid="studio-stop-menu-note"
              />
              <div className="fs-studio__stop-menu-note-actions">
                <button type="button" onClick={() => setNoteMode(null)}>{t('Cancel')}</button>
                <button type="button" onClick={submitNote} disabled={!note.trim()}>{t('Send')}</button>
              </div>
            </>
          ) : (
            <>
              {onSteer && (
                <button type="button" role="menuitem" onClick={() => setNoteMode('steer')}>
                  <strong>{t('Direct it…')}</strong>
                  <span>{t('Add an instruction it picks up before its next step.')}</span>
                </button>
              )}
              {onQueueSend && (
                <button type="button" role="menuitem" onClick={() => setNoteMode('queue')}>
                  <strong>{t('Send after')}</strong>
                  <span>{t('Queues a message for once this turn finishes.')}</span>
                </button>
              )}
            </>
          )}
        </div>
      )}
    </Popover>
  );
}

function ApprovalSelector({ disabled, onNotice }: { disabled: boolean; onNotice: ComposerProps['onNotice'] }) {
  const [mode, setMode] = useState('ask');
  const [ready, setReady] = useState(false);
  const [saving, setSaving] = useState(false);
  useEffect(() => { let live = true; void getSettings().then(s => { if (live) { setMode(String(s.tool_approval_mode ?? 'ask')); setReady(true); } }).catch(() => {}); return () => { live = false; }; }, []);
  const choices = [
    { value: 'ask', label: t('Ask for approval'), detail: t('Confirm desktop input and actions that require approval.') },
    { value: 'auto', label: t('Automatic approval'), detail: t('Allow routine desktop actions; ask when other approval checks require it.') },
    { value: 'full', label: t('No confirmations'), detail: t('Run enabled tools without approval prompts. Tool and account restrictions still apply.') },
  ];
  const choose = async (value: string) => {
    setSaving(true);
    try { await saveSettings({ tool_approval_mode: value }); setMode(value); }
    catch (e) { onNotice((e as Error).message, 'danger'); }
    finally { setSaving(false); }
  };
  return <Popover side="top" className="fs-studio__permission-menu" trigger={<button type="button" className="fs-studio__chip" data-permission={mode} disabled={!ready || disabled || saving}><Shield size={14} />{choices.find(c => c.value === mode)?.label ?? t('Ask for approval')}</button>}>
    <p>{t('Approval mode for all Faustus chats. Only an administrator can change it.')}</p>
    <div role="radiogroup" aria-label={t('Tool approvals')}>
      {choices.map(choice => <button key={choice.value} type="button" role="radio" aria-checked={mode === choice.value} disabled={saving || disabled} onClick={() => void choose(choice.value)}><strong>{choice.label}</strong><span>{choice.detail}</span></button>)}
    </div>
  </Popover>;
}
