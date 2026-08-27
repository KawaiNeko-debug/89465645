import os
import sys
import time
import random
import json
import requests
import smtplib
import threading
import re
from copy import deepcopy
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeoutError
from fake_useragent import UserAgent

try:
    from campaign_vote import (
        ACTIVITY_CONFIG_PATH,
        ACTIVITY_WINNING_PATH,
        CAMPAIGN_URL,
        VOTE_ACTIVITY_ACCESS_ID,
        VOTE_API_BASE,
        VOTE_CAMPAIGN_PATH,
        VOTE_CONFIG_PATH,
        VOTE_END_DATE,
        VOTE_PRODUCT_NAME,
        VOTE_PRODUCT_SKU,
        VOTE_START_DATE,
        VOTE_SUBMIT_PATH,
        VOTE_USER_INFO_PATH,
        activity_config_payload,
        campaign_session_ready,
        can_vote_after_sign,
        inspect_vote_config,
        is_vote_date,
        parse_lottery_winning_response,
        vote_environment_error,
    )
except ImportError:
    from h3.campaign_vote import (
        ACTIVITY_CONFIG_PATH,
        ACTIVITY_WINNING_PATH,
        CAMPAIGN_URL,
        VOTE_ACTIVITY_ACCESS_ID,
        VOTE_API_BASE,
        VOTE_CAMPAIGN_PATH,
        VOTE_CONFIG_PATH,
        VOTE_END_DATE,
        VOTE_PRODUCT_NAME,
        VOTE_PRODUCT_SKU,
        VOTE_START_DATE,
        VOTE_SUBMIT_PATH,
        VOTE_USER_INFO_PATH,
        activity_config_payload,
        campaign_session_ready,
        can_vote_after_sign,
        inspect_vote_config,
        is_vote_date,
        parse_lottery_winning_response,
        vote_environment_error,
    )

try:
    from account_data import AccountDataCollector, empty_account_data
    from exchange_history import normalize_exchange_records
    from feature_flags import ACCOUNT_DATA_ENABLED, LISTING_GIFT_ENABLED, SECKILL_ENABLED, VOTE_ENABLED
    from listing_gift import LISTING_GIFT_DATES, LISTING_GIFT_PATH, inspect_listing_gift_response, is_listing_gift_date
    from retry_components import COMPONENTS, component_status, needs_retry, retry_components
except ImportError:
    from h3.account_data import AccountDataCollector, empty_account_data
    from h3.exchange_history import normalize_exchange_records
    from h3.feature_flags import ACCOUNT_DATA_ENABLED, LISTING_GIFT_ENABLED, SECKILL_ENABLED, VOTE_ENABLED
    from h3.listing_gift import LISTING_GIFT_DATES, LISTING_GIFT_PATH, inspect_listing_gift_response, is_listing_gift_date
    from h3.retry_components import COMPONENTS, component_status, needs_retry, retry_components

# 统一东八区时间
os.environ.setdefault("TZ", "Asia/Shanghai")
try:
    time.tzset()
except Exception:
    pass

# ==============================================================================
# 从环境变量读取所有配置（必须设置）
# ==============================================================================
BASE_URL = os.getenv('BASE_URL')
PASSPORT_URL = os.getenv('PASSPORT_URL')
REFERER = os.getenv('REFERER')
API_SIGN_PATH = os.getenv('API_SIGN_PATH', '/api/activity/sign/signIn?source=4')
SCRIPT_VERSION = "2026-08-27-activity-config-v2"
RISK_CONTROL_MESSAGE = (os.getenv("RISK_CONTROL_MESSAGE") or "签到失败，疑似违反签到规则").strip()
CAMPAIGN_DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)
DEFAULT_SECKILL_CATEGORY_ACCESS_IDS = ["805341c7b7c242c6a8deb5c8789336b2"]
DEFAULT_LOTTERY_ACTIVITY_CODE = "LAKU"
UNCLAIMED_EXCHANGE_STATES = {1}
CLAIMED_EXCHANGE_STATES = {2, 6}
LOTTERY_CLAIMED_EXCHANGE_STATES = {2, 3, 6}
VOTE_RESULT_FIELDS = (
    'vote_required',
    'vote_success',
    'vote_attempted',
    'vote_status',
    'vote_time',
    'vote_product_sku',
    'vote_product_name',
    'vote_detail',
)
ACCOUNT_DATA_RESULT_FIELDS = (
    'account_data_required',
    'account_data_fetch_success',
    'account_data',
)
LISTING_GIFT_RESULT_FIELDS = (
    'listing_gift_required',
    'listing_gift_success',
    'listing_gift_attempted',
    'listing_gift_status',
    'listing_gift_time',
    'listing_gift_detail',
)
EXECUTION_RESULT_FIELDS = (
    'group_code',
    'account_category',
    'execution_mode',
    'sign_skipped',
)

HEADER_ACCESS_TOKEN_FALLBACKS = [
    k.strip().lower()
    for k in os.getenv('HEADER_ACCESS_TOKEN_FALLBACKS', '').split(',')
    if k.strip()
]
SLIDER_ID = os.getenv('SLIDER_ID')
WRAPPER_ID = os.getenv('WRAPPER_ID')

HEADER_CLIENT_TYPE = os.getenv('HEADER_CLIENT_TYPE')
HEADER_ACCESS_TOKEN = os.getenv('HEADER_ACCESS_TOKEN')
HEADER_SECRET_KEY = os.getenv('HEADER_SECRET_KEY', 'secretkey')

TOKEN_KEY = os.getenv('TOKEN_KEY')
TOKEN_ALTERNATIVE_KEYS = [k.strip() for k in os.getenv('TOKEN_ALTERNATIVE_KEYS', '').split(',') if k.strip()]

ACTIVE_STATUS_PATH = "/api/sms/front/internal-message/active-status"
LOGIN_API_PATH = "/api/cas/login/mobile/with-password"
PASSWORD_ERROR_HINTS = ["账号或密码不正确", "请重新输入", "密码错误"]

# 首页元素（用于判断是否进入首页）
HOME_SELECTOR = 'div.uni-tabbar__label:has-text("首页")'

# 签到相关接口
SIGN_CONFIG_PATH = "/api/activity/sign/getCurrentUserSignInConfig"
RECEIVE_VOUCHER_PATH = "/api/activity/sign/receiveVoucher"
CUSTOMER_INTEGRAL_PATH = "/api/activity/front/getCustomerIntegral"
SECKILL_RECORDS_PATH = "/api/activity/seckill/selectSeckillRecords"
LOTTERY_WINS_PATH = "/api/cgi/operationService/front/lottery/queryWins"
VOUCHER_CHANGE_RECORD_PATH = "/api/activity/front/selectIntegralVoucherChangeRecord"
BRAND_ACTIVITY_CONFIG_PATH = "/api/activity/brand/activity/ns/selectActivityConfig"
SECKILL_GOODS_PATH = "/api/activity/seckill/ns/getSeckillGoods"
RECORD_POST_PAYLOADS = [
    {"pageNum": 1, "pageSize": 50, "subAccountIds": []},
    {"pageNo": 1, "pageSize": 50, "subAccountIds": []},
]

# 检查必要变量
required_vars = [
    BASE_URL, PASSPORT_URL, REFERER,
    SLIDER_ID, WRAPPER_ID,
    HEADER_CLIENT_TYPE, HEADER_ACCESS_TOKEN, TOKEN_KEY
]
if not all(required_vars):
    print("? 缺少必要环境变量，请检查以下变量是否全部设置：")
    print("BASE_URL, PASSPORT_URL, REFERER, SLIDER_ID, WRAPPER_ID, HEADER_CLIENT_TYPE, HEADER_ACCESS_TOKEN, TOKEN_KEY")
    sys.exit(1)

parsed_base = urlparse(BASE_URL)
HOST = parsed_base.netloc
URL_PATTERN = f"**/{HOST}/**"

# ==============================================================================
# 小工具函数
# ==============================================================================
_UUID_RE = re.compile(r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b')
_PUBLIC_IP_CACHE = {"loaded": False, "value": ""}

def truthy(v) -> bool:
    if v is True:
        return True
    if v is False or v is None:
        return False
    if isinstance(v, (int, float)):
        return v != 0
    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "y", "on")


def execution_context() -> dict:
    group_code = (os.getenv('GROUP_CODE') or os.getenv('GROUP_NAME') or '').strip().lower()
    if group_code.startswith('old'):
        default_category = '老号全干组'
    elif group_code.startswith('new'):
        default_category = '新号全干组'
    elif group_code.startswith(('ll', 'zh')):
        default_category = '同行不签到组'
    elif group_code == 'test':
        default_category = '测试组'
    else:
        default_category = ''
    skip_sign = truthy(os.getenv('SKIP_SIGN')) or group_code.startswith(('ll', 'zh'))
    return {
        'group_code': group_code,
        'account_category': (os.getenv('ACCOUNT_CATEGORY') or default_category).strip(),
        'execution_mode': (os.getenv('EXECUTION_MODE') or ('skip_sign' if skip_sign else 'full')).strip(),
        'sign_skipped': skip_sign,
    }


def initial_vote_status(task_date: str) -> str:
    if not VOTE_ENABLED:
        return '投票功能已关闭'
    return '待执行' if is_vote_date(task_date) else '非投票日期'

def safe_int(v, default=0) -> int:
    try:
        return int(str(v).strip())
    except Exception:
        return default

def safe_float(v, default=0.0) -> float:
    try:
        return float(str(v).strip())
    except Exception:
        return default

def truncate_text(s: str, limit: int = 1200) -> str:
    if s is None:
        return ""
    s = str(s)
    if len(s) <= limit:
        return s
    return s[:limit] + f"...(truncated, len={len(s)})"

def redact_sensitive(s: str) -> str:
    if not s:
        return ""
    text = str(s)
    for value in sorted(_SENSITIVE_LOG_VALUES, key=len, reverse=True):
        if value:
            text = text.replace(value, "[REDACTED]")
    return _UUID_RE.sub(lambda m: m.group(0)[:8] + "-****-****-****-" + m.group(0)[-12:], text)

def is_unclaimed_reward_error(data: dict) -> bool:
    if not isinstance(data, dict):
        return False
    msg = str(data.get("message") or "")
    return ("存在签到未领取" in msg and "请先领取" in msg) or ("未领取" in msg and "先领取" in msg)

def is_duplicate_claim_error(data: dict) -> bool:
    if not isinstance(data, dict):
        return False
    msg = str(data.get("message") or "")
    return "重复领取金豆" in msg

def is_nonzero_reward_value(v) -> bool:
    if v is None:
        return False
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        t = v.strip().lower()
        if t in ("", "0", "0.0", "null", "none", "false"):
            return False
        try:
            return float(t) != 0
        except Exception:
            return True
    return True

def get_sign_gain_num(data: dict):
    if not isinstance(data, dict):
        return None
    payload = data.get("data")
    if isinstance(payload, dict):
        return payload.get("gainNum")
    return payload

def extract_integral_voucher(data):
    if not isinstance(data, dict):
        return None
    payload = data.get("data")
    if isinstance(payload, dict) and payload.get("integralVoucher") is not None:
        return safe_float(payload.get("integralVoucher"), None)
    for value in find_values_by_key(data, {"integralVoucher"}):
        points = safe_float(value, None)
        if points is not None:
            return points
    return None

def extract_message(value):
    if isinstance(value, dict):
        for key in ("message", "msg", "errorMessage", "error", "detail"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        for nested in value.values():
            candidate = extract_message(nested)
            if candidate:
                return candidate
        return ""
    if isinstance(value, list):
        for item in value:
            candidate = extract_message(item)
            if candidate:
                return candidate
        return ""
    if isinstance(value, str):
        return value.strip()
    return ""

def build_detail_reason(value, default=""):
    msg = extract_message(value)
    if msg:
        return redact_sensitive(truncate_text(msg, 800))
    if value is None:
        return default
    try:
        dumped = json.dumps(value, ensure_ascii=False)
    except Exception:
        dumped = str(value)
    dumped = redact_sensitive(truncate_text(dumped, 800))
    return dumped or default

def has_risk_control_text(*values) -> bool:
    if not RISK_CONTROL_MESSAGE:
        return False
    return any(RISK_CONTROL_MESSAGE in str(value or "") for value in values)

def is_risk_control_response(data) -> bool:
    return has_risk_control_text(build_detail_reason(data))

def is_risk_control_result(res: dict) -> bool:
    if not isinstance(res, dict):
        return False
    return truthy(res.get("risk_controlled")) or has_risk_control_text(
        res.get("detail_reason"),
        res.get("sign_status"),
    )

def is_data_only_retry_mode() -> bool:
    return truthy(os.getenv("DATA_ONLY_RETRY"))

def retry_previous_sign_success() -> bool:
    return truthy(os.getenv("PREVIOUS_SIGN_SUCCESS"))

def retry_previous_risk_controlled() -> bool:
    return truthy(os.getenv("PREVIOUS_RISK_CONTROLLED"))

def retry_previous_final_points() -> float:
    return safe_float(os.getenv("PREVIOUS_FINAL_POINTS"), 0.0)


def load_previous_result(path=None) -> dict:
    target = str(path or os.getenv("PREVIOUS_RESULT_PATH") or "").strip()
    if not target:
        return {}
    try:
        payload = json.loads(Path(target).read_text(encoding="utf-8"))
        rows = payload.get("results") if isinstance(payload, dict) else None
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return rows[0]
    except (OSError, ValueError, TypeError):
        pass
    return {}


def requested_components(previous_result=None) -> set[str]:
    explicit = {
        item.strip()
        for item in str(os.getenv("RETRY_COMPONENTS") or "").split(",")
        if item.strip() in COMPONENTS
    }
    if explicit:
        if previous_result:
            return explicit.intersection(retry_components(previous_result))
        return explicit
    if previous_result:
        return set(retry_components(previous_result))
    return set(COMPONENTS)

def parse_banned_accounts(raw=None) -> set[str]:
    raw = os.getenv("BANNED_ACCOUNTS", "") if raw is None else raw
    if raw is None:
        return set()
    if isinstance(raw, (list, tuple, set)):
        return {str(item).strip() for item in raw if str(item).strip()}

    text = str(raw).strip()
    if not text:
        return set()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, (list, tuple, set)):
            return {str(item).strip() for item in parsed if str(item).strip()}
        if isinstance(parsed, dict):
            return {str(key).strip() for key, enabled in parsed.items() if truthy(enabled) and str(key).strip()}
    except Exception:
        pass

    return {item.strip() for item in re.split(r"[,;\n\r]+", text) if item.strip()}

def is_banned_account(username: str) -> bool:
    banned = parse_banned_accounts()
    username = str(username or "").strip()
    return bool(username and username in banned)

def current_date_text() -> str:
    return datetime.now().strftime("%Y-%m-%d")

def normalize_task_start_date(value="") -> str:
    raw = str(value or os.getenv("SIGN_TASK_START_DATE") or "").strip()
    if raw:
        match = re.search(r"\d{4}-\d{2}-\d{2}", raw)
        if match:
            return match.group(0)
    return current_date_text()

def date_part(value="") -> str:
    match = re.search(r"\d{4}-\d{2}-\d{2}", str(value or ""))
    return match.group(0) if match else ""

def has_next_day_success(task_start_date: str, sign_time: str) -> bool:
    start = date_part(task_start_date)
    signed = date_part(sign_time)
    return bool(start and signed and signed > start)

def extract_data_list(payload) -> list:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("data", "list", "records", "rows", "result"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = extract_data_list(value)
            if nested:
                return nested
    return []

def parse_string_list(raw=None) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        return [str(item).strip() for item in raw if str(item).strip()]
    text = str(raw).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, (list, tuple, set)):
            return [str(item).strip() for item in parsed if str(item).strip()]
    except Exception:
        pass
    return [item.strip() for item in re.split(r"[,;\n\r]+", text) if item.strip()]

def find_values_by_key(payload, target_keys: set[str]) -> list:
    found = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in target_keys and value not in (None, ""):
                found.append(value)
            found.extend(find_values_by_key(value, target_keys))
    elif isinstance(payload, list):
        for item in payload:
            found.extend(find_values_by_key(item, target_keys))
    return found

def unique_text_values(values) -> list[str]:
    result = []
    seen = set()
    for value in values:
        text = str(value).strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result

