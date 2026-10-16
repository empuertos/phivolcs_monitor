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
MIN_MAGNITUDE = 4.0
CHECK_INTERVAL_SECONDS = 60

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ENABLE_TELEGRAM_ALERTS = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
ACCESS_KEY = os.getenv("ACCESS_KEY", "CHANGE-ME-TO-YOUR-SECRET-KEY")

ALLOWED_ORIGINS = [
    "https://steveweb.vercel.app",
    "http://localhost:3000",
    "http://127.0.0.1:5500"
]
# ========================================

SENT_EARTHQUAKES_FILE = "sent_earthquakes.json"
SENT_TSUNAMIS_FILE = "sent_tsunami.json"
PH_TIMEZONE_OFFSET = 8

app = Flask(__name__)

# --------------------------
# TIME UTILITY FUNCTIONS
# --------------------------
def get_ph_time():
    """Get current time in Philippines (UTC+8)"""
    return datetime.utcnow() + timedelta(hours=PH_TIMEZONE_OFFSET)

def get_ph_date_string():
    """Get today's date as string YYYY-MM-DD"""
    return get_ph_time().strftime("%Y-%m-%d")

def parse_phivolcs_datetime(datetime_string):
    """Try parsing different datetime formats from PHIVOLCS pages"""
    datetime_string = datetime_string.strip()
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%m/%d/%Y %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%m/%d/%Y %H:%M"
    ]
    for fmt in formats:
        try:
            return datetime.strptime(datetime_string, fmt)
        except ValueError:
            continue
    return None

def is_within_last_24_hours(datetime_string):
    """Check if a given datetime falls within the last 24 hours"""
    now = get_ph_time()
    twenty_four_hours_ago = now - timedelta(hours=24)
    
    parsed_datetime = parse_phivolcs_datetime(datetime_string)
    if not parsed_datetime:
        # Fallback: check if date string contains today or yesterday
        today = get_ph_date_string()
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        if today in datetime_string or today.replace("-", "/") in datetime_string:
            return True
        if yesterday in datetime_string or yesterday.replace("-", "/") in datetime_string:
            return True
        return False
    
    return twenty_four_hours_ago <= parsed_datetime.replace(tzinfo=None) <= now

# --------------------------
# SENT TRACKING FUNCTIONS
# --------------------------
def load_sent_ids(filename):
    """Load IDs of already sent alerts from file"""
    if os.path.exists(filename):
        try:
            with open(filename, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except (json.JSONDecodeError, IOError):
            return set()
    return set()

def save_sent_ids(filename, id_set):
    """Save IDs of sent alerts to file"""
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(list(id_set), f)

sent_earthquake_ids = load_sent_ids(SENT_EARTHQUAKES_FILE)
sent_tsunami_ids = load_sent_ids(SENT_TSUNAMIS_FILE)

# --------------------------
# SECURITY HEADERS
# --------------------------
@app.after_request
def add_cors_headers(response):
    """Allow access only from trusted websites"""
    origin = request.headers.get("Origin", "")
    if origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "GET"
    return response

# --------------------------
# TELEGRAM ALERT FUNCTIONS
# --------------------------
def send_telegram_message(message_text):
    """Send a message to Telegram chat"""
    if not ENABLE_TELEGRAM_ALERTS:
        return False
    try:
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        response = requests.post(api_url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message_text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }, timeout=15)
        return response.json().get("ok", False)
    except requests.exceptions.RequestException as error:
        print(f"⚠️ Telegram Error: {error}")
        return False

def format_earthquake_alert_message(earthquake):
    """Format earthquake data into readable Telegram message"""
    return f"""🌍 *NEW EARTHQUAKE ALERT*
📍 Location: {earthquake['location']}
🕒 Time: {earthquake['datetime']} (PH)
📊 Magnitude: {earthquake['magnitude']}
🌐 Coordinates: {earthquake['latitude']}°N, {earthquake['longitude']}°E
🔻 Depth: {earthquake['depth_km']} km
Source: PHIVOLCS-DOST"""

def format_tsunami_alert_message(tsunami):
    """Format tsunami data into readable Telegram message"""
    return f"""🌊 *NEW TSUNAMI ADVISORY*
⚠️ Tsunami threat detected!
🕒 Issued: {tsunami['datetime']} (PH)
📍 Location: {tsunami.get('location', 'Philippines')}
📊 Magnitude: {tsunami['magnitude']}
Advisory: {tsunami.get('advisory', 'Tsunami Information')}
Source: PHIVOLCS-DOST"""

# --------------------------
# DATA SCRAPERS
# --------------------------
def is_within_philippines(latitude, longitude):
    """Check if coordinates are within Philippine territory"""
    try:
        return PH_LAT_MIN <= float(latitude) <= PH_LAT_MAX and PH_LON_MIN <= float(longitude) <= PH_LON_MAX
    except (ValueError, TypeError):
        return False

