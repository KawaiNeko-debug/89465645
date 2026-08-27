import os
import re
import time
from copy import deepcopy
from datetime import date
from urllib.parse import urlparse


MEMBER_API_BASE = (os.getenv("MEMBER_API_BASE") or "").strip().rstrip("/")
MEMBER_ENTRY_PATH = "/integrated/content/couponList"
INVOICE_STATISTICS_PATH = "/api/integrated/vatInvoiceInfo/selectStatisticsMoney"
INVOICE_ORDERS_PATH = "/api/integrated/vatInvoiceInfo/noticeInvoiceInit"
INVOICE_PROFILE_PATHS = (
    "/api/integrated/vatInvoiceInfo/selectInvoiceInfo/plaintext",
    "/api/integrated/customerInvoiceInfo/group/list",
)
COUPON_PATH = "/api/integrated/customerOrderCenter/getEffectiveCouponsList"
LEGACY_COUPON_PATH = "/api/integrated/customerOrderCenter/LCCoupons"
PCB_ORDER_TYPES = {1, 2}
PCB_SPEND_THRESHOLD = 40.0

COUPON_STATUS_CONFIG = {
    "unused": {"label": "未使用", "sort_status": 2, "legacy_status": "no"},
    "used": {"label": "已使用", "sort_status": 4, "legacy_status": "yes"},
    "expired": {"label": "已过期", "sort_status": 5, "legacy_status": "expiration"},
}


def empty_account_data() -> dict:
    return {
        "fetch_success": False,
        "invoice_fetch_success": False,
        "coupon_fetch_success": False,
        "invoice_profile_status": "数据不足",
        "invoice_profile_exists": None,
        "invoice_month_threshold": 12,
        "invoice_within_months_amount": None,
        "invoice_over_months_amount": None,
        "pcb_order_fetch_success": False,
        "pcb_within_months_amount": None,
        "pcb_over_months_amount": None,
        "pcb_total_amount": None,
        "pcb_amount_shortfall": None,
        "pcb_order_count": 0,
        "coupons": {"unused": [], "used": [], "expired": []},
        "coupon_prediction": "数据不足",
        "prediction_reason": "会员资料接口未完成",
        "error": "",
    }


def member_environment_error(base=MEMBER_API_BASE) -> str:
    if not base:
        return "缺少会员接口基础地址环境变量 MEMBER_API_BASE"
    parsed = urlparse(base)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path not in ("", "/"):
        return "MEMBER_API_BASE 必须是仅包含 HTTPS scheme 和 host 的基础地址"
    return ""


def safe_float(value, default=None):
    if value is None or value == "":
        return default
    try:
        return float(str(value).replace(",", "").strip())
    except Exception:
        return default


def safe_int(value, default=0) -> int:
    try:
        return int(float(str(value).strip()))
    except Exception:
        return default


def response_succeeded(response) -> bool:
    if isinstance(response, list):
        return True
    if not isinstance(response, dict):
        return False
    if response.get("__http_ok") is False or response.get("success") is False:
        return False
    code = response.get("code", response.get("status"))
    if code not in (None, ""):
        try:
            if int(code) not in (0, 200):
                return False
        except Exception:
            if str(code).strip().lower() not in {"ok", "success"}:
                return False
    return True


def unwrap_data(value):
    current = value
    for _ in range(4):
        if not isinstance(current, dict):
            break
        next_value = next(
            (current.get(key) for key in ("data", "body", "result") if current.get(key) is not None),
            None,
        )
        if next_value is None or next_value is current:
            break
        current = next_value
    return current


def find_first_value(value, keys: set[str]):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys and item not in (None, ""):
                return item
        for item in value.values():
            found = find_first_value(item, keys)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_first_value(item, keys)
            if found not in (None, ""):
                return found
    return None


def extract_lists(value) -> list[list]:
    lists = []
    if isinstance(value, list):
        lists.append(value)
    elif isinstance(value, dict):
        for key in ("records", "list", "rows", "items", "couponList", "dataList", "root"):
            candidate = value.get(key)
            if isinstance(candidate, list):
                lists.append(candidate)
        for key in ("data", "body", "result", "page"):
            candidate = value.get(key)
            if isinstance(candidate, (dict, list)):
                lists.extend(extract_lists(candidate))
    return lists


