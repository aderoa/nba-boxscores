#!/usr/bin/env python3
"""
check_freshness.py — is the archive still being fed?

update_boxscores.py moved off GitHub Actions on 2026-10-10, because the only
surviving source (stats.nba.com leaguegamelog) refuses datacenter IPs while
cdn.nba.com now refuses everyone. It runs on Alberto's machine instead.

That trades one silent failure for another. The old one was a 403 on a green
run. The new one is simpler: nobody runs it. A laptop that stays off for a
fortnight in January produces exactly the same repo as a laptop that is
working perfectly during the off-season — no commits either way.

So this runs in the cloud, where the schedule is reliable, needs no network,
and does the one thing the runner still can: read state.json and complain
when it falls behind during a season.

    python scripts/check_freshness.py          exit 1 if stale in-season
    STALE_DAYS=7 python scripts/check_freshness.py

Off-season it always passes: there is nothing to ingest between July and
mid-October, and an alarm that cries all summer is an alarm nobody reads.
"""

import os
import sys
import json
from datetime import date, datetime

# The window where the archive SHOULD be growing. It starts the day after
# the regular-season opener, not at preseason: exhibition games are
# deliberately not ingested, so between early October and opening night the
# archive is correctly idle and an alarm then would be a false one.
SEASON_START = (10, 22)
SEASON_END = (6, 30)
DEFAULT_STALE_DAYS = 3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def in_season(d):
    md = (d.month, d.day)
    return md >= SEASON_START or md <= SEASON_END


def main():
    stale_days = int(os.environ.get("STALE_DAYS") or DEFAULT_STALE_DAYS)
    path = os.path.join(ROOT, "state.json")
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except Exception as e:                                  # noqa: BLE001
        print(f"::error::cannot read state.json ({e})")
        return 1

    lp = state.get("last_processed_date")
    if not lp:
        print("::error::state.json has no last_processed_date")
        return 1
    try:
        last = datetime.strptime(lp, "%Y-%m-%d").date()
    except ValueError:
        print(f"::error::last_processed_date is not a date: {lp!r}")
        return 1

    today = date.today()
    behind = (today - last).days
    season = in_season(today)
    print(f"last_processed_date={lp}  {behind} day(s) behind  "
          f"{'in season' if season else 'off-season'}")

    if not season:
        print("off-season — nothing to ingest, so nothing to report")
        return 0
    if behind <= stale_days:
        print(f"fresh (threshold {stale_days} day(s))")
        return 0

    print(f"::error::box score archive is {behind} days behind during the "
          f"season (last {lp}). Run update_boxscores.py locally — it cannot "
          f"run here: stats.nba.com refuses datacenter IPs and cdn.nba.com "
          f"refuses everyone since 2026-10-10.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
