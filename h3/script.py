import os
import sys
import time
import random
import json
import requests
import smtplib
import threading
import re
from email.message import EmailMessage
from datetime import datetime
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeoutError
from fake_useragent import UserAgent

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

def truthy(v) -> bool:
    if v is True:
        return True
    if v is False or v is None:
        return False
    if isinstance(v, (int, float)):
        return v != 0
    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "y", "on")

def safe_int(v, default=0) -> int:
    try:
        return int(str(v).strip())
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
    # 避免在 public Actions log 里泄露 UUID/token（适度脱敏，不影响看错误页面主体）
    if not s:
        return ""
    return _UUID_RE.sub(lambda m: m.group(0)[:8] + "-****-****-****-" + m.group(0)[-12:], s)

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

def log(msg):
    full_msg = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(full_msg, flush=True)
    if in_summary:
        summary_logs.append(msg)

def mask_account(account):
    if account is None:
        return ""
    s = str(account)
    if len(s) <= 4:
        return "*" * len(s)
    return s[:-4] + "****"

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
# API 客户端
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

        self._last_sign_day = 0  # 由配置接口解析出的“今天第几天”

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

    def request_json(self, url, method='GET', dump_body_on_error=False, tag="API"):
        method = method.upper().strip()
        try:
            resp = requests.request(method, url, headers=self.headers, timeout=12)

            if resp.status_code != 200:
                allow = resp.headers.get("Allow") or resp.headers.get("allow") or ""
                msg = f"账号{self.account_index} - {tag}请求失败 {resp.status_code} ({method} {url})"
                if allow:
                    msg += f" Allow={allow}"
                log(msg)

                if dump_body_on_error:
                    body = redact_sensitive(truncate_text(resp.text, 2000))
                    log(f"账号{self.account_index} - {tag}响应内容: {body}")
                return None

            try:
                return resp.json()
            except Exception:
                # 200 但不是 json（或被网关返回奇怪内容）
                log(f"账号{self.account_index} - {tag}响应JSON解析失败 (200 {method} {url})")
                if dump_body_on_error:
                    body = redact_sensitive(truncate_text(resp.text, 2000))
                    log(f"账号{self.account_index} - {tag}响应内容: {body}")
                return None

        except Exception as e:
            log(f"账号{self.account_index} - {tag}异常: {e}")
            return None

    @with_retry
    def get_points(self):
        data = self.request_json(f"{self.base_url}/api/activity/front/getCustomerIntegral", tag="积分", dump_body_on_error=False)
        if data and data.get('success'):
            return data.get('data', {}).get('integralVoucher', 0)

        # token 可能失效，尝试刷新后再返回 None（由 with_retry 重试）
        self._refresh_token()
        return None

    def _parse_today_day(self, data: dict) -> int:
        """
        尽量兼容不同字段名/结构：
        - 直接字段：todayDay / signInDay / currentDay / dayNum ...
        - 列表：signInConfigList / signInConfigs ... 找 isToday/isCurrent/today/current
        - 连续签到：continueSignInDay / signInCount ... 推算今天
        """
        if not isinstance(data, dict):
            return 0
        d = data.get("data") or {}
        if not isinstance(d, dict):
            return 0

        # 1) 直接字段
        direct_keys = [
            "todayDay", "todaySignInDay", "signInDay", "currentDay",
            "currentSignInDay", "day", "dayNum", "signDay", "currentSignDay"
        ]
        for k in direct_keys:
            if k in d and d.get(k) is not None:
                day = safe_int(d.get(k), 0)
                if day > 0:
                    return day

        # 2) 列表结构
        list_keys = [
            "signInConfigList", "signInConfigs", "configList", "configs",
            "signInList", "signInDetailList", "signInConfigDtoList"
        ]
        for lk in list_keys:
            lst = d.get(lk)
            if not isinstance(lst, list):
                continue

            # 2.1 找到“今天/当前”那条
            for item in lst:
                if not isinstance(item, dict):
                    continue
                if truthy(item.get("today")) or truthy(item.get("isToday")) or truthy(item.get("current")) or truthy(item.get("isCurrent")):
                    for dk in ("day", "dayNum", "signInDay", "index", "sort", "seq"):
                        day = safe_int(item.get(dk), 0)
                        if day > 0:
                            return day

            # 2.2 兜底：按“已签到条目数”推算
            signed_cnt = 0
            for item in lst:
                if not isinstance(item, dict):
                    continue
                if truthy(item.get("haveSignIn")) or truthy(item.get("signed")) or truthy(item.get("isSignIn")) or truthy(item.get("haveReceive")):
                    signed_cnt += 1
            if signed_cnt > 0:
                have_signed_today = truthy(d.get("haveSignIn")) or truthy(d.get("haveSign"))
                return min(7, signed_cnt if have_signed_today else signed_cnt + 1)

        # 3) 连续签到字段推算
        for k in ("continueSignInDay", "continueSignDay", "continuousDay", "continueDay", "seriesDay", "signedDays", "signInCount"):
            if k in d and d.get(k) is not None:
                cnt = safe_int(d.get(k), 0)
                if cnt > 0:
                    have_signed_today = truthy(d.get("haveSignIn")) or truthy(d.get("haveSign"))
                    return min(7, cnt if have_signed_today else cnt + 1)

        return 0

    def get_sign_config(self):
        """
        返回 (have_signed_today: bool, today_day: int, raw_data: dict) 或 None
        """
        url = f"{self.base_url}{SIGN_CONFIG_PATH}"
        data = self.request_json(url, method="GET", tag="签到配置", dump_body_on_error=True)
        if not (data and data.get("success")):
            # 尝试刷新 token 再来一次
            self._refresh_token()
            data = self.request_json(url, method="GET", tag="签到配置", dump_body_on_error=True)

        if not (data and data.get("success")):
            return None

        raw = data.get("data") or {}
        have_signed = False
        if isinstance(raw, dict):
            have_signed = truthy(raw.get("haveSignIn")) or truthy(raw.get("haveSign"))

        today_day = self._parse_today_day(data)
        self._last_sign_day = today_day

        if today_day > 0:
            log(f"账号{self.account_index} - 📅 签到配置解析：今天第 {today_day} 天，haveSignIn={have_signed}")

        return have_signed, today_day, data

    def receive_voucher(self):
        """
        领取额外奖励：
        成功时 data 通常是豆子数量（例如 8）
        返回 (ok: bool, beans: int)
        """
        url = f"{self.base_url}{RECEIVE_VOUCHER_PATH}"
        data = self.request_json(url, method="POST", tag="领取奖励", dump_body_on_error=True)
        if data and data.get("success"):
            beans = safe_int(data.get("data"), 0)
            log(f"账号{self.account_index} - ✅ 奖励领取成功（+{beans} 豆子）")
            return True, beans

        log(f"账号{self.account_index} - ❌ 奖励领取失败")
        return False, 0

    def sign_in(self):
        """
        正常签到：优先 GET，失败再 POST。
        ✅ 关键改造：当返回非 200（例如 405）时，打印服务器响应内容到 log。
        """
        url = f"{self.base_url}{API_SIGN_PATH}"

        log(f"账号{self.account_index} - 尝试使用 GET 方法签到...")
        data = self.request_json(url, method='GET', tag="签到", dump_body_on_error=True)
        if data and data.get('success'):
            log(f"账号{self.account_index} - ✅ 签到成功")
            self.sign_status = "签到成功"
            return True

        log(f"账号{self.account_index} - GET 失败，尝试 POST...")
        data = self.request_json(url, method='POST', tag="签到", dump_body_on_error=True)
        if data and data.get('success'):
            log(f"账号{self.account_index} - ✅ 签到成功")
            self.sign_status = "签到成功"
            return True

        msg = data.get('message', '未知错误') if isinstance(data, dict) else '请求失败'
        log(f"账号{self.account_index} - ❌ 签到失败: {msg}")
        self.sign_status = "签到失败"
        return False

    def execute_full_process(self):
        time.sleep(random.uniform(1, 2))
        self.initial_points = self.get_points() or 0
        time.sleep(random.uniform(1, 2))

        cfg = self.get_sign_config()
        if cfg is None:
            self.sign_status = "检查失败"
            return False

        have_signed, today_day, _raw = cfg

        # ✅ 第 7 天：直接尝试领取额外奖励；领取成功就算已签到，不走正常签到
        if today_day == 7:
            log(f"账号{self.account_index} - 🎁 检测到今天为第 7 天，直接领取额外奖励（不走正常签到）")
            ok, _beans = self.receive_voucher()
            if ok:
                self.has_reward = True
                self.sign_status = "领取奖励成功"  # 保持与汇总统计一致
                time.sleep(random.uniform(1, 2))
                self.final_points = self.get_points() or self.initial_points
                self.points_reward = self.final_points - self.initial_points
                return True

            # 领取失败就兜底：如果实际上已经签过，就当成功；否则走正常签到
            if have_signed:
                self.sign_status = "已签到过"
                time.sleep(random.uniform(1, 2))
                self.final_points = self.get_points() or self.initial_points
                self.points_reward = self.final_points - self.initial_points
                return True

            log(f"账号{self.account_index} - ⚠️ 第 7 天领取奖励失败，兜底走正常签到流程")

        # 非第 7 天正常流程
        if have_signed:
            self.sign_status = "已签到过"
        else:
            time.sleep(random.uniform(2, 3))
            if not self.sign_in():
                return False

        time.sleep(random.uniform(1, 2))
        self.final_points = self.get_points() or 0
        self.points_reward = self.final_points - self.initial_points
        return True

