#!/usr/bin/env python3
"""
NBA all-time box score backfill — 1946-47 onward, via stats.nba.com
`leaguegamelog` (ONE request returns a full season of player-game rows).

Writes the SAME layout the daily updater uses:
  data/{seasonEndYear}/games.ndjson      one line per game
  data/{seasonEndYear}/boxscores.ndjson  one line per player-game row
  backfill_state.json                    completed (season, type) checkpoints

Design notes
- Per season it makes up to 6 small requests:
    PlayerOrTeam=T and =P, for SeasonType in (Regular Season, Playoffs,
    PlayIn for 2021+). Team rows build games.ndjson (scores, home/away);
    player rows build boxscores.ndjson.
- Resumable: every completed (season, type) pair is checkpointed; rerunning
  skips finished work. Games already present in the NDJSON (e.g. from the
  daily cdn pipeline) are skipped by gameId, so backfill and daily coexist.
- Commits its own progress every COMMIT_EVERY seasons (git identity is set
  by the workflow), so a crashed run keeps everything finished so far.
- Historical stat gaps are real, not bugs: REB <1950-51, MIN <1951-52,
  STL/BLK/OREB/DREB <1973-74, TOV <1977-78, 3PT <1979-80. Missing values
  are stored as null.
- leaguegamelog has no starter flag or position; those fields are omitted
  from backfilled rows (the daily liveData rows do carry them).
"""

import json, os, subprocess, sys, time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
STATE_PATH = os.path.join(ROOT, "backfill_state.json")

LGL_URL = ("https://stats.nba.com/stats/leaguegamelog?Counter=0&Direction=ASC"
           "&LeagueID=00&PlayerOrTeam={pot}&Season={season}&SeasonType={stype}"
           "&Sorter=DATE")

# stats.nba.com refuses requests without its expected browser-ish headers
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

FETCH_RETRIES = 4
RETRY_SLEEP = 5
POLITE_DELAY = 1.5         # between requests; stats.nba.com punishes haste
COMMIT_EVERY = 5           # commit/push progress every N completed seasons


def log(msg): print(msg, flush=True)

def fetch_json(url):
    last = None
    for attempt in range(FETCH_RETRIES):
        try:
            with urlopen(Request(url, headers=HEADERS), timeout=75) as r:
                return json.loads(r.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            time.sleep(RETRY_SLEEP * (2 ** attempt))
    raise RuntimeError(f"fetch failed after {FETCH_RETRIES} tries: {url} ({last})")

def result_rows(payload):
    rs = payload.get("resultSets") or payload.get("resultSet") or []
    if isinstance(rs, dict): rs = [rs]
    if not rs: return [], []
    headers = rs[0].get("headers", [])
    return headers, rs[0].get("rowSet", [])

def season_str(end_year):       # 1947 -> "1946-47"
    s = end_year - 1
    return f"{s}-{str(end_year)[-2:].zfill(2)}"

def read_ndjson_ids(path, key):
    ids = set()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try: ids.add(json.loads(line)[key])
                    except Exception: pass
    return ids

def append_ndjson(path, rows):
    if not rows: return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":"), ensure_ascii=False) + "\n")

def num(v):
    """Pass numbers through, keep genuine absences as null."""
    if v is None or v == "": return None
    return v

def git(*args):
    subprocess.run(["git", *args], cwd=ROOT, check=True)

def commit_progress(msg):
    try:
        subprocess.run(["git", "add", "-A", "data", os.path.basename(STATE_PATH)], cwd=ROOT, check=True)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT)
        if diff.returncode != 0:
            git("commit", "-m", msg)
            git("push")
            log(f"  committed: {msg}")
    except subprocess.CalledProcessError as e:
        log(f"  WARN: git commit/push failed ({e}); data kept on disk")


# ------------------------------------------------------------ per season

