import glob
import json
import os
import re
import sys
from datetime import datetime

try:
    from retry_components import component_status
except ImportError:
    from h3.retry_components import component_status

RISK_CONTROL_MESSAGE = (os.getenv("RISK_CONTROL_MESSAGE") or "签到失败，疑似违反签到规则").strip()
DATA_FAILURE_MARKERS = (
    "金豆数量获取失败",
    "中奖记录获取未完成",
    "秒杀数据获取失败",
    "抽奖数据获取失败",
    "奖品过期记录获取失败",
    "兑换记录获取失败",
    "活动数据获取失败",
    "活动数据抓取异常",
    "会员资料获取未完成",
    "会员资料接口未完整返回",
)
VOTE_FIELDS = (
    "vote_required",
    "vote_success",
    "vote_attempted",
    "vote_status",
    "vote_time",
    "vote_product_sku",
    "vote_product_name",
    "vote_detail",
)
LISTING_GIFT_FIELDS = (
    "listing_gift_required",
    "listing_gift_success",
    "listing_gift_attempted",
    "listing_gift_status",
    "listing_gift_time",
    "listing_gift_detail",
)
EMPTY_ACTIVITY_RECORDS = {"seckill": [], "lottery": [], "exchange": []}


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
        picked["activity_records"] = fallback.get("activity_records") or dict(EMPTY_ACTIVITY_RECORDS)
        picked["activity_fetch_success"] = True
    picked_activity = picked.get("activity_records") if isinstance(picked.get("activity_records"), dict) else {}
    fallback_activity = fallback.get("activity_records") if isinstance(fallback.get("activity_records"), dict) else {}
    merged_activity = {}
    for key in ("seckill", "lottery", "exchange"):
        current_rows = picked_activity.get(key) if isinstance(picked_activity.get(key), list) else []
        fallback_rows = fallback_activity.get(key) if isinstance(fallback_activity.get(key), list) else []
        merged_activity[key] = fallback_rows if len(fallback_rows) > len(current_rows) else current_rows
    picked["activity_records"] = merged_activity
    if truthy(fallback.get("account_data_fetch_success")) and not truthy(picked.get("account_data_fetch_success")):
        picked["account_data"] = fallback.get("account_data") or {}
        picked["account_data_fetch_success"] = True
    picked["account_data_required"] = truthy(picked.get("account_data_required")) or truthy(
        fallback.get("account_data_required")
    )
    picked["data_fetch_completed"] = (
        truthy(picked.get("points_fetch_success"))
        and truthy(picked.get("activity_fetch_success"))
        and (
            not truthy(picked.get("account_data_required"))
            or truthy(picked.get("account_data_fetch_success"))
        )
    )
    if truthy(picked.get("data_fetch_completed")):
        picked["detail_reason"] = strip_resolved_data_failures(picked.get("detail_reason", ""))
        if is_risk_control_result(picked) and not picked.get("detail_reason"):
            picked["detail_reason"] = RISK_CONTROL_MESSAGE


def merge_component_fields(picked: dict, fallback: dict | None):
    if not fallback:
        picked["component_status"] = component_status(picked)
        return
    picked_status = component_status(picked)
    fallback_status = component_status(fallback)
    picked_account = picked.get("account_data") if isinstance(picked.get("account_data"), dict) else {}
    fallback_account = fallback.get("account_data") if isinstance(fallback.get("account_data"), dict) else {}
    account_groups = {
        "invoice": (
            "invoice_fetch_success", "invoice_profile_status", "invoice_profile_exists",
            "invoice_month_threshold", "invoice_within_months_amount", "invoice_over_months_amount",
        ),
        "pcb_orders": (
            "pcb_order_fetch_success", "pcb_within_months_amount", "pcb_over_months_amount",
            "pcb_total_amount", "pcb_amount_shortfall", "pcb_order_count",
        ),
        "coupons": ("coupon_fetch_success", "coupons", "coupon_prediction", "prediction_reason"),
    }
    for component, keys in account_groups.items():
        if not picked_status[component] and fallback_status[component]:
            for key in keys:
                if key in fallback_account:
                    picked_account[key] = fallback_account[key]
            picked_status[component] = True
    picked["account_data"] = picked_account

    picked_activity = picked.get("activity_records") if isinstance(picked.get("activity_records"), dict) else {}
    fallback_activity = fallback.get("activity_records") if isinstance(fallback.get("activity_records"), dict) else {}
    for component, key in (("lottery", "lottery"), ("exchange", "exchange")):
        if not picked_status[component] and fallback_status[component]:
            picked_activity[key] = fallback_activity.get(key) or []
            picked_status[component] = True
    picked["activity_records"] = {
        "seckill": picked_activity.get("seckill") or fallback_activity.get("seckill") or [],
        "lottery": picked_activity.get("lottery") or [],
        "exchange": picked_activity.get("exchange") or [],
    }
    picked["component_status"] = {
        key: bool(picked_status.get(key) or fallback_status.get(key))
        for key in set(picked_status) | set(fallback_status)
    }
    picked_account["fetch_success"] = all(
        picked["component_status"].get(key, False)
        for key in ("invoice", "pcb_orders", "coupons")
    )
    picked["account_data_fetch_success"] = picked_account["fetch_success"]
    picked["activity_fetch_success"] = all(
        picked["component_status"].get(key, False)
        for key in ("lottery", "exchange")
    )
    picked["data_fetch_completed"] = (
        picked["component_status"].get("points", False)
        and picked["activity_fetch_success"]
        and (
            not truthy(picked.get("account_data_required"))
            or picked["account_data_fetch_success"]
        )
    )


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
    row["_group_code"] = payload.get("group_code") if isinstance(payload, dict) else ""
    row["_account_category"] = payload.get("account_category") if isinstance(payload, dict) else ""
    row["_execution_mode"] = payload.get("execution_mode") if isinstance(payload, dict) else ""
    return row


