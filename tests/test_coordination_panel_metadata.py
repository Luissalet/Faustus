"""Browser regression of the actual controls with faithful nullable REST metadata.

This isolated component fixture does not replace the separately recorded native
API/MCP smoke test. It needs no app, model, user data or GPU.
"""
import json
import re
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]

@pytest.mark.skipif(not shutil.which('node') or not (ROOT/'node_modules/esbuild/lib/main.js').exists(),reason='node and esbuild required')
def test_project_and_connector_controls_accept_nullable_catalogue(tmp_path):
    playwright=pytest.importorskip('playwright.sync_api')
    preset={'id':'writer','name':'Writer','purpose':'Writing','capabilities':[],'placeholders':[],'defaults':{}}
    def row(cid,server,name='Writer',state='available'):
        return {'id':cid,'preset_id':'writer','preset':{**preset,'name':name},'values':{},'server':server,
                'status':{'state':state,'app':{'reachable':None},'adapter':{'mcp_status':'disconnected','tool_count':0},'reasons':[]}}
    def server(sid,enabled=True):return {'id':sid,'name':sid,'is_enabled':enabled,'status':'connected','tool_count':1}
    rows=[row('unconfigured',None),row('managed',server('good')),
          row(None,server('legacy')),row('off',server('offline'),state='app_off'),
          row('disabled',server('disabled',False),state='disabled'),row('outside',server('outside'))]
    state={'template':None,'usable':False,'unavailable':None}
    choice={'label':'QA repo','repo':'QA','resources':['path:QA'],'path':'QA workspace'}
    entry=tmp_path/'entry.tsx'
    entry.write_text("""import React,{useState} from 'react';import {createRoot} from 'react-dom/client';import {MemoryRouter} from 'react-router';
import {CoordinationPanel} from '"""+(ROOT/'studio/src/screens/project/CoordinationPanel').as_posix()+"""';
import {ConnectorPicker} from '"""+(ROOT/'studio/src/screens/connectors/ConnectorPicker').as_posix()+"""';
import {NewConnectorForm} from '"""+(ROOT/'studio/src/screens/connectors/NewConnectorForm').as_posix()+"""';
import {ConnectorToolsDrawer} from '"""+(ROOT/'studio/src/screens/connectors/ToolsDrawer').as_posix()+"""';
const preset="""+json.dumps(preset)+""";const unconfigured={id:'unconfigured',preset,values:{},server:null,status:{state:'unconfigured'}};
function App(){const [drawer,setDrawer]=useState(false);return <MemoryRouter><button onClick={()=>setDrawer(true)}>Open null tools</button><CoordinationPanel project={{id:'qa',connector_ids:['good','legacy','offline','disabled']}} onNotice={()=>{}}/>
<ConnectorPicker value={null} effective={['good']} onChange={()=>{}}/>
<NewConnectorForm preset={preset} existing={unconfigured} onClose={()=>{}} onSaved={()=>{}}/>
{drawer&&<ConnectorToolsDrawer connector={unconfigured} onClose={()=>setDrawer(false)}/>}</MemoryRouter>;}createRoot(document.getElementById('root')).render(<App/>);""",encoding='utf8')
    # Resolve dependencies from the actual repository while the entry stays private.
    build="const {build}=require('esbuild');build({entryPoints:[process.argv[1]],nodePaths:[process.argv[3]],bundle:true,platform:'browser',jsx:'automatic',format:'iife',outfile:process.argv[2],loader:{'.css':'empty'},logLevel:'silent'}).catch(e=>{console.error(e);process.exit(1)});"
    run=subprocess.run(['node','-e',build,str(entry),str(tmp_path/'app.js'),str(ROOT/'node_modules')],cwd=ROOT,capture_output=True,text=True,timeout=90)
    assert run.returncode==0,run.stdout+run.stderr
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            if self.path=='/app.js':body=(tmp_path/'app.js').read_bytes();kind='text/javascript'
            elif self.path.startswith('/api/app-connectors'):body=json.dumps(rows).encode();kind='application/json'
            elif self.path.startswith('/api/launch-profiles'):body=b'[]';kind='application/json'
            elif '/choices?' in self.path:
                available='connector_id=good' in self.path
                body=json.dumps({'choices':[choice] if available else [],'unavailable':None if available else 'offline'}).encode();kind='application/json'
            elif self.path.startswith('/api/coordination/projects/qa'):body=json.dumps(state).encode();kind='application/json'
            else:body=b'<div id="root"></div><script src="/app.js"></script>';kind='text/html'
            self.send_response(200);self.send_header('Content-Type',kind+'; charset=utf-8');self.end_headers();self.wfile.write(body)
        def do_PUT(self):
            data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state['template']={'operations':['poll'],**data,'id':'template','revision':1,'project_id':'qa','actor':'product'}
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(state).encode())
    httpd=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=httpd.serve_forever,daemon=True);thread.start()
    try:
        with playwright.sync_playwright() as p:
            browser=p.chromium.launch(headless=True);page=browser.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            try:
                page.goto(f'http://127.0.0.1:{httpd.server_port}')
                page.wait_for_function("()=>document.querySelector('[data-testid=project-coordination]')",timeout=5000);assert errors==[],errors
                panel=page.get_by_test_id('project-coordination');select=panel.get_by_test_id('coordination-connector')
                select.wait_for(state='attached');assert select.locator('option').count()==6
                assert select.locator('option[value="unconfigured"]').get_attribute('disabled') is not None,select.inner_html()
                assert select.locator('option[value="disabled"]').get_attribute('disabled') is not None
                assert select.locator('option[value="outside"]').count()==0
                assert panel.get_by_role('link',name='Open connections').count()==1
                assert page.get_by_test_id('connector-picker-item-unconfigured').is_disabled()
                assert page.locator('#conn-name').input_value()=='Writer'
                select.select_option('good');panel.get_by_role('checkbox',name=re.compile('QA repo')).check()
                panel.get_by_test_id('coordination-save').click()
                panel.get_by_role('button',name='Change',exact=True).wait_for()
                assert state['template']['connector_id']=='good' and state['template']['resources']==['path:QA']
                rows[:]=[r for r in rows if (r.get('server') or {}).get('id')!='good'];page.reload()
                panel.get_by_text('Saved connection unavailable',exact=False).wait_for()
                panel.get_by_role('button',name='Disable coordination',exact=True).click()
                try:panel.get_by_text('Coordination disabled',exact=True).wait_for(timeout=5000)
                except Exception:raise AssertionError({'state':state,'text':panel.inner_text(),'errors':errors})
                assert state['template']['resources']==['path:QA'] and state['template']['active'] is False
                assert state['template']['operations']==['poll']
                page.get_by_role('button',name='Open null tools').click()
                assert page.get_by_test_id('connector-tools-refresh').is_disabled()
                assert page.get_by_test_id('connector-tools-dialog').get_by_text('Not set up',exact=True).count()==1
                assert errors==[],errors
            finally:browser.close()
    finally:httpd.shutdown();httpd.server_close()
