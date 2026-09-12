"""Spike (d): Data API key + UUSH playlist. Needs YOUTUBE_API_KEY in .env.

    .venv\\Scripts\\python spikes\\uush_probe.py @EasyGerman [@Other ...]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.config import settings  # noqa: E402
from pipeline.youtube_api import PlaylistNotFound, YouTubeAPI, uush_id  # noqa: E402


def main() -> None:
    if not settings.youtube_api_key:
        raise SystemExit("YOUTUBE_API_KEY missing in .env")
    handles = sys.argv[1:] or ["@EasyGerman"]
    with YouTubeAPI() as api:
        for h in handles:
            info = api.resolve_handle(h)
            if not info:
                print(f"{h}: not found")
                continue
            pid = uush_id(info["id"])
            print(f"{h}: {info['title']} id={info['id']} shorts_playlist={pid}")
            try:
                items, _ = api.list_playlist_items(pid)
                for it in items[:5]:
                    print("   ", it["video_id"], it["published_at"])
                print(f"    {len(items)} items on page 1")
            except PlaylistNotFound:
                print("    no UUSH playlist (fallback: uploads playlist + shorts check)")
        print("quota units used:", api.session_units)


if __name__ == "__main__":
    main()
