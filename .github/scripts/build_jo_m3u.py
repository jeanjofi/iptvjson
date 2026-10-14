#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from cookie_feed import CookieUnavailable, cookie_from_value, load_premium_overrides, resolve_cookie


CHANNEL_FEED_URLS = (
      "https://raw.githubusercontent.com/jeanjofi/iptvjson/main/meta_mb.json",
    # "https://raw.githubusercontent.com/qwerty180506/json/refs/heads/main/Geoplus.json",
)
COOKIE_URL = "https://allinonereborn2.online/jstrweb2/cookies.json"
#COOKIE_URL = "https://raw.githubusercontent.com/qwerty180506/json/main/biscuit.json"
PLAYER_UA = "plaYtv/7.1.4 (Linux;Android 13) ygx/24.1 ExoPlayerLib/4.0"
JSON_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)
TIMEOUT = 30
MPD_TIMEOUT = 8
MPD_WORKERS = 12
OUT_M3U = Path("jo.m3u")

SLUG_TRIM_SUFFIX = re.compile(r"_(MOB|BTS|HD|FHD|UHD|MOB_HD|WV_DRM)$", re.I)
SLUG_FROM_URL = re.compile(
    r"/bpk-tv/([^/]+)/|/([^/]+)/index\.(?:mpd|m3u8)|/([^/]+)/master\.(?:mpd|m3u8)",
    re.I,
)


def fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": JSON_UA})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read().decode("utf-8", errors="replace")


def slug_from_url(url: str) -> Optional[str]:
    match = SLUG_FROM_URL.search(url)
    return match.group(1) or match.group(2) or match.group(3) if match else None


PAIR_RE = re.compile(r"\b([0-9a-fA-F]{32}):([0-9a-fA-F]{32})\b")
MPD_KID_RE = re.compile(
    r'(?:cenc:)?(?:default_)?[Kk][Ii][Dd]="(?:urn:uuid:)?([^"]+)"'
)


def parse_key_pairs(premium_keys: str) -> list[tuple[str, str]]:
    return [(kid.lower(), secret.lower()) for kid, secret in PAIR_RE.findall(premium_keys or "")]


def hex_kid(value: str) -> str:
    return re.sub(r"[^0-9a-f]", "", value.lower())


def signed_stream_url(url: str, cookie: str) -> str:
    token = cookie.split("__hdnea__=", 1)[1]
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}__hdnea__={token}"


def fetch_mpd_kids(url: str, cookie: str, user_agent: str, origin: str, referer: str) -> list[str]:
    headers = {"User-Agent": user_agent or JSON_UA, "Cookie": cookie}
    if origin:
        headers["Origin"] = origin
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(signed_stream_url(url, cookie), headers=headers)
    text = ""
    try:
        with urllib.request.urlopen(request, timeout=MPD_TIMEOUT) as response:
            text = response.read().decode("utf-8", errors="replace")
    except Exception as error:
        reason = str(getattr(error, "reason", error))
        if "CERTIFICATE_VERIFY_FAILED" in reason or "certificate verify failed" in reason:
            try:
                insecure = ssl._create_unverified_context()
                with urllib.request.urlopen(request, timeout=MPD_TIMEOUT, context=insecure) as response:
                    text = response.read().decode("utf-8", errors="replace")
            except Exception:
                return []
        else:
            return []
    kids: list[str] = []
    seen: set[str] = set()
    for match in MPD_KID_RE.finditer(text):
        kid = hex_kid(match.group(1))
        if len(kid) == 32 and kid not in seen:
            seen.add(kid)
            kids.append(kid)
    return kids


