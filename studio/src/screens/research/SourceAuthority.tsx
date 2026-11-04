import type { ResearchSource } from '../../adapters/research';
import { t } from '../../i18n';

/** The name of a site kind from `src/source_authority.py`; an unknown kind shows as it came. */
export function sourceTypeLabel(kind: string): string {
  const labels: Record<string, string> = {
    gov: t('government'), edu: t('university'), intergovernmental: t('intergovernmental'), docs: t('documentation'),
    paper: t('paper'), code: t('code host'), wiki: t('wiki'), qa: t('Q&A'), forum: t('forum'), blog: t('blog'),
    shop: t('shop'), social: t('social network'), video: t('video'),
  };
  return labels[kind] ?? kind;
}

/** The "official" and site-kind chips of one source (shared by the Deep Research list and the library). */
export function SourceAuthorityFlags({ source: s }: { source: ResearchSource }) {
  return (
    <>
      {s.official && (
        <span className="fs-rs__source-flag" data-tone="success" data-testid="research-source-official" title={t('Official source: government, university, intergovernmental body or vendor documentation')}>
          {t('official')}
        </span>
      )}
      {s.sourceType && (
        <span className="fs-rs__source-flag" data-tone="info" data-testid="research-source-type">
          {sourceTypeLabel(s.sourceType)}
        </span>
      )}
    </>
  );
}

export const officialCountOf = (sources: ResearchSource[]): number => sources.filter((s) => s.official).length;

/** The sources to list: only the official ones when the filter is on AND there is any. A ticked filter
 *  carried over to a report with none must not hide every source, and its checkbox is gone then. */
export function visibleSources(sources: ResearchSource[], officialOnly: boolean): ResearchSource[] {
  return officialOnly && officialCountOf(sources) > 0 ? sources.filter((s) => s.official) : sources;
}

/** The "Official sources only" checkbox; renders nothing while no source is official. */
export function OfficialOnlyFilter({ count, checked, onChange }: { count: number; checked: boolean; onChange: (on: boolean) => void }) {
  if (count <= 0) return null;
  return (
    <label className="fs-rs__source-filter">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} data-testid="research-official-only" />
      {' '}{t('Official sources only')}
    </label>
  );
}