#!/usr/bin/env python3
"""
Player timelines from box scores: every franchise a player appeared for, in
order, with the exact first and last game he played for each.

    python scripts/build_timelines.py
    python scripts/build_timelines.py --player "LeBron James"      # spot check
    python scripts/build_timelines.py --player 2544 --verbose

Reads   data/{season}/boxscores.ndjson   (dates joined from games.ndjson if the
                                          box score rows do not carry one)
Writes  data/timelines.ndjson            one line per stint

WHAT A STINT IS, AND WHAT IT IS NOT

A stint is a contiguous run of appearances for one franchise. Sorted by date and
walked in order, so a player who leaves and comes back gets two rows: LeBron
James is Cleveland, Miami, Cleveland, Los Angeles -- four stints, three
franchises. Grouping by team instead of by run would silently merge the two
Cleveland spells into one and report him as a Cavalier from 2003 to 2018.

It is where a player PLAYED, not where he was under contract. Box scores only
contain players who appeared, so a ten-day signing who never got off the bench
leaves no trace, and a stint's first date is a debut, which can fall well after
the trade that caused it. The fields are named first_game and last_game rather
than from and to for that reason.

A gap inside a stint is NOT a break. Nothing in a box score distinguishes a
waiver from an injury, a G-League assignment or a fortnight of DNP-CD, so a
same-franchise gap cannot be split on without inventing a contract event that
may not exist. Instead the longest gap is recorded per stint, so a suspicious
hole is something to go and look at. The transaction database is the right
source for the contract-level version of this question.
"""

import os
import re
import csv
import sys
import json
import glob
import argparse
import collections

VERSION = "v1.1.0-ongoing-season"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(DATA, "timelines.ndjson")

# --------------------------------------------------------------- game types
#
# From the game id prefix, which is the only reliable discriminator -- a date
# cannot tell a play-in game from a regular season one, and the schedule's own
# labels have changed wording between seasons.
#
# All-Star is the one that must not slip through: it would appear as a one-game
# stint for a franchise that does not exist.
GAME_TYPE = {
    "001": "preseason",
    "002": "regular",
    "003": "allstar",
    "004": "playoff",
    "005": "playin",
    "006": "cup",          # NBA Cup final, which counts for neither side
}
COUNTED = {"regular", "playoff", "playin", "cup"}

# --------------------------------------------------------------- franchises
#
# Tricode -> (franchise key, franchise name). One franchise across relocations
# and renames, so Seattle and Oklahoma City are one stint for Kevin Durant and
# New Jersey and Brooklyn are one for Brook Lopez.
#
# TWO DELIBERATE JUDGMENTS HERE, both of which the data cannot settle:
#
#   CHH (Charlotte Hornets, 1988-2002) maps to CHARLOTTE, not to New Orleans,
#   following the NBA's own reassignment of that history to the current Hornets.
#   So a player on CHH in 2001 and NOH in 2003 gets two stints, even though the
#   team he was on physically moved.
#
#   PHO and PHX are the same franchise appearing under two codes in different
#   feeds. Not a relocation at all -- just an inconsistency that would otherwise
#   split a Suns career in half.
# The repo's OWN tricode table is the authority for normalising codes, loaded at
# run time from data/team_codes.json rather than duplicated here. It already
# carries the relocations (SEA->OKC, NJN->BKN, VAN->MEM, CHH->CHA, NOH->NOP) and
# an era rule for BLT, which was two different franchises sharing one code -- a
# flat dict cannot express that and would silently merge them. Everything below
# only supplies DISPLAY NAMES for the normalised codes, plus the handful of codes
# team_codes.json does not mention because they never needed translating.
TEAM_CODES = {}


def load_team_codes():
    """-> ({simple}, {code: [rules]}). Absent file is not fatal, but is reported."""
    path = os.path.join(DATA, "team_codes.json")
    if not os.path.exists(path):
        print("  !! no data/team_codes.json -- tricodes will NOT be normalised,"
              " so relocations will split into separate stints")
        return {}, {}
    with open(path, encoding="utf-8") as f:
        j = json.load(f)
    return j.get("simple", {}), j.get("era", {})