def pick_clearkey(
    premium_keys: str,
    key_id: str,
    key: str,
    mpd_kids: Optional[list[str]] = None,
) -> str:
    """One kid:key for this player. Prefer the KID the MPD actually asks for."""
    pairs = parse_key_pairs(premium_keys)
    by_kid = {kid: secret for kid, secret in pairs}
    for kid in mpd_kids or []:
        if kid in by_kid:
            return f"{kid}:{by_kid[kid]}"
    if key_id in by_kid:
        return f"{key_id}:{by_kid[key_id]}"
    if pairs:
        return f"{pairs[0][0]}:{pairs[0][1]}"
    if re.fullmatch(r"[0-9a-f]{32}", key_id) and re.fullmatch(r"[0-9a-f]{32}", key):
        return f"{key_id}:{key}"
    return ""


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
    parser = argparse.ArgumentParser(description="Build the JO M3U playlist.")
    parser.add_argument("--limit", type=int, default=0, help="Process only the first N channels.")
    args = parser.parse_args()

    print("1) Fetch JO channel feeds")
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
    premium = None
    if any(not cookie_from_value(entry.get("cookie")) for entry in channels):
        print("2) Fetch fallback __hdnea__ cookie")
        try:
            resolved = resolve_cookie()
            fallback_cookie = resolved.cookie
            print(
                f"   -> {resolved.source} last_updated={resolved.last_updated} "
                f"exp={resolved.expires_at}"
            )
        except CookieUnavailable:
            print("   no global cookie; using premiumplugx per-channel cookies")
            premium = load_premium_overrides()
            fallback_cookie = premium.fallback_cookie()
    else:
        print("2) Use cookies from channel feed")

    print("3) Resolve streams and ClearKeys")
    pending: list[dict] = []
    seen: set[tuple[str, str]] = set()
    skipped = 0
    premium_applied = 0
    last_resort = 0
    for entry in channels:
        channel_id = str(entry.get("id") or "").strip()
        stream_url = str(entry.get("url") or entry.get("mpd") or "").strip()
        logo = str(entry.get("logo") or "").strip()
        cookie = cookie_from_value(entry.get("cookie"))
        match = None
        if premium:
            match = premium.lookup(stream_url, str(entry.get("name") or ""))
            if match:
                stream_url = match.url
                cookie = match.cookie
                premium_applied += 1
            elif not cookie and fallback_cookie:
                cookie = fallback_cookie
                last_resort += 1
        elif fallback_cookie:
            cookie = cookie or fallback_cookie
        key_id = str(entry.get("keyId") or "").strip().lower()
        key = str(entry.get("key") or "").strip().lower()
        if not stream_url or not cookie or not channel_id:
            skipped += 1
            continue
        identity = (channel_id, stream_url)
        if identity in seen:
            continue
        seen.add(identity)
        pending.append({
            "channel_id": channel_id,
            "stream_url": stream_url,
            "logo": logo,
            "cookie": cookie,
            "match": match,
            "key_id": key_id,
            "key": key,
            "category": str(entry.get("category") or "JIO Live").strip() or "JIO Live",
        })

    mpd_kids: dict[str, list[str]] = {}
    to_probe = [
        row for row in pending
        if row["match"] and len(parse_key_pairs(row["match"].license_key)) > 1
    ]
    if to_probe:
        print(f"   probing {len(to_probe)} MPDs for the matching ClearKey kid")

        def probe(row: dict) -> tuple[str, list[str]]:
            match = row["match"]
            kids = fetch_mpd_kids(
                row["stream_url"],
                row["cookie"],
                match.user_agent,
                match.origin,
                match.referer,
            )
            return row["stream_url"], kids

        with ThreadPoolExecutor(max_workers=MPD_WORKERS) as pool:
            futures = [pool.submit(probe, row) for row in to_probe]
            found = 0
            for future in as_completed(futures):
                url, kids = future.result()
                mpd_kids[url] = kids
                if kids:
                    found += 1
        print(f"   MPD kids found for {found}/{len(to_probe)} channels")

    print("4) Write M3U")
    lines = ["#EXTM3U"]
    written = 0
    for row in pending:
        match = row["match"]
        license_key = pick_clearkey(
            match.license_key if match else "",
            row["key_id"],
            row["key"],
            mpd_kids.get(row["stream_url"]),
        )
        if not license_key:
            skipped += 1
            continue
        separator = "&" if "?" in row["stream_url"] else "?"
        token = row["cookie"].split("__hdnea__=", 1)[1]
        stream_url = f"{row['stream_url']}{separator}__hdnea__={token}"
        user_agent = (match.user_agent if match and match.user_agent else PLAYER_UA)
        http_headers = {"cookie": row["cookie"]}
        if match:
            if match.origin:
                http_headers["Origin"] = match.origin
            if match.referer:
                http_headers["Referer"] = match.referer
        tag_lines = [
            "#KODIPROP:inputstream.adaptive.license_type=clearkey",
            f"#KODIPROP:inputstream.adaptive.license_key={license_key}",
            f"#EXTVLCOPT:http-user-agent={user_agent}",
        ]
        if match and match.referer:
            tag_lines.append(f"#EXTVLCOPT:http-referrer={match.referer}")
        tag_lines.extend([
            f"#EXTHTTP:{json.dumps(http_headers, separators=(',', ':'))}",
            f'#EXTINF:-1 tvg-id="{row["channel_id"]}" tvg-logo="{row["logo"]}" group-title="{row["category"]}",{channel_name(stream_url, row["channel_id"])}',
            stream_url,
            "",
        ])
        lines.extend(tag_lines)
        written += 1

    OUT_M3U.write_text("\n".join(lines) + "\n", encoding="utf-8")
    extra = ""
    if premium_applied or last_resort:
        extra = f", {premium_applied} premiumplugx matches, {last_resort} last-resort cookies"
    print(f"   wrote {OUT_M3U} ({written} channels, {skipped} skipped{extra})")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as error:
        print(f"FATAL: {error}", file=sys.stderr)
        sys.exit(2)
