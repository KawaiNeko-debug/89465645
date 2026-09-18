import os
from datetime import datetime


ACTIVITY_CODE = (os.getenv("BOX_LOTTERY_ACTIVITY_CODE") or "LAEE").strip() or "LAEE"
TURN_PATH = "/api/cgi/operationService/front/lottery/turn"
COUNT_PATH = "/api/cgi/operationService/front/lottery/getLuckyKeyCount"
CLAIM_PATH = "/api/cgi/operationService/front/lottery/receivePrize"


def is_box_lottery_required(task_date: str, group_code: str) -> bool:
    code = str(group_code or "").strip().lower()
    if not (code.startswith("old") or code.startswith("new") or code == "test"):
        return False
    try:
        return datetime.strptime(str(task_date or "")[:10], "%Y-%m-%d").weekday() == 5
    except ValueError:
        return False


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
