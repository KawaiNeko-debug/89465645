import io
import json
import zipfile
from copy import deepcopy
from unittest.mock import Mock

import pytest
import yaml

from h3.listing_gift import (
    MonthlyGiftCheckpoint, gift_identity, gift_result_success, should_claim_listing_gift,
    gift_stock_exhausted, inspect_listing_gift_response,
)
from h3.retry_components import component_status, retry_components
from h3.mixed_batches import batch_accounts, encode_chain_state, load_chain_state
from h3.mixed_results import build_retry_matrix
from h3.monthly_gift_recovery import (
    assert_idle, exact_summary_manifest, overlay_gift_result, recovery_state,
    merge_recovery_reports,
)
from h3.weekly_box_schedule import StateConflict
from h3.report import build_message, normalize_record, status_label


@pytest.fixture(autouse=True)
def synthetic_login_config(monkeypatch):
    for key in ('BASE_URL', 'PASSPORT_URL', 'REFERER'):
        monkeypatch.setenv(key, 'https://example.test')
    for key in ('SLIDER_ID', 'WRAPPER_ID', 'HEADER_CLIENT_TYPE',
                'HEADER_ACCESS_TOKEN', 'TOKEN_KEY'):
        monkeypatch.setenv(key, 'synthetic-test-setting')


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


def gift_row(**overrides):
    row = dict(task_start_date='2026-10-22', source_group='wudi1', group_code='wudi1',
               account_index=1, listing_gift_required=True, listing_gift_success=True,
               listing_gift_status='每月礼包领取成功', **gift_identity('2026-10-22'))
    row.update(overrides)
    return row


@pytest.mark.parametrize('day,expected', [
    ('2026-09-19', False), ('2026-09-20', False), ('2026-10-01', False),
    ('2026-10-22', True), ('2026-10-23', False), ('2026-10-30', False),
    ('2026-11-01', True), ('2026-11-30', False), ('2026-12-01', True),
    ('2027-01-01', True), ('2027-02-01', True), ('2027-02-30', False),
])
def test_monthly_dates(day, expected):
    assert should_claim_listing_gift(day, 'wudi1') is expected


def test_checkpoint_is_monthly_and_credential_free():
    store = MemoryStore()
    checkpoint = MonthlyGiftCheckpoint('2026-10-22', 'wudi1', 1, store)
    assert not checkpoint.completed()
    checkpoint.save('received')
    checkpoint.save('already')
    assert checkpoint.completed() and store.version == 1
    row, _ = checkpoint.read()
    assert set(row) == {'month', 'source_group', 'account_index', 'gift_id', 'state', 'completed_at'}
    assert not MonthlyGiftCheckpoint('2026-11-01', 'wudi1', 1, store).completed()
    assert not MonthlyGiftCheckpoint('2026-10-22', 'ld1', 1, store).completed()
    with pytest.raises(ValueError):
        checkpoint.save('failed')


def test_stock_shortage_is_not_reported_as_received(claim_client):
    client, store = claim_client
    client._browser_fetch_json_once.return_value = {'success': False, 'message': '券余量不足'}
    assert not client.execute_listing_gift('2026-10-22')
    assert not client.listing_gift_success
    assert not store.files
    assert client._browser_fetch_json_once.call_count == 1
    assert not client.execute_listing_gift('2026-10-22')
    assert client._browser_fetch_json_once.call_count == 1


def exhausted_row(**overrides):
    return gift_row(listing_gift_success=False,
                    listing_gift_status='每月礼包领取失败：券余量不足', **overrides)


@pytest.mark.parametrize('response', [
    {'success': False, 'message': '券余量不足'},
    {'success': True, 'data': {'success': False, 'msg': '券余量不足，请稍后再试'}},
])
def test_stock_shortage_response_is_terminal_failure(response):
    result = inspect_listing_gift_response(response)
    assert result['state'] == 'stock_exhausted' and result['success'] is False


def test_shortage_only_stops_gift_component_and_keeps_other_retries():
    row = exhausted_row(account_data={'coupon_fetch_success': False})
    assert component_status(row)['gift'] is True
    assert not gift_result_success(row)
    assert 'gift' not in retry_components(row)
    assert 'coupons' in retry_components(row)


