"""Native Windows socket/reset tests; no running app, model or fixed port."""
import ast,asyncio,json,socket,struct,subprocess,sys,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def probe(mode):
 tree=ast.parse((ROOT/'app.py').read_text(encoding='utf8'))
 blocks=[n for n in tree.body if isinstance(n,ast.If)and ast.unparse(n.test)=="sys.platform == 'win32'"]
 assert len(blocks)==1
 def configure():exec(compile(ast.Module(body=blocks,type_ignores=[]),str(ROOT/'app.py'),'exec'),{'asyncio':asyncio,'sys':sys})
 if mode in ('before','repeated'):configure()
 async def main():
  if mode in ('running','repeated'):configure()
  if mode=='repeated':configure()
  loop=asyncio.get_running_loop();events=[];handlers=set()
  def on_error(loop,context):
   exc=context.get('exception');events.append(dict(message=context.get('message'),error=type(exc).__name__,winerror=getattr(exc,'winerror',None)))
  loop.set_exception_handler(on_error)
  async def echo(reader,writer):
   task=asyncio.current_task();handlers.add(task)
   try:
    while line:=await reader.readline():writer.write(line);await writer.drain()
   except (ConnectionError,OSError):pass
   finally:
    writer.close()
    try:await writer.wait_closed()
    except (ConnectionError,OSError):pass
    handlers.discard(task)
  server=await asyncio.start_server(echo,'127.0.0.1',0)
  port=server.sockets[0].getsockname()[1]
  reader,writer=await asyncio.wait_for(asyncio.open_connection('127.0.0.1',port),3)
  async def ping(r,w,label):
   data=(label+'\n').encode();w.write(data);await w.drain();return await asyncio.wait_for(r.readline(),3)==data
  initial=await ping(reader,writer,'before-reset')
  candidates=[]
  try:
   # No loop turn between these clients: B resets while A's accept completes.
   for i in range(2):
    normal=socket.socket();normal.settimeout(2);normal.connect(('127.0.0.1',port));candidates.append(normal)
    reset=socket.socket();reset.settimeout(2);reset.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack('hh',1,0))
    reset.connect(('127.0.0.1',port));reset.close()
   await asyncio.sleep(.2)
   later=False;later_error=None
   try:
    r2,w2=await asyncio.wait_for(asyncio.open_connection('127.0.0.1',port),3)
    later=await ping(r2,w2,'after-reset');w2.close();await w2.wait_closed()
   except (OSError,TimeoutError)as exc:later_error=type(exc).__name__
   preserved=await ping(reader,writer,'held-stream-after-reset')
   child=await asyncio.create_subprocess_exec(sys.executable,'-c',"print('proactor-stdio-ok')",stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
   stdout,stderr=await asyncio.wait_for(child.communicate(),5)
   stdio=child.returncode==0 and stdout.strip()==b'proactor-stdio-ok'and not stderr
   return dict(policy=type(asyncio.get_event_loop_policy()).__name__,loop=type(loop).__name__,port=port,initialClient=initial,newClientAfterRST=later,newClientError=later_error,heldStreamAfterRST=preserved,subprocessStdio=stdio,loopErrors=events)
  finally:
   writer.close()
   try:await writer.wait_closed()
   except(ConnectionError,OSError):pass
   for c in candidates:c.close()
   server.close();await server.wait_closed()
   for t in list(handlers):t.cancel()
   if handlers:await asyncio.gather(*list(handlers),return_exceptions=True)
 
 return asyncio.run(main())


@unittest.skipUnless(sys.platform=='win32','Requires real Windows IOCP')
class AcceptStartupTests(unittest.TestCase):
 def check_startup(self,mode):
  result=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--probe',mode],capture_output=True,text=True,timeout=15,check=True)
  actual=json.loads(result.stdout)
  self.assertTrue(actual['initialClient'],actual)
  self.assertTrue(actual['heldStreamAfterRST'],actual)
  self.assertTrue(actual['newClientAfterRST'],actual)
  self.assertTrue(actual['subprocessStdio'],actual)
  self.assertEqual(actual['loopErrors'],[],actual)
 def test_policy_installed_before_loop(self):self.check_startup('before')
 def test_app_imported_inside_running_loop(self):self.check_startup('running')
 def test_repeated_configuration_preserves_connections(self):self.check_startup('repeated')


@unittest.skipUnless(sys.platform=='win32','Requires real Windows IOCP')
class AcceptLifetimeTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  from src.windows_asyncio import configure_windows_asyncio
  configure_windows_asyncio()
  self.proactor=asyncio.get_running_loop()._proactor
 def listener(self):
  s=socket.socket();s.bind(('127.0.0.1',0));s.listen();s.setblocking(False);self.addCleanup(s.close);return s
 async def test_cancel_closes_candidate_and_listener_can_be_reused(self):
  listener=self.listener();created=[];original=self.proactor._get_accept_socket
  def capture(family):
   peer=original(family);created.append(peer);return peer
  self.proactor._get_accept_socket=capture
  try:
   pending=self.proactor.accept(listener);await asyncio.sleep(0);self.assertEqual(len(created),1)
   pending.cancel()
   with self.assertRaises(asyncio.CancelledError):await pending
   self.assertEqual(created[0].fileno(),-1)
   next_accept=self.proactor.accept(listener)
   with socket.create_connection(listener.getsockname(),timeout=2)as client:
    peer,address=await asyncio.wait_for(next_accept,2)
    try:self.assertEqual(address,client.getsockname())
    finally:peer.close()
  finally:self.proactor._get_accept_socket=original
 async def test_closed_listener_error_is_not_retried(self):
  listener=self.listener();listener.close()
  with self.assertRaises(OSError):
   await asyncio.wait_for(self.proactor.accept(listener),2)


if __name__=='__main__':
 if sys.argv[1:2]==['--probe']:
  print(json.dumps(probe(sys.argv[2])));raise SystemExit(0)
 unittest.main()
