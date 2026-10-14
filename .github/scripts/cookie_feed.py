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
SONU_URL = "https://sonujson-v5.pages.dev/Data/sports.json"
BINGE_URL = "https://binge-giotv.pages.dev/data/id.json"
PREMIUM_URL = "https://premiumplugx.top/jiostb/mjelo.php?view=raw"
JSON_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)
TIMEOUT = 30
PREMIUM_TIMEOUT = 60
SKEW_SECONDS = 60
EXP_RE = re.compile(r"(?:^|~)exp=(\d+)(?:~|$)")
ACL_RE = re.compile(r"(?:^|~)acl=([^~]+)(?:~|$)")
GLOBAL_ACLS = frozenset({"/*", "*", "/bpk-tv/*"})
CHANNEL_FEEDS = (
    ("binge-giotv", BINGE_URL),
    ("sonujson", SONU_URL),    
)


class CookieUnavailable(ValueError):
    """Feeds responded, but no live cookie with a global ACL was found."""


class ResolvedCookie(NamedTuple):
    cookie: str
    source: str
    last_updated: str
    expires_at: int


class PremiumOverride(NamedTuple):
    cookie: str
    url: str
    slug: str
    expires_at: int


SLUG_RE = re.compile(r"/bpk-tv/([^/]+)/", re.I)
VARIANT_RE = re.compile(r"_(MOB|BTS|WV_DRM)$", re.I)
COOKIE_RE = re.compile(
    r"(__hdnea__=st=\d+~exp=\d+~acl=[^~\s]+~hmac=[0-9a-fA-F]+)"
)
GENERATED_RE = re.compile(r"Generated\s+(.+?)\s*$", re.I)


def fetch_text(url: str, timeout: int = TIMEOUT) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": JSON_UA})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as error:
        reason = str(getattr(error, "reason", error))
        if "CERTIFICATE_VERIFY_FAILED" not in reason and "certificate verify failed" not in reason:
            raise
        print(f"   SSL verify failed for {url}; retrying without verify")
        insecure = ssl._create_unverified_context()
        with urllib.request.urlopen(request, timeout=timeout, context=insecure) as response:
            return response.read().decode("utf-8", errors="replace")


def fetch_json(url: str) -> object:
    return json.loads(fetch_text(url))


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


def stream_slug(url: str) -> str:
    match = SLUG_RE.search(url)
    return match.group(1) if match else ""


def slug_core(slug: str) -> str:
    core = VARIANT_RE.sub("", slug)
    return re.sub(r"[^a-z0-9]", "", core.lower())


def _clean_premium_url(line: str) -> str:
    url = line.strip()
    if "|cookie=" in url:
        url = url.split("|cookie=", 1)[0]
    return url.rstrip("?&")


def _parse_premium_m3u(text: str) -> tuple[str, dict[str, PremiumOverride], int]:
    last_updated = ""
    pending_cookie = ""
    skipped_expired = 0
    by_slug: dict[str, PremiumOverride] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not last_updated:
            generated = GENERATED_RE.search(line)
            if generated:
                last_updated = generated.group(1).strip()
        if line.startswith("http"):
            url = _clean_premium_url(line)
            slug = stream_slug(url)
            cookie_match = COOKIE_RE.search(line)
            cookie = cookie_from_value(
                cookie_match.group(1) if cookie_match else pending_cookie
            )
            pending_cookie = ""
            if not slug or not cookie:
                continue
            if not is_live(cookie):
                skipped_expired += 1
                continue
            expires_at = cookie_expiry(cookie) or 0
            previous = by_slug.get(slug.lower())
            if previous and previous.expires_at >= expires_at:
                continue
            by_slug[slug.lower()] = PremiumOverride(cookie, url, slug, expires_at)
            continue
        cookie_match = COOKIE_RE.search(line)
        if cookie_match:
            pending_cookie = cookie_match.group(1)
    return last_updated, by_slug, skipped_expired


class PremiumPack:
    def __init__(self, last_updated: str, by_slug: dict[str, PremiumOverride]) -> None:
        self.last_updated = last_updated
        self.by_slug = by_slug
        self.by_core: dict[str, PremiumOverride] = {}
        for item in by_slug.values():
            core = slug_core(item.slug)
            if not core:
                continue
            previous = self.by_core.get(core)
            if previous and previous.expires_at >= item.expires_at:
                continue
            self.by_core[core] = item

    def lookup(self, url: str) -> Optional[PremiumOverride]:
        slug = stream_slug(url)
        if not slug:
            return None
        found = self.by_slug.get(slug.lower())
        if found:
            return found
        return self.by_core.get(slug_core(slug))

    def fallback_cookie(self) -> str:
        """Prefer a live global ACL cookie; else the newest live path cookie."""
        if not self.by_slug:
            return ""
        global_ones = [item for item in self.by_slug.values() if is_global_acl(item.cookie)]
        if global_ones:
            chosen = max(global_ones, key=lambda item: item.expires_at)
            print(
                f"   unmatched fallback: global ACL from {chosen.slug} "
                f"exp={_fmt_ist(chosen.expires_at)}"
            )
            return chosen.cookie
        chosen = max(self.by_slug.values(), key=lambda item: item.expires_at)
        print(
            f"   unmatched fallback: latest cookie from {chosen.slug} "
            f"acl={cookie_acl(chosen.cookie)} exp={_fmt_ist(chosen.expires_at)}"
        )
        return chosen.cookie

    def stamp(self) -> str:
        lines = [f"premiumplugx\n{self.last_updated}"]
        for slug, item in sorted(self.by_slug.items()):
            lines.append(f"{slug} {item.cookie}")
        return "\n".join(lines) + "\n"


def load_premium_overrides() -> PremiumPack:
    print("   trying premiumplugx per-channel fallback")
    try:
        text = fetch_text(PREMIUM_URL, timeout=PREMIUM_TIMEOUT)
    except (OSError, urllib.error.URLError) as error:
        print(f"   premiumplugx feed failed ({error})")
        return PremiumPack("", {})
    last_updated, by_slug, skipped_expired = _parse_premium_m3u(text)
    latest_exp = max((item.expires_at for item in by_slug.values()), default=0)
    now_ist = datetime.now(IST).strftime("%d %b %Y, %I:%M:%S %p IST")
    exp_ist = _fmt_ist(latest_exp) if latest_exp else "unknown"
    print(
        f"   premiumplugx: {len(by_slug)} live path cookies, "
        f"{skipped_expired} expired "
        f"(last_updated={last_updated}; now={now_ist}; latest_exp={exp_ist})"
    )
    return PremiumPack(last_updated, by_slug)
