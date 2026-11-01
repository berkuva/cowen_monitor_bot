#!/usr/bin/env python3
"""Monitor Benjamin Cowen's YouTube RSS feed and notify about new uploads."""

from __future__ import annotations

import email.message
import json
import os
import re
import smtplib
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET


CHANNEL_ID = os.getenv("YOUTUBE_CHANNEL_ID", "UCRvqjQPSeaWn-uEx-w0XOIg")
RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
STATE_PATH = Path(os.getenv("STATE_PATH", "state.json"))
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
MAX_TRANSCRIPT_CHARS = 45_000


def http_get(url: str, timeout: int = 30) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "benjamin-cowen-monitor/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch_feed() -> list[dict[str, str]]:
    root = ET.fromstring(http_get(RSS_URL))
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
        "media": "http://search.yahoo.com/mrss/",
    }
    videos: list[dict[str, str]] = []
    for entry in root.findall("atom:entry", ns):
        video_id = (entry.findtext("yt:videoId", default="", namespaces=ns) or "").strip()
        title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
        published = (entry.findtext("atom:published", default="", namespaces=ns) or "").strip()
        updated = (entry.findtext("atom:updated", default="", namespaces=ns) or "").strip()
        link = f"https://www.youtube.com/watch?v={video_id}"
        description = ""
        group = entry.find("media:group", ns)
        if group is not None:
            description = (group.findtext("media:description", default="", namespaces=ns) or "").strip()
        if video_id:
            videos.append(
                {
                    "id": video_id,
                    "title": title,
                    "published": published,
                    "updated": updated,
                    "url": link,
                    "description": description,
                }
            )
    return videos


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"seen_ids": []}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"seen_ids": []}
    except (OSError, json.JSONDecodeError):
        return {"seen_ids": []}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def clean_vtt(text: str) -> str:
    lines: list[str] = []
    previous = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("WEBVTT") or line.startswith("NOTE"):
            continue
        if "-->" in line or re.fullmatch(r"\d+", line):
            continue
        line = re.sub(r"<[^>]+>", "", line)
        line = re.sub(r"&amp;", "&", line)
        line = re.sub(r"&quot;", '"', line)
        line = re.sub(r"\s+", " ", line).strip()
        if line and line != previous:
            lines.append(line)
            previous = line
    return " ".join(lines)[:MAX_TRANSCRIPT_CHARS]


