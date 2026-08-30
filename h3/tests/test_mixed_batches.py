import json
import os
import io
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from h3.mixed_batches import (
    account_metadata,
    batch_accounts,
    configured_accounts,
    deterministic_order,
    download_batches,
    encode_chain_state,
    load_chain_state,
    new_chain_state,
    split_batches,
)
from h3.mixed_results import (
    build_retry_matrix,
    merge_results,
    stamp_result,
)
from h3.report import stable_account_order


def env_slots(**configured):
    values = {
        f"{prefix}{index}": ""
        for prefix in ("old", "new", "ll", "zh")
        for index in range(1, 21)
    }
    values.update(configured)
    return values


def complete_row(source_group: str, account_index: int, *, vote_success=True) -> dict:
    return {
        "source_group": source_group,
        "group_code": source_group,
        "account_index": account_index,
        "batch_id": "batch-1",
        "execution_order": account_index,
        "account_category": "老号全干组" if source_group.startswith("old") else "新号全干组",
        "execution_mode": "full",
        "sign_skipped": False,
        "token_extracted": True,
        "sign_success": True,
        "points_fetch_success": True,
        "activity_fetch_success": True,
        "account_data_required": True,
        "account_data_fetch_success": True,
        "account_data": {
            "invoice_fetch_success": True,
            "pcb_order_fetch_success": True,
            "coupon_fetch_success": True,
        },
        "listing_gift_required": False,
        "vote_required": True,
        "vote_success": vote_success,
        "vote_status": "投票成功" if vote_success else "投票失败：网络超时",
        "data_fetch_completed": True,
        "activity_records": {"seckill": [], "lottery": [], "exchange": []},
    }


def write_result(root: Path, name: str, row: dict) -> None:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "result.json").write_text(
        json.dumps({"results": [row]}, ensure_ascii=False), encoding="utf-8"
    )


def test_configured_pool_mixes_sources_without_test_secret():
    values = env_slots(
        old3="old-a,p\nold-b,p",
        new2="new-a,p",
        ll1="peer-a,p",
        test="must-not-appear,p",
    )
    with patch.dict(os.environ, values, clear=False):
        accounts = configured_accounts()
    assert {(item["source_group"], item["account_index"]) for item in accounts} == {
        ("old3", 1),
        ("old3", 2),
        ("new2", 1),
        ("ll1", 1),
    }
    assert next(item for item in accounts if item["source_group"] == "ll1")["skip_sign"] is True
    assert next(item for item in accounts if item["source_group"] == "new2")["execution_mode"] == "full"


def test_date_seed_is_stable_and_changes_on_another_day():
    accounts = [
        account_metadata("old1", index) for index in range(1, 21)
    ] + [account_metadata("new2", index) for index in range(1, 21)]
    first = deterministic_order(accounts, "2026-08-31")
    repeated = deterministic_order(list(reversed(accounts)), "2026-08-31")
    another_day = deterministic_order(accounts, "2026-09-01")
    def identity(rows):
        return [(row["source_group"], row["account_index"]) for row in rows]

    assert identity(first) == identity(repeated)
    assert identity(first) != identity(another_day)
    assert [row["execution_order"] for row in first] == list(range(1, 41))


def test_batch_boundaries_for_1_20_220_221_440_accounts():
    for count, expected in (
        (1, [1]),
        (20, [20]),
        (220, [220]),
        (221, [220, 1]),
        (440, [220, 220]),
    ):
        accounts = [account_metadata("old1", index) for index in range(1, count + 1)]
        batches = split_batches(accounts)
        assert [batch["account_count"] for batch in batches] == expected
        assert all(len(batch["accounts"]) <= 220 for batch in batches)


def test_chain_state_is_compressed_and_contains_no_credentials():
    values = env_slots(old1="visible-account,visible-password", new1="new-account,new-password")
    with patch.dict(os.environ, values, clear=False):
        state = new_chain_state("123", "main", "2026-08-31")
    encoded = encode_chain_state(state)
    restored = load_chain_state(encoded)
    serialized = json.dumps(restored, ensure_ascii=False)
    assert encoded.startswith("z1:")
    assert restored == state
    assert "visible-account" not in serialized
    assert "visible-password" not in serialized
    assert "username" not in serialized.lower()
    assert "password" not in serialized.lower()


