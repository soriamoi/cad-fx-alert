# cad-FX-alert

import os
import requests
import resend
import sqlite3
import datetime as dt
import logging
# import requests & resend: 'pip install resend requests' in Terminal


# === CONFIG ===

DB_PATH = "fx.db"

# BoC Valet series keys (1 unit foreign → CAD)
# BOC_SERIES_KEY_USD = "FXUSDCAD"   ## 1 USD → CAD
# BOC_SERIES_KEY_GBP = "FXGBPCAD"   ## 1 GBP → CAD (confirm on BoC site)
BOC_SERIES = {
    "USD→CAD": "FXUSDCAD",
    "GBP→CAD": "FXGBPCAD",
}

RESEND_API_KEY = os.getenv("RESEND_API_KEY")
TO_EMAIL = os.getenv("TO_EMAIL")

if not RESEND_API_KEY or not TO_EMAIL:
    raise ValueError("Missing environment variables (RESEND_API_KEY, TO_EMAIL)")

resend.api_key = RESEND_API_KEY


# === LOGGING SETUP ===

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)


# === DATABASE SETUP ===

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS rates (
            pair TEXT,
            date TEXT,
            rate REAL,
            PRIMARY KEY (pair, date)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            pair TEXT,
            date TEXT,
            rate REAL,
            PRIMARY KEY (pair, date, rate)
        )
    """)

    conn.commit()
    conn.close()


# === FETCH DATA ===

def fetch_boc_observations(series_key, params=None):
    """Fetch observations array from BoC Valet for a given series."""    
    url = f"https://www.bankofcanada.ca/valet/observations/{series_key}/json"
    resp = requests.get(url, params=params or {}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    # print(data) # Debug print - check if API structure matches what I'm expecting
    
    # The data structure is nested lists; 
    # BoC format: data["series"]["observations"] is a list of { "d": "2026-02-01", "v": "1.3..."}
    
    observations = data.get("observations", [])
    if not observations:
        raise ValueError(f"No data for {series_key}")

    return observations

def extract_rate(obs, series_key):
    return float(obs[series_key]["v"])
    
# def _fetch_boc_observations(series_key: str, params=None):
#     """Fetch observations array from BoC Valet for a given series."""
#     url = f"https://www.bankofcanada.ca/valet/observations/{series_key}/json"
#     resp = requests.get(url, params=params or {}, timeout=10)
#     resp.raise_for_status()
#     data = resp.json()
#     # print(data) # Debug print - check if API structure matches what I'm expecting

#     # The data structure is nested lists; 
#     # BoC format: data["series"]["observations"] is a list of { "d": "2026-02-01", "v": "1.3..."}
    
#     observations = data["observations"]
#     if not observations:
#         raise ValueError("No observations found for {series_key}")
    
#     return observations


# === DATA OPS (SAVE RATE) ===

def upsert_rate(pair, date_str, rate):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        INSERT OR REPLACE INTO rates(pair, date, rate)
        VALUES (?, ?, ?)
    """, (pair, date_str, rate))

    conn.commit()
    conn.close()

def get_30d_min(pair, date_str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        SELECT MIN(rate) FROM rates
        WHERE pair = ?
        AND date >= date(?, '-30 day')
        AND date <= ?
    """, (pair, date_str, date_str))

    result = cur.fetchone()[0]
    conn.close()
    return result

def already_notified(pair, date_str, rate):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        SELECT 1 FROM notifications
        WHERE pair = ? AND date = ? AND rate = ?
    """, (pair, date_str, rate))

    exists = cur.fetchone() is not None
    conn.close()
    return exists

def record_notification(pair, date_str, rate):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        INSERT OR IGNORE INTO notifications(pair, date, rate)
        VALUES (?, ?, ?)
    """, (pair, date_str, rate))

    conn.commit()
    conn.close()


# === SEND EMAIL WITH RESEND ===

def send_email(pair, date_str, rate, min_rate):
    try:
        resend.Emails.send({
            "from": "FX Bot <onboarding@resend.dev>",
            "to": [TO_EMAIL],
            "subject": f"{pair} 30-day low: {rate:.4f}",
            "html": f"""
                <p><strong>{pair}</strong> on {date_str}: {rate:.4f}</p>
                <p>30-day low: {min_rate:.4f}</p>
                <p>This matches the lowest rate in the last 30 recorded days ({min_rate:.4f}).</p>
                <p>Note: rates are from the Bank of Canada Valet API.</p>
            """
        })
        logging.info(f"Email sent for {pair}")
    except Exception as e:
        logging.error(f"Email failed: {e}")


# === MAIN DAILY TASK ===

def daily_task():
    logging.info("Fetching today's USD→CAD and GBP→CAD rates from Bank of Canada...")

    for pair, series_key in BOC_SERIES.items():
        try:
            # Backfill last 30 days every run (important!)
            observations = fetch_boc_observations(series_key, {"recent": 30})

            for obs in observations:
                date_str = obs["d"]
                rate = extract_rate(obs, series_key)
                upsert_rate(pair, date_str, rate)

            # Get TRUE latest rate (not from recent=30)
            all_obs = fetch_boc_observations(series_key)
            latest_obs = max(all_obs, key=lambda x: x["d"])
            
            # latest = observations[-1]
            
            date_str = latest_obs["d"]
            rate = extract_rate(latest_obs, series_key)

            logging.info(f"{pair} {date_str}: {rate:.4f}")

            min_rate = get_30d_min(pair, date_str)

            if min_rate and rate <= min_rate:
                if not already_notified(pair, date_str, rate):
                    logging.info(f"{pair} is 30-day low → alerting")
                    send_email(pair, date_str, rate, min_rate)
                    record_notification(pair, date_str, rate)
                else:
                    logging.info(f"{pair} already notified")
            else:
                logging.info(f"{pair} not a 30-day low")

        except Exception as e:
            logging.error(f"{pair} failed: {e}")

    logging.info("Done.")


# === RUN SCRIPT ===

if __name__ == "__main__":
    init_db() 
    daily_task()