def parse_datetime_value(value):
    if isinstance(value, (int, float)):
        try:
            timestamp = float(value)
            if timestamp > 100000000000:
                timestamp = timestamp / 1000
            return datetime.fromtimestamp(timestamp)
        except Exception:
            return None
    text = str(value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{10,13}", text):
        try:
            timestamp = float(text)
            if len(text) >= 13:
                timestamp = timestamp / 1000
            return datetime.fromtimestamp(timestamp)
        except Exception:
            pass
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

def next_year_last_day(value=None) -> str:
    dt = parse_datetime_value(value) or datetime.now()
    return f"{dt.year + 1}-12-31"

def exchange_claimed_state(exchange_state, customer_recipient_id=""):
    state = safe_int(exchange_state, None)
    if state in UNCLAIMED_EXCHANGE_STATES:
        return False
    if state in CLAIMED_EXCHANGE_STATES:
        return True
    if str(customer_recipient_id or "").strip():
        return True
    return None

def lottery_exchange_claimed_state(exchange_state, customer_recipient_id=""):
    state = safe_int(exchange_state, None)
    if state in UNCLAIMED_EXCHANGE_STATES:
        return False
    if state in LOTTERY_CLAIMED_EXCHANGE_STATES:
        return True
    if str(customer_recipient_id or "").strip():
        return True
    return None

def expiry_status_text(expiry_date: str, unclaimed_text="未领取") -> str:
    expiry_date = str(expiry_date or "").strip()
    if not expiry_date:
        return unclaimed_text
    expiry_dt = parse_datetime_value(expiry_date)
    local_today = datetime.now(timezone(timedelta(hours=8))).date()
    if expiry_dt and local_today > expiry_dt.date():
        return f"已过期 {expiry_date}"
    return f"{unclaimed_text} {expiry_date}"

def build_expiry_lookup(change_records: list[dict]) -> dict[str, dict]:
    lookup = {}
    for item in change_records:
        if not isinstance(item, dict):
            continue
        goods_name = str(item.get("goodsName") or item.get("skuTitle") or item.get("prizeTitle") or "").strip()
        created_at = parse_datetime_value(item.get("createTime") or item.get("createdAt") or item.get("createDate"))
        if not goods_name or not created_at:
            continue
        exchange_state = item.get("exchangeStates", item.get("exchangeState"))
        exchange_goods_access_id = str(item.get("exchangeGoodsAccessId") or "").strip()
        info = {
            "goods_name": goods_name,
            "expiry_date": (created_at + timedelta(days=7)).strftime("%Y-%m-%d"),
            "exchange_state": safe_int(exchange_state, None),
            "customer_recipient_id": str(item.get("customerRecipientId") or "").strip(),
            "exchange_goods_access_id": exchange_goods_access_id,
            "claimed": exchange_claimed_state(exchange_state, item.get("customerRecipientId")),
        }
        if exchange_goods_access_id:
            lookup[f"id:{exchange_goods_access_id}"] = info
        lookup[f"name:{goods_name}"] = info
    return lookup

def find_exchange_info(title: str, expiry_lookup: dict, exchange_goods_access_id="") -> dict:
    title = str(title or "").strip()
    exchange_goods_access_id = str(exchange_goods_access_id or "").strip()
    if exchange_goods_access_id:
        info = expiry_lookup.get(f"id:{exchange_goods_access_id}")
        if isinstance(info, dict):
            return info
    if not title:
        return {}
    for lookup_key, info in expiry_lookup.items():
        goods_name = str(info.get("goods_name") or "").strip() if isinstance(info, dict) else str(lookup_key)
        if goods_name and (goods_name == title or goods_name in title or title in goods_name):
            if isinstance(info, dict):
                return info
            return {"expiry_date": str(info or "").strip(), "claimed": None, "exchange_state": None}
    return {}

def find_expiry_date(title: str, expiry_lookup: dict) -> str:
    info = find_exchange_info(title, expiry_lookup)
    if info:
        return str(info.get("expiry_date") or "").strip()
    return ""

def apply_expiry_dates(records: list[dict], expiry_lookup: dict, lottery=False):
    for item in records:
        info = find_exchange_info(
            item.get("title"),
            expiry_lookup,
            item.get("biz_order_code") if lottery else "",
        )
        if not info:
            continue
        claimed = (
            lottery_exchange_claimed_state(
                info.get("exchange_state"),
                info.get("customer_recipient_id"),
            )
            if lottery else info.get("claimed")
        )
        item["exchange_state"] = info.get("exchange_state")
        if claimed is True:
            item["claimed"] = True
            item["expiry_date"] = ""
            item["status_text"] = "已经领取"
            continue
        if truthy(item.get("claimed")):
            continue
        expiry_date = str(info.get("expiry_date") or "").strip()
        if not expiry_date:
            continue
        item["expiry_date"] = expiry_date
        if lottery:
            item["claimed"] = False
            item["status_text"] = expiry_status_text(expiry_date)
        else:
            status_text = str(item.get("status_text") or "").strip() or "未领取"
            item["status_text"] = f"{status_text} {expiry_date}" if expiry_date not in status_text else status_text

def needs_expiry_lookup(records: list[dict]) -> bool:
    return any(
        isinstance(item, dict)
        and item.get("title")
        and not truthy(item.get("claimed"))
        and not item.get("expiry_date")
        for item in records
    )

def make_empty_extra_records() -> dict:
    return {"seckill": [], "lottery": [], "exchange": []}

def merge_activity_record_components(*values) -> dict:
    merged = make_empty_extra_records()
    for value in values:
        if not isinstance(value, dict):
            continue
        for key in merged:
            rows = value.get(key)
            if isinstance(rows, list) and len(rows) > len(merged[key]):
                merged[key] = rows
    return merged

def api_response_succeeded(data) -> bool:
    if not isinstance(data, dict):
        return False
    if data.get("success") is True:
        return True
    return data.get("success") is None and "data" in data

# ==============================================================================
# 移动端 UA 池（至少数千条）
# ==============================================================================
MOBILE_DEVICES = [
    "SM-G970F", "SM-G973F", "SM-G975F", "SM-G980F", "SM-G985F",
    "SM-G991B", "SM-G996B", "SM-S901B", "SM-S906B", "SM-S911B",
    "SM-S916B", "SM-S918B", "SM-A505F", "SM-A515F", "SM-A525F",
    "SM-A535F", "SM-A546B", "SM-A715F", "SM-A725F", "SM-A736B",
    "SM-F711B", "SM-F721B", "SM-F936B", "SM-F946B",
    "Pixel 4", "Pixel 4a", "Pixel 5", "Pixel 5a", "Pixel 6",
    "Pixel 6a", "Pixel 6 Pro", "Pixel 7", "Pixel 7a", "Pixel 7 Pro",
    "Pixel 8", "Pixel 8 Pro",
    "MI 9", "MI 10", "MI 11", "MI 12", "Mi 11T",
    "Redmi Note 10", "Redmi Note 11", "Redmi Note 12",
    "POCO F3", "POCO F4",
    "ONEPLUS A6013", "ONEPLUS A5000", "ONEPLUS A6003", "ONEPLUS A3003"
]

ANDROID_VERSIONS = ["8.0", "8.1", "9", "10", "11", "12", "13", "14"]

CHROME_VERSIONS = [
    "118.0.5993.80",
    "119.0.6045.134",
    "120.0.6099.224",
    "121.0.6167.164",
    "122.0.6261.105",
    "123.0.6312.120",
    "124.0.6367.207",
    "125.0.6422.147",
    "126.0.6478.122",
    "127.0.6533.103"
]

_FAKE_UA = None
try:
    _FAKE_UA = UserAgent(use_cache_server=False, verify_ssl=False)
except Exception:
    _FAKE_UA = None

def build_mobile_ua_pool():
    pool = []
    for device in MOBILE_DEVICES:
        for av in ANDROID_VERSIONS:
            for cv in CHROME_VERSIONS:
                ua = f"Mozilla/5.0 (Linux; Android {av}; {device}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{cv} Mobile Safari/537.36"
                pool.append(ua)

    if _FAKE_UA:
        seen = set(pool)
        for _ in range(200):
            try:
                candidate = _FAKE_UA.random
                if ("Mobile" in candidate or "Android" in candidate or "iPhone" in candidate) and candidate not in seen:
                    pool.append(candidate)
                    seen.add(candidate)
            except Exception:
                break

    random.shuffle(pool)
    return pool

MOBILE_UA_POOL = build_mobile_ua_pool()

def get_random_mobile_ua():
    if MOBILE_UA_POOL:
        return random.choice(MOBILE_UA_POOL)
    if _FAKE_UA:
        try:
            return _FAKE_UA.random
        except Exception:
            pass
    return "Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.6099.224 Mobile Safari/537.36"

# --- 全局日志变量 ---
in_summary = False
summary_logs = []
_SENSITIVE_LOG_VALUES = set()

def log(msg):
    safe_msg = redact_sensitive(str(msg))
    full_msg = f"[{datetime.now().strftime('%H:%M:%S')}] {safe_msg}"
    print(full_msg, flush=True)
    if in_summary:
        summary_logs.append(safe_msg)

def mask_account(account):
    if account is None:
        return ""
    s = str(account)
    visible = 5
    return "*" * max(0, len(s) - visible) + s[-visible:]

def current_time_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def get_public_ip() -> str:
    if _PUBLIC_IP_CACHE["loaded"]:
        return _PUBLIC_IP_CACHE["value"]

    candidates = (
        ("https://api.ipify.org?format=json", "json"),
        ("https://ifconfig.me/ip", "text"),
    )
    ip_value = ""
    for url, response_type in candidates:
        try:
            response = requests.get(url, timeout=8)
            if response.status_code != 200:
                continue
            if response_type == "json":
                ip_value = str((response.json() or {}).get("ip") or "").strip()
            else:
                ip_value = response.text.strip()
            if ip_value:
                break
        except Exception:
            continue

    _PUBLIC_IP_CACHE["loaded"] = True
    _PUBLIC_IP_CACHE["value"] = ip_value
    return ip_value

def finalize_result_metadata(result: dict):
    result["sign_time"] = str(result.get("sign_completed_at") or result.get("sign_time") or current_time_text()).strip()
    result["sign_ip"] = str(result.get("sign_ip") or get_public_ip()).strip()
    if result.get("sign_success") and not result.get("banned_account"):
        task_start_date = str(result.get("task_start_date") or "").strip()
        result["next_day_success"] = has_next_day_success(task_start_date, result["sign_time"])
    result["component_status"] = component_status(result)

def masked_label(result):
    if result.get('masked_username'):
        return result['masked_username']
    if result.get('username'):
        return mask_account(result['username'])
    return f"账号序号{result.get('account_index')}"

def with_retry(func, max_retries=5, delay=1):
    def wrapper(*args, **kwargs):
        for _ in range(max_retries):
            try:
                result = func(*args, **kwargs)
                if result is not None:
                    return result
                time.sleep(delay + random.uniform(0, 1))
            except Exception:
                time.sleep(delay + random.uniform(0, 1))
        return None
    return wrapper

def wait_token_from_requests(token_holder, timeout=8):
    start = time.time()
    while time.time() - start < timeout:
        token = token_holder.get('value')
        if token:
            return token
        time.sleep(0.2)
    return None

# ==============================================================================
# 滑块破解脚本（注入式，ID 从环境变量读取）
# ==============================================================================
def solve_slider_with_bezier(page: Page) -> bool:
    try:
        page.locator(f"#{SLIDER_ID}").wait_for(state="visible", timeout=10000)
        log("✅ 检测到滑块，准备注入破解脚本...")
    except Exception:
        log("🟢 未检测到滑块，跳过。")
        return True

    script = f"""
    (async function() {{
        const slider = document.getElementById('{SLIDER_ID}');
        const wrapper = document.getElementById('{WRAPPER_ID}');
        if (!slider || !wrapper) return false;

        wrapper.scrollIntoView({{behavior: 'instant', block: 'center'}});
        await new Promise(r => setTimeout(r, 300));

        function generateHumanPath(x1, y1, x2, y2) {{
            const points = [];
            const cx1 = x1 + (x2 - x1) * 0.3 + (Math.random() - 0.5) * 20;
            const cy1 = y1 + (Math.random() - 0.5) * 50;
            const cx2 = x1 + (x2 - x1) * 0.7 + (Math.random() - 0.5) * 20;
            const cy2 = y1 + (Math.random() - 0.5) * 50;
            const totalDuration = 800 + Math.random() * 700;
            const steps = 60 + Math.floor(Math.random() * 40);
            for (let i = 0; i <= steps; i++) {{
                const t = i / steps;
                const ease = t < 0.5 ? 2 * t * t : -1 + (4 - 2 * t) * t;
                const x = Math.pow(1 - ease, 3) * x1 +
                          3 * Math.pow(1 - ease, 2) * ease * cx1 +
                          3 * (1 - ease) * ease * ease * cx2 +
                          Math.pow(ease, 3) * x2;
                const y = Math.pow(1 - ease, 3) * y1 +
                          3 * Math.pow(1 - ease, 2) * ease * cy1 +
                          3 * (1 - ease) * ease * ease * cy2 +
                          Math.pow(ease, 3) * y2;
                points.push({{
                    x: x + (Math.random() - 0.5) * 2,
                    y: y + (Math.random() - 0.5) * 2,
                    t: Math.floor(totalDuration * t)
                }});
            }}
            return points;
        }}

        function triggerEvent(el, type, x, y) {{
            const mouseEvent = new MouseEvent(type, {{
                bubbles: true, cancelable: true, view: window,
                clientX: x, clientY: y, screenX: x, screenY: y,
                button: 0, buttons: 1
            }});
            el.dispatchEvent(mouseEvent);
            if (type.startsWith('mouse')) {{
                const pointerType = type.replace('mouse', 'pointer');
                const pointerEvent = new PointerEvent(pointerType, {{
                    bubbles: true, cancelable: true, view: window,
                    clientX: x, clientY: y, screenX: x, screenY: y,
                    button: 0, buttons: 1, pointerId: 1,
                    width: 1, height: 1, pressure: 0.5,
                    tiltX: 0, tiltY: 0, pointerType: 'mouse'
                }});
                el.dispatchEvent(pointerEvent);
            }}
        }}

        const sliderRect = slider.getBoundingClientRect();
        const wrapperRect = wrapper.getBoundingClientRect();
        const startX = sliderRect.left + sliderRect.width / 2;
        const startY = sliderRect.top + sliderRect.height / 2;
        const extraDistance = 15;
        const endX = wrapperRect.left + wrapperRect.width - (sliderRect.width / 2) + extraDistance;
        const endY = startY + (Math.random() - 0.5) * 5;

        const path = generateHumanPath(startX, startY, endX, endY);
        triggerEvent(slider, 'mousedown', startX, startY);
        let previousTime = 0;
        for (let point of path) {{
            const waitTime = point.t - previousTime;
            if (waitTime > 0) await new Promise(r => setTimeout(r, waitTime));
            triggerEvent(slider, 'mousemove', point.x, point.y);
            triggerEvent(document, 'mousemove', point.x, point.y);
            previousTime = point.t;
        }}
        await new Promise(r => setTimeout(r, 200 + Math.random() * 100));
        const last = path[path.length - 1];
        triggerEvent(slider, 'mouseup', last.x, last.y);
        triggerEvent(document, 'mouseup', last.x, last.y);
        return true;
    }})();
    """

    try:
        page.evaluate(script)
        log("✅ 滑块脚本执行完成")
    except Exception as e:
        log(f"❌ 滑块脚本异常: {e}")
        return False

    time.sleep(5)
    if page.locator(f"#{SLIDER_ID}").is_visible(timeout=2000):
        log("⚠️ 滑块仍然存在（5s检测）")
        time.sleep(5)
        if page.locator(f"#{SLIDER_ID}").is_visible(timeout=2000):
            log("❌ 滑块10秒后仍存在，进入重试阶段")
            return False
        log("✅ 10秒后滑块已消失，破解成功")
        return True

    log("✅ 滑块已消失，破解成功")
    return True

# ==============================================================================
# 提取 localStorage 中的 AccessToken（键名从环境变量读取）
# ==============================================================================
@with_retry
def extract_token_from_local_storage(page: Page):
    try:
        token = page.evaluate(f"() => window.localStorage.getItem('{TOKEN_KEY}')")
        if token:
            log("✅ 已提取到 token")
            return token
        for key in TOKEN_ALTERNATIVE_KEYS:
            token = page.evaluate(f"() => window.localStorage.getItem('{key}')")
            if token:
                log("✅ 已提取到 token")
                return token
    except Exception as e:
        log(f"❌ 提取 token 失败: {e}")
    return None

# ==============================================================================
# API 客户端（只用 GET；失败重试一次 GET）
# ==============================================================================
class ApiClient:
    def __init__(self, access_token, secretkey, account_index, page: Page, user_agent=None):
        self.base_url = BASE_URL
        self.user_agent = user_agent or get_random_mobile_ua()
        self.headers = {
            'user-agent': self.user_agent,
            HEADER_CLIENT_TYPE: 'WEB',
            'accept': 'application/json, text/plain, */*',
            HEADER_ACCESS_TOKEN: access_token,
            'Referer': REFERER,
        }
        if secretkey:
            self.headers[HEADER_SECRET_KEY] = secretkey

        self.account_index = account_index
        self.page = page

        self.initial_points = 0
        self.final_points = 0
        self.points_reward = 0

        self.sign_status = "未知"
        self.has_reward = False

        self.today_day = 0
        self.detail_reason = ""
        self.risk_controlled = False
        self.banned_account = False
        self.sign_completed_at = ""
        self.activity_records = make_empty_extra_records()
        self.brand_activity_config = None
        self.brand_activity_config_success = False
        self.activity_access_id = VOTE_ACTIVITY_ACCESS_ID
        self.points_fetch_success = False
        self.seckill_fetch_success = False
        self.lottery_fetch_success = False
        self.voucher_fetch_success = False
        self.activity_fetch_success = False
        self.account_data = empty_account_data()
        self.account_data_required = ACCOUNT_DATA_ENABLED
        self.account_data_fetch_success = not ACCOUNT_DATA_ENABLED
        self.listing_gift_required = LISTING_GIFT_ENABLED and is_listing_gift_date(current_date_text())
        self.listing_gift_success = False
        self.listing_gift_attempted = False
        self.listing_gift_status = (
            "待领取"
            if self.listing_gift_required
            else f"非领取日期（仅 {', '.join(sorted(LISTING_GIFT_DATES))}）"
        )
        self.listing_gift_time = ""
        self.listing_gift_detail = ""
        self.vote_required = VOTE_ENABLED and is_vote_date(current_date_text())
        self.vote_success = False
        self.vote_attempted = False
        self.vote_status = (
            "待执行"
            if self.vote_required
            else "非投票日期"
        )
        self.vote_time = ""
        self.vote_product_sku = VOTE_PRODUCT_SKU
        self.vote_product_name = VOTE_PRODUCT_NAME
        self.vote_detail = ""
        self._campaign_sso_wait_logged = False
        self._campaign_request_channel_logged = False
        self._campaign_cdp_session = None
        self._campaign_session_established = False

    def hydrate_from_previous(self, previous: dict):
        if not isinstance(previous, dict):
            return
        status = component_status(previous)
        if status["sign"]:
            self.sign_status = str(previous.get("sign_status") or self.sign_status)
            self.sign_completed_at = str(previous.get("sign_completed_at") or "")
            self.risk_controlled = truthy(previous.get("risk_controlled"))
            self.detail_reason = str(previous.get("detail_reason") or "")
        if status["points"]:
            self.initial_points = previous.get("initial_points") or 0
            self.final_points = previous.get("final_points") or 0
            self.points_reward = previous.get("points_reward") or 0
            self.points_fetch_success = True
        self.account_data = deepcopy(previous.get("account_data")) if isinstance(previous.get("account_data"), dict) else empty_account_data()
        self.account_data_fetch_success = truthy(previous.get("account_data_fetch_success"))
        self.activity_records = merge_activity_record_components(previous.get("activity_records"))
        self.lottery_fetch_success = status["lottery"]
        self.voucher_fetch_success = status["exchange"]
        self.seckill_fetch_success = True
        self.activity_fetch_success = self.lottery_fetch_success and self.voucher_fetch_success
        for key in LISTING_GIFT_RESULT_FIELDS:
            if key in previous:
                setattr(self, key, previous.get(key))
        for key in VOTE_RESULT_FIELDS:
            if key in previous:
                setattr(self, key, previous.get(key))

    def _mark_failure(self, status, raw=None, detail=""):
        reason = detail or build_detail_reason(raw, default=status)
        if is_risk_control_response(raw) or has_risk_control_text(reason):
            self.sign_status = "签到风控"
            self.detail_reason = reason or RISK_CONTROL_MESSAGE
            self.risk_controlled = True
            return
        self.sign_status = status
        self.detail_reason = reason or status

    def _refresh_token(self) -> bool:
        try:
            self.page.goto(BASE_URL, wait_until="networkidle")
            self.page.reload(wait_until="networkidle")
            new_token = extract_token_from_local_storage(self.page)
            if new_token:
                self.headers[HEADER_ACCESS_TOKEN] = new_token
                log(f"账号{self.account_index} - 🔄 token 已刷新")
                return True
        except Exception as e:
            log(f"账号{self.account_index} - 🔄 token 刷新失败: {e}")
        return False

    def _request_json_once(self, method, url, tag="API", payload=None, dump_body_on_error=False, dump_json_on_success_false=True):
        method = str(method or "GET").upper()
        try:
            headers = dict(self.headers)
            if method == "POST":
                request_kwargs = {"headers": headers, "timeout": 20}
                if payload is not None:
                    headers.setdefault("content-type", "application/json;charset=UTF-8")
                    request_kwargs["json"] = payload
                resp = requests.post(url, **request_kwargs)
            else:
                resp = requests.get(url, headers=headers, timeout=12)

            if resp.status_code != 200:
                allow = resp.headers.get("Allow") or resp.headers.get("allow") or ""
                msg = f"账号{self.account_index} - {tag}请求失败 {resp.status_code} ({method} {url})"
                if allow:
                    msg += f" Allow={allow}"
                if method == "POST" and payload is not None:
                    msg += f" payload={redact_sensitive(truncate_text(json.dumps(payload or {}, ensure_ascii=False), 500))}"
                log(msg)
                if dump_body_on_error:
                    body = redact_sensitive(truncate_text(resp.text, 2000))
                    log(f"账号{self.account_index} - {tag}响应内容: {body}")
                return None

            try:
                data = resp.json()
            except Exception:
                log(f"账号{self.account_index} - {tag}响应JSON解析失败 (200 {method} {url})")
                if dump_body_on_error:
                    body = redact_sensitive(truncate_text(resp.text, 2000))
                    log(f"账号{self.account_index} - {tag}响应内容: {body}")
                return None

            if dump_json_on_success_false and isinstance(data, dict) and data.get("success") is False:
                log(f"账号{self.account_index} - ⚠️ {tag}返回success=false: {redact_sensitive(truncate_text(json.dumps(data, ensure_ascii=False), 2000))}")

            if is_risk_control_response(data):
                self.risk_controlled = True
                self.detail_reason = build_detail_reason(data, "签到失败，疑似违反签到规则")
            return data

        except Exception as e:
            log(f"账号{self.account_index} - {tag}异常: {e}")
            return None

    def _get_json_once(self, url, tag="API", dump_body_on_error=False, dump_json_on_success_false=True):
        return self._request_json_once(
            "GET",
            url,
            tag=tag,
            dump_body_on_error=dump_body_on_error,
            dump_json_on_success_false=dump_json_on_success_false,
        )

    def _post_json_once(self, url, payload=None, tag="API", dump_body_on_error=False, dump_json_on_success_false=True):
        return self._request_json_once(
            "POST",
            url,
            tag=tag,
            payload=payload or {},
            dump_body_on_error=dump_body_on_error,
            dump_json_on_success_false=dump_json_on_success_false,
        )

    def _browser_fetch_json_once(
        self,
        method,
        url,
        tag="API",
        payload=None,
        dump_body_on_error=False,
        dump_json_on_success_false=True,
        log_http_error=True,
    ):
        if not self.page:
            return None
        method = str(method or "GET").upper()
        forbidden_headers = {"user-agent", "referer", "host", "origin", "content-length"}
        headers = {
            str(key): str(value)
            for key, value in self.headers.items()
            if key and value and str(key).lower() not in forbidden_headers
        }
        headers.setdefault("accept", "application/json, text/plain, */*")
        if method == "POST":
            headers.setdefault("content-type", "application/json;charset=UTF-8")
        try:
            result = self.page.evaluate(
                """async ({url, method, payload, headers}) => {
                    const captureRuntime = () => {
                        let runtime = null;
                        try {
                            if (Array.isArray(window.webpackJsonp)) {
                                const marker = `campaign-request-capture-${Date.now()}`;
                                window.webpackJsonp.push([
                                    [marker],
                                    {
                                        [marker]: (module, exports, require) => {
                                            runtime = require;
                                        },
                                    },
                                    [[marker]],
                                ]);
                            }
                        } catch (error) {}
                        return runtime;
                    };

                    const findOfficialRequest = () => {
                        if (window.__campaignOfficialRequest) {
                            return window.__campaignOfficialRequest;
                        }
                        const runtime = captureRuntime();
                        if (!runtime || !runtime.m) return null;
                        for (const [moduleId, factory] of Object.entries(runtime.m)) {
                            let source = "";
                            try {
                                source = String(factory);
                            } catch (error) {
                                continue;
                            }
                            if (
                                !source.includes("/api/portal/v1/secret/update") ||
                                !source.includes("secretkey")
                            ) {
                                continue;
                            }
                            try {
                                const exportsValue = runtime(moduleId);
                                const candidates = [
                                    exportsValue,
                                    ...Object.values(exportsValue || {}),
                                ];
                                const request = candidates.find((candidate) =>
                                    typeof candidate === "function" &&
                                    typeof candidate.use === "function"
                                );
                                if (request) {
                                    window.__campaignOfficialRequest = request;
                                    return request;
                                }
                            } catch (error) {}
                        }
                        return null;
                    };

                    const officialRequest = findOfficialRequest();
                    if (officialRequest) {
                        try {
                            const target = new URL(url, location.origin);
                            if (target.origin !== location.origin) {
                                throw new Error("campaign request origin mismatch");
                            }
                            const requestPath = `${target.pathname}${target.search}`;
                            const config = {
                                url: requestPath,
                                method: method.toLowerCase(),
                                flat: true,
                                errToReject: true,
                                errorTip: false,
                            };
                            if (method === "POST") {
                                config.data = payload || {};
                            } else {
                                config.params = payload || {};
                            }
                            const data = await officialRequest(config);
                            return {
                                status: 200,
                                ok: true,
                                allow: "",
                                data,
                                text: "",
                                channel: "official",
                            };
                        } catch (error) {
                            const response = error?.response || null;
                            const data = response?.data || null;
                            let text = "";
                            try {
                                text = JSON.stringify(data || {message: error?.message || "request failed"});
                            } catch (jsonError) {
                                text = String(error?.message || "request failed");
                            }
                            return {
                                status: Number(response?.status || 0),
                                ok: false,
                                allow: "",
                                data,
                                text: text.slice(0, 2000),
                                channel: "official",
                            };
                        }
                    }

                    const options = {
                        method,
                        credentials: "include",
                        headers,
                    };
                    if (method === "POST") {
                        options.body = JSON.stringify(payload || {});
                    }
                    const response = await fetch(url, options);
                    const text = await response.text();
                    let data = null;
                    try {
                        data = JSON.parse(text);
                    } catch (error) {
                        data = null;
                    }
                    return {
                        status: response.status,
                        ok: response.ok,
                        allow: response.headers.get("allow") || "",
                        data,
                        text: text.slice(0, 2000),
                        channel: "fetch-fallback",
                    };
                }""",
                {"url": url, "method": method, "payload": payload or {}, "headers": headers},
            )
            status = safe_int(result.get("status"), 0) if isinstance(result, dict) else 0
            data = result.get("data") if isinstance(result, dict) else None
            channel = str(result.get("channel") or "") if isinstance(result, dict) else ""
            if channel == "official" and not self._campaign_request_channel_logged:
                log(f"账号{self.account_index} - 活动页请求已接入官方会话通道")
                self._campaign_request_channel_logged = True
            if status != 200:
                if log_http_error:
                    allow = str(result.get("allow") or "") if isinstance(result, dict) else ""
                    msg = f"账号{self.account_index} - {tag}浏览器请求失败 {status} ({method} {url})"
                    if allow:
                        msg += f" Allow={allow}"
                    log(msg)
                    if dump_body_on_error and isinstance(result, dict):
                        log(f"账号{self.account_index} - {tag}浏览器响应内容: {redact_sensitive(truncate_text(result.get('text'), 2000))}")
                return None
            if data is None:
                log(f"账号{self.account_index} - {tag}浏览器响应JSON解析失败 (200 {method} {url})")
                if dump_body_on_error and isinstance(result, dict):
                    log(f"账号{self.account_index} - {tag}浏览器响应内容: {redact_sensitive(truncate_text(result.get('text'), 2000))}")
                return None
            if dump_json_on_success_false and isinstance(data, dict) and data.get("success") is False:
                log(f"账号{self.account_index} - ⚠️ {tag}浏览器返回success=false: {redact_sensitive(truncate_text(json.dumps(data, ensure_ascii=False), 2000))}")
            return data
        except Exception as e:
            log(f"账号{self.account_index} - {tag}浏览器请求异常: {e}")
            return None

    def get_json_retry1(self, url, tag="API", dump_body_on_error=False, dump_json_on_success_false=True):
        data = self._get_json_once(url, tag=tag, dump_body_on_error=dump_body_on_error, dump_json_on_success_false=dump_json_on_success_false)
        if isinstance(data, dict) and data.get("success") is True:
            return data
        if is_risk_control_response(data):
            self.risk_controlled = True
            self.detail_reason = build_detail_reason(data, RISK_CONTROL_MESSAGE)
            log(f"账号{self.account_index} - {tag}触发风控，停止GET重试")
            return data

        time.sleep(random.uniform(0.6, 1.2))
        log(f"账号{self.account_index} - 🔁 {tag}GET失败，重试一次GET...")
        data2 = self._get_json_once(url, tag=tag, dump_body_on_error=dump_body_on_error, dump_json_on_success_false=dump_json_on_success_false)
        return data2 if data2 is not None else data

    def post_json_retry_candidates(self, url, payloads=None, tag="API", dump_body_on_error=False, dump_json_on_success_false=True, prefer_records=False):
        payloads = payloads or [{}]
        last_data = None
        log(f"账号{self.account_index} - {tag}使用POST查询，候选参数 {len(payloads)} 组")
        for index, payload in enumerate(payloads, start=1):
            data = self._post_json_once(
                url,
                payload=payload,
                tag=tag,
                dump_body_on_error=dump_body_on_error,
                dump_json_on_success_false=dump_json_on_success_false,
            )
            if isinstance(data, dict) and data.get("success") is True:
                if prefer_records and not extract_data_list(data):
                    log(f"账号{self.account_index} - {tag}POST成功，列表为空，按正常结果处理")
                return data
            if isinstance(data, dict) and data.get("success") is None and extract_data_list(data):
                return data
            if data is not None:
                last_data = data
            if index < len(payloads):
                time.sleep(random.uniform(0.4, 0.9))
                log(f"账号{self.account_index} - 🔁 {tag}POST未成功，尝试下一组参数...")
        return last_data

    def get_points(self):
        url = f"{self.base_url}{CUSTOMER_INTEGRAL_PATH}"
        attempts = [
            ("requests", "GET", None),
            ("requests", "POST", {}),
            ("browser", "GET", None),
            ("browser", "POST", {}),
        ]
        for source, method, payload in attempts:
            if source == "requests" and method == "GET":
                data = self._get_json_once(
                    url,
                    tag="金豆",
                    dump_body_on_error=True,
                    dump_json_on_success_false=True,
                )
            elif source == "requests":
                data = self._post_json_once(
                    url,
                    payload=payload,
                    tag="金豆",
                    dump_body_on_error=True,
                    dump_json_on_success_false=True,
                )
            else:
                data = self._browser_fetch_json_once(
                    method,
                    url,
                    payload=payload,
                    tag="金豆",
                    dump_body_on_error=True,
                    dump_json_on_success_false=True,
                )

            points = extract_integral_voucher(data)
            if points is not None:
                self.points_fetch_success = True
                log(f"账号{self.account_index} - 金豆数量获取成功: {points:.1f}")
                return points

            if isinstance(data, dict) and data.get("success") is True:
                log(f"账号{self.account_index} - 金豆接口成功但未找到 integralVoucher: {redact_sensitive(truncate_text(json.dumps(data, ensure_ascii=False), 800))}")

        if self._refresh_token():
            data = self.get_json_retry1(
                url,
                tag="金豆",
                dump_body_on_error=True,
                dump_json_on_success_false=True,
            )
            points = extract_integral_voucher(data)
            if points is not None:
                self.points_fetch_success = True
                log(f"账号{self.account_index} - 金豆数量刷新token后获取成功: {points:.1f}")
                return points

        self.points_fetch_success = False
        log(f"账号{self.account_index} - 未获取到金豆数量")
        return None

    def fetch_voucher_change_records(self) -> list[dict]:
        records = []
        page_number = 1
        total_pages = 1
        self.voucher_fetch_success = True
        while page_number <= total_pages and page_number <= 100:
            payloads = [
                {**payload, "pageNum" if "pageNum" in payload else "pageNo": page_number}
                for payload in RECORD_POST_PAYLOADS
            ]
            data = self.post_json_retry_candidates(
                f"{self.base_url}{VOUCHER_CHANGE_RECORD_PATH}",
                payloads=payloads,
                tag=f"兑换记录第{page_number}页",
                dump_body_on_error=True,
                dump_json_on_success_false=True,
                prefer_records=True,
            )
            if not api_response_succeeded(data):
                self.voucher_fetch_success = False
                break
            page_records = [item for item in extract_data_list(data) if isinstance(item, dict)]
            records.extend(page_records)
            page_data = data.get("data") if isinstance(data, dict) else None
            if isinstance(page_data, dict):
                page_size = max(1, safe_int(page_data.get("pageSize"), 50))
                total_rows = max(0, safe_int(page_data.get("totalRows"), 0))
                calculated_pages = (total_rows + page_size - 1) // page_size if total_rows else page_number
                total_pages = max(
                    page_number,
                    safe_int(page_data.get("totalPages"), calculated_pages),
                    calculated_pages,
                )
            if page_number >= total_pages:
                break
            page_number += 1
        log(f"账号{self.account_index} - 兑换记录共获取 {len(records)} 条")
        return records

    def fetch_brand_activity_config(self) -> dict:
        if isinstance(self.brand_activity_config, dict):
            return self.brand_activity_config
        data = self.post_json_retry_candidates(
            f"{self.base_url}{BRAND_ACTIVITY_CONFIG_PATH}",
            payloads=[{}],
            tag="品牌活动配置",
            dump_body_on_error=True,
            dump_json_on_success_false=True,
        )
        self.brand_activity_config_success = api_response_succeeded(data)
        self.brand_activity_config = data if isinstance(data, dict) else {}
        return self.brand_activity_config

    def fetch_member_day_activity_config(self) -> dict:
        if not self.ensure_campaign_session():
            return {}
        payload = activity_config_payload(self.activity_access_id)
        if not payload:
            log(f"账号{self.account_index} - 盛夏活动配置缺少活动标识，跳过配置请求")
            return {}
        data = self._browser_fetch_json_once(
            "POST",
            f"{VOTE_API_BASE}{ACTIVITY_CONFIG_PATH}",
            payload=payload,
            tag="盛夏活动配置",
            dump_body_on_error=True,
            dump_json_on_success_false=False,
        )
        if api_response_succeeded(data):
            values = unique_text_values(
                find_values_by_key(data, {"activityAccessId", "accessId"})
            )
            if values:
                self.activity_access_id = values[0]
            return data
        return {}

    def get_seckill_category_ids(self, config=None) -> list[str]:
        config = config if isinstance(config, dict) else self.fetch_brand_activity_config()
        values = find_values_by_key(config, {"seckillActivityId", "categoryAccessId"})
        ids = unique_text_values(values)
        if ids:
            log(f"账号{self.account_index} - 从活动配置发现秒杀分类ID: {', '.join(ids)}")
            return ids
        fallback_ids = list(DEFAULT_SECKILL_CATEGORY_ACCESS_IDS)
        log(f"账号{self.account_index} - 品牌活动配置未提供秒杀分类ID，使用固定活动ID兜底: {', '.join(fallback_ids)}")
        return fallback_ids

    def get_lottery_activity_code(self, config=None) -> str:
        config = config if isinstance(config, dict) else self.fetch_brand_activity_config()
        values = find_values_by_key(config, {"lotteryActivityId", "activityCode"})
        codes = unique_text_values(values)
        if codes:
            log(f"账号{self.account_index} - 从活动配置发现抽奖活动码: {codes[0]}")
            return codes[0]
        log(f"账号{self.account_index} - 品牌活动配置未提供抽奖活动码，使用固定活动码兜底: {DEFAULT_LOTTERY_ACTIVITY_CODE}")
        return DEFAULT_LOTTERY_ACTIVITY_CODE

    def fetch_seckill_records(self, expiry_lookup: dict[str, str], category_ids=None) -> list[dict]:
        category_ids = unique_text_values(category_ids or self.get_seckill_category_ids())
        if not category_ids:
            self.seckill_fetch_success = self.brand_activity_config_success
            return []
        payloads = [
            {"categoryAccessIds": category_ids},
            {"categoryAccessId": category_ids[0]},
        ]
        data = self.post_json_retry_candidates(
            f"{self.base_url}{SECKILL_RECORDS_PATH}",
            payloads=payloads,
            tag="秒杀记录",
            dump_body_on_error=True,
            dump_json_on_success_false=True,
            prefer_records=True,
        )
        self.seckill_fetch_success = api_response_succeeded(data)
        rows = []
        for item in [row for row in extract_data_list(data) if isinstance(row, dict)][:2]:
            title = str(item.get("skuTitle") or item.get("goodsName") or "").strip()
            status = safe_int(item.get("status"), 0)
            claimed = status == 2
            expiry_date = "" if claimed else find_expiry_date(title, expiry_lookup)
            if status == 1:
                status_text = "暂未领取"
            elif status == 2:
                status_text = "已经领取"
            else:
                status_text = str(item.get("statusText") or item.get("statusName") or "").strip()
            if not claimed and expiry_date:
                status_text = f"{status_text or '未领取'} {expiry_date}"
            rows.append({
                "title": title,
                "status": status,
                "claimed": claimed,
                "status_text": status_text,
                "expiry_date": expiry_date,
            })
        log(f"账号{self.account_index} - 秒杀记录解析 {len(rows)} 条")
        return rows

    def fetch_lottery_wins(self, expiry_lookup: dict[str, str], activity_code="") -> list[dict]:
        activity_code = str(activity_code or self.get_lottery_activity_code()).strip()
        payloads = [
            {"pageNum": 1, "pageSize": 1000, "activityCode": activity_code},
            {"pageNum": 1, "pageSize": 1000},
        ] if activity_code else [
            {"pageNum": 1, "pageSize": 1000},
        ]
        data = self.post_json_retry_candidates(
            f"{self.base_url}{LOTTERY_WINS_PATH}",
            payloads=payloads,
            tag="抽奖记录",
            dump_body_on_error=True,
            dump_json_on_success_false=True,
            prefer_records=True,
        )
        self.lottery_fetch_success = api_response_succeeded(data)
        rows = []
        for item in [row for row in extract_data_list(data) if isinstance(row, dict)]:
            title = str(item.get("prizeTitle") or item.get("goodsName") or "").strip()
            is_points_prize = "金豆" in title
            expiry_date = next_year_last_day(item.get("createTime")) if is_points_prize else find_expiry_date(title, expiry_lookup)
            status_text = f"已经领取 {expiry_date}" if is_points_prize else "未领取"
            if not is_points_prize and expiry_date:
                status_text = f"未领取 {expiry_date}"
            rows.append({
                "title": title,
                "claimed": is_points_prize,
                "status_text": status_text,
                "expiry_date": expiry_date,
                "biz_order_code": str(item.get("bizOrderCode") or "").strip(),
                "receive_status": safe_int(item.get("receiveStatus"), None),
            })
        log(f"账号{self.account_index} - 抽奖记录解析 {len(rows)} 条")
        return rows

    def fetch_member_day_lottery_wins(self, config=None) -> list[dict]:
        if not self.ensure_campaign_session():
            self.lottery_fetch_success = False
            return []
        values = unique_text_values(
            find_values_by_key(config or {}, {"activityAccessId", "accessId"})
        )
        if values:
            self.activity_access_id = values[0]
        rows = []
        page_number = 1
        while True:
            data = self._browser_fetch_json_once(
                "POST",
                f"{VOTE_API_BASE}{ACTIVITY_WINNING_PATH}",
                payload={
                    "activityAccessId": self.activity_access_id,
                    "pageNum": page_number,
                    "pageSize": 1000,
                },
                tag=f"盛夏中奖记录第{page_number}页",
                dump_body_on_error=True,
                dump_json_on_success_false=False,
            )
            if not api_response_succeeded(data):
                self.lottery_fetch_success = False
                return []
            rows.extend(parse_lottery_winning_response(data))
            total_page_values = [
                safe_int(value, 1)
                for value in find_values_by_key(data, {"totalPages", "pages"})
            ]
            total_pages = max(total_page_values or [1])
            if page_number >= max(1, total_pages):
                break
            page_number += 1
        self.lottery_fetch_success = True
        log(f"账号{self.account_index} - 盛夏中奖记录解析 {len(rows)} 条")
        return rows

    def fetch_activity_component_with_retry(self, tag, fetcher, success_attr) -> list[dict]:
        records = []
        for attempt in range(2):
            try:
                records = fetcher()
            except Exception as e:
                setattr(self, success_attr, False)
                records = []
                log(f"账号{self.account_index} - {tag}抓取异常: {e}")
            if truthy(getattr(self, success_attr, False)):
                if attempt > 0:
                    log(f"账号{self.account_index} - {tag}活动数据重试成功")
                return records
            if attempt == 0:
                log(f"账号{self.account_index} - {tag}首次请求未完成，当前登录会话内重试一次（不会调用签到接口）")
                time.sleep(random.uniform(0.8, 1.5))
        log(f"账号{self.account_index} - {tag}重试后仍未获取成功")
        return records

    def fetch_activity_records(self, components=None) -> dict:
        requested = set(components or ("lottery", "exchange"))
        if not requested.intersection({"lottery", "exchange"}):
            return self.activity_records
        try:
            activity_label = "秒杀/抽奖/兑换" if SECKILL_ENABLED else "抽奖/兑换"
            log(f"账号{self.account_index} - 开始获取{activity_label}中奖记录")
            member_day_config = self.fetch_member_day_activity_config() if "lottery" in requested else None
            config = (member_day_config or self.fetch_brand_activity_config()) if "lottery" in requested else {}
            lottery_activity_code = self.get_lottery_activity_code(config)
            seckill_records = self.activity_records.get("seckill", [])
            if SECKILL_ENABLED:
                if "lottery" in requested:
                    seckill_category_ids = self.get_seckill_category_ids(config)
                    seckill_records = self.fetch_activity_component_with_retry(
                        "秒杀记录",
                        lambda: self.fetch_seckill_records({}, seckill_category_ids),
                        "seckill_fetch_success",
                    )
            else:
                self.seckill_fetch_success = True
            lottery_records = self.activity_records.get("lottery", [])
            if "lottery" in requested:
                lottery_records = []
                if member_day_config:
                    lottery_records = self.fetch_activity_component_with_retry(
                        "盛夏中奖记录",
                        lambda: self.fetch_member_day_lottery_wins(member_day_config),
                        "lottery_fetch_success",
                    )
                if not member_day_config or not self.lottery_fetch_success:
                    log(f"账号{self.account_index} - 盛夏中奖接口不可用，回退通用抽奖记录接口")
                    lottery_records = self.fetch_activity_component_with_retry(
                        "抽奖记录",
                        lambda: self.fetch_lottery_wins({}, lottery_activity_code),
                        "lottery_fetch_success",
                    )
            change_records = []
            exchange_records = self.activity_records.get("exchange", [])
            if "exchange" in requested:
                change_records = self.fetch_activity_component_with_retry(
                    "兑换记录",
                    self.fetch_voucher_change_records,
                    "voucher_fetch_success",
                )
                exchange_records = normalize_exchange_records(change_records)
            if needs_expiry_lookup(seckill_records + lottery_records):
                expiry_lookup = build_expiry_lookup(change_records or exchange_records)
                if SECKILL_ENABLED:
                    apply_expiry_dates(seckill_records, expiry_lookup)
                apply_expiry_dates(lottery_records, expiry_lookup, lottery=True)
            else:
                log(f"账号{self.account_index} - 未发现需要补截止时间的未领取奖品，跳过过期时间查询")
            self.activity_records = {
                "seckill": seckill_records,
                "lottery": lottery_records,
                "exchange": exchange_records,
            }
            self.activity_fetch_success = (
                (not SECKILL_ENABLED or self.seckill_fetch_success)
                and self.lottery_fetch_success
                and self.voucher_fetch_success
            )
            if SECKILL_ENABLED:
                log(
                    f"账号{self.account_index} - 活动记录获取完成：秒杀 {len(seckill_records)} 条，"
                    f"抽奖 {len(lottery_records)} 条，2026年8月起兑换 {len(exchange_records)} 条"
                )
            else:
                log(
                    f"账号{self.account_index} - 活动记录获取完成：抽奖 {len(lottery_records)} 条，"
                    f"2026年8月起兑换 {len(exchange_records)} 条"
                )
            if not self.activity_fetch_success:
                failures = []
                if SECKILL_ENABLED and not self.seckill_fetch_success:
                    failures.append("秒杀数据获取失败")
                if not self.lottery_fetch_success:
                    failures.append("抽奖数据获取失败")
                if not self.voucher_fetch_success:
                    failures.append("兑换记录获取失败")
                failure_reason = "；".join(failures) or "活动数据获取失败"
                if failure_reason not in self.detail_reason:
                    self.detail_reason = f"{self.detail_reason}；{failure_reason}".strip("；")
                log(f"账号{self.account_index} - 活动接口重试后仍未全部成功: {failure_reason}")
        except Exception as e:
            log(f"账号{self.account_index} - 活动记录抓取异常: {e}")
            if "lottery" in requested:
                self.lottery_fetch_success = False
            if "exchange" in requested:
                self.voucher_fetch_success = False
            self.activity_fetch_success = self.lottery_fetch_success and self.voucher_fetch_success
            failure_reason = f"活动数据抓取异常: {redact_sensitive(truncate_text(str(e), 500))}"
            if failure_reason not in self.detail_reason:
                self.detail_reason = f"{self.detail_reason}；{failure_reason}".strip("；")
        return self.activity_records

    def fetch_account_data(self, components=None) -> dict:
        if not self.account_data_required:
            return self.account_data
        requested = set(components or ("invoice", "pcb_orders", "coupons"))
        if not requested.intersection({"invoice", "pcb_orders", "coupons"}):
            return self.account_data
        self.account_data = AccountDataCollector(
            self.page,
            self.account_index,
            logger=log,
        ).collect(previous=self.account_data, components=requested)
        self.account_data_fetch_success = truthy(self.account_data.get("fetch_success"))
        if self.account_data_fetch_success:
            coupons = self.account_data.get("coupons") or {}
            log(
                f"账号{self.account_index} - 会员资料获取完成："
                f"未使用 {len(coupons.get('unused') or [])} 张，"
                f"已使用 {len(coupons.get('used') or [])} 张，"
                f"已过期 {len(coupons.get('expired') or [])} 张"
            )
        else:
            reason = str(self.account_data.get("error") or "会员资料接口未完整返回").strip()
            log(f"账号{self.account_index} - 会员资料获取失败：{reason}")
            if reason not in self.detail_reason:
                self.detail_reason = "；".join(part for part in (self.detail_reason, reason) if part)
        return self.account_data

    def execute_listing_gift(self, task_date="") -> bool:
        self.listing_gift_required = LISTING_GIFT_ENABLED and is_listing_gift_date(task_date or current_date_text())
        if not self.listing_gift_required:
            self.listing_gift_status = f"非领取日期（仅 {', '.join(sorted(LISTING_GIFT_DATES))}）"
            return True

        self.listing_gift_attempted = True
        last_result = {"state": "error", "success": False, "message": "礼包接口尚未执行"}
        for attempt in range(1, 4):
            response = self._request_json_once(
                "POST",
                f"{self.base_url}{LISTING_GIFT_PATH}",
                tag="上市礼包领取",
                payload=None,
                dump_body_on_error=True,
                dump_json_on_success_false=True,
            )
            last_result = inspect_listing_gift_response(response)
            if last_result.get("success"):
                self.listing_gift_success = True
                self.listing_gift_time = current_time_text()
                self.listing_gift_detail = str(last_result.get("message") or "").strip()
                self.listing_gift_status = (
                    "上市礼包已领取"
                    if last_result.get("state") == "already"
                    else "上市礼包领取成功"
                )
                log(f"账号{self.account_index} - ✅ {self.listing_gift_status}")
                return True
            if attempt < 3:
                log(f"账号{self.account_index} - 上市礼包领取未确认，当前会话内第 {attempt + 1} 次尝试")
                time.sleep(0.8 * attempt)

        self.listing_gift_detail = str(last_result.get("message") or "礼包接口未确认领取成功").strip()
        self.listing_gift_status = f"上市礼包领取失败：{self.listing_gift_detail}"
        log(f"账号{self.account_index} - ❌ {self.listing_gift_status}")
        return False

    def _fetch_vote_config(self):
        return self._browser_fetch_json_once(
            "POST",
            f"{VOTE_API_BASE}{VOTE_CONFIG_PATH}",
            payload={"activityAccessId": self.activity_access_id},
            tag="投票状态",
            dump_body_on_error=True,
            dump_json_on_success_false=True,
        )

    def _campaign_session_ready(self, quiet=False) -> bool:
        response = self._browser_fetch_json_once(
            "GET",
            f"{VOTE_API_BASE}{VOTE_USER_INFO_PATH}",
            tag="投票 SSO 会话",
            dump_body_on_error=not quiet,
            dump_json_on_success_false=False,
            log_http_error=not quiet,
        )
        return campaign_session_ready(response)

    def _prepare_campaign_navigation(self) -> bool:
        """保留当前会话 Cookie，只把活动页导航切换为桌面浏览器模式。"""
        try:
            session = self.page.context.new_cdp_session(self.page)
            session.send(
                "Emulation.setUserAgentOverride",
                {
                    "userAgent": CAMPAIGN_DESKTOP_USER_AGENT,
                    "acceptLanguage": "zh-CN,zh;q=0.9",
                    "platform": "Win32",
                    "userAgentMetadata": {
                        "brands": [
                            {"brand": "Chromium", "version": "138"},
                            {"brand": "Not=A?Brand", "version": "24"},
                        ],
                        "fullVersionList": [
                            {"brand": "Chromium", "version": "138.0.0.0"},
                            {"brand": "Not=A?Brand", "version": "24.0.0.0"},
                        ],
                        "fullVersion": "138.0.0.0",
                        "platform": "Windows",
                        "platformVersion": "10.0.0",
                        "architecture": "x86",
                        "model": "",
                        "mobile": False,
                        "bitness": "64",
                        "wow64": False,
                    },
                },
            )
            session.send(
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": 1365,
                    "height": 900,
                    "deviceScaleFactor": 1,
                    "mobile": False,
                    "screenWidth": 1365,
                    "screenHeight": 900,
                },
            )
            session.send("Emulation.setTouchEmulationEnabled", {"enabled": False})
            self._campaign_cdp_session = session
            log(f"账号{self.account_index} - 已切换活动页桌面浏览器模式")
            return True
        except Exception as cdp_error:
            try:
                self.page.set_extra_http_headers({"User-Agent": CAMPAIGN_DESKTOP_USER_AGENT})
                log(f"账号{self.account_index} - 已通过请求头切换活动页桌面模式")
                return True
            except Exception as header_error:
                detail = redact_sensitive(
                    truncate_text(f"{cdp_error}; {header_error}", 400)
                )
                log(f"账号{self.account_index} - 活动页桌面模式切换失败: {detail}")
                return False

    def _campaign_page_ready(self) -> bool:
        try:
            current_path = str(
                self.page.evaluate("() => location.pathname || '/'") or "/"
            ).rstrip("/") or "/"
        except Exception as e:
            log(
                f"账号{self.account_index} - 无法确认活动页路径: "
                f"{redact_sensitive(truncate_text(str(e), 300))}"
            )
            return False
        if current_path == VOTE_CAMPAIGN_PATH:
            return True
        log(
            f"账号{self.account_index} - 活动页被重定向："
            f"期望路径={VOTE_CAMPAIGN_PATH}，实际路径={current_path}"
        )
        return False

    def _trigger_campaign_sso(self) -> bool:
        """调用门户自己的登录入口，让统一账号会话完成活动页 SSO。"""
        try:
            sdk_result = self.page.evaluate(
                """() => {
                    const iframeSelector =
                        'iframe[id*="cas" i], iframe[name*="cas" i], iframe[src*="/by-pit"]';
                    const state = {
                        entryReady: typeof window.openLoginWindow === "function",
                        sdkReady: false,
                        triggered: false,
                        triggerMethod: "",
                        autoState: window.__campaignAutoSsoState?.status || "",
                        iframeCount: document.querySelectorAll(iframeSelector).length,
                    };

                    const findSdk = (exportsValue) => {
                        if (!exportsValue) return null;
                        const candidates = [exportsValue];
                        if (typeof exportsValue === "object" || typeof exportsValue === "function") {
                            try {
                                candidates.push(...Object.values(exportsValue));
                            } catch (error) {}
                        }
                        return candidates.find((candidate) =>
                            candidate &&
                            typeof candidate.crossCheckLogin === "function" &&
                            typeof candidate.iframeAutoLogin === "function" &&
                            typeof candidate.getAuthCode === "function"
                        ) || null;
                    };

                    let runtime = null;
                    try {
                        if (Array.isArray(window.webpackJsonp)) {
                            const marker = `campaign-auth-capture-${Date.now()}`;
                            window.webpackJsonp.push([
                                [marker],
                                {
                                    [marker]: (module, exports, require) => {
                                        runtime = require;
                                    },
                                },
                                [[marker]],
                            ]);
                        }
                    } catch (error) {}

                    let sdk = null;
                    if (runtime && runtime.c) {
                        for (const cachedModule of Object.values(runtime.c)) {
                            sdk = findSdk(cachedModule?.exports);
                            if (sdk) break;
                        }
                    }

                    let portalAuth = null;
                    if (runtime && runtime.c) {
                        for (const cachedModule of Object.values(runtime.c)) {
                            const exportsValue = cachedModule?.exports;
                            if (exportsValue && typeof exportsValue.tryLogin === "function") {
                                portalAuth = exportsValue;
                                break;
                            }
                        }
                    }
                    if (!portalAuth && runtime && runtime.m) {
                        for (const [moduleId, factory] of Object.entries(runtime.m)) {
                            let source = "";
                            try {
                                source = String(factory);
                            } catch (error) {
                                continue;
                            }
                            if (
                                !source.includes("/login/login-by-code") ||
                                !source.includes("triggerLoginOnFail")
                            ) {
                                continue;
                            }
                            try {
                                const exportsValue = runtime(moduleId);
                                if (exportsValue && typeof exportsValue.tryLogin === "function") {
                                    portalAuth = exportsValue;
                                    break;
                                }
                            } catch (error) {}
                        }
                    }

                    if (portalAuth) {
                        state.entryReady = true;
                        state.sdkReady = true;
                        let autoState = window.__campaignAutoSsoState;
                        if (!autoState || autoState.status === "failed") {
                            autoState = {
                                status: "starting",
                                httpStatus: 0,
                                apiCode: null,
                                checkStatus: 0,
                            };
                            window.__campaignAutoSsoState = autoState;
                            Promise.resolve(portalAuth.tryLogin(false))
                                .then(async (data) => {
                                    autoState.status = "exchange";
                                    autoState.httpStatus = 200;
                                    autoState.apiCode = data?.code ?? null;
                                    if (String(autoState.apiCode) !== "200") {
                                        throw new Error("official login exchange failed");
                                    }
                                    autoState.status = "check";
                                    const checkResponse = await fetch(
                                        "/api/portal/login/checkLoginState",
                                        {credentials: "include"}
                                    );
                                    autoState.checkStatus = checkResponse.status;
                                    if (!checkResponse.ok) {
                                        throw new Error("login state check failed");
                                    }
                                    autoState.status = "complete";
                                })
                                .catch(() => {
                                    autoState.status = "failed";
                                });
                        }
                        state.triggered = true;
                        state.triggerMethod = "portal-auth";
                        state.autoState = autoState.status;
                        state.iframeCount = document.querySelectorAll(iframeSelector).length;
                        return state;
                    }

                    if (!sdk && runtime) {
                        try {
                            sdk = findSdk(runtime(54));
                        } catch (error) {}
                    }

                    if (sdk) {
                        state.entryReady = true;
                        state.sdkReady = true;
                        let autoState = window.__campaignAutoSsoState;
                        if (!autoState || autoState.status === "failed") {
                            autoState = {
                                status: "starting",
                                httpStatus: 0,
                                apiCode: null,
                                checkStatus: 0,
                            };
                            window.__campaignAutoSsoState = autoState;
                            Promise.resolve()
                                .then(() => sdk.crossCheckLogin())
                                .catch(() => {
                                    autoState.status = "iframe";
                                    return sdk.iframeAutoLogin();
                                })
                                .then(async (code) => {
                                    autoState.status = "exchange";
                                    const body = new URLSearchParams();
                                    body.set("code", String(code || ""));
                                    const response = await fetch("/login/login-by-code", {
                                        method: "POST",
                                        credentials: "include",
                                        headers: {
                                            "Content-Type": "application/x-www-form-urlencoded",
                                        },
                                        body: body.toString(),
                                    });
                                    autoState.httpStatus = response.status;
                                    const data = await response.json().catch(() => null);
                                    autoState.apiCode = data?.code ?? null;
                                    if (!response.ok || String(autoState.apiCode) !== "200") {
                                        throw new Error("code exchange failed");
                                    }
                                    autoState.status = "check";
                                    const checkResponse = await fetch(
                                        "/api/portal/login/checkLoginState",
                                        {credentials: "include"}
                                    );
                                    autoState.checkStatus = checkResponse.status;
                                    if (!checkResponse.ok) {
                                        throw new Error("login state check failed");
                                    }
                                    autoState.status = "complete";
                                })
                                .catch(() => {
                                    autoState.status = "failed";
                                });
                        }
                        state.triggered = true;
                        state.triggerMethod = "sdk-auto";
                        state.autoState = autoState.status;
                        state.iframeCount = document.querySelectorAll(iframeSelector).length;
                        return state;
                    }

                    if (state.entryReady) {
                        try {
                            window.openLoginWindow();
                            state.triggered = true;
                            state.triggerMethod = "login-window";
                        } catch (error) {
                            state.triggerError = true;
                        }
                    }
                    state.iframeCount = document.querySelectorAll(iframeSelector).length;
                    return state;
                }"""
            )
            if isinstance(sdk_result, dict) and truthy(sdk_result.get("triggered")):
                iframe_count = safe_int(sdk_result.get("iframeCount"), 0)
                if sdk_result.get("triggerMethod") == "portal-auth":
                    log(
                        f"账号{self.account_index} - 已调用活动页官方 SSO 通道"
                        f"（状态: {sdk_result.get('autoState') or 'starting'}，"
                        f"CAS iframe: {iframe_count}）"
                    )
                elif sdk_result.get("triggerMethod") == "sdk-auto":
                    log(
                        f"账号{self.account_index} - 已调用活动页自动 SSO 通道"
                        f"（状态: {sdk_result.get('autoState') or 'starting'}，"
                        f"CAS iframe: {iframe_count}）"
                    )
                else:
                    log(
                        f"账号{self.account_index} - 已调用活动页登录组件触发 SSO"
                        f"（CAS iframe: {iframe_count}）"
                    )
                return True
        except Exception as e:
            if not self._campaign_sso_wait_logged:
                log(
                    f"账号{self.account_index} - 活动页登录组件尚未就绪: "
                    f"{redact_sensitive(truncate_text(str(e), 300))}"
                )

        # 兼容页面尚未暴露 SDK 入口、但已经渲染登录控件的版本。
        locator_factories = (
            lambda: self.page.get_by_role("button", name="登录", exact=True),
            lambda: self.page.get_by_role("link", name="登录", exact=True),
            lambda: self.page.get_by_text("登录", exact=True),
        )
        for locator_factory in locator_factories:
            try:
                login_entry = locator_factory()
                if login_entry.count() != 1 or not login_entry.is_visible():
                    continue
                login_entry.click(timeout=5000)
                log(f"账号{self.account_index} - 已点击活动页登录入口，等待 SSO 会话建立")
                return True
            except Exception:
                continue

        if not self._campaign_sso_wait_logged:
            log(f"账号{self.account_index} - 等待活动页登录组件初始化")
            self._campaign_sso_wait_logged = True
        return False

    def _campaign_sso_diagnostics(self) -> dict:
        """只采集不含凭据的页面状态，供 SSO 失败日志定位。"""
        try:
            result = self.page.evaluate(
                """() => ({
                    entryReady: typeof window.openLoginWindow === "function",
                    autoState: window.__campaignAutoSsoState ? {
                        status: window.__campaignAutoSsoState.status || "",
                        httpStatus: window.__campaignAutoSsoState.httpStatus || 0,
                        apiCode: window.__campaignAutoSsoState.apiCode ?? null,
                        checkStatus: window.__campaignAutoSsoState.checkStatus || 0,
                    } : null,
                    iframeCount: document.querySelectorAll(
                        'iframe[id*="cas" i], iframe[name*="cas" i], iframe[src*="/by-pit"]'
                    ).length,
                    path: location.pathname || "",
                })"""
            )
            return result if isinstance(result, dict) else {}
        except Exception:
            return {}

    def _wait_for_campaign_session(self, attempts=15) -> bool:
        """给活动页异步初始化留出时间，并在需要时主动触发一次 SSO。"""
        sso_triggered = False
        sso_reloaded = False
        attempts = max(1, safe_int(attempts, 15))
        for attempt in range(attempts):
            if self._campaign_session_ready(quiet=attempt < attempts - 1):
                if attempt > 0:
                    log(f"账号{self.account_index} - 活动页 SSO 会话已建立")
                return True

            if not sso_triggered:
                sso_triggered = self._trigger_campaign_sso()

            if sso_triggered and not sso_reloaded:
                diagnostics = self._campaign_sso_diagnostics()
                auto_state = diagnostics.get("autoState") if isinstance(diagnostics, dict) else None
                if isinstance(auto_state, dict) and auto_state.get("status") == "complete":
                    try:
                        self.page.reload(wait_until="domcontentloaded", timeout=60000)
                        sso_reloaded = True
                        log(f"账号{self.account_index} - 自动 SSO 交换完成，已重新加载活动页")
                        continue
                    except Exception as e:
                        log(
                            f"账号{self.account_index} - 自动 SSO 后活动页重载失败: "
                            f"{redact_sensitive(truncate_text(str(e), 300))}"
                        )

            if attempt < attempts - 1:
                time.sleep(random.uniform(1.2, 1.8))
        diagnostics = self._campaign_sso_diagnostics()
        auto_state = diagnostics.get("autoState") if isinstance(diagnostics, dict) else None
        auto_summary = "未启动"
        if isinstance(auto_state, dict):
            auto_summary = (
                f"{auto_state.get('status') or '未知'}"
                f"/HTTP {safe_int(auto_state.get('httpStatus'), 0)}"
                f"/API {str(auto_state.get('apiCode') or '-')}"
                f"/CHECK {safe_int(auto_state.get('checkStatus'), 0)}"
            )
        log(
            f"账号{self.account_index} - 活动页 SSO 诊断："
            f"登录组件={'已就绪' if truthy(diagnostics.get('entryReady')) else '未就绪'}，"
            f"CAS iframe={safe_int(diagnostics.get('iframeCount'), 0)}，"
            f"自动通道={auto_summary}，"
            f"页面路径={str(diagnostics.get('path') or '未知')}"
        )
        return False

    def ensure_campaign_session(self) -> bool:
        if self._campaign_session_established and self._campaign_session_ready(quiet=True):
            return True
        if vote_environment_error():
            return False
        if not self._prepare_campaign_navigation():
            return False
        try:
            self.page.goto(CAMPAIGN_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            log(f"账号{self.account_index} - 活动页打开失败: {type(exc).__name__}")
            return False
        if not self._campaign_page_ready() or not self._wait_for_campaign_session():
            return False
        self._campaign_session_established = True
        return True

    def execute_campaign_vote(self) -> bool:
        if not self.vote_required:
            log(f"账号{self.account_index} - {self.vote_status}")
            return True

        environment_error = vote_environment_error()
        if environment_error:
            self.vote_detail = environment_error
            self.vote_status = f"投票失败：{environment_error}"
            log(f"账号{self.account_index} - ❌ {self.vote_status}")
            return False

        self.vote_status = "投票失败"
        try:
            log(f"账号{self.account_index} - 签到全部操作完成，打开品牌活动页触发 SSO")
            if not self.ensure_campaign_session():
                self.vote_detail = "活动页 SSO 会话未建立"
                self.vote_status = f"投票失败：{self.vote_detail}"
                log(f"账号{self.account_index} - ❌ {self.vote_status}")
                return False
            decision = {"state": "error", "message": "活动页 SSO 会话未建立"}
            for attempt in range(2):
                config_response = self._fetch_vote_config()
                decision = inspect_vote_config(config_response)
                if decision.get("state") != "error":
                    break
                if attempt == 0:
                    log(f"账号{self.account_index} - 投票状态首次未就绪，在当前 SSO 会话内重试一次")
                    time.sleep(random.uniform(0.8, 1.4))

            state = decision.get("state")
            self.vote_detail = str(decision.get("message") or "")
            if state == "already":
                self.vote_success = True
                self.vote_status = self.vote_detail
                self.vote_time = current_time_text()
                log(f"账号{self.account_index} - ✅ {self.vote_status}")
                return True
            if state != "ready":
                self.vote_status = f"投票失败：{self.vote_detail or '目标商品状态不可用'}"
                log(f"账号{self.account_index} - ❌ {self.vote_status}")
                return False

            self.vote_attempted = True
            payload = {
                "activityAccessId": self.activity_access_id,
                "productSku": VOTE_PRODUCT_SKU,
            }
            submit_response = self._browser_fetch_json_once(
                "POST",
                f"{VOTE_API_BASE}{VOTE_SUBMIT_PATH}",
                payload=payload,
                tag="洗衣液投票",
                dump_body_on_error=True,
                dump_json_on_success_false=True,
            )
            submit_ok = isinstance(submit_response, dict) and submit_response.get("success") is True
            verify = {"state": "error", "message": "投票结果尚未复核"}
            verified = False
            for verify_attempt in range(3):
                time.sleep(random.uniform(0.5, 0.9) if verify_attempt == 0 else random.uniform(1.0, 1.6))
                verify_response = self._fetch_vote_config()
                verify = inspect_vote_config(verify_response)
                verified = verify.get("state") == "already" and verify.get("my_sku") == VOTE_PRODUCT_SKU
                if verified:
                    break

            if verified:
                self.vote_success = True
                self.vote_status = f"投票成功：{VOTE_PRODUCT_NAME}"
                self.vote_time = current_time_text()
                self.vote_detail = "投票接口成功并复核完成" if submit_ok else "投票结果复核完成"
                log(f"账号{self.account_index} - ✅ {self.vote_status}")
                return True

            submit_message = (
                "投票接口返回成功但复核未确认"
                if submit_ok
                else build_detail_reason(submit_response, "投票接口未成功")
            )
            verify_message = str(verify.get("message") or "投后复核失败")
            self.vote_detail = f"{submit_message}；{verify_message}".strip("；")
            self.vote_status = f"投票失败：{self.vote_detail}"
            log(f"账号{self.account_index} - ❌ {self.vote_status}")
            return False
        except Exception as e:
            self.vote_detail = redact_sensitive(truncate_text(str(e), 500))
            self.vote_status = f"投票异常：{self.vote_detail}"
            log(f"账号{self.account_index} - ❌ {self.vote_status}")
            return False

    def execute_banned_process(self):
        self.banned_account = True
        points = self.get_points()
        self.initial_points = points or 0.0
        self.final_points = points or 0.0
        self.points_reward = 0.0
        self.sign_status = "账号封禁"
        self.detail_reason = "账号在 BANNED_ACCOUNTS 中，已跳过签到" if points is not None else "账号在 BANNED_ACCOUNTS 中，已跳过签到；金豆数量获取失败"
        return True

    def finalize_banned_data_status(self):
        failures = []
        if not self.points_fetch_success:
            failures.append("金豆数量获取失败")
        if not self.activity_fetch_success:
            failures.append("中奖记录获取未完成")
        self.detail_reason = "账号在 BANNED_ACCOUNTS 中，已跳过签到"
        if failures:
            self.detail_reason += "；" + "；".join(failures)

    def get_sign_config(self):
        url = f"{self.base_url}{SIGN_CONFIG_PATH}"
        data = self.get_json_retry1(url, tag="签到配置", dump_body_on_error=True, dump_json_on_success_false=True)
        if not (data and data.get("success")):
            self._refresh_token()
            data = self.get_json_retry1(url, tag="签到配置", dump_body_on_error=True, dump_json_on_success_false=True)

        if not (data and data.get("success")):
            return None

        raw = data.get("data") or {}
        have_signed = truthy(raw.get("haveSignIn")) or truthy(raw.get("haveSign"))
        have_receive = truthy(raw.get("haveReceive"))
        today_day = safe_int(raw.get("day"), 0)

        self.today_day = today_day
        log(f"账号{self.account_index} - 📅 签到配置解析：今天第 {today_day} 天，haveSignIn={have_signed}, haveReceive={have_receive}")
        return have_signed, today_day, have_receive, data

    def receive_voucher(self):
        url = f"{self.base_url}{RECEIVE_VOUCHER_PATH}"
        data = self.get_json_retry1(url, tag="领取奖励", dump_body_on_error=True, dump_json_on_success_false=True)

        if data and data.get("success") and is_nonzero_reward_value(data.get("data")):
            jindou = safe_int(data.get("data"), 0)
            log(f"账号{self.account_index} - ✅ 奖励领取成功（+{jindou} 金豆）")
            return True, jindou, data

        if is_risk_control_response(data):
            self._mark_failure("签到风控", raw=data)
            return False
        if data and data.get("success"):
            log(f"账号{self.account_index} - ❌ 领取奖励接口 success=true 但 data 异常: {redact_sensitive(truncate_text(json.dumps(data, ensure_ascii=False), 2000))}")
        else:
            log(f"账号{self.account_index} - ❌ 奖励领取失败")
        return False, 0, data

    def sign_in_simple(self):
        """
        单纯做一次签到（只GET，失败重试一次）
        用于：非第7天“领取成功后额外签到”、第7天“重复领取金豆”兜底补签
        """
        url = f"{self.base_url}{API_SIGN_PATH}"
        log(f"账号{self.account_index} - 🧾 执行一次签到（只GET，失败重试一次）...")
        data = self.get_json_retry1(url, tag="签到", dump_body_on_error=True, dump_json_on_success_false=True)
        if data and data.get("success"):
            self.sign_status = "签到成功"
            self.sign_completed_at = current_time_text()
            log(f"账号{self.account_index} - ✅ 签到成功")
            return True
        self.sign_status = "签到失败"
        log(f"账号{self.account_index} - ❌ 签到失败")
        return False

    def sign_in(self):
        """
        ✅ 修复点1：第一次就检查“未领取”，不再先重试签到
        ✅ 修复点2：非第7天，领取成功后必须额外签到一次
        """
        url = f"{self.base_url}{API_SIGN_PATH}"
        log(f"账号{self.account_index} - 尝试使用 GET 方法签到...")

        # 第一次请求：先判断“未领取”
        data1 = self._get_json_once(url, tag="签到", dump_body_on_error=True, dump_json_on_success_false=True)

        if data1 and isinstance(data1, dict) and data1.get("success") is True:
            self.sign_status = "签到成功"
            self.sign_completed_at = current_time_text()
            log(f"账号{self.account_index} - ✅ 签到成功")
            return True

        if is_risk_control_response(data1):
            self.risk_controlled = True
            self.sign_status = "签到风控"
            self.detail_reason = build_detail_reason(data1, RISK_CONTROL_MESSAGE)
            log(f"账号{self.account_index} - 签到触发风控，停止本账号签到重试")
            return False

        if isinstance(data1, dict) and is_unclaimed_reward_error(data1):
            log(f"账号{self.account_index} - 🎁 检测到“存在签到未领取”，开始领取奖励...")
            ok, _jindou, raw = self.receive_voucher()
            if ok:
                self.has_reward = True
                # 只要不是第7天：领取后再额外签到一次
                if self.today_day != 7:
                    log(f"账号{self.account_index} - ➕ 非第7天：领取奖励后需要额外签到一次")
                    return self.sign_in_simple()
                # 第7天：领取成功即完成（不再签到）
                self.sign_status = "领取奖励成功"
                return True

            # 领取失败：第7天如果是“重复领取金豆” -> 额外签到一次
            if isinstance(raw, dict) and is_duplicate_claim_error(raw):
                log(f"账号{self.account_index} - ♻️ 领取返回“重复领取金豆”，改为额外执行一次签到")
                return self.sign_in_simple()

            self.sign_status = "领取奖励失败"
            return False

        # 不是“未领取”情况：才重试一次签到
        time.sleep(random.uniform(0.6, 1.2))
        log(f"账号{self.account_index} - 🔁 签到GET失败，重试一次GET...")
        data2 = self._get_json_once(url, tag="签到", dump_body_on_error=True, dump_json_on_success_false=True)

        if data2 and isinstance(data2, dict) and data2.get("success") is True:
            self.sign_status = "签到成功"
            self.sign_completed_at = current_time_text()
            log(f"账号{self.account_index} - ✅ 签到成功")
            return True

        # 重试后才出现“未领取”：也要处理（不再继续重试签到）
        if isinstance(data2, dict) and is_unclaimed_reward_error(data2):
            log(f"账号{self.account_index} - 🎁 重试后检测到“存在签到未领取”，开始领取奖励...")
            ok, _jindou, raw = self.receive_voucher()
            if ok:
                self.has_reward = True
                if self.today_day != 7:
                    log(f"账号{self.account_index} - ➕ 非第7天：领取奖励后需要额外签到一次")
                    return self.sign_in_simple()
                self.sign_status = "领取奖励成功"
                return True

            if isinstance(raw, dict) and is_duplicate_claim_error(raw):
                log(f"账号{self.account_index} - ♻️ 领取返回“重复领取金豆”，改为额外执行一次签到")
                return self.sign_in_simple()

            self.sign_status = "领取奖励失败"
            return False

        if is_risk_control_response(data2):
            self.risk_controlled = True
            self.sign_status = "签到风控"
            self.detail_reason = build_detail_reason(data2, RISK_CONTROL_MESSAGE)
            log(f"账号{self.account_index} - 签到重试触发风控，停止本账号签到重试")
            return False

        self.sign_status = "签到失败"
        log(f"账号{self.account_index} - ❌ 签到失败")
        return False

    def execute_full_process(self):
        time.sleep(random.uniform(1, 2))
        self.initial_points = self.get_points() or 0
        time.sleep(random.uniform(1, 2))

        cfg = self.get_sign_config()
        if cfg is None:
            self.sign_status = "检查失败"
            return False

        have_signed, today_day, have_receive, _raw = cfg

        # 第7天特殊逻辑：
        # 1) 不信任 haveReceive，不用它判断是否已领
        # 2) 如果配置显示已签到，则直接调用领奖接口
        # 3) 如果未签到，则先签到；若接口提示“当前用户当天已经签到”，也直接转领奖
        # 4) 如果领奖接口提示“重复领取金豆”，按“已经领取过”处理，算成功
        if today_day == 7:
            sign_url = f"{self.base_url}{API_SIGN_PATH}"
            log(f"账号{self.account_index} - 🎯 今天第7天：已签到则直接领奖；未签到则先签到再领奖（忽略 haveReceive 判断）")

            if have_signed:
                log(f"账号{self.account_index} - ℹ️ 第7天配置显示今日已签到，直接调用领取奖励接口")
            else:
                sign_data = self.get_json_retry1(sign_url, tag="签到", dump_body_on_error=True, dump_json_on_success_false=True)
                log(f"账号{self.account_index} - 🧩 第7天签到接口返回: {redact_sensitive(truncate_text(json.dumps(sign_data, ensure_ascii=False), 2000)) if sign_data is not None else 'null'}")

                already_signed = isinstance(sign_data, dict) and ("当前用户当天已经签到" in str(sign_data.get("message") or ""))
                if isinstance(sign_data, dict) and sign_data.get("success") is True:
                    self.sign_completed_at = current_time_text()
                    gain_num = get_sign_gain_num(sign_data)
                    if is_nonzero_reward_value(gain_num):
                        log(f"账号{self.account_index} - ✅ 第7天签到成功（gainNum={gain_num}）")
                    else:
                        log(f"账号{self.account_index} - ⚠️ 第7天签到返回 success=true，但 gainNum={gain_num}，继续尝试领取第7天奖励")
                elif already_signed:
                    log(f"账号{self.account_index} - ℹ️ 第7天签到接口提示“当前用户当天已经签到”，直接转领取奖励")
                else:
                    self.sign_status = "第7天签到失败"
                    log(f"账号{self.account_index} - ❌ 第7天签到失败，且不是“当天已签到”场景")
                    return False

            ok, _jindou, raw = self.receive_voucher()
            log(f"账号{self.account_index} - 🧩 第7天领取奖励接口返回: {redact_sensitive(truncate_text(json.dumps(raw, ensure_ascii=False), 2000)) if raw is not None else 'null'}")
            if ok:
                self.has_reward = True
                self.sign_status = "第7天签到并领取成功"
                if not self.sign_completed_at:
                    self.sign_completed_at = current_time_text()
            elif isinstance(raw, dict) and is_duplicate_claim_error(raw):
                self.has_reward = True
                self.sign_status = "第7天奖励已领取"
                if not self.sign_completed_at:
                    self.sign_completed_at = current_time_text()
                log(f"账号{self.account_index} - ℹ️ 第7天领取接口提示“重复领取金豆”，按已领取处理")
            else:
                self.sign_status = "第7天领取奖励失败"
                return False

            time.sleep(random.uniform(1, 2))
            self.final_points = self.get_points() or self.initial_points
            self.points_reward = self.final_points - self.initial_points
            return True

        # 非第7天：如果已签到，结束；否则走 sign_in()（里面会处理未领取 -> 领取 -> 再签）
        if have_signed:
            self.sign_status = "已签到过"
            self.sign_completed_at = current_time_text()
        else:
            time.sleep(random.uniform(1, 2))
            if not self.sign_in():
                return False

        time.sleep(random.uniform(1, 2))
        self.final_points = self.get_points() or 0
        self.points_reward = self.final_points - self.initial_points
        return True

# ==============================================================================
# 单个账号登录与签到主流程
# ==============================================================================
def sign_in_account(
    username,
    password,
    account_index,
    total_accounts,
    retry_count=0,
    is_final_retry=False,
    previous_result=None,
):
    _SENSITIVE_LOG_VALUES.update(
        value for value in (str(username or "").strip(), str(password or "")) if value
    )
    label = f" (重试{retry_count})" if retry_count > 0 else (" (最终重试)" if is_final_retry else "")
    log(f"开始处理账号 {account_index}/{total_accounts}{label}")
    task_start_date = normalize_task_start_date()
    banned_account = is_banned_account(username)
    data_only_retry = is_data_only_retry_mode()
    previous_sign_success = retry_previous_sign_success()
    previous_risk_controlled = retry_previous_risk_controlled()
    previous_final_points = retry_previous_final_points()
    previous_result = deepcopy(previous_result) if isinstance(previous_result, dict) else {}
    components = requested_components(previous_result)
    previous_status = component_status(previous_result)
    execution = execution_context()
    skip_sign = execution['sign_skipped']
    if banned_account:
        log(f"账号{account_index} - 命中 BANNED_ACCOUNTS，登录后只获取金豆与活动记录，跳过签到")
    elif data_only_retry:
        activity_label = "秒杀/抽奖" if SECKILL_ENABLED else "抽奖"
        log(f"账号{account_index} - 补数据重试模式：登录后只获取金豆与{activity_label}记录，跳过签到接口")
    elif skip_sign:
        log(f"账号{account_index} - {execution['group_code']} 按组配置跳过签到，继续执行全部后续操作")

    result = {
        'account_index': account_index,
        'username': username,
        'masked_username': mask_account(username),
        'sign_status': '签到风控' if previous_risk_controlled else ('补数据重试' if data_only_retry else '未知'),
        'sign_success': previous_sign_success if data_only_retry else False,
        'initial_points': previous_final_points if data_only_retry else 0,
        'final_points': previous_final_points if data_only_retry else 0,
        'points_reward': 0,
        'has_reward': False,
        'token_extracted': False,
        'secretkey_extracted': False,
        'retry_count': retry_count,
        'is_final_retry': is_final_retry,
        'password_error': False,
        'risk_controlled': previous_risk_controlled if data_only_retry else False,
        'detail_reason': RISK_CONTROL_MESSAGE if previous_risk_controlled else ('补数据重试，跳过签到接口' if data_only_retry else ''),
        'sign_time': '',
        'sign_ip': '',
        'banned_account': banned_account,
        'points_fetch_success': False,
        'activity_fetch_success': False,
        'data_fetch_completed': False,
        'account_data_required': ACCOUNT_DATA_ENABLED,
        'account_data_fetch_success': not ACCOUNT_DATA_ENABLED,
        'account_data': empty_account_data(),
        'next_day_success': False,
        'task_start_date': task_start_date,
        'sign_completed_at': '',
        'activity_records': make_empty_extra_records(),
        'listing_gift_required': LISTING_GIFT_ENABLED and is_listing_gift_date(task_start_date),
        'listing_gift_success': False,
        'listing_gift_attempted': False,
        'listing_gift_status': '待领取' if LISTING_GIFT_ENABLED and is_listing_gift_date(task_start_date) else f"非领取日期（仅 {', '.join(sorted(LISTING_GIFT_DATES))}）",
        'listing_gift_time': '',
        'listing_gift_detail': '',
        'vote_required': VOTE_ENABLED and is_vote_date(task_start_date),
        'vote_success': False,
        'vote_attempted': False,
        'vote_status': initial_vote_status(task_start_date),
        'vote_time': '',
        'vote_product_sku': VOTE_PRODUCT_SKU,
        'vote_product_name': VOTE_PRODUCT_NAME,
        'vote_detail': '',
        **execution,
    }
    if previous_result:
        result.update(deepcopy(previous_result))
        result.update({
            'account_index': account_index,
            'username': username,
            'masked_username': mask_account(username),
            'retry_count': retry_count,
            'is_final_retry': is_final_retry,
            **execution,
        })
        log(f"账号{account_index} - 仅补偿未完成组件: {', '.join(sorted(components)) or '无'}")

    ua_string = get_random_mobile_ua()

    with sync_playwright() as p:
        browser = None
        context = None
        page = None
        try:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-features=IsolateOrigins,site-per-process',
                    '--disable-web-security',
                ]
            )
            context = browser.new_context(
                user_agent=ua_string,
                viewport={'width': 375, 'height': 812},
                locale='zh-CN',
                timezone_id='Asia/Shanghai',
                device_scale_factor=2,
                has_touch=True,
                is_mobile=True,
            )
            context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
                Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh']});
                window.chrome = {runtime: {}};
            """)

            page = context.new_page()

            secretkey_holder = {'value': None}
            token_holder = {'value': None}

            def handle_route(route):
                headers = {k.lower(): v for k, v in route.request.headers.items()}
                key = headers.get(HEADER_SECRET_KEY.lower())
                if key:
                    secretkey_holder['value'] = key
                token = headers.get(HEADER_ACCESS_TOKEN.lower())
                if not token:
                    for hk in HEADER_ACCESS_TOKEN_FALLBACKS:
                        token = headers.get(hk)
                        if token:
                            break
                if token:
                    token_holder['value'] = token
                route.continue_()

            context.route(f"**{LOGIN_API_PATH}*", handle_route)

            # ---------- 登录流程 ----------
            log(f"账号{account_index} - 打开移动登录页...")
            page.goto(PASSPORT_URL, timeout=60000)
            page.wait_for_selector('input[placeholder*="手机号码"], input[placeholder*="邮箱"]', timeout=30000)
            log("✅ 登录页加载完成")

            page.locator('input[placeholder*="手机号码"], input[placeholder*="邮箱"]').first.fill(username)
            log("✅ 已填写账号")

            agree_selector = "#__layout > div > div > div > div > div:nth-child(3) > form > div.mt-30.mb-32 > div.consent-agreement > div > img:nth-child(2)"
            try:
                page.locator(agree_selector).click(timeout=5000)
                log("✅ 已点击同意协议")
            except Exception as e:
                log(f"⚠️ 点击同意协议失败（可能已默认同意）: {e}")

            first_login_btn = "#__layout > div > div > div > div > div:nth-child(3) > form > button"
            try:
                page.locator(first_login_btn).click(timeout=5000)
                log("✅ 已点击第一步登录按钮")
            except Exception as e:
                log(f"⚠️ 点击第一步登录按钮失败: {e}")

            time.sleep(1)

            password_xpath = "/html/body/div[1]/div/div/div/div/div/div[2]/div[2]/form/div[2]/div/div[1]/div[1]/input"
            page.wait_for_selector(f"xpath={password_xpath}", timeout=10000)
            log("✅ 密码框已出现")
            page.locator(f"xpath={password_xpath}").fill(password)
            log("✅ 已填写密码")

            second_login_btn = "#__layout > div > div > div > div > div:nth-child(2) > div:nth-child(2) > form > button"
            try:
                page.locator(second_login_btn).click(timeout=5000)
                log("✅ 已点击最终登录按钮")
            except Exception as e:
                log(f"⚠️ 点击最终登录按钮失败: {e}")
                page.locator('form button[type="submit"]').click()

            # ===== 执行滑块破解 =====
            slider_ok = solve_slider_with_bezier(page)
            if not slider_ok:
                result['sign_status'] = '滑块未通过'
                return result

            # ===== 滑块完成后，监控密码错误7秒，同时等待首页 =====
            monitor_start = time.time()
            home_found = False

            while time.time() - monitor_start < 7:
                if page.locator("text=/账号或密码不正确|用户名或密码错误|密码错误|登录失败/").is_visible(timeout=500):
                    log(f"账号{account_index} - ❌ 密码错误（滑块后检测）")
                    result['password_error'] = True
                    result['sign_status'] = '密码错误'
                    return result

                try:
                    page.wait_for_selector(HOME_SELECTOR, timeout=500)
                    home_found = True
                    break
                except PlaywrightTimeoutError:
                    continue

            if not home_found:
                page.wait_for_selector(HOME_SELECTOR, timeout=30000 - 7000)
                log(f"账号{account_index} - ✅ 已进入首页")
            else:
                log(f"账号{account_index} - ✅ 已进入首页")

            # 提取 token
            access_token = extract_token_from_local_storage(page)
            if not access_token:
                access_token = wait_token_from_requests(token_holder, timeout=8)

            if not access_token:
                page.reload(wait_until="networkidle")
                access_token = extract_token_from_local_storage(page)
                if not access_token:
                    access_token = wait_token_from_requests(token_holder, timeout=8)

            secretkey = secretkey_holder['value']
            result['token_extracted'] = bool(access_token)
            result['secretkey_extracted'] = bool(secretkey)

            if access_token:
                client = ApiClient(access_token, secretkey, account_index, page, user_agent=ua_string)
                client.hydrate_from_previous(previous_result)
                if banned_account:
                    client.listing_gift_required = False
                    client.listing_gift_status = "账号封禁，已跳过礼包领取"
                    if "points" in components:
                        log(f"账号{account_index} - 封禁账号已登录，开始获取金豆数量")
                        client.execute_banned_process()
                    success = False
                elif "sign" not in components:
                    success = truthy(previous_result.get("sign_success"))
                    if previous_status["sign"]:
                        log(f"账号{account_index} - 签到组件已完成，本次不再调用签到接口")
                    if "points" in components:
                        latest_points = client.get_points()
                        if latest_points is not None:
                            client.initial_points = previous_result.get("final_points") or latest_points
                            client.final_points = latest_points
                            client.points_reward = 0.0
                elif data_only_retry:
                    client.risk_controlled = previous_risk_controlled
                    client.sign_status = '签到风控' if previous_risk_controlled else '补数据重试'
                    client.detail_reason = RISK_CONTROL_MESSAGE if previous_risk_controlled else '补数据重试，跳过签到接口'
                    success = previous_sign_success
                    latest_points = client.get_points()
                    if latest_points is not None:
                        client.initial_points = previous_final_points or latest_points
                        client.final_points = latest_points
                        client.points_reward = 0.0 if not previous_final_points else latest_points - previous_final_points
                    else:
                        client.initial_points = previous_final_points
                        client.final_points = previous_final_points
                elif skip_sign:
                    latest_points = client.get_points()
                    if latest_points is not None:
                        client.initial_points = latest_points
                        client.final_points = latest_points
                        client.points_reward = 0.0
                    client.sign_status = '按组配置跳过签到'
                    client.detail_reason = '同行组按配置跳过签到；已继续执行全部后续操作'
                    success = False
                else:
                    log(f"账号{account_index} - 使用 token 进行签到（只GET；未领取先领；非第7天领完再签一次）")
                    success = client.execute_full_process()
                    if client.final_points == 0:
                        latest_points = client.get_points()
                        if latest_points is not None:
                            client.final_points = latest_points
                            client.points_reward = client.final_points - client.initial_points

                if not banned_account and "gift" in components:
                    client.execute_listing_gift(task_start_date)
                client.fetch_account_data(components)
                client.fetch_activity_records(components)
                if VOTE_ENABLED and "vote" in components:
                    vote_allowed = can_vote_after_sign(
                        success,
                        sign_step_completed=(
                            (not banned_account and "sign" in components)
                            or previous_status["sign"]
                        ),
                        sign_skipped=skip_sign,
                        data_only_retry=data_only_retry,
                        previous_sign_success=previous_sign_success,
                    )
                    if client.vote_required and not vote_allowed:
                        client.vote_status = '签到步骤未执行，已跳过投票'
                        client.vote_detail = '必须先完成签到步骤，未请求投票接口'
                        log(f"账号{account_index} - {client.vote_status}")
                    else:
                        client.execute_campaign_vote()
                if banned_account:
                    client.finalize_banned_data_status()
                result.update({
                    'sign_success': success,
                    'sign_status': '账号封禁' if banned_account else ('签到风控' if client.risk_controlled and not success else client.sign_status),
                    'initial_points': client.initial_points,
                    'final_points': client.final_points,
                    'points_reward': client.points_reward,
                    'has_reward': client.has_reward,
                    'risk_controlled': client.risk_controlled,
                    'detail_reason': client.detail_reason,
                    'banned_account': banned_account,
                    'points_fetch_success': client.points_fetch_success,
                    'activity_fetch_success': client.activity_fetch_success,
                    'data_fetch_completed': (
                        client.points_fetch_success
                        and client.activity_fetch_success
                        and client.account_data_fetch_success
                    ),
                    'account_data_required': client.account_data_required,
                    'account_data_fetch_success': client.account_data_fetch_success,
                    'account_data': client.account_data,
                    'sign_completed_at': client.sign_completed_at,
                    'activity_records': client.activity_records,
                    'listing_gift_required': client.listing_gift_required,
                    'listing_gift_success': client.listing_gift_success,
                    'listing_gift_attempted': client.listing_gift_attempted,
                    'listing_gift_status': client.listing_gift_status,
                    'listing_gift_time': client.listing_gift_time,
                    'listing_gift_detail': client.listing_gift_detail,
                    'vote_required': client.vote_required,
                    'vote_success': client.vote_success,
                    'vote_attempted': client.vote_attempted,
                    'vote_status': client.vote_status,
                    'vote_time': client.vote_time,
                    'vote_product_sku': client.vote_product_sku,
                    'vote_product_name': client.vote_product_name,
                    'vote_detail': client.vote_detail,
                    'component_status': {
                        'login': True,
                        'sign': (
                            truthy(success)
                            or skip_sign
                            or client.risk_controlled
                            or banned_account
                        ),
                        'points': client.points_fetch_success,
                        'invoice': truthy(client.account_data.get('invoice_fetch_success')),
                        'pcb_orders': truthy(client.account_data.get('pcb_order_fetch_success')),
                        'coupons': truthy(client.account_data.get('coupon_fetch_success')),
                        'lottery': client.lottery_fetch_success,
                        'exchange': client.voucher_fetch_success,
                        'gift': (
                            not client.listing_gift_required
                            or client.listing_gift_success
                        ),
                        'vote': (
                            not client.vote_required
                            or client.vote_success
                            or '本期已锁定其他商品' in f"{client.vote_status} {client.vote_detail}"
                        ),
                    },
                    **execution,
                })
            else:
                log(f"账号{account_index} - ❌ 未提取到 token")
                result['sign_status'] = '封禁账号取数失败' if banned_account else 'Token提取失败'
                result['detail_reason'] = (
                    '账号在 BANNED_ACCOUNTS 中，已跳过签到；Token提取失败，未能获取金豆和中奖记录'
                    if banned_account else 'Token提取失败'
                )

        except Exception as e:
            log(f"账号{account_index} - ❌ 执行异常: {e}")
            result['sign_status'] = '封禁账号取数失败' if banned_account else '执行异常'
            error_text = redact_sensitive(truncate_text(str(e), 500))
            result['detail_reason'] = (
                f'账号在 BANNED_ACCOUNTS 中，已跳过签到；数据获取执行异常: {error_text}'
                if banned_account else f'执行异常: {error_text}'
            )
        finally:
            if context:
                context.close()
            if browser:
                browser.close()
            finalize_result_metadata(result)
            time.sleep(1)

    return result

# ==============================================================================
# 重试逻辑与结果合并（保持不变）
# ==============================================================================
def should_retry(res):
    return needs_retry(res)

def process_single_account(username, password, account_index, total_accounts):
    merged = {
        'account_index': account_index,
        'username': username,
        'masked_username': mask_account(username),
        'sign_status': '未知',
        'sign_success': False,
        'initial_points': 0,
        'final_points': 0,
        'points_reward': 0,
        'has_reward': False,
        'token_extracted': False,
        'secretkey_extracted': False,
        'retry_count': 0,
        'is_final_retry': False,
        'password_error': False,
        'risk_controlled': False,
        'detail_reason': '',
        'sign_time': '',
        'sign_ip': '',
        'banned_account': is_banned_account(username),
        'points_fetch_success': False,
        'activity_fetch_success': False,
        'data_fetch_completed': False,
        'account_data_required': ACCOUNT_DATA_ENABLED,
        'account_data_fetch_success': not ACCOUNT_DATA_ENABLED,
        'account_data': empty_account_data(),
        'next_day_success': False,
        'task_start_date': normalize_task_start_date(),
        'sign_completed_at': '',
        'activity_records': make_empty_extra_records(),
        'listing_gift_required': LISTING_GIFT_ENABLED and is_listing_gift_date(normalize_task_start_date()),
        'listing_gift_success': False,
        'listing_gift_attempted': False,
        'listing_gift_status': '未执行',
        'listing_gift_time': '',
        'listing_gift_detail': '',
        'vote_required': VOTE_ENABLED and is_vote_date(normalize_task_start_date()),
        'vote_success': False,
        'vote_attempted': False,
        'vote_status': '未执行',
        'vote_time': '',
        'vote_product_sku': VOTE_PRODUCT_SKU,
        'vote_product_name': VOTE_PRODUCT_NAME,
        'vote_detail': '',
        **execution_context(),
    }
    seed = load_previous_result()
    if seed:
        merged.update(deepcopy(seed))
        merged.update({
            'account_index': account_index,
            'username': username,
            'masked_username': mask_account(username),
            **execution_context(),
        })
    max_retries = 3
    for attempt in range(max_retries + 1):
        if attempt > 0 or seed:
            pending = retry_components(merged)
            if not pending:
                break
        res = sign_in_account(
            username,
            password,
            account_index,
            total_accounts,
            retry_count=safe_int(merged.get('retry_count'), 0) + (1 if attempt > 0 or seed else 0),
            is_final_retry=bool(seed),
            previous_result=merged if attempt > 0 or seed else None,
        )

        if res.get('password_error'):
            merged['password_error'] = True
            merged['sign_status'] = '密码错误'
            merged['username'] = username
            merged['masked_username'] = mask_account(username)
            merged['detail_reason'] = res.get('detail_reason') or '密码错误'
            merged['sign_time'] = res.get('sign_time', '')
            merged['sign_ip'] = res.get('sign_ip', '')
            merged['activity_records'] = merge_activity_record_components(
                merged.get('activity_records'), res.get('activity_records')
            )
            break

        if merged.get('banned_account'):
            merged['token_extracted'] = truthy(merged.get('token_extracted')) or truthy(res.get('token_extracted'))
            merged['secretkey_extracted'] = truthy(merged.get('secretkey_extracted')) or truthy(res.get('secretkey_extracted'))
            if truthy(res.get('points_fetch_success')):
                for key in ('initial_points', 'final_points', 'points_reward'):
                    merged[key] = res.get(key, 0)
                merged['points_fetch_success'] = True
            if truthy(res.get('activity_fetch_success')):
                merged['activity_records'] = res.get('activity_records') or make_empty_extra_records()
                merged['activity_fetch_success'] = True
            if truthy(res.get('account_data_fetch_success')):
                merged['account_data'] = res.get('account_data') or empty_account_data()
                merged['account_data_fetch_success'] = True
            for key in ('sign_time', 'sign_ip', 'task_start_date', 'sign_completed_at'):
                if res.get(key):
                    merged[key] = res.get(key)
            merged['data_fetch_completed'] = (
                truthy(merged.get('points_fetch_success'))
                and truthy(merged.get('activity_fetch_success'))
                and (
                    not truthy(merged.get('account_data_required'))
                    or truthy(merged.get('account_data_fetch_success'))
                )
            )
            merged['sign_status'] = '账号封禁' if merged['data_fetch_completed'] else '封禁账号取数失败'
            failures = []
            if not merged.get('points_fetch_success'):
                failures.append('金豆数量获取失败')
            if not merged.get('activity_fetch_success'):
                failures.append('中奖记录获取未完成')
            if merged.get('account_data_required') and not merged.get('account_data_fetch_success'):
                failures.append('会员资料获取未完成')
            merged['detail_reason'] = '账号在 BANNED_ACCOUNTS 中，已跳过签到'
            if failures:
                merged['detail_reason'] += '；' + '；'.join(failures)
        elif res['sign_success'] and not merged['sign_success']:
            for k in ['sign_success', 'sign_status', 'initial_points', 'final_points', 'points_reward', 'has_reward', 'token_extracted', 'secretkey_extracted', 'risk_controlled', 'detail_reason', 'sign_time', 'sign_ip', 'banned_account', 'points_fetch_success', 'activity_fetch_success', 'data_fetch_completed', 'next_day_success', 'task_start_date', 'sign_completed_at', 'activity_records', 'account_data_required', 'account_data_fetch_success', 'account_data', 'listing_gift_required', 'listing_gift_success', 'listing_gift_attempted', 'listing_gift_status', 'listing_gift_time', 'listing_gift_detail', 'vote_required', 'vote_success', 'vote_attempted', 'vote_status', 'vote_time', 'vote_product_sku', 'vote_product_name', 'vote_detail']:
                merged[k] = res[k]
        elif not merged['sign_success']:
            for k in ['sign_status', 'token_extracted', 'secretkey_extracted', 'risk_controlled', 'detail_reason', 'sign_time', 'sign_ip', 'banned_account', 'points_fetch_success', 'activity_fetch_success', 'data_fetch_completed', 'next_day_success', 'task_start_date', 'sign_completed_at', 'activity_records', 'account_data_required', 'account_data_fetch_success', 'account_data', 'initial_points', 'final_points', 'points_reward', 'listing_gift_required', 'listing_gift_success', 'listing_gift_attempted', 'listing_gift_status', 'listing_gift_time', 'listing_gift_detail', 'vote_required', 'vote_success', 'vote_attempted', 'vote_status', 'vote_time', 'vote_product_sku', 'vote_product_name', 'vote_detail']:
                merged[k] = res.get(k)

        merged['activity_records'] = merge_activity_record_components(
            merged.get('activity_records'), res.get('activity_records')
        )
        merged['activity_fetch_success'] = truthy(merged.get('activity_fetch_success')) or truthy(
            res.get('activity_fetch_success')
        )
        merged['data_fetch_completed'] = (
            truthy(merged.get('points_fetch_success'))
            and truthy(merged.get('activity_fetch_success'))
            and (
                not truthy(merged.get('account_data_required'))
                or truthy(merged.get('account_data_fetch_success'))
            )
        )

        if truthy(res.get('vote_success')) or not truthy(merged.get('vote_success')):
            for key in VOTE_RESULT_FIELDS:
                if key in res:
                    merged[key] = res.get(key)
        if truthy(res.get('listing_gift_success')) or not truthy(merged.get('listing_gift_success')):
            for key in LISTING_GIFT_RESULT_FIELDS:
                if key in res:
                    merged[key] = res.get(key)
        for key in EXECUTION_RESULT_FIELDS:
            if key in res:
                merged[key] = res.get(key)

        current_components = component_status(merged)
        result_components = component_status(res)
        merged['component_status'] = {
            key: bool(current_components.get(key) or result_components.get(key))
            for key in set(current_components) | set(result_components)
        }

        merged['retry_count'] = res['retry_count']

        if not should_retry(merged) or attempt >= max_retries:
            break
        log(f"账号{account_index} - 🔄 准备第 {attempt+1} 次重试...")
        time.sleep(random.uniform(3, 7))
    return merged

def final_retry(all_results, usernames, passwords, total_accounts):
    log("=" * 70)
    log("🔄 执行最终重试（针对之前失败的账号）")
    log("=" * 70)
    failed = []
    for i, r in enumerate(all_results):
        if should_retry(r):
            failed.append({
                'index': i,
                'account_index': r['account_index'],
                'username': r.get('username') or usernames[i],
                'password': passwords[i],
                'prev_retry': r['retry_count']
            })
    if not failed:
        log("✅ 没有需要最终重试的账号")
        return all_results

    log(f"📋 需重试账号序号: {', '.join(str(f['account_index']) for f in failed)}")
    time.sleep(random.uniform(3, 5))

    for f in failed:
        log(f"🔄 最终重试账号 {f['account_index']}")
        final = sign_in_account(f['username'], f['password'], f['account_index'], total_accounts,
                                retry_count=f['prev_retry'] + 1, is_final_retry=True,
                                previous_result=all_results[f['index']])
        orig = all_results[f['index']]

        if final.get('password_error'):
            orig.update({
                'password_error': True,
                'sign_status': '密码错误',
                'username': f['username'],
                'masked_username': mask_account(f['username']),
                'detail_reason': final.get('detail_reason') or '密码错误',
                'sign_time': final.get('sign_time', ''),
                'sign_ip': final.get('sign_ip', ''),
                'activity_records': merge_activity_record_components(
                    orig.get('activity_records'), final.get('activity_records')
                ),
                'is_final_retry': True
            })
            continue

        if orig.get('banned_account'):
            orig['token_extracted'] = truthy(orig.get('token_extracted')) or truthy(final.get('token_extracted'))
            orig['secretkey_extracted'] = truthy(orig.get('secretkey_extracted')) or truthy(final.get('secretkey_extracted'))
            if truthy(final.get('points_fetch_success')):
                for key in ('initial_points', 'final_points', 'points_reward'):
                    orig[key] = final.get(key, 0)
                orig['points_fetch_success'] = True
            if truthy(final.get('activity_fetch_success')):
                orig['activity_records'] = final.get('activity_records') or make_empty_extra_records()
                orig['activity_fetch_success'] = True
            if truthy(final.get('account_data_fetch_success')):
                orig['account_data'] = final.get('account_data') or empty_account_data()
                orig['account_data_fetch_success'] = True
            for key in ('sign_time', 'sign_ip', 'task_start_date', 'sign_completed_at'):
                if final.get(key):
                    orig[key] = final.get(key)
            orig['data_fetch_completed'] = (
                truthy(orig.get('points_fetch_success'))
                and truthy(orig.get('activity_fetch_success'))
                and (
                    not truthy(orig.get('account_data_required'))
                    or truthy(orig.get('account_data_fetch_success'))
                )
            )
            failures = []
            if not orig.get('points_fetch_success'):
                failures.append('金豆数量获取失败')
            if not orig.get('activity_fetch_success'):
                failures.append('中奖记录获取未完成')
            if orig.get('account_data_required') and not orig.get('account_data_fetch_success'):
                failures.append('会员资料获取未完成')
            orig['sign_status'] = '账号封禁' if not failures else '封禁账号取数失败'
            orig['detail_reason'] = '账号在 BANNED_ACCOUNTS 中，已跳过签到'
            if failures:
                orig['detail_reason'] += '；' + '；'.join(failures)
        elif final['sign_success'] and not orig['sign_success']:
            for k in ['sign_success', 'sign_status', 'initial_points', 'final_points', 'points_reward', 'has_reward', 'token_extracted', 'secretkey_extracted', 'risk_controlled', 'detail_reason', 'sign_time', 'sign_ip', 'banned_account', 'points_fetch_success', 'activity_fetch_success', 'data_fetch_completed', 'next_day_success', 'task_start_date', 'sign_completed_at', 'activity_records', 'account_data_required', 'account_data_fetch_success', 'account_data', 'listing_gift_required', 'listing_gift_success', 'listing_gift_attempted', 'listing_gift_status', 'listing_gift_time', 'listing_gift_detail', 'vote_required', 'vote_success', 'vote_attempted', 'vote_status', 'vote_time', 'vote_product_sku', 'vote_product_name', 'vote_detail']:
                orig[k] = final[k]
        elif not orig['sign_success']:
            for k in ['sign_status', 'token_extracted', 'secretkey_extracted', 'risk_controlled', 'detail_reason', 'sign_time', 'sign_ip', 'banned_account', 'points_fetch_success', 'activity_fetch_success', 'data_fetch_completed', 'next_day_success', 'task_start_date', 'sign_completed_at', 'activity_records', 'account_data_required', 'account_data_fetch_success', 'account_data', 'initial_points', 'final_points', 'points_reward', 'listing_gift_required', 'listing_gift_success', 'listing_gift_attempted', 'listing_gift_status', 'listing_gift_time', 'listing_gift_detail', 'vote_required', 'vote_success', 'vote_attempted', 'vote_status', 'vote_time', 'vote_product_sku', 'vote_product_name', 'vote_detail']:
                orig[k] = final.get(k)

        orig['activity_records'] = merge_activity_record_components(
            orig.get('activity_records'), final.get('activity_records')
        )
        orig['activity_fetch_success'] = truthy(orig.get('activity_fetch_success')) or truthy(
            final.get('activity_fetch_success')
        )
        orig['data_fetch_completed'] = (
            truthy(orig.get('points_fetch_success'))
            and truthy(orig.get('activity_fetch_success'))
            and (
                not truthy(orig.get('account_data_required'))
                or truthy(orig.get('account_data_fetch_success'))
            )
        )

        if truthy(final.get('vote_success')) or not truthy(orig.get('vote_success')):
            for key in VOTE_RESULT_FIELDS:
                if key in final:
                    orig[key] = final.get(key)
        if truthy(final.get('listing_gift_success')) or not truthy(orig.get('listing_gift_success')):
            for key in LISTING_GIFT_RESULT_FIELDS:
                if key in final:
                    orig[key] = final.get(key)
        for key in EXECUTION_RESULT_FIELDS:
            if key in final:
                orig[key] = final.get(key)

        orig.update({
            'is_final_retry': True,
            'retry_count': f['prev_retry'] + 1,
            'username': f['username'],
            'masked_username': mask_account(f['username'])
        })

        if f != failed[-1]:
            time.sleep(random.uniform(4, 8))
    log("✅ 最终重试完成")
    return all_results

def summarize_results(all_results):
    success_count = 0
    total_reward = 0
    reward_count = 0
    password_error = []
    banned_accounts = []
    other_failed = []

    for r in all_results:
        if r.get('banned_account'):
            banned_accounts.append(r)
            if not truthy(r.get('data_fetch_completed')):
                other_failed.append(r)
            continue
        if r.get('sign_success'):
            success_count += 1
        elif truthy(r.get('sign_skipped')):
            if not truthy(r.get('data_fetch_completed')):
                other_failed.append(r)
        else:
            if r.get('password_error'):
                password_error.append(r)
            else:
                other_failed.append(r)

        try:
            total_reward += int(r.get('points_reward') or 0)
        except Exception:
            pass

        if r.get('has_reward') and r.get('sign_success') and r.get('sign_status') == "领取奖励成功":
            reward_count += 1

    return {
        "success_count": success_count,
        "total_reward": total_reward,
        "reward_count": reward_count,
        "password_error": password_error,
        "banned_accounts": banned_accounts,
        "other_failed": other_failed,
    }

def print_summary(all_results, total_accounts):
    global in_summary
    in_summary = True
    log("=" * 70)
    log("📊 签到任务总结")
    log("=" * 70)

    summary = summarize_results(all_results)
    success_count = summary["success_count"]
    reward_count = summary["reward_count"]
    password_error = summary["password_error"]
    banned_accounts = summary["banned_accounts"]
    other_failed = summary["other_failed"]

    log("📈 总体统计:")
    log(f"  ├── 总账号数: {total_accounts}")
    log(f"  ├── 签到成功: {success_count}/{total_accounts}")

    success_rate = (success_count / total_accounts) * 100 if total_accounts > 0 else 0
    log(f"  └── 签到成功率: {success_rate:.1f}%")

    if reward_count > 0:
        log(f"  ✅ 有额外奖励账号数: {reward_count}")
    if banned_accounts:
        labels = [masked_label(r) for r in banned_accounts]
        log(f"  ℹ️ 封禁跳过签到账号: {', '.join(labels)}")

    if not password_error and not other_failed:
        log("  🎉 所有账户签到正常!")
    else:
        if password_error:
            labels = [masked_label(r) for r in password_error]
            log(f"  ⚠️ 密码错误账号: {', '.join(labels)}")
        if other_failed:
            labels = [masked_label(r) for r in other_failed]
            log(f"  ⚠️ 签到失败账号: {', '.join(labels)}")

    log("=" * 70)

def should_notify(failed_exists):
    mode = os.getenv('NOTIFY_ON', 'always').strip().lower()
    if mode in ('never', 'none', 'off', 'false', '0'):
        return False
    if mode in ('failure', 'fail', 'error', 'errors'):
        return failed_exists
    return True

def write_results_json(path, all_results, total_accounts):
    try:
        sanitized = []
        group_name = os.getenv('GROUP_NAME', '') or os.getenv('BATCH_NAME', '')
        group_number = safe_int(os.getenv('GROUP_NUMBER'), 0)
        execution_order = safe_int(os.getenv('EXECUTION_ORDER'), 0)
        execution = execution_context()
        for r in all_results:
            sanitized.append({
                "account_index": r.get("account_index"),
                "execution_order": execution_order or r.get("account_index"),
                "group_name": group_name,
                "group_number": group_number,
                "group_position": (
                    f"{execution['group_code']}账号{r.get('account_index')}"
                    if execution['group_code']
                    else (f"{group_number}组账号{r.get('account_index')}" if group_number > 0 else f"账号{r.get('account_index')}")
                ),
                "sign_success": r.get("sign_success"),
                "sign_status": r.get("sign_status"),
                "initial_points": r.get("initial_points"),
                "final_points": r.get("final_points"),
                "points_reward": r.get("points_reward"),
                "has_reward": r.get("has_reward"),
                "token_extracted": r.get("token_extracted"),
                "secretkey_extracted": r.get("secretkey_extracted"),
                "password_error": r.get("password_error"),
                "risk_controlled": r.get("risk_controlled"),
                "banned_account": r.get("banned_account"),
                "points_fetch_success": r.get("points_fetch_success"),
                "activity_fetch_success": r.get("activity_fetch_success"),
                "account_data_required": r.get("account_data_required"),
                "account_data_fetch_success": r.get("account_data_fetch_success"),
                "account_data": r.get("account_data") or empty_account_data(),
                "data_fetch_completed": r.get("data_fetch_completed"),
                "next_day_success": r.get("next_day_success"),
                "task_start_date": r.get("task_start_date"),
                "sign_completed_at": r.get("sign_completed_at"),
                "retry_count": r.get("retry_count"),
                "is_final_retry": r.get("is_final_retry"),
                "detail_reason": r.get("detail_reason"),
                "sign_time": r.get("sign_time"),
                "sign_ip": r.get("sign_ip"),
                "activity_records": r.get("activity_records") or make_empty_extra_records(),
                "listing_gift_required": r.get("listing_gift_required"),
                "listing_gift_success": r.get("listing_gift_success"),
                "listing_gift_attempted": r.get("listing_gift_attempted"),
                "listing_gift_status": r.get("listing_gift_status"),
                "listing_gift_time": r.get("listing_gift_time"),
                "listing_gift_detail": r.get("listing_gift_detail"),
                "vote_required": r.get("vote_required"),
                "vote_success": r.get("vote_success"),
                "vote_attempted": r.get("vote_attempted"),
                "vote_status": r.get("vote_status"),
                "vote_time": r.get("vote_time"),
                "vote_product_sku": r.get("vote_product_sku"),
                "vote_product_name": r.get("vote_product_name"),
                "vote_detail": r.get("vote_detail"),
                "component_status": component_status(r),
                "group_code": r.get("group_code") or execution["group_code"],
                "account_category": r.get("account_category") or execution["account_category"],
                "execution_mode": r.get("execution_mode") or execution["execution_mode"],
                "sign_skipped": truthy(r.get("sign_skipped", execution["sign_skipped"])),
            })

        payload = {
            "generated_at": datetime.now().isoformat(),
            "batch_name": os.getenv('BATCH_NAME', ''),
            "group_name": group_name,
            "group_number": group_number,
            "group_code": execution["group_code"],
            "account_category": execution["account_category"],
            "execution_mode": execution["execution_mode"],
            "task_start_date": normalize_task_start_date(),
            "total_accounts": total_accounts,
            "results": sanitized,
        }
        dir_name = os.path.dirname(path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        log(f"结果已写入: {path}")
    except Exception as e:
        log(f"写入结果失败: {e}")

def main():
    if len(sys.argv) < 3:
        credential_file = str(os.getenv("ACCOUNT_CREDENTIAL_FILE") or "").strip()
        if credential_file:
            try:
                values = Path(credential_file).read_text(encoding="utf-8").splitlines()
                if len(values) >= 2 and values[0].strip() and values[1].strip():
                    sys.argv.extend([values[0].strip(), values[1].strip(), "false"])
            except Exception:
                pass
        if len(sys.argv) < 3:
            print("用法: python script.py \"账号1,账号2\" \"密码1,密码2\" [失败退出标志]")
            sys.exit(1)

    usernames = [u.strip() for u in sys.argv[1].split(',') if u.strip()]
    passwords = [p.strip() for p in sys.argv[2].split(',') if p.strip()]
    enable_failure_exit = len(sys.argv) >= 4 and sys.argv[3].lower() == 'true'

    log(f"失败退出功能: {'开启' if enable_failure_exit else '关闭'}")
    log(f"脚本版本: {SCRIPT_VERSION}")
    if len(usernames) != len(passwords):
        log("❌ 账号与密码数量不匹配!")
        sys.exit(1)

    total = len(usernames)
    log(f"总计 {total} 个账号")

    index_base = 1
    env_index = os.getenv('ACCOUNT_INDEX')
    if env_index:
        try:
            index_base = int(env_index)
        except ValueError:
            log(f"⚠️ ACCOUNT_INDEX 无效: {env_index}，已使用 1")
            index_base = 1

    all_results = []
    for offset, (u, p) in enumerate(zip(usernames, passwords)):
        account_index = index_base + offset
        res = process_single_account(u, p, account_index, total)
        all_results.append(res)
        if offset < total - 1:
            time.sleep(random.uniform(5, 10))

    if any(should_retry(r) for r in all_results):
        all_results = final_retry(all_results, usernames, passwords, total)

    print_summary(all_results, total)

    result_json_path = os.getenv('RESULT_JSON_PATH')
    if result_json_path:
        write_results_json(result_json_path, all_results, total)

    failed_exists = (
        any(not r['sign_success'] and not r.get('sign_skipped') and not r.get('banned_account') and not r.get('password_error') for r in all_results)
        or any(r.get('password_error') for r in all_results)
        or any(r.get('banned_account') and not truthy(r.get('data_fetch_completed')) for r in all_results)
        or any(r.get('sign_skipped') and should_retry(r) for r in all_results)
    )
    if enable_failure_exit and failed_exists:
        log("❌ 存在失败账号，退出码设为1")
        sys.exit(1)

    log("✅ 程序正常结束")
    sys.exit(0)

if __name__ == "__main__":
    main()
