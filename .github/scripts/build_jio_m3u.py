#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional


CHANNEL_FEED_URLS = (
    #   "https://raw.githubusercontent.com/live4wap/links/refs/heads/main/jiomb",
    # "https://raw.githubusercontent.com/live4wap/links/refs/heads/main/jiopl",
    "https://raw.githubusercontent.com/qwerty180506/json/refs/heads/main/Geoplus.json",
)
COOKIE_URL = "https://allinonereborn2.online/jstrweb2/cookies.json"
PLAYER_UA = "plaYtv/7.1.4 (Linux;Android 13) ygx/24.1 ExoPlayerLib/4.0"
JSON_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)
TIMEOUT = 30
OUT_M3U = Path("jio.m3u")

SLUG_TRIM_SUFFIX = re.compile(r"_(MOB|BTS|HD|FHD|UHD|MOB_HD|WV_DRM)$", re.I)
SLUG_FROM_URL = re.compile(
    r"/bpk-tv/([^/]+)/|/([^/]+)/index\.(?:mpd|m3u8)|/([^/]+)/master\.(?:mpd|m3u8)",
    re.I,
)


def fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": JSON_UA})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read().decode("utf-8", errors="replace")


def cookie_from_value(value: object) -> str:
    cookie = str(value or "").strip()
    return cookie if cookie.startswith("__hdnea__=") else ""


def slug_from_url(url: str) -> Optional[str]:
    match = SLUG_FROM_URL.search(url)
    return match.group(1) or match.group(2) or match.group(3) if match else None


def channel_name(url: str, channel_id: str) -> str:
    slug = slug_from_url(url) or f"Channel {channel_id}"
    name = SLUG_TRIM_SUFFIX.sub("", slug).replace("_", " ")
    name = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    name = re.sub(r"(\d)([A-Z])", r"\1 \2", name)
    name = re.sub(r"\s+", " ", name).strip() or f"Channel {channel_id}"
    return " ".join(
        token.upper() if token.lower() in {"hd", "uhd", "fhd", "tv", "mob"} else token
        for token in name.split()
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the JIO M3U playlist.")
    parser.add_argument("--limit", type=int, default=0, help="Process only the first N channels.")
    args = parser.parse_args()

    print("1) Fetch JIO channel feeds")
    channels: list[dict] = []
    for feed_url in CHANNEL_FEED_URLS:
        feed = json.loads(fetch_text(feed_url))
        if not isinstance(feed, list):
            raise ValueError(f"channel feed is not a JSON array: {feed_url}")
        channels.extend(entry for entry in feed if isinstance(entry, dict))
        print(f"   -> {len(feed)} entries from {feed_url}")
    if args.limit > 0:
        channels = channels[:args.limit]

    fallback_cookie = ""
    if any(not cookie_from_value(entry.get("cookie")) for entry in channels):
        print("2) Fetch fallback __hdnea__ cookie")
        cookie_data = json.loads(fetch_text(COOKIE_URL))
        fallback_cookie = next(
            (cookie for item in cookie_data if isinstance(item, dict)
             for cookie in (cookie_from_value(item.get("cookie")),) if cookie),
            "",
        )
        if not fallback_cookie:
            raise ValueError("cookie feed did not contain an __hdnea__ cookie")
    else:
        print("2) Use cookies from channel feed")

    print("3) Write M3U")
    lines = ["#EXTM3U"]
    seen: set[tuple[str, str]] = set()
    written = 0
    skipped = 0
    for entry in channels:
        channel_id = str(entry.get("id") or "").strip()
        stream_url = str(entry.get("url") or entry.get("mpd") or "").strip()
        logo = str(entry.get("logo") or "").strip()
        cookie = cookie_from_value(entry.get("cookie")) or fallback_cookie
        key_id = str(entry.get("keyId") or "").strip().lower()
        key = str(entry.get("key") or "").strip().lower()
        if not stream_url or not cookie:
            skipped += 1
            continue
        if not channel_id or not re.fullmatch(r"[0-9a-f]{32}", key_id) or not re.fullmatch(r"[0-9a-f]{32}", key):
            skipped += 1
            continue
        identity = (channel_id, stream_url)
        if identity in seen:
            continue
        seen.add(identity)
        separator = "&" if "?" in stream_url else "?"
        token = cookie.split("__hdnea__=", 1)[1]
        stream_url = f"{stream_url}{separator}__hdnea__={token}"
        category = str(entry.get("category") or "JIO Live").strip() or "JIO Live"
        lines.extend([
            "#KODIPROP:inputstream.adaptive.license_type=clearkey",
            f"#KODIPROP:inputstream.adaptive.license_key={key_id}:{key}",
            f"#EXTVLCOPT:http-user-agent={PLAYER_UA}",
            f"#EXTHTTP:{json.dumps({'cookie': cookie}, separators=(',', ':'))}",
            f'#EXTINF:-1 tvg-id="{channel_id}" tvg-logo="{logo}" group-title="{category}",{channel_name(stream_url, channel_id)}',
            stream_url,
            "",
        ])
        written += 1

    OUT_M3U.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"   wrote {OUT_M3U} ({written} channels, {skipped} skipped)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as error:
        print(f"FATAL: {error}", file=sys.stderr)
        sys.exit(2)
