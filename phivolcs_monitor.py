import requests
from bs4 import BeautifulSoup
import time
import json
import os
from datetime import datetime, timedelta
from flask import Flask, send_file, request
from threading import Thread

# ============= CONFIGURATION =============
SITES = {
    "tsunami": "https://tsunami.phivolcs.dost.gov.ph/",
    "earthquake": "https://earthquake.phivolcs.dost.gov.ph/EQLatest.html"
}

HEADERS = {
    "User-Agent": "PH-Earthquake-Monitor/1.0 (your-email@example.com)"
}

PH_LAT_MIN, PH_LAT_MAX = 4.0, 22.0
PH_LON_MIN, PH_LON_MAX = 116.0, 127.0
MIN_MAG = 4.0
CHECK_INTERVAL = 60

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
SEND_TELEGRAM_ALERTS = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
ACCESS_KEY = os.getenv("ACCESS_KEY", "CHANGE-ME-TO-YOUR-SECRET-KEY")

ALLOWED_ORIGINS = [
    "https://steveweb.vercel.app",
    "http://localhost:3000",
    "http://127.0.0.1:5500"
]
# ========================================

SENT_EQ_FILE = "sent_earthquakes.json"
SENT_TS_FILE = "sent_tsunami.json"
PH_TZ_OFFSET = 8  # Philippines is UTC+8

app = Flask(__name__)

# ─────────── PHILIPPINES TIME ───────────
def ph_now():
    """Get current time in Philippines (UTC+8)"""
    return datetime.utcnow() + timedelta(hours=PH_TZ_OFFSET)

def ph_today_str():
    """Today's date string in PH Time: YYYY-MM-DD"""
    return ph_now().strftime("%Y-%m-%d")

def parse_phivolcs_date(date_str):
    """Try parse PHIVOLCS date format, return datetime or None"""
    date_str = date_str.strip()
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%m/%d/%Y %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%m/%d/%Y %H:%M"
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt)
        except:
            continue
    return None

def is_today_in_ph(date_str):
    """Check if event happened TODAY in PH Time"""
    today = ph_today_str()
    dt = parse_phivolcs_date(date_str)
    if dt:
        event_date = dt.strftime("%Y-%m-%d")
        return event_date == today
    # Fallback: simple string match
    return today in date_str or today.replace("-", "/") in date_str

# ─────────── LOAD/SAVE SENT IDs ───────────
def load_sent_ids(filename):
    if os.path.exists(filename):
        try:
            with open(filename, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except:
            return set()
    return set()

def save_sent_ids(filename, data_set):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(list(data_set), f)

last_earthquakes = load_sent_ids(SENT_EQ_FILE)
last_tsunami = load_sent_ids(SENT_TS_FILE)

# ─────────── CORS PROTECTION ───────────
@app.after_request
def cors_protect(response):
    origin = request.headers.get("Origin", "")
    if origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "GET"
    else:
        response.headers["Access-Control-Allow-Origin"] = ""
    return response

# ─────────── TELEGRAM ───────────
def send_telegram_message(message):
    if not SEND_TELEGRAM_ALERTS:
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }, timeout=15)
        return resp.json().get("ok", False)
    except Exception as e:
        print(f"⚠️ Telegram Error: {e}")
        return False

def format_earthquake_alert(eq):
    return f"""🌍 *NEW EARTHQUAKE ALERT*
📍 Location: {eq['location']}
🕒 Time: {eq['datetime']} (PH)
📊 Magnitude: {eq['magnitude']}
🌐 Coordinates: {eq['latitude']}°N, {eq['longitude']}°E
🔻 Depth: {eq['depth_km']} km
Source: PHIVOLCS-DOST"""

def format_tsunami_alert(ts):
    return f"""🌊 *NEW TSUNAMI ADVISORY*
⚠️ Tsunami threat detected!
🕒 Time: {ts['datetime']} (PH)
📍 Location: {ts.get('location', 'Philippines')}
📊 Magnitude: {ts['magnitude']}
Source: PHIVOLCS-DOST"""

