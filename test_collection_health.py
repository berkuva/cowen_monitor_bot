import json
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from archive_store import Archive, write_json
import backfill
import collection_health
import recent_uploads
import speaker_policy
import monitor


def video(vid="abcdefghijk", date="2026-11-04"):
    return {"id": vid, "title": "Bitcoin cycle", "published": date,
            "url": "https://www.youtube.com/watch?v=" + vid}


class CollectionHealthTests(unittest.TestCase):
    def test_members_only_error_does_not_attempt_other_download_paths(self):
        payload=json.dumps({'code':'unplayable','message':'Join this channel for members-only content.'}).encode()
        error=urllib.error.HTTPError('https://youtubegpt.ai',451,'Unavailable',{},io.BytesIO(payload))
        with patch('monitor.http_get',side_effect=error) as fetch, patch('monitor.subprocess.run') as direct:
            result=monitor.get_transcript(video()['url'],with_metadata=True)
        self.assertFalse(result['text'])
        self.assertEqual(result['errors'][0]['code'],'access_restricted')
        self.assertEqual(fetch.call_count,1)
        direct.assert_not_called()

    def test_restricted_video_remains_incomplete_outside_failure_count(self):
        progress={'failures':{}}
        error=backfill.CollectionError('members only',stage='transcript',code='access_restricted')
        backfill.defer(progress,video(),error)
        self.assertFalse(progress['failures'])
        self.assertIn('abcdefghijk',progress['access_restricted'])

    def test_stale_failure_is_cleared_only_after_a_real_saved_source_exists(self):
        with tempfile.TemporaryDirectory() as temp:
            Archive(temp).create(video(), {"text": "Bitcoin could rally", "segments": []}, None, None)
            progress = {"failures": {"abcdefghijk": {"code": "node_bad_response"},
                                     "lmnopqrstuv": {"code": "node_bad_response"}}}
            collection_health.reconcile(temp, progress)
            self.assertEqual(set(progress["failures"]), {"lmnopqrstuv"})
            self.assertEqual(progress["resolved_failures"]["abcdefghijk"]["resolution"], "saved_transcript")

    def test_no_captions_is_incomplete_and_retries_without_losing_attempt_count(self):
        with tempfile.TemporaryDirectory() as temp:
            progress = {"failures": {"abcdefghijk": {"code": "no_captions", "attempts": 5}}}
            report = collection_health.reconcile(temp, progress)
            self.assertEqual(report["caption_unavailable"], 1)
            self.assertFalse(progress["failures"])
            self.assertFalse((Path(temp) / "records/abcdefghijk.json").exists())
            error = backfill.CollectionError("no captions", stage="transcript", code="no_captions")
            backfill.defer(progress, video(), error)
            self.assertEqual(progress["caption_unavailable"]["abcdefghijk"]["attempts"], 6)

    def test_held_transcript_is_not_counted_as_saved(self):
        with tempfile.TemporaryDirectory() as temp:
            write_json(Path(temp) / "speaker-review/records/abcdefghijk.json", {
                "video": video(), "transcript": {"text": "Complete acquired text"}})
            progress = {"failures": {"abcdefghijk": {"code": "node_bad_response"}}}
            result = collection_health.reconcile(temp, progress)
            self.assertEqual(result["speaker_review_pending"], 1)
            self.assertEqual(progress["resolved_failures"]["abcdefghijk"]["resolution"],
                             "transcript_acquired_speaker_review_pending")
            self.assertFalse((Path(temp) / "records/abcdefghijk.json").exists())

    def test_tab_reconciliation_recovers_upload_outside_rss_without_erasing_catalog(self):
        with tempfile.TemporaryDirectory() as temp:
            write_json(Path(temp) / "catalog.json", {"groups": {"videos": [video()]}})
            payload = json.dumps({"entries": [{"id": "lmnopqrstuv", "title": "New missed Bitcoin video"}]})
            def dated(v):
                v["published"] = "2026-11-21"
                return v["published"]
            with patch("recent_uploads.subprocess.run", return_value=subprocess.CompletedProcess("yt-dlp", 0, stdout=payload)), \
                 patch("backfill.publication_date", side_effect=dated):
                result = recent_uploads.reconcile(temp, force=True)
            catalog = json.loads((Path(temp) / "catalog.json").read_text())
            self.assertEqual(catalog["groups"]["videos"][0]["id"], "abcdefghijk")
            self.assertEqual([v["id"] for v in catalog["groups"]["recent"]], ["lmnopqrstuv"])
            self.assertEqual(result["counts"]["missing_transcript"], 2)
            self.assertTrue(result["inventory_reconciliation"]["success"])

    def test_failed_inventory_never_reports_success(self):
        with tempfile.TemporaryDirectory() as temp:
            write_json(Path(temp) / "catalog.json", {"groups": {"videos": [video()]}})
            with patch("recent_uploads.subprocess.run", side_effect=subprocess.TimeoutExpired("yt-dlp", 85)):
                report = recent_uploads.reconcile(temp, force=True)
            self.assertFalse(report["inventory_reconciliation"]["success"])
            self.assertEqual(len(report["inventory_reconciliation"]["errors"]), 3)

    def test_inventory_gap_expands_window_without_advancing_success_anchor(self):
        with tempfile.TemporaryDirectory() as temp:
            write_json(Path(temp) / "catalog.json", {"groups": {"videos": [video()]},
                "inventory_reconciliation": {"anchor_ids": {tab: ["abcdefghijk"] for tab in ['videos','streams','shorts']}}})
            payload = json.dumps({"entries": [{"id": "lmnopqrstuv", "title": "New video"}]})
            def dated(v):
                v['published']='2026-11-21'
                return v['published']
            with patch("recent_uploads.subprocess.run", return_value=subprocess.CompletedProcess("yt-dlp",0,stdout=payload)), \
                 patch("backfill.publication_date", side_effect=dated):
                report=recent_uploads.reconcile(temp,force=True,head_limit=1)
            state=report['inventory_reconciliation']
            self.assertFalse(state['success'])
            self.assertEqual(state['next_head_limit'],2)
            self.assertEqual(state['anchor_ids']['videos'],['abcdefghijk'])

    def test_full_long_solo_stream_is_not_starved_by_small_batch_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            text = "I think Bitcoin is moving. " * 3000
            self.assertGreater(len(text), 60000)
            write_json(root / "speaker-policy.json", {"version": 1, "decisions": {}, "removed": {}})
            speaker_policy.stage_review(root, {"video": video(), "transcript": {"text": text, "segments": []},
                                              "summary": {"text": None}})
            decision = {"abcdefghijk": {"label": "ben_solo", "method": "gemini_full_text_review", "reason": "full-text review"}}
            with patch("speaker_policy.review_batch", return_value=decision) as review:
                report = speaker_policy.clean(root, model_batches=1)
            self.assertEqual(review.call_args.args[0][0]["transcript"]["text"], text)
            self.assertEqual(report["retained"], 1)
            self.assertTrue((root / "records/abcdefghijk.json").exists())


if __name__ == "__main__":
    unittest.main()
