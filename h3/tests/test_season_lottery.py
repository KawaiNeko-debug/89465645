from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from h3 import season_lottery as season
from h3.mixed_results import applicable_components, missing_result
from h3.retry_components import retry_components
from h3.weekly_box_schedule import StateConflict


class MemoryStore:
    def __init__(self):
        self.files = {}
        self.version = 0

    def ensure_branch(self):
        pass

    def read(self, path):
        return deepcopy(self.files.get(path, (None, None)))

    def write(self, path, payload, sha=None):
        if self.files.get(path, (None, None))[1] != sha:
            raise StateConflict('changed')
        self.version += 1
        self.files[path] = deepcopy(payload), str(self.version)
        return str(self.version)


@pytest.fixture(autouse=True)
def runtime(monkeypatch):
    monkeypatch.setenv('WEEKLY_BOX_STATE_KEY', 'synthetic-encryption-fixture')
    monkeypatch.setattr(season, 'now_shanghai', lambda: datetime(2026, 11, 12, 12, tzinfo=season.SHANGHAI))


def configuration(code='fixture-activity', end=season.CONFIRMED_END):
    return {'code': 200, 'data': {'commonActivityId': code, 'activityType': 10, 'endTime': end,
                                'activityName': '20周年庆有料季活动', 'activityStatus': 2}}


def harness(count=2, manual=False):
    store = MemoryStore()
    state = {'count': count, 'wins': [], 'turns': 0, 'claims': 0, 'claim_ok': not manual}

    def request(method, path, payload):
        if path == season.CONFIG_PATH:
            return configuration()
        if path == season.COUNT_PATH:
            return {'success': True, 'data': {'count': state['count']}}
        if path == season.HISTORY_PATH:
            return {'code': 200, 'data': {'data': deepcopy(state['wins']), 'totalPages': 1}}
        if path == season.TURN_PATH:
            progress = season.Progress(store, 'old1', 1)
            assert progress.value['draws'][-1]['draw_status'] == '结果未知'
            state['turns'] += 1
            state['count'] -= 1
            prize = {'winCode': f'fixture-win-{state["turns"]}', 'prizeTitle': '100金豆', 'prizeType': 1,
                     'createTime': '2026-11-12T12:00:00+08:00'}
            state['wins'].append(prize)
            return {'code': 200, 'data': {'receiveMethod': 2 if manual else 1, 'prizeList': [prize]}}
        if path == season.CLAIM_PATH:
            state['claims'] += 1
            return {'success': state['claim_ok']}
        raise AssertionError(path)

    fetch = Mock(side_effect=request)
    return store, state, fetch


def run(fetch, store, group='old1', day='2026-11-12'):
    return season.SeasonLottery(fetch, store, group, 1, day, sleep=Mock()).run()


@pytest.mark.parametrize('day,expected', [('2026-11-11', False), ('2026-11-12', True),
    ('2026-11-13', False), ('2026-12-31', True), ('2027-01-07', False), ('2026-10-15', False)])
def test_thursday_dates(day, expected):
    assert season.scheduled(day, 'old1') is expected


@pytest.mark.parametrize('group', ['old1', 'wudi20', 'new1', 'ld2', 'yyy1', 'll1', 'zh20', 'test'])
def test_scheduled_groups(group):
    assert season.scheduled('2026-11-12', group)
    assert not season.scheduled('2026-11-12', 'gift_test')


def test_all_draws_reserved_encrypted_and_not_repeated():
    store, state, fetch = harness()
    result = run(fetch, store)
    assert result['complete'] and result['remaining_count'] == 0
    assert state['turns'] == 2 and state['claims'] == 0
    assert len(result['draws']) == 2
    assert 'fixture-win' not in str(result)
    assert 'fixture-win' not in str(store.files)
    assert not any('baseline' in d for d in result['draws'])
    again = run(fetch, store)
    assert again['complete'] and state['turns'] == 2
    assert len(again['draws']) == 2
    next_week = run(fetch, store, day='2026-11-19')
    assert next_week['draws'] == [] and state['turns'] == 2
    assert len(next_week['history_draws']) == 2


