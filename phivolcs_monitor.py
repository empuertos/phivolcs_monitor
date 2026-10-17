"""
PHIVOLCS Real-Time Monitor (v2)
- Shows ONLY today's (PH time) earthquakes & tsunami advisories
- 30s polling for near real-time updates
- Auto-resets sent-ID cache at midnight (PH)
- Atomic JSON writes, Telegram HTML escaping, robust date parsing
"""

import os
import json
import time
import html
from datetime import datetime, timedelta
from threading import Thread, Lock

import requests
from bs4 import BeautifulSoup
from flask import Flask, send_file, request, jsonify

# ============= CONFIGURATION =============
SITES = {
    "tsunami":    "https://tsunami.phivolcs.dost.gov.ph/",
    "earthquake": "https://earthquake.phivolcs.dost.gov.ph/EQLatest.html",
}

HEADERS = {
    "User-Agent": "PH-Earthquake-Monitor/2.0 (+https://steveweb.vercel.app)"
}

PH_LAT_MIN, PH_LAT_MAX = 4.0, 22.0
PH_LON_MIN, PH_LON_MAX = 116.0, 127.0
MIN_MAGNITUDE = 4.0

# ← tighter polling = closer to real-time
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "30"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
ENABLE_TELEGRAM_ALERTS = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
ACCESS_KEY = os.getenv("ACCESS_KEY", "CHANGE-ME-TO-YOUR-SECRET-KEY")

ALLOWED_ORIGINS = [
    "https://steveweb.vercel.app",
    "http://localhost:3000",
    "http://127.0.0.1:5500",
]
# =========================================

SENT_EARTHQUAKES_FILE = "sent_earthquakes.json"
SENT_TSUNAMIS_FILE    = "sent_tsunami.json"
PH_TIMEZONE_OFFSET    = 8   # UTC+8

app = Flask(__name__)
http = requests.Session()
http.headers.update(HEADERS)

# --------------------------
# LOGGING
# --------------------------
def log(msg):
    print(msg, flush=True)

# --------------------------
# TIME UTILITIES
# --------------------------
def get_ph_time():
    """Current Philippine time (UTC+8), naive datetime."""
    return datetime.utcnow() + timedelta(hours=PH_TIMEZONE_OFFSET)

def get_ph_date_string():
    return get_ph_time().strftime("%Y-%m-%d")

_DATETIME_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%d %B %Y - %I:%M %p",
    "%d %b %Y - %I:%M %p",
    "%d %B %Y %I:%M %p",
    "%d %b %Y %I:%M %p",
    "%d %B %Y - %H:%M",
    "%d %b %Y - %H:%M",
    "%B %d, %Y %I:%M %p",
    "%b %d, %Y %I:%M %p",
    "%B %d, %Y - %I:%M %p",
    "%b %d, %Y - %I:%M %p",
]

def parse_phivolcs_datetime(s):
    if not s:
        return None
    s = str(s).strip()
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None

def is_today_ph(datetime_string):
    """True only if the event happened TODAY in Philippine time."""
    if not datetime_string:
        return False

    now_ph = get_ph_time()
    parsed = parse_phivolcs_datetime(datetime_string)
    if parsed is not None:
        return parsed.date() == now_ph.date()

    # Fallback string matching (in case PHIVOLCS changes format)
    s = str(datetime_string)
    if now_ph.strftime("%Y-%m-%d") in s or now_ph.strftime("%m/%d/%Y") in s:
        return True

    day_variants = {str(now_ph.day), f"{now_ph.day:02d}"}
    month_full  = now_ph.strftime("%B")
    month_short = now_ph.strftime("%b")
    year        = now_ph.year
    for d in day_variants:
        if f"{d} {month_full} {year}" in s or f"{d} {month_short} {year}" in s:
            return True
    return False

def sort_newest_first(items):
    """Sort by parsed datetime, newest first."""
    def key(it):
        return parse_phivolcs_datetime(it.get("datetime", "")) or datetime.min
    return sorted(items, key=key, reverse=True)

