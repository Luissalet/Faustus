# Estado del lote T — Ajustes: Modelos locales

Fecha: 05-09-2026. Rama `feat/studio-ui`.

## Qué hay

`adapters/localModels.ts` + `screens/settings/LocalModels.tsx`, sobre
`routes/local_models_routes.py` (las mismas rutas que `static/js/localModels.js`):
lista con tarjeta(s), cargados, instalados, opciones, descargas con
`EventSource` por trabajo (una descarga es un trabajo del servidor; la
página se reengancha a lo que `/pulls` aún lista), cargar/descargar de
memoria, borrar, política de ubicación, catálogo. Los helpers de formato y
el veredicto de fit son los mismos que en la anterior, para que las dos
digan lo mismo.

## Verificado en el 7001

Dos tarjetas: conjunto y por tarjeta con presupuestos; 8 modelos
instalados con fit (cabe/repartido/no cabe); cargar `qwen3.5:9b` →
«GPU 1 · RTX 5060 Ti · 100% GPU · ctx 32k · 5 min», descargar de memoria;
formulario de opciones con main_gpu; descargar `qwen2.5:0.5b` con progreso
hasta «hecho» y borrarlo después.

## Arreglos y trampas

- Al dormirse el PC se cayeron el 7001 y Ollama; Ollama arrancado a mano
  necesita `OLLAMA_MODELS=D:\LocalAI\ollama-models` (si no, «0 modelos»).
- Al probar por consola, `querySelector` sin acotar a `.fs-set__body` pilla
  el formulario `#lm-pull-form` del legacy oculto (navega la página). Acotar.

## Pendiente

Editor de Tema (lote U).