def parse_invoice_statistics(response) -> dict:
    threshold = safe_int(
        find_first_value(response, {"overTimeNum", "overtimeNum", "monthThreshold"}), 12
    ) or 12
    within = safe_float(
        find_first_value(
            response,
            {
                "vatOrdinaryInvoiceMoney",
                "vatSpecialInvoiceMoney",
                "withinInvoiceMoney",
                "availableInvoiceMoney",
            },
        )
    )
    over = safe_float(
        find_first_value(
            response,
            {"overTimeElecInvoiceMoney", "overTimeInvoiceMoney", "overtimeInvoiceMoney"},
        )
    )
    return {"month_threshold": threshold, "within_amount": within, "over_amount": over}


def parse_invoice_order_page(response) -> dict:
    """Return page metadata and PCB-only order rows from the invoice detail API."""
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, dict):
        return {"page_num": 1, "total_pages": 1, "orders": []}
    raw_rows = data.get("data")
    orders = []
    for item in raw_rows if isinstance(raw_rows, list) else []:
        if not isinstance(item, dict) or safe_int(item.get("businessOrderType"), 0) not in PCB_ORDER_TYPES:
            continue
        amount = safe_float(item.get("invoiceMoney"))
        if amount is None:
            amount = safe_float(item.get("orderMoney"))
        if amount is None:
            continue
        orders.append(
            {
                "amount": amount,
                "business_order_type": safe_int(item.get("businessOrderType"), 0),
                "business_order_code": _text(item, "businessOrderCode"),
                "detail_id": _text(item, "vatDetailsRecordAccessId"),
                "order_date": _text(item, "orderDate"),
                "model": _text(item, "specificationModel"),
            }
        )
    return {
        "page_num": max(1, safe_int(data.get("pageNum"), 1)),
        "total_pages": max(1, safe_int(data.get("totalPages"), 1)),
        "orders": orders,
    }


def sum_pcb_invoice_orders(responses: list[dict]) -> tuple[float, int]:
    total = 0.0
    count = 0
    seen = set()
    for response in responses:
        for item in parse_invoice_order_page(response)["orders"]:
            identity = (
                item["detail_id"],
                item["business_order_code"],
                item["order_date"],
                item["model"],
                item["amount"],
            )
            if identity in seen:
                continue
            seen.add(identity)
            total += item["amount"]
            count += 1
    return round(total, 2), count


PROFILE_SIGNAL_KEYS = {
    "customerInvoiceInfoId",
    "invoiceTitle",
    "invoiceHead",
    "taxpayerIdentificationNumber",
    "taxNumber",
    "companyName",
    "vatCompanyName",
    "vatTaxCode",
    "vatAddress",
    "vatTelephone",
    "vatBank",
    "vatAccountNumber",
}


