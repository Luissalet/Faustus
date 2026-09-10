"""QA-44 · Teclado y zoom (docs/spec/v2/acceptance_scenarios.json).

Estimulo: completar chat, permisos y diff solo teclado a 200% zoom.
Resultado exigido (literal): "Foco util, controles accesibles y ningun boton
critico fuera de alcance."

Requisitos: A11Y-01, A11Y-03.

Estado: manual. A11Y-01 (atajos remapeables, paleta de comandos, trampa de
foco en dialogos via Radix) ya tiene cobertura e2e con Playwright
(tests/e2e/test_composer_shortcuts.py, tests/e2e/test_studio_baseline.py),
pero ese arnes corre a zoom 100% con un viewport fijo. Verificar que NINGUN
boton critico (enviar, aprobar/denegar, aceptar/rechazar diff) queda fuera
del viewport a 200% de zoom, navegando solo con teclado, requiere un
navegador real redimensionado/zoomeado interactivamente - no algo que un
test headless de Playwright con viewport fijo reproduzca por si solo sin
una configuracion dedicada que hoy no existe.

Pasos manuales:
1. En un navegador real, poner el zoom al 200%.
2. Completar un turno de chat usando solo el teclado (Tab/Shift+Tab/Enter).
3. Llegar a una tarjeta de aprobacion y resolverla solo con teclado.
4. Abrir una vista de diff y aceptar/rechazar un cambio solo con teclado.
5. En cada paso, comprobar que el elemento con foco es visible en el
   viewport (no queda recortado) y que ningun boton critico requiere hacer
   scroll horizontal para alcanzarlo.
"""
import pytest

pytestmark = pytest.mark.qa_state("manual")


@pytest.mark.skip(reason="hardware/navegador real: verificar zoom 200% y "
                         "alcance de teclado interactivamente; ver pasos "
                         "manuales en el docstring del modulo.")
def test_keyboard_only_at_200_percent_zoom_manual_walkthrough():
    pass
