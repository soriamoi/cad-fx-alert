# cad-FX-alert

import os
import requests, resend, sqlite3, datetime as dt
# import requests & resend: 'pip install resend requests' in Terminal

# === CONFIG ===
DB_PATH = "fx.db"

# BoC Valet series keys (1 unit foreign → CAD)
BOC_SERIES_KEY_USD = "FXUSDCAD"   ## 1 USD → CAD
BOC_SERIES_KEY_GBP = "FXGBPCAD"   ## 1 GBP → CAD (confirm on BoC site)

RESEND_API_KEY = "re_SdQytCYH_HraR7G8GUQWFu4BJGh1yBNky"
resend.api_key = RESEND_API_KEY

TO_EMAIL = "alicetshi75@gmail.com"

# === Database Setup ===

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # USD → CAD
    cur.execute("""
        CREATE TABLE IF NOT EXISTS rates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT UNIQUE NOT NULL,
            rate REAL NOT NULL
        )
    """)

    # GBP → CAD
    cur.execute("""
        CREATE TABLE IF NOT EXISTS rates_gbp (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT UNIQUE NOT NULL,
            rate REAL NOT NULL
        )
    """)

    conn.commit()
    conn.close()

# JSON from the FX to get the “get rate” function

def _fetch_boc_observations(series_key: str, params=None):
    """Fetch observations array from BoC Valet for a given series."""
    url = f"https://www.bankofcanada.ca/valet/observations/{series_key}/json"
    resp = requests.get(url, params=params or {}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    # print(data) # Debug print - check if API structure matches what I'm expecting

    # The data structure is nested lists; 
    # BoC format: data["series"]["observations"] is a list of { "d": "2026-02-01", "v": "1.3..."}
    
    observations = data["observations"]
    if not observations:
        raise ValueError("No observations found for {series_key}")
    
    return observations
    
def get_latest_rate(series_key):
    """Return (date_str, rate) for latest observation of a BoC FX series."""
    observations = _fetch_boc_observations(series_key)
    latest = observations[-1]  # most recent observation
    date_str = latest["d"]     # e.g. "2026-03-01"
    # rate = float(latest["v"])  # For FX series, value is normally under key "v"
    # FIXED: Use nested series_key["v"] format for FX rates
    rate = float(latest[series_key]["v"])
    return date_str, rate

def backfill_last_30_days(series_key: str, table_name: str):
    """Backfill last 30 days for given BoC FX series into given table."""
    print(f"Backfilling last 30 days for {series_key} into {table_name}...")

    observations = _fetch_boc_observations(series_key, params={"recent": 30})

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    inserted = 0
    for obs in observations:
        d = obs["d"]
        v = float(obs["v"])
        cur.execute(
            f"INSERT OR REPLACE INTO {table_name}(date, rate) VALUES (?, ?)",
            (d, v),
        )
        inserted += 1

    conn.commit()
    conn.close()

    print(f"Backfilled {inserted} rows for {series_key}.")


# === USD / GBP SAVE RATE ===

def save_rate_usd(date_str: str, rate: float):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO rates(date, rate) VALUES (?, ?)",
        (date_str, rate),
    )
    conn.commit()
    conn.close()


def save_rate_gbp(date_str: str, rate: float):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO rates_gbp(date, rate) VALUES (?, ?)",
        (date_str, rate),
    )
    conn.commit()
    conn.close()


def get_last_30_days_min(table_name: str, date_str: str):
    """Compute minimum rate over last 30 days in DB for a given table."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT MIN(rate) FROM {table_name}
        WHERE date >= date(?, '-30 day') AND date <= ?
        """,
        (date_str, date_str),
    )
    min_rate = cur.fetchone()[0]
    conn.close()
    return min_rate


def record_notification(pair: str, date_str: str, rate: float):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO notifications(pair, date, rate) VALUES (?, ?, ?)",
        (pair, date_str, rate),
    )
    conn.commit()
    conn.close()


