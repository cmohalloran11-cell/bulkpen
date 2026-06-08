"""
BULKPEN — server-side scoring layer
===================================
A faithful Python port of the heuristics that used to live only in
bulkpen.html's JavaScript. Turns the frontend into a thin client: it can
fetch one /api/predict/<date> payload and just render it.

Data layer (hybrid, by design):
  * python-mlb-statsapi  -> season pitching stats, game logs, team rosters
                            (the structured inputs the scoring actually needs).
  * raw schedule JSON    -> today's probable pitchers. The library's get_game
                            currently fails Pydantic validation on live MLB
                            data (gameData.absChallenges changed list->dict),
                            and its schedule model drops probablePitcher
                            entirely, so this one field comes straight from
                            statsapi.mlb.com like the frontend already does.

Every scoring function mirrors its JS counterpart line for line, including
Math.round's round-half-up behaviour (Python's round() is banker's rounding —
a silent off-by-one if you don't account for it).
"""
import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests

try:
    import mlbstatsapi
    MLB = mlbstatsapi.Mlb()
except Exception as e:                # library optional; endpoint reports it
    MLB = None
    print("note: python-mlb-statsapi not loaded:", e)

UPSTREAM = "https://statsapi.mlb.com/api/v1"
HEADERS = {"User-Agent": "Bulkpen/1.0 (predict)"}

# ----------------------------------------------------------- TTL caching -----
# Live stats drift through the day (game logs especially), so caches expire
# rather than living forever. Repeat loads of a date inside the window are
# near-instant; cross-date reuse (a pitcher appearing on several dates) is free
# until its entry ages out. Each entry is (expires_at_monotonic, value).
STAT_TTL = 600       # season stats / game logs / rosters: refresh every 10 min
PAYLOAD_TTL_LIVE = 300    # whole payload for today/future: 5 min
PAYLOAD_TTL_PAST = 86400  # whole payload for a past (settled) date: 24 h

_season_cache = {}   # (season, pid)            -> (exp, normalized season stat)
_log_cache = {}      # (season, pid, ref_iso)   -> (exp, availability dict)
_roster_cache = {}   # team_id                  -> (exp, [(pid, name), ...])
_payload_cache = {}  # date_str                 -> (exp, full predict payload)


def _cache_get(store, key):
    entry = store.get(key)
    if entry is None:
        return None
    if entry[0] <= time.monotonic():
        store.pop(key, None)        # expired
        return None
    return entry[1]


def _cache_set(store, key, value, ttl):
    store[key] = (time.monotonic() + ttl, value)
    return value


def _payload_ttl(date_str):
    """Past dates are settled (final games) -> cache long; today/future -> short."""
    try:
        if datetime.strptime(date_str, "%Y-%m-%d").date() < datetime.now().date():
            return PAYLOAD_TTL_PAST
    except ValueError:
        pass
    return PAYLOAD_TTL_LIVE


# ---------------------------------------------------------------- helpers ----
def jround(x):
    """Match JavaScript's Math.round (round half up), not Python's banker's rounding."""
    return math.floor(x + 0.5)


def clamp(x, lo=0, hi=100):
    return max(lo, min(hi, x))


def ip_to_innings(s):
    """MLB stores innings as .1 = 1/3, .2 = 2/3 (e.g. "61.1" = 61 1/3). Port of ipToInnings."""
    if s is None:
        return 0.0
    try:
        f = float(s)
    except (TypeError, ValueError):
        return 0.0
    w = math.floor(f)
    fr = jround((f - w) * 10)
    return w + fr / 3.0


# ------------------------------------------------------------ data layer -----
def get_season_stat(pid, name, season):
    """Season pitching line for one pitcher, via python-mlb-statsapi (cached)."""
    key = (season, pid)
    hit = _cache_get(_season_cache, key)
    if hit is not None:
        return hit
    val = {"name": name, "empty": True}
    if MLB is not None:
        try:
            s = MLB.get_player_stats(pid, stats=["season"], groups=["pitching"])
            season_obj = s.get("pitching", {}).get("season")
            splits = getattr(season_obj, "splits", None) or []
            if splits and getattr(splits[0].stat, "games_played", None):
                st = splits[0].stat
                val = {
                    "name": name,
                    "g": st.games_played or 0,
                    "gs": st.games_started or 0,
                    "ip": ip_to_innings(st.innings_pitched),
                    "era": getattr(st, "era", None),
                    "empty": False,
                }
        except Exception:
            pass
    return _cache_set(_season_cache, key, val, STAT_TTL)