# ==============================================================================
# 单个账号登录与签到主流程
# ==============================================================================
def sign_in_account(username, password, account_index, total_accounts, retry_count=0, is_final_retry=False):
    label = f" (重试{retry_count})" if retry_count > 0 else (" (最终重试)" if is_final_retry else "")
    log(f"开始处理账号 {account_index}/{total_accounts}{label}")

    result = {
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
        'retry_count': retry_count,
        'is_final_retry': is_final_retry,
        'password_error': False
    }

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
            try:
                page.wait_for_selector(f"xpath={password_xpath}", timeout=10000)
                log("✅ 密码框已出现")
                page.locator(f"xpath={password_xpath}").fill(password)
                log("✅ 已填写密码")
            except Exception as e:
                log(f"❌ 密码框未出现: {e}")
                try:
                    page.wait_for_selector('input[placeholder="请输入登录密码"]', timeout=5000)
                    password_inputs = page.locator('input[placeholder="请输入登录密码"]')
                    count = password_inputs.count()
                    for i in range(count):
                        if password_inputs.nth(i).is_visible():
                            password_inputs.nth(i).fill(password)
                            log("✅ 已通过备用选择器填写密码")
                            break
                    else:
                        raise Exception("没有可见的密码输入框")
                except Exception as e2:
                    log(f"❌ 备用选择器也失败: {e2}")
                    raise

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
                try:
                    page.wait_for_selector(HOME_SELECTOR, timeout=30000 - 7000)
                    home_found = True
                    log(f"账号{account_index} - ✅ 已进入首页")
                except PlaywrightTimeoutError:
                    log(f"账号{account_index} - ❌ 未检测到首页元素")
                    result['sign_status'] = '未进入首页'
                    return result
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
                log(f"账号{account_index} - 使用 token 进行签到（secretkey 非必需）")
                client = ApiClient(access_token, secretkey, account_index, page, user_agent=ua_string)
                success = client.execute_full_process()
                result.update({
                    'sign_success': success,
                    'sign_status': client.sign_status,
                    'initial_points': client.initial_points,
                    'final_points': client.final_points,
                    'points_reward': client.points_reward,
                    'has_reward': client.has_reward,
                })
            else:
                log(f"账号{account_index} - ❌ 未提取到 token")
                result['sign_status'] = 'Token提取失败'

        except Exception as e:
            log(f"账号{account_index} - ❌ 执行异常: {e}")
            result['sign_status'] = '执行异常'
            try:
                if page and page.locator("text=/密码错误/").is_visible():
                    result['password_error'] = True
            except Exception:
                pass
        finally:
            if context:
                context.close()
            if browser:
                browser.close()
            time.sleep(1)

    return result

