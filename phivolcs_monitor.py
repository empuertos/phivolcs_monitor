import requests
from bs4 import BeautifulSoup
import time
import csv
import os
from datetime import datetime
from flask import Flask

# ============= CONFIGURATION =============
SITES = {
    "tsunami": "https://tsunami.phivolcs.dost.gov.ph/",
    "earthquake": "https://earthquake.phivolcs.dost.gov.ph/EQLatest.html"
}

HEADERS = {
    "User-Agent": "PH-Earthquake-Monitor/1.0 (your-email@example.com)"
}

# Philippines boundaries
PH_LAT_MIN, PH_LAT_MAX = 4.0, 22.0
PH_LON_MIN, PH_LON_MAX = 116.0, 127.0
MIN_MAG = 4.0
DELAY_BETWEEN_SITES = 5   # seconds
CHECK_INTERVAL = 300      # check every 5 minutes (300 seconds)

# ---------- TELEGRAM SETTINGS — from Environment Variables ----------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
SEND_TELEGRAM_ALERTS = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
# ========================================

# Track sent alerts to avoid duplicates
last_earthquakes = set()
last_tsunami = set()

# Flask app for UptimeRobot (keeps Render awake)
app = Flask(__name__)

@app.route('/')
def home():
    return "✅ PHIVOLCS Monitor is running!"

# ─────────── TELEGRAM FUNCTIONS ───────────

def send_telegram_message(message):
    """Send message to Telegram chat"""
    if not SEND_TELEGRAM_ALERTS:
        print("[Telegram] ⚠️ Missing credentials — message skipped")
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
        result = resp.json()
        if result.get("ok"):
            print("[Telegram] ✅ Message sent")
            return True
        else:
            print(f"[Telegram] ❌ Error: {result.get('description', 'Unknown error')}")
            return False
    except Exception as e:
        print(f"[Telegram] ❌ Failed: {e}")
        return False

def format_earthquake_alert(eq):
    """Format earthquake alert message"""
    return f"""🌍 *EARTHQUAKE ALERT*
📍 Location: {eq['location']}
🕒 Time: {eq['datetime']}
📊 Magnitude: {eq['magnitude']}
🌐 Coordinates: {eq['latitude']}°N, {eq['longitude']}°E
🔻 Depth: {eq['depth_km']} km
Source: PHIVOLCS-DOST"""

def format_tsunami_alert(ts):
    """Format tsunami alert message"""
    return f"""🌊 *TSUNAMI ADVISORY*
⚠️ Tsunami threat detected!
🕒 Time: {ts['datetime']}
📍 Location: {ts['location']}
📊 Magnitude: {ts['magnitude']}
📢 Advisory: {ts.get('advisory', 'See PHIVOLCS for details')}
Source: PHIVOLCS-DOST"""

# ─────────── SCRAPER FUNCTIONS ───────────

def is_in_philippines(lat, lon):
    """Check if coordinates are within Philippine territory"""
    try:
        lat = float(lat)
        lon = float(lon)
        return PH_LAT_MIN <= lat <= PH_LAT_MAX and PH_LON_MIN <= lon <= PH_LON_MAX
    except:
        return False