# --------------------------
# SENT-ID TRACKING (today only)
# --------------------------
def load_sent_ids(filename):
    if not os.path.exists(filename):
        return set()
    try:
        with open(filename, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data) if isinstance(data, list) else set()
    except (json.JSONDecodeError, IOError):
        return set()

def save_sent_ids(filename, ids):
    try:
        tmp = filename + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted(ids), f)
        os.replace(tmp, filename)
    except IOError as e:
        log(f"⚠️ Could not save {filename}: {e}")

def prune_sent_ids(ids):
    """Keep only IDs whose embedded datetime is today (PH)."""
    return {i for i in ids if is_today_ph(i.split("|", 1)[0])}

sent_earthquake_ids = prune_sent_ids(load_sent_ids(SENT_EARTHQUAKES_FILE))
sent_tsunami_ids    = prune_sent_ids(load_sent_ids(SENT_TSUNAMIS_FILE))
save_sent_ids(SENT_EARTHQUAKES_FILE, sent_earthquake_ids)
save_sent_ids(SENT_TSUNAMIS_FILE,    sent_tsunami_ids)

# --------------------------
# SECURITY / CORS
# --------------------------
@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin", "")
    if origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"]  = origin
        response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Vary"] = "Origin"
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response

# --------------------------
# TELEGRAM
# --------------------------
def _esc(v):
    return html.escape(str(v), quote=False)

def send_telegram_message(message_text):
    if not ENABLE_TELEGRAM_ALERTS:
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        r = http.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message_text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=15)
        ok = r.json().get("ok", False)
        if not ok:
            log(f"⚠️ Telegram rejected: {r.text}")
        return ok
    except requests.exceptions.RequestException as e:
        log(f"⚠️ Telegram Error: {e}")
        return False

def format_earthquake_alert_message(eq):
    return (
        "🌍 <b>NEW EARTHQUAKE ALERT</b>\n"
        f"📍 Location: {_esc(eq['location'])}\n"
        f"🕒 Time: {_esc(eq['datetime'])} (PH)\n"
        f"📊 Magnitude: <b>{_esc(eq['magnitude'])}</b>\n"
        f"🌐 Coordinates: {_esc(eq['latitude'])}°N, {_esc(eq['longitude'])}°E\n"
        f"🔻 Depth: {_esc(eq['depth_km'])} km\n"
        "Source: PHIVOLCS-DOST"
    )

def format_tsunami_alert_message(ts):
    return (
        "🌊 <b>NEW TSUNAMI ADVISORY</b>\n"
        "⚠️ Tsunami threat detected!\n"
        f"🕒 Issued: {_esc(ts['datetime'])} (PH)\n"
        f"📍 Location: {_esc(ts.get('location') or 'Philippines')}\n"
        f"📊 Magnitude: {_esc(ts['magnitude'])}\n"
        f"Advisory: {_esc(ts.get('advisory') or 'Tsunami Information')}\n"
        "Source: PHIVOLCS-DOST"
    )

# --------------------------
# SCRAPERS
# --------------------------
def is_within_philippines(lat, lon):
    try:
        return PH_LAT_MIN <= float(lat) <= PH_LAT_MAX and PH_LON_MIN <= float(lon) <= PH_LON_MAX
    except (ValueError, TypeError):
        return False

def scrape_earthquake_data():
    now_str = get_ph_time().strftime("%Y-%m-%d %H:%M:%S")
    log(f"\n[{now_str}] 🕒 Earthquake scrape (today only)")
    try:
        r = http.get(SITES["earthquake"], timeout=30)
        soup = BeautifulSoup(r.text, "html.parser")

        results, hidden = [], 0
        for row in soup.find_all("tr"):
            cols = [c.get_text(strip=True) for c in row.find_all("td")]
            if len(cols) < 6:
                continue
            try:
                mag = float(cols[4])
                lat = float(cols[1])
                lon = float(cols[2])
            except (ValueError, IndexError):
                continue

            if mag < MIN_MAGNITUDE or not is_within_philippines(lat, lon):
                continue

            if is_today_ph(cols[0]):
                results.append({
                    "id": f"{cols[0]}|{mag}|{lat}|{lon}",
                    "datetime": cols[0],
                    "latitude": lat,
                    "longitude": lon,
                    "depth_km": cols[3],
                    "magnitude": mag,
                    "location": cols[5],
                    "is_today": True,
                    "last_updated": now_str,
                })
            else:
                hidden += 1

        results = sort_newest_first(results)
        log(f"✅ Earthquakes today: {len(results)} shown | {hidden} old hidden")
        return results
    except requests.exceptions.RequestException as e:
        log(f"❌ Earthquake scrape error: {e}")
        return []

