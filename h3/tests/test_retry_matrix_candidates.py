import json
from pathlib import Path

from h3.retry_components import build_retry_matrix


def _complete_row(account_index: int) -> dict:
    return {
        "account_index": account_index,
        "token_extracted": True,
        "sign_success": True,
        "points_fetch_success": True,
        "activity_fetch_success": True,
        "account_data_fetch_success": True,
        "account_data": {
            "invoice_fetch_success": True,
            "pcb_order_fetch_success": True,
            "coupon_fetch_success": True,
        },
        "vote_required": False,
        "listing_gift_required": False,
    }


def test_later_retry_round_only_considers_previous_matrix(tmp_path):
    (tmp_path / "result.json").write_text(
        json.dumps({"results": [_complete_row(2)]}), encoding="utf-8"
    )

    matrix = build_retry_matrix(str(tmp_path), 206, [2, 5])

    # Account 2 completed; missing account 5 is the only candidate to retry.
    assert [item["account_index"] for item in matrix] == [5]


def test_retry_preparation_jobs_receive_previous_matrix_output():
    workflow = (
        Path(__file__).resolve().parents[2] / ".github" / "workflows" / "dynamic-group.yml"
    ).read_text(encoding="utf-8")

    assert "needs: [prepare, prepare_retry, retry]" in workflow
    assert "needs: [prepare, prepare_retry2, retry2]" in workflow
