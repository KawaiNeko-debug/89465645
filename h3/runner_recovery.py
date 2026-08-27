import json
import os
import sys
from urllib.parse import quote

import requests


def should_rerun(run: dict, jobs: list[dict]) -> bool:
    if int(run.get("run_attempt") or 0) != 1:
        return False
    if str(run.get("status") or "") != "completed":
        return False
    if not jobs:
        return False
    for job in jobs:
        if int(job.get("runner_id") or 0) != 0:
            return False
        for step in job.get("steps") or []:
            conclusion = str(step.get("conclusion") or "")
            if conclusion and conclusion != "skipped":
                return False
    return True


def api(method: str, path: str, payload=None):
    base = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GITHUB_TOKEN"]
    response = requests.request(
        method,
        f"{base}/repos/{repo}{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json=payload,
        timeout=30,
    )
    if response.status_code not in {200, 201, 202, 204}:
        raise RuntimeError(f"GitHub API {method} {path} failed: HTTP {response.status_code}")
    return response.json() if response.content else {}


def main() -> int:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        return 0
    event = json.loads(open(event_path, encoding="utf-8").read())
    run = event.get("workflow_run") if isinstance(event, dict) else None
    if not isinstance(run, dict):
        return 0
    run_id = int(run.get("id") or 0)
    jobs_payload = api("GET", f"/actions/runs/{run_id}/jobs?filter=all&per_page=100")
    jobs = jobs_payload.get("jobs") if isinstance(jobs_payload, dict) else []
    if not should_rerun(run, jobs if isinstance(jobs, list) else []):
        print(f"[runner-recovery] run {run_id} does not match zero-runner failure", flush=True)
        return 0
    api("POST", f"/actions/runs/{quote(str(run_id), safe='')}/rerun")
    print(f"[runner-recovery] requested one rerun for {run_id}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