@pytest.mark.parametrize('group', ['ll1', 'zh1'])
def test_peer_never_draws_or_claims(group):
    store, state, fetch = harness(7)
    result = run(fetch, store, group)
    assert result['complete'] and result['remaining_count'] == 7
    assert result['mode'] == 'query_only'
    assert not state['turns'] and not state['claims']
    assert not any(c.args[1] in (season.TURN_PATH, season.CLAIM_PATH) for c in fetch.call_args_list)


@pytest.mark.parametrize('day', ['2026-11-11', '2026-11-13'])
def test_other_day_no_network_or_state(day):
    fetch, store = Mock(), Mock()
    result = run(fetch, store, day=day)
    assert result['complete'] and not result['required']
    fetch.assert_not_called()
    store.read.assert_not_called()


def test_claim_retry_does_not_repeat_draw():
    store, state, fetch = harness(1, manual=True)
    first = run(fetch, store)
    assert not first['complete'] and state['turns'] == 1
    state['claim_ok'] = True
    second = run(fetch, store)
    assert second['complete'] and state['turns'] == 1 and state['claims'] == 2


def test_unknown_draw_reconciles_or_stops():
    store, state, fetch = harness(1)
    original = fetch.side_effect
    def lost(method, path, payload):
        if path == season.TURN_PATH:
            original(method, path, payload)
            return None
        return original(method, path, payload)
    fetch.side_effect = lost
    result = run(fetch, store)
    assert result['draws'][0]['draw_success'] and state['turns'] == 1
    # A confirmed, uniquely identifiable win and decremented count permit reconciliation.
    state['wins'][0]['receiveStatus'] = 3
    result = run(fetch, store)
    assert result['complete'] and result['draws'][0]['draw_success']
    assert state['turns'] == 1


def test_unresolved_unknown_never_resubmits():
    store, state, fetch = harness(1)
    original = fetch.side_effect
    fetch.side_effect = lambda method, path, payload: None if path == season.TURN_PATH else original(method, path, payload)
    first = run(fetch, store)
    assert '未知' in first['status']
    before = len([c for c in fetch.call_args_list if c.args[1] == season.TURN_PATH])
    again = run(fetch, store)
    assert '未知' in again['status']
    assert len([c for c in fetch.call_args_list if c.args[1] == season.TURN_PATH]) == before


def test_no_count_is_not_query_failure_or_retirement():
    store, _, fetch = harness(0)
    result = run(fetch, store)
    assert result['complete'] and result['status'] == '无抽奖次数'
    assert not season.Lifecycle(store).state['retired']


def test_query_failure_not_zero_or_retirement():
    store, _, fetch = harness(0)
    original = fetch.side_effect
    fetch.side_effect = lambda method, path, payload: None if path == season.COUNT_PATH else original(method, path, payload)
    result = run(fetch, store)
    assert not result['complete'] and not result['query_success']
    assert result['remaining_count'] is None
    assert not season.Lifecycle(store).state['retired']


def test_expiry_persistent_and_old_task_cannot_reactivate(monkeypatch):
    store, _, fetch = harness()
    monkeypatch.setattr(season, 'now_shanghai', lambda: datetime(2027, 1, 1, tzinfo=season.SHANGHAI))
    result = run(fetch, store)
    assert result['status'] == '活动已永久停用'
    fetch.assert_not_called()
    monkeypatch.setattr(season, 'now_shanghai', lambda: datetime(2026, 11, 12, tzinfo=season.SHANGHAI))
    assert run(fetch, store)['status'] == '活动已永久停用'
    fetch.assert_not_called()


def test_early_end_and_changed_activity_not_adopted():
    store = MemoryStore()
    fetch = Mock(return_value=configuration(end='2026-11-11T00:00:00+08:00'))
    assert run(fetch, store)['status'] == '活动已永久停用'
    fetch.reset_mock()
    assert run(fetch, store)['status'] == '活动已永久停用'
    fetch.assert_not_called()
    other = MemoryStore()
    lifecycle = season.Lifecycle(other)
    lifecycle.configure(Mock(return_value=configuration()))
    lifecycle.state['checked_date'] = '2026-11-11'
    lifecycle.save()
    assert not run(Mock(return_value=configuration('different-event')), other)['complete']
    assert season.Lifecycle(other).state['activity_code'] == 'fixture-activity'