def scrape_earthquake_data():
    """Fetch and parse latest earthquake data from PHIVOLCS"""
    current_time = get_ph_time().strftime('%Y-%m-%d %H:%M:%S')
    print(f"\n[{current_time}] 🕒 Checking Earthquake page... (Last 24 hours only)")
    try:
        response = requests.get(SITES["earthquake"], headers=HEADERS, timeout=30)
        soup = BeautifulSoup(response.text, "html.parser")
        table_rows = soup.find_all("tr")
        
        results = []
        visible_count = 0
        hidden_count = 0
        
        for row in table_rows:
            columns = [cell.get_text(strip=True) for cell in row.find_all("td")]
            if len(columns) < 6:
                continue
            
            try:
                magnitude = float(columns[4])
                latitude = float(columns[1])
                longitude = float(columns[2])
            except (ValueError, IndexError):
                continue
            
            if magnitude >= MIN_MAGNITUDE and is_within_philippines(latitude, longitude):
                unique_id = f"{columns[0]}|{magnitude}|{latitude}|{longitude}"
                recent_enough = is_within_last_24_hours(columns[0])
                
                if recent_enough:
                    visible_count += 1
                    results.append({
                        "id": unique_id,
                        "datetime": columns[0],
                        "latitude": latitude,
                        "longitude": longitude,
                        "depth_km": columns[3],
                        "magnitude": magnitude,
                        "location": columns[5],
                        "is_within_24h": True,
                        "last_updated": current_time
                    })
                else:
                    hidden_count += 1
                    print(f"🗑️ Hidden (too old): {columns[0]} | M{magnitude}")
        
        results.reverse()
        print(f"✅ Earthquakes: {visible_count} shown | {hidden_count} old entries hidden")
        return results
    except requests.exceptions.RequestException as error:
        print(f"❌ Earthquake Scrape Error: {error}")
        return []

def scrape_tsunami_data():
    """Fetch and parse latest tsunami advisories from PHIVOLCS"""
    current_time = get_ph_time().strftime('%Y-%m-%d %H:%M:%S')
    print(f"\n[{current_time}] 🕒 Checking Tsunami page... (Last 24 hours only)")
    try:
        response = requests.get(SITES["tsunami"], headers=HEADERS, timeout=30)
        
        if "NO TSUNAMI THREAT" in response.text.upper():
            print("✅ No Tsunami Threat — Page reports no active threat")
            return []
        
        soup = BeautifulSoup(response.text, "html.parser")
        table_rows = soup.find_all("tr")
        
        results = []
        visible_count = 0
        hidden_count = 0
        
        for row in table_rows:
            columns = [cell.get_text(strip=True) for cell in row.find_all("td")]
            if len(columns) < 5:
                continue
            if "NO TSUNAMI THREAT" in " ".join(columns).upper():
                continue
            
            recent_enough = is_within_last_24_hours(columns[0])
            
            if recent_enough:
                visible_count += 1
                unique_id = f"{columns[0]}|{columns[4]}|{columns[5] if len(columns) > 5 else ''}"
                results.append({
                    "id": unique_id,
                    "datetime": columns[0],
                    "latitude": columns[1],
                    "longitude": columns[2],
                    "depth_km": columns[3],
                    "magnitude": columns[4],
                    "location": columns[5] if len(columns) > 5 else "",
                    "advisory": columns[6] if len(columns) > 6 else "Tsunami Advisory",
                    "is_within_24h": True,
                    "last_updated": current_time
                })
            else:
                hidden_count += 1
                print(f"🗑️ Hidden (too old): {columns[0]}")
        
        if results:
            results.reverse()
            print(f"⚠️ Tsunami: {visible_count} advisory(ies) shown | {hidden_count} old entries hidden")
        else:
            print(f"✅ Tsunami: No active threats | {hidden_count} old entries hidden")
        return results
    except requests.exceptions.RequestException as error:
        print(f"❌ Tsunami Scrape Error: {error}")
        return []

# --------------------------
# SAVE JSON FILES
# --------------------------
def save_data_to_json(data, filename):
    """Filter and save data to JSON file"""
    print(f"📅 Saving {filename} — filtering for last 24 hours only")
    
    filtered_data = [item for item in data if item.get("is_within_24h", True)]
    
    export_data = []
    for item in filtered_data:
        export_item = {key: value for key, value in item.items() if key not in ["id", "is_within_24h"]}
        export_data.append(export_item)
    
    clean_filename = filename.replace(".json", "")
    with open(f"{clean_filename}.json", "w", encoding="utf-8") as output_file:
        json.dump(export_data, output_file, ensure_ascii=False, indent=2)
    
    print(f"✅ Saved {clean_filename}.json — {len(filtered_data)} items visible\n")

# --------------------------
# API ENDPOINTS
# --------------------------
def verify_access_key(request):
    """Check if request has valid access key"""
    return request.args.get("key", "") == ACCESS_KEY

