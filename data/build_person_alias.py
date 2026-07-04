#!/usr/bin/env python3
"""
build_person_alias.py  -  Generate personId -> HoopsHype-name overrides for same-named players.

Joins your box scores (which carry personId + the plain NBA name + team + season) to the
All-Time Database CSV (which carries the HoopsHype name per team+year) and, for every player
whose correct HoopsHype name differs from what the name map would give, writes an override
keyed by personId. That's what lets the leaderboard tell apart two players who share a name
(Fast Eddie Johnson vs Eddie Johnson, the two George Johnsons, the year-suffixed Mike James /
Charles Smith / Larry Johnson, renames like Marcus D. Williams, etc.).

USAGE
-----
    python build_person_alias.py --csv "All-Time_Database.csv" --names player_names.json \
        --box data/1979/boxscores.ndjson data/1980/boxscores.ndjson ...
    # or point --box at a folder and it'll find every *.ndjson under it:
    python build_person_alias.py --csv all_time.csv --names player_names.json --box data/

Output: person_alias.json  ->  { "77144": "Fast Eddie Johnson", ... }
Put it at data/person_alias.json in your explorer repo; the app loads it automatically.
"""

import os
import csv
import re
import sys
import glob
import json
import difflib
import argparse
from collections import defaultdict, Counter

# ---- franchise normalization (box uses era tricodes; the CSV uses franchise codes) ----
FRANCHISE_DEFS = [
    ("ATL", ["TRI", "TCB", "MLH", "MIH", "STL", "ATL"]),
    ("GSW", ["PHW", "SFW", "SFO", "GOS", "GSW"]),
    ("SAC", ["ROC", "CIN", "KCO", "KCK", "SAC"]),
    ("PHI", ["SYR", "PHI", "PHL"]),
    ("DET", ["FTW", "DET"]),
    ("WAS", ["CHP", "CHS", "CHZ", "BAL", "CAP", "WSB", "WAS"]),
    ("OKC", ["SEA", "OKC"]),
    ("UTA", ["NOJ", "UTA", "UTH"]),
    ("HOU", ["SDR", "HOU"]),
    ("LAC", ["BUF", "SDC", "LAC"]),
    ("LAL", ["MNL", "LAL"]),
    ("BKN", ["NJA", "NYN", "NJN", "BRK", "BKN"]),
    ("MEM", ["VAN", "MEM"]),
    ("NOP", ["NOH", "NOK", "NOP"]),
    ("CHA", ["CHH", "CHA", "CHO"]),
    ("SAS", ["SAS", "SAN", "SAA"]),
    ("DEN", ["DEN", "DNR"]),
    # defunct franchises keep their own codes; add any the CSV uses:
    ("STB", ["BOM", "STB"]),       # St. Louis Bombers
    ("TOH", ["HUS", "TOH"]),       # Toronto Huskies
    ("DNN", ["DN", "DNN"]),        # Denver Nuggets (1949-50)
    ("INJ", ["JET", "INJ"]),       # Indianapolis Jets
    ("WSC", ["WSC", "CAP49"]),     # Washington Capitols
    ("BAL", ["BLT", "BLB", "BAL2"]),  # original Baltimore Bullets (1947-54)
]
TEAM_CANON = {}
for code, tris in FRANCHISE_DEFS:
    for t in tris:
        TEAM_CANON[t.upper()] = code


def canon_team(t):
    t = (t or "").upper()
    return TEAM_CANON.get(t, t)


def _norm(s):
    s = (s or "").lower()
    s = re.sub(r"\(\d{4}\)", "", s)                 # strip HoopsHype "(YYYY)" suffix
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", s)    # strip generational suffixes
    s = re.sub(r"[^a-z ]", "", s)
    return " ".join(s.split())


def load_names(path):
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_csv(path):
    idx = defaultdict(list)   # (team, year) -> [{hh, nb}]
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            hh = (r.get("PLAYER") or "").strip()
            team = (r.get("TEAM") or "").strip().upper()
            yr = (r.get("YEAR") or "").strip()
            if not hh or not team or not yr.isdigit():
                continue
            idx[(team, int(yr))].append({"hh": hh, "nb": (r.get("NB CODE") or "").strip()})
    return idx


