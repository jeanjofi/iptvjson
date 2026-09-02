#!/usr/bin/env python3
"""Resolve a live __hdnea__ cookie from primary, then sports.json fallback."""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import NamedTuple, Optional


PRIMARY_URL = "https://allinonereborn2.online/jstrweb2/cookies.json"
FALLBACK_URL = "https://sonujson-v3.pages.dev/Data/sports.json"
JSON_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)
TIMEOUT = 30
SKEW_SECONDS = 60
EXP_RE = re.compile(r"(?:^|~)exp=(\d+)(?:~|$)")
WILDCARD_ACL_RE = re.compile(r"(?:^|~)acl=/\*(?:~|$)")


class ResolvedCookie(NamedTuple):
    cookie: str
    source: str
    last_updated: str
    expires_at: int


def fetch_json(url: str) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": JSON_UA})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
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


def is_live(cookie: str, explicit: object = None) -> bool:
    expires_at = cookie_expiry(cookie, explicit)
    return expires_at is not None and expires_at > time.time() + SKEW_SECONDS


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
        expires_at = cookie_expiry(cookie) if cookie else None
        if cookie and expires_at is not None and is_live(cookie):
            return ResolvedCookie(cookie, "allinonereborn", last_updated, expires_at)
    return None


def _from_fallback(data: object) -> Optional[ResolvedCookie]:
    if not isinstance(data, dict):
        return None
    last_updated = str(data.get("last_updated") or "").strip()
    candidates: list[tuple[int, int, str]] = []
    for item in data.get("channels") or []:
        if not isinstance(item, dict):
            continue
        cookie = cookie_from_value(item.get("cookie"))
        if not cookie or not is_live(cookie, item.get("cookie_expire")):
            continue
        expires_at = cookie_expiry(cookie, item.get("cookie_expire"))
        if expires_at is None:
            continue
        wildcard = 1 if WILDCARD_ACL_RE.search(cookie) else 0
        candidates.append((wildcard, expires_at, cookie))
    if not candidates:
        return None
    # Prefer acl=/* (usable on all streams), then the latest expiry.
    wildcard, expires_at, cookie = max(candidates, key=lambda row: (row[0], row[1]))
    return ResolvedCookie(cookie, "sonujson", last_updated, expires_at)


def resolve_cookie() -> ResolvedCookie:
    primary_error = ""
    try:
        resolved = _from_primary(fetch_json(PRIMARY_URL))
        if resolved:
            return resolved
        print("   primary cookie missing or expired; trying sonujson fallback")
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as error:
        primary_error = str(error)
        print(f"   primary cookie feed failed ({error}); trying sonujson fallback")

    try:
        resolved = _from_fallback(fetch_json(FALLBACK_URL))
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as error:
        detail = f"; primary: {primary_error}" if primary_error else ""
        raise ValueError(f"fallback cookie feed failed ({error}){detail}") from error
    if resolved:
        return resolved
    raise ValueError("no unexpired __hdnea__ cookie in primary or sonujson feeds")
