import io
import json
import os
import shutil
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
try:
    from merge_results import load_single_result, pick_result, safe_int, truthy
except ImportError:
    from h3.merge_results import load_single_result, pick_result, safe_int, truthy


WORKFLOWS = [
    {"workflow_file": "sign-batch1.yml", "artifact_name": "batch1-result", "group_number": 1, "group_name": "1组"},
    {"workflow_file": "sign-batch2.yml", "artifact_name": "batch2-result", "group_number": 2, "group_name": "2组"},
    {"workflow_file": "sign-batch3.yml", "artifact_name": "batch3-result", "group_number": 3, "group_name": "3组"},
    {"workflow_file": "sign-batch4.yml", "artifact_name": "batch4-result", "group_number": 4, "group_name": "4组"},
    {"workflow_file": "sign-batch5.yml", "artifact_name": "batch5-result", "group_number": 5, "group_name": "5组"},
]

try:
    LOCAL_TZ = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:
    LOCAL_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def selected_workflows(raw_groups: str | None = None) -> list[dict]:
    raw = (raw_groups if raw_groups is not None else os.getenv("SUMMARY_GROUPS", "")).strip()
    if not raw:
        return list(WORKFLOWS)
    try:
        groups = {int(item.strip()) for item in raw.split(",") if item.strip()}
    except ValueError as exc:
        raise ValueError("SUMMARY_GROUPS 必须是逗号分隔的组号") from exc
    selected = [item for item in WORKFLOWS if item["group_number"] in groups]
    if not selected or {item["group_number"] for item in selected} != groups:
        raise ValueError("SUMMARY_GROUPS 包含不支持的组号")
    return selected


def api_request(url: str, token: str, params=None):
    response = requests.get(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        params=params,
        timeout=40,
    )
    response.raise_for_status()
    return response.json()


def iso_to_local_date(text: str) -> str:
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d")


def determine_target_date() -> str:
    hint = (os.getenv("TARGET_DATE_HINT") or "").strip()
    if hint:
        return iso_to_local_date(hint)
    return datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")


def pick_run(repo: str, token: str, workflow_file: str, target_date: str):
    api_url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}/runs"
    payload = api_request(api_url, token, params={"status": "completed", "per_page": 30})
    runs = payload.get("workflow_runs", [])
    for run in runs:
        source_time = run.get("created_at") or run.get("run_started_at") or run.get("updated_at")
        if source_time and iso_to_local_date(source_time) == target_date:
            return run
    return None


def list_run_artifacts(repo: str, token: str, run_id: int) -> list[dict]:
    api_url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/artifacts"
    artifacts = []
    page = 1
    while True:
        payload = api_request(api_url, token, params={"per_page": 100, "page": page})
        page_items = payload.get("artifacts", [])
        artifacts.extend(page_items)
        if len(page_items) < 100:
            break
        page += 1
    return artifacts


def download_artifact_archive(token: str, artifact: dict, target_dir: str):
    response = requests.get(
        artifact["archive_download_url"],
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=60,
        allow_redirects=True,
    )
    response.raise_for_status()
    os.makedirs(target_dir, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(response.content)) as zip_file:
        zip_file.extractall(target_dir)


def download_artifact(token: str, artifacts: list[dict], artifact_name: str, target_dir: str):
    for artifact in artifacts:
        if artifact.get("expired"):
            continue
        if artifact.get("name") != artifact_name:
            continue
        download_artifact_archive(token, artifact, target_dir)
        return artifact
    return None


def download_artifacts_by_prefix(token: str, artifacts: list[dict], prefixes: tuple[str, ...], target_dir: str) -> int:
    count = 0
    for artifact in artifacts:
        if artifact.get("expired"):
            continue
        name = str(artifact.get("name") or "")
        if not any(name.startswith(prefix) for prefix in prefixes):
            continue
        download_artifact_archive(token, artifact, os.path.join(target_dir, name))
        count += 1
    return count


