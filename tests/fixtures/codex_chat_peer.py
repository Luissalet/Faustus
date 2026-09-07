"""An inert stdio peer: no model requests, credentials or native tools."""
import json
import sys
import time

scenario = sys.argv[1]


def emit(value):
    print(json.dumps(value), flush=True)


for line in sys.stdin:
    message = json.loads(line)
    method = message.get('method')
    result = {}
    if method == 'initialized':
        continue
    if method == 'initialize' and scenario == 'oversized':
        print('x' * 2_000_001, flush=True)
        continue
    if method == 'initialize' and scenario == 'invalid':
        print('not-json', flush=True)
        continue
    if method == 'config/read':
        result = {'config': {'mcp_servers': {'a.b': {'command': 'never-run'}}}}
        if scenario == 'provider':
            result['config']['model_providers'] = {'openai': {'base_url': 'https://untrusted.invalid'}}
    if method == 'account/read':
        result = {'account': {'type': 'apiKey' if scenario == 'wrong-account' else 'chatgpt'}}
    if method == 'thread/start':
        params = message['params']
        assert params['environments'] == [] and params['ephemeral'] is True
        assert params['dynamicTools'] == [] and params['selectedCapabilityRoots'] == []
        assert params['config']['mcp_servers']['a.b']['enabled'] is False
        assert params['approvalPolicy'] == 'never' and params['sandbox'] == 'read-only'
        result = {'thread': {'id': 'thread-fixture'}, 'modelProvider': 'openai',
                  'approvalPolicy': 'never', 'sandbox': {'type': 'readOnly'}}
        if scenario == 'permissions':
            result['sandbox']['type'] = 'dangerFullAccess'
    if method == 'turn/start':
        if scenario in ('hang', 'stderr'):
            if scenario == 'stderr':
                sys.stderr.write('synthetic diagnostics' * 100_000)
                sys.stderr.flush()
            time.sleep(30)
            continue
        if scenario == 'exit':
            sys.exit(0)
        if scenario == 'request':
            emit({'id': 100, 'method': 'item/commandExecution/requestApproval', 'params': {}})
            continue
        if scenario == 'rpc-error':
            emit({'id': message['id'], 'error': {'message': 'synthetic-secret-must-not-leak'}})
            continue
        result = {'turn': {'id': 'turn-fixture'}}
        if scenario != 'early-events':
            emit({'id': message['id'], 'result': result})
        def item(ident, text, phase=None, kind='agentMessage'):
            emit({'method': 'item/completed', 'params': {'threadId': 'thread-fixture',
                'turnId': 'turn-fixture', 'item': {'id': ident, 'type': kind, 'text': text, 'phase': phase}}})
        item('progress', 'Still working', 'commentary')
        if scenario == 'empty-items':
            for index in range(20):
                item(f'empty-{index}', '', 'commentary')
        if scenario == 'repeat-events':
            for index in range(20):
                item('same', '', 'commentary')
        if scenario == 'replacement':
            item('answer', 'x' * 12, 'final_answer')
        item('answer', 'Hola, English ✓', 'final_answer', 'commandExecution' if scenario == 'native-tool' else 'agentMessage')
        emit({'method': 'turn/completed', 'params': {'threadId': 'thread-fixture',
            'turn': {'id': 'foreign' if scenario == 'foreign-turn' else 'turn-fixture',
                     'status': 'failed' if scenario == 'failed' else 'completed', 'error': None}}})
        if scenario == 'early-events':
            emit({'id': message['id'], 'result': result})
        continue
    emit({'id': message['id'], 'result': result})
