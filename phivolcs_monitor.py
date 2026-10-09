import requests
from bs4 import BeautifulSoup
import time
import json
import os
from datetime import datetime
from flask import Flask, send_file, request

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
DELAY_BETWEEN_SITES = 5
CHECK_INTERVAL = 300

# ---------- SECURITY & TELEGRAM ----------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
SEND_TELEGRAM_ALERTS = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
ACCESS_KEY = os.getenv("ACCESS_KEY", "CHANGE-ME-TO-YOUR-SECRET-KEY")

# ---------- ALLOWED WEBSITES (CORS) ----------
ALLOWED_ORIGINS = [
    "https://your-vercel-site.vercel.app",  # ← PALITAN MO ITO NG WEBSITE MO
    "http://localhost:3000",
    "http://127.0.0.1:5500"
]
# ========================================

last_earthquakes = set()
last_tsunami = set()

app = Flask(__name__)

# ─────────── SECURITY: CORS PROTECTION ───────────
@app.after_request
def cors_protect(response):
    origin = request.headers.get("Origin", "")
    if origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "GET"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    else:
        response.headers["Access-Control-Allow-Origin"] = ""
    return response

# ─────────── TELEGRAM FUNCTIONS ───────────
def send_telegram_message(message):
    if not SEND_TELEGRAM_ALERTS:
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }
        resp = requests.post(url, data=data, timeout=15)
        return resp.json().get("ok", False)
    except Exception as e:
        print(f"Telegram Error: {e}")
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
📍 Location: {ts['location']}
📊 Magnitude: {ts['magnitude']}
Source: PHIVOLCS-DOST"""

# ─────────── SCRAPER FUNCTIONS ───────────
def is_in_philippines(lat, lon):
    try:
        lat = float(lat)
        lon = float(lon)
        return PH_LAT_MIN <= lat <= PH_LAT_MAX and PH_LON_MIN <= lon <= PH_LON_MAX
    except:
        return False

def scrape_earthquake_page():
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Checking Earthquake page...")
    try:
        resp = requests.get(SITES["earthquake"], headers=HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.find_all("tr")
        filtered = []
        
        for row in rows:
            cols = [td.get_text(strip=True) for td in row.find_all("td")]
            if len(cols) < 6: continue
            try:
                mag = float(cols[4])
                lat = float(cols[1])
                lon = float(cols[2])
            except: continue
            
            if mag >= MIN_MAG and is_in_philippines(lat, lon):
                eq_id = f"{cols[0]}|{mag}|{lat}|{lon}"
                filtered.append({
                    "id": eq_id,
                    "datetime": cols[0],
                    "latitude": lat,
                    "longitude": lon,
                    "depth_km": cols[3],
                    "magnitude": mag,
                    "location": cols[5]
                })
        print(f"Found {len(filtered)} earthquake(s) ≥{MIN_MAG}.0 in the Philippines")
        return filtered
    except Exception as e:
        print(f"❌ Error: {e}")
        return []

def scrape_tsunami_page():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Checking Tsunami page...")
    try:
        resp = requests.get(SITES["tsunami"], headers=HEADERS, timeout=30)
        resp.raise_for_status()
        
        if "NO TSUNAMI THREAT" in resp.text.upper():
            print("ℹ️ No Tsunami Threat — skipped")
            return []
        
        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.find_all("tr")
        filtered = []
        
        for row in rows:
            cols = [td.get_text(strip=True) for td in row.find_all("td")]
            if len(cols) < 5: continue
            if "NO TSUNAMI THREAT" in " ".join(cols).upper(): continue
            
            ts_id = f"{cols[0]}|{cols[4]}|{cols[5]}"
            filtered.append({
                "id": ts_id,
                "datetime": cols[0],
                "latitude": cols[1],
                "longitude": cols[2],
                "depth_km": cols[3],
                "magnitude": cols[4],
                "location": cols[5] if len(cols)>5 else "",
                "advisory": cols[6] if len(cols)>6 else "Tsunami Advisory"
            })
        if filtered:
            print(f"⚠️ Found {len(filtered)} tsunami advisory(ies)")
        return filtered
    except Exception as e:
        print(f"❌ Error: {e}")
        return []

# ─────────── SAVE TO JSON ───────────
def save_json(data, filename):
    if not data:
        data = []
    export_data = [{k: v for k, v in item.items() if k != "id"} for item in data]
    with open(f"{filename}.json", "w", encoding="utf-8") as f:
        json.dump(export_data, f, ensure_ascii=False, indent=2)
    print(f"💾 Saved: {filename}.json ({len(export_data)} records)")

# ─────────── PROTECTED API ENDPOINTS ───────────
def check_key():
    return request.args.get("key", "") == ACCESS_KEY

@app.route('/')
def home():
    return "✅ PHIVOLCS Monitor — Access Protected"

@app.route('/earthquakes.json')
def get_earthquakes_json():
    if not check_key():
        return "❌ Access Denied — Invalid Key", 403
    return send_file('earthquakes.json', mimetype='application/json')

@app.route('/tsunami.json')
def get_tsunami_json():
    if not check_key():
        return "❌ Access Denied — Invalid Key", 403
    return send_file('tsunami.json', mimetype='application/json')

# ─────────── MAIN MONITOR ───────────
def monitor_loop():
    global last_earthquakes, last_tsunami
    
    print("\n" + "="*55)
    print("  🔒 PHIVOLCS MONITOR — PROTECTED VERSION")
    print("  M4.0+ PH Only | Tsunami Threats Only | Telegram Alerts")
    print("="*55)
    
    if SEND_TELEGRAM_ALERTS:
        print("📱 Telegram Alerts: ENABLED")
    else:
        print("⚠️ Telegram Alerts: DISABLED")
    
    print("\n📥 Initial check...")
    initial_eq = scrape_earthquake_page()
    for eq in initial_eq:
        last_earthquakes.add(eq["id"])
    save_json(initial_eq, "earthquakes")
    
    time.sleep(DELAY_BETWEEN_SITES)
    
    initial_ts = scrape_tsunami_page()
    for ts in initial_ts:
        last_tsunami.add(ts["id"])
    save_json(initial_ts, "tsunami")
    
    print("\n🚀 Monitoring started! Access key enabled.\n")
    
    while True:
        try:
            eq_list = scrape_earthquake_page()
            for eq in eq_list:
                if eq["id"] not in last_earthquakes:
                    print(f"🆕 NEW EARTHQUAKE: M{eq['magnitude']} — {eq['location']}")
                    send_telegram_message(format_earthquake_alert(eq))
                    last_earthquakes.add(eq["id"])
            save_json(eq_list, "earthquakes")
            
            time.sleep(DELAY_BETWEEN_SITES)
            
            ts_list = scrape_tsunami_page()
            for ts in ts_list:
                if ts["id"] not in last_tsunami:
                    print(f"🆕 NEW TSUNAMI ADVISORY!")
                    send_telegram_message(format_tsunami_alert(ts))
                    last_tsunami.add(ts["id"])
            save_json(ts_list, "tsunami")
            
            print(f"⏰ Next check in {CHECK_INTERVAL/60:.1f} min\n")
            time.sleep(CHECK_INTERVAL)
        except KeyboardInterrupt:
            print("\n⏹️ Stopped")
            break
        except Exception as e:
            print(f"⚠️ Error: {e} — retrying in 60s\n")
            time.sleep(60)

# ─────────── RUN ───────────
if __name__ == "__main__":
    from threading import Thread
    def run_flask():
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
    Thread(target=run_flask, daemon=True).start()
    print("🌐 API Running — Access key & CORS protection enabled\n")
    monitor_loop()