def count_result_rows(path: str) -> int:
    try:
        with open(path, "r", encoding="utf-8") as file:
            payload = json.load(file)
    except Exception:
        return 0
    rows = payload.get("results") if isinstance(payload, dict) else None
    return len(rows) if isinstance(rows, list) else 0


def merge_individual_results(results_dir: str, output_path: str) -> int:
    initial_map = {}
    retry_map = {}
    for path, _, files in os.walk(results_dir):
        if "result.json" not in files:
            continue
        result_path = os.path.join(path, "result.json")
        row = load_single_result(result_path)
        if not row:
            continue
        account_index = safe_int(row.get("account_index"), 0)
        if account_index <= 0:
            continue
        normalized_path = result_path.replace("\\", "/").lower()
        if "/retry-result-" in normalized_path:
            retry_map[account_index] = row
        elif "/initial-result-" in normalized_path:
            initial_map[account_index] = row

    merged = []
    for account_index in sorted(set(initial_map) | set(retry_map)):
        initial = initial_map.get(account_index)
        retry = retry_map.get(account_index)
        if initial and retry:
            merged.append(pick_result(initial, retry))
        elif retry:
            merged.append(retry)
        elif initial:
            merged.append(initial)

    group_name = ""
    group_number = 0
    task_start_date = ""
    if merged:
        group_name = merged[0].get("group_name") or merged[0].get("_group_name") or merged[0].get("_batch_name") or ""
        group_number = safe_int(merged[0].get("group_number", merged[0].get("_group_number")), 0)
        task_start_date = str(merged[0].get("task_start_date") or "").strip()

    payload = {
        "generated_at": datetime.now().isoformat(),
        "batch_name": group_name,
        "group_name": group_name,
        "group_number": group_number,
        "task_start_date": task_start_date,
        "total_accounts": len(merged),
        "results": [],
    }
    for row in sorted(merged, key=lambda item: safe_int(item.get("account_index"), 0)):
        payload["results"].append(
            {
                "account_index": safe_int(row.get("account_index"), 0),
                "execution_order": safe_int(row.get("execution_order"), 0),
                "group_name": row.get("group_name") or group_name,
                "group_number": safe_int(row.get("group_number"), group_number),
                "group_position": row.get("group_position") or (
                    f"{group_number}组账号{safe_int(row.get('account_index'), 0)}" if group_number > 0 else f"账号{safe_int(row.get('account_index'), 0)}"
                ),
                "sign_success": truthy(row.get("sign_success")),
                "sign_status": row.get("sign_status", ""),
                "initial_points": row.get("initial_points", 0.0),
                "final_points": row.get("final_points", 0.0),
                "points_reward": row.get("points_reward", 0.0),
                "has_reward": truthy(row.get("has_reward")),
                "password_error": truthy(row.get("password_error")),
                "risk_controlled": truthy(row.get("risk_controlled")),
                "banned_account": truthy(row.get("banned_account")),
                "points_fetch_success": truthy(row.get("points_fetch_success")),
                "activity_fetch_success": truthy(row.get("activity_fetch_success")),
                "data_fetch_completed": truthy(row.get("data_fetch_completed")),
                "next_day_success": truthy(row.get("next_day_success")),
                "task_start_date": row.get("task_start_date", ""),
                "sign_completed_at": row.get("sign_completed_at", ""),
                "retry_count": safe_int(row.get("retry_count"), 0),
                "is_final_retry": truthy(row.get("is_final_retry")),
                "detail_reason": row.get("detail_reason", ""),
                "sign_time": row.get("sign_time", ""),
                "sign_ip": row.get("sign_ip", ""),
                "activity_records": row.get("activity_records") or {"seckill": [], "lottery": []},
                "account_data_required": truthy(row.get("account_data_required")),
                "account_data_fetch_success": truthy(row.get("account_data_fetch_success")),
                "account_data": row.get("account_data") or {},
                "listing_gift_required": truthy(row.get("listing_gift_required")),
                "listing_gift_success": truthy(row.get("listing_gift_success")),
                "listing_gift_attempted": truthy(row.get("listing_gift_attempted")),
                "listing_gift_status": row.get("listing_gift_status", ""),
                "listing_gift_time": row.get("listing_gift_time", ""),
                "listing_gift_detail": row.get("listing_gift_detail", ""),
                "vote_required": truthy(row.get("vote_required")),
                "vote_success": truthy(row.get("vote_success")),
                "vote_attempted": truthy(row.get("vote_attempted")),
                "vote_status": row.get("vote_status", ""),
                "vote_time": row.get("vote_time", ""),
                "vote_product_sku": row.get("vote_product_sku", ""),
                "vote_product_name": row.get("vote_product_name", ""),
                "vote_detail": row.get("vote_detail", ""),
            }
        )

    if payload["results"]:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
    return len(payload["results"])