def box_files(paths):
    out = []
    for p in paths:
        if os.path.isdir(p):
            out += glob.glob(os.path.join(p, "**", "*.ndjson"), recursive=True)
        else:
            out.append(p)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, help="All-Time Database CSV (PLAYER/YEAR/TEAM/NB CODE)")
    ap.add_argument("--names", help="player_names.json (to decide which overrides are needed)")
    ap.add_argument("--box", nargs="+", required=True, help="box .ndjson files or a folder")
    ap.add_argument("--out", default="person_alias.json")
    args = ap.parse_args()

    pn = load_names(args.names)
    disp = lambda n: pn.get(n, n)
    csv_idx = load_csv(args.csv)

    # gather box: per (team,year) roster, and per-person plain-name votes
    ts_roster = defaultdict(list)          # (team, year) -> [(pid, plain)]
    pid_plain = defaultdict(Counter)       # pid -> Counter(plain)
    files = box_files(args.box)
    if not files:
        print("No box .ndjson files found. Looked in:")
        for p in args.box:
            ap = os.path.abspath(p)
            if os.path.isdir(ap):
                any_files = glob.glob(os.path.join(ap, "**", "*"), recursive=True)
                nd = [x for x in any_files if x.lower().endswith(".ndjson")]
                print(f"   {ap}  (folder exists; {len(nd)} .ndjson, {len(any_files)} items total)")
            elif os.path.exists(ap):
                print(f"   {ap}  (this is a file, not a folder)")
            else:
                print(f"   {ap}  (does NOT exist)")
        print()
        print("Fix: point --box at the folder holding your season files (each a boxscores.ndjson),")
        print("  e.g.  --box \"C:\\path\\to\\nba-box-explorer\\data\"")
        print("or pass the file(s) directly, e.g.  --box C:\\path\\to\\boxscores.ndjson")
        print("Tip: run  dir /s /b *.ndjson  from a folder to locate your box files.")
        sys.exit(1)
    print(f"box files found: {len(files)}")
    for fp in files:
        with open(fp, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                pid = r.get("personId")
                if pid is None:
                    continue
                pid = str(pid)
                plain = r.get("name", "")
                team = canon_team(r.get("team"))
                sy = r.get("sy")
                pid_plain[pid][plain] += 1
                ts_roster[(team, sy)].append((pid, plain))

    # match rosters (team, year) -> assign HH names
    pid_hh = defaultdict(Counter)          # pid -> Counter(hh)
    unmatched = Counter()
    for (team, yr), roster in ts_roster.items():
        cands = csv_idx.get((team, yr), [])
        used = [False] * len(cands)
        seen = set()
        pending = []
        for pid, plain in roster:
            if (pid, plain) in seen:
                continue
            seen.add((pid, plain))
            hit = None
            for i, c in enumerate(cands):
                if used[i]:
                    continue
                if (c["nb"] and c["nb"] == plain) or (disp(plain) == c["hh"]) or (plain == c["hh"]):
                    hit = i
                    break
            if hit is not None:
                used[hit] = True
                pid_hh[pid][cands[hit]["hh"]] += 1
            else:
                pending.append((pid, plain))
        # leftover pairing: match remaining box players to remaining CSV names by
        # normalized-name similarity (handles "(YYYY)" suffixes and nicknames like Sammy/Sam).
        rem_cand = [i for i, u in enumerate(used) if not u]
        scores = []
        for pi, (pid, plain) in enumerate(pending):
            np = _norm(plain)
            for ci in rem_cand:
                nc = _norm(cands[ci]["hh"])
                if np == nc:
                    sc = 1.0
                elif np.split()[-1:] == nc.split()[-1:]:   # same last name
                    sc = difflib.SequenceMatcher(None, np, nc).ratio()
                else:
                    sc = 0.0
                if sc >= 0.6:
                    scores.append((sc, pi, ci))
        scores.sort(reverse=True)
        pdone, cdone = set(), set()
        for sc, pi, ci in scores:
            if pi in pdone or ci in cdone:
                continue
            pdone.add(pi); cdone.add(ci)
            pid_hh[pending[pi][0]][cands[ci]["hh"]] += 1
        for pi, (pid, plain) in enumerate(pending):
            if pi not in pdone:
                unmatched[(team, yr)] += 1

    # build overrides: only where the resolved HH name differs from disp(plain)
    alias = {}
    for pid, hhc in pid_hh.items():
        hh = hhc.most_common(1)[0][0]
        plain = pid_plain[pid].most_common(1)[0][0]
        if hh and hh != disp(plain):
            alias[pid] = hh

    # collision check: same PLAIN name shared by 2+ personIds where some never matched a CSV
    # name. Those are the ones whose stats could still merge in the app.
    plain_to_pids = defaultdict(set)
    for pid, cnt in pid_plain.items():
        plain_to_pids[cnt.most_common(1)[0][0]].add(pid)
    missed = []
    for plain, pids in plain_to_pids.items():
        if len(pids) < 2:
            continue
        for pid in pids:
            if pid not in pid_hh:
                missed.append((plain, pid))

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(alias.items(), key=lambda x: int(x[0]))), f,
                  ensure_ascii=False, indent=0)

    print(f"box files: {len(files)} | personIds seen: {len(pid_plain)}")
    print(f"overrides written: {len(alias)}  ->  {args.out}")
    tot_unmatched = sum(unmatched.values())
    if tot_unmatched:
        print(f"unmatched box players (no confident CSV name): {tot_unmatched} "
              f"across {len(unmatched)} team-seasons")
    if missed:
        print(f"\nMISSED COLLISIONS ({len(missed)}) - same-named players with no CSV match, "
              f"stats may still merge for these:")
        for plain, pid in sorted(missed)[:50]:
            print(f"   {plain}  (personId {pid})")
        if len(missed) > 50:
            print(f"   ... and {len(missed) - 50} more")
        # also drop the worst team-seasons so a code mismatch is easy to spot
        worst = sorted(unmatched.items(), key=lambda x: -x[1])[:8]
        print("worst team-seasons by unmatched count (a big one often = a team-code mismatch):")
        for (team, yr), n in worst:
            print(f"   {team} {yr}: {n}")
    else:
        print("collision check: every same-named player resolved to a distinct name \u2713")


if __name__ == "__main__":
    main()
