"""Darwin's panel on a project's overview comes through the hub's family proxy: the answer is unwrapped, and a
closed app, a refused call or an odd body become {'ok': False, 'error'} instead of an exception."""
from routes.hoard_hub_routes import darwin_panel_result


def test_answer_is_unwrapped():
    raw = {'ok': True, 'app': 'darwin', 'tool': 'darwin_panel', 'status': 200,
           'result': {'scope': 'app', 'app': {'app_id': 'silver', 'name': "Silver's Hoard"}, 'top': [], 'gaps': []}}
    out = darwin_panel_result(raw)
    assert out['ok'] is True and out['scope'] == 'app' and out['app']['app_id'] == 'silver'


def test_failures_say_why():
    assert darwin_panel_result(None) == {'ok': False, 'error': "Darwin's Hoard did not answer"}
    closed = darwin_panel_result({'ok': False, 'status': 502, 'error': 'darwin is not running'})
    assert closed == {'ok': False, 'error': 'darwin is not running'}
    odd = darwin_panel_result({'ok': True, 'result': {'error': 'Unknown tool: darwin_panel'}})
    assert odd == {'ok': False, 'error': 'Unknown tool: darwin_panel'}
    assert darwin_panel_result({'ok': True, 'result': 'text'})['ok'] is False