def is_already_notified(pair: str, date_str: str, rate: float) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT 1 FROM notifications
        WHERE pair = ? AND date = ? AND rate = ?
        LIMIT 1
        """,
        (pair, date_str, rate),
    )
    row = cur.fetchone()
    conn.close()
    return row is not None


# === SEND EMAIL WITH RESEND ===

def send_email_notification(date_str: str, pair: str, rate: float, min_rate: float):
    params = {
        "from": "FX Bot <onboarding@resend.dev>",
        "to": [TO_EMAIL],
        "subject": f"{pair} is cheapest in 30 days ({rate:.4f} CAD)",
        "html": f"""
            <p>On <strong>{date_str}</strong>, {pair} is <strong>{rate:.4f}</strong>.</p>
            <p>This matches the lowest rate in the last 30 recorded days ({min_rate:.4f}).</p>
            <p>Note: rates are from the Bank of Canada Valet API.</p>
        """,
    }
    email = resend.Emails.send(params)
    print("Email sent:", email)


# === MAIN DAILY TASK ===

def daily_task():
    print("Fetching today's USD→CAD and GBP→CAD rates from Bank of Canada...")

    # === USD→CAD ===
    try:
        date_usd, rate_usd = get_latest_rate(BOC_SERIES_KEY_USD)
        print(f"BoC USD→CAD on {date_usd}: {rate_usd:.4f} CAD per USD")

        save_rate_usd(date_usd, rate_usd)

        min_usd = get_last_30_days_min("rates", date_usd)
        if min_usd is not None and rate_usd <= min_usd and not is_already_notified("USD→CAD", date_usd, rate_usd):
            print("🔔 USD→CAD matches 30‑day low; sending email...")
            send_email_notification(date_usd, "USD→CAD", rate_usd, min_usd)
            record_notification("USD→CAD", date_usd, rate_usd)
        else:
            if min_usd is not None:
                print(f"USD→CAD is not a 30‑day low (min was {min_usd:.4f}).")
            else:
                print("Not enough USD→CAD data yet to compute 30‑day low.")
    except Exception as e:
        print("Error in USD→CAD logic:", e)

    # === GBP→CAD ===
    try:
        date_gbp, rate_gbp = get_latest_rate(BOC_SERIES_KEY_GBP)
        print(f"BoC GBP→CAD on {date_gbp}: {rate_gbp:.4f} CAD per GBP")

        save_rate_gbp(date_gbp, rate_gbp)

        min_gbp = get_last_30_days_min("rates_gbp", date_gbp)
        if min_gbp is not None and rate_gbp <= min_gbp and not is_already_notified("GBP→CAD", date_gbp, rate_gbp):
            print("🔔 GBP→CAD matches 30‑day low; sending email...")
            send_email_notification(date_gbp, "GBP→CAD", rate_gbp, min_gbp)
            record_notification("GBP→CAD", date_gbp, rate_gbp)
        else:
            if min_gbp is not None:
                print(f"GBP→CAD is not a 30‑day low (min was {min_gbp:.4f}).")
            else:
                print("Not enough GBP→CAD data yet to compute 30‑day low.")
    except Exception as e:
        print("Error in GBP→CAD logic:", e)


# === ONE‑TIME BACKFILL OF LAST 30 DAYS GBP→CAD ===

def backfill_last_30_days_gbp_only():
    """Backfill last 30 days GBP→CAD, handling BoC's nested observation format."""
    print("Backfilling last 30 days GBP→CAD...")
    
    observations = _fetch_boc_observations(BOC_SERIES_KEY_GBP, {"recent": 30})
    
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    inserted = 0
    for obs in observations:
        date_str = obs["d"]
        
        # BoC FX series have nested structure: obs["FXGBPCAD"]["v"]
        # USD uses obs["FXUSDCAD"]["v"], GBP uses obs["FXGBPCAD"]["v"]
        rate = float(obs[BOC_SERIES_KEY_GBP]["v"])
        
        cur.execute(
            "INSERT OR REPLACE INTO rates_gbp(date, rate) VALUES (?, ?)",
            (date_str, rate)
        )
        inserted += 1
    
    conn.commit()
    conn.close()
    print(f"Backfilled {inserted} GBP→CAD rows.")


# === RUN SCRIPT ===

if __name__ == "__main__":
    init_db() 

    # To backfill the last 30 days of GBP→CAD once, uncomment:
    backfill_last_30_days_gbp_only()
    exit(0)

    daily_task()
