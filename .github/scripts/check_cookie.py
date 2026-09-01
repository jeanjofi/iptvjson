#!/usr/bin/env python3
"""Compare cookies.json to the last committed stamp. Sets changed=true/false."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

COOKIE_URL = "https://allinonereborn2.online/jstrweb2/cookies.json"
STATE = Path(".github/state/cookie.stamp")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)


def fetch_stamp() -> str:
    request = urllib.request.Request(COOKIE_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8", errors="replace"))
    if not isinstance(data, list):
        raise ValueError("cookie feed is not a JSON array")
    updated = next(
        (str(item.get("last_updated", "")).strip() for item in data
         if isinstance(item, dict) and "last_updated" in item),
        "",
    )
    cookie = next(
        (str(item.get("cookie", "")).strip() for item in data
         if isinstance(item, dict) and str(item.get("cookie", "")).startswith("__hdnea__=")),
        "",
    )
    if not cookie:
        raise ValueError("cookie feed did not contain an __hdnea__ cookie")
    return f"{updated}\n{cookie}\n"


def main() -> int:
    force = os.environ.get("FORCE_REBUILD", "").lower() in {"1", "true", "yes"}
    stamp = fetch_stamp()
    previous = STATE.read_text(encoding="utf-8") if STATE.exists() else ""
    changed = force or stamp != previous

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as handle:
            handle.write(f"changed={'true' if changed else 'false'}\n")

    if changed:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(stamp, encoding="utf-8")
        reason = "forced rebuild" if force and stamp == previous else "cookie updated"
        print(f"{reason}; will build jo.m3u")
    else:
        print("Cookie unchanged; skipping build")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as error:
        print(f"FATAL: {error}", file=sys.stderr)
        sys.exit(2)
