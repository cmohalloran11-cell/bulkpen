"""
Vercel serverless entrypoint.

@vercel/python serves a module-level WSGI callable named `app`, so we just put the
repo root on sys.path and re-export the existing Flask app from app.py. All routes
(/, /api/predict/<date>, /mlb/<path>) are handled by that one app via the rewrite
in vercel.json.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402,F401  (re-exported for the Vercel Python runtime)
