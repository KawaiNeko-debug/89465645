import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from h3.box_lottery import empty_box_lottery, is_box_lottery_required, merge_box_records
from h3.mixed_batches import (
    account_metadata, batch_accounts, encode_chain_state, load_chain_state,
    new_chain_state, start_chain,
)
from h3.mixed_results import applicable_components, build_retry_matrix, missing_result
from h3.report import summarize_box_lottery
from h3.retry_components import retry_components
from h3.weekly_box_schedule import (
    AccountCheckpoint, StateConflict, WeeklyStore, assign_accounts,
    checkpoint_payload, cycle_dates, due, week_id,
)


def accounts(count):
    return [account_metadata('old1', i) for i in range(1, count + 1)]


@pytest.mark.parametrize('count', [1, 7, 8, 100, 220, 221, 440])
@pytest.mark.parametrize('task_date,days', [('2026-10-06', 6), ('2026-10-12', 7)])
def test_distribution_stable_and_balanced(count, task_date, days):
    rows = assign_accounts(accounts(count), task_date)
    dates = cycle_dates(task_date)
    sizes = [sum(row['assigned_date'] == day for row in rows) for day in dates]
    assert len(dates) == days
    assert len(rows) == count
    assert max(sizes) - min(sizes) <= 1
    assert rows == assign_accounts(list(reversed(accounts(count))), dates[-1])
    assert len({(row['source_group'], row['account_index']) for row in rows}) == count
    assert all(row['week_id'] == week_id(task_date) for row in rows)


def test_cutover_scope_and_sunday_bounds(monkeypatch):
    monkeypatch.delenv('WEEKLY_BOX_LOTTERY_ASSIGNED_DATE', raising=False)
    assert assign_accounts(accounts(7), '2026-10-05') == []
    assert not is_box_lottery_required('2026-10-05', 'old1')
    assert cycle_dates('2026-10-06') == [f'2026-10-{i:02d}' for i in range(6, 12)]
    assert cycle_dates('2026-10-12') == [f'2026-10-{i:02d}' for i in range(12, 19)]
    # No default Tuesday full run, even when called outside the controller.
    assert not is_box_lottery_required('2026-10-06', 'old1')
    assert not is_box_lottery_required('2026-10-13', 'old1')
    assert due('2026-10-11', 'old1', '2026-10-06')
    assert not due('2026-10-11', 'old1', '2026-10-05')
    assert not due('2026-10-18', 'old1', '2026-10-06')
    for code in ('old1', 'wudi1', 'ld20', 'yyy2'):
        assert is_box_lottery_required('2026-10-06', code, '2026-10-06')
    for code in ('ll1', 'zh1', 'test', 'gift_test'):
        assert not due('2026-10-06', code, '2026-10-06')
    selected = assign_accounts([account_metadata(code, 1) for code in
                               ('old1', 'wudi1', 'ld1', 'yyy1', 'll1', 'zh1', 'test')],
                              '2026-10-06')
    assert {row['source_group'] for row in selected} == {'old1', 'wudi1', 'ld1', 'yyy1'}


class MemoryStore:
    def __init__(self):
        self.files = {}
        self.version = 0

    def ensure_branch(self):
        pass

    def read(self, path):
        value, sha = self.files.get(path, (None, None))
        return deepcopy(value), sha

    def write(self, path, payload, sha=None):
        _, current = self.read(path)
        if sha != current:
            raise StateConflict('stale version')
        self.version += 1
        self.files[path] = deepcopy(payload), str(self.version)
        return str(self.version)

    freeze = WeeklyStore.freeze


def test_frozen_roster_does_not_change_with_secrets():
    store = MemoryStore()
    first = store.freeze(accounts(8), '2026-10-06')
    assert store.freeze(accounts(221), '2026-10-10') == first
    assert len(store.freeze(accounts(221), '2026-10-12')['accounts']) == 221


def test_invalid_frozen_roster_fails_closed():
    store = MemoryStore()
    roster = store.freeze(accounts(8), '2026-10-06')
    roster['accounts'][0]['assigned_date'] = '2026-10-05'
    store.files['weekly/2026-10-05/roster.json'] = roster, 'changed'
    with pytest.raises(ValueError):
        store.freeze(accounts(8), '2026-10-06')