def test_compact_state_scales_without_embedding_every_account():
    batches = []
    total = 16_000
    for number, start in enumerate(range(1, total + 1, 220), start=1):
        count = min(220, total - start + 1)
        batches.append(
            {
                "batch_id": f"batch-{number}",
                "batch_number": number,
                "account_count": count,
                "start_order": start,
                "end_order": start + count - 1,
                "run_id": 0,
                "handoff_status": "pending",
            }
        )
    state = {
        "schema_version": 3,
        "workflow_mode": "mixed_batches",
        "orchestration_id": "large",
        "task_start_date": "2026-08-31",
        "random_seed": "2026-08-31",
        "batch_size": 220,
        "ref": "main",
        "total_accounts": total,
        "group_counts": {"old1": total},
        "groups": [
            {"group_code": "old1", "account_category": "老号全干组", "account_count": total}
        ],
        "batches": batches,
    }
    encoded = encode_chain_state(state)
    assert len(encoded) < 10_000
    assert load_chain_state(encoded)["total_accounts"] == total


def test_batch_accounts_are_rebuilt_from_frozen_counts():
    values = env_slots(old3="a,p\nb,p", new2="c,p", ll1="d,p")
    with patch.dict(os.environ, values, clear=False):
        state = new_chain_state("123", "main", "2026-08-31")
    first = batch_accounts(state, "batch-1")
    assert len(first) == 4
    assert {item["source_group"] for item in first} == {"old3", "new2", "ll1"}
    assert all(item["batch_id"] == "batch-1" for item in first)


def test_retry_matrix_uses_composite_identity_and_only_shrinks(tmp_path):
    old = {**account_metadata("old1", 1), "batch_id": "batch-1", "execution_order": 1}
    new = {**account_metadata("new1", 1), "batch_id": "batch-1", "execution_order": 2}
    write_result(tmp_path, "old", complete_row("old1", 1))
    write_result(tmp_path, "new", complete_row("new1", 1, vote_success=False))

    first = build_retry_matrix(tmp_path, [old, new], task_date="2026-08-31")
    assert [(item["source_group"], item["account_index"]) for item in first] == [("new1", 1)]
    assert first[0]["retry_components"] == "vote"

    missing_retry_dir = tmp_path / "missing"
    missing_retry_dir.mkdir()
    second = build_retry_matrix(
        missing_retry_dir,
        [old, new],
        candidate_accounts=first,
        task_date="2026-08-31",
    )
    assert len(second) == 1
    assert second[0]["retry_components"] == "vote"

    expanded = complete_row("new1", 1, vote_success=True)
    expanded["account_data"]["invoice_fetch_success"] = False
    write_result(tmp_path, "next", expanded)
    third = build_retry_matrix(
        tmp_path / "next",
        [old, new],
        candidate_accounts=second,
        task_date="2026-08-31",
    )
    assert third == []


def test_merge_keeps_same_account_index_from_different_groups(tmp_path):
    old = {**account_metadata("old1", 1), "batch_id": "batch-1", "execution_order": 1}
    new = {**account_metadata("new1", 1), "batch_id": "batch-1", "execution_order": 2}
    write_result(tmp_path, "old-result", complete_row("old1", 1))
    write_result(tmp_path, "new-result", complete_row("new1", 1))
    output = tmp_path / "merged.json"
    merge_results(tmp_path, output, [old, new], "2026-08-31")
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert len(payload["results"]) == 2
    assert {(row["source_group"], row["account_index"]) for row in payload["results"]} == {
        ("old1", 1),
        ("new1", 1),
    }


