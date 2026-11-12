# Ask the Cowen transcript archive in Codex

The `cowen-archive` MCP server lets Codex retrieve evidence from the private
`cowen_video_archive` checkout and write answers with timestamped YouTube links.
Ask in ordinary language, for example:

> Use the Cowen archive: what does Ben say about Bitcoin's halving cycle?
> Compare his views on the cycle bottom across the collected videos. Cite the videos.
> What price targets did he state before November 2026? Separate targets from scenarios.

Codex supplies the reasoning and answer generation using your existing Codex access.
Retrieval needs no new API key, embedding service or vector database. Normal Codex
usage limits still apply. The corpus stays in the private GitHub repository and local
search cache; retrieved passages enter your Codex conversation when you ask about them.

## Installation

Python 3.9 or later with SQLite FTS5 on macOS or Linux is sufficient. No pip packages are required.
Clone the public bot code and private archive into sibling folders. Keep an existing
Git login that can read the private archive. Then register the local stdio server:

```bash
codex mcp add cowen-archive -- /usr/bin/python3 /absolute/path/cowen_monitor_bot_live/cowen_mcp.py --archive /absolute/path/cowen_video_archive --auto-sync
```

The installer for this machine uses actual absolute paths. Start a new Codex chat
after registration if the running chat has not discovered the tools yet.
`codex mcp get cowen-archive` shows the installed command; `codex mcp remove cowen-archive`
removes only this integration.

## Tools

- `search_cowen_transcripts`: question, optional query reformulations, publication
  date filters, relevance or timeline mode, and limits per video.
- `read_cowen_video`: read surrounding passages at a caption timestamp or paginate
  through the complete transcript with `start_passage` / `next_passage`.
- `list_cowen_videos`: browse collected videos, titles and publication dates.
- `cowen_archive_status`: show coverage, freshness and pending collection status.
- `refresh_cowen_archive`: fetch the private archive and refresh the local index.

Search uses SQLite BM25, a small cryptocurrency concept glossary, reciprocal-rank
fusion across supplied query reformulations, and diversity across videos. It is
lexical retrieval, not dense-vector semantic search. Codex can formulate additional
searches and inspect surrounding text for conceptual or multistep questions.
Timeline mode samples relevant passages across available months; it is not an
exhaustive review of every matching passage. Only spoken-content transcripts are
indexed, excluding generated summaries and email bodies.

## Command line

Run from the code checkout (or use an absolute script path):

```bash
python3 cowen_rag.py --sync status
python3 cowen_rag.py --sync search "Bitcoin cycle bottom" --limit 8
python3 cowen_rag.py context "How did Ben's views change?" --query "Bitcoin halving" --mode timeline
python3 cowen_rag.py search "Bitcoin risk" --before 2026-11-01 --after 2026-10-01
python3 cowen_rag.py read g2MQiby7_qY --at-seconds 2102
python3 cowen_rag.py --sync ask "What did Ben say about midterm-year Bitcoin bottoms?"
```

`search` and `context` return JSON evidence for any agent. `ask` invokes the signed-in
Codex CLI to generate a structured answer whose claims select pre-extracted source
excerpts by ID. It creates citation links from validated source IDs and caption
timestamps, and checks any direct quotations against the cited transcript text.
It rejects invented citation IDs, direct quotes and URLs rather than printing an
unverified answer. These checks validate source provenance; they are not a proof
that a model interpreted the passage correctly. In a Codex chat the MCP evidence
tools provide the sources and Codex generates its answer directly.

## Freshness, privacy and limits

With `--auto-sync`, the MCP server refreshes from private GitHub before retrieval,
at most once every five minutes. Index changes are incremental, including added,
changed and removed records. It will not overwrite tracked local changes; failed
network/authentication refreshes retain the local snapshot and return a warning.
Use `refresh_cowen_archive` for an immediate refresh.

The cache is `.cowen-rag/index.sqlite` inside the private archive and must be ignored
by Git. It contains private transcript text. Neither transcripts nor this database
belong in the public code repository. Git credentials are used through the existing
credential helper and are never included in tool responses or error logs.

Your Mac must be running to query its local MCP server. The cloud transcript
collector and future-video email delivery continue independently when it is off.
New transcripts become searchable at the next successful refresh.

Historical backfill is configured for 2021–2026, newest first. Archive status
includes the requested date range and collected counts by year. Expanding collection
does not mean the earlier years are already complete; filter and cite only the
transcripts that have actually been collected.

Answers cover collected videos only. Check the returned video count, date span and
backfill status; zero failures does not mean the historical archive is complete.
Caption recognition errors remain possible. No chart pixels are interpreted.
Unknown caption timestamps remain unknown; citations then link to the video without
an invented time. Livestreams may include other speakers, so check context before
attributing a statement to Ben. Historical forecasts are not current market facts.

Run checks with `python3 -m unittest test_rag.py test_archive.py`.
