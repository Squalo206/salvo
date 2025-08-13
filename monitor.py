#!/usr/bin/env python3
import os, math, json, requests
from datetime import datetime, timedelta, timezone

# === CONFIG ===
CENTER_LAT = 42.050      # San Salvo
CENTER_LON = 14.717
RADIUS_KM = 40.0
ALT_THRESHOLD_M = 15000.0
QUIET_MINUTES = 10

# Endpoint gratuiti compatibili con ADS-B Exchange v2
PROVIDERS = [
    "https://api.adsb.one",
    "https://api.adsb.lol",
]

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
STATE_FILE = "state.json"

def km_to_nm(km): return km * 0.539956803
def feet_to_m(ft): return ft * 0.3048

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0088
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlon/2)**2
    return 2 * R * math.asin(math.sqrt(a))

def load_state():
    if not os.path.exists(STATE_FILE): return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        out = {}
        for k, v in raw.items():
            try: out[k] = datetime.fromisoformat(v)
            except: pass
        return out
    except: return {}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({k: v.isoformat() for k, v in state.items()}, f)

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=20)
    r.raise_for_status()

def fetch_aircraft(lat, lon, radius_km):
    range_nm = max(1, int(round(km_to_nm(radius_km))))
    last_exc = None
    for base in PROVIDERS:
        url = f"{base}/v2/point/{lat:.6f}/{lon:.6f}/{range_nm}"
        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("ac", []) or []
        except Exception as e:
            last_exc = e
            continue
    if last_exc: raise last_exc
    return []

def get_altitude_m(ac):
    alt_ft = None
    if isinstance(ac.get("alt_baro"), (int, float)): alt_ft = ac["alt_baro"]
    elif isinstance(ac.get("alt_geom"), (int, float)): alt_ft = ac["alt_geom"]
    return None if alt_ft is None else feet_to_m(alt_ft)

def identify(ac):
    callsign = (ac.get("call") or ac.get("flight") or "").strip()
    reg = (ac.get("r") or "").strip()
    icao = (ac.get("icao") or ac.get("hex") or "").strip()
    label = callsign or (reg and f"({reg})") or icao or "Sconosciuto"
    key = (icao or callsign or reg or "unknown").upper()
    return label, key

def format_msg(ac, dist_km, alt_m):
    callsign = ac.get("call") or ac.get("flight") or ""
    reg = ac.get("r") or ""
    icao = ac.get("icao") or ac.get("hex") or ""
    typ = ac.get("t") or ac.get("type") or ""
    spd = ac.get("gs") or ac.get("spd")
    hdg = ac.get("trak") or ac.get("hdg")

    lines = [
        "✈️ Velivolo a bassa quota",
        f"{callsign} {f'({reg})' if reg else ''}".strip() or (icao or "ICAO?"),
        f"Tipo: {typ}" if typ else None,
        f"Distanza: {dist_km:.1f} km",
        f"Quota: {int(round(alt_m))} m" if alt_m is not None else "Quota: n/d",
        f"Velocità: {int(round(spd))} kt" if isinstance(spd, (int, float)) else None,
        f"Prua: {int(round(hdg))}°" if isinstance(hdg, (int, float)) else None,
    ]
    return "\n".join([x for x in lines if x])

def run_once():
    state = load_state()
    quiet = timedelta(minutes=QUIET_MINUTES)
    now = datetime.now(timezone.utc)

    aircraft = fetch_aircraft(CENTER_LAT, CENTER_LON, RADIUS_KM)
    alerted = 0

    for ac in aircraft:
        lat, lon = ac.get("lat"), ac.get("lon")
        if lat is None or lon is None: 
            continue
        dist_km = haversine_km(CENTER_LAT, CENTER_LON, lat, lon)
        if dist_km > RADIUS_KM + 0.5:
            continue
        alt_m = get_altitude_m(ac)
        if alt_m is None or alt_m >= ALT_THRESHOLD_M:
            continue

        label, key = identify(ac)
        last = state.get(key)
        if last and (now - last) < quiet:
            continue

        msg = format_msg(ac, dist_km, alt_m)
        try:
            send_telegram(msg)
            state[key] = now
            alerted += 1
        except Exception as e:
            print("Telegram error:", e)

    save_state(state)
    print(f"Done at {now.isoformat()} — alerts sent: {alerted}")

if __name__ == "__main__":
    run_once()
