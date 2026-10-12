                      

import os
import threading
import time
import datetime
import collections
import random
import json
import math

import requests as _req
from flask import Flask, Response, jsonify, request, stream_with_context

                                                                                
try:
    import smbus2
    import bme280 as _bme280
    _HAS_BME = True
except ImportError:
    _HAS_BME = False

BASE_DIR       = os.path.dirname(os.path.abspath(__file__))

def _load_env_file(path):
    if not os.path.exists(path):
        return
    for raw in open(path, "r", encoding="utf-8"):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
            v = v[1:-1]
        os.environ.setdefault(k, v)

_load_env_file(os.path.join(BASE_DIR, ".env"))

I2C_BUS          = int(os.getenv("I2C_BUS", "1"))
BME280_ADDR      = int(os.getenv("BME280_ADDR", "119"))
BME_INTERVAL     = float(os.getenv("BME_INTERVAL", "2.0"))
ESP32_IP         = os.getenv("ESP32_IP", "192.168.0.213")
AC_IP            = os.getenv("AC_IP", "192.168.0.230")
KITCHEN_IP       = os.getenv("KITCHEN_IP", "192.168.0.76")
ESP32_BASE_URL   = os.getenv("ESP32_BASE_URL", "")
AC_BASE_URL      = os.getenv("AC_BASE_URL", "")
KITCHEN_API_URL  = os.getenv("KITCHEN_API_URL", "")
DISCORD_WEBHOOK  = os.getenv("DISCORD_WEBHOOK", "")
DISCORD_INTERVAL = int(os.getenv("DISCORD_INTERVAL", "60"))
POLL_INTERVAL    = float(os.getenv("POLL_INTERVAL", "3.0"))
HISTORY_LEN      = int(os.getenv("HISTORY_LEN", "120"))
HISTORY_15MIN    = int(os.getenv("HISTORY_15MIN", "450"))
VGHS_URL         = os.getenv("VGHS_URL", "")
VGHS_INTERVAL    = int(os.getenv("VGHS_INTERVAL", "600"))
SAT_BASE         = os.getenv("SAT_BASE", "")
DC_SAT_WEBHOOK   = os.getenv("DC_SAT_WEBHOOK", "")
DC_LIVE_WEBHOOK  = os.getenv("DC_LIVE_WEBHOOK", "")
ICON_MAIN_URL    = os.getenv("ICON_MAIN_URL", "")
ICON_STATS_URL   = os.getenv("ICON_STATS_URL", "")
CHART_JS_CDN     = os.getenv("CHART_JS_CDN", "")
WEBHOOK_PREFIX   = os.getenv("WEBHOOK_PREFIX", "/api/webhooks/")
SAT_SAVE_DIR     = os.path.join(BASE_DIR, "sat_images")
WEB_PORT         = int(os.getenv("WEB_PORT", "5000"))
WEB_HOST         = os.getenv("WEB_HOST", "0.0.0.0")


def _valid_webhook(url):
    return bool(url) and (WEBHOOK_PREFIX in url)

                                                                          

class SharedState:
    def __init__(self):
        self.lock = threading.Lock()
                
        self.bme = {"temp": None, "hum": None, "pres": None, "ok": False}
        self.bme_hist = {
            "times": collections.deque(maxlen=HISTORY_LEN),
            "temp":  collections.deque(maxlen=HISTORY_LEN),
            "hum":   collections.deque(maxlen=HISTORY_LEN),
            "pres":  collections.deque(maxlen=HISTORY_LEN),
        }
                                                                                     
        self.bme_hist15 = collections.deque(maxlen=HISTORY_15MIN)
        self.esp_hist15 = collections.deque(maxlen=HISTORY_15MIN)
                      
        self.esp = {}
        self.esp_ok = False
        self.last_rained = None                                           
        self.water_hist = {
            "times":  collections.deque(maxlen=HISTORY_LEN),
            "volts":  collections.deque(maxlen=HISTORY_LEN),
        }
            
        self.ac = {}
        self.ac_ok = False
                       
        self.kitchen = {"temp": None, "hum": None, "real_feel": None, "ok": False}
        self.kitchen_ok = False
        self.kitchen_hist15 = collections.deque(maxlen=HISTORY_15MIN)
                                              
        self.discord_url      = ""
        self.discord_interval = 60
        self.discord_active   = False
        self.discord_status   = "Idle"
        self.discord_count    = 0
        self.discord_last     = "~"
        self.discord_preview  = ""
        self._discord_thread   = None
        self.discord_message_id = None
        self.dc_live_msg_id   = None                                               
        self.dc_sat_msg_id    = None                                              
                                  
        self.dc_url      = ""
        self.dc_interval = 60
        self.dc_active   = False
        self.dc_status   = "Idle"
        self.dc_count    = 0
        self.dc_last     = "~"
        self.dc_preview  = ""
                               
        self.vghs_raw    = {}
        self.vghs_ok     = False
        self.vghs_last   = "~"
        self.vghs_error  = ""

state = SharedState()
app   = Flask(__name__, static_folder=None)

                                                                          

def bme_reader():
    global state
    if _HAS_BME:
        try:
            bus = smbus2.SMBus(I2C_BUS)
            cal = _bme280.load_calibration_params(bus, BME280_ADDR)
            while True:
                try:
                    d = _bme280.sample(bus, BME280_ADDR, cal)
                    t = round(d.temperature, 1)
                    h = round(d.humidity, 1)
                    p = round(d.pressure, 1)
                    now_str = datetime.datetime.now().strftime("%H:%M:%S")
                    with state.lock:
                        state.bme = {"temp": t, "hum": h, "pres": p, "ok": True}
                        state.bme_hist["times"].append(now_str)
                        state.bme_hist["temp"].append(t)
                        state.bme_hist["hum"].append(h)
                        state.bme_hist["pres"].append(p)
                        state.bme_hist15.append((time.time(), t, h, p))
                except Exception:
                    pass
                time.sleep(BME_INTERVAL)
        except Exception:
            pass

                         
    t, h, p = 22.3, 54.5, 1013.2
    while True:
        t = round(t + random.gauss(0, 0.15), 1)
        h = round(max(10.0, min(99.0, h + random.gauss(0, 0.25))), 1)
        p = round(p + random.gauss(0, 0.20), 1)
        now_str = datetime.datetime.now().strftime("%H:%M:%S")
        with state.lock:
            state.bme = {"temp": t, "hum": h, "pres": p, "ok": False}
            state.bme_hist["times"].append(now_str)
            state.bme_hist["temp"].append(t)
            state.bme_hist["hum"].append(h)
            state.bme_hist["pres"].append(p)
            state.bme_hist15.append((time.time(), t, h, p))
        time.sleep(BME_INTERVAL)


def esp_reader():
    while True:
        try:
            r = _req.get(f"{ESP32_BASE_URL}/status", timeout=3)
            r.raise_for_status()
            data = r.json()
            now_str = datetime.datetime.now().strftime("%H:%M:%S")
            with state.lock:
                state.esp    = data
                state.esp_ok = True
                v = float(data.get("water_voltage", 0))
                state.water_hist["times"].append(now_str)
                state.water_hist["volts"].append(round(v, 3))
                if v > 0.1:
                    state.last_rained = time.time()
                ot = data.get("bme280_temperature_c")
                oh = data.get("bme280_humidity")
                op = data.get("bme280_pressure_hpa")
                it = data.get("ds18b20_temperature_c")
                state.esp_hist15.append((time.time(),
                    float(ot) if ot is not None else None,
                    float(oh) if oh is not None else None,
                    float(op) if op is not None else None,
                    float(it) if it is not None else None,
                    v))
        except Exception:
            with state.lock:
                state.esp_ok = False
        time.sleep(POLL_INTERVAL)


def kitchen_reader():
    while True:
        try:
            r = _req.get(KITCHEN_API_URL, timeout=3)
            r.raise_for_status()
            data = r.json()
            t  = data.get("temperature_c")
            h  = data.get("humidity_percent")
            rf = data.get("real_feel_c")
            with state.lock:
                state.kitchen = {
                    "temp":      float(t)  if t  is not None else None,
                    "hum":       float(h)  if h  is not None else None,
                    "real_feel": float(rf) if rf is not None else None,
                    "ok":        data.get("status") == "ok",
                }
                state.kitchen_ok = data.get("status") == "ok"
                state.kitchen_hist15.append((time.time(),
                    float(t)  if t  is not None else None,
                    float(h)  if h  is not None else None))
        except Exception:
            with state.lock:
                state.kitchen_ok = False
        time.sleep(POLL_INTERVAL)


                                                              

_CLOUD_COVER = {
    "SKC": "Sky Clear", "CLR": "Clear", "NSC": "No Significant Cloud",
    "NCD": "No Cloud Detected", "FEW": "Few", "SCT": "Scattered",
    "BKN": "Broken", "OVC": "Overcast", "VV": "Vertical Visibility",
}
_WEATHER_CODES = {
    "RA": "Rain", "TSRA": "Thunderstorm + Rain", "TS": "Thunderstorm",
    "DZ": "Drizzle", "TSDZ": "Thunderstorm + Drizzle",
    "SN": "Snow", "FG": "Fog", "BR": "Mist", "HZ": "Haze",
    "FU": "Smoke", "+RA": "Heavy Rain", "-RA": "Light Rain",
    "+TS": "Heavy Thunderstorm", "-TS": "Light Thunderstorm",
    "+TSRA": "Heavy Thunderstorm + Rain", "-TSRA": "Light Thunderstorm + Rain",
    "FZRA": "Freezing Rain", "FZDZ": "Freezing Drizzle",
    "NSW": "No significant weather",
    "NOSIG": "No significant change expected in the next 2 hours (NOSIG)",
}
                                            
_WEATHER_DISCORD = {
    "RA":     "It is raining",
    "+RA":    "It is raining heavily",
    "-RA":    "There is light rain",
    "TSRA":   "There is a thunderstorm with rain",
    "+TSRA":  "There is a heavy thunderstorm with heavy rain",
    "-TSRA":  "There is a light thunderstorm with light rain",
    "TS":     "There is a thunderstorm",
    "+TS":    "There is a heavy thunderstorm",
    "-TS":    "There is a light thunderstorm",
    "DZ":     "There is drizzle",
    "TSDZ":   "There is a thunderstorm with drizzle",
    "SN":     "It is snowing",
    "FG":     "There is fog",
    "BR":     "There is mist",
    "HZ":     "It is hazy",
    "FU":     "There is smoke in the air",
    "FZRA":   "There is freezing rain",
    "FZDZ":   "There is freezing drizzle",
    "NSW":    "No significant weather present",
    "NOSIG":  "No significant change expected in the next 2 hours",
}
_WIND_NAMES = ["North","North-North East","North East","East-North East",
               "East","East-South East","South East","South-South East",
               "South","South-South West","South West","West-South West",
               "West","West-North West","North West","North-North West"]

import re as _re

def _wind_name(deg_str):
    try:
        idx = round(int(deg_str) / 22.5) % 16
        return _WIND_NAMES[idx]
    except Exception:
        return "~"

def _decode_clouds(cloud_str):
    parts = (cloud_str or "").strip().split()
    out = []
    for p in parts:
        m = _re.match(r'^(FEW|SCT|BKN|OVC|VV|SKC|CLR|NSC|NCD)(\d{3})?', p)
        if m:
            cover = _CLOUD_COVER.get(m.group(1), m.group(1))
            if m.group(2):
                out.append(f"{cover} @ {int(m.group(2))*100:,} ft")
            else:
                out.append(cover)
        else:
            out.append(p)
    return " / ".join(out) if out else "~"

def _decode_weather(wx):
    if not wx: return "Clear"
    return "  ·  ".join(_WEATHER_CODES.get(p, p) for p in wx.strip().split())

def _wx_emoji(wx):
    if not wx: return "️"
    c = wx.upper()
    if "TS" in c: return "️"
    if "RA" in c: return "️"
    if "SN" in c: return "️"
    if "FG" in c or "HZ" in c: return "️"
    return "️"

def _vis_label(vis_m):
    try:
        v = int(vis_m)
        if v >= 10000: return "10 km+ ~ Excellent"
        if v >= 5000:  return f"{v//1000} km ~ Good"
        if v >= 1500:  return f"{v} m ~ Moderate"
        if v >= 800:   return f"{v} m ~ Poor"
        return f"{v} m ~ Very Poor ️"
    except Exception:
        return vis_m or "~"

def _metar_time_to_bst(time_str):
    """Convert METAR time like '270305Z' to Bangladesh Standard Time (UTC+6)."""
    m = _re.match(r'^(\d{2})(\d{2})(\d{2})Z$', (time_str or '').strip())
    if not m:
        return time_str or '~'
    day, hour, minute = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        now_utc = datetime.datetime.utcnow()
        dt_utc = datetime.datetime(now_utc.year, now_utc.month, day,
                                   hour, minute,
                                   tzinfo=datetime.timezone.utc)
                                                                              
        if (dt_utc - datetime.datetime.now(datetime.timezone.utc)).total_seconds() > 43200:
            if now_utc.month == 1:
                dt_utc = dt_utc.replace(year=now_utc.year - 1, month=12)
            else:
                import calendar as _cal
                dt_utc = dt_utc.replace(month=now_utc.month - 1)
        bst = dt_utc + datetime.timedelta(hours=6)
        return bst.strftime("%d %b %H:%M BST")
    except Exception:
        return time_str or '~'

def _decode_weather_discord(wx):
    """Full natural-language weather description for Discord."""
    if not wx: return "Skies are clear"
    parts = wx.strip().split()
    decoded = []
    for p in parts:
        decoded.append(_WEATHER_DISCORD.get(p, _WEATHER_CODES.get(p, p)))
    return " and ".join(decoded)

def _decode_warn_label(label):
    """Translate aviation shorthand in warning labels to plain English."""
    if not label: return label
    replacements = [
                                                                
        ('+TSRA', 'Heavy Thunderstorm with Heavy Rain'),
        ('-TSRA', 'Light Thunderstorm with Light Rain'),
        ('TSRA',  'Thunderstorm with Rain'),
        ('+TS',   'Heavy Thunderstorm'),
        ('-TS',   'Light Thunderstorm'),
        ('TS',    'Thunderstorm'),
        ('+RA',   'Heavy Rain'),
        ('-RA',   'Light Rain'),
        ('FZRA',  'Freezing Rain'),
        ('FZDZ',  'Freezing Drizzle'),
        ('RA',    'Rain'),
        ('DZ',    'Drizzle'),
        ('SN',    'Snow'),
        ('FG',    'Fog'),
        ('BR',    'Mist'),
        ('HZ',    'Haze'),
        ('FU',    'Smoke'),
        ('NSW',   'No Significant Weather'),
        ('NOSIG', 'No Significant Change'),
                                      
        ('Low CL',  'Low Cloud'),
        ('LCA',     'Low Cloud'),
        ('LCL',     'Low Cloud Level'),
        ('CB',      'Cumulonimbus (Thunderstorm Cloud)'),
        ('TCU',     'Towering Cumulus'),
        ('OVC',     'Overcast'),
        ('BKN',     'Broken Cloud'),
        ('SCT',     'Scattered Cloud'),
        ('FEW',     'Few Cloud'),
        ('SKC',     'Sky Clear'),
                           
        ('VIS',   'Visibility'),
        ('WS',    'Wind Shear'),
        ('LLWS',  'Low-Level Wind Shear'),
        ('VA',    'Volcanic Ash'),
        ('GR',    'Hail'),
        ('GS',    'Small Hail'),
    ]
    result = label
    for code, full in replacements:
        result = result.replace(code, full)
    return result

def _build_vghs_display(entry):
    if not entry: return {}
    wx = entry.get("val_weather", "")
    return {
        "code":      entry.get("val_code", ""),
        "time_utc":  entry.get("val_time", "~"),
        "time_bst":  _metar_time_to_bst(entry.get("val_time", "")),
        "wind_speed":entry.get("val_wind", "~"),
        "wind_dir":  entry.get("val_wind_dir", "~"),
        "wind_name": _wind_name(entry.get("val_wind_dir", "")),
        "visibility":entry.get("val_visibility", "~"),
        "vis_label": _vis_label(entry.get("val_visibility", "")),
        "weather_raw": wx,
        "weather":   _decode_weather(wx),
        "wx_emoji":  _wx_emoji(wx),
        "cloud_raw": entry.get("val_cloud", "~"),
        "cloud":     _decode_clouds(entry.get("val_cloud", "")),
        "temp":      entry.get("val_tt", "~"),
        "dewpoint":  entry.get("val_td", "~"),
        "rh":        entry.get("val_rh", "~"),
        "qnh":       entry.get("val_qnh", "~"),
        "end":       entry.get("val_end", ""),
        "raw_metar": entry.get("val_robex", ""),
    }

def _clean_warnings(warnings):
    import html as _html_mod
    out = []
    for w in (warnings or []):
        body = w.get("content", "")
                                                                               
        body = _html_mod.unescape(body)
        body = _re.sub(r'<[^>]*>', '', body, flags=_re.DOTALL)
        body = _html_mod.unescape(body)                                            
        body = _re.sub(r'\s+', ' ', body).strip()
        out.append({
            "label":      w.get("label", "").replace("=", "").strip(),
            "issue_time": w.get("issue_time", "~"),
            "date_start": w.get("date_start", "~"),
            "date_end":   w.get("date_end",   "~"),
            "content":    body,
        })
    return out

def vghs_poller():
    while True:
        try:
            r = _req.get(VGHS_URL, timeout=12)
            r.raise_for_status()
            data = r.json()
            with state.lock:
                state.vghs_raw  = data
                state.vghs_ok   = True
                state.vghs_last = datetime.datetime.now().strftime("%H:%M:%S")
                state.vghs_error = ""
        except Exception as e:
            with state.lock:
                state.vghs_ok    = False
                state.vghs_error = str(e)
        time.sleep(VGHS_INTERVAL)

def _get_vghs_payload():
    """Return serialisable VGHS dict for the /api/vghs route and SSE."""
    with state.lock:
        raw  = dict(state.vghs_raw)
        ok   = state.vghs_ok
        last = state.vghs_last
        err  = state.vghs_error
    main     = _build_vghs_display(raw.get("data", {}))
    second   = _build_vghs_display(raw.get("second", {}))
    warnings = _clean_warnings(raw.get("warnings", []))
    return {"ok": ok, "last": last, "error": err,
            "main": main, "second": second, "warnings": warnings}

                                                               

