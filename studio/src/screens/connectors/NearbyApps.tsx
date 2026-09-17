import { CheckCircle2, Plug, RefreshCw } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { Button, EmptyState, Skeleton } from '../../components';
import { adoptApp, discoverApps, type Connector, type DiscoveredApp } from '../../adapters/connectors';
import { t } from '../../i18n';
import './connectors.css';

/**
 * Nearby apps — a Bluetooth-pairing-style panel for the Connectors screen.
 *
 * `GET /api/app-connectors/discover` is read-only: it scans local ports and
 * says what answered, never touches a Connector. `POST …/adopt` is the one
 * write, and it only runs when the person presses "Add" on a specific row.
 * A row with a recognised preset and no missing placeholder adopts in one
 * click; a row with a preset but a gap (e.g. discovery could not fill
 * JOBHUNT_DIR from the process cwd) opens the normal `NewConnectorForm`
 * pre-filled instead of guessing. A row with no preset is not a button at
 * all — this panel never invents an MCP server for an arbitrary process.
 */

function appLabel(app: DiscoveredApp): string {
  return app.preset_name || app.title || app.process || `:${app.port}`;
}

export function NearbyApps({
  onAdded,
  onNotice,
  onRequestForm,
  onScanned,
}: {
  onAdded: (c: Connector) => void;
  onNotice: (msg: string) => void;
  /** A row needs the full form (missing placeholders): hand the preset id
   *  and what discovery already knows up to the parent, which owns the
   *  NewConnectorForm mount (same one "Add" and "Edit" already use). */
  onRequestForm: (presetId: string, values: Record<string, string>, port: number) => void;
  /** How many apps the last scan found, for the collapsed toggle's count. */
  onScanned?: (count: number) => void;
}) {
  const [apps, setApps] = useState<DiscoveredApp[] | null>(null);
  const [scannedAt, setScannedAt] = useState<string | null>(null);
  const [scanning, setScanning] = useState(false);
  const [adoptingPort, setAdoptingPort] = useState<number | null>(null);
  const [failed, setFailed] = useState(false);

  const scan = useCallback(() => {
    setScanning(true);
    discoverApps()
      .then((r) => {
        setApps(r.apps);
        setScannedAt(r.scanned_at);
        setFailed(false);
        onScanned?.(r.apps.length);
      })
      .catch((e) => {
        setFailed(true);
        onNotice((e as Error).message);
      })
      .finally(() => setScanning(false));
  }, [onNotice, onScanned]);

  useEffect(() => {
    scan();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const add = async (app: DiscoveredApp) => {
    if (!app.preset_id) return;
    setAdoptingPort(app.port);
    try {
      const c = await adoptApp({ port: app.port });
      onAdded(c);
      scan();
    } catch (e) {
      onNotice((e as Error).message);
    } finally {
      setAdoptingPort(null);
    }
  };

  return (
    <section className="fs-nearby" data-testid="nearby-apps" aria-label={t('Nearby apps')}>
      <div className="fs-nearby__head">
        <div className="fs-nearby__head-text">
          <h2 className="fs-nearby__title">{t('Nearby apps')}</h2>
          <span className="fs-set__help">
            {scannedAt ? t('Last scan: {time}', { time: new Date(scannedAt).toLocaleTimeString() }) : t('Not scanned yet.')}
          </span>
        </div>
        <Button
          size="sm"
          variant="ghost"
          icon={RefreshCw}
          label={t('Scan')}
          loading={scanning}
          onClick={scan}
          testId="nearby-scan"
        />
      </div>

      {failed ? (
        <EmptyState
          icon={Plug}
          tone="error"
          title={t('Could not scan for nearby apps.')}
          body={t('GET /api/app-connectors/discover failed.')}
          primaryAction={{ label: t('Try again'), onClick: scan }}
        />
      ) : apps === null ? (
        <Skeleton label={t('Scanning')} count={2} height="52px" />
      ) : apps.length === 0 ? (
        <EmptyState icon={Plug} title={t('Nothing found')} body={t("Nothing answering on this machine's ports")} />
      ) : (
        <ul className="fs-nearby__list" data-testid="nearby-apps-list">
          {apps.map((app) => (
            <li key={app.port} className="fs-nearby__row" data-testid="nearby-app-row" data-port={app.port}>
              <div className="fs-nearby__row-main">
                <span className="fs-nearby__identity">
                  <strong>{appLabel(app)}</strong>
                  <span className="fs-chip fs-nearby__port">:{app.port}</span>
                </span>
                <span className="fs-set__help">{app.process}</span>
                {app.cwd && <span className="fs-nearby__cwd" title={app.cwd}>{app.cwd}</span>}
              </div>
              <div className="fs-nearby__row-action">
                {app.connector_id ? (
                  <span className="fs-set__help fs-nearby__added">
                    <CheckCircle2 size={13} aria-hidden="true" /> {t('Added')}
                  </span>
                ) : app.preset_id && app.missing.length === 0 ? (
                  <Button
                    size="sm"
                    variant="primary"
                    label={t('Add')}
                    loading={adoptingPort === app.port}
                    disabled={adoptingPort !== null}
                    onClick={() => void add(app)}
                    testId={`nearby-add-${app.port}`}
                  />
                ) : app.preset_id ? (
                  <Button
                    size="sm"
                    variant="secondary"
                    label={t('Add…')}
                    disabled={adoptingPort !== null}
                    onClick={() => onRequestForm(app.preset_id as string, app.values, app.port)}
                    testId={`nearby-add-partial-${app.port}`}
                  />
                ) : (
                  <span className="fs-set__help">{t('Not a known app — add it as an MCP server')}</span>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
