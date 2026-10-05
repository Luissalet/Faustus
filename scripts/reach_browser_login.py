"""Human login in a dedicated local research profile; never copies normal Chrome cookies."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.reach.browser import BrowserRequest, browser_for
from src.reach.extract import _safe_ref


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url', help='Public site to open for your own manual login')
    parser.add_argument('--owner', required=True, help='Your exact Faustus user ID')
    parser.add_argument('--profile', default='research')
    parser.add_argument('--timeout', type=int, default=600)
    args = parser.parse_args()
    request = BrowserRequest(url=args.url, profile=args.profile)
    if not 30 <= args.timeout <= 1800:
        parser.error('timeout must be 30..1800 seconds')
    browser = browser_for(args.owner, request.profile)
    try:
        result = browser.open_for_human(request.url, timeout_s=args.timeout)
        print(json.dumps({'profile': request.profile, 'closed_by_user': result.get('closed_by_user'),
                          'channel': result.get('channel'), 'final_url': _safe_ref(result.get('final_url') or request.url),
                          'navigation_failed': bool(result.get('goto_error'))}, ensure_ascii=False))
    finally:
        browser.close()


if __name__ == '__main__':
    main()
