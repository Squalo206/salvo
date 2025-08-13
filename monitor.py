#!/usr/bin/env python3
import os, math, json, requests
from datetime import datetime, timedelta, timezone

# === LOCALITÀ DA MONITORARE (nome, lat, lon) ===
LOCATIONS = [
    ("San Salvo", 42.050, 14.717),
    ("Isernia",   41.5931, 14.2326),
]

# === PARAMETRI COMUNI ===
RADIUS_KM       = 40.0
ALT_THRESHOLD_M = 2000.0
QUIET_MINUTES   = 10

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
    import math
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
    r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True}, timeout=20)
    r.raise_for_status()

# ---- Link utili (FR24 + ADSBExchange) ---------------------------------------

def fr24_url(ac, lat=None, lon=None):
    """Costruisce un link FR24 sensato: prima callsign live, poi pagina aircraft per registration, infine mappa centrata su lat/lon."""
    call = (ac.get("call") or ac.get("flight") or "").strip().replace(" ", "")
    reg  = (ac.get("r") or "").strip().replace(" ", "")
    if call:
        return f"https://www.flightradar24.com/{call}"
    if reg:
        return f"https://www.flightradar24.com/data/aircraft/{reg}"
    if lat is not None and lon is not None:
        # Mappa centrata sulla posizione attuale (zoom 8)
        return f"https://www.flightradar24.com/{lat:.5f},{lon:.5f}/8"
    return "https://www.flightradar24.com/"

def adsbx_url(ac):
    """Link diretto all’icona su ADSBExchange Globe quando abbiamo l’ICAO hex."""
    hx = (ac.get("icao") or ac.get("hex") or "").strip().lower()
    return f"https://globe.adsbexchange.com/?icao={hx}" if hx else "https://globe.adsbexchange.com/"

# -----------------------------------------------------------------------------

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

def format_msg(ac, dist_km, alt_m, place):
    callsign = ac.get("call") or ac.get("flight") or ""
    reg = ac.get("r") or ""
    icao = ac.get("icao") or ac.get("hex") or ""
    typ = ac.get("t") or ac.get("type") or ""
    spd = ac.get("gs") or ac.get("spd")
    hdg = ac.get("trak") or ac.get("hdg")
    lat = ac.get("lat"); lon = ac.get("lon")

    fr24 = fr24_url(ac, lat, lon)
    globe = adsbx_url(ac)

    lines = [
        f"✈️ Velivolo a bassa quota — {place}",
        f"{callsign} {f'({reg})' if reg else ''}".strip() or (icao or "ICAO?"),
        f"Tipo: {typ}" if typ else None,
        f"Distanza: {dist_km:.1f} km",
        f"Quota: {int(round(alt_m))} m" if alt_m is not None else "Quota: n/d",
        f"Velocità: {int(round(spd))} kt" if isinstance(spd, (int, float)) else None,
        f"Prua: {int(round(hdg))}°" if isinstance(hdg, (int, float)) else None,
        f"FR24: {fr24}",
        f"ADSBx: {globe}",
    ]
    return "\n".join([x for x in lines if x])

def run_once_for(place, center_lat, center_lon):
    state = load_state()
    quiet = timedelta(minutes=QUIET_MINUTES)
    now = datetime.now(timezone.utc)

    aircraft = fetch_aircraft(center_lat, center_lon, RADIUS_KM)
    alerted = 0

    for ac in aircraft:
        lat, lon = ac.get("lat"), ac.get("lon")
        if lat is None or lon is None:
            continue
        dist_km = haversine_km(center_lat, center_lon, lat, lon)
        if dist_km > RADIUS_KM + 0.5:
            continue
        alt_m = get_altitude_m(ac)
        if alt_m is None or alt_m >= ALT_THRESHOLD_M:
            continue

        label, key = identify(ac)
        scoped_key = f"{place}:{key}"  # antispam separato per località
        last = state.get(scoped_key)
        if last and (now - last) < quiet:
            continue

        msg = format_msg(ac, dist_km, alt_m, place)
        try:
            send_telegram(msg)
            state[scoped_key] = now
            alerted += 1
        except Exception as e:
            print(f"Telegram error ({place}):", e)

    save_state(state)
    print(f"[{place}] {now.isoformat()} — alerts sent: {alerted}")

def main():
    for name, lat, lon in LOCATIONS:
        print(f"--- Controllo {name} ---")
        try:
            run_once_for(name, lat, lon)
        except Exception as e:
            print(f"Errore per {name}: {e}")

if __name__ == "__main__":
    main()
