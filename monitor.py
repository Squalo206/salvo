#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import math
import json
import requests
import urllib.parse
from datetime import datetime, timedelta, timezone

# === LOCALITÀ DA MONITORARE (nome, lat, lon) ===
LOCATIONS = [
    # ("San Salvo", 42.050, 14.717),
    ("Isernia",   41.5931, 14.2326),
]

# === PARAMETRI COMUNI ===
RADIUS_KM       = 40.0
ALT_THRESHOLD_M = 2000.0
QUIET_MINUTES   = 10            # antispam per singolo velivolo/località
STATE_FILE      = "state.json"

# Endpoint gratuiti compatibili con ADS-B Exchange v2
PROVIDERS = [
    "https://api.adsb.one",
    "https://api.adsb.lol",
]

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID")  # opzionale
# Facoltativo: lista chat extra separata da virgole (es. "123,456,789")
TELEGRAM_EXTRA_CHAT_IDS = os.environ.get("TELEGRAM_EXTRA_CHAT_IDS", "")

# ----------------- UTIL -----------------
def km_to_nm(km): return km * 0.539956803
def feet_to_m(ft): return ft * 0.3048

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0088
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlon/2)**2
    return 2 * R * math.asin(math.sqrt(a))

def load_state():
    if not os.path.exists(STATE_FILE): 
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        out = {}
        for k, v in raw.items():
            try:
                out[k] = datetime.fromisoformat(v)
            except Exception:
                pass
        return out
    except Exception:
        return {}

def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({k: v.isoformat() for k, v in state.items()}, f, ensure_ascii=False)
    except Exception as e:
        print("Save state error:", e)

# ---------- Foto aereo (Planespotters) ---------------------------------------
def get_aircraft_photo(reg=None, icao=None):
    """
    Ritorna un URL immagine (stringa) se disponibile.
    Prova prima per matricola (reg), poi per esadecimale (icao/hex).
    Gestisce sia stringhe dirette che eventuali dizionari (retro-compat).
    """
    def _first_url_from_photo_obj(p):
        # preferisci thumbnail_large, poi thumbnail, poi image (se presente)
        candidates = [p.get("thumbnail_large"), p.get("thumbnail"), p.get("image")]
        for v in candidates:
            if isinstance(v, str) and v.startswith("http"):
                return v
            if isinstance(v, dict):
                u = v.get("src") or v.get("href")
                if isinstance(u, str) and u.startswith("http"):
                    return u
        return None

    headers = {"User-Agent": "Mozilla/5.0 (compatible; ADSBbot/1.0)"}
    endpoints = []

    reg = (reg or "").strip().upper()
    icao = (icao or "").strip().lower()

    if reg:
        endpoints.append(f"https://api.planespotters.net/pub/photos/reg/{reg}")
    if icao:
        endpoints.append(f"https://api.planespotters.net/pub/photos/hex/{icao}")

    for url in endpoints:
        try:
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code != 200:
                continue
            data = r.json()
            photos = data.get("photos") or []
            if not photos:
                continue
            photo_url = _first_url_from_photo_obj(photos[0])
            if photo_url:
                # print("[PHOTO]", url, "->", photo_url)  # debug facoltativo
                return photo_url
        except Exception as e:
            print("Photo lookup error:", e)

    return None

# ---------- FR24 links (app-first con fallback) ------------------------------
def fr24_links(ac, lat=None, lon=None):
    call = (ac.get("call") or ac.get("flight") or "").strip().replace(" ", "")
    reg  = (ac.get("r") or "").strip().replace(" ", "")
    if call:
        path = call
    elif reg:
        path = f"data/aircraft/{reg}"
    elif lat is not None and lon is not None:
        path = f"{lat:.5f},{lon:.5f}/8"
    else:
        path = ""

    https = f"https://www.flightradar24.com/{path}"
    intent = (
        "intent://www.flightradar24.com/" + path +
        "#Intent;scheme=https;package=com.flightradar24free;" +
        "S.browser_fallback_url=" + urllib.parse.quote(https, safe="") + ";end"
    )
    ios_scheme = "fr24://"   # apre l'app FR24 su iOS (se il client lo consente)
    return {"https": https, "android_intent": intent, "ios_scheme": ios_scheme}

def adsbx_url(ac):
    hx = (ac.get("icao") or ac.get("hex") or "").strip().lower()
    return f"https://globe.adsbexchange.com/?icao={hx}" if hx else "https://globe.adsbexchange.com/"