def _sat_url_now():
    """Return (url, bst_str, utc_str) for the latest Himawari image (30-min offset)."""
    now_utc = datetime.datetime.utcnow()
    total_min = now_utc.hour * 60 + now_utc.minute
                                                    
    code_min  = (total_min - 30) // 10 * 10
    code_min  = code_min % 1440
    ch = code_min // 60
    cm = code_min % 60
    code = f"{ch:02d}{cm:02d}"
    url  = SAT_BASE.format(code=code)
                                       
    img_min = (code_min + 10) % 1440
    ih = img_min // 60; im = img_min % 60
    bh = (img_min + 360) % 1440 // 60; bm = (img_min + 360) % 60
    utc_str = f"{ih:02d}:{im:02d} UTC"
    bst_str = f"{bh:02d}:{bm:02d} BST"
    return url, bst_str, utc_str

def _real_feel(t, rh, wind_ms=None):
    """Australian BOM apparent temperature formula. Returns float or None."""
    if t is None or rh is None:
        return None
    try:
        e = (float(rh) / 100) * 6.105 * math.exp(17.27 * float(t) / (237.7 + float(t)))
        w = float(wind_ms) if wind_ms is not None else 0.0
        return float(t) + 0.33 * e - 0.70 * w - 4.00
    except Exception:
        return None

def _rf_str_d(t, rh, wind_ms=None):
    """Return Discord-formatted real feel string, e.g. '(RF- 28.3°C)' or ''."""
    rf = _real_feel(t, rh, wind_ms)
    return f"(RF- {rf:.1f}°C)" if rf is not None else ""

def _build_measurements_table(r_t, r_h, r_p, out_t, out_h, out_p, in_t, vm, k_t=None, k_h=None, k_rf=None):
    """Build a list-style comparison for Discord embeds."""
    def fv(v, dec=1):
        if v is None: return "~"
        try: return f"{float(v):.{dec}f}"
        except: return str(v) if v else "~"

    vt  = str(vm.get("temp") or "~")
    vrh = str(vm.get("rh")   or "~")
    vqn = str(vm.get("qnh")  or "~")
    wind_dir  = vm.get("wind_dir","~")
    wind_name = vm.get("wind_name","~")
    wind_spd  = vm.get("wind_speed","~")
    wind_kmh  = ""
    try: wind_kmh = f" ({round(int(wind_spd) * 1.852)} km/h)"
    except: pass
    vis_lbl = vm.get("vis_label","~")
    cloud   = vm.get("cloud","~")
    wx_disc = _decode_weather_discord(vm.get("weather_raw",""))

                     
    rf_room = _real_feel(r_t, r_h)
    rf_out  = _real_feel(out_t, out_h)
    vt_num  = None
    try: vt_num = float(vt) if vt != '~' else None
    except: pass
    vrh_num = None
    try: vrh_num = float(vrh) if vrh != '~' else None
    except: pass
    vws_ms  = 0.0
    try: vws_ms = int(wind_spd) * 0.5144
    except: pass
    rf_vghs = _real_feel(vt_num, vrh_num, vws_ms)
    def frf(v): return f" (RF- {v:.1f}°C)" if v is not None else ""

    return (
        "️ **Temperature**\n"
        f">  Room (Pi)       ~ **{fv(r_t)}°C**{frf(rf_room)}\n"
        f"> ️ Outside (shade) ~ **{fv(out_t)}°C**{frf(rf_out)}\n"
        f"> ️ Dining (DS18)   ~ **{fv(in_t)}°C**\n"
        f">  Kitchen         ~ **{fv(k_t)}°C**{frf(k_rf)}\n"
        f"> ️ Dhaka Airport   ~ **{vt}°C**{frf(rf_vghs)}\n"
        "\n"
        " **Humidity**\n"
        f">  Room (Pi)       ~ **{fv(r_h)}%**\n"
        f"> ️ Outside (shade) ~ **{fv(out_h)}%**\n"
        f">  Kitchen         ~ **{fv(k_h)}%**\n"
        f"> ️ Dhaka Airport   ~ **{vrh}%**\n"
        "\n"
        " **Pressure**\n"
        f">  Room (Pi)       ~ **{fv(r_p)} hPa**\n"
        f"> ️ Outside (shade) ~ **{fv(out_p)} hPa**\n"
        f"> ️ Dhaka Airport   ~ **{vqn} hPa**\n"
        "\n"
        " **Wind** ~ "  f"{wind_dir}° {wind_name} @ **{wind_spd} kt**{wind_kmh}\n"
        f"️ **Visibility** ~ {vis_lbl}\n"
        f"️ **Cloud** ~ {cloud}\n"
        f"️ **Conditions** ~ {wx_disc}"
    )

def _discord_build_payload():
    """Build the Discord embed payload + text preview from current sensor state."""
    now_ts = time.time()
    cutoff  = now_ts - 15 * 60
    with state.lock:
        bme = dict(state.bme)
        esp = dict(state.esp)
        ac  = dict(state.ac)
        kitchen = dict(state.kitchen)
        last_rained_ts = state.last_rained
                              
        bme_15 = None
        for entry in state.bme_hist15:
            if entry[0] >= cutoff:
                bme_15 = {"temp": entry[1], "hum": entry[2], "pres": entry[3],
                          "age": round(now_ts - entry[0])}
                break
        esp_15 = None
        for entry in state.esp_hist15:
            if entry[0] >= cutoff:
                esp_15 = {"out_temp": entry[1], "out_hum": entry[2], "out_pres": entry[3],
                          "in_temp": entry[4], "water_v": entry[5],
                          "age": round(now_ts - entry[0])}
                break

    ts = datetime.datetime.now().strftime("%d %b %Y  ·  %H:%M:%S")

    def _f(v, dec=1, unit=""): return f"{v:.{dec}f}{unit}" if v is not None else "~"
    def _delta(now_v, ago_v, unit=""):
        if now_v is None or ago_v is None: return "~"
        d = now_v - ago_v
        if abs(d) < 0.05: return f" ±0{unit}"
        return f"▲ +{d:.1f}{unit}" if d > 0 else f"▼ {d:.1f}{unit}"
    def _age(secs):
        if secs is None: return ""
        return f"{secs}s ago" if secs < 60 else f"{round(secs/60)}min ago"

                                
    r_t = bme.get('temp'); r_h = bme.get('hum'); r_p = bme.get('pres')
    bme_src = " Real Sensor" if bme.get('ok') else " Simulated"

                 
    b15_t = bme_15['temp'] if bme_15 else None
    b15_h = bme_15['hum']  if bme_15 else None
    b15_p = bme_15['pres'] if bme_15 else None
    b15_age = _age(bme_15['age']) if bme_15 else "waiting…"

    room_val = (
        f"️  Temp     **{_f(r_t,'1','°C')}** {_rf_str_d(r_t, r_h)}"
        + (f"  •  _{_delta(r_t, b15_t, '°C')} since {b15_age}_" if bme_15 else "  •  _no history yet_")
        + f"\n  Humidity  **{_f(r_h,'1','%')}**"
        + (f"  •  _{_delta(r_h, b15_h, '%')} since {b15_age}_" if bme_15 else "")
        + f"\n  Pressure  **{_f(r_p,'1',' hPa')}**"
        + (f"  •  _{_delta(r_p, b15_p, ' hPa')} since {b15_age}_" if bme_15 else "")
    )

                            
    g_light = esp.get('light', '~')
    g_mode  = esp.get('mode',  '~')
    g_sched = esp.get('schedule', '~')
    g_time  = esp.get('time', '~')
    light_icon = "" if g_light == "ON" else ""

                                   
    g_bme_t = esp.get('bme280_temperature_c')
    g_bme_h = esp.get('bme280_humidity')
    g_bme_p = esp.get('bme280_pressure_hpa')
    g_ds_t  = esp.get('ds18b20_temperature_c')
    g_wv    = esp.get('water_voltage')
    g_wr    = esp.get('water_raw', '~')

    out_t = float(g_bme_t) if g_bme_t is not None else None
    out_h = float(g_bme_h) if g_bme_h is not None else None
    out_p = float(g_bme_p) if g_bme_p is not None else None
    in_t  = float(g_ds_t)  if g_ds_t  is not None else None
    rain_v  = float(g_wv) if g_wv is not None else None
    def _last_rained_str(lr_ts, now):
        if lr_ts is None: return "no data yet"
        secs = int(now - lr_ts)
        if secs < 60:   return f"{secs}s ago"
        if secs < 3600: return f"{secs // 60}min ago"
        h = secs // 3600; m = (secs % 3600) // 60
        return f"{h}h {m}min ago" if m else f"{h}h ago"
    _lr = _last_rained_str(last_rained_ts, now_ts)
    rain_status = (
        "️ Currently raining!"
        if rain_v is not None and rain_v > 0.1
        else f"️ Dry  •  _Last rained: {_lr}_"
    )

    e15_ot  = esp_15['out_temp'] if esp_15 else None
    e15_oh  = esp_15['out_hum']  if esp_15 else None
    e15_op  = esp_15['out_pres'] if esp_15 else None
    e15_it  = esp_15['in_temp']  if esp_15 else None
    e15_age = _age(esp_15['age']) if esp_15 else "waiting…"

    outdoor_val = (
        f"️  Temp     **{_f(out_t,'1','°C')}** {_rf_str_d(out_t, out_h)}"
        + (f"  •  _{_delta(out_t, e15_ot, '°C')} since {e15_age}_" if esp_15 else "  •  _no history yet_")
        + f"\n  Humidity  **{_f(out_h,'1','%')}**"
        + (f"  •  _{_delta(out_h, e15_oh, '%')} since {e15_age}_" if esp_15 else "")
        + f"\n  Pressure  **{_f(out_p,'1',' hPa')}**"
        + (f"  •  _{_delta(out_p, e15_op, ' hPa')} since {e15_age}_" if esp_15 else "")
    )

    dining_temp_val = (
        f"️  Temperature  **{_f(in_t,'1','°C')}**"
        + (f"  •  _{_delta(in_t, e15_it, '°C')} since {e15_age}_" if esp_15 else "  •  _no history yet_")
    )

    rain_val = (
        f"️  Voltage  **{_f(rain_v,'3',' V')}**  (raw {g_wr})\n"
        + f"{rain_status}"
    )

    garden_ctrl = (
        f"{light_icon}  Light     **{g_light}**\n"
        f"  Mode      **{g_mode}**\n"
        f"⏰  Schedule   **{g_sched}**\n"
        f"  ESP Time  **{g_time}**"
    )

           
    a_pwr  = ac.get('power', '~')
    a_temp = str(ac.get('temp', '~')) + ' °C' if ac.get('temp') else '~'
    a_mode = ac.get('mode', '~')
    a_fan  = ac.get('fan',  '~')
    a_sv   = ac.get('swingV', '~')
    a_sh   = ac.get('swingH', '~')
    extras = [k.capitalize() for k in ['turbo', 'quiet', 'sleep', 'health', 'lock']
              if ac.get(k) == 'ON']
    pwr_icon = "" if a_pwr == "ON" else ""

    ac_val = (
        f"{pwr_icon}  Power  **{a_pwr}**  ·  ️  **{a_temp}**\n"
        f"  Mode  **{a_mode}**  ·    Fan  **{a_fan}**\n"
        f"↕  V-Swing  **{a_sv}**  ·  ↔  H-Swing  **{a_sh}**"
        + (f"\n  Extras: **{', '.join(extras)}**" if extras else "")
    )

                
    k_t  = kitchen.get('temp')
    k_h  = kitchen.get('hum')
    k_rf = kitchen.get('real_feel')
    k_ok = kitchen.get('ok', False)
    k_rf_str = f"(RF- {k_rf:.1f}°C)" if k_rf is not None else ""
    k_src = " Live" if k_ok else " Offline"
    kitchen_val = (
        f"️  Temp     **{_f(k_t,'1','°C')}** {k_rf_str}\n"
        f"  Humidity  **{_f(k_h,'1','%')}**\n"
        f"_{k_src}_"
    )

                                 
    colour = 0xa6e3a1 if (r_t or 999) <= 26 else 0xfab387 if (r_t or 999) <= 32 else 0xf38ba8

                      
    vghs = _get_vghs_payload()
    vm   = vghs.get("main", {})
    vwarn = vghs.get("warnings", [])
    def _vf(k): return vm.get(k, "~") or "~"
    vghs_ok = vghs.get("ok", False)
    vghs_status = " Live" if vghs_ok else " Offline"
    vwx_disc = _decode_weather_discord(vm.get("weather_raw", ""))
    vwx_str = f"{_vf('wx_emoji')} {_vf('weather_raw')} ~ {_decode_weather_discord(vm.get('weather_raw',''))}" if vm.get("weather_raw") else "️ Skies are clear"
                    
    _vt_n = None
    try: _vt_n = float(vm.get('temp')) if vm.get('temp') else None
    except: pass
    _vrh_n = None
    try: _vrh_n = float(vm.get('rh')) if vm.get('rh') else None
    except: pass
    _vws_ms = 0.0
    try: _vws_ms = int(vm.get('wind_speed') or 0) * 0.5144
    except: pass
    _vghs_rf = _rf_str_d(_vt_n, _vrh_n, _vws_ms)
    vghs_field = (
        f"️  **{_vf('temp')}°C** {_vghs_rf}  (Dew: {_vf('dewpoint')}°C)   **{_vf('rh')}%**\n"
        f"  QNH: **{_vf('qnh')} hPa**\n"
        f"  Wind: **{_vf('wind_dir')}° ({_vf('wind_name')}) @ {_vf('wind_speed')} kt**\n"
        f"️  Visibility: **{_vf('vis_label')}**\n"
        f"️  Cloud: **{_vf('cloud')}**\n"
        f"{vwx_str}\n"
        + (f"️ **{len(vwarn)} Warning{'s' if len(vwarn)>1 else ''}:** "
           + " | ".join(_decode_warn_label(w.get("label",""))[:60] for w in vwarn[:2])
           if vwarn else " No active warnings")
        + f"\n_ {_vf('time_bst')}  ·  Last fetched: {vghs.get('last', '~')}  ·  {vghs_status}_"
    )

    payload = {
        "username": "Live Dashboard",
        "avatar_url": ICON_MAIN_URL,
        "embeds": [{
            "title": "  Infi's Room  ~  Live Dashboard",
            "color": colour,
            "description": f"  **{ts}**  ·  {bme_src}",
            "fields": [
                {
                    "name": "  Infi's Room  (BME280 on Pi)",
                    "value": room_val,
                    "inline": False
                },
                {"name": "\u200b", "value": "\u200b", "inline": False},
                {
                    "name": "️  Outside Temperature (Shade) & Humidity & Pressure",
                    "value": outdoor_val,
                    "inline": True
                },
                {
                    "name": "️  Dining Room Temperature",
                    "value": dining_temp_val,
                    "inline": True
                },
                {
                    "name": "  Kitchen Temperature & Humidity",
                    "value": kitchen_val,
                    "inline": True
                },
                {"name": "\u200b", "value": "\u200b", "inline": False},
                {
                    "name": "️  Rain Sensor",
                    "value": rain_val,
                    "inline": True
                },
                {
                    "name": "  Garden Control",
                    "value": garden_ctrl,
                    "inline": True
                },
                {
                    "name": "️  Haier AC",
                    "value": ac_val,
                    "inline": True
                },
                {"name": "\u200b", "value": "\u200b", "inline": False},
                {
                    "name": "️  VGHS ~ Dhaka Airport Weather",
                    "value": vghs_field,
                    "inline": False
                },
                {"name": "\u200b", "value": "\u200b", "inline": False},
                {
                    "name": "  All Measurements ~ Comparison Table",
                    "value": _build_measurements_table(r_t, r_h, r_p, out_t, out_h, out_p, in_t, vm, k_t, k_h, k_rf),
                    "inline": False
                },
            ],
            "footer": {
                "text": "Infi\u2019s Room  \u00b7  Live Dashboard  \u00b7  Edits every 1 min  \u00b7  \u0394 = change vs 15 min ago",
                "icon_url": ICON_MAIN_URL
            },
            "image": {"url": _sat_url_now()[0]},
            "timestamp": datetime.datetime.utcnow().isoformat(),
        }]
    }

    preview = (
        f" Room:    {_f(r_t,'1','°C')}  |  {_f(r_h,'1','%')}  |  {_f(r_p,'1',' hPa')}\n"
        f"  Δ 15m:  temp {_delta(r_t,b15_t,'°C')}  hum {_delta(r_h,b15_h,'%')}  pres {_delta(r_p,b15_p,' hPa')}\n"
        f"️ Outside: {_f(out_t,'1','°C')}  {_f(out_h,'1','%')}  {_f(out_p,'1',' hPa')}\n"
        f"  Δ 15m:  temp {_delta(out_t,e15_ot,'°C')}  hum {_delta(out_h,e15_oh,'%')}  pres {_delta(out_p,e15_op,' hPa')}\n"
        f"�️ DS18B20:  {_f(in_t,'1','°C')}  Δ {_delta(in_t,e15_it,'°C')}\n"
        f"� Kitchen:  {_f(k_t,'1','°C')}  {_f(k_h,'1','%')}\n"
        f"�️ Rain:    {_f(rain_v,'3',' V')}  (raw {g_wr})  {rain_status}\n"
        f" Garden:  Light {g_light}  |  {g_mode}  |  {g_sched}\n"
        f"️ AC:      {a_pwr}  ·  {a_temp}  ·  {a_mode}  ·  Fan {a_fan}\n"
        f"️ VGHS:   {vm.get('temp','~')}°C  {_decode_weather_discord(vm.get('weather_raw',''))}  Wind {vm.get('wind_dir','~')}° {vm.get('wind_name','~')} / {vm.get('wind_speed','~')}kt  Vis {vm.get('vis_label','~')}\n"
        f" Updated: {ts}"
    )
    return payload, preview


def _discord_send_or_edit():
    """Send a new message or edit the existing one. Returns (ok, msg)."""
    payload, preview = _discord_build_payload()
    with state.lock:
        msg_id = state.discord_message_id
        sat_msg_id = state.dc_sat_msg_id
        live_msg_id = state.dc_live_msg_id
    try:
                                           
        if msg_id is None:
            r = _req.post(DISCORD_WEBHOOK + "?wait=true", json=payload, timeout=8)
            r.raise_for_status()
            now = datetime.datetime.now().strftime("%H:%M:%S")
            with state.lock:
                state.discord_message_id = r.json().get("id")
                state.discord_count += 1
                state.discord_last = now
                state.discord_status = "Running "
                state.discord_preview = preview
        else:
            r = _req.patch(f"{DISCORD_WEBHOOK}/messages/{msg_id}", json=payload, timeout=8)
            r.raise_for_status()
            now = datetime.datetime.now().strftime("%H:%M:%S")
            with state.lock:
                state.discord_count += 1
                state.discord_last = now
                state.discord_status = "Running "
                state.discord_preview = preview

                                                              
        try:
            if sat_msg_id is None:
                rs = _req.post(DC_SAT_WEBHOOK + "?wait=true", json=payload, timeout=8)
                rs.raise_for_status()
                with state.lock:
                    state.dc_sat_msg_id = rs.json().get("id")
            else:
                _req.patch(f"{DC_SAT_WEBHOOK}/messages/{sat_msg_id}", json=payload, timeout=8)
        except Exception:
            pass

                                                               
        try:
            if live_msg_id is None:
                rl = _req.post(DC_LIVE_WEBHOOK + "?wait=true", json=payload, timeout=8)
                rl.raise_for_status()
                with state.lock:
                    state.dc_live_msg_id = rl.json().get("id")
            else:
                _req.patch(f"{DC_LIVE_WEBHOOK}/messages/{live_msg_id}", json=payload, timeout=8)
        except Exception:
            pass

        return True, "OK"
    except Exception as e:
        with state.lock:
            state.discord_status = f"Error: {e}"
        return False, str(e)


