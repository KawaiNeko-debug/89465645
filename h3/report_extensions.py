"""Additional workbook views over already-collected account results."""
from collections import Counter
from copy import copy
import os
import re

from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

try:
    from season_lottery import scheduled
except ImportError:
    from h3.season_lottery import scheduled

SOURCE_HEADERS = ["来源分类", "原始组", "组内序号"]
SEASON_HEADERS = ["有料季查询状态", "有料季初始次数", "有料季已抽次数", "有料季剩余次数", "有料季奖品"]
PREFIXES = ("old", "wudi", "ld", "yyy", "ll", "zh")
COLORS = ("E2F0D9", "DDEBF7", "FFF2CC", "E4DFEC", "FCE4D6", "D9E1F2")


def source(record):
    return [record.get("account_category") or "", record.get("source_group") or record.get("group_code") or "",
            record.get("account_index") or 0]


def source_key(record):
    group = str(source(record)[1])
    match = re.fullmatch(r"(old|wudi|new|ld|yyy|ll|zh)(\d+)", group)
    if not match:
        return (99, 99)
    prefix = "wudi" if match[1] == "new" else match[1]
    return PREFIXES.index(prefix), int(match[2])


def main_order(records, default_sort):
    if os.getenv("REPORT_MASTER") != "true":
        return default_sort(records)
    groups = {}
    for row in records:
        groups.setdefault(source_key(row), []).append(row)
    return [row for key in sorted(groups) for row in default_sort(groups[key])]


def prizes(value):
    counts = Counter()
    seen = set()
    for draw in value.get("draws") or []:
        for p in draw.get("prizes") or []:
            key = p.get("winCode") or p.get('prize_id') or (draw.get("id"), p.get("name"))
            if key in seen:
                continue
            seen.add(key)
            counts[str(p.get("name") or "未命名奖品")] += max(1, int(p.get("quantity") or 1))
    return counts


def season_columns(record):
    value = record.get("season_lottery") or {}
    required = scheduled(record.get("task_start_date"), record.get("group_code") or record.get("source_group"))
    return [value.get("status") or ("未检测" if required else "不适用"),
            value.get("initial_count"), sum(bool(d.get("draw_success")) for d in value.get("draws") or []),
            value.get("remaining_count"), "；".join(f"{name} × {quantity}" for name, quantity in prizes(value).items())]


def table(workbook, title, headers, rows):
    sheet = workbook.create_sheet(title)
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    for cell in sheet[1]:
        cell.fill = PatternFill("solid", fgColor="D9E2F3")
        cell.font = Font(bold=True)
    for column in range(1, sheet.max_column + 1):
        sheet.column_dimensions[get_column_letter(column)].width = 22
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="center", wrap_text=True)
            if isinstance(cell.value, str):
                cell.data_type = "s"  # Account/prize text must never become spreadsheet formulas.
    if '开票资料' in headers:
        column = headers.index('开票资料') + 1
        for index in range(2, sheet.max_row + 1):
            cell = sheet.cell(index, column)
            cell.font = Font(color='008000' if cell.value == '有' else 'C00000' if cell.value == '无' else '0000FF')
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    return sheet