# ─────────── SCRAPERS ───────────
def is_in_philippines(lat, lon):
    try:
        return PH_LAT_MIN <= float(lat) <= PH_LAT_MAX and PH_LON_MIN <= float(lon) <= PH_LON_MAX
    except:
        return False

def scrape_earthquakes():
    now = ph_now().strftime('%Y-%m-%d %H:%M:%S')
    today = ph_today_str()
    print(f"\n[{now}] 🕒 PH Time — Checking Earthquake page... (TODAY: {today})")
    try:
        resp = requests.get(SITES["earthquake"], headers=HEADERS, timeout=30)
        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.find_all("tr")
        results = []
        today_count = 0
        for row in rows:
            cols = [td.get_text(strip=True) for td in row.find_all("td")]
            if len(cols) < 6: continue
            try:
                mag = float(cols[4])
                lat, lon = float(cols[1]), float(cols[2])
            except: continue
            if mag >= MIN_MAG and is_in_philippines(lat, lon):
                item_id = f"{cols[0]}|{mag}|{lat}|{lon}"
                is_today = is_today_in_ph(cols[0])
                if is_today:
                    today_count += 1
                results.append({
                    "id": item_id,
                    "datetime": cols[0],
                    "latitude": lat,
                    "longitude": lon,
                    "depth_km": cols[3],
                    "magnitude": mag,
                    "location": cols[5],
                    "is_today": is_today,
                    "last_updated": now
                })
        
        # Sort: newest first (reverse order since PHIVOLCS shows oldest first)
        results.reverse()
        print(f"✅ Found {len(results)} total — {today_count} from TODAY only")
        return results
    except Exception as e:
        print(f"❌ Earthquake Scrape Error: {e}")
        return []

def scrape_tsunami():
    now = ph_now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{now}] 🕒 PH Time — Checking Tsunami page...")
    try:
        resp = requests.get(SITES["tsunami"], headers=HEADERS, timeout=30)
        if "NO TSUNAMI THREAT" in resp.text.upper():
            print("✅ No Tsunami Threat")
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.find_all("tr")
        results = []
        for row in rows:
            cols = [td.get_text(strip=True) for td in row.find_all("td")]
            if len(cols) < 5: continue
            if "NO TSUNAMI THREAT" in " ".join(cols).upper(): continue
            item_id = f"{cols[0]}|{cols[4]}|{cols[5] if len(cols)>5 else ''}"
            results.append({
                "id": item_id,
                "datetime": cols[0],
                "latitude": cols[1],
                "longitude": cols[2],
                "depth_km": cols[3],
                "magnitude": cols[4],
                "location": cols[5] if len(cols)>5 else "",
                "advisory": cols[6] if len(cols)>6 else "Tsunami Advisory",
                "last_updated": now
            })
        if results:
            results.reverse()
            print(f"⚠️ Found {len(results)} tsunami advisory(ies)")
        return results
    except Exception as e:
        print(f"❌ Tsunami Scrape Error: {e}")
        return []

# ─────────── SAVE JSON ───────────
def save_json(data, filename):
    # Filter: TODAY'S earthquakes only
    if filename == "earthquakes.json":
        data = [d for d in data if d.get("is_today", True)]
    export = [{k: v for k, v in item.items() if k not in ["id", "is_today"]} for item in data]
    with open(f"{filename}.json", "w", encoding="utf-8") as f:
        json.dump(export, f, ensure_ascii=False, indent=2)

# ─────────── API ROUTES ───────────
def check_key():
    return request.args.get("key", "") == ACCESS_KEY

@app.route('/')
def home():
    return {
        "status": "online",
        "ph_time": ph_now().strftime('%Y-%m-%d %H:%M:%S'),
        "today": ph_today_str(),
        "refresh_interval": f"{CHECK_INTERVAL} seconds",
        "allowed_website": "https://steveweb.vercel.app",
        "note": "TODAY'S events only — no old news"
    }

