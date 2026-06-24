from flask import Flask
import requests
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timezone, timedelta
import schedule
import time
import threading
import os
import json

app = Flask(__name__)

# ===================== CONFIG =====================
SYMBOL = "NIFTY"
ATM_RANGE = 500
STRIKE_INTERVAL = 50
# ==================================================


def get_nse_data():
    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Referer": "https://www.nseindia.com/option-chain",
    }
    try:
        session.get("https://www.nseindia.com", headers=headers, timeout=10)
        time.sleep(2)
        session.get("https://www.nseindia.com/option-chain", headers=headers, timeout=10)
        time.sleep(2)
        url = f"https://www.nseindia.com/api/option-chain-indices?symbol={SYMBOL}"
        response = session.get(url, headers=headers, timeout=15)
        print(f"NSE Status: {response.status_code}")
        if response.status_code == 200:
            return response.json()
        else:
            print(f"NSE Error: {response.status_code} - {response.text[:200]}")
            return None
    except Exception as e:
        print(f"Fetch Error: {e}")
        return None


def save_to_sheets(data):
    try:
        creds_json = os.environ.get("GOOGLE_CREDS")
        creds_dict = json.loads(creds_json)
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive"
        ]
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        sheet_id = os.environ.get("SHEET_ID")
        sheet = client.open_by_key(sheet_id).sheet1
        records = data["records"]["data"]
        expiry = data["records"]["expiryDates"][0]
        underlying = data["records"]["underlyingValue"]
        atm = round(underlying / STRIKE_INTERVAL) * STRIKE_INTERVAL
        timestamp = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d %H:%M:%S")
        rows = []
        for item in records:
            if item.get("expiryDate") != expiry:
                continue
            strike = item["strikePrice"]
            if abs(strike - atm) > ATM_RANGE:
                continue
            ce = item.get("CE", {})
            pe = item.get("PE", {})
            ce_oi = ce.get("openInterest", 0)
            pe_oi = pe.get("openInterest", 0)
            pcr = round(pe_oi / ce_oi, 2) if ce_oi > 0 else 0
            rows.append([
                timestamp, SYMBOL, expiry, strike,
                ce_oi,
                ce.get("changeinOpenInterest", 0),
                ce.get("lastPrice", 0),
                pe_oi,
                pe.get("changeinOpenInterest", 0),
                pe.get("lastPrice", 0),
                pcr,
                round(underlying, 2)
            ])
        if rows:
            if sheet.row_count == 0 or sheet.cell(1, 1).value is None:
                headers_row = [
                    "Timestamp", "Symbol", "Expiry", "Strike",
                    "CE OI", "CE Change OI", "CE LTP",
                    "PE OI", "PE Change OI", "PE LTP",
                    "PCR", "Underlying"
                ]
                sheet.insert_row(headers_row, 1)
            sheet.append_rows(rows)
            print(f"✅ {len(rows)} rows saved at {timestamp}")
        else:
            print("⚠️ No rows to save.")
    except Exception as e:
        print(f"Sheets Error: {e}")


def fetch_and_save():
    IST = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(IST)
    market_open = (
        now.weekday() < 5 and
        (now.hour > 9 or (now.hour == 9 and now.minute >= 15)) and
        (now.hour < 15 or (now.hour == 15 and now.minute <= 30))
    )
    if market_open:
        print(f"📡 Fetching at {now.strftime('%H:%M:%S')} IST...")
        data = get_nse_data()
        if data:
            save_to_sheets(data)
        else:
            print("❌ No data received from NSE.")
    else:
        print(f"🔴 Market closed - {now.strftime('%H:%M:%S')} IST")


def run_scheduler():
    schedule.every(3).minutes.do(fetch_and_save)
    while True:
        schedule.run_pending()
        time.sleep(1)


@app.route("/")
def home():
    return "✅ NSE Options Fetcher Running!"


@app.route("/fetch")
def manual_fetch():
    fetch_and_save()
    return "✅ Manual fetch done!"


@app.route("/status")
def status():
    IST = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(IST)
    return f"🕐 Current IST Time: {now.strftime('%Y-%m-%d %H:%M:%S')}"


t = threading.Thread(target=run_scheduler)
t.daemon = True
t.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