def build_links_text(ac, lat=None, lon=None):
    links = fr24_links(ac, lat, lon)
    globe = adsbx_url(ac)
    return "\n".join([
        f"FR24 (Android app): {links['android_intent']}",
        f"FR24 (iOS app): {links['ios_scheme']}",
        f"FR24 (web): {links['https']}",
        f"ADSBx (web): {globe}",
    ])

# ---------- Telegram ---------------------------------------------------------
def _truncate_caption(caption, limit=1024):
    # Limite Telegram per caption = 1024 caratteri
    if caption and len(caption) > limit:
        return caption[:limit-1] + "…"
    return caption

def _telegram_recipients():
    chat_ids = []
    if TELEGRAM_CHAT_ID:
        chat_ids.append(str(TELEGRAM_CHAT_ID))
    # fisso richiesto
    chat_ids.append("5278987817")
    # eventuali extra da env
    extra = [x.strip() for x in TELEGRAM_EXTRA_CHAT_IDS.split(",") if x.strip()]
    chat_ids.extend(extra)
    # dedup preservando ordine
    seen = set()
    out = []
    for cid in chat_ids:
        if cid not in seen:
            out.append(cid)
            seen.add(cid)
    return out

def send_telegram(text, photo_url=None, extra_text=None):
    """
    Se c'è 'photo_url' prova prima a inviare per URL.
    Se fallisce, scarica l'immagine e la ricarica come file (fallback).
    Dopo la foto (o il testo), se 'extra_text' è valorizzato, invia un secondo
    messaggio di testo (utile per link lunghi come intent://, fr24://, ecc.).
    """
    if not TELEGRAM_BOT_TOKEN:
        print("Telegram not configured: TELEGRAM_BOT_TOKEN assente.")
        return

    base_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    chat_ids = _telegram_recipients()
    caption = _truncate_caption(text)

    for cid in chat_ids:
        try:
            if photo_url:
                # 1) tenta invio per URL (più leggero)
                r = requests.post(
                    f"{base_url}/sendPhoto",
                    json={"chat_id": cid, "caption": caption, "photo": photo_url},
                    timeout=20
                )
                ok = False
                try:
                    ok = r.status_code == 200 and r.json().get("ok", False)
                except Exception:
                    ok = (r.status_code == 200)

                if not ok:
                    # 2) fallback: scarica e ricarica come file
                    img = requests.get(photo_url, timeout=20)
                    if img.status_code == 200 and img.content:
                        files = {"photo": ("aircraft.jpg", img.content)}
                        data = {"chat_id": cid, "caption": caption}
                        r2 = requests.post(f"{base_url}/sendPhoto", data=data, files=files, timeout=30)
                        r2.raise_for_status()
                    else:
                        # 3) se pure il download fallisce, mando almeno il testo con link della foto
                        fallback_text = f"{text}\n\n(Foto): {photo_url}"
                        r3 = requests.post(
                            f"{base_url}/sendMessage",
                            json={"chat_id": cid, "text": fallback_text, "disable_web_page_preview": False},
                            timeout=20
                        )
                        r3.raise_for_status()
            else:
                r = requests.post(
                    f"{base_url}/sendMessage",
                    json={"chat_id": cid, "text": text, "disable_web_page_preview": True},
                    timeout=20
                )
                r.raise_for_status()

            # Eventuale secondo messaggio con i link (deep link app + web)
            if extra_text and extra_text.strip():
                rL = requests.post(
                    f"{base_url}/sendMessage",
                    json={"chat_id": cid, "text": extra_text, "disable_web_page_preview": False},
                    timeout=20
                )
                rL.raise_for_status()

        except Exception as e:
            print(f"Telegram error for chat {cid}:", e)

# ---------------------------------------------------------------------------
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
    if last_exc:
        raise last_exc
    return []

def get_altitude_m(ac):
    alt_ft = None
    if isinstance(ac.get("alt_baro"), (int, float)): 
        alt_ft = ac["alt_baro"]
    elif isinstance(ac.get("alt_geom"), (int, float)): 
        alt_ft = ac["alt_geom"]
    return None if alt_ft is None else feet_to_m(alt_ft)

def identify(ac):
    callsign = (ac.get("call") or ac.get("flight") or "").strip()
    reg = (ac.get("r") or "").strip()
    icao = (ac.get("icao") or ac.get("hex") or "").strip()
    label = callsign or (reg and f"({reg})") or icao or "Sconosciuto"
    key = (icao or callsign or reg or "unknown").upper()
    return label, key