def get_avail(pid, season, ref):
    """Availability from recent game logs, via python-mlb-statsapi (cached)."""
    key = (season, pid, ref.isoformat())
    hit = _cache_get(_log_cache, key)
    if hit is not None:
        return hit
    splits = []
    if MLB is not None:
        try:
            s = MLB.get_player_stats(pid, stats=["gameLog"], groups=["pitching"])
            gl = s.get("pitching", {}).get("gameLog")
            splits = getattr(gl, "splits", None) or []
        except Exception:
            splits = []
    val = compute_avail(splits, ref)
    return _cache_set(_log_cache, key, val, STAT_TTL)


def team_pitchers(team_id):
    """Active-roster pitcher (pid, name) pairs for a team, via python-mlb-statsapi (cached)."""
    hit = _cache_get(_roster_cache, team_id)
    if hit is not None:
        return hit
    ids = []
    if MLB is not None:
        try:
            roster = MLB.get_team_roster(team_id)
            ids = [
                (p.id, p.full_name)
                for p in roster
                if getattr(p.primary_position, "abbreviation", None) == "P"
            ]
        except Exception:
            ids = []
    return _cache_set(_roster_cache, team_id, ids, STAT_TTL)


def get_slate(date_str):
    """Schedule + probables straight from statsapi JSON (the field the library drops)."""
    url = (f"{UPSTREAM}/schedule?sportId=1&date={date_str}"
           "&hydrate=probablePitcher,team,linescore")
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    j = r.json()
    dates = j.get("dates") or []
    games = dates[0].get("games", []) if dates else []
    games.sort(key=lambda g: g.get("gameDate", ""))
    return games


# -------------------------------------------------------------- scoring ------
def opener_score(stat, season):
    """Port of openerScore: 0-100 opener risk from a starter's season line."""
    if not stat or stat.get("empty"):
        return {"score": None, "label": f"No {season} data", "cls": ""}
    g, gs, ip = stat["g"], stat["gs"], stat["ip"]
    if g == 0:
        return {"score": None, "label": f"No {season} data", "cls": ""}
    ip_app = ip / g
    score = 100 - (ip_app - 1) * 22
    if (g - gs) / g > 0.5:
        score += 10
    if gs == 0:
        score += 22
    score = clamp(jround(score))
    label, cls = "Traditional start", "green"
    if score >= 70:
        label, cls = "Likely opener", "red"
    elif score >= 45:
        label, cls = "Possible opener", "gold"
    return {"score": score, "label": label, "cls": cls,
            "ipApp": ip_app, "gs": gs, "g": g, "ip": ip}


def bulk_score(stat):
    """Port of bulkScore: reliever fit for soaking up bulk innings, or None if not a fit."""
    if not stat or stat.get("empty"):
        return None
    g, gs, ip = stat["g"], stat["gs"], stat["ip"]
    if g < 2:
        return None
    ip_app = ip / g
    # Anyone who starts more than half his games is a rotation arm, not a day-to-day
    # bulk reliever. (Backtest finding: the old gs/g>0.6 AND ip_app>4 guard let
    # struggling/short starters through and they over-ranked everyone.)
    if gs / g > 0.5 and ip_app > 3:
        return None
    score = clamp(jround(ip_app * 38 + (12 if gs > 0 else 0) + min(ip, 40) * 0.4))
    return {"base": score, "ipApp": ip_app, "g": g, "gs": gs, "ip": ip,
            "name": stat["name"], "era": stat.get("era"),
            "swing": gs > 0, "multiInning": ip_app >= 1.6}


