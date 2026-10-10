#!/usr/bin/env python3
"""
NBA all-time box score database — daily updater.  v2.0.0-stats-local

SOURCE CHANGED, AND SO DID WHERE THIS RUNS.

v1 used cdn.nba.com static endpoints exclusively, deliberately, because
stats.nba.com is unreachable from datacenter IPs. On 2026-10-10 that bargain
collapsed: cdn.nba.com answers 403 to EVERYTHING -- python urllib, curl,
curl --http2, node fetch, and Chrome on a residential connection. It is an
Akamai access rule (reference prefix 18., errors.edgesuite.net), not a bot
challenge, so no client or header set gets around it. Measured the same hour:

    cdn.nba.com  staticData schedule        403   (was the v1 schedule source)
    cdn.nba.com  liveData boxscore          403   (was the v1 box source)
    cdn.nba.com  logos/*.svg                200   (unaffected; pages are fine)
    stats.nba.com leaguegamelog             200   from a residential IP
    stats.nba.com leaguegamelog             ---   refused from a GH runner

So the only surviving source is the one that will not run on Actions. This
script therefore runs LOCALLY and pushes, the way backfill_boxscores.py
already does. daily.yml keeps building timelines from committed data, which
needs no network.

WHAT IT FETCHES NOW

leaguegamelog, exactly as backfill_boxscores.py does -- two requests per
season-type for the whole window, not one per game:

    .../stats/leaguegamelog?PlayerOrTeam=T&Season=2026-27&SeasonType=...&DateFrom=...
    .../stats/leaguegamelog?PlayerOrTeam=P&...

Team rows build games.ndjson, player rows build boxscores.ndjson. Row shape
is byte-identical to the backfill's, which means the two differ from v1 in
one way worth knowing: leaguegamelog carries no `pos` or `starter`, so those
keys are absent, as they already are for every backfilled season.

SEASON TYPES: Regular Season, PlayIn, Playoffs -- matching the backfill.
Preseason is NOT ingested by default and must never be, silently: these
files feed Global Rating season totals, and exhibition games would quietly
inflate them. --preseason exists for deliberate experiments and says so.

  python update_boxscores.py                  incremental since state.json
  python update_boxscores.py --dry-run        fetch and report, write nothing
  python update_boxscores.py --preseason --dry-run
        tonight's exhibition games as a live test of the whole path, with no
        risk to the archive -- the only way to test this before 2026-10-21
  python update_boxscores.py --since 2026-10-21 --no-push
  python update_boxscores.py --repo D:\\repos\\nba-boxscores
        when this script is run from a copy outside the repo

Writes last_run.json on EVERY attempt and exits non-zero on failure, because
the fault this replaces was invisible for two months: the updater failed on
every run while the workflow stayed green and the data simply stopped.
"""

import os
import re
import sys
import json
import time
import argparse
import subprocess
from datetime import date, datetime, timedelta, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

VERSION = "v2.0.1-stats-local"

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:
    ET = None

LGL_URL = ("https://stats.nba.com/stats/leaguegamelog?Counter=0&Direction=ASC"
           "&LeagueID=00&PlayerOrTeam={pot}&Season={season}&SeasonType={stype}"
           "&Sorter=DATE")

# Verbatim from backfill_boxscores.py, which is the request shape proven to
# work from this machine. stats.nba.com refuses anything less.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
    "Connection": "keep-alive",
}

STYPES = ["Regular Season", "PlayIn", "Playoffs"]
PRESEASON = "Pre Season"
FETCH_RETRIES = 4
RETRY_SLEEP = 5
POLITE_DELAY = 1.5          # stats.nba.com punishes haste


def log(m): print(m, flush=True)


def utc_stamp():
    """'2026-10-10T22:47:27.702068Z' — the shape state.json already carries,
    via a timezone-aware clock. datetime.utcnow() is deprecated in 3.12 and
    goes away later, and isoformat() on an aware object would otherwise write
    '+00:00' where every existing row ends in 'Z'."""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"


# ------------------------------------------------------------------ fetch

class FetchError(RuntimeError):
    def __init__(self, url, last):
        self.url = url; self.last = last
        self.status = getattr(last, "code", None)
        super().__init__(f"fetch failed: {url} ({last})")


