"""Native macOS BLE collector with optional Home Assistant MQTT discovery."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import fcntl
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import signal
import threading
import time

from bleak import BleakScanner
from airthings_ble import AirthingsBluetoothDeviceData, parse_advertisement_data
import paho.mqtt.client as mqtt

LOG = logging.getLogger("airthings_poller")
# Source values use metric mode. Temperature and radon are converted here,
# explicitly, rather than relying on upstream is_metric's partial conversion.
MEASUREMENTS = {
    "temperature": ("Temperature", "°F", "temperature", "mdi:thermometer"),
    "humidity": ("Humidity", "%", "humidity", "mdi:water-percent"),
    "co2": ("CO₂", "ppm", "carbon_dioxide", "mdi:molecule-co2"),
    "voc": ("VOC", "ppb", "volatile_organic_compounds_parts", "mdi:air-filter"),
    "radon_1day_avg": ("Radon 1-day average", "pCi/L", None, "mdi:radioactive"),
    "radon_longterm_avg": ("Radon long-term average", "pCi/L", None, "mdi:radioactive"),
    "pressure": ("Pressure", "hPa", "atmospheric_pressure", "mdi:gauge"),
    "battery": ("Battery", "%", "battery", "mdi:battery"),
    "noise": ("Noise", "dB", "sound_pressure", "mdi:volume-high"),
    "lux": ("Illuminance", "lx", "illuminance", "mdi:brightness-5"),
}


def normalized(sensors):
    """Keep only supported finite measurements; never turn missing data into 0."""
    result = {}
    for key in MEASUREMENTS:
        value = sensors.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if not math.isfinite(value):
            continue
        if key == "temperature":
            value = round(value * 9 / 5 + 32, 2)
        elif key.startswith("radon_"):
            value = round(value / 37, 3)
        result[key] = value
    return result


def discovery(serial, name, model, key, stale_after):
    label, unit, device_class, icon = MEASUREMENTS[key]
    base = f"airthings_mac/{serial}"
    payload = {
        "name": label,
        "unique_id": f"airthings_mac_{serial}_{key}",
        "state_topic": f"{base}/state",
        "value_template": "{{ value_json." + key + " | default(none) }}",
        "unit_of_measurement": unit,
        "state_class": "measurement",
        "icon": icon,
        "expire_after": stale_after,
        "availability_mode": "all",
        "availability": [{"topic": "airthings_mac/availability"},
                         {"topic": f"{base}/availability"}],
        "device": {"identifiers": [f"airthings_mac_{serial}"],
                   "name": name, "manufacturer": "Airthings", "model": model},
    }
    if device_class:
        payload["device_class"] = device_class
    return f"homeassistant/sensor/airthings_mac_{serial}/{key}/config", payload


class Publisher:
    def __init__(self, config):
        self.connected = threading.Event()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                  client_id="airthings-mac-poller")
        password = os.environ.get(config.get("password_env", "AIRTHINGS_MQTT_PASSWORD"))
        if not password or not config.get("username"):
            raise ValueError("MQTT username and password environment variable are required")
        self.client.username_pw_set(config["username"], password)
        if config.get("tls"):
            self.client.tls_set()
        self.client.will_set("airthings_mac/availability", "offline", qos=1, retain=True)
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.reconnect_delay_set(2, 60)
        self.client.connect_async(config["host"], config.get("port", 1883), 60)
        self.client.loop_start()

    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            LOG.error("MQTT connection rejected: %s", reason_code)
            return
        self.connected.set()
        client.publish("airthings_mac/availability", "online", qos=1, retain=True)
        LOG.info("MQTT connected")

    def on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected.clear()
        LOG.warning("MQTT disconnected: %s", reason_code)

    def send(self, topic, payload, retain=False):
        if not self.connected.is_set():
            return False
        value = json.dumps(payload, allow_nan=False) if isinstance(payload, dict) else payload
        info = self.client.publish(topic, value, qos=1, retain=retain)
        return info.rc == mqtt.MQTT_ERR_SUCCESS

    def close(self):
        if self.connected.is_set():
            info = self.client.publish("airthings_mac/availability", "offline", qos=1, retain=True)
            try:
                info.wait_for_publish(timeout=5)
            except RuntimeError:
                pass
        self.client.disconnect()
        self.client.loop_stop()


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(json.dumps(state, indent=2, allow_nan=False) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


async def run(config, state_path, once=False):
    interval = max(60, int(config.get("interval", 300)))
    stale = int(config.get("stale_after", interval * 3))
    if stale < interval * 2:
        raise ValueError("stale_after must be at least twice interval")
    wanted = {str(device["serial"]): device for device in config["devices"]}
    if not wanted or any(not serial.isdigit() or len(serial) != 10 for serial in wanted):
        raise ValueError("Configure actual 10-digit Airthings serial numbers")
    publisher = Publisher(config["mqtt"]) if config.get("mqtt") else None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    readers = {serial: AirthingsBluetoothDeviceData(LOG, is_metric=True, max_attempts=2)
               for serial in wanted}
    snapshots = {}
    last_success = {}
    known_keys = {}
    try:
        while not stop.is_set():
            started = time.monotonic()
            found = {}
            successful = set()
            def on_advertisement(device, advertisement):
                parsed = parse_advertisement_data(advertisement.manufacturer_data.get(820),
                                                  advertisement.service_uuids)
                if parsed and parsed.serial_number in wanted:
                    found[parsed.serial_number] = (device, parsed)
            try:
                async with BleakScanner(detection_callback=on_advertisement):
                    await asyncio.sleep(config.get("scan_seconds", 20))
            except Exception as exc:
                LOG.warning("Bluetooth scan failed (%s): %s", type(exc).__name__, exc)
            for serial, entry in wanted.items():
                try:
                    if serial not in found:
                        raise RuntimeError("Device not seen in this scan")
                    device, parsed = found[serial]
                    if not parsed.should_poll:
                        raise RuntimeError("Device in SmartLink mode or stopped battery")
                    async with asyncio.timeout(90):
                        reading = await readers[serial].update_device(device)
                    values = normalized(reading.sensors)
                    if not values:
                        raise RuntimeError("No valid sensor measurements")
                    now = datetime.now(timezone.utc).isoformat()
                    snapshots[serial] = {"name": entry["name"], "model": reading.name,
                                         "collected_at": now, "values": values, "available": True}
                    successful.add(serial)
                    last_success[serial] = time.monotonic()
                    known_keys.setdefault(serial, set()).update(values)
                    LOG.info("Read %s: %d measurements", entry["name"], len(values))
                    if publisher:
                        # Replay discovery on every successful poll: idempotent and
                        # recovers from broker/HA restarts without retained old state.
                        for key in known_keys[serial]:
                            topic, payload = discovery(serial, entry["name"], reading.name, key, stale)
                            publisher.send(topic, payload, retain=True)
                        publisher.send(f"airthings_mac/{serial}/state", {**values, "collected_at": now})
                        publisher.send(f"airthings_mac/{serial}/availability", "online", retain=True)
                except Exception as exc:
                    LOG.warning("Read failed for %s (%s): %s", entry["name"], type(exc).__name__, exc)
                    if serial not in last_success or time.monotonic() - last_success[serial] >= stale:
                        snapshots.setdefault(serial, {"name": entry["name"]})["available"] = False
                        if publisher:
                            publisher.send(f"airthings_mac/{serial}/availability", "offline", retain=True)
                save_state(state_path, {"devices": snapshots})
            if once:
                return 0 if successful == set(wanted) else 1
            delay = max(1, interval - (time.monotonic() - started))
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass
    finally:
        if publisher:
            publisher.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--state", type=Path, default=Path(__file__).parent / "state/latest.json")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).parent
    (root / "logs").mkdir(exist_ok=True)
    (root / "state").mkdir(exist_ok=True)
    handler = RotatingFileHandler(root / "logs/poller.log", maxBytes=1_000_000, backupCount=3)
    logging.basicConfig(level=logging.INFO, handlers=[handler, logging.StreamHandler()],
                        format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("bleak", "bleak_retry_connector"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # Advisory process lock prevents overlapping Bluetooth polls from this installation.
    with (root / "state/poller.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOG.error("Another poller is already running")
            return 2
        return asyncio.run(run(json.loads(args.config.read_text()), args.state, args.once))


if __name__ == "__main__":
    raise SystemExit(main())
