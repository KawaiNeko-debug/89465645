import importlib
import json
from copy import deepcopy
from unittest.mock import Mock

import pytest
from openpyxl import load_workbook

from h3.account_data import empty_account_data
from h3.batch_runner import normalize_result
from h3.goldbean_expiry import (
    empty_points_expiry, merge_points_expiry, normalize_points_expiry,
    parse_points_expiry, points_expiring_in_2026,
)
from h3.merge_results import pick_result
from h3.mixed_results import merge_results, missing_result, stamp_result
from h3.report import normalize_record, write_xlsx
from h3.retry_components import retry_components
from h3.seventh_reward import SHELL_COUPON


def overview(amount=25, expires="2026-12-31"):
    return {"success": True, "code": 200, "data": {
        "integralVoucher": 1000, "deadlineExpireNum": amount, "expireTime": expires,
        "basicIntegral": 77,
    }}


@pytest.mark.parametrize("amount", [0, 25, "120.5", "1,234"])
def test_official_overview_preserves_zero_and_numbers(amount):
    expiry = parse_points_expiry(overview(amount))
    assert expiry["fetch_success"]
    assert points_expiring_in_2026(expiry) == float(str(amount).replace(",", ""))
    assert set(expiry) == {"fetch_success", "expire_date", "expiring_points", "fetched_at"}


@pytest.mark.parametrize("amount", [None, "", "not-a-number", -1, True, "NaN", "Infinity", "1e1000"])
def test_bad_expiry_amount_is_unavailable_not_zero(amount):
    assert points_expiring_in_2026(parse_points_expiry(overview(amount))) is None


@pytest.mark.parametrize("response", [None, {}, {"success": False, "data": overview()["data"]},
                                        {"code": 401, "data": overview()["data"]},
                                        {"success": True, "data": {"integralVoucher": 100}}])
def test_missing_or_failed_response_never_means_no_expiring_points(response):
    assert parse_points_expiry(response) == empty_points_expiry()


@pytest.mark.parametrize(("expires", "expected"), [
    ("2026-12-31", 25), ("2027-12-31", 0), ("2025-12-31", None),
    ("2026-12-30", None), ("2026-12-31T16:00:00Z", 0),
    ("2026-12-31T15:59:59Z", 25), ("invalid", None),
])
def test_target_date_and_shanghai_boundaries(expires, expected):
    assert points_expiring_in_2026(parse_points_expiry(overview(expires=expires))) == expected


def snapshot(amount, timestamp):
    value = parse_points_expiry(overview(amount))
    value["fetched_at"] = timestamp
    return value


def test_merge_uses_latest_valid_snapshot_even_when_latest_count_is_zero():
    older = snapshot(100, "2026-11-14T01:00:00Z")
    newer = snapshot(0, "2026-11-14T02:00:00Z")
    for values in ((older, newer), (newer, older), ({}, older, newer)):
        assert points_expiring_in_2026(merge_points_expiry(*values)) == 0
    assert merge_points_expiry(empty_points_expiry(), older) == normalize_points_expiry(older)
    initial = {"sign_success": True, "final_points": 100, "points_expiry": older}
    retry = {"sign_success": False, "retry_count": 1, "points_expiry": newer}
    assert points_expiring_in_2026(pick_result(initial, retry)["points_expiry"]) == 0


def script_module(monkeypatch):
    for key, value in {
        "BASE_URL": "https://example.invalid", "PASSPORT_URL": "https://example.invalid",
        "REFERER": "https://example.invalid", "SLIDER_ID": "slider", "WRAPPER_ID": "wrapper",
        "HEADER_CLIENT_TYPE": "client", "HEADER_ACCESS_TOKEN": "access", "TOKEN_KEY": "token",
        "GROUP_CODE": "wudi1",
    }.items():
        monkeypatch.setenv(key, value)
    return importlib.import_module("h3.script")


@pytest.mark.parametrize("channel", ["requests", "browser", "refresh"])
def test_existing_points_request_collects_expiry_without_extra_request(monkeypatch, channel):
    script = script_module(monkeypatch)
    client = script.ApiClient("synthetic-access", "", 1, Mock())
    response = overview(42)
    client._get_json_once = Mock(return_value=response if channel == "requests" else None)
    client._post_json_once = Mock(return_value=None)
    client._browser_fetch_json_once = Mock(return_value=response if channel == "browser" else None)
    client._refresh_token = Mock(return_value=channel == "refresh")
    client.get_json_retry1 = Mock(return_value=response)
    assert client.get_points() == 1000
    assert points_expiring_in_2026(client.points_expiry) == 42
    if channel == "requests":
        client._get_json_once.assert_called_once()
        client._post_json_once.assert_not_called()
        client._browser_fetch_json_once.assert_not_called()
        client._refresh_token.assert_not_called()


def test_balance_success_does_not_depend_on_optional_expiry_metadata(monkeypatch):
    script = script_module(monkeypatch)
    client = script.ApiClient("synthetic-access", "", 1, Mock())
    client._get_json_once = Mock(return_value={"success": True, "data": {"integralVoucher": 0}})
    assert client.get_points() == 0
    assert client.points_fetch_success
    assert not client.points_expiry["fetch_success"]
    legacy = {"points_fetch_success": True}
    assert retry_components(legacy) == retry_components({**legacy, "points_expiry": empty_points_expiry()})


