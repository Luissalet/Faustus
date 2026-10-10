import { useEffect, useMemo, useState } from 'react';
import { t } from '../i18n';
import { subtitleParts, wordAt, type Playback } from './visemes';

/** The sentence being spoken, with the word that is being said lit up as it is said (karaoke style). The position
 *  comes from the audio element through `playback.currentMs()`; the component re-renders only when the word changes.
 *  Without marks the sentence is shown as plain text. With reduced motion the highlight stays and its fade goes
 *  (voice.css), so nothing moves. */
export function VoiceSubtitle({ playback }: { playback: Playback | null }) {
  const [now, setNow] = useState(-1);
  const parts = useMemo(() => (playback ? subtitleParts(playback.text, playback.marks) : []), [playback]);
  useEffect(() => {
    setNow(-1);
    const marks = playback?.marks;
    if (!playback || !marks) return;
    let frame = 0, last = -2;
    const tick = () => {
      frame = requestAnimationFrame(tick);
      const k = wordAt(marks, playback.currentMs());
      if (k !== last) { last = k; setNow(k); }
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [playback]);
  if (!playback || !parts.length) return null;
  return (
    <div className="fs-voice__subtitle" role="group" aria-label={t('Spoken subtitle')} data-testid="voice-subtitle" data-word={now} data-karaoke={playback.marks ? 'true' : 'false'}>
      {parts.map((part, i) => part.word === null
        ? <span key={i}>{part.text}</span>
        : <span key={i} className="fs-voice__word" data-state={part.word < now ? 'done' : part.word === now ? 'now' : 'next'}>{part.text}</span>)}
    </div>
  );
}
