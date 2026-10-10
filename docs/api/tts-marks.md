# Marcas de la voz sintetizada: palabras y visemas - `POST /api/tts/synthesize`

Implementación: `services/tts/marks.py` (cálculo), `routes/tts_routes.py` (API),
`services/tts/command_voice.py` y `services/tts/tts_service.py` (lo que informa
el motor), `studio/src/voice/visemes.ts` (lectura en el navegador),
`studio/src/voice/VoiceOrb.tsx` y `VoiceSubtitle.tsx` (esfera y subtítulo).

## Para qué sirve

La esfera de Jarvis toma la forma de la boca del sonido que suena y el
subtítulo ilumina la palabra que se dice. Para ello el servidor devuelve, junto
al audio, cuándo empieza y acaba cada palabra y qué forma de boca (visema)
tiene cada tramo. Cualquier otra aplicación puede usarlo igual: por ejemplo, el
botón «Escuchar» de Links podría resaltar la palabra que suena.

## Petición

`POST /api/tts/synthesize` con el cuerpo de siempre más:

| Campo | Valor | Efecto |
|---|---|---|
| `marks` | `true` (por defecto `false`) | además del audio, devuelve las marcas |
| `format` | tiene que ser `"base64"` con `marks: true` | audio y marcas viajan en un solo JSON; con `"audio"` la API responde 422 |
| `language` | `es`, `en`, `es-ES`... | elige las reglas de letras de los visemas; vacío o `auto`, se adivina por el texto |

Sin `marks` nada cambia: `format: "audio"` sigue devolviendo el audio en
binario y `format: "base64"` sigue devolviendo `{"audio": "..."}`.

## Respuesta con `marks: true`

```json
{
  "audio": "<WAV o MP3 en base64>",
  "mime": "audio/wav",
  "marks": {
    "version": 1,
    "language": "es",
    "duration_ms": 5279,
    "timing": "audio",
    "words_source": "estimated",
    "visemes_source": "estimated",
    "viseme_set": ["rest", "AA", "E", "I", "O", "U", "MBP", "FV", "L", "OTHER"],
    "words": [
      {"i": 0, "text": "Hola", "start_char": 0, "end_char": 4, "start_ms": 170, "end_ms": 470},
      {"i": 1, "text": "qué", "start_char": 7, "end_char": 10, "start_ms": 880, "end_ms": 1055}
    ],
    "visemes": [
      {"v": "rest", "start_ms": 0, "end_ms": 170},
      {"v": "O", "start_ms": 170, "end_ms": 285},
      {"v": "L", "start_ms": 285, "end_ms": 347}
    ]
  },
  "marks_ms": 3,
  "synth_ms": 912
}
```

- `words`: una entrada por palabra del texto (los signos de los bordes no
  cuentan). `start_char`/`end_char` son posiciones en **unidades UTF-16** del
  texto enviado (lo que cuenta `String.slice` en JavaScript): `texto.slice(start_char, end_char) === text`.
  `start_ms`/`end_ms` son milisegundos desde el principio del audio; en orden,
  sin solaparse, dentro de `[0, duration_ms]`.
- `visemes`: tramos consecutivos y sin solaparse. Los huecos de 40 ms o más
  (silencios, pausas) vienen como `rest`; donde no hay entrada la boca está
  cerrada.
- Los diez visemas: `rest` boca cerrada, `AA`, `E`, `I`, `O`, `U` vocales,
  `MBP` labios cerrados (m, b, p; en español también v), `FV` diente sobre
  labio (f; en inglés también v), `L` y `OTHER` el resto de consonantes.
- `words_source` / `visemes_source`: `"engine"` si el dato lo informó el motor
  de voz, `"estimated"` si se calculó a partir del audio y del texto.
- `timing`: `"audio"` si la duración sale del audio; `"text_only"` si el audio
  no se pudo leer (ni WAV ni cabecera MPEG) y se estimó por el texto.
- `marks` es `null` si el cálculo falla: el audio nunca se pierde por eso.
- `marks_ms` es lo que tardó el cálculo (1-3 ms para una frase de 5 s).

## De dónde salen las marcas

