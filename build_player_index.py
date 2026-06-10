#!/usr/bin/env python3
"""
Build data/player_index.json from the local data/ tree:
    { "Player Name": [seasonEndYear, ...], ... }

Run from the repo clone root (the folder that contains data/):
    python scripts\\build_player_index.py

The daily updater maintains this file incrementally afterward; this script
is for the initial build (and can be rerun anytime to rebuild from scratch).
"""

import json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
OUT = os.path.join(DATA_DIR, "player_index.json")


def main():
    if not os.path.isdir(DATA_DIR):
        sys.exit(f"no data/ directory found at {DATA_DIR}")
    index = {}
    seasons = sorted(d for d in os.listdir(DATA_DIR)
                     if d.isdigit() and os.path.isdir(os.path.join(DATA_DIR, d)))
    for s in seasons:
        path = os.path.join(DATA_DIR, s, "boxscores.ndjson")
        if not os.path.exists(path):
            continue
        sy = int(s)
        names = set()
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    names.add(json.loads(line)["name"])
                except Exception:
                    continue
        for n in names:
            if n:
                index.setdefault(n, []).append(sy)
        print(f"{s}: {len(names)} players")
    for n in index:
        index[n] = sorted(set(index[n]))
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(index, f, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    print(f"\nwrote {OUT}: {len(index)} players, "
          f"{sum(len(v) for v in index.values())} player-seasons, "
          f"{os.path.getsize(OUT)//1024} KB")


if __name__ == "__main__":
    main()
