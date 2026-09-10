import { getSettings, saveSettings } from '../../adapters/settings';
import { t } from '../../i18n';
import {
  ArrowUp,
  AudioLines,
  Bot,
  Brain,
  Database,
  EyeOff,
  FileText,
  FolderOpen,
  Gauge,
  Globe,
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
import { matchCommands, type Suggestion } from './commands';
import { clipboardFiles, insertPastedText } from '../../lib/clipboard-attachments';
import {REFERENCE_ROLES} from '../../lib/image-references';
import {MediaRecipes} from './MediaRecipes';
import { createAttachmentUploads, type PendingAttachment } from '../../lib/attachment-uploads';

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

  /* ── Suggestions: `@` files or `/` commands ── */
  const [mention, setMention] = useState<{ query: string; items: WorkspaceFile[] } | null>(null);
  const [commands, setCommands] = useState<Suggestion[] | null>(null);
  const [active, setActive] = useState(0);
  const mentionAbort = useRef<AbortController | null>(null);

  const refreshSuggestions = useCallback(
    (value: string, caret: number) => {
      const before = value.slice(0, caret);
      const m = MENTION.exec(before);
      if (m && workspace) {
        const query = m[2];
        mentionAbort.current?.abort();
        const controller = new AbortController();
        mentionAbort.current = controller;
        searchWorkspaceFiles(workspace, query, controller.signal)
          .then((items) => {
            if (controller.signal.aborted) return;
            setMention({ query, items });
            setActive(0);
          })
          .catch(() => undefined);
        setCommands(null);
        return;
      }
      setMention(null);
      // A slash line, possibly with a subcommand word: `/chats ex`.
      if (/^\/[a-z0-9?_-]*(?:\s+[a-z0-9?_-]*)?$/i.test(value)) {
        setCommands(matchCommands(value));
        setActive(0);
        return;
      }
      setCommands(null);
    },
    [workspace],
  );

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
  const addFiles = (files: File[]) => uploads.add(files);

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
    if (event.key === 'Escape' && busy) onStop();
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
              {entry.state === 'failed' && <button type="button" className="fs-studio__attachment-x" aria-label={t('Retry {name}', {name:entry.file.name})} onClick={() => uploads.retry(entry.id)}><RefreshCw size={13} aria-hidden="true" /></button>}
              <button type="button" className="fs-studio__attachment-x" aria-label={t('Remove {name}', {name:entry.file.name})} onClick={() => uploads.remove(entry.id)}><X size={12} aria-hidden="true" /></button>
            </li>
          ))}
          {attachments.map((a) => (
            <li key={a.id} className="fs-studio__attachment" data-image={isImage(a.mime)||undefined} data-testid="studio-attachment">
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
                {isImage(a.mime) && <select className="fs-studio__reference-role"
                  aria-label={t('Reference role for {name}', {name:a.name})}
                  title={t('Adds visible guidance to your message. The image model determines how closely it can follow it.')}
                  value={a.referenceRole || ''}
                  onChange={event=>{const role=REFERENCE_ROLES.find(role=>role.value===event.target.value)?.value;
                    setAttachments(list=>list.map(item=>item.id===a.id?{...item,referenceRole:role}:item));}}
                ><option value="">{t('Attachment only')}</option>{REFERENCE_ROLES.map(role=><option key={role.value} value={role.value}>{t(role.label)}</option>)}</select>}
              </span>
              <button
                type="button"
                className="fs-studio__attachment-x"
                aria-label={t('Remove {name}', { name: a.name })}
                onClick={() => setAttachments((list) => list.filter((x) => x.id !== a.id))}
              >
                <X size={12} aria-hidden="true" />
              </button>
            </li>
          ))}
        </ul>
      )}

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
            <IconButton icon={Square} label={t('Stop')} onClick={onStop} testId="studio-stop" />
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
