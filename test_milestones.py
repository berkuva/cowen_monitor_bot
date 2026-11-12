import json
import os
from pathlib import Path
import smtplib
import tempfile
import unittest
from unittest.mock import patch

from archive_store import Archive, release_metadata
from archive_milestones import audit, notify
import monitor


def populate(root, count):
    archive = Archive(root)
    for number in range(count):
        video_id = f"v{number:010d}"
        archive.create({"id": video_id, "title": "Bitcoin update", "url": "https://www.youtube.com/watch?v=" + video_id,
                        "published": "2026-10-30T23:15:00+00:00"}, {"text": "spoken analysis", "segments": []}, None, None)


class MilestoneTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_before_100_no_email_at_100_once_and_updates_do_not_increment(self):
        populate(self.root, 99)
        with patch("archive_milestones.monitor.send_mail", return_value=True) as send:
            self.assertTrue(notify(self.root))
            send.assert_not_called()
            populate(self.root, 100)
            self.assertTrue(notify(self.root))
            self.assertEqual(send.call_count, 1)
            self.assertIn("100 transcripts", send.call_args.args[1])
            self.assertTrue(notify(self.root))
            self.assertEqual(send.call_count, 1)
        state = json.loads((self.root / "milestones.json").read_text())
        self.assertEqual(state["next_milestone"], 200)

    def test_crossed_multiple_boundaries_notifies_each_once(self):
        populate(self.root, 202)
        with patch("archive_milestones.monitor.send_mail", return_value=True) as send:
            notify(self.root)
            self.assertEqual(send.call_count, 2)
            notify(self.root)
            self.assertEqual(send.call_count, 2)
        self.assertEqual(set(json.loads((self.root / "milestones.json").read_text())["delivered"]), {"100", "200"})

    def test_failed_delivery_stays_pending_and_retries_stable_message_id(self):
        populate(self.root, 100)
        with patch("archive_milestones.monitor.send_mail", side_effect=smtplib.SMTPException("private error")) as send:
            self.assertFalse(notify(self.root))
            first_id = send.call_args.kwargs["message_id"]
        state = json.loads((self.root / "milestones.json").read_text())
        self.assertEqual(state["delivered"], {})
        self.assertEqual(state["next_milestone"], 100)
        self.assertNotIn("private error", json.dumps(state))
        with patch("archive_milestones.monitor.send_mail", return_value=True) as send:
            self.assertTrue(notify(self.root))
            self.assertEqual(send.call_args.kwargs["message_id"], first_id)

    def test_no_email_configuration_does_not_mark_milestone_delivered(self):
        populate(self.root, 100)
        with patch("archive_milestones.monitor.send_mail", return_value=False):
            self.assertFalse(notify(self.root))
        self.assertEqual(json.loads((self.root / "milestones.json").read_text())["delivered"], {})

    def test_empty_or_undated_records_do_not_count(self):
        populate(self.root, 100)
        path = self.root / "records" / "v0000000000.json"
        data = json.loads(path.read_text())
        data["video"]["published"] = None
        path.write_text(json.dumps(data))
        path = self.root / "records" / "v0000000001.json"
        data = json.loads(path.read_text())
        data["transcript"]["text"] = ""
        path.write_text(json.dumps(data))
        with patch("archive_milestones.monitor.send_mail") as send:
            notify(self.root)
            send.assert_not_called()
        report = audit(self.root)
        self.assertEqual(report["successful_transcripts"], 98)
        self.assertEqual(len(report["invalid_records"]), 2)

    def test_release_date_preserves_original_timestamp_and_precision(self):
        full = release_metadata({"published": "2026-10-30T23:15:00-07:00"})
        self.assertEqual(full["release_date"], "2026-10-30")
        self.assertEqual(full["published"], "2026-10-30T23:15:00-07:00")
        self.assertEqual(full["published_precision"], "timestamp")
        day = release_metadata({"published": "2026-10-30"})
        self.assertEqual(day["published_precision"], "day")
        for invalid in (None, "", "2026-02-30", "yesterday", "20261030"):
            with self.assertRaises(ValueError):
                release_metadata({"published": invalid})

    def test_archive_rejects_missing_date_before_writing(self):
        with self.assertRaises(ValueError):
            Archive(self.root).create({"id": "abcdefghijk", "published": None}, {"text": "analysis"}, None, None)
        self.assertFalse((self.root / "records" / "abcdefghijk.json").exists())

    def test_audit_adds_release_date_to_existing_records_and_passages(self):
        populate(self.root, 1)
        path = self.root / "records" / "v0000000000.json"
        record = json.loads(path.read_text())
        record["video"].pop("release_date")
        record["video"].pop("published_precision")
        path.write_text(json.dumps(record))
        report = audit(self.root, repair=True)
        self.assertEqual(report["records_updated"], 1)
        self.assertEqual(json.loads(path.read_text())["video"]["release_date"], "2026-10-30")
        passage = json.loads((self.root / "passages" / "v0000000000.jsonl").read_text())
        self.assertEqual(passage["release_date"], "2026-10-30")
        self.assertEqual(audit(self.root, repair=True)["records_updated"], 0)

    def test_smtp_subject_message_id_and_private_mime_record(self):
        values = {"SMTP_HOST": "smtp.test.invalid", "SMTP_USERNAME": "sender@test.invalid", "SMTP_PASSWORD": "placeholder",
                  "EMAIL_FROM": "sender@test.invalid", "EMAIL_TO": "recipient@test.invalid", "SMTP_PORT": "587"}
        destination = self.root / "milestone-emails" / "100.eml"
        with patch.dict(os.environ, values), patch.object(monitor.smtplib, "SMTP") as smtp:
            smtp.return_value.__enter__.return_value.send_message.return_value = {}
            self.assertTrue(monitor.send_mail("Milestone body", "100 transcripts", destination=destination,
                                              message_id="<milestone-100@test.invalid>"))
            message = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
            self.assertEqual(message["Subject"], "100 transcripts")
            self.assertEqual(message["Message-ID"], "<milestone-100@test.invalid>")
        self.assertTrue(destination.exists())


if __name__ == "__main__":
    unittest.main()
