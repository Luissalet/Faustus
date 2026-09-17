import { Mic, MicOff, Paperclip, Send, Square, X } from 'lucide-react';
import { useCallback, useEffect, useRef, useState, type ChangeEvent } from 'react';
import { Button, IconButton } from '../../components';
import { waSend, waTyping, waUpload, type WaMessage } from '../../adapters/whatsapp';
import type { Dictation } from '../../adapters/speech';
import { t } from '../../i18n';

const TYPING_THROTTLE_MS = 4000;
const TYPING_PAUSE_MS = 5000;

export interface ComposerProps {
  chat: string;
  text: string;
  setText: (v: string) => void;
  replyTo: WaMessage | null;
  onClearReply: () => void;
  onSent: () => void;
  say: (msg: string) => void;
}

export function Composer({ chat, text, setText, replyTo, onClearReply, onSent, say }: ComposerProps) {
  const [busy, setBusy] = useState(false);
  const [dictation, setDictation] = useState<Dictation | null>(null);
  const [transcribing, setTranscribing] = useState(false);
  const [recording, setRecording] = useState(false);
  const dictationController = useRef<AbortController | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const lastTypingSent = useRef(0);
  const pausedTimer = useRef<number | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);

  useEffect(() => () => { dictationController.current?.abort(); }, [chat]);
  useEffect(() => () => { if (pausedTimer.current) window.clearTimeout(pausedTimer.current); }, [chat]);

  const notifyTyping = useCallback(() => {
    const now = Date.now();
    if (now - lastTypingSent.current > TYPING_THROTTLE_MS) {
      lastTypingSent.current = now;
      void waTyping(chat, 'composing').catch(() => {});
    }
    if (pausedTimer.current) window.clearTimeout(pausedTimer.current);
    pausedTimer.current = window.setTimeout(() => {
      void waTyping(chat, 'paused').catch(() => {});
    }, TYPING_PAUSE_MS);
  }, [chat]);

  const handleChange = (v: string) => {
    setText(v);
    if (v.trim()) notifyTyping();
  };

  const send = async () => {
    const value = text.trim();
    if (!value || busy) return;
    setBusy(true);
    try {
      await waSend(chat, value, { quote: replyTo?.id });
      setText('');
      onClearReply();
      onSent();
    } catch (e) {
      say((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const pickFile = () => fileRef.current?.click();

  const onFileChosen = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file) return;
    setBusy(true);
    try {
      await waUpload(chat, file, { caption: text.trim() || undefined, quote: replyTo?.id });
      setText('');
      onClearReply();
      onSent();
    } catch (err) {
      say((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const toggleRecording = async () => {
    if (recording) {
      recorderRef.current?.stop();
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const rec = new MediaRecorder(stream, { mimeType: 'audio/webm;codecs=opus' });
      chunksRef.current = [];
      rec.ondataavailable = (ev) => { if (ev.data.size > 0) chunksRef.current.push(ev.data); };
      rec.onstop = () => {
        stream.getTracks().forEach((tr) => tr.stop());
        setRecording(false);
        const blob = new Blob(chunksRef.current, { type: 'audio/webm;codecs=opus' });
        if (blob.size > 0) {
          setBusy(true);
          waUpload(chat, blob, { filename: 'voice.webm', voice: true, quote: replyTo?.id })
            .then(() => { onClearReply(); onSent(); })
            .catch((err: Error) => say(err.message))
            .finally(() => setBusy(false));
        }
      };
      recorderRef.current = rec;
      rec.start();
      setRecording(true);
    } catch (e) {
      say((e as Error).message);
    }
  };

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
      dictationController.current?.abort();
      dictationController.current = controller;
      const d = await startDictation(undefined, controller.signal);
      if (controller.signal.aborted) { d.cancel(); return; }
      setDictation(d);
      d.done
        .then((value) => {
          if (controller.signal.aborted) return;
          if (value) setText(text ? `${text.trimEnd()} ${value}` : value);
          else say(t('I did not hear anything.'));
        })
        .catch((e: Error) => { if (!controller.signal.aborted) say(e.message); })
        .finally(() => {
          setDictation(null);
          setTranscribing(false);
          requestAnimationFrame(() => textareaRef.current?.focus());
        });
    } catch (e) {
      say((e as Error).message);
    }
  };

  return (
    <div className="fs-wa__composer-wrap">
      {replyTo && (
        <div className="fs-wa__reply-strip" data-testid="whatsapp-replying-to">
          <span>
            <strong>{replyTo.from_name}</strong>
            <span>{replyTo.text || t('[attachment]')}</span>
          </span>
          <IconButton icon={X} label={t('Cancel reply')} size="sm" onClick={onClearReply} />
        </div>
      )}
      <div className="fs-wa__composer">
        <input ref={fileRef} type="file" hidden onChange={(e) => void onFileChosen(e)} data-testid="whatsapp-attach-input" />
        <IconButton icon={Paperclip} label={t('Attach a file')} size="sm" onClick={pickFile} disabled={busy} testId="whatsapp-attach" />
        <textarea
          ref={textareaRef}
          className="fs-wa__composer-input"
          value={text}
          placeholder={t('Write a message…')}
          onChange={(e) => handleChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              void send();
            }
          }}
          rows={2}
          data-testid="whatsapp-composer-input"
        />
        <IconButton
          icon={dictation ? MicOff : Mic}
          label={dictation ? t('Stop dictating') : transcribing ? t('Transcribing…') : t('Dictate')}
          size="sm"
          disabled={transcribing || recording}
          onClick={() => void toggleDictation()}
          testId="whatsapp-dictate"
        />
        <IconButton
          icon={recording ? Square : Mic}
          label={recording ? t('Stop recording') : t('Record voice note')}
          size="sm"
          disabled={!!dictation}
          onClick={() => void toggleRecording()}
          testId="whatsapp-record"
        />
        <Button icon={Send} label={t('Send')} loading={busy} disabled={!text.trim()} onClick={() => void send()} testId="whatsapp-send" />
      </div>
    </div>
  );
}
