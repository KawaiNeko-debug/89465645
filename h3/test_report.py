import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


SHANGHAI_TIMEZONE = timezone(timedelta(hours=8))


def prepare_manifest(results_dir: Path, orchestration_id: str, task_date: str = "") -> dict:
    result_path = results_dir / "test" / "result.json"
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    records = payload.get("results") if isinstance(payload, dict) else []
    records = records if isinstance(records, list) else []
    resolved_date = str(task_date or payload.get("task_start_date") or "").strip()
    if not resolved_date:
        resolved_date = datetime.now(SHANGHAI_TIMEZONE).strftime("%Y-%m-%d")
    manifest = {
        "orchestration_id": str(orchestration_id),
        "task_start_date": resolved_date,
        "groups": [
            {
                "group_code": "test",
                "account_category": "测试组",
                "account_count": len(records),
            }
        ],
    }
    (results_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    results_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "results")
    orchestration_id = sys.argv[2] if len(sys.argv) > 2 else ""
    task_date = sys.argv[3] if len(sys.argv) > 3 else ""
    manifest = prepare_manifest(results_dir, orchestration_id, task_date)
    account_count = manifest["groups"][0]["account_count"]
    env = os.environ.copy()
    env.update(
        {
            "SUMMARY_CATEGORY": "测试组",
            "SUMMARY_CATEGORY_LABEL": "测试组",
            "EXPECTED_TOTAL": str(account_count),
            "OUTPUT_XLSX_PATH": str(
                results_dir / f"{manifest['task_start_date']}-测试组.xlsx"
            ),
            "GENERATE_XLSX": "true",
            "TELEGRAM_SEND_TEXT": "true",
            "TELEGRAM_SEND_XLSX": "true",
        }
    )
    subprocess.run(
        [sys.executable, str(Path(__file__).with_name("report.py")), str(results_dir)],
        env=env,
        check=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
