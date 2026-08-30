import argparse
import base64
import hashlib
import json
import os
import zlib
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from dynamic_groups import (
        api_request,
        dispatch_once,
        safe_extract,
        write_json,
    )
except ImportError:
    from h3.dynamic_groups import (
        api_request,
        dispatch_once,
        safe_extract,
        write_json,
    )


GROUP_PREFIXES = ("old", "new", "ll", "zh")
GROUP_CODES = tuple(
    f"{prefix}{index}" for prefix in GROUP_PREFIXES for index in range(1, 21)
)
BATCH_SIZE = 220
BATCH_WORKFLOW_FILE = "dynamic-batch.yml"
SUMMARY_WORKFLOW_FILE = "dynamic-summary.yml"
STATE_PREFIX = "z1:"
MAX_DISPATCH_STATE_LENGTH = 60_000
FORBIDDEN_STATE_KEYS = {
    "username",
    "password",
    "credential",
    "credentials",
    "cookie",
    "cookies",
    "token",
}


def category_for(source_group: str) -> str:
    if source_group.startswith("old"):
        return "老号全干组"
    if source_group.startswith("new"):
        return "新号全干组"
    if source_group.startswith(("ll", "zh")):
        return "同行不签到组"
    raise ValueError(f"unsupported source group: {source_group}")


def account_count(raw: str) -> int:
    return sum(
        1
        for line in str(raw or "").splitlines()
        if line.strip() and "," in line
    )


def account_metadata(source_group: str, account_index: int) -> dict:
    skip_sign = source_group.startswith(("ll", "zh"))
    return {
        "source_group": source_group,
        "account_index": int(account_index),
        "account_category": category_for(source_group),
        "execution_mode": "skip_sign" if skip_sign else "full",
        "skip_sign": skip_sign,
    }


def configured_accounts() -> list[dict]:
    accounts = []
    for source_group in GROUP_CODES:
        count = account_count(os.getenv(source_group, ""))
        for account_index in range(1, count + 1):
            accounts.append(account_metadata(source_group, account_index))
    return accounts


def configured_group_counts() -> dict[str, int]:
    return {
        source_group: count
        for source_group in GROUP_CODES
        if (count := account_count(os.getenv(source_group, ""))) > 0
    }


def accounts_from_group_counts(counts: dict[str, int]) -> list[dict]:
    accounts = []
    for source_group in GROUP_CODES:
        for account_index in range(1, int(counts.get(source_group, 0)) + 1):
            accounts.append(account_metadata(source_group, account_index))
    return accounts


def deterministic_order(accounts: list[dict], task_date: str) -> list[dict]:
    def order_key(account: dict) -> bytes:
        identity = f"{task_date}:{account['source_group']}:{account['account_index']}"
        return hashlib.sha256(identity.encode("utf-8")).digest()

    ordered = [deepcopy(account) for account in sorted(accounts, key=order_key)]
    for execution_order, account in enumerate(ordered, start=1):
        account["execution_order"] = execution_order
    return ordered


def split_batches(accounts: list[dict], batch_size: int = BATCH_SIZE) -> list[dict]:
    if batch_size <= 0 or batch_size > 220:
        raise ValueError("batch_size must be between 1 and 220")
    batches = []
    for offset in range(0, len(accounts), batch_size):
        batch_number = len(batches) + 1
        batch_id = f"batch-{batch_number}"
        selected = [deepcopy(item) for item in accounts[offset : offset + batch_size]]
        for item in selected:
            item["batch_id"] = batch_id
        batches.append(
            {
                "batch_id": batch_id,
                "batch_number": batch_number,
                "account_count": len(selected),
                "run_id": 0,
                "handoff_status": "pending",
                "accounts": selected,
            }
        )
    return batches