def get_transcript(video_url: str) -> str:
    """Download English captions, with hosted and direct fallbacks."""
    video_id = urllib.parse.parse_qs(urllib.parse.urlparse(video_url).query).get("v", [""])[0]
    with tempfile.TemporaryDirectory() as directory:
        output_template = str(Path(directory) / "video.%(ext)s")
        command = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--skip-download",
            "--write-auto-subs",
            "--sub-langs",
            "en.*,en",
            "--sub-format",
            "vtt",
            "--no-warnings",
            "--output",
            output_template,
            video_url,
        ]
        try:
            subprocess.run(command, check=False, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            pass
        candidates = sorted(Path(directory).glob("*.vtt"))
        if candidates:
            try:
                transcript = clean_vtt(
                    candidates[0].read_text(encoding="utf-8", errors="ignore")
                )
                if transcript:
                    return transcript
            except OSError:
                pass

    # GitHub Actions cloud IPs are commonly blocked by YouTube. Use a free
    # hosted transcript endpoint before trying direct YouTube access again.
    if video_id:
        hosted_url = "https://api.freetranscriptapi.com/v1/transcript?" + urllib.parse.urlencode(
            {"video_url": video_id, "lang": "en"}
        )
        try:
            data = json.loads(http_get(hosted_url, timeout=30).decode("utf-8"))
            snippets = data.get("transcript", [])
            transcript = " ".join(
                str(snippet.get("text", "")).strip()
                for snippet in snippets
                if isinstance(snippet, dict) and str(snippet.get("text", "")).strip()
            )
            if transcript:
                return transcript[:MAX_TRANSCRIPT_CHARS]
        except Exception:
            pass

    # yt-dlp can fail when YouTube rejects a player client even though the
    # transcript endpoint still exposes auto-generated captions.
    if video_id:
        try:
            from youtube_transcript_api import YouTubeTranscriptApi

            snippets = YouTubeTranscriptApi().fetch(video_id, languages=["en"])
            transcript = " ".join(
                str(snippet.text).strip()
                for snippet in snippets
                if str(snippet.text).strip()
            )
            return transcript[:MAX_TRANSCRIPT_CHARS]
        except Exception:
            pass
    return ""


def summarize(video: dict[str, str], transcript: str) -> str:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured")

    # The RSS description is usually a block of links, disclaimers, and scam
    # warnings. It is metadata, not the spoken content of the video. Never
    # present a description-only result as analysis of Cowen's video.
    if not transcript.strip():
        return (
            "THESIS:\n"
            "No substantive video summary is available because YouTube captions "
            "could not be retrieved.\n\n"
            "KEY POINTS:\n"
            "- No spoken-content key points were verified.\n"
            "- The available RSS text was excluded because it contains promotional "
            "links, disclaimers, and social/scam notices rather than the video transcript.\n\n"
            "PRICE / TIMING CLAIMS:\n"
            "No claims could be verified without the transcript.\n\n"
            "BIAS:\n"
            "Not determinable from the available source.\n\n"
            "RELEVANCE TO OUR HALVING-CYCLE ANALYSIS:\n"
            "Do not update the cycle analysis based on this notification."
        )

    source = transcript
    prompt = f"""
You are summarizing a newly published Benjamin Cowen Bitcoin/crypto video.
Use only the transcript below as evidence. Do not use the title to invent content.
Do not invent claims, quotes, prices, dates, or chart readings. Separate what Cowen says from your own inference.
Do not treat promotional links, disclaimers, social links, or scam warnings as
the video's analytical key points. If the transcript does not contain a claim,
write "Not stated in the transcript."
Return plain text with these headings:
THESIS:
KEY POINTS:
PRICE / TIMING CLAIMS:
BIAS:
RELEVANCE TO OUR HALVING-CYCLE ANALYSIS:

Video title: {video['title']}
Published: {video['published']}
URL: {video['url']}

Transcript (spoken content):
{source}
""".strip()
    endpoint = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{urllib.parse.quote(MODEL, safe='')}:generateContent?key={urllib.parse.quote(api_key, safe='')}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1200},
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Gemini API error {error.code}: {detail}") from error
    except (urllib.error.URLError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Gemini request failed: {error}") from error
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    text = "\n".join(part.get("text", "") for part in parts if part.get("text"))
    if not text.strip():
        raise RuntimeError("Gemini returned no summary text")
    return text.strip()


def notification_text(video: dict[str, str], summary: str) -> str:
    text = f"Benjamin Cowen — new video\n\n{video['title']}\nPublished: {video['published']}\n{video['url']}\n\n{summary}"
    return text[:3900]


def send_telegram(text: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    request = urllib.request.Request(endpoint, data=payload, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as http_response:
            response = json.loads(http_response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Telegram API error {error.code}: {detail}") from error
    if not response.get("ok"):
        raise RuntimeError(f"Telegram API rejected the message: {response}")
    return True


def send_email(text: str, video: dict[str, str]) -> bool:
    host = os.getenv("SMTP_HOST", "").strip()
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    recipient = os.getenv("EMAIL_TO", "").strip()
    sender = os.getenv("EMAIL_FROM", username).strip()
    values = [host, username, password, recipient, sender]
    if not any(values):
        return False
    if not all(values):
        raise RuntimeError("SMTP/email configuration is incomplete")
    message = email.message.EmailMessage()
    message["Subject"] = f"Benjamin Cowen: {video['title']}"
    message["From"] = sender
    message["To"] = recipient
    message.set_content(text)
    port = int(os.getenv("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(username, password)
        refused = smtp.send_message(message)
    if refused:
        raise RuntimeError(f"SMTP refused recipients: {refused}")
    return True


def ensure_notification_destination() -> None:
    telegram_configured = bool(
        os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        and os.getenv("TELEGRAM_CHAT_ID", "").strip()
    )
    email_configured = all(
        os.getenv(name, "").strip()
        for name in (
            "SMTP_HOST",
            "SMTP_USERNAME",
            "SMTP_PASSWORD",
            "EMAIL_TO",
        )
    )
    if not telegram_configured and not email_configured:
        raise RuntimeError(
            "Configure Telegram secrets or SMTP/email secrets before enabling the workflow"
        )


def main() -> int:
    videos = fetch_feed()
    if not videos:
        raise RuntimeError("YouTube RSS feed returned no videos")

    ensure_notification_destination()

    state = load_state()
    seen_ids = list(dict.fromkeys(str(item) for item in state.get("seen_ids", [])))
    seen_set = set(seen_ids)

    if not seen_ids and os.getenv("SEND_LATEST_ON_FIRST_RUN", "false").lower() != "true":
        state["seen_ids"] = [videos[0]["id"]]
        save_state(state)
        print(f"Initialized state at latest video: {videos[0]['id']}")
        return 0

    new_videos = [video for video in reversed(videos) if video["id"] not in seen_set]
    if not new_videos:
        print("No new Benjamin Cowen videos.")
        return 0

    for video in new_videos:
        transcript = get_transcript(video["url"])
        summary = summarize(video, transcript)
        text = notification_text(video, summary)
        delivered = send_telegram(text)
        delivered = send_email(text, video) or delivered
        if not delivered:
            raise RuntimeError(
                f"No notification was delivered for {video['id']}; leaving it unmarked for retry"
            )
        print(f"Sent summary for {video['id']}: {video['title']}")
        seen_ids.append(video["id"])
        # Persist after every successful delivery so a later video failing in
        # the same batch does not cause already-delivered videos to repeat.
        state["seen_ids"] = seen_ids[-50:]
        save_state(state)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # keep the workflow log useful
        print(f"ERROR: {error}", file=sys.stderr)
        raise
