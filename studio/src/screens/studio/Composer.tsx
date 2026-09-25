import { getSettings, saveSettings } from '../../adapters/settings';
import { t, tn, useLang } from '../../i18n';
import { usePlatform } from '../../shell/platform';
import { Link } from 'react-router';
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
  Pin,
  Plug,
  Plus,
  Shield,
  Loader2,
  RefreshCw,
  SlidersHorizontal,
  Square,
  Terminal,
  Theater,
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
  genEffectiveValue,
  genFieldSource,
  genWithOverride,
  genWithoutOverride,
  isImage,
  searchWorkspaceFiles,
  supportsThinking,
  THINK_MODES,
  thinkModeChipText,
  thinkModeLabel,
  reasoningLevelLabel,
  uploadFiles,
  type Attachment,
  type GenOverrides,
  type SamplingDefaults,
  type ThinkMode,
  type WorkspaceFile,
} from '../../adapters/composer';
import type { Suggestion } from './commands';
import { capMentionItems, resolveSuggestionIntent } from './composer-suggest';
import { frameBatcher } from '../../lib/frame-batch';
import { clipboardFiles, insertPastedText } from '../../lib/clipboard-attachments';
import { createFileDropSession, isFileDrag } from '../../lib/file-drop';
import {REFERENCE_ROLES} from '../../lib/image-references';
import {MediaRecipes} from './MediaRecipes';
import { createAttachmentUploads, type PendingAttachment } from '../../lib/attachment-uploads';
import type { ContextOverrides, DocContextRef } from '../../adapters/chat';
import { ContextPanel, pruneOverrides } from './ContextPanel';
import { COMPOSER_CONTEXT_EVENT, type ComposerContextDetail } from '../../lib/docSession';
import { addMaterial } from '../../adapters/sideThreads';
import { modeDescription, modeLabel, type BehaviorMode } from '../../adapters/behaviorModes';
import { getSessionConnectors, getSessionToolSupport, setSessionConnectors, type ConnectorSelection, type ToolSupport } from '../../adapters/sessions';
import { ConnectorPicker } from '../connectors/ConnectorPicker';

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
  /** CMP-03/W3-A: document context chips still attached when this turn is
   *  sent (`COMPOSER_CONTEXT_EVENT`) — forwarded to sendTurn() as
   *  `docContext` exactly like `contextOverrides` above; see
   *  `adapters/chat.ts`'s `DocContextRef` doc comment for the wire shape
   *  and the one wiring step (Studio.tsx forwarding it into `sendTurn`)
   *  this lot leaves for the orchestrator, same gap that doc comment names. */
  docContext?: DocContextRef[];
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
  /** CMP-GEN: a control in the sampling panel changing one field, or the
   *  slash commands (`/temp`, `/gen`…) — same state, same setter. */
  onSetGen: (update: GenOverrides) => void;
  /** The active model name, so the panel knows whether to show the think
   *  switch (`supportsThinking`, adapters/composer.ts) — `null` while the
   *  route has not resolved yet. */
  modelName: string | null;
  /** Lot T: this chat's reasoning mode and what Auto chose on the last
   *  turn (null before any). The chip shows only for a thinking model. */
  thinkMode?: ThinkMode;
  thinkChosen?: ThinkMode | null;
  onSetThinkMode?: (mode: ThinkMode) => void;
  /** The reasoning levels the current model's template accepts (may be empty). */
  reasoningLevels?: string[];
  /** This chat's explicit level, or null to follow the mode. */
  thinkEffort?: string | null;
  onSetThinkEffort?: (effort: string | null) => void;
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
  /** CONTRATO_MODOS Lote B: the catalog (Studio.tsx fetches it once,
   *  "Default" first — `src/behavior_modes.py::list_modes`'s own order) and
   *  this conversation's own active id — `null` only while it has not
   *  loaded yet. Picking one calls `onPickBehaviorMode`; Studio.tsx owns
   *  what that actually does (`setSessionMode` for an existing session, a
   *  pending pick applied to the very first turn otherwise) — this
   *  component never touches the session's mode on its own, in a
   *  `useEffect` or anywhere else. */
  behaviorModes: BehaviorMode[];
  behaviorModeId: string | null;
  onPickBehaviorMode: (id: string) => void;
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

/** How long typing must pause before the draft is handed up to Studio.
 *  Short enough that a reload or a conversation switch a moment later keeps
 *  the text, long enough that a burst of typing costs one screen render
 *  instead of one per character. */
const DRAFT_PUSH_MS = 250;

