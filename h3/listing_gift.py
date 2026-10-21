import re
import time
from datetime import date, datetime, timezone, timedelta
from urllib.parse import urlsplit, urlunsplit


MONTHLY_GIFT_DAY = 1
MONTHLY_GIFT_SPECIAL_DATE = "2026-10-22"
MONTHLY_GIFT_REGULAR_START = "2026-11-01"
MONTHLY_GIFT_PAGE_PATH = "/pages/coupon-page/index?id=43"
MONTHLY_GIFT_API_PATH = "/api/appPlatform/couponPage/receiveCoupon"
MONTHLY_GIFT_ID = 43
MONTHLY_GIFT_GROUP_PREFIXES = ("wudi", "ld", "yyy", "new")
MONTHLY_GIFT_TEST_GROUP = "gift_test"
# Legacy names are retained so existing result fields and imports remain compatible.
LISTING_GIFT_DATES = {MONTHLY_GIFT_SPECIAL_DATE}
LISTING_GIFT_PATH = MONTHLY_GIFT_PAGE_PATH
GIFT_GROUP_RE = re.compile(r"(?:wudi|ld|yyy|new)(?:[1-9]|1[0-9]|20)$")


def monthly_gift_origin(base_url: str) -> str:
    """Derive the mobile origin for the campaign gift page.

    The configured application URL may use a desktop host.  Gift claiming is
    served by its sibling mobile host, so derive that host from configuration
    rather than hard-coding a production domain.
    """
    value = str(base_url or "").strip().rstrip("/")
    try:
        parts = urlsplit(value)
        hostname = parts.hostname or ""
        if not parts.scheme or not hostname:
            return value
        labels = hostname.split(".")
        if labels and labels[0].lower() == "www":
            labels[0] = "m"
        elif not labels or labels[0].lower() != "m":
            labels.insert(0, "m")
        mobile_host = ".".join(labels)
        if parts.port:
            mobile_host = f"{mobile_host}:{parts.port}"
        return urlunsplit((parts.scheme, mobile_host, "", "", "")).rstrip("/")
    except ValueError:
        return value
ALREADY_RECEIVED_HINTS = (
    "已领取",
    "已经领取",
    "重复领取",
    "领取过",
    "不可重复",
    "限制周期内无法再次参与",
)


def date_part(value="") -> str:
    match = re.search(r"\d{4}-\d{2}-\d{2}", str(value or ""))
    return match.group(0) if match else ""


def is_listing_gift_date(value) -> bool:
    text = date_part(value)
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return False
    return text == MONTHLY_GIFT_SPECIAL_DATE or (
        text >= MONTHLY_GIFT_REGULAR_START and parsed.day == MONTHLY_GIFT_DAY
    )


def is_monthly_gift_group(group_code: str) -> bool:
    value = str(group_code or "").strip().lower()
    return value == MONTHLY_GIFT_TEST_GROUP or bool(GIFT_GROUP_RE.fullmatch(value))


def should_claim_listing_gift(value, group_code: str = "") -> bool:
    return is_listing_gift_date(value) and is_monthly_gift_group(group_code)


def gift_identity(task_date) -> dict:
    return {"listing_gift_id": MONTHLY_GIFT_ID, "listing_gift_month": date_part(task_date)[:7]}


def gift_result_success(row: dict, task_date="") -> bool:
    task_date = (task_date or row.get("task_start_date") or row.get("_task_start_date")
                 or row.get("listing_gift_time") or "")
    expected = gift_identity(task_date)
    return (
        row.get("listing_gift_success") in (True, "true", "True", 1)
        and row.get("listing_gift_id") == expected["listing_gift_id"]
        and bool(expected["listing_gift_month"])
        and row.get("listing_gift_month") == expected["listing_gift_month"]
    )


def gift_stock_exhausted(row: dict, task_date="") -> bool:
    task_date = (task_date or row.get("task_start_date") or row.get("_task_start_date")
                 or row.get("listing_gift_time") or "")
    expected = gift_identity(task_date)
    return (
        not gift_result_success(row, task_date)
        and row.get("listing_gift_id") == expected["listing_gift_id"]
        and bool(expected["listing_gift_month"])
        and row.get("listing_gift_month") == expected["listing_gift_month"]
        and "券余量不足" in str(row.get("listing_gift_status") or "") + str(
            row.get("listing_gift_detail") or "")
    )


def gift_result_rank(row: dict, task_date="") -> int:
    # A terminal shortage must survive unrelated retries, but never replace success.
    return 2 if gift_result_success(row, task_date) else int(gift_stock_exhausted(row, task_date))


