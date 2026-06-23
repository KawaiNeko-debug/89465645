import glob
import json
import os
import re
import sys
from datetime import datetime

RISK_CONTROL_MESSAGE = (os.getenv("RISK_CONTROL_MESSAGE") or "签到失败，疑似违反签到规则").strip()
DATA_FAILURE_MARKERS = (
    "金豆数量获取失败",
    "中奖记录获取未完成",
    "秒杀数据获取失败",
    "抽奖数据获取失败",
    "奖品过期记录获取失败",
    "活动数据获取失败",
    "活动数据抓取异常",
)


def truthy(value) -> bool:
    if value is True:
        return True
    if value is False or value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def has_risk_control_text(*values) -> bool:
    if not RISK_CONTROL_MESSAGE:
        return False
    return any(RISK_CONTROL_MESSAGE in str(value or "") for value in values)


def is_risk_control_result(row: dict) -> bool:
    if not isinstance(row, dict):
        return False
    return truthy(row.get("risk_controlled")) or has_risk_control_text(
        row.get("detail_reason"),
        row.get("sign_status"),
    )


def safe_int(value, default=0) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default


def strip_resolved_data_failures(text: str) -> str:
    parts = [part.strip() for part in re.split(r"[；;]\s*", str(text or "")) if part.strip()]
    kept = [part for part in parts if not any(marker in part for marker in DATA_FAILURE_MARKERS)]
    return "；".join(kept)


def merge_data_fields(picked: dict, fallback: dict | None):
    if not fallback:
        return
    if not truthy(picked.get("points_fetch_success")) and truthy(fallback.get("points_fetch_success")):
        for key in ("initial_points", "final_points", "points_reward"):
            picked[key] = fallback.get(key, 0.0)
        picked["points_fetch_success"] = True
    if not truthy(picked.get("activity_fetch_success")) and truthy(fallback.get("activity_fetch_success")):
        picked["activity_records"] = fallback.get("activity_records") or {"seckill": [], "lottery": []}
        picked["activity_fetch_success"] = True
    picked["data_fetch_completed"] = (
        truthy(picked.get("points_fetch_success"))
        and truthy(picked.get("activity_fetch_success"))
    )
    if truthy(picked.get("data_fetch_completed")):
        picked["detail_reason"] = strip_resolved_data_failures(picked.get("detail_reason", ""))
        if is_risk_control_result(picked) and not picked.get("detail_reason"):
            picked["detail_reason"] = RISK_CONTROL_MESSAGE


def load_single_result(path: str):
    try:
        with open(path, "r", encoding="utf-8") as file:
            payload = json.load(file)
    except Exception:
        return None
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        return None
    row = rows[0]
    if not isinstance(row, dict):
        return None
    row["_batch_name"] = payload.get("batch_name") if isinstance(payload, dict) else ""
    row["_group_name"] = payload.get("group_name") if isinstance(payload, dict) else ""
    row["_group_number"] = payload.get("group_number") if isinstance(payload, dict) else 0
    return row


def score(row: dict):
    return (
        1 if truthy(row.get("sign_success")) else 0,
        1 if truthy(row.get("data_fetch_completed")) else 0,
        int(truthy(row.get("points_fetch_success"))) + int(truthy(row.get("activity_fetch_success"))),
        safe_int(row.get("retry_count"), 0),
        1 if truthy(row.get("risk_controlled")) else 0,
    )


def pick_result(initial: dict, retry: dict | None):
    if retry is None:
        return initial
    if is_risk_control_result(initial):
        picked = initial
    elif truthy(initial.get("banned_account")) and not truthy(retry.get("banned_account")):
        picked = initial
    elif is_risk_control_result(retry) and not truthy(initial.get("sign_success")):
        picked = retry
    else:
        picked = retry if score(retry) >= score(initial) else initial
    fallback = initial if picked is retry else retry
    if fallback:
        for key in ("activity_records", "final_points", "sign_time", "sign_completed_at"):
            if not picked.get(key) and fallback.get(key):
                picked[key] = fallback[key]
        merge_data_fields(picked, fallback)
        if truthy(picked.get("banned_account")):
            if not truthy(picked.get("points_fetch_success")) and truthy(fallback.get("points_fetch_success")):
                for key in ("initial_points", "final_points", "points_reward"):
                    picked[key] = fallback.get(key, 0.0)
                picked["points_fetch_success"] = True
            if not truthy(picked.get("activity_fetch_success")) and truthy(fallback.get("activity_fetch_success")):
                picked["activity_records"] = fallback.get("activity_records") or {"seckill": [], "lottery": []}
                picked["activity_fetch_success"] = True
            picked["data_fetch_completed"] = (
                truthy(picked.get("points_fetch_success"))
                and truthy(picked.get("activity_fetch_success"))
            )
            failures = []
            if not truthy(picked.get("points_fetch_success")):
                failures.append("金豆数量获取失败")
            if not truthy(picked.get("activity_fetch_success")):
                failures.append("中奖记录获取未完成")
            picked["sign_status"] = "账号封禁" if not failures else "封禁账号取数失败"
            picked["detail_reason"] = "账号在 BANNED_ACCOUNTS 中，已跳过签到"
            if failures:
                picked["detail_reason"] += "；" + "；".join(failures)
    return picked


