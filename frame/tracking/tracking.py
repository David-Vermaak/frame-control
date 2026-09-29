#!/usr/bin/env python3
"""Frame-local tracking tools. No network destination or data log by default."""
import argparse
import math
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import time


class TrackingError(RuntimeError):
    """A safe, actionable status message containing no sensor data."""


HRS = "0000180d-0000-1000-8000-00805f9b34fb"
MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"
DEVICE = "org.bluez.Device1"
SERVICE = "org.bluez.GattService1"
CHARACTERISTIC = "org.bluez.GattCharacteristic1"


def heart_rate(data):
    """Validate the Bluetooth HRS measurement, returning BPM/contact only.

    Energy and RR intervals are checked for length but never retained.
    None means contact is supported and the strap reports no skin contact.
    """
    data = bytes(data)
    if len(data) < 2 or data[0] & 0xe0:
        raise ValueError("invalid HRS measurement")
    flags = data[0]
    size = 2 if flags & 1 else 1
    end = 1 + size + (2 if flags & 8 else 0)
    if len(data) < end:
        raise ValueError("truncated HRS measurement")
    extra = len(data) - end
    if (flags & 16 and (extra < 2 or extra % 2)) or (not flags & 16 and extra):
        raise ValueError("invalid HRS optional fields")
    if flags & 4 and not flags & 2:
        return None
    bpm = int.from_bytes(data[1:1 + size], "little")
    return bpm if bpm else None


def gaze_angles(quaternion):
    """OpenXR head-relative -Z forward → VRChat degrees, down/right positive."""
    if len(quaternion) != 4 or not all(math.isfinite(v) for v in quaternion):
        raise ValueError("invalid gaze orientation")
    norm = math.sqrt(sum(v * v for v in quaternion))
    if not 0.9 < norm < 1.1:
        raise ValueError("invalid gaze orientation")
    x, y, z, w = (v / norm for v in quaternion)
    # Rotate OpenXR's forward vector (0, 0, -1) into VIEW space.
    dx, dy, dz = -2 * (x*z + w*y), 2 * (w*x - y*z), 2 * (x*x + y*y) - 1
    return math.degrees(math.atan2(-dy, math.hypot(dx, dz))), math.degrees(math.atan2(dx, -dz))


def osc_message(address, values):
    if not address.startswith("/") or any(c.isspace() or c in '\0#*,?[]{}' for c in address):
        raise ValueError("OSC address must be a literal path")
    def string(value):
        encoded = value.encode("utf-8") + b"\0"
        return encoded + b"\0" * (-len(encoded) % 4)
    tags, payload = ",", b""
    for value in values:
        if type(value) is int:
            tags += "i"
            payload += struct.pack(">i", value)
        else:
            if not math.isfinite(value):
                raise ValueError("OSC value must be finite")
            tags += "f"
            payload += struct.pack(">f", value)
    return string(address) + string(tags) + payload


class Osc:
    def __init__(self, endpoint=None):
        self.sock = None
        self.target = None
        if endpoint:
            import ipaddress
            address = ipaddress.ip_address(endpoint[0])
            port = int(endpoint[1])
            if address.is_unspecified or address.is_multicast or not 1 <= port <= 65535:
                raise ValueError("OSC needs a unicast IP address and port 1..65535")
            self.sock = socket.socket(socket.AF_INET6 if address.version == 6 else socket.AF_INET, socket.SOCK_DGRAM)
            self.target = (str(address), port)

    def send(self, address, values):
        if self.sock:
            self.sock.sendto(osc_message(address, values), self.target)

    def close(self):
        if self.sock:
            self.sock.close()


class HeartSession:
    def __init__(self, osc, address, log=None, clock=time.monotonic):
        self.osc, self.address, self.clock = osc, address, clock
        self.bpm, self.updated = None, None
        self.log = None
        if log:
            # Exclusive creation refuses existing files and symlinks; mode is
            # private even with a permissive process umask.
            fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            self.log = os.fdopen(fd, "w")
            self.log.write("unix_seconds,bpm\n")

    def notification(self, data):
        self.bpm = heart_rate(data)
        self.updated = self.clock()
        if self.bpm is not None:
            self.osc.send(self.address, [self.bpm])
            if self.log:
                self.log.write(f"{time.time():.3f},{self.bpm}\n")
                self.log.flush()

    def current(self):
        if self.updated is None or self.clock() - self.updated > 5:
            return None
        return self.bpm

    def close(self):
        if self.log:
            self.log.close()


