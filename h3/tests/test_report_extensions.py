import json
from unittest.mock import Mock

from openpyxl import load_workbook

from h3.report import normalize_record, write_xlsx, build_message
from h3.report_extensions import compact_message, season_message
from h3.weekly_box_report import (acknowledge, build_report, classify, closed_weeks,
                                  frozen_snapshot, prepare_reports)
from h3.weekly_box_schedule import AccountCheckpoint, assign_accounts
from h3.tests.test_season_lottery import MemoryStore


def record(group, index, points=100, risk=False):
    row = normalize_record({
        'group_code': group, 'account_index': index, 'task_start_date': '2026-11-12',
        'final_points': points, 'sign_success': not risk, 'risk_controlled': risk,
        'points_fetch_success': True, 'activity_fetch_success': True,
        'data_fetch_completed': True, 'account_data_required': False,
        'season_lottery': {'complete': True, 'query_success': True, 'status': '无抽奖次数',
                           'initial_count': 0, 'remaining_count': 0, 'draws': []},
    }, {}, {})
    row['username'] = f'fixture-{group}-{index}'
    row['password'] = 'synthetic-visible'
    row['account_data'] = {'invoice_profile_status': '有', 'coupons': {'unused': [
        {'name': '普通券', 'expires_at': '2099-01-01'},
        {'name': 'PCB+SMT券', 'coupon_extend_state': 'PCB+SMT', 'expires_at': '2099-01-01'},
    ]}}
    return row


def test_master_sources_all_sheets_and_order(tmp_path, monkeypatch):
    monkeypatch.setenv('REPORT_MASTER', 'true')
    records = [record('zh1', 1), record('wudi1', 1, 500), record('old1', 1, 200), record('old1', 2, 1, True)]
    path = tmp_path / 'master.xlsx'
    write_xlsx(str(path), records)
    workbook = load_workbook(path)
    main = workbook['签到汇总']
    headers = {cell.value: cell.column for cell in main[1]}
    assert [main.cell(i, headers['原始组']).value for i in range(2, 6)] == ['old1', 'old1', 'wudi1', 'zh1']
    assert [main.cell(i, headers['组内序号']).value for i in range(2, 4)] == [2, 1]
    assert main.cell(2, headers['原始组']).fill.fgColor.rgb != main.cell(4, headers['原始组']).fill.fgColor.rgb
    assert '有料季查询状态' in headers
    for title in ('普通券', 'PCB+SMT券', '有料季抽奖'):
        sheet = workbook[title]
        heads = {c.value: c.column for c in sheet[1]}
        assert all(k in heads for k in ('来源分类', '原始组', '组内序号'))
        assert {sheet.cell(i, heads['原始组']).value for i in range(2, 6)} == {'old1', 'wudi1', 'zh1'}


def test_duplicate_account_sources_not_inferred_from_username(tmp_path, monkeypatch):
    monkeypatch.setenv('REPORT_MASTER', 'true')
    a, b = record('old1', 1), record('ld1', 1)
    a['username'] = b['username'] = 'fixture-same-customer'
    path = tmp_path / 'sources.xlsx'
    write_xlsx(str(path), [a, b])
    workbook = load_workbook(path)
    for name in ('普通券', 'PCB+SMT券'):
        sheet = workbook[name]
        heads = {c.value: c.column for c in sheet[1]}
        assert {sheet.cell(i, heads['原始组']).value for i in (2, 3)} == {'old1', 'ld1'}


def test_message_preserves_full_content_or_compacts_with_utf16_limit():
    short = [('老号全干组', '总账号数: 2'), ('无敌全干组', '总账号数: 3')]
    assert '=============================' in compact_message(short)
    long = [(f'组{i}', '🧧' * 3000 + '\n总账号数: 20\n无抽奖次数: 3') for i in range(5)]
    output = compact_message(long)
    assert len(output.encode('utf-16-le')) // 2 <= 4096
    assert '见随附 XLSX' in output
    assert all(f'组{i}' in output for i in range(5))


def test_season_message_prizes_peers_and_off_days():
    peer = record('ll1', 1)
    peer['season_lottery'].update(mode='query_only', initial_count=4, remaining_count=4)
    assert '1个账号有次数' in season_message([peer])
    assert '已知剩余: 4次' in season_message([peer])
    peer['task_start_date'] = '2026-11-13'
    assert not season_message([peer])
    peer['task_start_date'] = '2026-11-12'
    peer['season_lottery']['status'] = '活动已永久停用'
    assert not season_message([peer])


