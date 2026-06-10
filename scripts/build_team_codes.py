#!/usr/bin/env python3
r"""
Build data/team_codes.json: stats.nba.com tricode -> HoopsHype code mapping,
era-aware, plus an AUDIT of every tricode actually present in your data.

    python build_team_codes.py C:\GitHub\nba-boxscores\data
    (or no argument to auto-detect the data folder, like build_player_index)

HoopsHype convention: franchises collapse to their modern code (Tri-Cities ->
ATL, Sonics -> OKC); defunct franchises keep their own code (CHS, CLR, ...).
Era rule needed for BAL: original BAA Bullets (<=1955) stay BAL; the 1963-73
Baltimore Bullets belong to the Wizards franchise -> WAS.
"""

import json, os, sys

# (nba_tricode, hh_code, display_name, minSy, maxSy)  — None = unbounded.
RULES = [
    # --- modern 30 (codes identical) ---
    ("ATL","ATL","Atlanta Hawks",None,None), ("BOS","BOS","Boston Celtics",None,None),
    ("BKN","BKN","Brooklyn Nets",None,None), ("CHA","CHA","Charlotte Hornets",None,None),
    ("CHI","CHI","Chicago Bulls",None,None), ("CLE","CLE","Cleveland Cavaliers",None,None),
    ("DAL","DAL","Dallas Mavericks",None,None), ("DEN","DEN","Denver Nuggets",None,None),
    ("DET","DET","Detroit Pistons",None,None), ("GSW","GSW","Golden State Warriors",None,None),
    ("HOU","HOU","Houston Rockets",None,None), ("IND","IND","Indiana Pacers",None,None),
    ("LAC","LAC","Los Angeles Clippers",None,None), ("LAL","LAL","Los Angeles Lakers",None,None),
    ("MEM","MEM","Memphis Grizzlies",None,None), ("MIA","MIA","Miami Heat",None,None),
    ("MIL","MIL","Milwaukee Bucks",None,None), ("MIN","MIN","Minnesota Timberwolves",None,None),
    ("NOP","NOP","New Orleans Pelicans",None,None), ("NYK","NYK","New York Knicks",None,None),
    ("OKC","OKC","Oklahoma City Thunder",None,None), ("ORL","ORL","Orlando Magic",None,None),
    ("PHI","PHI","Philadelphia Sixers",None,None), ("PHX","PHX","Phoenix Suns",None,None),
    ("POR","POR","Portland Trail Blazers",None,None), ("SAC","SAC","Sacramento Kings",None,None),
    ("SAS","SAS","San Antonio Spurs",None,None), ("TOR","TOR","Toronto Raptors",None,None),
    ("UTA","UTA","Utah Jazz",None,None), ("WAS","WAS","Washington Wizards",None,None),
    # --- relocated/renamed eras -> modern HH code ---
    ("PHW","GSW","Philadelphia Warriors",None,None), ("SFW","GSW","San Francisco Warriors",None,None),
    ("GOS","GSW","Golden State Warriors",None,None),
    ("MNL","LAL","Minneapolis Lakers",None,None),
    ("ROC","SAC","Rochester Royals",None,None), ("CIN","SAC","Cincinnati Royals",None,None),
    ("KCO","SAC","Kansas City-Omaha Kings",None,None), ("KCK","SAC","Kansas City Kings",None,None),
    ("FTW","DET","Fort Wayne Pistons",None,None),
    ("SYR","PHI","Syracuse Nationals",None,None), ("PHL","PHI","Philadelphia 76ers",None,None),
    ("TCB","ATL","Tri-Cities Blackhawks",None,None), ("TRI","ATL","Tri-Cities Blackhawks",None,None),
    ("MLH","ATL","Milwaukee Hawks",None,None), ("STL","ATL","St. Louis Hawks",None,None),
    ("SDR","HOU","San Diego Rockets",None,None),
    ("SEA","OKC","Seattle SuperSonics",None,None),
    ("BUF","LAC","Buffalo Braves",None,None), ("SDC","LAC","San Diego Clippers",None,None),
    ("NOJ","UTA","New Orleans Jazz",None,None),
    ("NYN","BKN","New York Nets",None,None), ("NJN","BKN","New Jersey Nets",None,None),
    ("CHH","CHA","Charlotte Hornets",None,None),  # 1989-2002 originals
    ("NOH","NOP","New Orleans Hornets",None,None), ("NOK","NOP","New Orleans/OKC Hornets",None,None),
    ("VAN","MEM","Vancouver Grizzlies",None,None),
    ("CHP","WAS","Chicago Packers",None,None), ("CHZ","WAS","Chicago Zephyrs",None,None),
    ("CAP","WAS","Capital Bullets",None,None), ("WSB","WAS","Washington Bullets",None,None),
    # --- era-split: BAL / BLT (stats uses both spellings) ---
    ("BAL","BAL","Baltimore Bullets (BAA)",None,1955),
    ("BAL","WAS","Baltimore Bullets",1956,None),
    ("BLT","BAL","Baltimore Bullets (BAA)",None,1955),
    ("BLT","WAS","Baltimore Bullets",1956,None),
    ("BLB","BAL","Baltimore Bullets (BAA)",None,None),
    # --- stats spellings confirmed by data audit ---
    ("DEF","DTF","Detroit Falcons",None,None),
    ("HUS","TOH","Toronto Huskies",None,None),
    ("MIH","ATL","Milwaukee Hawks",None,None),
    ("SAN","SAS","San Antonio Spurs",None,None),
    ("UTH","UTA","Utah Jazz",None,None),
    # --- defunct franchises (own HH codes) ---
    ("AND","AND","Anderson Packers",None,None),
    ("CHS","CHS","Chicago Stags",None,None),
    ("CLR","CLR","Cleveland Rebels",None,None),
    ("DNN","DNN","Denver Nuggets (1950)",None,None), ("DN" ,"DNN","Denver Nuggets (1950)",None,None),
    ("DTF","DTF","Detroit Falcons",None,None),
    ("INJ","INJ","Indianapolis Jets",None,None), ("JET","INJ","Indianapolis Jets",None,None),
    ("INO","INO","Indianapolis Olympians",None,None),
    ("PIT","PIT","Pittsburgh Ironmen",None,None),
    ("PRO","PRO","Providence Steamrollers",None,None),
    ("SHE","SHE","Sheboygan Red Skins",None,None),
    ("STB","STB","St. Louis Bombers",None,None), ("BOM","STB","St. Louis Bombers",None,None),
    ("TRH","TOH","Toronto Huskies",None,None), ("TOH","TOH","Toronto Huskies",None,None),
    ("WSC","WSC","Washington Capitols",None,None), ("WAT","WAT","Waterloo Hawks",None,None),
]


