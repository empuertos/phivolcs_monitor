import requests
from bs4 import BeautifulSoup
import time
import json
import os
from datetime import datetime
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
CHECK_INTERVAL = 60  # ⏱️ UPDATED: 60 seconds = 1 minute

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
SEND_TELEGRAM_ALERTS = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
ACCESS_KEY = os.getenv("ACCESS_KEY", "CHANGE-ME-TO-YOUR-SECRET-KEY")

ALLOWED_ORIGINS = [
    "https://your-vercel-site.vercel.app",
    "http://localhost:3000",
    "http://127.0.0.1:5500"
]
# ========================================

last_earthquakes = set()
last_tsunami = set()

app = Flask(__name__)

# ─────────── SECURITY ───────────
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
    return f"""🌍 *EARTHQUAKE ALERT*
📍 Location: {eq['location']}
🕒 Time: {eq['datetime']}
📊 Magnitude: {eq['magnitude']}
🌐 Coordinates: {eq['latitude']}°N, {eq['longitude']}°E
🔻 Depth: {eq['depth_km']} km
Source: PHIVOLCS-DOST"""

def format_tsunami_alert(ts):
    return f"""🌊 *TSUNAMI ADVISORY*
⚠️ Tsunami threat detected!
🕒 Time: {ts['datetime']}
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
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"\n[{now}] 🔄 Checking Earthquake page...")
    try:
        resp = requests.get(SITES["earthquake"], headers=HEADERS, timeout=30)
        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.find_all("tr")
        results = []
        for row in rows:
            cols = [td.get_text(strip=True) for td in row.find_all("td")]
            if len(cols) < 6: continue
            try:
                mag = float(cols[4])
                lat, lon = float(cols[1]), float(cols[2])
            except: continue
            if mag >= MIN_MAG and is_in_philippines(lat, lon):
                item_id = f"{cols[0]}|{mag}|{lat}|{lon}"
                results.append({
                    "id": item_id,
                    "datetime": cols[0],
                    "latitude": lat,
                    "longitude": lon,
                    "depth_km": cols[3],
                    "magnitude": mag,
                    "location": cols[5],
                    "last_updated": now
                })
        print(f"✅ Found {len(results)} earthquake(s) ≥{MIN_MAG}.0")
        return results
    except Exception as e:
        print(f"❌ Earthquake Scrape Error: {e}")
        return []

def scrape_tsunami():
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{now}] 🔄 Checking Tsunami page...")
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
            print(f"⚠️ Found {len(results)} tsunami advisory(ies)")
        return results
    except Exception as e:
        print(f"❌ Tsunami Scrape Error: {e}")
        return []

# ─────────── SAVE JSON ───────────
def save_json(data, filename):
    export = [{k: v for k, v in item.items() if k != "id"} for item in data]
    with open(f"{filename}.json", "w", encoding="utf-8") as f:
        json.dump(export, f, ensure_ascii=False, indent=2)

# ─────────── API ROUTES ───────────
def check_key():
    return request.args.get("key", "") == ACCESS_KEY

@app.route('/')
def home():
    return {
        "status": "online",
        "last_updated": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "refresh_interval": f"{CHECK_INTERVAL} seconds"
    }

@app.route('/earthquakes.json')
def get_eq():
    return send_file('earthquakes.json', mimetype='application/json') if check_key() else ("❌ Access Denied", 403)

@app.route('/tsunami.json')
def get_ts():
    return send_file('tsunami.json', mimetype='application/json') if check_key() else ("❌ Access Denied", 403)

# ─────────── MAIN LOOP ───────────
def monitor():
    global last_earthquakes, last_tsunami
    print("\n" + "="*60)
    print("  🇵🇭 PHIVOLCS MONITOR — UPDATED 2026-10-15")
    print(f"  ⏱️ Refresh: Every {CHECK_INTERVAL} sec | M{MIN_MAG}.0+ PH Only | Tsunami Only")
    print("="*60 + "\n")
    print(f"📱 Telegram: {'✅ ENABLED' if SEND_TELEGRAM_ALERTS else '❌ DISABLED'}")

    # Initial run
    eq_list = scrape_earthquakes()
    for eq in eq_list: last_earthquakes.add(eq["id"])
    save_json(eq_list, "earthquakes")

    ts_list = scrape_tsunami()
    for ts in ts_list: last_tsunami.add(ts["id"])
    save_json(ts_list, "tsunami")

    print("\n🚀 Monitoring active — checking every 60 seconds...\n")

    while True:
        try:
            # Earthquakes
            eq_list = scrape_earthquakes()
            for eq in eq_list:
                if eq["id"] not in last_earthquakes:
                    print(f"🆕 NEW: M{eq['magnitude']} — {eq['location']}")
                    send_telegram_message(format_earthquake_alert(eq))
                    last_earthquakes.add(eq["id"])
            save_json(eq_list, "earthquakes")

            # Tsunami
            ts_list = scrape_tsunami()
            for ts in ts_list:
                if ts["id"] not in last_tsunami:
                    print(f"🆕 NEW TSUNAMI ADVISORY!")
                    send_telegram_message(format_tsunami_alert(ts))
                    last_tsunami.add(ts["id"])
            save_json(ts_list, "tsunami")

            time.sleep(CHECK_INTERVAL)
        except Exception as e:
            print(f"⚠️ Loop Error: {e} — retrying...")
            time.sleep(10)

if __name__ == "__main__":
    def run_flask():
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
    Thread(target=run_flask, daemon=True).start()
    monitor()