def main():
    output_dir = sys.argv[1] if len(sys.argv) > 1 else "results"
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    repo = os.getenv("GITHUB_REPOSITORY")
    if not token or not repo:
        print("缺少 GITHUB_TOKEN 或 GITHUB_REPOSITORY", flush=True)
        sys.exit(1)

    os.makedirs(output_dir, exist_ok=True)
    target_date = determine_target_date()
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target_date": target_date,
        "batches": [],
    }

    found_any = False
    for item in selected_workflows():
        batch = dict(item)
        batch["found"] = False
        batch["reason"] = ""
        run = pick_run(repo, token, item["workflow_file"], target_date)
        if not run:
            batch["reason"] = "未找到当日 workflow run"
            manifest["batches"].append(batch)
            continue

        batch["run_id"] = run.get("id")
        batch["run_url"] = run.get("html_url")
        batch["conclusion"] = run.get("conclusion")
        target_dir = os.path.join(output_dir, f"group{item['group_number']}")
        artifacts = list_run_artifacts(repo, token, run["id"])
        batch["artifact_count"] = len(artifacts)

        artifact = download_artifact(token, artifacts, item["artifact_name"], target_dir)
        final_result_path = os.path.join(target_dir, "result.json")
        final_rows = count_result_rows(final_result_path)

        individual_dir = os.path.join(target_dir, "_individual")
        individual_artifacts = download_artifacts_by_prefix(token, artifacts, ("initial-result-", "retry-result-"), individual_dir)
        individual_result_path = os.path.join(target_dir, "result.from-individual.json")
        individual_rows = merge_individual_results(individual_dir, individual_result_path) if individual_artifacts else 0
        if individual_rows and individual_rows >= final_rows:
            shutil.copyfile(individual_result_path, final_result_path)
            batch["source"] = "individual-artifacts"
            batch["individual_artifacts"] = individual_artifacts
            batch["result_rows"] = individual_rows
        else:
            batch["source"] = "batch-artifact" if artifact else ""
            batch["individual_artifacts"] = individual_artifacts
            batch["result_rows"] = final_rows

        if os.path.exists(individual_result_path):
            os.remove(individual_result_path)
        if os.path.isdir(individual_dir):
            shutil.rmtree(individual_dir, ignore_errors=True)

        if not artifact:
            if individual_rows:
                batch["reason"] = "未找到最终结果 artifact，已用单账号 artifacts 合并"
            else:
                batch["reason"] = "未找到结果 artifact"
                manifest["batches"].append(batch)
                continue

        batch["found"] = True
        batch["artifact_id"] = artifact.get("id") if artifact else None
        batch["artifact_name"] = artifact.get("name") if artifact else None
        batch["extract_dir"] = target_dir
        manifest["batches"].append(batch)
        found_any = True

    with open(os.path.join(output_dir, "manifest.json"), "w", encoding="utf-8") as file:
        json.dump(manifest, file, ensure_ascii=False, indent=2)

    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    sys.exit(0 if found_any else 1)


if __name__ == "__main__":
    main()
