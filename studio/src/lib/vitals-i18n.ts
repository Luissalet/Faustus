import { getLang, locale, t, tn } from '../i18n';
import type { Health } from '../adapters/usage';
import type { DegradationReport } from '../adapters/degradation';
import type { ServiceHealth } from '../adapters/services';

/** Server ids are stable; labels are prose and may change independently. */
export const HEALTH_LABEL: Record<string, string> = {
  ollama: 'Ollama reachable',
  gpu: 'GPU visible to nvidia-smi',
  vram: 'VRAM headroom',
  host: 'RAM headroom',
  disk: 'Disk headroom',
  runners: 'No orphaned runners',
  dispatch: 'Recent dispatched jobs',
};

const DEPENDENCY_LABEL: Record<string, string> = {
  chromadb: 'Vector store', searxng: 'Web search', ntfy: 'Notifications',
  email: 'Email', providers: 'Model endpoints', sparks: 'Sparks', mcp_servers: 'MCP servers', mcp_config: 'MCP configuration',
};

function number(raw: string): string {
  const places = Math.min(20, raw.split('.')[1]?.length ?? 0);
  return Number(raw).toLocaleString(locale(), { minimumFractionDigits: places, maximumFractionDigits: 20 });
}

/** Only recognise complete server templates. Unknown diagnostics remain literal. */
export function vitalsText(value: string, depth = 0): string {
  if (getLang() === 'en' || depth > 3) return value;
  if (value === 'none') return t('no orphaned runners');
  let m: RegExpMatchArray | null;
  if ((m = value.match(/^(\d+(?:\.\d+)?)% free( · SPILLING)?$/))) {
    const free = t('{n}% free', { n: number(m[1]) });
    return m[2] ? `${free} · ${t('spilling into system RAM')}` : free;
  }
  if ((m = value.match(/^(\d+(?:\.\d+)?)% used$/))) return t('{n}% used', { n: number(m[1]) });
  if ((m = value.match(/^(\d+(?:\.\d+)?) GB free \((\d+(?:\.\d+)?)%\)$/))) {
    return t('{gb} GB free ({pct}%)', { gb: number(m[1]), pct: number(m[2]) });
  }
  if ((m = value.match(/^(\d+(?:\.\d+)?) GB free of (\d+(?:\.\d+)?) GB where Faustus writes$/))) {
    return t('{free} GB free of {total} GB where Faustus writes', { free: number(m[1]), total: number(m[2]) });
  }
  if ((m = value.match(/^(\d+(?:\.\d+)?) GB free across (\d+) card\(s\)$/))) {
    return t('{gb} GB free across {n} cards', { gb: number(m[1]), n: number(m[2]) });
  }
  if ((m = value.match(/^(\d+(?:\.\d+)?)% of system RAM in use$/))) return t('{n}% of system RAM in use', { n: number(m[1]) });
  if ((m = value.match(/^nvidia-smi answered for (\d+) card\(s\)$/))) return t('nvidia-smi answered for {n} cards', { n: number(m[1]) });
  if ((m = value.match(/^answering at (.+)$/))) return t('answering at {url}', { url: m[1] });
  if ((m = value.match(/^unreachable: (.+)$/))) return t('unreachable: {why}', { why: vitalsText(m[1], depth + 1) });
  if ((m = value.match(/^reachable · (\d+) model\(s\) loaded$/))) {
    return t('reachable') + ' · ' + tn(Number(m[1]), '{n} model loaded', '{n} models loaded', { n: number(m[1]) });
  }
  if ((m = value.match(/^(\d+) card\(s\)$/))) return tn(Number(m[1]), '{n} card', '{n} cards', { n: number(m[1]) });
  if ((m = value.match(/^(\d+) orphaned$/))) return tn(Number(m[1]), '{n} orphaned runner', '{n} orphaned runners', { n: number(m[1]) });
  if ((m = value.match(/^(\d+) job\(s\), (\d+) failed$/))) {
    return tn(Number(m[1]), '{n} job', '{n} jobs', { n: number(m[1]) }) + ', ' + t('{n} failed', { n: number(m[2]) });
  }
  if ((m = value.match(/^(\d+)\/(\d+) (endpoint\(s\)|account\(s\)) reachable\.$/))) {
    return t(m[3] === 'endpoint(s)' ? '{a}/{b} model endpoints reachable.' : '{a}/{b} mail accounts reachable.', { a: number(m[1]), b: number(m[2]) });
  }
  if (value === 'No endpoint(s) reachable.') return t('No model endpoint is reachable.');
  if (value === 'No account(s) reachable.') return t('No mail account is reachable.');
  if ((m = value.match(/^Reachable \(HTTP (\d+)\)\.$/))) return t('Reachable (HTTP {code}).', { code: m[1] });
  if ((m = value.match(/^Unreachable \((.+)\)\.$/))) return t('Unreachable ({why}).', { why: vitalsText(m[1], depth + 1) });
  if ((m = value.match(/^Search provider is '([^']+)', not SearXNG\.$/))) return t("Search provider is '{provider}', not SearXNG.", { provider: m[1] });
  // Keep exception classes, server ids, URLs and provider output intact.
  if ((m = value.match(/^(\d+) high findings in enabled servers \((.*)\)$/))) {
    return t('{n} high findings in enabled servers ({names})', { n: number(m[1]), names: m[2] });
  }
  if ((m = value.match(/^([A-Za-z]\w*(?:Error|Warning)): (.+)$/))) return `${m[1]}: ${vitalsText(m[2], depth + 1)}`;
  if ((m = value.match(/^(\d+) of (\d+) servers failing \(([^)]*)\)(.*)$/))) {
    return t('{a} of {b} servers failing ({names})', { a: number(m[1]), b: number(m[2]), names: m[3] }) + m[4];
  }
  return t(value);
}

export function healthForDisplay(health: Health): Health {
  return {
    ...health,
    components: health.components.map((c) => ({
      ...c,
      label: t(HEALTH_LABEL[c.name] ?? c.label ?? c.name),
      value: typeof c.value === 'string' ? vitalsText(c.value)
        : typeof c.value === 'number' && getLang() === 'es' ? c.value.toLocaleString(locale()) : c.value,
      why: c.why ? vitalsText(c.why) : c.why,
    })),
  };
}

export function degradationForDisplay(report: DegradationReport): DegradationReport {
  const items = report.items.map((item) => ({
    ...item,
    label: t(DEPENDENCY_LABEL[item.id] ?? item.label),
    cause: vitalsText(item.cause),
    affects: item.affects.map((name) => t(name)),
  }));
  return { ...report, items, problems: items.filter((i) => i.state === 'degraded' || i.state === 'down') };
}

export function servicesForDisplay(health: ServiceHealth): ServiceHealth {
  return { ...health, services: health.services.map((s) => ({ ...s, detail: vitalsText(s.detail) })) };
}
