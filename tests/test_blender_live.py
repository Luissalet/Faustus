"""Protocol framing, persistent results and native/official MCP composition."""
import asyncio
import json
import socket
import threading
import time

import pytest

from src.blender_live import client as live


def socket_server(response, received):
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    port = listener.getsockname()[1]

    def serve():
        with listener:
            stream, _ = listener.accept()
            with stream:
                data = bytearray()
                while b'\0' not in data:
                    data.extend(stream.recv(37))
                received.append(json.loads(data.partition(b'\0')[0]))
                for index in range(0, len(response), 19):
                    stream.sendall(response[index:index+19])
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return port, thread


def test_official_protocol_handles_multibyte_and_large_fragmented_output(tmp_path):
    value = 'ámbito árbol ☀' * 1500
    body = json.dumps({'status':'ok', 'result':{'value':value}}, ensure_ascii=False).encode() + b'\0'
    received = []
    port, thread = socket_server(body, received)
    result = live.BridgeClient(port=port, directory=tmp_path).execute("result={'value':'prueba ñ'}")
    thread.join(3)
    assert result['result']['value'] == value
    assert received == [{'type':'execute', 'code':"result={'value':'prueba ñ'}", 'strict_json':True}]


def test_truncated_response_cannot_be_reported_as_a_completed_call(tmp_path):
    port, thread = socket_server(b'{"status":"ok","result":{', [])
    with pytest.raises(RuntimeError, match='servidor MCP oficial'):
        live.BridgeClient(port=port, directory=tmp_path).execute('result={}')
    thread.join(3)


def test_engine_error_is_preserved(tmp_path):
    body = json.dumps({'status':'error','message':'Invalid socket identifier'}).encode() + b'\0'
    port, thread = socket_server(body, [])
    with pytest.raises(RuntimeError, match='Invalid socket identifier'):
        live.BridgeClient(port=port, directory=tmp_path).execute('result={}')
    thread.join(3)


def test_connection_discovery_ignores_obsolete_custom_http_metadata(tmp_path, monkeypatch):
    monkeypatch.delenv('BLENDER_MCP_PORT', raising=False)
    monkeypatch.delenv('FAUSTUS_BLENDER_PORT', raising=False)
    live.write_json(tmp_path/'connection.json', {'port':12345, 'service':'old-http-bridge'})
    assert live.BridgeClient(directory=tmp_path).port == 9876
    live.write_json(tmp_path/'connection.json', {'port':12345, 'service':'blender-lab-mcp'})
    assert live.BridgeClient(directory=tmp_path).port == 12345
    assert live.BridgeClient(directory=tmp_path, port=23456).port == 23456


def test_cancel_is_durable_and_does_not_rewrite_inflight_stage_results(tmp_path):
    job_id = 'a'*32
    record = {'id':job_id, 'state':'running','created_at':time.time(),'result':{'stages':[{'name':'model'}]}}
    live.write_json(tmp_path/'jobs'/(job_id+'.json'), record)
    client = live.BridgeClient(directory=tmp_path)
    assert client.cancel(job_id)['cancel_requested']
    assert (tmp_path/'jobs'/(job_id+'.cancel')).is_file()
    assert client.job(job_id) == record


def test_a_lost_worker_is_not_replayed_or_mistaken_for_another_process(tmp_path, monkeypatch):
    import psutil
    job_id = 'b'*32
    record = {'id':job_id,'state':'running','created_at':time.time()-60,'result':{'partial':True}}
    path = tmp_path/'jobs'/(job_id+'.json')
    live.write_json(path, record)
    live.write_json(path.with_suffix('.pid'), {'pid':87654})
    class UnrelatedProcess:
        def cmdline(self):
            return ['another-program.exe']
    monkeypatch.setattr(psutil, 'Process', lambda pid: UnrelatedProcess())
    client = live.BridgeClient(directory=tmp_path)
    first = client.job(job_id)
    assert first['state'] == 'interrupted' and first['result'] == record['result']
    assert client.job(job_id) == first


def test_native_mcp_preserves_official_structured_output(monkeypatch):
    import mcp_servers.blender_scene_server as server
    from src.blender_live import official
    from mcp.types import TextContent
    structured = {'status':'ok','result':{'version':'5.2.2'}}
    async def call(name, args):
        assert name == 'get_objects_summary'
        return ([TextContent(type='text',text=json.dumps(structured))], structured)
    monkeypatch.setattr(official, 'call', call)
    result = asyncio.run(server.call_tool('blender_official_get_objects_summary', {}))
    assert result[1] == structured


def test_official_failures_are_native_errors_with_the_original_message(monkeypatch):
    import mcp_servers.blender_scene_server as server
    from src.blender_live import official
    async def call(name, args):
        raise RuntimeError('No VIEW_3D context')
    monkeypatch.setattr(official, 'call', call)
    result = asyncio.run(server.call_tool('blender_official_render_viewport_to_path', {}))
    assert result.isError and 'No VIEW_3D context' in result.content[0].text


def test_live_module_does_not_load_unrelated_agent_catalogue():
    # This independent interpreter follows the actual native MCP import path.
    # Loading the whole agent catalogue previously stalled a real stdio caller.
    import subprocess
    import sys
    code = 'import sys; import src.blender_scene.tool; assert "src.agent_tools" not in sys.modules; assert "numpy" not in sys.modules'
    subprocess.run([sys.executable,'-c',code],check=True,stdin=subprocess.DEVNULL,timeout=15)


def test_blender_failure_envelope_is_a_failure_in_both_native_surfaces(monkeypatch):
    import mcp_servers.blender_scene_server as server
    from src.blender_live import official
    from src.blender_live.service import execute_live
    from mcp.types import TextContent
    payload = {'status':'error','message':'Unknown object'}
    async def call(name, args):
        return ([TextContent(type='text',text=json.dumps(payload))], payload)
    monkeypatch.setattr(official, 'call', call)
    mcp = asyncio.run(server.call_tool('blender_official_get_object_detail_summary', {'name':'missing'}))
    assert mcp.isError and mcp.structuredContent == payload
    native = asyncio.run(execute_live('official_call', {'name':'get_object_detail_summary','arguments':{'name':'missing'}}))
    assert native['exit_code'] == 1 and 'Unknown object' in native['output']
