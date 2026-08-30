import argparse
import glob
import json
import os
from copy import deepcopy
from datetime import datetime
from pathlib import Path

try:
    from account_data import empty_account_data
    from campaign_vote import is_vote_date
    from listing_gift import should_claim_listing_gift
    from merge_results import pick_result
    from retry_components import COMPONENTS, component_status, retry_components
except ImportError:
    from h3.account_data import empty_account_data
    from h3.campaign_vote import is_vote_date
    from h3.listing_gift import should_claim_listing_gift
    from h3.merge_results import pick_result
    from h3.retry_components import COMPONENTS, component_status, retry_components


SENSITIVE_RESULT_KEYS = {"username", "password", "masked_username", "credentials"}


def truthy(value) -> bool:
    if value is True:
        return True
    if value is False or value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def safe_int(value, default=0) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default


def identity(value: dict) -> tuple[str, int]:
    source_group = str(value.get("source_group") or value.get("group_code") or "").strip().lower()
    return source_group, safe_int(value.get("account_index"), 0)


def sanitized(value):
    if isinstance(value, dict):
        return {
            key: sanitized(nested)
            for key, nested in value.items()
            if str(key).strip().lower() not in SENSITIVE_RESULT_KEYS
        }
    if isinstance(value, list):
        return [sanitized(item) for item in value]
    return value


def metadata_from_env() -> dict:
    source_group = str(os.getenv("SOURCE_GROUP") or os.getenv("GROUP_CODE") or "").strip().lower()
    return {
        "source_group": source_group,
        "group_code": source_group,
        "account_index": safe_int(os.getenv("ACCOUNT_INDEX"), 0),
        "execution_order": safe_int(os.getenv("EXECUTION_ORDER"), 0),
        "batch_id": str(os.getenv("BATCH_ID") or "").strip(),
        "account_category": str(os.getenv("ACCOUNT_CATEGORY") or "").strip(),
        "execution_mode": str(os.getenv("EXECUTION_MODE") or "").strip(),
        "sign_skipped": truthy(os.getenv("SKIP_SIGN")),
    }