def format_msg_and_photo(ac, dist_km, alt_m, place):
    callsign = (ac.get("call") or ac.get("flight") or "").strip()
    reg = (ac.get("r") or "").strip()
    icao = (ac.get("icao") or ac.get("hex") or "").strip()
    typ = ac.get("t") or ac.get("type") or ""
    spd = ac.get("gs") or ac.get("spd")
    hdg = ac.get("trak") or ac.get("hdg")
    lat = ac.get("lat"); lon = ac.get("lon")

    # Messaggio "corto" senza i link (che invieremo a parte per aprire l'app)
    lines = [
        f"✈️ Velivolo a bassa quota — {place}",
        f"{(callsign + ' ').strip()}{f'({reg})' if reg else ''}".strip() or (icao or "ICAO?"),
        f"Tipo: {typ}" if typ else None,
        f"Distanza: {dist_km:.1f} km",
        f"Quota: {int(round(alt_m))} m" if alt_m is not None else "Quota: n/d",
        f"Velocità: {int(round(spd))} kt" if isinstance(spd, (int, float)) else None,
        f"Prua: {int(round(hdg))}°" if isinstance(hdg, (int, float)) else None,
    ]
    msg = "\n".join([x for x in lines if x])

    # Link (FR24 app-first + ADSBx) in messaggio separato
    links_text = build_links_text(ac, lat, lon)

    # Foto (prova reg, poi icao/hex)
    photo_url = get_aircraft_photo(reg=reg, icao=icao)
    return msg, links_text, photo_url

def run_once_for(place, center_lat, center_lon):
    state = load_state()
    quiet = timedelta(minutes=QUIET_MINUTES)
    now = datetime.now(timezone.utc)

    try:
        aircraft = fetch_aircraft(center_lat, center_lon, RADIUS_KM)
    except Exception as e:
        print(f"Fetch error ({place}):", e)
        return

    # 1) Filtra tutti i velivoli che rispettano raggio/altitudine
    eligible = []
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
        eligible.append((dist_km, alt_m, ac))

    # Ordina per distanza (più vicini prima)
    eligible.sort(key=lambda x: x[0])

    # 2) Invia alert individuali solo per i "nuovi" (fuori quiet)
    alerted = 0
    for dist_km, alt_m, ac in eligible:
        _, key = identify(ac)
        scoped_key = f"{place}:{key}"  # antispam separato per località
        last = state.get(scoped_key)
        if last and (now - last) < quiet:
            continue
        msg, links_text, photo_url = format_msg_and_photo(ac, dist_km, alt_m, place)
        try:
            # Invia foto (o testo) + secondo messaggio con i link (deep link app)
            send_telegram(msg, photo_url=photo_url, extra_text=links_text)
            state[scoped_key] = now
            alerted += 1
        except Exception as e:
            print(f"Telegram error ({place}):", e)

    # 3) Se non è partito nulla ma "ci sono velivoli", manda un riepilogo (almeno 1 messaggio ogni run)
    if alerted == 0 and len(eligible) > 0:
        nearest_dist, nearest_alt, nearest_ac = eligible[0]
        lines = [
            f"✈️ {len(eligible)} velivolo/i a bassa quota — {place}",
            f"Raggio: {RADIUS_KM:.0f} km • Soglia: < {int(ALT_THRESHOLD_M)} m",
        ]
        for dist_km, alt_m, ac in eligible[:6]:
            lab, _ = identify(ac)
            lines.append(f"• {lab}: {dist_km:.1f} km, {int(round(alt_m))} m")
        if len(eligible) > 6:
            lines.append(f"+{len(eligible)-6} altri…")

        lat = nearest_ac.get("lat"); lon = nearest_ac.get("lon")
        links_text = build_links_text(nearest_ac, lat, lon)

        # Prova a mettere la foto del più vicino
        nearest_reg = nearest_ac.get("r") or ""
        nearest_hex = (nearest_ac.get("icao") or nearest_ac.get("hex") or "")
        photo_url = get_aircraft_photo(reg=nearest_reg, icao=nearest_hex)

        try:
            send_telegram("\n".join(lines), photo_url=photo_url, extra_text=links_text)
        except Exception as e:
            print(f"Telegram summary error ({place}):", e)

    save_state(state)
    print(f"[{place}] {now.isoformat()} — eligible: {len(eligible)} — alerts sent: {alerted}")

def main():
    for name, lat, lon in LOCATIONS:
        print(f"--- Controllo {name} ---")
        try:
            run_once_for(name, lat, lon)
        except Exception as e:
            print(f"Errore per {name}: {e}")

if __name__ == "__main__":
    main()
