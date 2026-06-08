# BULKPEN — project context for Claude Code

A live web tool that pulls today's MLB schedule + probable pitchers and predicts
which relievers are likely to absorb **bulk innings** (the long reliever after an
opener / in a bullpen game). Built in a Claude.ai chat session; this file carries
that state over so you can continue here.

## Files
- `bulkpen.html` — the entire frontend (HTML + CSS + vanilla JS, no build step).
  Dark "night-game" theme. Fetches live data, scores opener risk + bulk relievers,
  renders a games board and a slate leaderboard.
- `app.py` — Flask backend. Transparent passthrough to statsapi.mlb.com (kills CORS)
  + two `python-mlb-statsapi`-powered endpoints. Serves `bulkpen.html` at `/`.
- `requirements.txt` — flask, requests, python-mlb-statsapi.

## Run it
```bash
pip install -r requirements.txt
python app.py          # -> http://localhost:8000
```
`bulkpen.html` also works opened directly as a file (it falls back to public CORS
proxies), but the backend is the robust path.

## Data source
MLB Stats API: `https://statsapi.mlb.com/api/v1`. No key required. Key endpoints used:
- `/schedule?sportId=1&date=YYYY-MM-DD&hydrate=probablePitcher,team,linescore`
- `/people?personIds=a,b,c&hydrate=stats(group=[pitching],type=[season],season=YYYY)`
- `/people?personIds=...&hydrate=stats(group=[pitching],type=[gameLog],season=YYYY)`  (recent usage)
- `/teams/{id}/roster?rosterType=active`

### The CORS problem (important)
statsapi.mlb.com does NOT reliably send `Access-Control-Allow-Origin`, so a direct
browser fetch is blocked. Two mitigations exist in the code:
1. Frontend `STRATS` chain tries, in order: **local backend (`/mlb/...`)** → direct →
   3 public CORS proxies. First one that works gets locked in; a status dot shows which.
2. `app.py` `/mlb/<path>` forwards requests server-side (no CORS at all). This is why
   the backend is preferred.

## Frontend architecture (bulkpen.html)
- `getJSON(url)` — resilient fetch over the `STRATS` chain.
- `fetchStats(ids)` / `fetchLogs(ids)` — batched season + game-log pulls, cached in
  `cache` / `logCache` Maps.
- Scoring (all heuristic, model-free — see "honesty" below):
  - `openerScore(stat)` — 0-100 from season innings-per-appearance, relief share, and
    whether a non-starter is listed to start. >=70 Likely opener, 45-69 Possible, <45 Traditional.
  - `bulkScore(stat)` — base reliever fit from IP/appearance, swingman bonus, workload.
  - `computeAvail(splits)` — last-7-day innings + days of rest → availability label and a
    score adjustment (Fresh +6, Worked recently -12, Gassed -26).
  - `rankWithUsage()` — final `proj = clamp(base + availability adj)`.
- UI: games board (`renderBoard`) with per-game "predict bulk reliever" + an
  "All games / Openers only" filter; slate leaderboard (`predictSlate`).

## Known constraints
- Predictions are **transparent heuristics, not official projections**. Managers don't
  announce bulk relievers — treat output as a shortlist. The math is shown on each card.
- Future dates only work once MLB posts probables (~1-2 days out); else TBD → flagged as
  possible bullpen game.
- Public proxies can rate-limit; the local backend avoids that.
- `python-mlb-statsapi` returns Pydantic objects, not raw dicts — field access differs
  from the JSON the JS consumes.

## Suggested next steps (open threads)
1. Port the opener + bulk scoring into `app.py` as a finished `/api/predict/<date>` JSON
   endpoint (built on python-mlb-statsapi), turning bulkpen.html into a thin client.
2. Add deploy config (Render/Railway/Fly/Dockerfile) so it's a public site, not just localhost.
3. Cache MLB responses server-side (short TTL) to cut latency and API load.
4. Add a real recent-usage weighting curve and bullpen-role tags (closer/setup/long).
5. Tests for the scoring functions with fixed stat fixtures.
