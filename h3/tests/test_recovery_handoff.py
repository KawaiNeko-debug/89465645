from copy import deepcopy
from datetime import datetime
from unittest.mock import Mock

import pytest

from h3.recovery_handoff import (
    guard_recoveries, handoff_path, read_handoff, release_daily, update_handoff,
)
from h3.schedule_guard import has_controller_run_today
from h3.weekly_box_schedule import StateConflict


NOW = datetime.fromisoformat('2026-10-22T07:22:00+08:00')


class MemoryStore:
    def __init__(self):
        self.files = {}
        self.version = 0

    def ensure_branch(self):
        pass

    def read(self, path):
        return deepcopy(self.files.get(path, (None, None)))

    def write(self, path, row, sha=None):
        if self.files.get(path, (None, None))[1] != sha:
            raise StateConflict('changed')
        self.version += 1
        self.files[path] = deepcopy(row), str(self.version)


def run(run_id, title=None, event='workflow_dispatch', **kwargs):
    row = dict(id=run_id, event=event, display_title=title or f'dynamic-controller-{run_id}',
               created_at='2026-10-21T23:22:00Z', status='completed', conclusion='success')
    row.update(kwargs)
    return row


def payload(*runs):
    return {'workflow_runs': list(runs)}


def test_recovery_is_not_a_daily_claim_even_after_it_finishes():
    recovery = run(100, 'gift-recovery-100')
    assert not has_controller_run_today(payload(recovery), NOW)
    assert has_controller_run_today(payload(recovery, run(101)), NOW)
    assert not has_controller_run_today(payload(recovery, run(101)), NOW,
                                        excluded_run_ids=['101'])


def test_no_recovery_does_not_touch_state_service():
    store = Mock()
    assert guard_recoveries(payload(run(101)), NOW, '101', 'repo', 'token', store) == (set(), False)
    store.read.assert_not_called()


def test_active_recovery_defers_daily_instead_of_consuming_its_slot(monkeypatch):
    store = MemoryStore()
    update_handoff(store, '100')
    rows = payload(run(100, 'gift-recovery-100'), run(101))
    excluded, deferred = guard_recoveries(rows, NOW, '101', 'repo', 'token', store)
    assert deferred and excluded == {'101'}
    saved, _ = read_handoff(store, 100)
    assert set(saved) == {'recovery_id', 'completed', 'deferred_run_ids'}
    assert saved['deferred_run_ids'] == ['101']
    update_handoff(store, 100, completed=True)
    rows['workflow_runs'].append(run(102))
    excluded, deferred = guard_recoveries(rows, NOW, '102', 'repo', 'token', store)
    assert not deferred
    assert not has_controller_run_today(rows, NOW, '102', excluded)


def test_existing_daily_chain_is_never_deferred_or_duplicated():
    store = MemoryStore()
    update_handoff(store, 100)
    rows = payload(run(100, 'gift-recovery-100'), run(101), run(102))
    excluded, deferred = guard_recoveries(rows, NOW, '102', 'repo', 'token', store)
    assert not deferred
    assert has_controller_run_today(rows, NOW, '102', excluded)
    assert read_handoff(store, 100)[0]['deferred_run_ids'] == []


def test_pre_patch_recovery_is_supported_and_completed_summary_is_not_blocked(monkeypatch):
    store = MemoryStore()
    rows = payload(run(100, 'gift-recovery-100'), run(101))
    api = Mock(return_value=payload(run(200, 'dynamic-summary-100', status='completed')))
    monkeypatch.setattr('h3.recovery_handoff.api_request', api)
    assert guard_recoveries(rows, NOW, '101', 'repo', 'token', store) == (set(), False)
    api.return_value = payload()
    assert guard_recoveries(rows, NOW, '101', 'repo', 'token', store)[1]


def test_summary_finishing_during_deferral_cas_does_not_lose_daily_entry(monkeypatch):
    store = MemoryStore()
    update_handoff(store, 100)
    original = store.write
    def complete_first(path, row, sha=None):
        store.write = original
        update_handoff(store, 100, completed=True)
        raise StateConflict('summary finished concurrently')
    store.write = complete_first
    monkeypatch.setattr('h3.recovery_handoff.time.sleep', lambda _: None)
    rows = payload(run(100, 'gift-recovery-100'), run(101))
    assert guard_recoveries(rows, NOW, '101', 'repo', 'token', store) == (set(), False)


def test_summary_releases_daily_then_history_blocks_repeated_dispatch(monkeypatch):
    store = MemoryStore()
    update_handoff(store, 100, defer_run_id=101)
    rows = payload(run(100, 'gift-recovery-100'), run(101))
    calls = []
    def api(method, repo, token, path, **kwargs):
        calls.append((method, path, kwargs))
        return rows if method == 'GET' else {}
    monkeypatch.setenv('GITHUB_REPOSITORY', 'example/repo')
    monkeypatch.setenv('GITHUB_TOKEN', 'synthetic-token')
    monkeypatch.setattr('h3.recovery_handoff.api_request', api)
    state = {'gift_recovery': True, 'orchestration_id': '100', 'ref': 'main'}
    assert release_daily(state, store, NOW)
    posts = [call for call in calls if call[0] == 'POST']
    assert len(posts) == 1
    assert posts[0][2]['payload']['inputs'] == {
        'task_start_date': '2026-10-22', 'gift_recovery': 'false'}
    rows['workflow_runs'].append(run(102))
    assert not release_daily(state, store, NOW)
    assert len([call for call in calls if call[0] == 'POST']) == 1


def test_non_recovery_and_stale_daily_entry_cannot_start_a_second_daily_chain(monkeypatch):
    store = MemoryStore()
    assert not release_daily({'gift_recovery': False}, store, NOW)
    assert not store.files
    update_handoff(store, 100, defer_run_id=101)
    monkeypatch.setenv('GITHUB_REPOSITORY', 'example/repo')
    monkeypatch.setenv('GITHUB_TOKEN', 'synthetic-token')
    api = Mock(return_value=payload(run(101, created_at='2026-10-20T23:22:00Z')))
    monkeypatch.setattr('h3.recovery_handoff.api_request', api)
    assert not release_daily({'gift_recovery': True, 'orchestration_id': '100'}, store, NOW)
    assert all(call.args[0] == 'GET' for call in api.call_args_list)


def test_handoff_rejects_unknown_fields():
    store = MemoryStore()
    store.files[handoff_path(100)] = ({'password': 'synthetic'}, '1')
    with pytest.raises(ValueError, match='metadata'):
        read_handoff(store, 100)