def test_three_runner_rounds_never_reintroduce_exhausted_gift(tmp_path):
    state = recovery_state('123', 'main', '2026-10-22', {'wudi1': 2})
    accounts = batch_accounts(state, 'batch-1')
    candidates = accounts
    for round_number in range(3):
        rows = [exhausted_row(account_index=1,
                             account_data={'coupon_fetch_success': False}),
                exhausted_row(account_index=2,
                             account_data={'coupon_fetch_success': True})]
        directory = tmp_path / str(round_number)
        directory.mkdir()
        for row in rows:
            artifact = directory / str(row['account_index'])
            artifact.mkdir()
            (artifact / 'result.json').write_text(json.dumps({'results': [row]}), encoding='utf-8')
        candidates = build_retry_matrix(tmp_path, accounts, candidates, '2026-10-22')
        assert len(candidates) == 1
        assert candidates[0]['account_index'] == 1
        assert candidates[0]['retry_components'] == 'coupons'


def test_xlsx_keeps_shortage_failure_and_red_status(tmp_path):
    from openpyxl import load_workbook
    from h3.report import write_xlsx, FONT_RED
    row = normalize_record(exhausted_row(), {'task_start_date': '2026-10-22'}, {})
    path = tmp_path / 'report.xlsx'
    write_xlsx(str(path), [row])
    workbook = load_workbook(path)
    sheet = workbook['签到汇总']
    matches = [cell for cell in sheet[2] if cell.value == '每月礼包领取失败：券余量不足']
    assert len(matches) == 1
    assert matches[0].font.color.rgb == FONT_RED.color.rgb
    workbook.close()


@pytest.mark.parametrize('overrides', [{'listing_gift_id': 72}, {'listing_gift_month': '2026-09'}])
def test_old_shortage_does_not_suppress_current_gift(overrides):
    row = exhausted_row(**overrides)
    assert not gift_stock_exhausted(row)
    assert 'gift' in retry_components(row)


def test_shortage_does_not_suppress_next_month(claim_client):
    client, _ = claim_client
    client._browser_fetch_json_once.return_value = {'success': False, 'message': '券余量不足'}
    assert not client.execute_listing_gift('2026-10-22')
    client._browser_fetch_json_once.return_value = {'success': True, 'data': ['synthetic-coupon']}
    assert client.execute_listing_gift('2026-11-01')
    assert client._browser_fetch_json_once.call_count == 2


@pytest.mark.parametrize('reverse', [False, True])
def test_shortage_survives_unrelated_retry_and_success_wins(reverse):
    from h3.merge_results import pick_result
    shortage = exhausted_row()
    failed = gift_row(listing_gift_success=False, listing_gift_status='未执行', retry_count=2)
    left, right = (failed, shortage) if reverse else (shortage, failed)
    merged = pick_result(deepcopy(left), deepcopy(right))
    assert gift_stock_exhausted(merged)
    assert 'gift' not in retry_components(merged)
    assert gift_stock_exhausted(overlay_gift_result(shortage, failed))
    assert gift_result_success(pick_result(merged, gift_row()))
    assert gift_result_success(overlay_gift_result(gift_row(), shortage))


def test_shortage_hydrates_for_coupon_only_retry(claim_client, monkeypatch):
    import h3.script as script
    client, _ = claim_client
    monkeypatch.setattr(script, 'normalize_task_start_date', lambda: '2026-10-22')
    client.hydrate_from_previous(exhausted_row())
    assert not client.execute_listing_gift('2026-10-22')
    client._browser_fetch_json_once.assert_not_called()


@pytest.mark.parametrize('gift_only', [False, True])
def test_report_counts_stock_shortage_separately(monkeypatch, gift_only):
    monkeypatch.setenv('REPORT_GIFT_ONLY', str(gift_only).lower())
    raw = [exhausted_row(), exhausted_row(account_index=2), gift_row(account_index=3)]
    rows = [normalize_record(row, {'task_start_date': '2026-10-22'}, {}) for row in raw]
    message, summary = build_message(rows, {}, 3)
    assert summary['listing_gift_stock_exhausted'] == 2
    assert summary['listing_gift_success'] == 1
    assert '券余量不足（不再重试）: 2个账号' in message
    old = normalize_record(exhausted_row(task_start_date='2026-10-23'),
                           {'task_start_date': '2026-10-23'}, {})
    assert build_message([old], {}, 1)[1]['listing_gift_stock_exhausted'] == 0