def _discord_loop():
    while True:
        with state.lock:
            if not state.discord_active:
                break
        _discord_send_or_edit()
        for _ in range(DISCORD_INTERVAL):
            with state.lock:
                if not state.discord_active:
                    return
            time.sleep(1)

                                                                             

def _dc_vghs_field():
    """Compact VGHS summary for Discord C embed."""
    v = _get_vghs_payload()
    m = v.get("main", {})
    warns = v.get("warnings", [])
    def _f(k): return m.get(k, "~") or "~"
    ok_str = " Live" if v.get("ok") else " Offline"
    wx = f"{_f('wx_emoji')} {_decode_weather_discord(m.get('weather_raw',''))}" if m.get("weather_raw") else "️ Skies are clear"
                    
    _dc_vt = None
    try: _dc_vt = float(_f('temp')) if _f('temp') != '~' else None
    except: pass
    _dc_vrh = None
    try: _dc_vrh = float(_f('rh')) if _f('rh') != '~' else None
    except: pass
    _dc_vws = 0.0
    try: _dc_vws = int(_f('wind_speed') or 0) * 0.5144
    except: pass
    _dc_vrf = _rf_str_d(_dc_vt, _dc_vrh, _dc_vws)
    return (
        f"️  **{_f('temp')}°C** {_dc_vrf}  Dew point: {_f('dewpoint')}°C   **{_f('rh')}%**\n"
        f"  QNH: **{_f('qnh')} hPa**\n"
        f"  Wind: **{_f('wind_dir')}° ({_f('wind_name')}) @ {_f('wind_speed')} kt**\n"
        f"️  Visibility: **{_f('vis_label')}**\n"
        f"️  Cloud cover: **{_f('cloud')}**\n"
        f"{wx}\n"
        + (f"️ **{len(warns)} Warning(s):** " + " | ".join(_decode_warn_label(w.get('label',''))[:60] for w in warns[:2])
           if warns else " No active warnings")
        + f"\n_ {_f('time_bst')}  ·  Last fetched: {v.get('last','~')}  ·  {ok_str}_"
    )


def _dc_build_payload():
    """Build payload for the customizable Discord C webhook."""
    with state.lock:
        bme = dict(state.bme)
        esp = dict(state.esp)
        ac  = dict(state.ac)
        kitchen = dict(state.kitchen)
        bme_hist_temp  = list(state.bme_hist["temp"])
        bme_hist_hum   = list(state.bme_hist["hum"])
        bme_hist_pres  = list(state.bme_hist["pres"])

    ts  = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    temp = bme.get('temp'); hum = bme.get('hum'); pres = bme.get('pres')

    def fmt(v, dec=1, unit=""): return f"{v:.{dec}f}{unit}" if v is not None else "~"
    def minmax(lst): return (f"{min(lst):.1f}", f"{max(lst):.1f}", f"{sum(lst)/len(lst):.1f}") if lst else ("~","~","~")

    t_mn, t_mx, t_av = minmax(bme_hist_temp)
    h_mn, h_mx, h_av = minmax(bme_hist_hum)
    p_mn, p_mx, p_av = minmax(bme_hist_pres)

    colour = 0xa6e3a1 if (temp or 999) <= 26 else 0xfab387 if (temp or 999) <= 32 else 0xf38ba8

            
    g_light = esp.get('light', '~'); g_mode = esp.get('mode','~')
    g_sched = esp.get('schedule','~')
    g_wv    = esp.get('water_voltage')
    g_wr    = esp.get('water_raw','~')
    g_bme_t = esp.get('bme280_temperature_c')
    g_bme_h = esp.get('bme280_humidity')
    g_bme_p = esp.get('bme280_pressure_hpa')
    g_ds_t  = esp.get('ds18b20_temperature_c')

    rain_v_dc   = float(g_wv) if g_wv is not None else None
    rain_txt    = f"{rain_v_dc:.3f} V (raw {g_wr})" if rain_v_dc is not None else "~"
    with state.lock:
        _lr_ts_dc = state.last_rained
    def _lr_fmt(lr_ts):
        if lr_ts is None: return "no data yet"
        secs = int(time.time() - lr_ts)
        if secs < 60:   return f"{secs}s ago"
        if secs < 3600: return f"{secs // 60}min ago"
        h = secs // 3600; m = (secs % 3600) // 60
        return f"{h}h {m}min ago" if m else f"{h}h ago"
    rain_st_dc  = (
        "️ Currently raining!"
        if rain_v_dc is not None and rain_v_dc > 0.1
        else f"️ Dry  ~  Last rained: {_lr_fmt(_lr_ts_dc)}"
    )

        
    a_pwr  = ac.get('power','~'); a_tmp = ac.get('temp','~')
    a_mode = ac.get('mode','~'); a_fan = ac.get('fan','~')
    a_sv   = ac.get('swingV','~'); a_sh = ac.get('swingH','~')
    extras = [k.capitalize() for k in ['turbo','quiet','sleep','health','lock'] if ac.get(k)=='ON']

             
    k_t  = kitchen.get('temp')
    k_h  = kitchen.get('hum')
    k_rf = kitchen.get('real_feel')
    k_ok = kitchen.get('ok', False)
    k_dc_rf_str = f" (RF- {k_rf:.1f}°C)" if k_rf is not None else ""
    k_src = " Live" if k_ok else " Offline"

    source = "BME280 Sensor" if _HAS_BME else "Simulated"
                         
    _vghs_dc = _get_vghs_payload()
    _vm_dc   = _vghs_dc.get("main", {})
                        
    _rf_room_dc = _real_feel(temp, hum)
    _rf_room_dc_str = f"  {_rf_str_d(temp, hum)}" if _rf_room_dc is not None else ""
    payload = {
        "username": "Infi Room Stats",
        "avatar_url": ICON_STATS_URL,
        "embeds": [{
            "title": "  Infi's Room  ~  Detailed Stats",
            "color": colour,
            "description": f"**{source}**  ·  {ts}",
            "fields": [
                {"name": "️  Temperature",
                 "value": f"**{fmt(temp,'1','°C')}**{_rf_room_dc_str}\nMin {t_mn} / Max {t_mx} / Avg {t_av} °C",
                 "inline": True},
                {"name": "  Humidity",
                 "value": f"**{fmt(hum,'1','%')}**\nMin {h_mn} / Max {h_mx} / Avg {h_av} %",
                 "inline": True},
                {"name": "  Pressure",
                 "value": f"**{fmt(pres,'1',' hPa')}**\nMin {p_mn} / Max {p_mx} / Avg {p_av} hPa",
                 "inline": True},
                {"name": "  Garden Status",
                 "value": (
                     f" Light: **{g_light}**  ·   Mode: **{g_mode}**\n"
                     f"⏰ Schedule: **{g_sched}**\n"
                     f"️ Rain: **{rain_txt}**  {rain_st_dc}"
                 ), "inline": False},
                {"name": "️  Garden Temps",
                 "value": (
                     f"Outside (shade): **{fmt(float(g_bme_t) if g_bme_t else None,'1','°C')}** {_rf_str_d(float(g_bme_t) if g_bme_t else None, float(g_bme_h) if g_bme_h else None)}"
                     f"   **{fmt(float(g_bme_h) if g_bme_h else None,'1','%')}**"
                     f"   **{fmt(float(g_bme_p) if g_bme_p else None,'1',' hPa')}**\n"
                     f"Inside (house): **{fmt(float(g_ds_t) if g_ds_t else None,'1','°C')}**"
                 ), "inline": False},
                {"name": "  Kitchen",
                 "value": (
                     f"️ Temp: **{fmt(k_t,'1','°C')}**{k_dc_rf_str}\n"
                     f" Humidity: **{fmt(k_h,'1','%')}**\n"
                     f"_{k_src}_"
                 ), "inline": False},
                {"name": "️  Haier AC",
                 "value": (
                     f" Power: **{a_pwr}**  ·  ️ Temp: **{a_tmp}°C**\n"
                     f" Mode: **{a_mode}**  ·   Fan: **{a_fan}**\n"
                     f"↕↔ Swing: **{a_sv}** / **{a_sh}**"
                     + (f"\n {', '.join(extras)}" if extras else "")
                 ), "inline": False},
                {"name": "\u200b", "value": "\u200b", "inline": False},
                {"name": "️  VGHS ~ Dhaka Airport Weather",
                 "value": _dc_vghs_field(), "inline": False},
                {"name": "\u200b", "value": "\u200b", "inline": False},
                {"name": "  All Measurements ~ Comparison Table",
                 "value": _build_measurements_table(
                     temp, hum, pres,
                     float(g_bme_t) if g_bme_t else None,
                     float(g_bme_h) if g_bme_h else None,
                     float(g_bme_p) if g_bme_p else None,
                     float(g_ds_t)  if g_ds_t  else None,
                     _vm_dc, k_t, k_h, k_rf), "inline": False},
            ],
            "footer": {"text": f"Infi's Room  ·  Custom Stats Bot  ·  {len(bme_hist_temp)} readings"},
            "image": {"url": _sat_url_now()[0]},
            "timestamp": datetime.datetime.utcnow().isoformat(),
        }]
    }

    preview = (
        f" Room:  {fmt(temp,'1','°C')}  {fmt(hum,'1','%')}  {fmt(pres,'1',' hPa')}\n"
        f" Garden:  {g_light}  |  {g_mode}  |  {g_sched}  |  ️ Rain {rain_txt}  {rain_st_dc}\n"
        f"️ Outside: {fmt(float(g_bme_t) if g_bme_t else None,'1','°C')}  "
        f"   Inside: {fmt(float(g_ds_t) if g_ds_t else None,'1','°C')}\n"
        f" Kitchen: {fmt(k_t,'1','°C')}  {fmt(k_h,'1','%')}\n"
        f"️ AC:  {a_pwr}  ·  {a_tmp}°C  ·  {a_mode}  ·  Fan {a_fan}\n"
        f" Sent: {datetime.datetime.now().strftime('%H:%M:%S')}"
    )
    return payload, preview


def _dc_send_once():
    with state.lock:
        url = state.dc_url
    if not url:
        return False, "No webhook URL set."
    payload, preview = _dc_build_payload()
    try:
        r = _req.post(url, json=payload, timeout=8)
        r.raise_for_status()
        now = datetime.datetime.now().strftime("%H:%M:%S")
        with state.lock:
            state.dc_count   += 1
            state.dc_last     = now
            state.dc_status   = "Running "
            state.dc_preview  = preview
        return True, "OK"
    except Exception as e:
        with state.lock:
            state.dc_status = f"Error: {e}"
        return False, str(e)


def _dc_loop():
    while True:
        with state.lock:
            if not state.dc_active:
                break
            interval = state.dc_interval
        _dc_send_once()
        for _ in range(interval):
            with state.lock:
                if not state.dc_active:
                    return
            time.sleep(1)


def _sat_download_and_save():
    """Download the current Himawari image and save it to SAT_SAVE_DIR."""
    try:
        os.makedirs(SAT_SAVE_DIR, exist_ok=True)
        url, bst_str, utc_str = _sat_url_now()
        now = datetime.datetime.now()
        fname = now.strftime("%Y-%m-%d_%H%M") + f"_{bst_str.replace(':','').replace(' ','_')}.jpg"
        fpath = os.path.join(SAT_SAVE_DIR, fname)
        if not os.path.exists(fpath):
            resp = _req.get(url, timeout=15)
            resp.raise_for_status()
            with open(fpath, 'wb') as f:
                f.write(resp.content)
    except Exception:
        pass


def _dc_sat_loop():
    """Post a fresh message (with JPG attached) to DC_SAT_WEBHOOK at :05/:15/.../:55."""
    _last_sent_min = -1
    while True:
        now = datetime.datetime.now()
        if now.minute % 10 == 5 and now.minute != _last_sent_min:
            _last_sent_min = now.minute
            try:
                sat_url, bst_str, utc_str = _sat_url_now()
                                              
                img_resp = _req.get(sat_url, timeout=15)
                img_resp.raise_for_status()
                img_bytes = img_resp.content
                fname = f"himawari_{bst_str.replace(':', '').replace(' ', '_')}.jpg"
                                                                          
                payload, _ = _dc_build_payload()
                payload["embeds"][0]["image"] = {"url": f"attachment://{fname}"}
                                                                   
                _req.post(
                    DC_SAT_WEBHOOK,
                    files={
                        "file": (fname, img_bytes, "image/jpeg"),
                        "payload_json": (None, json.dumps(payload), "application/json"),
                    },
                    timeout=30,
                )
            except Exception:
                pass
            _sat_download_and_save()
        time.sleep(30)


                                                                           

@app.route("/")
def index():
    html = HTML.replace("__CHART_JS_CDN__", CHART_JS_CDN).replace("__SAT_BASE_JS__", SAT_BASE).replace("__WEBHOOK_PREFIX__", WEBHOOK_PREFIX)
    return Response(html, mimetype="text/html")


@app.route("/api/data")
def api_data():
    now_ts = time.time()
    cutoff  = now_ts - 15 * 60                  
    with state.lock:
        bme  = dict(state.bme)
        esp  = dict(state.esp)
        ac   = dict(state.ac)
        kitchen = dict(state.kitchen)
        kitchen_ok = state.kitchen_ok
        wh_t = list(state.water_hist["times"])
        wh_v = list(state.water_hist["volts"])
        bh_t = list(state.bme_hist["times"])
        bh_T = list(state.bme_hist["temp"])
        bh_h = list(state.bme_hist["hum"])
        bh_p = list(state.bme_hist["pres"])
        bme_ok = state.bme["ok"] if state.bme else False
        esp_ok = state.esp_ok
        ac_ok  = state.ac_ok
                                                       
        bme_15 = None
        for entry in state.bme_hist15:
            if entry[0] >= cutoff:
                bme_15 = {"temp": entry[1], "hum": entry[2], "pres": entry[3],
                          "age": round(now_ts - entry[0])}
                break
                                                               
        esp_15 = None
        for entry in state.esp_hist15:
            if entry[0] >= cutoff:
                esp_15 = {"out_temp": entry[1], "out_hum": entry[2], "out_pres": entry[3],
                          "in_temp": entry[4], "water_v": entry[5],
                          "age": round(now_ts - entry[0])}
                break
    return jsonify({
        "bme": bme, "esp": esp, "ac": ac,
        "kitchen": kitchen, "kitchen_ok": kitchen_ok,
        "bme_ok": bme_ok, "esp_ok": esp_ok, "ac_ok": ac_ok,
        "bme_hist": {"times": bh_t, "temp": bh_T, "hum": bh_h, "pres": bh_p},
        "water_hist": {"times": wh_t, "volts": wh_v},
        "bme_15": bme_15,
        "esp_15": esp_15,
        "ts": datetime.datetime.now().strftime("%H:%M:%S"),
    })


@app.route("/api/vghs")
def api_vghs():
    return jsonify(_get_vghs_payload())


@app.route("/api/esp/<path:cmd>")
def esp_cmd(cmd):
    try:
        r = _req.get(f"{ESP32_BASE_URL}/{cmd}", timeout=4,
                     params=request.args.to_dict())
        r.raise_for_status()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@app.route("/api/ac/<path:cmd>")
