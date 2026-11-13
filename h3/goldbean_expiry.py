"""Keep only the expiry fields from the existing integral overview response."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation


SHANGHAI = timezone(timedelta(hours=8))
TARGET_EXPIRY_DATE = date(2026, 12, 31)


def nonnegative_number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number < 0:
        return None
    result = float(number)
    return result if result != float("inf") else None


def expiry_date(value):
    try:
        text = str(value or "").strip()
        if len(text) == 10:
            return date.fromisoformat(text)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(SHANGHAI)
        return parsed.date()
    except (ValueError, TypeError):
        return None


def empty_points_expiry():
    return {"fetch_success": False, "expire_date": "", "expiring_points": None, "fetched_at": ""}


def normalize_points_expiry(value):
    value = value if isinstance(value, dict) else {}
    expires = expiry_date(value.get("expire_date"))
    amount = nonnegative_number(value.get("expiring_points"))
    if value.get("fetch_success") is not True or expires is None or amount is None:
        return empty_points_expiry()
    try:
        fetched_at = datetime.fromisoformat(str(value.get("fetched_at") or "").replace("Z", "+00:00"))
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=SHANGHAI)
        timestamp = fetched_at.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        timestamp = ""
    return {"fetch_success": True, "expire_date": expires.isoformat(),
            "expiring_points": amount, "fetched_at": timestamp}


def parse_points_expiry(response):
    if not isinstance(response, dict) or response.get("success") is False:
        return empty_points_expiry()
    if response.get("success") is not True and str(response.get("code")) != "200":
        return empty_points_expiry()
    data = response.get("data")
    if not isinstance(data, dict):
        return empty_points_expiry()
    return normalize_points_expiry({
        "fetch_success": True,
        "expire_date": data.get("expireTime"),
        "expiring_points": data.get("deadlineExpireNum"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })


def merge_points_expiry(*values):
    normalized = [normalize_points_expiry(value) for value in values]
    valid = [value for value in normalized if value["fetch_success"]]
    # A retry may contain a newer count even when the selected sign result is older.
    return max(valid, key=lambda value: value["fetched_at"]) if valid else empty_points_expiry()


def points_expiring_in_2026(value):
    value = normalize_points_expiry(value)
    if not value["fetch_success"]:
        return None
    expires = expiry_date(value["expire_date"])
    if expires == TARGET_EXPIRY_DATE:
        return value["expiring_points"]
    # A later nearest expiry means none are due in 2026. An earlier date does
    # not tell us the amount due at the end of 2026, so leave it unavailable.
    return 0.0 if expires > TARGET_EXPIRY_DATE else None
