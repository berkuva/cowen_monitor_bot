"""Audit release dates and email each hundred successfully stored transcripts."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from archive_store import Archive, now, release_metadata, write_json
import monitor

INTERVAL = 100
ARCHIVE_URL = "https://github.com/berkuva/cowen_video_archive"


def audit(root, *, repair=False):
    archive = Archive(root)
    videos, invalid, updated = {}, [], 0
    for path in sorted((archive.root / "records").glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            video = release_metadata(record["video"])
            text = record["transcript"]["text"]
            if archive.path(video["id"]) != path or not isinstance(text, str) or not text.strip():
                raise ValueError("Invalid video ID or empty transcript")
            if repair and record["video"] != video:
                record["video"] = video
                archive.save(record)
                updated += 1
            videos[video["id"]] = video
        except (ValueError, KeyError, TypeError) as error:
            invalid.append({"record": path.name, "error_type": type(error).__name__})
    dates = sorted(video["release_date"] for video in videos.values())
    return {"checked_at": now(), "successful_transcripts": len(videos), "invalid_records": invalid,
            "records_updated": updated, "earliest_release_date": dates[0] if dates else None,
            "latest_release_date": dates[-1] if dates else None}


def notification_text(milestone, report):
    total = report["successful_transcripts"]
    return (f"Benjamin Cowen transcript archive — {milestone:,}-transcript milestone\n\n"
            f"Successfully saved transcripts: {total:,}\n"
            f"Milestone reached: {milestone:,}\n"
            f"Release-date coverage: {report['earliest_release_date']} to {report['latest_release_date']}\n"
            f"Records requiring attention: {len(report['invalid_records'])}\n\n"
            "Each counted video has a nonempty saved transcript and a verified release date. "
            "Repeat attempts and updates to the same video do not add to the count.\n\n"
            f"Full transcripts: {ARCHIVE_URL}/tree/main/records\n"
            f"Collection progress: {ARCHIVE_URL}/blob/main/backfill.json\n"
            f"Milestone delivery history: {ARCHIVE_URL}/blob/main/milestones.json\n\n"
            f"Next milestone after this one: {milestone + INTERVAL:,} transcripts.\n"
            "Historical collection continues without individual summary emails. "
            "Future-video summary emails continue as before.\n")


def notify(root):
    root = Path(root)
    report = audit(root)
    state_path = root / "milestones.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {"schema_version": 1, "delivered": {}}
    state["interval"] = INTERVAL
    state["last_checked_at"] = now()
    state["successful_transcripts"] = report["successful_transcripts"]
    state["invalid_records"] = report["invalid_records"]
    delivered = state.setdefault("delivered", {})
    state.pop("last_error", None)
    success = True
    # The serialized cloud workflow writes this checkpoint after each run.
    # A stable Message-ID also identifies retries of the same milestone.
    for milestone in range(INTERVAL, report["successful_transcripts"] + 1, INTERVAL):
        key = str(milestone)
        if key in delivered:
            continue
        message_id = f"<cowen-transcripts-{milestone}@cowen-monitor-bot.local>"
        try:
            sent = monitor.send_mail(notification_text(milestone, report),
                                     f"Benjamin Cowen archive: {milestone:,} transcripts saved",
                                     destination=root / "milestone-emails" / (key + ".eml"), message_id=message_id)
            if not sent:
                raise RuntimeError("Milestone email destination is not configured")
        except Exception as error:
            state["last_error"] = {"milestone": milestone, "at": now(), "error_type": type(error).__name__}
            print(f"Milestone {milestone} email pending retry: {type(error).__name__}", flush=True)
            success = False
            break
        delivered[key] = {"sent_at": now(), "message_id": message_id,
                          "successful_transcripts": report["successful_transcripts"],
                          "earliest_release_date": report["earliest_release_date"],
                          "latest_release_date": report["latest_release_date"]}
        write_json(state_path, state)
        print(f"Sent transcript milestone email: {milestone}", flush=True)
    next_milestone = INTERVAL
    while str(next_milestone) in delivered:
        next_milestone += INTERVAL
    state["next_milestone"] = next_milestone
    write_json(state_path, state)
    print(f"Successful transcripts: {report['successful_transcripts']}; next unsent milestone: {next_milestone}")
    return success


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("audit", "notify"))
    parser.add_argument("--archive", default=os.getenv("ARCHIVE_PATH", "private-archive"))
    args = parser.parse_args()
    if args.command == "audit":
        report = audit(args.archive, repair=True)
        write_json(Path(args.archive) / "release-date-audit.json", report)
        print(json.dumps(report))
        raise SystemExit(1 if report["invalid_records"] else 0)
    raise SystemExit(0 if notify(args.archive) else 1)
