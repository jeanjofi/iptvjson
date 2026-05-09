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

# JIO mobile player User-Agent. Akamai's bot scoring on jiotv*.cdn.jio.com
# rates okhttp/* (ExoPlayer's default) HEAVILY when the request comes from
# a residential IP — the same UA from a cloud datacenter passes. That's
# why curl tests from anywhere "work" but the same URL from a Firestick
# at home gets rejected. Emitting #EXTVLCOPT:http-user-agent=... in the
# M3U lets the existing parser in App.tsx (see normalizeHeaderName +
# applyM3UDirectiveLine) plumb this UA through to ExoPlayer's source
# headers. The app's old jiostar.m3u (Arunjunan20/My-IPTV) used this
# exact UA on every JIO entry — that was THE missing piece when we
# moved playlist generation server-side.
PLAYER_UA = "plaYtv/7.1.4 (Linux;Android 13) ygx/24.1 ExoPlayerLib/4.0"

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
    no_token = 0
    for id_, entry in data.items():
        if not isinstance(entry, dict):
            continue
        raw_url = (entry.get("url") or "").strip()
        if not raw_url:
            no_url += 1
            continue
        # The JSON encodes auth as:
        #   "<manifest-url>|cookie=__hdnea__=st=...~exp=...~hmac=..."
        # The Akamai `__hdnea__` token is what authenticates against the
        # mobile host (jiotvmblive.cdn.jio.com). It works EQUALLY as either
        # a Cookie header OR a URL query param. We pick query param because:
        #
        #   * react-native-video plumbs every HTTP request through React
        #     Native's shared OkHttp client, which has a CookieJar attached
        #     (ForwardingCookieHandler -> Android system CookieManager).
        #     OkHttp's BridgeInterceptor REPLACES the user-supplied Cookie
        #     header with whatever the cookie jar returns; once Akamai's
        #     first response writes Set-Cookie: bm_sv=... / ak_bmsc=...
        #     into the jar, our __hdnea__ token is overwritten on the next
        #     fetch (manifest refresh runs every ~2s for these MPDs).
        #     Result: 403 on the second request and ExoPlayer surfaces it
        #     as ERROR_CODE_IO_BAD_HTTP_STATUS / 22004.
        #
        #   * A token in the URL query is immutable - the cookie jar can't
        #     touch it, and Akamai's signature (acl=/*) accepts it for both
        #     the manifest and every segment that resolves under the same
        #     base URL.
        #
        # So: take the URL LHS, peel out the __hdnea__ value from the
        # cookie suffix, and append it as ?__hdnea__=<value>.
        stream_url, _, cookie_part = raw_url.partition("|")
        stream_url = stream_url.strip()
        token = ""
        if cookie_part:
            # cookie_part looks like: "cookie=__hdnea__=st=...~exp=...~hmac=..."
            # (or sometimes a bare cookie value). Strip the leading
            # "cookie=" wrapper if present, then strip the "__hdnea__="
            # cookie name to get the raw signed value.
            cv = cookie_part.strip()
            if cv.lower().startswith("cookie="):
                cv = cv[len("cookie="):]
            cv = cv.strip().lstrip("; ").strip()
            if cv.startswith("__hdnea__="):
                token = cv[len("__hdnea__="):]
            elif "__hdnea__=" in cv:
                # Cookie may include other name=value pairs ahead of ours.
                token = cv.split("__hdnea__=", 1)[1].split(";", 1)[0]
            # Some entries inline the token without a name (rare); accept it.
            elif cv.startswith("st="):
                token = cv

        if not stream_url:
            no_url += 1
            continue

        if token:
            sep = "&" if "?" in stream_url else "?"
            stream_url = f"{stream_url}{sep}__hdnea__={token}"
        else:
            no_token += 1

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
    if no_token:
        print(f"   NOTE: {no_token} entries had no __hdnea__ token "
              f"(may 403 at playback time)")

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
            # Auth strategy:
            #   * __hdnea__ token: already pinned in the URL query
            #     string, so it survives OkHttp's CookieJar (which
            #     would otherwise stomp any Cookie: header on the next
            #     manifest refresh — see the long comment up top).
            #   * User-Agent: ExoPlayer's default is okhttp/<ver>, and
            #     Akamai's WAF on jiotv*.cdn.jio.com correlates that UA
            #     with residential IPs as "scraper" and 403s segments
            #     even when manifest auth is fine. We force the JIO
            #     mobile player UA (plaYtv/...) which the WAF whitelists.
            #     The app's M3U parser turns this into a source.headers
            #     entry that react-native-video plumbs into ExoPlayer.
            lines.append("#KODIPROP:inputstream.adaptive.license_type=clearkey")
            lines.append(
                "#KODIPROP:inputstream.adaptive.license_key="
                f"{e['kid']}:{e['key']}"
            )
            lines.append(f"#EXTVLCOPT:http-user-agent={PLAYER_UA}")
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