def test_unreadable_progress_never_draws():
    store, _, fetch = harness()
    store.write(f'season-lottery/{season.EVENT_ID}/accounts/old1-1.json', {'encrypted': 'invalid'})
    result = run(fetch, store)
    assert not result['complete']
    assert not any(c.args[1] == season.TURN_PATH for c in fetch.call_args_list)


def test_checkpoint_conflict_never_submits(monkeypatch):
    store, _, fetch = harness()
    monkeypatch.setattr(season.Progress, 'save', Mock(side_effect=StateConflict('changed')))
    assert not run(fetch, store)['complete']
    assert not any(c.args[1] == season.TURN_PATH for c in fetch.call_args_list)


def test_component_scope_and_merge():
    account = {'source_group': 'old1', 'account_index': 1, 'skip_sign': False}
    row = missing_result(account, '2026-11-12')
    assert 'season_lottery' in retry_components(row)
    row['season_lottery'].update(complete=True, status='无抽奖次数')
    assert 'season_lottery' not in retry_components(row)
    row['task_start_date'] = '2026-11-13'
    row['season_lottery']['complete'] = False
    assert 'season_lottery' not in retry_components(row)
    assert 'season_lottery' not in applicable_components(account, '2026-11-13')
    complete = {'complete': True, 'query_success': True, 'draws': [{'id': 'a', 'draw_success': True, 'prizes': []}]}
    assert season.merge_results({}, complete)['draws'] == complete['draws']


def test_retired_skips_navigation(monkeypatch):
    store = MemoryStore()
    store.write(season.LIFECYCLE_PATH, {'event_id': season.EVENT_ID, 'retired': True, 'ends_at': season.CONFIRMED_END})
    monkeypatch.setattr(season, 'WeeklyStore', lambda: store)
    client = SimpleNamespace(page=Mock(), account_index=1)
    result = season.execute(client, '2026-11-12', {'group_code': 'old1'})
    assert result['complete'] and not result['required']
    client.page.goto.assert_not_called()


def test_explicit_end_stops_all_following_calls():
    store, state, fetch = harness(1)
    original = fetch.side_effect
    fetch.side_effect = lambda method, path, payload: {'success': False, 'message': '活动已结束'} if path == season.COUNT_PATH else original(method, path, payload)
    assert run(fetch, store)['status'] == '活动已永久停用'
    fetch.reset_mock()
    assert run(fetch, store)['status'] == '活动已永久停用'
    fetch.assert_not_called()
    assert state['turns'] == 0


def test_stale_count_cannot_trigger_new_draw_on_rerun():
    store, state, fetch = harness(1)
    original = fetch.side_effect
    fetch.side_effect = lambda method, path, payload: {'code': 200, 'data': {'count': 1}} if path == season.COUNT_PATH else original(method, path, payload)
    assert '未减少' in run(fetch, store)['status']
    assert '未减少' in run(fetch, store)['status']
    assert state['turns'] == 1


def test_unknown_with_old_record_cannot_reconcile():
    store, state, fetch = harness(1)
    original = fetch.side_effect
    def lost(method, path, payload):
        result = original(method, path, payload)
        if path == season.TURN_PATH:
            state['wins'][0]['createTime'] = '2026-10-01T12:00:00+08:00'
            return None
        return result
    fetch.side_effect = lost
    run(fetch, store)
    assert '未知' in run(fetch, store)['status']
    assert state['turns'] == 1


def test_no_chance_business_terminal_not_retried():
    store, state, fetch = harness(1)
    original = fetch.side_effect
    fetch.side_effect = lambda method, path, payload: {'success': False, 'message': '暂无抽奖机会'} if path == season.TURN_PATH else original(method, path, payload)
    assert run(fetch, store)['complete']
    assert run(fetch, store)['complete']
    assert sum(c.args[1] == season.TURN_PATH for c in fetch.call_args_list) == 1


def test_shanghai_date_boundary():
    assert season.scheduled('2026-11-11T17:00:00+00:00', 'old1')
    assert not season.scheduled('2026-11-12T17:00:00+00:00', 'old1')