def test_season_failures_visible_but_other_components_preserved():
    row = record('old1', 1)
    row['season_lottery'].update(complete=False, query_success=False, status='有料季查询失败')
    message, _ = build_message([row], {'task_start_date': '2026-11-12'}, 1)
    assert '有料季查询失败' in message
    row['task_start_date'] = '2026-11-13'
    message, _ = build_message([row], {'task_start_date': '2026-11-13'}, 1)
    assert '有料季查询失败' not in message


def test_retired_history_is_retained_without_waiting_or_reminders(tmp_path, monkeypatch):
    monkeypatch.setenv('SEASON_LOTTERY_RETIRED', 'true')
    row = record('old1', 1)
    row['season_lottery'] = {'complete': True, 'status': '活动已永久停用', 'history_draws': [
        {'id': 'prior-win', 'draw_time': '2026-11-12T12:00:00+08:00', 'prizes': [
            {'name': '5金豆', 'quantity': 1, 'claim_status': '已领取'}]}]}
    path = tmp_path / 'retired.xlsx'
    write_xlsx(str(path), [row])
    workbook = load_workbook(path)
    assert workbook['有料季历史奖品'].max_row == 2
    assert '有料季待抽账号' not in workbook.sheetnames
    assert not season_message([row])


def draw(index, name='5金豆', claimed=True):
    return {'attempt': index, 'draw_success': True, 'claim_success': claimed, 'draw_status': '抽奖成功',
            'claim_status': '领取成功' if claimed else '领取失败',
            'prizes': [{'name': name, 'quantity': 1, 'winCode': f'fixture-win-{index}',
                        'claim_status': '已领取' if claimed else '领取失败'}]}


def test_week_classification():
    assert classify([]) == '未执行'
    assert classify([], '状态不可核实') == '状态不可核实'
    assert classify([draw(1), draw(2)]) == '两次抽奖及领奖完成'
    assert classify([draw(1), draw(2, claimed=False)]) == '领取失败'
    assert classify([draw(1)]) == '部分完成'
    assert classify([{'draw_status': '暂无抽奖机会', 'terminal': True}]) == '无抽奖次数'
    assert classify([{'draw_status': '结果未知'}]) == '结果未知'


def weekly_store(monkeypatch):
    monkeypatch.setenv('WEEKLY_BOX_STATE_KEY', 'synthetic-weekly-fixture')
    store = MemoryStore()
    accounts = assign_accounts([{'source_group': 'old1', 'account_index': i} for i in range(1, 5)], '2026-11-09')
    store.write('weekly/2026-11-09/roster.json', {
        'week_id': '2026-11-09', 'cycle_dates': [f'2026-11-{i:02d}' for i in range(9, 16)], 'accounts': accounts})
    for account in accounts[:3]:
        rows = [draw(1), draw(2)] if account == accounts[0] else ([draw(1, '测试优惠券'), draw(2, '测试优惠券')]
                   if account == accounts[1] else [{'draw_status': '次数不足', 'terminal': True}])
        AccountCheckpoint(store, account).save(rows)
    return store


def test_week_snapshot_whole_roster_and_immutable(monkeypatch):
    store = weekly_store(monkeypatch)
    first = frozen_snapshot(store, '2026-11-09')
    assert len(first['rows']) == 4
    assert sum(r['report_status'] == '状态缺失' for r in first['rows']) == 1
    assert 'fixture-win' not in json.dumps(store.files)
    AccountCheckpoint(store, first['rows'][0]).save([])
    assert frozen_snapshot(store, '2026-11-09') == first


def test_week_report_counts_prizes_password_and_dedup(tmp_path, monkeypatch):
    store = weekly_store(monkeypatch)
    snapshot = frozen_snapshot(store, '2026-11-09')
    # A duplicate payload entry is not a second prize.
    snapshot['rows'][0]['box_lottery'][0]['prizes'] *= 2
    credentials = {('old1', row['account_index']): {'username': f'fixture-{row["account_index"]}', 'password': 'synthetic-hidden'}
                   for row in snapshot['rows']}
    monkeypatch.setenv('CONFIDENTIAL_PASSWORDS', 'synthetic-hidden')
    path = tmp_path / 'weekly.xlsx'
    message = build_report(snapshot, path, credentials)
    assert '计划账号: 4' in message and '无抽奖次数: 1' in message and '未完成账号: 1' in message
    workbook = load_workbook(path)
    assert workbook.sheetnames == ['周统计', '账号明细', '奖品分布', '优惠券分布', '未完成账号']
    assert all(workbook['账号明细'].cell(i, 2).value == '保密' for i in range(2, 6))
    prizes = {r[0]: r[1] for r in workbook['奖品分布'].iter_rows(min_row=2, values_only=True)}
    assert prizes == {'5金豆': 2, '测试优惠券': 2}
    assert list(workbook['优惠券分布'].values)[1][1] == 1
    assert 'synthetic-hidden' not in message and 'fixture-win' not in message


