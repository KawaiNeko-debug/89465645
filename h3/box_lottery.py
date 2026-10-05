import os
from copy import deepcopy
from datetime import date, datetime


ACTIVITY_CODE = (os.getenv("BOX_LOTTERY_ACTIVITY_CODE") or "LAEE").strip() or "LAEE"
TURN_PATH = "/api/cgi/operationService/front/lottery/turn"
COUNT_PATH = "/api/cgi/operationService/front/lottery/getLuckyKeyCount"
CLAIM_PATH = "/api/cgi/operationService/front/lottery/receivePrize"


def is_box_lottery_required(task_date: str, group_code: str, assigned_date: str = "") -> bool:
    code = str(group_code or "").strip().lower()
    if not (
        code.startswith(("old", "wudi", "ld", "yyy", "new"))
        or code == "test"
    ):
        return False
    try:
        current = datetime.strptime(str(task_date or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return False

    if current >= date(2026, 10, 6) and code != "test":
        try:
            from h3.weekly_box_schedule import due
        except ImportError:
            from weekly_box_schedule import due
        assigned = assigned_date or os.getenv("WEEKLY_BOX_LOTTERY_ASSIGNED_DATE", "")
        return due(current, code, assigned)
    if current == date(2026, 10, 5):
        return False
    return current.weekday() == 1


def can_run_box_lottery_after_sign(
    required: bool,
    sign_success: bool,
    previous_sign_success: bool,
    risk_controlled: bool,
    banned_account: bool,
) -> bool:
    if not required or banned_account:
        return False
    return bool(sign_success or previous_sign_success or risk_controlled)


def empty_box_lottery() -> list[dict]:
    return [
        {"attempt": 1, "draw_success": False, "terminal": False, "draw_status": "待执行", "prizes": [], "claim_success": False, "claim_status": "待执行", "claim_detail": "", "draw_time": ""},
        {"attempt": 2, "draw_success": False, "terminal": False, "draw_status": "待执行", "prizes": [], "claim_success": False, "claim_status": "待执行", "claim_detail": "", "draw_time": ""},
    ]


def box_lottery_complete(required, rows, *, terminal_without_draw=True) -> bool:
    if not required:
        return True
    rows = rows if isinstance(rows, list) else []
    if len(rows) < 2 or not all(isinstance(item, dict) for item in rows[:2]):
        return False
    return all(
        (terminal_without_draw and bool(item.get("terminal")))
        or (bool(item.get("draw_success")) and bool(item.get("claim_success")))
        for item in rows[:2]
    )


def merge_box_records(current, previous):
    """Keep confirmed draws and their claim IDs even after a worse retry."""
    def rank(row):
        return (bool(row.get('draw_success')), bool(row.get('claim_success')),
                bool(row.get('terminal')), len(row.get('prizes') or []))
    current = current if isinstance(current, list) else []
    previous = previous if isinstance(previous, list) else []
    result = []
    for index in range(max(len(current), len(previous))):
        a = current[index] if index < len(current) else {}
        b = previous[index] if index < len(previous) else {}
        a = a if isinstance(a, dict) else {}
        b = b if isinstance(b, dict) else {}
        selected = deepcopy(b if rank(b) > rank(a) else a or b)
        # Preserve already-claimed rewards when another claim in the same
        # draw failed. Never replace prize IDs with an unrelated response.
        claimed_codes = {p.get('winCode') for row in (a, b)
                         for p in row.get('prizes', []) if isinstance(p, dict)
                         and p.get('winCode') and p.get('claim_status') == '已领取'}
        for prize in selected.get('prizes', []):
            if prize.get('winCode') in claimed_codes:
                prize['claim_status'] = '已领取'
        result.append(selected)
    return result
