/**
 * "Faustus on the phone" (lot P-B): which chrome the shell shows.
 *
 * `mobile` is a narrow viewport (<=767px, the same breakpoint the rest of
 * `styles/shell.css` already collapses at) OR an installed PWA that is
 * still that narrow — a phone home-screen launch reports
 * `display-mode: standalone` before its first layout, and by the time a
 * plain width query would catch up the desktop-only chrome (the "Ctrl+K"
 * hint, the tour) would already have flashed on screen.
 *
 * `data-platform` is written onto `<html>` so plain CSS can hide
 * desktop-only chrome the same way `data-nav`/`data-theme` already steer
 * the shell, and `usePlatform()` is the React-side read of the same value
 * for components that need to branch (Rail's five-tab bottom bar).
 */
import { useEffect, useState } from 'react';

export type Platform = 'desktop' | 'mobile';

const NARROW = '(max-width: 767px)';
const STANDALONE_NARROW = '(display-mode: standalone) and (max-width: 767px)';

function computePlatform(): Platform {
  if (typeof window === 'undefined' || !window.matchMedia) return 'desktop';
  const narrow = window.matchMedia(NARROW).matches;
  const standaloneNarrow = window.matchMedia(STANDALONE_NARROW).matches;
  return narrow || standaloneNarrow ? 'mobile' : 'desktop';
}

export function usePlatform(): Platform {
  const [platform, setPlatform] = useState<Platform>(() => computePlatform());

  useEffect(() => {
    const queries = [window.matchMedia(NARROW), window.matchMedia(STANDALONE_NARROW)];
    const update = () => {
      const next = computePlatform();
      setPlatform(next);
      document.documentElement.setAttribute('data-platform', next);
    };
    update();
    queries.forEach((mq) => mq.addEventListener('change', update));
    return () => queries.forEach((mq) => mq.removeEventListener('change', update));
  }, []);

  return platform;
}
