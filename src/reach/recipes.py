"""Owned extraction recipes with conservative recovery from changed CSS classes.

Recovery requires an exact stable attribute and an unambiguous match. No fuzzy
similarity, copied document text, embeddings, remote service or generated code.
"""
import asyncio
import hashlib
import json
import threading
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urljoin
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, model_validator
from src.constants import DATA_DIR
from src.contracts.base import now_iso
from src.reach.extract import ExtractField, ExtractRequest, Selector, MAX_BODY_BYTES, _safe_ref, _value, fetch_page
from src.browser_extraction import detect_restricted_access

_LOCK = threading.RLock()
_ATTRS = ('data-testid', 'itemprop', 'name', 'aria-label', 'id')


class RecipeRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    action: Literal['learn', 'extract', 'list', 'delete'] = 'extract'
    recipe_id: str | None = Field(default=None, pattern=r'^[a-zA-Z0-9_-]{1,64}$')
    url: str | None = Field(default=None, max_length=2048)
    html: str | None = Field(default=None, max_length=MAX_BODY_BYTES)
    row_selectors: list[Selector] = Field(default_factory=list, max_length=5)
    fields: dict[str, ExtractField] = Field(default_factory=dict, max_length=30)
    max_items: int = Field(default=50, ge=1, le=200)
    transport: Literal['fetch', 'browser'] = 'fetch'
    profile: str = Field(default='research', pattern=r'^[a-zA-Z0-9_-]{1,64}$')

    @model_validator(mode='after')
    def valid_request(self):
        if self.action != 'list' and not self.recipe_id: raise ValueError('recipe_id is required')
        if self.action in ('learn', 'extract'):
            if not self.url or urlsplit(self.url).scheme not in ('http', 'https'): raise ValueError('HTTP(S) url is required')
        if self.action == 'learn':
            if not self.fields or any(not f.selectors for f in self.fields.values()): raise ValueError('learn needs HTML field selectors')
            ExtractRequest(html='', fields=self.fields, row_selectors=self.row_selectors)
        return self


def _root(owner):
    return Path(DATA_DIR) / 'reach-recipes' / hashlib.sha256(str(owner or '').encode()).hexdigest()


def _site(url):
    p = urlsplit(url)
    if p.username or p.password: raise ValueError('URL credentials are not allowed')
    return (p.scheme.lower(), p.netloc.lower(), p.path or '/')


def _matches(scope, anchor, limit):
    return scope.find_all(anchor['tag'], attrs={anchor['key']: anchor['value']}, limit=limit)


def _anchor(nodes, scopes, *, row=False):
    if not nodes or any(n is None for n in nodes): return None
    first = nodes[0]
    for key in _ATTRS:
        value = first.get(key)
        if not isinstance(value, str) or not value or len(value) > 200: continue
        candidate = {'tag': first.name, 'key': key, 'value': value}
        if all(n.name == first.name and n.get(key) == value for n in nodes):
            if row or all(len(_matches(scope, candidate, 2)) == 1 for scope in scopes):
                return candidate
    return None


def _node(row, spec):
    return next((n for selector in spec.selectors if (n := row.select_one(selector)) is not None), None)


