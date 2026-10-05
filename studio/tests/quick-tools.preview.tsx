// Isolated verification of the shipped component; not a product route or app shell.
import React, { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { QuickTools } from '../src/screens/studio/QuickTools';
import '../src/styles/tokens.css';
localStorage.setItem('faustus_studio_lang', 'es');
function Preview() {
  const [draft, setDraft] = useState('lista: Revisar fuentes; Contrastar fechas; Guardar resultados');
  const [notice, setNotice] = useState('');
  return <main><p className="scope">PRUEBA DEL COMPONENTE · HERRAMIENTAS LOCALES</p><h1>Escribe y utiliza el resultado</h1>
    <form onSubmit={e => e.preventDefault()}><label htmlFor="draft">Mensaje</label><textarea id="draft" value={draft} onChange={e => setDraft(e.target.value)} />
      <QuickTools draft={draft} onInsert={setDraft} onNotice={setNotice} /></form><p role="status">{notice}</p></main>;
}
const style = document.createElement('style');
style.textContent = `body{margin:0;background:var(--fs-canvas);color:var(--fs-text-1);font:14px var(--fs-font-ui)}main{max-width:680px;margin:72px auto;padding:24px}.scope{color:var(--fs-text-2);font-size:11px;letter-spacing:.08em}h1{font-size:24px;font-weight:500;margin:16px 0 32px}form{padding:16px;border:1px solid var(--fs-border);border-radius:12px;background:var(--fs-surface-1)}label{display:block;margin-bottom:8px}textarea{box-sizing:border-box;width:100%;min-height:100px;resize:vertical;border:0;background:transparent;color:inherit;font:inherit;line-height:1.6;outline-offset:4px}@media(max-width:500px){main{margin:24px auto;padding:16px}h1{font-size:22px}}`;
document.head.appendChild(style);
createRoot(document.getElementById('root')!).render(<Preview />);