class MonthlyGiftCheckpoint:
    """Store confirmed claims only; the official endpoint enforces cycle limits."""

    def __init__(self, task_date, group, index, store=None):
        if not is_monthly_gift_group(group) or int(index) < 1:
            raise ValueError("invalid monthly gift identity")
        month = date.fromisoformat(date_part(task_date)).strftime("%Y-%m")
        self.identity = {"month": month, "source_group": group, "account_index": int(index),
                         "gift_id": MONTHLY_GIFT_ID}
        if store is None:
            try:
                from weekly_box_schedule import WeeklyStore
            except ImportError:
                from h3.weekly_box_schedule import WeeklyStore
            store = WeeklyStore()
        self.store = store
        self.path = f"monthly-gifts/{month}/{group}-{int(index)}.json"

    def read(self):
        row, sha = self.store.read(self.path)
        if row is not None and (
            not isinstance(row, dict)
            or set(row) != set(self.identity) | {"state", "completed_at"}
            or any(row.get(key) != value for key, value in self.identity.items())
            or row.get("state") not in {"received", "already"}
        ):
            raise ValueError("invalid monthly gift checkpoint")
        return row, sha

    def completed(self):
        row, _ = self.read()
        return row is not None

    def save(self, state):
        try:
            from weekly_box_schedule import StateConflict
        except ImportError:
            from h3.weekly_box_schedule import StateConflict
        if state not in {"received", "already"}:
            raise ValueError("unconfirmed monthly gift cannot be saved")
        self.store.ensure_branch()
        for _ in range(8):
            current, sha = self.read()
            if current:
                return
            payload = dict(self.identity, state=state,
                           completed_at=datetime.now(timezone(timedelta(hours=8))).isoformat())
            try:
                self.store.write(self.path, payload, sha)
                return
            except StateConflict:
                time.sleep(.3)
        raise RuntimeError("monthly gift checkpoint busy")


def _message(response) -> str:
    values = []
    if isinstance(response, dict):
        for key in ("message", "msg", "errorMessage"):
            if response.get(key):
                values.append(str(response[key]).strip())
        data = response.get("data")
        if isinstance(data, dict):
            for key in ("message", "msg", "errorMessage"):
                if data.get(key):
                    values.append(str(data[key]).strip())
    return "；".join(dict.fromkeys(value for value in values if value))


def inspect_listing_gift_response(response) -> dict:
    message = _message(response)
    if "券余量不足" in message:
        return {"state": "stock_exhausted", "success": False, "message": "券余量不足"}
    if any(hint in message for hint in ALREADY_RECEIVED_HINTS):
        return {"state": "already", "success": True, "message": message or "每月礼包已领取"}
    if not isinstance(response, dict) or response.get("success") is not True:
        return {"state": "error", "success": False, "message": message or "礼包接口请求失败"}
    data = response.get("data")
    # The mobile gift endpoint returns the IDs of the coupons issued by the
    # bundle, rather than a nested {success: true} object.
    if response.get("success") is True and isinstance(data, list):
        return {
            "state": "received" if data else "already",
            "success": True,
            "message": "每月礼包领取成功" if data else "每月礼包已处理",
            "coupon_ids": [str(item).strip() for item in data if str(item).strip()],
        }
    if isinstance(data, dict) and data.get("success") is True:
        return {
            "state": "received",
            "success": True,
            "message": "每月礼包领取成功",
            "order_code": str(data.get("orderCode") or "").strip(),
        }
    code = response.get("code")
    if (
        response.get("success") is True
        and isinstance(data, dict)
        and data.get("success") is False
        and not message
        and not str(data.get("orderCode") or "").strip()
        and code in (None, 0, 200, "0", "200")
    ):
        return {
            "state": "already",
            "success": True,
            "message": "礼包接口已处理，无新增礼包订单",
        }
    return {"state": "error", "success": False, "message": message or "礼包接口未确认领取成功"}


def inspect_monthly_gift_page_text(text: str) -> dict:
    """Interpret the visible result after visiting the campaign gift page."""
    value = str(text or "").strip()
    if any(hint in value for hint in ALREADY_RECEIVED_HINTS):
        return {"state": "already", "success": True, "message": value}
    if any(hint in value for hint in ("领取成功", "领取完成", "已领取")):
        return {"state": "received", "success": True, "message": value}
    return {"state": "error", "success": False, "message": value or "每月礼包页面未确认领取成功"}