class BluezHeart:
    """One explicitly selected, already discovered strap; no ambient scan."""
    def __init__(self, bus, interface, address, on_value):
        self.bus, self.interface, self.on_value = bus, interface, on_value
        self.device = self.characteristic = None
        self.connected_here = False
        self.notifying = False
        self.match = None
        objects = self.objects()
        matches = [path for path, interfaces in objects.items()
                   if str(interfaces.get(DEVICE, {}).get("Address", "")).upper() == address.upper()]
        if len(matches) != 1:
            raise TrackingError("Strap not found uniquely in BlueZ; pair/discover it in SteamOS Bluetooth settings first")
        self.device = matches[0]
        self.match = bus.add_signal_receiver(self.changed, signal_name="PropertiesChanged",
                                            dbus_interface="org.freedesktop.DBus.Properties",
                                            bus_name="org.bluez", path_keyword="path")
        try:
            if not objects[self.device][DEVICE].get("Connected"):
                self.call(self.device, DEVICE).Connect(timeout=20)
                self.connected_here = True
        except Exception:
            self.close()
            raise

    def objects(self):
        return self.call("/", "org.freedesktop.DBus.ObjectManager").GetManagedObjects()

    def call(self, path, kind):
        return self.interface(self.bus.get_object("org.bluez", path), kind)

    def subscribe(self):
        objects = self.objects()
        if not objects.get(self.device, {}).get(DEVICE, {}).get("ServicesResolved"):
            return False
        services = {p for p, obj in objects.items() if str(obj.get(SERVICE, {}).get("UUID", "")).lower() == HRS
                    and obj[SERVICE].get("Device") == self.device}
        for path, obj in objects.items():
            props = obj.get(CHARACTERISTIC, {})
            if props.get("Service") in services and str(props.get("UUID", "")).lower() == MEASUREMENT:
                if "notify" not in props.get("Flags", []):
                    raise TrackingError("Heart-rate characteristic does not support notifications")
                self.characteristic = path
                self.call(path, CHARACTERISTIC).StartNotify()
                self.notifying = True
                return True
        raise TrackingError("Selected device has no standard Heart Rate Service measurement")

    def changed(self, kind, changes, invalidated, path=None):
        if kind == CHARACTERISTIC and path == self.characteristic and "Value" in changes:
            self.on_value(changes["Value"])
        elif kind == DEVICE and path == self.device and "Connected" in changes and not changes["Connected"]:
            self.on_value(None)

    def close(self):
        try:
            if self.notifying:
                self.call(self.characteristic, CHARACTERISTIC).StopNotify()
        finally:
            if self.match:
                self.match.remove()
            if self.connected_here:
                self.call(self.device, DEVICE).Disconnect()


def run_gaze(args, osc):
    binary = Path(__file__).with_name("gaze")
    command = [str(binary), "--seconds", str(args.seconds)]
    if not args.osc:
        return subprocess.call(command)
    read_fd, write_fd = os.pipe()
    process = None
    try:
        process = subprocess.Popen(command + ["--fd", str(write_fd)], pass_fds=(write_fd,))
        os.close(write_fd)
        write_fd = None
        with os.fdopen(read_fd) as source:
            read_fd = None
            for line in source:
                try:
                    angles = gaze_angles([float(v) for v in line.split()])
                except ValueError:
                    continue
                osc.send("/tracking/eye/CenterPitchYaw", angles)
        return process.wait()
    finally:
        if read_fd is not None:
            os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


class HeartPanel:
    """Our GTK panel, using the Frame's existing GTK4/GI platform libraries."""
    def __init__(self):
        os.environ["GDK_BACKEND"] = "x11"
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("GdkX11", "4.0")
        from gi.repository import Gtk, Gdk, GdkX11, GLib
        Gtk.init()
        self.running = True
        self.window = Gtk.Window(title="Frame Control · Heart rate")
        self.window.set_default_size(480, 320)
        self.window.connect("close-request", self.stop)
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)
        box.set_size_request(440, -1)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + side)(24)
        self.window.set_child(box)
        title = Gtk.Label(label="Heart rate")
        title.add_css_class("title-2")
        box.append(title)
        self.reading = Gtk.Label(label="—")
        self.reading.add_css_class("reading")
        box.append(self.reading)
        self.status = Gtk.Label(label="Waiting for strap")
        box.append(self.status)
        button = Gtk.Button(label="Stop")
        button.connect("clicked", self.stop)
        box.append(button)
        css = Gtk.CssProvider()
        css.load_from_data(b".reading { font-size: 144px; font-weight: 700; }")
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.window.present()
        context = GLib.MainContext.default()
        while context.pending():
            context.iteration(False)
        try:
            xid = GdkX11.X11Surface.get_xid(self.window.get_surface())
            subprocess.run(["xprop", "-id", str(xid), "-f", "STEAM_GAME", "32c", "-set", "STEAM_GAME", "2000000027"],
                           check=True, stdout=subprocess.DEVNULL)
        except Exception:
            self.window.destroy()
            raise

    def stop(self, *args):
        self.running = False
        return True

    def update(self, bpm):
        self.reading.set_label(str(bpm) if bpm is not None else "—")
        self.status.set_label("beats per minute" if bpm is not None else "Waiting for strap")

    def close(self):
        self.window.destroy()


