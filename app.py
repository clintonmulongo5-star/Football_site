"""
app.py — Football Form Dashboard (standalone, single-file version)
---------------------------------------------------------------------
Everything (data fetching, storage, Poisson prediction, and the web
routes) lives in this one file. This avoids a Pydroid quirk where it
sometimes fails to import a second local file (predictor.py) even
when it's sitting right next to this script.

Run locally:
    pip install flask requests
    python app.py
Then open http://127.0.0.1:5000 in a browser on the same device.

Environment variables:
    FOOTBALL_DATA_API_KEY  football-data.org key (European leagues etc.)
    API_FOOTBALL_KEY       optional; API-Football key that turns on the
                           local East African leagues (Kenya/Tanzania/Uganda)
"""

import os
import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, render_template_string, redirect, url_for

app = Flask(__name__)

# ---------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------
API_KEY = os.environ.get("FOOTBALL_DATA_API_KEY", "")
if not API_KEY:
    print("WARNING: FOOTBALL_DATA_API_KEY is not set. Set it as an "
          "environment variable before running (see README/deployment notes).")
BASE_URL = "https://api.football-data.org/v4"
HEADERS = {"X-Auth-Token": API_KEY}
DB_PATH = os.environ.get("FOOTBALL_DB_PATH", "football_data.db")
COMPETITIONS = ["PL", "PD", "SA", "BL1", "FL1", "CL", "DED", "PPL", "ELC", "BSA", "WC", "EC"]

# --- Local (East African) leagues -------------------------------------
# football-data.org's free plan has no African leagues, so these come from
# API-Football (https://www.api-football.com). Set API_FOOTBALL_KEY to turn
# them on; without it the app runs exactly as before with the 12 above.
# NOTE: API-Football's FREE plan only serves old seasons (2022-2024), so
# current fixtures need a paid plan (Pro).
API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY", "")
API_FOOTBALL_URL = "https://v3.football.api-sports.io"
API_FOOTBALL_HEADERS = {"x-apisports-key": API_FOOTBALL_KEY}

# code -> how to find the league. The league id is looked up automatically
# by country + name and cached in the database. If a league is missing or
# the wrong one gets picked, put its API-Football id in "id" to force it.
LOCAL_LEAGUES = {
    "KEN": {"country": "Kenya",    "name": "Kenya Premier League",
            "match": ("premier league",),          "id": None},
    "TAN": {"country": "Tanzania", "name": "Tanzania Premier League",
            "match": ("ligi kuu", "premier league"), "id": None},
    "UGA": {"country": "Uganda",   "name": "Uganda Premier League",
            "match": ("premier league",),          "id": None},
}

# API-Football fixture ids could clash with football-data.org match ids
# (both are plain integers in one table), so local ids get a big offset.
LOCAL_ID_OFFSET = 1_000_000_000

# API-Football short status -> the status names the rest of the app uses
API_FOOTBALL_STATUS = {
    "NS": "TIMED", "TBD": "TIMED",
    "FT": "FINISHED", "AET": "FINISHED", "PEN": "FINISHED",
    "1H": "IN_PLAY", "HT": "PAUSED", "2H": "IN_PLAY", "ET": "IN_PLAY",
    "BT": "PAUSED", "P": "IN_PLAY", "LIVE": "IN_PLAY", "INT": "PAUSED",
    "PST": "POSTPONED", "CANC": "CANCELLED", "SUSP": "SUSPENDED",
    "ABD": "CANCELLED", "AWD": "AWARDED", "WO": "AWARDED",
}

COMP_NAMES = {
    "PL": "Premier League",
    "PD": "La Liga",
    "SA": "Serie A",
    "BL1": "Bundesliga",
    "FL1": "Ligue 1",
    "CL": "Champions League",
    "DED": "Eredivisie",
    "PPL": "Primeira Liga",
    "ELC": "Championship",
    "BSA": "Brasileiro Série A",
    "WC": "World Cup",
    "EC": "European Championship",
    "KEN": "Kenya Premier League",
    "TAN": "Tanzania Premier League",
    "UGA": "Uganda Premier League",
}

