"""Live public-page smoke for local browser capture and durable crawl; no account required."""
import asyncio
import json
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.reach.browser import BrowserRequest, run_browser
from src.reach.crawl import CrawlRequest, run_crawl


async def main():
    owner = 'local-adoption-smoke'
    result = {}
    try:
        result['browser_read'] = await run_browser(BrowserRequest(url='https://example.com'), owner=owner)
        result['capture'] = await run_browser(BrowserRequest(action='capture', url='https://httpbin.org/json', endpoint_contains='/json'), owner=owner)
    finally:
        await run_browser(BrowserRequest(action='close'), owner=owner)
    started = run_crawl(CrawlRequest(urls=['https://example.com'], max_pages=2, max_depth=1), owner=owner)
    end = time.monotonic() + 45
    while time.monotonic() < end:
        status = run_crawl(CrawlRequest(action='status', run_id=started['run_id']), owner=owner)
        if status['status'] in ('completed', 'cancelled', 'paused'): break
        await asyncio.sleep(.5)
    else:
        run_crawl(CrawlRequest(action='cancel', run_id=started['run_id']), owner=owner)
        raise TimeoutError('crawl smoke budget')
    assert status['status'] == 'completed' and status['pages'] >= 1, status
    result['crawl'] = status
    result['export'] = run_crawl(CrawlRequest(action='export', run_id=started['run_id']), owner=owner)
    path = Path(__file__).resolve().parents[1] / 'docs/adaptations/local-research-smoke-2026-10-03.json'
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({'browser_title': result['browser_read']['title'], 'json_responses': result['capture']['captured_count'],
                      'crawl_pages': status['pages'], 'evidence': str(path)}))


if __name__ == '__main__': asyncio.run(main())
