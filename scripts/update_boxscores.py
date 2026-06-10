#!/usr/bin/env python3
"""
NBA all-time box score database — daily updater.

Designed to run on GitHub Actions (cron) where stats.nba.com is unreliable
from datacenter IPs. Uses ONLY cdn.nba.com static endpoints:

  schedule (full season, all games + statuses):
    https://cdn.nba.com/static/json/staticData/scheduleLeagueV2_1.json
    https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json   (fallback)

  per-game box score (player-level, persists after the game):
    https://cdn.nba.com/static/json/liveData/boxscore/boxscore_{gameId}.json

Data layout (NDJSON: one JSON object per line — clean git diffs, daily
updates are pure line-appends):

  data/{seasonEndYear}/games.ndjson      one line per game
  data/{seasonEndYear}/boxscores.ndjson  one line per player-game row
  state.json                             {"last_processed_date": "YYYY-MM-DD"}

Idempotent + self-healing: walks every ET date from state date (exclusive)
through yesterday (inclusive); games already present are skipped, so a
failed or missed run simply catches up on the next one.
"""

import json, os, re, sys, time
from datetime import date, datetime, timedelta
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

try:
    from zoneinfo import ZoneInfo  # py3.9+
    ET = ZoneInfo("America/New_York")
except Exception:
    ET = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
STATE_PATH = os.path.join(ROOT, "state.json")

SCHEDULE_URLS = [
    "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2_1.json",
    "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json",
]
BOXSCORE_URL = "https://cdn.nba.com/static/json/liveData/boxscore/boxscore_{gid}.json"

# cdn.nba.com sits behind Akamai, which 403s datacenter IPs presenting
# non-browser UAs. A full browser-like header set passes.
UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

MAX_GAMES_PER_RUN = 250        # safety bound on catch-up bursts
FETCH_RETRIES = 3
RETRY_SLEEP = 4                # seconds, doubled per retry
POLITE_DELAY = 0.6             # between box score fetches


# ---------------------------------------------------------------- helpers

def log(msg):
    print(msg, flush=True)

def fetch_json(url):
    last_err = None
    for attempt in range(FETCH_RETRIES):
        try:
            req = Request(url, headers=UA)
            with urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = e
            # 404 on a box score won't heal by retrying
            if isinstance(e, HTTPError) and e.code == 404:
                break
            time.sleep(RETRY_SLEEP * (2 ** attempt))
    raise RuntimeError(f"fetch failed: {url} ({last_err})")

def today_et():
    if ET:
        return datetime.now(ET).date()
    return (datetime.utcnow() - timedelta(hours=5)).date()  # crude ET fallback

def parse_iso_minutes(s):
    """liveData minutes come as ISO durations like 'PT34M12.00S'."""
    if not s:
        return 0.0
    m = re.match(r"PT(?:(\d+)M)?(?:([\d.]+)S)?", s)
    if not m:
        return 0.0
    mins = int(m.group(1) or 0)
    secs = float(m.group(2) or 0)
    return round(mins + secs / 60.0, 2)

def season_end_year(game_id, game_date):
    """gameId digits 3-4 are the season START year % 100 (e.g. 00225xxxxx ->
    start 2025 -> end year 2026). Falls back to date heuristic (Aug+ = new
    season start)."""
    try:
        yy = int(str(game_id)[3:5])
        start = 2000 + yy if yy < 70 else 1900 + yy
        return start + 1
    except Exception:
        d = datetime.strptime(game_date, "%Y-%m-%d").date()
        return d.year + 1 if d.month >= 8 else d.year

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
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":"), ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- schedule

def load_schedule():
    last_err = None
    for url in SCHEDULE_URLS:
        try:
            j = fetch_json(url)
            games = []
            for gd in j["leagueSchedule"]["gameDates"]:
                for g in gd.get("games", []):
                    games.append(g)
            log(f"schedule: {len(games)} games from {url}")
            return games
        except Exception as e:
            last_err = e
            log(f"schedule source failed, trying next: {e}")
    raise RuntimeError(f"all schedule sources failed ({last_err})")

def game_date_et(g):
    """Prefer explicit ET fields; fall back to UTC datetime."""
    for k in ("gameDateEst", "gameDateTimeEst", "homeTeamTime", "gameDateUTC", "gameDateTimeUTC"):
        v = g.get(k)
        if v:
            m = re.match(r"(\d{4})-(\d{2})-(\d{2})", v)
            if m:
                return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


# ---------------------------------------------------------------- box score

