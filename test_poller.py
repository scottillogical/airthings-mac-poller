"""Offline checks for data conversion, MQTT metadata, and private state output."""
import json
from pathlib import Path
import tempfile
import unittest
from poller import normalized, discovery, save_state


class PollerTests(unittest.TestCase):
    def test_metric_conversion(self):
        self.assertEqual(normalized({'temperature': 20, 'radon_1day_avg': 37,
                                     'humidity': 51.5, 'co2': 750, 'voc': 139}),
                         {'temperature': 68.0, 'radon_1day_avg': 1.0,
                          'humidity': 51.5, 'co2': 750, 'voc': 139})

    def test_missing_and_nonfinite_are_not_zero(self):
        self.assertEqual(normalized({'temperature': None, 'co2': float('nan'),
                                     'voc': float('inf'), 'humidity': True,
                                     'illuminance': 0}), {})
        self.assertEqual(normalized({'co2': 0}), {'co2': 0})

    def test_discovery_stable_identity_and_units(self):
        args = ('0000000000', 'Example device', 'Wave Radon', 'radon_1day_avg', 900)
        topic, payload = discovery(*args)
        self.assertTrue(topic.startswith('homeassistant/sensor/'))
        self.assertEqual(payload['unit_of_measurement'], 'pCi/L')
        self.assertNotIn('device_class', payload)
        self.assertEqual(payload['expire_after'], 900)
        self.assertEqual(payload['availability_mode'], 'all')
        changed = discovery('0000000000', 'Renamed device', *args[2:])[1]
        self.assertEqual(payload['unique_id'], changed['unique_id'])
        self.assertEqual(payload['device']['identifiers'], changed['device']['identifiers'])
        self.assertEqual(discovery(*args[:3], 'temperature', 900)[1]['unit_of_measurement'], '°F')

    def test_private_atomic_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'nested/latest.json'
            save_state(path, {'devices': {}})
            self.assertEqual(json.loads(path.read_text()), {'devices': {}})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertFalse(path.with_suffix('.tmp').exists())


if __name__ == '__main__':
    unittest.main()
