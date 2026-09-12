"""Spike (c): transcript availability + IP blocking from this connection. No DB writes.

    .venv\\Scripts\\python spikes\\transcripts_probe.py --ids a,b,c [--sleep 20]
    .venv\\Scripts\\python spikes\\transcripts_probe.py --channel UCbxb2fqe9oNgglAoYqsYOtQ --n 10

Observed 2026-09-12 (German residential IP): ~12 rapid requests -> IpBlocked for a while. Use --sleep >= 20.
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from pipeline.rss import FeedError, fetch_playlist_feed  # noqa: E402
from pipeline.transcripts import fetch_de, merge_segments  # noqa: E402
from pipeline.youtube_api import uush_id  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ids", default="")
    ap.add_argument("--channel", default="")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--sleep", type=float, default=20.0)
    a = ap.parse_args()
    ids = [x.strip() for x in a.ids.split(",") if x.strip()]
    if a.channel:
        try:
            ids += [e["video_id"] for e in fetch_playlist_feed(uush_id(a.channel))][: a.n]
        except FeedError as e:
            raise SystemExit(f"feed failed: {e}")
    if not ids:
        raise SystemExit("give --ids or --channel")
    counts = {"manual": 0, "generated": 0, "none": 0, "disabled": 0, "blocked": 0, "error": 0, "unavailable": 0}
    segs_total = 0
    for i, vid in enumerate(ids):
        r = fetch_de(vid)
        st = r["status"]
        if st == "ok":
            counts["generated" if r["is_generated"] else "manual"] += 1
            n = len(merge_segments(r["snippets"]))
            segs_total += n
            print(f"{vid}: ok {'auto' if r['is_generated'] else 'manual'} snippets={len(r['snippets'])} segments={n}")
        else:
            counts[st] += 1
            print(f"{vid}: {st} {r.get('error', '')}")
            if st == "blocked":
                print("blocked by YouTube -> stop; wait a few hours before the next attempt")
                break
        if i < len(ids) - 1:
            time.sleep(random.uniform(a.sleep, a.sleep * 1.5))
    done = sum(counts.values())
    print("\nsummary:", counts, f"| avg segments {segs_total / max(1, counts['manual'] + counts['generated']):.1f}",
          f"| de available {100 * (counts['manual'] + counts['generated']) / max(1, done):.0f}%")


if __name__ == "__main__":
    main()
