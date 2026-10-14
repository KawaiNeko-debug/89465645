"""Seventh-day reward selection with an atomic, credential-free monthly quota."""
from __future__ import annotations

import random
import re
import time
import uuid
from datetime import date, datetime, timedelta, timezone

try:
    from weekly_box_schedule import WeeklyStore, StateConflict
except ImportError:
    from h3.weekly_box_schedule import WeeklyStore, StateConflict

TARGET = 30
RULE_PATH = '/api/activity/sign/getSignInConfig'
LIST_PATH = '/api/activity/sign/selectCouponList'
COUPON_PATH = '/api/activity/sign/receiveCoupon'
BEANS_PATH = '/api/activity/sign/receiveVoucher'
WUDI_RE = re.compile(r'wudi(?:[1-9]|1[0-9]|20)$')
SHELL_COUPON = '壳体1折券-六面激光工艺【星火展会】'
TERMINAL_MARKERS = ('已领取', '已经领取', '重复领取', '周期内', '限制周期', '已上限', '不可选', '不能兑换')


def applies(group):
    return bool(WUDI_RE.fullmatch(str(group or '').lower()))


def enabled(value):
    return value is True or str(value).strip().lower() in ('1', 'true', 'yes')


def successful(response):
    return isinstance(response, dict) and (
        response.get('success') is True
        or (response.get('success') is not False and response.get('code') == 200)
    )


def is_target_coupon(item):
    if not isinstance(item, dict):
        return False
    name = str(item.get('goodsTitle') or item.get('name') or item.get('couponName') or '')
    canonical = re.sub(r'\s+', '', name)
    # A bean option mentioning a coupon in its description is not a coupon.
    kind = str(item.get('rewardType') or '').lower()
    return ('加急' in canonical and '减免' in canonical and '券' in canonical
            and kind not in ('bean', 'beans', '金豆'))


def option_is_current(item, claim_date):
    # Only explicit validity fields are used; createDate is not validity.
    for names, comparison in (
        (('valid_from', 'effectiveTime', 'startDate', 'validStartTime'), 'start'),
        (('expires_at', 'endDate', 'expireTime', 'validEndTime'), 'end'),
    ):
        value = next((str(item[key])[:10] for key in names if item.get(key)), '')
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            if comparison == 'start' and value > claim_date:
                return False
            if comparison == 'end' and value < claim_date:
                return False
    return True


def empty_reward():
    return {'required': False, 'complete': True, 'status': '不适用',
            'choice': '', 'month': '', 'coupon_count': 0, 'fallback': False,
            'period_limited': False, 'interface_failed': False, 'claim_date': ''}


def safe_reward(value):
    """Only this projection may leave the authenticated runner."""
    output = empty_reward()
    if isinstance(value, dict):
        for key in output:
            if key in value:
                output[key] = value[key]
    for key in ('required', 'complete', 'fallback', 'period_limited', 'interface_failed'):
        output[key] = enabled(output[key])
    try:
        output['coupon_count'] = min(1, max(0, int(output.get('coupon_count') or 0)))
    except (TypeError, ValueError):
        output['coupon_count'] = 0
    if output['choice'] not in ('', 'coupon', 'beans'):
        output['choice'] = ''
    for key, pattern in (('month', r'\d{4}-\d{2}'), ('claim_date', r'\d{4}-\d{2}-\d{2}')):
        if not isinstance(output[key], str) or not re.fullmatch(pattern, output[key]):
            output[key] = ''
    # Status strings are generated locally, never server response text.
    allowed = {'不适用', '待领取', '领取成功', '金豆已领取', '奖励已领取',
               '周期限制', '奖励不可选', '接口失败', '状态保存失败',
               '领取结果未知', '配额预留中', '签到配置获取失败'}
    if output['status'] not in allowed:
        output['status'] = '接口失败'
    return output


def merge_reward(current, previous):
    a, b = safe_reward(current), safe_reward(previous)
    if b['required'] and (not a['required'] or (b['complete'] and not a['complete'])
                          or b['coupon_count'] > a['coupon_count']):
        return b
    return a


def record_reward(row):
    row = row if isinstance(row, dict) else {}
    group = row.get('group_code') or row.get('source_group')
    if ((group and not applies(group)) or enabled(row.get('sign_skipped'))
            or enabled(row.get('banned_account'))):
        return empty_reward()
    return safe_reward(row.get('seventh_reward'))