# East Africa Time (Nairobi, Kampala, Dar es Salaam, Addis Ababa): UTC+3,
# no daylight saving. Data is still stored in UTC; only display is converted.
EAT = timezone(timedelta(hours=3), "EAT")


def utc_str_to_eat(utc_str):
    """Convert an ISO UTC string like '2026-09-19T15:00:00Z' (or with
    '+00:00' / microseconds) into a datetime in East Africa Time."""
    dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(EAT)



# ---------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY,
            competition TEXT,
            utc_date TEXT,
            status TEXT,
            home_team TEXT,
            away_team TEXT,
            home_score INTEGER,
            away_score INTEGER,
            home_ht_score INTEGER,
            away_ht_score INTEGER,
            fetched_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS update_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ran_at TEXT,
            summary TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS local_league_ids (
            code TEXT PRIMARY KEY,
            league_id INTEGER,
            name TEXT
        )
    """)

    # Migration: earlier versions of this app didn't store half-time
    # scores. Add the columns if they're missing so existing databases
    # (with match history already collected) don't break.
    cur.execute("PRAGMA table_info(matches)")
    existing_cols = {row[1] for row in cur.fetchall()}
    if "home_ht_score" not in existing_cols:
        cur.execute("ALTER TABLE matches ADD COLUMN home_ht_score INTEGER")
    if "away_ht_score" not in existing_cols:
        cur.execute("ALTER TABLE matches ADD COLUMN away_ht_score INTEGER")

    conn.commit()
    return conn


# ---------------------------------------------------------------------
# DATA FETCHING
# ---------------------------------------------------------------------
def fetch_matches(competition_code, date_from=None, date_to=None):
    params = {}
    if date_from:
        params["dateFrom"] = date_from
    if date_to:
        params["dateTo"] = date_to

    url = f"{BASE_URL}/competitions/{competition_code}/matches"
    resp = requests.get(url, headers=HEADERS, params=params, timeout=15)

    if resp.status_code != 200:
        raise RuntimeError(f"{competition_code}: {resp.status_code} — {resp.text[:200]}")

    return resp.json().get("matches", [])


def store_matches(conn, matches, competition_code):
    cur = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    for m in matches:
        home_score = m["score"]["fullTime"].get("home")
        away_score = m["score"]["fullTime"].get("away")
        ht = m["score"].get("halfTime", {}) or {}
        home_ht_score = ht.get("home")
        away_ht_score = ht.get("away")
        cur.execute("""
            INSERT OR REPLACE INTO matches
            (id, competition, utc_date, status, home_team, away_team,
             home_score, away_score, home_ht_score, away_ht_score, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            m["id"], competition_code, m["utcDate"], m["status"],
            m["homeTeam"]["name"], m["awayTeam"]["name"],
            home_score, away_score, home_ht_score, away_ht_score, now
        ))
    conn.commit()


def af_get(endpoint, params):
    """Call API-Football. It returns HTTP 200 even for plan/quota errors and
    reports them in an 'errors' field, so check that too."""
    resp = requests.get(f"{API_FOOTBALL_URL}/{endpoint}",
                        headers=API_FOOTBALL_HEADERS, params=params, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f"API-Football {endpoint}: {resp.status_code} — {resp.text[:200]}")
    data = resp.json()
    if data.get("errors"):
        raise RuntimeError(f"API-Football {endpoint}: {str(data['errors'])[:200]}")
    return data.get("response", [])