def test_result_serialization_batch_and_mixed_report_keep_expiry(tmp_path, monkeypatch):
    script = script_module(monkeypatch)
    expiry = parse_points_expiry(overview(19))
    row = {"account_index": 1, "sign_success": True, "points_fetch_success": True,
           "final_points": 1000, "points_expiry": expiry}
    path = tmp_path / "single.json"
    script.write_results_json(str(path), [row], 1)
    raw = json.loads(path.read_text(encoding="utf-8"))["results"][0]
    assert points_expiring_in_2026(raw["points_expiry"]) == 19
    normalized = normalize_result({"account_index": 1, "username": "fixture-customer"}, str(path))
    assert points_expiring_in_2026(normalized["points_expiry"]) == 19
    monkeypatch.setenv("SOURCE_GROUP", "wudi1")
    monkeypatch.setenv("ACCOUNT_INDEX", "1")
    folder = tmp_path / "batch"
    folder.mkdir()
    mixed = folder / "result.json"
    mixed.write_text(json.dumps({"results": [raw]}), encoding="utf-8")
    stamp_result(mixed)
    output = tmp_path / "merged.json"
    merge_results(folder, output, [{"source_group": "wudi1", "account_index": 1}], "2026-11-14")
    merged = json.loads(output.read_text(encoding="utf-8"))["results"][0]
    reported = normalize_record(merged, {}, {})
    assert points_expiring_in_2026(reported["points_expiry"]) == 19
    assert missing_result({"source_group": "old1", "account_index": 1}, "2026-11-14")["points_expiry"] == empty_points_expiry()


@pytest.mark.parametrize("master", [False, True])
def test_all_coupon_sheets_ascend_and_expiry_column_is_adjacent(tmp_path, monkeypatch, master):
    monkeypatch.setenv("REPORT_MASTER", str(master).lower())
    monkeypatch.setenv("CONFIDENTIAL_PASSWORDS", "synthetic-hidden")
    rows = []
    for index, (points, invoice, password) in enumerate([
        (500, "有", "synthetic-visible"), (0, "无", "synthetic-hidden"),
        (20, "有", "synthetic-hidden"), (20, "无", "synthetic-visible"),
        (20, "有", "synthetic-visible"), (None, "有", "synthetic-visible"),
    ], 1):
        data = empty_account_data()
        data.update(invoice_profile_status=invoice, coupons={"unused": [
            {"name": "PCB+SMT优惠券", "coupon_extend_state": "PCB+SMT", "expires_at": "2099-01-01"},
            {"name": "PCB加急减免券", "expires_at": "2099-01-01"},
            {"name": SHELL_COUPON, "expires_at": "2099-01-01"},
            {"name": "普通券", "expires_at": "2099-01-01"},
        ]})
        rows.append({"account_index": index, "username": f"fixture-{index}", "password": password,
                     "group_code": "old1", "final_points": points or 0,
                     "points_fetch_success": points is not None, "account_data": data,
                     "points_expiry": parse_points_expiry(overview(25 if index == 1 else 0))
                     if index < 6 else empty_points_expiry()})
    path = tmp_path / "report.xlsx"
    write_xlsx(str(path), rows)
    workbook = load_workbook(path)
    main = workbook["签到汇总"]
    headers = [cell.value for cell in main[1]]
    assert headers[headers.index("金豆数量") + 1] == "2026年底到期金豆"
    heads = {cell.value: cell.column for cell in main[1]}
    found = {main.cell(i, heads["客编"]).value: main.cell(i, heads["2026年底到期金豆"])
             for i in range(2, main.max_row + 1)}
    assert found["fixture-1"].value == 25
    assert found["fixture-1"].font.color.rgb == "00C00000"
    assert found["fixture-2"].value == 0
    assert found["fixture-6"].value == "数据不足"
    # Source columns and the main sheet's original ordering remain independent.
    for sheet in workbook.worksheets[1:]:
        headers = [cell.value for cell in sheet[1]]
        assert headers[headers.index("优惠券过期时间") + 1] == "金豆数量"
        point_col = headers.index("金豆数量") + 1
        assert [sheet.cell(i, point_col).value for i in range(2, 8)] == [0, 20, 20, 20, 500, "数据不足"]
        assert [sheet.cell(i, 2).value for i in range(2, 8)] == ["fixture-2", "fixture-5", "fixture-3", "fixture-4", "fixture-1", "fixture-6"]
        assert sheet.cell(4, 3).value == "保密"
        assert sheet.cell(2, point_col).data_type == "n"
        if master:
            assert headers[headers.index("金豆数量") + 1:][:3] == ["来源分类", "原始组", "组内序号"]


def test_expiry_whitelist_discards_unrelated_response_fields():
    response = overview()
    response["data"].update(password="synthetic-hidden", cookie="synthetic-cookie", token="synthetic-token")
    value = parse_points_expiry(response)
    encoded = json.dumps(normalize_points_expiry(deepcopy(value)))
    assert "synthetic-" not in encoded
    assert "basicIntegral" not in encoded