def normalise(tri, season):
    """
    One franchise code, applying the repo's era rules before its simple map.

    Era first: BLT is BAL up to 1955 and WAS from 1963, and a simple lookup would
    answer one of those for both.
    """
    simple, era = TEAM_CODES.get("simple", {}), TEAM_CODES.get("era", {})
    if tri in era:
        for rule in era[tri]:
            lo, hi = rule.get("minYear"), rule.get("maxYear")
            if (lo is None or season >= lo) and (hi is None or season <= hi):
                tri = rule["code"]
                break
    return simple.get(tri, tri)


FRANCHISE = {}


def _f(key, name, *codes):
    for c in codes:
        FRANCHISE[c] = (key, name)


_f("ATL", "Atlanta Hawks", "ATL", "STL", "MLH", "TRI")
_f("BOS", "Boston Celtics", "BOS")
_f("BKN", "Brooklyn Nets", "BKN", "BRK", "NJN", "NYN", "NYA")
_f("CHA", "Charlotte Hornets", "CHA", "CHO", "CHH")
_f("CHI", "Chicago Bulls", "CHI")
_f("CLE", "Cleveland Cavaliers", "CLE")
_f("DAL", "Dallas Mavericks", "DAL")
_f("DEN", "Denver Nuggets", "DEN", "DNA", "DNR")
_f("DET", "Detroit Pistons", "DET", "FTW")
_f("GSW", "Golden State Warriors", "GSW", "GS", "SFW", "PHW")
_f("HOU", "Houston Rockets", "HOU", "SDR")
_f("IND", "Indiana Pacers", "IND", "INA")
_f("LAC", "Los Angeles Clippers", "LAC", "SDC", "BUF")
_f("LAL", "Los Angeles Lakers", "LAL", "MNL")
_f("MEM", "Memphis Grizzlies", "MEM", "VAN")
_f("MIA", "Miami Heat", "MIA")
_f("MIL", "Milwaukee Bucks", "MIL")
_f("MIN", "Minnesota Timberwolves", "MIN")
_f("NOP", "New Orleans Pelicans", "NOP", "NOH", "NOK")
_f("NYK", "New York Knicks", "NYK", "NY")
_f("OKC", "Oklahoma City Thunder", "OKC", "SEA")
_f("ORL", "Orlando Magic", "ORL")
_f("PHI", "Philadelphia 76ers", "PHI", "SYR")
_f("PHX", "Phoenix Suns", "PHX", "PHO")
_f("POR", "Portland Trail Blazers", "POR")
_f("SAC", "Sacramento Kings", "SAC", "KCK", "KCO", "CIN", "ROC")
_f("SAS", "San Antonio Spurs", "SAS", "SA", "DLC", "TEX")
_f("TOR", "Toronto Raptors", "TOR")
_f("UTA", "Utah Jazz", "UTA", "UTH", "NOJ")
_f("WAS", "Washington Wizards", "WAS", "WSB", "CAP", "BAL", "CHZ", "CHP")

# DEFUNCT, with no modern successor. Named rather than left as bare codes, since a
# stint reading "AND" is indistinguishable from a bug -- and folding them into a
# surviving franchise would be worse: these teams died, they did not move.
_f("AND", "Anderson Packers", "AND")
_f("CHS", "Chicago Stags", "CHS")
_f("INO", "Indianapolis Olympians", "INO")
_f("SHE", "Sheboygan Red Skins", "SHE")
_f("WAT", "Waterloo Hawks", "WAT")
_f("STB", "St. Louis Bombers", "STB")
_f("PIT", "Pittsburgh Ironmen", "PIT")
_f("CLR", "Cleveland Rebels", "CLR")
_f("DTF", "Detroit Falcons", "DTF")
_f("TRH", "Toronto Huskies", "TRH")
_f("PRO", "Providence Steamrollers", "PRO")
_f("WSC", "Washington Capitols", "WSC")

# --------------------------------------------------------- field detection
#
# The archive's own field names rather than an assumed schema: this reads files
# written by a different script, and guessing wrong would either crash or, worse,
# silently read the wrong column. Detected from the first row and PRINTED, so a
# schema change shows up as a line of output instead of a wrong answer.
CANDIDATES = {
    "pid":  ["personId", "person_id", "playerId", "player_id", "pid"],
    "name": ["name", "playerName", "player_name", "fullName", "player"],
    "team": ["team", "teamTricode", "tricode", "teamAbbrev", "team_abbrev",
             "teamAbbreviation", "tri", "team_tricode"],
    "gid":  ["gameId", "game_id", "gid"],
    "date": ["date", "gameDate", "game_date", "gameDateEst", "gameDateEast"],
    "min":  ["min", "minutes", "mins", "minutesPlayed", "min_played"],
}