def test_prize_types_and_claim_identifier_sanitization():
    parsed = season.prize_rows({'data': {'receiveMethod': 1, 'prizeList': [
        {'prizeType': -1, 'prizeTitle': '谢谢参与'}, {'prizeType': 0},
        {'prizeType': 4, 'prizeTitle': '实物', 'winCode': 'fixture-code'},
        {'prizeType': 1, 'prizeTitle': '金豆'},
    ]}})
    assert len(parsed) == 2 and parsed[0]['claim_status'] == '待人工处理'
    assert parsed[1]['claim_status'] == '已领取'


def test_lifecycle_network_error_is_not_terminal():
    store = MemoryStore()
    result = run(Mock(return_value=None), store)
    assert not result['complete']
    assert not season.Lifecycle(store).state['retired']


def test_same_day_config_is_cached_without_weakening_retirement():
    store = MemoryStore()
    fetch = Mock(return_value=configuration())
    assert season.Lifecycle(store).configure(fetch)
    assert season.Lifecycle(store).configure(fetch)
    assert fetch.call_count == 1


def test_retired_cas_wins_over_late_configuration():
    store = MemoryStore()
    first, second = season.Lifecycle(store), season.Lifecycle(store)
    first.state.update(retired=True)
    first.save()
    second.configure(Mock(return_value=configuration()))
    assert second.state['retired'] and season.Lifecycle(store).state['retired']


def test_unrelated_first_activity_is_not_adopted():
    response = configuration()
    response['data']['activityName'] = '另一个活动'
    store = MemoryStore()
    result = run(Mock(return_value=response), store)
    assert not result['complete']
    assert not season.Lifecycle(store).state.get('activity_code')


def test_exact_expiration_is_permanent(monkeypatch):
    store, _, fetch = harness()
    monkeypatch.setattr(season, 'now_shanghai', lambda: season.parse_time(season.CONFIRMED_END))
    assert run(fetch, store)['status'] == '活动已永久停用'
    assert season.Lifecycle(store).state['retired']
    fetch.assert_not_called()


def test_history_merge_preserves_rewards_across_retry():
    complete = {'complete': True, 'query_success': True, 'draws': [], 'history_draws': []}
    older = {'history_draws': [{'id': 'fixture-old', 'draw_success': True,
                              'prizes': [{'name': '5金豆', 'claim_status': '已领取'}]}]}
    result = season.merge_results(complete, older)
    assert len(result['history_draws']) == 1 and not result['draws']


def test_quiet_transport_preserves_business_end_without_leaking_errors(monkeypatch):
    from h3.tests.test_seventh_reward import script_module
    script = script_module(monkeypatch)
    client = script.ApiClient('synthetic-access', '', 1, Mock())
    monkeypatch.setattr(script, 'log', Mock())
    client.page.evaluate.return_value = {'status': 400, 'data': {'success': False, 'message': '活动已结束'}}
    response = client._browser_fetch_json_once('POST', 'https://example.invalid/turn',
                                               return_business_errors=True, log_http_error=False)
    assert response['message'] == '活动已结束'
    script.log.assert_not_called()
    client.page.evaluate.side_effect = RuntimeError('synthetic-sensitive-exception')
    assert client._browser_fetch_json_once('POST', 'https://example.invalid/turn', log_http_error=False) is None
    script.log.assert_not_called()


@pytest.mark.parametrize('endpoint', [season.HISTORY_PATH, season.CLAIM_PATH])
def test_explicit_end_from_history_or_claim_permanently_stops(endpoint):
    store, state, fetch = harness(1, manual=True)
    original = fetch.side_effect
    fetch.side_effect = lambda method, path, payload: ({'success': False, 'message': '活动已结束'}
        if path == endpoint else original(method, path, payload))
    assert run(fetch, store)['status'] == '活动已永久停用'
    assert season.Lifecycle(store).state['retired']
    fetch.reset_mock()
    assert run(fetch, store)['status'] == '活动已永久停用'
    fetch.assert_not_called()


def test_other_worker_retirement_blocks_reserved_draw():
    store, state, fetch = harness(1)
    original = fetch.side_effect
    def request(method, path, payload):
        response = original(method, path, payload)
        if path == season.HISTORY_PATH:
            lifecycle = season.Lifecycle(store)
            lifecycle.state['retired'] = True
            lifecycle.save()
        return response
    fetch.side_effect = request
    assert run(fetch, store)['status'] == '活动已永久停用'
    assert state['turns'] == 0