def fetch_season(sy, state):
    """Backfill one season (end-year sy). Returns True if fully done."""
    season = season_str(sy)
    stypes = ["Regular Season", "Playoffs"] + (["PlayIn"] if sy >= 2021 else [])
    gpath = os.path.join(DATA_DIR, str(sy), "games.ndjson")
    bpath = os.path.join(DATA_DIR, str(sy), "boxscores.ndjson")
    have_games = read_ndjson_ids(gpath, "gameId")
    have_rows_gids = read_ndjson_ids(bpath, "gameId")

    all_ok = True
    for stype in stypes:
        ck = f"{season}|{stype}"
        if ck in state["done"]:
            continue
        try:
            # ---- team logs -> games.ndjson
            t_pay = fetch_json(LGL_URL.format(pot="T", season=season.replace(" ", "+"),
                                              stype=stype.replace(" ", "+")))
            th, trows = result_rows(t_pay)
            ti = {h: i for i, h in enumerate(th)}
            time.sleep(POLITE_DELAY)

            games = {}
            for r in trows:
                gid = r[ti["GAME_ID"]]
                matchup = r[ti["MATCHUP"]] or ""
                is_home = " vs. " in matchup
                g = games.setdefault(gid, {"gameId": gid,
                                           "date": (r[ti["GAME_DATE"]] or "")[:10],
                                           "sy": sy, "home": None, "away": None,
                                           "homeScore": None, "awayScore": None})
                tri, pts = r[ti["TEAM_ABBREVIATION"]], num(r[ti["PTS"]])
                if is_home: g["home"], g["homeScore"] = tri, pts
                else:       g["away"], g["awayScore"] = tri, pts

            # ---- player logs -> boxscores.ndjson
            p_pay = fetch_json(LGL_URL.format(pot="P", season=season.replace(" ", "+"),
                                              stype=stype.replace(" ", "+")))
            ph, prows = result_rows(p_pay)
            pi = {h: i for i, h in enumerate(ph)}
            time.sleep(POLITE_DELAY)

            new_games, new_rows = [], []
            for gid, g in games.items():
                if gid not in have_games:
                    new_games.append(g); have_games.add(gid)
            for r in prows:
                gid = r[pi["GAME_ID"]]
                if gid in have_rows_gids and gid not in {x["gameId"] for x in new_games}:
                    # whole game already ingested by daily pipeline — skip rows
                    continue
                matchup = r[pi["MATCHUP"]] or ""
                new_rows.append({
                    "gameId": gid, "date": (r[pi["GAME_DATE"]] or "")[:10], "sy": sy,
                    "team": r[pi["TEAM_ABBREVIATION"]],
                    "opp": matchup.split(" ")[-1] if matchup else None,
                    "homeGame": 1 if " vs. " in matchup else 0,
                    "personId": r[pi["PLAYER_ID"]],
                    "name": r[pi["PLAYER_NAME"]],
                    "min": num(r[pi["MIN"]]),
                    "fgm": num(r[pi["FGM"]]), "fga": num(r[pi["FGA"]]),
                    "tpm": num(r[pi.get("FG3M")]) if "FG3M" in pi else None,
                    "tpa": num(r[pi.get("FG3A")]) if "FG3A" in pi else None,
                    "ftm": num(r[pi["FTM"]]), "fta": num(r[pi["FTA"]]),
                    "oreb": num(r[pi.get("OREB")]) if "OREB" in pi else None,
                    "dreb": num(r[pi.get("DREB")]) if "DREB" in pi else None,
                    "reb": num(r[pi.get("REB")]) if "REB" in pi else None,
                    "ast": num(r[pi.get("AST")]) if "AST" in pi else None,
                    "stl": num(r[pi.get("STL")]) if "STL" in pi else None,
                    "blk": num(r[pi.get("BLK")]) if "BLK" in pi else None,
                    "tov": num(r[pi.get("TOV")]) if "TOV" in pi else None,
                    "pf": num(r[pi.get("PF")]) if "PF" in pi else None,
                    "pts": num(r[pi["PTS"]]),
                    "pm": num(r[pi.get("PLUS_MINUS")]) if "PLUS_MINUS" in pi else None,
                })
                have_rows_gids.add(gid)

            append_ndjson(gpath, sorted(new_games, key=lambda x: (x["date"], x["gameId"])))
            append_ndjson(bpath, sorted(new_rows, key=lambda x: (x["date"], x["gameId"], x["team"] or "")))
            state["done"].append(ck)
            save_state(state)
            log(f"{season} {stype}: +{len(new_games)} games, +{len(new_rows)} player rows")
        except Exception as e:
            # PlayIn legitimately may not exist for some seasons; other types failing matters
            if stype == "PlayIn":
                log(f"{season} PlayIn: skipped ({e})")
                state["done"].append(ck); save_state(state)
            else:
                log(f"{season} {stype}: FAILED ({e})")
                all_ok = False
    return all_ok


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)


def main():
    start_sy = int(os.environ.get("START_SEASON_END", "1947"))
    end_sy = int(os.environ.get("END_SEASON_END", "2026"))

    state = {"done": []}
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
    if "done" not in state: state["done"] = []

    log(f"backfill end-years {start_sy}..{end_sy} "
        f"({len(state['done'])} season-type chunks already done)")

    failures = 0
    completed_since_commit = 0
    for sy in range(start_sy, end_sy + 1):
        ok = fetch_season(sy, state)
        if not ok:
            failures += 1
            if failures >= 3:
                log("3 seasons with failures — stopping so we can inspect "
                    "(likely stats.nba.com blocking this runner). "
                    "Progress so far is committed; rerun resumes here.")
                commit_progress(f"Backfill progress through {sy} (partial)")
                sys.exit(1)
        completed_since_commit += 1
        if completed_since_commit >= COMMIT_EVERY:
            commit_progress(f"Backfill through {season_str(sy)}")
            completed_since_commit = 0

    commit_progress(f"Backfill complete {season_str(start_sy)}–{season_str(end_sy)}")
    log("backfill finished")


if __name__ == "__main__":
    main()
