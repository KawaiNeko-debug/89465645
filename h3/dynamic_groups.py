import argparse
import base64
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
GROUP_WORKFLOW_FILE = "dynamic-group.yml"
SUMMARY_WORKFLOW_FILE = "dynamic-summary.yml"
DISPATCH_ATTEMPTS = 3


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
            groups.append(
                {
                    "group_code": code,
                    "account_category": category_for(code),
                    "account_count": count,
                    "run_id": 0,
                    "handoff_status": "pending",
                }
            )
    return groups


def write_json(path: str | Path, payload: dict):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def compact_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def encode_chain_state(payload: dict) -> str:
    return base64.b64encode(compact_json(payload).encode("utf-8")).decode("ascii")


def load_chain_state(raw: str) -> dict:
    raw = str(raw or "").strip()
    if raw and not raw.startswith("{"):
        try:
            raw = base64.b64decode(raw, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("chain_state is neither JSON nor valid Base64 JSON") from exc
    try:
        state = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("chain_state is not valid JSON") from exc
    if not isinstance(state, dict) or not str(state.get("orchestration_id") or "").strip():
        raise ValueError("chain_state has no orchestration_id")
    groups = state.get("groups")
    if not isinstance(groups, list):
        raise ValueError("chain_state groups must be a list")
    seen = set()
    for group in groups:
        code = str(group.get("group_code") or "").strip().lower() if isinstance(group, dict) else ""
        if code not in GROUP_CODES or code in seen:
            raise ValueError(f"invalid or duplicate group code: {code}")
        seen.add(code)
    return state


def new_chain_state(
    orchestration_id: str,
    ref: str,
    task_start_date: str = "",
    after_group: str = "",
) -> dict:
    task_date = task_start_date or datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    groups = configured_groups()
    after = str(after_group or "").strip().lower()
    if after:
        positions = {item["group_code"]: index for index, item in enumerate(groups)}
        if after in positions:
            groups = groups[positions[after] + 1 :]
    return {
        "schema_version": 1,
        "orchestration_id": str(orchestration_id),
        "task_start_date": task_date,
        "ref": ref or "main",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "groups": groups,
    }


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


def workflow_runs(repo: str, token: str, ref: str, workflow_file: str) -> list[dict]:
    encoded = quote(workflow_file, safe="")
    data = api_request(
        "GET",
        repo,
        token,
        f"/actions/workflows/{encoded}/runs",
        params={"event": "workflow_dispatch", "branch": ref, "per_page": 100},
    )
    return data.get("workflow_runs", [])


def existing_run(repo: str, token: str, ref: str, workflow_file: str, title: str) -> dict | None:
    matches = [
        run
        for run in workflow_runs(repo, token, ref, workflow_file)
        if str(run.get("display_title") or "") == title
    ]
    return min(matches, key=lambda item: int(item.get("id") or 0)) if matches else None


def dispatch_once(repo: str, token: str, ref: str, workflow_file: str, title: str, inputs: dict) -> dict:
    existing = existing_run(repo, token, ref, workflow_file, title)
    if existing:
        print(f"[handoff] already exists: {title} (run {existing.get('id')})", flush=True)
        return {"status": "existing", "run_id": int(existing.get("id") or 0)}

    encoded = quote(workflow_file, safe="")
    last_error = None
    for attempt in range(1, DISPATCH_ATTEMPTS + 1):
        try:
            api_request(
                "POST",
                repo,
                token,
                f"/actions/workflows/{encoded}/dispatches",
                payload={"ref": ref, "inputs": inputs},
            )
            print(f"[handoff] dispatched: {title}", flush=True)
            return {"status": "dispatched", "run_id": 0}
        except Exception as exc:
            last_error = exc
            print(f"[handoff] dispatch attempt {attempt}/{DISPATCH_ATTEMPTS} failed: {exc}", flush=True)
            for _ in range(10):
                time.sleep(2)
                existing = existing_run(repo, token, ref, workflow_file, title)
                if existing:
                    print(f"[handoff] dispatch confirmed: {title} (run {existing.get('id')})", flush=True)
                    return {"status": "existing", "run_id": int(existing.get("id") or 0)}
            if attempt < DISPATCH_ATTEMPTS:
                time.sleep(attempt * 2)
    raise RuntimeError(f"failed to dispatch {title} after {DISPATCH_ATTEMPTS} attempts") from last_error


def github_context() -> tuple[str, str]:
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ.get("GITHUB_TOKEN") or os.environ["GH_TOKEN"]
    return repo, token


def dispatch_group(state: dict, group_code: str) -> dict:
    repo, token = github_context()
    orchestration_id = str(state["orchestration_id"])
    ref = str(state.get("ref") or os.getenv("GITHUB_REF_NAME") or "main")
    title = f"group-{orchestration_id}-{group_code}"
    return dispatch_once(
        repo,
        token,
        ref,
        GROUP_WORKFLOW_FILE,
        title,
        {
            "orchestration_id": orchestration_id,
            "group_code": group_code,
            "task_start_date": str(state.get("task_start_date") or ""),
            "continue_chain": "true",
            "chain_state": encode_chain_state(state),
            **(
                {"components": str(state.get("components") or "").strip()}
                if str(state.get("components") or "").strip()
                else {}
            ),
        },
    )


def dispatch_summary(state: dict) -> dict:
    repo, token = github_context()
    orchestration_id = str(state["orchestration_id"])
    ref = str(state.get("ref") or os.getenv("GITHUB_REF_NAME") or "main")
    title = f"dynamic-summary-{orchestration_id}"
    return dispatch_once(
        repo,
        token,
        ref,
        SUMMARY_WORKFLOW_FILE,
        title,
        {"orchestration_id": orchestration_id, "chain_state": encode_chain_state(state)},
    )


def start_chain(args) -> int:
    state = new_chain_state(
        args.orchestration_id,
        args.ref,
        args.task_start_date,
        getattr(args, "after_group", ""),
    )
    write_json(args.output, state)
    groups = state["groups"]
    if groups:
        dispatch_group(state, groups[0]["group_code"])
    else:
        dispatch_summary(state)
    print(f"[handoff] chain started with {len(groups)} configured groups", flush=True)
    return 0


def advance_chain(args) -> int:
    raw = os.environ.get(args.chain_state_env, "") if args.chain_state_env else args.chain_state
    state = load_chain_state(raw)
    current_code = args.current_group.strip().lower()
    current_index = next(
        (index for index, group in enumerate(state["groups"]) if group["group_code"] == current_code),
        None,
    )
    if current_index is None:
        raise ValueError(f"current group is not present in chain_state: {current_code}")
    state["groups"][current_index]["run_id"] = int(args.current_run_id)
    if args.current_result:
        try:
            result_payload = json.loads(Path(args.current_result).read_text(encoding="utf-8"))
            result_count = int(result_payload.get("total_accounts") or len(result_payload.get("results") or []))
            state["groups"][current_index]["account_count"] = max(0, result_count)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            print(f"::warning::could not read current group account count: {exc}", flush=True)
    state["groups"][current_index]["handoff_status"] = "finalized"
    state["groups"][current_index]["finalized_at"] = datetime.now(timezone.utc).isoformat()
    write_json(args.output, state)

    if current_index + 1 < len(state["groups"]):
        dispatch_group(state, state["groups"][current_index + 1]["group_code"])
    else:
        state["completed_at"] = datetime.now(timezone.utc).isoformat()
        write_json(args.output, state)
        dispatch_summary(state)
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
    repo, token = github_context()
    raw = os.environ.get(args.chain_state_env, "") if args.chain_state_env else args.chain_state
    manifest = load_chain_state(raw)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    orchestration_id = str(manifest["orchestration_id"])

    for group in manifest["groups"]:
        run_id = int(group.get("run_id") or 0)
        group["artifact_downloaded"] = False
        if not run_id:
            group["artifact_error"] = "group run id is missing"
            continue
        try:
            run = api_request("GET", repo, token, f"/actions/runs/{run_id}")
            group["run_status"] = str(run.get("status") or "")
            group["conclusion"] = str(run.get("conclusion") or "")
            group["run_url"] = str(run.get("html_url") or "")
            data = api_request("GET", repo, token, f"/actions/runs/{run_id}/artifacts", params={"per_page": 100})
            expected = f"group-result-{orchestration_id}-{group['group_code']}"
            artifact = next(
                (item for item in data.get("artifacts", []) if item.get("name") == expected and not item.get("expired")),
                None,
            )
            if not artifact:
                raise FileNotFoundError("exact group artifact not found")
            content = api_request("GET", repo, token, f"/actions/artifacts/{artifact['id']}/zip", raw=True)
            safe_extract(content, output / group["group_code"])
            group["artifact_downloaded"] = True
            group.pop("artifact_error", None)
        except Exception as exc:
            group["artifact_error"] = str(exc)
            print(f"::warning::{group['group_code']}: {exc}", flush=True)

    manifest["summary_downloaded_at"] = datetime.now(timezone.utc).isoformat()
    write_json(output / "manifest.json", manifest)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start")
    start.add_argument("--ref", default=os.getenv("GITHUB_REF_NAME") or "main")
    start.add_argument("--orchestration-id", required=True)
    start.add_argument("--task-start-date", default="")
    start.add_argument("--after-group", default="")
    start.add_argument("--output", default="chain-state.json")

    advance = subparsers.add_parser("advance")
    advance.add_argument("--chain-state", default="")
    advance.add_argument("--chain-state-env", default="")
    advance.add_argument("--current-group", required=True)
    advance.add_argument("--current-run-id", required=True, type=int)
    advance.add_argument("--current-result", default="")
    advance.add_argument("--output", default="chain-state.json")

    download = subparsers.add_parser("download")
    download.add_argument("--chain-state", default="")
    download.add_argument("--chain-state-env", default="")
    download.add_argument("--output-dir", default="results")

    args = parser.parse_args()
    if args.command == "start":
        return start_chain(args)
    if args.command == "advance":
        return advance_chain(args)
    return download_groups(args)


if __name__ == "__main__":
    raise SystemExit(main())
