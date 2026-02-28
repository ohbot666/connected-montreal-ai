#!/usr/bin/env python3
from flask import Flask, request, jsonify, send_file, session, redirect, url_for, make_response, render_template
from flask_cors import CORS
import requests, json, os, time, uuid, hashlib
from pathlib import Path
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "cm-portal-secret-2026-change-in-prod")
CORS(app)

# Secrets — set as environment variables in production (never hardcoded)
POSTHOG_API_KEY = os.environ.get("POSTHOG_API_KEY", "")
POSTHOG_PROJECT = os.environ.get("POSTHOG_PROJECT", "259946")
AIRTABLE_TOKEN  = os.environ.get("AIRTABLE_TOKEN", "")
AIRTABLE_BASE   = os.environ.get("AIRTABLE_BASE", "appHT9Re4l53GO16t")
AIRTABLE_TABLE  = os.environ.get("AIRTABLE_TABLE", "tbl4P7tqdonXv5vcY")

# Dashboard HTML — bundled in repo
DASHBOARD_PATH = Path(__file__).parent / "dashboard.html"

# Quote token storage
TOKENS_FILE = Path(__file__).parent / "quote_tokens.json"

def load_tokens():
    if TOKENS_FILE.exists():
        try:
            return json.loads(TOKENS_FILE.read_text())
        except Exception:
            return {}
    return {}

def save_tokens(tokens):
    TOKENS_FILE.write_text(json.dumps(tokens, indent=2))

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

# ─────────────────────────────────────────────────────────────
# CLIENT QUOTE PORTAL
# ─────────────────────────────────────────────────────────────

EVENTS_TABLE = "tblLuq2c0C405bP3g"

EVENT_EMOJIS = {
    "Strip Club": "🍾",
    "Nightclub": "🎶",
    "Bar Crawl": "🍺",
    "Restaurant": "🍽️",
    "Activity": "🎯",
    "Transportation": "🚗",
    "Boat": "⛵",
    "Brunch": "🥂",
    "Casino": "🎰",
    "Golf": "⛳",
    "Spa": "💆",
    "Private Chef": "👨‍🍳",
    "Comedy": "😂",
    "Sports": "🏅",
    "Concert": "🎸",
    "VIP": "⭐",
    "Day Party": "☀️",
    "Pool": "🏊",
    "Paintball": "🎯",
    "Go Kart": "🏎️",
    "Axe Throwing": "🪓",
    "Photography": "📸",
    "Airsoft": "🔫",
    "Escape Room": "🔐",
    "Brewery": "🍻",
    "Karaoke": "🎤",
    "Hookah": "💨",
}

def format_cad(val):
    try:
        return f"${float(val):,.2f}"
    except Exception:
        return val or "—"

def fetch_client_record(record_id):
    if not AIRTABLE_TOKEN:
        return None
    headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}
    r = requests.get(
        f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{AIRTABLE_TABLE}/{record_id}",
        headers=headers, timeout=15
    )
    if r.ok:
        return r.json().get("fields", {})
    return None

EXPERIENCE_TABLE = "tblHsIUTzp0LRGdYD"