def score(row: dict):
    return (
        1 if truthy(row.get("sign_success")) or (
            truthy(row.get("sign_skipped")) and truthy(row.get("data_fetch_completed"))
        ) else 0,
        1 if truthy(row.get("listing_gift_success")) else 0,
        1 if truthy(row.get("vote_success")) else 0,
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
        merge_component_fields(picked, fallback)
        if truthy(fallback.get("vote_success")) and not truthy(picked.get("vote_success")):
            for key in VOTE_FIELDS:
                picked[key] = fallback.get(key)
        if truthy(fallback.get("listing_gift_success")) and not truthy(picked.get("listing_gift_success")):
            for key in LISTING_GIFT_FIELDS:
                picked[key] = fallback.get(key)
        if truthy(picked.get("banned_account")):
            if not truthy(picked.get("points_fetch_success")) and truthy(fallback.get("points_fetch_success")):
                for key in ("initial_points", "final_points", "points_reward"):
                    picked[key] = fallback.get(key, 0.0)
                picked["points_fetch_success"] = True
            if not truthy(picked.get("activity_fetch_success")) and truthy(fallback.get("activity_fetch_success")):
                picked["activity_records"] = fallback.get("activity_records") or dict(EMPTY_ACTIVITY_RECORDS)
                picked["activity_fetch_success"] = True
            if truthy(fallback.get("account_data_fetch_success")) and not truthy(picked.get("account_data_fetch_success")):
                picked["account_data"] = fallback.get("account_data") or {}
                picked["account_data_fetch_success"] = True
            picked["data_fetch_completed"] = (
                truthy(picked.get("points_fetch_success"))
                and truthy(picked.get("activity_fetch_success"))
                and (
                    not truthy(picked.get("account_data_required"))
                    or truthy(picked.get("account_data_fetch_success"))
                )
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
    picked["component_status"] = component_status(picked)
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
            retry_map.setdefault(account_index, []).append(row)
        elif "/initial-result-" in normalized_path or "/account-result-" in normalized_path:
            initial_map[account_index] = row

    merged = []
    account_indexes = sorted(set(initial_map.keys()) | set(retry_map.keys()))
    for account_index in account_indexes:
        initial = initial_map.get(account_index)
        retries = sorted(
            retry_map.get(account_index, []),
            key=lambda item: safe_int(item.get("retry_count"), 0),
        )
        if initial:
            selected = initial
            for retry in retries:
                selected = pick_result(selected, retry)
            merged.append(selected)
        elif retries:
            selected = retries[0]
            for retry in retries[1:]:
                selected = pick_result(selected, retry)
            merged.append(selected)
        elif initial:
            merged.append(initial)

    group_name = ""
    group_number = 0
    group_code = ""
    account_category = ""
    execution_mode = ""
    task_start_date = ""
    if merged:
        group_name = merged[0].get("group_name") or merged[0].get("_group_name") or merged[0].get("_batch_name") or ""
        group_number = safe_int(merged[0].get("group_number", merged[0].get("_group_number")), 0)
        group_code = str(merged[0].get("group_code") or merged[0].get("_group_code") or "").strip()
        account_category = str(merged[0].get("account_category") or merged[0].get("_account_category") or "").strip()
        execution_mode = str(merged[0].get("execution_mode") or merged[0].get("_execution_mode") or "").strip()
        task_start_date = str(merged[0].get("task_start_date") or "").strip()

    payload = {
        "generated_at": datetime.now().isoformat(),
        "batch_name": group_name,
        "group_name": group_name,
        "group_number": group_number,
        "group_code": group_code,
        "account_category": account_category,
        "execution_mode": execution_mode,
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
                    f"{group_code}账号{safe_int(row.get('account_index'), 0)}" if group_code else (
                        f"{group_number}组账号{safe_int(row.get('account_index'), 0)}" if group_number > 0 else f"账号{safe_int(row.get('account_index'), 0)}"
                    )
                ),
                "group_code": row.get("group_code") or group_code,
                "account_category": row.get("account_category") or account_category,
                "execution_mode": row.get("execution_mode") or execution_mode,
                "sign_skipped": truthy(row.get("sign_skipped")),
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
                "activity_records": row.get("activity_records") or dict(EMPTY_ACTIVITY_RECORDS),
                "account_data_required": truthy(row.get("account_data_required")),
                "account_data_fetch_success": truthy(row.get("account_data_fetch_success")),
                "account_data": row.get("account_data") or {},
                "listing_gift_required": truthy(row.get("listing_gift_required")),
                "listing_gift_success": truthy(row.get("listing_gift_success")),
                "listing_gift_attempted": truthy(row.get("listing_gift_attempted")),
                "listing_gift_status": row.get("listing_gift_status", ""),
                "listing_gift_time": row.get("listing_gift_time", ""),
                "listing_gift_detail": row.get("listing_gift_detail", ""),
                "vote_required": truthy(row.get("vote_required")),
                "vote_success": truthy(row.get("vote_success")),
                "vote_attempted": truthy(row.get("vote_attempted")),
                "vote_status": row.get("vote_status", ""),
                "vote_time": row.get("vote_time", ""),
                "vote_product_sku": row.get("vote_product_sku", ""),
                "vote_product_name": row.get("vote_product_name", ""),
                "vote_detail": row.get("vote_detail", ""),
                "component_status": component_status(row),
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
