import re


MONTHLY_GIFT_DAY = 30
MONTHLY_GIFT_PAGE_PATH = "/pages/coupon-page/index?id=43"
# Legacy names are retained so existing result fields and imports remain compatible.
LISTING_GIFT_DATES = set()
LISTING_GIFT_PATH = MONTHLY_GIFT_PAGE_PATH
MONTHLY_GIFT_GROUP_PREFIX = "new"
ALREADY_RECEIVED_HINTS = ("已领取", "已经领取", "重复领取", "领取过", "不可重复")


def date_part(value="") -> str:
    match = re.search(r"\d{4}-\d{2}-\d{2}", str(value or ""))
    return match.group(0) if match else ""


def is_listing_gift_date(value) -> bool:
    match = re.search(r"\d{4}-\d{2}-(\d{2})", str(value or ""))
    return bool(match and int(match.group(1)) == MONTHLY_GIFT_DAY)


def is_monthly_gift_group(group_code: str) -> bool:
    return str(group_code or "").strip().lower().startswith(MONTHLY_GIFT_GROUP_PREFIX)


def should_claim_listing_gift(value, group_code: str = "") -> bool:
    return is_listing_gift_date(value) and is_monthly_gift_group(group_code)


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
    if any(hint in message for hint in ALREADY_RECEIVED_HINTS):
        return {"state": "already", "success": True, "message": message or "今日已领取每月礼包"}
    if not isinstance(response, dict) or response.get("success") is not True:
        return {"state": "error", "success": False, "message": message or "礼包接口请求失败"}
    data = response.get("data")
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
    """Interpret the visible result after visiting the monthly gift page."""
    value = str(text or "").strip()
    if any(hint in value for hint in ALREADY_RECEIVED_HINTS):
        return {"state": "already", "success": True, "message": value}
    if any(hint in value for hint in ("领取成功", "领取完成", "已领取")):
        return {"state": "received", "success": True, "message": value}
    return {"state": "error", "success": False, "message": value or "每月礼包页面未确认领取成功"}