def run_heart(args, osc):
    import dbus
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib
    DBusGMainLoop(set_as_default=True)
    session = HeartSession(osc, args.address, args.log)
    reader, root = None, None
    failure = []
    def value(data):
        if data is None:
            session.bpm = None
            failure.append("Strap disconnected; reconnect and start again")
            return
        try:
            session.notification(data)
        except ValueError:
            session.bpm = None
        except OSError:
            failure.append("OSC or session log write failed")
    try:
        reader = BluezHeart(dbus.SystemBus(), dbus.Interface, args.device, value)
        context = GLib.MainContext.default()
        deadline = time.monotonic() + 20
        while not reader.subscribe():
            if time.monotonic() > deadline:
                raise TrackingError("Timed out waiting for the strap's GATT services")
            while context.pending():
                context.iteration(False)
            time.sleep(0.1)
        end = time.monotonic() + args.seconds
        print("Heart-rate notifications started; readings stay local unless OSC or a log was selected.")
        if args.panel:
            root = HeartPanel()
        running = lambda: root.running if root else True
        while running() and time.monotonic() < end and not failure:
            while context.pending():
                context.iteration(False)
            if root:
                root.update(session.current())
            time.sleep(0.05)
        if failure:
            raise TrackingError(failure[0])
        return 0
    finally:
        if root:
            root.close()
        try:
            if reader:
                reader.close()
        finally:
            session.close()


def run_pulse(args, osc):
    """Experimental: estimate pulse from the IR eye cameras (see pulse.py)."""
    import pulse
    if not pulse.ET_BIN.exists():
        raise TrackingError("SteamVR's eye-tracking tool is not installed on this Frame")
    print(f"Capturing {args.seconds} s from the eye cameras. Wear the headset and keep still.", flush=True)
    readings = [pulse.proximity()]
    try:
        frames = pulse.Capture(args.seconds).run()
        readings.append(pulse.proximity())
    except RuntimeError as error:  # capture status only; no image data
        raise TrackingError(str(error))
    print(f"Captured {len(frames)} eye frames; images already deleted. Analysing...", flush=True)
    try:
        result = pulse.analyse(frames)
    except ValueError as error:
        raise TrackingError(str(error))
    on_face = all(pulse.worn(reading) for reading in readings)
    if not on_face:
        # Unworn, the cameras still show a steady periodic artifact that looks
        # like a pulse (verified on the Frame), so it is never called clear.
        print("The proximity sensor says the headset is not being worn; no pulse can be read.")
    clear = on_face and pulse.reliable(result)
    offset = time.time() - time.monotonic()  # the capture's timestamps are CLOCK_MONOTONIC
    if args.log:
        fd = os.open(args.log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as log:
            log.write("unix_seconds,bpm\n")
            for when, bpm in result["series"]:
                log.write(f"{when + offset:.3f},{bpm:.1f}\n")
    if clear:
        osc.send(args.address, [round(result["bpm"])])
    if args.show:
        print(f"Estimate: {result['bpm']:.1f} BPM ({'clear' if clear else 'NOT clear'}; "
              f"patch agreement {result['agreement']:.0%}, signal/noise {result['snr']:.2f}, "
              f"{result['patches']} of {result['usable']} usable patches)")
        print("Per-second estimates (15 s windows): "
              + " ".join(str(round(bpm)) for _, bpm in result["series"]))
    else:
        print("A clear pulse was found." if clear else "No clear pulse was found.")
    return 0 if clear else 3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    gaze = commands.add_parser("gaze", help="headless OpenXR; prints counters only without --osc")
    heart = commands.add_parser("heart", help="standard BLE HRS from an explicitly selected strap")
    pulse = commands.add_parser("pulse", help="experimental pulse estimate from the IR eye cameras")
    for command in (gaze, heart, pulse):
        command.add_argument("--osc", nargs=2, metavar=("IP", "PORT"), help="explicit UDP destination; no default")
    for command in (gaze, heart):
        command.add_argument("--seconds", type=int, default=10, help="bounded run, 1..86400 seconds (default: 10)")
    pulse.add_argument("--seconds", type=int, default=60, help="capture length, 20..300 seconds (default: 60)")
    heart.add_argument("--device", required=True, help="strap Bluetooth address already discovered by BlueZ")
    heart.add_argument("--panel", action="store_true", help="show our panel on gamescope DISPLAY=:0")
    for command in (heart, pulse):
        command.add_argument("--address", default="/avatar/parameters/HeartRate", help="integer BPM OSC parameter")
        command.add_argument("--log", help="new private CSV file; disabled by default")
    pulse.add_argument("--show", action="store_true", help="print the estimate and per-second series")
    args = parser.parse_args()
    if not 1 <= args.seconds <= 86400:
        parser.error("--seconds must be 1..86400")
    if args.command == "pulse" and not 20 <= args.seconds <= 300:
        parser.error("pulse --seconds must be 20..300")
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, interrupted)
    osc = None
    try:
        if args.command in ("heart", "pulse"):
            osc_message(args.address, [0])
            if getattr(args, "panel", False):
                os.environ["DISPLAY"] = ":0"
        osc = Osc(args.osc)
        run = {"gaze": run_gaze, "heart": run_heart, "pulse": run_pulse}[args.command]
        return run(args, osc)
    except TrackingError as error:
        print(str(error))
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        # Never dump notifications, gaze, BLE addresses or exception payloads.
        print(f"Tracking stopped ({type(error).__name__}). Check the device, runtime and selected output.")
        return 1
    finally:
        if osc:
            osc.close()


if __name__ == "__main__":
    raise SystemExit(main())
