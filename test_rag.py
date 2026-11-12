import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from archive_store import Archive, write_json
from cowen_mcp import MODERN, Server
from cowen_rag import RagArchive, ask_codex, citation, grounded_answer


def record(root, video_id="abcdefghijk", text="Bitcoin may bottom after the midterm election.", published="2026-10-30", timed=True):
    return Archive(root).create({"id": video_id, "title": "Bitcoin cycle outlook", "published": published,
                                 "url": "https://www.youtube.com/watch?v=" + video_id},
                                {"text": text, "segments": [{"text": text, "start": 75, "duration": 10}] if timed else []},
                                "Made-up summary says unicorns soar", None)


class RagTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        record(self.root)
        self.rag = RagArchive(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_concept_expansion_retrieves_btc_bitcoin_and_stems_bottoms(self):
        result = self.rag.search("BTC bottoms")
        self.assertEqual(result["source_count"], 1)
        self.assertIn("Bitcoin may bottom", result["sources"][0]["text"])
        self.assertEqual(result["sources"][0]["source_url"], "https://www.youtube.com/watch?v=abcdefghijk&t=75s")

    def test_generated_summaries_are_not_spoken_evidence(self):
        self.assertTrue(self.rag.search("unicorns")["no_evidence"])

    def test_incremental_index_adds_updates_and_removes_records(self):
        self.assertEqual(self.rag.status()["videos"], 1)
        record(self.root, "lmnopqrstuv", "Ethereum dominance increases", "2026-11-01")
        self.assertEqual(self.rag.status()["videos"], 2)
        record(self.root, text="Gold rallies while the dollar weakens")
        self.assertTrue(self.rag.search("Bitcoin")["no_evidence"] is False)  # title still matches
        self.assertTrue(self.rag.search("midterm")["no_evidence"])
        self.assertEqual(self.rag.search("dollar")["source_count"], 1)
        (self.root / "records" / "lmnopqrstuv.json").unlink()
        self.assertEqual(self.rag.status()["videos"], 1)
        self.assertTrue(self.rag.search("Ethereum")["no_evidence"])

    def test_date_bounds_include_full_end_day_and_reject_invalid_dates(self):
        record(self.root, "lmnopqrstuv", "Bitcoin cycle bottom", "2026-10-30T23:59:00+00:00")
        self.assertEqual(len(self.rag.search("bottom", before="2026-10-30", after="2026-10-30")["sources"]), 2)
        self.assertTrue(self.rag.search("bottom", after="2026-10-31")["no_evidence"])
        for filters in ({"before": "2026-02-30"}, {"before": "2025-01-01", "after": "2026-01-01"}):
            with self.assertRaises(ValueError):
                self.rag.search("bottom", **filters)

    def test_timeline_and_diversity_include_older_videos(self):
        for vid, month in (("lmnopqrstuv", "03"), ("stream12345", "07")):
            record(self.root, vid, "Bitcoin bottom and cycle risk", "2026-" + month + "-01")
        sources = self.rag.search("Bitcoin bottom", mode="timeline", limit=3, max_per_video=1)["sources"]
        self.assertEqual([s["published_at"][:7] for s in sources], ["2026-03", "2026-07", "2026-10"])

    def test_fts_syntax_is_escaped_and_empty_query_returns_no_evidence(self):
        self.assertIsInstance(self.rag.search('" OR NOT (bitcoin*) : -') ["sources"], list)
        self.assertTrue(self.rag.search("What does he say?")["no_evidence"])

    def test_read_pagination_can_reach_tail_without_fabricating_timestamps(self):
        tail = "UNIQUE_END_EVIDENCE"
        text = "bitcoin cycle " * 2000 + tail
        record(self.root, text=text, timed=False)
        page = self.rag.read_video("abcdefghijk", passage_count=1)
        self.assertIsNotNone(page["next_passage"])
        last = self.rag.read_video("abcdefghijk", start_passage=page["total_passages"] - 1)
        self.assertIn(tail, last["sources"][0]["text"])
        self.assertIsNone(last["sources"][0]["start_seconds"])
        self.assertNotIn("&t=", last["sources"][0]["source_url"])
        with self.assertRaises(ValueError):
            self.rag.read_video("abcdefghijk", at_seconds=100)
        with self.assertRaises(ValueError):
            self.rag.read_video("../../secrets")

    def test_timestamp_context_and_canonical_citations(self):
        page = self.rag.read_video("abcdefghijk", at_seconds=80)
        self.assertEqual(page["sources"][0]["timestamp"], "1:15")
        source = {**page["sources"][0], "source_url": "https://attacker.invalid", "title": "[bad](link)"}
        self.assertTrue(citation(source)["source_url"].startswith("https://www.youtube.com/watch?v="))

    def test_status_distinguishes_zero_errors_from_completed_backfill(self):
        write_json(self.root / "backfill.json", {"failures": {}, "status": "running_or_retrying"})
        status = self.rag.status()
        self.assertEqual(status["videos"], 1)
        self.assertEqual(status["pending_recorded_failures"], 0)
        self.assertIn("incomplete", status["coverage_note"])

    def test_failed_sync_returns_local_snapshot_without_leaking_git_output(self):
        (self.root / ".git").mkdir()
        with patch("cowen_rag.subprocess.run", side_effect=subprocess.CalledProcessError(1, "git", stderr=b"SECRET")):
            state = self.rag.sync(force=True)
        self.assertIn("local snapshot", state["warning"])
        self.assertNotIn("SECRET", json.dumps(state))
        self.assertEqual(self.rag.status()["videos"], 1)

    def test_local_changes_are_preserved_by_refresh(self):
        (self.root / ".git").mkdir()
        with patch("cowen_rag.subprocess.run", return_value=subprocess.CompletedProcess("git", 0, stdout=b" M catalog.json")) as run:
            self.assertIn("tracked changes", self.rag.sync(force=True)["warning"])
        self.assertEqual(run.call_count, 1)

    def test_answer_rejects_fabricated_quotes_and_urls(self):
        context = self.rag.search("midterm")
        source = context["sources"][0]
        payload = {"claims": [{"text": "He discusses an election-linked bottom.", "support": [
            source["id"] + ":excerpt:0"]}], "limitations": []}
        answer = grounded_answer(payload, context)
        self.assertIn("&t=75s", answer)
        payload["claims"][0]["text"] = 'He said "Bitcoin will absolutely hit a million dollars."'
        with self.assertRaises(ValueError):
            grounded_answer(payload, context)
        payload["claims"][0]["text"] = "He discusses a bottom."
        payload["claims"][0]["support"] = ["invented:excerpt:0"]
        with self.assertRaises(ValueError):
            grounded_answer(payload, context)

    def test_mcp_legacy_and_modern_tools_use_identical_evidence(self):
        server = Server(self.rag)
        legacy = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}})
        self.assertEqual(legacy["result"]["protocolVersion"], "2025-11-25")
        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "search_cowen_transcripts", "arguments": {"question": "midterm"},
            "_meta": {"io.modelcontextprotocol/protocolVersion": MODERN}}}
        result = server.handle(call)["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertEqual(result["structuredContent"]["source_count"], 1)
        call["params"]["arguments"]["limit"] = True
        self.assertTrue(server.handle(call)["result"]["isError"])
        call["params"]["arguments"] = {"question": "bitcoin", "command": "rm something"}
        self.assertTrue(server.handle(call)["result"]["isError"])
        self.assertIsNone(server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_answer_mode_disables_mcp_with_valid_codex_config_paths(self):
        context = self.rag.search("midterm")
        def run(command, **kwargs):
            self.assertIn("mcp_servers.cowen-archive.enabled=false", command)
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text(json.dumps({"claims": [], "limitations": ["Insufficient evidence"]}))
            return subprocess.CompletedProcess(command, 0)
        with patch("cowen_rag.shutil.which", return_value="/fake/codex"), patch("cowen_rag.subprocess.run", side_effect=run), \
             patch("cowen_rag.answer_only_overrides", return_value=["-c", "mcp_servers.cowen-archive.enabled=false"]):
            self.assertIn("Insufficient evidence", ask_codex(context))

    def test_real_stdio_transport_emits_only_json_rpc(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "server/discover"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "cowen_archive_status", "arguments": {}}},
        ]
        result = subprocess.run([sys.executable, str(Path(__file__).with_name("cowen_mcp.py")), "--archive", str(self.root)],
                                input="\n".join(json.dumps(r) for r in requests) + "\n", text=True, capture_output=True, timeout=15, check=True)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(replies), 3)
        self.assertEqual(len(replies[1]["result"]["tools"]), 5)
        self.assertEqual(replies[2]["result"]["structuredContent"]["videos"], 1)


if __name__ == "__main__":
    unittest.main()
