"""Prevent a delayed scheduled controller run after a manual run today."""

import json
import os
import sys
from datetime import datetime, time, timedelta, timezone
from urllib.parse import quote
from urllib.request import Request, urlopen


SHANGHAI = timezone(timedelta(hours=8))


def has_manual_run_today(payload: dict, now=None) -> bool:
    now = now or datetime.now(SHANGHAI)
    today = now.astimezone(SHANGHAI).date()
    for run in payload.get("workflow_runs", []) if isinstance(payload, dict) else []:
        if not isinstance(run, dict) or str(run.get("event") or "") != "workflow_dispatch":
            continue
        created = str(run.get("created_at") or "").strip()
        try:
            created_at = datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(SHANGHAI)
        except ValueError:
            continue
        if created_at.date() == today:
            return True
    return False


def main() -> int:
    if str(os.getenv("GITHUB_EVENT_NAME") or "") != "schedule":
        return 0
    api_root = str(os.getenv("GITHUB_API_URL") or "").rstrip("/")
    repository = str(os.getenv("GITHUB_REPOSITORY") or "").strip()
    token = str(os.getenv("GITHUB_TOKEN") or "").strip()
    if not api_root or not repository or not token:
        print("schedule guard could not query workflow history; continuing", flush=True)
        return 0
    url = f"{api_root}/repos/{quote(repository, safe='/')}/actions/workflows/dynamic-controller.yml/runs?event=workflow_dispatch&per_page=100"
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
        print(f"schedule guard query failed ({type(exc).__name__}); continuing", flush=True)
        return 0
    if has_manual_run_today(payload):
        print("A manual controller run already exists today; scheduled invocation is blocked.", flush=True)
        print("skip=true")
        return 0
    print("No manual controller run exists today; scheduled invocation may continue.", flush=True)
    print("skip=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