1. **El motor informa.** Una plantilla de comando (`tts_command_template`) puede
   incluir `{marks_path}`: la ruta de un JSON que el comando escribe junto al
   audio. Formatos admitidos (todo opcional, lo que no cuadra con el texto o con
   el audio se ignora):
   - `words`: `[{"text", "start_ms", "end_ms", "phonemes"?}]`, una por palabra del
     texto → tiempos de palabra del motor (`words_source: "engine"`);
   - `phonemes`: `[{"p", "start_ms", "end_ms"}]` para todo el audio (símbolos IPA
     o códigos ARPAbet) → visemas del motor (`visemes_source: "engine"`);
   - `segments`: `[{"text", "phonemes"?, "start_ms", "end_ms"}]`, trozos
     consecutivos del texto con su sitio en el audio (así los informa el motor
     neuronal local, con los fonemas separados por palabra) → cada trozo ancla
     la estimación y los fonemas dan los visemas.
2. **Estimación a partir del audio.** Sin ayuda del motor: el audio WAV (PCM de
   8/16/24/32 bits o coma flotante, mono o varios canales, también con la
   longitud sin rellenar) se corta en tramos hablados por su energía (10 ms por
   fotograma); las pausas largas que coinciden con signos de puntuación del
   texto fijan esa frontera; dentro de cada tramo, las palabras se reparten por
   sílabas y cada frontera se elige entre los valles de energía cercanos (una
   programación dinámica que prefiere valles profundos y que cada palabra
   contenga tantos valles internos como sílabas menos una). Los visemas salen
   de las letras (español e inglés: h muda, qu, gu, ll, rr, ph, e final muda...)
   repartidas por el peso de cada sonido. Solo biblioteca estándar, CPU,
   determinista. Audio MP3: solo duración (cabecera de capa III) y reparto por
   sílabas, sin energía.

## Precisión medida

Contra un motor falso con tiempos conocidos (`tests/fake_tts_marks.py`, 7 frases
en español e inglés, 91 palabras, pausas de coma y de punto): error medio de
frontera 30 ms, mediana 24 ms, percentil 90 de 46 ms, máximo 57 ms; el punto
medio de las 91 palabras cae dentro de la palabra pronunciada. El audio
sintético tiene una envolvente más limpia que la voz real: en voz real los
tiempos serán algo peores, y por eso la interfaz no depende de ellos (si las
marcas no llegan o no cuadran con el texto, la esfera usa el volumen y el
subtítulo se muestra sin resaltar).

Coste: 14 ms para 282 caracteres (24 s de audio); 158 ms para 2.263 caracteres
(187 s). Una frase de voz (50-250 caracteres) cuesta unos pocos milisegundos.

## Uso desde otra aplicación

```js
const r = await fetch('/api/tts/synthesize', {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ text, format: 'base64', marks: true, language: 'es' }),
});
const { audio, mime, marks } = await r.json();
const el = new Audio(`data:${mime};base64,${audio}`);
el.play();
const timer = setInterval(() => {
  const ms = el.currentTime * 1000;
  const now = marks && marks.words.find(w => ms >= w.start_ms && ms < w.end_ms);
  // resalta text.slice(now.start_char, now.end_char)
  if (el.ended) clearInterval(timer);
}, 30);
```

`studio/src/voice/visemes.ts` trae las mismas funciones con validación
(`parseMarks` descarta unas marcas que no describen el texto: iluminar la
palabra equivocada es peor que no iluminar ninguna).

## Verificación

`tests/test_orb_visemes_251.py` (67 pruebas: visemas en español e inglés,
fonemas IPA/ARPAbet, sílabas, desplazamientos UTF-16, tiempos monótonos, dentro
de límites y que cubren todas las palabras con 7 frases, precisión contra la
verdad conocida, audio sin pausas, en silencio, estéreo, 8 bits, coma flotante,
sin longitud, ilegible y MPEG, datos del motor correctos e incoherentes,
contrato `{marks_path}`, forma de la API y que sin `marks` no cambia nada) y
`tests/test_orb_visemes_251_js.py` con `tests/voice_visemes.check.mjs` (la parte
pura del navegador contra marcas reales del servidor).
