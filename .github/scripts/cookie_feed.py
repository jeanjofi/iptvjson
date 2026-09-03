#!/usr/bin/env python3
"""Resolve a live, globally usable __hdnea__ cookie from ordered feeds."""
from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import NamedTuple, Optional

IST = timezone(timedelta(hours=5, minutes=30))

PRIMARY_URL = "https://allinonereborn2.online/jstrweb2/cookies.json"
SONU_URL = "https://sonujson-v3.pages.dev/Data/sports.json"
BINGE_URL = "https://binge-giotv.pages.dev/data/id.json"
JSON_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)
TIMEOUT = 30
SKEW_SECONDS = 60
EXP_RE = re.compile(r"(?:^|~)exp=(\d+)(?:~|$)")
ACL_RE = re.compile(r"(?:^|~)acl=([^~]+)(?:~|$)")
GLOBAL_ACLS = frozenset({"/*", "*", "/bpk-tv/*"})
CHANNEL_FEEDS = (
    ("sonujson", SONU_URL),
    ("binge-giotv", BINGE_URL),
)


class CookieUnavailable(ValueError):
    """Feeds responded, but no live cookie with a global ACL was found."""


class ResolvedCookie(NamedTuple):
    cookie: str
    source: str
    last_updated: str
    expires_at: int


def fetch_json(url: str) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": JSON_UA})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.URLError as error:
        reason = str(getattr(error, "reason", error))
        if "CERTIFICATE_VERIFY_FAILED" not in reason and "certificate verify failed" not in reason:
            raise
        print(f"   SSL verify failed for {url}; retrying without verify")
        insecure = ssl._create_unverified_context()
        with urllib.request.urlopen(request, timeout=TIMEOUT, context=insecure) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))


def cookie_from_value(value: object) -> str:
    cookie = str(value or "").strip()
    return cookie if cookie.startswith("__hdnea__=") else ""


def cookie_expiry(cookie: str, explicit: object = None) -> Optional[int]:
    if explicit is not None and str(explicit).strip():
        try:
            return int(explicit)
        except (TypeError, ValueError):
            pass
    match = EXP_RE.search(cookie)
    return int(match.group(1)) if match else None


def cookie_acl(cookie: str) -> str:
    match = ACL_RE.search(cookie)
    return match.group(1) if match else ""


def is_global_acl(cookie: str) -> bool:
    return cookie_acl(cookie) in GLOBAL_ACLS


def is_live(cookie: str, explicit: object = None) -> bool:
    expires_at = cookie_expiry(cookie, explicit)
    return expires_at is not None and expires_at > time.time() + SKEW_SECONDS


def _usable(cookie: str, explicit: object = None) -> Optional[int]:
    """Return expiry if the cookie is live and not path-locked; else None."""
    if not cookie or not is_live(cookie, explicit) or not is_global_acl(cookie):
        return None
    return cookie_expiry(cookie, explicit)


def _fmt_ist(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, IST).strftime("%d %b %Y, %I:%M:%S %p IST")


def _from_primary(data: object) -> Optional[ResolvedCookie]:
    if not isinstance(data, list):
        return None
    last_updated = next(
        (str(item.get("last_updated", "")).strip() for item in data
         if isinstance(item, dict) and "last_updated" in item),
        "",
    )
    for item in data:
        if not isinstance(item, dict):
            continue
        cookie = cookie_from_value(item.get("cookie"))
        expires_at = _usable(cookie)
        if cookie and expires_at is not None:
            return ResolvedCookie(cookie, "allinonereborn", last_updated, expires_at)
        if cookie and is_live(cookie) and not is_global_acl(cookie):
            print(f"   skip primary path ACL {cookie_acl(cookie)}")
    return None


def _from_channel_feed(source: str, data: object) -> Optional[ResolvedCookie]:
    if not isinstance(data, dict):
        return None
    last_updated = str(data.get("last_updated") or data.get("updatedAt") or "").strip()
    skipped_acl = 0
    skipped_expired = 0
    latest_exp = 0
    candidates: list[tuple[int, str]] = []
    for item in data.get("channels") or []:
        if not isinstance(item, dict):
            continue
        cookie = cookie_from_value(item.get("cookie"))
        if not cookie:
            continue
        exp = cookie_expiry(cookie, item.get("cookie_expire"))
        if exp:
            latest_exp = max(latest_exp, exp)
        if not is_live(cookie, item.get("cookie_expire")):
            skipped_expired += 1
            continue
        expires_at = _usable(cookie, item.get("cookie_expire"))
        if expires_at is None:
            skipped_acl += 1
            continue
        candidates.append((expires_at, cookie))
    now_ist = datetime.now(IST).strftime("%d %b %Y, %I:%M:%S %p IST")
    exp_ist = _fmt_ist(latest_exp) if latest_exp else "unknown"
    print(
        f"   {source}: {skipped_expired} expired, "
        f"{skipped_acl} path-locked, {len(candidates)} global "
        f"(last_updated={last_updated}; now={now_ist}; latest_exp={exp_ist})"
    )
    if not candidates:
        return None
    expires_at, cookie = max(candidates, key=lambda row: row[0])
    return ResolvedCookie(cookie, source, last_updated, expires_at)


def resolve_cookie() -> ResolvedCookie:
    try:
        resolved = _from_primary(fetch_json(PRIMARY_URL))
        if resolved:
            return resolved
        print("   primary cookie missing, expired, or path-locked")
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as error:
        print(f"   primary cookie feed failed ({error})")

    for source, url in CHANNEL_FEEDS:
        print(f"   trying {source} fallback")
        try:
            resolved = _from_channel_feed(source, fetch_json(url))
        except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as error:
            print(f"   {source} feed failed ({error})")
            continue
        if resolved:
            return resolved

    raise CookieUnavailable(
        "no live __hdnea__ cookie with a global ACL (/* or /bpk-tv/*) "
        "in allinonereborn, sonujson, or binge-giotv feeds"
    )
