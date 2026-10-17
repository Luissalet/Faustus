"""The Agora board drawn in Objectives and in a room: the Hub's four columns."""
from routes.hoard_hub_routes import compact_board


def test_board_groups_statuses_like_the_hub_and_trims_done():
    tasks = [
        {'id': 1, 'title': 'Open one', 'status': 'open', 'priority': 2, 'updated': 5},
        {'id': 2, 'title': 'Claimed', 'status': 'claimed', 'owner': 'claude', 'priority': 1, 'updated': 4,
         'activity': {'state': 'recent'}, 'body': 'x' * 2000},
        {'id': 3, 'title': 'Changes asked', 'status': 'changes', 'priority': 1, 'updated': 9},
        {'id': 4, 'title': 'In review', 'status': 'review', 'reviewer': 'codex'},
        {'id': 5, 'title': 'Approved', 'status': 'approved', 'closed_at': 10},
        {'id': 6, 'title': 'Done', 'status': 'done', 'closed_at': 20},
        {'id': 7, 'title': 'Dropped', 'status': 'dropped'},
    ]
    board = compact_board({'ok': True, 'space_id': 'silver', 'tasks': tasks, 'counts': {'done': 1},
                           'agents': [{'id': 'claude', 'state': 'working', 'doing': '#2', 'secret': 'no'}]}, done_limit=1)
    columns = board['columns']
    assert [t['id'] for t in columns['open']] == [1]
    assert [t['id'] for t in columns['in_progress']] == [3, 2]  # same priority: most recently updated first
    assert [t['id'] for t in columns['review']] == [4]
    assert [t['id'] for t in columns['done']] == [6] and board['done_total'] == 2
    assert board['dropped'] == 1 and board['space_id'] == 'silver'
    assert len(columns['in_progress'][1]['body']) == 600 and columns['in_progress'][1]['activity'] == 'recent'
    assert board['agents'] == [{'id': 'claude', 'state': 'working', 'doing': '#2', 'stale': None}]
