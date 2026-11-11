"""Opt-in acceptance: real extension + stdio MCP in an isolated browser profile.

Run directly, outside pytest. Uses only an owned loopback fixture and output directory.
No running app or installed user extension is changed. --host-rules maps this
isolated browser's fixed extension relay URL to the ephemeral test listener.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests.browser_relay.test_mcp import StdioClient
from src.browser_relay.protocol import EXTENSION_ID

HTML = b'''<!doctype html><html lang="es"><title>Browser session acceptance</title>
<style>body{font:20px system-ui;margin:40px;background:#e8e2d8}button,input{font:inherit;padding:12px;margin:10px}#clock{color:#712d35}</style>
<h1>Faustus browser fixture</h1><p id="clock">Ready</p><label>Draft<input aria-label="Draft"></label>
<button id="next" onclick="document.querySelector('#clock').textContent='Clicked';fetch('/data?query=one')">Next step</button>
<a href="/redirect">Redirect</a><script>setInterval(()=>document.body.style.backgroundColor=Date.now()%1000>500?'#e8e2d8':'#dbd3c4',300)</script></html>'''


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/redirect':
            self.send_response(302); self.send_header('Location', '/'); self.end_headers(); return
        body = b'{"answer":"captured"}' if self.path.startswith('/data') else HTML
        self.send_response(200); self.send_header('Content-Type', 'application/json' if self.path.startswith('/data') else 'text/html')
        self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)

    def log_message(self, *args):
        pass


async def run(output: Path, executable: str | None):
    output.mkdir(parents=True, exist_ok=True)
    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True); thread.start()
    url = f'http://127.0.0.1:{http.server_port}/'
    env = {**os.environ, 'PYTHONPATH': str(ROOT), 'PYTHONIOENCODING': 'utf-8'}
    process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'src.browser_relay.server', '--port', '0', '--peer-wait', '8',
        cwd=ROOT, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    client = StdioClient(process)
    transcript, context, browser = [], None, None
    async def call(args, ok=True):
        response = await client.call(args)
        data = response['structuredContent']; transcript.append({'arguments': args, 'response': data})
        (output / 'mcp.json').write_text(json.dumps(transcript, indent=2, ensure_ascii=False), encoding='utf-8')
        assert data['ok'] is ok, data
        assert '"frames": [' not in json.dumps(response), 'Private JPEG frames leaked into MCP'
        return data.get('result', data)
    try:
        port = int((await asyncio.wait_for(process.stderr.readline(), 60)).decode().strip().rsplit(':', 1)[1])
        await client.rpc('initialize', {'protocolVersion': '2024-11-05', 'capabilities': {}, 'clientInfo': {'name': 'browser-journeys-live', 'version': '1'}})
        process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'); await process.stdin.drain()
        catalog = await client.rpc('tools/list'); (output / 'catalog.json').write_text(json.dumps(catalog, indent=2), encoding='utf-8')
        async with async_playwright() as pw:
            async with contextlib.AsyncExitStack() as cleanup:
                profile = cleanup.enter_context(tempfile.TemporaryDirectory(prefix='faustus-browser-profile-'))
                browser = pw.chromium
                launch = dict(headless=True, args=[f'--disable-extensions-except={ROOT / "browser-extension"}', f'--load-extension={ROOT / "browser-extension"}',
                    f'--host-rules=MAP 127.0.0.1:7016 127.0.0.1:{port}', '--no-first-run'], ignore_default_args=['--disable-extensions'], viewport={'width': 1000, 'height': 720})
                if executable: launch['executable_path'] = executable
                context = await cleanup.enter_async_context(await browser.launch_persistent_context(profile, **launch))
                worker = context.service_workers[0] if context.service_workers else await context.wait_for_event('serviceworker', timeout=15000)
                assert EXTENSION_ID in worker.url, worker.url
                page = await context.new_page(); await page.goto(url)
                tabs = (await call({'action': 'tabs'}))['tabs']; tab = next(tab for tab in tabs if tab['url'] == url); tab_id = tab['id']
                await call({'action': 'snapshot', 'tab_id': tab_id, 'view': {'text': True}})
                await call({'action': 'session_save', 'tab_id': tab_id, 'name': 'fixture session'})
                await call({'action': 'har_start', 'tab_id': tab_id, 'include_bodies': True})
                await call({'action': 'recording_start', 'tab_id': tab_id, 'output_path': str(output / 'walkthrough.webm'), 'cursor': True})
                await asyncio.sleep(.8)
                snapshot = await call({'action': 'snapshot', 'tab_id': tab_id})
                button = next(node['ref'] for node in snapshot['nodes'] if node.get('role') == 'button')
                await call({'action': 'click', 'tab_id': tab_id, 'ref': button})
                await page.locator('[data-faustus-recording-cursor]').wait_for(state='visible')
                await asyncio.sleep(.6)
                await page.screenshot(path=str(output / 'recorded-cursor.png'))
                await call({'action': 'handoff', 'tab_id': tab_id, 'reason': 'Completa el paso de prueba y pulsa Continuar.'})
                blocked = await call({'action': 'navigate', 'tab_id': tab_id, 'url': url + 'blocked'}, ok=False)
                assert blocked['error']['code'] == 'human_turn'
                panel = await context.new_page(); await panel.goto(f'chrome-extension://{EXTENSION_ID}/panel.html')
                await panel.get_by_role('button', name='Continuar', exact=True).wait_for()
                await panel.emulate_media(color_scheme='dark')
                await panel.screenshot(path=str(output / 'handoff.png'))
                await panel.emulate_media(color_scheme='light')
                await panel.screenshot(path=str(output / 'handoff-light.png'))
                await panel.locator('body').evaluate("body => body.style.fontSize = '200%'")
                assert await panel.evaluate('document.body.scrollWidth <= innerWidth')
                await panel.screenshot(path=str(output / 'handoff-scaled.png'))
                await panel.locator('body').evaluate("body => body.style.fontSize = ''")
                # DOM click is untrusted and MUST leave the turn pending.
                await panel.get_by_role('button', name='Continuar', exact=True).evaluate('(button) => button.click()')
                assert (await call({'action': 'handoff_status', 'tab_id': tab_id}))['turn']['status'] == 'waiting'
                # Browser-generated input is the same path as a person's click.
                await panel.get_by_role('button', name='Continuar', exact=True).click()
                await panel.get_by_text('No hay ninguna intervención pendiente.').wait_for()
                assert (await call({'action': 'handoff_status', 'tab_id': tab_id}))['turn']['status'] == 'continued'
                await panel.close()
                # Adoption request uses the card and saves the name only after the click.
                await call({'action': 'adopt_tab', 'tab_id': tab_id, 'name': 'ceded fixture'})
                panel = await context.new_page(); await panel.goto(f'chrome-extension://{EXTENSION_ID}/panel.html')
                await panel.locator('article').get_by_role('button', name='Ceder pestaña', exact=True).click()
                await panel.get_by_text('No hay ninguna intervención pendiente.').wait_for(); await panel.close()
                await call({'action': 'navigate', 'tab_id': tab_id, 'url': url + 'redirect'})
                await call({'action': 'har_stop', 'tab_id': tab_id, 'output_path': str(output / 'network.har')})
                await asyncio.sleep(.8)
                failed = await call({'action': 'click', 'tab_id': tab_id, 'ref': 'stale-owned-ref'}, ok=False)
                assert failed['error']['code'] == 'stale_ref' and failed['failure_recording']['complete']
                await call({'action': 'last_minute', 'tab_id': tab_id, 'output_path': str(output / 'last-minute.webm')})
                await call({'action': 'recording_stop', 'tab_id': tab_id})
                assert await page.locator('[data-faustus-recording-cursor]').count() == 0
                await page.screenshot(path=str(output / 'fixture.png'))
                await call({'action': 'release', 'tab_id': tab_id})
                names = await call({'action': 'sessions'}); assert len(names['sessions']) == 2, names
                await call({'action': 'recording_start', 'tab_id': tab_id, 'output_path': str(output / 'disconnect.webm')})
                await asyncio.sleep(.8)
                await context.close(); context = None
                lost = await call({'action': 'snapshot', 'tab_id': tab_id}, ok=False)
                assert lost['failure_recording']['complete'] and lost['error']['code'] == 'extension_unavailable', lost
                await call({'action': 'last_minute', 'tab_id': tab_id, 'output_path': str(output / 'disconnected-minute.webm')})
                # Same isolated profile, fresh browser process: names persist and URL restores.
                context = await cleanup.enter_async_context(await browser.launch_persistent_context(profile, **launch))
                await asyncio.sleep(2)
                names = await call({'action': 'sessions'}); assert len(names['sessions']) == 2, names
                restored = await call({'action': 'session_open', 'name': 'ceded fixture'}); assert restored['tab']['url'] == url
                await call({'action': 'wait', 'tab_id': restored['tab']['id'], 'seconds': 1, 'view': {'text': True}})
                await call({'action': 'session_forget', 'name': 'fixture session'})
                await call({'action': 'release', 'tab_id': restored['tab']['id']})
                await context.close(); context = None
        har = json.loads((output / 'network.har').read_text(encoding='utf-8'))
        assert any(entry['response']['status'] == 302 for entry in har['log']['entries'])
        assert any('captured' in entry['response']['content'].get('text', '') for entry in har['log']['entries'])
        files = {path.name: {'bytes': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()} for path in output.iterdir() if path.suffix in {'.webm', '.har', '.png'}}
        (output / 'receipt.json').write_text(json.dumps({'passed': True, 'mcp_calls': len(transcript), 'files': files}, indent=2), encoding='utf-8')
        print(json.dumps({'passed': True, 'mcp_calls': len(transcript), 'artifacts': list(files)}))
    finally:
        if context:
            with contextlib.suppress(Exception):
                await context.close()
        if process.returncode is None:
            process.stdin.close()
            try: await asyncio.wait_for(process.wait(), 8)
            except asyncio.TimeoutError: process.kill(); await process.wait()
        http.shutdown(); http.server_close(); thread.join()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--output', required=True); parser.add_argument('--browser')
    options = parser.parse_args(); asyncio.run(run(Path(options.output).resolve(), options.browser))
