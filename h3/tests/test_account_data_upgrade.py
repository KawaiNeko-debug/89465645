import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import load_workbook

from h3.account_data import (
    browser_fetch_json,
    empty_account_data,
    normalize_coupon,
    parse_coupon_response,
    parse_invoice_profile_exists,
    parse_invoice_statistics,
    parse_invoice_order_page,
    predict_pcb_smt,
    sum_pcb_invoice_orders,
)
from h3.merge_results import pick_result
from h3.listing_gift import inspect_listing_gift_response, is_listing_gift_date
from h3.report import max_lottery_count, normalize_activity_records, write_xlsx
from h3.exchange_history import exchange_status_text, normalize_exchange_records
from h3.campaign_vote import (
    can_vote_after_sign,
    inspect_vote_config,
    is_vote_date,
    parse_lottery_winning_response,
)
from h3.dynamic_groups import configured_groups
from h3.category_reports import main as category_reports_main
from h3.report import is_problem_record, normalize_record, resolve_output_xlsx_path


def lottery_rows(count: int) -> list[dict]:
    return [
        {
            "title": f"奖品{index}",
            "claimed": index % 2 == 0,
            "status_text": "已经领取" if index % 2 == 0 else "未领取",
        }
        for index in range(1, count + 1)
    ]


def account_data_with_amount(within=100, over=0) -> dict:
    data = empty_account_data()
    data.update(
        {
            "fetch_success": True,
            "invoice_fetch_success": True,
            "coupon_fetch_success": True,
            "invoice_profile_status": "有",
            "invoice_profile_exists": True,
            "invoice_month_threshold": 12,
            "invoice_within_months_amount": within,
            "invoice_over_months_amount": over,
            "pcb_order_fetch_success": True,
            "pcb_within_months_amount": within,
            "pcb_over_months_amount": over,
            "pcb_total_amount": within + over,
            "pcb_amount_shortfall": max(0, 40 - within - over),
        }
    )
    return data


def record(index: int, lottery_count: int, account_data=None) -> dict:
    return {
        "account_index": index,
        "username": f"account{index}",
        "group_number": 1,
        "group_position": f"1组账号{index}",
        "sign_success": True,
        "sign_status": "签到成功",
        "final_points": 100,
        "activity_records": {"seckill": [], "lottery": lottery_rows(lottery_count)},
        "account_data_required": True,
        "account_data_fetch_success": True,
        "account_data": account_data or account_data_with_amount(),
        "listing_gift_required": True,
        "listing_gift_success": True,
        "listing_gift_attempted": True,
        "listing_gift_status": "上市礼包领取成功",
        "listing_gift_time": "2026-08-05 12:00:00",
        "listing_gift_detail": "上市礼包领取成功",
    }


class DynamicLotteryTests(unittest.TestCase):
    def test_normalization_never_truncates_supported_counts(self):
        for count in (0, 1, 3, 5, 8, 15):
            with self.subTest(count=count):
                normalized = normalize_activity_records(
                    {"seckill": [], "lottery": lottery_rows(count)}
                )
                self.assertEqual(len(normalized["lottery"]), count)

    def test_xlsx_headers_follow_the_largest_lottery_count(self):
        for count in (0, 1, 3, 5, 8, 15):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as temp_dir:
                path = os.path.join(temp_dir, "report.xlsx")
                write_xlsx(path, [record(1, count)])
                sheet = load_workbook(path)["签到汇总"]
                headers = [cell.value for cell in sheet[1]]
                self.assertEqual(
                    [value for value in headers if str(value or "").startswith("抽奖")],
                    [f"抽奖{index}" for index in range(1, count + 1)],
                )
                self.assertEqual(
                    [value for value in headers if str(value or "").startswith("领取情况")],
                    [f"领取情况{index}" for index in range(1, count + 1)],
                )

    def test_accounts_are_padded_to_the_daily_maximum(self):
        rows = [record(1, 2), record(2, 6), record(3, 8)]
        self.assertEqual(max_lottery_count(rows), 8)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "report.xlsx")
            write_xlsx(path, rows)
            sheet = load_workbook(path)["签到汇总"]
            headers = [cell.value for cell in sheet[1]]
            first_row = next(row for row in sheet.iter_rows(min_row=2) if row[2].value == "account1")
            self.assertEqual(headers.count("抽奖8"), 1)
            self.assertIsNone(first_row[headers.index("抽奖3")].value)
            self.assertIsNone(first_row[headers.index("抽奖8")].value)

    def test_retry_merge_keeps_the_more_complete_lottery_array(self):
        initial = record(1, 15)
        initial.update({"sign_success": False, "activity_fetch_success": True, "retry_count": 0})
        retry = record(1, 3)
        retry.update({"sign_success": True, "activity_fetch_success": True, "retry_count": 1})
        picked = pick_result(initial, retry)
        self.assertEqual(len(picked["activity_records"]["lottery"]), 15)


