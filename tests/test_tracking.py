"""Tracking protocols and fake-Frame BlueZ lifecycle; no headset or strap needed."""
import importlib.util
import math
import os
from pathlib import Path
import socket
import stat
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location("tracking", Path(__file__).resolve().parents[1] / "frame/tracking/tracking.py")
t = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(t)


class Protocols(unittest.TestCase):
    def test_hrs_formats(self):
        self.assertEqual(t.heart_rate(b"\x00\x48"), 72)
        self.assertEqual(t.heart_rate(b"\x01\x2c\x01"), 300)
        self.assertEqual(t.heart_rate(b"\x1e\x48\x01\x00\x00\x04\x00\x04"), 72)
        self.assertEqual(t.heart_rate(b"\x02\x48"), 72)  # contact not supported
        self.assertIsNone(t.heart_rate(b"\x04\x48"))  # no contact
        self.assertIsNone(t.heart_rate(b"\x00\x00"))

    def test_hrs_malformed(self):
        for packet in (b"", b"\x00", b"\x01\x48", b"\x08\x48\x00", b"\x10\x48",
                       b"\x10\x48\x00", b"\x00\x48\x01", b"\xe0\x48"):
            with self.subTest(packet=packet), self.assertRaises(ValueError):
                t.heart_rate(packet)

    def test_gaze_coordinates(self):
        self.assertEqual(t.gaze_angles([0, 0, 0, 1]), (0, 0))
        angle = math.radians(15)
        pitch, yaw = t.gaze_angles([math.sin(angle), 0, 0, math.cos(angle)])
        self.assertAlmostEqual(pitch, -30)  # OpenXR +X rotation looks up
        self.assertAlmostEqual(yaw, 0)
        pitch, yaw = t.gaze_angles([0, -math.sin(angle), 0, math.cos(angle)])
        self.assertAlmostEqual(pitch, 0)
        self.assertAlmostEqual(yaw, 30)  # right

    def test_invalid_gaze(self):
        for pose in ([0, 0, 0, 0], [math.nan, 0, 0, 1], [0, 0, 0], [0, 0, 0, math.inf]):
            with self.assertRaises(ValueError):
                t.gaze_angles(pose)

    def test_osc_wire(self):
        self.assertEqual(t.osc_message('/x', [72]), b'/x\0\0,i\0\0' + struct.pack('>i', 72))
        self.assertEqual(t.osc_message('/x', [1.0, -2.0]), b'/x\0\0,ff\0' + struct.pack('>ff', 1, -2))
        for address in ('x', '/x\0y', '/x y', '/x*'):
            with self.assertRaises(ValueError):
                t.osc_message(address, [1])

    def test_default_never_opens_socket(self):
        with patch.object(t.socket, 'socket') as create:
            osc = t.Osc()
            osc.send('/x', [72])
            osc.close()
            create.assert_not_called()

    def test_only_configured_endpoint(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
            receiver.bind(('127.0.0.1', 0))
            receiver.settimeout(1)
            osc = t.Osc(receiver.getsockname())
            try:
                osc.send('/tracking/eye/CenterPitchYaw', [0.0, 30.0])
                self.assertEqual(receiver.recv(1024), t.osc_message('/tracking/eye/CenterPitchYaw', [0.0, 30.0]))
            finally:
                osc.close()

    def test_endpoint_validation(self):
        for endpoint in [('example.org', 9000), ('0.0.0.0', 9000), ('224.0.0.1', 9000), ('127.0.0.1', 0), ('::1', 65536)]:
            with self.assertRaises(ValueError):
                t.Osc(endpoint)

    def test_heart_staleness_contact_and_log(self):
        now = [0]
        osc = Mock()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.csv'
            session = t.HeartSession(osc, '/hr', path, lambda: now[0])
            self.assertIsNone(session.current())
            session.notification(b'\x00\x48')
            self.assertEqual(session.current(), 72)
            osc.send.assert_called_once_with('/hr', [72])
            now[0] = 6
            self.assertIsNone(session.current())
            session.notification(b'\x04\x48')
            self.assertIsNone(session.current())
            self.assertEqual(osc.send.call_count, 1)
            session.close()
            self.assertEqual(path.read_text().splitlines()[0], 'unix_seconds,bpm')
            self.assertEqual(len(path.read_text().splitlines()), 2)
            if os.name != 'nt':
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(FileExistsError):
                t.HeartSession(osc, '/hr', path)

    def test_no_log_by_default(self):
        with patch.object(t.os, 'open') as create:
            session = t.HeartSession(Mock(), '/hr')
            session.notification(b'\x00\x48')
            session.close()
            create.assert_not_called()


class FakeBluez(unittest.TestCase):
    def setUp(self):
        self.device = '/org/bluez/hci0/dev_TEST'
        self.service = self.device + '/service1'
        self.char = self.service + '/char1'
        self.objects = {
            self.device: {t.DEVICE: {'Address': 'AA:BB:CC:DD:EE:FF', 'Connected': False, 'ServicesResolved': True}},
            self.service: {t.SERVICE: {'UUID': t.HRS, 'Device': self.device}},
            self.char: {t.CHARACTERISTIC: {'UUID': t.MEASUREMENT, 'Service': self.service, 'Flags': ['notify']}},
        }
        self.api = Mock()
        self.api.GetManagedObjects.side_effect = lambda: self.objects
        self.bus = Mock()
        self.interface = Mock(return_value=self.api)
        self.values = Mock()

    def reader(self):
        return t.BluezHeart(self.bus, self.interface, 'AA:BB:CC:DD:EE:FF', self.values)

    def test_subscribe_receive_and_cleanup(self):
        reader = self.reader()
        self.api.Connect.assert_called_once()
        self.assertTrue(reader.subscribe())
        self.api.StartNotify.assert_called_once()
        reader.changed(t.CHARACTERISTIC, {'Value': [0, 72]}, [], self.char)
        self.values.assert_called_once_with([0, 72])
        reader.changed(t.CHARACTERISTIC, {'Value': [0, 73]}, [], '/other/strap')
        self.assertEqual(self.values.call_count, 1)
        reader.close()
        self.api.StopNotify.assert_called_once()
        self.api.Disconnect.assert_called_once()
        self.bus.add_signal_receiver.return_value.remove.assert_called_once()

    def test_preserve_existing_connection(self):
        self.objects[self.device][t.DEVICE]['Connected'] = True
        reader = self.reader()
        reader.subscribe()
        reader.close()
        self.api.Connect.assert_not_called()
        self.api.Disconnect.assert_not_called()

    def test_only_selected_device_service(self):
        self.objects[self.service][t.SERVICE]['Device'] = '/other/device'
        reader = self.reader()
        try:
            with self.assertRaises(RuntimeError):
                reader.subscribe()
        finally:
            reader.close()
        self.api.StartNotify.assert_not_called()

    def test_wait_for_services(self):
        self.objects[self.device][t.DEVICE]['ServicesResolved'] = False
        reader = self.reader()
        self.assertFalse(reader.subscribe())
        reader.close()
        self.api.StartNotify.assert_not_called()
        self.api.StopNotify.assert_not_called()

    def test_disconnect_notification(self):
        reader = self.reader()
        reader.changed(t.DEVICE, {'Connected': 0}, [], self.device)  # dbus.Boolean behaves as int
        self.values.assert_called_once_with(None)
        reader.close()

    def test_failed_notify_cleans_connection(self):
        reader = self.reader()
        self.api.StartNotify.side_effect = RuntimeError('failure')
        with self.assertRaises(RuntimeError):
            reader.subscribe()
        reader.close()
        self.api.StopNotify.assert_not_called()
        self.api.Disconnect.assert_called_once()

    def test_unknown_device_does_not_connect_or_scan(self):
        self.objects.clear()
        with self.assertRaises(RuntimeError):
            self.reader()
        self.api.Connect.assert_not_called()
        self.api.StartDiscovery.assert_not_called()
