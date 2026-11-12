import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import archive_store
import backfill
import monitor


def video(identifier, published):
    return {"id": identifier, "published": published, "title": "Bitcoin cycle update",
            "url": "https://www.youtube.com/watch?v=" + identifier}


class ArchiveTests(unittest.TestCase):
    def test_complete_hosted_transcript_retains_tail_and_timestamps(self):
        text = "bitcoin " * 10000 + "IMPORTANT TAIL"
        payload = {"ok": True, "video": {"id": "abcdefghijk"}, "track": {"language": "en", "generated": True},
                   "segments": [{"text": text, "start": 123000, "dur": 1000}]}
        with patch.object(monitor, "http_get", return_value=json.dumps(payload).encode()):
            bundle = monitor.get_transcript(video("abcdefghijk", "2026-01-01")["url"], with_metadata=True)
        self.assertEqual(bundle["text"], text)
        self.assertEqual(bundle["segments"][0]["start"], 123)

    def test_long_video_summary_includes_every_section(self):
        text = "a" * 50000 + "TAIL_EVIDENCE"
        with patch.object(monitor, "summarize_part", return_value="notes") as summarize:
            monitor.summarize(video("abcdefghijk", "2026-01-01"), text)
        mapped = [c.args[1] for c in summarize.call_args_list if not c.kwargs.get("intermediate")]
        self.assertEqual("".join(mapped), text)

    def test_rag_export_retains_sources_and_date_filters(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = archive_store.Archive(Path(temp) / "archive")
            for identifier, date in (("abcdefghijk", "2026-01-02"), ("lmnopqrstuv", "2026-06-03")):
                archive.create(video(identifier, date), {"text": "bitcoin halving cycle bottom",
                    "segments": [{"text": "bitcoin halving cycle bottom", "start": 75, "duration": 5}]}, None, None)
            output = Path(temp) / "rag"
            self.assertEqual(archive_store.export(archive.root, output), 2)
            results = archive_store.query(output / "rag.sqlite", "bitcoin halving?", before="2026-03-01")
            self.assertEqual(len(results), 1)
            self.assertIn("&t=75s", results[0]["source_url"])
            self.assertEqual(results[0]["video_id"], "abcdefghijk")

    def test_overlapping_chunks_point_to_actual_earliest_caption(self):
        record = {"video": video("abcdefghijk", "2026-01-01"), "transcript": {
            "text": "", "segments": [{"text": "alpha " * 250, "start": 10, "duration": 10},
                                      {"text": "beta " * 250, "start": 20, "duration": 10}]},
            "summary": {"text": None}}
        passages = list(archive_store.chunks(record))
        self.assertEqual(passages[1]["start_seconds"], 20)
        self.assertTrue(passages[0]["text"])

    def test_backfill_newest_first_year_boundary_and_resume_without_email(self):
        with tempfile.TemporaryDirectory() as temp:
            catalog = {"target_year": 2026, "groups": {"videos": [video("abcdefghijk", "2026-10-02"),
                       video("lmnopqrstuv", "2026-01-01"), video("oldvideo123", "2025-12-31")],
                       "streams": [video("stream12345", "2026-11-01")]}}
            archive_store.write_json(Path(temp) / "catalog.json", catalog)
            bundle = {"text": "complete transcript", "segments": []}
            with patch.object(monitor, "get_transcript", return_value=bundle) as fetch, \
                 patch.object(monitor, "send_email") as email, \
                 patch.object(monitor, "summarize") as summarize, patch.object(backfill.time, "sleep"):
                backfill.run(temp, year=2026, batch_size=2)
                self.assertIn("stream12345", fetch.call_args_list[0].args[0])
                self.assertIn("abcdefghijk", fetch.call_args_list[1].args[0])
                backfill.run(temp, year=2026, batch_size=10)
                self.assertEqual(fetch.call_count, 3)
                email.assert_not_called()
                summarize.assert_not_called()
            self.assertEqual(len(list(archive_store.Archive(temp).records())), 3)

    def test_missing_captions_remain_retryable_and_do_not_block_older_video(self):
        with tempfile.TemporaryDirectory() as temp:
            archive_store.write_json(Path(temp) / "catalog.json", {"target_year": 2026,
                "groups": {"videos": [video("abcdefghijk", "2026-10-02"), video("lmnopqrstuv", "2026-01-01")]}})
            with patch.object(monitor, "get_transcript", side_effect=[{"text": "", "segments": []},
                {"text": "available", "segments": []}]), patch.object(backfill.time, "sleep"):
                backfill.run(temp, year=2026, batch_size=2)
            self.assertIsNone(archive_store.Archive(temp).get("abcdefghijk"))
            progress = json.loads((Path(temp) / "backfill.json").read_text())
            self.assertIn("abcdefghijk", progress["failures"])
            self.assertIsNotNone(archive_store.Archive(temp).get("lmnopqrstuv"))

    def test_unknown_publication_date_cannot_be_assumed_current_year(self):
        with patch.object(monitor, "http_get", return_value=b"<html>No date</html>"):
            with self.assertRaises(RuntimeError):
                backfill.publication_date(video("abcdefghijk", None))

    def test_publication_date_watch_metadata_without_player(self):
        pages = [b'<meta content="2026-10-30" itemprop="datePublished">',
                 b'<script>var ytInitialData = {"contents":{"videoPrimaryInfoRenderer":'
                 b'{"dateText":{"simpleText":"Streamed live on Oct 30, 2026"}}}};</script>']
        for page in pages:
            with self.subTest(page=page), patch.object(monitor, "http_get", return_value=page):
                v = video("abcdefghijk", None)
                self.assertEqual(backfill.publication_date(v), "2026-10-30")
                self.assertEqual(v["published_precision"], "day")

    def test_metadata_failure_records_cause_and_does_not_look_like_caption_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            archive_store.write_json(Path(temp) / "catalog.json", {"target_year": 2026,
                "groups": {"streams": [video("abcdefghijk", None)]}})
            with patch.object(monitor, "http_get", return_value=b'"playabilityStatus":{"status":"LOGIN_REQUIRED"} Sign in to confirm'), \
                 patch.object(monitor, "get_transcript") as fetch:
                backfill.run(temp, year=2026)
            fetch.assert_not_called()
            failure = json.loads((Path(temp) / "backfill.json").read_text())["failures"]["abcdefghijk"]
            self.assertEqual(failure["stage"], "publication_date")
            self.assertEqual(failure["code"], "date_unavailable")
            self.assertEqual(failure["details"]["player_status"], "LOGIN_REQUIRED")

    def test_hosted_provider_error_exposes_specific_code(self):
        import io
        import urllib.error
        error = urllib.error.HTTPError("https://youtubegpt.ai/api/transcript", 404, "missing", {},
                                       io.BytesIO(b'{"code":"no_captions","message":"No captions"}'))
        with patch.object(monitor, "http_get", side_effect=error):
            bundle = monitor.get_transcript(video("abcdefghijk", None)["url"], with_metadata=True, hosted_only=True)
        self.assertEqual(bundle["errors"][0]["code"], "no_captions")
        self.assertEqual(bundle["errors"][0]["http_status"], 404)

    def test_provider_rate_limit_preserves_retry_after(self):
        progress = {"failures": {}}
        error = backfill.CollectionError("Caption provider returned no transcript", stage="transcript",
            code="http_error", details={"providers": [{"http_status": 429, "retry_after": "3600"}]})
        backfill.defer(progress, video("abcdefghijk", None), error)
        from datetime import datetime, timezone
        failure = progress["failures"]["abcdefghijk"]
        self.assertEqual(failure["http_status"], 429)
        self.assertGreater((datetime.fromisoformat(failure["retry_after"]) - datetime.now(timezone.utc)).total_seconds(), 3500)

    def test_legacy_failures_are_retried_under_fixed_parser(self):
        with tempfile.TemporaryDirectory() as temp:
            archive_store.write_json(Path(temp) / "catalog.json", {"target_year": 2026,
                "groups": {"shorts": [video("abcdefghijk", None)]}})
            archive_store.write_json(Path(temp) / "backfill.json", {"failures": {
                "abcdefghijk": {"error_type": "RuntimeError", "retry_after": "2099-01-01"}}})
            with patch.object(monitor, "http_get", return_value=b'<meta itemprop="datePublished" content="2026-10-30">'), \
                 patch.object(monitor, "get_transcript", return_value={"text": "captions", "segments": []}), \
                 patch.object(backfill.time, "sleep"):
                backfill.run(temp, year=2026, batch_size=1)
            self.assertIsNotNone(archive_store.Archive(temp).get("abcdefghijk"))
            self.assertFalse(json.loads((Path(temp) / "backfill.json").read_text())["failures"])

    def test_live_upload_reuses_historical_transcript_and_still_sends_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "archive"
            (root / ".git").mkdir(parents=True)
            archive = archive_store.Archive(root)
            current = video("abcdefghijk", "2026-11-12")
            archive.create(current, {"text": "full caption", "segments": []}, None, None)
            state = Path(temp) / "state.json"
            archive_store.write_json(state, {"seen_ids": ["oldvideo123"]})
            with patch.dict(os.environ, {"ARCHIVE_PATH": str(root)}), \
                 patch.object(monitor, "STATE_PATH", state), \
                 patch.object(monitor, "ensure_notification_destination"), \
                 patch.object(monitor, "fetch_feed", return_value=[current]), \
                 patch.object(monitor, "get_transcript") as fetch, \
                 patch.object(monitor, "summarize", return_value="real summary") as summarize, \
                 patch.object(monitor, "send_telegram", return_value=False), \
                 patch.object(monitor, "send_email", return_value=True) as email:
                monitor.main()
                fetch.assert_not_called()
                summarize.assert_called_once_with(current, "full caption")
                self.assertIn("real summary", email.call_args.args[0])
            record = archive.get(current["id"])
            self.assertEqual(record["summary"]["text"], "real summary")
            self.assertTrue(record["email"]["sent_at"])
            self.assertIn(current["id"], json.loads(state.read_text())["seen_ids"])


if __name__ == "__main__":
    unittest.main()
