import { Info } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Link } from 'react-router';
import { attachmentDestination } from '../../adapters/dataBoundary';
import { t } from '../../i18n';
import '../settings/data-boundary.css';

/** Informational only: never changes upload, send or approval state. */
export function AttachmentNotice({ endpointId, sessionId, modelName }: { endpointId: string | null; sessionId: string | null; modelName: string | null }) {
  const [target, setTarget] = useState<{ warning: boolean; address: string } | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    if (!modelName) {
      setTarget({ warning: true, address: '' });
      return;
    }
    setTarget(null);
    attachmentDestination(endpointId, sessionId, controller.signal).then(result => { if (!controller.signal.aborted) setTarget(result); }).catch(() => {
      if (!controller.signal.aborted) setTarget({ warning: true, address: '' });
    });
    return () => controller.abort();
  }, [endpointId, sessionId, modelName]);
  if (!target?.warning) return null;
  return <p className="fs-boundary-notice" role="status" data-testid="attachment-destination-notice"><Info size={14} aria-hidden="true" />
    <span>{target?.address ? t('Sending can share these attachments or their extracted content with {destination}.', { destination: target.address }) : t('The attachment destination is not fully known. Sending may share files or extracted content outside this machine.')}{' '}
      <Link to="/settings?s=data_destinations">{t('Data destinations')}</Link></span>
  </p>;
}
