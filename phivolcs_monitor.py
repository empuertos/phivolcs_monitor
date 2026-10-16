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
PH_TZ_OFFSET = 8

app = Flask(__name__)

# ─────────── PHILIPPINES TIME ───────────
def ph_now():
    return datetime.utcnow() + timedelta(hours=PH_TZ_OFFSET)

def ph_today_str():
    return ph_now().strftime("%Y-%m-%d")

def parse_phivolcs_date(date_str):
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

def is_within_24_hours(date_str):
    now = ph_now()
    twenty_four_hours_ago = now - timedelta(hours=24)
    
    dt = parse_phivolcs_date(date_str)
    if not dt:
        today = ph_today_str()
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        if today in date_str or today.replace("-","/") in date_str:
            return True
        if yesterday in date_str or yesterday.replace("-","/") in date_str:
            return True
        return False
    
    event_dt = dt.replace(tzinfo=None)
    return twenty_four_hours_ago <= event_dt <= now

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
    print(f"\n[{now}] 🕒 PH Time — Checking Earthquake page... (LAST 24 HOURS)")
    try:
        resp = requests.get(SITES["earthquake"], headers=HEADERS, timeout=30)
        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.find_all("tr")
        results = []
        recent_count = 0
        old_count = 0
        
        for row in rows:
            cols = [td.get_text(strip=True) for td in row.find_all("td")]
            if len(cols) < 6: continue
            try:
                mag = float(cols[4])
                lat, lon = float(cols[1]), float(cols[2])
            except: continue
            
            if mag >= MIN_MAG and is_in_philippines(lat, lon):
                item_id = f"{cols[0]}|{mag}|{lat}|{lon}"
                within_24h = is_within_24_hours(cols[0])
                
                if within_24h:
                    recent_count += 1
                    results.append({
                        "id": item_id,
                        "datetime": cols[0],
                        "latitude": lat,
                        "longitude": lon,
                        "depth_km": cols[3],
                        "magnitude": mag,
                        "location": cols[5],
                        "is_within_24h": True,
                        "last_updated": now
                    })
                else:
                    old_count += 1
                    print(f"🗑️ SKIPPED (too old): {cols[0]} | M{mag} — NOT sent to Telegram")
        
        results.reverse()
        print(f"✅ Result: {recent_count} events (last 24hrs) | {old_count} older skipped")
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
    print(f"📅 Filtering — keeping events from the last 24 hours only")
    
    if filename == "earthquakes.json":
        filtered = []
        for d in data:
            if d.get("is_within_24h", True):
                filtered.append(d)
            else:
                print(f"🗑️ Removed from list: {d.get('datetime', 'unknown')}")
        data = filtered
    
    export = [{k: v for k, v in item.items() if k not in ["id", "is_within_24h"]} for item in data]
    with open(f"{filename}.json", "w", encoding="utf-8") as f:
        json.dump(export, f, ensure_ascii=False, indent=2)
    
    print(f"✅ Saved {filename}.json — {len(data)} items in list")

# ─────────── API ROUTES ───────────
def check_key():
    return request.args.get("key", "") == ACCESS_KEY

@app.route('/')
def home():
    return {
        "status": "online",
        "ph_time": ph_now().strftime('%Y-%m-%d %H:%M:%S'),
        "filter": "Last 24 hours only — no old alerts",
        "refresh_interval": f"{CHECK_INTERVAL} seconds",
        "allowed_website": "https://steveweb.vercel.app",
        "note": "Old earthquakes (>24hrs) are NEVER sent to Telegram — only NEW events trigger alerts"
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
    print("  🇵🇭 PHIVOLCS MONITOR — FINAL VERSION")
    print(f"  🕒 PH Time: {ph_now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  ⏱️ Refresh: Every {CHECK_INTERVAL}sec | M{MIN_MAG}.0+ | Last 24 Hours")
    print("  ✅ Old events NEVER sent — only NEW events trigger alerts")
    print("  ✅ Sent IDs saved — duplicates blocked even after restart")
    print("="*65 + "\n")
    print(f"📱 Telegram: {'✅ ENABLED' if SEND_TELEGRAM_ALERTS else '❌ DISABLED'}")
    print(f"🔒 Allowed Website: https://steveweb.vercel.app")
    print(f"💾 Loaded {len(last_earthquakes)} sent IDs — will NOT re-alert\n")

    # MARK EXISTING EVENTS AS SENT — NO ALERTS ON STARTUP
    print("🔵 STEP 1: Marking ALL existing events as ALREADY SENT — NO ALERTS ON STARTUP")
    eq_list = scrape_earthquakes()
    for eq in eq_list:
        last_earthquakes.add(eq["id"])
    save_sent_ids(SENT_EQ_FILE, last_earthquakes)
    save_json(eq_list, "earthquakes")
    print(f"✅ {len(eq_list)} existing earthquakes — MARKED AS SENT • NO TELEGRAM ALERT")

    ts_list = scrape_tsunami()
    for ts in ts_list:
        last_tsunami.add(ts["id"])
    save_sent_ids(SENT_TS_FILE, last_tsunami)
    save_json(ts_list, "tsunami")
    print(f"✅ {len(ts_list)} existing tsunami — MARKED AS SENT • NO TELEGRAM ALERT")

    print("\n🚀 MONITORING STARTED — ONLY NEW EVENTS FROM NOW ON WILL SEND ALERTS!\n")

    while True:
        try:
            # EARTHQUAKES — NEW + WITHIN 24H = ALERT
            eq_list = scrape_earthquakes()
            new_eq_count = 0
            for eq in eq_list:
                if eq["id"] not in last_earthquakes:
                    if eq.get("is_within_24h", False):
                        print(f"🆕 NEW ALERT SENT: M{eq['magnitude']} — {eq['location']} at {eq['datetime']}")
                        send_telegram_message(format_earthquake_alert(eq))
                        new_eq_count += 1
                    else:
                        print(f"⏳ TOO OLD — NOT SENT: {eq['datetime']} | M{eq['magnitude']}")
                    last_earthquakes.add(eq["id"])
            if new_eq_count > 0:
                save_sent_ids(SENT_EQ_FILE, last_earthquakes)
                print(f"✅ {new_eq_count} NEW earthquake alert(s) sent")
            save_json(eq_list, "earthquakes")

            # TSUNAMI
            ts_list = scrape_tsunami()
            new_ts_count = 0
            for ts in ts_list:
                if ts["id"] not in last_tsunami:
                    print(f"🆕 NEW TSUNAMI ALERT SENT: M{ts['magnitude']} — {ts['datetime']}")
                    send_telegram_message(format_tsunami_alert(ts))
                    last_tsunami.add(ts["id"])
                    new_ts_count += 1
            if new_ts_count > 0:
                save_sent_ids(SENT_TS_FILE, last_tsunami)
                print(f"✅ {new_ts_count} NEW tsunami alert(s) sent")
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
