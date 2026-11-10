import { CornerUpLeft, Download, FileText, SmilePlus, X } from 'lucide-react';
import { useState } from 'react';
import { fileUrl, type MessageFile, type MessageQuote, type MessageReaction } from '../../adapters/agora';
import { IconButton, Popover } from '../../components';
import { t } from '../../i18n';
import { fileSize } from './util';

/** The ones a tap away; anything else is typed (an emoji or a short word). */
export const QUICK_REACTIONS = ['👍', '✅', '👀', '❤️', '😂', '🎉', '🤔', '👎'];

/** The reactions under a message: who put each one, and a click to add one's own or take it back. */
export function Reactions({ reactions, onToggle }: { reactions: MessageReaction[] | undefined; onToggle: (emoji: string, on: boolean) => void }) {
  if (!reactions?.length) return null;
  return (
    <div className="fs-agx-reactions" data-testid="agora-reactions">
      {reactions.map((r) => (
        <button key={r.emoji} type="button" className="fs-agx-reaction" aria-pressed={r.mine} title={r.agents.join(', ') + (r.count > r.agents.length ? ' …' : '')}
          aria-label={t('{emoji}: {n}. {names}', { emoji: r.emoji, n: r.count, names: r.agents.slice(0, 8).join(', ') })}
          onClick={() => onToggle(r.emoji, !r.mine)} data-testid="agora-reaction">
          <span aria-hidden="true">{r.emoji}</span><span aria-hidden="true">{r.count}</span>
        </button>
      ))}
    </div>
  );
}

/** The button that opens the reactions to choose from. */
export function ReactButton({ onPick }: { onPick: (emoji: string) => void }) {
  const [open, setOpen] = useState(false);
  const [other, setOther] = useState('');
  const pick = (emoji: string) => { const value = emoji.trim(); if (!value) return; setOpen(false); setOther(''); onPick(value); };
  return (
    <Popover open={open} onOpenChange={setOpen} side="top" align="end" className="fs-agx-react-pop" testId="agora-react-pop"
      trigger={<IconButton size="sm" icon={SmilePlus} label={t('React')} testId="agora-react" />}>
      <div className="fs-agx-react-pop__grid" role="group" aria-label={t('React')}>
        {QUICK_REACTIONS.map((emoji) => <button key={emoji} type="button" onClick={() => pick(emoji)} aria-label={emoji} data-testid="agora-react-quick">{emoji}</button>)}
      </div>
      <form className="fs-agx-react-pop__other" onSubmit={(e) => { e.preventDefault(); pick(other); }}>
        <input className="fs-field" value={other} maxLength={16} placeholder={t('Another emoji or a short word')} aria-label={t('Another emoji or a short word')}
          onChange={(e) => setOther(e.target.value.replace(/\s+/g, ''))} />
      </form>
    </Popover>
  );
}

/** The message this one answers, above it: a click goes to it. */
export function Quoted({ quote, onJump }: { quote: MessageQuote; onJump: (id: number) => void }) {
  if (quote.gone || !quote.author) return <p className="fs-agx-quote" data-gone><CornerUpLeft size={12} aria-hidden="true" /><span>{t('The message it answered is no longer there.')}</span></p>;
  return (
    <button type="button" className="fs-agx-quote" onClick={() => onJump(quote.id)} title={t('Go to that message')} data-testid="agora-quote">
      <CornerUpLeft size={12} aria-hidden="true" /><strong>{quote.author}</strong><span>{quote.excerpt}</span>
    </button>
  );
}

/** What a message carries: pictures in place, anything else as a file to open or save. */
export function Files({ files }: { files: MessageFile[] | undefined }) {
  if (!files?.length) return null;
  return (
    <div className="fs-agx-files" data-testid="agora-files">
      {files.map((file) => file.inline && file.type.startsWith('image/')
        ? (
          <a key={file.id} className="fs-agx-files__image" href={fileUrl(file.id)} target="_blank" rel="noopener" title={file.name + ' · ' + fileSize(file.size)}>
            <img src={fileUrl(file.id)} alt={file.name} loading="lazy" />
          </a>
        ) : (
          <a key={file.id} className="fs-agx-file" href={fileUrl(file.id)} {...(file.inline ? { target: '_blank', rel: 'noopener' } : { download: file.name })} data-testid="agora-file">
            {file.inline ? <FileText size={18} aria-hidden="true" /> : <Download size={18} aria-hidden="true" />}
            <span className="fs-agx-file__text"><strong>{file.name}</strong><span>{fileSize(file.size)}</span></span>
          </a>
        ))}
    </div>
  );
}

/** Above the box while writing: the message being answered and the files the next message takes along. */
export function Carrying({ quote, files, sending, onDropQuote, onDropFile }: {
  quote: { author: string; body: string } | null; files: MessageFile[]; sending: number; onDropQuote: () => void; onDropFile: (id: string) => void;
}) {
  if (!quote && !files.length && !sending) return null;
  return (
    <div className="fs-agx-carry" data-testid="agora-carry">
      {quote && (
        <p className="fs-agx-carry__quote">
          <CornerUpLeft size={13} aria-hidden="true" />
          <span>{t('Answering {name}', { name: quote.author })}: {quote.body.replace(/\s+/g, ' ').slice(0, 140)}</span>
          <IconButton size="sm" icon={X} label={t('Do not quote it')} onClick={onDropQuote} />
        </p>
      )}
      {(files.length > 0 || sending > 0) && (
        <ul className="fs-agx-carry__files">
          {files.map((file) => (
            <li key={file.id}><FileText size={13} aria-hidden="true" /><span>{file.name}</span><span className="fs-muted">{fileSize(file.size)}</span>
              <IconButton size="sm" icon={X} label={t('Leave {name} out', { name: file.name })} onClick={() => onDropFile(file.id)} /></li>
          ))}
          {sending > 0 && <li data-sending aria-live="polite">{t('Sending…')}</li>}
        </ul>
      )}
    </div>
  );
}
