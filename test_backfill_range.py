import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from archive_store import Archive, write_json
import backfill
import monitor


def video(year, day="07-01"):
    video_id = f"v{year:010d}"
    return {"id": video_id, "title": f"Bitcoin {year}", "published": f"{year}-{day}",
            "url": "https://www.youtube.com/watch?v=" + video_id}


class RangeTests(unittest.TestCase):
    def test_widen_legacy_catalog_reuses_records_and_collects_2025_through_2022(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = {"target_year": 2026, "date_verification": {"note": "keep this cache"},
                       "groups": {"videos": [video(year) for year in range(2027, 2020, -1)]}}
            write_json(root / "catalog.json", catalog)
            write_json(root / "backfill.json", {"failures": {}, "year_boundaries": {"videos": video(2025)["id"]}})
            milestone = {"delivered": {"100": {"sent_at": "earlier"}}, "next_milestone": 200}
            write_json(root / "milestones.json", milestone)
            archive = Archive(root)
            archive.create(video(2026), {"text": "already collected", "segments": []}, None, None)
            original = archive.path(video(2026)["id"]).read_bytes()
            with patch.object(monitor, "get_transcript", return_value={"text": "complete older transcript", "segments": []}) as fetch, \
                 patch.object(monitor, "send_email") as email, patch.object(monitor, "summarize") as summarize, \
                 patch.object(backfill.time, "sleep"):
                backfill.run(root, start_year=2022, end_year=2026, batch_size=10)
            self.assertEqual([call.args[0] for call in fetch.call_args_list], [video(y)["url"] for y in (2025, 2024, 2023, 2022)])
            self.assertEqual(archive.path(video(2026)["id"]).read_bytes(), original)
            self.assertEqual(len(list(archive.records())), 5)
            self.assertIsNone(archive.get(video(2021)["id"]))
            self.assertIsNone(archive.get(video(2027)["id"]))
            self.assertEqual(json.loads((root / "milestones.json").read_text()), milestone)
            saved = json.loads((root / "catalog.json").read_text())
            self.assertEqual(saved["date_verification"], catalog["date_verification"])
            self.assertEqual(saved["collection_scope"]["start_date"], "2022-01-01")
            self.assertNotIn("target_year", saved)
            progress = json.loads((root / "backfill.json").read_text())
            self.assertEqual(progress["records_in_scope"], 5)
            self.assertEqual(progress["records_by_year"], {str(y): 1 for y in range(2022, 2027)})
            self.assertEqual(progress["year_boundaries"]["videos"]["before"], "2022-01-01")
            email.assert_not_called()
            summarize.assert_not_called()

    def test_rolling_five_year_lower_date_is_inclusive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            entries = [video(2021, "11-13"), {**video(2021, "11-12"), "id": "boundary123"},
                       {**video(2021, "11-11"), "id": "oldvideo123"}]
            for item in entries:
                item["url"] = "https://www.youtube.com/watch?v=" + item["id"]
            write_json(root / "catalog.json", {"target_year": 2026, "groups": {"videos": entries}})
            with patch.object(monitor, "get_transcript", return_value={"text": "actual speech", "segments": []}), \
                 patch.object(backfill.time, "sleep"):
                backfill.run(root, start_date="2021-11-12", end_year=2026, batch_size=10)
            self.assertEqual(len(list(Archive(root).records())), 2)
            self.assertIsNone(Archive(root).get("oldvideo123"))

    def test_newest_first_across_all_tabs_and_years(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_json(root / "catalog.json", {"target_year": 2026, "groups": {
                "videos": [video(2026), video(2023)], "streams": [video(2025)], "shorts": [video(2024)]}})
            with patch.object(monitor, "get_transcript", return_value={"text": "speech", "segments": []}) as fetch, \
                 patch.object(backfill.time, "sleep"):
                backfill.run(root, start_year=2022, end_year=2026, batch_size=4)
            self.assertEqual([call.args[0] for call in fetch.call_args_list], [video(y)["url"] for y in (2026, 2025, 2024, 2023)])

    def test_existing_retry_is_preserved_while_older_years_progress(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_json(root / "catalog.json", {"target_year": 2026,
                       "groups": {"videos": [video(2025), video(2024)]}})
            failure = {"stage": "transcript", "code": "node_unreachable", "retry_after": "2099-01-01"}
            write_json(root / "backfill.json", {"failures": {video(2025)["id"]: failure}})
            with patch.object(monitor, "get_transcript", return_value={"text": "speech", "segments": []}), \
                 patch.object(backfill.time, "sleep"):
                backfill.run(root, start_year=2022, end_year=2026)
            self.assertIsNotNone(Archive(root).get(video(2024)["id"]))
            progress = json.loads((root / "backfill.json").read_text())
            self.assertEqual(progress["failures"][video(2025)["id"]], failure)

    def test_invalid_ranges_are_rejected(self):
        for kwargs in ({"start_year": 2027, "end_year": 2026},
                       {"year": 2026, "start_year": 2022},
                       {"start_year": 2022, "start_date": "2021-11-12"},
                       {"start_date": "2022-02-30"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                backfill.collection_scope(**kwargs)

    def test_full_2021_bull_cycle_is_included_by_default(self):
        self.assertEqual(backfill.collection_scope()["start_date"], "2021-01-01")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_json(root / "catalog.json", {"target_year": 2026, "groups": {
                "videos": [video(2021, "01-01"), video(2020, "12-31")]}})
            with patch.object(monitor, "get_transcript", return_value={"text": "2021 bull cycle speech", "segments": []}), \
                 patch.object(backfill.time, "sleep"):
                backfill.run(root, batch_size=3)
            self.assertIsNotNone(Archive(root).get(video(2021)["id"]))
            self.assertIsNone(Archive(root).get(video(2020)["id"]))


if __name__ == "__main__":
    unittest.main()