def detect(rows, need=("pid", "team", "gid")):
    """-> {logical: actual}. Loud about anything it could not find."""
    keys = set()
    for r in rows[:200]:
        keys |= set(r.keys())
    got = {}
    for logical, opts in CANDIDATES.items():
        for o in opts:
            if o in keys:
                got[logical] = o
                break
    missing = [n for n in need if n not in got]
    if missing:
        print(f"  !! could not find field(s) for: {', '.join(missing)}")
        print(f"     the rows carry: {', '.join(sorted(keys))}")
        print(f"     add the name to CANDIDATES in {os.path.basename(__file__)}")
        sys.exit(2)
    return got


def read_ndjson(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"  !! {os.path.basename(path)} line {n}: {e}")
    return out


def game_type(gid):
    """-> one of GAME_TYPE's values, or 'unknown'."""
    g = str(gid or "")
    if len(g) >= 3 and g[:3] in GAME_TYPE:
        return GAME_TYPE[g[:3]]
    return "unknown"


def minutes_of(v):
    """
    Minutes as a float, from any of the shapes these feeds use.

    liveData sends ISO durations ('PT34M12.00S'), the archive may hold 'MM:SS'
    or a plain number, and an inactive player may hold '' or None. A DNP has to
    be distinguishable from a real appearance or every inactive player on the
    bench becomes a debut.
    """
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    m = re.match(r"^PT(?:(\d+)M)?(?:([\d.]+)S)?$", s)
    if m:
        return int(m.group(1) or 0) + float(m.group(2) or 0) / 60.0
    if ":" in s:
        a, _, b = s.partition(":")
        try:
            return int(a or 0) + float(b or 0) / 60.0
        except ValueError:
            return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def days_between(a, b):
    """Whole days between two YYYY-MM-DD strings, without importing a calendar."""
    import datetime
    try:
        da = datetime.date(*(int(x) for x in a[:10].split("-")))
        db = datetime.date(*(int(x) for x in b[:10].split("-")))
        return (db - da).days
    except Exception:
        return 0


def season_of(date_str, gid=""):
    """
    Season end year. October onwards belongs to the following year, so the 2025-26
    season is 2026 -- matching the directory names in data/.
    """
    try:
        y, m = int(date_str[:4]), int(date_str[5:7])
        return y + 1 if m >= 9 else y
    except Exception:
        g = str(gid)
        if len(g) >= 5 and g[3:5].isdigit():
            yy = int(g[3:5])
            return 2000 + yy + 1 if yy < 90 else 1900 + yy + 1
        return 0


