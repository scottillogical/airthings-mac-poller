"""Launch collector with an optional password from a private, ignored local file."""
import os
from pathlib import Path
import stat
import sys

root = Path(__file__).resolve().parent
password_file = root / 'mqtt-password.txt'
if password_file.exists():
    if stat.S_IMODE(password_file.stat().st_mode) & 0o077:
        raise SystemExit('mqtt-password.txt must be private: chmod 600 mqtt-password.txt')
    password = password_file.read_text().strip()
    if password:
        os.environ['AIRTHINGS_MQTT_PASSWORD'] = password
os.execv(sys.executable, [sys.executable, str(root / 'poller.py'), *sys.argv[1:]])
