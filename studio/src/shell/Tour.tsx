import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router';
import { ChevronLeft, ChevronRight, X } from 'lucide-react';
import { Button, IconButton } from '../components';
import { t } from '../i18n';
import { markTourSeen, placeTooltip, seenTours, tourById, tourForPath, type Box, type Placed } from '../lib/tours';
import { useShell } from './store';
import './tour.css';

/**
 * One runner for every tour (lib/tours.ts has the steps).
 *
 * A step points at something that may not exist yet — screens are lazy
 * chunks and a route change has to land first — so the runner navigates,
 * then waits for the target, then gives up on that step and moves on
 * rather than dying. Nothing here clicks on the person's behalf: the tour
 * shows, the person drives.
 */

const WAIT_MS = 2600;
const POLL_MS = 80;

function boxOf(element: Element): Box {
  const r = element.getBoundingClientRect();
  return { top: r.top, left: r.left, width: r.width, height: r.height };
}

function Runner({ id, onEnd }: { id: string; onEnd: () => void }) {
  const tour = tourById(id);
  const navigate = useNavigate();
  const location = useLocation();
  const [index, setIndex] = useState(0);
  const [box, setBox] = useState<Box | null>(null);
  const [placed, setPlaced] = useState<Placed | null>(null);
  const cardRef = useRef<HTMLDivElement>(null);
  const targetRef = useRef<Element | null>(null);
  const step = tour?.steps[index] ?? null;
  const total = tour?.steps.length ?? 0;

  const go = useCallback(
    (delta: number) => {
      setIndex((i) => {
        const next = i + delta;
        if (next < 0) return 0;
        if (next >= total) {
          onEnd();
          return i;
        }
        return next;
      });
    },
    [total, onEnd],
  );

  // Land on the step's screen, then wait for its target to show up.
  useEffect(() => {
    if (!step) return;
    let alive = true;
    setBox(null);
    targetRef.current = null;
    if (step.route) {
      const here = `${location.pathname}${location.search}`;
      if (here !== step.route) navigate(step.route);
    }
    const started = Date.now();
    const look = () => {
      if (!alive) return;
      const found = document.querySelector(step.target);
      if (found) {
        targetRef.current = found;
        found.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        setBox(boxOf(found));
        return;
      }
      if (Date.now() - started > WAIT_MS) {
        // The screen changed under the tour: skip this step, keep the tour.
        go(1);
        return;
      }
      window.setTimeout(look, POLL_MS);
    };
    const first = window.setTimeout(look, step.route ? POLL_MS : 0);
    return () => {
      alive = false;
      window.clearTimeout(first);
    };
    // location is read, not depended on: a step must not restart on its own navigate.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step, go, navigate]);

  // Follow the target while the page moves.
  useEffect(() => {
    if (!box) return;
    const follow = () => {
      const element = targetRef.current;
      if (element?.isConnected) setBox(boxOf(element));
    };
    window.addEventListener('resize', follow);
    window.addEventListener('scroll', follow, true);
    return () => {
      window.removeEventListener('resize', follow);
      window.removeEventListener('scroll', follow, true);
    };
  }, [box]);

  useLayoutEffect(() => {
    const card = cardRef.current;
    if (!box || !card) {
      setPlaced(null);
      return;
    }
    setPlaced(
      placeTooltip(box, { width: card.offsetWidth || 300, height: card.offsetHeight || 140 }, { width: window.innerWidth, height: window.innerHeight }),
    );
  }, [box, index]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onEnd();
      } else if (event.key === 'ArrowRight') {
        event.preventDefault();
        go(1);
      } else if (event.key === 'ArrowLeft') {
        event.preventDefault();
        go(-1);
      }
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [go, onEnd]);

  // While a step's target is still being looked for there is nothing to
  // point at, and a card floating over the middle of the screen pointing at
  // nothing is worse than a beat of quiet. The wait is 2.6 s at the most.
  if (!tour || !step || !box) return null;
  const last = index === total - 1;
  return (
    <div className="fs-tour" data-testid="tour">
      <div
        className="fs-tour__halo"
        aria-hidden="true"
        // Dimming the page around something that IS the page helps nobody.
        data-big={box.width * box.height > window.innerWidth * window.innerHeight * 0.55 || undefined}
        style={{ top: `${box.top - 4}px`, left: `${box.left - 4}px`, inlineSize: `${box.width + 8}px`, blockSize: `${box.height + 8}px` }}
      />
      <div
        ref={cardRef}
        className="fs-tour__card"
        role="dialog"
        aria-label={t(tour.title)}
        data-side={placed?.side}
        style={placed ? { top: `${placed.top}px`, left: `${placed.left}px` } : { visibility: 'hidden' }}
      >
        <header className="fs-tour__head">
          <span className="fs-tour__title">{t(tour.title)}</span>
          <span className="fs-tour__count">
            {index + 1}/{total}
          </span>
          <IconButton icon={X} label={t('End the tour')} size="sm" onClick={onEnd} />
        </header>
        <p className="fs-tour__text">{t(step.text)}</p>
        <footer className="fs-tour__foot">
          <ul className="fs-tour__dots" aria-hidden="true">
            {tour.steps.map((_, i) => (
              <li key={i} data-on={i <= index || undefined} />
            ))}
          </ul>
          <Button variant="ghost" size="sm" icon={ChevronLeft} label={t('Back')} disabled={index === 0} onClick={() => go(-1)} />
          <Button variant="primary" size="sm" icon={last ? undefined : ChevronRight} label={last ? t('Done') : t('Next')} onClick={() => go(1)} />
        </footer>
      </div>
    </div>
  );
}

/** "First time here? Take the tour." Once per tour, and never again. */
function Offer({ onTake, onDismiss, title }: { onTake: () => void; onDismiss: () => void; title: string }) {
  return (
    <aside className="fs-tour__offer" data-testid="tour-offer">
      <p>{t('First time in {where}? There is a two-minute tour.', { where: t(title) })}</p>
      <Button variant="secondary" size="sm" label={t('Take it')} onClick={onTake} />
      <IconButton icon={X} label={t('Not now')} size="sm" onClick={onDismiss} />
    </aside>
  );
}

export function Tour() {
  const tourId = useShell((s) => s.tourId);
  const setTour = useShell((s) => s.setTour);
  const location = useLocation();
  const [offered, setOffered] = useState<string | null>(null);

  const end = useCallback(() => {
    if (tourId) markTourSeen(tourId);
    setTour(null);
  }, [tourId, setTour]);

  // The offer waits a moment: a screen that has just loaded is busy enough.
  useEffect(() => {
    if (tourId) return;
    const candidate = tourForPath(location.pathname, location.search);
    if (!candidate || seenTours().includes(candidate.id)) {
      setOffered(null);
      return;
    }
    const timer = window.setTimeout(() => setOffered(candidate.id), 1800);
    return () => window.clearTimeout(timer);
  }, [location.pathname, location.search, tourId]);

  if (tourId) return <Runner id={tourId} onEnd={end} />;
  const waiting = offered ? tourById(offered) : null;
  if (!waiting) return null;
  return (
    <Offer
      title={waiting.title}
      onTake={() => {
        setOffered(null);
        setTour(waiting.id);
      }}
      onDismiss={() => {
        markTourSeen(waiting.id);
        setOffered(null);
      }}
    />
  );
}
