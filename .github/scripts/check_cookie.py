#!/usr/bin/env python3
"""Compare the resolved live cookie to the last committed stamp."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
from pathlib import Path

from cookie_feed import (
    CookieUnavailable,
    load_source_pack,
    resolve_cookie,
    resolve_entry_source,
)

STATE = Path(".github/state/cookie.stamp")
REPO_ROOT = Path(__file__).resolve().parents[2]
META_CANDIDATES = (Path("meta_mb.json"), REPO_ROOT / "meta_mb.json")


def load_meta_channels() -> list[dict]:
    for path in META_CANDIDATES:
        if path.is_file():
            feed = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(feed, list):
                return [entry for entry in feed if isinstance(entry, dict)]
    return []


def overlay_stamp(global_ok: bool) -> str:
    channels = load_meta_channels()
    premium = load_source_pack("premiumplugx")
    if not global_ok:
        return premium.stamp() if premium.by_slug else ""

    needed: set[str] = set()
    overlay_entries: list[tuple[str, dict]] = []
    if channels:
        for entry in channels:
            source = resolve_entry_source(entry)
            if source == "global":
                continue
            needed.add(source)
            overlay_entries.append((source, entry))
    else:
        return premium.overlay_stamp()

    packs = {"premiumplugx": premium}
    for source in needed:
        if source not in packs:
            packs[source] = load_source_pack(source)

    lines = ["overlay"]
    for source, entry in overlay_entries:
        pack = packs.get(source)
        if not pack:
            continue
        match = pack.lookup(str(entry.get("url") or ""), str(entry.get("name") or ""))
        if match:
            lines.append(f"{source} {match.slug} {match.cookie}")
    return "\n".join(lines) + "\n"


def fetch_stamp() -> str | None:
    parts: list[str] = []
    global_ok = False
    try:
        resolved = resolve_cookie()
        global_ok = True
        print(
            f"Using {resolved.source} last_updated={resolved.last_updated} "
            f"exp={resolved.expires_at}"
        )
        parts.append(f"{resolved.source}\n{resolved.last_updated}\n{resolved.cookie}\n")
    except CookieUnavailable as error:
        print(f"{error}")

    extra = overlay_stamp(global_ok)
    if extra:
        if global_ok:
            print("Including hybrid overlay cookies in stamp")
        else:
            pack = load_source_pack("premiumplugx")
            print(
                f"Using premiumplugx last_updated={pack.last_updated} "
                f"channels={len(pack.by_slug)}"
            )
        parts.append(extra)
    elif not global_ok:
        print("premiumplugx has no live cookies; keeping the last playlist")
        return None
    return "".join(parts)


def main() -> int:
    force = os.environ.get("FORCE_REBUILD", "").lower() in {"1", "true", "yes"}
    github_output = os.environ.get("GITHUB_OUTPUT")

    def write_changed(value: str) -> None:
        if github_output:
            with open(github_output, "a", encoding="utf-8") as handle:
                handle.write(f"changed={value}\n")

    stamp = fetch_stamp()
    if stamp is None:
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
