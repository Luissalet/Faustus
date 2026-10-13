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
  const latest = useRef({key, value});
  latest.current = {key, value};
  const setValue = (next: T | ((old: T) => T)) => {
    const previous = read(key) ?? (latest.current.key === key ? latest.current.value : value);
    const resolved = typeof next === 'function' ? (next as (old: T) => T)(previous) : next;
    try {sessionStorage.setItem(key, JSON.stringify(resolved));} catch { /* Keep the in-memory draft. */ }
    setStored({key, value: resolved});
  };
  return [value, setValue] as const;
}

const pendingAttempts = new Map<string, {content: string; recipient: string; id: string}>();
/** An uncertain acknowledgement keeps the same composed message identity. */
export function coordinationAttempt(scope: string, content: string, recipient = '') {
  const key = 'agora:pending:' + scope;
  let previous = pendingAttempts.get(key);
  try {previous = JSON.parse(sessionStorage.getItem(key) || 'null') || previous;} catch { /* Use memory. */ }
  const attempt = previous?.content === content && previous.recipient === recipient ? previous : {content, recipient, id: crypto.randomUUID()};
  pendingAttempts.set(key, attempt);
  try {sessionStorage.setItem(key, JSON.stringify(attempt));} catch { /* Use memory. */ }
  return attempt;
}

export function acknowledgeCoordinationAttempt(scope: string, id: string) {
  const key = 'agora:pending:' + scope;
  if (pendingAttempts.get(key)?.id === id) pendingAttempts.delete(key);
  try {if (JSON.parse(sessionStorage.getItem(key) || 'null')?.id === id) sessionStorage.removeItem(key);} catch { /* Use memory. */ }
}
