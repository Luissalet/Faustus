# Jarvis: conversación por voz en Faustus

Estado: implementación inicial verificada con el alcance indicado abajo. No equivale
a completar todas las fases del plan de inspiración.

## Uso

1. En Ajustes → Voz, configurar reconocimiento y síntesis. En Windows,
   «Preparar voz local en Windows» selecciona Whisper base en CPU y las voces
   instaladas de Windows. Guardar los cambios.
2. Abrir el modo de conversación por voz junto al compositor del chat.
3. Elegir detección automática, español o inglés. Hablar con el botón o Alt+V.
4. Revisar la transcripción y enviarla. Por defecto, hablar no ejecuta una orden
   hasta pulsar Enviar. Las opciones permiten omitir esta revisión y volver a
   escuchar después de cada respuesta, si se desea.

La conversación usa el chat actual, su proyecto, sus herramientas y sus permisos.
Las autorizaciones de acciones sensibles siguen requiriendo la aprobación del
chat; no se conceden por reconocer un «sí» hablado.

Escape silencia el modo de voz. Interrumpir la reproducción no cancela el trabajo
del agente; Cancelar tarea es una acción separada. Al cambiar de chat u ocultar
la pestaña se detienen captura y reproducción, no el trabajo en segundo plano.

## Instalación local

Instalar las dependencias opcionales con el Python del entorno de Faustus:

```powershell
venv/Scripts/python.exe -m pip install -r requirements-voice.txt
```

Whisper descarga el modelo la primera vez; después puede transcribir localmente.
En Windows la síntesis usa System.Speech y voces instaladas del idioma elegido:
no requiere Kokoro, PyTorch ni GPU. «En este servidor» significa el equipo donde
corre Faustus, que puede no ser el dispositivo desde el que se abre el navegador.
En otras plataformas se debe configurar otro proveedor de síntesis compatible.

El navegador necesita permiso de micrófono y un contexto seguro (localhost o
HTTPS). No se cambia automáticamente a un servicio externo cuando falla el local.
El reconocimiento del navegador puede usar su servicio remoto: la interfaz lo
advierte. Los endpoints configurados tienen su propia política de retención.

## Inglés y español

- Whisper detecta el idioma por intervención en modo automático, sin depender
  del idioma de la interfaz. También se puede fijar `es` o `en`.
- El reconocimiento del navegador necesita un idioma fijo; no ofrece la misma
  detección automática que Whisper.
- La salida selecciona idioma mediante una heurística sobre el texto de la
  respuesta, con el idioma reconocido como respaldo. Frases muy cortas o mixtas
  pueden ser ambiguas; no es un clasificador lingüístico completo.
- La política lingüística del modelo de chat sigue siendo responsabilidad del
  flujo de chat: reconocer español no garantiza por sí solo que el LLM lo respete.

## Mapa del código

| Responsabilidad | Archivos |
| --- | --- |
| Integración con el chat y cambio de sesión | `studio/src/screens/Studio.tsx`, `studio/src/screens/studio/Composer.tsx` |
| Estados, revisión, reproducción por frases e interrupción | `studio/src/voice/VoicePanel.tsx` |
| Captura, permisos, cancelación, proveedores y reproducción | `studio/src/voice/audio.ts` |
| Detección de pausas, segmentación e idioma de salida | `studio/src/voice/engine.ts` |
| Esfera reactiva y estilos adaptables | `studio/src/voice/VoiceOrb.tsx`, `studio/src/voice/voice.css` |
| Dictado y lectura compartidos con el resto de la app | `studio/src/adapters/speech.ts` |
| Configuración y traducciones | `studio/src/screens/Settings.tsx`, `docs/ui/i18n/es.tsv` |
| Capacidades sin cargar modelos | `services/speech_runtime.py` |
| Rutas y trabajo de inferencia fuera del bucle del servidor | `routes/stt_routes.py`, `routes/tts_routes.py` |
| Reconocimiento y caché del modelo local | `services/stt/stt_service.py` |
| Síntesis y voces nativas de Windows | `services/tts/tts_service.py`, `services/tts/system_voice.py` |

## Privacidad y límites reales

La captura es temporal; las respuestas de voz omiten la caché de audio en disco.
Los mensajes transcritos enviados siguen las reglas normales del historial.
La síntesis nativa transmite el texto por entrada estándar a un programa fijo,
sin interpolarlo como código. Se comprueba que el proveedor no haya cambiado
entre la selección del cliente y la ejecución del servidor.

La esfera se deforma con la energía real del audio. Su rotación en reposo no
simula escucha. Respeta reducción de movimiento y deja de dibujar en pestañas
ocultas. Los estados también se expresan en texto, no sólo mediante color.

Esta versión usa captura por turnos y síntesis por frases, no audio dúplex en
tiempo real. La detección de pausas es por energía, no Silero VAD. No incluye
activación por «Jarvis» ni interrupción acústica automática mientras habla el
asistente; la interrupción es manual. Cancelar una petición descarta sus
resultados, pero una inferencia nativa ya iniciada puede terminar en el servidor.

## Verificación

```powershell
venv/Scripts/python.exe -m pytest tests/test_voice_routes.py tests/test_studio_voice_js.py -q
npx tsc --noEmit
venv/Scripts/python.exe scripts/voice_smoke.py
```

Las dos primeras comprobaciones se repitieron el 07-09-2026: 20 tests de voz
correctos y comprobación TypeScript correcta. El smoke test genera voz sintética
en inglés y español y la transcribe con Whisper; no prueba un micrófono físico,
el permiso del navegador ni una conversación completa con el LLM.

La ejecución completa final dio 12.318 pruebas correctas, 82 omitidas y un fallo
de ubicación de este documento. Se movió a `docs/design/` y se repitieron las
comprobaciones de documentación sobre el contenido preparado para publicar.
Los 12 fallos de la primera ejecución quedaron resueltos en la segunda.
Los informes `logs/jarvis-release-final.log` y `.xml` son artefactos locales,
no documentación para publicar. No se ha repetido la suite completa después del
cambio de ubicación: la comprobación posterior se limita al área afectada.