def load_rows(verbose=False):
    """Every counted player-game appearance, as flat tuples."""
    files = sorted(glob.glob(os.path.join(DATA, "*", "boxscores.ndjson")))
    if not files:
        print(f"  !! no data/*/boxscores.ndjson under {DATA}")
        sys.exit(2)
    print(f"rgm build_timelines {VERSION}\n\n  {len(files)} season file(s)")

    sample = read_ndjson(files[0])[:200]
    F = detect(sample)
    print("  fields: " + ", ".join(f"{k}={v}" for k, v in sorted(F.items())))
    has_date = "date" in F
    has_min = "min" in F
    if not has_date:
        print("  box score rows carry no date -- joining from games.ndjson")
    if not has_min:
        print("  box score rows carry no minutes -- every row counts as an"
              " appearance, so an inactive player may read as a debut")

    seen = set()                       # (pid, gid), against a re-run appending
    kinds = collections.Counter()
    dropped_dnp = 0
    rows = []
    for path in files:
        season_dir = os.path.basename(os.path.dirname(path))
        dates = {}
        if not has_date:
            gpath = os.path.join(os.path.dirname(path), "games.ndjson")
            if os.path.exists(gpath):
                grows = read_ndjson(gpath)
                if grows:
                    G = detect(grows, need=("gid",))
                    if "date" in G:
                        for g in grows:
                            dates[str(g.get(G["gid"]))] = str(g.get(G["date"]))[:10]
            if not dates:
                print(f"  !! {season_dir}: no dates available, skipping")
                continue
        raw = read_ndjson(path)
        # MINUTES WERE NOT RECORDED BEFORE 1951-52, so in the earliest seasons
        # every row reads as zero and the DNP filter deletes the whole season --
        # which it silently did, taking Anderson, Sheboygan and Waterloo with it.
        # Decided per season, and reported: a season that logs no minutes at all
        # is a season where minutes are unknown, not one where nobody played.
        drop_dnp = has_min
        if has_min and raw:
            z = sum(1 for r in raw if not minutes_of(r.get(F["min"])))
            if z >= 0.95 * len(raw):
                drop_dnp = False
                print(f"    {season_dir}: no minutes recorded this season --"
                      f" keeping every row")
        for r in raw:
            gid = str(r.get(F["gid"]) or "")
            pid = str(r.get(F["pid"]) or "").strip()
            if not pid or not gid:
                continue
            kind = game_type(gid)
            kinds[kind] += 1
            if kind not in COUNTED:
                continue
            key = (pid, gid)
            if key in seen:
                continue
            seen.add(key)
            if drop_dnp and minutes_of(r.get(F["min"])) <= 0:
                dropped_dnp += 1
                continue
            tri = str(r.get(F["team"]) or "").strip().upper()
            date = (str(r.get(F["date"]))[:10] if has_date
                    else dates.get(gid, ""))
            if not date:
                continue
            rows.append((pid, date, gid, tri, kind,
                         str(r.get(F["name"]) or "").strip() if "name" in F else ""))

    print(f"\n  {len(rows)} appearance(s) counted")
    for k, n in kinds.most_common():
        mark = "" if k in COUNTED else "   (excluded)"
        print(f"    {n:>9,}  {k}{mark}")
    if dropped_dnp:
        print(f"    {dropped_dnp:>9,}  zero minutes{'':<4}(excluded: on the bench,"
              f" not in the game)")
    if kinds.get("unknown"):
        print("\n  !! unknown game id prefix(es) -- these were EXCLUDED. If they are"
              "\n     real games, add the prefix to GAME_TYPE.")
    return rows


def franchise_of(tri, season):
    code = normalise(tri, season)
    key, name = FRANCHISE.get(code, (code or "???", code or "???"))
    return key, name


def build(rows, verbose=False):
    """-> list of stint dicts, ordered by player then date."""
    by_player = collections.defaultdict(list)
    for pid, date, gid, tri, kind, name in rows:
        by_player[pid].append((date, gid, tri, kind, name))

    unmapped = collections.Counter()
    stints = []
    for pid, games in sorted(by_player.items()):
        # Date first, then game id: two games on one date need a stable order or
        # a trade day could produce the stints back to front.
        games.sort(key=lambda g: (g[0], g[1]))
        name = ""
        for g in games:
            if g[4]:
                name = g[4]
        cur = None
        n = 0
        for date, gid, tri, kind, _nm in games:
            fkey, fname = franchise_of(tri, season_of(date, gid))
            if fkey not in FRANCHISE:
                unmapped[f"{tri} -> {fkey}"] += 1
            if cur is None or cur["franchise"] != fkey:
                if cur:
                    stints.append(cur)
                n += 1
                cur = {"personId": pid, "name": name, "franchise": fkey,
                       "franchise_name": fname, "stint": n,
                       "tricodes": [tri] if tri else [],
                       "first_game": date, "first_game_id": gid,
                       "last_game": date, "last_game_id": gid,
                       "seasons": [], "gp": 0, "gp_regular": 0, "gp_playoff": 0,
                       "gp_playin": 0, "gp_cup": 0,
                       "longest_gap_days": 0, "longest_gap_from": "",
                       "_prev": date}
            # WITHIN A SEASON ONLY. Measured across the whole stint, the longest
            # gap is always the offseason -- a hundred and fifty-odd days every
            # summer -- which drowns the thing this field exists to surface: a
            # month-long hole in the middle of a season, where a waiver, a trade
            # that took a while to debut, or a long injury lives.
            if season_of(cur["_prev"]) == season_of(date):
                gap = days_between(cur["_prev"], date)
                if gap > cur["longest_gap_days"]:
                    cur["longest_gap_days"] = gap
                    cur["longest_gap_from"] = cur["_prev"]
            cur["_prev"] = date
            if tri and tri not in cur["tricodes"]:
                cur["tricodes"].append(tri)
            cur["last_game"], cur["last_game_id"] = date, gid
            cur["gp"] += 1
            cur["gp_" + kind] = cur.get("gp_" + kind, 0) + 1
            s = season_of(date, gid)
            if s and s not in cur["seasons"]:
                cur["seasons"].append(s)
        if cur:
            stints.append(cur)
        # The name is only known after every row is seen, so backfill it.
        for st in stints[-n:] if n else []:
            st["name"] = name

    newest_season = max((season_of(r[1], r[2]) for r in rows), default=0)
    for st in stints:
        st.pop("_prev", None)
        # BY SEASON, not by days. Ten days from the archive's newest game sounded
        # reasonable and was useless: on an archive ending at the Finals it was
        # true for the two finalists and false for the other twenty-eight
        # franchises, so anything asking "who is there now" got two answers.
        #
        # Still not "on the roster" -- a box score cannot know that, and a player
        # traded in February reads as ongoing for the rest of that season. The
        # last_game date beside it is what settles those cases.
        st["ongoing"] = bool(newest_season) and (
            st["seasons"] and max(st["seasons"]) == newest_season)
        st["seasons"] = sorted(st["seasons"])

    if unmapped:
        print(f"\n  !! {len(unmapped)} tricode(s) not in the franchise table --"
              f" each became its own franchise:")
        for t, c in unmapped.most_common(12):
            print(f"     {t}  ({c:,} appearance(s))")
    return stints


