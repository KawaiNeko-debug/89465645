import glob
import json
import os
import sys
from pathlib import Path


COMPONENTS = (
    "sign",
    "points",
    "invoice",
    "pcb_orders",
    "coupons",
    "lottery",
    "exchange",
    "gift",
    "vote",
)


def truthy(value) -> bool:
    if value is True:
        return True
    if value is False or value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def vote_is_terminal_conflict(row: dict) -> bool:
    text = f"{row.get('vote_status', '')} {row.get('vote_detail', '')}"
    return "本期已锁定其他商品" in text


def vote_is_terminal_insufficient_points(row: dict) -> bool:
    """Treat an insufficient-points response as a terminal business result."""
    text = f"{row.get('vote_status', '')} {row.get('vote_detail', '')}"
    return "\u91d1\u8c46\u4e0d\u8db3" in text


def component_status(row: dict | None) -> dict[str, bool]:
    row = row if isinstance(row, dict) else {}
    stored = row.get("component_status") if isinstance(row.get("component_status"), dict) else {}
    account_data = row.get("account_data") if isinstance(row.get("account_data"), dict) else {}
    sign_complete = (
        truthy(row.get("sign_success"))
        or truthy(row.get("sign_skipped"))
        or truthy(row.get("risk_controlled"))
        or truthy(row.get("banned_account"))
    )
    vote_complete = (
        not truthy(row.get("vote_required"))
        or truthy(row.get("vote_success"))
        or vote_is_terminal_conflict(row)
        or vote_is_terminal_insufficient_points(row)
    )
    gift_complete = not truthy(row.get("listing_gift_required")) or truthy(
        row.get("listing_gift_success")
    )
    activity_success = truthy(row.get("activity_fetch_success"))
    result = {
        "login": truthy(row.get("token_extracted")),
        "sign": sign_complete,
        "points": truthy(row.get("points_fetch_success")),
        "invoice": truthy(account_data.get("invoice_fetch_success")),
        "pcb_orders": truthy(account_data.get("pcb_order_fetch_success")),
        "coupons": truthy(account_data.get("coupon_fetch_success")),
        "lottery": activity_success,
        "exchange": activity_success,
        "gift": gift_complete,
        "vote": vote_complete,
    }
    for key in result:
        if key in stored:
            result[key] = result[key] or truthy(stored[key])
    return result


def retry_components(row: dict | None) -> list[str]:
    row = row if isinstance(row, dict) else {}
    if truthy(row.get("password_error")):
        return []
    status = component_status(row)
    return [name for name in COMPONENTS if not status.get(name, False)]


def needs_retry(row: dict | None) -> bool:
    return bool(retry_components(row))


def load_result(path: str | Path) -> dict | None:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return None
    return rows[0]


def _candidate_indexes_from_env() -> list[int] | None:
    """Return only the accounts that entered the immediately previous retry round."""
    raw = os.environ.get("RETRY_CANDIDATE_MATRIX", "").strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return []
    include = payload.get("include") if isinstance(payload, dict) else None
    if not isinstance(include, list):
        return []
    indexes = []
    for item in include:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("account_index") or 0)
        except (TypeError, ValueError):
            continue
        if index > 0 and index not in indexes:
            indexes.append(index)
    return indexes


def build_retry_matrix(
    results_dir: str | Path,
    expected_count: int,
    candidate_indexes: list[int] | None = None,
) -> list[dict]:
    rows = {}
    pattern = os.path.join(str(results_dir), "**", "result.json")
    for path in glob.glob(pattern, recursive=True):
        row = load_result(path)
        if not row:
            continue
        try:
            account_index = int(row.get("account_index") or 0)
        except (TypeError, ValueError):
            continue
        if account_index > 0:
            rows[account_index] = row

    matrix = []
    # The first retry round may use the full account count because a missing
    # initial artifact means that account never completed. Later rounds must
    # be restricted to the previous retry matrix; otherwise skipped accounts
    # are incorrectly reintroduced as failures.
    all_indexes = (
        candidate_indexes
        if candidate_indexes is not None
        else (range(1, max(0, expected_count) + 1) if expected_count else sorted(rows))
    )
    for account_index in all_indexes:
        row = rows.get(account_index)
        components = list(COMPONENTS) if row is None else retry_components(row)
        if components:
            matrix.append(
                {
                    "account_index": account_index,
                    "retry_components": ",".join(components),
                }
            )
    return matrix


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] != "matrix":
        raise SystemExit("usage: retry_components.py matrix RESULTS_DIR [EXPECTED_COUNT]")
    results_dir = sys.argv[2] if len(sys.argv) > 2 else "initial-results"
    expected_count = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    matrix = build_retry_matrix(results_dir, expected_count, _candidate_indexes_from_env())
    output = os.environ.get("GITHUB_OUTPUT")
    values = {
        "matrix": json.dumps({"include": matrix}, ensure_ascii=False, separators=(",", ":")),
        "has_failed": "true" if matrix else "false",
    }
    if output:
        with open(output, "a", encoding="utf-8") as file:
            for key, value in values.items():
                file.write(f"{key}={value}\n")
    else:
        print(json.dumps(values, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