def ac_cmd(cmd):
    args = request.args.to_dict()
    val  = args.get("value", "").upper()
    item = args.get("item",  "").lower()
    with state.lock:
        state.ac_ok = True
        if cmd == "power":    state.ac["power"] = val
        elif cmd == "temp":   state.ac["temp"]  = val if val not in ("UP","DOWN") else state.ac.get("temp", val)
        elif cmd == "mode":   state.ac["mode"]  = val
        elif cmd == "fan":    state.ac["fan"]   = val
        elif cmd == "swingv": state.ac["swingV"] = val
        elif cmd == "swingh": state.ac["swingH"] = val
        elif cmd == "toggle" and item:
            cur = state.ac.get(item, "OFF")
            state.ac[item] = "OFF" if cur == "ON" else "ON"
    def _send():
        try:
            _req.get(f"{AC_BASE_URL}/{cmd}", timeout=5, params=args)
        except Exception:
            pass
    threading.Thread(target=_send, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/discord_c/config", methods=["POST"])
def dc_config():
    data     = request.get_json(force=True)
    url      = data.get("url", "").strip()
    interval = max(10, int(data.get("interval", 60)))
    if url and (not _valid_webhook(url)):
        return jsonify({"ok": False, "error": "Invalid webhook URL."}), 400
    with state.lock:
        state.dc_url      = url
        state.dc_interval = interval
    return jsonify({"ok": True})


@app.route("/api/discord_c/start", methods=["POST"])
def dc_start():
    with state.lock:
        if state.dc_active:
            return jsonify({"ok": True, "status": "already running"})
        if not _valid_webhook(state.dc_url):
            return jsonify({"ok": False, "error": "Set a valid webhook URL first."}), 400
        state.dc_active = True
        state.dc_status = "Starting..."
    threading.Thread(target=_dc_loop, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/discord_c/stop", methods=["POST"])
def dc_stop():
    with state.lock:
        state.dc_active = False
        state.dc_status = "Stopped"
    return jsonify({"ok": True})


@app.route("/api/discord_c/send", methods=["POST"])
def dc_send_now():
    ok, msg = _dc_send_once()
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/discord_c/status")
def dc_status():
    with state.lock:
        return jsonify({
            "active":   state.dc_active,
            "status":   state.dc_status,
            "count":    state.dc_count,
            "last":     state.dc_last,
            "preview":  state.dc_preview,
            "url":      state.dc_url,
            "interval": state.dc_interval,
        })


@app.route("/api/discord/start", methods=["POST"])
def discord_start():
    with state.lock:
        if state.discord_active:
            return jsonify({"ok": True, "status": "already running"})
        state.discord_active = True
        state.discord_status = "Starting..."
    threading.Thread(target=_discord_loop, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/discord/stop", methods=["POST"])
def discord_stop():
    with state.lock:
        state.discord_active = False
        state.discord_status = "Stopped"
    return jsonify({"ok": True})


@app.route("/api/discord/send", methods=["POST"])
def discord_send_now():
    ok, msg = _discord_send_or_edit()
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/discord/status")
def discord_status():
    with state.lock:
        return jsonify({
            "active":      state.discord_active,
            "status":      state.discord_status,
            "count":       state.discord_count,
            "last":        state.discord_last,
            "preview":     state.discord_preview,
            "message_id":  state.discord_message_id,
        })


@app.route("/sse")
def sse():
    """Server-Sent Events – pushes a data update every 2 s."""
    def generate():
        while True:
            with state.lock:
                bme   = dict(state.bme)
                esp   = dict(state.esp)
                ac    = dict(state.ac)
                kitchen = dict(state.kitchen)
                esp_ok = state.esp_ok
                ac_ok  = state.ac_ok
                bme_ok = state.bme.get("ok", False)
                kitchen_ok = state.kitchen_ok
            payload = json.dumps({
                "bme": bme, "esp": esp, "ac": ac,
                "kitchen": kitchen, "kitchen_ok": kitchen_ok,
                "bme_ok": bme_ok, "esp_ok": esp_ok, "ac_ok": ac_ok,
                "ts": datetime.datetime.now().strftime("%H:%M:%S"),
            })
            yield f"data: {payload}\n\n"
            time.sleep(2)
    return Response(stream_with_context(generate()),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})

                                                                           

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Infi's Room Dashboard</title>
<script src="__CHART_JS_CDN__"></script>
<style>
:root {
  --bg:   #1e1e2e; --card: #2a2a3e; --drk: #181825; --mid: #313244;
  --acc:  #89b4fa; --red:  #f38ba8; --grn:  #a6e3a1; --org: #fab387;
  --prp:  #cba6f7; --txt:  #cdd6f4; --sub:  #6c7086;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--txt); font-family: "Segoe UI", system-ui, sans-serif; min-height: 100vh; }
a { color: inherit; text-decoration: none; }


header { background: var(--drk); padding: 12px 20px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; }
header h1 { font-size: 1.1rem; color: var(--acc); font-weight: 700; }
.pill { background: var(--mid); border-radius: 20px; padding: 3px 12px; font-size: .78rem; display: inline-flex; align-items: center; gap: 6px; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--sub); }
.dot.on  { background: var(--grn); }
.dot.off { background: var(--red); }
.hdr-pills { display: flex; gap: 8px; flex-wrap: wrap; }


.tabs { display: flex; background: var(--drk); padding: 0 16px; overflow-x: auto; }
.tab { padding: 10px 22px; cursor: pointer; font-weight: 600; font-size: .9rem; color: var(--sub); border-bottom: 3px solid transparent; white-space: nowrap; transition: color .2s, border-color .2s; }
.tab.active { color: var(--acc); border-color: var(--acc); }
.tab-content { display: none; padding: 16px; }
.tab-content.active { display: block; }


.grid { display: grid; gap: 14px; }
.g2 { grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); }
.g3 { grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }
.g4 { grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); }
.g5 { grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); }
.card { background: var(--card); border-radius: 16px; padding: 18px; }
.card-title { font-size: .75rem; font-weight: 700; color: var(--sub); text-transform: uppercase; letter-spacing: .06em; margin-bottom: 8px; }
.big-val { font-size: 3rem; font-weight: 700; font-family: "Consolas", monospace; line-height: 1; }
.unit { font-size: 1.05rem; font-weight: 600; color: var(--sub); margin-left: 4px; }
.stats-row { display: flex; gap: 12px; margin-top: 10px; border-top: 1px solid var(--mid); padding-top: 8px; }
.stat { flex: 1; text-align: center; }
.stat .lbl { font-size: .65rem; color: var(--sub); font-weight: 700; }
.stat .val { font-size: .85rem; font-family: "Consolas", monospace; font-weight: 700; }
.delta-row { display: flex; gap: 10px; margin-top: 8px; border-top: 1px solid var(--mid); padding-top: 8px; align-items: center; flex-wrap: wrap; }
.delta-label { font-size: .68rem; color: var(--sub); font-weight: 700; flex: 0 0 100%; margin-bottom: 2px; }
.delta-item { display: flex; flex-direction: column; align-items: center; flex: 1; }
.delta-item .d-lbl { font-size: .62rem; color: var(--sub); }
.delta-item .d-now { font-size: .82rem; font-weight: 700; font-family: Consolas, monospace; }
.delta-item .d-ago { font-size: .72rem; font-family: Consolas, monospace; color: var(--sub); }
.delta-item .d-chg { font-size: .78rem; font-weight: 700; font-family: Consolas, monospace; }
.up   { color: var(--red)  !important; }
.down { color: var(--acc)  !important; }
.same { color: var(--sub)  !important; }
.outdoor-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin-top: 10px; }
.outdoor-val-block { background: var(--drk); border-radius: 12px; padding: 12px 10px; text-align: center; }
.outdoor-val-block .ov-icon { font-size: 1.4rem; }
.outdoor-val-block .ov-label { font-size: .62rem; color: var(--sub); font-weight: 700; text-transform: uppercase; letter-spacing: .05em; margin: 2px 0; }
.outdoor-val-block .ov-value { font-size: 1.5rem; font-weight: 700; font-family: Consolas, monospace; line-height: 1.1; }
.outdoor-val-block .ov-unit  { font-size: .7rem; color: var(--sub); }
.outdoor-val-block .ov-ago   { font-size: .68rem; color: var(--sub); margin-top: 4px; }
.outdoor-val-block .ov-delta { font-size: .78rem; font-weight: 700; font-family: Consolas, monospace; }
.info-val { font-size: 1.5rem; font-weight: 700; font-family: "Consolas", monospace; margin-top: 6px; }
.section-title { font-size: 1rem; font-weight: 700; color: var(--acc); margin: 18px 0 10px; }
.divider { border: none; border-top: 1px solid var(--mid); margin: 12px 0; }
.chart-wrap { height: 180px; margin-top: 8px; }


