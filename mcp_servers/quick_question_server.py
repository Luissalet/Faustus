"""Configure or request the desktop quick window on the active Faustus."""
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool
from mcp_servers.app_dock_server import _headers
from src.constants import internal_api_base
from src.stdio_guard import guard

server = Server('quick_question')


@server.list_tools()
async def list_tools():
    return [Tool(name='quick_question', description=(
        'Read or configure the desktop quick-question shortcut, or request that the running desktop '
        'opens its floating Studio window. An optional existing session_id continues that same chat. '
        'Open records a request; it does not prove that a desktop is connected or that the window was displayed. '
        'Does not read the clipboard, send a message or start a model.'), inputSchema={
            'type': 'object', 'properties': {
                'action': {'type': 'string', 'enum': ['status', 'configure', 'open'], 'default': 'status'},
                'enabled': {'type': 'boolean'}, 'hotkey': {'type': 'string', 'minLength': 1},
                'session_id': {'type': 'string'},
            }, 'additionalProperties': False})]


@server.call_tool()
async def call_tool(name, arguments):
    if name != 'quick_question':
        raise ValueError('Unknown tool')
    action = arguments.get('action', 'status')
    if action not in {'status', 'configure', 'open'}:
        raise ValueError('Unknown action')
    base = (os.environ.get('FAUSTUS_INTERNAL_API_URL') or os.environ.get('FAUSTUS_URL') or internal_api_base()).rstrip('/')
    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
        patch = {}
        if action == 'configure':
            if 'enabled' in arguments:
                if not isinstance(arguments['enabled'], bool):
                    raise ValueError('enabled must be a boolean')
                patch['quick_question_enabled'] = arguments['enabled']
            if 'hotkey' in arguments:
                if not isinstance(arguments['hotkey'], str) or not arguments['hotkey'].strip():
                    raise ValueError('hotkey must be a nonempty string')
                patch['quick_question_hotkey'] = arguments['hotkey'].strip()
        if action == 'open':
            sid = arguments.get('session_id', '')
            if not isinstance(sid, str):
                raise ValueError('session_id must be a string')
            # Check visibility through the normal API before asking a desktop
            # to show an existing chat. The same owner rules still apply.
            if sid:
                response = await client.get(base + '/api/sessions', headers=_headers())
                response.raise_for_status()
                data = response.json()
                rows = data if isinstance(data, list) else data.get('sessions', [])
                if not any(row.get('id') == sid for row in rows):
                    raise ValueError('Session is not visible to this connection')
            patch['quick_question_request'] = {'id': str(uuid.uuid4()), 'session_id': sid}
        response = await (client.post(base + '/api/auth/settings', json=patch, headers=_headers()) if patch
                          else client.get(base + '/api/auth/settings', headers=_headers()))
        response.raise_for_status()
        settings = response.json()
        result = {'enabled': settings.get('quick_question_enabled', True),
                  'hotkey': settings.get('quick_question_hotkey', 'Ctrl+Shift+Space'),
                  'desktop_required': True}
        if action == 'open':
            query = urlencode({'s': sid}) if sid else ''
            result.update(requested=True, request_id=patch['quick_question_request']['id'],
                          session_id=sid, studio_path='/studio' + ('?' + query if query else ''),
                          floating_path='/studio?quick=1' + ('&' + query if query else ''))
        return [TextContent(type='text', text=json.dumps(result, ensure_ascii=False))]


async def main():
    async with stdio_server() as (read, write):
        with guard():
            await server.run(read, write, server.create_initialization_options())


if __name__ == '__main__':
    asyncio.run(main())
