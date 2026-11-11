"""End-of-week reports from frozen, encrypted checkpoints; never starts draws."""
import base64
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import random
import re
import time
from pathlib import Path

from openpyxl import Workbook

try:
    from weekly_box_schedule import AccountCheckpoint, StateConflict, parse_date, validate_roster
    from report_extensions import table
except ImportError:
    from h3.weekly_box_schedule import AccountCheckpoint, StateConflict, parse_date, validate_roster
    from h3.report_extensions import table


def cipher():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = os.environ["WEEKLY_BOX_STATE_KEY"]
    return AESGCM(hashlib.sha256(("weekly-report-v1:" + key).encode()).digest())


def classify(rows, error=""):
    if error:
        return error
    if not rows:
        return "未执行"
    if any("未知" in str(d.get("draw_status") or "") for d in rows):
        return "结果未知"
    if any(p.get("claim_status") not in ("已领取",) for d in rows if d.get("draw_success")
           for p in d.get("prizes", [])):
        return "领取失败"
    if len(rows) >= 2 and all(d.get("draw_success") and d.get("claim_success") for d in rows[:2]):
        return "两次抽奖及领奖完成"
    try:
        from weekly_box_schedule import no_chance
    except ImportError:
        from h3.weekly_box_schedule import no_chance
    if no_chance(rows):
        return "无抽奖次数"
    return "部分完成" if any(d.get("draw_success") for d in rows) else "未执行"


def categories(group):
    for prefix, label in (("old", "老号全干组"), ("wudi", "无敌全干组"),
                          ("ld", "立东全干组"), ("yyy", "YYY全干组")):
        if group.startswith(prefix):
            return label
    return ""


def closed_weeks(store, task_date):
    response = store.request("GET", "git/trees/lottery-state?recursive=1")
    if response.status_code == 404:
        return []
    payload = response.json()
    if response.status_code != 200 or payload.get("truncated"):
        raise RuntimeError("weekly report index unavailable")
    weeks = []
    for node in payload.get("tree", []):
        match = re.fullmatch(r"weekly/(\d{4}-\d{2}-\d{2})/roster.json", node.get("path", ""))
        if match and parse_date(match[1]) + timedelta(days=6) <= parse_date(task_date):
            weeks.append(match[1])
    return sorted(set(weeks))


def frozen_snapshot(store, week):
    path = f"weekly/{week}/report-snapshot.json"
    payload, _ = store.read(path)
    if payload:
        raw = base64.b64decode(payload["encrypted"])
        return json.loads(cipher().decrypt(raw[:12], raw[12:], path.encode()))
    sunday = (parse_date(week) + timedelta(days=6)).isoformat()
    roster, _ = store.read(f"weekly/{week}/roster.json")
    roster = validate_roster(roster, sunday)

    def load(account):
        row = dict(account)
        try:
            checkpoint = AccountCheckpoint(store, account)
            row["box_lottery"] = checkpoint.rows
            row["report_status"] = classify(checkpoint.rows, "状态缺失" if checkpoint.sha is None else "")
        except Exception:
            row["box_lottery"] = []
            row["report_status"] = "状态不可核实"
        return row

    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(load, roster["accounts"]))
    snapshot = {"week_id": week, "cycle_dates": roster["cycle_dates"], "rows": rows}
    nonce = os.urandom(12)
    payload = {"week_id": week, "encrypted": base64.b64encode(
        nonce + cipher().encrypt(nonce, json.dumps(snapshot, ensure_ascii=False).encode(), path.encode())).decode()}
    try:
        store.write(path, payload)
    except StateConflict:
        saved, _ = store.read(path)
        if saved:
            raw = base64.b64decode(saved['encrypted'])
            return json.loads(cipher().decrypt(raw[:12], raw[12:], path.encode()))
        # Another file changed the branch tip. Retry this same frozen snapshot.
        for _ in range(8):
            try:
                store.write(path, payload)
                break
            except StateConflict:
                time.sleep(random.uniform(0.1, 0.3))
        else:
            raise StateConflict('weekly report snapshot busy')
    return snapshot


