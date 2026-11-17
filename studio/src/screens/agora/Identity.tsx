import { Bot, Cog, MousePointer2, Sparkles, SquareTerminal, type LucideIcon } from 'lucide-react';
import type { CSSProperties } from 'react';
import type { AgoraMember, Presence } from '../../adapters/agora';
import { BrandMark } from '../../shell/BrandMark';
import { t } from '../../i18n';
import { agentColor, agentInitials, RANK_LABEL, ROLE_LABEL, roleOf } from './util';

/** The mark of each provider: what tells Claude, Codex, Cursor and Faustus apart at a glance. */
const ROLE_ICON: Record<string, LucideIcon> = { claude: Sparkles, codex: SquareTerminal, cursor: MousePointer2, system: Cog, other: Bot };

/** A session started by a provider (`claude-worker-…`, `codex-juegos`) rather than the provider's own chat. */
export const isChildAgent = (id: string) => {
  const role = roleOf(id);
  return role !== 'person' && role !== 'other' && role !== 'system' && id.toLowerCase() !== role;
};

/** What goes inside an avatar: the provider's mark, or the initials of a person. */
export function AgentFace({ id, size = 18 }: { id: string; size?: number }) {
  const role = roleOf(id);
  if (role === 'person') return <>{agentInitials(id)}</>;
  if (role === 'faustus') return <BrandMark size={size} />;
  const Icon = ROLE_ICON[role] ?? Bot;
  return <Icon size={size} aria-hidden="true" strokeWidth={2.2} />;
}

/** A round avatar in the agent's colour with its mark and, when known, a dot that says whether it is around. */
export function AgentAvatar({ id, presence, size = 'md' }: { id: string; presence?: Presence; size?: 'sm' | 'md' | 'lg' }) {
  const px = size === 'lg' ? 26 : size === 'sm' ? 15 : 19;
  return (
    <span className="fs-agx-avatar" data-size={size === 'md' ? undefined : size} data-presence={presence} data-child={isChildAgent(id) || undefined}
      data-role={roleOf(id)} style={{ '--agx-c': agentColor(id) } as CSSProperties} aria-hidden="true">
      <AgentFace id={id} size={px} />
    </span>
  );
}

/** The line under a name: who it works for and its rank on this server («Claude · Responsable»). */
export function roleLine(id: string, member?: Pick<AgoraMember, 'rank'> | null): string {
  const role = roleOf(id);
  if (role === 'person') return t('Person');
  if (role === 'system') return t('System');
  const bits = [t(ROLE_LABEL[role] ?? role)];
  if (isChildAgent(id)) bits.push(t('session'));
  if (member?.rank) bits.push(t(RANK_LABEL[member.rank] ?? member.rank));
  return bits.join(' · ');
}