def test_controller_state_outage_does_not_cancel_daily_chain(monkeypatch, tmp_path):
    monkeypatch.setattr('h3.mixed_batches.configured_group_counts', lambda: {'old1': 8})
    store = MemoryStore()
    store.freeze = Mock(side_effect=RuntimeError('unavailable'))
    monkeypatch.setattr('h3.weekly_box_schedule.WeeklyStore', lambda: store)
    dispatch = Mock()
    monkeypatch.setattr('h3.mixed_batches.dispatch_batch', dispatch)
    output = tmp_path / 'chain.json'
    start_chain(SimpleNamespace(orchestration_id='safe', ref='main',
                               task_start_date='2026-10-06', batch_size=220, output=output))
    state = json.loads(output.read_text(encoding='utf-8'))
    assert state['total_accounts'] == 8 and state['weekly_schedule_error']
    assert 'weekly_assignments' not in state
    dispatch.assert_called_once()


def test_checkpoint_key_rotation_never_discards_prior_draw(monkeypatch):
    monkeypatch.setenv('WEEKLY_BOX_STATE_KEY', 'synthetic-encryption-key')
    account = assign_accounts(accounts(1), '2026-10-06')[0]
    store = MemoryStore()
    checkpoint = AccountCheckpoint(store, account)
    checkpoint.save(empty_box_lottery())
    monkeypatch.setenv('WEEKLY_BOX_STATE_KEY', 'rotated-synthetic-key')
    with pytest.raises(RuntimeError, match='could not be decrypted'):
        AccountCheckpoint(store, account)


def test_controller_keeps_all_daily_accounts_and_batches(monkeypatch, tmp_path):
    monkeypatch.setattr('h3.mixed_batches.configured_group_counts', lambda: {'old1': 221, 'll1': 1})
    store = MemoryStore()
    monkeypatch.setattr('h3.weekly_box_schedule.WeeklyStore', lambda: store)
    dispatch = Mock()
    monkeypatch.setattr('h3.mixed_batches.dispatch_batch', dispatch)
    output = tmp_path / 'chain.json'
    start_chain(SimpleNamespace(orchestration_id='safe', ref='main',
                               task_start_date='2026-10-06', batch_size=220, output=output))
    state = load_chain_state(encode_chain_state(json.loads(output.read_text(encoding='utf-8'))))
    assert state['total_accounts'] == 222
    assert len(state['weekly_assignments']) == 221
    assert [b['account_count'] for b in state['batches']] == [220, 2]
    rows = sum([batch_accounts(state, b['batch_id']) for b in state['batches']], [])
    assert len(rows) == 222
    peer = next(row for row in rows if row['source_group'] == 'll1')
    assert 'assigned_date' not in peer
    dispatch.assert_called_once()
    assert dispatch.call_args.args[1] == 'batch-1'


def test_checkpoint_only_stores_whitelisted_fields_and_cas(monkeypatch):
    monkeypatch.setenv('WEEKLY_BOX_STATE_KEY', 'synthetic-encryption-key')
    account = assign_accounts(accounts(1), '2026-10-06')[0]
    store = MemoryStore()
    first, second = AccountCheckpoint(store, account), AccountCheckpoint(store, account)
    rows = empty_box_lottery()
    rows[0].update(terminal=True, draw_status='结果未知', password='must-not-save')
    rows[0]['prizes'] = [{'name': '奖品', 'winCode': 'claim-id', 'token': 'must-not-save'}]
    first.save(rows)
    with pytest.raises(StateConflict):
        second.save(rows)
    saved = json.dumps(store.read(first.path)[0])
    assert 'password' not in saved and 'token' not in saved and 'must-not-save' not in saved
    assert 'claim-id' not in saved and '奖品' not in saved
    assert AccountCheckpoint(store, account).rows[0]['draw_status'] == '结果未知'
    assert checkpoint_payload(account, rows)['box_lottery'][0]['prizes'][0]['winCode'] == 'claim-id'