def find_local_league(conn, code):
    """Return (league_id, league_name) for a LOCAL_LEAGUES code, looking it
    up once by country + name and caching it in the database."""
    cur = conn.cursor()
    cur.execute("SELECT league_id, name FROM local_league_ids WHERE code = ?", (code,))
    row = cur.fetchone()
    if row:
        return row

    cfg = LOCAL_LEAGUES[code]
    if cfg.get("id"):
        league_id, league_name = cfg["id"], cfg["name"]
    else:
        skip = ("women", "u17", "u19", "u20", "u21", "u23", "youth", "cup", "reserve")
        league_id = league_name = None
        for item in af_get("leagues", {"country": cfg["country"], "type": "league"}):
            name = item["league"]["name"]
            low = name.lower()
            if any(s in low for s in skip):
                continue
            if any(k in low for k in cfg["match"]):
                league_id, league_name = item["league"]["id"], name
                break
        if league_id is None:
            raise RuntimeError(
                f"Couldn't find the {cfg['country']} league automatically — "
                f"set its API-Football id in LOCAL_LEAGUES['{code}']['id']"
            )

    cur.execute(
        "INSERT OR REPLACE INTO local_league_ids (code, league_id, name) VALUES (?, ?, ?)",
        (code, league_id, league_name),
    )
    conn.commit()
    return league_id, league_name


