import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from archive_store import Archive, export, write_json
from archive_milestones import audit
from cowen_rag import RagArchive
import monitor
import speaker_policy as policy


BEN = "Hey everyone and thanks for jumping back into the cryptoverse. Today we're going to talk about Bitcoin. Bitcoin dominance may increase."


def video(identifier="abcdefghijk", title="Bitcoin: Risk", published="2026-11-15T01:00:00+00:00"):
    return {"id": identifier, "title": title, "published": published, "channel_id": policy.CHANNEL_ID,
            "url": "https://www.youtube.com/watch?v=" + identifier}


def bundle(text=BEN):
    return {"text": text, "segments": [], "provider": "test"}


class SpeakerTests(unittest.TestCase):
    def test_solo_live_and_written_questions_are_kept(self):
        result = policy.decide(video(title="Bitcoin Q4 Dump (Live Show!)"), BEN + " >> Someone says what about Ethereum? I think Bitcoin is stronger.")
        self.assertEqual(result["label"], "ben_solo")

    def test_nfa_guests_and_other_speakers_are_excluded(self):
        for title in ("NFA Live! Bitcoin in 2025", "Market Discussion with Gareth Soloway", "Out Of The Cryptoverse: Part 1",
                      "Investing Through the Cycles: Live with the Speakers", "Crypto Fundamentals with Guy from Coin Bureau"):
            with self.subTest(title=title):
                self.assertEqual(policy.decide(video(title=title), BEN)["label"], "exclude")
        self.assertEqual(policy.decide(video(), BEN + " I'm joined today by Rob from Digital Asset News.")["label"], "exclude")

    def test_topic_words_alone_do_not_exclude_ben(self):
        for title in ("Bitcoin: A Date With Destiny", "Market Returns by Political Party", "See you at the Bitcoin Conference"):
            self.assertEqual(policy.decide(video(title=title), BEN)["label"], "ben_solo")

    def test_late_guest_or_unidentified_clip_requires_review(self):
        self.assertEqual(policy.decide(video(), BEN + "x" * 6000 + " I'm joined today by a guest.")["label"], "review")
        self.assertEqual(policy.decide(video(), "I think the dollar could rise.")["label"], "review")
        self.assertEqual(policy.decide({**video(), "channel_id": "other-channel"}, BEN)["label"], "exclude")

    def test_cleanup_removes_records_passages_emails_and_search_hits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = Archive(root)
            archive.create(video(), bundle(), None, None)
            archive.create(video("lmnopqrstuv", "NFA Live!"), bundle("Rob and Guy discuss obscureguesttopic"), None, None)
            archive.create(video("stream12345", "Unknown clip"), bundle("Unidentified discussion of obscureunknownword"), None, None)
            (root / "emails").mkdir()
            (root / "emails" / "lmnopqrstuv.eml").write_text("old guest summary")
            rag = RagArchive(root)
            self.assertTrue(rag.search("obscureguesttopic")["sources"])
            report = policy.clean(root)
            self.assertEqual(report["retained"], 1)
            self.assertEqual(report["removed_transcript_count"], 1)
            self.assertEqual(report["pending_review"], 1)
            for folder, suffix in (("records", ".json"), ("passages", ".jsonl"), ("emails", ".eml")):
                self.assertFalse((root / folder / ("lmnopqrstuv" + suffix)).exists())
            self.assertTrue((root / "speaker-review" / "records" / "stream12345.json").exists())
            self.assertTrue(rag.search("obscureguesttopic")["no_evidence"])
            self.assertTrue(rag.search("obscureunknownword")["no_evidence"])
            self.assertEqual(rag.status()["videos"], 1)
            self.assertEqual(audit(root)["successful_transcripts"], 1)
            self.assertEqual(export(root, root / "export"), 1)
            self.assertEqual(policy.clean(root)["retained"], 1)

    def test_review_approved_ben_clip_is_restored_and_other_clip_deleted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = Archive(root)
            archive.create(video(), bundle("My Bitcoin dominance analysis is unchanged."), None, None)
            archive.create(video("lmnopqrstuv"), bundle("I am another unidentified analyst."), None, None)
            decisions = {"abcdefghijk": {"label": "ben_solo", "reason": "Ben's solo clip", "method": "gemini_full_text_review"},
                         "lmnopqrstuv": {"label": "exclude", "reason": "Another speaker", "method": "gemini_full_text_review"}}
            with patch.object(policy, "review_batch", return_value=decisions):
                report = policy.clean(root, model_batches=1)
            self.assertEqual(report["retained"], 1)
            self.assertEqual(report["pending_review"], 0)
            self.assertIsNotNone(archive.get("abcdefghijk"))
            self.assertIsNone(archive.get("lmnopqrstuv"))

    def test_unreviewed_source_cannot_bypass_archive_guard(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_json(root / policy.POLICY_FILE, policy.load_policy(root))
            with self.assertRaises(ValueError):
                Archive(root).create(video(title="NFA Live!"), bundle(), None, None)
            with self.assertRaises(ValueError):
                Archive(root).create(video(), bundle("Unidentified voice"), None, None)

    def test_cleanup_rejects_mismatched_ids_before_removing_any_transcript(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record = Archive(root).create(video(), bundle(), None, None)
            record["video"]["id"] = "../outside"
            write_json(root / "records" / "abcdefghijk.json", record)
            with self.assertRaises(ValueError):
                policy.clean(root)
            self.assertTrue((root / "records" / "abcdefghijk.json").exists())

    def test_clip_matches_admitted_monologue_and_parent_edits_invalidate_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = Archive(root)
            words = [f"distinctiveword{i}" for i in range(200)]
            archive.create(video(), bundle(BEN + " " + " ".join(words)), None, None)
            archive.create(video("lmnopqrstuv", "A clipped analysis"), bundle(" ".join(words[60:150])), None, None)
            report = policy.clean(root)
            self.assertEqual(report["clips_matched_to_ben_monologues"], 1)
            self.assertEqual(report["retained"], 2)
            self.assertEqual(report["pending_review"], 0)
            rag = RagArchive(root)
            self.assertEqual(rag.status()["videos"], 2)
            archive.create(video(), bundle(BEN), None, None)
            self.assertEqual(rag.status()["videos"], 1)
            report = policy.clean(root)
            self.assertEqual(report["pending_review"], 1)
            self.assertIsNone(archive.get("lmnopqrstuv"))

    def test_model_output_requires_matching_ids_confidence_and_real_evidence(self):
        record = {"video": video(), "transcript": bundle("My Bitcoin dominance analysis is unchanged.")}
        def response(decisions):
            return {"candidates": [{"content": {"parts": [{"text": json.dumps({"decisions": decisions})}]}}]}
        decision = {"id": "abcdefghijk", "label": "ben_solo", "confidence": .99,
                    "reason": "Ben", "evidence": "fabricated supporting text"}
        with patch.dict(os.environ, {"GEMINI_API_KEY": "placeholder"}), patch.object(policy.urllib.request, "urlopen") as request:
            request.return_value.__enter__.return_value = io.BytesIO(json.dumps(response([decision])).encode())
            self.assertEqual(policy.review_batch([record])["abcdefghijk"]["label"], "review")
            decision["evidence"] = "Bitcoin dominance analysis is unchanged."
            request.return_value.__enter__.return_value = io.BytesIO(json.dumps(response([decision])).encode())
            self.assertEqual(policy.review_batch([record])["abcdefghijk"]["label"], "ben_solo")
            request.return_value.__enter__.return_value = io.BytesIO(json.dumps(response([])).encode())
            with self.assertRaises(ValueError):
                policy.review_batch([record])


class UploadTests(unittest.TestCase):
    def environment(self, root, state):
        (root / ".git").mkdir()
        write_json(root / "catalog.json", {"groups": {"videos": []}, "collection_scope": {"start_date": "2021-01-01"}})
        write_json(state, {"seen_ids": ["oldvideo123"]})

    def test_transcript_survives_summary_failure_and_feed_expiry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, state = Path(temporary) / "archive", Path(temporary) / "state.json"
            root.mkdir(); self.environment(root, state)
            with patch.dict(os.environ, {"ARCHIVE_PATH": str(root)}), patch.object(monitor, "STATE_PATH", state), \
                 patch.object(monitor, "ensure_notification_destination"), \
                 patch.object(monitor, "fetch_feed", return_value=[video()]), \
                 patch.object(monitor, "get_transcript", return_value=bundle()), \
                 patch.object(monitor, "summarize", side_effect=RuntimeError("quota")):
                self.assertEqual(monitor.main(), 1)
            self.assertIsNotNone(Archive(root).get("abcdefghijk"))
            saved = json.loads(state.read_text())
            self.assertIn("abcdefghijk", saved["pending_uploads"])
            self.assertNotIn("abcdefghijk", saved["seen_ids"])
            with patch.dict(os.environ, {"ARCHIVE_PATH": str(root)}), patch.object(monitor, "STATE_PATH", state), \
                 patch.object(monitor, "ensure_notification_destination"), \
                 patch.object(monitor, "fetch_feed", return_value=[video("oldvideo123")]), \
                 patch.object(monitor, "get_transcript") as fetch, \
                 patch.object(monitor, "summarize", return_value="recovered summary"), \
                 patch.object(monitor, "send_telegram", return_value=False), patch.object(monitor, "send_email", return_value=True):
                self.assertEqual(monitor.main(), 0)
                fetch.assert_not_called()
            self.assertIn("abcdefghijk", json.loads(state.read_text())["seen_ids"])
            self.assertNotIn("abcdefghijk", json.loads(state.read_text())["pending_uploads"])

    def test_new_upload_is_merged_even_if_already_notified(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, state = Path(temporary) / "archive", Path(temporary) / "state.json"
            root.mkdir(); self.environment(root, state)
            write_json(state, {"seen_ids": ["abcdefghijk"]})
            with patch.dict(os.environ, {"ARCHIVE_PATH": str(root)}), patch.object(monitor, "STATE_PATH", state), \
                 patch.object(monitor, "fetch_feed", return_value=[video()]):
                monitor.main()
                monitor.main()
            catalog = json.loads((root / "catalog.json").read_text())
            self.assertEqual([v["id"] for v in catalog["groups"]["recent"]], ["abcdefghijk"])

    def test_future_conversation_is_skipped_before_download_or_email(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, state = Path(temporary) / "archive", Path(temporary) / "state.json"
            root.mkdir(); self.environment(root, state)
            policy.clean(root)
            with patch.dict(os.environ, {"ARCHIVE_PATH": str(root)}), patch.object(monitor, "STATE_PATH", state), \
                 patch.object(monitor, "ensure_notification_destination"), \
                 patch.object(monitor, "fetch_feed", return_value=[video(title="NFA Live! Bitcoin")]), \
                 patch.object(monitor, "get_transcript") as fetch, patch.object(monitor, "send_email") as email:
                monitor.main()
                fetch.assert_not_called(); email.assert_not_called()
            self.assertEqual(len(list(Archive(root).records())), 0)
            self.assertIn("abcdefghijk", json.loads(state.read_text())["speaker_filtered_ids"])


if __name__ == "__main__":
    unittest.main()
