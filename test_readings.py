"""Read-only hardware smoke test using Airthings' official BLE library."""
import argparse
import asyncio
import dataclasses
from datetime import datetime, timezone
from enum import Enum
import json
import logging
from pathlib import Path

from bleak import BleakScanner
from airthings_ble import AirthingsBluetoothDeviceData, parse_advertisement_data


def encode(value):
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if isinstance(value, Enum):
        return value.value
    return str(value)


async def main(args):
    output = Path(__file__).parent / "results"
    output.mkdir(exist_ok=True)
    path = output / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".jsonl")
    def record(event, **data):
        item = {"time": datetime.now(timezone.utc).isoformat(), "event": event, **data}
        line = json.dumps(item, default=encode)
        print(line, flush=True)
        with path.open("a") as stream:
            stream.write(line + "\n")

    devices = {}
    def found(device, advertisement):
        parsed = parse_advertisement_data(
            advertisement.manufacturer_data.get(820), advertisement.service_uuids
        )
        if parsed is not None:
            devices[device.address] = (device, parsed)

    record("scan_started", seconds=args.scan_seconds)
    try:
        async with BleakScanner(detection_callback=found):
            await asyncio.sleep(args.scan_seconds)
    except Exception as exc:
        record("scan_failed", error_type=type(exc).__name__, error=str(exc))
        return 2
    for device, parsed in devices.values():
        record("device_discovered", address=device.address, advertisement=parsed)
    if not devices:
        record("no_airthings_devices")
        return 3

    successes = {address: 0 for address in devices}
    readers = {
        address: AirthingsBluetoothDeviceData(logging.getLogger("airthings"),
                                             is_metric=False, max_attempts=2)
        for address in devices
    }
    for round_index in range(args.rounds):
        for address, (device, parsed) in devices.items():
            if not parsed.should_poll:
                record("device_skipped", address=address, reason="SmartLink or stopped battery")
                continue
            try:
                async with asyncio.timeout(90):
                    result = await readers[address].update_device(device)
                if not result.sensors:
                    raise RuntimeError("Device returned no sensor values")
                successes[address] += 1
                temp_c = result.sensors.get("temperature")
                record("read_success", round=round_index + 1, device=result,
                       temperature_f=(round(temp_c * 9 / 5 + 32, 2)
                                      if isinstance(temp_c, (int, float)) else None),
                       units={"device.sensors.temperature": "C", "temperature_f": "F",
                              "radon": "pCi/L", "humidity": "%", "co2": "ppm"})
            except Exception as exc:
                record("read_failed", round=round_index + 1, address=address,
                       error_type=type(exc).__name__, error=str(exc))
        if round_index + 1 < args.rounds:
            await asyncio.sleep(args.interval)
    passed = len(devices) >= 2 and all(n == args.rounds for n in successes.values())
    record("summary", successes=successes, rounds=args.rounds,
           both_devices_repeatedly_read=passed,
           note="Successful polls establish transport reliability, not new measurement cadence")
    return 0 if passed else 4


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--scan-seconds", type=int, default=30)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--interval", type=int, default=60)
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    raise SystemExit(asyncio.run(main(arguments)))