def extract_rows(box, game_date, sy):
    """Player-level rows from a liveData boxscore payload."""
    g = box["game"]
    gid = g["gameId"]
    home, away = g["homeTeam"], g["awayTeam"]
    game_row = {
        "gameId": gid,
        "date": game_date,
        "sy": sy,
        "home": home.get("teamTricode"),
        "away": away.get("teamTricode"),
        "homeScore": home.get("score"),
        "awayScore": away.get("score"),
    }
    player_rows = []
    for team, opp, is_home in ((home, away, 1), (away, home, 0)):
        tri, opp_tri = team.get("teamTricode"), opp.get("teamTricode")
        for p in team.get("players", []):
            st = p.get("statistics", {}) or {}
            if p.get("status") == "INACTIVE":
                continue
            player_rows.append({
                "gameId": gid, "date": game_date, "sy": sy,
                "team": tri, "opp": opp_tri, "homeGame": is_home,
                "personId": p.get("personId"),
                "name": p.get("name") or (p.get("firstName", "") + " " + p.get("familyName", "")).strip(),
                "pos": p.get("position") or "",
                "starter": 1 if str(p.get("starter", "0")) == "1" else 0,
                "min": parse_iso_minutes(st.get("minutes")),
                "fgm": st.get("fieldGoalsMade", 0), "fga": st.get("fieldGoalsAttempted", 0),
                "tpm": st.get("threePointersMade", 0), "tpa": st.get("threePointersAttempted", 0),
                "ftm": st.get("freeThrowsMade", 0), "fta": st.get("freeThrowsAttempted", 0),
                "oreb": st.get("reboundsOffensive", 0), "dreb": st.get("reboundsDefensive", 0),
                "reb": st.get("reboundsTotal", 0),
                "ast": st.get("assists", 0), "stl": st.get("steals", 0),
                "blk": st.get("blocks", 0), "tov": st.get("turnovers", 0),
                "pf": st.get("foulsPersonal", 0), "pts": st.get("points", 0),
                "pm": st.get("plusMinusPoints", 0),
            })
    return game_row, player_rows


# ---------------------------------------------------------------- main

def main():
    # --- state
    state = {"last_processed_date": None}
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
    if not state.get("last_processed_date"):
        log("state.json has no last_processed_date — set one first.")
        sys.exit(1)

    start = datetime.strptime(state["last_processed_date"], "%Y-%m-%d").date() + timedelta(days=1)
    end = today_et() - timedelta(days=1)      # through yesterday ET
    if start > end:
        log(f"nothing to do (state {state['last_processed_date']}, yesterday {end}).")
        return

    log(f"processing ET dates {start} .. {end}")
    schedule = load_schedule()

    # candidate games: in window AND finished (gameStatus 3 = Final)
    wanted = []
    for g in schedule:
        d = game_date_et(g)
        if not d:
            continue
        gd = datetime.strptime(d, "%Y-%m-%d").date()
        if start <= gd <= end and int(g.get("gameStatus", 0)) == 3:
            wanted.append((g["gameId"], d))
    log(f"{len(wanted)} final games in window")

    if len(wanted) > MAX_GAMES_PER_RUN:
        wanted = sorted(wanted, key=lambda x: x[1])[:MAX_GAMES_PER_RUN]
        log(f"capped to {MAX_GAMES_PER_RUN} this run; the rest catch up next run")

    # group by season for dedup + append
    season_cache = {}
    def season_files(sy):
        if sy not in season_cache:
            sd = os.path.join(DATA_DIR, str(sy))
            gpath, bpath = os.path.join(sd, "games.ndjson"), os.path.join(sd, "boxscores.ndjson")
            season_cache[sy] = {"gpath": gpath, "bpath": bpath,
                                "have": read_ndjson_ids(gpath, "gameId"),
                                "new_games": [], "new_rows": []}
        return season_cache[sy]

    fetched = skipped = failed = 0
    last_ok_date = state["last_processed_date"]
    processed_dates = set()
    for gid, d in sorted(wanted, key=lambda x: x[1]):
        sy = season_end_year(gid, d)
        sf = season_files(sy)
        if gid in sf["have"]:
            skipped += 1
            processed_dates.add(d)
            continue
        try:
            box = fetch_json(BOXSCORE_URL.format(gid=gid))
            game_row, rows = extract_rows(box, d, sy)
            sf["new_games"].append(game_row)
            sf["new_rows"].extend(rows)
            sf["have"].add(gid)
            fetched += 1
            processed_dates.add(d)
            time.sleep(POLITE_DELAY)
        except Exception as e:
            failed += 1
            log(f"  FAIL {gid} ({d}): {e}")

    # append per season
    for sy, sf in season_cache.items():
        if sf["new_games"]:
            append_ndjson(sf["gpath"], sorted(sf["new_games"], key=lambda r: (r["date"], r["gameId"])))
            append_ndjson(sf["bpath"], sorted(sf["new_rows"], key=lambda r: (r["date"], r["gameId"], r["team"])))
            log(f"season {sy}: +{len(sf['new_games'])} games, +{len(sf['new_rows'])} player rows")

    # advance state only through the last date with NO failures, so a flaky
    # day gets retried tomorrow instead of being skipped forever
    if failed == 0:
        new_state = end.isoformat()
    else:
        ok_dates = sorted(processed_dates)
        new_state = ok_dates[-1] if ok_dates else last_ok_date
        log(f"{failed} fetch failure(s) — state advances conservatively")
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"last_processed_date": new_state, "updated_at": datetime.utcnow().isoformat() + "Z"}, f, indent=2)

    log(f"done: {fetched} fetched, {skipped} already present, {failed} failed; state -> {new_state}")


if __name__ == "__main__":
    main()