def test_legacy_campaign_or_previous_month_cannot_complete_gift():
    for row in (gift_row(listing_gift_id=72), gift_row(listing_gift_month='2026-09'),
                gift_row(listing_gift_id=None)):
        row['component_status'] = {'gift': True}
        assert not component_status(row)['gift']
        assert 'gift' in retry_components(row)
        normalized = normalize_record(row, {'task_start_date': '2026-10-22'}, {})
        assert not normalized['listing_gift_success']


@pytest.fixture
def claim_client(monkeypatch):
    import h3.script as script
    store = MemoryStore()
    client = script.ApiClient.__new__(script.ApiClient)
    client.account_index = 1
    client.base_url = 'https://www.example.test'
    client.page = Mock()
    client.listing_gift_success = False
    client.listing_gift_attempted = False
    client._browser_fetch_json_once = Mock(return_value={'success': True, 'data': ['synthetic-coupon']})
    monkeypatch.setattr(script, 'execution_context', lambda: {'group_code': 'wudi1'})
    monkeypatch.setattr(script, 'MonthlyGiftCheckpoint',
                        lambda day, group, index: MonthlyGiftCheckpoint(day, group, index, store))
    monkeypatch.setattr(script.time, 'sleep', lambda _: None)
    return client, store


def test_claim_once_and_monthly_checkpoint_skips_api(claim_client):
    client, store = claim_client
    assert client.execute_listing_gift('2026-10-22')
    args, kwargs = client._browser_fetch_json_once.call_args
    assert args[0] == 'POST' and args[1].endswith('/api/appPlatform/couponPage/receiveCoupon')
    assert kwargs['payload'] == {'id': 43} and kwargs['dump_body_on_error'] is False
    assert client.execute_listing_gift('2026-10-22')
    assert client._browser_fetch_json_once.call_count == 1
    assert len(store.files) == 1


@pytest.mark.parametrize('message', ['本月已领取', '您已领取过优惠券，在限制周期内无法再次参与，请稍后再试'])
def test_already_claimed_is_terminal(claim_client, message):
    client, _ = claim_client
    client._browser_fetch_json_once.return_value = {'success': False, 'message': message}
    assert client.execute_listing_gift('2026-10-22')
    assert client._browser_fetch_json_once.call_count == 1


def test_network_failure_stays_retryable_and_does_not_trust_page_text(claim_client):
    client, store = claim_client
    client._browser_fetch_json_once.return_value = None
    assert not client.execute_listing_gift('2026-10-22')
    assert client._browser_fetch_json_once.call_count == 3
    client.page.locator.assert_not_called()
    assert not store.files
    assert not component_status(gift_row(listing_gift_success=False))['gift']


def test_off_day_never_opens_gift_page(claim_client):
    client, _ = claim_client
    assert client.execute_listing_gift('2026-10-30')
    client.page.goto.assert_not_called()
    client._browser_fetch_json_once.assert_not_called()


def test_account_process_initializes_month_and_phone_guard_without_browser(monkeypatch, capsys):
    import h3.script as script
    monkeypatch.setenv('GROUP_CODE', 'wudi1')
    monkeypatch.setattr(script, 'normalize_task_start_date', lambda *args: '2026-10-22')
    monkeypatch.setattr(script.time, 'sleep', lambda _: None)
    browser = Mock(side_effect=AssertionError('must not open browser'))
    monkeypatch.setattr(script, 'sync_playwright', browser)
    row = script.process_single_account('13800000000', 'synthetic-password', 1, 1)
    assert row['account_format_error'] is True
    assert row['listing_gift_id'] == 43 and row['listing_gift_month'] == '2026-10'
    browser.assert_not_called()
    assert '13800000000' not in capsys.readouterr().out


@pytest.mark.parametrize('count,parts', [(1, [1]), (220, [220]), (221, [220, 1]), (441, [220, 220, 1])])
def test_recovery_batches_are_scoped_and_exclude_other_groups(count, parts):
    state = recovery_state('123', 'main', '2026-10-22',
                           {'wudi1': count, 'old1': 4, 'll1': 3, 'zh1': 2, 'gift_test': 1})
    loaded = load_chain_state(encode_chain_state(state))
    assert [batch['account_count'] for batch in loaded['batches']] == parts
    accounts = batch_accounts(loaded, 'batch-1')
    assert all(row['retry_components'] == 'gift,coupons' for row in accounts)
    assert set(loaded['group_counts']) == {'wudi1'}


