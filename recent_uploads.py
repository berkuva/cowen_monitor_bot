"""Merge every observed upload into the durable archive inventory."""
from pathlib import Path
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import re
import subprocess
import sys

from archive_store import now, write_json


def merge_recent(root, videos):
    path = Path(root) / "catalog.json"
    if not path.exists():
        return
    catalog = json.loads(path.read_text())
    groups = catalog.setdefault("groups", {})
    known = {v["id"]: v for group in groups.values() for v in group}
    for video in videos:
        if video["id"] in known:
            original = known[video["id"]]
            if not original.get("published") or len(original.get("published", "")) == 10:
                for key in ("published", "published_source", "channel_id"):
                    if video.get(key):
                        original[key] = video[key]
        else:
            value = {**video, "tab": "recent", "discovered_via": "live_rss", "first_seen_at": now()}
            groups.setdefault("recent", []).append(value)
            known[video["id"]] = value
    if "recent" in groups:
        groups["recent"].sort(key=lambda v: v.get("published") or "", reverse=True)
    catalog["last_feed_merge_at"] = now()
    write_json(path, catalog)


def coverage(root, since="2026-11-01"):
    """Report each known post-activation upload, including intentional exclusions."""
    from archive_store import Archive
    import speaker_policy
    root = Path(root)
    catalog = json.loads((root / "catalog.json").read_text())
    policy = speaker_policy.load_policy(root)
    archive = Archive(root)
    progress_path = root / "backfill.json"
    progress = json.loads(progress_path.read_text()) if progress_path.exists() else {}
    known = {v["id"]: v for group in catalog["groups"].values() for v in group}
    rows, counts = [], {}
    for video_id, video in known.items():
        published = video.get("published")
        if not published or published[:10] < since:
            continue
        status = "missing_transcript"
        if archive.get(video_id):
            status = "saved"
        elif video_id in policy.get("removed", {}):
            status = "excluded_by_speaker_policy"
        elif (root / "speaker-review/records" / (video_id + ".json")).exists():
            status = "speaker_review_pending"
        elif video_id in progress.get("access_restricted", {}):
            status = "access_restricted"
        elif video_id in progress.get("caption_unavailable", {}):
            status = "caption_unavailable"
        counts[status] = counts.get(status, 0) + 1
        rows.append({"id": video_id, "title": video.get("title"), "published": published, "status": status})
    report = {"checked_at": now(), "since_inclusive": since, "counts": counts,
              "videos": sorted(rows, key=lambda v: v["published"], reverse=True),
              "inventory_reconciliation": catalog.get("inventory_reconciliation"),
              "scope": "Known public uploads; no claim of complete enumeration if reconciliation failed."}
    write_json(root / "recent-upload-audit.json", report)
    return report


def reconcile(root, *, since="2026-11-01", force=False, refresh_seconds=21600, head_limit=100):
    """Recheck all three channel tabs so RSS omissions cannot lose an upload ID."""
    import backfill
    import monitor
    root = Path(root)
    path = root / "catalog.json"
    catalog = json.loads(path.read_text())
    previous = catalog.get("inventory_reconciliation", {})
    head_limit = max(head_limit, min(4000, int(previous.get("next_head_limit", head_limit))))
    if not force and previous.get("success") and previous.get("checked_at"):
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(previous["checked_at"])).total_seconds()
        if age < refresh_seconds:
            return coverage(root, since)
    def inventory(tab):
        try:
            response = subprocess.run([sys.executable, "-m", "yt_dlp", "--flat-playlist", "--playlist-end", str(head_limit),
                "--dump-single-json", "--no-warnings", "--socket-timeout", "15", "--retries", "1",
                "https://www.youtube.com/channel/" + monitor.CHANNEL_ID + "/" + tab],
                capture_output=True, text=True, timeout=85, check=True)
            entries = json.loads(response.stdout).get("entries", [])
            return tab, entries, None
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            return tab, [], type(error).__name__
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(inventory, ("videos", "streams", "shorts")))
    known = {v["id"]: v for group in catalog["groups"].values() for v in group}
    videos, errors, unresolved, tab_counts, anchors = [], {}, [], {}, {}
    for tab, entries, error in results:
        tab_counts[tab] = len(entries)
        if error or not entries:
            errors[tab] = error or "empty_inventory"
            continue
        previous_anchors = set(previous.get("anchor_ids", {}).get(tab, [])) or set(known)
        ids = [e.get("id") for e in entries]
        if len(entries) >= head_limit and not previous_anchors.intersection(ids):
            errors[tab] = "inventory_gap_requires_larger_head_window"
        anchors[tab] = [vid for vid in ids if isinstance(vid, str)]
        for entry in entries:
            video_id = entry.get("id") or ""
            if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id) or video_id in known:
                continue
            video = {"id": video_id, "title": entry.get("title") or video_id,
                     "url": "https://www.youtube.com/watch?v=" + video_id,
                     "channel_id": monitor.CHANNEL_ID, "published": None,
                     "discovered_via": "channel_tab_reconciliation", "source_tab": tab}
            # IDs are retained even if a source date cannot be verified yet.
            try:
                backfill.publication_date(video)
            except Exception as failure:
                unresolved.append({"video_id": video_id, "error_type": type(failure).__name__})
            known[video_id] = video
            videos.append(video)
    merge_recent(root, videos)
    catalog = json.loads(path.read_text())
    catalog["inventory_reconciliation"] = {"checked_at": now(), "success": not errors,
        "tab_counts": tab_counts, "errors": errors, "head_limit_per_tab": head_limit,
        "new_video_ids": [v["id"] for v in videos], "unverified_publication_dates": unresolved,
        "anchor_ids": anchors if not errors else previous.get("anchor_ids", {}),
        "next_head_limit": min(4000, head_limit * 2) if errors else 100}
    write_json(path, catalog)
    return coverage(root, since)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("reconcile", "coverage"))
    parser.add_argument("--archive", required=True)
    parser.add_argument("--since", default="2026-11-01")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    result = reconcile(args.archive, since=args.since, force=args.force) if args.command == "reconcile" else coverage(args.archive, args.since)
    print(json.dumps({k: v for k, v in result.items() if k != "videos"}, indent=2))
