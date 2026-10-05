import hashlib
import json
from pathlib import Path
import httpx
import pytest
from src.reach import crawl


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setattr(crawl, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(crawl.threading.Thread, 'start', lambda self: None)
    monkeypatch.setattr(crawl.time, 'sleep', lambda _: None)
    crawl._RUNNING.clear()
    yield tmp_path
    crawl._RUNNING.clear()


def response(url, text, status=200):
    return httpx.Response(status, text=text, headers={'content-type': 'text/html'}, request=httpx.Request('GET', url))


def test_durable_bfs_robots_dedup_owner_and_export(local, monkeypatch):
    calls = []
    def fetch(url):
        calls.append(url)
        if url.endswith('/robots.txt'):
            return response(url, 'User-agent: *\nDisallow: /private')
        if '/next' in url:
            return response(url, '<title>Next</title><h1>Second</h1>')
        return response(url, '<title>First</title><a href="/next?token=secret">next</a>'
            '<a href="/next?token=secret#same">duplicate</a><a href="/private">private</a>'
            '<a href="https://other.example/">offsite</a><h1>First</h1>')
    monkeypatch.setattr(crawl, 'fetch_page', fetch)
    started = crawl.run_crawl(crawl.CrawlRequest(urls=['https://example.org/', 'https://example.org/#dup'],
          fields={'heading': {'selectors': ['h1']}}, delay_s=0.5), owner='alice')
    run_id = started['run_id']
    crawl._RUNNING.clear()  # represents process interruption before first page
    assert crawl.run_crawl(crawl.CrawlRequest(action='status', run_id=run_id), owner='alice')['reason'] == 'process_interrupted'
    crawl.work('alice', run_id)
    result = crawl.run_crawl(crawl.CrawlRequest(action='status', run_id=run_id), owner='alice')
    assert result['status'] == 'completed' and result['pages'] == 2
    assert result['results'][1]['items'] == [{'heading': 'Second'}]
    assert calls == ['https://example.org/robots.txt', 'https://example.org/', 'https://example.org/next?token=secret']
    assert result['errors'][0]['error'] == 'robots_or_transport_refused'
    with pytest.raises(ValueError, match='not found'):
        crawl.run_crawl(crawl.CrawlRequest(action='status', run_id=run_id), owner='bob')
    exported = crawl.run_crawl(crawl.CrawlRequest(action='export', run_id=run_id), owner='alice')
    body = Path(exported['path']).read_bytes()
    assert b'token=secret' not in body and b'frontier' not in body
    assert hashlib.sha256(body).hexdigest() == exported['sha256']


def test_cancel_during_fetch_is_not_overwritten(local, monkeypatch):
    rid = []
    def fetch(url):
        if url.endswith('/robots.txt'):
            return response(url, '', 404)
        crawl.run_crawl(crawl.CrawlRequest(action='cancel', run_id=rid[0]), owner='alice')
        return response(url, '<a href="/next">next</a>')
    monkeypatch.setattr(crawl, 'fetch_page', fetch)
    rid.append(crawl.run_crawl(crawl.CrawlRequest(urls=['https://example.org/']), owner='alice')['run_id'])
    crawl.work('alice', rid[0])
    result = crawl.run_crawl(crawl.CrawlRequest(action='status', run_id=rid[0]), owner='alice')
    assert result['status'] == 'cancelled' and result['pages'] == 1 and result['pending'] == 1


def test_restarted_run_skips_completed_page(local, monkeypatch):
    result = crawl.run_crawl(crawl.CrawlRequest(urls=['https://example.org/']), owner='alice')
    state = crawl._load('alice', result['run_id'])
    state.update(visited=['https://example.org/'], frontier=[['https://example.org/', 0], ['https://example.org/next', 1]])
    crawl._save('alice', state)
    monkeypatch.setattr(crawl, '_robot', lambda *_: (True, 0))
    def fetch(url):
        assert url.endswith('/next')
        return response(url, '<h1>Recovered</h1>')
    monkeypatch.setattr(crawl, 'fetch_page', fetch)
    crawl.work('alice', result['run_id'])
    assert crawl._load('alice', result['run_id'])['visited'] == ['https://example.org/', 'https://example.org/next']


def test_robot_failure_refuses_and_large_delay_pauses(local, monkeypatch):
    monkeypatch.setattr(crawl, 'fetch_page', lambda url: response(url, '', 503))
    rid = crawl.run_crawl(crawl.CrawlRequest(urls=['https://example.org/']), owner='alice')['run_id']
    crawl.work('alice', rid)
    assert crawl._load('alice', rid)['errors'][0]['error'] == 'robots_or_transport_refused'
    monkeypatch.setattr(crawl, '_robot', lambda *_: (True, 120))
    rid = crawl.run_crawl(crawl.CrawlRequest(urls=['https://example.org/']), owner='alice')['run_id']
    crawl.work('alice', rid)
    assert crawl._load('alice', rid)['status'] == 'paused'


def test_cancelled_run_can_explicitly_resume_after_worker_stops(local, monkeypatch):
    rid = crawl.run_crawl(crawl.CrawlRequest(urls=['https://example.org/']), owner='alice')['run_id']
    crawl.run_crawl(crawl.CrawlRequest(action='cancel', run_id=rid), owner='alice')
    crawl._RUNNING.clear()
    assert crawl.run_crawl(crawl.CrawlRequest(action='resume', run_id=rid), owner='alice')['status'] == 'queued'
    monkeypatch.setattr(crawl, '_robot', lambda *_: (True, 0))
    monkeypatch.setattr(crawl, 'fetch_page', lambda url: response(url, '<h1>Resumed</h1>'))
    crawl.work('alice', rid)
    assert crawl._load('alice', rid)['status'] == 'completed'


def test_active_runs_are_bounded_per_owner(local):
    for _ in range(2):
        crawl.run_crawl(crawl.CrawlRequest(urls=['https://example.org/']), owner='alice')
    result = crawl.run_crawl(crawl.CrawlRequest(urls=['https://example.org/']), owner='alice')
    assert result['status'] == 'paused' and result['reason'] == 'active_run_limit'