.btn-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin-top: 4px; }
.btn { border: none; border-radius: 10px; padding: 9px 10px; font-size: .82rem; font-weight: 700; cursor: pointer; transition: opacity .15s, transform .1s; }
.btn:active { opacity: .8; transform: scale(.97); }
.btn-grn { background: var(--grn); color: #1e1e2e; }
.btn-red { background: var(--red); color: #1e1e2e; }
.btn-acc { background: var(--acc); color: #1e1e2e; }
.btn-org { background: var(--org); color: #1e1e2e; }
.btn-prp { background: var(--prp); color: #1e1e2e; }
.btn-mid { background: var(--mid); color: var(--txt); }
.btn-wide { grid-column: 1 / -1; }
.temp-grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 4px; margin-top: 4px; }
.temp-grid .btn { padding: 7px 2px; font-size: .78rem; }
.ac-status-bar { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
.badge { background: var(--mid); border-radius: 8px; padding: 5px 10px; font-size: .78rem; }
.badge span { font-weight: 700; }
.badge.pwr-on span { color: var(--grn); }
.badge.pwr-off span { color: var(--red); }


footer { text-align: center; padding: 14px; color: var(--sub); font-size: .75rem; background: var(--drk); margin-top: 20px; }
.ts { color: var(--txt); font-weight: 600; }

.vghs-metar { background: var(--drk); border-radius: 10px; padding: 12px 16px; margin-bottom: 14px; display: flex; gap: 12px; align-items: flex-start; flex-wrap: wrap; }
.vghs-metar .vm-lbl { font-size: .68rem; font-weight: 800; color: var(--sub); text-transform: uppercase; letter-spacing: .07em; white-space: nowrap; padding-top: 2px; }
.vghs-metar .vm-txt { font-family: Consolas, monospace; font-size: .85rem; color: var(--yel, #f9e2af); word-break: break-all; flex: 1; }
.wx-warn { background: #2e1a1a; border: 1px solid #5c2a2a; border-radius: 12px; padding: 12px 16px; margin-bottom: 8px; }
.wx-warn .ww-title { color: var(--red); font-weight: 800; font-size: .88rem; margin-bottom: 5px; }
.wx-warn .ww-meta  { color: var(--org); font-size: .72rem; margin-bottom: 7px; }
.wx-warn .ww-body  { color: var(--txt); font-size: .8rem; line-height: 1.5; }
.vghs-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(155px, 1fr)); gap: 10px; margin-bottom: 12px; }
.vcard { background: var(--card); border-radius: 14px; padding: 14px 14px 12px; position: relative; overflow: hidden; }
.vcard::before { content: ''; position: absolute; top: 0; left: 0; right: 0; height: 3px; border-radius: 14px 14px 0 0; }
.vcard.vc-org::before { background: var(--org); }
.vcard.vc-acc::before { background: var(--acc); }
.vcard.vc-prp::before { background: var(--prp); }
.vcard.vc-teal::before { background: #94e2d5; }
.vcard.vc-grn::before { background: var(--grn); }
.vcard.vc-red::before { background: var(--red); }
.vcard.vc-yel::before { background: #f9e2af; }
.vcard.vc-sky::before { background: #89dceb; }
.vcard-icon  { font-size: 1.6rem; line-height: 1; margin-bottom: 4px; }
.vcard-title { font-size: .65rem; font-weight: 800; color: var(--sub); text-transform: uppercase; letter-spacing: .07em; margin-bottom: 3px; }
.vcard-big   { font-size: 2.4rem; font-weight: 800; font-family: Consolas, monospace; line-height: 1.05; }
.vcard-unit  { font-size: .9rem; color: var(--sub); font-weight: 600; margin-left: 2px; }
.vcard-sub   { font-size: .78rem; color: var(--sub); margin-top: 5px; line-height: 1.45; }
.vcard-sub span { color: var(--txt); font-weight: 600; }
.vcard-info  { font-size: 1.25rem; font-weight: 800; font-family: Consolas, monospace; margin-top: 3px; line-height: 1.3; }
.vcard-tag   { display: inline-block; background: var(--mid); border-radius: 6px; padding: 2px 7px; font-size: .7rem; font-weight: 700; margin-top: 5px; }
.compass-wrap { margin-top: 10px; display: flex; flex-direction: column; align-items: center; gap: 3px; }
.compass-lbl  { font-size: .7rem; color: var(--sub); font-weight: 700; letter-spacing: .06em; }
.vghs-compare { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 14px; }
.vcmp-card { background: var(--drk); border-radius: 10px; padding: 12px 14px; }
.vcmp-title { font-size: .68rem; font-weight: 800; color: var(--sub); text-transform: uppercase; letter-spacing: .06em; margin-bottom: 7px; }
.vcmp-row { display: flex; justify-content: space-between; padding: 3px 0; border-bottom: 1px solid var(--mid); }
.vcmp-row:last-child { border-bottom: none; }
.vcmp-lbl { font-size: .73rem; color: var(--sub); }
.vcmp-val { font-size: .8rem; font-weight: 700; font-family: Consolas, monospace; color: var(--txt); }
</style>
</head>
<body>

<header>
  <h1> Infi's Room Dashboard</h1>
  <div class="hdr-pills">
    <div class="pill"><div class="dot" id="dot-bme"></div><span id="lbl-bme">BME280</span></div>
    <div class="pill"><div class="dot" id="dot-esp"></div><span id="lbl-esp">Garden ESP32</span></div>
    <div class="pill"><div class="dot" id="dot-ac"></div><span id="lbl-ac">Haier AC</span></div>
    <div class="pill"><div class="dot" id="dot-kitchen"></div><span id="lbl-kitchen">Kitchen</span></div>
    <div class="pill"> <span id="ts" class="ts">--:--:--</span></div>
  </div>
</header>

<div class="tabs">
  <div class="tab active" onclick="showTab('summary');loadSummary()">️ Summary</div>
  <div class="tab" onclick="showTab('live')"> Live</div>
  <div class="tab" onclick="showTab('garden')"> Garden</div>
  <div class="tab" onclick="showTab('ac')">️ Haier AC</div>
  <div class="tab" onclick="showTab('aviation');loadVghs()">️ Aviation</div>
  <div class="tab" onclick="showTab('satellite');loadSatellite()">️ Satellite</div>
  <div class="tab" onclick="showTab('discord');loadDiscordStatus()"> Discord</div>
  <div class="tab" onclick="showTab('discord_c');loadDcStatus()"> Discord C</div>
</div>


<div id="tab-summary" class="tab-content active">
  <div class="section-title" style="margin-top:0">️ Summary
    <span style="font-size:.78rem;color:var(--sub);font-weight:400">All sensors &middot; AC &middot; Aviation &middot; Satellite</span>
    &nbsp;<button class="btn btn-mid" onclick="loadSummary()" style="padding:4px 14px;font-size:.78rem"> Refresh</button>
    &nbsp;<span class="pill" id="sm-ts">~</span>
  </div>

  
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px;align-items:start">

    
    <div class="card" style="height:100%">
      <div class="card-title">️ Himawari-9 ~ Latest</div>
      <div style="text-align:center;margin-top:8px">
        <div style="color:var(--sub);font-size:.78rem;margin-bottom:6px" id="sm-sat-lbl">Loading~</div>
        <img id="sm-sat-img" src="" alt="Latest satellite"
          style="max-width:100%;border-radius:8px;border:2px solid var(--brd)"
          onerror="this.style.opacity=\'0.2\'">
      </div>
    </div>

    
    <div style="display:flex;flex-direction:column;gap:12px">

      <div class="card">
        <div class="card-title"> Infi\'s Room &nbsp;<span style="font-weight:400;color:var(--sub);font-size:.8rem">BME280 on Pi</span></div>
        <div class="grid g3" style="gap:8px;margin-top:8px">
          <div>
            <div class="lbl">️ Temp</div>
            <div class="big-val" id="sm-r-temp" style="color:var(--grn);font-size:1.6rem">~</div>
            <div style="font-size:.72rem;color:var(--sub);margin-top:2px" id="sm-r-temp-rf"></div>
            <div style="font-size:.7rem;color:var(--sub)" id="sm-r-temp-d">~</div>
          </div>
          <div>
            <div class="lbl"> Humidity</div>
            <div class="big-val" id="sm-r-hum" style="color:var(--acc);font-size:1.6rem">~</div>
            <div style="font-size:.7rem;color:var(--sub)" id="sm-r-hum-d">~</div>
          </div>
          <div>
            <div class="lbl"> Pressure</div>
            <div class="big-val" id="sm-r-pres" style="color:var(--org);font-size:1.3rem">~</div>
            <div style="font-size:.7rem;color:var(--sub)" id="sm-r-pres-d">~</div>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-title">️ Outside &nbsp;<span style="font-weight:400;color:var(--sub);font-size:.8rem">ESP32 BME280</span></div>
        <div class="grid g3" style="gap:8px;margin-top:8px">
          <div>
            <div class="lbl">️ Temp</div>
            <div class="big-val" id="sm-o-temp" style="color:var(--org);font-size:1.6rem">~</div>
            <div style="font-size:.72rem;color:var(--sub);margin-top:2px" id="sm-o-temp-rf"></div>
            <div style="font-size:.7rem;color:var(--sub)" id="sm-o-temp-d">~</div>
          </div>
          <div>
            <div class="lbl"> Humidity</div>
            <div class="big-val" id="sm-o-hum" style="color:var(--acc);font-size:1.6rem">~</div>
            <div style="font-size:.7rem;color:var(--sub)" id="sm-o-hum-d">~</div>
          </div>
          <div>
            <div class="lbl"> Pressure</div>
            <div class="big-val" id="sm-o-pres" style="color:var(--prp);font-size:1.3rem">~</div>
            <div style="font-size:.7rem;color:var(--sub)" id="sm-o-pres-d">~</div>
          </div>
        </div>
        <div style="margin-top:10px;display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px">
          <div>
            <div class="lbl">️ Dining (DS18B20)</div>
            <div class="big-val" id="sm-ds-temp" style="color:var(--grn);font-size:1.4rem">~</div>
          </div>
          <div>
            <div class="lbl">️ Rain Sensor</div>
            <div class="big-val" id="sm-rain" style="color:var(--acc);font-size:1rem;margin-top:2px">~</div>
            <div class="big-val" id="sm-rain-v" style="color:var(--txt);font-size:1.3rem;margin-top:2px">~</div>
            <div style="font-size:.7rem;color:var(--sub)" id="sm-rain-raw">~</div>
          </div>
          <div>
            <div class="lbl"> Kitchen</div>
            <div class="big-val" id="sm-k-temp" style="color:var(--org);font-size:1.4rem">~</div>
            <div style="font-size:.72rem;color:var(--sub);margin-top:2px" id="sm-k-temp-rf"></div>
            <div class="big-val" id="sm-k-hum" style="color:var(--acc);font-size:1.1rem;margin-top:2px">~</div>
          </div>
        </div>
      </div>

    </div>
  </div>

  
  <div class="card" style="margin-bottom:12px">
    <div class="card-title">️ VGHS ~ Dhaka Airport
      &nbsp;<span class="pill" style="display:inline-flex"><span class="dot" id="sm-vg-dot"></span>&nbsp;<span id="sm-vg-status">~</span></span>
      &nbsp;<span class="pill" id="sm-vg-time">~</span>
    </div>
    <div class="grid g3" style="gap:10px;margin-top:10px">
      <div>
        <div class="lbl">️ Temp</div>
        <div class="big-val" id="sm-vg-temp" style="color:var(--org);font-size:1.8rem">~</div>
        <div style="font-size:.72rem;color:var(--sub);margin-top:2px" id="sm-vg-temp-rf"></div>
        <div style="font-size:.82rem;color:var(--sub);margin-top:3px" id="sm-vg-wx-inline">~</div>
      </div>
      <div>
        <div class="lbl"> Humidity</div>
        <div class="big-val" id="sm-vg-hum" style="color:var(--acc);font-size:1.8rem">~</div>
        <div style="font-size:.75rem;color:var(--sub);margin-top:3px" id="sm-vg-dew">~</div>
      </div>
      <div>
        <div class="lbl"> QNH</div>
        <div class="big-val" id="sm-vg-qnh" style="color:var(--prp);font-size:1.5rem">~</div>
      </div>
      <div>
        <div class="lbl"> Wind</div>
        <div class="info-val" id="sm-vg-wind" style="font-size:1rem">~</div>
        <div style="font-size:.78rem;color:var(--sub);margin-top:2px" id="sm-vg-wind2">~</div>
      </div>
      <div>
        <div class="lbl">️ Visibility</div>
        <div class="info-val" id="sm-vg-vis" style="font-size:1rem">~</div>
      </div>
      <div>
        <div class="lbl">️ Cloud</div>
        <div class="info-val" id="sm-vg-cloud" style="font-size:1rem">~</div>
      </div>
    </div>
    <div id="sm-vg-warns" style="margin-top:8px"></div>
  </div>

  
  <div class="card" style="margin-bottom:12px">
    <div class="card-title">️ Haier AC Status</div>
    <div class="grid g5" style="gap:10px;margin-top:8px">
      <div><div class="lbl"> Power</div><div class="info-val" id="sm-ac-pwr" style="color:var(--grn)">~</div></div>
      <div><div class="lbl">️ Set Temp</div><div class="info-val" id="sm-ac-temp" style="color:var(--org)">~</div></div>
      <div><div class="lbl"> Mode</div><div class="info-val" id="sm-ac-mode" style="color:var(--acc)">~</div></div>
      <div><div class="lbl"> Fan</div><div class="info-val" id="sm-ac-fan" style="color:var(--prp)">~</div></div>
      <div><div class="lbl">↕↔ Swing</div><div class="info-val" id="sm-ac-swing" style="color:var(--txt);font-size:1rem">~</div></div>
    </div>
    <div id="sm-ac-extras" style="margin-top:6px;font-size:.85rem;color:var(--red)"></div>
  </div>
</div>


<div id="tab-live" class="tab-content">

  
  <div class="section-title"> Infi's Room &nbsp;<span style="font-size:.78rem;color:var(--sub);font-weight:400">BME280 on Pi</span></div>
  <div class="grid g3">

    <div class="card">
      <div class="card-title">️ Temperature</div>
      <div><span class="big-val" id="v-temp" style="color:var(--grn)">--.-</span><span class="unit">°C</span></div>
      <div style="font-size:.73rem;color:var(--sub);margin-top:3px" id="v-temp-rf"></div>
      <div class="stats-row">
        <div class="stat"><div class="lbl"> MIN</div><div class="val" id="mn-temp" style="color:var(--acc)">~</div></div>
        <div class="stat"><div class="lbl"> MAX</div><div class="val" id="mx-temp" style="color:var(--red)">~</div></div>
        <div class="stat"><div class="lbl">〰️ AVG</div><div class="val" id="av-temp" style="color:var(--grn)">~</div></div>
      </div>
      <div class="delta-row">
        <div class="delta-label"> vs 15 min ago</div>
        <div class="delta-item"><div class="d-lbl">NOW</div><div class="d-now" id="d-temp-now" style="color:var(--grn)">~</div></div>
        <div class="delta-item"><div class="d-lbl">15m AGO</div><div class="d-ago" id="d-temp-ago">~</div></div>
        <div class="delta-item"><div class="d-lbl">CHANGE</div><div class="d-chg" id="d-temp-chg">~</div></div>
      </div>
      <div class="chart-wrap"><canvas id="chart-temp"></canvas></div>
    </div>

    <div class="card">
      <div class="card-title"> Humidity</div>
      <div><span class="big-val" id="v-hum" style="color:var(--acc)">--.-</span><span class="unit">%</span></div>
      <div class="stats-row">
        <div class="stat"><div class="lbl"> MIN</div><div class="val" id="mn-hum" style="color:var(--acc)">~</div></div>
        <div class="stat"><div class="lbl"> MAX</div><div class="val" id="mx-hum" style="color:var(--red)">~</div></div>
        <div class="stat"><div class="lbl">〰️ AVG</div><div class="val" id="av-hum" style="color:var(--acc)">~</div></div>
      </div>
      <div class="delta-row">
        <div class="delta-label"> vs 15 min ago</div>
        <div class="delta-item"><div class="d-lbl">NOW</div><div class="d-now" id="d-hum-now" style="color:var(--acc)">~</div></div>
        <div class="delta-item"><div class="d-lbl">15m AGO</div><div class="d-ago" id="d-hum-ago">~</div></div>
        <div class="delta-item"><div class="d-lbl">CHANGE</div><div class="d-chg" id="d-hum-chg">~</div></div>
      </div>
      <div class="chart-wrap"><canvas id="chart-hum"></canvas></div>
    </div>

    <div class="card">
      <div class="card-title"> Pressure</div>
      <div><span class="big-val" id="v-pres" style="color:var(--org)">---.-</span><span class="unit">hPa</span></div>
      <div class="stats-row">
        <div class="stat"><div class="lbl"> MIN</div><div class="val" id="mn-pres" style="color:var(--acc)">~</div></div>
        <div class="stat"><div class="lbl"> MAX</div><div class="val" id="mx-pres" style="color:var(--red)">~</div></div>
        <div class="stat"><div class="lbl">〰️ AVG</div><div class="val" id="av-pres" style="color:var(--org)">~</div></div>
      </div>
      <div class="delta-row">
        <div class="delta-label"> vs 15 min ago</div>
        <div class="delta-item"><div class="d-lbl">NOW</div><div class="d-now" id="d-pres-now" style="color:var(--org)">~</div></div>
        <div class="delta-item"><div class="d-lbl">15m AGO</div><div class="d-ago" id="d-pres-ago">~</div></div>
        <div class="delta-item"><div class="d-lbl">CHANGE</div><div class="d-chg" id="d-pres-chg">~</div></div>
      </div>
      <div class="chart-wrap"><canvas id="chart-pres"></canvas></div>
    </div>

  </div>

  
  <div class="section-title"> Garden / Outdoor Sensors &nbsp;<span style="font-size:.78rem;color:var(--sub);font-weight:400">ESP32 192.168.0.213</span></div>
  <div class="grid" style="grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap:14px">

    <div class="card">
      <div class="card-title">️ Outside (Shade) ~ BME280</div>
      <div class="outdoor-grid">
        <div class="outdoor-val-block">
          <div class="ov-icon">️</div>
          <div class="ov-label">Temperature</div>
          <div class="ov-value" id="lv-g-bme-temp" style="color:var(--org)">~</div>
          <div class="ov-unit">°C</div>
          <div style="font-size:.7rem;color:var(--sub);margin-top:2px" id="lv-g-bme-temp-rf"></div>
          <div class="ov-ago">15m ago: <span id="d-out-temp-ago">~</span></div>
          <div class="ov-delta" id="d-out-temp-chg">~</div>
        </div>
        <div class="outdoor-val-block">
          <div class="ov-icon"></div>
          <div class="ov-label">Humidity</div>
          <div class="ov-value" id="lv-g-bme-hum" style="color:var(--acc)">~</div>
          <div class="ov-unit">%</div>
          <div class="ov-ago">15m ago: <span id="d-out-hum-ago">~</span></div>
          <div class="ov-delta" id="d-out-hum-chg">~</div>
        </div>
        <div class="outdoor-val-block">
          <div class="ov-icon"></div>
          <div class="ov-label">Pressure</div>
          <div class="ov-value" id="lv-g-bme-pres" style="color:var(--prp)">~</div>
          <div class="ov-unit">hPa</div>
          <div class="ov-ago">15m ago: <span id="d-out-pres-ago">~</span></div>
          <div class="ov-delta" id="d-out-pres-chg">~</div>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-title">�️ DS18B20 &amp; ️ Rain Sensor</div>
      <div class="outdoor-grid">
        <div class="outdoor-val-block">
          <div class="ov-icon">️</div>
          <div class="ov-label">DS18B20 Temp</div>
          <div class="ov-value" id="lv-g-ds-temp" style="color:var(--grn)">~</div>
          <div class="ov-unit">°C</div>
          <div class="ov-ago">15m ago: <span id="d-in-temp-ago">~</span></div>
          <div class="ov-delta" id="d-in-temp-chg">~</div>
        </div>
        <div class="outdoor-val-block">
          <div class="ov-icon">️</div>
          <div class="ov-label">Rain Voltage</div>
          <div class="ov-value" id="v-rain" style="color:var(--acc)">~</div>
          <div class="ov-unit">Volts</div>
          <div class="ov-ago">Raw ADC: <span id="v-raw">~</span></div>
          <div class="ov-delta" id="v-rain-status" style="font-size:.7rem;margin-top:4px">~</div>
        </div>
        <div class="outdoor-val-block">
          <div class="ov-icon"></div>
          <div class="ov-label">Light</div>
          <div class="ov-value" id="v-light" style="color:var(--grn);font-size:1.1rem">~</div>
          <div class="ov-unit">Status</div>
          <div class="ov-ago">Mode: <span id="v-mode">~</span></div>
        </div>
      </div>
      <div class="chart-wrap" style="height:140px;margin-top:12px"><canvas id="chart-water"></canvas></div>
    </div>

  </div>

  <div class="section-title"> Kitchen Sensor &nbsp;<span style="font-size:.78rem;color:var(--sub);font-weight:400">ESP32 192.168.0.76</span></div>
  <div class="grid g2" style="margin-bottom:14px">
    <div class="card">
      <div class="card-title">️ Temperature</div>
      <div><span class="big-val" id="v-k-temp" style="color:var(--org)">~</span><span class="unit">°C</span></div>
      <div style="font-size:.73rem;color:var(--sub);margin-top:3px" id="v-k-temp-rf"></div>
    </div>
    <div class="card">
      <div class="card-title"> Humidity</div>
      <div><span class="big-val" id="v-k-hum" style="color:var(--acc)">~</span><span class="unit">%</span></div>
    </div>
  </div>

  <div class="section-title">️ Haier AC &nbsp;<span style="font-size:.8rem;color:var(--sub);font-weight:400">quick view</span></div>
  <div class="grid g5">
    <div class="card">
      <div class="card-title"> Power</div>
      <div class="info-val" id="v-ac-power" style="color:var(--grn)">~</div>
    </div>
    <div class="card">
      <div class="card-title">️ Set Temp</div>
      <div class="info-val" id="v-ac-temp" style="color:var(--org)">~</div>
    </div>
    <div class="card">
      <div class="card-title"> Mode</div>
      <div class="info-val" id="v-ac-mode" style="color:var(--acc)">~</div>
    </div>
    <div class="card">
      <div class="card-title"> Fan</div>
      <div class="info-val" id="v-ac-fan" style="color:var(--prp)">~</div>
    </div>
    <div class="card">
      <div class="card-title">↕↔ Swing</div>
      <div class="info-val" id="v-ac-swing" style="color:var(--txt);font-size:1.1rem">~</div>
    </div>
  </div>
</div>

<div id="tab-garden" class="tab-content">

  <div class="section-title"> Garden ESP32  <span style="color:var(--sub);font-size:.8rem;font-weight:400">192.168.0.213</span></div>

  <div class="grid g4" style="margin-bottom:16px">
    <div class="card">
      <div class="card-title">Light</div>
      <div class="info-val" id="g-light" style="color:var(--grn)">~</div>
    </div>
    <div class="card">
      <div class="card-title">Mode</div>
      <div class="info-val" id="g-mode" style="color:var(--acc)">~</div>
    </div>
    <div class="card">
      <div class="card-title">Schedule</div>
      <div class="info-val" id="g-schedule" style="color:var(--org);font-size:1rem">~</div>
    </div>
    <div class="card">
      <div class="card-title">ESP32 Time</div>
      <div class="info-val" id="g-time" style="color:var(--sub)">~</div>
    </div>
  </div>

  <div class="grid g2">
    <div class="card">
      <div class="section-title" style="margin-top:0">Relay Control</div>
      <div class="btn-grid">
        <button class="btn btn-grn" onclick="espCmd('on')"> TURN ON</button>
        <button class="btn btn-red" onclick="espCmd('off')"> TURN OFF</button>
        <button class="btn btn-acc" onclick="espCmd('auto')"> AUTO MODE</button>
        <button class="btn btn-prp" onclick="espCmd('manual')"> MANUAL</button>
      </div>
    </div>
    <div class="card">
      <div class="section-title" style="margin-top:0">️ Rain Sensor</div>
      <div class="grid g2" style="gap:8px">
        <div>
          <div class="card-title">Rain Voltage</div>
          <div class="info-val" id="g-rain-v" style="color:var(--acc)">~</div>
        </div>
        <div>
          <div class="card-title">Raw ADC</div>
          <div class="info-val" id="g-rain-r" style="color:var(--txt)">~</div>
        </div>
      </div>
      <div id="g-rain-status" style="margin-top:8px;font-size:.9rem;font-weight:700;text-align:center">~</div>
      <div class="chart-wrap"><canvas id="chart-water"></canvas></div>
    </div>
  </div>

  <div class="card" style="margin-top:12px">
    <div class="section-title" style="margin-top:0"> Garden Environment Sensors</div>
    <div class="grid g4" style="gap:8px">
      <div>
        <div class="card-title">️ BME Temp</div>
        <div class="info-val" id="g-bme-temp" style="color:var(--org)">~</div>
      </div>
      <div>
        <div class="card-title"> BME Humidity</div>
        <div class="info-val" id="g-bme-hum" style="color:var(--acc)">~</div>
      </div>
      <div>
        <div class="card-title"> Pressure</div>
        <div class="info-val" id="g-bme-pres" style="color:var(--prp)">~</div>
      </div>
      <div>
        <div class="card-title">️ DS18B20 Temp</div>
        <div class="info-val" id="g-ds-temp" style="color:var(--grn)">~</div>
      </div>
    </div>
  </div>

  <div class="card" style="margin-top:12px">
    <div class="section-title" style="margin-top:0">⏰ Schedule Timer</div>
    <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
      <label style="color:var(--sub);font-size:.85rem">ON time (HH:MM)</label>
      <input id="on-time" type="text" value="06:00" style="background:var(--mid);color:var(--txt);border:none;border-radius:8px;padding:7px 12px;font-size:.9rem;font-family:Consolas,monospace;width:90px">
      <label style="color:var(--sub);font-size:.85rem">OFF time (HH:MM)</label>
      <input id="off-time" type="text" value="18:00" style="background:var(--mid);color:var(--txt);border:none;border-radius:8px;padding:7px 12px;font-size:.9rem;font-family:Consolas,monospace;width:90px">
      <button class="btn btn-org" onclick="saveSchedule()" style="padding:8px 22px"> Save Schedule</button>
    </div>
  </div>
</div>


<div id="tab-ac" class="tab-content">

  <div class="section-title" style="margin-top:0">️ Haier AC  <span style="color:var(--sub);font-size:.8rem;font-weight:400">192.168.0.230  ·  ESP32 IR</span></div>

  <div class="ac-status-bar" id="ac-status-bar">
    <div class="badge pwr-off"><span id="ac-s-power">~</span></div>
    <div class="badge"><span id="ac-s-temp" style="color:var(--org)">~</span></div>
    <div class="badge"><span id="ac-s-mode" style="color:var(--acc)">~</span></div>
    <div class="badge"><span id="ac-s-fan"  style="color:var(--prp)">~</span></div>
    <div class="badge"><span id="ac-s-sv" style="color:var(--txt)">~</span></div>
    <div class="badge"><span id="ac-s-sh" style="color:var(--txt)">~</span></div>
    <div class="badge"><span id="ac-s-ex" style="color:var(--red)">~</span></div>
  </div>

  <div class="grid g4">

    
    <div class="card">
      <div class="section-title" style="margin-top:0"> Power</div>
      <div class="btn-grid">
        <button class="btn btn-grn" onclick="acCmd('power','on')">ON</button>
        <button class="btn btn-red" onclick="acCmd('power','off')">OFF</button>
      </div>
      <hr class="divider">
      <div class="section-title">️ Temperature</div>
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
        <button class="btn btn-mid" onclick="acCmd('temp','down')" style="width:40px;font-size:1.2rem">▼</button>
        <span id="ac-big-temp" style="font-size:2.2rem;font-weight:700;font-family:Consolas,monospace;color:var(--org);min-width:70px;text-align:center">~</span>
        <button class="btn btn-mid" onclick="acCmd('temp','up')" style="width:40px;font-size:1.2rem">▲</button>
      </div>
      <div class="temp-grid">
        <button class="btn btn-mid" onclick="acCmd('temp','16')">16</button>
        <button class="btn btn-mid" onclick="acCmd('temp','17')">17</button>
        <button class="btn btn-mid" onclick="acCmd('temp','18')">18</button>
        <button class="btn btn-mid" onclick="acCmd('temp','19')">19</button>
        <button class="btn btn-mid" onclick="acCmd('temp','20')">20</button>
        <button class="btn btn-mid" onclick="acCmd('temp','21')">21</button>
        <button class="btn btn-mid" onclick="acCmd('temp','22')">22</button>
        <button class="btn btn-mid" onclick="acCmd('temp','23')">23</button>
        <button class="btn btn-mid" onclick="acCmd('temp','24')">24</button>
        <button class="btn btn-mid" onclick="acCmd('temp','25')">25</button>
        <button class="btn btn-mid" onclick="acCmd('temp','26')">26</button>
        <button class="btn btn-mid" onclick="acCmd('temp','27')">27</button>
        <button class="btn btn-mid" onclick="acCmd('temp','28')">28</button>
        <button class="btn btn-mid" onclick="acCmd('temp','29')">29</button>
        <button class="btn btn-mid" onclick="acCmd('temp','30')">30</button>
      </div>
    </div>

    
    <div class="card">
      <div class="section-title" style="margin-top:0"> Mode</div>
      <div class="btn-grid">
        <button class="btn btn-acc" onclick="acCmd('mode','auto')">Auto</button>
        <button class="btn btn-acc" onclick="acCmd('mode','cool')">Cool</button>
        <button class="btn btn-red" onclick="acCmd('mode','heat')">Heat</button>
        <button class="btn btn-prp" onclick="acCmd('mode','dry')">Dry</button>
        <button class="btn btn-mid btn-wide" onclick="acCmd('mode','fan')">Fan Only</button>
      </div>
      <hr class="divider">
      <div class="section-title"> Fan Speed</div>
      <div class="btn-grid">
        <button class="btn btn-acc" onclick="acCmd('fan','auto')">Auto</button>
        <button class="btn btn-grn" onclick="acCmd('fan','low')">Low</button>
        <button class="btn btn-org" onclick="acCmd('fan','med')">Medium</button>
        <button class="btn btn-red" onclick="acCmd('fan','high')">High</button>
      </div>
    </div>

    
    <div class="card">
      <div class="section-title" style="margin-top:0">↕ Vertical Swing</div>
      <div class="btn-grid">
        <button class="btn btn-acc" onclick="acCmd('swingv','auto')">Auto</button>
        <button class="btn btn-mid" onclick="acCmd('swingv','top')">Top</button>
        <button class="btn btn-mid" onclick="acCmd('swingv','middle')">Middle</button>
        <button class="btn btn-mid" onclick="acCmd('swingv','down')">Down</button>
        <button class="btn btn-mid btn-wide" onclick="acCmd('swingv','off')">Off</button>
      </div>
      <hr class="divider">
      <div class="section-title">↔ Horizontal Swing</div>
      <div class="btn-grid">
        <button class="btn btn-acc" onclick="acCmd('swingh','auto')">Auto</button>
        <button class="btn btn-mid" onclick="acCmd('swingh','left')">Left</button>
        <button class="btn btn-mid" onclick="acCmd('swingh','middle')">Middle</button>
        <button class="btn btn-mid" onclick="acCmd('swingh','right')">Right</button>
      </div>
    </div>

    
    <div class="card">
      <div class="section-title" style="margin-top:0">⭐ Presets</div>
      <div class="btn-grid">
        <button class="btn btn-acc" onclick="acPreset('dir24')">24° ↑Top →R</button>
        <button class="btn btn-acc" onclick="acPreset('dir25')">25° ↑Top →R</button>
        <button class="btn btn-acc" onclick="acPreset('dir26')">26° ↑Top →R</button>
        <button class="btn btn-prp" onclick="acPreset('sleep25')">Sleep 25°C</button>
        <button class="btn btn-red" onclick="acPreset('turbo24')">Turbo 24°C</button>
        <button class="btn btn-org btn-wide" onclick="acPreset('dry25')">Dry 25°C</button>
      </div>
      <hr class="divider">
      <div class="section-title"> Toggle Extras</div>
      <div class="btn-grid">
        <button class="btn btn-red"  onclick="acToggle('turbo')">Turbo</button>
        <button class="btn btn-grn"  onclick="acToggle('quiet')">Quiet</button>
        <button class="btn btn-prp"  onclick="acToggle('sleep')">Sleep</button>
        <button class="btn btn-acc"  onclick="acToggle('health')">Health</button>
        <button class="btn btn-org btn-wide" onclick="acToggle('lock')">Lock</button>
      </div>
    </div>

  </div>
</div>


<div id="tab-aviation" class="tab-content">

  <div class="section-title" style="margin-top:0">
    ️ VGHS ~ Dhaka Aviation Weather
    <span style="font-size:.78rem;color:var(--sub);font-weight:400">Hazrat Shahjalal International Airport &middot; BMD METAR/SPECI</span>
    &nbsp;<div class="pill" style="display:inline-flex"><div class="dot" id="dot-vghs"></div><span id="lbl-vghs">Loading…</span></div>
    &nbsp;<span class="pill" id="vghs-ts">last fetch: ~</span>
  </div>

  
  <div id="vghs-warn-area"></div>

  
  <div class="vghs-metar" id="vghs-metar1" style="display:none">
    <div class="vm-lbl">LATEST</div><div class="vm-txt" id="vghs-raw1">~</div>
  </div>
  <div class="vghs-metar" id="vghs-metar2" style="display:none;margin-top:-8px">
    <div class="vm-lbl">PREV</div><div class="vm-txt" id="vghs-raw2">~</div>
  </div>

  <div id="vghs-page">
    <div style="text-align:center;padding:40px 20px;color:var(--sub)"> Fetching aviation data…</div>
  </div>

</div>


<div id="tab-aviation" class="tab-content">

  <div class="section-title" style="margin-top:0">
    ️ VGHS ~ Dhaka Aviation Weather
    <span style="font-size:.78rem;color:var(--sub);font-weight:400">Hazrat Shahjalal International Airport &middot; BMD METAR/SPECI</span>
    &nbsp;<span class="pill" style="display:inline-flex"><span class="dot" id="dot-vghs"></span>&nbsp;<span id="lbl-vghs">Loading…</span></span>
    &nbsp;<span class="pill" id="vghs-ts-pill">last fetch: ~</span>
    &nbsp;<button class="btn btn-mid" onclick="loadVghs()" style="padding:4px 14px;font-size:.78rem"> Refresh</button>
  </div>

  <div id="vghs-warn-area"></div>

  <div class="vghs-metar" id="vghs-metar1" style="display:none">
    <div class="vm-lbl">LATEST</div><div class="vm-txt" id="vghs-raw1">~</div>
  </div>
  <div class="vghs-metar" id="vghs-metar2" style="display:none;margin-top:-8px">
    <div class="vm-lbl">PREV</div><div class="vm-txt" id="vghs-raw2">~</div>
  </div>

  <div id="vghs-page">
    <div style="text-align:center;padding:40px 20px;color:var(--sub)"> Click the Aviation tab to load data…</div>
  </div>

</div>



<div id="tab-satellite" class="tab-content">

  <div class="section-title" style="margin-top:0">
    ️ Himawari-9 Satellite ~ Dhaka / Asia Region
    <span style="font-size:.78rem;color:var(--sub);font-weight:400">JMA ha3 · Visible+IR · Updates every 10 min · BST = UTC+6</span>
    &nbsp;<button class="btn btn-mid" onclick="loadSatellite()" style="padding:4px 14px;font-size:.78rem"> Refresh</button>
  </div>

  
  <div id="sat-latest-wrap" style="text-align:center;margin-bottom:24px">
    <div style="color:var(--sub);font-size:.85rem;margin-bottom:8px" id="sat-latest-lbl">Loading latest image…</div>
    <img id="sat-latest-img" src="" alt="Latest satellite image"
      style="max-width:100%;width:640px;border-radius:10px;border:2px solid var(--brd);display:none"
      onerror="this.style.display='none';document.getElementById('sat-latest-err').style.display=''">
    <div id="sat-latest-err" style="display:none;color:var(--red);padding:20px">️ Latest image not available</div>
  </div>

  
  <div class="section-title" style="font-size:.95rem"> Last 24 Hours</div>
  <div id="sat-grid" style="display:flex;flex-wrap:wrap;gap:8px;padding:4px 0">
    <div style="color:var(--sub);padding:20px">Loading 24-hour archive…</div>
  </div>

</div>


<div id="tab-discord" class="tab-content">

  <div class="section-title" style="margin-top:0"> Live Dashboard &middot; Discord</div>

  <div class="grid g2" style="margin-bottom:12px">
    <div class="card">
      <div class="card-title">Webhook</div>
      <div style="background:var(--drk);border-radius:8px;padding:9px 12px;font-size:.72rem;font-family:Consolas,monospace;color:var(--sub);margin-top:6px;word-break:break-all"> Hardcoded &middot; Live Dashboard channel</div>
      <hr class="divider">
      <div class="card-title">Update Interval</div>
      <div style="background:var(--drk);border-radius:8px;padding:10px 12px;font-size:.9rem;color:var(--acc);text-align:center;font-weight:700;margin-top:6px">⏱ Edits message every 1 minute</div>
      <hr class="divider">
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn btn-grn" id="dc-toggle" onclick="dcToggle()" style="flex:1">▶ Start</button>
        <button class="btn btn-org" onclick="dcSendNow()" style="flex:1">️ Edit Now</button>
      </div>
    </div>

    <div class="card">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
        <div class="card-title" style="margin-bottom:0">Status</div>
        <div class="pill"><div class="dot" id="dc-dot"></div><span id="dc-status-lbl">Idle</span></div>
      </div>
      <div class="grid g2" style="gap:8px;margin-bottom:12px">
        <div class="card" style="padding:10px">
          <div class="card-title">Edits Done</div>
          <div class="info-val" id="dc-count" style="color:var(--grn)">0</div>
        </div>
        <div class="card" style="padding:10px">
          <div class="card-title">Last Edit</div>
          <div class="info-val" id="dc-last" style="color:var(--acc);font-size:1.1rem">~</div>
        </div>
      </div>
      <div class="card-title">Message ID</div>
      <div id="dc-msg-id" style="background:var(--drk);border-radius:8px;padding:8px 12px;font-size:.75rem;font-family:Consolas,monospace;color:var(--sub);margin-top:6px;word-break:break-all">not sent yet</div>
      <div class="card-title" style="margin-top:12px">Last Preview</div>
      <pre id="dc-preview" style="background:var(--drk);border-radius:8px;padding:12px;font-size:.78rem;color:var(--txt);font-family:Consolas,monospace;white-space:pre-wrap;min-height:80px;margin-top:6px;overflow:auto">~</pre>
    </div>
  </div>

</div>


<div id="tab-discord_c" class="tab-content">

  <div class="section-title" style="margin-top:0"> Discord C &middot; Customizable Stats Webhook</div>

  <div class="grid g2" style="margin-bottom:12px">
    <div class="card">
      <div class="card-title">Webhook URL</div>
      <input id="dcc-url" type="text" placeholder="webhook"
        style="width:100%;background:var(--drk);color:var(--txt);border:1px solid var(--mid);border-radius:8px;padding:9px 12px;font-size:.85rem;font-family:Consolas,monospace;margin-top:6px;outline:none">
      <hr class="divider">
      <div class="card-title" style="margin-bottom:8px">Send Interval</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap" id="dcc-interval-btns">
        <button class="btn btn-mid" onclick="dccSetInterval(30)"  data-v="30" >30 s</button>
        <button class="btn btn-mid" onclick="dccSetInterval(60)"  data-v="60" >1 min</button>
        <button class="btn btn-mid" onclick="dccSetInterval(120)" data-v="120">2 min</button>
        <button class="btn btn-mid" onclick="dccSetInterval(300)" data-v="300">5 min</button>
        <button class="btn btn-mid" onclick="dccSetInterval(600)" data-v="600">10 min</button>
      </div>
      <hr class="divider">
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn btn-grn" id="dcc-toggle" onclick="dccToggle()" style="flex:1">&#9654; Start</button>
        <button class="btn btn-acc" onclick="dccSave()" style="flex:1">&#128190; Save</button>
        <button class="btn btn-org" onclick="dccSendNow()" style="flex:1">&#128228; Send Now</button>
      </div>
    </div>

    <div class="card">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
        <div class="card-title" style="margin-bottom:0">Status</div>
        <div class="pill"><div class="dot" id="dcc-dot"></div><span id="dcc-status-lbl">Idle</span></div>
      </div>
      <div class="grid g2" style="gap:8px;margin-bottom:12px">
        <div class="card" style="padding:10px">
          <div class="card-title">Messages Sent</div>
          <div class="info-val" id="dcc-count" style="color:var(--grn)">0</div>
        </div>
        <div class="card" style="padding:10px">
          <div class="card-title">Last Sent</div>
          <div class="info-val" id="dcc-last" style="color:var(--acc);font-size:1.1rem">~</div>
        </div>
      </div>
      <div class="card-title">Last Preview</div>
      <pre id="dcc-preview" style="background:var(--drk);border-radius:8px;padding:12px;font-size:.78rem;color:var(--txt);font-family:Consolas,monospace;white-space:pre-wrap;min-height:80px;margin-top:6px;overflow:auto">~</pre>
    </div>
  </div>

</div>

<footer>Infi's Room &nbsp;·&nbsp; BME280 + Rain &amp; Garden Light + Haier AC &nbsp;·&nbsp; <span id="ts2" class="ts">--:--:--</span></footer>

<script>

function showTab(name) {
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  event.currentTarget.classList.add('active');
}


const chartOpts = (color) => ({
  responsive: true, maintainAspectRatio: false, animation: false,
  plugins: { legend: { display: false } },
  scales: {
    x: { ticks: { color: '#6c7086', maxTicksLimit: 5, font: { size: 9 } }, grid: { color: '#313244' } },
    y: { ticks: { color: '#cdd6f4', font: { size: 9 } }, grid: { color: '#313244' } }
  }
});

function makeChart(id, color) {
  return new Chart(document.getElementById(id), {
    type: 'line',
    data: { labels: [], datasets: [{ data: [], borderColor: color, backgroundColor: color + '30',
      fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2 }] },
    options: chartOpts(color)
  });
}

const charts = {
  temp:  makeChart('chart-temp',  '#a6e3a1'),
  hum:   makeChart('chart-hum',   '#89b4fa'),
  pres:  makeChart('chart-pres',  '#fab387'),
  water: makeChart('chart-water', '#cba6f7'),
};

function updateChart(ch, labels, data) {
  ch.data.labels = labels;
  ch.data.datasets[0].data = data;
  ch.update('none');
}


function setStats(pfx, arr, color, dec) {
  if (!arr.length) return;
  const mn = Math.min(...arr), mx = Math.max(...arr), av = arr.reduce((a,b)=>a+b,0)/arr.length;
  document.getElementById('mn-'+pfx).textContent = mn.toFixed(dec);
  document.getElementById('mx-'+pfx).textContent = mx.toFixed(dec);
  document.getElementById('av-'+pfx).textContent = av.toFixed(dec);
}


const es = new EventSource('/sse');
es.onmessage = function(e) {
  const d = JSON.parse(e.data);
  updateAll(d);
};
es.onerror = function() {
  setTimeout(() => location.reload(), 5000);
};

function updateAll(d) {

  setDot('bme', d.bme_ok);
  setDot('esp', d.esp_ok);
  setDot('ac',  d.ac_ok);
  setDot('kitchen', d.kitchen_ok);
  document.getElementById('ts').textContent  = d.ts;
  document.getElementById('ts2').textContent = d.ts;

  if (d.bme && d.bme.temp !== null) {
    const t = d.bme.temp, h = d.bme.hum, p = d.bme.pres;
    document.getElementById('v-temp').textContent = t.toFixed(1);
    document.getElementById('v-hum').textContent  = h.toFixed(1);
    document.getElementById('v-pres').textContent = p.toFixed(1);
    document.getElementById('v-temp').style.color = tempColor(t);
    document.getElementById('v-hum').style.color  = (h > 80 || h < 20) ? 'var(--red)' : 'var(--acc)';
    const rfEl = document.getElementById('v-temp-rf');
    if (rfEl) rfEl.textContent = h != null ? `(RF- ${realFeel(t, h).toFixed(1)}°C)` : '';
  }

  if (d.esp && Object.keys(d.esp).length) {
    const e = d.esp;
    setText('v-light',    e.light   ?? '~');
    setText('v-mode',     e.mode    ?? '~');
    setText('v-raw',      e.water_raw ?? '~');
    setText('g-light',    e.light   ?? '~');
    setText('g-mode',     e.mode    ?? '~');
    setText('g-schedule', e.schedule ?? '~');
    setText('g-time',     e.time    ?? '~');
    const rainV = e.water_voltage !== undefined ? parseFloat(e.water_voltage) : null;
    const rainVStr = rainV !== null ? rainV.toFixed(3) + ' V' : '~';
    const rainStatus = rainV === null ? '~' : rainV > 0.1 ? '️ Currently raining!' : '️ Dry / no rain';
    const rainColor  = rainV !== null && rainV > 0.1 ? 'var(--acc)' : 'var(--sub)';
    setText('v-rain', rainVStr);
    setText('v-rain-status', rainStatus);
    document.getElementById('v-rain-status').style.color = rainColor;
    setText('g-rain-v', rainVStr);
    setText('g-rain-r', e.water_raw ?? '~');
    setText('g-rain-status', rainStatus);
    document.getElementById('g-rain-status').style.color = rainColor;

    if (e.bme280_found) {
      const rawBt = e.bme280_temperature_c !== null ? parseFloat(e.bme280_temperature_c) : null;
      const rawBh = e.bme280_humidity     !== null ? parseFloat(e.bme280_humidity)      : null;
      const bt = rawBt !== null ? rawBt.toFixed(1) + '°C' : '~';
      const bh = rawBh !== null ? rawBh.toFixed(1) + '%'  : '~';
      const bp = e.bme280_pressure_hpa !== null ? parseFloat(e.bme280_pressure_hpa).toFixed(1) + ' hPa' : '~';
      const btRf = (rawBt !== null && rawBh !== null) ? ` (RF- ${realFeel(rawBt, rawBh).toFixed(1)}°C)` : '';
      setText('g-bme-temp',  bt + btRf); setText('lv-g-bme-temp', bt);
      setText('g-bme-hum',   bh); setText('lv-g-bme-hum',  bh);
      setText('g-bme-pres',  bp); setText('lv-g-bme-pres', bp);
      const lvRfEl = document.getElementById('lv-g-bme-temp-rf');
      if (lvRfEl) lvRfEl.textContent = btRf.trim();
    } else {
      ['g-bme-temp','lv-g-bme-temp','g-bme-hum','lv-g-bme-hum','g-bme-pres','lv-g-bme-pres'].forEach(id => setText(id, 'N/A'));
    }

    if (e.ds18b20_found) {
      const dt = e.ds18b20_temperature_c !== null ? parseFloat(e.ds18b20_temperature_c).toFixed(1) + '°C' : '~';
      setText('g-ds-temp', dt); setText('lv-g-ds-temp', dt);
    } else {
      setText('g-ds-temp', 'N/A'); setText('lv-g-ds-temp', 'N/A');
    }
  }

  if (d.ac && Object.keys(d.ac).length) {
    const a = d.ac;
    const extras = ['turbo','quiet','sleep','health','lock'].filter(k => a[k] === 'ON').map(k => k[0].toUpperCase()+k.slice(1));
    setText('v-ac-power', a.power ?? '~');
    setText('v-ac-temp',  a.temp !== undefined ? a.temp + '°C' : '~');
    setText('v-ac-mode',  a.mode ?? '~');
    setText('v-ac-fan',   a.fan  ?? '~');
    setText('v-ac-swing', (a.swingV ?? '~') + ' / ' + (a.swingH ?? '~'));
    document.getElementById('v-ac-power').style.color = a.power === 'ON' ? 'var(--grn)' : 'var(--red)';
    setText('ac-s-power', 'Power: ' + (a.power ?? '~'));
    setText('ac-s-temp',  'Temp: '  + (a.temp ?? '~') + '°C');
    setText('ac-s-mode',  'Mode: '  + (a.mode ?? '~'));
    setText('ac-s-fan',   'Fan: '   + (a.fan  ?? '~'));
    setText('ac-s-sv',    'Swing↕: '+ (a.swingV ?? '~'));
    setText('ac-s-sh',    'Swing↔: '+ (a.swingH ?? '~'));
    setText('ac-s-ex',    extras.length ? extras.join(' · ') : 'No extras');
    setText('ac-big-temp', a.temp !== undefined ? a.temp + '°C' : '~');
    const pwrBadge = document.querySelector('.ac-status-bar .badge:first-child');
    pwrBadge.className = 'badge ' + (a.power === 'ON' ? 'pwr-on' : 'pwr-off');
  }

  if (d.kitchen) {
    const k = d.kitchen;
    const kt = k.temp != null ? parseFloat(k.temp) : null;
    const kh = k.hum  != null ? parseFloat(k.hum)  : null;
    const krf = k.real_feel != null ? parseFloat(k.real_feel) : null;
    setText('v-k-temp', kt != null ? kt.toFixed(1) : '~');
    const kTempEl = document.getElementById('v-k-temp');
    if (kTempEl && kt != null) kTempEl.style.color = tempColor(kt);
    const kRfEl = document.getElementById('v-k-temp-rf');
    if (kRfEl) kRfEl.textContent = krf != null ? `(RF- ${krf.toFixed(1)}°C)` : (kt != null && kh != null ? `(RF- ${realFeel(kt, kh).toFixed(1)}°C)` : '');
    setText('v-k-hum', kh != null ? kh.toFixed(1) : '~');
  }
}


function fetchFull() {
  fetch('/api/data').then(r => r.json()).then(d => {
    updateAll(d);
    updateChart(charts.temp,  d.bme_hist.times, d.bme_hist.temp);
    updateChart(charts.hum,   d.bme_hist.times, d.bme_hist.hum);
    updateChart(charts.pres,  d.bme_hist.times, d.bme_hist.pres);
    updateChart(charts.water, d.water_hist.times, d.water_hist.volts);
    setStats('temp', d.bme_hist.temp,  'var(--grn)', 1);
    setStats('hum',  d.bme_hist.hum,   'var(--acc)', 1);
    setStats('pres', d.bme_hist.pres,  'var(--org)', 1);

    if (d.bme && d.bme_15) {
      const now = d.bme, ago = d.bme_15;
      setDelta('temp', now.temp, ago.temp, '°C', ago.age);
      setDelta('hum',  now.hum,  ago.hum,  '%',  ago.age);
      setDelta('pres', now.pres, ago.pres, ' hPa', ago.age);
    }

    if (d.esp && d.esp_15) {
      const e = d.esp, ago = d.esp_15;
      const outT = e.bme280_temperature_c !== undefined ? parseFloat(e.bme280_temperature_c) : null;
      const outH = e.bme280_humidity !== undefined ? parseFloat(e.bme280_humidity) : null;
      const outP = e.bme280_pressure_hpa !== undefined ? parseFloat(e.bme280_pressure_hpa) : null;
      const inT  = e.ds18b20_temperature_c !== undefined ? parseFloat(e.ds18b20_temperature_c) : null;
      setOutDelta('out-temp', outT, ago.out_temp, '°C');
      setOutDelta('out-hum',  outH, ago.out_hum,  '%');
      setOutDelta('out-pres', outP, ago.out_pres, ' hPa');
      setOutDelta('in-temp',  inT,  ago.in_temp,  '°C');
    }
  });
}
fetchFull();
setInterval(fetchFull, 15000);   // re-sync charts every 15 s


function espCmd(cmd) {
  fetch('/api/esp/' + cmd).then(r => r.json()).then(d => { if (!d.ok) alert('Error: ' + d.error); });
}
function saveSchedule() {
  const on  = document.getElementById('on-time').value.trim();
  const off = document.getElementById('off-time').value.trim();
  fetch('/api/esp/settime?on=' + encodeURIComponent(on) + '&off=' + encodeURIComponent(off))
    .then(r => r.json()).then(d => { if (!d.ok) alert('Error: ' + d.error); });
}
function acCmd(type, val) {
  fetch('/api/ac/' + type + '?value=' + encodeURIComponent(val))
    .then(r => r.json()).then(d => { if (!d.ok) alert('AC Error: ' + d.error); });
}
function acPreset(val) {
  fetch('/api/ac/preset?value=' + encodeURIComponent(val))
    .then(r => r.json()).then(d => { if (!d.ok) alert('AC Error: ' + d.error); });
}
function acToggle(item) {
  fetch('/api/ac/toggle?item=' + encodeURIComponent(item))
    .then(r => r.json()).then(d => { if (!d.ok) alert('AC Error: ' + d.error); });
}


let dcActive = false;

function loadDiscordStatus() {
  fetch('/api/discord/status').then(r => r.json()).then(d => {
    dcActive = d.active;
    setDcUI(d);
  });
}

function setDcUI(d) {
  document.getElementById('dc-status-lbl').textContent  = d.status  || 'Idle';
  document.getElementById('dc-count').textContent       = d.count   ?? 0;
  document.getElementById('dc-last').textContent        = d.last    || '~';
  document.getElementById('dc-preview').textContent     = d.preview || '~';
  const msgEl = document.getElementById('dc-msg-id');
  if (msgEl) msgEl.textContent = d.message_id || 'not sent yet';
  const dot = document.getElementById('dc-dot');
  if (dot) dot.className = 'dot ' + (d.active ? 'on' : 'off');
  const btn = document.getElementById('dc-toggle');
  if (btn) {
    btn.textContent = d.active ? '■ Stop' : '▶ Start';
    btn.className   = 'btn ' + (d.active ? 'btn-red' : 'btn-grn');
  }
}

function dcToggle() {
  if (dcActive) {
    fetch('/api/discord/stop', { method: 'POST' })
      .then(r => r.json()).then(() => { dcActive = false; loadDiscordStatus(); });
  } else {
    fetch('/api/discord/start', { method: 'POST' })
      .then(r => r.json()).then(d => {
        if (!d.ok) { alert('Error: ' + d.error); return; }
        dcActive = true; loadDiscordStatus();
      });
  }
}

function dcSendNow() {
  fetch('/api/discord/send', { method: 'POST' })
    .then(r => r.json()).then(d => {
      if (!d.ok) alert('Error: ' + d.msg);
      loadDiscordStatus();
    });
}

setInterval(() => {
  if (document.getElementById('tab-discord').classList.contains('active')) {
    loadDiscordStatus();
  }
}, 5000);


let dccActive = false;
let dccInterval = 60;

function loadDcStatus() {
  fetch('/api/discord_c/status').then(r => r.json()).then(d => {
    dccActive   = d.active;
    dccInterval = d.interval || 60;
    if (d.url) document.getElementById('dcc-url').value = d.url;
    dccHighlightInterval(d.interval || 60);
    setDccUI(d);
  });
}

function setDccUI(d) {
  document.getElementById('dcc-status-lbl').textContent = d.status  || 'Idle';
  document.getElementById('dcc-count').textContent      = d.count   ?? 0;
  document.getElementById('dcc-last').textContent       = d.last    || '~';
  document.getElementById('dcc-preview').textContent    = d.preview || '~';
  const dot = document.getElementById('dcc-dot');
  if (dot) dot.className = 'dot ' + (d.active ? 'on' : 'off');
  const btn = document.getElementById('dcc-toggle');
  if (btn) {
    btn.textContent = d.active ? '■ Stop' : '▶ Start';
    btn.className   = 'btn ' + (d.active ? 'btn-red' : 'btn-grn');
  }
}

function dccHighlightInterval(v) {
  document.querySelectorAll('#dcc-interval-btns .btn').forEach(b => {
    b.className = parseInt(b.dataset.v) === v ? 'btn btn-acc' : 'btn btn-mid';
  });
}

function dccSetInterval(v) {
  dccInterval = v;
  dccHighlightInterval(v);
}

function dccSave() {
  const url = document.getElementById('dcc-url').value.trim();
  if (url && !url.includes('__WEBHOOK_PREFIX__')) {
    alert('Invalid webhook URL.'); return;
  }
  fetch('/api/discord_c/config', { method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ url, interval: dccInterval }) })
  .then(r => r.json()).then(d => {
    if (!d.ok) alert('Error: ' + d.error);
    else alert('Saved!');
  });
}

function dccToggle() {
  if (dccActive) {
    fetch('/api/discord_c/stop', { method: 'POST' })
      .then(r => r.json()).then(() => { dccActive = false; loadDcStatus(); });
  } else {
    const url = document.getElementById('dcc-url').value.trim();
    fetch('/api/discord_c/config', { method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ url, interval: dccInterval }) })
    .then(() => fetch('/api/discord_c/start', { method: 'POST' }))
    .then(r => r.json()).then(d => {
      if (!d.ok) { alert('Error: ' + d.error); return; }
      dccActive = true; loadDcStatus();
    });
  }
}

function dccSendNow() {
  const url = document.getElementById('dcc-url').value.trim();
  fetch('/api/discord_c/config', { method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ url, interval: dccInterval }) })
  .then(() => fetch('/api/discord_c/send', { method: 'POST' }))
  .then(r => r.json()).then(d => {
    if (!d.ok) alert('Error: ' + d.msg);
    loadDcStatus();
  });
}

setInterval(() => {
  if (document.getElementById('tab-discord_c').classList.contains('active')) {
    loadDcStatus();
  }
}, 5000);

function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}


function fmtAge(secs) {
  if (secs == null) return '';
  if (secs < 60) return secs + 's ago';
  return Math.round(secs / 60) + 'min ago';
}
function deltaArrow(diff) {
  if (diff == null || isNaN(diff)) return { txt: '~', cls: 'same' };
  if (Math.abs(diff) < 0.05) return { txt: ' ±0', cls: 'same' };
  return diff > 0
    ? { txt: '▲ +' + diff.toFixed(1), cls: 'up' }
    : { txt: '▼ ' + diff.toFixed(1), cls: 'down' };
}
function setDelta(pfx, now, ago, unit, ageSecs) {
  const nowEl = document.getElementById('d-' + pfx + '-now');
  const agoEl = document.getElementById('d-' + pfx + '-ago');
  const chgEl = document.getElementById('d-' + pfx + '-chg');
  if (!nowEl) return;
  if (now == null) { nowEl.textContent = '~'; agoEl.textContent = '~'; chgEl.textContent = '~'; return; }
  nowEl.textContent = now.toFixed(1) + unit;
  if (ago == null) { agoEl.textContent = 'waiting…'; chgEl.textContent = '~'; return; }
  agoEl.textContent = ago.toFixed(1) + unit + ' (' + fmtAge(ageSecs) + ')';
  const d = deltaArrow(now - ago);
  chgEl.textContent  = d.txt + unit;
  chgEl.className    = 'd-chg ' + d.cls;
}
function setOutDelta(pfx, now, ago, unit) {
  const agoEl = document.getElementById('d-' + pfx + '-ago');
  const chgEl = document.getElementById('d-' + pfx + '-chg');
  if (!agoEl) return;
  if (ago == null) { agoEl.textContent = '~'; if(chgEl) chgEl.textContent = '~'; return; }
  agoEl.textContent = ago.toFixed(1) + unit;
  if (now == null || chgEl == null) return;
  const d = deltaArrow(now - ago);
  chgEl.textContent = d.txt + unit;
  chgEl.className   = 'ov-delta ' + d.cls;
}
function setDot(name, on) {
  const d = document.getElementById('dot-' + name);
  const l = document.getElementById('lbl-' + name);
  if (d) { d.className = 'dot ' + (on ? 'on' : 'off'); }
}
function tempColor(t) {
  if (t < 10) return 'var(--acc)';
  if (t < 18) return 'var(--prp)';
  if (t <= 26) return 'var(--grn)';
  if (t <= 32) return 'var(--org)';
  return 'var(--red)';
}

function realFeel(t, rh, windKts) {
  if (t == null || rh == null) return null;
  const w = (windKts != null && !isNaN(windKts)) ? windKts * 0.5144 : 0;
  const e = (rh / 100) * 6.105 * Math.exp(17.27 * t / (237.7 + t));
  return t + 0.33 * e - 0.70 * w - 4.00;
}
function rfStr(t, rh, windKts) {
  const rf = realFeel(t, rh, windKts);
  return rf != null ? ` (RF- ${rf.toFixed(1)}°C)` : '';
}



function vghsVisColor(vis) {
  const v = parseInt(vis)||0;
  if (v >= 5000) return 'var(--grn)';
  if (v >= 1500) return 'var(--org)';
  return 'var(--red)';
}
function vghsWindColor(spd) {
  const v = parseInt(spd)||0;
  if (v <= 10) return '#94e2d5';
  if (v <= 20) return 'var(--org)';
  return 'var(--red)';
}

function vghsCompass(dir, spd) {
  const deg   = parseInt(dir) || 0;
  const color = vghsWindColor(spd);
  const ticks = Array.from({length:36}, (_,i) => {
    const a = i*10, rad = a*Math.PI/180;
    const big = a%90===0, med = a%45===0;
    const r1 = big?35:med?37:40, r2 = 43;
    const x1 = Math.sin(rad)*r1, y1 = -Math.cos(rad)*r1;
    const x2 = Math.sin(rad)*r2, y2 = -Math.cos(rad)*r2;
    return `<line x1="${x1.toFixed(1)}" y1="${y1.toFixed(1)}" x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}" stroke="${big?'#45475a':med?'#313244':'#27273a'}" stroke-width="${big?1.5:1}"/>`;
  }).join('');
  return `<svg width="90" height="90" viewBox="-45 -45 90 90" style="overflow:visible">
    <circle r="43" fill="#181825" opacity=".7"/>
    <circle r="43" fill="none" stroke="#313244" stroke-width="1.5"/>
    ${ticks}
    <text x="0" y="-29" text-anchor="middle" font-size="8" font-weight="800" fill="#f38ba8" font-family="system-ui">N</text>
    <text x="0" y="35"  text-anchor="middle" font-size="8" font-weight="700" fill="#6c7086" font-family="system-ui">S</text>
    <text x="31" y="4"  text-anchor="middle" font-size="8" font-weight="700" fill="#6c7086" font-family="system-ui">E</text>
    <text x="-31" y="4" text-anchor="middle" font-size="8" font-weight="700" fill="#6c7086" font-family="system-ui">W</text>
    <g transform="rotate(${deg})">
      <line x1="0" y1="26" x2="0" y2="-24" stroke="${color}" stroke-width="2.5" stroke-linecap="round"/>
      <polygon points="0,-27 -6,-13 6,-13" fill="${color}" opacity=".95"/>
      <circle cx="0" cy="26" r="2.8" fill="${color}" opacity=".6"/>
    </g>
    <circle r="3.5" fill="#cdd6f4"/>
  </svg>`;
}

function vghsCmpRow(lbl, val) {
  return `<div class="vcmp-row"><span class="vcmp-lbl">${lbl}</span><span class="vcmp-val">${val||'~'}</span></div>`;
}

function renderVghs(d) {
  const m = d.main || {};
  const s = d.second || {};

  const dot = document.getElementById('dot-vghs');
  if (dot) dot.className = 'dot ' + (d.ok ? 'on' : 'off');
  const lbl = document.getElementById('lbl-vghs');
  if (lbl) lbl.textContent = d.ok ? 'BMD Live ' : 'Offline';
  const tpill = document.getElementById('vghs-ts-pill');
  if (tpill) tpill.textContent = 'last fetch: ' + (d.last || '~');

  const warea = document.getElementById('vghs-warn-area');
  if (warea) {
    if (d.warnings && d.warnings.length) {
      warea.innerHTML = '';
      d.warnings.forEach(w => {
        const div = document.createElement('div');
        div.className = 'wx-warn';
        const body = (w.content||'');
        const truncated = body.length > 400 ? body.substring(0,400) + '…' : body;
        div.innerHTML = `<div class="ww-title">️ ${w.label||'Aviation Warning'}</div>
        <div class="ww-meta"> Issued: ${w.issue_time||'~'} &nbsp;|&nbsp; Valid: ${w.date_start||'~'} → ${w.date_end||'~'}</div>`;
        const bodyDiv = document.createElement('div');
        bodyDiv.className = 'ww-body';
        bodyDiv.textContent = truncated;
        div.appendChild(bodyDiv);
        warea.appendChild(div);
      });
    } else {
      warea.innerHTML = '';
    }
  }

  if (m.raw_metar) {
    document.getElementById('vghs-metar1').style.display = '';
    document.getElementById('vghs-raw1').textContent = m.raw_metar;
  }
  if (s.raw_metar) {
    document.getElementById('vghs-metar2').style.display = '';
    document.getElementById('vghs-raw2').textContent = s.raw_metar;
  }

  if (!m.temp) {
    document.getElementById('vghs-page').innerHTML =
      `<div style="text-align:center;padding:40px;color:var(--sub)"> No data yet ~ ${d.error||'waiting…'}</div>`;
    return;
  }

  const temp    = parseInt(m.temp) || 0;
  const dewpt   = parseInt(m.dewpoint) || 0;
  const spread  = temp - dewpt;
  const rh      = parseInt(m.rh) || 0;
  const visM    = parseInt(m.visibility) || 0;
  const windSpd = parseInt(m.wind_speed) || 0;
  const wxHasTs = (m.weather_raw||'').indexOf('TS') >= 0;
  const rfVghsVal = realFeel(temp, rh || null, windSpd || null);

  const rhLabel = rh >= 90 ? '️ Very humid' : rh >= 60 ? ' Humid' : rh >= 40 ? ' Comfortable' : '️ Dry';
  const visLabel = m.vis_label || m.visibility + ' m';
  const visColor = vghsVisColor(m.visibility);
  const windColor = vghsWindColor(m.wind_speed);
  const vis10k = visM >= 10000 ? '10,000+' : visM.toLocaleString();
  const windKmh = windSpd ? Math.round(windSpd * 1.852) : 0;

  const codeTag = `<span class="vcard-tag" style="background:${m.code==='METAR'?'#1a3a1a':'#3a1a1a'};color:${m.code==='METAR'?'var(--grn)':'var(--red)'}">${m.code}</span>`;

  let html = `
  <div style="margin-bottom:10px;display:flex;flex-wrap:wrap;gap:8px;align-items:center">
    ${codeTag}
    <span class="vcard-tag"> ${m.time_bst||m.time_utc||'~'}</span>
    <span class="vcard-tag">VGHS · Dhaka HSIA</span>
    ${d.warnings&&d.warnings.length ? `<span class="vcard-tag" style="background:#3d1f1f;color:var(--red)">️ ${d.warnings.length} Warning${d.warnings.length>1?'s':''}</span>` : ''}
  </div>

  <div class="vghs-grid">

    
    <div class="vcard vc-org">
      <div class="vcard-icon">️</div>
      <div class="vcard-title">Temperature</div>
      <div><span class="vcard-big" style="color:${tempColor(temp)}">${m.temp}</span><span class="vcard-unit">°C</span></div>
      ${rfVghsVal != null ? `<div class="vcard-sub">Real Feel: <span style="color:var(--org)">${rfVghsVal.toFixed(1)}°C</span></div>` : ''}
      <div class="vcard-sub">Dew Point: <span>${m.dewpoint}°C</span></div>
      <div class="vcard-sub">Spread: <span>${spread}°C</span></div>
      ${s.temp ? `<div class="vcard-sub">Prev (${s.code}): <span>${s.temp}°C</span></div>` : ''}
    </div>

    
    <div class="vcard vc-acc">
      <div class="vcard-icon"></div>
      <div class="vcard-title">Humidity</div>
      <div><span class="vcard-big" style="color:${rh>=80?'var(--acc)':rh>=40?'var(--grn)':'var(--org)'}">${m.rh}</span><span class="vcard-unit">%</span></div>
      <div class="vcard-sub">${rhLabel}</div>
      ${s.rh ? `<div class="vcard-sub">Prev: <span>${s.rh}%</span></div>` : ''}
    </div>

    
    <div class="vcard vc-prp">
      <div class="vcard-icon"></div>
      <div class="vcard-title">Pressure QNH</div>
      <div><span class="vcard-big" style="color:var(--prp)">${m.qnh}</span><span class="vcard-unit">hPa</span></div>
      ${s.qnh ? `<div class="vcard-sub">Prev: <span>${s.qnh} hPa</span></div>` : ''}
    </div>

    
    <div class="vcard ${wxHasTs?'vc-red':'vc-sky'}">
      <div class="vcard-icon">${m.wx_emoji||'️'}</div>
      <div class="vcard-title">Present Weather</div>
      <div class="vcard-info" style="color:${wxHasTs?'var(--red)':'#89dceb'}">${m.weather||'Clear'}</div>
      ${m.weather_raw ? `<div class="vcard-sub" style="font-family:monospace;opacity:.7">(${m.weather_raw})</div>` : ''}
      ${s.weather_raw ? `<div class="vcard-sub">Prev: <span>${s.weather||s.weather_raw}</span></div>` : ''}
    </div>

    
    <div class="vcard vc-teal" style="grid-row:span 2">
      <div class="vcard-icon"></div>
      <div class="vcard-title">Wind</div>
      <div><span class="vcard-big" style="color:${windColor}">${m.wind_speed||'0'}</span><span class="vcard-unit">kt</span></div>
      ${windSpd ? `<div class="vcard-sub">${windKmh} km/h</div>` : ''}
      <div class="vcard-sub">From: <span>${m.wind_dir||'~'}° ~ ${m.wind_name||'~'}</span></div>
      ${s.wind_speed ? `<div class="vcard-sub">Prev: <span>${s.wind_dir}°/${s.wind_speed}kt</span></div>` : ''}
      <div class="compass-wrap">
        ${vghsCompass(m.wind_dir, m.wind_speed)}
        <div class="compass-lbl">${m.wind_name||'~'}</div>
      </div>
    </div>

    
    <div class="vcard ${visM>=5000?'vc-grn':visM>=1500?'vc-org':'vc-red'}">
      <div class="vcard-icon">️</div>
      <div class="vcard-title">Visibility</div>
      <div><span class="vcard-big" style="color:${visColor};font-size:1.8rem">${vis10k}</span><span class="vcard-unit">m</span></div>
      <div class="vcard-sub">${visLabel}</div>
      ${s.visibility ? `<div class="vcard-sub">Prev: <span>${parseInt(s.visibility).toLocaleString()} m</span></div>` : ''}
      ${visM<5000 ? '<div class="vcard-tag" style="background:#3a1a1a;color:var(--red)">️ Reduced vis</div>' : '<div class="vcard-tag" style="background:#1a3a1a;color:var(--grn)"> Good visibility</div>'}
    </div>

    
    <div class="vcard vc-yel">
      <div class="vcard-icon">️</div>
      <div class="vcard-title">Cloud Cover</div>
      <div class="vcard-info" style="color:#f9e2af;font-size:1rem;line-height:1.4">${m.cloud||'~'}</div>
      <div class="vcard-sub">Raw: <span>${m.cloud_raw||'~'}</span></div>
      ${s.cloud_raw ? `<div class="vcard-sub">Prev: <span>${s.cloud||s.cloud_raw}</span></div>` : ''}
      ${m.end ? `<div class="vcard-tag">${m.end.replace('=','').trim()}</div>` : ''}
    </div>

  </div>`;

  if (s.temp) {
    html += `
  <div class="section-title" style="margin-bottom:8px"> Latest vs Previous Report</div>
  <div class="vghs-compare">
    <div class="vcmp-card">
      <div class="vcmp-title">${m.code||'METAR'} &nbsp; ${m.time_bst||m.time_utc||''}</div>
      ${vghsCmpRow('Temp', m.temp+'°C')}
      ${vghsCmpRow('Humidity', m.rh+'%')}
      ${vghsCmpRow('Wind', m.wind_dir+'°/'+m.wind_speed+'kt')}
      ${vghsCmpRow('Visibility', visM.toLocaleString()+' m')}
      ${vghsCmpRow('Cloud', m.cloud_raw)}
      ${vghsCmpRow('Weather', m.weather_raw||'None')}
      ${vghsCmpRow('QNH', m.qnh+' hPa')}
    </div>
    <div class="vcmp-card">
      <div class="vcmp-title">${s.code||'SPECI'} &nbsp; ${s.time_bst||s.time_utc||''}</div>
      ${vghsCmpRow('Temp', s.temp+'°C')}
      ${vghsCmpRow('Humidity', s.rh+'%')}
      ${vghsCmpRow('Wind', s.wind_dir+'°/'+s.wind_speed+'kt')}
      ${vghsCmpRow('Visibility', (parseInt(s.visibility)||0).toLocaleString()+' m')}
      ${vghsCmpRow('Cloud', s.cloud_raw)}
      ${vghsCmpRow('Weather', s.weather_raw||'None')}
      ${vghsCmpRow('QNH', s.qnh+' hPa')}
    </div>
  </div>`;
  }

  document.getElementById('vghs-page').innerHTML = html;
}

function loadVghs() {
  fetch('/api/vghs').then(r => r.json()).then(renderVghs).catch(err => {
    const p = document.getElementById('vghs-page');
    if (p) p.innerHTML = `<div style="text-align:center;padding:40px;color:var(--red)">️ Failed to load: ${err}</div>`;
  });
}

setInterval(() => {
  if (document.getElementById('tab-aviation') &&
      document.getElementById('tab-aviation').classList.contains('active')) {
    loadVghs();
  }
}, 600000);


const SAT_BASE = '__SAT_BASE_JS__';

function satCode(utcDate) {




  const totalMin = utcDate.getUTCHours() * 60 + utcDate.getUTCMinutes();
  const codeMin  = Math.floor((totalMin - 30) / 10) * 10;
  const ch = Math.floor(((codeMin % 1440) + 1440) % 1440 / 60);
  const cm = ((codeMin % 60) + 60) % 60;
  return String(ch).padStart(2,'0') + String(cm).padStart(2,'0');
}

function satUrl(code) {
  return SAT_BASE.replace('{code}', code);
}

function satImageTime(code) {

  const ch = parseInt(code.slice(0,2));
  const cm = parseInt(code.slice(2,4));
  const totalMin = ch * 60 + cm + 10;
  const uh = Math.floor(totalMin / 60) % 24;
  const um = totalMin % 60;
  const bh = Math.floor((totalMin + 360) / 60) % 24;
  const bm = (totalMin + 360) % 60;
  const pad = n => String(n).padStart(2,'0');
  return {
    utc: pad(uh)+':'+pad(um)+' UTC',
    bst: pad(bh)+':'+pad(bm)+' BST',
  };
}

function satPrevCode(code, steps) {

  const ch = parseInt(code.slice(0,2));
  const cm = parseInt(code.slice(2,4));
  let totalMin = ch * 60 + cm - steps * 10;
  totalMin = ((totalMin % 1440) + 1440) % 1440;
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  return String(h).padStart(2,'0') + String(m).padStart(2,'0');
}

function loadSatellite() {
  const now    = new Date();
  const latest = satCode(now);

  const times  = satImageTime(latest);
  const lbl    = document.getElementById('sat-latest-lbl');
  const img    = document.getElementById('sat-latest-img');
  const err    = document.getElementById('sat-latest-err');
  if (lbl) lbl.textContent =
    `️ Latest image: ${times.bst}  ·  ${times.utc}  ·  code: ${latest}`;
  if (img) {
    img.style.display = '';
    err.style.display = 'none';
    img.src = satUrl(latest) + '?t=' + Date.now();
    img.onerror = () => { img.style.display='none'; err.style.display=''; };
  }

  const grid = document.getElementById('sat-grid');
  if (!grid) return;
  grid.innerHTML = '';

  for (let i = 0; i < 144; i++) {
    const code = satPrevCode(latest, i);
    const t    = satImageTime(code);
    const url  = satUrl(code);

    const card = document.createElement('div');
    card.style.cssText = 'text-align:center;background:#1a1a2e;border-radius:6px;padding:5px;width:180px;flex-shrink:0;cursor:pointer';
    card.title = t.bst + ' / ' + t.utc;
    card.onclick = () => {
      document.getElementById('sat-latest-img').src = url;
      document.getElementById('sat-latest-lbl').textContent =
        '\uD83D\uDEF0\uFE0F Selected: ' + t.bst + '  \u00B7  ' + t.utc + '  \u00B7  code: ' + code;
      window.scrollTo({top:0, behavior:'smooth'});
    };

    const im = document.createElement('img');
    im.src    = url;
    im.width  = 170;
    im.loading = 'lazy';
    im.style.cssText = 'border-radius:4px;display:block;margin:0 auto';
    im.onerror = () => { im.style.opacity = '0.12'; };

    const bstLbl = document.createElement('div');
    bstLbl.style.cssText = 'color:#a0c4ff;font-size:10px;margin-top:4px;font-family:monospace;font-weight:bold';
    bstLbl.textContent = t.bst;

    const utcLbl = document.createElement('div');
    utcLbl.style.cssText = 'color:#506080;font-size:9px;font-family:monospace';
    utcLbl.textContent = t.utc;

    const codeLbl = document.createElement('div');
    codeLbl.style.cssText = 'color:#333;font-size:8px;margin-top:1px';
    codeLbl.textContent = 'code: ' + code;

    card.appendChild(im);
    card.appendChild(bstLbl);
    card.appendChild(utcLbl);
    card.appendChild(codeLbl);
    grid.appendChild(card);
  }
}

setInterval(() => {
  if (document.getElementById('tab-satellite') &&
      document.getElementById('tab-satellite').classList.contains('active')) {
    loadSatellite();
  }
}, 600000);


async function loadSummary() {
  try {
    const [d, v] = await Promise.all([
      fetch('/api/data').then(r => r.json()),
      fetch('/api/vghs').then(r => r.json())
    ]);
    const bme = d.bme || {}, esp = d.esp || {}, ac = d.ac || {};
    const b15 = d.bme_15, e15 = d.esp_15;
    function fv(val, dec, unit) { unit = unit||''; dec = dec==null?1:dec; return val != null ? parseFloat(val).toFixed(dec)+unit : '~'; }
    function el(id) { return document.getElementById(id); }

    el('sm-r-temp').textContent = fv(bme.temp, 1, '°C');
    el('sm-r-hum').textContent  = fv(bme.hum,  1, '%');
    el('sm-r-pres').textContent = fv(bme.pres, 1, ' hPa');
    const rfRoom = realFeel(bme.temp != null ? parseFloat(bme.temp) : null,
                            bme.hum  != null ? parseFloat(bme.hum)  : null);
    if (el('sm-r-temp-rf')) el('sm-r-temp-rf').textContent = rfRoom != null ? `(RF- ${rfRoom.toFixed(1)}°C)` : '';
    if (b15) {
      function dstr(now, ago, unit) {
        if (now == null || ago == null) return '~';
        const d = (parseFloat(now)-parseFloat(ago)).toFixed(1);
        return (d>0?'▲ +':'▼ ') + d + unit;
      }
      el('sm-r-temp-d').textContent = dstr(bme.temp, b15.temp, '°C') + ' vs 15m ago';
      el('sm-r-hum-d').textContent  = dstr(bme.hum,  b15.hum,  '%')  + ' vs 15m ago';
      el('sm-r-pres-d').textContent = dstr(bme.pres, b15.pres, ' hPa') + ' vs 15m ago';
    }

    el('sm-o-temp').textContent = fv(esp.bme280_temperature_c, 1, '°C');
    el('sm-o-hum').textContent  = fv(esp.bme280_humidity,      1, '%');
    el('sm-o-pres').textContent = fv(esp.bme280_pressure_hpa,  1, ' hPa');
    const rfOut = realFeel(esp.bme280_temperature_c != null ? parseFloat(esp.bme280_temperature_c) : null,
                           esp.bme280_humidity      != null ? parseFloat(esp.bme280_humidity)      : null);
    if (el('sm-o-temp-rf')) el('sm-o-temp-rf').textContent = rfOut != null ? `(RF- ${rfOut.toFixed(1)}°C)` : '';
    if (e15) {
      function dstr2(now, ago, unit) {
        if (now == null || ago == null) return '~';
        const d = (parseFloat(now)-parseFloat(ago)).toFixed(1);
        return (d>0?'▲ +':'▼ ') + d + unit;
      }
      el('sm-o-temp-d').textContent = dstr2(esp.bme280_temperature_c, e15.out_temp, '°C') + ' vs 15m ago';
      el('sm-o-hum-d').textContent  = dstr2(esp.bme280_humidity,      e15.out_hum,  '%')  + ' vs 15m ago';
      el('sm-o-pres-d').textContent = dstr2(esp.bme280_pressure_hpa,  e15.out_pres, ' hPa') + ' vs 15m ago';
    }
    el('sm-ds-temp').textContent = fv(esp.ds18b20_temperature_c, 1, '°C');
    const rv = esp.water_voltage;
    el('sm-rain').textContent = rv != null ? (parseFloat(rv) > 0.1 ? '️ Raining' : '️ Dry') : '~';
    el('sm-rain-v').textContent  = rv != null ? parseFloat(rv).toFixed(3) + ' V' : '~';
    el('sm-rain-raw').textContent = rv != null ? 'raw ADC: ' + (esp.water_raw||'~') : '';

    const k = d.kitchen || {};
    const kt = k.temp != null ? parseFloat(k.temp) : null;
    const kh = k.hum  != null ? parseFloat(k.hum)  : null;
    const krf_sm = k.real_feel != null ? parseFloat(k.real_feel) : null;
    if (el('sm-k-temp')) el('sm-k-temp').textContent = fv(kt, 1, '°C');
    if (el('sm-k-hum'))  el('sm-k-hum').textContent  = fv(kh, 1, '%');
    if (el('sm-k-temp-rf')) el('sm-k-temp-rf').textContent = krf_sm != null ? `(RF- ${krf_sm.toFixed(1)}°C)` : '';

    el('sm-ac-pwr').textContent   = ac.power || '~';
    el('sm-ac-pwr').style.color   = ac.power === 'ON' ? 'var(--grn)' : 'var(--red)';
    el('sm-ac-temp').textContent  = ac.temp  ? ac.temp + ' °C' : '~';
    el('sm-ac-mode').textContent  = ac.mode  || '~';
    el('sm-ac-fan').textContent   = ac.fan   || '~';
    el('sm-ac-swing').textContent = (ac.swingV||'~') + ' / ' + (ac.swingH||'~');
    const exs = ['turbo','quiet','sleep','health','lock'].filter(k => ac[k]==='ON').map(k => k[0].toUpperCase()+k.slice(1));
    el('sm-ac-extras').textContent = exs.length ? ' ' + exs.join(', ') : '';

    const m = v.main || {}, warns = v.warnings || [];
    el('sm-vg-dot').className    = 'dot' + (v.ok ? '' : ' dot-err');
    el('sm-vg-status').textContent = v.ok ? 'Live' : 'Offline';
    el('sm-vg-time').textContent = (m.time_bst||'~') + '  ·  last fetched ' + (v.last||'~');
    el('sm-vg-temp').textContent = (m.temp  || '~') + '°C';
    const wktSm = parseFloat(m.wind_speed) || 0;
    const rfVghs = realFeel(m.temp ? parseFloat(m.temp) : null,
                            m.rh   ? parseFloat(m.rh)   : null,
                            wktSm);
    if (el('sm-vg-temp-rf')) el('sm-vg-temp-rf').textContent = rfVghs != null ? `(RF- ${rfVghs.toFixed(1)}°C)` : '';
    el('sm-vg-wx-inline').textContent = m.wx_emoji ? (m.wx_emoji + ' ' + (m.weather||'')) : (m.cloud ? '️ ' + m.cloud : 'Clear skies');
    el('sm-vg-hum').textContent  = (m.rh    || '~') + '%';
    el('sm-vg-dew').textContent  = m.dewpoint ? 'Dew point: ' + m.dewpoint + '°C' : '';
    el('sm-vg-qnh').textContent  = (m.qnh   || '~') + ' hPa';
    const wkt = parseFloat(m.wind_speed);
    const wkmh = !isNaN(wkt) ? Math.round(wkt * 1.852) : null;
    el('sm-vg-wind').textContent  = (m.wind_dir||'~') + '° ' + (m.wind_name||'') + ' @ ' + (m.wind_speed||'~') + ' kt';
    el('sm-vg-wind2').textContent = wkmh != null ? '≈ ' + wkmh + ' km/h' : '';
    el('sm-vg-vis').textContent  = m.vis_label || '~';
    el('sm-vg-cloud').textContent = m.cloud   || '~';
    el('sm-vg-warns').innerHTML  = warns.length
      ? warns.slice(0,3).map(w => `<div style="background:var(--red);color:#fff;border-radius:6px;padding:6px 12px;margin-top:6px;font-size:.82rem;font-weight:600">️ ${w.label||''}${w.date_start?' &middot; from '+w.date_start:''}</div>`).join('')
      : '<div style="color:var(--sub);font-size:.8rem;margin-top:4px"> No active warnings</div>';

    const code = satCode(new Date());
    const imgMin = (parseInt(code.slice(0,2))*60 + parseInt(code.slice(2)) + 10) % 1440;
    const bh = Math.floor((imgMin+360)%1440/60), bm = (imgMin+360)%60;
    el('sm-sat-lbl').textContent = `Image time ~ ${String(bh).padStart(2,'0')}:${String(bm).padStart(2,'0')} BST`;
    el('sm-sat-img').src = satUrl(code) + '?t=' + Date.now();

    el('sm-ts').textContent = 'Updated: ' + d.ts;
  } catch(e) { console.error('loadSummary', e); }
}

loadSummary();

setInterval(() => {
  if (document.getElementById('tab-summary').classList.contains('active')) {
    loadSummary();
  }
}, 60000);
</script>
</body>
</html>
"""

                                                                          

if __name__ == "__main__":
    import socket
                                                   
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "localhost"

    print("=" * 56)
    print("  Infi's Room  ·  Local Network Web Dashboard")
    print("=" * 56)
    print(f"  BME280:  {'real sensor' if _HAS_BME else 'simulated'}")
    print("  Press Ctrl+C to stop")
    print("=" * 56)

    threading.Thread(target=bme_reader,     daemon=True).start()
    threading.Thread(target=esp_reader,     daemon=True).start()
    threading.Thread(target=kitchen_reader, daemon=True).start()
    threading.Thread(target=vghs_poller,   daemon=True).start()
    threading.Thread(target=_dc_sat_loop,  daemon=True).start()

    app.run(host=WEB_HOST, port=WEB_PORT, threaded=True)