def fetch_json(url):
    last = None
    for i in range(FETCH_RETRIES):
        try:
            with urlopen(Request(url, headers=HEADERS), timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            if isinstance(e, HTTPError) and e.code in (400, 404):
                break                      # a bad season/type will not heal
            time.sleep(RETRY_SLEEP * (i + 1))
    raise FetchError(url, last)


def result_rows(payload):
    """stats.nba.com resultSets -> (headers, rowSet). Empty is legitimate:
    Playoffs in October simply has no rows yet."""
    rs = payload.get("resultSets") or payload.get("resultSet") or []
    if isinstance(rs, dict):
        rs = [rs]
    if not rs:
        return [], []
    return (rs[0].get("headers") or []), (rs[0].get("rowSet") or [])


# ------------------------------------------------------------------ helpers

def today_et():
    if ET:
        return datetime.now(ET).date()
    return (datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(hours=5)).date()


def num(v):
    """Pass numbers through, keep genuine absences as null.
    Same contract as backfill_boxscores.py."""
    if v is None or v == "":
        return None
    return v


def season_end_year_for(d):
    """A date in Aug..Dec belongs to the season ending the NEXT year."""
    return d.year + 1 if d.month >= 8 else d.year


def season_str(sy):
    """2026 -> '2025-26'."""
    return f"{sy - 1}-{str(sy % 100).zfill(2)}"


def read_ndjson_ids(path, key):
    ids = set()
    if not os.path.exists(path):
        return ids
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ids.add(json.loads(line)[key])
            except Exception:
                continue
    return ids


def append_ndjson(path, rows):
    if not rows:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":"),
                               ensure_ascii=False) + "\n")


def mdy(d):
    """stats.nba.com wants MM/DD/YYYY for DateFrom/DateTo."""
    return d.strftime("%m/%d/%Y")


# ------------------------------------------------------------------ mapping
# Both builders below mirror backfill_boxscores.py field for field. Any edit
# here must be made there too, or the archive acquires two row shapes.

def games_from_team_rows(headers, rows, sy):
    ti = {h: i for i, h in enumerate(headers)}
    games = {}
    for r in rows:
        gid = r[ti["GAME_ID"]]
        matchup = r[ti["MATCHUP"]] or ""
        is_home = " vs. " in matchup
        g = games.setdefault(gid, {"gameId": gid,
                                   "date": (r[ti["GAME_DATE"]] or "")[:10],
                                   "sy": sy, "home": None, "away": None,
                                   "homeScore": None, "awayScore": None})
        tri, pts = r[ti["TEAM_ABBREVIATION"]], num(r[ti["PTS"]])
        if is_home:
            g["home"], g["homeScore"] = tri, pts
        else:
            g["away"], g["awayScore"] = tri, pts
    return games


def rows_from_player_rows(headers, rows, sy):
    pi = {h: i for i, h in enumerate(headers)}

    def col(r, name):
        return num(r[pi[name]]) if name in pi else None

    out = []
    for r in rows:
        matchup = r[pi["MATCHUP"]] or ""
        out.append({
            "gameId": r[pi["GAME_ID"]],
            "date": (r[pi["GAME_DATE"]] or "")[:10], "sy": sy,
            "team": r[pi["TEAM_ABBREVIATION"]],
            "opp": matchup.split(" ")[-1] if matchup else None,
            "homeGame": 1 if " vs. " in matchup else 0,
            "personId": r[pi["PLAYER_ID"]],
            "name": r[pi["PLAYER_NAME"]],
            "min": col(r, "MIN"),
            "fgm": col(r, "FGM"), "fga": col(r, "FGA"),
            "tpm": col(r, "FG3M"), "tpa": col(r, "FG3A"),
            "ftm": col(r, "FTM"), "fta": col(r, "FTA"),
            "oreb": col(r, "OREB"), "dreb": col(r, "DREB"),
            "reb": col(r, "REB"), "ast": col(r, "AST"),
            "stl": col(r, "STL"), "blk": col(r, "BLK"),
            "tov": col(r, "TOV"), "pf": col(r, "PF"),
            "pts": col(r, "PTS"), "pm": col(r, "PLUS_MINUS"),
        })
    return out


# ------------------------------------------------------------------ git