def _assert_no_credentials(value, path="state") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).strip().lower()
            if normalized in FORBIDDEN_STATE_KEYS:
                raise ValueError(f"credential-like key is forbidden in chain state: {path}.{key}")
            _assert_no_credentials(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _assert_no_credentials(nested, f"{path}[{index}]")


def group_counts_from_batches(batches: list[dict]) -> dict[str, int]:
    counts = {}
    for batch in batches:
        for account in batch.get("accounts") or []:
            source_group = str(account.get("source_group") or "").strip().lower()
            account_index = int(account.get("account_index") or 0)
            counts[source_group] = max(counts.get(source_group, 0), account_index)
    return counts


def report_groups_from_batches(batches: list[dict]) -> list[dict]:
    counts = group_counts_from_batches(batches)
    return [
        {
            "group_code": source_group,
            "account_category": category_for(source_group),
            "account_count": counts[source_group],
        }
        for source_group in GROUP_CODES
        if counts.get(source_group, 0) > 0
    ]


def new_chain_state(
    orchestration_id: str,
    ref: str,
    task_start_date: str = "",
    batch_size: int = BATCH_SIZE,
) -> dict:
    task_date = task_start_date or datetime.now(
        timezone(timedelta(hours=8))
    ).strftime("%Y-%m-%d")
    group_counts = configured_group_counts()
    accounts = deterministic_order(accounts_from_group_counts(group_counts), task_date)
    expanded_batches = split_batches(accounts, batch_size)
    batches = [
        {
            key: value
            for key, value in batch.items()
            if key != "accounts"
        }
        for batch in expanded_batches
    ]
    for batch in batches:
        start_order = (batch["batch_number"] - 1) * batch_size + 1
        batch["start_order"] = start_order
        batch["end_order"] = start_order + batch["account_count"] - 1
    state = {
        "schema_version": 3,
        "workflow_mode": "mixed_batches",
        "orchestration_id": str(orchestration_id),
        "task_start_date": task_date,
        "random_seed": task_date,
        "batch_size": batch_size,
        "ref": ref or "main",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "total_accounts": len(accounts),
        "group_counts": group_counts,
        "groups": [
            {
                "group_code": source_group,
                "account_category": category_for(source_group),
                "account_count": count,
            }
            for source_group, count in group_counts.items()
        ],
        "batches": batches,
    }
    _assert_no_credentials(state)
    return state


def encode_chain_state(state: dict) -> str:
    _assert_no_credentials(state)
    raw = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    encoded = STATE_PREFIX + base64.urlsafe_b64encode(zlib.compress(raw, level=9)).decode("ascii")
    if len(encoded) > MAX_DISPATCH_STATE_LENGTH:
        raise ValueError(
            f"compressed chain state is too large for workflow_dispatch: {len(encoded)} characters"
        )
    return encoded


def load_chain_state(raw: str) -> dict:
    text = str(raw or "").strip()
    try:
        if text.startswith(STATE_PREFIX):
            compressed = base64.urlsafe_b64decode(text[len(STATE_PREFIX) :].encode("ascii"))
            text = zlib.decompress(compressed).decode("utf-8")
        state = json.loads(text)
    except (ValueError, TypeError, UnicodeDecodeError, zlib.error) as exc:
        raise ValueError("mixed chain_state is invalid") from exc
    if not isinstance(state, dict) or state.get("workflow_mode") != "mixed_batches":
        raise ValueError("chain_state is not a mixed batch state")
    batches = state.get("batches")
    if not isinstance(batches, list):
        raise ValueError("chain_state batches must be a list")
    seen_batches = set()
    group_counts = state.get("group_counts")
    if not isinstance(group_counts, dict):
        raise ValueError("chain_state group_counts must be an object")
    for source_group, count in group_counts.items():
        if source_group not in GROUP_CODES or int(count) <= 0:
            raise ValueError(f"invalid frozen group count: {source_group}={count}")
    expected_total = sum(int(value) for value in group_counts.values())
    if expected_total != int(state.get("total_accounts") or 0):
        raise ValueError("chain_state group counts do not match total_accounts")
    for batch_number, batch in enumerate(batches, start=1):
        batch_id = str(batch.get("batch_id") or "").strip()
        if batch_id != f"batch-{batch_number}" or batch_id in seen_batches:
            raise ValueError(f"invalid or duplicate batch id: {batch_id}")
        seen_batches.add(batch_id)
        count = int(batch.get("account_count") or 0)
        if count <= 0 or count > BATCH_SIZE:
            raise ValueError(f"invalid account count for {batch_id}")
    if sum(int(batch.get("account_count") or 0) for batch in batches) != expected_total:
        raise ValueError("chain_state batch counts do not match total_accounts")
    _assert_no_credentials(state)
    return state


def batch_by_id(state: dict, batch_id: str) -> dict:
    for batch in state["batches"]:
        if batch.get("batch_id") == batch_id:
            return batch
    raise ValueError(f"batch is not present in chain state: {batch_id}")


def batch_accounts(state: dict, batch_id: str) -> list[dict]:
    batch = batch_by_id(state, batch_id)
    ordered = deterministic_order(
        accounts_from_group_counts(state.get("group_counts") or {}),
        str(state.get("random_seed") or state.get("task_start_date") or ""),
    )
    start = int(batch.get("start_order") or 1) - 1
    selected = [deepcopy(item) for item in ordered[start : start + batch["account_count"]]]
    for item in selected:
        item["batch_id"] = batch_id
    return selected


def github_context() -> tuple[str, str]:
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ.get("GITHUB_TOKEN") or os.environ["GH_TOKEN"]
    return repo, token


def dispatch_batch(state: dict, batch_id: str) -> dict:
    repo, token = github_context()
    orchestration_id = str(state["orchestration_id"])
    ref = str(state.get("ref") or os.getenv("GITHUB_REF_NAME") or "main")
    title = f"mixed-{orchestration_id}-{batch_id}"
    return dispatch_once(
        repo,
        token,
        ref,
        BATCH_WORKFLOW_FILE,
        title,
        {
            "orchestration_id": orchestration_id,
            "batch_id": batch_id,
            "task_start_date": str(state.get("task_start_date") or ""),
            "continue_chain": "true",
            "chain_state": encode_chain_state(state),
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
        {
            "orchestration_id": orchestration_id,
            "chain_state": encode_chain_state(state),
            "chain_mode": "mixed_batches",
        },
    )


def start_chain(args) -> int:
    state = new_chain_state(
        args.orchestration_id,
        args.ref,
        args.task_start_date,
        args.batch_size,
    )
    write_json(args.output, state)
    if state["batches"]:
        dispatch_batch(state, state["batches"][0]["batch_id"])
    else:
        dispatch_summary(state)
    print(
        f"[mixed] started {len(state['batches'])} batches for {state['total_accounts']} accounts",
        flush=True,
    )
    return 0


def advance_chain(args) -> int:
    raw = os.environ.get(args.chain_state_env, "") if args.chain_state_env else args.chain_state
    state = load_chain_state(raw)
    current = batch_by_id(state, args.current_batch)
    current["run_id"] = int(args.current_run_id)
    current["handoff_status"] = "finalized"
    current["finalized_at"] = datetime.now(timezone.utc).isoformat()
    write_json(args.output, state)
    current_index = state["batches"].index(current)
    if current_index + 1 < len(state["batches"]):
        dispatch_batch(state, state["batches"][current_index + 1]["batch_id"])
    else:
        state["completed_at"] = datetime.now(timezone.utc).isoformat()
        write_json(args.output, state)
        dispatch_summary(state)
    return 0


def output_batch_matrix(args) -> int:
    raw = os.environ.get(args.chain_state_env, "") if args.chain_state_env else args.chain_state
    state = load_chain_state(raw)
    accounts = batch_accounts(state, args.batch_id)
    matrix = {"include": accounts}
    output = os.environ.get("GITHUB_OUTPUT")
    values = {
        "matrix": json.dumps(matrix, ensure_ascii=False, separators=(",", ":")),
        "count": str(len(accounts)),
    }
    if output:
        with open(output, "a", encoding="utf-8") as file:
            for key, value in values.items():
                file.write(f"{key}={value}\n")
    else:
        print(json.dumps(values, ensure_ascii=False))
    return 0


def download_batches(args) -> int:
    repo, token = github_context()
    raw = os.environ.get(args.chain_state_env, "") if args.chain_state_env else args.chain_state
    manifest = load_chain_state(raw)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    orchestration_id = str(manifest["orchestration_id"])
    for batch in manifest["batches"]:
        run_id = int(batch.get("run_id") or 0)
        batch["artifact_downloaded"] = False
        if not run_id:
            batch["artifact_error"] = "batch run id is missing"
            continue
        try:
            run = api_request("GET", repo, token, f"/actions/runs/{run_id}")
            batch["run_status"] = str(run.get("status") or "")
            batch["conclusion"] = str(run.get("conclusion") or "")
            batch["run_url"] = str(run.get("html_url") or "")
            expected = f"batch-result-{orchestration_id}-{batch['batch_id']}"
            artifact = None
            for page in range(1, 21):
                data = api_request(
                    "GET",
                    repo,
                    token,
                    f"/actions/runs/{run_id}/artifacts",
                    params={"per_page": 100, "page": page},
                )
                rows = data.get("artifacts", [])
                artifact = next(
                    (
                        item
                        for item in rows
                        if item.get("name") == expected and not item.get("expired")
                    ),
                    None,
                )
                if artifact or len(rows) < 100:
                    break
            if not artifact:
                raise FileNotFoundError("exact batch artifact not found")
            content = api_request(
                "GET", repo, token, f"/actions/artifacts/{artifact['id']}/zip", raw=True
            )
            safe_extract(content, output / batch["batch_id"])
            batch["artifact_downloaded"] = True
            batch.pop("artifact_error", None)
        except Exception as exc:
            batch["artifact_error"] = str(exc)
            print(f"::warning::{batch['batch_id']}: {exc}", flush=True)
    manifest["summary_downloaded_at"] = datetime.now(timezone.utc).isoformat()
    write_json(output / "manifest.json", manifest)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    start = commands.add_parser("start")
    start.add_argument("--orchestration-id", required=True)
    start.add_argument("--ref", default=os.getenv("GITHUB_REF_NAME") or "main")
    start.add_argument("--task-start-date", default="")
    start.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    start.add_argument("--output", default="chain-state.json")

    advance = commands.add_parser("advance")
    advance.add_argument("--chain-state", default="")
    advance.add_argument("--chain-state-env", default="")
    advance.add_argument("--current-batch", required=True)
    advance.add_argument("--current-run-id", required=True, type=int)
    advance.add_argument("--output", default="chain-state.json")

    matrix = commands.add_parser("matrix")
    matrix.add_argument("--chain-state", default="")
    matrix.add_argument("--chain-state-env", default="")
    matrix.add_argument("--batch-id", required=True)

    download = commands.add_parser("download")
    download.add_argument("--chain-state", default="")
    download.add_argument("--chain-state-env", default="")
    download.add_argument("--output-dir", default="results")

    args = parser.parse_args()
    if args.command == "start":
        return start_chain(args)
    if args.command == "advance":
        return advance_chain(args)
    if args.command == "matrix":
        return output_batch_matrix(args)
    return download_batches(args)


if __name__ == "__main__":
    raise SystemExit(main())
