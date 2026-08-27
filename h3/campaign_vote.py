import os
import re
from datetime import date
from urllib.parse import urlparse


CAMPAIGN_URL = (os.getenv("VOTE_CAMPAIGN_URL") or "").strip()
VOTE_API_BASE = (os.getenv("VOTE_API_BASE") or "").strip().rstrip("/")
VOTE_CAMPAIGN_PATH = "/portal/brand-campaign"
VOTE_USER_INFO_PATH = "/api/integral/user/getUserInfo"
VOTE_CONFIG_PATH = "/api/integral/member/day/activity/ns/selectVoteConfig"
VOTE_SUBMIT_PATH = "/api/integral/member/day/activity/vote"
ACTIVITY_CONFIG_PATH = "/api/integral/member/day/activity/ns/selectActivityConfigDetail"
ACTIVITY_WINNING_PATH = "/api/integral/member/day/activity/selectMyWinning"
VOTE_ACTIVITY_ACCESS_ID = os.getenv("VOTE_ACTIVITY_ACCESS_ID", "bf69c3403f094a52a787bfae528da7ea")
VOTE_PRODUCT_SKU = os.getenv("VOTE_PRODUCT_SKU", "SKUJM7")
VOTE_PRODUCT_NAME = os.getenv("VOTE_PRODUCT_NAME", "当妮香氛洗衣液 1.9kg*3瓶")
VOTE_START_DATE = os.getenv("VOTE_START_DATE", "2026-08-11")
VOTE_END_DATE = os.getenv("VOTE_END_DATE", "2026-08-31")


def activity_config_payload(access_id=VOTE_ACTIVITY_ACCESS_ID) -> dict:
    value = str(access_id or "").strip()
    return {"activityAccessId": value} if value else {}


def vote_environment_error(campaign_url=CAMPAIGN_URL, api_base=VOTE_API_BASE) -> str:
    missing = []
    if not str(campaign_url or "").strip():
        missing.append("VOTE_CAMPAIGN_URL")
    if not str(api_base or "").strip():
        missing.append("VOTE_API_BASE")
    if missing:
        return f"缺少投票环境变量：{', '.join(missing)}"

    for name, value in (
        ("VOTE_CAMPAIGN_URL", campaign_url),
        ("VOTE_API_BASE", api_base),
    ):
        parsed = urlparse(str(value).strip())
        if parsed.scheme != "https" or not parsed.netloc:
            return f"投票环境变量格式无效：{name} 必须是 HTTPS 地址"
    campaign_path = urlparse(str(campaign_url).strip()).path.rstrip("/") or "/"
    if campaign_path != VOTE_CAMPAIGN_PATH:
        return f"投票环境变量格式无效：VOTE_CAMPAIGN_URL 路径必须是 {VOTE_CAMPAIGN_PATH}"
    return ""


def truthy(value) -> bool:
    if value is True:
        return True
    if value is False or value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def safe_int(value, default=None):
    try:
        return int(float(str(value).strip()))
    except Exception:
        return default


def _response_rows(value) -> list[dict]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not isinstance(value, dict):
        return []
    for key in ("records", "list", "rows", "items", "dataList", "root"):
        candidate = value.get(key)
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
    for key in ("data", "body", "result", "page"):
        rows = _response_rows(value.get(key))
        if rows:
            return rows
    return []


def parse_lottery_winning_response(response) -> list[dict]:
    rows = []
    for item in _response_rows(response):
        title = str(
            item.get("prizeTitle")
            or item.get("prizeName")
            or item.get("awardName")
            or item.get("goodsName")
            or item.get("productName")
            or ""
        ).strip()
        if not title:
            continue
        receive_status = safe_int(item.get("receiveStatus"), None)
        claimed = truthy(item.get("claimed")) or receive_status in {2, 3, 6}
        status_text = str(item.get("statusText") or item.get("receiveStatusText") or "").strip()
        rows.append(
            {
                "title": title,
                "claimed": claimed,
                "status_text": status_text or ("已经领取" if claimed else "未领取"),
                "expiry_date": str(item.get("expiryDate") or item.get("expireTime") or "").strip(),
                "biz_order_code": str(item.get("bizOrderCode") or "").strip(),
                "receive_status": receive_status,
            }
        )
    return rows


def date_part(value="") -> str:
    match = re.search(r"\d{4}-\d{2}-\d{2}", str(value or ""))
    return match.group(0) if match else ""


def is_vote_date(value, start_date=VOTE_START_DATE, end_date=VOTE_END_DATE) -> bool:
    current = date_part(value)
    try:
        return date.fromisoformat(start_date) <= date.fromisoformat(current) <= date.fromisoformat(end_date)
    except (TypeError, ValueError):
        return False


def can_vote_after_sign(
    sign_success,
    *,
    sign_step_completed=False,
    sign_skipped=False,
    data_only_retry=False,
    previous_sign_success=False,
) -> bool:
    return (
        truthy(sign_success)
        or truthy(sign_step_completed)
        or truthy(sign_skipped)
        or (truthy(data_only_retry) and truthy(previous_sign_success))
    )


def campaign_session_ready(response) -> bool:
    if not isinstance(response, dict):
        return False
    body = response.get("body")
    return str(response.get("code") or "") == "200" and isinstance(body, dict) and bool(body.get("customerCode"))


def inspect_vote_config(response, target_sku=VOTE_PRODUCT_SKU, expected_name=VOTE_PRODUCT_NAME) -> dict:
    if not isinstance(response, dict) or response.get("success") is not True:
        message = response.get("message") if isinstance(response, dict) else ""
        return {"state": "error", "message": str(message or "投票配置请求失败")}

    data = response.get("data")
    if not isinstance(data, dict):
        return {"state": "error", "message": "投票配置响应缺少 data"}

    products = data.get("voteProductConfigList")
    if not isinstance(products, list):
        return {"state": "error", "message": "投票配置响应缺少商品列表"}

    target = next(
        (item for item in products if isinstance(item, dict) and str(item.get("productSku") or "") == target_sku),
        None,
    )
    if not target:
        return {"state": "error", "message": f"投票配置中未找到目标 SKU {target_sku}"}

    product_name = str(target.get("productName") or "").strip()
    if expected_name and product_name != expected_name:
        return {
            "state": "error",
            "message": f"目标 SKU 商品名不匹配：期望 {expected_name}，实际 {product_name or '空'}",
        }

    my_sku = str(data.get("myVotedProductSku") or "").strip()
    result = {
        "activity_status": data.get("activityStatus"),
        "can_vote": truthy(target.get("canVote")),
        "my_sku": my_sku,
        "product_name": product_name,
        "target_sku": target_sku,
    }
    if my_sku and my_sku != target_sku:
        result.update({"state": "conflict", "message": f"本期已锁定其他商品 {my_sku}"})
    elif not result["can_vote"] and my_sku == target_sku:
        result.update({"state": "already", "message": f"今日已投票：{product_name}"})
    elif not result["can_vote"]:
        result.update({"state": "unavailable", "message": f"目标商品当前不可投：{product_name}"})
    else:
        result.update({"state": "ready", "message": f"目标商品可以投票：{product_name}"})
    return result
