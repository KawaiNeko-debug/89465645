import json
import os
import subprocess
import sys
from pathlib import Path

try:
    from report import send_telegram_documents, send_telegram_message, load_credential_lookup
    from report_extensions import compact_message, table
    from weekly_box_schedule import WeeklyStore
    from weekly_box_report import prepare_reports, acknowledge
except ImportError:
    from h3.report import send_telegram_documents, send_telegram_message, load_credential_lookup
    from h3.report_extensions import compact_message, table
    from h3.weekly_box_schedule import WeeklyStore
    from h3.weekly_box_report import prepare_reports, acknowledge


CATEGORIES = (
    ("老号全干组", "老号全干组"),
    ("无敌全干组", "无敌全干组"),
    ("立东全干组", "立东全干组"),
    ("YYY全干组", "YYY全干组"),
    ("同行不签到组", "同行不签到组"),
)
SEPARATOR = "============================="


def combine_category_messages(messages: list[tuple[str, str]]) -> str:
    return f"\n\n{SEPARATOR}\n\n".join(
        f"{category}:\n{message.strip()}"
        for category, message in messages
        if message.strip()
    )


def main() -> int:
    results_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "results")
    manifest = json.loads((results_dir / "manifest.json").read_text(encoding="utf-8"))
    task_date = str(manifest.get("task_start_date") or "").strip()
    active_groups = manifest.get("report_groups") or manifest.get("groups", [])
    configured_groups = manifest.get("all_groups") or active_groups
    group_codes = ",".join(
        str(group.get("group_code") or "").strip().lower()
        for group in active_groups
        if str(group.get("group_code") or "").strip()
    )
    group_limits = {
        str(group.get("group_code") or "").strip().lower(): int(
            group.get("account_count") or 0
        )
        for group in active_groups
        if str(group.get("group_code") or "").strip()
    }
    counts = {category: 0 for category, _ in CATEGORIES}
    for group in configured_groups:
        category = str(group.get("account_category") or "")
        if category in counts:
            counts[category] += int(group.get("account_count") or 0)

    active_codes = {
        str(group.get("group_code") or "").strip().lower()
        for group in active_groups
        if str(group.get("group_code") or "").strip()
    }
    excluded_by_category = {category: [] for category, _ in CATEGORIES}
    for group in configured_groups:
        category = str(group.get("account_category") or "")
        code = str(group.get("group_code") or "").strip().lower()
        if category in excluded_by_category and code and code not in active_codes:
            excluded_by_category[category].append(code)

    requested = str(os.getenv("SUMMARY_CATEGORY_FILTER") or "").strip().lower()
    if requested in {"peer", "同行不签到组", "同行", "ll_zh"}:
        selected_categories = tuple(
            item for item in CATEGORIES if item[0] == "同行不签到组"
        )
    elif requested:
        selected_categories = tuple(
            item
            for item in CATEGORIES
            if requested in {str(item[0]).strip().lower(), str(item[1]).strip().lower()}
        )
    else:
        selected_categories = CATEGORIES
    isolated_gifts = bool(manifest.get('gift_recovery') and not manifest.get('recovery_base_chain'))
    if isolated_gifts:
        selected_categories = tuple(item for item in selected_categories if counts[item[0]] > 0)

    failures = []
    messages = []
    xlsx_paths = []
    weekly_reports = []
    # A persisted early retirement also suppresses stale fallback failures in reports.
    if os.getenv('GITHUB_TOKEN'):
        try:
            try:
                from season_lottery import Lifecycle
            except ImportError:
                from h3.season_lottery import Lifecycle
            if Lifecycle(WeeklyStore()).stopped():
                os.environ['SEASON_LOTTERY_RETIRED'] = 'true'
        except Exception:
            print('::warning::season lifecycle could not be read for summary', flush=True)
    for category, filename_label in selected_categories:
        output_path = results_dir / f"{task_date}-{filename_label}.xlsx"
        message_path = results_dir / f".{filename_label}-message.txt"
        env = os.environ.copy()
        env.update(
            {
                "SUMMARY_CATEGORY": category,
                "SUMMARY_CATEGORY_LABEL": category,
                "EXPECTED_TOTAL": str(counts[category]),
                "OUTPUT_XLSX_PATH": str(output_path),
                "REPORT_MESSAGE_PATH": str(message_path),
                "REPORT_PRINT_MESSAGE": "false",
                "GENERATE_XLSX": "true",
                "TELEGRAM_SEND_TEXT": "false",
                "TELEGRAM_SEND_XLSX": "false",
                "REPORT_GROUP_CODES": group_codes,
                "REPORT_GROUP_FILTER_ACTIVE": "true",
                "REPORT_GROUP_LIMITS": json.dumps(group_limits, ensure_ascii=False),
                "REPORT_GIFT_ONLY": "true" if isolated_gifts else "false",
                "REPORT_MASTER": "false",
                "SUMMARY_RECOVERY_EXCLUDED_GROUPS": ",".join(
                    excluded_by_category[category]
                ),
            }
        )
        completed = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("report.py")),
                str(results_dir / 'combined') if manifest.get('recovery_overlay_ready') else str(results_dir),
            ],
            env=env,
            check=False,
        )
        if completed.returncode:
            failures.append(category)
            print(
                f"::error::{category} report failed with exit code {completed.returncode}",
                flush=True,
            )
            continue
        if message_path.exists():
            messages.append((category, message_path.read_text(encoding="utf-8")))
        else:
            failures.append(category)
            print(f"::error::{category} report message was not generated", flush=True)
        if output_path.exists():
            xlsx_paths.append(str(output_path))
        else:
            failures.append(category)
            print(f"::error::{category} XLSX was not generated", flush=True)

    full_daily = not requested and not manifest.get('gift_recovery')
    if full_daily:
        master_path = results_dir / f"{task_date}-超级总表.xlsx"
        master_env = os.environ.copy()
        master_env.update({
            'SUMMARY_CATEGORY': '', 'SUMMARY_CATEGORY_LABEL': '超级总表',
            'EXPECTED_TOTAL': str(sum(counts.values())), 'REPORT_MASTER': 'true',
            'OUTPUT_XLSX_PATH': str(master_path), 'REPORT_PRINT_MESSAGE': 'false',
            'REPORT_MESSAGE_PATH': '', 'GENERATE_XLSX': 'true',
            'TELEGRAM_SEND_TEXT': 'false', 'TELEGRAM_SEND_XLSX': 'false',
            'REPORT_GROUP_FILTER_ACTIVE': 'true', 'REPORT_GROUP_CODES': group_codes,
            'REPORT_GROUP_LIMITS': json.dumps(group_limits),
        })
        completed = subprocess.run([sys.executable, str(Path(__file__).with_name('report.py')), str(results_dir)],
                                   env=master_env, check=False)
        if completed.returncode == 0 and master_path.exists():
            xlsx_paths.append(str(master_path))
        else:
            failures.append('超级总表')
        try:
            if os.getenv('GITHUB_TOKEN') and os.getenv('WEEKLY_BOX_STATE_KEY'):
                weekly_store = WeeklyStore()
                credentials, _ = load_credential_lookup()
                weekly_reports = prepare_reports(weekly_store, task_date, results_dir, credentials)
                for weekly in weekly_reports:
                    if not weekly['receipt'].get('text_sent'):
                        messages.append(('纸盒抽奖周报', weekly['message']))
                    if not weekly['receipt'].get('documents_sent'):
                        xlsx_paths.append(weekly['path'])
        except Exception:
            failures.append('纸盒抽奖周报')
            print('::error::weekly report unavailable; daily reports continue', flush=True)
        if master_path.exists():
            try:
                from openpyxl import load_workbook
                workbook = load_workbook(master_path)
                table(workbook, '通知完整明细', ['分类', '完整统计文本'],
                      [(label, line) for label, text in messages for line in text.splitlines()])
                workbook.save(master_path)
            except Exception:
                failures.append('通知完整明细')

    telegram_configured = bool(
        os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")
    )
    text_sent = documents_sent = False
    if telegram_configured and messages:
        combined = compact_message(messages)
        text_sent = send_telegram_message(combined, single_message=True)
        if not text_sent:
            failures.append("Telegram 合并文字")
            print("::error::combined Telegram message failed", flush=True)
    if telegram_configured and xlsx_paths:
        documents_sent = send_telegram_documents(xlsx_paths)
        if not documents_sent:
            failures.append("Telegram XLSX 媒体组")
            print("::error::Telegram XLSX media group failed after retry", flush=True)
    if weekly_reports and telegram_configured:
        try:
            acknowledge(weekly_store, weekly_reports, text_sent, documents_sent)
        except Exception:
            failures.append('周报发送状态保存')
            print('::error::weekly report receipt could not be saved', flush=True)

    print(
        f"[category-reports] generated={len(xlsx_paths)} messages={len(messages)} "
        f"telegram={'on' if telegram_configured else 'off'} failures={len(failures)}",
        flush=True,
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
