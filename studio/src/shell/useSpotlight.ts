import { useCallback, type MouseEvent } from 'react';

/**
 * Pointer-following spotlight.
 *
 * Writes the pointer position into two custom properties on the hovered
 * element; candy.css draws the light from them. It is a style write per
 * mousemove on ONE element — no layout read, no state, no re-render — so it
 * costs nothing measurable. On touch there is no pointer to follow and the
 * light simply stays centred.
 */
export function useSpotlight() {
  return useCallback((event: MouseEvent<HTMLElement>) => {
    const target = event.currentTarget;
    const rect = target.getBoundingClientRect();
    target.style.setProperty('--mx', `${event.clientX - rect.left}px`);
    target.style.setProperty('--my', `${event.clientY - rect.top}px`);
  }, []);
}
