import { Suspense } from 'react';
import { NavLink, useLocation } from 'react-router';
import { Skeleton } from '../../components';
import { t } from '../../i18n';
import { lazyChunk } from '../../shell/lazyChunk';
import { LibraryNavigation } from './LibraryNavigation';
import '../library.css';

const MemoryScreen = lazyChunk(() => import('../Memory').then(m => ({ default: m.MemoryScreen })));
const BrainScreen = lazyChunk(() => import('../Brain').then(m => ({ default: m.BrainScreen })));

export function KnowledgeScreen() {
  const { pathname } = useLocation();
  return (
    <div className="fs-screen fs-knowledge" data-testid="library-knowledge">
      <header className="fs-screen__head">
        <h1 className="fs-screen__title">{t('Knowledge')}</h1>
      </header>
      <LibraryNavigation />
      <nav className="fs-knowledge__sections" aria-label={t('Knowledge')}>
        <NavLink to="/memory" className="fs-link" data-testid="knowledge-memory">{t('Memory')}</NavLink>
        <NavLink to="/brain" className="fs-link" data-testid="knowledge-brain">{t('Brain')}</NavLink>
      </nav>
      <div className="fs-knowledge__body">
        <Suspense fallback={<Skeleton label={t('Loading the screen')} count={4} height="56px" />}>
          {pathname === '/brain' ? <BrainScreen /> : <MemoryScreen embedded />}
        </Suspense>
      </div>
    </div>
  );
}
