"""Continuously archive this year's public captions, newest first, without historical email."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from archive_store import Archive, now, write_json
import monitor

TABS = ("videos", "streams", "shorts")


def discover(root, year):
    """Read complete channel tabs; the uploads playlist can silently stop at 100."""
    def tab_videos(tab):
        result = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--flat-playlist", "--dump-single-json",
             "--ignore-no-formats-error", "--no-warnings",
             f"https://www.youtube.com/channel/{monitor.CHANNEL_ID}/{tab}"],
            capture_output=True, text=True, timeout=300, check=True,
        )
        data = json.loads(result.stdout)
        videos, seen = [], set()
        for entry in data.get("entries", []):
            video_id = entry.get("id") or ""
            if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id) or video_id in seen:
                continue
            seen.add(video_id)
            videos.append({"id": video_id, "title": entry.get("title") or video_id,
                           "published": None, "url": "https://www.youtube.com/watch?v=" + video_id,
                           "catalog_position": len(videos), "tab": tab, "channel_id": monitor.CHANNEL_ID})
        return tab, videos

    with ThreadPoolExecutor(max_workers=3) as pool:
        groups = dict(pool.map(tab_videos, TABS))
    if not any(groups.values()):
        raise RuntimeError("Channel enumeration returned no public uploads")
    catalog = {"schema_version": 1, "channel_id": monitor.CHANNEL_ID,
               "retrieved_at": now(), "target_year": year, "order": "newest_first_per_tab",
               "scope": "public accessible videos, streams and shorts; private/deleted/member-only videos excluded",
               "groups": groups}
    write_json(Path(root) / "catalog.json", catalog)
    return catalog


def publication_date(video):
    if video.get("published"):
        return video["published"]
    page = monitor.http_get(video["url"], timeout=20).decode("utf-8", errors="replace")
    match = re.search(r'"publishDate"\s*:\s*"([^"\\]+)"', page)
    if not match:
        raise RuntimeError("Publication date unavailable; cannot verify year")
    value = match[1]
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    video["published"] = parsed.astimezone(timezone.utc).isoformat() if parsed.tzinfo else value
    video["published_source"] = "YouTube player microformat.publishDate"
    video["published_precision"] = "day" if len(value) == 10 else "timestamp"
    return video["published"]


def run(root, *, year, batch_size=3, seconds=160):
    archive = Archive(root)
    catalog_path, progress_path = archive.root / "catalog.json", archive.root / "backfill.json"
    catalog = json.loads(catalog_path.read_text()) if catalog_path.exists() else discover(root, year)
    if catalog.get("target_year") != year:
        raise RuntimeError("Catalog year mismatch; explicitly discover a new catalog")
    progress = json.loads(progress_path.read_text()) if progress_path.exists() else {"failures": {}}
    deadline = time.monotonic() + seconds
    completed = attempts = 0
    heads = []
    while completed < batch_size and attempts < batch_size * 3 and time.monotonic() < deadline:
        heads = []
        for tab, videos in catalog["groups"].items():
            for video in videos:
                if archive.get(video["id"]):
                    continue
                failure = progress.get("failures", {}).get(video["id"], {})
                if failure.get("retry_after", "") > now():
                    continue
                try:
                    published = publication_date(video)
                    if published[:4] < str(year):
                        progress.setdefault("year_boundaries", {})[tab] = video["id"]
                        break
                    if published[:4] > str(year):
                        continue
                    heads.append(video)
                except Exception as error:
                    defer(progress, video, error)
                break
        write_json(catalog_path, catalog)
        if not heads:
            break
        video = max(heads, key=lambda item: item["published"])
        attempts += 1
        try:
            bundle = monitor.get_transcript(video["url"], with_metadata=True, hosted_only=True)
            if not bundle["text"].strip():
                raise RuntimeError("Transcript unavailable")
            record = archive.create(video, bundle, None, None)
            record["backfill_complete"] = True
            record["historical_email_requested"] = False
            archive.save(record)
            completed += 1
            progress.setdefault("failures", {}).pop(video["id"], None)
            progress["last_success"] = {"video_id": video["id"], "at": now()}
            print(f"Archived {video['published']} {video['id']}: {len(bundle['text'])} characters")
        except Exception as error:
            defer(progress, video, error)
        write_json(progress_path, progress)
        time.sleep(1)  # Modest sequential traffic to public extraction endpoints.
    progress["last_scan_at"] = now()
    progress["records"] = sum(1 for _ in archive.records())
    progress["completed_this_run"] = completed
    progress["status"] = "running_or_retrying" if heads else "at_year_boundaries_or_waiting_for_retries"
    write_json(progress_path, progress)
    print(f"Saved {completed} transcripts this run. Total records: {progress['records']}.")


def defer(progress, video, error):
    progress.setdefault("failures", {})[video["id"]] = {
        "error_type": type(error).__name__, "at": now(),
        "retry_after": (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat(),
    }
    print(f"Deferred {video['id']}: {type(error).__name__}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["discover", "run"])
    parser.add_argument("--archive", default=os.getenv("ARCHIVE_PATH", "private-archive"))
    parser.add_argument("--year", type=int, default=int(os.getenv("BACKFILL_YEAR", "2026")))
    parser.add_argument("--batch-size", type=int, default=3)
    args = parser.parse_args()
    if args.command == "discover":
        catalog = discover(args.archive, args.year)
        print({key: len(value) for key, value in catalog["groups"].items()})
    else:
        run(args.archive, year=args.year, batch_size=args.batch_size)
