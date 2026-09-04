import {
  ArrowUp,
  Bot,
  Database,
  FileText,
  FolderOpen,
  Globe,
  ListTodo,
  MessageSquare,
  Paperclip,
  SlidersHorizontal,
  Square,
  Terminal,
  X,
} from 'lucide-react';
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ChangeEvent,
  type ClipboardEvent,
  type DragEvent,
  type KeyboardEvent,
  type ReactNode,
  type RefObject,
} from 'react';
import { IconButton } from '../../components';
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
import { matchCommands, type SlashCommand } from './commands';

export type Mode = 'chat' | 'agent';

export interface Knobs {
  mode: Mode;
  web: boolean;
  bash: boolean;
  plan: boolean;
  rag: boolean;
}

export interface ComposerProps {
  draft: string;
  setDraft: (value: string) => void;
  busy: boolean;
  pending: boolean;
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
  onNotice: (text: string, tone?: 'info' | 'warning' | 'danger') => void;
  modelPicker: ReactNode;
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
  onNotice,
  modelPicker,
  textareaRef,
}: ComposerProps) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const [dragging, setDragging] = useState(false);

  /* ── Suggestions: `@` files or `/` commands ── */
  const [mention, setMention] = useState<{ query: string; items: WorkspaceFile[] } | null>(null);
  const [commands, setCommands] = useState<SlashCommand[] | null>(null);
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
      if (/^\/[a-z-]*$/i.test(value)) {
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

  const pickCommand = (command: SlashCommand) => {
    setDraft(`/${command.name} `);
    setCommands(null);
    requestAnimationFrame(() => textareaRef.current?.focus());
  };

  /* ── Attachments ── */
  const addFiles = useCallback(
    async (files: File[]) => {
      if (!files.length) return;
      setUploading(true);
      try {
        const uploaded = await uploadFiles(files, sessionId);
        setAttachments((list) => [...list, ...uploaded.filter((u) => !list.some((a) => a.id === u.id))]);
      } catch (error) {
        onNotice(`No he podido subir el archivo: ${(error as Error).message}`, 'danger');
      } finally {
        setUploading(false);
      }
    },
    [sessionId, setAttachments, onNotice],
  );

  const onPaste = (event: ClipboardEvent<HTMLTextAreaElement>) => {
    const files = Array.from(event.clipboardData.files ?? []);
    if (files.length) {
      event.preventDefault();
      void addFiles(files);
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
      onSend(draft);
      return;
    }
    if (event.key === 'Escape' && busy) onStop();
  };

  useEffect(() => {
    if (!draft) {
      setMention(null);
      setCommands(null);
    }
  }, [draft]);

  const genLabel = describeGen(gen);
  const canSend = (draft.trim().length > 0 || attachments.length > 0) && !uploading;

  return (
    <form
      className="fs-studio__composer fs-panel"
      data-dragging={dragging || undefined}
      onSubmit={(event) => {
        event.preventDefault();
        onSend(draft);
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
        <ul className="fs-studio__suggest" role="listbox" aria-label="Ficheros del workspace" data-testid="studio-mentions">
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
        <ul className="fs-studio__suggest" role="listbox" aria-label="Comandos" data-testid="studio-commands">
          {commands.map((command, i) => (
            <li
              key={command.name}
              role="option"
              aria-selected={i === active}
              className="fs-studio__suggest-item"
              onMouseDown={(event) => {
                event.preventDefault();
                pickCommand(command);
              }}
            >
              <span className="fs-studio__suggest-main">
                <code>{command.usage}</code>
              </span>
              <span className="fs-studio__suggest-help">{command.help}</span>
            </li>
          ))}
        </ul>
      )}

      {attachments.length > 0 && (
        <ul className="fs-studio__attachments" aria-label="Adjuntos">
          {attachments.map((a) => (
            <li key={a.id} className="fs-studio__attachment" data-testid="studio-attachment">
              {isImage(a.mime) ? (
                <img src={attachmentUrl(a.id)} alt="" width={36} height={36} />
              ) : (
                <FileText size={16} aria-hidden="true" />
              )}
              <span className="fs-studio__attachment-name" title={a.name}>
                {a.name}
              </span>
              <button
                type="button"
                className="fs-studio__attachment-x"
                aria-label={`Quitar ${a.name}`}
                onClick={() => setAttachments((list) => list.filter((x) => x.id !== a.id))}
              >
                <X size={12} aria-hidden="true" />
              </button>
            </li>
          ))}
        </ul>
      )}

      <textarea
        ref={textareaRef}
        className="fs-studio__input"
        rows={1}
        value={draft}
        placeholder={
          pending
            ? 'Responde arriba, o escribe para seguir…'
            : knobs.mode === 'agent'
              ? 'Dime qué quieres que haga…  @fichero · #regla · /comando'
              : 'Escribe un mensaje…  /comando'
        }
        aria-label="Mensaje"
        onChange={onChange}
        onKeyDown={onKey}
        onPaste={onPaste}
        onClick={(event) => refreshSuggestions(draft, event.currentTarget.selectionStart ?? draft.length)}
        data-testid="studio-input"
      />

      <div className="fs-studio__bar">
        <div className="fs-studio__seg" role="radiogroup" aria-label="Modo">
          <span className="fs-studio__seg-thumb" data-mode={knobs.mode} aria-hidden="true" />
          <button
            type="button"
            role="radio"
            aria-checked={knobs.mode === 'chat'}
            onClick={() => setKnobs((k) => ({ ...k, mode: 'chat' }))}
            data-testid="studio-mode-chat"
          >
            <MessageSquare size={13} aria-hidden="true" /> Chat
          </button>
          <button
            type="button"
            role="radio"
            aria-checked={knobs.mode === 'agent'}
            onClick={() => setKnobs((k) => ({ ...k, mode: 'agent' }))}
            data-testid="studio-mode-agent"
          >
            <Bot size={13} aria-hidden="true" /> Agente
          </button>
        </div>

        <div className="fs-studio__knobs">
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
          <IconButton
            icon={Paperclip}
            label={uploading ? 'Subiendo…' : 'Adjuntar archivos'}
            size="sm"
            disabled={uploading}
            onClick={() => fileInputRef.current?.click()}
            testId="studio-attach"
          />
          <button
            type="button"
            className="fs-studio__chip"
            aria-pressed={knobs.web}
            onClick={() => setKnobs((k) => ({ ...k, web: !k.web }))}
            data-testid="studio-knob-web"
          >
            <Globe size={13} aria-hidden="true" /> Web
          </button>
          <button
            type="button"
            className="fs-studio__chip"
            aria-pressed={knobs.rag}
            title="Buscar en tus documentos indexados (RAG)"
            onClick={() => setKnobs((k) => ({ ...k, rag: !k.rag }))}
            data-testid="studio-knob-rag"
          >
            <Database size={13} aria-hidden="true" /> Docs
          </button>
          {knobs.mode === 'agent' && (
            <>
              <button
                type="button"
                className="fs-studio__chip"
                aria-pressed={knobs.bash}
                onClick={() => setKnobs((k) => ({ ...k, bash: !k.bash }))}
                data-testid="studio-knob-bash"
              >
                <Terminal size={13} aria-hidden="true" /> Terminal
              </button>
              <button
                type="button"
                className="fs-studio__chip"
                aria-pressed={knobs.plan}
                onClick={() => setKnobs((k) => ({ ...k, plan: !k.plan }))}
                data-testid="studio-knob-plan"
              >
                <ListTodo size={13} aria-hidden="true" /> Plan
              </button>
              <span className="fs-studio__chipgroup">
                <button
                  type="button"
                  className="fs-studio__chip fs-studio__chip--folder"
                  aria-pressed={Boolean(workspace)}
                  title={workspace ? `Carpeta: ${workspace}` : 'Sin carpeta: el agente no puede leer ni editar ficheros'}
                  onClick={onPickWorkspace}
                  data-testid="studio-workspace"
                >
                  <FolderOpen size={13} aria-hidden="true" />
                  <span>{workspace ? basename(workspace) : 'Elegir carpeta'}</span>
                </button>
                {workspace && (
                  <button
                    type="button"
                    className="fs-studio__chip-x"
                    aria-label="Quitar la carpeta"
                    onClick={onClearWorkspace}
                  >
                    <X size={11} aria-hidden="true" />
                  </button>
                )}
              </span>
            </>
          )}
          {genLabel && (
            <span className="fs-studio__chipgroup">
              <span className="fs-studio__chip" aria-pressed="true" title="Ajustes de generación de este chat (/temp, /maxtokens, /topp, /think, /gen)">
                <SlidersHorizontal size={13} aria-hidden="true" /> {genLabel}
              </span>
              <button type="button" className="fs-studio__chip-x" aria-label="Quitar los ajustes de generación" onClick={onClearGen}>
                <X size={11} aria-hidden="true" />
              </button>
            </span>
          )}
          {modelPicker}
        </div>

        <div className="fs-studio__send">
          {busy ? (
            <IconButton icon={Square} label="Parar" onClick={onStop} testId="studio-stop" />
          ) : (
            <button type="submit" className="fs-studio__go" disabled={!canSend} aria-label="Enviar" data-testid="studio-send">
              <ArrowUp size={18} aria-hidden="true" />
            </button>
          )}
        </div>
      </div>
    </form>
  );
}
