import { ChevronDown, Globe } from 'lucide-react';
import type { ReactNode } from 'react';
import { t } from '../i18n';
import { safeExternal } from '../lib/markdown';
import { sourceDomain, type ResponseSource } from '../lib/responsePresentation';

export function SourceLink({ source, href = source.url, children }: { source: ResponseSource; href?: string; children?: ReactNode }) {
  const url = safeExternal(href);
  const icon = source.favicon && (/^\/api\/favicon\?/.test(source.favicon) || safeExternal(source.favicon))
    ? <img src={source.favicon} alt="" width={14} height={14} loading="lazy" aria-hidden="true" />
    : <Globe size={13} aria-hidden="true" />;
  const content = <>{icon}<span>{children || sourceDomain(source)}</span></>;
  return url
    ? <a className="fs-response-source" href={url} target="_blank" rel="noreferrer" title={source.title || url} aria-label={`${source.title || sourceDomain(source)} · ${sourceDomain(source)}`}>{content}</a>
    : <span className="fs-response-source">{content}</span>;
}

/** Keep the complete observed list available, including sources beyond the first six. */
export function ResponseSources({ sources, expanded = false }: { sources: readonly ResponseSource[]; expanded?: boolean }) {
  if (!sources.length) return null;
  const links = <ul className="fs-response-sources__list">{sources.map((source, i) =>
    <li key={`${source.url}-${i}`}><SourceLink source={source}>{source.title || sourceDomain(source)}</SourceLink><small>{sourceDomain(source)}</small></li>,
  )}</ul>;
  if (expanded) return <div className="fs-response-sources" data-testid="response-reading-sources">{links}</div>;
  return <details className="fs-response-sources" data-testid="response-sources">
    <summary><Globe size={14} aria-hidden="true" /><span>{t('Sources')} · {sources.length}</span><ChevronDown size={13} aria-hidden="true" /></summary>
    {links}
  </details>;
}
