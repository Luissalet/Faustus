import { useEffect, useState } from 'react';
import { Brain } from 'lucide-react';
import { t } from '../i18n';

/* Darwin #992: a voice turn is an ordinary chat turn, so it consults and feeds
   the Second Brain exactly like a typed one (memory recall, the brain context
   source, memory capture → entities). This line says so with the real switches
   (GET /api/voice/brain) and carries the one toggle: the chat's own
   "Skip memory recall" (`no_memory`), the same knob as the composer's. */

export interface VoiceBrainLink {
  voice_brain: boolean;
  brain_enabled: boolean;
  consulted: { memory_recall: boolean; brain_context_source: boolean; context_engine: boolean };
  fed: { memory_capture: boolean; brain_extraction: boolean };
  brain: { notes: number; entities: number; relations: number; pending_extraction: number };
  chat_memories: number;
}

export async function loadVoiceBrain(sessionId: string, noMemory: boolean, signal?: AbortSignal): Promise<VoiceBrainLink | null> {
  const q = new URLSearchParams({ session_id: sessionId, no_memory: String(noMemory) });
  const res = await fetch(`/api/voice/brain?${q}`, { credentials: 'same-origin', signal });
  if (!res.ok) return null;
  return (await res.json()) as VoiceBrainLink;
}

function summary(link: VoiceBrainLink): string {
  if (!link.voice_brain) return t('Off for this chat: voice turns neither recall nor save memories.');
  const read = link.consulted.brain_context_source ? t('memories, notes and entities') : link.consulted.memory_recall ? t('memories') : '';
  const write = link.fed.brain_extraction ? t('saves memories that become entities') : link.fed.memory_capture ? t('saves memories') : '';
  const parts = [read && t('consults {what}', { what: read }), write].filter(Boolean);
  const counts = t('{e} entities · {n} notes', { e: link.brain.entities, n: link.brain.notes });
  return `${parts.join(' · ') || t('nothing is consulted or saved (memory is off in Settings)')} · ${counts}`;
}

export function VoiceBrain({ sessionId, brain, onBrain }: { sessionId?: string | null; brain: boolean; onBrain?(on: boolean): void }) {
  const [link, setLink] = useState<VoiceBrainLink | null>(null);
  useEffect(() => {
    if (!sessionId) return;
    const controller = new AbortController();
    loadVoiceBrain(sessionId, !brain, controller.signal).then(setLink).catch(() => setLink(null));
    return () => controller.abort();
  }, [sessionId, brain]);
  return (
    <div className="fs-voice__brain" data-testid="voice-brain" data-on={brain || undefined}>
      <label title={t('The same switch as “Skip memory recall” in the composer: voice turns use the Second Brain like typed ones.')}>
        <input type="checkbox" checked={brain} disabled={!onBrain} onChange={(e) => onBrain?.(e.target.checked)} data-testid="voice-brain-toggle" />
        <Brain size={14} aria-hidden="true" /> {t('Second Brain')}
      </label>
      {link && <span className="fs-voice__hint">{summary(link)}</span>}
    </div>
  );
}
