"""Isolated monthly-gift compensation using exact batch/summary run IDs."""
import argparse
import io
import json
import os
import zipfile
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

try:
    from dynamic_groups import api_request, write_json
    from mixed_batches import (configured_group_counts, new_chain_state, dispatch_batch,
                               dispatch_summary, download_batches, encode_chain_state,
                               load_chain_state, github_context)
    from mixed_results import sanitized, identity
    from listing_gift import (is_listing_gift_date, is_monthly_gift_group,
                              gift_result_success, gift_result_rank)
    from merge_results import LISTING_GIFT_FIELDS, pick_result
    from retry_components import component_status
    from account_data import predict_pcb_smt
except ImportError:
    from h3.dynamic_groups import api_request, write_json
    from h3.mixed_batches import (configured_group_counts, new_chain_state, dispatch_batch,
                                  dispatch_summary, download_batches, encode_chain_state,
                                  load_chain_state, github_context)
    from h3.mixed_results import sanitized, identity
    from h3.listing_gift import (is_listing_gift_date, is_monthly_gift_group,
                                 gift_result_success, gift_result_rank)
    from h3.merge_results import LISTING_GIFT_FIELDS, pick_result
    from h3.retry_components import component_status
    from h3.account_data import predict_pcb_smt


def assert_idle(repo, token, current_run_id):
    for status in ('queued', 'in_progress', 'waiting', 'requested', 'pending'):
        for page in range(1, 11):
            payload = api_request('GET', repo, token, '/actions/runs',
                                  params={'status': status, 'per_page': 100, 'page': page})
            runs = payload.get('workflow_runs', [])
            for run in runs:
                if str(run.get('id')) == str(current_run_id):
                    continue
                workflow = str(run.get('path') or '').split('@')[0]
                if workflow in {
                    '.github/workflows/dynamic-controller.yml',
                    '.github/workflows/dynamic-batch.yml',
                    '.github/workflows/dynamic-group.yml',
                    '.github/workflows/dynamic-summary.yml',
                }:
                    raise RuntimeError('another daily/recovery chain is active; no gifts dispatched')
            if len(runs) < 100:
                break
        else:
            raise RuntimeError('active run inventory incomplete; no gifts dispatched')


def exact_summary_manifest(repo, token, run_id, task_date):
    run = api_request('GET', repo, token, f'/actions/runs/{int(run_id)}')
    if (run.get('status') != 'completed'
            or str(run.get('path', '')).split('@')[0] != '.github/workflows/dynamic-summary.yml'):
        raise ValueError('baseline must be an exact completed daily summary run')
    artifacts = api_request('GET', repo, token, f'/actions/runs/{int(run_id)}/artifacts',
                            params={'per_page': 100}).get('artifacts', [])
    expected_id = str(run.get('display_title') or '').removeprefix('dynamic-summary-')
    artifact = next((row for row in artifacts if not row.get('expired')
                     and row.get('name') == f'category-reports-{expected_id}'), None)
    if artifact is None:
        raise ValueError('exact baseline summary artifact is unavailable')
    raw = api_request('GET', repo, token, f"/actions/artifacts/{artifact['id']}/zip", raw=True)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = [name for name in archive.namelist() if name.rsplit('/', 1)[-1] == 'manifest.json']
        if len(names) != 1:
            raise ValueError('baseline manifest is ambiguous')
        # Never extract the XLSX files: they may contain report-only passwords.
        state = load_chain_state(archive.read(names[0]).decode('utf-8'))
    if state.get('task_start_date') != task_date or state.get('gift_recovery'):
        raise ValueError('baseline must be the original daily task on the same date')
    return state


def recovery_state(orchestration_id, ref, task_date, counts, baseline=None):
    if not is_listing_gift_date(task_date):
        raise ValueError('not a monthly gift claim date')
    selected = {code: count for code, count in counts.items()
                if is_monthly_gift_group(code) and code != 'gift_test'}
    state = new_chain_state(orchestration_id, ref, task_date, group_counts=selected)
    state.update(gift_recovery=True, component_scope=['gift', 'coupons'])
    if baseline:
        if baseline.get('task_start_date') != task_date or baseline.get('gift_recovery'):
            raise ValueError('invalid recovery baseline')
        state['recovery_base_chain'] = baseline
    return state


