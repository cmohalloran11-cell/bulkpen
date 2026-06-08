"""
BULKPEN — local backend
========================
Runs a tiny server so the webpage talks to MLB through Python instead of
flaky public CORS proxies. Two pieces:

  1. /mlb/<path>            -> transparent passthrough to statsapi.mlb.com
                              (server-to-server, so there is NO CORS at all).
                              This is what bulkpen.html uses automatically.
  2. /api/predict/<date>    -> the finished prediction: full board + leaderboard
                              payload, scored server-side in predict.py. Lets
                              bulkpen.html become a thin client.
  3. /api/lib/...           -> example endpoints powered by python-mlb-statsapi,
                              the structured data layer predict.py builds on.

Run:
    pip install -r requirements.txt
    python app.py
Then open http://localhost:8000
"""
from flask import Flask, request, Response, send_from_directory, jsonify
import requests, os
from datetime import datetime
import predict as predictor       # server-side scoring layer (see predict.py)

MLB = predictor.MLB              # python-mlb-statsapi client (None if unavailable)

app = Flask(__name__, static_folder=None)
HERE = os.path.dirname(os.path.abspath(__file__))
UPSTREAM = "https://statsapi.mlb.com"
HEADERS = {"User-Agent": "Bulkpen/1.0 (local backend)"}


@app.route("/")
def index():
    # no-store so edits to bulkpen.html always show on refresh (no stale browser cache)
    resp = send_from_directory(HERE, "bulkpen.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.route("/mlb/<path:path>")
def mlb_passthrough(path):
    """Forward any statsapi path verbatim, server-side. No CORS involved."""
    qs = request.query_string.decode()
    url = f"{UPSTREAM}/{path}" + (f"?{qs}" if qs else "")
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        return Response(
            r.content,
            status=r.status_code,
            content_type=r.headers.get("Content-Type", "application/json"),
        )
    except requests.RequestException as e:
        return jsonify({"error": "upstream fetch failed", "detail": str(e)}), 502


# ---- the finished prediction endpoint (turns bulkpen.html into a thin client) ----

@app.route("/api/predict/<date>")
def api_predict(date):
    """Full board + leaderboard payload for a date (YYYY-MM-DD), scored server-side."""
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400
    fresh = request.args.get("fresh") in ("1", "true", "yes")
    try:
        resp = jsonify(predictor.predict(date, fresh=fresh))
        # Edge-cache at Vercel's CDN: in-process caching dies with each serverless
        # instance, but s-maxage lets the CDN serve a computed payload to everyone
        # without re-invoking the function. stale-while-revalidate means once a date
        # has been computed once, users always get an instant (possibly stale) result
        # while a refresh happens in the background — no cold-compute timeouts.
        resp.headers["Cache-Control"] = (
            "no-store" if fresh
            else "public, s-maxage=300, stale-while-revalidate=86400"
        )
        return resp
    except requests.RequestException as e:
        return jsonify({"error": "schedule fetch failed", "detail": str(e)}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---- python-mlb-statsapi powered endpoints (the structured data layer) ----

@app.route("/api/lib/schedule/<date>")
def lib_schedule(date):
    """Proof the library is wired in: game count for a date (YYYY-MM-DD)."""
    if MLB is None:
        return jsonify({"error": "python-mlb-statsapi unavailable"}), 500
    try:
        games = MLB.get_scheduled_games_by_date(date) or []
        return jsonify({"date": date, "game_count": len(games)})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/lib/pitcher-season/<int:person_id>")
def lib_pitcher_season(person_id):
    """Season pitching stats for one pitcher via the library."""
    if MLB is None:
        return jsonify({"error": "python-mlb-statsapi unavailable"}), 500
    try:
        stats = MLB.get_player_stats(person_id, stats=["season"], groups=["pitching"])
        return jsonify({"person_id": person_id, "stats": str(stats)})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


if __name__ == "__main__":
    print("\n  BULKPEN backend running  ->  http://localhost:8000\n")
    app.run(host="0.0.0.0", port=8000, debug=False)
