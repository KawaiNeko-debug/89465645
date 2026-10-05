"""Frozen weekly roster and credential-free checkpoints for daily batches."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import random
import time
from copy import deepcopy
from datetime import date, timedelta

import requests

WEEKLY_START = date(2026, 10, 6)
STATE_BRANCH = "lottery-state"
GROUP_RE = re.compile(r"(?:old|wudi|ld|yyy)(?:[1-9]|1[0-9]|20)$")
NO_CHANCE_MARKERS = (
    "次数不足", "已达上限", "没有抽奖次数", "暂无抽奖机会",
    "暂无抽奖次数", "没有抽奖机会", "已抽完", "已达抽奖上限",
)


def parse_date(value) -> date:
    return date.fromisoformat(str(value)[:10])


def week_id(task_date) -> str:
    current = parse_date(task_date)
    return (current - timedelta(days=current.weekday())).isoformat()


def enabled_for(task_date) -> bool:
    return parse_date(task_date) >= WEEKLY_START


def cycle_dates(task_date) -> list[str]:
    current = parse_date(task_date)
    if not enabled_for(current):
        return []
    monday = parse_date(week_id(current))
    start = max(monday, WEEKLY_START)
    return [(start + timedelta(days=i)).isoformat()
            for i in range((monday + timedelta(days=6) - start).days + 1)]


def assign_accounts(accounts: list[dict], task_date) -> list[dict]:
    dates = cycle_dates(task_date)
    if not dates:
        return []
    week = week_id(task_date)
    eligible = [item for item in accounts if GROUP_RE.fullmatch(item['source_group'])]
    ordered = sorted(eligible, key=lambda item: hashlib.sha256(
        f"{week}:{item['source_group']}:{item['account_index']}".encode()).digest())
    base, extra = divmod(len(ordered), len(dates))
    result, offset = [], 0
    for index, assigned in enumerate(dates):
        count = base + (index < extra)
        for item in ordered[offset:offset + count]:
            result.append({
                'source_group': item['source_group'],
                'account_index': int(item['account_index']),
                'week_id': week, 'assigned_date': assigned,
                'weekly_box_lottery': True,
            })
        offset += count
    return result


def due(task_date, group_code, assigned_date) -> bool:
    try:
        current, assigned = parse_date(task_date), parse_date(assigned_date)
    except (ValueError, TypeError):
        return False
    return bool(GROUP_RE.fullmatch(str(group_code)) and enabled_for(current)
                and assigned.isoformat() in cycle_dates(current)
                and (assigned == current or (current.weekday() == 6 and assigned < current)))


def no_chance(rows) -> bool:
    return any(any(marker in str(row.get('draw_status', '')) for marker in NO_CHANCE_MARKERS)
               for row in (rows or []) if isinstance(row, dict))


class StateConflict(RuntimeError):
    pass


class WeeklyStore:
    """Only whitelisted metadata and prizes are saved, never full results."""

    def __init__(self):
        self.api = os.environ['GITHUB_API_URL'].rstrip('/')
        self.repo = os.environ['GITHUB_REPOSITORY']
        self.token = os.environ['GITHUB_TOKEN']
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {self.token}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
        })

    def request(self, method, path, **kwargs):
        # Do not include URL, token or response body in errors.
        try:
            response = self.session.request(method, f'{self.api}/repos/{self.repo}/{path}',
                                            timeout=30, **kwargs)
        except requests.RequestException:
            raise RuntimeError('weekly state transport failed') from None
        if response.status_code not in (200, 201, 404, 409, 422):
            raise RuntimeError(f'weekly state API failed ({response.status_code})')
        return response

    def ensure_branch(self):
        response = self.request('GET', f'git/ref/heads/{STATE_BRANCH}')
        if response.status_code == 200:
            return
        ref = os.getenv('GITHUB_REF_NAME') or 'main'
        head = self.request('GET', f'git/ref/heads/{ref}')
        if head.status_code != 200:
            raise RuntimeError('weekly state base ref unavailable')
        created = self.request('POST', 'git/refs', json={
            'ref': f'refs/heads/{STATE_BRANCH}', 'sha': head.json()['object']['sha'],
        })
        if created.status_code not in (201, 422):
            raise RuntimeError('weekly state branch creation failed')

    def read(self, path):
        response = self.request('GET', f'contents/{path}', params={'ref': STATE_BRANCH})
        if response.status_code == 404:
            return None, None
        if response.status_code != 200:
            raise RuntimeError('weekly state read failed')
        payload = response.json()
        return json.loads(base64.b64decode(payload['content'])), payload['sha']

    def write(self, path, payload, sha=None):
        body = {'branch': STATE_BRANCH, 'message': 'chore: checkpoint weekly lottery',
                'content': base64.b64encode(json.dumps(payload, ensure_ascii=False,
                                                     separators=(',', ':')).encode()).decode()}
        if sha:
            body['sha'] = sha
        response = self.request('PUT', f'contents/{path}', json=body)
        if response.status_code in (409, 422):
            raise StateConflict('weekly checkpoint changed; reload before continuing')
        if response.status_code not in (200, 201):
            raise RuntimeError('weekly checkpoint save failed')
        return response.json()['content']['sha']

    def freeze(self, accounts, task_date):
        self.ensure_branch()
        path = f'weekly/{week_id(task_date)}/roster.json'
        frozen, _ = self.read(path)
        if frozen is not None:
            return validate_roster(frozen, task_date)
        roster = {'week_id': week_id(task_date), 'cycle_dates': cycle_dates(task_date),
                  'accounts': assign_accounts(accounts, task_date)}
        for _ in range(5):
            try:
                self.write(path, roster)
                return roster
            except StateConflict:
                frozen, _ = self.read(path)
                if frozen is not None:
                    return validate_roster(frozen, task_date)
                time.sleep(random.uniform(0.4, 1.2))
        raise RuntimeError('weekly roster could not be frozen')


def validate_roster(roster, task_date):
    if not isinstance(roster, dict) or set(roster) != {'week_id', 'cycle_dates', 'accounts'}:
        raise ValueError('invalid frozen weekly roster')
    if roster['week_id'] != week_id(task_date) or roster['cycle_dates'] != cycle_dates(task_date):
        raise ValueError('frozen weekly roster has a different cycle')
    if not isinstance(roster['accounts'], list):
        raise ValueError('invalid weekly accounts')
    seen = set()
    for account in roster['accounts']:
        if not isinstance(account, dict) or set(account) != {
            'source_group', 'account_index', 'week_id', 'assigned_date', 'weekly_box_lottery'
        }:
            raise ValueError('invalid weekly account metadata')
        identity = (account['source_group'], account['account_index'])
        if (not isinstance(identity[0], str) or not GROUP_RE.fullmatch(identity[0])
                or type(identity[1]) is not int or identity[1] < 1
                or identity in seen or account['week_id'] != roster['week_id']
                or account['assigned_date'] not in roster['cycle_dates']
                or account['weekly_box_lottery'] is not True):
            raise ValueError('invalid weekly assignment')
        seen.add(identity)
    return roster


DRAW_KEYS = ('attempt', 'draw_success', 'terminal', 'draw_status', 'claim_success',
             'claim_status', 'claim_detail', 'draw_time')
PRIZE_KEYS = ('name', 'prize_type', 'quantity', 'winCode', 'claim_status')


def checkpoint_payload(account, rows):
    payload = {key: account[key] for key in
               ('week_id', 'source_group', 'account_index', 'assigned_date')}
    payload['box_lottery'] = []
    for row in rows[:2]:
        clean = {key: deepcopy(row[key]) for key in DRAW_KEYS if key in row}
        clean['prizes'] = [{key: deepcopy(prize[key]) for key in PRIZE_KEYS if key in prize}
                           for prize in row.get('prizes', []) if isinstance(prize, dict)]
        payload['box_lottery'].append(clean)
    return payload


class AccountCheckpoint:
    def __init__(self, store, account):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        self.store, self.account = store, account
        code, index = account['source_group'], int(account['account_index'])
        if not GROUP_RE.fullmatch(code) or index < 1:
            raise ValueError('invalid weekly account identity')
        if (parse_date(account['week_id']).weekday() != 0
                or week_id(account['assigned_date']) != account['week_id']
                or account['assigned_date'] not in cycle_dates(account['assigned_date'])):
            raise ValueError('invalid weekly account cycle')
        self.path = f"weekly/{account['week_id']}/accounts/{code}-{index}.json"
        secret = os.getenv('WEEKLY_BOX_STATE_KEY', '')
        if not secret:
            raise RuntimeError('weekly state encryption key unavailable')
        self.cipher = AESGCM(hashlib.sha256(('weekly-box-v1:' + secret).encode()).digest())
        payload, self.sha = store.read(self.path)
        self.rows = []
        if payload:
            try:
                sealed = base64.b64decode(payload['box_lottery_encrypted'])
                self.rows = json.loads(self.cipher.decrypt(
                    sealed[:12], sealed[12:], self.path.encode()))
            except Exception:
                # Never discard unreadable progress and start drawing again.
                raise RuntimeError('weekly checkpoint could not be decrypted') from None

    def save(self, rows):
        payload = checkpoint_payload(self.account, rows)
        nonce = os.urandom(12)
        raw = json.dumps(payload.pop('box_lottery'), ensure_ascii=False).encode()
        payload['box_lottery_encrypted'] = base64.b64encode(
            nonce + self.cipher.encrypt(nonce, raw, self.path.encode())).decode()
        # Account files share a branch tip. Retry only if our own file did
        # not change; a changed file means another worker won the reservation.
        for _ in range(8):
            try:
                self.sha = self.store.write(self.path, payload, self.sha)
                self.rows = deepcopy(rows[:2])
                return
            except StateConflict:
                _, current_sha = self.store.read(self.path)
                if current_sha != self.sha:
                    raise
                time.sleep(random.uniform(0.4, 1.2))
        raise StateConflict('weekly state branch busy; no draw submitted')