@pytest.fixture
def lottery_client(monkeypatch):
    import importlib
    from unittest.mock import patch
    required = {key: 'test-placeholder' for key in (
        'SLIDER_ID', 'WRAPPER_ID', 'HEADER_CLIENT_TYPE', 'HEADER_ACCESS_TOKEN', 'TOKEN_KEY')}
    required.update(BASE_URL='https://example.invalid',
                    PASSPORT_URL='https://example.invalid', REFERER='https://example.invalid')
    with patch.dict('os.environ', required):
        script = importlib.import_module('h3.script')
    context = {'group_code': 'old1', 'source_group': 'old1', 'week_id': '2026-10-05',
               'assigned_date': '2026-10-06', 'weekly_box_lottery': True}
    monkeypatch.setattr(script, 'execution_context', lambda: context)
    monkeypatch.setenv('WEEKLY_BOX_LOTTERY_ASSIGNED_DATE', '2026-10-06')
    monkeypatch.setenv('WEEKLY_BOX_STATE_KEY', 'synthetic-encryption-key')
    monkeypatch.setattr(script.time, 'sleep', Mock())
    monkeypatch.setattr(script, 'log', Mock())
    store = MemoryStore()
    monkeypatch.setattr('h3.weekly_box_schedule.WeeklyStore', lambda: store)
    client = object.__new__(script.ApiClient)
    client.account_index = 1
    client.base_url = ''
    client.box_lottery = empty_box_lottery()
    return client, store


def draw_response():
    return {'success': True, 'data': {'prizeList': [
        {'prizeTitle': '5金豆', 'winCode': 'claim-id'}]}}


def test_success_reserved_before_request_and_never_redrawn(lottery_client):
    client, store = lottery_client
    def request(method, url, **kwargs):
        if url.endswith('/turn'):
            saved = AccountCheckpoint(store, {
                'week_id': '2026-10-05', 'source_group': 'old1',
                'account_index': 1, 'assigned_date': '2026-10-06',
            }).rows
            assert any(row['draw_status'] == '结果未知' and row['terminal'] for row in saved)
            return draw_response()
        return {'success': True}
    client._browser_fetch_json_once = Mock(side_effect=request)
    assert client.execute_box_lottery('2026-10-06')
    assert sum(call.args[1].endswith('/turn') for call in client._browser_fetch_json_once.call_args_list) == 2
    client._browser_fetch_json_once.reset_mock()
    client.box_lottery = empty_box_lottery()
    assert client.execute_box_lottery('2026-10-06')
    client._browser_fetch_json_once.assert_not_called()


def test_partial_claim_sunday_only_claims_never_draws(lottery_client, monkeypatch):
    client, store = lottery_client
    client._browser_fetch_json_once = Mock(side_effect=lambda method, url, **kwargs:
        draw_response() if url.endswith('/turn') else {'success': False, 'message': 'NOT FOUND'})
    assert not client.execute_box_lottery('2026-10-06')
    client.box_lottery = empty_box_lottery()
    client._browser_fetch_json_once = Mock(return_value={'success': True})
    assert client.execute_box_lottery('2026-10-11')
    assert client._browser_fetch_json_once.call_count == 2
    assert all(call.args[1].endswith('/receivePrize') for call in client._browser_fetch_json_once.call_args_list)


def test_each_claim_success_is_saved_before_next_claim(lottery_client):
    client, store = lottery_client
    claim_calls = 0
    def request(method, url, **kwargs):
        nonlocal claim_calls
        if url.endswith('/turn'):
            return {'success': True, 'data': {'prizeList': [
                {'prizeTitle': '5金豆', 'winCode': 'one'},
                {'prizeTitle': '券', 'winCode': 'two'}]}}
        claim_calls += 1
        if claim_calls == 2:
            saved = AccountCheckpoint(store, {
                'week_id': '2026-10-05', 'source_group': 'old1',
                'account_index': 1, 'assigned_date': '2026-10-06',
            }).rows
            assert saved[0]['prizes'][0]['claim_status'] == '已领取'
        return {'success': True}
    client._browser_fetch_json_once = Mock(side_effect=request)
    assert client.execute_box_lottery('2026-10-06')
    assert claim_calls == 4