def write(stints):
    cols = ["personId", "name", "franchise", "franchise_name", "stint", "tricodes",
            "first_game", "first_game_id", "last_game", "last_game_id", "seasons",
            "gp", "gp_regular", "gp_playoff", "gp_playin", "gp_cup",
            "longest_gap_days", "longest_gap_from", "ongoing"]
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for st in stints:
            f.write(json.dumps({k: st.get(k) for k in cols},
                               ensure_ascii=False, separators=(",", ":")) + "\n")
    os.replace(tmp, OUT)
    players = len({s["personId"] for s in stints})
    print(f"\n  {len(stints):,} stint(s) for {players:,} player(s)"
          f"\n  -> {os.path.relpath(OUT, ROOT)}")


def show(stints, who):
    """One player's timeline, for checking the file against a career you know."""
    w = who.strip().lower()
    hit = [s for s in stints
           if s["personId"] == who.strip() or w in (s["name"] or "").lower()]
    if not hit:
        print(f"\n  no timeline for {who!r}")
        return
    names = sorted({s["name"] or s["personId"] for s in hit})
    if len(names) > 1:
        print(f"\n  {who!r} matches {len(names)}: {', '.join(names[:8])}")
    for nm in names:
        rows = [s for s in hit if (s["name"] or s["personId"]) == nm]
        rows.sort(key=lambda s: s["first_game"])
        print(f"\n  {nm}  ({rows[0]['personId']})")
        print(f"    {'#':>2} {'FRANCHISE':<24} {'FIRST GAME':<12} {'LAST GAME':<12}"
              f" {'GP':>5} {'PO':>4}  SEASONS")
        print("    " + "-" * 88)
        for s in rows:
            tri = "/".join(s["tricodes"])
            seas = (f"{s['seasons'][0]}-{s['seasons'][-1]}" if len(s["seasons"]) > 1
                    else str(s["seasons"][0]) if s["seasons"] else "")
            gap = (f"   in-season gap {s['longest_gap_days']}d from"
                   f" {s['longest_gap_from']}" if s["longest_gap_days"] >= 21 else "")
            print(f"    {s['stint']:>2} {s['franchise_name'][:23]:<24}"
                  f" {s['first_game']:<12} {s['last_game']:<12}"
                  f" {s['gp_regular']:>5} {s['gp_playoff']:>4}  {seas}"
                  f"  [{tri}]{gap}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--player", default="", help="name or personId, to print")
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    simple, era = load_team_codes()
    TEAM_CODES["simple"], TEAM_CODES["era"] = simple, era
    print(f"  team codes: {len(simple)} simple, {len(era)} era rule(s)")
    rows = load_rows(a.verbose)
    stints = build(rows, a.verbose)
    if not a.no_write:
        write(stints)
    if a.player:
        show(stints, a.player)


if __name__ == "__main__":
    main()
