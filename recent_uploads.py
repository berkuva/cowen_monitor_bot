"""Merge every observed upload into the durable archive inventory."""
from pathlib import Path
import json

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
