"""Browser interaction checks and viewport evidence for the actual QuickTools component."""
import argparse
import json
from pathlib import Path
from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:5178/static/studio/tests/quick-tools.preview.html')
    args = parser.parse_args()
    folder = Path(__file__).resolve().parents[1] / '.impeccable/review/quick-tools'
    folder.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='msedge', headless=True)
        context = browser.new_context(locale='es-ES')
        context.add_init_script("localStorage.setItem('faustus_studio_lang', 'es');")
        page = context.new_page()
        failures = []
        page.on('pageerror', lambda e: failures.append(str(e)))
        page.goto(args.url)
        draft = page.locator('#draft')
        panel = page.get_by_test_id('quick-tool')
        panel.wait_for()
        panel.get_by_role('checkbox').first.check()
        assert panel.get_by_role('checkbox').first.is_checked()
        for name, value in [('list', 'lista: Revisar fuentes; Contrastar fechas; Guardar resultados'),
                            ('calculation', '= (18 + 4) * 3 / 2'),
                            ('conversion', '10 km a millas'), ('color', '#86c5a6'),
                            ('split', 'reparte 10,01 € entre 3 personas'), ('timer', 'timer 90 segundos')]:
            for device, width, height in [('desktop', 1280, 800), ('mobile', 390, 844)]:
                page.set_viewport_size({'width': width, 'height': height})
                draft.fill(value)
                page.wait_for_function("([kind]) => document.querySelector('[data-testid=quick-tool]')?.dataset.kind === kind", arg=[name])
                assert page.locator('body').evaluate('(node) => node.scrollWidth <= window.innerWidth')
                page.screenshot(path=str(folder / f'{device}-{name}.png'), full_page=True)
        panel.get_by_role('button', name='Empezar', exact=True).click()
        draft.fill('Esto es texto normal')
        page.wait_for_timeout(350)
        assert panel.get_attribute('data-kind') == 'timer'
        panel.get_by_role('button', name='Pausar', exact=True).click()
        panel.get_by_role('button', name='Cerrar herramienta rápida').click()
        assert panel.count() == 0
        draft.fill('timer 1 segundo')
        panel.wait_for()
        panel.get_by_role('button', name='Empezar', exact=True).click()
        page.get_by_role('status').filter(has_text='Temporizador terminado').wait_for(timeout=4000)
        panel.get_by_role('button', name='Cerrar herramienta rápida').click()
        draft.fill('= 1 / 0')
        panel.get_by_role('status').filter(has_text='División entre cero').wait_for()
        assert panel.get_by_role('button', name='Usar en el mensaje').count() == 0
        draft.fill('= 6 * 7')
        panel.get_by_role('button', name='Usar en el mensaje').wait_for()
        panel.get_by_role('button', name='Usar en el mensaje').click()
        assert draft.input_value() == '42'  # modifies draft, never submits it
        assert not failures, failures
        browser.close()
    print(json.dumps({'component': 'QuickTools', 'viewports': [1280, 390], 'states_captured': 6,
                      'interactions_passed': True, 'screenshots': str(folder)}))


if __name__ == '__main__': main()