def extend_workbook(workbook, ordered):
    master = os.getenv("REPORT_MASTER") == "true"
    sheet = workbook.worksheets[0]
    start = sheet.max_column + 1
    headers = SEASON_HEADERS + (SOURCE_HEADERS if master else [])
    for column, name in enumerate(headers, start):
        cell = sheet.cell(1, column, name)
        cell._style = copy(sheet.cell(1, 1)._style)
        sheet.column_dimensions[get_column_letter(column)].width = 24
    for index, record in enumerate(ordered, 2):
        for column, value in enumerate(season_columns(record) + (source(record) if master else []), start):
            cell = sheet.cell(index, column, value)
            if isinstance(value, str):
                cell.data_type = "s"
            cell.alignment = Alignment(vertical="center", wrap_text=True)
            if master and column >= start + len(SEASON_HEADERS):
                bucket = min(source_key(record)[0], len(COLORS) - 1)
                cell.fill = PatternFill("solid", fgColor=COLORS[bucket])
    relevant = [r for r in ordered if (r.get("season_lottery") or {}).get("query_success")
                or (r.get("season_lottery") or {}).get("draws")
                or (r.get("season_lottery") or {}).get("history_draws")
                or scheduled(r.get("task_start_date"), r.get("group_code") or r.get("source_group"))]
    if not relevant:
        return
    heads = ["客编", "密码", "开票资料", *SOURCE_HEADERS, *SEASON_HEADERS]
    rows, waiting = [], []
    for record in relevant:
        row = [record.get("username") or "", record.get("password") or "",
               (record.get("account_data") or {}).get("invoice_profile_status") or "数据不足",
               *source(record), *season_columns(record)]
        rows.append(row)
        value = record.get("season_lottery") or {}
        if ((scheduled(record.get('task_start_date'), record.get('group_code') or record.get('source_group'))
             and value.get('status') != '活动已永久停用')
                and (not value.get("query_success") or int(value.get("remaining_count") or 0) > 0)):
            waiting.append(row)
    table(workbook, "有料季抽奖", heads, rows)
    if waiting:
        table(workbook, "有料季待抽账号", heads, waiting)
    history = []
    for record in relevant:
        for draw in (record.get('season_lottery') or {}).get('history_draws') or []:
            for prize in draw.get('prizes') or []:
                history.append([record.get('username') or '', *source(record), draw.get('draw_time') or '',
                                prize.get('name') or '', prize.get('quantity') or 1, prize.get('claim_status') or ''])
    if history:
        table(workbook, '有料季历史奖品', ['客编', *SOURCE_HEADERS, '抽奖时间', '奖品', '数量', '领取情况'], history)


def season_message(records):
    relevant = [r for r in records if scheduled(r.get("task_start_date"), r.get("group_code") or r.get("source_group"))
                and (r.get("season_lottery") or {}).get("status") != "活动已永久停用"]
    if not relevant:
        return ""
    values = [r.get("season_lottery") or {} for r in relevant]
    checked = sum(bool(v.get("query_success")) for v in values)
    available = sum(int(v.get("initial_count") or 0) > 0 for v in values)
    successes = sum(sum(bool(d.get("draw_success")) for d in v.get("draws") or []) for v in values)
    peer = [v for r, v in zip(relevant, values) if str(r.get("group_code") or "").startswith(("ll", "zh"))]
    counts = Counter()
    for value in values:
        counts.update(prizes(value))
    lines = ["🎯 有料季抽奖", f"检测成功: {checked}/{len(values)}；有次数账号: {available}",
             f"成功抽奖: {successes}次；已知剩余: {sum(int(v.get('remaining_count') or 0) for v in values)}次",
             f"查询失败: {len(values) - checked}；未完成/待核实: {sum(not v.get('complete') or '未知' in v.get('status', '') for v in values)}"]
    if peer:
        lines.append(f"同行仅检测: {sum(int(v.get('remaining_count') or 0) > 0 for v in peer)}个账号有次数（名单见表）")
    if counts:
        lines.append("🎁 有料季奖品: " + "；".join(f"{k} × {v}" for k, v in counts.most_common()))
    return "\n".join(lines)


def season_issue(record):
    if not scheduled(record.get('task_start_date'), record.get('group_code') or record.get('source_group')):
        return False
    value = record.get('season_lottery') or {}
    return (not record.get('banned_account') and value.get('status') != '活动已永久停用'
            and (not value.get('complete') or '未知' in value.get('status', '')
                 or '未减少' in value.get('status', '')))


def compact_message(messages, limit=4096):
    separator = "\n\n=============================\n\n"
    full = separator.join(f"{label}:\n{text.strip()}" for label, text in messages if text.strip())
    size = lambda text: len(text.encode("utf-16-le")) // 2
    if size(full) <= limit:
        return full
    sections = []
    for label, message in messages:
        # Keep numeric headline statistics; complete detail remains in each workbook.
        lines = [line for line in message.splitlines() if any(key in line for key in (
            "总账号数", "签到成功:", "无抽奖次数", "未完成", "抽奖成功:", "计划账号", "成功率", "券余量不足"))]
        sections.append(f"{label}:\n" + "\n".join(lines[:7]))
    suffix = "\n\n详细问题、奖品分布及账号名单见随附 XLSX。"
    result = separator.join(sections)
    while size(result + suffix) > limit:
        result = result[:-1]
    return result + suffix
