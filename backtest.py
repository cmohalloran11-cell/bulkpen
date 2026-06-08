"""
BULKPEN backtest — did the bulk-reliever predictions actually pan out?

For each past date:
  1. Run predict(date) to get the predicted bulk relievers (per opener/bullpen side).
  2. Pull each game's real box score from statsapi.
  3. The ACTUAL bulk reliever = the non-starter who threw the most innings.
  4. Score predicted vs actual.

Caveat (stated honestly): predict() reads SEASON stats as they stand *today*, not as
of the backtest date, so there's mild lookahead. For dates a few days back the season
totals have barely moved, so it's a fair approximation — not a rigorous as-of backtest.

Usage:  python backtest.py 2026-06-05 2026-06-04 2026-06-03
"""
import sys
from concurrent.futures import ThreadPoolExecutor

import requests

import predict

try:
    sys.stdout.reconfigure(encoding="utf-8")   # so accented names render in the console
except Exception:
    pass

H = {"User-Agent": "Bulkpen/1.0 (backtest)"}
OPENER_IP = 2.0     # starter going <= this many innings => opener/bullpen game
BULK_IP = 2.0       # a reliever going >= this many innings => a real bulk outing


def boxscore(pk):
    r = requests.get(f"https://statsapi.mlb.com/api/v1/game/{pk}/boxscore", headers=H, timeout=20)
    r.raise_for_status()
    return r.json()


def team_actuals(box_side):
    """From a boxscore team side: starter IP, and relievers sorted by IP desc."""
    order = box_side.get("pitchers", [])
    players = box_side.get("players", {})
    def line(pid):
        p = players.get(f"ID{pid}", {})
        pit = p.get("stats", {}).get("pitching", {})
        return {"id": pid,
                "name": p.get("person", {}).get("fullName", str(pid)),
                "ip": predict.ip_to_innings(pit.get("inningsPitched"))}
    if not order:
        return None
    starter = line(order[0])
    relievers = sorted([line(pid) for pid in order[1:]], key=lambda x: -x["ip"])
    return {"starter": starter, "relievers": relievers,
            "bulk": relievers[0] if relievers else None}


def run_date(date_str):
    payload = predict.predict(date_str, fresh=True)
    games = payload["games"]
    pks = [g["gamePk"] for g in games]
    with ThreadPoolExecutor(max_workers=8) as ex:
        boxes = dict(zip(pks, ex.map(lambda pk: _safe_box(pk), pks)))

    rows = []          # one per predicted side
    missed = []        # actual opener/bullpen sides we made NO prediction for
    for g in games:
        box = boxes.get(g["gamePk"])
        if not box:
            continue
        for side in ("away", "home"):
            actual = team_actuals(box["teams"][side])
            if not actual:
                continue
            preds = (g.get("candidates") or {}).get(side) or []
            starter_ip = actual["starter"]["ip"]
            was_opener = starter_ip <= OPENER_IP
            bulk = actual["bulk"]

            if preds:
                pred_ids = [c["id"] for c in preds]
                actual_rank = (pred_ids.index(bulk["id"]) + 1) if (bulk and bulk["id"] in pred_ids) else None
                top1 = bool(bulk and preds[0]["id"] == bulk["id"])
                pitched = next((c for c in preds if bulk and c["id"] == bulk["id"]), None)
                rows.append({
                    "date": date_str, "team": g[side]["abbr"],
                    "starter": (g[side]["probable"] or {}).get("name", "TBD"),
                    "openerLabel": (g[side]["probable"] or {}).get("opener", {}).get("label", "Bullpen game"),
                    "pred1": preds[0]["name"], "pred1_proj": preds[0]["proj"],
                    "actualBulk": bulk["name"] if bulk else "—",
                    "actualBulkIP": round(bulk["ip"], 1) if bulk else 0,
                    "starterIP": round(starter_ip, 1),
                    "wasOpener": was_opener,
                    "top1": top1, "rankOfActual": actual_rank,
                    "predBulkActuallyPitched": any(
                        f"ID{c['id']}" in box["teams"][side].get("players", {}) and
                        predict.ip_to_innings(box["teams"][side]["players"][f"ID{c['id']}"]
                                              .get("stats", {}).get("pitching", {}).get("inningsPitched")) > 0
                        for c in preds[:1]),
                })
            elif was_opener and bulk and bulk["ip"] >= BULK_IP:
                # a real opener/bullpen game we did NOT flag (coverage gap)
                missed.append({"date": date_str, "team": g[side]["abbr"],
                               "starterIP": round(starter_ip, 1),
                               "actualBulk": bulk["name"], "actualBulkIP": round(bulk["ip"], 1)})
    return rows, missed


def _safe_box(pk):
    try:
        return boxscore(pk)
    except Exception:
        return None


def main(dates):
    all_rows, all_missed = [], []
    for d in dates:
        rows, missed = run_date(d)
        all_rows += rows
        all_missed += missed

    print("=" * 78)
    print("BULKPEN BACKTEST — predicted bulk reliever vs. who actually ate the innings")
    print("=" * 78)
    for r in all_rows:
        mark = "HIT " if r["top1"] else ("top3" if r["rankOfActual"] else ("pen " if r["predBulkActuallyPitched"] else "MISS"))
        opener = "OPENER" if r["wasOpener"] else f"SP {r['starterIP']}ip"
        print(f"[{mark}] {r['date']} {r['team']:<4} behind {r['starter'][:18]:<18} ({opener})")
        print(f"        predicted: {r['pred1']:<22} (proj {r['pred1_proj']})")
        print(f"        actual   : {r['actualBulk']:<22} ({r['actualBulkIP']} IP relief)")

    n = len(all_rows)
    if n:
        hits = sum(r["top1"] for r in all_rows)
        top3 = sum(1 for r in all_rows if r["rankOfActual"])
        pen = sum(r["predBulkActuallyPitched"] for r in all_rows)
        opener_rows = [r for r in all_rows if r["wasOpener"]]
        oh = sum(r["top1"] for r in opener_rows)
        print("\n" + "-" * 78)
        print(f"Predictions made: {n}")
        print(f"  Exact #1 hit (our top pick WAS the longest reliever): {hits}/{n}  ({100*hits//n}%)")
        print(f"  Actual bulk arm in our top-3:                         {top3}/{n}  ({100*top3//n}%)")
        print(f"  Our #1 pick pitched in relief at all:                 {pen}/{n}  ({100*pen//n}%)")
        if opener_rows:
            print(f"  On games that were TRUE opener/bullpen games:         {oh}/{len(opener_rows)} exact #1")
    if all_missed:
        print("\n" + "-" * 78)
        print(f"Coverage gaps — real opener/bullpen games we did NOT flag ({len(all_missed)}):")
        for m in all_missed:
            print(f"  {m['date']} {m['team']:<4} starter only {m['starterIP']}ip -> {m['actualBulk']} threw {m['actualBulkIP']} IP")


if __name__ == "__main__":
    args = sys.argv[1:] or ["2026-06-05", "2026-06-04", "2026-06-03"]
    main(args)
