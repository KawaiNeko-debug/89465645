"""Thursday-only seasonal lottery with encrypted, compare-and-swap progress."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import re
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

try:
    from weekly_box_schedule import WeeklyStore, StateConflict
except ImportError:
    from h3.weekly_box_schedule import WeeklyStore, StateConflict

SHANGHAI = timezone(timedelta(hours=8))
EVENT_ID = "material-season-2026"
LIFECYCLE_PATH = f"season-lottery/{EVENT_ID}/lifecycle.json"
CONFIRMED_END = "2026-12-31T23:59:59+08:00"
ENABLED_FROM = "2026-11-10"
PAGE_PATH = "/sempage/20thAnniversary"
CONFIG_PATH = "/api/newOrder/lottery-activity/get-activity-info?activityType=10"
COUNT_PATH = "/api/cgi/operationService/front/lottery/getLuckyKeyCount"
TURN_PATH = "/api/cgi/operationService/front/lottery/turn"
CLAIM_PATH = "/api/cgi/operationService/front/lottery/receivePrize"
HISTORY_PATH = "/api/newOrder/lottery-activity/win-page-query"
GROUP_RE = re.compile(r"(?:old|wudi|new|ld|yyy|ll|zh)(?:[1-9]|1[0-9]|20)$|test$")
NO_CHANCE = ("次数不足", "没有抽奖次数", "没有抽奖机会", "暂无抽奖机会", "暂无抽奖次数", "已达上限", "已达抽奖上限", "已抽完")


def now_shanghai():
    return datetime.now(SHANGHAI)


def parse_time(value):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return result.replace(tzinfo=SHANGHAI) if result.tzinfo is None else result


def scheduled(task_date, group_code):
    if os.getenv('SEASON_LOTTERY_RETIRED') == 'true' or now_shanghai() >= parse_time(CONFIRMED_END):
        return False
    try:
        value = str(task_date)
        day = (parse_time(value).astimezone(SHANGHAI).date() if len(value) > 10
               else datetime.fromisoformat(value).date())
    except (TypeError, ValueError):
        return False
    return bool(GROUP_RE.fullmatch(str(group_code or "").lower())
                and day.weekday() == 3 and ENABLED_FROM <= day.isoformat() <= "2026-12-31")


def empty_result(task_date="", group_code=""):
    return {"required": scheduled(task_date, group_code), "complete": not scheduled(task_date, group_code),
            "task_start_date": str(task_date), "query_success": False, "initial_count": None,
            "remaining_count": None, "mode": "query_only" if str(group_code).startswith(("ll", "zh")) else "draw",
            "status": ("活动已永久停用" if os.getenv('SEASON_LOTTERY_RETIRED') == 'true' or now_shanghai() >= parse_time(CONFIRMED_END)
                       else "待检测" if scheduled(task_date, group_code) else "不适用"),
            "draws": []}


def component_complete(row):
    if not scheduled(row.get("task_start_date") or row.get("_task_start_date"),
                     row.get("group_code") or row.get("source_group") or row.get("_group_code")):
        return True
    value = row.get("season_lottery") or {}
    return bool(row.get("banned_account") or value.get("complete"))


def merge_results(a, b):
    a, b = (deepcopy(v) if isinstance(v, dict) else {} for v in (a, b))
    if a.get("task_start_date") and b.get("task_start_date") and a["task_start_date"] != b["task_start_date"]:
        return a
    selected = max((a, b), key=lambda v: (bool(v.get("complete")), bool(v.get("query_success")), len(v.get("draws") or [])))
    other = b if selected is a else a
    for field in ('draws', 'history_draws'):
        draws = {v.get("id"): deepcopy(v) for v in other.get(field, []) if v.get("id")}
        for draw in selected.get(field, []):
            old = draws.get(draw.get("id"), {})
            if draw.get("id"):
                draws[draw["id"]] = max((old, draw), key=lambda v: (
                    bool(v.get("draw_success")), all(p.get("claim_status") == "已领取" for p in v.get("prizes", [])),
                    len(v.get("prizes") or [])))
        selected[field] = list(draws.values())
    return selected


def succeeded(payload):
    return (isinstance(payload, dict) and payload.get('success') is not False
            and (payload.get("success") is True or payload.get("code") == 200))


def business_reason(payload, default="接口请求失败"):
    # Do not copy arbitrary server messages (which may echo credentials) into artifacts.
    text = str((payload or {}).get("message") or "") if isinstance(payload, dict) else ""
    for marker in (*NO_CHANCE, "活动已结束", "活动未开始", "已领取", "已发放"):
        if marker in text:
            return marker
    return default


class Lifecycle:
    def __init__(self, store):
        self.store = store
        self.state, self.sha = store.read(LIFECYCLE_PATH)
        self.state = self.state or {"event_id": EVENT_ID, "ends_at": CONFIRMED_END, "retired": False}
        if self.state.get("event_id") != EVENT_ID:
            raise ValueError("season lifecycle identity mismatch")

    def save(self):
        for _ in range(8):
            try:
                self.sha = self.store.write(LIFECYCLE_PATH, self.state, self.sha)
                return
            except StateConflict:
                current, sha = self.store.read(LIFECYCLE_PATH)
                if current and current.get('event_id') != EVENT_ID:
                    raise ValueError('season lifecycle identity mismatch')
                if current and current.get('retired'):
                    self.state, self.sha = current, sha
                    return
                if current and current.get('activity_code'):
                    if self.state.get('activity_code') not in (None, current['activity_code']):
                        raise ValueError('season activity changed')
                    self.state['activity_code'] = current['activity_code']
                    if (not self.state.get('retired') and
                            current.get('checked_date') == self.state.get('checked_date')):
                        self.state, self.sha = current, sha
                        return
                self.sha = sha
                time.sleep(random.uniform(0.1, 0.3))
        raise StateConflict("season lifecycle busy")

    def stopped(self, now=None):
        if self.state.get("retired"):
            return True
        if (now or now_shanghai()) >= parse_time(self.state["ends_at"]):
            self.state["retired"] = True
            self.state["retired_at"] = (now or now_shanghai()).isoformat()
            self.save()
            return True
        return False

    def observe_end(self, response):
        message = str(response.get('message') or '').strip() if isinstance(response, dict) else ''
        if not succeeded(response) and message in ('活动已结束', '该活动已结束', '本活动已结束'):
            self.state.update(retired=True, retired_at=now_shanghai().isoformat())
            self.save()
            return True
        return False

    def configure(self, fetch):
        if self.stopped():
            return False
        checked_date = now_shanghai().date().isoformat()
        if self.state.get('activity_code') and self.state.get('checked_date') == checked_date:
            return True
        response = fetch("GET", CONFIG_PATH, None)
        if self.observe_end(response):
            return False
        if not succeeded(response):
            raise RuntimeError("season configuration unavailable")
        data = response.get("data") or {}
        code = str(data.get("commonActivityId") or "")
        end = parse_time(data.get("endTime"))
        name = str(data.get('activityName') or '')
        if (not code or end > parse_time(CONFIRMED_END) or data.get("activityType") != 10
                or '20周年' not in name or '有料季' not in name):
            raise ValueError("unexpected seasonal activity")
        if self.state.get("activity_code") and self.state["activity_code"] != code:
            raise ValueError("season activity changed; automatic switching prohibited")
        self.state.update(activity_code=code, ends_at=end.isoformat(), checked_date=checked_date)
        # End timestamps are authoritative; do not guess undocumented numeric status values.
        if end < now_shanghai() or data.get("activityStatus") in ("ENDED", "FINISHED", "CLOSED"):
            self.state["retired"] = True
            self.state["retired_at"] = now_shanghai().isoformat()
        self.save()
        return not self.stopped()


def maintain_lifecycle(store):
    """After the confirmed cutoff, persist the tombstone without contacting the vendor."""
    if now_shanghai() >= parse_time(CONFIRMED_END):
        store.ensure_branch()
        Lifecycle(store).stopped()


class Progress:
    def __init__(self, store, group, index):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        if not GROUP_RE.fullmatch(group) or int(index) < 1:
            raise ValueError("invalid seasonal account metadata")
        key = os.getenv("WEEKLY_BOX_STATE_KEY", "")
        if not key:
            raise RuntimeError("season encryption key unavailable")
        self.cipher = AESGCM(hashlib.sha256(("season-v1:" + key).encode()).digest())
        self.store = store
        self.path = f"season-lottery/{EVENT_ID}/accounts/{group}-{int(index)}.json"
        payload, self.sha = store.read(self.path)
        self.value = {"draws": [], "queries": {}}
        if payload:
            try:
                raw = base64.b64decode(payload["encrypted"])
                self.value = json.loads(self.cipher.decrypt(raw[:12], raw[12:], self.path.encode()))
            except Exception:
                raise RuntimeError("season checkpoint unreadable") from None

    def save(self):
        nonce = os.urandom(12)
        raw = json.dumps(self.value, ensure_ascii=False).encode()
        payload = {"event_id": EVENT_ID, "encrypted": base64.b64encode(
            nonce + self.cipher.encrypt(nonce, raw, self.path.encode())).decode()}
        for _ in range(8):
            try:
                self.sha = self.store.write(self.path, payload, self.sha)
                return
            except StateConflict:
                _, sha = self.store.read(self.path)
                if sha != self.sha:
                    raise
                time.sleep(random.uniform(0.1, 0.3))
        raise StateConflict("season progress busy")


def prize_rows(response):
    data = response.get("data") or {}
    items = data.get("prizeList") or []
    automatic = data.get("receiveMethod") in (1, "AUTO", "AUTOMATIC", "auto")
    return [{"name": str(p.get("prizeTitle") or p.get("prizeName") or p.get("name") or "未命名奖品"),
             "quantity": max(1, int(p.get("quantity") or p.get("prizeNum") or 1)),
             "prize_type": p.get("prizeType"), "winCode": str(p.get("winCode") or ""),
             "claim_status": ("待人工处理" if p.get('prizeType') in (4, 6) else
                              "已领取" if automatic or p.get("receiveStatus") == 3 else "待领取")}
            for p in items if isinstance(p, dict) and p.get('prizeType') not in (-1, 0)]


class SeasonLottery:
    def __init__(self, fetch, store, group, index, task_date, sleep=time.sleep):
        self.fetch, self.store, self.group, self.index = fetch, store, group, index
        self.day, self.sleep = task_date, sleep
        self.result = empty_result(task_date, group)

    def api(self, method, path, payload):
        # Recheck the shared tombstone before every request, including claim-only retries.
        lifecycle = Lifecycle(self.store)
        if lifecycle.stopped():
            raise ActivityEnded()
        response = self.fetch(method, path, payload)
        if lifecycle.observe_end(response):
            raise ActivityEnded()
        return response

    def count(self, code):
        response = self.api("POST", COUNT_PATH, {"activityCode": code})
        value = (response.get("data") or {}).get("count") if succeeded(response) else None
        if isinstance(value, bool) or not str(value).isdigit():
            raise RuntimeError("season count unavailable")
        return int(value)

    def history(self, code):
        rows, page = [], 1
        while True:
            response = self.api("POST", HISTORY_PATH, {"activityCode": code, "pageNum": page, "pageSize": 80})
            data = response.get("data") if succeeded(response) else None
            if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                raise RuntimeError("season history unavailable")
            rows.extend(data["data"])
            if len(data["data"]) < 80 or (data.get('totalPages') is not None and page >= int(data['totalPages'])):
                return rows
            page += 1

    def claim(self, draw):
        for prize in draw.get("prizes", []):
            if prize.get("claim_status") in ("已领取", "待人工处理"):
                continue
            if prize.get("prize_type") == 4:
                prize["claim_status"] = "待人工处理"
                continue
            if not prize.get("winCode"):
                prize["claim_status"] = "领取状态待核实"
                continue
            response = self.api("POST", CLAIM_PATH, {"winCode": prize["winCode"]})
            reason = business_reason(response)
            prize["claim_status"] = "已领取" if succeeded(response) or reason in ("已领取", "已发放") else "领取失败"

    def reconcile(self, draw, code, remaining):
        additions = []
        for prize in self.history(code):
            try:
                timely = abs((parse_time(prize['createTime']) - parse_time(draw['draw_time'])).total_seconds()) <= 120
            except (KeyError, ValueError, TypeError):
                timely = False
            if timely and prize.get('winCode') and prize['winCode'] not in draw['baseline']:
                additions.append(prize)
        if remaining == draw['before_count'] - 1 and len(additions) == 1:
            draw.update(draw_success=True, draw_status='抽奖成功', count_verified=True,
                        prizes=prize_rows({'data': {'prizeList': additions}}))
            return True
        return False

    def run(self):
        if not self.result["required"]:
            if now_shanghai() >= parse_time(CONFIRMED_END):
                maintain_lifecycle(self.store)
            return self.result
        try:
            lifecycle = Lifecycle(self.store)
            self.lifecycle = lifecycle
            if lifecycle.stopped():
                self.result.update(required=False, complete=True, status="活动已永久停用")
                return self.result
            if not lifecycle.configure(self.api):
                self.result.update(required=False, complete=True, status="活动已永久停用")
                return self.result
            code = lifecycle.state["activity_code"]
            progress = Progress(self.store, self.group, self.index)
            remaining = self.count(code)
            query = progress.value["queries"].setdefault(self.day, {"initial_count": remaining})
            self.result.update(activity_code=code, initial_count=query["initial_count"],
                               remaining_count=remaining, query_success=True)
            if self.result["mode"] == "query_only":
                query["remaining_count"] = remaining
                progress.save()
                self.result.update(complete=True, status="仅检测，不抽奖")
                return self.result
            if query.get('no_chance'):
                self.result.update(complete=True, status='无抽奖次数', remaining_count=0)
                return self.finish(progress)
            draws = progress.value["draws"]
            for draw in draws:
                if draw.get("draw_status") == "结果未知":
                    if self.reconcile(draw, code, remaining):
                        progress.save()
                    else:
                        self.result.update(complete=True, status="抽奖结果未知，已停止重复提交")
                        return self.finish(progress)
                if draw.get("draw_success"):
                    self.claim(draw)
                    progress.save()
                    if not draw.get('count_verified'):
                        if remaining >= draw['before_count']:
                            self.result.update(complete=True, status='剩余次数未减少，已停止重复提交')
                            return self.finish(progress)
                        draw['count_verified'] = True
                        progress.save()
            # The initial confirmed count bounds this run even if a faulty endpoint never decrements.
            for _ in range(remaining):
                if lifecycle.stopped():
                    break
                records = self.history(code)
                draw = {"id": f"{self.day}-{len(draws) + 1}", "task_start_date": self.day,
                        "before_count": remaining, "baseline": [p["winCode"] for p in records if p.get("winCode")],
                        "draw_time": now_shanghai().isoformat(), "draw_success": False,
                        "draw_status": "结果未知", "prizes": []}
                draws.append(draw)
                progress.save()  # Reserve durably before a non-idempotent request.
                response = self.api("POST", TURN_PATH, {"clientType": "WEB", "activityCode": code, "luckyKey": ""})
                if not succeeded(response) or not isinstance((response.get('data') or {}).get('prizeList'), list):
                    if lifecycle.observe_end(response):
                        draw.update(draw_status='活动已结束', terminal=True)
                        progress.save()
                        raise ActivityEnded()
                    reason = business_reason(response, "结果未知")
                    if reason in NO_CHANCE:
                        draw["draw_status"] = "无抽奖次数"
                        draw["terminal"] = True
                        query['no_chance'] = True
                        progress.save()
                        self.result.update(complete=True, status="无抽奖次数", remaining_count=0)
                    else:
                        latest = self.count(code)
                        self.result['remaining_count'] = latest
                        if not self.reconcile(draw, code, latest):
                            self.result.update(complete=True, status="抽奖结果未知，已停止重复提交")
                            return self.finish(progress)
                    if not draw.get('draw_success'):
                        return self.finish(progress)
                else:
                    draw.update(draw_success=True, draw_status="抽奖成功", prizes=prize_rows(response))
                progress.save()
                self.claim(draw)
                progress.save()
                latest = self.count(code)
                self.result["remaining_count"] = latest
                if latest >= remaining:
                    self.result.update(complete=True, status="剩余次数未减少，已停止重复提交")
                    return self.finish(progress)
                remaining = latest
                draw['count_verified'] = True
                progress.save()
                if remaining == 0:
                    break
                self.sleep(8)
            query["remaining_count"] = remaining
            progress.save()
            pending = any(p.get("claim_status") not in ("已领取", "待人工处理")
                          for draw in draws for p in draw.get("prizes", []))
            self.result.update(complete=not pending and remaining == 0,
                               status="部分领取失败" if pending else ("已抽完" if draws else "无抽奖次数"))
            return self.finish(progress)
        except ActivityEnded:
            self.result.update(required=False, complete=True, status='活动已永久停用')
            return self.finish(progress) if 'progress' in locals() else self.result
        except Exception:
            # Neither raw responses nor exception text may expose authentication material.
            self.result["status"] = "有料季查询或进度保存失败，本次暂停"
            if "progress" in locals():
                return self.finish(progress)
            return self.result

    def finish(self, progress):
        history = [{k: deepcopy(v) for k, v in draw.items() if k != 'baseline'} for draw in progress.value['draws']]
        for draw in history:
            for prize in draw.get('prizes', []):
                if prize.get('winCode'):
                    prize['prize_id'] = hashlib.sha256(prize['winCode'].encode()).hexdigest()
                prize.pop('winCode', None)  # Claim identifiers live only in encrypted checkpoints.
        self.result['draws'] = [d for d in history if d.get('task_start_date') == self.day]
        self.result['history_draws'] = history
        return self.result


class ActivityEnded(RuntimeError):
    pass


def ensure_portal_session(page):
    """Use the same silent CAS login helper as the public portal; no interactive login."""
    return page.evaluate("""async () => {
        const loggedIn = () => Boolean(window.$nuxt?.$store?.state?.customerIntegralInfo?.customerCode);
        if (loggedIn()) return true;
        let runtime;
        const marker = `season-session-${Date.now()}`;
        window.webpackJsonp?.push([[marker], {
            [marker]: (module, exports, require) => { runtime = require; }
        }, [[marker]]]);
        if (!runtime?.m) return false;
        for (const [id, factory] of Object.entries(runtime.m)) {
            if (!String(factory).includes('jlcTryLogin')) continue;
            try {
                const value = runtime(id);
                if (typeof value?.jlcTryLogin !== 'function') continue;
                const result = await Promise.race([
                    value.jlcTryLogin(false),
                    new Promise(resolve => setTimeout(() => resolve('timeout'), 15000))
                ]);
                return result === 'success' || loggedIn();
            } catch (_) { return false; }
        }
        return false;
    }""")


def execute(client, task_date, context):
    group = str(context.get("group_code") or "")
    result = empty_result(task_date, group)
    if not result["required"]:
        if now_shanghai() >= parse_time(CONFIRMED_END):
            try:
                maintain_lifecycle(WeeklyStore())
            except Exception:
                pass  # The date gate still prohibits all vendor requests.
        return result
    try:
        store = WeeklyStore()
        store.ensure_branch()
        lifecycle = Lifecycle(store)
        if lifecycle.stopped():
            result.update(required=False, complete=True, status="活动已永久停用")
            return result
        # Derive the official portal origin from existing configuration; never store a new domain literal.
        configured = os.getenv("SEASON_LOTTERY_PAGE_URL", "").strip()
        base = urlparse(configured or client.base_url)
        host = base.hostname or ""
        if not configured and host.startswith("m."):
            host = "www." + host[2:]
        if base.scheme != "https" or not host or base.username or base.password:
            raise ValueError("invalid season origin")
        origin = f"https://{host}"
        client.page.goto(origin + PAGE_PATH, wait_until="domcontentloaded", timeout=60000)
        if not ensure_portal_session(client.page):
            raise RuntimeError('season portal session unavailable')
        # Use the portal's own request/session implementation, without logging raw bodies.
        def fetch(method, path, payload):
            return client._browser_fetch_json_once(method, origin + path, payload=payload,
                tag="有料季", dump_body_on_error=False, dump_json_on_success_false=False,
                log_http_error=False, return_business_errors=True)
        return SeasonLottery(fetch, store, group, client.account_index, task_date).run()
    except Exception:
        result["status"] = "有料季会话或状态不可用，本次暂停"
        return result