def _run(request, owner):
    path = _root(owner) / ((request.recipe_id or 'unused') + '.json')
    if request.action == 'list':
        paths = sorted(_root(owner).glob('*.json'))
        return {'results': [{'recipe_id': p.stem} for p in paths[:100]], 'truncated': len(paths) > 100}
    if request.action == 'delete':
        with _LOCK:
            existed = path.exists()
            path.unlink(missing_ok=True)
        return {'recipe_id': request.recipe_id, 'deleted': existed}
    recipe = None
    if request.action == 'extract':
        with _LOCK:
            try: recipe = json.loads(path.read_text(encoding='utf-8'))
            except FileNotFoundError: raise ValueError('recipe not found') from None
        if list(_site(request.url)) != recipe['site']: raise ValueError('recipe is scoped to its original origin and path')
    if request.html is not None:
        body, actual = request.html.encode('utf-8'), request.url
    else:
        if request.transport == 'browser':
            from src.reach.browser import read_response
            response = read_response(request.url, owner=owner, profile=request.profile)
        else: response = fetch_page(request.url)
        response.raise_for_status()
        body, actual = response.content, str(response.url)
    if len(body) > MAX_BODY_BYTES: raise ValueError('document exceeds byte budget')
    if _site(actual) != _site(request.url): raise ValueError('redirect changed recipe origin or path')
    soup = BeautifulSoup(body, 'html.parser')
    if detect_restricted_access(soup.get_text(' ', strip=True)): raise ValueError('restricted page')
    for node in soup.select('script,style,noscript'): node.decompose()
    fields = request.fields if recipe is None else {key: ExtractField.model_validate(value) for key, value in recipe['fields'].items()}
    row_selectors = request.row_selectors if recipe is None else recipe['row_selectors']
    rows = next((found for selector in row_selectors if (found := soup.select(selector, limit=request.max_items + 1))), []) if row_selectors else [soup]
    adapted_rows = False
    if not rows and recipe and recipe['row_anchor']:
        rows = _matches(soup, recipe['row_anchor'], request.max_items + 1)
        adapted_rows = bool(rows)
    if request.action == 'learn':
        if not rows: raise ValueError('row selectors matched no rows')
        samples = rows[:20]
        anchors = {name: _anchor([_node(row, spec) for row in samples], samples) for name, spec in fields.items()}
        row_anchor = _anchor(samples, [soup], row=True) if row_selectors else None
        recipe = {'site': list(_site(actual)), 'row_selectors': row_selectors, 'row_anchor': row_anchor,
            'fields': {name: spec.model_dump() for name, spec in fields.items()}, 'anchors': anchors,
            'learned_at': now_iso(), 'source_sha256': hashlib.sha256(body).hexdigest()}
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists() and len(list(path.parent.glob('*.json'))) >= 100: raise ValueError('recipe limit reached; delete an old recipe first')
            temp = path.with_suffix('.tmp')
            temp.write_text(json.dumps(recipe, ensure_ascii=False), encoding='utf-8')
            temp.replace(path)
        return {'recipe_id': request.recipe_id, 'learned': True, 'adaptable_fields': [name for name, anchor in anchors.items() if anchor],
                'adaptable_rows': bool(row_anchor), 'source_sha256': recipe['source_sha256'], 'untrusted_content': True}
    items, missing, adapted, truncated, total_chars = [], [], [], len(rows) > request.max_items, 0
    for index, row in enumerate(rows[:request.max_items]):
        item = {}
        for name, spec in fields.items():
            node = _node(row, spec)
            if node is None and recipe['anchors'].get(name):
                candidates = _matches(row, recipe['anchors'][name], 2)
                if len(candidates) == 1:
                    node = candidates[0]
                    adapted.append({'row': index, 'field': name, 'method': 'unique_exact_attribute'})
            value = node.get(spec.attribute) if node is not None and spec.attribute else node.get_text(' ', strip=True) if node is not None else None
            if value is None: missing.append({'row': index, 'field': name})
            if value is not None and spec.attribute in ('href', 'src'): value = urljoin(actual, str(value))
            item[name], cut = _value(value)
            truncated |= cut
        size = len(json.dumps(item, ensure_ascii=False))
        if total_chars + size > 500_000:
            truncated = True
            break
        total_chars += size
        items.append(item)
    return {'recipe_id': request.recipe_id, 'url': _safe_ref(actual), 'items': items, 'item_count': len(items),
        'missing': missing[:100], 'missing_count': len(missing), 'adapted': adapted[:100], 'adapted_count': len(adapted),
        'adapted_rows': adapted_rows, 'truncated': truncated, 'source_sha256': hashlib.sha256(body).hexdigest(),
        'untrusted_content': True}


async def run_recipe(request: RecipeRequest, *, owner=None):
    return await asyncio.to_thread(_run, request, owner)
