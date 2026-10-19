
import json
import re
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

import requests
import serial
from serial.tools import list_ports


ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "bridge_config.json"

DEFAULTS = {
    "serial_port": "COM3",
    "baud_rate": 115200,
    "device_id": "SB-ESP32-001",
    "api_url": (
        "https://safeband-pulse-guard.base44.app/"
        "functions/ingestDeviceData"
    ),
    "api_key": "",
    "dry_run": True,
    "event_dedup_seconds": 20,
    "post_timeout_seconds": 10,
}


def load_config():
    if not CONFIG.exists():
        raise SystemExit(
            "Missing bridge_config.json in the project folder."
        )

    cfg = DEFAULTS.copy()
    cfg.update(json.loads(CONFIG.read_text(encoding="utf-8")))

    if not cfg["api_key"]:
        raise SystemExit("Configure your API key privately.")

    if not cfg["api_url"].startswith("https://"):
        raise SystemExit("The API URL must use HTTPS.")

    if not cfg["dry_run"]:
        print("LIVE MODE: requests will be sent to Base44.")

    return cfg


def now_iso():
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def clean(line):
    return re.sub(
        r"^\s*\d{1,2}:\d{2}:\d{2}(?:\.\d+)?\s*->\s*",
        "",
        line,
    ).strip()


def gps_from(line):
    patterns = [
        r"https?://(?:www\.)?maps\.google\.com/\?q="
        r"(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)",

        r"(?:GPS|Location|Coordinates|Latitude)"
        r"[^\d-]*(-?\d{1,2}\.\d+)\s*[, ]+\s*"
        r"(-?\d{1,3}\.\d+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, line, re.I)
        if not match:
            continue

        lat = float(match.group(1))
        lon = float(match.group(2))

        if (
            -90 <= lat <= 90
            and -180 <= lon <= 180
            and (lat != 0 or lon != 0)
        ):
            return {"latitude": lat, "longitude": lon}

    return None


def parse_values(line):
    values = {}

    match = re.search(
        r"\b(?:Current\s+BPM|Heart\s*Rate|BPM|HR)\s*[:=]\s*(\d{1,3})\b",
        line,
        re.I,
    )
    if match:
        bpm = int(match.group(1))
        if 30 <= bpm <= 220:
            values["heart_rate"] = bpm

    match = re.search(
        r"\bSpO\s*[₂2]?\s*[:=]\s*(\d{1,3})\s*%?",
        line,
        re.I,
    )
    if match:
        spo2 = int(match.group(1))
        if 50 <= spo2 <= 100:
            values["spo2"] = spo2

    match = re.search(
        r"\bBattery(?:\s+Level)?\s*[:=]\s*(\d{1,3})\s*%?",
        line,
        re.I,
    )
    if match:
        battery = int(match.group(1))
        if 0 <= battery <= 100:
            values["battery_level"] = battery

    location = gps_from(line)
    if location:
        values["location"] = location

    return values


def event_from(line):
    text = line.lower()

    if "alert cancelled by user" in text:
        return "ALERT_CANCELLED"

    if "standby over - back to normal" in text:
        return "NORMAL"

    if "alert buffer started:" in text:
        return "ALERT_BUFFER_STARTED"

    # These lines report delivery/status, not a new emergency.
    if (
        "gsm response:" in text
        or "sms sent successfully" in text
    ):
        return None

    # A detection alone must not bypass the 10-second countdown.
    if "emergency alert" not in text:
        return None

    if "fall detected" in text:
        return "FALL_DETECTED"

    if "high bpm" in text:
        return "HIGH_BPM_ALERT"

    if "button pressed" in text:
        return "SOS_BUTTON"

    return "EMERGENCY_ALERT"


class Bridge:
    def __init__(self, cfg):
        self.cfg = cfg
        self.session = requests.Session()
        self.latest_values = {}
        self.emergency_episode_open = False

    def post(self, fields, label):
        payload = {
            "device_id": self.cfg["device_id"],
            "api_key": self.cfg["api_key"],
            "timestamp": now_iso(),
            **fields,
        }

        shown = dict(payload)
        shown["api_key"] = "***REDACTED***"

        print(f"\n[{label}]")
        print(json.dumps(shown, indent=2))

        if self.cfg["dry_run"]:
            print("[DRY RUN] Not sent to Base44.")
            return True

        try:
            response = self.session.post(
                self.cfg["api_url"],
                json=payload,
                timeout=int(self.cfg["post_timeout_seconds"]),
            )

            print(f"Base44 HTTP {response.status_code}")
            response.raise_for_status()

            try:
                result = response.json()
                print("Base44 response:", json.dumps(result)[:800])
            except ValueError:
                print("Base44 returned a non-JSON response.")

            return True

        except requests.RequestException as error:
            print("Base44 POST failed:", error)
            return False

    def handle(self, raw):
        line = clean(raw)
        if not line:
            return

        print("[ESP32]", line)

        values = parse_values(line)
        self.latest_values.update(values)

        # Partial sensor updates must not clear an active SOS.
        if values:
            self.post(values, "SENSOR DATA")

        event = event_from(line)
        if not event:
            return

        if event == "ALERT_BUFFER_STARTED":
            print(
                "[INFO] Countdown started. "
                "Waiting for confirmation after the countdown."
            )
            return

        if event in ("ALERT_CANCELLED", "NORMAL"):
            success = self.post(
                {
                    "status": "NORMAL",
                    "is_sos_active": False,
                    "event_type": event,
                },
                event,
            )

            if success:
                self.emergency_episode_open = False
            return

        # Fall/BPM/button events from the same incident should not
        # create repeated emergency updates.
        if self.emergency_episode_open:
            print("[INFO] Duplicate emergency suppressed:", event)
            return

        emergency_fields = {
            **self.latest_values,
            "status": "EMERGENCY",
            "is_sos_active": True,
            "event_type": event,
        }

        if self.post(emergency_fields, event):
            self.emergency_episode_open = True


def main():
    cfg = load_config()

    print("SafeBand Serial Bridge")
    print("Device:", cfg["device_id"])
    print("Endpoint:", cfg["api_url"])
    print("Dry run:", cfg["dry_run"])

    ports = list(list_ports.comports())
    print(
        "Detected ports:",
        ", ".join(
            f"{port.device} ({port.description})"
            for port in ports
        ) or "none",
    )

    bridge = Bridge(cfg)

    try:
        with serial.Serial(
            cfg["serial_port"],
            int(cfg["baud_rate"]),
            timeout=1,
        ) as connection:
            time.sleep(2)
            connection.reset_input_buffer()

            print(
                f"Listening on {cfg['serial_port']} "
                f"at {cfg['baud_rate']} baud. Ctrl+C to stop."
            )

            while True:
                raw = connection.readline()
                if raw:
                    bridge.handle(
                        raw.decode("utf-8", errors="replace")
                    )

    except KeyboardInterrupt:
        print("\nBridge stopped.")

    except serial.SerialException as error:
        print("Serial connection failed:", error)
        print(
            "Check the COM port, USB cable and driver. "
            "Close Arduino Serial Monitor before starting the bridge."
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
