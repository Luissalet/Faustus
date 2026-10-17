import { groupOrigin, isFinalTurn, speakerName, type GroupSnapshot } from '../../adapters/group';
import { blankTurn, type Turn } from '../studio/model';

/** Present the existing replies together; the canonical records stay intact. */
export function packDebates(turns: Turn[], snapshot: GroupSnapshot | null): Turn[] {
  const states = new Map<string, Omit<NonNullable<Turn['debate']>, 'replies'>>();
  for (const source of snapshot?.turns ?? []) {
    if (!source.debate && !isFinalTurn(source)) continue;
    const origin = groupOrigin(source);
    const related = snapshot!.turns.filter(t => groupOrigin(t) === origin);
    const final = related.find(isFinalTurn);
    const status = related.some(t => t.status === 'waiting_user') ? 'waiting_user'
      : related.some(t => t.status === 'queued' || t.status === 'running') ? 'working'
      : final?.status === 'done' ? 'done'
      : related.at(-1)?.status === 'stopped' ? 'stopped'
      : related.some(t => t.status === 'completed_with_errors') ? 'error' : 'done';
    const ids = new Set(related.map(t => t.id));
    const active = new Set(snapshot!.steps.filter(s => ids.has(s.turn_id) && ['queued','running','waiting_user'].includes(s.status)).map(s => s.member_id));
    const roster = related.flatMap(t => t.participants ?? snapshot!.group.participants);
    states.set(origin, {origin, state:status, finalizing:!!final && ['queued','running'].includes(final.status), speakers:[...new Map(roster.filter(m=>active.has(m.sessionId)).map(m=>[m.sessionId,speakerName(m)])).values()]});
  }
  const result: Turn[] = [];
  const containers = new Map<string, Turn>();
  function container(origin: string): Turn {
    let current = containers.get(origin);
    if (!current) {
      current = {...blankTurn('assistant'), id:'debate-'+origin, streaming:false,
        debate:{...(states.get(origin) ?? {origin,state:'done' as const,finalizing:false,speakers:[]}),replies:[]}};
      containers.set(origin,current); result.push(current);
    }
    return current;
  }
  for (const turn of turns) {
    if (turn.role === 'user') {
      result.push(turn);
      const origin = turn.id.replace(/^room-/, '');
      const status = states.get(origin)?.state;
      if (status === 'working' || status === 'waiting_user') container(origin);
    } else if (turn.groupReply && !turn.groupReply.final) {
      const target = container(turn.groupReply.origin);
      target.debate!.replies.push(turn);
      target.text += (target.text ? '\n\n' : '') + turn.text;
    } else result.push(turn);
  }
  return result;
}
