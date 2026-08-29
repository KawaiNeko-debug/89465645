import json
import os
import subprocess
import sys
from pathlib import Path


CATEGORIES = (
    ("老号全干组", "老号全干组"),
    ("新号全干组", "新号全干组"),
    ("同行不签到组", "同行不签到组"),
)


def main() -> int:
    results_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "results")
    manifest = json.loads((results_dir / "manifest.json").read_text(encoding="utf-8"))
    task_date = str(manifest.get("task_start_date") or "").strip()
    group_codes = ",".join(
        str(group.get("group_code") or "").strip().lower()
        for group in manifest.get("groups", [])
        if str(group.get("group_code") or "").strip()
    )
    group_limits = {
        str(group.get("group_code") or "").strip().lower(): int(group.get("account_count") or 0)
        for group in manifest.get("groups", [])
        if str(group.get("group_code") or "").strip()
    }
    counts = {category: 0 for category, _ in CATEGORIES}
    for group in manifest.get("groups", []):
        category = str(group.get("account_category") or "")
        if category in counts:
            counts[category] += int(group.get("account_count") or 0)

    requested = str(os.getenv("SUMMARY_CATEGORY_FILTER") or "").strip().lower()
    if requested in {"peer", "同行不签到组", "同行", "ll_zh"}:
        selected_categories = (CATEGORIES[2],)
    elif requested:
        selected_categories = tuple(
            item for item in CATEGORIES
            if requested in {str(item[0]).strip().lower(), str(item[1]).strip().lower()}
        )
    else:
        selected_categories = CATEGORIES

    failures = []
    for category, filename_label in selected_categories:
        env = os.environ.copy()
        env.update(
            {
                "SUMMARY_CATEGORY": category,
                "SUMMARY_CATEGORY_LABEL": category,
                "EXPECTED_TOTAL": str(counts[category]),
                "OUTPUT_XLSX_PATH": str(results_dir / f"{task_date}-{filename_label}.xlsx"),
                "GENERATE_XLSX": "true",
                "TELEGRAM_SEND_TEXT": "true",
                "TELEGRAM_SEND_XLSX": "true",
                "REPORT_GROUP_CODES": group_codes,
                "REPORT_GROUP_FILTER_ACTIVE": "true",
                "REPORT_GROUP_LIMITS": json.dumps(group_limits, ensure_ascii=False),
            }
        )
        completed = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("report.py")), str(results_dir)],
            env=env,
            check=False,
        )
        if completed.returncode:
            failures.append(category)
            print(f"::error::{category} report failed with exit code {completed.returncode}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