def test_no_chance_is_terminal_and_summary_counts_it(lottery_client):
    client, _ = lottery_client
    client._browser_fetch_json_once = Mock(return_value={'success': False, 'message': '暂无抽奖机会'})
    assert client.execute_box_lottery('2026-10-06')
    assert client._browser_fetch_json_once.call_count == 1
    row = {'task_start_date': '2026-10-06', 'group_code': 'old1', 'assigned_date': '2026-10-06',
           'box_lottery_required': True, 'box_lottery': client.box_lottery}
    assert 'box_lottery' not in retry_components(row)
    summary = summarize_box_lottery([row])
    assert summary['box_no_chance'] == 1
    assert summary['box_unfinished'] == 0
    assert summary['box_draw_success'] == 0


def test_unknown_result_and_runner_crash_do_not_repeat(lottery_client):
    client, store = lottery_client
    client._browser_fetch_json_once = Mock(side_effect=TimeoutError())
    client.execute_box_lottery('2026-10-06')
    assert client._browser_fetch_json_once.call_count == 2
    client.box_lottery = empty_box_lottery()
    client._browser_fetch_json_once.reset_mock()
    client.execute_box_lottery('2026-10-11')
    client._browser_fetch_json_once.assert_not_called()
    summary = summarize_box_lottery([{'box_lottery_required': True, 'box_lottery': client.box_lottery}])
    assert summary['box_unfinished'] == 1


def test_state_failure_blocks_draw_not_other_components(lottery_client):
    client, store = lottery_client
    store.write = Mock(side_effect=RuntimeError('unavailable'))
    client._browser_fetch_json_once = Mock()
    assert not client.execute_box_lottery('2026-10-06')
    client._browser_fetch_json_once.assert_not_called()
    assert not client.box_lottery[0]['terminal']


def test_partial_draw_data_survives_worse_result():
    previous = empty_box_lottery()
    previous[0].update(draw_success=True, prizes=[{'winCode': 'claim-id', 'claim_status': '已领取'}])
    current = empty_box_lottery()
    current[0]['terminal'] = True
    merged = merge_box_records(current, previous)
    assert merged[0]['draw_success']
    assert merged[0]['prizes'][0]['claim_status'] == '已领取'


def test_more_complete_prize_list_retains_claims_from_either_result():
    previous = empty_box_lottery()
    previous[0].update(draw_success=True, prizes=[
        {'winCode': 'one', 'claim_status': '待领取'},
        {'winCode': 'two', 'claim_status': '待领取'}])
    current = empty_box_lottery()
    current[0].update(draw_success=True, prizes=[{'winCode': 'one', 'claim_status': '已领取'}])
    merged = merge_box_records(current, previous)
    assert len(merged[0]['prizes']) == 2
    assert merged[0]['prizes'][0]['claim_status'] == '已领取'


def test_retry_and_fallback_use_assigned_day_not_stale_flags(tmp_path):
    account = account_metadata('old1', 1)
    account.update(assigned_date='2026-10-07', week_id='2026-10-05', weekly_box_lottery=True)
    assert 'box_lottery' not in applicable_components(account, '2026-10-06')
    assert not missing_result(account, '2026-10-06')['box_lottery_required']
    assert missing_result(account, '2026-10-07')['box_lottery_required']
    assert missing_result(account, '2026-10-11')['box_lottery_required']
    assert not missing_result(account, '2026-10-12')['box_lottery_required']
    matrix = build_retry_matrix(tmp_path, [account], task_date='2026-10-07')
    assert matrix[0]['assigned_date'] == '2026-10-07'
    assert 'box_lottery' in matrix[0]['retry_components']
    stale = {'group_code': 'old1', 'task_start_date': '2026-10-06',
             'assigned_date': '2026-10-07', 'box_lottery_required': True,
             'weekly_box_lottery': True}
    assert 'box_lottery' not in retry_components(stale)


def test_test_group_is_still_manual_and_daily_controller_remains():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / '.github' / 'workflows'
    assert 'schedule:' not in (root / 'test-group.yml').read_text(encoding='utf-8')
    controller = (root / 'dynamic-controller.yml').read_text(encoding='utf-8')
    assert '22 23 * * *' in controller
    assert 'weekly-cutover' not in controller
    assert not (root / 'weekly-box-lottery.yml').exists()
