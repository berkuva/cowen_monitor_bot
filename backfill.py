"""Continuously archive this year's public captions, newest first, without historical email."""
from __future__ import annotations

import argparse
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error

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


class CollectionError(RuntimeError):
    def __init__(self, message, *, stage, code, details=None):
        super().__init__(message)
        self.stage, self.code, self.details = stage, code, details or {}


class DateTags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("itemprop") in ("datePublished", "uploadDate"):
            self.values[attrs["itemprop"]] = attrs.get("content", "")


def publication_date(video):
    if video.get("published"):
        return video["published"]
    page = monitor.http_get(video["url"] + "&hl=en", timeout=20).decode("utf-8", errors="replace")
    candidates = []
    for field in ("publishDate", "uploadDate"):
        match = re.search(r'"' + field + r'"\s*:\s*"([^"\\]+)"', page)
        if match:
            candidates.append((match[1], "YouTube player microformat." + field))
    tags = DateTags()
    tags.feed(page)
    candidates.extend((value, "YouTube watch meta." + field) for field, value in tags.values.items())
    # Cloud watch pages may omit the player microformat while retaining the
    # publication date in the video's primary information panel.
    match = re.search(r'(?:var\s+)?ytInitialData\s*=\s*', page)
    initial = {}
    if match:
        try:
            initial, _ = json.JSONDecoder().raw_decode(page[match.end():])
        except ValueError:
            pass
    def primary_dates(node):
        if isinstance(node, dict):
            primary = node.get("videoPrimaryInfoRenderer", {})
            date = primary.get("dateText", {})
            value = date.get("simpleText") or "".join(r.get("text", "") for r in date.get("runs", []))
            if value:
                yield value
            for child in node.values():
                yield from primary_dates(child)
        elif isinstance(node, list):
            for child in node:
                yield from primary_dates(child)
    primary = list(primary_dates(initial))
    for value in primary:
        match = re.search(r'([A-Z][a-z]{2,8} \d{1,2}, \d{4})', value)
        if match:
            for fmt in ("%b %d, %Y", "%B %d, %Y"):
                try:
                    candidates.append((datetime.strptime(match[1], fmt).date().isoformat(),
                                       "YouTube videoPrimaryInfoRenderer.dateText"))
                    break
                except ValueError:
                    pass
    for value, source in candidates:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        video["published"] = parsed.astimezone(timezone.utc).isoformat() if parsed.tzinfo else value
        video["published_source"] = source
        video["published_precision"] = "day" if len(value) == 10 else "timestamp"
        return video["published"]
    status = re.search(r'"playabilityStatus"\s*:\s*\{\s*"status"\s*:\s*"([A-Z_]+)"', page)
    raise CollectionError("YouTube watch page omitted a verifiable publication date", stage="publication_date",
                          code="date_unavailable", details={"page_bytes": len(page),
                          "player_status": status[1] if status else None,
                          "primary_date_text": primary[:1],
                          "bot_check": "Sign in to confirm" in page})


def run(root, *, year, batch_size=3, seconds=160):
    archive = Archive(root)
    catalog_path, progress_path = archive.root / "catalog.json", archive.root / "backfill.json"
    catalog = json.loads(catalog_path.read_text()) if catalog_path.exists() else discover(root, year)
    if catalog.get("target_year") != year:
        raise RuntimeError("Catalog year mismatch; explicitly discover a new catalog")
    progress = json.loads(progress_path.read_text()) if progress_path.exists() else {"failures": {}}
    # Old entries lost their cause. Recheck each once under the new diagnostics;
    # normal backoff still applies to newly classified errors.
    for failure in progress.get("failures", {}).values():
        if "stage" not in failure:
            failure["retry_after"] = ""
    date_attempts = 0
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
                if not video.get("published"):
                    if date_attempts >= batch_size * 2 or time.monotonic() >= deadline:
                        break
                    if progress.get("date_lookup_retry_after", "") > now():
                        break
                    date_attempts += 1
                try:
                    published = publication_date(video)
                    if published[:4] < str(year):
                        progress.setdefault("year_boundaries", {})[tab] = video["id"]
                        break
                    if published[:4] > str(year):
                        continue
                    if progress.get("failures", {}).get(video["id"], {}).get("stage") == "publication_date":
                        progress["failures"].pop(video["id"])
                    heads.append(video)
                except Exception as error:
                    defer(progress, video, error, stage="publication_date")
                break
        write_json(catalog_path, catalog)
        if not heads:
            break
        video = max(heads, key=lambda item: item["published"])
        attempts += 1
        try:
            bundle = monitor.get_transcript(video["url"], with_metadata=True, hosted_only=True)
            if not bundle["text"].strip():
                errors = bundle.get("errors", [])
                raise CollectionError("Caption provider returned no transcript", stage="transcript",
                                      code=errors[0]["code"] if errors else "transcript_unavailable",
                                      details={"providers": errors})
            record = archive.create(video, bundle, None, None)
            record["backfill_complete"] = True
            record["historical_email_requested"] = False
            archive.save(record)
            completed += 1
            progress.setdefault("failures", {}).pop(video["id"], None)
            progress["last_success"] = {"video_id": video["id"], "at": now()}
            print(f"Archived {video['published']} {video['id']}: {len(bundle['text'])} characters")
        except Exception as error:
            defer(progress, video, error, stage="transcript")
        write_json(progress_path, progress)
        time.sleep(1)  # Modest sequential traffic to public extraction endpoints.
    progress["last_scan_at"] = now()
    progress["records"] = sum(1 for _ in archive.records())
    progress["completed_this_run"] = completed
    progress["status"] = "running_or_retrying" if heads else "at_year_boundaries_or_waiting_for_retries"
    write_json(progress_path, progress)
    print(f"Saved {completed} transcripts this run. Total records: {progress['records']}.")


def defer(progress, video, error, *, stage="transcript"):
    previous = progress.setdefault("failures", {}).get(video["id"], {})
    attempts = previous.get("attempts", 0) + 1
    providers = getattr(error, "details", {}).get("providers", [])
    provider = providers[0] if providers else {}
    status = error.code if isinstance(error, urllib.error.HTTPError) else provider.get("http_status")
    delay = min(360, 10 * 2 ** min(attempts - 1, 6))
    if status == 429:
        try:
            delay = max(delay, int(error.headers.get("Retry-After", "0") if isinstance(error, urllib.error.HTTPError)
                                   else provider.get("retry_after") or "0") / 60)
        except ValueError:
            pass
        delay = max(delay, 30)
    failure = {"error_type": type(error).__name__, "stage": getattr(error, "stage", stage),
               "code": getattr(error, "code", "request_failed"), "message": str(error)[:300],
               "details": getattr(error, "details", {}), "http_status": status,
               "attempts": attempts, "at": now(),
               "retry_after": (datetime.now(timezone.utc) + timedelta(minutes=delay)).isoformat()}
    progress["failures"][video["id"]] = failure
    if status == 429 and stage == "publication_date":
        progress["date_lookup_retry_after"] = failure["retry_after"]
    print(f"Deferred {video['id']} [{failure['stage']}/{failure['code']}]: {failure['message']} {failure['details']}", flush=True)


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
