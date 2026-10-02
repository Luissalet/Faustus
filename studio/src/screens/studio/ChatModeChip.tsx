import { Feather, Layers } from 'lucide-react';
import { t } from '../../i18n';
import type { ChatMode } from '../../adapters/chatMode';

/**
 * Lean/Normal chip for the composer. One click flips this chat's mode; the
 * pick goes through `onPick` (never from an effect). Lean = fewer prompt
 * blocks and only the core tools, fixed for this chat until it is changed.
 */
export default function ChatModeChip({ mode, onPick }: { mode: ChatMode; onPick: (next: ChatMode) => void }) {
  const lean = mode === 'lean';
  const label = lean ? t('Lean') : t('Normal');
  const hint = lean
    ? t('Lean chat: no skills index, repo map, instincts or plugin tools; only the core tools. Click for Normal.')
    : t('Normal chat: the full harness. Click for Lean (fewer prompt blocks, core tools only).');
  return (
    <button
      type="button"
      className="fs-studio__chip fs-studio__chip--compact"
      data-mode={mode}
      data-testid="chat-mode-chip"
      title={hint}
      aria-label={t('Chat mode: {label}', { label })}
      aria-pressed={lean}
      onClick={() => onPick(lean ? 'normal' : 'lean')}
    >
      {lean ? <Feather size={14} aria-hidden="true" /> : <Layers size={14} aria-hidden="true" />}{' '}
      <span className="fs-studio__chip-label">{label}</span>
    </button>
  );
}
