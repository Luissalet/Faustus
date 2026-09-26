import { useState } from 'react';
import { Check, Star } from 'lucide-react';
import { t } from '../i18n';
import type { ChoicesSpec, DecisionSpec, UiBlockSpec } from '../lib/uiBlocks';

/**
 * A structured reply block (```choices``` / ```decision```, lib/uiBlocks.ts)
 * drawn with fixed components. A pick never sends by itself: it puts the
 * answer in the composer (`onReply`), so the user still presses Enter and can
 * add to it. Where there is no composer (a Home card, a report) the buttons
 * are shown disabled with a hint instead of doing something surprising.
 */
export function UiBlock({ spec, onReply }: { spec: UiBlockSpec; onReply?: (text: string) => void }) {
  return spec.kind === 'choices' ? <Choices spec={spec} onReply={onReply} /> : <Decision spec={spec} onReply={onReply} />;
}

const NO_COMPOSER = 'Answer in a conversation to use these buttons.';

function Choices({ spec, onReply }: { spec: ChoicesSpec; onReply?: (text: string) => void }) {
  const [picked, setPicked] = useState<string[]>([]);
  const toggle = (label: string) => setPicked((p) => (p.includes(label) ? p.filter((x) => x !== label) : [...p, label]));
  return (
    <div className="fs-uib" data-kind="choices" data-testid="ui-block-choices">
      {spec.question && <div className="fs-uib__title">{spec.question}</div>}
      <div className="fs-uib__choices" role={spec.multi ? 'group' : undefined} aria-label={spec.question || t('Options')}>
        {spec.options.map((o) => {
          const on = picked.includes(o.label);
          return (
            <button
              key={o.label}
              type="button"
              className="fs-uib__choice"
              data-testid="ui-block-choice"
              aria-pressed={spec.multi ? on : undefined}
              disabled={!onReply}
              title={onReply ? o.detail || undefined : t(NO_COMPOSER)}
              onClick={() => (spec.multi ? toggle(o.label) : onReply?.(o.label))}
            >
              {spec.multi && <span className="fs-uib__check" aria-hidden="true">{on ? <Check size={12} /> : null}</span>}
              <span className="fs-uib__label">{o.label}</span>
              {o.detail && <span className="fs-uib__detail">{o.detail}</span>}
            </button>
          );
        })}
      </div>
      {spec.multi && (
        <div className="fs-uib__foot">
          <button type="button" className="fs-btn" data-variant="primary" data-size="sm" data-testid="ui-block-use" disabled={!onReply || picked.length === 0} onClick={() => onReply?.(picked.join(', '))}>
            {t('Use these ({n})', { n: String(picked.length) })}
          </button>
        </div>
      )}
    </div>
  );
}

function Decision({ spec, onReply }: { spec: DecisionSpec; onReply?: (text: string) => void }) {
  return (
    <div className="fs-uib" data-kind="decision" data-testid="ui-block-decision">
      {spec.title && <div className="fs-uib__title">{spec.title}</div>}
      <div className="fs-uib__cards">
        {spec.options.map((o) => (
          <section key={o.name} className="fs-uib__card" data-recommended={o.recommended || undefined} aria-label={o.name}>
            <header className="fs-uib__card-head">
              <strong>{o.name}</strong>
              {o.recommended && (
                <span className="fs-uib__badge">
                  <Star size={11} aria-hidden="true" /> {t('Recommended')}
                </span>
              )}
            </header>
            {o.summary && <p className="fs-uib__summary">{o.summary}</p>}
            {o.pros.length > 0 && (
              <ul className="fs-uib__points" data-tone="pro" aria-label={t('For')}>
                {o.pros.map((p, i) => <li key={i}>{p}</li>)}
              </ul>
            )}
            {o.cons.length > 0 && (
              <ul className="fs-uib__points" data-tone="con" aria-label={t('Against')}>
                {o.cons.map((p, i) => <li key={i}>{p}</li>)}
              </ul>
            )}
            {o.cost && <p className="fs-uib__cost">{o.cost}</p>}
            <button type="button" className="fs-btn" data-variant={o.recommended ? 'primary' : 'secondary'} data-size="sm" data-testid="ui-block-pick" disabled={!onReply} title={onReply ? undefined : t(NO_COMPOSER)} onClick={() => onReply?.(t("Let's go with {name}.", { name: o.name }))}>
              {t('Choose')}
            </button>
          </section>
        ))}
      </div>
      {spec.verdict && <p className="fs-uib__verdict">{spec.verdict}</p>}
    </div>
  );
}