@app.route('/earthquakes.json')
def get_eq():
    return send_file('earthquakes.json', mimetype='application/json') if check_key() else ("❌ Access Denied", 403)

@app.route('/tsunami.json')
def get_ts():
    return send_file('tsunami.json', mimetype='application/json') if check_key() else ("❌ Access Denied", 403)

# ─────────── MAIN MONITOR ───────────
def monitor():
    global last_earthquakes, last_tsunami
    print("\n" + "="*65)
    print("  🇵🇭 PHIVOLCS MONITOR — FIXED: NO OLD NEWS")
    print(f"  🕒 PH Time: {ph_now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  ⏱️ Refresh: Every {CHECK_INTERVAL}sec | M{MIN_MAG}.0+ | TODAY ONLY ONLY")
    print("  ✅ Remembers sent alerts — NEVER re-send old news")
    print("="*65 + "\n")
    print(f"📱 Telegram: {'✅ ENABLED' if SEND_TELEGRAM_ALERTS else '❌ DISABLED'}")
    print(f"🔒 Allowed Website: https://steveweb.vercel.app")
    print(f"💾 Loaded {len(last_earthquakes)} sent IDs — will NOT re-alert\n")

    # FIRST RUN: Mark ALL existing items as already sent — NO ALERTS ON STARTUP!
    print("🔵 Initial load — marking existing events as already sent...")
    eq_list = scrape_earthquakes()
    for eq in eq_list:
        last_earthquakes.add(eq["id"])
    save_sent_ids(SENT_EQ_FILE, last_earthquakes)
    save_json(eq_list, "earthquakes")
    print(f"✅ {len(eq_list)} existing earthquakes marked — will NOT send alerts for these")

    ts_list = scrape_tsunami()
    for ts in ts_list:
        last_tsunami.add(ts["id"])
    save_sent_ids(SENT_TS_FILE, last_tsunami)
    save_json(ts_list, "tsunami")
    print(f"✅ {len(ts_list)} existing tsunami marked — will NOT send alerts for these")

    print("\n🚀 Monitoring ACTIVE — ONLY NEW events from NOW ON will trigger alerts!\n")

    while True:
        try:
            # EARTHQUAKES — TODAY'S ONLY + ONLY NEW ones trigger alert
            eq_list = scrape_earthquakes()
            new_eq_count = 0
            for eq in eq_list:
                if eq["id"] not in last_earthquakes:
                    # ✅ ONLY alert if it's TODAY'S event
                    if eq.get("is_today", False):
                        print(f"🆕 NEW TODAY'S EARTHQUAKE: M{eq['magnitude']} — {eq['location']} at {eq['datetime']}")
                        send_telegram_message(format_earthquake_alert(eq))
                        new_eq_count += 1
                    last_earthquakes.add(eq["id"])
            if new_eq_count > 0:
                save_sent_ids(SENT_EQ_FILE, last_earthquakes)
            save_json(eq_list, "earthquakes")

            # TSUNAMI
            ts_list = scrape_tsunami()
            new_ts_count = 0
            for ts in ts_list:
                if ts["id"] not in last_tsunami:
                    print(f"🆕 NEW TSUNAMI ADVISORY: M{ts['magnitude']} — {ts['datetime']}")
                    send_telegram_message(format_tsunami_alert(ts))
                    last_tsunami.add(ts["id"])
                    new_ts_count += 1
            if new_ts_count > 0:
                save_sent_ids(SENT_TS_FILE, last_tsunami)
            save_json(ts_list, "tsunami")

            time.sleep(CHECK_INTERVAL)
        except Exception as e:
            print(f"⚠️ Loop Error: {e} — retrying in 10s...")
            time.sleep(10)

if __name__ == "__main__":
    def run_flask():
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
    Thread(target=run_flask, daemon=True).start()
    monitor()
