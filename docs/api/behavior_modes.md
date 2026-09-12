# Modos de comportamiento — `/api/behavior-modes*`

CONTRATO_MODOS Lote A. Implementación: `src/behavior_modes.py` (catálogo,
resolución, inyección, checks — sin FastAPI), `routes/behavior_mode_routes.py`
(rutas).

## Qué es un modo (y qué no)

Un **modo de comportamiento** es una postura conversacional con nombre,
elegida por el usuario, que cambia **cómo** habla y argumenta Faustus —
nunca **qué** puede hacer. Es ortogonal a:

- el modo Chat/Agent/research (`Session.mode`, en `core/database.py`): eso
  decide qué herramientas puede tocar la sesión;
- los presets de tarea (`src/preset_manager.py`: `code_analyze`,
  `brainstorm`, `reason`…): eso decide para qué tipo de tarea se afina el
  prompt;
- el modelo, el proyecto y las skills activas.

Un modo **nunca** cambia `UNTRUSTED_CONTEXT_POLICY`, las reglas del agente,
los permisos ni las instrucciones explícitas del usuario — esas ganan
siempre. El propio texto de cada modo lo dice, y además la posición del
bloque en el prompt lo garantiza estructuralmente (ver más abajo): no es
solo una promesa que el modo tenga que cumplir por su cuenta.

## Catálogo integrado

Ocho modos vienen con el repo, en `config/behavior_modes/*.json`
(`builtin: true`, de solo lectura — el usuario no puede editarlos ni
borrarlos, aunque sí crear los suyos con otro id):

| id | Una línea |
|---|---|
| `default` | Faustus tal cual: sin postura añadida. |
| `adversarial` | Un asesor más listo que tú: nunca abre dándote la razón, etiqueta su confianza, prohíbe la adulación. |
| `socratic` | Enseña preguntando, una pregunta cada vez; da la respuesta solo si se la pides directamente. |
| `terse` | Primero la respuesta, con las menos palabras que sigan siendo correctas. |
| `mentor` | Un colega sénior paciente: la forma de la respuesta primero, luego el razonamiento reconstruible, una pregunta de comprobación al final. |
| `red_team` | Busca cómo falla tu plan/código/texto/argumento, por daño esperado, sin proponer arreglos. |
| `observer` | Para temas políticos/culturales/morales: criterios simétricos, distancia emocional, sin el encuadre dominante. |
| `editor` | Lee tu texto como un lector exigente: opiniones concretas, sugerencias que puedes rechazar, nunca una reescritura silenciosa. |

Cada uno vive en su propio JSON (`id`, `name.{en,es}`, `description.{en,es}`,
`prompt` en inglés para el modelo, `checks`). Un JSON roto se salta con
`logger.warning` al cargar — nunca tumba el arranque (`load_builtin`).

## Dónde entra en el prompt, y por qué ahí

`chat_processor.build_context_preface` inserta el bloque del modo como un
mensaje `system` **después** del preset (proyecto + preset de tarea) y
**antes** de `UNTRUSTED_CONTEXT_POLICY`:

```
[preset / proyecto]        ← "qué tarea"
[behaviour mode]           ← "cómo habla" — esto
[UNTRUSTED_CONTEXT_POLICY] ← la última palabra sobre qué puede hacer
...
```

Así, ni siquiera un modo que intentara reescribir las reglas de seguridad
en su propio `prompt` podría colarse por delante de la política — el orden
del array lo impide, no la buena fe del texto. Ni el modo Agente ni
Incógnito suprimen este bloque: un modo es una postura, no memoria, así que
se aplica igual en ambos. `default` (o un `prompt` vacío) no añade ningún
mensaje — cero coste, cero diferencia de comportamiento.

El bloque lleva una línea de cabecera, `Behaviour mode "<name.en>":`, para
que el compactador de contexto y el inspector de prompt (`/api/config/
effective/prompt`) lo reconozcan como lo que es en vez de tratarlo como
prosa de proyecto.