def scrape_tsunami_data():
    now_str = get_ph_time().strftime("%Y-%m-%d %H:%M:%S")
    log(f"\n[{now_str}] 🕒 Tsunami scrape (today only)")
    try:
        r = http.get(SITES["tsunami"], timeout=30)
        if "NO TSUNAMI THREAT" in r.text.upper():
            log("✅ No tsunami threat reported.")
            return []

        soup = BeautifulSoup(r.text, "html.parser")
        results, hidden = [], 0

        for row in soup.find_all("tr"):
            cols = [c.get_text(strip=True) for c in row.find_all("td")]
            if len(cols) < 5:
                continue
            if "NO TSUNAMI THREAT" in " ".join(cols).upper():
                continue

            if is_today_ph(cols[0]):
                results.append({
                    "id": f"{cols[0]}|{cols[4]}|{cols[5] if len(cols) > 5 else ''}",
                    "datetime": cols[0],
                    "latitude": cols[1],
                    "longitude": cols[2],
                    "depth_km": cols[3],
                    "magnitude": cols[4],
                    "location": cols[5] if len(cols) > 5 else "",
                    "advisory": cols[6] if len(cols) > 6 else "Tsunami Advisory",
                    "is_today": True,
                    "last_updated": now_str,
                })
            else:
                hidden += 1

        results = sort_newest_first(results)
        if results:
            log(f"⚠️ Tsunami today: {len(results)} shown | {hidden} old hidden")
        else:
            log(f"✅ Tsunami: no today advisories | {hidden} old hidden")
        return results
    except requests.exceptions.RequestException as e:
        log(f"❌ Tsunami scrape error: {e}")
        return []

# --------------------------
# JSON OUTPUT (atomic, today only)
# --------------------------
def save_data_to_json(data, filename):
    items = [d for d in data if d.get("is_today", True)]
    export = [
        {k: v for k, v in it.items() if k not in ("id", "is_today")}
        for it in items
    ]
    tmp = filename + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(export, f, ensure_ascii=False, indent=2)
        os.replace(tmp, filename)
        log(f"💾 {filename} → {len(export)} today item(s)")
    except IOError as e:
        log(f"⚠️ Failed to write {filename}: {e}")

# --------------------------
# API ENDPOINTS
# --------------------------
def verify_access_key(req):
    return req.args.get("key", "") == ACCESS_KEY

def _send_json(filename):
    if not os.path.exists(filename):
        return jsonify([])
    resp = send_file(filename, mimetype="application/json")
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp

@app.route("/")
def status_page():
    return {
        "status": "online",
        "ph_time": get_ph_time().strftime("%Y-%m-%d %H:%M:%S"),
        "filter": "TODAY only (PH time) — auto-resets at midnight",
        "refresh_interval_seconds": CHECK_INTERVAL_SECONDS,
        "allowed_website": "https://steveweb.vercel.app",
    }

@app.route("/earthquakes.json")
def get_earthquakes_json():
    if not verify_access_key(request):
        return "❌ Access Denied — Invalid key", 403
    return _send_json("earthquakes.json")

@app.route("/tsunami.json")
def get_tsunami_json():
    if not verify_access_key(request):
        return "❌ Access Denied — Invalid key", 403
    return _send_json("tsunami.json")