class ExchangeHistoryTests(unittest.TestCase):
    def test_status_mapping_and_august_2026_cutoff(self):
        rows = [
            {
                "integralChangeType": 5,
                "exchangeStates": state,
                "exchangeNum": 1,
                "integralChangeNum": 100,
                "createTime": f"2026-08-0{state}T02:04:01.000Z",
                "goodsName": f"奖品{state}",
            }
            for state in range(1, 7)
        ]
        rows.append(
            {
                "integralChangeType": 5,
                "exchangeStates": 6,
                "createTime": "2026-07-31T15:59:59.000Z",
                "goodsName": "七月奖品",
            }
        )
        normalized = normalize_exchange_records(rows)
        self.assertEqual([item["status_text"] for item in normalized], [
            "已确认收货", "已退回", "已冻结", "已兑换待发货", "已发货", "已兑换"
        ])
        self.assertNotIn("七月奖品", [item["title"] for item in normalized])
        self.assertEqual(normalized[-1]["created_at"], "2026-08-01 10:04:01")
        self.assertEqual(exchange_status_text({"integralChangeType": 2}), "已发到账户")

    def test_xlsx_has_unique_prize_pairs_and_status_colors(self):
        first = record(1, 0)
        first["activity_records"]["exchange"] = [
            {"title": "抽纸", "status": 1, "status_text": "已兑换", "quantity": 1, "points": 390, "created_at": "2026-08-06 10:04:01"},
            {"title": "水杯", "status": 5, "status_text": "已退回", "quantity": 1, "points": 0, "created_at": "2026-08-05 09:00:00"},
        ]
        second = record(2, 0)
        second["activity_records"]["exchange"] = [
            {"title": "抽纸", "status": 3, "status_text": "已兑换待发货", "quantity": 2, "points": 780, "created_at": "2026-08-08 08:00:00"},
            {"title": "露营车", "status": 6, "status_text": "已确认收货", "quantity": 1, "points": 1000, "created_at": "2026-08-07 08:00:00"},
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "report.xlsx")
            write_xlsx(path, [first, second])
            sheet = load_workbook(path)["签到汇总"]
            headers = [cell.value for cell in sheet[1]]
            self.assertEqual(
                [value for value in headers if str(value or "").startswith("兑换物品：")],
                ["兑换物品：抽纸", "兑换物品：水杯", "兑换物品：露营车"],
            )
            paper_status = headers.index("兑换状态：抽纸") + 1
            cup_status = headers.index("兑换状态：水杯") + 1
            cart_status = headers.index("兑换状态：露营车") + 1
            self.assertEqual(sheet.cell(2, paper_status).fill.fgColor.rgb, "00FFD966")
            self.assertEqual(sheet.cell(2, cup_status).fill.fgColor.rgb, "00F8696B")
            self.assertEqual(sheet.cell(3, paper_status).fill.fgColor.rgb, "00C6E0B4")
            self.assertEqual(sheet.cell(3, cart_status).fill.fgColor.rgb, "00C6E0B4")
            paper_detail = headers.index("兑换物品：抽纸") + 1
            self.assertIn("2件", sheet.cell(3, paper_detail).value)

    def test_retry_merge_keeps_longer_exchange_array(self):
        initial = record(1, 1)
        initial.update({"sign_success": False, "activity_fetch_success": True})
        initial["activity_records"]["exchange"] = [
            {"title": f"奖品{index}", "status_text": "已兑换"} for index in range(3)
        ]
        retry = record(1, 2)
        retry.update({"sign_success": True, "activity_fetch_success": True, "retry_count": 1})
        retry["activity_records"]["exchange"] = [{"title": "奖品0", "status_text": "已兑换"}]
        picked = pick_result(initial, retry)
        self.assertEqual(len(picked["activity_records"]["lottery"]), 2)
        self.assertEqual(len(picked["activity_records"]["exchange"]), 3)