class MonthlyQuota:
    """CAS serializes the last quota slot across all twenty parallel runners.

    A sent/unknown reservation never expires automatically: a timeout may
    have issued a coupon. Only a confirmed acquisition increments the count.
    """

    def __init__(self, month, store=None):
        date.fromisoformat(month + '-01')
        self.month = month
        self.store = store or WeeklyStore()
        self.path = f'monthly-rewards/{month}/wudi.json'

    def read(self):
        value, sha = self.store.read(self.path)
        if value is None:
            value = {'month': self.month, 'entries': {}}
        if (set(value) != {'month', 'entries'} or value['month'] != self.month
                or not isinstance(value['entries'], dict)):
            raise ValueError('invalid monthly reward state')
        for key, row in value['entries'].items():
            if (not isinstance(row, dict) or set(row) != {
                    'source_group', 'account_index', 'month', 'claim_date',
                    'state', 'quantity', 'reservation'}
                    or not applies(row['source_group'])
                    or type(row['account_index']) is not int or row['account_index'] < 1
                    or row['month'] != self.month
                    or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', row['claim_date'])
                    or row['claim_date'][:7] != self.month
                    or key != self.key(row['source_group'], row['account_index'], row['claim_date'])
                    or row['state'] not in {'reserved', 'sent', 'completed', 'released'}
                    or type(row['quantity']) is not int or row['quantity'] < 0
                    or not re.fullmatch(r'[0-9a-f]{32}', row['reservation'])):
                raise ValueError('invalid monthly reward entry')
        return value, sha

    @staticmethod
    def key(group, index, claim_date):
        return f'{group}:{int(index)}:{claim_date}'

    @staticmethod
    def total(value):
        return sum(row['quantity'] for row in value['entries'].values()
                   if row['state'] == 'completed')

    def mutate(self, operation):
        self.store.ensure_branch()
        for _ in range(12):
            value, sha = self.read()
            result, changed = operation(value)
            if not changed:
                return result
            try:
                self.store.write(self.path, value, sha)
                return result
            except StateConflict:
                time.sleep(random.uniform(.15, .55))
        raise StateConflict('monthly reward state busy; no claim submitted')

    def reserve(self, group, index, claim_date):
        if not applies(group) or int(index) < 1 or claim_date[:7] != self.month:
            raise ValueError('invalid monthly reward identity')
        key, token = self.key(group, index, claim_date), uuid.uuid4().hex

        def operation(value):
            own = value['entries'].get(key)
            if own and own['state'] == 'completed':
                return ('completed', own['reservation']), False
            for row in value['entries'].values():
                if (row['source_group'] == group and row['account_index'] == int(index)
                        and row['state'] == 'sent'):
                    return ('unknown', row['reservation']), False
            # Before 'sent' is durable no request can have been submitted.
            # Transfer this reservation via CAS; the old owner then cannot
            # transition it to sent or call the claim endpoint.
            for row in value['entries'].values():
                if (row['source_group'] == group and row['account_index'] == int(index)
                        and row['state'] == 'reserved'):
                    row['state'] = 'released'
            total = self.total(value)
            if total >= TARGET:
                return ('full', ''), False
            held = sum(row['state'] in ('reserved', 'sent') for row in value['entries'].values())
            if total + held >= TARGET:
                return ('busy', ''), False
            value['entries'][key] = {
                'source_group': group, 'account_index': int(index), 'month': self.month,
                'claim_date': claim_date, 'state': 'reserved', 'quantity': 0,
                'reservation': token,
            }
            return ('reserved', token), True
        return self.mutate(operation)

    def update(self, group, index, claim_date, token, state):
        def operation(value):
            row = value['entries'].get(self.key(group, index, claim_date))
            if not row or row['reservation'] != token:
                raise StateConflict('monthly reservation ownership changed')
            if row['state'] == 'completed':
                return True, False
            row['state'] = state
            row['quantity'] = int(state == 'completed')
            return True, True
        return self.mutate(operation)


