import json
import os
import subprocess
import sys
from pathlib import Path

try:
    from report import send_telegram_documents, send_telegram_message
except ImportError:
    from h3.report import send_telegram_documents, send_telegram_message


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
    active_groups = manifest.get("groups", [])
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

    failures = []
    messages = []
    xlsx_paths = []
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
                "SUMMARY_RECOVERY_EXCLUDED_GROUPS": ",".join(
                    excluded_by_category[category]
                ),
            }
        )
        completed = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("report.py")),
                str(results_dir),
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

    telegram_configured = bool(
        os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")
    )
    if telegram_configured and messages:
        combined = combine_category_messages(messages)
        if not send_telegram_message(combined, single_message=True):
            failures.append("Telegram 合并文字")
            print("::error::combined Telegram message failed", flush=True)
    if telegram_configured and xlsx_paths:
        if not send_telegram_documents(xlsx_paths):
            failures.append("Telegram XLSX 媒体组")
            print("::error::Telegram XLSX media group failed after retry", flush=True)

    print(
        f"[category-reports] generated={len(xlsx_paths)} messages={len(messages)} "
        f"telegram={'on' if telegram_configured else 'off'} failures={len(failures)}",
        flush=True,
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