def find_data_dir():
    if len(sys.argv) > 1:
        d = os.path.abspath(sys.argv[1])
        if os.path.isdir(d): return d
        sys.exit(f"not a directory: {d}")
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(os.getcwd(), "data"),
                 os.path.join(here, "data"),
                 os.path.join(os.path.dirname(here), "data")):
        if os.path.isdir(cand): return cand
    sys.exit("couldn't find a data folder — pass it explicitly")


def resolve(tri, sy):
    for nba, hh, name, lo, hi in RULES:
        if nba == tri and (lo is None or sy >= lo) and (hi is None or sy <= hi):
            return hh, name
    return None, None


def main():
    data_dir = find_data_dir()
    out = os.path.join(data_dir, "team_codes.json")

    # AUDIT: every tricode actually present, with season range
    seen = {}   # tri -> [minSy, maxSy, count]
    for s in sorted(d for d in os.listdir(data_dir)
                    if d.isdigit() and os.path.isdir(os.path.join(data_dir, d))):
        p = os.path.join(data_dir, s, "games.ndjson")
        if not os.path.exists(p): continue
        sy = int(s)
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try: g = json.loads(line)
                except Exception: continue
                for tri in (g.get("home"), g.get("away")):
                    if not tri: continue
                    v = seen.setdefault(tri, [sy, sy, 0])
                    v[0] = min(v[0], sy); v[1] = max(v[1], sy); v[2] += 1

    unmapped = []
    print(f"{len(seen)} distinct tricodes in data:")
    for tri in sorted(seen):
        lo, hi, n = seen[tri]
        hh_lo, name_lo = resolve(tri, lo)
        hh_hi, name_hi = resolve(tri, hi)
        if hh_lo is None or hh_hi is None:
            unmapped.append(tri)
            print(f"  {tri:4s} {lo}-{hi} ({n} game-sides)  -> *** UNMAPPED ***")
        elif hh_lo != hh_hi:
            print(f"  {tri:4s} {lo}-{hi} ({n})  -> {hh_lo} (early) / {hh_hi} (late)  [era split]")
        else:
            print(f"  {tri:4s} {lo}-{hi} ({n})  -> {hh_lo}  {name_lo}")

    rules_json = [{"nba": n, "hh": h, "name": nm,
                   **({"minSy": lo} if lo else {}), **({"maxSy": hi} if hi else {})}
                  for n, h, nm, lo, hi in RULES]
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"rules": rules_json}, f, separators=(",", ":"), ensure_ascii=False)
    print(f"\nwrote {out} ({len(rules_json)} rules)")
    if unmapped:
        print("\n*** UNMAPPED CODES — paste this list back so the mapping can be extended: ***")
        print("   ", ", ".join(unmapped))
    else:
        print("all tricodes in the data are covered.")


if __name__ == "__main__":
    main()