@app.route('/')
def status_page():
    return {
        "status": "online",
        "ph_time": get_ph_time().strftime('%Y-%m-%d %H:%M:%S'),
        "data_filter": "Last 24 hours only — old entries automatically hidden",
        "refresh_interval": f"{CHECK_INTERVAL_SECONDS} seconds",
        "allowed_website": "https://steveweb.vercel.app"
    }

@app.route('/earthquakes.json')
def get_earthquakes_json():
    if verify_access_key(request):
        return send_file('earthquakes.json', mimetype='application/json')
    return "❌ Access Denied — Invalid key", 403

@app.route('/tsunami.json')
def get_tsunami_json():
    if verify_access_key(request):
        return send_file('tsunami.json', mimetype='application/json')
    return "❌ Access Denied — Invalid key", 403

# --------------------------
# MAIN MONITOR LOOP
# --------------------------
def start_monitoring():
    global sent_earthquake_ids, sent_tsunami_ids
    
    print("\n" + "="*65)
    print("  🇵🇭 PHIVOLCS MONITOR — RUNNING")
    print(f"  🕒 Philippine Time: {get_ph_time().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  ✅ Earthquakes: M{MIN_MAGNITUDE}+ within PH — last 24 hours only")
    print(f"  ✅ Tsunami: Active advisories — last 24 hours only")
    print(f"  ✅ Old entries automatically hidden from website & Telegram")
    print("="*65 + "\n")
    print(f"📱 Telegram Alerts: {'✅ ENABLED' if ENABLE_TELEGRAM_ALERTS else '❌ DISABLED'}\n")
    
    # Mark existing events as already sent — NO ALERTS ON STARTUP
    print("🔵 Step 1: Loading current data — marking existing events as already sent...")
    
    initial_earthquakes = scrape_earthquake_data()
    for eq in initial_earthquakes:
        sent_earthquake_ids.add(eq["id"])
    save_sent_ids(SENT_EARTHQUAKES_FILE, sent_earthquake_ids)
    save_data_to_json(initial_earthquakes, "earthquakes.json")
    print(f"✅ Earthquakes: {len(initial_earthquakes)} loaded — NO TELEGRAM ALERTS SENT\n")
    
    initial_tsunamis = scrape_tsunami_data()
    for ts in initial_tsunamis:
        sent_tsunami_ids.add(ts["id"])
    save_sent_ids(SENT_TSUNAMIS_FILE, sent_tsunami_ids)
    save_data_to_json(initial_tsunamis, "tsunami.json")
    print(f"✅ Tsunami: {len(initial_tsunamis)} loaded — NO TELEGRAM ALERTS SENT\n")
    
    print("🚀 MONITORING STARTED — ONLY NEW EVENTS WILL TRIGGER ALERTS!\n")
    
    # Main loop
    while True:
        try:
            # Check for new earthquakes
            current_earthquakes = scrape_earthquake_data()
            new_earthquake_count = 0
            
            for quake in current_earthquakes:
                if quake["id"] not in sent_earthquake_ids:
                    if quake.get("is_within_24h", False):
                        print(f"🆕 NEW EARTHQUAKE: M{quake['magnitude']} — {quake['location']}")
                        send_telegram_message(format_earthquake_alert_message(quake))
                        new_earthquake_count += 1
                    else:
                        print(f"⏳ Skipped (too old): {quake['datetime']}")
                    sent_earthquake_ids.add(quake["id"])
            
            if new_earthquake_count > 0:
                save_sent_ids(SENT_EARTHQUAKES_FILE, sent_earthquake_ids)
                print(f"✅ {new_earthquake_count} new earthquake alert(s) sent")
            
            save_data_to_json(current_earthquakes, "earthquakes.json")
            
            # Check for new tsunami advisories
            current_tsunamis = scrape_tsunami_data()
            new_tsunami_count = 0
            
            for advisory in current_tsunamis:
                if advisory["id"] not in sent_tsunami_ids:
                    if advisory.get("is_within_24h", False):
                        print(f"🆕 NEW TSUNAMI ADVISORY: M{advisory['magnitude']} — {advisory['datetime']}")
                        send_telegram_message(format_tsunami_alert_message(advisory))
                        new_tsunami_count += 1
                    else:
                        print(f"⏳ Skipped (too old): {advisory['datetime']}")
                    sent_tsunami_ids.add(advisory["id"])
            
            if new_tsunami_count > 0:
                save_sent_ids(SENT_TSUNAMIS_FILE, sent_tsunami_ids)
                print(f"✅ {new_tsunami_count} new tsunami alert(s) sent")
            
            save_data_to_json(current_tsunamis, "tsunami.json")
            
            # Wait before next check
            time.sleep(CHECK_INTERVAL_SECONDS)
            
        except Exception as error:
            print(f"⚠️ Loop Error: {error} — Retrying in 10 seconds...")
            time.sleep(10)

if __name__ == "__main__":
    # Start Flask web server in background thread
    def run_web_server():
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
    
    from threading import Thread
    web_server_thread = Thread(target=run_web_server, daemon=True)
    web_server_thread.start()
    
    # Start monitoring
    start_monitoring()