class AccountDataTests(unittest.TestCase):
    def test_har_invoice_fields_profile_signal_and_bodyless_post(self):
        response = {
            "success": True,
            "code": 200,
            "data": {
                "vatOrdinaryInvoiceMoney": 300.0,
                "overTimeNum": 12,
                "overTimeElecInvoiceMoney": 0.51,
                "customerInvoiceInfoVO": {"vatCompanyName": "fixture"},
            },
        }
        self.assertEqual(
            parse_invoice_statistics(response),
            {"month_threshold": 12, "within_amount": 300.0, "over_amount": 0.51},
        )
        self.assertTrue(parse_invoice_profile_exists([response]))

        class FakePage:
            def __init__(self):
                self.argument = None

            def evaluate(self, _source, argument):
                self.argument = argument
                return {"ok": True, "data": {"success": True, "code": 200}}

        page = FakePage()
        self.assertTrue(browser_fetch_json(page, "POST", "/path", None)["success"])
        self.assertIsNone(page.argument["payload"])
        browser_fetch_json(page, "POST", "/path", {"invoiceType": "2"}, form_encoded=True)
        self.assertTrue(page.argument["formEncoded"])

    def test_invoice_fields_and_profile_presence(self):
        parsed = parse_invoice_statistics(
            {"success": True, "data": {"overTimeNum": 18, "vatSpecialInvoiceMoney": "12.50", "overTimeInvoiceMoney": 3}}
        )
        self.assertEqual(parsed, {"month_threshold": 18, "within_amount": 12.5, "over_amount": 3.0})
        self.assertTrue(
            parse_invoice_profile_exists(
                [{"success": True, "data": {"customerInvoiceInfoVO": {"invoiceTitle": "测试企业"}}}]
            )
        )
        self.assertFalse(parse_invoice_profile_exists([{"success": True, "data": {}}]))
        self.assertFalse(
            parse_invoice_profile_exists(
                [{"success": True, "data": {"customerInvoiceInfoVO": {"invoiceTitle": ""}}}]
            )
        )

    def test_coupon_status_expiry_and_rules_are_preserved(self):
        response = {
            "success": True,
            "data": {
                "records": [
                    {
                        "couponId": "1",
                        "couponName": "PCB高多层150元优惠券",
                        "businessType": "PCB",
                        "expirationTime": "2026-12-31 23:59:59",
                        "useRule": "满额可用",
                    }
                ]
            },
        }
        coupon = parse_coupon_response(response, "unused")[0]
        self.assertEqual(coupon["status"], "未使用")
        self.assertEqual(coupon["expires_at"], "2026-12-31 23:59:59")
        self.assertEqual(coupon["rule_text"], "满额可用")
        legacy = parse_coupon_response(
            {"success": True, "data": {"root": [{"historyCoupon": {"couponName": "旧版券"}}]}},
            "expired",
        )
        self.assertEqual(legacy[0]["name"], "旧版券")

    def test_har_coupon_date_fields_are_preserved(self):
        coupon = normalize_coupon(
            {
                "couponName": "fixture",
                "startDate": "2026-08-01 00:00:00",
                "endDate": "2026-08-31 23:59:59",
            },
            "unused",
        )
        self.assertEqual(coupon["valid_from"], "2026-08-01 00:00:00")
        self.assertEqual(coupon["expires_at"], "2026-08-31 23:59:59")

    def test_all_prediction_branches(self):
        insufficient = empty_account_data()
        self.assertEqual(predict_pcb_smt(insufficient)[0], "数据不足")

        no_spend = account_data_with_amount(0, 0)
        self.assertEqual(predict_pcb_smt(no_spend)[0], "不可能")

        current = account_data_with_amount()
        current["coupons"]["unused"] = [
            normalize_coupon({"couponName": "PCB+SMT专用券"}, "unused")
        ]
        self.assertEqual(predict_pcb_smt(current)[0], "很小可能")

        expired = account_data_with_amount()
        expired["coupons"]["expired"] = [
            normalize_coupon({"couponName": "PCB与SMT组合券"}, "expired")
        ]
        self.assertEqual(predict_pcb_smt(expired)[0], "很大可能")

        never_seen = account_data_with_amount()
        self.assertEqual(predict_pcb_smt(never_seen)[0], "100%可能")

    def test_coupon_sheets_handle_duplicates_illegal_chars_and_long_names(self):
        first = account_data_with_amount()
        second = account_data_with_amount()
        shared_name = "PCB/SMT:*?[]优惠券" + "A" * 30
        colliding_name = "PCB_SMT______优惠券" + "A" * 30
        first["coupons"]["unused"] = [
            normalize_coupon({"couponName": shared_name, "expirationTime": "2026-10-01"}, "unused")
        ]
        second["coupons"]["unused"] = [
            normalize_coupon({"couponName": shared_name, "expirationTime": "2026-11-01"}, "unused"),
            normalize_coupon({"couponName": colliding_name, "expirationTime": "2026-12-01"}, "unused"),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "report.xlsx")
            write_xlsx(path, [record(1, 0, first), record(2, 0, second)])
            workbook = load_workbook(path)
            coupon_sheets = workbook.sheetnames[1:]
            self.assertEqual(len(coupon_sheets), 2)
            self.assertTrue(all(len(name) <= 31 for name in coupon_sheets))
            self.assertEqual(len({name.lower() for name in coupon_sheets}), 2)
            self.assertEqual(workbook[coupon_sheets[0]].max_row + workbook[coupon_sheets[1]].max_row, 5)

    def test_no_profile_keeps_invoice_amount_cells_empty(self):
        data = account_data_with_amount(10, 5)
        data.update(
            {
                "invoice_profile_status": "无",
                "invoice_profile_exists": False,
                "invoice_within_months_amount": None,
                "invoice_over_months_amount": None,
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "report.xlsx")
            write_xlsx(path, [record(1, 0, data)])
            sheet = load_workbook(path)["签到汇总"]
            headers = {cell.value: cell.column for cell in sheet[1]}
            self.assertEqual(sheet.cell(2, headers["开票资料"]).value, "无")
            self.assertIsNone(sheet.cell(2, headers["不超过12个月可开金额"]).value)
            self.assertIsNone(sheet.cell(2, headers["超过12个月可开金额"]).value)

    def test_pcb_invoice_order_types_amount_fallback_and_deduplication(self):
        response = {
            "data": {
                "pageNum": 1,
                "totalPages": 2,
                "data": [
                    {"businessOrderType": 1, "invoiceMoney": "20.50", "businessOrderCode": "a"},
                    {"businessOrderType": 2, "invoiceMoney": None, "orderMoney": "19.50", "businessOrderCode": "b"},
                    {"businessOrderType": 3, "invoiceMoney": "100", "businessOrderCode": "c"},
                    {"businessOrderType": 9, "invoiceMoney": "100", "businessOrderCode": "d"},
                ],
            }
        }
        page = parse_invoice_order_page(response)
        self.assertEqual(page["total_pages"], 2)
        self.assertEqual([item["business_order_type"] for item in page["orders"]], [1, 2])
        total, count = sum_pcb_invoice_orders([response, response])
        self.assertEqual(total, 40.0)
        self.assertEqual(count, 2)

    def test_pcb_threshold_boundaries(self):
        for amount, expected, shortfall in (
            (0, "不可能", 40),
            (39.99, "不可能", 0.01),
            (40, "100%可能", 0),
            (40.01, "100%可能", 0),
        ):
            with self.subTest(amount=amount):
                data = account_data_with_amount(amount, 0)
                self.assertEqual(predict_pcb_smt(data)[0], expected)
                self.assertAlmostEqual(data["pcb_amount_shortfall"], shortfall, places=2)


class ListingGiftTests(unittest.TestCase):
    def test_date_window_is_exact(self):
        self.assertFalse(is_listing_gift_date("2026-08-04"))
        self.assertTrue(is_listing_gift_date("2026-08-05 23:59:59"))
        self.assertTrue(is_listing_gift_date("2026-08-06"))
        self.assertFalse(is_listing_gift_date("2026-08-07"))

    def test_har_success_shape_is_required(self):
        success = inspect_listing_gift_response(
            {"success": True, "code": 200, "data": {"success": True, "orderCode": "test"}}
        )
        self.assertEqual(success["state"], "received")
        self.assertTrue(success["success"])
        self.assertEqual(success["order_code"], "test")
        self.assertFalse(inspect_listing_gift_response({"success": True, "data": {}})["success"])

    def test_already_received_is_idempotent_success(self):
        result = inspect_listing_gift_response(
            {"success": False, "message": "今日已经领取，请勿重复领取"}
        )
        self.assertEqual(result["state"], "already")
        self.assertTrue(result["success"])

    def test_bodyless_repeat_response_is_idempotent_success(self):
        result = inspect_listing_gift_response(
            {"success": True, "code": 200, "data": {"success": False, "orderCode": None}}
        )
        self.assertEqual(result["state"], "already")
        self.assertTrue(result["success"])

    def test_retry_merge_preserves_successful_gift_result(self):
        initial = record(1, 1)
        initial.update({"sign_success": False, "retry_count": 0})
        retry = record(1, 1)
        retry.update(
            {
                "sign_success": True,
                "retry_count": 1,
                "listing_gift_success": False,
                "listing_gift_status": "领取失败",
            }
        )
        picked = pick_result(initial, retry)
        self.assertTrue(picked["listing_gift_success"])
        self.assertEqual(picked["listing_gift_status"], "上市礼包领取成功")

    def test_xlsx_contains_gift_status_column(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "report.xlsx")
            write_xlsx(path, [record(1, 1)])
            sheet = load_workbook(path)["签到汇总"]
            headers = [cell.value for cell in sheet[1]]
            column = headers.index("上市礼包领取情况") + 1
            self.assertIn("上市礼包领取成功", sheet.cell(2, column).value)
            self.assertEqual(sheet.cell(2, column).font.color.rgb, "00008000")


class VoteTests(unittest.TestCase):
    def test_vote_window_boundaries(self):
        self.assertFalse(is_vote_date("2026-08-10"))
        self.assertTrue(is_vote_date("2026-08-11"))
        self.assertTrue(is_vote_date("2026-08-31 23:59:59"))
        self.assertFalse(is_vote_date("2026-09-01"))

    def test_full_groups_must_sign_successfully_before_vote(self):
        self.assertFalse(can_vote_after_sign(False))
        self.assertTrue(can_vote_after_sign(True))
        self.assertTrue(can_vote_after_sign(False, sign_skipped=True))
        self.assertTrue(
            can_vote_after_sign(
                False,
                data_only_retry=True,
                previous_sign_success=True,
            )
        )
        self.assertFalse(
            can_vote_after_sign(
                False,
                data_only_retry=True,
                previous_sign_success=False,
            )
        )

    def test_vote_ready_already_and_conflict(self):
        target_sku = "target"
        target_name = "product"

        def response(can_vote, voted=""):
            return {
                "success": True,
                "data": {
                    "myVotedProductSku": voted,
                    "voteProductConfigList": [
                        {"productSku": target_sku, "productName": target_name, "canVote": can_vote}
                    ],
                },
            }

        self.assertEqual(inspect_vote_config(response(True), target_sku, target_name)["state"], "ready")
        self.assertEqual(inspect_vote_config(response(False, target_sku), target_sku, target_name)["state"], "already")
        self.assertEqual(inspect_vote_config(response(False, "other"), target_sku, target_name)["state"], "conflict")

    def test_new_winning_response_is_never_truncated(self):
        for count in (0, 1, 8, 15):
            with self.subTest(count=count):
                response = {
                    "success": True,
                    "data": {
                        "records": [
                            {
                                "prizeName": f"prize-{index}",
                                "receiveStatus": 2 if index % 2 else 0,
                            }
                            for index in range(count)
                        ]
                    },
                }
                rows = parse_lottery_winning_response(response)
                self.assertEqual(len(rows), count)
                if rows:
                    self.assertIn("claimed", rows[0])


class DynamicGroupTests(unittest.TestCase):
    def test_group_detection_keeps_global_order_and_skips_gaps(self):
        values = {
            f"{prefix}{index}": ""
            for prefix in ("old", "new", "ll", "zh")
            for index in range(1, 21)
        }
        values.update(
            {
                "old2": "old,pw",
                "new1": "new,pw\nnew2,pw",
                "ll3": "peer,pw",
                "zh1": "peer2,pw",
            }
        )
        with patch.dict(os.environ, values, clear=False):
            groups = configured_groups()
        self.assertEqual(
            [item["group_code"] for item in groups],
            ["old2", "new1", "ll3", "zh1"],
        )
        self.assertEqual([item["account_count"] for item in groups], [1, 2, 1, 1])
        self.assertEqual(groups[-1]["account_category"], "同行不签到组")

    def test_skip_sign_is_normal_only_when_data_is_complete(self):
        payload = {"group_code": "ll1", "account_category": "同行不签到组", "execution_mode": "skip_sign"}
        base = {
            "account_index": 1,
            "sign_skipped": True,
            "data_fetch_completed": True,
            "account_data_required": False,
            "points_fetch_success": True,
            "activity_fetch_success": True,
            "listing_gift_required": False,
            "vote_required": False,
        }
        complete = normalize_record(base, payload, {})
        self.assertFalse(is_problem_record(complete))
        incomplete = normalize_record({**base, "data_fetch_completed": False, "points_fetch_success": False}, payload, {})
        self.assertTrue(is_problem_record(incomplete))

    def test_configured_report_path_is_not_rewritten(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ, {"OUTPUT_XLSX_PATH": os.path.join(temp_dir, "2026-08-25-老号全干组.xlsx")}
        ):
            path = resolve_output_xlsx_path(temp_dir, {"task_start_date": "2026-08-25"})
        self.assertTrue(path.endswith("2026-08-25-老号全干组.xlsx"))

    def test_three_category_reports_are_generated_separately(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            results_dir = os.path.join(temp_dir, "results")
            os.makedirs(os.path.join(results_dir, "old1"))
            with open(os.path.join(results_dir, "manifest.json"), "w", encoding="utf-8") as file:
                json.dump(
                    {
                        "task_start_date": "2026-08-25",
                        "groups": [
                            {"group_code": "old1", "account_category": "老号全干组", "account_count": 1}
                        ],
                    },
                    file,
                    ensure_ascii=False,
                )
            with open(os.path.join(results_dir, "old1", "result.json"), "w", encoding="utf-8") as file:
                json.dump(
                    {
                        "group_code": "old1",
                        "account_category": "老号全干组",
                        "results": [
                            {
                                "account_index": 1,
                                "group_code": "old1",
                                "account_category": "老号全干组",
                                "sign_success": True,
                                "sign_status": "签到成功",
                                "points_fetch_success": True,
                                "activity_fetch_success": True,
                                "data_fetch_completed": True,
                                "account_data_required": False,
                                "activity_records": {"lottery": [], "exchange": []},
                                "listing_gift_required": False,
                                "vote_required": False,
                            }
                        ],
                    },
                    file,
                    ensure_ascii=False,
                )
            values = {
                f"{prefix}{index}": ""
                for prefix in ("old", "new", "ll", "zh")
                for index in range(1, 21)
            }
            values.update(
                {
                    "old1": "fixture-account,fixture-password",
                    "NOTIFY_CHANNELS": "",
                    "TELEGRAM_BOT_TOKEN": "",
                    "TELEGRAM_CHAT_ID": "",
                }
            )
            with patch.dict(os.environ, values, clear=False), patch.object(
                sys, "argv", ["category_reports.py", results_dir]
            ):
                self.assertEqual(category_reports_main(), 0)
            names = sorted(name for name in os.listdir(results_dir) if name.endswith(".xlsx"))
            self.assertEqual(
                names,
                [
                    "2026-08-25-同行不签到组.xlsx",
                    "2026-08-25-新号全干组.xlsx",
                    "2026-08-25-老号全干组.xlsx",
                ],
            )


if __name__ == "__main__":
    unittest.main()
