import json
import os
import sys
from datetime import datetime

try:
    from campaign_vote import is_vote_date
    from feature_flags import LISTING_GIFT_ENABLED, VOTE_ENABLED
    from listing_gift import is_listing_gift_date
except ImportError:
    from h3.campaign_vote import is_vote_date
    from h3.feature_flags import LISTING_GIFT_ENABLED, VOTE_ENABLED
    from h3.listing_gift import is_listing_gift_date


def truthy(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "result.json"
    if os.path.exists(path):
        return 0
    group_code = str(os.getenv("GROUP_CODE") or "").strip().lower()
    account_index = int(os.getenv("ACCOUNT_INDEX") or 0)
    category = str(os.getenv("ACCOUNT_CATEGORY") or "").strip()
    execution_mode = str(os.getenv("EXECUTION_MODE") or "full").strip()
    skipped = truthy(os.getenv("SKIP_SIGN"))
    task_date = os.getenv("SIGN_TASK_START_DATE", "")
    gift_required = LISTING_GIFT_ENABLED and is_listing_gift_date(task_date)
    vote_required = VOTE_ENABLED and is_vote_date(task_date)
    row = {
        "account_index": account_index,
        "execution_order": account_index,
        "group_name": group_code,
        "group_number": 0,
        "group_code": group_code,
        "group_position": f"{group_code}账号{account_index}",
        "account_category": category,
        "execution_mode": execution_mode,
        "sign_skipped": skipped,
        "sign_success": False,
        "sign_status": "取数异常" if skipped else "签到异常",
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
        "retry_count": 0,
        "is_final_retry": False,
        "detail_reason": "result.json missing",
        "activity_records": {"seckill": [], "lottery": [], "exchange": []},
        "account_data_required": True,
        "account_data_fetch_success": False,
        "account_data": {},
        "listing_gift_required": gift_required,
        "listing_gift_success": False,
        "listing_gift_attempted": False,
        "listing_gift_status": "未执行",
        "vote_required": vote_required,
        "vote_success": False,
        "vote_attempted": False,
        "vote_status": "未执行",
        "component_status": {
            "login": False,
            "sign": skipped,
            "points": False,
            "invoice": False,
            "pcb_orders": False,
            "coupons": False,
            "lottery": False,
            "exchange": False,
            "gift": not gift_required,
            "vote": not vote_required,
        },
    }
    payload = {
        "generated_at": datetime.now().isoformat(),
        "group_name": group_code,
        "group_code": group_code,
        "account_category": category,
        "execution_mode": execution_mode,
        "total_accounts": 1,
        "results": [row],
    }
    with open(path, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