def test_missing_recovery_result_never_expands_to_sign_or_lottery(tmp_path):
    state = recovery_state('123', 'main', '2026-10-22', {'wudi1': 2, 'ld1': 1, 'yyy1': 1})
    candidates = batch_accounts(state, 'batch-1')
    retry = build_retry_matrix(tmp_path, candidates, candidates, '2026-10-22')
    assert len(retry) == 4
    assert all(row['retry_components'] == 'gift,coupons' for row in retry)


def test_recovery_overlay_keeps_other_components_and_replaces_coupon_snapshot():
    base = gift_row(sign_success=True, final_points=500, points_reward=10,
                    listing_gift_success=False, vote_success=True,
                    activity_records={'lottery': [{'title': 'saved-prize'}]},
                    account_data={'invoice_profile_exists': True, 'invoice_fetch_success': True,
                                  'pcb_order_fetch_success': True, 'pcb_total_amount': 100,
                                  'coupon_fetch_success': True, 'coupons': {'unused': []}})
    patch = gift_row(account_data={'coupon_fetch_success': True,
                                   'coupons': {'unused': [{'name': 'new-coupon'}]}})
    merged = overlay_gift_result(base, patch)
    for key in ('sign_success', 'final_points', 'points_reward', 'vote_success', 'activity_records'):
        assert merged[key] == base[key]
    assert merged['account_data']['invoice_profile_exists'] is True
    assert merged['account_data']['coupons']['unused'] == [{'name': 'new-coupon'}]
    assert gift_result_success(merged)
    failed = overlay_gift_result(merged, gift_row(listing_gift_success=False))
    assert gift_result_success(failed)


def test_isolated_report_does_not_claim_sign_failures(monkeypatch):
    monkeypatch.setenv('REPORT_GIFT_ONLY', 'true')
    row = overlay_gift_result(None, gift_row(account_data={'coupon_fetch_success': True}))
    assert status_label(row) == '仅补领每月礼包'
    message, summary = build_message([row], {}, 1)
    assert '每月礼包完成: 1/1' in message
    assert '签到成功:' not in message and '签到失败' not in message


def test_idle_guard_stops_before_dispatch(monkeypatch):
    monkeypatch.setattr('h3.monthly_gift_recovery.api_request', lambda *args, **kwargs: {
        'workflow_runs': [{'id': 99, 'path': '.github/workflows/dynamic-batch.yml'}]})
    with pytest.raises(RuntimeError, match='another daily'):
        assert_idle('example/repo', 'not-a-real-token', 123)


def test_exact_baseline_checks_date_and_never_extracts_xlsx(monkeypatch):
    from h3.mixed_batches import new_chain_state
    state = new_chain_state('100', 'main', '2026-10-22', group_counts={'wudi1': 1})
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, 'w') as archive:
        archive.writestr('manifest.json', json.dumps(state))
        archive.writestr('report.xlsx', b'not-extracted')
    responses = [
        {'status': 'completed', 'path': '.github/workflows/dynamic-summary.yml',
         'display_title': 'dynamic-summary-100'},
        {'artifacts': [{'id': 456, 'name': 'category-reports-100', 'expired': False}]},
        raw.getvalue(),
    ]
    api = Mock(side_effect=responses)
    monkeypatch.setattr('h3.monthly_gift_recovery.api_request', api)
    assert exact_summary_manifest('example/repo', 'not-a-real-token', 789, '2026-10-22') == state
    api.side_effect = responses
    with pytest.raises(ValueError, match='same date'):
        exact_summary_manifest('example/repo', 'not-a-real-token', 789, '2026-11-01')


