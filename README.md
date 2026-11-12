# Airthings Mac poller

Read nearby Airthings Wave Radon and Wave Enhance sensors with the Mac's native
Bluetooth, outside a Home Assistant virtual machine. Optionally publish measurements
to Home Assistant through MQTT discovery. No Airthings hub or cloud API is used.

```text
Wave Radon / Wave Enhance → macOS CoreBluetooth → Python collector
                                                ├─ private local JSON
                                                └─ MQTT broker → Home Assistant
```

## Validation status

Tested on an Intel Mac running macOS Sonoma with Python 3.13. Both physical devices
returned readings in three successive test rounds (six successful reads). The
production collector also read both devices successfully. Offline tests cover unit
conversion, invalid values, discovery metadata, stable identifiers, and private
state-file permissions. MQTT delivery and Home Assistant entity creation have not
yet been verified against the target Home Assistant installation.

Successful reads confirm Bluetooth transport, not that the sensor has taken a new
measurement. Radon readings are device-calculated averages and change slowly.
This is a small personal project, not an official Airthings application.

## Install

Use Python 3.13 and a project-local virtual environment. With an existing Python:

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp config.example.json config.json
chmod 600 config.json
```

Alternatively, with uv installed from its official source:

```sh
uv python install 3.13
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

The official [Airthings BLE library](https://github.com/Airthings/airthings-ble)
is pinned to the commit in `source-commit.txt`. cbor2 5.9.0 avoids a Rust source
build on the tested Intel Mac. System Python is not changed.

Allow Bluetooth access for the terminal or application launching Python under
macOS System Settings → Privacy & Security → Bluetooth. Close the Airthings phone
app if it holds a competing Bluetooth connection. Devices must be in Bluetooth
mode; SmartLink mode is not supported by this collector.

## Configure and run

In `config.json`, enter each device's actual 10-digit serial number and a friendly
name. The collector reads only configured devices. For a local-only test, remove
the entire `mqtt` object. No Home Assistant changes are needed for that test.

```sh
.venv/bin/python poller.py --once
.venv/bin/python poller.py
```

Default polling is every five minutes with a 20-second discovery scan. The
collector saves private snapshots to `state/latest.json` and rotates its logs in
`logs/`. A process lock prevents overlapping runs from the same installation.
A failed read preserves the previous collection timestamp; after `stale_after`
seconds the device is marked unavailable. The one-shot command returns nonzero
if any configured device fails its current read.

Temperature is converted from °C to °F, radon from Bq/m³ to pCi/L using division
by 37. CO₂ uses ppm, VOC ppb, and humidity %. Only finite numeric values are
published. Missing values never become zero. Wave Radon's uncalibrated light
reading is omitted rather than mislabeled as lux.

### Home Assistant MQTT

1. Use an existing MQTT broker, or install the official Mosquitto broker app in
   Home Assistant. Configure authenticated access and the MQTT integration using
   supported Home Assistant setup. Do not forward broker ports to the internet.
2. Set the broker hostname, port, username, and optional TLS in `config.json`.
   Default port 1883 without TLS sends credentials and readings unencrypted on
   your LAN. For an existing TLS broker, set `tls: true` and its port (often 8883);
   system certificate validation remains enabled.
3. Supply the password in the named environment variable, keeping it out of Git:

   ```sh
   read -s AIRTHINGS_MQTT_PASSWORD
   export AIRTHINGS_MQTT_PASSWORD
   .venv/bin/python poller.py
   ```

4. Verify two MQTT devices and their live values in Settings → Devices & services
   → MQTT. Entity IDs are assigned by Home Assistant; this project does not assume
   them. Discovery and availability are retained; measurement messages are not.
   Discovery is replayed on each successful poll, so HA restarts recover within
   a polling interval. Both per-device and collector availability must be online.

If cloud Airthings entities already exist, use distinguishable names (for example
“Wave Radon Local”) while validating. Do not remove existing integrations or
repoint dashboards until you have checked the local readings. This project never
edits Homebridge or existing Home Assistant configuration.

### Continuous operation

Keep the Mac awake, powered, and within Bluetooth range. A LaunchAgent can start
`.venv/bin/python` with the absolute path to `poller.py`, `WorkingDirectory` set to
the project, and `RunAtLoad` and `KeepAlive` enabled. It runs after that macOS user
logs in, not before login. A background launch may require its own Bluetooth
permission: verify real updates after installing it. No startup service is
installed automatically by this repository. For MQTT, provide its password to
the background process through a private local mechanism; interactive shell
environment variables are not automatically inherited by LaunchAgents.

## Tests

```sh
.venv/bin/python -m unittest -v test_poller
.venv/bin/python test_readings.py --rounds 3 --interval 60
```

The hardware smoke test scans all nearby Airthings devices and writes private
JSONL results. Exit status 0 requires at least two discovered devices to respond
in every round; inspect their reported models to confirm the intended pair.
It does not publish to Home Assistant. Its upstream nonmetric mode leaves raw
temperature in Celsius; the test also records explicitly converted Fahrenheit.

## Privacy

`config.json`, raw readings, logs, state, local environments, and the upstream
checkout are excluded from Git. Never commit MQTT passwords, actual device
serial numbers, Bluetooth identifiers, or personal readings. Review staged files
before publishing. The collector sends data only to the configured MQTT broker;
it does not upload measurements to Airthings' cloud.