# ==============================================================================
# 重试逻辑与结果合并
# ==============================================================================
def should_retry(res):
    if res.get('password_error'):
        return False
    return not res['sign_success']

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
        'password_error': False
    }
    max_retries = 3
    for attempt in range(max_retries + 1):
        res = sign_in_account(username, password, account_index, total_accounts, retry_count=attempt)

        if res.get('password_error'):
            merged['password_error'] = True
            merged['sign_status'] = '密码错误'
            merged['username'] = username
            merged['masked_username'] = mask_account(username)
            break

        if res['sign_success'] and not merged['sign_success']:
            for k in ['sign_success', 'sign_status', 'initial_points', 'final_points', 'points_reward', 'has_reward']:
                merged[k] = res[k]

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
                                retry_count=f['prev_retry'] + 1, is_final_retry=True)
        orig = all_results[f['index']]

        if final.get('password_error'):
            orig.update({
                'password_error': True,
                'sign_status': '密码错误',
                'username': f['username'],
                'masked_username': mask_account(f['username']),
                'is_final_retry': True
            })
            continue

        if final['sign_success'] and not orig['sign_success']:
            for k in ['sign_success', 'sign_status', 'initial_points', 'final_points', 'points_reward', 'has_reward']:
                orig[k] = final[k]

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
    other_failed = []

    for r in all_results:
        if r.get('sign_success'):
            success_count += 1
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
        "other_failed": other_failed,
    }

