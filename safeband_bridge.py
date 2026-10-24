import json, re, sys, time
from pathlib import Path
from datetime import datetime, timezone
import requests
import serial
from serial.tools import list_ports

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "bridge_config.json"
DEFAULTS = {
    "serial_port": "COM7", "baud_rate": 115200, "device_id": "SB-ESP32-001",
    "api_url": "https://safeband-pulse-guard.base44.app/functions/ingestDeviceData",
    "api_key": "", "dry_run": True, "location_post_interval_seconds": 5,
    "event_dedup_seconds": 20, "post_timeout_seconds": 10
}

def load_config():
    if not CONFIG_PATH.exists():
        raise SystemExit("Missing bridge_config.json. Copy bridge_config.example.json to bridge_config.json first.")
    try:
        cfg = DEFAULTS.copy(); cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Could not read bridge_config.json: {exc}")
    if not cfg["api_url"].startswith("https://"):
        raise SystemExit("api_url must use HTTPS.")
    if not cfg["dry_run"] and (not cfg.get("api_key") or cfg["api_key"].startswith("PASTE_")):
        raise SystemExit("Set your rotated API key privately in bridge_config.json.")
    return cfg

def timestamp_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def clean(line):
    return re.sub(r"^\s*\d{1,2}:\d{2}:\d{2}(?:\.\d+)?\s*->\s*", "", line).strip()

def parse_location(line):
    patterns = [
        r"https?://(?:www\.)?maps\.google\.com/\?q=(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)",
        r"https?://(?:www\.)?google\.com/maps\?q=(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)",
        r"\b(?:GPS|Location|Coordinates)\s*[:=]\s*(-?\d{1,2}(?:\.\d+)?)\s*[,; ]+\s*(-?\d{1,3}(?:\.\d+)?)",
        r"\bLatitude\s*[:=]\s*(-?\d{1,2}(?:\.\d+)?)\s*[,; ]+\s*Longitude\s*[:=]\s*(-?\d{1,3}(?:\.\d+)?)"
    ]
    for pattern in patterns:
        m = re.search(pattern, line, re.I)
        if m:
            lat, lon = float(m.group(1)), float(m.group(2))
            if -90 <= lat <= 90 and -180 <= lon <= 180 and (lat != 0 or lon != 0):
                return {"latitude": lat, "longitude": lon}
    return None

def classify_event(line):
    s = line.lower()
    if "alert cancelled by user" in s: return "ALERT_CANCELLED"
    if "standby over - back to normal" in s: return "NORMAL"
    if "alert buffer started:" in s: return "ALERT_BUFFER_STARTED"
    if "gsm response:" in s or "sms sent successfully" in s: return None
    # Only treat a confirmed firmware emergency message as an SOS.
    if "emergency alert" not in s: return None
    if "fall detected" in s: return "FALL_DETECTED"
    if "button pressed" in s or "sos button" in s or "sos pressed" in s: return "SOS_BUTTON"
    return "EMERGENCY_ALERT"

class Bridge:
    def __init__(self, cfg):
        self.cfg=cfg; self.session=requests.Session(); self.last_location=None
        self.last_location_post=0.0; self.last_event={}; self.emergency_open=False
    def post(self, fields, label):
        payload={"device_id":self.cfg["device_id"], "timestamp":timestamp_iso(), **fields}
        if self.cfg.get("api_key"): payload["api_key"]=self.cfg["api_key"]
        shown=dict(payload)
        if "api_key" in shown: shown["api_key"]="***REDACTED***"
        print(f"\n[{label}]\n"+json.dumps(shown,indent=2))
        if self.cfg.get("dry_run", True):
            print("[DRY RUN] Not sent to Base44."); return True
        try:
            r=self.session.post(self.cfg["api_url"],json=payload,timeout=int(self.cfg["post_timeout_seconds"]))
            print("Base44 HTTP",r.status_code); r.raise_for_status()
            try: result=r.json(); print("Base44 response:",json.dumps(result)[:1200])
            except ValueError: print("Non-JSON response; saving is unconfirmed."); return False
            if isinstance(result,dict) and result.get("success") is False: return False
            if label == "GPS LOCATION" and isinstance(result,dict) and result.get("location_saved") is False:
                print("ERROR: backend says location_saved=false; check backend schema."); return False
            return True
        except requests.RequestException as e:
            print("Base44 POST failed:",e); return False
    def handle(self, raw):
        line=clean(raw)
        if not line: return
        print("[ESP32]",line)
        loc=parse_location(line)
        if loc:
            now=time.monotonic(); interval=max(1,int(self.cfg.get("location_post_interval_seconds",5)))
            if loc != self.last_location or now-self.last_location_post >= interval:
                fields={"location":loc,"latitude":loc["latitude"],"longitude":loc["longitude"],"event_type":"LOCATION_UPDATE"}
                # Do not force NORMAL status on every GPS update; avoid clearing active SOS.
                if self.post(fields,"GPS LOCATION"):
                    self.last_location=loc; self.last_location_post=now
        event=classify_event(line)
        if not event:
            if any(w in line.lower() for w in ("gps","location","latitude","longitude")) and not loc:
                print("[INFO] GPS-like line but no coordinates parsed. Expected: GPS: 16.5062, 80.6480")
            return
        if event == "ALERT_BUFFER_STARTED":
            print("[INFO] Countdown started; waiting for confirmed emergency."); return
        if event in ("ALERT_CANCELLED","NORMAL"):
            if self.post({"status":"NORMAL","is_sos_active":False,"event_type":event},event): self.emergency_open=False
            return
        now=time.monotonic(); dedup=max(0,int(self.cfg.get("event_dedup_seconds",20)))
        if now-self.last_event.get(event,0) < dedup:
            print("[INFO] Duplicate event suppressed:",event); return
        if self.emergency_open:
            print("[INFO] Emergency already active; duplicate suppressed:",event); return
        fields={"status":"EMERGENCY","is_sos_active":True,"event_type":event}
        if self.last_location: fields.update({"location":self.last_location,"latitude":self.last_location["latitude"],"longitude":self.last_location["longitude"]})
        if self.post(fields,event): self.emergency_open=True; self.last_event[event]=now

def main():
    cfg=load_config(); print("SafeBand GPS + SOS Serial Bridge")
    print("Device:",cfg["device_id"]); print("Endpoint:",cfg["api_url"]); print("Dry run:",cfg["dry_run"])
    ports=list(list_ports.comports())
    print("Detected ports:",", ".join(f"{p.device} ({p.description})" for p in ports) or "none")
    try:
        with serial.Serial(cfg["serial_port"],int(cfg["baud_rate"]),timeout=1) as conn:
            time.sleep(2); conn.reset_input_buffer()
            print(f"Listening on {cfg['serial_port']} at {cfg['baud_rate']} baud. Ctrl+C to stop.")
            print("Waiting for GPS coordinates or confirmed SOS messages...")
            bridge=Bridge(cfg)
            while True:
                raw=conn.readline()
                if raw: bridge.handle(raw.decode("utf-8",errors="replace"))
    except KeyboardInterrupt: print("\nBridge stopped.")
    except serial.SerialException as e:
        print("Serial connection failed:",e); print("Check COM port/cable; close Arduino Serial Monitor first."); sys.exit(2)

if __name__ == "__main__": main()
