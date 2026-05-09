#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional


JIO_JSON_URL = "https://jo-json.vodep39240327.workers.dev/"
KEY_URL_TEMPLATE = "https://temp.webplay.fun/jtv/key.php?id={id}"

OUT_M3U = Path("jio.m3u")
KEYS_CACHE = Path(".keys_cache.json")

# temp.webplay.fun gates /jtv/key.php on User-Agent: any UA that is not the
# JIO TV mobile player gets a 302 to a Telegram link (verified by curling the
# endpoint with both UAs). Using the same UA the live app uses for streaming
# (see SOURCE_HEADERS_UA in App.tsx -> "plaYtv/7.1.4 ...") returns valid JWK.
JSON_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)
KEY_UA = "plaYtv/7.1.4 (Linux;Android 13) ygx/24.1 ExoPlayerLib/4.0"

TIMEOUT = 30
PARALLEL = 4               # concurrent key fetches (be polite)
INTER_REQ_SLEEP = 0.05     # extra spacing between fetches


# ---------------------------------------------------------------------------
# HTTP / encoding helpers
# ---------------------------------------------------------------------------

def fetch_text(url: str, *, ua: str = JSON_UA) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", errors="replace")


def b64url_to_hex(b64: str) -> str:
    """JWK uses base64url WITHOUT padding. Convert to lowercase hex."""
    pad = "=" * (-len(b64) % 4)
    return base64.urlsafe_b64decode(b64 + pad).hex()


def fetch_clearkey(id_: str) -> Optional[tuple[str, str]]:
    """Return (kid_hex, key_hex) for a channel id, or None on failure.

    The key endpoint returns JWK shaped like:
        { "keys": [{ "kty":"oct", "k":"<b64url>", "kid":"<b64url>" }] }
    Must be requested with the JIO mobile UA — any other UA gets redirected
    to a Telegram link.
    """
    try:
        text = fetch_text(KEY_URL_TEMPLATE.format(id=id_), ua=KEY_UA)
        data = json.loads(text)
        keys = data.get("keys") or []
        if not keys:
            return None
        first = keys[0]
        kid_b64 = (first.get("kid") or "").strip()
        k_b64 = (first.get("k") or "").strip()
        if not kid_b64 or not k_b64:
            return None
        return (b64url_to_hex(kid_b64), b64url_to_hex(k_b64))
    except Exception:
        # Per-id failures are expected (404s, transient 5xx, rate limit).
        # We log a single summary count at the end instead of spamming.
        return None


# ---------------------------------------------------------------------------
# Channel naming + bucketing (mirrors what App.tsx used to do in-process,
# so the user-visible buckets stay identical).
# ---------------------------------------------------------------------------

SLUG_TRIM_SUFFIX = re.compile(
    r"_(MOB|BTS|HD|FHD|UHD|MOB_HD|WV_DRM)$",
    re.IGNORECASE,
)
SLUG_FROM_URL = re.compile(
    r"/bpk-tv/([^/]+)/"
    r"|/([^/]+)/index\.(?:mpd|m3u8)"
    r"|/([^/]+)/master\.(?:mpd|m3u8)",
    re.IGNORECASE,
)


def slug_from_url(url: str) -> Optional[str]:
    m = SLUG_FROM_URL.search(url)
    if not m:
        return None
    return m.group(1) or m.group(2) or m.group(3)


def slug_to_name(slug: str) -> str:
    name = SLUG_TRIM_SUFFIX.sub("", slug)
    name = name.replace("_", " ")
    name = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    name = re.sub(r"(\d)([A-Z])", r"\1 \2", name)
    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        return slug
    upper_tokens = {"hd", "uhd", "fhd", "tv", "mob"}
    return " ".join(
        token.upper() if token.lower() in upper_tokens else token
        for token in name.split(" ")
    )


def bucket_for(name: str) -> str:
    n = name.lower()
    if re.search(r"\b(sport|sports|cricket|wwe|nba|football|kabaddi)\b", n):
        return "Jo Sports"
    if re.search(r"\b(movie|movies|cinema|cine|picture|pix)\b", n):
        return "Jo Movies"
    if re.search(r"\b(news|samachar|samay|aaj|ndtv|cnbc|times now|republic|jagran)\b", n):
        return "Jo News"
    if re.search(r"\b(kid|kids|cartoon|toon|disney|pogo|nick|sonic|hungama|chu chu|baby)\b", n):
        return "Jo Kids"
    if re.search(r"\b(music|songs|beats|mtv|9xm|9x|mastiii|b4u music)\b", n):
        return "Jo Music"
    if re.search(
        r"\b(devotional|bhakti|aastha|sanskar|sadhna|peace|sharnam|soham|buddha|chardham|god)\b",
        n,
    ):
        return "Jo Devotional"
    if re.search(r"\b(discovery|nat geo|history|animal|planet|travel|food|epic)\b", n):
        return "Jo Infotainment"
    return "JIO Live"


