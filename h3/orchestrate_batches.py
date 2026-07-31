import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests


API_ROOT = "https://api.github.com"
try:
    LOCAL_TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    LOCAL_TZ = timezone(timedelta(hours=8))
BATCHES = {
    5: {
        "workflow_file": "sign-batch5.yml",
        "workflow_name": "sign-batch5",
        "artifact_name": "batch5-result",
    },
    6: {
        "workflow_file": "sign-batch6.yaml",
        "workflow_name": "sign-batch6",
        "artifact_name": "batch6-result",
    },
    7: {
        "workflow_file": "sign-batch7.yml",
        "workflow_name": "sign-batch7",
        "artifact_name": "batch7-result",
    },
    8: {
        "workflow_file": "sign-batch8.yml",
        "workflow_name": "sign-batch8",
        "artifact_name": "batch8-result",
    },
}


def env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def parse_groups(value: str) -> list[int]:
    groups = []
    for item in str(value or "").split(","):
        if not item.strip():
            continue
        group = int(item.strip())
        if group not in BATCHES:
            raise ValueError(f"unsupported batch group: {group}")
        if group not in groups:
            groups.append(group)
    if not groups:
        raise ValueError("at least one batch group is required")
    return groups


def api_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def api_request(
    method: str,
    repo: str,
    token: str,
    path: str,
    *,
    params: dict | None = None,
    payload: dict | None = None,
    raw: bool = False,
):
    response = requests.request(
        method,
        f"{API_ROOT}/repos/{repo}{path}",
        headers=api_headers(token),
        params=params,
        json=payload,
        timeout=60,
        allow_redirects=True,
    )
    response.raise_for_status()
    if raw:
        return response.content
    if response.status_code == 204 or not response.content:
        return {}
    return response.json()


def workflow_runs(repo: str, token: str, workflow_file: str, ref: str) -> list[dict]:
    encoded = quote(workflow_file, safe="")
    data = api_request(
        "GET",
        repo,
        token,
        f"/actions/workflows/{encoded}/runs",
        params={"event": "workflow_dispatch", "branch": ref, "per_page": 30},
    )
    return data.get("workflow_runs", []) if isinstance(data, dict) else []


def dispatch_workflow(
    repo: str,
    token: str,
    workflow_file: str,
    ref: str,
    orchestration_id: str,
) -> None:
    encoded = quote(workflow_file, safe="")
    api_request(
        "POST",
        repo,
        token,
        f"/actions/workflows/{encoded}/dispatches",
        payload={"ref": ref, "inputs": {"orchestration_id": orchestration_id}},
    )


def parse_api_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def select_dispatched_run(
    runs: list[dict],
    expected_title: str,
    known_run_ids: set[int],
    dispatched_at: datetime,
) -> dict | None:
    exact = [run for run in runs if str(run.get("display_title") or "") == expected_title]
    if exact:
        return max(exact, key=lambda run: int(run.get("id") or 0))

    threshold = dispatched_at - timedelta(seconds=5)
    candidates = []
    for run in runs:
        run_id = int(run.get("id") or 0)
        created_at = parse_api_time(run.get("created_at"))
        if run_id in known_run_ids or not created_at or created_at < threshold:
            continue
        candidates.append(run)
    return max(candidates, key=lambda run: int(run.get("id") or 0)) if candidates else None


def wait_for_dispatched_run(
    repo: str,
    token: str,
    spec: dict,
    ref: str,
    orchestration_id: str,
    known_run_ids: set[int],
    dispatched_at: datetime,
) -> dict:
    timeout_seconds = env_int("ORCHESTRATION_DISCOVERY_TIMEOUT_SECONDS", 180)
    poll_seconds = env_int("ORCHESTRATION_POLL_SECONDS", 15)
    deadline = time.monotonic() + timeout_seconds
    expected_title = f"{spec['workflow_name']} ({orchestration_id})"
    while time.monotonic() < deadline:
        run = select_dispatched_run(
            workflow_runs(repo, token, spec["workflow_file"], ref),
            expected_title,
            known_run_ids,
            dispatched_at,
        )
        if run:
            return run
        time.sleep(poll_seconds)
    raise TimeoutError(f"timed out waiting for {spec['workflow_name']} run discovery")