def _has_profile_signal(value) -> bool:
    if isinstance(value, dict):
        if any(key in PROFILE_SIGNAL_KEYS and item not in (None, "", [], {}) for key, item in value.items()):
            return True
        for key in ("customerInvoiceInfoVO", "customerInvoiceInfo", "invoiceInfo"):
            candidate = value.get(key)
            if isinstance(candidate, dict) and _has_profile_signal(candidate):
                return True
        return any(_has_profile_signal(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_profile_signal(item) for item in value)
    return False


def parse_invoice_profile_exists(responses: list[dict]) -> bool:
    return any(_has_profile_signal(unwrap_data(response)) for response in responses)


def _text(item: dict, *keys) -> str:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def normalize_coupon(item: dict, status_key: str) -> dict:
    source = {}
    for key in ("operateCoupon", "historyCoupon", "coupon"):
        nested = item.get(key)
        if isinstance(nested, dict):
            source.update(nested)
    source.update(item)
    rule_parts = []
    for key in ("couponRule", "ruleText", "useRule", "couponDesc", "description", "remark", "limitDesc"):
        value = source.get(key)
        if value not in (None, ""):
            rule_parts.append(str(value).strip())
    return {
        "name": _text(source, "couponName", "name", "couponTitle", "title", "displayName", "couponDesc") or "未命名优惠券",
        "business_type": _text(source, "businessType", "bizType", "businessName", "businessLine", "productType", "useScope"),
        "valid_from": _text(
            source, "effectiveTime", "startDate", "startTime", "validStartTime", "beginTime", "createDate"
        ),
        "expires_at": _text(
            source, "expirationTime", "endDate", "expireTime", "endTime", "validEndTime", "invalidTime"
        ),
        "status": COUPON_STATUS_CONFIG[status_key]["label"],
        "status_key": status_key,
        "rule_text": "；".join(dict.fromkeys(part for part in rule_parts if part)),
        "target_url": _text(source, "targetUrl", "jumpUrl", "url", "useUrl"),
        "coupon_id": _text(source, "couponId", "customerCouponId", "id", "couponCode"),
    }


def parse_coupon_response(response, status_key: str) -> list[dict]:
    rows = []
    seen = set()
    for candidate in extract_lists(response):
        for item in candidate:
            if not isinstance(item, dict):
                continue
            coupon = normalize_coupon(item, status_key)
            identity = (coupon["coupon_id"], coupon["name"], coupon["expires_at"], coupon["status_key"])
            if identity in seen:
                continue
            seen.add(identity)
            rows.append(coupon)
    return rows


def merge_coupons(*groups: list[dict]) -> list[dict]:
    merged = []
    seen = set()
    for group in groups:
        for coupon in group or []:
            identity = (
                str(coupon.get("coupon_id") or ""),
                str(coupon.get("name") or ""),
                str(coupon.get("expires_at") or ""),
                str(coupon.get("status_key") or ""),
            )
            if identity in seen:
                continue
            seen.add(identity)
            merged.append(coupon)
    return merged


def is_pcb_smt_coupon(coupon: dict) -> bool:
    text = " ".join(
        str(coupon.get(key) or "") for key in ("name", "business_type", "rule_text", "target_url")
    ).upper()
    text = re.sub(r"\s+", "", text)
    return "PCB" in text and "SMT" in text


def predict_pcb_smt(account_data: dict) -> tuple[str, str]:
    if (
        not account_data.get("invoice_fetch_success")
        or not account_data.get("coupon_fetch_success")
        or not account_data.get("pcb_order_fetch_success")
    ):
        return "数据不足", "开票或优惠券接口未完整返回"
    if account_data.get("invoice_profile_exists") is False:
        return "不可能", "无开票资料，无法形成有效 PCB 消费证据"
    total = safe_float(account_data.get("pcb_total_amount"), 0.0) or 0.0
    if total < PCB_SPEND_THRESHOLD:
        shortfall = round(max(0.0, PCB_SPEND_THRESHOLD - total), 2)
        return "不可能", f"样板/小批量 PCB 累计消费 {total:g} 元，距离 40 元还差 {shortfall:g} 元"
    coupons = account_data.get("coupons") or {}
    if any(is_pcb_smt_coupon(item) for key in ("unused", "used") for item in coupons.get(key, [])):
        return "很小可能", f"PCB 累计消费 {total:g} 元；未使用或已使用列表中出现过 PCB+SMT 优惠券"
    if any(is_pcb_smt_coupon(item) for item in coupons.get("expired", [])):
        return "很大可能", f"PCB 累计消费 {total:g} 元；仅已过期列表中出现过 PCB+SMT 优惠券"
    return "100%可能", f"PCB 累计消费 {total:g} 元，全部优惠券状态中未出现过 PCB+SMT 优惠券"


def browser_fetch_json(page, method: str, url: str, payload=None, form_encoded: bool = False):
    result = page.evaluate(
        """async ({url, method, payload, formEncoded}) => {
            const upperMethod = String(method || "POST").toUpperCase();
            let target = url;
            const options = {method: upperMethod, credentials: "include", headers: {accept: "application/json, text/plain, */*"}};
            if (upperMethod === "GET") {
                const params = new URLSearchParams(payload || {});
                if ([...params].length) target += `${target.includes("?") ? "&" : "?"}${params.toString()}`;
            } else if (payload !== null && payload !== undefined) {
                if (formEncoded) {
                    options.headers["content-type"] = "application/x-www-form-urlencoded";
                    options.body = new URLSearchParams(payload).toString();
                } else {
                    options.headers["content-type"] = "application/json;charset=UTF-8";
                    options.body = JSON.stringify(payload);
                }
            }
            const response = await fetch(target, options);
            const text = await response.text();
            let data = null;
            try { data = JSON.parse(text); } catch (error) {}
            if (data && typeof data === "object" && !Array.isArray(data)) data.__http_ok = response.ok;
            return {status: response.status, ok: response.ok, data};
        }""",
        {"url": url, "method": method, "payload": payload, "formEncoded": form_encoded},
    )
    if not isinstance(result, dict) or not result.get("ok"):
        return None
    data = result.get("data")
    if isinstance(data, dict):
        data.setdefault("__http_ok", True)
    return data


class AccountDataCollector:
    def __init__(self, page, account_index: int, logger=print, base=MEMBER_API_BASE):
        self.page = page
        self.account_index = account_index
        self.log = logger
        self.base = str(base or "").strip().rstrip("/")

    def _request_with_retry(
        self, method: str, path: str, payload=None, tag: str = "接口", form_encoded: bool = False
    ):
        for attempt in range(1, 4):
            try:
                response = browser_fetch_json(
                    self.page, method, f"{self.base}{path}", payload, form_encoded=form_encoded
                )
                if response_succeeded(response):
                    return response
            except Exception as exc:
                if attempt == 3:
                    self.log(f"账号{self.account_index} - {tag}请求异常: {type(exc).__name__}")
            if attempt < 3:
                time.sleep(0.6 * attempt)
        self.log(f"账号{self.account_index} - {tag}重试后仍未成功")
        return None

    def _open_member_session(self) -> bool:
        try:
            self.page.goto(f"{self.base}{MEMBER_ENTRY_PATH}", wait_until="domcontentloaded", timeout=60000)
            self.page.wait_for_timeout(1500)
            current_origin = self.page.evaluate("location.origin")
            return str(current_origin or "").rstrip("/") == self.base
        except Exception as exc:
            self.log(f"账号{self.account_index} - 会员中心 SSO 页面打开失败: {type(exc).__name__}")
            return False

    def _fetch_pcb_orders(self, profile_responses: list[dict]) -> tuple[bool, float, float, int]:
        company_name = find_first_value(profile_responses, {"vatCompanyName", "companyName", "invoiceTitle"})
        organization = find_first_value(profile_responses, {"invoiceOrganization"})
        today = date.today()
        try:
            begin = today.replace(year=today.year - 2)
        except ValueError:
            begin = today.replace(year=today.year - 2, day=28)
        common = {
            "orderBeginTime": begin.isoformat(),
            "orderEndTime": today.isoformat(),
            "businessOrderType": None,
            "businessOrderCode": None,
            "pageSize": 1000,
            "vatCompanyName": company_name,
            "invoiceOrganization": organization,
            "invoiceType": "1",
            "invoiceFlag": 1,
            "subAccountIds": None,
        }
        totals = {}
        order_count = 0
        for time_mark in (1, 2):
            page_number = 1
            responses = []
            while True:
                response = self._request_with_retry(
                    "POST",
                    INVOICE_ORDERS_PATH,
                    {**common, "timeMark": time_mark, "pageNum": page_number},
                    f"PCB开票订单（分区{time_mark}第{page_number}页）",
                )
                if response is None:
                    return False, 0.0, 0.0, 0
                responses.append(response)
                page = parse_invoice_order_page(response)
                if page_number >= page["total_pages"]:
                    break
                page_number += 1
            amount, count = sum_pcb_invoice_orders(responses)
            totals[time_mark] = amount
            order_count += count
        return True, totals.get(1, 0.0), totals.get(2, 0.0), order_count

    def collect(self, previous=None, components=None) -> dict:
        previous = previous if isinstance(previous, dict) else {}
        requested = set(components or ("invoice", "pcb_orders", "coupons"))
        result = empty_account_data()
        result.update(deepcopy(previous))
        environment_error = member_environment_error(self.base)
        if environment_error:
            result["error"] = environment_error
            return result
        if not self._open_member_session():
            result["error"] = "会员中心 SSO 未建立"
            return result

        statistics = None
        profile_responses = []
        profile_request_successes = []
        if requested.intersection({"invoice", "pcb_orders"}):
            for path in INVOICE_PROFILE_PATHS:
                response = self._request_with_retry("POST", path, None, "开票资料")
                if response is not None:
                    profile_request_successes.append(True)
                    profile_responses.append(response)
                    break
        if "invoice" in requested:
            statistics = self._request_with_retry(
                "POST",
                INVOICE_STATISTICS_PATH,
                {"invoiceType": "2"},
                "开票金额",
                form_encoded=True,
            )

        # The profile response carries the two available-amount fields. The
        # statistics call is supplemental and must not invalidate usable data.
        invoice_success = result.get("invoice_fetch_success", False)
        if "invoice" in requested:
            invoice_success = any(profile_request_successes)
            result["invoice_fetch_success"] = invoice_success
        if "invoice" in requested and invoice_success:
            parsed = parse_invoice_statistics([statistics, *profile_responses])
            profile_exists = parse_invoice_profile_exists(profile_responses)
            result.update({
                "invoice_profile_status": "有" if profile_exists else "无",
                "invoice_profile_exists": profile_exists,
                "invoice_month_threshold": parsed["month_threshold"],
                "invoice_within_months_amount": parsed["within_amount"] if profile_exists else None,
                "invoice_over_months_amount": parsed["over_amount"] if profile_exists else None,
            })
            if profile_exists and "pcb_orders" in requested:
                pcb_success, pcb_within, pcb_over, pcb_order_count = self._fetch_pcb_orders(profile_responses)
                pcb_total = round(pcb_within + pcb_over, 2) if pcb_success else None
                result.update({
                    "pcb_order_fetch_success": pcb_success,
                    "pcb_within_months_amount": pcb_within if pcb_success else None,
                    "pcb_over_months_amount": pcb_over if pcb_success else None,
                    "pcb_total_amount": pcb_total,
                    "pcb_amount_shortfall": round(max(0.0, PCB_SPEND_THRESHOLD - pcb_total), 2) if pcb_total is not None else None,
                    "pcb_order_count": pcb_order_count if pcb_success else 0,
                })
            elif not profile_exists:
                result["pcb_order_fetch_success"] = True

        if "pcb_orders" in requested and result.get("invoice_profile_exists") is True:
            if not profile_responses:
                result["pcb_order_fetch_success"] = False
            elif "invoice" not in requested:
                pcb_success, pcb_within, pcb_over, pcb_order_count = self._fetch_pcb_orders(profile_responses)
                pcb_total = round(pcb_within + pcb_over, 2) if pcb_success else None
                result.update({
                    "pcb_order_fetch_success": pcb_success,
                    "pcb_within_months_amount": pcb_within if pcb_success else None,
                    "pcb_over_months_amount": pcb_over if pcb_success else None,
                    "pcb_total_amount": pcb_total,
                    "pcb_amount_shortfall": round(max(0.0, PCB_SPEND_THRESHOLD - pcb_total), 2) if pcb_total is not None else None,
                    "pcb_order_count": pcb_order_count if pcb_success else 0,
                })

        coupon_success = result.get("coupon_fetch_success", False)
        if "coupons" in requested:
            coupon_success = True
            coupons = {"unused": [], "used": [], "expired": []}
            for status_key, config in COUPON_STATUS_CONFIG.items():
                common = {"pageNum": 1, "pageSize": 1000}
                current = self._request_with_retry(
                    "POST", COUPON_PATH, {**common, "sortStatus": config["sort_status"]}, f"{config['label']}优惠券"
                )
                legacy = self._request_with_retry(
                    "POST", LEGACY_COUPON_PATH, {**common, "couponUseStatus": config["legacy_status"]}, f"{config['label']}旧版优惠券"
                )
                if current is None and legacy is None:
                    coupon_success = False
                    continue
                coupons[status_key] = merge_coupons(
                    parse_coupon_response(current, status_key) if current is not None else [],
                    parse_coupon_response(legacy, status_key) if legacy is not None else [],
                )
            result["coupons"] = coupons
            result["coupon_fetch_success"] = coupon_success
        result["fetch_success"] = invoice_success and coupon_success and result["pcb_order_fetch_success"]
        result["coupon_prediction"], result["prediction_reason"] = predict_pcb_smt(result)
        if not result["fetch_success"]:
            result["error"] = "会员资料接口未完整返回"
        return deepcopy(result)