def stamp_result(path: str | Path) -> int:
    target = Path(path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("result payload has no results list")
    metadata = metadata_from_env()
    stamped = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        row = sanitized(raw)
        row.update(metadata)
        row["group_name"] = metadata["source_group"]
        row["group_position"] = (
            f"{metadata['source_group']}账号{metadata['account_index']}"
        )
        row["component_status"] = component_status(row)
        stamped.append(row)
    payload = sanitized(payload)
    payload.update(
        {
            "batch_id": metadata["batch_id"],
            "group_name": metadata["source_group"],
            "group_code": metadata["source_group"],
            "source_group": metadata["source_group"],
            "account_category": metadata["account_category"],
            "execution_mode": metadata["execution_mode"],
            "total_accounts": len(stamped),
            "results": stamped,
        }
    )
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


def load_matrix(raw: str) -> list[dict]:
    try:
        payload = json.loads(str(raw or ""))
    except (TypeError, ValueError):
        return []
    include = payload.get("include") if isinstance(payload, dict) else None
    return [item for item in include if isinstance(item, dict)] if isinstance(include, list) else []


def load_result(path: str | Path) -> dict | None:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return None
    return sanitized(rows[0])


def result_map(results_dir: str | Path) -> dict[tuple[str, int], dict]:
    rows = {}
    pattern = os.path.join(str(results_dir), "**", "result.json")
    for path in glob.glob(pattern, recursive=True):
        row = load_result(path)
        if row and identity(row)[0] and identity(row)[1] > 0:
            rows[identity(row)] = row
    return rows


def applicable_components(account: dict, task_date: str) -> list[str]:
    components = list(COMPONENTS)
    source_group = str(account.get("source_group") or "").strip().lower()
    if truthy(account.get("skip_sign")) or source_group.startswith(("ll", "zh")):
        components.remove("sign")
    if not should_claim_listing_gift(task_date, source_group):
        components.remove("gift")
    if not is_vote_date(task_date):
        components.remove("vote")
    return components


def build_retry_matrix(
    results_dir: str | Path,
    batch_accounts: list[dict],
    candidate_accounts: list[dict] | None = None,
    task_date: str = "",
) -> list[dict]:
    rows = result_map(results_dir)
    candidates = candidate_accounts if candidate_accounts is not None else batch_accounts
    matrix = []
    for candidate in candidates:
        account = {
            key: candidate.get(key)
            for key in (
                "source_group",
                "account_index",
                "execution_order",
                "batch_id",
                "account_category",
                "execution_mode",
                "skip_sign",
            )
        }
        row = rows.get(identity(account))
        previous = [
            item.strip()
            for item in str(candidate.get("retry_components") or "").split(",")
            if item.strip() in COMPONENTS
        ]
        if row is None:
            components = previous or applicable_components(account, task_date)
        else:
            components = retry_components(row)
            if previous:
                components = [item for item in components if item in previous]
        if components:
            account["retry_components"] = ",".join(components)
            matrix.append(account)
    return matrix


def output_retry_matrix(args) -> int:
    batch_accounts = load_matrix(os.getenv(args.batch_matrix_env, ""))
    candidate_raw = os.getenv(args.candidate_matrix_env, "") if args.candidate_matrix_env else ""
    candidate_accounts = load_matrix(candidate_raw) if candidate_raw else None
    matrix = build_retry_matrix(
        args.results_dir,
        batch_accounts,
        candidate_accounts,
        args.task_date,
    )
    values = {
        "matrix": json.dumps({"include": matrix}, ensure_ascii=False, separators=(",", ":")),
        "has_failed": "true" if matrix else "false",
    }
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as file:
            for key, value in values.items():
                file.write(f"{key}={value}\n")
    else:
        print(json.dumps(values, ensure_ascii=False))
    return 0


def missing_result(account: dict, task_date: str) -> dict:
    source_group = str(account.get("source_group") or "").strip().lower()
    skip_sign = truthy(account.get("skip_sign"))
    gift_required = should_claim_listing_gift(task_date, source_group)
    vote_required = is_vote_date(task_date)
    row = {
        **deepcopy(account),
        "group_code": source_group,
        "group_name": source_group,
        "group_position": f"{source_group}账号{account.get('account_index')}",
        "sign_skipped": skip_sign,
        "sign_success": False,
        "sign_status": "取数异常" if skip_sign else "签到异常",
        "initial_points": 0.0,
        "final_points": 0.0,
        "points_reward": 0.0,
        "has_reward": False,
        "password_error": False,
        "risk_controlled": False,
        "banned_account": False,
        "points_fetch_success": False,
        "activity_fetch_success": False,
        "data_fetch_completed": False,
        "task_start_date": task_date,
        "sign_completed_at": "",
        "retry_count": 3,
        "is_final_retry": True,
        "detail_reason": "批次账号结果缺失",
        "sign_time": "",
        "sign_ip": "",
        "activity_records": {"seckill": [], "lottery": [], "exchange": []},
        "account_data_required": True,
        "account_data_fetch_success": False,
        "account_data": empty_account_data(),
        "listing_gift_required": gift_required,
        "listing_gift_success": False,
        "listing_gift_attempted": False,
        "listing_gift_status": "缺少每月礼包领取结果" if gift_required else "非每月礼包领取日期或当前组不适用",
        "listing_gift_time": "",
        "listing_gift_detail": "",
        "vote_required": vote_required,
        "vote_success": False,
        "vote_attempted": False,
        "vote_status": "缺少投票结果" if vote_required else "非投票日期",
        "vote_time": "",
        "vote_product_sku": "",
        "vote_product_name": "",
        "vote_detail": "",
    }
    row["component_status"] = component_status(row)
    return row


def merge_results(results_dir: str | Path, output_path: str | Path, batch_accounts: list[dict], task_date: str) -> int:
    attempts: dict[tuple[str, int], list[dict]] = {}
    pattern = os.path.join(str(results_dir), "**", "result.json")
    for path in glob.glob(pattern, recursive=True):
        row = load_result(path)
        if row:
            attempts.setdefault(identity(row), []).append(row)

    merged = []
    for account in batch_accounts:
        rows = sorted(
            attempts.get(identity(account), []),
            key=lambda item: safe_int(item.get("retry_count"), 0),
        )
        if not rows:
            selected = missing_result(account, task_date)
        else:
            selected = rows[0]
            for retry in rows[1:]:
                selected = pick_result(selected, retry)
            selected.update(
                {
                    key: account.get(key)
                    for key in (
                        "source_group",
                        "account_index",
                        "execution_order",
                        "batch_id",
                        "account_category",
                        "execution_mode",
                        "skip_sign",
                    )
                }
            )
            selected["group_code"] = account.get("source_group")
            selected["group_name"] = account.get("source_group")
            selected["group_position"] = (
                f"{account.get('source_group')}账号{account.get('account_index')}"
            )
            selected["sign_skipped"] = truthy(account.get("skip_sign"))
            selected["component_status"] = component_status(selected)
        merged.append(sanitized(selected))

    batch_id = str(batch_accounts[0].get("batch_id") or "") if batch_accounts else ""
    payload = {
        "generated_at": datetime.now().isoformat(),
        "workflow_mode": "mixed_batches",
        "batch_id": batch_id,
        "task_start_date": task_date,
        "total_accounts": len(merged),
        "results": merged,
    }
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"merged": len(merged), "output": str(target)}, ensure_ascii=False))
    return 0


def run_merge(args) -> int:
    batch_accounts = load_matrix(os.getenv(args.batch_matrix_env, ""))
    return merge_results(args.results_dir, args.output, batch_accounts, args.task_date)


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    stamp = commands.add_parser("stamp")
    stamp.add_argument("path")

    retry = commands.add_parser("retry-matrix")
    retry.add_argument("results_dir")
    retry.add_argument("--batch-matrix-env", default="BATCH_MATRIX")
    retry.add_argument("--candidate-matrix-env", default="")
    retry.add_argument("--task-date", default="")

    merge = commands.add_parser("merge")
    merge.add_argument("results_dir")
    merge.add_argument("output")
    merge.add_argument("--batch-matrix-env", default="BATCH_MATRIX")
    merge.add_argument("--task-date", default="")

    args = parser.parse_args()
    if args.command == "stamp":
        return stamp_result(args.path)
    if args.command == "retry-matrix":
        return output_retry_matrix(args)
    return run_merge(args)


if __name__ == "__main__":
    raise SystemExit(main())