def wait_for_completion(repo: str, token: str, run_id: int) -> dict:
    timeout_seconds = env_int("ORCHESTRATION_BATCH_TIMEOUT_SECONDS", 4500)
    poll_seconds = env_int("ORCHESTRATION_POLL_SECONDS", 15)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        run = api_request("GET", repo, token, f"/actions/runs/{run_id}")
        if run.get("status") == "completed":
            return run
        time.sleep(poll_seconds)
    raise TimeoutError(f"timed out waiting for run {run_id} completion")


def write_json(path: str | Path, payload: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)


def run_batches(
    repo: str,
    token: str,
    ref: str,
    orchestration_id: str,
    groups: list[int],
    output_path: str,
) -> dict:
    manifest = {
        "orchestration_id": orchestration_id,
        "ref": ref,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "batches": [],
    }
    write_json(output_path, manifest)

    for group in groups:
        spec = BATCHES[group]
        record = {
            "group_number": group,
            "workflow_file": spec["workflow_file"],
            "artifact_name": spec["artifact_name"],
            "status": "dispatching",
            "conclusion": None,
        }
        try:
            existing = workflow_runs(repo, token, spec["workflow_file"], ref)
            known_run_ids = {int(run.get("id") or 0) for run in existing}
            dispatched_at = datetime.now(timezone.utc)
            dispatch_workflow(
                repo,
                token,
                spec["workflow_file"],
                ref,
                orchestration_id,
            )
            run = wait_for_dispatched_run(
                repo,
                token,
                spec,
                ref,
                orchestration_id,
                known_run_ids,
                dispatched_at,
            )
            run_id = int(run["id"])
            record.update(
                {
                    "run_id": run_id,
                    "run_url": run.get("html_url", ""),
                    "status": run.get("status", "queued"),
                }
            )
            print(f"batch {group}: dispatched run {run_id}", flush=True)
            completed = wait_for_completion(repo, token, run_id)
            record.update(
                {
                    "status": completed.get("status", "completed"),
                    "conclusion": completed.get("conclusion"),
                    "completed_at": completed.get("updated_at", ""),
                }
            )
            print(
                f"batch {group}: run {run_id} completed with {record['conclusion']}",
                flush=True,
            )
        except Exception as error:
            record.update({"status": "error", "conclusion": "failure", "error": str(error)})
            print(f"batch {group}: orchestration error: {error}", flush=True)
        manifest["batches"].append(record)
        write_json(output_path, manifest)

    manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
    write_json(output_path, manifest)
    return manifest


def list_artifacts(repo: str, token: str, run_id: int) -> list[dict]:
    artifacts = []
    page = 1
    while True:
        data = api_request(
            "GET",
            repo,
            token,
            f"/actions/runs/{run_id}/artifacts",
            params={"per_page": 100, "page": page},
        )
        items = data.get("artifacts", []) if isinstance(data, dict) else []
        artifacts.extend(items)
        if len(items) < 100:
            break
        page += 1
    return artifacts


def extract_archive(content: bytes, target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    root = target_dir.resolve()
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        for member in archive.infolist():
            destination = (root / member.filename).resolve()
            if destination != root and root not in destination.parents:
                raise ValueError(f"unsafe artifact path: {member.filename}")
        archive.extractall(root)


def download_artifact(repo: str, token: str, artifact: dict, target_dir: Path) -> None:
    artifact_id = int(artifact["id"])
    content = api_request(
        "GET",
        repo,
        token,
        f"/actions/artifacts/{artifact_id}/zip",
        raw=True,
    )
    extract_archive(content, target_dir)


def result_count(path: Path) -> int:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    rows = payload.get("results") if isinstance(payload, dict) else None
    return len(rows) if isinstance(rows, list) else 0


def merge_individual_results(results_dir: str, output_path: str) -> int:
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("merge_results.py")),
            results_dir,
            output_path,
        ],
        check=True,
    )
    return result_count(Path(output_path))


def target_date(orchestration_manifest: dict) -> str:
    created_at = parse_api_time(orchestration_manifest.get("created_at"))
    value = created_at or datetime.now(timezone.utc)
    return value.astimezone(LOCAL_TZ).strftime("%Y-%m-%d")