def store_local_fixtures(conn, fixtures, code):
    cur = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    for fx in fixtures:
        f, teams = fx["fixture"], fx["teams"]
        score = fx.get("score") or {}
        goals = fx.get("goals") or {}
        ft = score.get("fulltime") or {}
        ht = score.get("halftime") or {}

        home_score = ft.get("home") if ft.get("home") is not None else goals.get("home")
        away_score = ft.get("away") if ft.get("away") is not None else goals.get("away")

        kickoff = datetime.fromisoformat(f["date"].replace("Z", "+00:00"))
        utc_date = kickoff.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        status = API_FOOTBALL_STATUS.get(f["status"]["short"], f["status"]["short"])

        cur.execute("""
            INSERT OR REPLACE INTO matches
            (id, competition, utc_date, status, home_team, away_team,
             home_score, away_score, home_ht_score, away_ht_score, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            LOCAL_ID_OFFSET + f["id"], code, utc_date, status,
            teams["home"]["name"], teams["away"]["name"],
            home_score, away_score, ht.get("home"), ht.get("away"), now
        ))
    conn.commit()


def update_local_leagues(conn, results):
    """Pull this season's and last season's fixtures for each local league.
    (Two seasons because leagues that run August-May straddle two calendar
    years, and last season gives the model form data early on.)
    Costs about 2 API-Football requests per league per refresh."""
    if not API_FOOTBALL_KEY:
        results["local"] = {"ok": False, "error": "API_FOOTBALL_KEY not set"}
        return

    this_year = datetime.now(timezone.utc).year
    for code in LOCAL_LEAGUES:
        try:
            league_id, league_name = find_local_league(conn, code)
            total = 0
            for season in (this_year, this_year - 1):
                fixtures = af_get("fixtures", {"league": league_id, "season": season})
                store_local_fixtures(conn, fixtures, code)
                total += len(fixtures)
            results[code] = {"ok": True, "count": total, "league": league_name}
        except Exception as e:
            results[code] = {"ok": False, "error": str(e)}


def daily_update(conn):
    """Pull the last 365 days of finished matches + next 7 days of fixtures.
    A full year (not just 90 days) matters early in a season: without it,
    teams only have a handful of matches logged and every confidence
    label reads 'very low'. Going back a year lets the model draw on
    last season's form too, once this season has few games played."""
    date_from = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%d")
    date_to = (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%d")

    results = {}
    for comp in COMPETITIONS:
        try:
            matches = fetch_matches(comp, date_from, date_to)
            store_matches(conn, matches, comp)
            results[comp] = {"ok": True, "count": len(matches)}
        except Exception as e:
            results[comp] = {"ok": False, "error": str(e)}

    update_local_leagues(conn, results)

    cur = conn.cursor()
    cur.execute(
        "INSERT INTO update_log (ran_at, summary) VALUES (?, ?)",
        (datetime.now(timezone.utc).isoformat(), str(results))
    )
    conn.commit()
    return results


def last_update_time(conn):
    cur = conn.cursor()
    cur.execute("SELECT ran_at FROM update_log ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------
# ANALYSIS + PREDICTION
# ---------------------------------------------------------------------
def get_team_form(conn, team_name, n_matches=10):
    cur = conn.cursor()
    cur.execute("""
        SELECT home_team, away_team, home_score, away_score, utc_date
        FROM matches
        WHERE status = 'FINISHED'
          AND (home_team = ? OR away_team = ?)
        ORDER BY utc_date DESC
        LIMIT ?
    """, (team_name, team_name, n_matches))
    rows = cur.fetchall()

    if not rows:
        return None

    scored, conceded = [], []
    for home, away, hs, as_, _ in rows:
        if home == team_name:
            scored.append(hs)
            conceded.append(as_)
        else:
            scored.append(as_)
            conceded.append(hs)

    return {
        "matches_analyzed": len(rows),
        "avg_scored": sum(scored) / len(scored),
        "avg_conceded": sum(conceded) / len(conceded),
    }


def get_team_ht_form(conn, team_name, n_matches=10):
    """Same idea as get_team_form, but using half-time scores. Only
    counts matches where half-time data was actually returned by the
    API (older/some matches may have nulls here)."""
    cur = conn.cursor()
    cur.execute("""
        SELECT home_team, away_team, home_ht_score, away_ht_score, utc_date
        FROM matches
        WHERE status = 'FINISHED'
          AND home_ht_score IS NOT NULL AND away_ht_score IS NOT NULL
          AND (home_team = ? OR away_team = ?)
        ORDER BY utc_date DESC
        LIMIT ?
    """, (team_name, team_name, n_matches))
    rows = cur.fetchall()

    if not rows:
        return None

    scored, conceded = [], []
    for home, away, hs, as_, _ in rows:
        if home == team_name:
            scored.append(hs)
            conceded.append(as_)
        else:
            scored.append(as_)
            conceded.append(hs)

    return {
        "matches_analyzed": len(rows),
        "avg_scored": sum(scored) / len(scored),
        "avg_conceded": sum(conceded) / len(conceded),
    }


def poisson_pmf(k, lam):
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def local_league_avg_goals(conn, comp, default=1.1, min_matches=30):
    """Goals per team per match in a local league, from its stored results.
    The model's default (1.4) suits the big European leagues; East African
    leagues score noticeably less (Kenya's 2025-26 season averaged ~1.1),
    and using 1.4 would overestimate every team's expected goals."""
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*), AVG(home_score + away_score)
        FROM matches
        WHERE competition = ? AND status = 'FINISHED'
          AND home_score IS NOT NULL AND away_score IS NOT NULL
    """, (comp,))
    n, avg_total = cur.fetchone()
    if n and n >= min_matches and avg_total:
        return avg_total / 2
    return default


def predict_match(conn, home_team, away_team, league_avg_goals=1.4, max_goals=6):
    home_form = get_team_form(conn, home_team)
    away_form = get_team_form(conn, away_team)

    if not home_form or not away_form:
        return {"error": "Not enough historical data for a reliable estimate."}

    home_attack = home_form["avg_scored"] / league_avg_goals
    home_defense = home_form["avg_conceded"] / league_avg_goals
    away_attack = away_form["avg_scored"] / league_avg_goals
    away_defense = away_form["avg_conceded"] / league_avg_goals

    home_expected = home_attack * away_defense * league_avg_goals * 1.1
    away_expected = away_attack * home_defense * league_avg_goals * 0.9

    home_win, draw, away_win = 0.0, 0.0, 0.0
    over_2_5, under_2_5 = 0.0, 0.0
    gg, ng = 0.0, 0.0
    scoreline_probs = {}

    for h in range(max_goals + 1):
        for a in range(max_goals + 1):
            p = poisson_pmf(h, home_expected) * poisson_pmf(a, away_expected)
            scoreline_probs[(h, a)] = p

            if h > a:
                home_win += p
            elif h == a:
                draw += p
            else:
                away_win += p

            if h + a > 2.5:
                over_2_5 += p
            else:
                under_2_5 += p

            if h > 0 and a > 0:
                gg += p
            else:
                ng += p

    total = home_win + draw + away_win
    min_matches = min(home_form["matches_analyzed"], away_form["matches_analyzed"])

    if min_matches < 5:
        confidence = "very-low"
    elif min_matches < 8:
        confidence = "low"
    elif min_matches < 12:
        confidence = "moderate"
    else:
        confidence = "higher"

    # Most likely single scoreline (just the single most probable grid
    # cell — real matches have huge variance, so treat this as a
    # "most likely of many options" figure, not a forecast to bank on).
    best_score, best_p = max(scoreline_probs.items(), key=lambda kv: kv[1])

    return {
        "home_team": home_team,
        "away_team": away_team,
        "matches_used_home": home_form["matches_analyzed"],
        "matches_used_away": away_form["matches_analyzed"],
        "confidence": confidence,
        "home_win_pct": round(100 * home_win / total, 1),
        "draw_pct": round(100 * draw / total, 1),
        "away_win_pct": round(100 * away_win / total, 1),
        "over_2_5_pct": round(100 * over_2_5 / total, 1),
        "under_2_5_pct": round(100 * under_2_5 / total, 1),
        "gg_pct": round(100 * gg / total, 1),
        "ng_pct": round(100 * ng / total, 1),
        "predicted_score": f"{best_score[0]}-{best_score[1]}",
        "predicted_score_pct": round(100 * best_p / total, 1),
    }


def get_upcoming_fixtures(conn, days_ahead=7):
    cur = conn.cursor()
    # "Today" starts at midnight East Africa Time, converted back to UTC
    # so it can be compared with the UTC dates stored in the database.
    start_eat = datetime.now(EAT).replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_eat.astimezone(timezone.utc)
    today = start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    future = (start_utc + timedelta(days=days_ahead)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur.execute("""
        SELECT home_team, away_team, utc_date, competition
        FROM matches
        WHERE status IN ('SCHEDULED', 'TIMED') AND utc_date BETWEEN ? AND ?
        ORDER BY utc_date ASC
    """, (today, future))
    return cur.fetchall()


# ---------------------------------------------------------------------
# WEB ROUTES
# ---------------------------------------------------------------------
def build_dashboard_data():
    conn = init_db()
    fixtures = get_upcoming_fixtures(conn)

    by_date = defaultdict(list)
    league_avgs = {}
    for home, away, utc_date, comp in fixtures:
        if comp in LOCAL_LEAGUES:
            if comp not in league_avgs:
                league_avgs[comp] = local_league_avg_goals(conn, comp)
            prediction = predict_match(conn, home, away, league_avg_goals=league_avgs[comp])
        else:
            prediction = predict_match(conn, home, away)
        kickoff = utc_str_to_eat(utc_date)
        day = kickoff.strftime("%Y-%m-%d")
        by_date[day].append({
            "home": home,
            "away": away,
            "competition": COMP_NAMES.get(comp, comp),
            "time": kickoff.strftime("%H:%M"),
            "prediction": prediction,
        })

    last_update = last_update_time(conn)
    if last_update:
        last_update = utc_str_to_eat(last_update).strftime("%Y-%m-%d %H:%M")
    conn.close()

    return dict(sorted(by_date.items())), last_update


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Match Form</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@600;700;800&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #0F1613;
  --surface: #16211C;
  --line: #263229;
  --text: #EAEFE7;
  --text-muted: #8CA093;
  --home: #3FA66B;
  --draw: #D9A441;
  --away: #B8564A;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: 'IBM Plex Sans', -apple-system, Segoe UI, Roboto, sans-serif;
  line-height: 1.5;
}
.site-header { border-bottom: 1px solid var(--line); padding: 2.25rem 1.5rem 1.75rem; }
.header-inner { max-width: 680px; margin: 0 auto; }
.site-header h1 {
  font-family: 'Archivo', -apple-system, Segoe UI, sans-serif;
  font-weight: 800; font-size: 1.9rem; letter-spacing: -0.01em;
  margin: 0 0 0.35rem; color: var(--home);
}
.tagline { color: var(--text-muted); margin: 0; font-size: 0.95rem; max-width: 46ch; }
main { max-width: 680px; margin: 0 auto; padding: 1.5rem 1.5rem 3rem; }
.status-bar {
  display: flex; align-items: center; justify-content: space-between; gap: 1rem;
  padding: 0.9rem 1rem; background: var(--surface); border: 1px solid var(--line);
  border-radius: 6px; margin-bottom: 1.75rem; flex-wrap: wrap;
}
.status-text { display: flex; flex-direction: column; font-size: 0.85rem; }
.status-label { color: var(--text-muted); }
.status-value { color: var(--text); font-weight: 500; }
.refresh-btn {
  background: var(--home); color: #0F1613; border: none;
  font-family: 'IBM Plex Sans', sans-serif; font-weight: 600; font-size: 0.88rem;
  padding: 0.55rem 1.1rem; border-radius: 5px; cursor: pointer;
}
.refresh-btn:active { opacity: 0.85; }
.empty-state { color: var(--text-muted); padding: 1.5rem 0; font-size: 0.95rem; }
.day-group { margin-bottom: 2rem; }
.day-heading {
  font-family: 'Archivo', sans-serif; font-weight: 700; font-size: 1.05rem;
  color: var(--text); border-bottom: 1px solid var(--line);
  padding-bottom: 0.5rem; margin: 0 0 0.75rem;
}
.match-row { padding: 0.9rem 0; border-bottom: 1px solid var(--line); }
.match-row:last-child { border-bottom: none; }
.match-meta { display: flex; justify-content: space-between; font-size: 0.78rem; color: var(--text-muted); margin-bottom: 0.3rem; }
.match-teams { font-family: 'Archivo', sans-serif; font-weight: 600; font-size: 1.02rem; margin-bottom: 0.55rem; }
.match-teams .vs { color: var(--text-muted); font-weight: 400; margin: 0 0.4rem; font-size: 0.85rem; }
.prob-bar { display: flex; height: 8px; border-radius: 4px; overflow: hidden; background: var(--line); margin-bottom: 0.45rem; }
.prob-bar .seg.home { background: var(--home); }
.prob-bar .seg.draw { background: var(--draw); }
.prob-bar .seg.away { background: var(--away); }
.prob-labels { display: flex; gap: 1.1rem; font-size: 0.82rem; color: var(--text-muted); margin-bottom: 0.4rem; }
.prob-labels b { color: var(--text); font-weight: 600; }
.extra-markets { display: flex; flex-direction: column; gap: 0.2rem; font-size: 0.8rem; color: var(--text-muted); margin-bottom: 0.5rem; }
.extra-markets b { color: var(--text); font-weight: 600; }
.extra-markets .market-dim { color: var(--text-muted); opacity: 0.75; }
.confidence { display: flex; align-items: center; gap: 0.45rem; font-size: 0.78rem; color: var(--text-muted); }
.confidence .dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
.confidence-very-low .dot { background: var(--away); }
.confidence-low .dot { background: var(--draw); }
.confidence-moderate .dot { background: #6FA8C7; }
.confidence-higher .dot { background: var(--home); }
.no-data { color: var(--text-muted); font-size: 0.85rem; font-style: italic; }
.disclaimer { margin-top: 2.5rem; padding-top: 1.25rem; border-top: 1px solid var(--line); color: var(--text-muted); font-size: 0.82rem; max-width: 56ch; }
@media (max-width: 480px) {
  .site-header { padding: 1.75rem 1.25rem 1.5rem; }
  main { padding: 1.25rem 1.25rem 2.5rem; }
}
</style>
</head>
<body>

<header class="site-header">
  <div class="header-inner">
    <h1>Match Form</h1>
    <p class="tagline">Win, draw and loss estimates built from each team's last few results.</p>
  </div>
</header>

<main>
  <section class="status-bar">
    <div class="status-text">
      {% if last_update %}
        <span class="status-label">Data last pulled</span>
        <span class="status-value">{{ last_update }} EAT</span>
      {% else %}
        <span class="status-label">No data yet</span>
        <span class="status-value">Run a refresh to pull fixtures</span>
      {% endif %}
    </div>
    <form action="{{ url_for('update') }}" method="post">
      <button type="submit" class="refresh-btn">Refresh data</button>
    </form>
  </section>

  {% if not has_data %}
    <div class="empty-state">
      <p>No fixtures loaded yet. Click <strong>Refresh data</strong> above to pull the next 7 days of matches and build today's estimates.</p>
    </div>
  {% endif %}

  {% for day, matches in by_date.items() %}
    <section class="day-group">
      <h2 class="day-heading">{{ day }}</h2>

      {% for m in matches %}
        <article class="match-row">
          <div class="match-meta">
            <span class="competition">{{ m.competition }}</span>
            <span class="kickoff">{{ m.time }}</span>
          </div>

          <div class="match-teams">
            <span class="team home">{{ m.home }}</span>
            <span class="vs">vs</span>
            <span class="team away">{{ m.away }}</span>
          </div>

          {% if m.prediction.error %}
            <p class="no-data">{{ m.prediction.error }}</p>
          {% else %}
            <div class="prob-bar">
              <div class="seg home" style="width: {{ m.prediction.home_win_pct }}%"></div>
              <div class="seg draw" style="width: {{ m.prediction.draw_pct }}%"></div>
              <div class="seg away" style="width: {{ m.prediction.away_win_pct }}%"></div>
            </div>

            <div class="prob-labels">
              <span><b>{{ m.prediction.home_win_pct }}%</b> home</span>
              <span><b>{{ m.prediction.draw_pct }}%</b> draw</span>
              <span><b>{{ m.prediction.away_win_pct }}%</b> away</span>
            </div>

            <div class="extra-markets">
              <span class="market"><b>{{ m.prediction.over_2_5_pct }}%</b> over 2.5 <span class="market-dim">/ {{ m.prediction.under_2_5_pct }}% under</span></span>
              <span class="market"><b>{{ m.prediction.gg_pct }}%</b> both score <span class="market-dim">/ {{ m.prediction.ng_pct }}% no</span></span>
              <span class="market">likeliest score <b>{{ m.prediction.predicted_score }}</b> <span class="market-dim">({{ m.prediction.predicted_score_pct }}%)</span></span>
            </div>

            <div class="confidence confidence-{{ m.prediction.confidence }}">
              <span class="dot"></span>
              <span class="confidence-text">
                {% if m.prediction.confidence == 'very-low' %}very low confidence — only {{ m.prediction.matches_used_home }}–{{ m.prediction.matches_used_away }} matches on record
                {% elif m.prediction.confidence == 'low' %}low confidence — limited match history
                {% elif m.prediction.confidence == 'moderate' %}moderate confidence
                {% else %}higher confidence — larger sample
                {% endif %}
              </span>
            </div>
          {% endif %}
        </article>
      {% endfor %}
    </section>
  {% endfor %}

  <footer class="disclaimer">
    <p>These are statistical estimates from recent scoring form, not guarantees. Small samples (early season, promoted teams) produce noisy numbers — check the confidence label before trusting a percentage.</p>
  </footer>
</main>

</body>
</html>"""


@app.route("/")
def index():
    by_date, last_update = build_dashboard_data()
    return render_template_string(
        PAGE_TEMPLATE,
        by_date=by_date,
        last_update=last_update,
        has_data=bool(by_date),
    )


@app.route("/update", methods=["POST"])
def update():
    conn = init_db()
    daily_update(conn)
    conn.close()
    return redirect(url_for("index"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