def choose_reward(group, index, task_date, current, request, quota=None, previous=None):
    """request(path, params) returns decoded JSON, never retries claim writes."""
    result = empty_reward()
    if not applies(group):
        return result
    previous = safe_reward(previous)
    if previous['required'] and previous['complete']:
        return previous
    # Month follows actual Shanghai acquisition date, not a stale task date.
    claim_date = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    result.update(month=claim_date[:7], claim_date=claim_date)
    if previous['coupon_count']:
        # Keep proof of a confirmed claim even if the subsequent configuration
        # read or checkpoint fails. Never replace it with an empty failure.
        result.update(previous)
    if not isinstance(current, dict):
        result.update(required=True, complete=False, status='签到配置获取失败', interface_failed=True)
        return result
    if current.get('signModeCode') != 'sign002' and not previous['coupon_count']:
        return result
    # haveReceive=False means an outstanding choice, including a prior cycle.
    if enabled(current.get('haveReceive')) and not previous['coupon_count']:
        if previous['required'] or str(current.get('day')) == '7':
            result.update(required=True, status='奖励已领取', fallback=True)
        return result
    result.update(required=True, complete=False, status='待领取')
    try:
        quota = quota or MonthlyQuota(previous['month'] if previous['coupon_count'] else result['month'])
        # A confirmed response whose checkpoint failed can be saved without
        # sending a second receiveCoupon request.
        if previous['coupon_count'] and previous['claim_date']:
            value, _ = quota.read()
            key = quota.key(group, index, previous['claim_date'])
            entry = value['entries'].get(key)
            if entry:
                quota.update(group, index, previous['claim_date'], entry['reservation'], 'completed')
                previous.update(complete=True, status='领取成功', interface_failed=False)
                return previous
            raise ValueError('confirmed reward checkpoint missing')
        rules = request(RULE_PATH, {'platformType': 'WEB', 'configCode': 'sign002',
                                    'version': current.get('version', '')})
        if not successful(rules):
            result.update(status='接口失败', interface_failed=True)
            return result
        rule_data = rules.get('data') or {}
        choice_available = any(str(rule.get('scene')) == '3'
                               for rule in rule_data.get('signInRuleConfigs', []) if isinstance(rule, dict))
        options = request(LIST_PATH, {}) if choice_available else {'success': True, 'data': []}
        if not successful(options) or not isinstance(options.get('data'), list):
            result.update(status='接口失败', interface_failed=True)
            return result
        candidates = [item for item in options['data'] if is_target_coupon(item)]
        available = [item for item in candidates if item.get('isCanExchange') is not False
                     and str(item.get('isCanExchange')).lower() not in ('false', '0')
                     and option_is_current(item, claim_date)
                     and (item.get('id') is not None or item.get('couponId') is not None)]
        if available:
            reservation, token = quota.reserve(group, index, claim_date)
            if reservation == 'completed':
                result.update(complete=True, choice='coupon', status='领取成功', coupon_count=1)
                return result
            if reservation in ('unknown', 'busy'):
                result.update(status='领取结果未知' if reservation == 'unknown' else '配额预留中',
                              complete=reservation == 'unknown')
                return result
            if reservation == 'reserved':
                result['choice'] = 'coupon'
                coupon = sorted(available, key=lambda item: str(item.get('id') or item.get('couponId')))[0]
                # Persist BEFORE submitting; a killed runner must not redraw.
                quota.update(group, index, claim_date, token, 'sent')
                response = request(COUPON_PATH, {
                    'couponId': coupon.get('id') if coupon.get('id') is not None else coupon.get('couponId'),
                    'terminal': coupon.get('terminal') or 'WEB',
                })
                if response is None:
                    result.update(complete=True, status='领取结果未知', interface_failed=True)
                    return result
                if successful(response):
                    result['coupon_count'] = 1
                    quota.update(group, index, claim_date, token, 'completed')
                    result.update(complete=True, status='领取成功')
                    return result
                text = str(response.get('message') or '')
                quota.update(group, index, claim_date, token, 'released')
                if any(marker in text for marker in TERMINAL_MARKERS):
                    result['period_limited'] = True
                    # Already claimed ends the exclusive reward choice. A
                    # coupon-specific limit can still leave beans available.
                    if any(marker in text for marker in ('已领取', '已经领取', '重复领取')):
                        result.update(complete=True, status='周期限制')
                        return result
                    result['fallback'] = True
                else:
                    result.update(status='接口失败', interface_failed=True)
                    return result
        else:
            result['fallback'] = True
            result['period_limited'] = bool(candidates)
        result['choice'] = 'beans'
        response = request(BEANS_PATH, {'platformType': 'WEB'})
        if response is None:
            result.update(complete=True, status='领取结果未知', interface_failed=True)
        elif successful(response):
            result.update(complete=True, status='金豆已领取')
        elif any(marker in str(response.get('message') or '') for marker in TERMINAL_MARKERS):
            result.update(complete=True, status='周期限制', period_limited=True, fallback=True)
        else:
            result.update(status='接口失败', interface_failed=True)
    except Exception:
        # Do not serialize API exceptions, URLs, bodies or authentication data.
        result.update(status='状态保存失败', interface_failed=True)
    return result


def monthly_summary(month, store=None):
    value, _ = MonthlyQuota(month, store).read()
    acquired = MonthlyQuota.total(value)
    return {'month': month, 'acquired': acquired, 'remaining': max(0, TARGET - acquired),
            'unknown': sum(row['state'] == 'sent' for row in value['entries'].values())}
