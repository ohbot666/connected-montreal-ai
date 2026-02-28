#!/usr/bin/env python3
from flask import Flask, request, jsonify, send_file, session, redirect, url_for, make_response
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

def fetch_client_events(record_id):
    if not AIRTABLE_TOKEN:
        return []
    headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}
    formula = f"FIND('{record_id}', ARRAYJOIN({{Clients}}, ','))"
    params = [
        ("filterByFormula", formula),
        ("sort[0][field]", "Day Number"),
        ("sort[0][direction]", "asc"),
        ("sort[1][field]", "Start Time"),
        ("sort[1][direction]", "asc"),
    ]
    r = requests.get(
        f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{EVENTS_TABLE}",
        headers=headers, params=params, timeout=15
    )
    if r.ok:
        return [rec.get("fields", {}) for rec in r.json().get("records", [])]
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

@app.route("/quote/<token>/view", methods=["GET"])
def quote_view(token):
    tokens = load_tokens()
    if token not in tokens:
        return "Quote not found.", 404
    if not session.get(f"quote_{token}"):
        return redirect(f"/quote/{token}")

    record_id = tokens[token]["record_id"]
    fields = fetch_client_record(record_id) or {}
    events  = fetch_client_events(record_id)

    # ── Client fields ──────────────────────────────────────────
    first   = fields.get("First Name", "")
    last    = fields.get("Last Name", "")
    doa     = fields.get("DOA", "")
    people  = fields.get("People", "")
    nights  = fields.get("Nights", "")
    accom_name = fields.get("Party Accommodation", "")
    accom_addr = fields.get("Accommodation Address", "")
    essential  = fields.get("Essential Service Set", "")

    # Accommodation photo (first attachment)
    accom_photos = fields.get("Accommodation Picture", [])
    accom_photo_url = accom_photos[0].get("url", "") if accom_photos else ""

    # Accommodation PDF (link stored in Accommodation Link field)
    accom_pdf = fields.get("Accommodation Link", "")

    # Payment link
    cad_form = fields.get("CAD Form URL", "")

    # Pricing
    service_subtotal = format_cad(fields.get("Subtotal"))
    service_hst      = format_cad(fields.get("HST"))
    service_qst      = format_cad(fields.get("QST"))
    service_cc_fee   = format_cad(fields.get("Credit Card Fee"))
    service_total    = format_cad(fields.get("Service Total"))

    accom_subtotal   = format_cad(fields.get("Accommodation Subtotal"))
    accom_hst        = format_cad(fields.get("Accommodation HST"))
    accom_qst        = format_cad(fields.get("Accommodation QST"))
    accom_hosp_tax   = format_cad(fields.get("Accommodation Hospitality Tax"))
    accom_cc_fee     = format_cad(fields.get("Accommodation Credit Card Fee"))
    accom_total      = format_cad(fields.get("Accommodation Total"))

    grand_total      = format_cad(fields.get("Grand Total"))
    per_person       = format_cad(fields.get("Total Per Person"))
    down_payment     = format_cad(fields.get("Total Down Payment"))
    accom_downpay    = format_cad(fields.get("Accommodation Downpayment"))

    # ── Build itinerary HTML ───────────────────────────────────
    itinerary_html = ""
    current_day = None
    for ev in events:
        day_num  = ev.get("Day Number", "")
        ev_date  = ev.get("Date", "")
        ev_name  = ev.get("Name (from Experience)", [None])[0] if isinstance(ev.get("Name (from Experience)"), list) else ev.get("Name (from Experience)", "")
        ev_start = ev.get("Start Time", "")
        ev_type  = ev.get("Type", "")
        ev_desc  = ev.get("Description for Documont", "")
        ev_total = ev.get("Total (tax in)", "")
        emoji    = EVENT_EMOJIS.get(ev_type, "✨")

        if day_num != current_day:
            if current_day is not None:
                itinerary_html += "</div>"  # close day group
            current_day = day_num
            day_label = f"Day {day_num}" if day_num else "Schedule"
            if ev_date:
                day_label += f" — {ev_date}"
            itinerary_html += f'<div class="day-group"><div class="day-label">{day_label}</div>'

        ev_time_html  = '<div class="event-time">' + ev_start + '</div>' if ev_start else ""
        ev_price_html = '<div class="event-price">' + format_cad(ev_total) + '</div>' if ev_total else ""
        ev_desc_html  = '<div class="event-desc">' + ev_desc + '</div>' if ev_desc else ""
        itinerary_html += (
            '<div class="event-card">'
            '<div class="event-top">'
            '<span class="event-emoji">' + emoji + '</span>'
            '<div class="event-info">'
            '<div class="event-name">' + (ev_name or ev_type) + '</div>'
            + ev_time_html +
            '</div>'
            + ev_price_html +
            '</div>'
            + ev_desc_html +
            '</div>'
        )

    if current_day is not None:
        itinerary_html += "</div>"  # close last day group

    # ── Accommodations section ─────────────────────────────────
    accom_photo_html = ""
    if accom_photo_url:
        if accom_pdf:
            accom_photo_html = f'<a href="{accom_pdf}" target="_blank" rel="noopener"><img class="accom-photo" src="{accom_photo_url}" alt="{accom_name}"><div class="photo-badge">📄 View PDF</div></a>'
        else:
            accom_photo_html = f'<img class="accom-photo" src="{accom_photo_url}" alt="{accom_name}">'

    maps_url = f"https://maps.google.com/?q={requests.utils.quote(accom_addr)}" if accom_addr else ""

    # ── Essential services list ────────────────────────────────
    services_html = ""
    if essential:
        items = [s.strip() for s in essential.replace(",", "\n").split("\n") if s.strip()]
        services_html = "".join(f"<li>{item}</li>" for item in items)

    # ── Pre-compute conditional sections (avoid backslash in f-string exprs) ──
    full_name         = (first + " " + last).strip()
    accom_name_html   = "<h3>" + accom_name + "</h3>" if accom_name else ""
    accom_photo_wrap  = '<div class="accom-photo-wrap">' + accom_photo_html + "</div>" if accom_photo_html else ""
    accom_addr_html   = '<a class="addr-link" href="' + maps_url + '" target="_blank" rel="noopener">\U0001f4cd ' + accom_addr + "</a>" if accom_addr else ""
    services_section  = (
        '<div class="section">'
        '<div class="section-title">What\'s Included</div>'
        '<ul class="services-list">' + services_html + "</ul>"
        "</div>"
    ) if services_html else ""
    itinerary_section = (
        '<div class="section">'
        '<div class="section-title">Your Itinerary</div>'
        + itinerary_html +
        "</div>"
    ) if itinerary_html else ""
    cta_btn_html      = '<a class="cta-btn" href="' + cad_form + '" target="_blank" rel="noopener">Book My Trip \U0001f389</a>' if cad_form else '<div style="color:#555;font-size:14px;">Payment link coming soon — text us to book!</div>'
    down_note_html    = '<div class="down-note">Deposit: ' + down_payment + "</div>" if down_payment != "\u2014" else ""
    down_pay_row      = '<div class="per-person" style="margin-top:6px;">Down payment to book: <strong>' + down_payment + "</strong></div>" if down_payment != "\u2014" else ""

    # ── Full portal HTML ───────────────────────────────────────
    current_url = request.url

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{first} {last} — Connected Montreal Quote</title>
<style>
  /* ── Reset & base ───────────────────── */
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, sans-serif;
         background: #0a0a0a; color: #f0f0f0; font-size: 15px; line-height: 1.6;
         padding-bottom: 80px; }}
  a {{ color: inherit; text-decoration: none; }}

  /* ── Hero ───────────────────────────── */
  .hero {{ width: 100%; display: block; }}
  .hero img {{ width: 100%; display: block; max-height: 260px; object-fit: cover; }}

  /* ── AS SEEN IN ─────────────────────── */
  .press-bar {{ background: #111; border-bottom: 1px solid #222;
               padding: 14px 20px; text-align: center; }}
  .press-label {{ font-size: 10px; font-weight: 700; letter-spacing: 1.5px;
                 text-transform: uppercase; color: #555; margin-bottom: 10px; }}
  .press-logos {{ display: flex; justify-content: center; align-items: center;
                  gap: 28px; flex-wrap: wrap; }}
  .press-logos img {{ height: 18px; opacity: 0.55; filter: grayscale(1) brightness(2); }}

  /* ── Section chrome ─────────────────── */
  .section {{ padding: 28px 20px; border-bottom: 1px solid #1c1c1c; }}
  .section-title {{ font-size: 11px; font-weight: 700; letter-spacing: 2px;
                    text-transform: uppercase; color: #c9a84c; margin-bottom: 16px; }}
  h2 {{ font-size: 22px; font-weight: 800; letter-spacing: -0.5px; margin-bottom: 4px; }}
  h3 {{ font-size: 16px; font-weight: 700; margin-bottom: 8px; }}

  /* ── Client info band ───────────────── */
  .info-band {{ display: flex; gap: 0; }}
  .info-chip {{ flex: 1; background: #161616; border: 1px solid #222; padding: 14px 16px;
                text-align: center; }}
  .info-chip:first-child {{ border-radius: 10px 0 0 10px; }}
  .info-chip:last-child  {{ border-radius: 0 10px 10px 0; border-left: 0; }}
  .chip-val {{ font-size: 18px; font-weight: 800; letter-spacing: -0.3px; }}
  .chip-lbl {{ font-size: 11px; color: #666; margin-top: 2px; text-transform: uppercase;
               letter-spacing: 0.5px; }}

  /* ── Accommodation ──────────────────── */
  .accom-photo-wrap {{ position: relative; border-radius: 12px; overflow: hidden;
                       margin-bottom: 16px; }}
  .accom-photo {{ width: 100%; display: block; max-height: 220px; object-fit: cover; }}
  .photo-badge {{ position: absolute; bottom: 10px; right: 10px; background: rgba(0,0,0,0.75);
                  color: #fff; font-size: 12px; font-weight: 600; padding: 5px 10px;
                  border-radius: 20px; backdrop-filter: blur(4px); }}
  .accom-meta {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 12px; }}
  .accom-meta-item {{ background: #161616; border: 1px solid #222; border-radius: 8px;
                      padding: 10px 12px; }}
  .meta-lbl {{ font-size: 10px; color: #666; text-transform: uppercase; letter-spacing: 0.5px; }}
  .meta-val {{ font-size: 14px; font-weight: 700; margin-top: 2px; }}
  .addr-link {{ color: #c9a84c; font-size: 14px; font-weight: 600;
                display: inline-flex; align-items: center; gap: 4px; margin-top: 10px; }}

  /* ── Services list ──────────────────── */
  .services-list {{ list-style: none; display: flex; flex-direction: column; gap: 8px; }}
  .services-list li {{ display: flex; align-items: flex-start; gap: 10px; font-size: 14px;
                       color: #ccc; }}
  .services-list li::before {{ content: "✓"; color: #c9a84c; font-weight: 800;
                                flex-shrink: 0; margin-top: 1px; }}

  /* ── Itinerary ──────────────────────── */
  .day-group {{ margin-bottom: 20px; }}
  .day-label {{ font-size: 12px; font-weight: 700; letter-spacing: 1px;
                text-transform: uppercase; color: #c9a84c; margin-bottom: 10px; }}
  .event-card {{ background: #161616; border: 1px solid #222; border-radius: 10px;
                 padding: 14px; margin-bottom: 8px; }}
  .event-top {{ display: flex; align-items: flex-start; gap: 12px; }}
  .event-emoji {{ font-size: 22px; flex-shrink: 0; }}
  .event-info {{ flex: 1; min-width: 0; }}
  .event-name {{ font-size: 15px; font-weight: 700; }}
  .event-time {{ font-size: 12px; color: #888; margin-top: 2px; }}
  .event-price {{ font-size: 14px; font-weight: 800; color: #c9a84c; white-space: nowrap; }}
  .event-desc {{ font-size: 13px; color: #999; margin-top: 8px; line-height: 1.5;
                 border-top: 1px solid #222; padding-top: 8px; }}

  /* ── Pricing table ──────────────────── */
  .price-table {{ width: 100%; }}
  .price-row {{ display: flex; justify-content: space-between; align-items: center;
                padding: 9px 0; border-bottom: 1px solid #1a1a1a; font-size: 14px; }}
  .price-row:last-child {{ border-bottom: none; }}
  .price-row .label {{ color: #aaa; }}
  .price-row .val {{ font-weight: 700; }}
  .price-group-head {{ font-size: 11px; font-weight: 700; letter-spacing: 1.5px;
                       text-transform: uppercase; color: #c9a84c; padding: 14px 0 6px; }}
  .price-total-row {{ display: flex; justify-content: space-between; align-items: center;
                      background: #c9a84c; border-radius: 10px; padding: 14px 16px;
                      margin-top: 16px; }}
  .price-total-row .label {{ font-size: 14px; font-weight: 700; color: #000; }}
  .price-total-row .val   {{ font-size: 20px; font-weight: 900; color: #000; }}
  .per-person {{ text-align: center; color: #888; font-size: 13px; margin-top: 10px; }}
  .per-person strong {{ color: #f0f0f0; }}

  /* ── Next steps ─────────────────────── */
  .steps {{ display: flex; flex-direction: column; gap: 14px; }}
  .step  {{ display: flex; gap: 14px; align-items: flex-start; }}
  .step-num {{ width: 32px; height: 32px; background: #c9a84c; border-radius: 50%;
               display: flex; align-items: center; justify-content: center;
               font-size: 14px; font-weight: 800; color: #000; flex-shrink: 0; }}
  .step-text h3 {{ font-size: 15px; font-weight: 700; margin-bottom: 2px; }}
  .step-text p  {{ font-size: 13px; color: #888; }}

  /* ── CTA ────────────────────────────── */
  .cta-section {{ padding: 32px 20px; text-align: center; }}
  .cta-section p {{ color: #888; font-size: 14px; margin-bottom: 24px; line-height: 1.5; }}
  .cta-btn {{ display: inline-block; background: #c9a84c; color: #000;
              font-size: 17px; font-weight: 800; padding: 17px 40px;
              border-radius: 12px; letter-spacing: -0.2px; width: 100%;
              transition: opacity .2s; }}
  .cta-btn:hover {{ opacity: 0.88; }}
  .down-note {{ font-size: 12px; color: #666; margin-top: 10px; }}

  /* ── Sticky contact bar ─────────────── */
  .contact-bar {{ position: fixed; bottom: 0; left: 0; right: 0;
                  background: #111; border-top: 1px solid #222;
                  display: flex; z-index: 100; }}
  .contact-btn {{ flex: 1; display: flex; flex-direction: column; align-items: center;
                  justify-content: center; padding: 10px 0; font-size: 11px;
                  font-weight: 600; color: #aaa; letter-spacing: 0.3px;
                  transition: color .15s; }}
  .contact-btn:hover {{ color: #fff; }}
  .contact-btn .icon {{ font-size: 20px; margin-bottom: 2px; }}
  .contact-btn + .contact-btn {{ border-left: 1px solid #222; }}

  /* ── Desktop ─────────────────────────── */
  @media (min-width: 640px) {{
    body {{ max-width: 680px; margin: 0 auto; }}
    .hero img {{ max-height: 360px; border-radius: 0 0 16px 16px; }}
    .section {{ padding: 32px 28px; }}
  }}
</style>
</head>
<body>

<!-- HERO -->
<div class="hero">
  <img src="https://documint.s3.amazonaws.com/accounts/61db2ecae636260004b12f5b/assets/686c1ab7930cb27324a72381"
       alt="Connected Montreal">
</div>

<!-- AS SEEN IN -->
<div class="press-bar">
  <div class="press-label">As Seen In</div>
  <div class="press-logos">
    <img src="https://documint.s3.amazonaws.com/accounts/61db2ecae636260004b12f5b/assets/64cc14cbc102014a1f1fbf2f" alt="Eater">
    <img src="https://documint.s3.amazonaws.com/accounts/61db2ecae636260004b12f5b/assets/64cc14cbc102014a1f1fbf30" alt="Fast Company">
    <img src="https://documint.s3.amazonaws.com/accounts/61db2ecae636260004b12f5b/assets/64cc14cbc102014a1f1fbf31" alt="Vice">
  </div>
</div>

<!-- CLIENT INFO BAND -->
<div class="section">
  <div class="section-title">Your Trip</div>
  <h2>{first}'s Montreal Bachelor Party</h2>
  <p style="color:#888;margin:6px 0 20px;font-size:14px;">{full_name}</p>
  <div class="info-band">
    <div class="info-chip">
      <div class="chip-val">{doa or '—'}</div>
      <div class="chip-lbl">Date of Arrival</div>
    </div>
    <div class="info-chip">
      <div class="chip-val">{people or '—'}</div>
      <div class="chip-lbl">Guests</div>
    </div>
    <div class="info-chip">
      <div class="chip-val">{nights or '—'}</div>
      <div class="chip-lbl">Nights</div>
    </div>
  </div>
</div>

<!-- ACCOMMODATIONS -->
<div class="section">
  <div class="section-title">Accommodations</div>
  {accom_name_html}
  {accom_photo_wrap}
  {accom_addr_html}
  <div class="accom-meta">
    <div class="accom-meta-item">
      <div class="meta-lbl">Nights</div>
      <div class="meta-val">{nights or '—'}</div>
    </div>
    <div class="accom-meta-item">
      <div class="meta-lbl">Guests</div>
      <div class="meta-val">{people or '—'}</div>
    </div>
  </div>
</div>

<!-- WHAT'S INCLUDED -->
{services_section}

<!-- ITINERARY -->
{itinerary_section}

<!-- PRICING -->
<div class="section">
  <div class="section-title">Pricing</div>
  <div class="price-table">
    <div class="price-group-head">Services</div>
    <div class="price-row"><span class="label">Service Subtotal</span><span class="val">{service_subtotal}</span></div>
    <div class="price-row"><span class="label">HST</span><span class="val">{service_hst}</span></div>
    <div class="price-row"><span class="label">QST</span><span class="val">{service_qst}</span></div>
    <div class="price-row"><span class="label">Credit Card Fee</span><span class="val">{service_cc_fee}</span></div>
    <div class="price-row"><span class="label" style="font-weight:700;color:#f0f0f0;">Services Total</span><span class="val" style="color:#c9a84c;">{service_total}</span></div>

    <div class="price-group-head">Accommodation</div>
    <div class="price-row"><span class="label">Accommodation Subtotal</span><span class="val">{accom_subtotal}</span></div>
    <div class="price-row"><span class="label">HST</span><span class="val">{accom_hst}</span></div>
    <div class="price-row"><span class="label">QST</span><span class="val">{accom_qst}</span></div>
    <div class="price-row"><span class="label">Hospitality Tax</span><span class="val">{accom_hosp_tax}</span></div>
    <div class="price-row"><span class="label">Credit Card Fee</span><span class="val">{accom_cc_fee}</span></div>
    <div class="price-row"><span class="label" style="font-weight:700;color:#f0f0f0;">Accommodation Total</span><span class="val" style="color:#c9a84c;">{accom_total}</span></div>

    <div class="price-total-row">
      <span class="label">Grand Total</span>
      <span class="val">{grand_total}</span>
    </div>
    <div class="per-person">That's <strong>{per_person} per person</strong> for {people or 'your group'} guests</div>
    {down_pay_row}
  </div>
</div>

<!-- NEXT STEPS -->
<div class="section">
  <div class="section-title">Next Steps</div>
  <div class="steps">
    <div class="step">
      <div class="step-num">1</div>
      <div class="step-text">
        <h3>Review your quote</h3>
        <p>Look through your itinerary and pricing. Any questions? Text or email us — we're fast.</p>
      </div>
    </div>
    <div class="step">
      <div class="step-num">2</div>
      <div class="step-text">
        <h3>Pay your deposit</h3>
        <p>Secure your weekend with a deposit. Everything is locked in once payment clears.</p>
      </div>
    </div>
    <div class="step">
      <div class="step-num">3</div>
      <div class="step-text">
        <h3>We handle the rest</h3>
        <p>Sit back. We confirm every venue, manage all bookings, and brief you the week before.</p>
      </div>
    </div>
  </div>
</div>

<!-- BOOK MY TRIP CTA -->
<div class="cta-section">
  <p>Ready to make it official? Pay your deposit and lock in your dates — spots fill fast.</p>
  {cta_btn_html}
  {down_note_html}
</div>

<!-- STICKY CONTACT BAR -->
<div class="contact-bar">
  <a class="contact-btn" href="sms:+15143496565">
    <span class="icon">💬</span>Text Us
  </a>
  <a class="contact-btn" href="mailto:Oren@connectedmontreal.com">
    <span class="icon">✉️</span>Email
  </a>
  <button class="contact-btn" onclick="sharePortal()">
    <span class="icon">🔗</span>Share
  </button>
</div>

<script>
function sharePortal() {{
  const url = "{current_url}";
  if (navigator.share) {{
    navigator.share({{ title: "My Montreal Bachelor Party Quote", url: url }})
      .catch(() => copyLink(url));
  }} else {{
    copyLink(url);
  }}
}}
function copyLink(url) {{
  navigator.clipboard.writeText(url).then(() => {{
    alert("Link copied to clipboard!");
  }}).catch(() => {{
    prompt("Copy this link:", url);
  }});
}}
</script>
</body>
</html>"""

    return html


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