## Precedencia de resolución

`behavior_modes.resolve(requested, session_mode, default_setting, owner)`:

1. **Petición** — `behavior_mode` en el cuerpo/form de ese turno (un solo
   uso, no se persiste solo por pedirlo).
2. **Sesión** — lo último fijado con `POST /api/session/{id}/behavior-mode`
   (columna `Session.behavior_mode`).
3. **Ajuste global** — `behavior_mode_default` (`src/settings.py`, admin).
4. **`default`** — si nada de lo anterior resuelve.

Un id desconocido en cualquier nivel (typo, modo de usuario borrado, cliente
con caché vieja) se registra con `logger.warning` y se prueba el siguiente
nivel — **nunca** rompe el turno. `regenerate_chat_response` no acepta
override de petición: siempre respeta el modo ya persistido en la sesión.

## Checks: heurísticos, y por qué

`check_response(mode, text)` es un **detector**, no un juez: nunca modifica
la respuesta, solo informa qué comprobó y qué no se cumplió.

- `confidence_tags` — exige al menos una etiqueta `[Certain]`/`[Likely]`/
  `[Guessing]` (o `[Seguro]`/`[Probable]`/`[Suposición]`) en algún punto del
  texto.
- `first_sentence: "challenge"` — la primera frase no puede abrir con una
  fórmula de acuerdo (`"yes"`, `"tienes razón"`, `"exactly"`…).
- `forbidden_phrases` — subcadena, sin distinguir mayúsculas, **sobre el
  texto sin bloques de código** (una frase prohibida dentro de un ```
  fenced block``` no cuenta — sería un falso positivo constante en modos
  usados para trabajar con código).
- `ends_with_question` — hay un `?` en el último párrafo.
- `max_questions` — cuenta de `?` en todo el texto.
- `max_words` — cuenta de palabras.

Texto vacío no dispara ninguna comprobación. Un modo sin `checks` (la
mayoría de los builtin) nunca añade un `mode_check` vacío a sus respuestas
— `routes/chat_helpers.py::save_assistant_response` solo estampa
`metadata.mode_check` cuando `checked` no está vacío.

**Ningún test puede verificar que el modelo real obedezca estas
instrucciones** — eso es exactamente lo que `mode_check` mide en vivo, turno
a turno. "El modelo no siguió el modo en esta respuesta" es un dato sobre
esa respuesta, no un fallo de Faustus: el detector solo puede confirmar
señales de superficie (una etiqueta, una frase, un conteo), nunca la
sinceridad o la calidad del razonamiento.

## Crear un modo propio

`POST /api/behavior-modes` con `{id, name: {en, es}, description: {en, es},
prompt, checks}`:

- `id` debe cumplir `^[a-z][a-z0-9_]{1,31}$` y no puede coincidir con uno
  integrado (`409 modes.builtin`).
- `prompt` ≤ 4000 caracteres (`400 modes.invalid`).
- `checks` solo acepta las claves de arriba, con sus tipos — cualquier otra
  clave o un tipo equivocado es `400 modes.invalid`.
- Se guarda en `DATA_DIR/behavior_modes.json` (todos los modos de usuario en
  un único archivo, `{id: modo}`), con escritura atómica
  (`core.atomic_io.atomic_write_json`) — un fallo a mitad de escritura no
  puede dejar el archivo truncado.

`POST /api/behavior-modes/check {mode, text}` deja probar cómo se ve el
resultado de `check_response` contra un texto de ejemplo antes de fijar el
modo en una sesión de verdad — pensado para la vista previa de Ajustes.

`DELETE /api/behavior-modes/{id}` borra un modo propio (`404
modes.not_found` si no es tuyo o no existe, `409 modes.builtin` si es
integrado). `POST /api/behavior-modes/default` (solo admin) cambia el
ajuste global — únicamente a un id integrado, porque el modo global tiene
que existir para cualquier usuario, y un modo de usuario es privado por
diseño.