def test_recovery_overlay_is_report_input_without_duplicate_raw_rows(tmp_path, monkeypatch):
    state = recovery_state('123', 'main', '2026-10-22', {'wudi1': 1})
    batch_dir = tmp_path / 'batch-1'
    batch_dir.mkdir()
    (batch_dir / 'result.json').write_text(json.dumps({'results': [gift_row()]}), encoding='utf-8')
    merge_recovery_reports(tmp_path, state)
    manifest = json.loads((tmp_path / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['recovery_overlay_ready'] is True
    rows = json.loads((tmp_path / 'combined' / 'result.json').read_text(encoding='utf-8'))['results']
    assert len(rows) == 1 and rows[0]['execution_mode'] == 'gift_recovery'


def test_baseline_overlay_retains_missing_groups_and_chain_metadata(tmp_path, monkeypatch):
    from h3.mixed_batches import new_chain_state
    baseline = new_chain_state('100', 'main', '2026-10-22',
                               group_counts={'old1': 1, 'wudi1': 1, 'll1': 1})
    state = recovery_state('123', 'main', '2026-10-22', baseline['group_counts'], baseline)
    batch_dir = tmp_path / 'batch-1'
    batch_dir.mkdir()
    (batch_dir / 'result.json').write_text(json.dumps({'results': [gift_row()]}), encoding='utf-8')
    def download_base(args):
        directory = tmp_path / 'baseline'
        directory.mkdir(exist_ok=True)
        rows = [gift_row(source_group=group, group_code=group, sign_success=True,
                         listing_gift_success=False) for group in ('old1', 'wudi1', 'll1')]
        (directory / 'result.json').write_text(json.dumps({'results': rows}), encoding='utf-8')
    monkeypatch.setattr('h3.monthly_gift_recovery.download_batches', download_base)
    merge_recovery_reports(tmp_path, state)
    result = json.loads((tmp_path / 'combined/result.json').read_text(encoding='utf-8'))
    assert len(result['results']) == 3
    assert all(row['sign_success'] for row in result['results'])
    assert load_chain_state(encode_chain_state(state))['total_accounts'] == 1
    merge_recovery_reports(tmp_path, state)
    again = json.loads((tmp_path / 'combined/result.json').read_text(encoding='utf-8'))
    assert len(again['results']) == 3


def test_category_recovery_generates_three_isolated_files(tmp_path, monkeypatch):
    from h3.category_reports import main as category_main
    state = recovery_state('123', 'main', '2026-10-22', {'wudi1': 1, 'ld1': 1, 'yyy1': 1})
    directory = tmp_path / 'batch-1'
    directory.mkdir()
    rows = [gift_row(source_group=group, group_code=group,
                     account_data={'coupon_fetch_success': True}) for group in ('wudi1', 'ld1', 'yyy1')]
    (directory / 'result.json').write_text(json.dumps({'results': rows}), encoding='utf-8')
    merge_recovery_reports(tmp_path, state)
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', '')
    monkeypatch.setenv('TELEGRAM_CHAT_ID', '')
    monkeypatch.setenv('SUMMARY_CATEGORY_FILTER', '')
    monkeypatch.setenv('NOTIFY_CHANNELS', '')
    for group in ('wudi1', 'ld1', 'yyy1'):
        monkeypatch.setenv(group, 'synthetic-account,synthetic-password')
    monkeypatch.setattr('sys.argv', ['category_reports.py', str(tmp_path)])
    assert category_main() == 0
    assert len(list(tmp_path.glob('*.xlsx'))) == 3
    for path in tmp_path.glob('.*-message.txt'):
        content = path.read_text(encoding='utf-8')
        assert '签到失败' not in content and 'synthetic-password' not in content


def test_workflow_has_no_duplicate_yaml_keys_and_gift_test_stays_manual():
    from pathlib import Path
    class UniqueLoader(yaml.SafeLoader):
        pass
    def unique_mapping(loader, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in result:
                raise AssertionError('duplicate YAML key')
            result[key] = loader.construct_object(value_node, deep=deep)
        return result
    UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)
    root = Path(__file__).resolve().parents[2]
    controller = yaml.load((root / '.github/workflows/dynamic-controller.yml').read_text(encoding='utf-8'), Loader=UniqueLoader)
    steps = controller['jobs']['start-chain']['steps']
    normal = next(step for step in steps if step.get('name') == 'Freeze and dispatch first mixed batch')
    assert '!inputs.gift_recovery' in normal['if']
    gift_test = yaml.safe_load((root / '.github/workflows/gift-test.yml').read_text(encoding='utf-8'))
    assert 'schedule' not in gift_test.get('on', gift_test.get(True, {}))
