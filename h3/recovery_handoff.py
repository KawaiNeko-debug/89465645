"""Defer a daily entry while its isolated gift recovery is still active."""
import os
import time
from datetime import datetime, timedelta, timezone

try:
    from weekly_box_schedule import WeeklyStore, StateConflict
    from dynamic_groups import api_request
except ImportError:
    from h3.weekly_box_schedule import WeeklyStore, StateConflict
    from h3.dynamic_groups import api_request


def handoff_path(recovery_id):
    return f'monthly-gift-recovery/{int(recovery_id)}/handoff.json'


def read_handoff(store, recovery_id):
    row, sha = store.read(handoff_path(recovery_id))
    if row is None:
        row = {'recovery_id': str(int(recovery_id)), 'completed': False,
               'deferred_run_ids': []}
    if (not isinstance(row, dict)
            or set(row) != {'recovery_id', 'completed', 'deferred_run_ids'}
            or row['recovery_id'] != str(int(recovery_id))
            or type(row['completed']) is not bool
            or not isinstance(row['deferred_run_ids'], list)
            or any(not isinstance(value, str) or not value.isdecimal()
                   for value in row['deferred_run_ids'])):
        raise ValueError('invalid recovery handoff metadata')
    return row, sha


def update_handoff(store, recovery_id, *, defer_run_id=None, completed=False):
    store.ensure_branch()
    for _ in range(8):
        row, sha = read_handoff(store, recovery_id)
        if completed:
            row['completed'] = True
        elif row['completed']:
            return row
        if defer_run_id is not None:
            value = str(int(defer_run_id))
            if value not in row['deferred_run_ids']:
                row['deferred_run_ids'].append(value)
        try:
            store.write(handoff_path(recovery_id), row, sha)
            return row
        except StateConflict:
            time.sleep(.3)
    raise RuntimeError('recovery handoff busy')


def recovery_runs_today(payload, now):
    today = now.astimezone(timezone(timedelta(hours=8))).date()
    for run in payload.get('workflow_runs', []):
        if not str(run.get('display_title') or '').startswith('gift-recovery-'):
            continue
        try:
            created = datetime.fromisoformat(run['created_at'].replace('Z', '+00:00'))
        except (KeyError, ValueError):
            continue
        if created.astimezone(timezone(timedelta(hours=8))).date() == today:
            if run.get('conclusion') not in {'failure', 'cancelled', 'skipped', 'timed_out'}:
                yield run


def guard_recoveries(payload, now, current_run_id, repo, token, store=None):
    """Return (previously deferred entries, whether this entry was deferred)."""
    recoveries = list(recovery_runs_today(payload, now))
    if not recoveries:
        return set(), False
    store = store or WeeklyStore()
    deferred, active = set(), []
    summary_runs = None
    for run in recoveries:
        row, sha = read_handoff(store, run['id'])
        deferred.update(row['deferred_run_ids'])
        if row['completed']:
            continue
        # Older deployed recoveries may not have created a checkpoint yet.
        if sha is None:
            if summary_runs is None:
                summary_runs = api_request('GET', repo, token,
                    '/actions/workflows/dynamic-summary.yml/runs',
                    params={'per_page': 100}).get('workflow_runs', [])
            title = f"dynamic-summary-{run['id']}"
            if any(item.get('display_title') == title and item.get('status') == 'completed'
                   for item in summary_runs):
                continue
        active.append(run['id'])
    try:
        from schedule_guard import has_controller_run_today
    except ImportError:
        from h3.schedule_guard import has_controller_run_today
    if has_controller_run_today(payload, now, current_run_id, deferred):
        return deferred, False
    for recovery_id in active:
        row = update_handoff(store, recovery_id, defer_run_id=current_run_id)
        deferred.update(row['deferred_run_ids'])
        if not row['completed']:
            return deferred, True
    return deferred, False


def release_daily(state, store=None, now=None):
    """A short summary handoff, never a polling job or a second daily chain."""
    if not state.get('gift_recovery'):
        return False
    store = store or WeeklyStore()
    row = update_handoff(store, state['orchestration_id'], completed=True)
    if not row['deferred_run_ids']:
        return False
    now = now or datetime.now(timezone(timedelta(hours=8)))
    repo = os.environ['GITHUB_REPOSITORY']
    token = os.environ['GITHUB_TOKEN']
    payload = api_request('GET', repo, token,
                          '/actions/workflows/dynamic-controller.yml/runs',
                          params={'per_page': 100})
    today_ids = {str(run['id']) for run in payload.get('workflow_runs', [])
                 if _created_today(run, now)}
    if not today_ids.intersection(row['deferred_run_ids']):
        print('[monthly-gift] deferred daily entry expired; no past-date run dispatched')
        return False
    try:
        from schedule_guard import has_controller_run_today
    except ImportError:
        from h3.schedule_guard import has_controller_run_today
    for attempt in range(3):
        if has_controller_run_today(payload, now, excluded_run_ids=row['deferred_run_ids']):
            return False
        try:
            api_request('POST', repo, token,
                        '/actions/workflows/dynamic-controller.yml/dispatches',
                        payload={'ref': state.get('ref') or 'main', 'inputs': {
                            'task_start_date': now.astimezone(timezone(timedelta(hours=8))).date().isoformat(),
                            'gift_recovery': 'false'}})
            print('[monthly-gift] resumed deferred daily entry')
            return True
        except Exception:
            if attempt == 2:
                raise RuntimeError('deferred daily dispatch failed') from None
            time.sleep(2)
            payload = api_request('GET', repo, token,
                                  '/actions/workflows/dynamic-controller.yml/runs',
                                  params={'per_page': 100})
    return False


def _created_today(run, now):
    try:
        created = datetime.fromisoformat(run['created_at'].replace('Z', '+00:00'))
        zone = timezone(timedelta(hours=8))
        return created.astimezone(zone).date() == now.astimezone(zone).date()
    except (KeyError, ValueError):
        return False


if __name__ == '__main__':
    try:
        from dynamic_groups import load_chain_state
    except ImportError:
        from h3.dynamic_groups import load_chain_state
    release_daily(load_chain_state(os.environ['CHAIN_STATE']))