def fetch_accommodation_details(client_fields):
    """Fetch accommodation house stats + PDF URL from the linked Accommodation event
    and Experience records. Returns a dict with: bedrooms, beds, bathrooms,
    accom_pdf, checkin, checkout, venue_address."""
    result = {"bedrooms": None, "beds": None, "bathrooms": None,
              "accom_pdf": "", "checkin": "", "checkout": "", "venue_address": ""}
    if not AIRTABLE_TOKEN:
        return result
    headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}

    # 1. Get stats from Experience record (tblHsIUTzp0LRGdYD)
    exp_ids = client_fields.get("Accommodation", [])
    exp_id  = exp_ids[0] if isinstance(exp_ids, list) and exp_ids else None
    if exp_id:
        try:
            r = requests.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{EXPERIENCE_TABLE}/{exp_id}",
                headers=headers, timeout=10)
            if r.ok:
                ef = r.json().get("fields", {})
                result["bedrooms"]  = ef.get("house bedrooms")
                result["beds"]      = ef.get("Beds")
                result["bathrooms"] = ef.get("bathrooms")
        except Exception:
            pass

    # 2. Get PDF URL + address from accommodation event record (EVENTS_TABLE)
    accom_link_ids = client_fields.get("Accommodation Link", [])
    accom_link_id  = accom_link_ids[0] if isinstance(accom_link_ids, list) and accom_link_ids else None
    if accom_link_id:
        try:
            r = requests.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{EVENTS_TABLE}/{accom_link_id}",
                headers=headers, timeout=10)
            if r.ok:
                af = r.json().get("fields", {})
                desc_raw = af.get("Description", [""])
                desc     = desc_raw[0] if isinstance(desc_raw, list) else desc_raw
                # Extract PDF URL from description text
                import re as _re_accom
                pdf_match = _re_accom.search(r'https?://\S+\.pdf', str(desc))
                if pdf_match:
                    result["accom_pdf"] = pdf_match.group(0)
                # Check-in / Check-out from the record
                ci = af.get("Check In", [""])
                co = af.get("Check Out", [""])
                result["checkin"]  = ci[0] if isinstance(ci, list) and ci else ci
                result["checkout"] = co[0] if isinstance(co, list) and co else co
                # Venue address
                va = af.get("Venue Address", [""])
                result["venue_address"] = va[0] if isinstance(va, list) and va else va
        except Exception:
            pass

    return result

def fetch_client_events(record_id, client_fields=None):
    """Fetch itinerary events for a client.
    Uses Day 1/2/3/4 Link fields from the client record to get event IDs,
    then fetches those event records directly from the events table."""
    if not AIRTABLE_TOKEN:
        return []
    headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}

    # Collect event record IDs from Day 1/2/3/4/5 Link fields
    event_ids = []
    if client_fields:
        for day_num in range(1, 8):
            field = f"Day {day_num} Link"
            ids = client_fields.get(field, [])
            if isinstance(ids, list):
                event_ids.extend(ids)
        # Also include Essential Services and any Accommodation Link events
        # (skip — essential services shown separately)

    if not event_ids:
        # Fallback: filter by Party Main Contact
        formula = f"FIND('{record_id}', ARRAYJOIN({{Party Main Contact}}, ','))"
        params = [
            ("filterByFormula", formula),
            ("sort[0][field]", "Day Number"),
            ("sort[0][direction]", "asc"),
            ("sort[1][field]", "24 Hour Clock"),
            ("sort[1][direction]", "asc"),
        ]
        r = requests.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{EVENTS_TABLE}",
            headers=headers, params=params, timeout=15
        )
        if r.ok:
            return [{**rec.get("fields", {}), "_record_id": rec.get("id", "")} for rec in r.json().get("records", [])]
        return []

    # Fetch specific event records by ID using OR formula
    id_conditions = " ,".join([f"RECORD_ID()='{eid}'" for eid in event_ids])
    formula = f"OR({id_conditions})"
    params = [
        ("filterByFormula", formula),
        ("sort[0][field]", "Day Number"),
        ("sort[0][direction]", "asc"),
        ("sort[1][field]", "24 Hour Clock"),
        ("sort[1][direction]", "asc"),
    ]
    r = requests.get(
        f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{EVENTS_TABLE}",
        headers=headers, params=params, timeout=15
    )
    if r.ok:
        return [{**rec.get("fields", {}), "_record_id": rec.get("id", "")} for rec in r.json().get("records", [])]
    return []

@app.route("/generate-quote", methods=["POST"])
def generate_quote():
    body = request.json or {}
    record_id = body.get("record_id", "").strip()
    password   = body.get("password", "").strip()
    if not record_id or not password:
        return jsonify({"ok": False, "error": "record_id and password required"}), 400
    tokens = load_tokens()
    token = str(uuid.uuid4())
    tokens[token] = {
        "record_id": record_id,
        "password": password,
        "created_at": __import__("datetime").datetime.utcnow().isoformat()
    }
    save_tokens(tokens)
    base_url = request.host_url.rstrip("/")
    return jsonify({"ok": True, "token": token, "url": f"{base_url}/quote/{token}"})

