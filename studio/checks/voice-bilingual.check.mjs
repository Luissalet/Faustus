import assert from 'node:assert/strict';
import { build } from 'esbuild';
const compiled = await build({ entryPoints: ['studio/src/voice/engine.ts'], bundle: true, format: 'esm', platform: 'node', write: false });
const { speechLanguage } = await import(`data:text/javascript;base64,${Buffer.from(compiled.outputFiles[0].text).toString('base64')}`);
// Opposite-language fallback deliberately models a switch between consecutive turns.
const cases = [
  ['Archivo guardado.', 'en', 'es'], ['File saved.', 'es', 'en'],
  ['El informe está listo.', 'en', 'es'], ['The file is ready.', 'es', 'en'],
  ['Operación completada.', 'en', 'es'], ['Operation completed.', 'es', 'en'],
  ['No lo borres.', 'en', 'es'], ['Do not delete it.', 'es', 'en'],
  ['Espera un momento.', 'en', 'es'], ['Wait a moment.', 'es', 'en'],
  ['La carpeta contiene dos documentos.', 'en', 'es'], ['The folder contains two documents.', 'es', 'en'],
  ['He añadido los cambios.', 'en', 'es'], ['I have added the changes.', 'es', 'en'],
  ['¿Quieres continuar?', 'en', 'es'], ['Would you like to continue?', 'es', 'en'],
  ['Hola.', 'en', 'es'], ['Hello.', 'es', 'en'],
  ['Gracias.', 'en', 'es'], ['Thanks.', 'es', 'en'],
  // No usable evidence: numbers/names/mixed or shared terms retain the fallback.
  ['42', 'es', 'es'], ['42', 'en', 'en'], ['No.', 'es', 'es'], ['No.', 'en', 'en'],
  ['Faustus', 'es', 'es'], ['Faustus', 'en', 'en'],
  ['Hello. Gracias.', 'es', 'es'], ['Hello. Gracias.', 'en', 'en'],
];
const failures = cases.filter(([text, fallback, expected]) => speechLanguage(text, fallback) !== expected);
for (const [text, fallback, expected] of failures) console.error(JSON.stringify({ text, fallback, expected, actual: speechLanguage(text, fallback) }));
assert.equal(failures.length, 0, `${failures.length}/${cases.length} bilingual voice choices failed`);
console.log(`ALL OK: ${cases.length} bilingual voice choices and alternating-language fallbacks`);
