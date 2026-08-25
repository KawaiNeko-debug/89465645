import argparse
import io
import json
import os
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests


GROUP_PREFIXES = ("old", "new", "ll", "zh")
GROUP_CODES = [f"{prefix}{index}" for prefix in GROUP_PREFIXES for index in range(1, 21)]
WORKFLOW_FILE = "dynamic-group.yml"


def category_for(code: str) -> str:
    if code.startswith("old"):
        return "老号全干组"
    if code.startswith("new"):
        return "新号全干组"
    if code.startswith(("ll", "zh")):
        return "同行不签到组"
    raise ValueError(f"unsupported group code: {code}")


def account_count(raw: str) -> int:
    return sum(1 for line in str(raw or "").splitlines() if line.strip() and "," in line)


def configured_groups() -> list[dict]:
    groups = []
    for code in GROUP_CODES:
        count = account_count(os.getenv(code, ""))
        if count:
            groups.append({"group_code": code, "account_category": category_for(code), "account_count": count})
    return groups


def api_request(method: str, repo: str, token: str, path: str, *, params=None, payload=None, raw=False):
    api_root = (os.getenv("GITHUB_API_URL") or "").rstrip("/")
    if not api_root:
        raise RuntimeError("GITHUB_API_URL is required")
    response = requests.request(
        method,
        f"{api_root}/repos/{repo}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        params=params,
        json=payload,
        timeout=60,
        allow_redirects=True,
    )
    response.raise_for_status()
    if raw:
        return response.content
    return response.json() if response.content else {}


def workflow_runs(repo: str, token: str, ref: str) -> list[dict]:
    encoded = quote(WORKFLOW_FILE, safe="")
    data = api_request(
        "GET",
        repo,
        token,
        f"/actions/workflows/{encoded}/runs",
        params={"event": "workflow_dispatch", "branch": ref, "per_page": 100},
    )
    return data.get("workflow_runs", [])


def dispatch(repo: str, token: str, ref: str, orchestration_id: str, group_code: str, task_date: str):
    encoded = quote(WORKFLOW_FILE, safe="")
    api_request(
        "POST",
        repo,
        token,
        f"/actions/workflows/{encoded}/dispatches",
        payload={
            "ref": ref,
            "inputs": {
                "orchestration_id": orchestration_id,
                "group_code": group_code,
                "task_start_date": task_date,
            },
        },
    )


def wait_for_run(repo: str, token: str, ref: str, title: str, known_ids: set[int]) -> dict:
    deadline = time.monotonic() + int(os.getenv("ORCHESTRATION_DISCOVERY_TIMEOUT_SECONDS", "240"))
    while time.monotonic() < deadline:
        candidates = [
            run for run in workflow_runs(repo, token, ref)
            if int(run.get("id") or 0) not in known_ids and str(run.get("display_title") or "") == title
        ]
        if candidates:
            return max(candidates, key=lambda item: int(item.get("id") or 0))
        time.sleep(10)
    raise TimeoutError(f"timed out discovering {title}")


def wait_for_completion(repo: str, token: str, run_id: int) -> dict:
    deadline = time.monotonic() + int(os.getenv("ORCHESTRATION_GROUP_TIMEOUT_SECONDS", "7200"))
    while time.monotonic() < deadline:
        run = api_request("GET", repo, token, f"/actions/runs/{run_id}")
        if run.get("status") == "completed":
            return run
        time.sleep(15)
    raise TimeoutError(f"timed out waiting for run {run_id}")


def write_json(path: str | Path, payload: dict):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run_groups(args) -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ.get("GITHUB_TOKEN") or os.environ["GH_TOKEN"]
    groups = configured_groups()
    if not groups:
        print("no configured account groups", flush=True)
    task_date = args.task_start_date or datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    manifest = {
        "orchestration_id": args.orchestration_id,
        "task_start_date": task_date,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "groups": [],
    }
    write_json(args.output, manifest)
    for group in groups:
        record = dict(group)
        try:
            known_ids = {int(run.get("id") or 0) for run in workflow_runs(repo, token, args.ref)}
            dispatch(repo, token, args.ref, args.orchestration_id, group["group_code"], task_date)
            title = f"group-{args.orchestration_id}-{group['group_code']}"
            run = wait_for_run(repo, token, args.ref, title, known_ids)
            record["run_id"] = int(run["id"])
            completed = wait_for_completion(repo, token, record["run_id"])
            record["conclusion"] = completed.get("conclusion")
            record["run_url"] = completed.get("html_url", "")
            print(f"{group['group_code']}: {record['conclusion']} (run {record['run_id']})", flush=True)
        except Exception as exc:
            record.update({"conclusion": "failure", "error": str(exc)})
            print(f"::error::{group['group_code']}: {exc}", flush=True)
        manifest["groups"].append(record)
        write_json(args.output, manifest)
    manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
    write_json(args.output, manifest)
    return 0


def safe_extract(content: bytes, target: Path):
    target.mkdir(parents=True, exist_ok=True)
    root = target.resolve()
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        for member in archive.infolist():
            destination = (root / member.filename).resolve()
            if destination != root and root not in destination.parents:
                raise ValueError(f"unsafe artifact path: {member.filename}")
        archive.extractall(root)


def download_groups(args) -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ.get("GITHUB_TOKEN") or os.environ["GH_TOKEN"]
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    for group in manifest.get("groups", []):
        run_id = int(group.get("run_id") or 0)
        if not run_id:
            continue
        data = api_request("GET", repo, token, f"/actions/runs/{run_id}/artifacts", params={"per_page": 100})
        expected = f"group-result-{manifest['orchestration_id']}-{group['group_code']}"
        artifact = next(
            (item for item in data.get("artifacts", []) if item.get("name") == expected and not item.get("expired")),
            None,
        )
        if not artifact:
            group["artifact_error"] = "exact run artifact not found"
            continue
        content = api_request("GET", repo, token, f"/actions/artifacts/{artifact['id']}/zip", raw=True)
        safe_extract(content, output / group["group_code"])
    write_json(output / "manifest.json", manifest)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--ref", default=os.getenv("GITHUB_REF_NAME") or "main")
    run.add_argument("--orchestration-id", required=True)
    run.add_argument("--task-start-date", default="")
    run.add_argument("--output", default="orchestration.json")
    download = subparsers.add_parser("download")
    download.add_argument("--manifest", default="orchestration.json")
    download.add_argument("--output-dir", default="results")
    args = parser.parse_args()
    return run_groups(args) if args.command == "run" else download_groups(args)


if __name__ == "__main__":
    raise SystemExit(main())
