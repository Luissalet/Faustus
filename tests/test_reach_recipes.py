import asyncio
import json
import pytest
from src.reach import recipes as r


@pytest.fixture
def recipe(tmp_path, monkeypatch):
    monkeypatch.setattr(r, 'DATA_DIR', tmp_path)
    def run(**kwargs):
        return asyncio.run(r.run_recipe(r.RecipeRequest(recipe_id='products', url='https://example.org/catalog', **kwargs), owner='alice'))
    learned = run(action='learn', html='<article class="old" data-testid="product"><b class="title" itemprop="name">Old</b></article>',
                  row_selectors=['.old'], fields={'title': {'selectors': ['.title']}})
    assert learned['adaptable_rows'] and learned['adaptable_fields'] == ['title']
    return tmp_path, run


def test_exact_anchors_recover_changed_classes_and_values(recipe):
    _, run = recipe
    result = run(action='extract', html='<article class="new" data-testid="product"><b class="newtitle" itemprop="name">New value</b></article>')
    assert result['items'] == [{'title': 'New value'}]
    assert result['adapted_rows'] and result['adapted_count'] == 1


def test_ambiguous_or_missing_anchor_refuses_invention(recipe):
    _, run = recipe
    result = run(action='extract', html='<article data-testid="product"><b itemprop="name">A</b><b itemprop="name">B</b></article>')
    assert result['items'] == [{'title': None}] and result['missing_count'] == 1
    assert not result['adapted']


def test_recipe_scope_owner_no_document_values_and_delete(recipe):
    folder, run = recipe
    stored = next(folder.rglob('products.json')).read_text(encoding='utf-8')
    assert '>Old<' not in stored and '"Old"' not in stored
    with pytest.raises(ValueError, match='scoped'):
        asyncio.run(r.run_recipe(r.RecipeRequest(recipe_id='products', url='https://other.org/catalog', html=''), owner='alice'))
    with pytest.raises(ValueError, match='not found'):
        asyncio.run(r.run_recipe(r.RecipeRequest(recipe_id='products', url='https://example.org/catalog', html=''), owner='bob'))
    assert run(action='delete')['deleted']
    assert not run(action='delete')['deleted']