# ==============================================================================
# 总结与推送
# ==============================================================================
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
    other_failed = summary["other_failed"]

    log("📈 总体统计:")
    log(f"  ├── 总账号数: {total_accounts}")
    log(f"  ├── 签到成功: {success_count}/{total_accounts}")

    success_rate = (success_count / total_accounts) * 100 if total_accounts > 0 else 0
    log(f"  └── 签到成功率: {success_rate:.1f}%")

    if reward_count > 0:
        log(f"  ✅ 有额外奖励账号数: {reward_count}")

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

def parse_notify_channels():
    raw = os.getenv('NOTIFY_CHANNELS', '').strip()
    if raw:
        return [c.strip().lower() for c in raw.split(',') if c.strip()]
    channels = []
    if os.getenv('TELEGRAM_BOT_TOKEN') and os.getenv('TELEGRAM_CHAT_ID'):
        channels.append('telegram')
    if os.getenv('SMTP_HOST') and os.getenv('SMTP_TO'):
        channels.append('email')
    return channels

def send_telegram(text):
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    chat_id = os.getenv('TELEGRAM_CHAT_ID')
    if not (token and chat_id):
        log("未配置 Telegram 环境变量，跳过 Telegram 通知")
        return False
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = requests.get(url, params={'chat_id': chat_id, 'text': text}, timeout=10)
        if resp.status_code == 200:
            log("Telegram 推送成功")
            return True
        log(f"Telegram 推送失败，状态码: {resp.status_code}")
    except Exception as e:
        log(f"Telegram 推送异常: {e}")
    return False

