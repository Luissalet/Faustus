import {useCallback, useRef, useState} from 'react';
import {useLocation, useSearchParams} from 'react-router';

/** Preserve the mode when legacy room controls are mounted inside Agora. */
export function useCoordinationParams(mode: string) {
  const [params, setParams] = useSearchParams();
  const {pathname} = useLocation();
  const update = useCallback((values: Record<string, string>, options?: {replace?: boolean}) => {
    setParams(pathname === '/agora' ? {...values, mode} : values, options);
  }, [pathname, mode, setParams]);
  return [params, update] as const;
}

/** Room/setup drafts survive mode changes and reconnects in the current tab. */
export function useCoordinationDraft<T>(key: string, initial: T) {
  const read = (name: string) => {
    try {return JSON.parse(sessionStorage.getItem(name) || 'null') as T | null;}
    catch {return null;}
  };
  const initialRef = useRef(initial);
  const [stored, setStored] = useState(() => ({key, value: read(key) ?? initial}));
  const value = stored.key === key ? stored.value : read(key) ?? initialRef.current;
  const setValue = (next: T | ((old: T) => T)) => {
    const resolved = typeof next === 'function' ? (next as (old: T) => T)(value) : next;
    try {sessionStorage.setItem(key, JSON.stringify(resolved));} catch { /* Keep the in-memory draft. */ }
    setStored({key, value: resolved});
  };
  return [value, setValue] as const;
}
