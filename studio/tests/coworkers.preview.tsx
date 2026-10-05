// Verification surface for the shipped component; never a product route.
import React from 'react';
import { createRoot } from 'react-dom/client';
import { CoworkersPanel } from '../src/screens/agents/Coworkers';
import '../src/styles/tokens.css';
localStorage.setItem('faustus_studio_lang','es');
document.body.style.cssText='margin:0;background:var(--fs-canvas);color:var(--fs-text-1);font:15px/1.5 var(--fs-font-ui)';
createRoot(document.getElementById('root')!).render(<main style={{maxWidth:980,margin:'40px auto',padding:16}}><CoworkersPanel/></main>);
