# BULKPEN — Live Opener & Bulk-Reliever Radar

A live web tool that pulls today's MLB schedule + probable pitchers and predicts which
relievers are likely to absorb **bulk innings** — the long reliever behind an opener or in
a bullpen game. All scoring is transparent, model-free heuristics; the math is shown on
every card. **Not affiliated with MLB.**

## How it works

A small Flask backend pulls the schedule, probables, season stats, and game logs from the
[MLB Stats API](https://statsapi.mlb.com) and does all the scoring server-side, then hands
the frontend one ready-made `/api/predict/<date>` payload. The page (`bulkpen.html`) is a
thin client that just renders it.

- **Opener risk** — from each listed starter's season innings-per-appearance, relief share,
  and whether a non-starter is penciled in to start.
- **Bulk Score** — ranks relievers by innings-per-appearance, swingman history, and workload.
- **Recent usage** — last-7-day innings and days of rest adjust each score for who's actually
  available (Fresh / Worked recently / Gassed).

See [`predict.py`](predict.py) for the scoring; every function is a documented port of the
original in-browser heuristics.

## Run it

```bash
pip install -r requirements.txt
python app.py          # -> http://localhost:8000
```

## Endpoints

| Route | Purpose |
|---|---|
| `/` | The web UI (`bulkpen.html`) |
| `/api/predict/<date>` | Full board + leaderboard payload, scored server-side (cached; `?fresh=1` to bypass) |
| `/mlb/<path>` | Transparent passthrough to statsapi.mlb.com (kills CORS) |

## Tests & backtest

```bash
python -m unittest test_predict     # 26 unit tests for the scoring functions
python backtest.py 2026-06-05 ...   # score past predictions vs. who actually ate the innings
```

## Data layer note

Probable pitchers come straight from the schedule JSON, while season stats / game logs /
rosters go through [`python-mlb-statsapi`](https://github.com/zero-sum-seattle/python-mlb-statsapi).
This hybrid is deliberate: that library's `get_game` currently fails validation on live MLB
data and its schedule model drops `probablePitcher`, so the one field it can't deliver is
fetched raw.

## Caveats

Predictions are a smart **shortlist, not official projections** — managers don't announce
bulk relievers, so the tool infers role from how pitchers have been used. Future dates only
work once MLB posts probables (~1–2 days out).