# --------------------------
# MAIN MONITOR LOOP
# --------------------------
def start_monitoring():
    global sent_earthquake_ids, sent_tsunami_ids

    log("\n" + "=" * 65)
    log("  🇵🇭 PHIVOLCS MONITOR v2 — RUNNING")
    log(f"  🕒 PH time: {get_ph_time().strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"  ✅ Filter: TODAY only (auto-resets at midnight PH)")
    log(f"  ✅ Poll interval: {CHECK_INTERVAL_SECONDS}s")
    log(f"  📱 Telegram: {'ENABLED' if ENABLE_TELEGRAM_ALERTS else 'DISABLED'}")
    log("=" * 65 + "\n")

    # Mark today's existing events as already-sent (no spam on startup)
    log("🔵 Startup: seeding sent-IDs with today's events (NO alerts)...")
    for eq in scrape_earthquake_data():
        sent_earthquake_ids.add(eq["id"])
    save_sent_ids(SENT_EARTHQUAKES_FILE, sent_earthquake_ids)
    save_data_to_json(scrape_earthquake_data(), "earthquakes.json")

    for ts in scrape_tsunami_data():
        sent_tsunami_ids.add(ts["id"])
    save_sent_ids(SENT_TSUNAMIS_FILE, sent_tsunami_ids)
    save_data_to_json(scrape_tsunami_data(), "tsunami.json")

    log("🚀 Monitoring started — only NEW events will trigger alerts.\n")
    last_day = get_ph_date_string()

    while True:
        try:
            # ---- Midnight reset ----
            current_day = get_ph_date_string()
            if current_day != last_day:
                log(f"\n📅 New PH day ({current_day}) — resetting sent-ID cache.\n")
                sent_earthquake_ids.clear()
                sent_tsunami_ids.clear()
                save_sent_ids(SENT_EARTHQUAKES_FILE, sent_earthquake_ids)
                save_sent_ids(SENT_TSUNAMIS_FILE,    sent_tsunami_ids)
                last_day = current_day

            # ---- Earthquakes ----
            quakes = scrape_earthquake_data()
            new_eq = 0
            for q in quakes:
                if q["id"] in sent_earthquake_ids:
                    continue
                if q.get("is_today"):
                    log(f"🆕 NEW EARTHQUAKE: M{q['magnitude']} — {q['location']}")
                    send_telegram_message(format_earthquake_alert_message(q))
                    new_eq += 1
                sent_earthquake_ids.add(q["id"])
            if new_eq:
                save_sent_ids(SENT_EARTHQUAKES_FILE, sent_earthquake_ids)
                log(f"✅ {new_eq} new earthquake alert(s) sent.")
            save_data_to_json(quakes, "earthquakes.json")

            # ---- Tsunami ----
            tsunamis = scrape_tsunami_data()
            new_ts = 0
            for t in tsunamis:
                if t["id"] in sent_tsunami_ids:
                    continue
                if t.get("is_today"):
                    log(f"🆕 NEW TSUNAMI: M{t['magnitude']} — {t['datetime']}")
                    send_telegram_message(format_tsunami_alert_message(t))
                    new_ts += 1
                sent_tsunami_ids.add(t["id"])
            if new_ts:
                save_sent_ids(SENT_TSUNAMIS_FILE, sent_tsunami_ids)
                log(f"✅ {new_ts} new tsunami alert(s) sent.")
            save_data_to_json(tsunamis, "tsunami.json")

            time.sleep(CHECK_INTERVAL_SECONDS)

        except Exception as e:
            log(f"⚠️ Loop error: {e} — retry in 10s")
            time.sleep(10)

# --------------------------
# BOOTSTRAP (works with python + gunicorn)
# --------------------------
_monitor_lock = Lock()
_monitor_started = False

def ensure_monitor_started():
    global _monitor_started
    with _monitor_lock:
        if _monitor_started:
            return
        _monitor_started = True
    Thread(target=start_monitoring, daemon=True).start()

# Start monitor as soon as this module is imported.
# On Render/gunicorn use: gunicorn -w 1 phivolcs_monitor:app
ensure_monitor_started()

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", 10000)),
        use_reloader=False,   # prevents duplicate monitor thread
    )