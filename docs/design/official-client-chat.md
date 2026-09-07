# Clientes oficiales dentro del chat — 07-09-2026

Implementación local, sin commit ni push. No equivale a cerrar la integración
de todos los clientes ni a una prueba de generación contra una cuenta real.

## Qué funciona en código

El selector del chat abre **Conectar un proveedor IA** sin cambiar de conversación.
Reutiliza el formulario API existente (OpenAI, Claude, Gemini), y permite registrar
Claude Code como conexión privada de texto con suscripción o API explícita.
No cambia el modelo actual ni el predeterminado global. Los ejecutores externos
siguen requiriendo activación humana; la conexión no activa ese permiso.

`POST /api/agent-runners/claude/model` exige administrador humano y mismo origen.
Comprueba instalación y autenticación con el cliente oficial, sin leer archivos de
credenciales, sin login/logout y sin enviar una tarea. La conexión se persiste como
`ModelEndpoint` privado y manual, con una capacidad opaca cifrada (no una clave del
proveedor). Sólo el resolutor autorizado entrega esa capacidad al adaptador; una URL
inventada no autoriza lanzar procesos. Revocar/desactivar la conexión invalida los
siguientes turnos, incluidos los de conversaciones antiguas.

El alta es idempotente por dueño/cliente/vía/modelo: reintentar o competir desde
dos peticiones devuelve el mismo endpoint y no rota la capacidad. La clave primaria
resuelve la carrera en SQLite. Una conexión deshabilitada/modificada exige revisarla
en Ajustes; el alta no la reactiva ni pisa sus preferencias. La transacción trabaja
fuera del bucle HTTP, para que esperar a SQLite no congele otros chats.

`src/cli_model.py` integra las tres entradas de `src/llm_core.py`: llamada síncrona,
asíncrona y streaming. Reutiliza `external_worker`, no otro supervisor. El contexto
incluye roles, instrucciones y resultados de herramientas como registros JSON;
las herramientas nativas de Claude, MCP, comandos de skills y persistencia de su
sesión quedan desactivados. El proceso trabaja en un directorio temporal, no descubre
el contexto del proyecto por su cuenta. **Faustus conserva su propio bucle,
herramientas, aprobaciones y delegación**. Los endpoints declaran `supports_tools=False`
para usar el protocolo textual que ese bucle ya admite.

La respuesta se separa del registro de progreso/costes. El adaptador no convierte
salida truncada en una respuesta completa. Rechaza imágenes explícitamente en vez de
ignorarlas; limita el contexto a dos millones de caracteres; conserva supervisión,
cancelación y límite temporal. Durante la espera envía señales de conexión.

`runner_billing.py` selecciona y comprueba el modo antes de enviar el prompt. En
suscripción retira claves y redirecciones API del entorno hijo; no modifica el entorno
del servidor. Los errores no activan fallback, ni siquiera desde el modo antiguo de
reintento sin filtro de estados. No hay garantía sobre cambios futuros del cliente o
de facturación del proveedor: se informa únicamente del método confirmado por el
cliente instalado en ese momento.

## Archivos para continuar

- `src/cli_model.py`: autorización, contexto, transporte y cancelación.
- `src/runner_billing.py`: configuración y comprobación de suscripción/API.
- `src/external_worker.py`: único supervisor; respuesta separada y límites de salida.
- `routes/agent_runner_routes.py`: alta humana y diagnóstico de cliente.
- `src/llm_core.py`, `src/endpoint_resolver.py`: integración con las rutas de modelos.
- `studio/src/screens/ModelConnections.tsx`, `ModelPicker.tsx`, `ModelPalette.tsx`:
  conexión desde el chat. API reutiliza `settings/ProviderConnect.tsx`.
- `tests/test_cli_model.py`, `test_cli_model_routes.py`, `test_runner_billing.py`,
  `test_runner_launch_supervision.py`: regresiones y contratos.

## Verificación y límites pendientes

- 205 pruebas correctas, una omitida en `logs/astra-cli-model-focused.xml`;
  TypeScript, traducciones y build correctos. No es la suite general.
- Revisión final del formulario real a 1920 y 390 px: campos/botones de 44 px,
  anchura del documento 390 px sin desbordamiento y selector accesible por teclado.
  Servidor reiniciado con el lanzador el 07-09 a las 16:18, PID 89604, HTTP 200.
- No se ha enviado ningún prompt a Claude/Codex ni activado el permiso global en esta
  tanda. La prueba de generación real y el equipo mixto siguen sin verificar.
- Codex conserva su ejecutor y diagnóstico existentes; falta un puente de chat con
  herramientas/permisos comprobados. No se ofrece como conexión de texto sólo por
  estar instalado. No se usa una API privada de ChatGPT.
- No hay catálogo dinámico de Claude: `client-default` es una opción de delegación al
  cliente, no un nombre de modelo inventado. Puede indicarse un modelo disponible en
  la cuenta. La generación decidirá si esa cuenta tiene acceso.
- API normal sigue independiente. No hay selección individual de clave API por cuenta
  dentro del CLI: éste usa la configuración de autenticación del cliente del servidor.

## Correcciones colaterales

El instalador de ejecutores tenía una espera ilimitada después del timeout. Ahora
aplica un límite total incluso si sigue imprimiendo progreso, mata el árbol al cancelar
y espera su limpieza de forma acotada. Las pruebas lanzan un proceso Python local
inerte; no instalan software. El supervisor general acota cada línea a 1 MiB y la
colección de llamadas nativas a 4096. Cancelar durante la comprobación de autenticación
no llega a enviar la tarea.

Las traducciones recientes de equipo/panel se incorporan a `docs/ui/i18n/es.tsv`,
fuente generadora, para que regenerar `es.ts` no las pierda.

## Comprobación adicional de Codex

Cliente instalado inspeccionado: `codex 0.153.4`. Los esquemas públicos generados con
`app-server generate-json-schema --experimental` incluyen `dynamicTools`, pero su
presencia no demuestra que todas las herramientas nativas queden desactivadas.
La [documentación oficial de hooks](https://learn.chatgpt.com/docs/hooks#tool-coverage)
indica excepciones en herramientas alojadas y rutas especializadas: no son una
barrera completa de permisos. Por eso este lote no anuncia un adaptador textual de
Codex basándose sólo en un hook de rechazo. La vía oficial para el puente sigue siendo
[App Server](https://learn.chatgpt.com/docs/app-server), con permisos y llamadas a
herramientas comprobados; no se ha invocado una API privada ni enviado prompts de pago.