def commit_and_push(root, msg):
    try:
        subprocess.run(["git", "add", "-A", "data", "state.json"],
                       cwd=root, check=True)
        if subprocess.run(["git", "diff", "--cached", "--quiet"],
                          cwd=root).returncode == 0:
            log("  nothing staged — no commit")
            return True
        subprocess.run(["git", "commit", "-m", msg], cwd=root, check=True)
        subprocess.run(["git", "push"], cwd=root, check=True)
        log(f"  pushed: {msg}")
        return True
    except subprocess.CalledProcessError as e:
        log(f"  WARN: git commit/push failed ({e}) — data is on disk,"
            f" push it by hand")
        return False


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="",
                    help="repo root, if running from a copy of this script")
    ap.add_argument("--since", default="",
                    help="YYYY-MM-DD; overrides state.json")
    ap.add_argument("--through", default="",
                    help="YYYY-MM-DD; default is yesterday ET")
    ap.add_argument("--preseason", action="store_true",
                    help="ALSO ingest Pre Season — changes what the archive "
                         "means; Global Rating totals would include it")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and report, write and commit nothing")
    ap.add_argument("--no-push", action="store_true",
                    help="write files but do not commit or push")
    a = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    log(f"update_boxscores.py {VERSION}")

    # ---- locate the repo
    root = os.path.abspath(a.repo) if a.repo else os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))
    data_dir = os.path.join(root, "data")
    state_path = os.path.join(root, "state.json")
    if not os.path.isdir(data_dir) or not os.path.exists(state_path):
        log(f"  not a box-score repo: {root}")
        log("  pass --repo with the path to your nba-boxscores clone")
        return 2
    log(f"  repo: {root}")

    run = {"version": VERSION, "started_utc": utc_stamp(),
           "ok": False, "error": None, "dry_run": a.dry_run,
           "seasons": {}, "games_added": 0, "rows_added": 0}

    def finish(code):
        run["finished_utc"] = utc_stamp()
        run["ok"] = (code == 0)
        try:
            with open(os.path.join(root, "last_run.json"), "w",
                      encoding="utf-8") as f:
                json.dump(run, f, indent=2)
        except Exception as e:                              # noqa: BLE001
            log(f"  WARN: could not write last_run.json ({e})")
        return code

    # ---- window
    with open(state_path, "r", encoding="utf-8") as f:
        state = json.load(f)
    if a.since:
        start = datetime.strptime(a.since, "%Y-%m-%d").date()
    else:
        lp = state.get("last_processed_date")
        if not lp:
            log("  state.json has no last_processed_date — set one first.")
            run["error"] = "no last_processed_date"
            return finish(1)
        start = datetime.strptime(lp, "%Y-%m-%d").date() + timedelta(days=1)
    end = (datetime.strptime(a.through, "%Y-%m-%d").date() if a.through
           else today_et() - timedelta(days=1))
    run["window"] = [start.isoformat(), end.isoformat()]
    if start > end:
        log(f"  nothing to do: window {start} .. {end} is empty"
            f" (state {state.get('last_processed_date')})")
        return finish(0)
    log(f"  window: {start} .. {end} (ET)")

    stypes = list(STYPES) + ([PRESEASON] if a.preseason else [])
    if a.preseason:
        log("  PRESEASON INCLUDED — exhibition games will enter the archive"
            " and Global Rating totals unless this is a --dry-run")

    # A window can straddle a season boundary; ask each season it touches.
    seasons = sorted({season_end_year_for(start), season_end_year_for(end)})
    log(f"  season(s): {', '.join(season_str(s) for s in seasons)}")

    failures = []
    total_games = total_rows = 0
    name_seasons = set()

    for sy in seasons:
        season = season_str(sy)
        gpath = os.path.join(data_dir, str(sy), "games.ndjson")
        bpath = os.path.join(data_dir, str(sy), "boxscores.ndjson")
        have_games = read_ndjson_ids(gpath, "gameId")
        have_rows = read_ndjson_ids(bpath, "gameId")
        new_games, new_rows = [], []

        for stype in stypes:
            tag = f"{season} {stype}"
            base = ("&DateFrom=" + mdy(start).replace("/", "%2F")
                    + "&DateTo=" + mdy(end).replace("/", "%2F"))
            try:
                t_url = LGL_URL.format(pot="T", season=season.replace(" ", "+"),
                                       stype=stype.replace(" ", "+")) + base
                th, trows = result_rows(fetch_json(t_url))
                time.sleep(POLITE_DELAY)
                if not trows:
                    log(f"  {tag}: no games in window")
                    continue
                p_url = LGL_URL.format(pot="P", season=season.replace(" ", "+"),
                                       stype=stype.replace(" ", "+")) + base
                ph, prows = result_rows(fetch_json(p_url))
                time.sleep(POLITE_DELAY)

                games = games_from_team_rows(th, trows, sy)
                prs = rows_from_player_rows(ph, prows, sy)

                add_g = [g for gid, g in games.items() if gid not in have_games]
                for g in add_g:
                    have_games.add(g["gameId"])
                add_r = [r for r in prs if r["gameId"] not in have_rows]
                for r in add_r:
                    have_rows.add(r["gameId"])

                new_games += add_g
                new_rows += add_r
                for r in add_r:
                    if r.get("name"):
                        name_seasons.add((r["name"], sy))
                log(f"  {tag}: {len(games)} game(s) seen, +{len(add_g)} new,"
                    f" +{len(add_r)} player row(s)"
                    + ("  [already present]" if games and not add_g else ""))
            except Exception as e:                          # noqa: BLE001
                # A missing PlayIn or Playoffs block is normal; a failing
                # Regular Season block is not, and must not pass quietly.
                if stype in (PRESEASON, "PlayIn", "Playoffs"):
                    log(f"  {tag}: unavailable ({e})")
                else:
                    failures.append(f"{tag}: {e}")
                    log(f"  {tag}: FAILED ({e})")

        run["seasons"][season] = {"games": len(new_games), "rows": len(new_rows)}
        total_games += len(new_games)
        total_rows += len(new_rows)

        if a.dry_run:
            for g in sorted(new_games, key=lambda x: (x["date"], x["gameId"]))[:8]:
                log(f"      {g['date']}  {g['gameId']}  "
                    f"{g['away']} {g['awayScore']} @ {g['home']} {g['homeScore']}")
            if len(new_games) > 8:
                log(f"      ... and {len(new_games) - 8} more")
            continue

        append_ndjson(gpath, sorted(new_games,
                                    key=lambda x: (x["date"], x["gameId"])))
        append_ndjson(bpath, sorted(new_rows,
                                    key=lambda x: (x["date"], x["gameId"],
                                                   x["team"] or "")))

    run["games_added"] = total_games
    run["rows_added"] = total_rows

    if a.dry_run:
        log(f"\n  DRY RUN — nothing written. {total_games} game(s),"
            f" {total_rows} player row(s) would be added.")
        if failures:
            log("  failures: " + "; ".join(failures))
            return finish(1)
        return finish(0)

    # ---- player index, same contract as v1
    if name_seasons:
        ipath = os.path.join(data_dir, "player_index.json")
        try:
            index = {}
            if os.path.exists(ipath):
                with open(ipath, "r", encoding="utf-8") as f:
                    index = json.load(f)
            changed = False
            for nm, sy in name_seasons:
                ss = index.setdefault(nm, [])
                if sy not in ss:
                    ss.append(sy); ss.sort(); changed = True
            if changed:
                with open(ipath, "w", encoding="utf-8") as f:
                    json.dump(index, f, separators=(",", ":"),
                              ensure_ascii=False, sort_keys=True)
                log("  player_index.json updated")
        except Exception as e:                              # noqa: BLE001
            log(f"  WARN: player index update failed ({e})"
                f" — rebuild with build_player_index.py")

    # ---- state: advance only on a clean run, so a bad day is retried
    if failures:
        log(f"  {len(failures)} failure(s) — state NOT advanced, so the next"
            f" run retries this window")
        run["error"] = "; ".join(failures)
    else:
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump({"last_processed_date": end.isoformat(),
                       "updated_at": utc_stamp()},
                      f, indent=2)
        log(f"  state -> {end.isoformat()}")

    if not a.no_push and (total_games or total_rows or not failures):
        commit_and_push(root, f"Update {date.today().isoformat()}:"
                              f" +{total_games} game(s), +{total_rows} row(s)")

    log(f"\n  done: +{total_games} game(s), +{total_rows} player row(s)"
        + (f", {len(failures)} failure(s)" if failures else ""))
    return finish(1 if failures else 0)


if __name__ == "__main__":
    sys.exit(main())
