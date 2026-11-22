"""Reconcile actual source files with retry bookkeeping; never invent success."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from archive_store import Archive, now, release_metadata, write_json
import speaker_policy


def reconcile(root, progress=None):
    root = Path(root)
    path = root / "backfill.json"
    save = progress is None
    if progress is None:
        progress = json.loads(path.read_text()) if path.exists() else {}
    archive = Archive(root)
    policy = speaker_policy.load_policy(root)
    failures = progress.setdefault("failures", {})
    unavailable = progress.setdefault("caption_unavailable", {})
    restricted = progress.setdefault("access_restricted", {})
    resolved = progress.setdefault("resolved_failures", {})
    cleared = 0
    for queue in (failures, unavailable, restricted):
        for video_id, failure in list(queue.items()):
            resolution = None
            record = archive.get(video_id)
            if record and record.get("transcript", {}).get("text", "").strip():
                release_metadata(record["video"])
                resolution = "saved_transcript"
            elif video_id in policy.get("removed", {}):
                resolution = "excluded_by_speaker_policy"
            else:
                held = root / "speaker-review/records" / (video_id + ".json")
                if held.exists() and json.loads(held.read_text()).get("transcript", {}).get("text", "").strip():
                    resolution = "transcript_acquired_speaker_review_pending"
            if resolution:
                resolved[video_id] = {"resolution": resolution, "resolved_at": now(), "previous_failure": failure}
                queue.pop(video_id)
                cleared += 1
            elif queue is failures and failure.get("code") == "no_captions":
                unavailable[video_id] = {**failure,
                    "availability_basis": "caption_provider_report; not a completed transcript",
                    "retry_after": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}
                queue.pop(video_id)
    held_count = len(list((root / "speaker-review/records").glob("*.json")))
    report = {"checked_at": now(), "retrieval_failures": len(failures),
              "caption_unavailable": len(unavailable), "speaker_review_pending": held_count,
              "access_restricted": len(restricted),
              "resolved_failure_records": len(resolved), "resolved_this_check": cleared,
              "note": "Unavailable captions and held speakers are incomplete, not successful sources."}
    progress["collection_health"] = report
    if save:
        write_json(path, progress)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True)
    args = parser.parse_args()
    print(json.dumps(reconcile(args.archive), indent=2))
