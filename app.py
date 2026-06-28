from flask import Flask, render_template
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
INSTRUMENT_KEY = "NSE_INDEX|Nifty 50"
ATM_RANGE = 500
STRIKE_INTERVAL = 50
# ==================================================


def get_sheet():
    creds_json = os.environ.get("GOOGLE_CREDS")
    creds_dict = json.loads(creds_json)
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    sheet_id = os.environ.get("SHEET_ID")
    return client.open_by_key(sheet_id).sheet1


def get_nearest_expiry(access_token):
    url = f"https://api.upstox.com/v2/option/contract?instrument_key={INSTRUMENT_KEY}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json"
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            expiries = sorted(set(item["expiry"] for item in data["data"]))
            return expiries[0] if expiries else None
        else:
            print(f"Expiry Error: {response.status_code}")
            return None
    except Exception as e:
        print(f"Expiry Fetch Error: {e}")
        return None


def get_upstox_option_chain(access_token, expiry_date):
    url = f"https://api.upstox.com/v2/option/chain"
    params = {
        "instrument_key": INSTRUMENT_KEY,
        "expiry_date": expiry_date
    }
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    try:
        response = requests.get(url, params=params, headers=headers, timeout=10)
        print(f"Upstox Status: {response.status_code}")
        if response.status_code == 200:
            return response.json()
        else:
            print(f"Upstox Error: {response.status_code} - {response.text[:200]}")
            return None
    except Exception as e:
        print(f"Option Chain Fetch Error: {e}")
        return None


def save_to_sheets(option_data, expiry_date, underlying):
    try:
        sheet = get_sheet()

        IST = timezone(timedelta(hours=5, minutes=30))
        timestamp = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
        atm = round(underlying / STRIKE_INTERVAL) * STRIKE_INTERVAL

        rows = []
        for item in option_data:
            strike = item.get("strike_price", 0)
            if abs(strike - atm) > ATM_RANGE:
                continue

            ce = item.get("call_options", {})
            pe = item.get("put_options", {})

            ce_market = ce.get("market_data", {})
            pe_market = pe.get("market_data", {})

            ce_greek = ce.get("option_greeks", {})
            pe_greek = pe.get("option_greeks", {})

            ce_oi = ce_market.get("oi", 0)
            pe_oi = pe_market.get("oi", 0)

            ce_chng_oi = ce_oi - ce_market.get("prev_oi", 0)
            pe_chng_oi = pe_oi - pe_market.get("prev_oi", 0)

            ce_ltp = round(ce_market.get("ltp", 0), 2)
            pe_ltp = round(pe_market.get("ltp", 0), 2)

            ce_iv = round(ce_greek.get("iv", 0), 2)
            pe_iv = round(pe_greek.get("iv", 0), 2)

            ce_vol = ce_market.get("volume", 0)
            pe_vol = pe_market.get("volume", 0)

            pcr = round(pe_oi / ce_oi, 2) if ce_oi > 0 else 0
            underlying_price = round(underlying, 2)

            rows.append([
                timestamp, SYMBOL, expiry_date, strike,
                ce_oi, ce_chng_oi, ce_ltp, ce_iv, ce_vol,
                pe_oi, pe_chng_oi, pe_ltp, pe_iv, pe_vol,
                pcr, underlying_price
            ])

        if rows:
            try:
                first_cell = sheet.cell(1, 1).value
            except:
                first_cell = None

            if not first_cell:
                headers_row = [
                    "Timestamp", "Symbol", "Expiry", "Strike",
                    "CE_OI", "CE_Chng_OI", "CE_LTP", "CE_IV", "CE_Volume",
                    "PE_OI", "PE_Chng_OI", "PE_LTP", "PE_IV", "PE_Volume",
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

        access_token = os.environ.get("UPSTOX_ACCESS_TOKEN")
        if not access_token:
            print("❌ UPSTOX_ACCESS_TOKEN not set!")
            return

        expiry_date = get_nearest_expiry(access_token)
        if not expiry_date:
            print("❌ Could not fetch expiry date!")
            return

        print(f"📅 Expiry: {expiry_date}")

        data = get_upstox_option_chain(access_token, expiry_date)
        if not data:
            print("❌ No data received from Upstox!")
            return

        option_data = data.get("data", [])
        if not option_data:
            print("❌ Empty option chain data!")
            return

        try:
            underlying = option_data[0].get("underlying_spot_price", 0)
        except:
            underlying = 0

        save_to_sheets(option_data, expiry_date, underlying)

    else:
        print(f"🔴 Market closed - {now.strftime('%H:%M:%S')} IST")


def run_scheduler():
    schedule.every(3).minutes.do(fetch_and_save)
    while True:
        schedule.run_pending()
        time.sleep(1)


@app.route("/")
def home():
    return "✅ Upstox Options Fetcher Running!"


@app.route("/chart")
def chart():
    sheet_id = os.environ.get("SHEET_ID", "")
    return render_template("chart.html", sheet_id=sheet_id)


@app.route("/chart2")
def chart2():
    sheet_id = os.environ.get("SHEET_ID", "")
    return render_template("chart2.html", sheet_id=sheet_id)


@app.route("/fetch")
def manual_fetch():
    fetch_and_save()
    return "✅ Manual fetch done!"


@app.route("/status")
def status():
    IST = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(IST)
    return f"🕐 Current IST Time: {now.strftime('%Y-%m-%d %H:%M:%S')}"


@app.route("/clear")
def clear_sheet():
    try:
        sheet = get_sheet()
        sheet.clear()
        headers_row = [
            "Timestamp", "Symbol", "Expiry", "Strike",
            "CE_OI", "CE_Chng_OI", "CE_LTP", "CE_IV", "CE_Volume",
            "PE_OI", "PE_Chng_OI", "PE_LTP", "PE_IV", "PE_Volume",
            "PCR", "Underlying"
        ]
        sheet.insert_row(headers_row, 1)
        return "✅ Sheet cleared! Ab /fetch karo."
    except Exception as e:
        return f"❌ Error: {e}"


@app.route("/debug")
def debug():
    access_token = os.environ.get("UPSTOX_ACCESS_TOKEN")
    expiry_date = get_nearest_expiry(access_token)
    data = get_upstox_option_chain(access_token, expiry_date)
    if data:
        first_item = data.get("data", [])[0]
        ce = first_item.get("call_options", {})
        pe = first_item.get("put_options", {})
        return {
            "ce_market": ce.get("market_data", {}),
            "ce_greeks": ce.get("option_greeks", {}),
            "pe_market": pe.get("market_data", {}),
            "pe_greeks": pe.get("option_greeks", {})
        }
    return {"error": "No data"}


# Scheduler background thread
t = threading.Thread(target=run_scheduler)
t.daemon = True
t.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
