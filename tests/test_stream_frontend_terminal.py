"""Run the production TS decoder in Node memory; no app/browser or model needed."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('wire,incomplete', [
    ('data: {"delta":"partial"}\n\n', True),
    ('',True),
    ('data: {"delta":"partial"}\n\ndata: {"delta":"unfinished"',True),
    ('data: {"delta":"complete"}\n\ndata: [DONE]\n\n',False),
    ('data: {"type":"chat_terminal"}\n\n',False),
    ('data: {"type":"paused"}\n\n',False),
    ('data: {"type":"cancelled"}\n\n',False),
    ('event: error\ndata: {"error":"stalled","error_class":"timeout.inactivity"}\n\n',False),
    ('data: {"type":"error","error":"typed failure"}\n\n',False),
    ('data: {"type":"agent_terminal","data":{"failure":{"message":"failed"}}}\n\n',False),
    ('data: {"type":"finish","finish_reason":"tool_calls"}\n\n',True),
])
def test_eof_requires_an_explicit_done_or_terminal(wire, incomplete):
    node = shutil.which('node')
    if not node or not (ROOT/'node_modules/esbuild/lib/main.js').is_file():
        pytest.skip('Node and local esbuild are required')
    script = r'''
      const fs=require('node:fs'),vm=require('node:vm'),esbuild=require('esbuild');
      const source=fs.readFileSync(process.argv[1],'utf8').replace(/\r\n/g,'\n');
      function fn(start){const i=source.indexOf(start);if(i<0)throw Error(start);return source.slice(i,source.indexOf('\n}\n',i)+2).replace(/^export /,'');}
      const code=esbuild.transformSync(fn('function str(')+'\n'+fn('export function getVersionMismatch(')+'\n'+fn('function errorTraceFields(')+'\n'+fn('export function decode(')+'\n'+fn('async function* streamEvents('),{loader:'ts',target:'es2020'}).code;
      const ctx={TextDecoder,Uint8Array,ReadableStream,t:s=>s,_versionMismatch:null};vm.createContext(ctx);vm.runInContext(code,ctx);
      (async()=>{const body=new ReadableStream({start(c){c.enqueue(new TextEncoder().encode(process.argv[2]));c.close();}});
      const events=[];for await(const event of ctx.streamEvents(body))events.push(event);console.log(JSON.stringify(events));})();
    '''
    process = subprocess.run([node,'-e',script,str(ROOT/'studio/src/adapters/chat.ts'),wire],
                             capture_output=True,text=True,encoding='utf-8',timeout=10,cwd=ROOT)
    assert process.returncode == 0,process.stderr
    events = json.loads(process.stdout)
    errors = [e for e in events if e.get('errorClass') == 'transport.incomplete_stream']
    assert bool(errors) is incomplete,events
    assert events[-1]['type'] == 'done'
    if '"delta":"partial"' in wire:
        assert events[0]['type'] == 'delta' and events[0]['text'] == 'partial'
