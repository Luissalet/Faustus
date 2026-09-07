import { capabilities, capture, playSpeech } from '../voice/audio';

/** Dictation and read-aloud share Jarvis's explicit provider and cleanup rules. */
export interface Dictation { done: Promise<string>; stop(): void; cancel(): void }

export async function startDictation(lang = 'auto', signal?: AbortSignal): Promise<Dictation> {
  const controller = new AbortController();
  const combined = signal ? AbortSignal.any([signal, controller.signal]) : controller.signal;
  const config = await capabilities('stt', combined);
  const recording = await capture(config, { signal: combined, lang, autoStop: false });
  return { done: recording.done, stop: recording.stop, cancel: () => controller.abort() };
}

let current: AbortController | null = null;
export async function speak(text: string): Promise<() => void> {
  stopSpeaking();
  const controller = new AbortController(); current = controller;
  const config = await capabilities('tts', controller.signal);
  await new Promise<void>((resolve, reject) => {
    void playSpeech(text, config, controller.signal, (_, playing) => { if (playing) resolve(); })
      .then(resolve, reject).finally(() => { if (current === controller) current = null; });
  });
  return () => controller.abort();
}
export function stopSpeaking(): void { current?.abort(); current = null; }
