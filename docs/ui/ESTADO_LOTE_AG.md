# Estado del lote AG — Cookbook

Fecha: 05-09-2026. Rama `feat/studio-ui`. Cierra la fila «Cookbook
(servir/descargar modelos)» de PARIDAD §1, la última «Anterior» de §1. Nada
de `cookbook*.js` (17.700 líneas en diez ficheros) sobrevive como UI; la
lógica pura vive en `studio/src/lib/cookbook/`: `serve.ts` (detección de
motor, parsers y optimizaciones por familia, el comando de lanzamiento de
cada motor, rutas GGUF), `diagnosis.ts` (53 patrones de error con arreglos
como datos), `recipes.ts` (recetas de instalación), `tasks.ts` (forma de
las sesiones y los comandos tmux / PowerShell para leer, parar y matar).

## Brief (banco de trabajo)

**Trabajo.** Tener modelos locales funcionando sin salir de la app: saber
qué cabe en esta máquina (o en un servidor), traerlo, lanzarlo con el motor
adecuado y los mandos correctos, ver la sesión y arreglarla cuando falla,
y tener a mano lo que cada motor necesita instalado.

**Lo que hacía mal la anterior.** Un modal de 780 px con cuatro pestañas
(Launch, Download, Dependencies, Settings) donde «En marcha» era una
pestaña más y el estado vivía en `localStorage` con sincronización a mano;
casillas de modelos por endpoint, el grafo de hardware plegado, los
formularios de lanzamiento como una fila de campos sin agrupar, y los
diagnósticos como HTML pegado a la tarjeta.

**Dirección.** Una pantalla ancha con seis pestañas que son los seis
momentos del trabajo (Ajuste → Modelos → Descargar → En marcha →
Dependencias → Servidores), el servidor elegido arriba para todas, y cada
sitio con URL. El estado es el del servidor (`/api/cookbook/state`), en un
store externo que cada pestaña lee y que se guarda con retardo; el monitor
de sesiones corre mientras la pantalla está abierta, registra el endpoint
de un lanzamiento listo y dobla el diagnóstico del servidor con el propio.
El formulario de lanzamiento agrupa los mandos por motor, sugiere el puerto
libre, muestra las GPU con su VRAM libre y los perfiles de hardware, y deja
editar el comando generado antes de lanzar.

**Revisión contra los defaults.** Cada estado vacío dice el siguiente
paso; las confirmaciones dicen la consecuencia (tamaño que se libera,
árbol de procesos que se mata); nada depende del hover; los colores de
servidor viajan como valor en el estado (elección en `.ts`, pintado por
variable). Los `localStorage` de la anterior desaparecen salvo el espejo
del estado (pintado instantáneo) y la máquina imaginada.

## Qué entra

- `adapters/cookbook.ts`: store (`useCookbookState`, `updateState`,
  `loadState`, `saveState` con token pendiente), servidores (`serverKey`,
  `selectServer`, `serveCtx`), tareas (`addTask`, `patchTask`,
  `removeTask`, `tasksStatus`), shell, `serveModel`, `downloadModel`,
  `stopSession`/`sessionOutput` (`/api/codex/cookbook/*` con sesión admin),
  `cachedModels`, `listGpus`, hwfit (`hwSystem`, `hwModels`,
  `hwImageModels`, `serveProfiles`), catálogos (`ollamaLibrary`,
  `hfLatest`, `hfGgufFiles`), dependencias (`listPackages`,
  `installLocalPackage`, `installSystemDeps`, `rebuildEngine`), SSH
  (`sshKey`, `generateSshKey`, `testSsh`, `setupServer`), endpoints
  (`registerEndpoint`, `probeEndpoint`), `scheduleServe`.
- `screens/cookbook/{Cookbook,Fit,Models,ServeForm,Download,Running,
  Dependencies,Servers,Schedule,parts,actions,monitor}.tsx|ts` +
  `screens/cookbook.css`.
- Rutas: `/cookbook` ya existía en `app.py`; `TOOLS` ready:true;
  `SERVER_ROUTES`; comando `/cookbook`; `open_cookbook`; el aviso de rembg
  del editor → `/cookbook?t=deps&pkg=rembg`; anchura «wide».
- i18n: 311 filas.

## Verificado en el navegador (7001, Windows local)

- Ajuste: 2 × RTX 4070 Ti detectadas, catálogo de 120 filas con ajuste y
  motor (llama.cpp en Windows), filtros y búsqueda («Qwen2-0.5B» → una
  fila), fila desplegada con fuente GGUF → **Descargar GGUF** → tarea.
- Modelos: 20 en caché (HF + Ollama + FLUX), tipos con recuento, formulario
  de `qwen3.5:9b` (Ollama detectado) → Launch → sesión `serve-…` con salida
  (`ollama show`) y estado. Formulario de `bartowski/Qwen2-0.5B-Instruct-GGUF`
  (llama.cpp detectado, chips de GPU con VRAM libre, perfiles Quality Q6_K /
  Balanced Q4_K_M) → Launch → sesión con el comando `MODEL_FILE=$(…find…)`,
  `http://localhost:8080/v1`, **diagnóstico del servidor** «No GGUF file
  found…» y salida.
- Descargar: `smollm2:135m` → «Pull with Ollama» → En marcha con progreso
  «100% · 59 MB/s», `DOWNLOAD_OK`, Done y botón **Serve** → abre el
  formulario del modelo; novedades de HF y biblioteca de Ollama cargadas.
- Dependencias: 16 paquetes por categoría, llama_cpp parcial (rueda CPU)
  con motivo, prerequisitos (cmake ✗ g++ ✗ git ✓), receta pip/docker con
  «Run on Local»; botón «Install cmake, g++».
- Servidores: tarjeta de esta máquina (entorno, ruta, color, carpetas con
  destino), «Add an SSH server», token de HF, GPU por defecto, clave SSH.
- Programar…: diálogo con horas, días y espejo al calendario (cancelado
  sin guardar).

## Sin verificar

- PENDIENTES 120.