def test_stamp_removes_account_and_password_fields(tmp_path):
    path = tmp_path / "result.json"
    path.write_text(
        json.dumps(
            {
                "username": "credential-user-value",
                "password": "credential-pass-value",
                "results": [
                    {
                        **complete_row("old1", 1),
                        "username": "credential-user-value",
                        "masked_username": "***ount",
                        "password": "credential-pass-value",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with patch.dict(
        os.environ,
        {
            "SOURCE_GROUP": "old1",
            "GROUP_CODE": "old1",
            "ACCOUNT_INDEX": "1",
            "EXECUTION_ORDER": "9",
            "BATCH_ID": "batch-1",
            "ACCOUNT_CATEGORY": "老号全干组",
            "EXECUTION_MODE": "full",
            "SKIP_SIGN": "false",
        },
        clear=False,
    ):
        stamp_result(path)
    text = path.read_text(encoding="utf-8")
    payload = json.loads(text)
    assert "credential-user-value" not in text
    assert "credential-pass-value" not in text
    assert payload["results"][0]["execution_order"] == 9
    assert payload["results"][0]["source_group"] == "old1"


def test_workflow_uses_mixed_batches_and_three_scoped_retries():
    root = Path(__file__).resolve().parents[2]
    controller = (root / ".github/workflows/dynamic-controller.yml").read_text(encoding="utf-8")
    workflow = (root / ".github/workflows/dynamic-batch.yml").read_text(encoding="utf-8")
    assert "python h3/mixed_batches.py start" in controller
    assert "python h3/dynamic_groups.py start" not in controller
    assert workflow.count("max-parallel: 20") == 4
    assert "needs: [prepare, prepare_retry, retry]" in workflow
    assert "needs: [prepare, prepare_retry2, retry2]" in workflow
    assert "--candidate-matrix-env RETRY_CANDIDATE_MATRIX" in workflow
    assert "secrets.TEST" not in workflow


def test_summary_downloads_exact_batch_run_artifact(tmp_path):
    state = {
        "schema_version": 3,
        "workflow_mode": "mixed_batches",
        "orchestration_id": "123",
        "task_start_date": "2026-08-31",
        "random_seed": "2026-08-31",
        "batch_size": 220,
        "ref": "main",
        "total_accounts": 1,
        "group_counts": {"old1": 1},
        "groups": [
            {"group_code": "old1", "account_category": "老号全干组", "account_count": 1}
        ],
        "batches": [
            {
                "batch_id": "batch-1",
                "batch_number": 1,
                "account_count": 1,
                "start_order": 1,
                "end_order": 1,
                "run_id": 456,
                "handoff_status": "finalized",
            }
        ],
    }
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("result.json", json.dumps({"results": []}))

    def fake_api(method, repo, token, path, **kwargs):
        if path == "/actions/runs/456":
            return {"status": "completed", "conclusion": "success", "html_url": "run-url"}
        if path == "/actions/runs/456/artifacts":
            return {
                "artifacts": [
                    {"id": 99, "name": "batch-result-123-batch-1", "expired": False}
                ]
            }
        if path == "/actions/artifacts/99/zip":
            return archive.getvalue()
        raise AssertionError(path)

    with patch.dict(
        os.environ,
        {
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_TOKEN": "token",
            "GITHUB_API_URL": "https://api.example",
        },
        clear=False,
    ), patch("h3.mixed_batches.api_request", side_effect=fake_api):
        args = SimpleNamespace(
            chain_state=encode_chain_state(state),
            chain_state_env="",
            output_dir=str(tmp_path / "results"),
        )
        assert download_batches(args) == 0
    manifest = json.loads((tmp_path / "results/manifest.json").read_text(encoding="utf-8"))
    assert manifest["batches"][0]["artifact_downloaded"] is True
    assert (tmp_path / "results/batch-1/result.json").exists()


def test_report_order_ignores_random_execution_order():
    records = [
        {"group_code": "new1", "account_index": 2, "execution_order": 1},
        {"group_code": "old10", "account_index": 1, "execution_order": 2},
        {"group_code": "old2", "account_index": 2, "execution_order": 3},
        {"group_code": "old2", "account_index": 1, "execution_order": 4},
        {"group_code": "ll1", "account_index": 1, "execution_order": 5},
    ]
    ordered = stable_account_order(records)
    assert [(row["group_code"], row["account_index"]) for row in ordered] == [
        ("old2", 1),
        ("old2", 2),
        ("old10", 1),
        ("new1", 2),
        ("ll1", 1),
    ]
