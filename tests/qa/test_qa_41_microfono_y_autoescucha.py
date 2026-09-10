"""QA-41 · Microfono y autoescucha (docs/spec/v2/acceptance_scenarios.json).

Estimulo: abrir voz sin permiso, luego concederlo y reproducir respuesta.
Resultado exigido (literal): "No grabacion inicial; TTS no genera nueva
orden; Stop detiene captura."

Requisitos: VOICE-01, VOICE-02.

Estado: manual. Requiere un microfono/altavoz reales y un navegador con
permisos de `getUserMedia` concedidos/denegados interactivamente
(`studio/src/voice/{VoiceOrb,VoicePanel,engine,audio}.tsx/ts`) - no hay forma
de simular hardware de audio real ni el dialogo de permisos del navegador
desde pytest sin hardware.

Pasos manuales:
1. Abrir el panel de voz SIN haber concedido permiso de microfono todavia;
   comprobar que no arranca ninguna grabacion (ni un blob de audio en la red)
   antes de que el usuario conceda el permiso.
2. Conceder el permiso; hablar y comprobar que la transcripcion arranca solo
   entonces.
3. Reproducir la respuesta por TTS y comprobar en las DevTools que el propio
   audio de salida no se retranscribe como una nueva orden del usuario
   (autoescucha).
4. Pulsar Stop durante la captura y comprobar que el microfono deja de grabar
   de inmediato (indicador del navegador de microfono activo se apaga).
"""
import pytest

pytestmark = pytest.mark.qa_state("manual")


@pytest.mark.skip(reason="hardware real: microfono, altavoz y permisos de "
                         "navegador interactivos; ver pasos manuales en el "
                         "docstring del modulo.")
def test_voice_capture_and_playback_manual_walkthrough():
    pass