# ---------------------------------------------------------------------------
# Cache I/O
# ---------------------------------------------------------------------------

def load_cache() -> dict:
    if KEYS_CACHE.exists():
        try:
            return json.loads(KEYS_CACHE.read_text())
        except Exception as e:
            print(f"WARN: cache file unreadable, starting fresh: {e}",
                  file=sys.stderr)
    return {}


def save_cache(cache: dict) -> None:
    KEYS_CACHE.write_text(json.dumps(cache, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit", type=int, default=0,
        help="If >0, only process the first N channel ids (for local smoke tests).",
    )
    args = parser.parse_args()

    print(f"1) Fetch JIO JSON: {JIO_JSON_URL}")
    raw = fetch_text(JIO_JSON_URL)
    try:
        data = json.loads(raw)
    except Exception as e:
        print(f"FATAL: JIO JSON did not parse: {e}", file=sys.stderr)
        return 2
    if not isinstance(data, dict) or not data:
        print("FATAL: JIO JSON is empty or not a dict", file=sys.stderr)
        return 2
    print(f"   -> {len(data)} entries")
    if args.limit > 0:
        data = dict(list(data.items())[: args.limit])
        print(f"   (limited to first {len(data)} for smoke test)")

    print("2) Resolve ClearKey for each channel (cache + fresh fetches)")
    cache = load_cache()
    needed = list(data.keys())
    todo = [
        id_ for id_ in needed
        if id_ not in cache
        or not cache[id_].get("kid")
        or not cache[id_].get("key")
    ]
    print(f"   cache hit: {len(needed) - len(todo)}, "
          f"fresh fetch needed: {len(todo)}")

    if todo:
        ok = 0
        fail = 0
        with ThreadPoolExecutor(max_workers=PARALLEL) as ex:
            futures = {ex.submit(fetch_clearkey, id_): id_ for id_ in todo}
            done = 0
            for fut in as_completed(futures):
                id_ = futures[fut]
                res = fut.result()
                if res:
                    cache[id_] = {"kid": res[0], "key": res[1]}
                    ok += 1
                else:
                    cache.setdefault(id_, {"kid": "", "key": ""})
                    fail += 1
                done += 1
                if done % 100 == 0:
                    print(f"   ...{done}/{len(todo)}  ok={ok} fail={fail}")
                time.sleep(INTER_REQ_SLEEP)
        print(f"   fresh fetch results: ok={ok} fail={fail}")

    save_cache(cache)

    print("3) Group + write M3U")
    grouped: dict[str, list[dict]] = {}
    no_url = 0
    for id_, entry in data.items():
        if not isinstance(entry, dict):
            continue
        raw_url = (entry.get("url") or "").strip()
        if not raw_url:
            no_url += 1
            continue
        # Drop everything after "|" — the cookie suffix is redundant
        # because the LHS already carries __hdnea__ in its query string,
        # which is enough for the mobile host.
        stream_url = raw_url.split("|", 1)[0].strip()
        if not stream_url:
            no_url += 1
            continue
        slug = slug_from_url(stream_url) or f"Channel {id_}"
        name = slug_to_name(slug) or f"Channel {id_}"
        info = cache.get(id_, {})
        kid_hex = info.get("kid", "")
        key_hex = info.get("key", "")
        bucket = bucket_for(name)
        grouped.setdefault(bucket, []).append({
            "id": id_,
            "name": name,
            "kid": kid_hex,
            "key": key_hex,
            "url": stream_url,
        })

    if no_url:
        print(f"   WARN: skipped {no_url} entries with no usable url")

    # Bucket order: catch-all "JIO Live" first, then alphabetical.
    bucket_order = sorted(grouped.keys(), key=lambda b: (b != "JIO Live", b))

    lines: list[str] = ["#EXTM3U"]
    total_with_key = 0
    total_no_key = 0
    for bucket in bucket_order:
        entries = sorted(grouped[bucket], key=lambda e: e["name"].lower())
        for e in entries:
            if not e["kid"] or not e["key"]:
                # Without a key we cannot decrypt; skip rather than emit a
                # broken entry that would spew DRM errors in the player.
                total_no_key += 1
                continue
            lines.append("#KODIPROP:inputstream.adaptive.license_type=clearkey")
            lines.append(
                "#KODIPROP:inputstream.adaptive.license_key="
                f"{e['kid']}:{e['key']}"
            )
            # Quote attribute values; the comma after the last attribute
            # separates the title (per M3U spec).
            lines.append(
                f'#EXTINF:-1 tvg-id="{e["id"]}" group-title="{bucket}",'
                f'{e["name"]}'
            )
            lines.append(e["url"])
            lines.append("")
            total_with_key += 1

    OUT_M3U.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"   wrote {OUT_M3U} ({OUT_M3U.stat().st_size} bytes)")
    print(f"   channels with key: {total_with_key}")
    print(f"   channels skipped (no key): {total_no_key}")
    print(f"   buckets: {', '.join(bucket_order)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