/** Sync size gate — runs before the chip appears so huge files never flash. */
function sizeIncompatibility(file: File): string | null {
  if (file.size > MAX_ATTACHMENT_BYTES) {
    return t('{name} is too large ({size} MB) to attach.', { name: file.name, size: Math.round(file.size / 1024 / 1024) });
  }
  return null;
}

/** Async media-backend gate — never delays the pending preview chip. */
async function mediaIncompatibility(file: File): Promise<string | null> {
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
  draft: draftProp,
  setDraft: setDraftProp,
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
  onSetGen,
  modelName,
  thinkMode = 'auto',
  thinkChosen = null,
  onSetThinkMode,
  reasoningLevels = [],
  thinkEffort = null,
  onSetThinkEffort,
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
  behaviorModes,
  behaviorModeId,
  onPickBehaviorMode,
  presetChip,
  extraControls,
  lastSent,
  textareaRef,
}: ComposerProps) {
  /* ── The draft is typed here and only then handed up ──────────────────
   * 20-09-2026: the textarea was controlled straight from Studio's own
   * state, so every keystroke re-rendered the whole screen — transcript
   * included. In a long conversation that is tens of milliseconds per
   * character, and typing a paragraph into one froze the tab for half a
   * minute. The text now lives here (one small component re-renders per
   * keystroke) and is pushed up when typing pauses, when focus leaves, and
   * always before a send — so everything that reads `draft` up there (the
   * outbox, the model-missing warning that puts the text back, ?draft=)
   * still sees it. A draft set from outside (a quote, ↑, dictation) flows
   * back down through the effect below. */
  const [draft, setLocalDraft] = useState(draftProp);
  const draftRef = useRef(draftProp);
  const pushedRef = useRef(draftProp);
  const pushTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const setDraftPropRef = useRef(setDraftProp);
  setDraftPropRef.current = setDraftProp;

  const flushDraft = useCallback(() => {
    if (pushTimer.current !== null) {
      clearTimeout(pushTimer.current);
      pushTimer.current = null;
    }
    if (draftRef.current !== pushedRef.current) {
      pushedRef.current = draftRef.current;
      setDraftPropRef.current(draftRef.current);
    }
  }, []);

  const setDraft = useCallback((value: string) => {
    draftRef.current = value;
    setLocalDraft(value);
    if (pushTimer.current !== null) clearTimeout(pushTimer.current);
    pushTimer.current = setTimeout(flushDraft, DRAFT_PUSH_MS);
  }, [flushDraft]);

  useEffect(() => {
    if (draftProp === pushedRef.current) return;
    pushedRef.current = draftProp;
    draftRef.current = draftProp;
    setLocalDraft(draftProp);
  }, [draftProp]);

  // Never lose what is typed: hand it up before this composer goes away
  // (another conversation, a layout change).
  useEffect(() => () => flushDraft(), [flushDraft]);

  const fileInputRef = useRef<HTMLInputElement>(null);
  const platform = usePlatform();
  const [pendingFiles, setPendingFiles] = useState<PendingAttachment[]>([]);
  const attachmentTarget = useRef({sessionId, setAttachments});
  attachmentTarget.current = {sessionId, setAttachments};
  const uploads = useMemo(() => createAttachmentUploads<Attachment>({
    upload: (file, signal, onProgress) => uploadFiles([file], sessionId, signal, onProgress),
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
  const dropSession = useMemo(() => createFileDropSession(setDragging), []);
  useEffect(() => () => dropSession.dispose(), [dropSession]);

  /* ── CMP-03/W3-A: document context chips ──
   * `docSession.ts`'s `sendComposerContext` (used today by `SidePanel.tsx`'s
   * "Sobre esta selección…" and "enviar comentarios seleccionados") dispatches
   * `COMPOSER_CONTEXT_EVENT` for the composer to pick up — that module's own
   * doc comment says a listener "may not exist yet... wired up in whichever
   * lot lands it"; this is that lot. Shown as a removable chip; the quoted
   * text is NEVER written into `draft` as though the human had typed it — it
   * travels with the turn as its own field (`Knobs.docContext`, kept in sync
   * below), the exact posture `contextOverrides` already has. */
  const [docContext, setDocContext] = useState<ComposerContextDetail[]>([]);
  // A conversation switch leaves any attached context behind too — it was
  // "context for the next message in THIS chat", not a global clipboard.
  useEffect(() => { setDocContext([]); }, [sessionId]);
  useEffect(() => {
    const onContext = (event: Event) => {
      const detail = (event as CustomEvent<ComposerContextDetail>).detail;
      if (detail) setDocContext((list) => [...list, detail]);
    };
    window.addEventListener(COMPOSER_CONTEXT_EVENT, onContext);
    return () => window.removeEventListener(COMPOSER_CONTEXT_EVENT, onContext);
  }, []);
  const removeDocContext = (index: number) => setDocContext((list) => list.filter((_, i) => i !== index));
  // F2 (CONTRATO_CABLES2): "Fijar" turns this transient, single-turn chip
  // into a material wired PERMANENTLY into the session (`POST .../
  // materials`) — never shown when the chat has no session yet (nothing to
  // wire it into).
  const [pinningIndex, setPinningIndex] = useState<number | null>(null);
  const pinDocContext = (index: number) => {
    const item = docContext[index];
    if (!sessionId || !item || pinningIndex !== null) return;
    const quotes = item.items.map((it) => it.quote).filter(Boolean);
    setPinningIndex(index);
    addMaterial(sessionId, { kind: 'document', documentId: item.doc.id, depth: 'selection', quotes, ranges: item.ranges })
      .then(() => {
        removeDocContext(index);
        onNotice(t('Pinned as a context wire.'));
      })
      .catch((e: Error) => onNotice(`${t('Could not pin this context.')} ${e.message}`, 'danger'))
      .finally(() => setPinningIndex(null));
  };
  useEffect(() => {
    setKnobs((k) => ({
      ...k,
      docContext: docContext.length
        ? docContext.map((d) => ({ docId: d.doc.id, docTitle: d.doc.title, ranges: d.ranges, action: d.action, quotes: d.items.map((it) => it.quote) }))
        : undefined,
    }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [docContext]);
  /** A sent turn has consumed its attached context — cleared here so the
   *  next message starts from nothing, exactly like attachments do. */
  const trySend = () => {
    if (uploads.hasPending()) return;
    flushDraft();
    // `draftRef`, not `draft`: the ref is written synchronously on every
    // keystroke while the state behind `draft` is a render behind. Typing
    // quickly and pressing Enter in the same tick therefore sent the PREVIOUS
    // value -- empty, on the first message of a conversation, so the turn
    // silently never started and the composer just cleared itself.
    const current = draftRef.current;
    if (busy && onSteer) {
      const text = current.trim();
      if (!text) return;
      onSteer(text);
      return;
    }
    onSend(current);
    if (docContext.length) setDocContext([]);
  };

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
  // Queue (and show the preview chip) on the same turn as paste/drop; only
  // the sync size gate runs first. Media-backend checks stay async and fail
  // the pending chip afterward — never hold the UI blank while
  // `/api/media/capabilities` loads.
  const addFiles = (files: File[]) => {
    const accepted: File[] = [];
    for (const file of files) {
      const reason = sizeIncompatibility(file);
      if (reason) onNotice(reason, 'warning');
      else accepted.push(file);
    }
    if (!accepted.length) return;
    uploads.add(accepted);
    void (async () => {
      for (const file of accepted) {
        const reason = await mediaIncompatibility(file);
        if (reason) {
          uploads.failFile(file, reason);
          onNotice(reason, 'warning');
        }
      }
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
    dropSession.drop();
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
      trySend();
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
        trySend();
      }}
      onDragEnter={(event) => {
        if (isFileDrag(event.dataTransfer?.types)) event.preventDefault();
        dropSession.enter(event.dataTransfer?.types);
      }}
      onDragOver={(event) => {
        if (!isFileDrag(event.dataTransfer?.types)) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = 'copy';
      }}
      onDragLeave={() => dropSession.leave()}
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

      {docContext.length > 0 && (
        <ul className="fs-studio__context-chips" aria-label={t('Document context for this message')} data-testid="composer-doc-context">
          {docContext.map((item, i) => {
            const quote = item.items[0]?.quote ?? '';
            const short = quote.length > 80 ? `${quote.slice(0, 80)}…` : quote;
            return (
              <li key={i} className="fs-studio__context-chip" data-testid="composer-doc-context-chip">
                <FileText size={12} aria-hidden="true" />
                <span className="fs-studio__context-chip-name" title={item.doc.title}>{item.doc.title}</span>
                {short && <span className="fs-sa__muted">“{short}”</span>}
                {sessionId && (
                  <button
                    type="button"
                    className="fs-studio__chip-x"
                    aria-label={t('Keep in context until you withdraw it')}
                    title={t('Keep in context until you withdraw it')}
                    disabled={pinningIndex !== null}
                    onClick={() => pinDocContext(i)}
                    data-testid="doc-context-pin"
                  >
                    <Pin size={11} aria-hidden="true" />
                  </button>
                )}
                <button
                  type="button"
                  className="fs-studio__chip-x"
                  aria-label={t('Remove context: {name}', { name: item.doc.title })}
                  onClick={() => removeDocContext(i)}
                >
                  <X size={11} aria-hidden="true" />
                </button>
              </li>
            );
          })}
        </ul>
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
              ? platform === 'mobile'
                ? t('Tell me what you want done…')
                : t('Tell me what you want done…  @file · #rule · /command')
              : t('Write a message…  /command')
        }
        aria-label={t('Message')}
        onChange={onChange}
        onKeyDown={onKey}
        onPaste={onPaste}
        onBlur={flushDraft}
        onClick={(event) => refreshSuggestions(draft, event.currentTarget.selectionStart ?? draft.length)}
        data-testid="studio-input"
      />
      <p className="fs-studio__drop-hint" data-active={dragging || undefined} aria-hidden={!dragging}>
        {t('Drop to attach')}
      </p>

      <div className="fs-studio__bar">
        <div className="fs-studio__bar-start">
        <Popover placement="composer" className="fs-studio__add-menu" testId="studio-add-menu" trigger={<IconButton icon={Plus} size="sm" label={t('Add files and tools')} testId="studio-add" />}>
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
              <Popover placement="composer" className="fs-media-recipes" trigger={<button type="button" className="fs-studio__chip" aria-pressed={Boolean(knobs.noSkills||knobs.inputTokenBudget)}><SlidersHorizontal size={13}/>{t('Agent context')}</button>}>
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
          {onSetThinkMode && supportsThinking(modelName) && (
            <ThinkModeChip mode={thinkMode} chosen={thinkChosen} onPick={onSetThinkMode}
              levels={reasoningLevels} effort={thinkEffort} onPickEffort={onSetThinkEffort} />
          )}
          <span className="fs-studio__chipgroup">
            <GenSettingsPopover gen={gen} onSetGen={onSetGen} modelName={modelName} genLabel={genLabel} />
            {genLabel && (
              <button type="button" className="fs-studio__chip-x" aria-label={t('Remove the generation settings')} onClick={onClearGen}>
                <X size={11} aria-hidden="true" />
              </button>
            )}
          </span>
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
            <MessageSquare size={13} aria-hidden="true" /> <span className="fs-studio__seg-label">{t('Chat')}</span>
          </button>
          <button
            type="button"
            role="radio"
            aria-checked={knobs.mode === 'agent'}
            onClick={() => setKnobs((k) => ({ ...k, mode: 'agent' }))}
            data-testid="studio-mode-agent"
          >
            <Bot size={13} aria-hidden="true" /> <span className="fs-studio__seg-label">{t('Agent')}</span>
          </button>
        </div>
        </div>
        <div className="fs-studio__bar-tools">
        {knobs.mode === 'agent' && (
          <AutonomyPresetSelector
            preset={knobs.autonomyPreset ?? 'supervised'}
            onPick={(value) => setKnobs((k) => ({ ...k, autonomyPreset: value }))}
          />
        )}
        <BehaviorModeSelector modes={behaviorModes} activeId={behaviorModeId} onPick={onPickBehaviorMode} />
        <SessionConnectorsSelector sessionId={sessionId} />
        <ApprovalSelector disabled={busy} onNotice={onNotice} />
        <StrategyProfileSelector profile={strategyProfile} onPick={pickStrategyProfile} />
        <RecipeSelector recipeId={activeRecipeId} recipes={recipes} onPick={pickRecipe} />
        </div>
        <div className="fs-studio__bar-end">
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
          {onVoice && <IconButton icon={AudioLines} size="sm" label={t(voiceActive ? 'Close voice mode' : 'Talk to Faustus')} onClick={onVoice} testId="studio-voice" />}
        <div className="fs-studio__send">
          {busy && canSend && onSteer && (
            <button type="button" className="fs-studio__go" onClick={trySend} aria-label={t('Send')} data-testid="studio-steer-send">
              <ArrowUp size={16} aria-hidden="true" />
            </button>
          )}
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
              <ArrowUp size={16} aria-hidden="true" />
            </button>
          )}
        </div>
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
      {pendingFiles.map((entry) => {
        const pct = entry.state === 'uploading' && typeof entry.progress === 'number'
          ? Math.round(entry.progress * 100)
          : null;
        return (
        <li key={entry.id} className="fs-studio__attachment" data-state={entry.state} data-testid="studio-pending-attachment">
          <span className="fs-studio__attachment-thumb">
            {entry.preview ? <img src={entry.preview} alt="" width={36} height={36} /> : <FileText size={16} aria-hidden="true" />}
            {entry.state !== 'failed' && (
              <span className="fs-studio__attachment-spinner" aria-hidden="true">
                <Loader2 size={14} className="fs-spin" />
                {pct !== null && <span className="fs-studio__attachment-pct">{pct}%</span>}
              </span>
            )}
          </span>
          <span className="fs-studio__attachment-info">
            <span className="fs-studio__attachment-name" title={entry.file.name}>{entry.file.name || t('Screenshot')}</span>
            <span role={entry.state === 'failed' ? 'alert' : 'status'} className="fs-studio__attachment-status">
              {entry.state === 'failed' ? entry.error : entry.state === 'queued' ? t('Waiting to upload…') : pct !== null ? t('Uploading {pct}%…', { pct }) : t('Uploading…')}
            </span>
          </span>
          {entry.state === 'failed' && <button type="button" className="fs-studio__attachment-x" aria-label={t('Retry {name}', {name:entry.file.name})} onClick={() => onRetry(entry.id)}><RefreshCw size={13} aria-hidden="true" /></button>}
          <button type="button" className="fs-studio__attachment-x" aria-label={t('Remove {name}', {name:entry.file.name})} onClick={() => onRemovePending(entry.id)}><X size={12} aria-hidden="true" /></button>
        </li>
        );
      })}
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
      placement="composer"
      className="fs-studio__permission-menu"
      trigger={
        <button type="button" className="fs-studio__chip fs-studio__chip--compact" data-autonomy={preset} data-testid="studio-autonomy-preset" title={current.label} aria-label={t('Autonomy: {label}', { label: current.label })}>
          <Gauge size={14} aria-hidden="true" /> <span className="fs-studio__chip-label">{current.label}</span>
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

// CONTRATO_MODOS Lote B: the behaviour-mode chip. `activeId` is `null` only
// while Studio.tsx has not fetched anything yet — the trigger falls back to
// `t('Default')` for that split second rather than an empty chip. Picking a
// row only ever calls `onPick`; this component never calls `setSessionMode`
// (or anything else that persists a mode) on its own, and never from a
// `useEffect` — see `behaviorModeChip`'s `data-testid` for what the check
// script anchors the "never changes on its own" grep to.
function BehaviorModeSelector({
  modes, activeId, onPick,
}: { modes: BehaviorMode[]; activeId: string | null; onPick: (id: string) => void }) {
  const lang = useLang();
  const active = modes.find((m) => m.id === activeId) ?? null;
  // Controlled so a pick closes the menu (seen live: it stayed open over the
  // composer after choosing, and the next click landed on another option).
  const [open, setOpen] = useState(false);
  return (
    <Popover
      placement="composer"
      className="fs-studio__permission-menu"
      open={open}
      onOpenChange={setOpen}
      trigger={
        <button
          type="button"
          className="fs-studio__chip fs-studio__chip--compact"
          data-mode={activeId ?? ''}
          data-testid="behavior-mode-chip"
          title={t('Behaviour mode: {label}', { label: modeLabel(active, lang) })}
          aria-label={t('Behaviour mode: {label}', { label: modeLabel(active, lang) })}
        >
          <Theater size={14} aria-hidden="true" /> <span className="fs-studio__chip-label">{modeLabel(active, lang)}</span>
        </button>
      }
    >
      <p>{t('How Faustus argues in this conversation — never what it is allowed to do. The content policy, the agent rules and your own instructions always win.')}</p>
      <div role="radiogroup" aria-label={t('Behaviour mode')} data-testid="behavior-mode-options">
        {modes.map((mode) => (
          <button
            key={mode.id}
            type="button"
            role="radio"
            aria-checked={activeId === mode.id}
            onClick={() => { onPick(mode.id); setOpen(false); }}
            data-testid={`behavior-mode-option-${mode.id}`}
          >
            <strong>{modeLabel(mode, lang)}</strong>
            <span>{modeDescription(mode, lang)}</span>
          </button>
        ))}
      </div>
      <Link to="/settings?s=modes" className="fs-studio__permission-link">
        {t('Manage modes…')}
      </Link>
    </Popover>
  );
}

// CONTRATO_CONECTORES Lote F3: which connectors (F2's `connector_ids`) this
// conversation may use. There is nothing to persist to before the first
// message creates a session — `sessionId === null` hides the chip rather
// than faking a selection that has nowhere to be saved yet, the same way
// the picker in the composer bar for a brand-new chat simply is not shown
// for anything else that needs a session id.
function SessionConnectorsSelector({ sessionId }: { sessionId: string | null }) {
  const [selection, setSelection] = useState<ConnectorSelection | null>(null);
  const [toolSupport, setToolSupport] = useState<ToolSupport | null>(null);
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!sessionId) {
      setSelection(null);
      setToolSupport(null);
      return;
    }
    let live = true;
    getSessionConnectors(sessionId).then((s) => { if (live) setSelection(s); }).catch(() => { if (live) setSelection(null); });
    getSessionToolSupport(sessionId).then((s) => { if (live) setToolSupport(s); }).catch(() => { if (live) setToolSupport(null); });
    return () => { live = false; };
  }, [sessionId]);

  if (!sessionId) return null;

  const count = selection?.connector_ids ? selection.connector_ids.length : null;
  const label = count === null ? t('All connectors') : count === 0 ? t('No connectors') : tn(count, '{n} connector', '{n} connectors');

  return (
    <Popover
      placement="composer"
      className="fs-studio__permission-menu"
      open={open}
      onOpenChange={setOpen}
      trigger={
        <button
          type="button"
          className="fs-studio__chip fs-studio__chip--compact"
          data-testid="session-connectors-chip"
          title={t('Connectors for this conversation')}
          aria-label={t('Connectors for this conversation')}
        >
          <Plug size={14} aria-hidden="true" /> <span className="fs-studio__chip-label">{label}</span>
        </button>
      }
    >
      <ConnectorPicker
        value={selection?.connector_ids ?? null}
        effective={selection?.effective}
        source={selection?.source}
        toolSupport={toolSupport}
        disabled={saving}
        inheritLabel={t('Inherit from project')}
        onChange={(next) => {
          setSaving(true);
          setSessionConnectors(sessionId, next)
            .then(setSelection)
            .catch(() => undefined)
            .finally(() => setSaving(false));
        }}
      />
      <Link to="/connectors" className="fs-studio__permission-link">
        {t('Manage connectors…')}
      </Link>
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
      placement="composer"
      className="fs-studio__permission-menu"
      trigger={
        <button type="button" className="fs-studio__chip fs-studio__chip--compact" data-strategy-profile={profile} data-testid="studio-strategy-profile" title={current.label} aria-label={t('Strategy profile: {label}', { label: current.label })}>
          <Layers size={14} aria-hidden="true" /> <span className="fs-studio__chip-label">{current.label}</span>
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
  const [open, setOpen] = useState(false);
  return (
    <Popover
      placement="composer"
      className="fs-studio__permission-menu"
      open={open}
      onOpenChange={setOpen}
      trigger={
        <button type="button" className="fs-studio__chip fs-studio__chip--compact" data-recipe={recipeId ?? ''} data-testid="studio-recipe-selector" title={current ? current.title : t('No recipe')} aria-label={t('Recipe: {label}', { label: current ? current.title : t('No recipe') })}>
          <BookOpen size={14} aria-hidden="true" /> <span className="fs-studio__chip-label">{current ? current.title : t('No recipe')}</span>
        </button>
      }
    >
      <p>{t('A recipe injects a fixed procedure for this turn instead of the default strategy.')}</p>
      <div role="radiogroup" aria-label={t('Recipe')}>
        <button
          type="button"
          role="radio"
          aria-checked={!recipeId}
          onClick={() => { onPick(null); setOpen(false); }}
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
            onClick={() => { onPick(recipe.id); setOpen(false); }}
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
      placement="composer"
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
                  <span>{t('Adds it to the conversation now — it follows it while it works.')}</span>
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
  const label = choices.find(c => c.value === mode)?.label ?? t('Ask for approval');
  return <Popover placement="composer" className="fs-studio__permission-menu" trigger={<button type="button" className="fs-studio__chip fs-studio__chip--compact" data-permission={mode} disabled={!ready || disabled || saving} title={label} aria-label={t('Approval: {label}', { label })}><Shield size={14} aria-hidden="true" /><span className="fs-studio__chip-label">{label}</span></button>}>
    <p>{t('Approval mode for all Faustus chats. Only an administrator can change it.')}</p>
    <div role="radiogroup" aria-label={t('Tool approvals')}>
      {choices.map(choice => <button key={choice.value} type="button" role="radio" aria-checked={mode === choice.value} disabled={saving || disabled} onClick={() => void choose(choice.value)}><strong>{choice.label}</strong><span>{choice.detail}</span></button>)}
    </div>
  </Popover>;
}

/**
 * Lot T: the reasoning-mode chip (Auto / Fast / Think / Deep). Same
 * Popover + radiogroup shape as `ApprovalSelector` above. After an Auto turn
 * the chip says what Auto chose ("Auto · Think").
 */
function ThinkModeChip({ mode, chosen, onPick, levels = [], effort = null, onPickEffort }: {
  mode: ThinkMode;
  chosen: ThinkMode | null;
  onPick: (mode: ThinkMode) => void;
  levels?: string[];
  effort?: string | null;
  onPickEffort?: (effort: string | null) => void;
}) {
  const details: Record<ThinkMode, string> = {
    auto: t('Decides per message: quick for small talk, reasoning for code, maths or analysis.'),
    fast: t('Answers straight away, without reasoning first.'),
    think: t('Reasons before answering, with the normal budget.'),
    deep: t('Reasons at length, with a larger budget. Slower.'),
  };
  const text = effort ? `${t('Level#effort')}: ${reasoningLevelLabel(effort)}` : thinkModeChipText(mode, chosen);
  return (
    <Popover
      placement="composer"
      className="fs-studio__permission-menu"
      testId="studio-think-mode-menu"
      trigger={
        <button type="button" className="fs-studio__chip fs-studio__chip--compact" data-think-mode={mode}
          title={t('Reasoning: {label}', { label: text })} aria-label={t('Reasoning: {label}', { label: text })}
          data-testid="studio-think-mode-chip">
          <Brain size={14} aria-hidden="true" /><span className="fs-studio__chip-label">{text}</span>
        </button>
      }
    >
      <p>{t('How much the model reasons before answering, for this chat. /think auto|fast|think|deep does the same.')}</p>
      <div role="radiogroup" aria-label={t('Reasoning')}>
        {THINK_MODES.map((m) => (
          <button key={m} type="button" role="radio" aria-checked={mode === m} onClick={() => onPick(m)}
            data-testid={`studio-think-mode-${m}`}>
            <strong>{thinkModeLabel(m)}</strong><span>{details[m]}</span>
          </button>
        ))}
      </div>
      {levels.length > 0 && onPickEffort && (
        <>
          <p>{t("This model's own reasoning levels. A level picked here wins over the mode above.")}</p>
          <div role="radiogroup" aria-label={t('Reasoning level')} data-testid="studio-think-levels">
            <button type="button" role="radio" aria-checked={!effort} onClick={() => onPickEffort(null)}
              data-testid="studio-think-level-auto">
              <strong>{t('Follow the mode#effort')}</strong>
            </button>
            {levels.map((lv) => (
              <button key={lv} type="button" role="radio" aria-checked={effort === lv} onClick={() => onPickEffort(lv)}
                data-testid={`studio-think-level-${lv}`}>
                <strong>{reasoningLevelLabel(lv)}</strong><span>{lv}</span>
              </button>
            ))}
            <button type="button" role="radio" aria-checked={effort === 'none'} onClick={() => onPickEffort('none')}
              data-testid="studio-think-level-none">
              <strong>{reasoningLevelLabel('none')}</strong><span>{t('Answers without reasoning. Quick, but it gets dates and counts wrong.')}</span>
            </button>
          </div>
        </>
      )}
    </Popover>
  );
}

/**
 * CMP-GEN: the generation chip opens real controls instead of only showing
 * `describeGen`'s read-only summary. Same pattern the "Agent context"
 * Popover above uses (a trigger chip + a labelled form inside), same
 * settings-fetch pattern `ApprovalSelector` uses just above for its own
 * global setting.
 *
 * Every control's effective value is the explicit per-chat override when
 * one exists, else the global default (SET-07's `local_*_default`
 * settings) — `adapters/composer.ts`'s `genEffectiveValue`/`genFieldSource`
 * make that same default-vs-override decision the slash commands' own
 * `genFromArgs` never had to. Changing a control writes ONE field through
 * `onSetGen` (the same `GenOverrides` the slash commands write); a
 * per-control "Reset" removes just that field; the chip's outer X
 * (`onClearGen`, in the caller) still clears every override at once.
 */
function GenSettingsPopover({ gen, onSetGen, modelName, genLabel }: {
  gen: GenOverrides;
  onSetGen: (update: GenOverrides) => void;
  modelName: string | null;
  genLabel: string;
}) {
  const [defaults, setDefaults] = useState<SamplingDefaults>({});
  useEffect(() => {
    let live = true;
    void getSettings().then((s) => {
      if (!live) return;
      setDefaults({
        temperature: typeof s.local_temperature_default === 'number' ? s.local_temperature_default : undefined,
        top_p: typeof s.local_top_p_default === 'number' ? s.local_top_p_default : undefined,
        top_k: typeof s.local_top_k_default === 'number' ? s.local_top_k_default : undefined,
      });
    }).catch(() => { /* the panel still works with no defaults shown */ });
    return () => { live = false; };
  }, []);

  const thinkApplies = supportsThinking(modelName);
  const set = (key: 'temperature' | 'max_tokens' | 'top_p' | 'top_k' | 'think', value: number | boolean) =>
    onSetGen(genWithOverride(gen, key, value));
  const reset = (key: 'temperature' | 'max_tokens' | 'top_p' | 'top_k' | 'think') =>
    onSetGen(genWithoutOverride(gen, key));

  const Row = ({
    id, label, keyName, value, min, max, step, unit, onChange,
  }: {
    id: string; label: string; keyName: 'temperature' | 'max_tokens' | 'top_p' | 'top_k';
    value: number | undefined; min: number; max: number; step: number;
    unit?: string; onChange: (v: number) => void;
  }) => {
    const source = genFieldSource(keyName, gen);
    const shown = value ?? '';
    return (
      <div className="fs-gen-panel__row">
        <label htmlFor={id}>
          {label}
          <span className="fs-gen-panel__src" data-source={source}>
            {source === 'override' ? t('override') : t('default')}
          </span>
        </label>
        <div className="fs-gen-panel__controls">
          <input
            id={id} type="range" min={min} max={max} step={step}
            value={value ?? (min + max) / 2}
            onChange={(e) => onChange(Number(e.target.value))}
            aria-describedby={`${id}-value`}
          />
          <input
            id={`${id}-value`} type="number" min={min} max={max} step={step}
            value={shown} placeholder={t('auto')}
            aria-label={t('{label} value', { label })}
            onChange={(e) => { const v = Number(e.target.value); if (!Number.isNaN(v)) onChange(v); }}
          />
          {unit && <span className="fs-gen-panel__unit">{unit}</span>}
          {source === 'override' && (
            <button type="button" className="fs-gen-panel__reset" onClick={() => reset(keyName)}>
              {t('Reset')}
            </button>
          )}
        </div>
      </div>
    );
  };

  const temperature = genEffectiveValue('temperature', gen, defaults) as number | undefined;
  const topP = genEffectiveValue('top_p', gen, defaults) as number | undefined;
  const topK = genEffectiveValue('top_k', gen, defaults) as number | undefined;
  const maxTokens = genEffectiveValue('max_tokens', gen, defaults) as number | undefined;
  const thinkSource = genFieldSource('think', gen);
  const thinkValue = Boolean(genEffectiveValue('think', gen, defaults));

  return (
    <Popover
      placement="composer"
      className="fs-gen-panel"
      testId="studio-gen-menu"
      trigger={
        <button type="button" className="fs-studio__chip" aria-pressed={Boolean(genLabel)}
          title={t('Generation settings of this chat (/temp, /maxtokens, /topp, /think, /gen)')}
          data-testid="studio-gen-chip">
          <SlidersHorizontal size={13} aria-hidden="true" /> {genLabel || t('Generation')}
        </button>
      }
    >
      <section aria-label={t('Generation settings')}>
        <h3>{t('Generation settings')}</h3>
        <p>{t('Starts from your default; changing a control overrides it for this chat only.')}</p>
        <Row id="gen-temperature" label={t('Temperature')} keyName="temperature"
          value={temperature} min={0} max={2} step={0.05}
          onChange={(v) => set('temperature', Math.min(2, Math.max(0, v)))} />
        <Row id="gen-top-p" label="top_p" keyName="top_p"
          value={topP} min={0} max={1} step={0.05}
          onChange={(v) => set('top_p', Math.min(1, Math.max(0, v)))} />
        <Row id="gen-top-k" label="top_k" keyName="top_k"
          value={topK} min={0} max={200} step={1}
          onChange={(v) => set('top_k', Math.round(Math.min(200, Math.max(0, v))))} />
        <Row id="gen-max-tokens" label={t('Max tokens')} keyName="max_tokens"
          value={maxTokens} min={0} max={32768} step={64} unit={t('tokens')}
          onChange={(v) => set('max_tokens', Math.round(Math.max(0, v)))} />
        {thinkApplies && (
          <div className="fs-gen-panel__row fs-gen-panel__row--switch">
            <label htmlFor="gen-think">
              {t('Reasoning (think)')}
              <span className="fs-gen-panel__src" data-source={thinkSource}>
                {thinkSource === 'override' ? t('override') : t('default')}
              </span>
            </label>
            <div className="fs-gen-panel__controls">
              <input
                id="gen-think" type="checkbox" role="switch"
                checked={thinkValue}
                onChange={(e) => set('think', e.target.checked)}
              />
              {thinkSource === 'override' && (
                <button type="button" className="fs-gen-panel__reset" onClick={() => reset('think')}>
                  {t('Reset')}
                </button>
              )}
            </div>
          </div>
        )}
      </section>
    </Popover>
  );
}
