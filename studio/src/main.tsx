/**
 * Faustus Studio — entry point.
 *
 * Nothing mounts into the live Faustus UI yet: the AppShell is UI-021 and
 * arrives behind the `faustus_studio_shell` flag. What renders here is the
 * component gallery from UI-011, so the primitives can be reviewed in a
 * real browser — dark, light, keyboard and reduced motion — before any
 * screen is built on top of them.
 */

import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import './styles/fonts.css';
import './styles/tokens.css';
import './styles/base.css';
import './styles/legacy-bridge.css';
import './styles/components.css';
import { Gallery } from './gallery/Gallery';

const root = document.getElementById('studio-root');
if (root) {
  createRoot(root).render(
    <StrictMode>
      <Gallery />
    </StrictMode>,
  );
}
