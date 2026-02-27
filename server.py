#!/usr/bin/env python3
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import requests, json, os, time
from pathlib import Path
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

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
CACHE_FILE = Path(__file__).parent / ".cache.json"
CACHE_TTL = 1800  # 30 min

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
            # Fetch only active statuses — skip No Go (530+ records we don't need)
            ACTIVE_FILTER = "OR({Status}='New Request',{Status}='talked to/ quoted',{Status}='Booked')"
            FIELDS = ["First Name","Last Name","Status","DOA","People","Created On",
                      "Source of lead","Phone","Email","Tell us what you have in mind?","Contact Type"]
            all_records = []
            offset = None
            while True:
                params = [("pageSize", "100"), ("filterByFormula", ACTIVE_FILTER)]
                for field in FIELDS:
                    params.append(("fields[]", field))
                if offset:
                    params.append(("offset", offset))
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
            
            # Extract full lead details
            leads = []
            for rec in main_contacts:
                fields = rec.get("fields", {})
                status = fields.get("Status", "")
                bucket = status_map.get(status)
                if bucket:
                    pipeline[bucket] += 1
                
                lead = {
                    "id": rec.get("id", ""),
                    "first_name": fields.get("First Name", ""),
                    "last_name": fields.get("Last Name", ""),
                    "status": status,
                    "doa": fields.get("DOA", ""),
                    "people": fields.get("People", ""),
                    "created_on": fields.get("Created On", ""),
                    "source": fields.get("Source of lead", ""),
                    "phone": fields.get("Phone", ""),
                    "email": fields.get("Email", ""),
                    "notes": fields.get("Tell us what you have in mind?", "")
                }
                leads.append(lead)
            
            data["pipeline"] = pipeline
            data["leads"] = leads
            data["total_leads"] = len(main_contacts)
    except Exception as e:
        data["airtable_error"] = str(e)
    _cache["data"] = data
    _cache["ts"] = now
    try:
        CACHE_FILE.write_text(json.dumps({"data": data, "ts": now}))
    except Exception:
        pass
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
            json={"model": "gemma3:4b", "messages": messages, "stream": False}, timeout=90)
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


@app.route("/api/send-sms", methods=["POST"])
def api_send_sms():
    body = request.json or {}
    to = body.get("to", "").strip()
    message = body.get("message", "").strip()
    if not to or not message:
        return jsonify({"ok": False, "error": "Missing to or message"}), 400
    # Get BlueBubbles server URL from config DB
    try:
        import sqlite3, os
        db_path = os.path.expanduser('~/Library/Application Support/bluebubbles-server/config.db')
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT name, value FROM config WHERE name IN ('server_address','password')")
        rows = {r[0]: r[1] for r in cur.fetchall()}
        conn.close()
        bb_url = rows.get('server_address', '').rstrip('/')
        bb_pass = rows.get('password', '')
    except Exception as e:
        return jsonify({"ok": False, "error": f"BlueBubbles config error: {e}"}), 500
    if not bb_url:
        return jsonify({"ok": False, "error": "BlueBubbles URL not found"}), 500
    try:
        import uuid
        r = requests.post(f"{bb_url}/api/v1/message/text",
            headers={"Content-Type": "application/json"},
            params={"password": bb_pass},
            json={"chatGuid": f"SMS;-;{to}", "message": message,
                  "method": "private-api", "tempGuid": str(uuid.uuid4())},
            timeout=15)
        if r.ok:
            return jsonify({"ok": True})
        else:
            return jsonify({"ok": False, "error": r.text}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# Warm cache from disk on startup
try:
    if CACHE_FILE.exists():
        saved = json.loads(CACHE_FILE.read_text())
        _cache["data"] = saved.get("data")
        _cache["ts"] = saved.get("ts", 0)
except Exception:
    pass

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    print(f"Connected Montreal AI Server running at http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