@app.route("/quote/<token>", methods=["GET"])
def quote_gate(token):
    tokens = load_tokens()
    if token not in tokens:
        return "Quote not found.", 404
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Connected Montreal — Your Quote</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0a0a0a; color: #fff; min-height: 100vh;
         display: flex; flex-direction: column; align-items: center; justify-content: center;
         padding: 24px; }}
  .hero-img {{ width: 100%; max-width: 420px; border-radius: 12px; margin-bottom: 32px;
               box-shadow: 0 8px 40px rgba(0,0,0,0.6); }}
  .card {{ background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 16px;
           padding: 32px 24px; width: 100%; max-width: 420px; }}
  h1 {{ font-size: 22px; font-weight: 700; margin-bottom: 8px; letter-spacing: -0.3px; }}
  p  {{ color: #999; font-size: 14px; margin-bottom: 24px; line-height: 1.5; }}
  label {{ display: block; font-size: 12px; font-weight: 600; letter-spacing: 0.5px;
           text-transform: uppercase; color: #888; margin-bottom: 6px; }}
  input[type=password] {{ width: 100%; padding: 14px 16px; background: #111;
    border: 1px solid #333; border-radius: 10px; color: #fff; font-size: 16px;
    outline: none; transition: border-color .2s; margin-bottom: 16px; }}
  input[type=password]:focus {{ border-color: #c9a84c; }}
  .remember {{ display: flex; align-items: center; gap: 8px; margin-bottom: 20px; }}
  .remember input {{ width: 16px; height: 16px; accent-color: #c9a84c; }}
  .remember span {{ font-size: 13px; color: #888; }}
  button {{ width: 100%; padding: 15px; background: #c9a84c; color: #000;
            font-size: 16px; font-weight: 700; border: none; border-radius: 10px;
            cursor: pointer; transition: opacity .2s; }}
  button:hover {{ opacity: 0.88; }}
  .error {{ color: #ff6b6b; font-size: 13px; margin-top: 12px; display: none; }}
</style>
</head>
<body>
<img class="hero-img"
  src="https://documint.s3.amazonaws.com/accounts/61db2ecae636260004b12f5b/assets/686c1ab7930cb27324a72381"
  alt="Connected Montreal">
<div class="card">
  <h1>Your Quote is Ready 🎉</h1>
  <p>Enter the password you received to view your personalised Connected Montreal quote.</p>
  <form id="authForm">
    <label for="pw">Password</label>
    <input type="password" id="pw" name="password" placeholder="Enter password" autocomplete="current-password">
    <div class="remember">
      <input type="checkbox" id="remember" checked>
      <span>Remember me on this device</span>
    </div>
    <button type="submit">View My Quote →</button>
    <div class="error" id="err">Incorrect password. Please try again.</div>
  </form>
</div>
<script>
const TOKEN = "{token}";
const STORED_KEY = "cm_pw_" + TOKEN;
// Auto-fill from localStorage
const saved = localStorage.getItem(STORED_KEY);
if (saved) {{
  document.getElementById("pw").value = saved;
  document.getElementById("authForm").requestSubmit();
}}
document.getElementById("authForm").addEventListener("submit", async (e) => {{
  e.preventDefault();
  const pw = document.getElementById("pw").value;
  const remember = document.getElementById("remember").checked;
  const resp = await fetch("/quote/{token}/auth", {{
    method: "POST",
    headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{password: pw}})
  }});
  const data = await resp.json();
  if (data.ok) {{
    if (remember) localStorage.setItem(STORED_KEY, pw);
    else localStorage.removeItem(STORED_KEY);
    window.location.href = "/quote/{token}/view";
  }} else {{
    document.getElementById("err").style.display = "block";
  }}
}});
</script>
</body>
</html>"""
    return html

@app.route("/quote/<token>/auth", methods=["POST"])
def quote_auth(token):
    tokens = load_tokens()
    if token not in tokens:
        return jsonify({"ok": False, "error": "Invalid token"}), 404
    body = request.json or {}
    password = body.get("password", "")
    if password == tokens[token]["password"]:
        session[f"quote_{token}"] = True
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Wrong password"}), 401

@app.route("/quote/<token>/update-event", methods=["POST"])
def quote_update_event(token):
    tokens = load_tokens()
    if token not in tokens:
        return jsonify({"ok": False, "error": "Invalid token"}), 404
    if not session.get(f"quote_{token}"):
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    body = request.json or {}
    event_id  = body.get("event_id", "")
    start_time = body.get("start_time", "")
    new_day   = body.get("day_num")  # 1-indexed from client
    if not event_id:
        return jsonify({"ok": False, "error": "event_id required"}), 400
    if not AIRTABLE_TOKEN:
        return jsonify({"ok": False, "error": "No Airtable token"}), 500
    headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}", "Content-Type": "application/json"}
    patch_fields = {}
    if start_time:
        patch_fields["Manual Start Time"] = start_time
    quantity = body.get("quantity")
    duration = body.get("duration")
    if quantity is not None:
        try:
            patch_fields["Manu Quantity"] = int(quantity)
        except (ValueError, TypeError):
            pass
    if duration is not None:
        try:
            patch_fields["Manual Duration"] = float(duration)
        except (ValueError, TypeError):
            pass
    if new_day is not None:
        # Look up date for this day from client record
        record_id = tokens[token]["record_id"]
        client_fields = fetch_client_record(record_id) or {}
        day_field = f"Day {new_day} Date"
        raw_date = client_fields.get(day_field, "")
        if raw_date:
            # Parse "Day N-Fri, Aug 07, 2026" → ISO "2026-08-07"
            import re as _re3
            from datetime import datetime as _dt
            parts = _re3.split(r'-', raw_date, maxsplit=1)
            date_str = parts[1].strip() if len(parts) > 1 else raw_date
            try:
                patch_fields["Date"] = _dt.strptime(date_str, "%a, %b %d, %Y").strftime("%Y-%m-%d")
            except Exception:
                patch_fields["Date"] = date_str
    if not patch_fields:
        return jsonify({"ok": False, "error": "Nothing to update"}), 400
    r = requests.patch(
        f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{EVENTS_TABLE}/{event_id}",
        headers=headers,
        json={"fields": patch_fields},
        timeout=15
    )
    if r.ok:
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": r.text}), 500

@app.route("/quote/<token>/update-field", methods=["POST"])
def quote_update_field(token):
    tokens = load_tokens()
    if token not in tokens:
        return jsonify({"ok": False, "error": "Invalid token"}), 404
    if not session.get(f"quote_{token}"):
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    body = request.json or {}
    field = body.get("field")
    value = body.get("value")
    ALLOWED_FIELDS = {"People": int}
    if field not in ALLOWED_FIELDS:
        return jsonify({"ok": False, "error": "Field not allowed"}), 400
    try:
        value = ALLOWED_FIELDS[field](value)
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "Invalid value"}), 400
    record_id = tokens[token]["record_id"]
    if not AIRTABLE_TOKEN:
        return jsonify({"ok": False, "error": "No Airtable token"}), 500
    headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}", "Content-Type": "application/json"}
    r = requests.patch(
        f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{AIRTABLE_TABLE}/{record_id}",
        headers=headers,
        json={"fields": {field: value}},
        timeout=15
    )
    if r.ok:
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": r.text}), 500

@app.route("/quote/<token>/view", methods=["GET"])
def quote_view(token):
    tokens = load_tokens()
    if token not in tokens:
        return "Quote not found.", 404
    if not session.get(f"quote_{token}"):
        return redirect(f"/quote/{token}")

    record_id = tokens[token]["record_id"]
    fields = fetch_client_record(record_id) or {}
    raw_events = fetch_client_events(record_id, client_fields=fields)
    accom_details = fetch_accommodation_details(fields)

    def _arr(val, default=""):
        """Unwrap single-element Airtable arrays."""
        if isinstance(val, list):
            return val[0] if val else default
        return val if val is not None else default

    # ── Client fields ──────────────────────────────────────────
    first      = fields.get("First Name", "")
    last       = fields.get("Last Name", "")
    # Use DOAText (formatted) or fall back to raw DOA
    doa        = fields.get("DOAText", "") or fields.get("DOA", "")
    people     = fields.get("People", "")
    nights     = fields.get("Nights", "")
    accom_name = fields.get("Party Accommodation", "")
    # Accommodation address + Google Maps URL
    accom_addr = accom_details.get("venue_address") or _arr(fields.get("Accommodation Address", ""))
    if accom_addr:
        import urllib.parse as _urlparse
        maps_query = _urlparse.quote(accom_addr + ", Montreal, QC")
        accom_maps_url = f"https://www.google.com/maps/search/?api=1&query={maps_query}"
    else:
        accom_maps_url = ""
    # House stats
    accom_bedrooms  = accom_details.get("bedrooms")
    accom_beds      = accom_details.get("beds")
    accom_bathrooms = accom_details.get("bathrooms")
    essential  = fields.get("Essential Service Set", "")
    # Check-in/out from accommodation record (fall back to defaults)
    checkin  = accom_details.get("checkin") or fields.get("Check In Time", "3:00 PM")
    checkout = accom_details.get("checkout") or fields.get("Check Out Time", "11:00 AM")
    accom_desc   = ""
    # Build a day → formatted date map from Day N Date fields ("Day 1-Thu, Aug 06, 2026")
    day_dates = {}
    for n in range(1, 8):
        raw = fields.get(f"Day {n} Date", "")
        if raw:
            # Strip "Day N-" prefix → "Thu, Aug 06, 2026"
            parts = raw.split("-", 1)
            day_dates[n] = parts[1].strip() if len(parts) > 1 else raw

    # Accommodation photo (first attachment)
    accom_photos = fields.get("Accommodation Picture", [])
    accom_photo_url = accom_photos[0].get("url", "") if accom_photos else ""

    # ── Build events list for template ────────────────────────
    events = []
    for ev in raw_events:
        day_num  = ev.get("Day Number", "")
        ev_date  = ev.get("Date", "")
        import re as _re
        def _first(val, default=""):
            return val[0] if isinstance(val, list) and val else (val if not isinstance(val, list) else default)
        ev_name  = _first(ev.get("Name (from Experience)", ""))
        ev_type  = _first(ev.get("Type", ""))
        ev_start = _first(ev.get("Start Time", ""))
        ev_desc  = _first(ev.get("Description for Documont", ""))
        ev_desc  = _re.sub(r'<[^>]+>', '', ev_desc).strip().lstrip('•·\t ')
        # Day number: Airtable is 0-indexed, display as 1-indexed
        raw_day_num = ev.get("Day Number", 0)
        display_day = (raw_day_num or 0) + 1
        # Use pre-built day date string from client fields ("Thu, Aug 06, 2026")
        ev_date = day_dates.get(display_day, ev.get("Date", ""))
        events.append({
            "day_num":     display_day,
            "date":        ev_date,
            "name":        ev_name or ev_type,
            "subtitle":    ev.get("Quote Text", ""),
            "start_time":  ev_start,
            "description": ev_desc,
            "record_id":   ev.get("_record_id", ""),
            "min_time":    _first(ev.get("Earliest Start Time", "")),
            "max_time":    _first(ev.get("Latest Start Time", "")),
            "quantity":    _first(ev.get("Quantity", "")) or "",
            "duration":    _first(ev.get("Duration", "")) or "",
            "quantity_type": _first(ev.get("Quantity Type", "")) or "",
            "quantity_default": _first(ev.get("Quantity Default", "")) or "",
        })

    # ── Accommodation ──────────────────────────────────────────
    accom_photos    = fields.get("Accommodation Picture", [])
    accom_photo_url = accom_photos[0].get("url", "") if accom_photos else ""
    # Accommodation Link is a linked record — no direct PDF URL in Airtable
    # Use Fillout Rental Agreement link as the "view details" link if available
    accom_pdf       = fields.get("Accommodation PDF URL", "") or ""

    # PDF link — parse from accommodation event record description
    accom_pdf = accom_details.get("accom_pdf") or fields.get("Accommodation PDF URL", "") or ""

    # Payment link — use prefilled down payment URL (has invoice # baked in)
    cad_form = (fields.get("Benjiform Prefill Down Payment", "")
                or fields.get("Benjiform Prefill", "")
                or fields.get("CAD Form URL", ""))

    # ── Pricing ────────────────────────────────────────────────
    service_subtotal = format_cad(fields.get("Subtotal"))
    service_hst      = format_cad(fields.get("HST"))
    service_qst      = format_cad(fields.get("QST"))
    service_cc_fee   = format_cad(fields.get("Credit Card Fee"))
    service_total    = format_cad(fields.get("Service Total"))
    service_pp       = format_cad(fields.get("Service Total Per Person"))

    accom_subtotal   = format_cad(fields.get("Accommodation Subtotal"))
    accom_hst        = format_cad(fields.get("Accommodation HST"))
    accom_qst        = format_cad(fields.get("Accommodation QST"))
    accom_hosp_tax   = format_cad(fields.get("Accommodation Hospitality Tax"))
    accom_cleaning   = format_cad(fields.get("Cleaning Fee"))          # field is "Cleaning Fee" not "Accommodation Cleaning Fee"
    accom_cc_fee     = format_cad(fields.get("Accommodation Credit Card Fee"))
    accom_total      = format_cad(fields.get("Accommodation Total"))
    accom_pp         = format_cad(fields.get("Accommodation per Person"))  # lowercase "per"

    grand_total   = format_cad(fields.get("Grand Total"))
    per_person    = format_cad(fields.get("Total Per Person"))
    down_payment  = format_cad(fields.get("Total Down Payment") or fields.get("Down Payment"))
    accom_downpay = format_cad(fields.get("Accommodation Downpayment"))

    # ── Essential services list ────────────────────────────────
    import re as _re2
    services_list = []
    # Try to parse from text field first
    if isinstance(essential, str) and len(essential) > 5:
        services_list = [s.strip() for s in essential.replace(",", "\n").split("\n") if s.strip()]
    # If that failed, pull from the linked Essential Services event records
    if not services_list:
        svc_ids = fields.get("Essential Services", [])
        if svc_ids and AIRTABLE_TOKEN:
            for sid in svc_ids[:1]:  # just first record — it has the full description
                try:
                    sr = requests.get(
                        f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{EVENTS_TABLE}/{sid}",
                        headers={"Authorization": f"Bearer {AIRTABLE_TOKEN}"}, timeout=10
                    )
                    if sr.ok:
                        sfields = sr.json().get("fields", {})
                        desc_raw = sfields.get("Description", [""])
                        desc = desc_raw[0] if isinstance(desc_raw, list) else desc_raw
                        desc = _re2.sub(r'<[^>]+>', '', str(desc))
                        for line in desc.split("\n"):
                            clean = _re2.sub(r'^[\s✔•\t]+', '', line).strip()
                            if clean:
                                services_list.append(clean)
                except Exception:
                    pass
    # Hardcoded fallback
    if not services_list:
        services_list = [
            "Our legendary bachelor party planning service and expertise — We ensure you have the best time.",
            "Customized itinerary — We will plan out the trip based on your input.",
            "Travel Concierge — Get immediate support while in town.",
            "Dinner reservations.",
            "No cover charges or waiting in line at the night clubs.",
            "Tips for doormen at nightclubs and strip clubs.",
        ]

    personal_host = "Personal host: your man on the ground for the weekend. They will escort you into the venues and take care of you during your trip."

    current_url = request.url
    total_days = max((e["day_num"] for e in events), default=1)

    return render_template(
        "quote.html",
        first=first, last=last,
        doa=doa, people=people, nights=nights,
        accom_name=accom_name, accom_addr=accom_addr,
        accom_maps_url=accom_maps_url,
        accom_bedrooms=accom_bedrooms, accom_beds=accom_beds, accom_bathrooms=accom_bathrooms,
        accom_photo_url=accom_photo_url, accom_pdf=accom_pdf,
        accom_desc=accom_desc,
        checkin=checkin, checkout=checkout,
        services_list=services_list,
        personal_host=personal_host,
        events=events,
        day_dates=day_dates,
        total_days=total_days,
        service_subtotal=service_subtotal, service_hst=service_hst,
        service_qst=service_qst, service_cc_fee=service_cc_fee,
        service_total=service_total, service_pp=service_pp,
        accom_subtotal=accom_subtotal, accom_hst=accom_hst,
        accom_qst=accom_qst, accom_hosp_tax=accom_hosp_tax,
        accom_cleaning=accom_cleaning, accom_cc_fee=accom_cc_fee,
        accom_total=accom_total, accom_pp=accom_pp,
        grand_total=grand_total, per_person=per_person,
        down_payment=down_payment, accom_downpay=accom_downpay,
        cad_form=cad_form,
        current_url=current_url,
    )


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
