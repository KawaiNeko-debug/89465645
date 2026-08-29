"""Allow at most one daily dynamic-controller run across all trigger sources."""

import json
import os
import sys
from datetime import datetime, time, timedelta, timezone
from urllib.parse import quote
from urllib.request import Request, urlopen


SHANGHAI = timezone(timedelta(hours=8))


def has_controller_run_today(payload: dict, now=None, current_run_id: str = "") -> bool:
    now = now or datetime.now(SHANGHAI)
    today = now.astimezone(SHANGHAI).date()
    current_run_id = str(current_run_id or "").strip()
    for run in payload.get("workflow_runs", []) if isinstance(payload, dict) else []:
        if not isinstance(run, dict) or str(run.get("event") or "") not in {"workflow_dispatch", "schedule"}:
            continue
        if current_run_id and str(run.get("id") or run.get("run_id") or "").strip() == current_run_id:
            continue
        created = str(run.get("created_at") or "").strip()
        try:
            created_at = datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(SHANGHAI)
        except ValueError:
            continue
        if created_at.date() == today:
            return True
    return False


def has_manual_run_today(payload: dict, now=None) -> bool:
    """Backward-compatible helper retained for callers and older tests."""
    return has_controller_run_today(payload, now)


def main() -> int:
    event_name = str(os.getenv("GITHUB_EVENT_NAME") or "").strip()
    if event_name not in {"schedule", "workflow_dispatch"}:
        return 0
    api_root = str(os.getenv("GITHUB_API_URL") or "").rstrip("/")
    repository = str(os.getenv("GITHUB_REPOSITORY") or "").strip()
    token = str(os.getenv("GITHUB_TOKEN") or "").strip()
    if not api_root or not repository or not token:
        print("daily guard could not query workflow history; blocking this run", flush=True)
        print("skip=true")
        return 0
    url = f"{api_root}/repos/{quote(repository, safe='/')}/actions/workflows/dynamic-controller.yml/runs?per_page=100"
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except Exception as exc:
        print(f"daily guard query failed ({type(exc).__name__}); blocking this run", flush=True)
        print("skip=true")
        return 0
    if has_controller_run_today(payload, current_run_id=os.getenv("GITHUB_RUN_ID", "")):
        print("A controller run already exists today; this invocation is blocked.", flush=True)
        print("skip=true")
        return 0
    print("No other controller run exists today; this invocation may continue.", flush=True)
    print("skip=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