def download_results(repo: str, token: str, manifest_path: str, output_dir: str) -> dict:
    orchestration = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target_date": target_date(orchestration),
        "orchestration_id": orchestration.get("orchestration_id", ""),
        "batches": [],
    }

    for record in orchestration.get("batches", []):
        group = int(record.get("group_number") or 0)
        spec = BATCHES.get(group)
        batch = dict(record)
        batch.update({"found": False, "reason": ""})
        run_id = int(record.get("run_id") or 0)
        if not spec or run_id <= 0:
            batch["reason"] = record.get("error") or "batch run was not created"
            summary["batches"].append(batch)
            continue

        artifacts = list_artifacts(repo, token, run_id)
        batch["artifact_count"] = len(artifacts)
        group_dir = output_root / f"group{group}"
        final_artifact = next(
            (
                item
                for item in artifacts
                if not item.get("expired") and item.get("name") == spec["artifact_name"]
            ),
            None,
        )
        if final_artifact:
            download_artifact(repo, token, final_artifact, group_dir)

        final_path = group_dir / "result.json"
        final_rows = result_count(final_path)
        individual_dir = group_dir / "_individual"
        individual_count = 0
        for artifact in artifacts:
            name = str(artifact.get("name") or "")
            if artifact.get("expired") or not name.startswith(("initial-result-", "retry-result-")):
                continue
            download_artifact(repo, token, artifact, individual_dir / name)
            individual_count += 1

        merged_path = group_dir / "result.from-individual.json"
        individual_rows = (
            merge_individual_results(str(individual_dir), str(merged_path))
            if individual_count
            else 0
        )
        if individual_rows >= final_rows and individual_rows > 0:
            shutil.copyfile(merged_path, final_path)
            final_rows = individual_rows
            batch["source"] = "individual-artifacts"
        elif final_artifact:
            batch["source"] = "batch-artifact"

        if merged_path.exists():
            merged_path.unlink()
        if individual_dir.exists():
            shutil.rmtree(individual_dir)

        batch["individual_artifacts"] = individual_count
        batch["result_rows"] = final_rows
        batch["found"] = final_rows > 0
        if not batch["found"]:
            batch["reason"] = "no usable result artifacts"
        summary["batches"].append(batch)

    write_json(output_root / "manifest.json", summary)
    return summary


def check_manifest(path: str, groups: list[int]) -> list[str]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    records = {
        int(item.get("group_number") or 0): item
        for item in payload.get("batches", [])
        if isinstance(item, dict)
    }
    errors = []
    for group in groups:
        record = records.get(group)
        if not record:
            errors.append(f"batch {group}: missing orchestration record")
        elif record.get("conclusion") != "success":
            errors.append(
                f"batch {group}: conclusion={record.get('conclusion') or record.get('status') or 'unknown'}"
            )
    return errors


def required_runtime() -> tuple[str, str]:
    repo = (os.getenv("GITHUB_REPOSITORY") or "").strip()
    token = (os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or "").strip()
    if not repo or not token:
        raise RuntimeError("GITHUB_REPOSITORY and GITHUB_TOKEN are required")
    return repo, token


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run and collect sequential batch workflows")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--groups", default="5,6,7,8")
    run_parser.add_argument("--ref", default=os.getenv("GITHUB_REF_NAME") or "main")
    run_parser.add_argument("--orchestration-id", default=os.getenv("GITHUB_RUN_ID") or "")
    run_parser.add_argument("--output", default="orchestration.json")

    download_parser = subparsers.add_parser("download")
    download_parser.add_argument("--manifest", default="orchestration.json")
    download_parser.add_argument("--output-dir", default="results")

    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("--groups", default="5,6,7,8")
    check_parser.add_argument("--manifest", default="orchestration.json")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    groups = parse_groups(getattr(args, "groups", "5,6,7,8"))
    if args.command == "run":
        if not args.orchestration_id:
            raise RuntimeError("an orchestration ID is required")
        repo, token = required_runtime()
        run_batches(repo, token, args.ref, args.orchestration_id, groups, args.output)
        return 0
    if args.command == "download":
        repo, token = required_runtime()
        download_results(repo, token, args.manifest, args.output_dir)
        return 0

    errors = check_manifest(args.manifest, groups)
    for error in errors:
        print(f"::error::{error}")
    return 1 if errors else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"::error::{error}")
        sys.exit(1)
