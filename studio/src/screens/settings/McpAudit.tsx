import { AlertTriangle } from 'lucide-react';
import { Button } from '../../components';
import { useMcpAudit, type McpAuditConfig, type McpAuditFinding } from '../../adapters/mcp-audit';
import { t, tn } from '../../i18n';

const COPY: Record<string, [string, string]> = {
  plaintext_secret: ['Credential stored in plain text', 'Use a reference to a secret in the vault or environment. Keep credentials out of launch arguments and URLs.'],
  insecure_url: ['Remote connection without encryption', 'Use HTTPS for remote servers. Local loopback connections can use HTTP.'],
  unpinned_package: ['Package version can change on restart', 'Pin the package to a specific version and update it deliberately.'],
  broad_root: ['Access to an entire drive or user profile', 'Give the server only the project folders it needs.'],
  inherits_env: ['The server inherits the entire environment', 'Use the minimal environment and declare the variables the server needs.'],
  shell_wrapper: ['Launch command interpreted by a shell', 'Point Command to the executable and supply its arguments directly.'],
  duplicate: ['Another server has the same configuration', 'Keep one server or disable the duplicate.'],
};

export function McpAuditBadge({ findings }: { findings: McpAuditFinding[] }) {
  if (!findings.length) return null;
  return <span className="fs-mcp-audit__badge">
    <AlertTriangle size={13} aria-hidden="true" />
    {tn(findings.length, '{n} configuration finding', '{n} configuration findings')}
  </span>;
}

export function McpAuditFindings({ findings }: { findings: McpAuditFinding[] }) {
  if (!findings.length) return null;
  return <details className="fs-mcp-audit" data-testid="mcp-audit-findings">
    <summary>{t('Review configuration findings')}</summary>
    <p className="fs-set__help">{t('These warnings do not prevent saving or using the server.')}</p>
    <ul className="fs-mcp-audit__findings">
      {findings.map((f, i) => {
        const copy = COPY[f.code];
        return <li key={`${f.code}-${i}`}>
          <strong>{t(copy?.[0] ?? f.message)}</strong>
          <span className="fs-set__help">{t(f.severity === 'high' ? 'High priority' : f.severity === 'medium' ? 'Medium priority' : 'Low priority')}</span>
          {f.detail && <code className="fs-mcp-audit__detail">{f.detail}</code>}
          <p className="fs-set__help">{t(copy?.[1] ?? f.fix ?? 'Review the server configuration.')}</p>
        </li>;
      })}
    </ul>
  </details>;
}

export function McpAuditDraft({ config }: { config: McpAuditConfig }) {
  const audit = useMcpAudit(config);
  let invalid = false;
  if (config.transport === 'stdio') {
    try {
      const args = typeof config.args === 'string' ? JSON.parse(config.args || '[]') : config.args;
      const env = typeof config.env === 'string' ? JSON.parse(config.env || '{}') : config.env;
      invalid = !Array.isArray(args) || !env || typeof env !== 'object' || Array.isArray(env);
    } catch { invalid = true; }
  }
  return <div className="fs-mcp-audit__draft" aria-live="polite" data-testid="mcp-audit-draft">
    {invalid ? <p className="fs-set__help">{t('Complete the JSON arguments and environment to preview configuration findings.')}</p>
      : audit.loading ? <p className="fs-set__help">{t('Checking configuration…')}</p>
      : audit.error ? <div className="fs-mcp-audit__retry"><p className="fs-set__help">{t('Configuration audit could not be loaded. You can still save the server.')}</p><Button size="sm" variant="ghost" label={t('Retry audit')} onClick={audit.refresh} /></div>
      : audit.report?.findings.length ? <><McpAuditBadge findings={audit.report.findings} /><McpAuditFindings findings={audit.report.findings} /></>
      : <p className="fs-set__help">{t('No configuration findings in this draft.')}</p>}
  </div>;
}
