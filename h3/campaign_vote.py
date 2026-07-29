import os
import re
from datetime import date
from urllib.parse import urlparse


CAMPAIGN_URL = (os.getenv("VOTE_CAMPAIGN_URL") or "").strip()
VOTE_API_BASE = (os.getenv("VOTE_API_BASE") or "").strip().rstrip("/")
VOTE_USER_INFO_PATH = "/api/integral/user/getUserInfo"
VOTE_CONFIG_PATH = "/api/integral/member/day/activity/ns/selectVoteConfig"
VOTE_SUBMIT_PATH = "/api/integral/member/day/activity/vote"
VOTE_ACTIVITY_ACCESS_ID = os.getenv("VOTE_ACTIVITY_ACCESS_ID", "fc7534debba644c5a0d26af52651d16f")
VOTE_PRODUCT_SKU = os.getenv("VOTE_PRODUCT_SKU", "SKUJY5")
VOTE_PRODUCT_NAME = os.getenv("VOTE_PRODUCT_NAME", "京东京造户外露营车 石墨黑")
VOTE_START_DATE = os.getenv("VOTE_START_DATE", "2026-07-29")
VOTE_END_DATE = os.getenv("VOTE_END_DATE", "2026-07-31")


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
    return ""


def truthy(value) -> bool:
    if value is True:
        return True
    if value is False or value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def date_part(value="") -> str:
    match = re.search(r"\d{4}-\d{2}-\d{2}", str(value or ""))
    return match.group(0) if match else ""


def is_vote_date(value, start_date=VOTE_START_DATE, end_date=VOTE_END_DATE) -> bool:
    current = date_part(value)
    try:
        return date.fromisoformat(start_date) <= date.fromisoformat(current) <= date.fromisoformat(end_date)
    except (TypeError, ValueError):
        return False


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
