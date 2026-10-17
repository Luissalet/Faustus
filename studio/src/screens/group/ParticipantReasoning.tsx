import { useEffect, useState } from 'react';
import { getReasoningLevels, type ReasoningLevels } from '../../adapters/composer';
import type { Participant } from '../../adapters/group';
import { t } from '../../i18n';

export function reasoningChoice(value: string): Pick<Participant, 'reasoningEffort' | 'thinkingEnabled'> {
  return { reasoningEffort: !value || value === 'on' || value === 'off' ? undefined : value,
    thinkingEnabled: value === 'off' ? false : value ? true : undefined };
}

/** The same verified endpoint/model capabilities used by Studio's composer. */
export function ParticipantReasoning({ member, onChange }: {
  member: Participant; onChange: (changes: Pick<Participant, 'reasoningEffort' | 'thinkingEnabled'>) => void;
}) {
  const [caps, setCaps] = useState<ReasoningLevels>({ levels: [] });
  useEffect(() => {
    const controller = new AbortController();
    setCaps({ levels: [] });
    void getReasoningLevels(member.route.endpointId ?? '', member.route.model, controller.signal).then((value) => {
      if (!controller.signal.aborted) setCaps(value);
    });
    return () => controller.abort();
  }, [member.route.endpointId, member.route.model]);
  const selected = member.thinkingEnabled === false ? 'off' : member.reasoningEffort ?? (member.thinkingEnabled ? 'on' : '');
  const options = [...new Set(['', ...(caps.thinking_supported ? ['on'] : []), ...(caps.thinking_toggle ? ['off'] : []), ...caps.levels])];
  const labels: Record<string, string> = { '': 'Project defaults', on: 'Thinking', off: 'Off.', auto: 'Automatic', none: 'Off.', minimal: 'Minimal', low: 'Low', medium: 'Medium', high: 'High', xhigh: 'Extra high', max: 'Maximum' };
  return <label className="fs-grp__option">{t('Reasoning')}
    <select className="fs-field" aria-label={t('Reasoning') + ' · ' + (member.personaName || member.route.model)}
      value={selected} onChange={(event) => onChange(reasoningChoice(event.target.value))}>
      {!options.includes(selected) && <option value={selected} disabled>{t(labels[selected] ?? selected)} · {t('Unavailable')}</option>}
      {options.map((value) => <option key={value} value={value}>{t(labels[value] ?? value)}</option>)}
    </select>
  </label>;
}