def build_report(snapshot, path, credentials):
    try:
        from report import report_password
    except ImportError:
        from h3.report import report_password
    workbook = Workbook()
    workbook.remove(workbook.active)
    statuses = Counter(row["report_status"] for row in snapshot["rows"])
    awards, claimed, coupons = Counter(), Counter(), {}
    details, incomplete = [], []
    for row in sorted(snapshot["rows"], key=lambda r: (r["source_group"], int(r["account_index"]))):
        group, index = row["source_group"], int(row["account_index"])
        credential = credentials.get((group, index), {})
        draws = row.get("box_lottery") or []
        rewards, claims = [], []
        seen = set()
        for position in range(2):
            draw = draws[position] if position < len(draws) else {}
            names = []
            for prize in draw.get("prizes", []):
                name = str(prize.get("name") or "未命名奖品")
                quantity = max(1, int(prize.get("quantity") or 1))
                names.append(f"{name} × {quantity}")
                identity = prize.get("winCode") or (position, name)
                if identity in seen:
                    continue
                seen.add(identity)
                awards[name] += quantity
                if prize.get("claim_status") == "已领取":
                    claimed[name] += quantity
                if "券" in name or prize.get("prize_type") in (2, 3):
                    coupons.setdefault(name, set()).add((group, index))
            rewards.append("；".join(names) or draw.get("draw_status") or "未执行")
            claims.append(draw.get("claim_status") or "未执行")
        detail = [credential.get("username") or f"{group}:{index}（客编不可用）",
                  report_password(str(credential.get("password") or "")), categories(group), group, index,
                  row["assigned_date"], row["report_status"], rewards[0], claims[0], rewards[1], claims[1]]
        details.append(detail)
        if row["report_status"] not in ("两次抽奖及领奖完成", "无抽奖次数"):
            incomplete.append(detail)
    total = len(snapshot["rows"])
    complete = statuses["两次抽奖及领奖完成"]
    stats = [["计划账号", total], ["两次抽奖及领奖完成", complete], ["无抽奖次数", statuses["无抽奖次数"]],
             ["未完成账号", len(incomplete)], ["抽奖成功率", f"{complete / total * 100 if total else 0:.1f}%"],
             ["奖品总数", sum(awards.values())], ["确认领取数量", sum(claimed.values())],
             ["优惠券种类", len(coupons)], *[[k, v] for k, v in sorted(statuses.items())
                                           if k not in ("两次抽奖及领奖完成", "无抽奖次数")]]
    table(workbook, "周统计", ["统计项目", "数量"], stats)
    headers = ["客编", "密码", *["来源分类", "原始组", "组内序号"], "分配日期", "本周状态",
               "纸盒抽奖1奖励", "纸盒抽奖1领取情况", "纸盒抽奖2奖励", "纸盒抽奖2领取情况"]
    table(workbook, "账号明细", headers, details)
    table(workbook, "奖品分布", ["奖品", "中奖数量", "确认领取数量"], [[k, v, claimed[k]] for k, v in awards.most_common()])
    table(workbook, "优惠券分布", ["优惠券", "获得账号数量", "奖品数量"],
          [[k, len(v), awards[k]] for k, v in sorted(coupons.items())])
    table(workbook, "未完成账号", headers, incomplete)
    workbook.save(path)
    return (f"📅 纸盒抽奖周报 {snapshot['cycle_dates'][0]} 至 {snapshot['cycle_dates'][-1]}\n"
            + "\n".join(f"{key}: {value}" for key, value in stats)
            + "\n🎁 奖品分布\n" + ("\n".join(f"{k}: {v}" for k, v in awards.most_common()) or "无中奖记录")
            + "\n🎟️ 优惠券\n" + ("\n".join(f"{k}: {len(v)}个账号" for k, v in sorted(coupons.items())) or "无"))


def prepare_reports(store, task_date, results_dir, credentials):
    reports = []
    owner = os.getenv('GITHUB_RUN_ID', 'local') + ':' + os.getenv('GITHUB_RUN_ATTEMPT', '1')
    for week in closed_weeks(store, task_date):
        delivery_path = f"weekly/{week}/report-delivery.json"
        receipt, sha = store.read(delivery_path)
        receipt = receipt or {"week_id": week, "text_sent": False, "documents_sent": False}
        if receipt.get("text_sent") and receipt.get("documents_sent"):
            continue
        current_time = datetime.now(timezone.utc)
        if receipt.get('owner') and receipt['owner'] != owner:
            acquired = datetime.fromisoformat(receipt['reserved_at'])
            if current_time - acquired < timedelta(hours=2):
                continue
        receipt.update(owner=owner, reserved_at=current_time.isoformat())
        # Reserve the report before reading its entire roster or sending notifications.
        for _ in range(8):
            try:
                sha = store.write(delivery_path, receipt, sha)
                break
            except StateConflict:
                current, current_sha = store.read(delivery_path)
                if current_sha != sha:
                    break
        else:
            raise StateConflict('weekly report reservation busy')
        if sha != store.read(delivery_path)[1] or store.read(delivery_path)[0].get('owner') != owner:
            continue
        snapshot = frozen_snapshot(store, week)
        path = Path(results_dir) / f"{week}-{snapshot['cycle_dates'][-1]}-纸盒抽奖周报.xlsx"
        message = build_report(snapshot, path, credentials)
        reports.append({"week_id": week, "path": str(path), "message": message, "receipt": receipt, "sha": sha})
        # A Telegram media group accepts at most ten files (six daily + four catch-up reports).
        if len(reports) == 4:
            break
    return reports


def acknowledge(store, reports, text_sent, documents_sent):
    for report in reports:
        receipt = report["receipt"]
        receipt["text_sent"] = bool(receipt.get("text_sent") or text_sent)
        receipt["documents_sent"] = bool(receipt.get("documents_sent") or documents_sent)
        receipt['owner'] = ''
        path = f"weekly/{report['week_id']}/report-delivery.json"
        sha = report['sha']
        for _ in range(8):
            try:
                store.write(path, receipt, sha)
                break
            except StateConflict:
                current, sha = store.read(path)
                receipt['text_sent'] = bool(receipt['text_sent'] or (current or {}).get('text_sent'))
                receipt['documents_sent'] = bool(receipt['documents_sent'] or (current or {}).get('documents_sent'))
        else:
            raise StateConflict('weekly report acknowledgement busy')
