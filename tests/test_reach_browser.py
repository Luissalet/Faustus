import asyncio
import json
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
import pytest
from src.reach import browser as b


class Response:
    def __init__(self, data, url='https://example.org/api/feed?token=private', status=200, mime='application/json'):
        self.url, self.status = url, status
        self.headers = {'content-type': mime}
        self._body = json.dumps(data).encode()
    def body(self): return self._body


class Page:
    def __init__(self, responses): self.responses, self.closed = responses, False
    def on(self, event, callback): self.callback = callback
    def goto(self, *args, **kwargs):
        for response in self.responses: self.callback(response)
    def wait_for_timeout(self, _): pass
    def evaluate(self, code): assert code == 'window.scrollTo(0, document.body.scrollHeight)'
    def inner_text(self, _): return 'Public feed'
    def close(self): self.closed = True


def test_capture_bounds_dedup_and_private_request_material(monkeypatch):
    page = Page([Response({'items': [1]}), Response({'items': [1]}), Response({'items': [2]}),
                 Response({'secret': 'failed'}, status=403), Response({'ignored': True}, mime='text/html')])
    @contextmanager
    def session(**kwargs): yield SimpleNamespace(new_page=lambda: page)
    owners = []
    monkeypatch.setattr(b, 'browser_for', lambda owner, profile: owners.append((owner, profile)) or SimpleNamespace(browser_session=session))
    monkeypatch.setattr('src.hoard_link.web.safety.check_url', lambda *_: None)
    result = b.capture(b.BrowserRequest(action='capture', url='https://example.org/', endpoint_contains='/api/feed', max_responses=1), owner='alice')
    assert owners == [('alice', 'research')] and page.closed
    assert result['captured_count'] == 1 and result['truncated']
    assert 'token=private' not in json.dumps(result)
    assert result['results'][0]['data'] == {'items': [1]}


def test_private_top_level_url_refused_before_opening(monkeypatch):
    monkeypatch.setattr(b, 'browser_for', lambda *_: SimpleNamespace(browser_session=lambda **_: pytest.fail('must not open')))
    with pytest.raises(ValueError):
        b.capture(b.BrowserRequest(action='capture', url='http://127.0.0.1/', endpoint_contains='/api'))


def test_read_preserves_final_url_and_refuses_errors(monkeypatch):
    fr = SimpleNamespace(error='', status=200, text='<h1>Hi</h1>', final_url='https://example.org/final', content_type='text/html')
    monkeypatch.setattr(b, 'browser_for', lambda *_: SimpleNamespace(fetch=lambda *a, **k: fr))
    assert str(b.read_response('https://example.org/').url) == fr.final_url
    fr.error = 'browser unavailable'
    with pytest.raises(ValueError, match='unavailable'): b.read_response('https://example.org/')


def test_profiles_do_not_escape_or_share_owners(tmp_path, monkeypatch):
    monkeypatch.setattr(b, 'DATA_DIR', tmp_path)
    assert b.profile_path('alice') != b.profile_path('bob')
    with pytest.raises(ValueError): b.BrowserRequest(action='status', profile='../normal-chrome')


def test_mirror_off_default_never_contacts_jina(monkeypatch):
    from src.reach import web
    monkeypatch.setattr(web, 'get_setting', lambda k, default=None: default)
    monkeypatch.setattr(web, 'make_client', lambda **_: pytest.fail('must not contact third party'))
    with pytest.raises(web.ReachBackendError, match='off'):
        asyncio.run(web.JinaReaderBackend().read('https://example.org/'))


def test_cancelled_queued_reader_never_fetches_after_profile_becomes_free(monkeypatch):
    visits, started, release = [], threading.Event(), threading.Event()
    def read(url, **kwargs):
        visits.append(url)
        started.set()
        release.wait(2)
        return SimpleNamespace(text='<h1>Done</h1>', url=url)
    monkeypatch.setattr(b, '_read_response', read)
    async def scenario():
        first = asyncio.create_task(b.run_browser(b.BrowserRequest(url='https://example.org/first'), owner='queue-test'))
        assert await asyncio.to_thread(started.wait, 2)
        second = asyncio.create_task(b.run_browser(b.BrowserRequest(url='https://example.org/second'), owner='queue-test'))
        await asyncio.sleep(.1)
        second.cancel()
        with pytest.raises(asyncio.CancelledError): await second
        release.set()
        await first
        await asyncio.sleep(.15)
    asyncio.run(scenario())
    assert visits == ['https://example.org/first']


def test_profile_queue_wait_has_a_deadline():
    with b._profile_use('deadline-test', 'research', 1):
        started = time.monotonic()
        with pytest.raises(ValueError, match='timed out'):
            with b._profile_use('deadline-test', 'research', .1): pytest.fail('must not enter')
        assert time.monotonic() - started < .5
