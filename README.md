# SafeBand Python Serial Bridge

## Files
- `safeband_bridge.py`: reads ESP32 serial text, parses recognized values/events, and posts JSON to Base44.
- `bridge_config.example.json`: safe template. Copy it to `bridge_config.json`.
- `requirements.txt`: dependencies.

## Windows setup
1. Install Python 3.10+.
2. In this folder, run: `py -m pip install -r requirements.txt`
3. Copy `bridge_config.example.json` to `bridge_config.json`.
4. In `bridge_config.json`, set the ESP32's actual COM port and put the `DEVICE_API_KEY` from Base44 Secrets in `api_key`. Never share or commit this file.
5. Connect ESP32 to your laptop by USB. Close Arduino Serial Monitor; it and Python normally cannot share the same COM port.
6. Run: `py safeband_bridge.py`
7. Keep `dry_run: true` for the first test. It prints payloads but sends nothing.
8. When the printed values/events look correct, set `dry_run` to `false`, save, restart, and check the Base44 HTTP response.

## Current parser coverage
Recognizes `Current BPM: N`, `Heart Rate: N`, `BPM: N`, `HR: N`; SpO2 labels; battery labels; Google Maps URL coordinates; `Fall Detected!`; high-BPM alerts; `GSM Response: EMERGENCY ALERT!`; `SMS SENT SUCCESSFULLY`; alert cancellation; and return to normal.
BPM 0 is ignored. No missing values are invented. The screenshots show `Location not available yet`, so GPS cannot be forwarded until real coordinates are printed. SpO2 and battery parsing should be verified against actual Serial lines when available.

## Known integration check
The bridge sends `event_type`, `is_sos_active`, and `status` for event messages. Confirm the existing Base44 function uses these fields to create `EmergencyEvent` records. If the API returns an error or records are not created, share the response/error (never share the API key) so the payload can be adjusted to the actual function schema.