def test_closed_week_only_and_delivery_retry(tmp_path, monkeypatch):
    store = weekly_store(monkeypatch)
    response = Mock(status_code=200)
    response.json.return_value = {'tree': [{'path': 'weekly/2026-11-09/roster.json'},
                                         {'path': 'weekly/2026-11-16/roster.json'}]}
    store.request = Mock(return_value=response)
    assert closed_weeks(store, '2026-11-14') == []
    assert closed_weeks(store, '2026-11-15') == ['2026-11-09']
    reports = prepare_reports(store, '2026-11-16', tmp_path, {})
    assert len(reports) == 1  # Prior Sunday is picked up on Monday; no draws are executed.
    acknowledge(store, reports, True, False)
    again = prepare_reports(store, '2026-11-16', tmp_path, {})
    assert again[0]['receipt']['text_sent'] and not again[0]['receipt']['documents_sent']
    acknowledge(store, again, False, True)
    assert prepare_reports(store, '2026-11-16', tmp_path, {}) == []


def test_unreadable_checkpoint_is_unverified(monkeypatch):
    store = weekly_store(monkeypatch)
    roster, _ = store.read('weekly/2026-11-09/roster.json')
    account = roster['accounts'][0]
    path = f"weekly/2026-11-09/accounts/old1-{account['account_index']}.json"
    _, sha = store.read(path)
    store.write(path, {'box_lottery_encrypted': 'invalid'}, sha)
    assert frozen_snapshot(store, '2026-11-09')['rows'][0]['report_status'] == '状态不可核实'


def test_category_bundle_includes_master_weekly_and_full_message(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from pathlib import Path
    from openpyxl import Workbook
    from h3 import category_reports as reports
    (tmp_path / 'manifest.json').write_text(json.dumps({'task_start_date': '2026-11-15', 'groups': []}))
    monkeypatch.setattr(reports.sys, 'argv', ['category_reports.py', str(tmp_path)])
    monkeypatch.setenv('GITHUB_TOKEN', 'synthetic-token')
    monkeypatch.setenv('WEEKLY_BOX_STATE_KEY', 'synthetic-key')
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'synthetic-bot')
    monkeypatch.setenv('TELEGRAM_CHAT_ID', 'synthetic-chat')
    monkeypatch.delenv('SUMMARY_CATEGORY_FILTER', raising=False)
    monkeypatch.setattr(reports, 'WeeklyStore', MemoryStore)
    monkeypatch.setattr(reports, 'load_credential_lookup', lambda: ({}, {}))
    def render(args, *, env, check):
        Workbook().save(env['OUTPUT_XLSX_PATH'])
        if env.get('REPORT_MESSAGE_PATH'):
            Path(env['REPORT_MESSAGE_PATH']).write_text('总账号数: 1\n' + '完整奖品' * 1500, encoding='utf-8')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(reports.subprocess, 'run', render)
    weekly_path = tmp_path / 'weekly.xlsx'
    Workbook().save(weekly_path)
    weekly = [{'path': str(weekly_path), 'message': '计划账号: 7', 'receipt': {}}]
    monkeypatch.setattr(reports, 'prepare_reports', lambda *args: weekly)
    text, documents, ack = Mock(return_value=True), Mock(return_value=True), Mock()
    monkeypatch.setattr(reports, 'send_telegram_message', text)
    monkeypatch.setattr(reports, 'send_telegram_documents', documents)
    monkeypatch.setattr(reports, 'acknowledge', ack)
    assert reports.main() == 0
    assert len(documents.call_args.args[0]) == 7
    assert text.call_count == documents.call_count == 1
    assert len(text.call_args.args[0].encode('utf-16-le')) // 2 <= 4096
    master = load_workbook(tmp_path / '2026-11-15-超级总表.xlsx')
    assert '完整奖品' in master['通知完整明细'].cell(3, 2).value
    assert ack.call_args.args[-2:] == (True, True)