def compute_avail(splits, ref):
    """Port of computeAvail: last-7-day workload + days of rest -> availability label/adj."""
    ip_last7 = ip_last3 = 0.0
    last_date = None
    last_ip = 0.0
    apps7 = 0
    last_start = None                      # most recent game he STARTED
    for sp in splits:
        d = getattr(sp, "date", None)
        if not d:
            continue
        try:
            dd = datetime.strptime(d, "%Y-%m-%d").date()
        except ValueError:
            continue
        diff = (ref - dd).days
        if diff < 0:                       # future of the reference date
            continue
        ip = ip_to_innings(getattr(sp.stat, "innings_pitched", None))
        if diff <= 7:
            ip_last7 += ip
            apps7 += 1
        if diff <= 3:
            ip_last3 += ip
        if last_date is None or dd > last_date:
            last_date = dd
            last_ip = ip
        if (getattr(sp.stat, "games_started", 0) or 0) and (last_start is None or dd > last_start):
            last_start = dd
    rest_days = (ref - last_date).days if last_date else None
    # A start in the last ~6 days = active rotation member on his rest cycle, not
    # available as today's bulk reliever. (Backtest finding: rotation/swing arms
    # with high IP/app kept topping the list on days they weren't even pitching.)
    days_since_start = (ref - last_start).days if last_start else None
    started_recently = days_since_start is not None and days_since_start <= 6
    label, cls, adj = "Available", "", 0
    if rest_days is None:
        label, cls, adj = "No recent logs", "", 0
    elif (rest_days <= 1 and last_ip >= 1.8) or ip_last3 >= 3.5:
        label, cls, adj = "Gassed", "warn", -26
    elif rest_days <= 1 or ip_last7 >= 4:
        label, cls, adj = "Worked recently", "warn", -12
    elif rest_days >= 3:
        label, cls, adj = "Fresh", "green", 6
    return {"ipLast7": round(ip_last7 * 10) / 10, "ipLast3": ip_last3,
            "restDays": rest_days, "lastIP": last_ip, "apps7": apps7,
            "daysSinceStart": days_since_start, "startedRecently": started_recently,
            "label": label, "cls": cls, "adj": adj}


# --------------------------------------------------- candidate assembly ------
def team_relievers(team_id, season, ex=None):
    """Port of teamRelievers: rostered pitchers -> bulkScore -> sorted by base desc."""
    ids = team_pitchers(team_id)
    if ex is not None:
        list(ex.map(lambda t: get_season_stat(t[0], t[1], season), ids))
    cands = []
    for pid, name in ids:
        b = bulk_score(get_season_stat(pid, name, season))
        if b:
            cands.append({"id": pid, "b": b})
    cands.sort(key=lambda c: -c["b"]["base"])
    return cands


def rank_with_usage(cands, season, ref, ex=None):
    """Port of rankWithUsage: proj = clamp(base + availability adj), sorted by proj desc."""
    if ex is not None:
        list(ex.map(lambda c: get_avail(c["id"], season, ref), cands))
    for c in cands:
        c["av"] = get_avail(c["id"], season, ref)
        # Penalize (don't outright drop) arms who started in the last ~6 days: they're
        # likely rotation members, so they sink below true relievers but stay eligible
        # if there's no better option.
        pen = 25 if c["av"].get("startedRecently") else 0
        c["proj"] = clamp(c["b"]["base"] + c["av"]["adj"] - pen)
    cands.sort(key=lambda c: -c["proj"])
    return cands


def cand_dict(c):
    """Serialize a ranked candidate for the JSON payload."""
    s = c["b"]
    av = c.get("av", {})
    return {
        "id": c["id"],
        "name": s["name"],
        "proj": c.get("proj", s["base"]),
        "base": s["base"],
        "ipApp": round(s["ipApp"], 2),
        "g": s["g"], "gs": s["gs"], "ip": round(s["ip"], 1),
        "era": s["era"], "swing": s["swing"], "multiInning": s["multiInning"],
        "avail": {
            "label": av.get("label", ""), "cls": av.get("cls", ""),
            "ipLast7": av.get("ipLast7"), "restDays": av.get("restDays"),
        },
    }


