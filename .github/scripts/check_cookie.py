#!/usr/bin/env python3
"""Compare the resolved live cookie to the last committed stamp."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
from pathlib import Path

from cookie_feed import CookieUnavailable, resolve_cookie

STATE = Path(".github/state/cookie.stamp")


def fetch_stamp() -> str:
    resolved = resolve_cookie()
    print(
        f"Using {resolved.source} last_updated={resolved.last_updated} "
        f"exp={resolved.expires_at}"
    )
    return f"{resolved.source}\n{resolved.last_updated}\n{resolved.cookie}\n"


def main() -> int:
    force = os.environ.get("FORCE_REBUILD", "").lower() in {"1", "true", "yes"}
    github_output = os.environ.get("GITHUB_OUTPUT")

    def write_changed(value: str) -> None:
        if github_output:
            with open(github_output, "a", encoding="utf-8") as handle:
                handle.write(f"changed={value}\n")

    try:
        stamp = fetch_stamp()
    except CookieUnavailable as error:
        print(f"{error}; keeping the last playlist")
        write_changed("false")
        return 0

    previous = STATE.read_text(encoding="utf-8") if STATE.exists() else ""
    changed = force or stamp != previous
    write_changed("true" if changed else "false")

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
