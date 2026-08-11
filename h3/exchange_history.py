from datetime import datetime, timedelta, timezone


EXCHANGE_RECORD_START_DATE = "2026-08-01"
EXCHANGE_STATUS_LABELS = {
    1: "已兑换",
    2: "已发货",
    3: "已兑换待发货",
    4: "已冻结",
    5: "已退回",
    6: "已确认收货",
}


def safe_int(value, default=0):
    try:
        return int(str(value).strip())
    except Exception:
        return default


def safe_float(value, default=0.0):
    try:
        return float(str(value).strip())
    except Exception:
        return default


def parse_datetime_value(value):
    if isinstance(value, (int, float)):
        try:
            timestamp = float(value)
            if timestamp > 100000000000:
                timestamp /= 1000
            return datetime.fromtimestamp(timestamp, timezone.utc)
        except Exception:
            return None
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19], fmt)
        except Exception:
            continue
    return None


def local_datetime(value):
    dt = parse_datetime_value(value)
    if dt is None:
        return None
    local_tz = timezone(timedelta(hours=8))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=local_tz)
    return dt.astimezone(local_tz)


def exchange_status_text(item: dict) -> str:
    integral_type = safe_int(item.get("integralChangeType"), None)
    if integral_type in {4, 5, 7, 9}:
        state = safe_int(item.get("exchangeStates", item.get("exchangeState")), None)
        return EXCHANGE_STATUS_LABELS.get(
            state,
            str(item.get("exchangeStateText") or item.get("statusText") or f"未知状态({state})").strip(),
        )
    if integral_type in {2, 6}:
        return "已发到账户"
    return "已兑换"


def normalize_exchange_records(records: list[dict], start_date=EXCHANGE_RECORD_START_DATE) -> list[dict]:
    cutoff = parse_datetime_value(start_date)
    cutoff_date = cutoff.date() if cutoff else datetime(2026, 8, 1).date()
    normalized = []
    for item in records or []:
        if not isinstance(item, dict):
            continue
        created_at = local_datetime(item.get("createTime") or item.get("createdAt") or item.get("createDate"))
        if created_at is None or created_at.date() < cutoff_date:
            continue
        title = str(item.get("goodsName") or item.get("skuTitle") or item.get("prizeTitle") or "").strip()
        if not title:
            continue
        normalized.append(
            {
                "title": title,
                "status": safe_int(item.get("exchangeStates", item.get("exchangeState")), None),
                "status_text": exchange_status_text(item),
                "quantity": max(1, safe_int(item.get("exchangeNum"), 1)),
                "points": safe_float(item.get("integralChangeNum"), 0.0),
                "created_at": created_at.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
    normalized.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return normalized