# ------------------------------------------------------------- top level -----
def _status_info(g):
    st = g.get("status", {})
    abstract = st.get("abstractGameState")
    detailed = st.get("detailedState", "")
    if abstract == "Live":
        return {"abstract": abstract, "detailed": detailed, "live": True, "text": detailed or "Live"}
    if abstract == "Final":
        return {"abstract": abstract, "detailed": detailed, "live": False, "text": "Final"}
    text = detailed
    gd = g.get("gameDate")
    if gd:
        try:
            text = datetime.fromisoformat(gd.replace("Z", "+00:00")).strftime("%H:%M UTC")
        except ValueError:
            pass
    return {"abstract": abstract, "detailed": detailed, "live": False, "text": text}


def predict(date_str, fresh=False):
    """Build the full board + leaderboard payload for a date (YYYY-MM-DD).

    Cached per date (see _payload_ttl); pass fresh=True to bypass and recompute
    (the page's Refresh button does this).
    """
    if not fresh:
        hit = _cache_get(_payload_cache, date_str)
        if hit is not None:
            return {**hit, "cached": True}

    season = int(date_str[:4])
    ref = datetime.strptime(date_str, "%Y-%m-%d").date()
    games = get_slate(date_str)

    board, leaderboard = [], []
    with ThreadPoolExecutor(max_workers=8) as ex:
        # Prefetch probable-pitcher season stats (drives opener scoring) concurrently.
        probables = {(g["teams"][side]["probablePitcher"]["id"],
                      g["teams"][side]["probablePitcher"].get("fullName", ""))
                     for g in games for side in ("away", "home")
                     if g["teams"][side].get("probablePitcher")}
        list(ex.map(lambda t: get_season_stat(t[0], t[1], season), probables))
        # Any pitcher listed to start ANY game today is unavailable as a bulk reliever
        # (he's in a rotation, not the bullpen) — exclude the whole slate's probables.
        probable_ids = {pid for pid, _ in probables}

        for g in games:
            teams = g["teams"]
            entry = {
                "gamePk": g.get("gamePk"),
                "status": _status_info(g),
                "candidates": {},
            }
            for side in ("away", "home"):
                t = teams[side]
                team = t.get("team", {})
                pp = t.get("probablePitcher")
                if pp:
                    opener = opener_score(get_season_stat(pp["id"], pp.get("fullName", ""), season), season)
                else:
                    opener = {"score": 70, "label": "Bullpen game (TBD)", "cls": "red"}

                entry[side] = {
                    "abbr": team.get("abbreviation") or (team.get("name", "")[:3].upper()),
                    "name": team.get("name"),
                    "teamId": team.get("id"),
                    "probable": ({"id": pp["id"], "name": pp.get("fullName"), "opener": opener}
                                 if pp else None),
                }

                score = opener["score"]
                # Per-game candidates: skip only a clear traditional starter (mirrors predictGame).
                if score is not None and score < 45:
                    continue
                team_id = team.get("id")
                if team_id is None:
                    continue
                cands = team_relievers(team_id, season, ex)
                cands = [c for c in cands if c["id"] not in probable_ids][:6]
                cands = rank_with_usage(cands, season, ref, ex)[:3]
                if not cands:
                    continue
                entry["candidates"][side] = [cand_dict(c) for c in cands]

                # Leaderboard excludes no-data probables (mirrors predictSlate).
                lead_ok = (pp is None) or (score is not None and score >= 45)
                if lead_ok:
                    opp = (teams["away"]["team"].get("abbreviation") if side == "home"
                           else teams["home"]["team"].get("abbreviation"))
                    leaderboard.append({
                        "teamAbbr": entry[side]["abbr"],
                        "opp": opp,
                        "openerLabel": opener["label"],
                        "starter": pp.get("fullName") if pp else "TBD",
                        "best": cand_dict(cands[0]),
                    })
            board.append(entry)

    leaderboard.sort(key=lambda r: -r["best"]["proj"])
    for i, r in enumerate(leaderboard):
        r["rank"] = i + 1

    payload = {
        "date": date_str,
        "season": season,
        "gameCount": len(games),
        "dataLayer": "python-mlb-statsapi (stats/roster) + raw schedule JSON (probables)",
        "libraryLoaded": MLB is not None,
        "games": board,
        "leaderboard": leaderboard,
        "cached": False,
    }
    _cache_set(_payload_cache, date_str, payload, _payload_ttl(date_str))
    return payload