def send_email(subject, body):
    host = os.getenv('SMTP_HOST')
    to_raw = os.getenv('SMTP_TO', '')
    if not host or not to_raw:
        log("未配置 SMTP 环境变量，跳过邮件通知")
        return False
    port = int(os.getenv('SMTP_PORT', '587'))
    user = os.getenv('SMTP_USER')
    password = os.getenv('SMTP_PASS')
    sender = os.getenv('SMTP_FROM') or user or "no-reply@example.com"
    to_list = [t.strip() for t in to_raw.split(',') if t.strip()]
    if not to_list:
        log("SMTP_TO 为空，跳过邮件通知")
        return False

    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = sender
    msg['To'] = ", ".join(to_list)
    msg.set_content(body)

    use_ssl = os.getenv('SMTP_SSL', 'false').lower() in ('1', 'true', 'yes', 'on')
    use_tls = os.getenv('SMTP_TLS', 'true').lower() in ('1', 'true', 'yes', 'on')
    try:
        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=10)
        else:
            server = smtplib.SMTP(host, port, timeout=10)
            if use_tls:
                server.starttls()
        if user and password:
            server.login(user, password)
        server.send_message(msg)
        server.quit()
        log("邮件通知发送成功")
        return True
    except Exception as e:
        log(f"邮件通知发送失败: {e}")
        return False

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
        for r in all_results:
            sanitized.append({
                "account_index": r.get("account_index"),
                "sign_success": r.get("sign_success"),
                "sign_status": r.get("sign_status"),
                "initial_points": r.get("initial_points"),
                "final_points": r.get("final_points"),
                "points_reward": r.get("points_reward"),
                "has_reward": r.get("has_reward"),
                "password_error": r.get("password_error"),
                "retry_count": r.get("retry_count"),
                "is_final_retry": r.get("is_final_retry"),
            })

        payload = {
            "generated_at": datetime.now().isoformat(),
            "batch_name": os.getenv('BATCH_NAME', ''),
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

def push_summary(all_results, total_accounts):
    today = datetime.now().strftime("%Y年%m月%d日")
    batch_name = os.getenv('BATCH_NAME', '').strip()
    header = f"{today} 📊 任务总结"
    if batch_name:
        header += f" - {batch_name}"

    summary = summarize_results(all_results)
    success_count = summary["success_count"]
    total_reward = float(summary["total_reward"])
    reward_count = summary["reward_count"]
    password_error = summary["password_error"]
    other_failed = summary["other_failed"]

    lines = []
    lines.append(header)
    lines.append("=" * 50)
    lines.append("")
    stats = [
        f"总账号数: {total_accounts}",
        f"签到成功: {success_count}/{total_accounts}",
        f"总计获得 +{total_reward:.1f} 🌽",
    ]
    if reward_count > 0:
        stats.append("有额外奖励🎁")
    success_rate = (success_count / total_accounts) * 100 if total_accounts > 0 else 0
    stats.append(f"签到成功率: {success_rate:.1f}%")

    for i, line in enumerate(stats):
        prefix = "  └── " if i == len(stats) - 1 else "  ├── "
        lines.append(prefix + line)

    if not password_error and not other_failed:
        lines.append("  🎉 所有账号签到成功!")
    else:
        lines.append("")
        lines.append("失败的账户")
        for r in password_error + other_failed:
            label = r.get('username') or f"账号序号{r.get('account_index')}"
            if r.get("password_error"):
                reason = "密码错误❌"
            elif r.get("sign_status") and "签到失败" in r.get("sign_status"):
                reason = "签到失败❗"
            else:
                reason = "未知情况❓"
            lines.append(f"{label}：{reason}")

    lines.append("=" * 50)
    full_text = "\n".join(lines)

    channels = parse_notify_channels()
    if not channels:
        log("未配置通知渠道，跳过推送")
        return
    if 'telegram' in channels:
        send_telegram(full_text)
    if 'email' in channels:
        send_email(header, full_text)

def main():
    if len(sys.argv) < 3:
        print("用法: python script.py \"账号1,账号2\" \"密码1,密码2\" [失败退出标志]")
        print("示例: python script.py 13800138000 mypassword false")
        print("      python script.py \"user1,user2\" \"pass1,pass2\" true")
        sys.exit(1)

    usernames = [u.strip() for u in sys.argv[1].split(',') if u.strip()]
    passwords = [p.strip() for p in sys.argv[2].split(',') if p.strip()]
    enable_failure_exit = len(sys.argv) >= 4 and sys.argv[3].lower() == 'true'

    log(f"失败退出功能: {'开启' if enable_failure_exit else '关闭'}")
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

    failed_exists = any(not r['sign_success'] and not r.get('password_error') for r in all_results) or any(r.get('password_error') for r in all_results)
    if should_notify(failed_exists):
        push_summary(all_results, total)
    else:
        log("通知已按 NOTIFY_ON 配置跳过")

    if enable_failure_exit and failed_exists:
        log("❌ 存在失败账号，退出码设为1")
        sys.exit(1)

    log("✅ 程序正常结束")
    sys.exit(0)

if __name__ == "__main__":
    main()