def collect_rows(root):
    output = {}
    for path in sorted(Path(root).rglob('result.json')):
        payload = json.loads(path.read_text(encoding='utf-8'))
        for raw in payload.get('results', []):
            row = sanitized(raw)
            key = identity(row)
            output[key] = pick_result(output[key], row) if key in output else row
    return output


def overlay_gift_result(base, patch):
    if base is None:
        output = deepcopy(patch)
        output['sign_status'] = '未执行（仅补领每月礼包）'
        output['execution_mode'] = 'gift_recovery'
        if not output.get('password_error') and not output.get('account_format_error'):
            output['detail_reason'] = ('' if gift_result_success(output)
                                       else str(output.get('listing_gift_status') or '每月礼包尚未完成'))
        return output
    output = deepcopy(base)
    if gift_result_rank(patch) >= gift_result_rank(output):
        for key in LISTING_GIFT_FIELDS:
            output[key] = patch.get(key)
    fresh = patch.get('account_data') or {}
    if fresh.get('coupon_fetch_success') is True:
        account = output.setdefault('account_data', {})
        account['coupons'] = deepcopy(fresh.get('coupons') or {})
        account['coupon_fetch_success'] = True
        account['coupon_prediction'], account['prediction_reason'] = predict_pcb_smt(account)
    output['component_status'] = component_status(output)
    return output


def merge_recovery_reports(output_dir, manifest):
    root = Path(output_dir)
    patches = {}
    for batch in manifest['batches']:
        patches.update(collect_rows(root / batch['batch_id']))
    baseline = manifest.get('recovery_base_chain')
    rows = {}
    if baseline:
        download_batches(SimpleNamespace(chain_state_env='', chain_state=encode_chain_state(baseline),
                                          output_dir=str(root / 'baseline')))
        rows = collect_rows(root / 'baseline')
        manifest['report_groups'] = deepcopy(baseline['groups'])
        manifest['weekly_assignments'] = deepcopy(baseline.get('weekly_assignments', {}))
    for key, patch in patches.items():
        rows[key] = overlay_gift_result(rows.get(key), patch)
    combined = root / 'combined'
    combined.mkdir(exist_ok=True)
    write_json(combined / 'result.json', {'task_start_date': manifest['task_start_date'],
                                        'results': list(rows.values())})
    write_json(combined / 'manifest.json', manifest)
    manifest['recovery_overlay_ready'] = True
    write_json(root / 'manifest.json', manifest)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--orchestration-id', required=True)
    parser.add_argument('--ref', default='main')
    args = parser.parse_args()
    task_date = os.getenv('RECOVERY_TASK_DATE') or datetime.now(
        timezone(timedelta(hours=8))).date().isoformat()
    repo, token = github_context()
    assert_idle(repo, token, args.orchestration_id)
    run_id = int(os.getenv('BASE_SUMMARY_RUN_ID') or 0)
    baseline = exact_summary_manifest(repo, token, run_id, task_date) if run_id else None
    counts = baseline['group_counts'] if baseline else configured_group_counts()
    state = recovery_state(args.orchestration_id, args.ref, task_date, counts, baseline)
    state['recovery_base_summary_run_id'] = run_id
    try:
        from recovery_handoff import update_handoff, WeeklyStore
    except ImportError:
        from h3.recovery_handoff import update_handoff, WeeklyStore
    update_handoff(WeeklyStore(), args.orchestration_id)
    write_json('chain-state.json', state)
    if state['batches']:
        dispatch_batch(state, state['batches'][0]['batch_id'])
    else:
        dispatch_summary(state)
    print(f"[monthly-gift] dispatched {len(state['batches'])} isolated batches; "
          f"accounts={state['total_accounts']}; no sign/vote/lottery actions")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
