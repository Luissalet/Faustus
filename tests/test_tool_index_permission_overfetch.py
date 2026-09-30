"""Bounded H17 permission-aware vector widening on real ToolIndex logic."""
import pytest
from src import tool_index as ti
from tests.test_tool_index_permission_cutoff import Lane, index

class CountingLane(Lane):
    def __init__(self, hits):
        super().__init__(hits)
        self.encodes = 0
    def encode(self, texts):
        self.encodes += 1
        return super().encode(texts)


def test_legacy_without_filter_keeps_single_window():
    lane = CountingLane([(f'denied_{i}', .99-i*.001) for i in range(30)])
    assert index([lane]).retrieve('synthetic', k=1) == ['denied_0']
    assert lane.requests == [24] and lane.encodes == 1


@pytest.mark.parametrize('backend', ['test', ti.BACKEND_MEMORY, ti.BACKEND_CHROMA])
def test_allowed_after_original_window_survives_vector_and_fusion(backend, monkeypatch):
    lane = CountingLane([(f'denied_{i}', .99-i*.001) for i in range(30)] + [('allowed', .8)])
    idx = index([lane], backend=backend)
    monkeypatch.setattr(idx, '_strong_lexical_anchor', lambda query: None)
    assert idx.retrieve('synthetic', k=1, candidate_filter=lambda name:name == 'allowed') == ['allowed']
    assert lane.requests == [24, 31] and lane.encodes == 1


def test_all_denied_stops_at_explicit_cap():
    lane = CountingLane([(f'denied_{i}', .99) for i in range(1000)])
    assert index([lane]).retrieve('synthetic', k=1, candidate_filter=lambda name:False) == []
    assert lane.requests == [24,48,96,192,256] and lane.encodes == 1


def test_allowed_beyond_cap_remains_explicit_residual():
    lane = CountingLane([(f'denied_{i}', .99) for i in range(256)] + [('allowed', .9)])
    assert index([lane]).retrieve('synthetic', k=1, candidate_filter=lambda name:name == 'allowed') == []
    assert lane.requests[-1] == 256


def test_direct_stops_when_k_unique_allowed_found():
    lane = CountingLane([('allowed', .99)] + [(f'denied_{i}', .8) for i in range(100)])
    assert index([lane]).retrieve('synthetic', k=1, candidate_filter=lambda name:name == 'allowed') == ['allowed']
    assert lane.requests == [24]


def test_short_backend_reply_terminates_even_when_count_overstates():
    lane = CountingLane([('denied', .9)])
    lane.count = lambda:1000
    assert index([lane]).retrieve('synthetic', k=1, candidate_filter=lambda name:False) == []
    assert lane.requests == [24]


def test_filter_error_never_widens_authority():
    lane = CountingLane([('denied', .9),('allowed', .8)])
    def broken(name):
        raise RuntimeError('synthetic predicate failure')
    with pytest.raises(RuntimeError, match='synthetic predicate'):
        index([lane]).retrieve('synthetic', k=1, candidate_filter=broken)
    assert lane.requests == [2]


def test_fusion_lexical_permission_filter_reaches_corpus_before_cut(monkeypatch):
    lane = CountingLane([('denied_vector', .9)])
    idx = index([lane], backend=ti.BACKEND_MEMORY)
    idx._corpus = {'builtin': {**{f'denied_{i:02d}':'synthetic permission needle' for i in range(24)},
        'zzz_allowed':'synthetic permission needle'}, 'mcp':{}}
    monkeypatch.setattr(idx, '_strong_lexical_anchor', lambda query:None)
    assert 'zzz_allowed' not in idx.lexical_retrieve('synthetic permission needle', k=24)
    assert idx.retrieve('synthetic permission needle', k=1, candidate_filter=lambda name:name == 'zzz_allowed') == ['zzz_allowed']


def test_large_k_keeps_absolute_permission_window_cap():
    lane = CountingLane([(f'denied_{i}', .99) for i in range(400)])
    assert index([lane]).retrieve('synthetic', k=100, candidate_filter=lambda name:False) == []
    assert lane.requests == [256] and lane.encodes == 1
    assert len(index([lane]).retrieve('synthetic', k=100)) == 100
    assert lane.requests[-1] == 300


def test_score_floor_terminates_without_deeper_irrelevant_query(monkeypatch):
    lane = CountingLane([('denied', .9)] + [(f'irrelevant_{i}', .1) for i in range(100)])
    monkeypatch.setattr(ti, 'TOOL_SCORE_FLOOR', {lane.model:.5})
    assert index([lane]).retrieve('synthetic', k=1, candidate_filter=lambda name:False) == []
    assert lane.requests == [24]
