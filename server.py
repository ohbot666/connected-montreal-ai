#!/usr/bin/env python3
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import requests, json, os, time
from pathlib import Path

app = Flask(__name__)
CORS(app)

# Secrets — set as environment variables in production (never hardcoded)
POSTHOG_API_KEY = os.environ.get("POSTHOG_API_KEY", "")
POSTHOG_PROJECT = os.environ.get("POSTHOG_PROJECT", "259946")
AIRTABLE_TOKEN  = os.environ.get("AIRTABLE_TOKEN", "")
AIRTABLE_BASE   = os.environ.get("AIRTABLE_BASE", "appHT9Re4l53GO16t")
AIRTABLE_TABLE  = os.environ.get("AIRTABLE_TABLE", "tbl4P7tqdonXv5vcY")

# Dashboard HTML — bundled in repo
DASHBOARD_PATH = Path(__file__).parent / "dashboard.html"

_cache = {"data": None, "ts": 0}
CACHE_TTL = 300  # 5 min

def fetch_live_data():
    now = time.time()
    if _cache["data"] and now - _cache["ts"] < CACHE_TTL:
        return _cache["data"]
    data = {}
    # PostHog
    try:
        headers = {"Authorization": f"Bearer {POSTHOG_API_KEY}"}
        r = requests.get(f"https://us.posthog.com/api/projects/{POSTHOG_PROJECT}/events/",
            headers=headers, params={"event": "$pageview", "limit": 500}, timeout=10)
        events = r.json().get("results", []) if r.ok else []
        from collections import Counter
        pages = Counter(e.get("properties", {}).get("$pathname", "/") for e in events)
        data["pageviews_7d"] = len(events)
        data["top_pages"] = [{"url": u, "views": c} for u, c in pages.most_common(5)]
    except Exception as e:
        data["posthog_error"] = str(e)
    # Airtable
    try:
        if AIRTABLE_TOKEN:
            headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}
            # Paginate through all records
            all_records = []
            offset = None
            while True:
                params = {"pageSize": 100}
                if offset:
                    params["offset"] = offset
                r = requests.get(f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{AIRTABLE_TABLE}",
                    headers=headers, params=params, timeout=30)
                if not r.ok:
                    break
                d = r.json()
                all_records.extend(d.get("records", []))
                offset = d.get("offset")
                if not offset:
                    break
            pipeline = {"new": 0, "quoted": 0, "booked": 0, "no_go": 0}
            status_map = {
                "New Request": "new",
                "talked to/ quoted": "quoted",
                "Booked - Deposit": "booked",
                "Booked": "booked",
                "No Go": "no_go",
                "No Go - Coming to Town": "no_go",
                "No Go - Not Coming to Town": "no_go",
            }
            main_contacts = [r for r in all_records if r.get("fields", {}).get("Contact Type") == "Party Main Contact"]
            for rec in main_contacts:
                s = rec.get("fields", {}).get("Status", "")
                bucket = status_map.get(s)
                if bucket:
                    pipeline[bucket] += 1
            data["pipeline"] = pipeline
            data["total_leads"] = len(main_contacts)
    except Exception as e:
        data["airtable_error"] = str(e)
    _cache["data"] = data
    _cache["ts"] = now
    return data

@app.route("/")
def index():
    return send_file(DASHBOARD_PATH)

@app.route("/api/data")
def api_data():
    return jsonify(fetch_live_data())

@app.route("/api/chat", methods=["POST"])
def api_chat():
    body = request.json or {}
    message = body.get("message", "")
    history = body.get("history", [])
    data = fetch_live_data()
    system = f"""You are an AI assistant for Connected Montreal, a bachelor party planning business in Montreal and Austin TX.
You have access to live business data. Answer questions about leads, pipeline, traffic, and marketing. Be concise and direct.

LIVE DATA:
{json.dumps(data, indent=2)}"""
    messages = [{"role": "system", "content": system}] + history + [{"role": "user", "content": message}]
    # Try Ollama (local only)
    try:
        r = requests.post("http://localhost:11434/api/chat",
            json={"model": "gemma3:4b", "messages": messages, "stream": False}, timeout=10)
        resp = r.json().get("message", {}).get("content", "")
        if resp:
            return jsonify({"response": resp})
    except Exception:
        pass
    return jsonify({"error": "Ollama not available in cloud mode. Use Ask OpenClaw instead."}), 503

@app.route("/api/ask-openclaw", methods=["POST"])
def api_ask_openclaw():
    body = request.json or {}
    message = body.get("message", "")
    data = fetch_live_data()
    summary = json.dumps({"pipeline": data.get("pipeline"), "pageviews_7d": data.get("pageviews_7d"), "top_pages": data.get("top_pages")})
    prefixed = f"[Connected Montreal Live Data]\n{summary}\n\nQuestion: {message}"
    try:
        r = requests.post("http://localhost:9999/api/chat",
            json={"message": prefixed, "channel": "webchat"}, timeout=30)
        resp = r.json().get("response") or r.text
        return jsonify({"response": resp})
    except Exception:
        return jsonify({"error": "OpenClaw is only available when running locally."}), 503

@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    _cache["ts"] = 0
    return jsonify(fetch_live_data())

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    print(f"Connected Montreal AI Server running at http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
