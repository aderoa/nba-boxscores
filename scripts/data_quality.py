#!/usr/bin/env python3
"""
Data-quality sentinel for the box score database. Runs daily after the
updater; writes data/quality_report.json consumed by the NBA Admin.

Checks (current season unless noted):
  freshness   state.json last_processed_date is recent
  dup_games   no duplicate gameIds in games.ndjson
  vs_schedule game count matches the league schedule's Final count (cdn)
  orphans     every boxscore row's gameId exists in games.ndjson
  coverage    every game has player rows, none suspiciously thin (<16)
  nulls       modern rows never have null points

Status: ok | warn | fail | skip (per check); overall = worst non-skip.
"""

import json, os, re, sys
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:
    ET = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(DATA, "quality_report.json")

SCHEDULE_URLS = [
    "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2_1.json",
    "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json",
]
HEADERS = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
           "Referer": "https://www.nba.com/", "Accept": "application/json"}

def now_et():
    return datetime.now(ET) if ET else datetime.utcnow()

def current_sy():
    n = now_et()
    return n.year + 1 if n.month >= 9 else n.year

def ndjson(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try: out.append(json.loads(line))
                except Exception: pass
    return out

def check(checks, cid, label, status, detail=""):
    checks.append({"id": cid, "label": label, "status": status, "detail": detail})
    print(f"[{status.upper():4s}] {label}" + (f" — {detail}" if detail else ""), flush=True)

def main():
    sy = current_sy()
    checks = []

    # freshness
    try:
        st = json.load(open(os.path.join(ROOT, "state.json"), encoding="utf-8"))
        last = datetime.strptime(st["last_processed_date"], "%Y-%m-%d").date()
        lag = (now_et().date() - last).days
        in_season = now_et().month in (10, 11, 12, 1, 2, 3, 4, 5, 6)
        if not in_season:
            check(checks, "freshness", "State freshness", "ok", f"offseason; through {last}")
        elif lag <= 2:
            check(checks, "freshness", "State freshness", "ok", f"through {last} ({lag}d)")
        elif lag <= 5:
            check(checks, "freshness", "State freshness", "warn", f"{lag} days behind ({last})")
        else:
            check(checks, "freshness", "State freshness", "fail", f"{lag} days behind ({last})")
    except Exception as e:
        check(checks, "freshness", "State freshness", "fail", f"state.json unreadable: {e}")
        last = None

    games = ndjson(os.path.join(DATA, str(sy), "games.ndjson"))
    rows = ndjson(os.path.join(DATA, str(sy), "boxscores.ndjson"))
    gids = [g.get("gameId") for g in games]
    gset = set(gids)

    # duplicates
    dups = len(gids) - len(gset)
    check(checks, "dup_games", "Duplicate games",
          "ok" if dups == 0 else "fail",
          f"{len(gids)} games, {dups} duplicate id(s)")

    # vs schedule
    try:
        sched = None
        for u in SCHEDULE_URLS:
            try:
                with urlopen(Request(u, headers=HEADERS), timeout=45) as r:
                    sched = json.loads(r.read().decode("utf-8")); break
            except Exception:
                continue
        if not sched: raise RuntimeError("all schedule sources failed")
        finals = 0
        for gd in sched["leagueSchedule"]["gameDates"]:
            for g in gd.get("games", []):
                gid = str(g.get("gameId", ""))
                if len(gid) > 2 and gid[2] in ("2", "4", "5") and int(g.get("gameStatus", 0)) == 3:
                    d = None
                    for k in ("gameDateEst", "gameDateTimeEst", "gameDateUTC"):
                        if g.get(k):
                            m = re.match(r"(\d{4}-\d{2}-\d{2})", g[k])
                            if m: d = m.group(1); break
                    if last is None or (d and d <= last.isoformat()):
                        finals += 1
        diff = finals - len(gset)
        check(checks, "vs_schedule", "Games vs schedule",
              "ok" if diff == 0 else ("warn" if abs(diff) <= 2 else "fail"),
              f"schedule finals {finals} vs DB {len(gset)} (diff {diff:+d})")
    except Exception as e:
        check(checks, "vs_schedule", "Games vs schedule", "skip", f"schedule unavailable: {e}")

    # orphans + coverage
    per_game = {}
    orphans = 0
    null_pts = 0
    for r in rows:
        gid = r.get("gameId")
        per_game[gid] = per_game.get(gid, 0) + 1
        if gid not in gset: orphans += 1
        if r.get("pts") is None: null_pts += 1
    check(checks, "orphans", "Orphan box score rows",
          "ok" if orphans == 0 else "fail", f"{orphans} rows w/o matching game")
    missing = [g for g in gset if g not in per_game]
    thin = [g for g, n in per_game.items() if n < 16]
    if missing:
        check(checks, "coverage", "Game coverage", "fail", f"{len(missing)} game(s) with NO player rows")
    elif thin:
        check(checks, "coverage", "Game coverage", "warn", f"{len(thin)} game(s) with <16 player rows")
    else:
        check(checks, "coverage", "Game coverage", "ok", f"{len(per_game)} games, all populated")
    check(checks, "nulls", "Null points (modern rows)",
          "ok" if null_pts == 0 else "fail", f"{null_pts} row(s) with null pts")

    order = {"ok": 0, "skip": 0, "warn": 1, "fail": 2}
    worst = max((order[c["status"]] for c in checks), default=0)
    status = {0: "ok", 1: "warn", 2: "fail"}[worst]
    os.makedirs(DATA, exist_ok=True)
    json.dump({"generated_at_utc": datetime.now(timezone.utc).isoformat(),
               "season": sy, "status": status, "checks": checks},
              open(OUT, "w", encoding="utf-8"), separators=(",", ":"), ensure_ascii=False)
    print(f"\noverall: {status.upper()} — wrote {OUT}")

if __name__ == "__main__":
    main()
