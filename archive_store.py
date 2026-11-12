"""Private, versioned source records plus a local, citation-preserving retrieval index."""
from __future__ import annotations

import argparse
from bisect import bisect_right
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

SCHEMA_VERSION = 1


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


class Archive:
    def __init__(self, root):
        self.root = Path(root)

    def path(self, video_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            raise ValueError("Invalid video ID")
        return self.root / "records" / (video_id + ".json")

    def get(self, video_id):
        path = self.path(video_id)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def save(self, record):
        write_json(self.path(record["video"]["id"]), record)
        passages = self.root / "passages" / (record["video"]["id"] + ".jsonl")
        passages.parent.mkdir(parents=True, exist_ok=True)
        temporary = passages.with_suffix(".jsonl.tmp")
        temporary.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in chunks(record)), encoding="utf-8")
        temporary.replace(passages)

    def create(self, video, bundle, summary, email_body):
        record = {
            "schema_version": SCHEMA_VERSION,
            "video": video,
            "transcript": bundle,
            "transcript_sha256": hashlib.sha256(bundle["text"].encode()).hexdigest(),
            "summary": {"text": summary, "model": os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite") if summary else None,
                        "generated_at": now(), "prompt_version": "cowen-summary-v2"},
            "email": {"body": email_body, "sent_at": None},
        }
        self.save(record)
        return record

    def records(self):
        for path in sorted((self.root / "records").glob("*.json")):
            yield json.loads(path.read_text(encoding="utf-8"))


def chunks(record, max_chars=1800, overlap=200):
    """Keep timestamps when available; explicitly leave them null for text-only sources."""
    video = record["video"]
    segments = record["transcript"].get("segments") or []
    offsets, position, texts, timed = [], 0, [], []
    for segment in segments:
        text = str(segment.get("text", "")).strip()
        if text:
            offsets.append(position)
            texts.append(text)
            timed.append(segment)
            position += len(text) + 1
    source = " ".join(texts) if timed else record["transcript"]["text"]
    offset, number = 0, 0
    while offset < len(source):
        cut = min(len(source), offset + max_chars)
        if cut < len(source):
            boundary = source.rfind(" ", offset, cut)
            cut = boundary if boundary > offset + max_chars // 2 else cut
        start = end = None
        if timed:
            first = timed[max(0, bisect_right(offsets, offset) - 1)]
            last = timed[max(0, bisect_right(offsets, cut - 1) - 1)]
            start, end = first.get("start"), last.get("start")
            if end is not None:
                end += last.get("duration") or 0
        yield make_chunk(video, number, source[offset:cut], start, end, "transcript")
        number += 1
        if cut == len(source):
            break
        offset = max(offset + 1, cut - overlap)
    if record["summary"]["text"]:
        yield make_chunk(video, number + 1, record["summary"]["text"], None, None, "summary")


def make_chunk(video, number, text, start, end, kind):
    url = video["url"]
    if start is not None:
        url += "&t=" + str(max(0, int(start))) + "s"
    return {"id": f"{video['id']}:{kind}:{number}", "video_id": video["id"],
            "title": video["title"], "published_at": video.get("published") or None,
            "source_url": url, "kind": kind, "start_seconds": start,
            "end_seconds": end, "text": text}


def export(root, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(output / "rag.sqlite")
    db.execute("DROP TABLE IF EXISTS passages")
    db.execute("CREATE VIRTUAL TABLE passages USING fts5(text, title, metadata UNINDEXED)")
    total = 0
    with (output / "passages.jsonl").open("w", encoding="utf-8") as stream:
        for record in Archive(root).records():
            for passage in chunks(record):
                encoded = json.dumps(passage, ensure_ascii=False)
                stream.write(encoded + "\n")
                db.execute("INSERT INTO passages VALUES (?, ?, ?)",
                           (passage["text"], passage["title"], encoded))
                total += 1
    db.commit()
    db.close()
    return total


def query(database, question, limit=8, before=None, after=None):
    words = re.findall(r"\w+", question, flags=re.UNICODE)
    if not words:
        return []
    # Never pass raw natural-language syntax into FTS MATCH.
    match = " OR ".join('"' + word.replace('"', '""') + '"' for word in words)
    db = sqlite3.connect(database)
    conditions, arguments = ["passages MATCH ?"], [match]
    for bound, operator in ((before, "<="), (after, ">=")):
        if bound:
            field = "json_extract(metadata, '$.published_at')"
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", bound):
                field = "substr(" + field + ", 1, 10)"
            conditions.append(field + " " + operator + " ?")
            arguments.append(bound)
    arguments.append(min(100, max(1, limit)))
    rows = db.execute("SELECT metadata, bm25(passages) FROM passages WHERE " +
                      " AND ".join(conditions) + " ORDER BY bm25(passages) LIMIT ?", arguments)
    results = [{**json.loads(row[0]), "retrieval_score": row[1]} for row in rows]
    db.close()
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("export")
    build.add_argument("--archive", required=True)
    build.add_argument("--output", default="rag")
    search = commands.add_parser("query")
    search.add_argument("question")
    search.add_argument("--database", default="rag/rag.sqlite")
    search.add_argument("--limit", type=int, default=8)
    search.add_argument("--before")
    search.add_argument("--after")
    args = parser.parse_args()
    if args.command == "export":
        print(json.dumps({"passages": export(args.archive, args.output)}))
    else:
        print(json.dumps(query(args.database, args.question, args.limit, args.before, args.after),
                         ensure_ascii=False, indent=2))
