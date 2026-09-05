import { useEffect, useRef, useState } from 'react';
import { Check, Copy } from 'lucide-react';
import { t } from '../../i18n';
import { rain, type Egg as EggData } from '../../lib/fun';

/**
 * The hidden commands, drawn. `lib/fun.ts` decides what to show; this only
 * shows it, so a coin flip is a fact of the turn and not a thing that keeps
 * changing while you look at it. The one exception is the rain, which is
 * the whole point of the rain.
 */

function Rain() {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    return rain(canvas, window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }, []);
  return <canvas ref={ref} className="fs-egg__rain" width={400} height={180} aria-label={t('Rain')} role="img" />;
}

function Swatch({ hex }: { hex: string }) {
  const [copied, setCopied] = useState(false);
  if (!hex) return null;
  return (
    <div className="fs-egg__swatch">
      <button
        type="button"
        className="fs-egg__chip"
        style={{ background: hex }}
        aria-label={t('Copy {hex}').replace('{hex}', hex)}
        title={t('Copy {hex}').replace('{hex}', hex)}
        onClick={() => {
          navigator.clipboard
            .writeText(hex)
            .then(() => {
              setCopied(true);
              setTimeout(() => setCopied(false), 1400);
            })
            .catch(() => undefined);
        }}
      />
      <code>{hex}</code>
      {copied ? <Check size={13} aria-hidden="true" /> : <Copy size={13} aria-hidden="true" />}
    </div>
  );
}

export function Egg({ data }: { data: EggData }) {
  switch (data.kind) {
    case 'flip':
      return (
        <div className="fs-egg" data-egg="flip">
          <div className="fs-egg__coin">{data.text}</div>
          {data.aside && <p className="fs-egg__aside">{data.aside}</p>}
        </div>
      );
    case 'roll':
      return (
        <div className="fs-egg" data-egg="roll">
          <ul className="fs-egg__dice">
            {(data.values ?? []).map((v, i) => (
              <li key={i} style={{ animationDelay: `${i * 0.07}s` }}>
                {v}
              </li>
            ))}
          </ul>
          {data.aside && <p className="fs-egg__aside">{data.aside}</p>}
        </div>
      );
    case '8ball':
      return (
        <div className="fs-egg" data-egg="8ball">
          <div className="fs-egg__ball">
            <span>8</span>
          </div>
          {data.aside && <p className="fs-egg__aside">{data.aside}</p>}
          <p className="fs-egg__answer" data-tone={data.tone}>
            {data.text}
          </p>
        </div>
      );
    case 'fortune':
      return (
        <div className="fs-egg fs-egg--card" data-egg="fortune">
          <p className="fs-egg__kicker">{t('Fortune cookie')}</p>
          <p className="fs-egg__quote">{data.text}</p>
          <p className="fs-egg__aside">{(data.values ?? []).join(' ')}</p>
        </div>
      );
    case 'odyssey':
    case 'wisdom':
      return (
        <blockquote className="fs-egg fs-egg--quote" data-egg={data.kind}>
          <p className="fs-egg__quote">{data.text}</p>
          <footer className="fs-egg__aside">{data.aside}</footer>
        </blockquote>
      );
    case 'ascii':
    case 'cowsay':
      return <pre className="fs-egg fs-egg--art" data-egg={data.kind}>{data.text}</pre>;
    case 'matrix':
      return (
        <div className="fs-egg" data-egg="matrix">
          <Rain />
        </div>
      );
    case 'uptime':
      return (
        <div className="fs-egg" data-egg="uptime">
          <p className="fs-egg__big">{data.text}</p>
          <div className="fs-egg__meter">
            <span style={{ inlineSize: `${data.values?.[0] ?? 0}%` }} />
          </div>
          <p className="fs-egg__aside">{t('this session has been open')}</p>
        </div>
      );
    case 'color':
      return (
        <div className="fs-egg" data-egg="color">
          <Swatch hex={data.text ?? ''} />
        </div>
      );
    default:
      return null;
  }
}