def scrape_earthquake_page():
    """Scrape earthquake page — M4.0+ in Philippines only"""
    print("\n" + "="*55)
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Checking Earthquake page...")
    try:
        resp = requests.get(SITES["earthquake"], headers=HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.find_all("tr")
        
        filtered = []
        for row in rows:
            cols = [td.get_text(strip=True) for td in row.find_all("td")]
            if len(cols) < 6:
                continue
            try:
                mag = float(cols[4])
                lat = float(cols[1])
                lon = float(cols[2])
            except:
                continue
            
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
        print(f"❌ Error scraping earthquake page: {e}")
        return []

def scrape_tsunami_page():
    """Scrape tsunami page — threats only, skip 'No Tsunami Threat'"""
    print("\n" + "="*55)
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
            if len(cols) < 5:
                continue
            
            advisory_text = " ".join(cols[5:]).upper() if len(cols) > 5 else ""
            if "NO TSUNAMI THREAT" in advisory_text:
                continue
            
            ts_id = f"{cols[0]}|{cols[4]}|{cols[5]}"
            filtered.append({
                "id": ts_id,
                "datetime": cols[0],
                "latitude": cols[1],
                "longitude": cols[2],
                "depth_km": cols[3],
                "magnitude": cols[4],
                "location": cols[5] if len(cols) > 5 else "",
                "advisory": cols[6] if len(cols) > 6 else "Tsunami Advisory"
            })
        
        if filtered:
            print(f"⚠️ Found {len(filtered)} tsunami advisory(ies) with threat")
        else:
            print("ℹ️ No active tsunami threat")
        return filtered
    except Exception as e:
        print(f"❌ Error scraping tsunami page: {e}")
        return []

def save_to_csv(data, filename):
    """Save data to CSV file"""
    if not data:
        return
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fn = f"{filename}_{ts}.csv"
    try:
        with open(fn, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=data[0].keys())
            writer.writeheader()
            writer.writerows(data)
        print(f"💾 Saved: {fn}")
    except Exception as e:
        print(f"⚠️ Could not save CSV: {e}")

def check_for_new_alerts():
    """Check for new entries and send alerts"""
    global last_earthquakes, last_tsunami
    
    # Check Earthquakes
    eq_list = scrape_earthquake_page()
    new_eq = 0
    for eq in eq_list:
        if eq["id"] not in last_earthquakes:
            print(f"🆕 New Earthquake: M{eq['magnitude']} — {eq['location']}")
            send_telegram_message(format_earthquake_alert(eq))
            last_earthquakes.add(eq["id"])
            new_eq += 1
    
    if new_eq == 0 and eq_list:
        print("✅ No new earthquakes")
    
    time.sleep(DELAY_BETWEEN_SITES)
    
    # Check Tsunami
    ts_list = scrape_tsunami_page()
    new_ts = 0
    for ts in ts_list:
        if ts["id"] not in last_tsunami:
            print(f"🆕 New Tsunami Advisory!")
            send_telegram_message(format_tsunami_alert(ts))
            last_tsunami.add(ts["id"])
            new_ts += 1
    
    if new_ts == 0:
        print("✅ No new tsunami advisories")
    
    # Save data
    if eq_list:
        save_to_csv(eq_list, "phivolcs_earthquakes")
    if ts_list:
        save_to_csv(ts_list, "phivolcs_tsunami")
    
    print(f"\n⏰ Next check in {CHECK_INTERVAL/60:.1f} minutes\n")
    print("-" * 55)

def monitor_loop():
    """Main monitoring loop"""
    global last_earthquakes, last_tsunami
    
    print("\n" + "=" * 55)
    print("  PHIVOLCS Earthquake & Tsunami Monitor")
    print("  M4.0+ in Philippines | Tsunami Threats Only")
    print("=" * 55)
    
    # Telegram status
    if SEND_TELEGRAM_ALERTS:
        print("📱 Telegram Alerts: ENABLED")
    else:
        print("⚠️ Telegram Alerts: DISABLED — check env vars")
    
    # Initial load — don't alert on existing data
    print("\n📥 Performing initial check...")
    initial_eq = scrape_earthquake_page()
    for eq in initial_eq:
        last_earthquakes.add(eq["id"])
    print(f"✅ Loaded {len(last_earthquakes)} past earthquake records")
    
    time.sleep(DELAY_BETWEEN_SITES)
    
    initial_ts = scrape_tsunami_page()
    for ts in initial_ts:
        last_tsunami.add(ts["id"])
    print(f"✅ Loaded {len(last_tsunami)} past tsunami records")
    
    print("\n🚀 Monitoring started! Waiting for new events...\n")
    
    # Main loop
    while True:
        try:
            check_for_new_alerts()
            time.sleep(CHECK_INTERVAL)
        except KeyboardInterrupt:
            print("\n\n⏹️ Stopped by user")
            break
        except Exception as e:
            print(f"\n⚠️ Loop error: {e}")
            print(f"Retrying in {CHECK_INTERVAL/60:.1f} minutes...\n")
            time.sleep(CHECK_INTERVAL)

# ─────────── RUN ───────────

if __name__ == "__main__":
    # Start Flask server for UptimeRobot in background
    from threading import Thread
    def run_flask():
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
    
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print("🌐 Web server started — ready for UptimeRobot")
    
    # Start monitoring
    monitor_loop()
