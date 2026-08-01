import io
import json
import os
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from h3.orchestrate_batches import (
    check_manifest,
    extract_archive,
    parse_groups,
    select_dispatched_run,
)
from h3.fetch_results import selected_workflows
from h3.report import load_account_lookup


class BatchOrchestrationTests(unittest.TestCase):
    def test_daily_summary_runs_after_batch4_and_selects_groups_1_to_4(self):
        root = Path(__file__).resolve().parents[2]
        text = (root / ".github" / "workflows" / "daily-summary.yml").read_text(encoding="utf-8")
        self.assertIn('workflows: ["sign-batch4"]', text)
        self.assertIn("types: [completed]", text)
        self.assertIn('SUMMARY_GROUPS: "1,2,3,4"', text)
        self.assertNotIn("ACCOUNTS_BATCH5", text)
        self.assertIn("results/*.xlsx", text)
        self.assertEqual(
            [item["group_number"] for item in selected_workflows("1,2,3,4")],
            [1, 2, 3, 4],
        )

    def test_group_parser_keeps_requested_order_and_removes_duplicates(self):
        self.assertEqual(parse_groups("5,6,6,7,8"), [5, 6, 7, 8])
        with self.assertRaises(ValueError):
            parse_groups("4")

    def test_run_discovery_prefers_the_unique_orchestration_title(self):
        dispatched_at = datetime(2026, 7, 31, 1, 0, tzinfo=timezone.utc)
        runs = [
            {
                "id": 101,
                "display_title": "sign-batch5 (manual)",
                "created_at": "2026-07-31T01:00:02Z",
            },
            {
                "id": 102,
                "display_title": "sign-batch5 (9001)",
                "created_at": "2026-07-31T01:00:03Z",
            },
        ]
        selected = select_dispatched_run(runs, "sign-batch5 (9001)", {101}, dispatched_at)
        self.assertEqual(selected["id"], 102)

    def test_run_discovery_falls_back_to_a_new_run_created_after_dispatch(self):
        dispatched_at = datetime(2026, 7, 31, 1, 0, tzinfo=timezone.utc)
        runs = [
            {"id": 201, "display_title": "old", "created_at": "2026-07-31T00:59:00Z"},
            {"id": 202, "display_title": "new", "created_at": "2026-07-31T01:00:01Z"},
        ]
        selected = select_dispatched_run(runs, "missing-title", {201}, dispatched_at)
        self.assertEqual(selected["id"], 202)

    def test_check_requires_all_four_batches_to_succeed(self):
        payload = {
            "batches": [
                {"group_number": group, "conclusion": "success"}
                for group in (5, 6, 7, 8)
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "orchestration.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(check_manifest(str(path), [5, 6, 7, 8]), [])
            payload["batches"][-1]["conclusion"] = "failure"
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertIn("batch 8", check_manifest(str(path), [5, 6, 7, 8])[0])

    def test_artifact_extraction_rejects_parent_directory_paths(self):
        archive_bytes = io.BytesIO()
        with zipfile.ZipFile(archive_bytes, "w") as archive:
            archive.writestr("../outside.txt", "unsafe")
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(ValueError):
                extract_archive(archive_bytes.getvalue(), Path(temp_dir))

    def test_report_account_lookup_includes_batches_7_and_8(self):
        with patch.dict(
            os.environ,
            {
                "ACCOUNTS_BATCH5": "user5,password5",
                "ACCOUNTS_BATCH6": "user6,password6",
                "ACCOUNTS_BATCH7": "user7,password7",
                "ACCOUNTS_BATCH8": "user8,password8",
            },
            clear=True,
        ):
            lookup, total = load_account_lookup()
        self.assertEqual(total, 4)
        self.assertEqual(lookup[(7, 1)], "user7")
        self.assertEqual(lookup[(8, 1)], "user8")

    def test_batch_workflows_are_manual_only_and_allow_twenty_runners(self):
        root = Path(__file__).resolve().parents[2]
        workflow_files = {
            5: "sign-batch5.yml",
            6: "sign-batch6.yaml",
            7: "sign-batch7.yml",
            8: "sign-batch8.yml",
        }
        for group, filename in workflow_files.items():
            text = (root / ".github" / "workflows" / filename).read_text(encoding="utf-8")
            self.assertIn("workflow_dispatch:", text)
            self.assertNotIn("schedule:", text)
            self.assertEqual(text.count("max-parallel: 20"), 2)
            self.assertIn(f"ACCOUNTS_BATCH{group}", text)
            self.assertIn(f"batch{group}-result", text)

        orchestrator = (
            root / ".github" / "workflows" / "sign-batches5-8.yml"
        ).read_text(encoding="utf-8")
        self.assertNotIn("schedule:", orchestrator)
        self.assertIn("--groups 5,6,7,8", orchestrator)
        self.assertIn("actions: write", orchestrator)
        self.assertIn("results/*.xlsx", orchestrator)


if __name__ == "__main__":
    unittest.main()