def main():
    results_dir = sys.argv[1] if len(sys.argv) > 1 else "results"
    output_path = sys.argv[2] if len(sys.argv) > 2 else "result.json"

    initial_map = {}
    retry_map = {}

    for path in glob.glob(os.path.join(results_dir, "**", "result.json"), recursive=True):
        row = load_single_result(path)
        if not row:
            continue
        account_index = safe_int(row.get("account_index"), 0)
        if account_index <= 0:
            continue
        normalized_path = path.replace("\\", "/").lower()
        if "/retry-result-" in normalized_path:
            retry_map[account_index] = row
        elif "/initial-result-" in normalized_path:
            initial_map[account_index] = row

    merged = []
    account_indexes = sorted(set(initial_map.keys()) | set(retry_map.keys()))
    for account_index in account_indexes:
        initial = initial_map.get(account_index)
        retry = retry_map.get(account_index)
        if initial and retry:
            merged.append(pick_result(initial, retry))
        elif retry:
            merged.append(retry)
        elif initial:
            merged.append(initial)

    group_name = ""
    group_number = 0
    task_start_date = ""
    if merged:
        group_name = merged[0].get("group_name") or merged[0].get("_group_name") or merged[0].get("_batch_name") or ""
        group_number = safe_int(merged[0].get("group_number", merged[0].get("_group_number")), 0)
        task_start_date = str(merged[0].get("task_start_date") or "").strip()

    payload = {
        "generated_at": datetime.now().isoformat(),
        "batch_name": group_name,
        "group_name": group_name,
        "group_number": group_number,
        "task_start_date": task_start_date,
        "total_accounts": len(merged),
        "results": [],
    }

    for row in sorted(merged, key=lambda item: safe_int(item.get("account_index"), 0)):
        payload["results"].append(
            {
                "account_index": safe_int(row.get("account_index"), 0),
                "execution_order": safe_int(row.get("execution_order"), 0),
                "group_name": row.get("group_name") or group_name,
                "group_number": safe_int(row.get("group_number"), group_number),
                "group_position": row.get("group_position") or (
                    f"{group_number}组账号{safe_int(row.get('account_index'), 0)}" if group_number > 0 else f"账号{safe_int(row.get('account_index'), 0)}"
                ),
                "sign_success": truthy(row.get("sign_success")),
                "sign_status": row.get("sign_status", ""),
                "initial_points": row.get("initial_points", 0.0),
                "final_points": row.get("final_points", 0.0),
                "points_reward": row.get("points_reward", 0.0),
                "has_reward": truthy(row.get("has_reward")),
                "password_error": truthy(row.get("password_error")),
                "risk_controlled": truthy(row.get("risk_controlled")),
                "banned_account": truthy(row.get("banned_account")),
                "points_fetch_success": truthy(row.get("points_fetch_success")),
                "activity_fetch_success": truthy(row.get("activity_fetch_success")),
                "data_fetch_completed": truthy(row.get("data_fetch_completed")),
                "next_day_success": truthy(row.get("next_day_success")),
                "task_start_date": row.get("task_start_date", ""),
                "sign_completed_at": row.get("sign_completed_at", ""),
                "retry_count": safe_int(row.get("retry_count"), 0),
                "is_final_retry": truthy(row.get("is_final_retry")),
                "detail_reason": row.get("detail_reason", ""),
                "sign_time": row.get("sign_time", ""),
                "sign_ip": row.get("sign_ip", ""),
                "activity_records": row.get("activity_records") or {"seckill": [], "lottery": []},
            }
        )

    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
    print(json.dumps({"merged": len(merged), "output": output_path}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
