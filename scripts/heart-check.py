#!/usr/bin/env python3
"""Check Frame Control's heart-rate readings against a reference, on your computer.

  python3 scripts/heart-check.py listen --port 9000 --out ours.csv
  python3 scripts/heart-check.py compare ours.csv reference.csv
  python3 scripts/heart-check.py compare ours.csv ~/Downloads/export.zip

`listen` receives our integer-BPM OSC messages (send them here with
`tracking-on-frame.py heart --osc <this computer's IP> 9000`) and shows each
reading live, so you can watch it next to a reference such as your Apple
Watch. `--out` also records `unix_seconds,bpm` to a new private file.

`compare` lines up two recordings by time and reports how far apart they are.
The reference can be:
- a CSV whose first column is a time (unix seconds or ISO 8601) and whose
  second is BPM, such as our own log, `listen --out`, or the test strap's
  output (a third `flags` column marks skin-contact loss as "no reading");
- an Apple Health export (`export.zip` or `export.xml`, from Health → your
  profile → Export All Health Data). Only heart-rate records within the
  recording's time range are read.

Everything stays on this computer. Nothing is uploaded.
"""
import argparse
import bisect
import csv
from datetime import datetime
import io
import os
from pathlib import Path
import re
import socket
import struct
import sys
import time
import zipfile
from xml.etree import ElementTree

ADDRESS = "/avatar/parameters/HeartRate"


def parse_osc(packet):
    """Return (address, values) for a single OSC message with i/f arguments."""
    def string(offset):
        end = packet.index(b"\0", offset)
        return packet[offset:end].decode("utf-8"), (end + 4) & ~3
    address, offset = string(0)
    tags, offset = string(offset)
    if not tags.startswith(","):
        raise ValueError("not an OSC message")
    values = []
    for tag in tags[1:]:
        if tag not in "if" or offset + 4 > len(packet):
            raise ValueError("unsupported OSC argument")
        values.append(struct.unpack(">i" if tag == "i" else ">f", packet[offset:offset + 4])[0])
        offset += 4
    return address, values


def listen(port, address, out, seconds, clock=time.time, stream=sys.stdout):
    log = None
    if out:
        log = os.fdopen(os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w")
        log.write("unix_seconds,bpm\n")
    received = 0
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("0.0.0.0", port))
        sock.settimeout(0.5)
        print(f"Listening for {address} on UDP port {port}. Ctrl-C stops.", file=stream, flush=True)
        end = clock() + seconds if seconds else None
        try:
            while end is None or clock() < end:
                try:
                    packet = sock.recv(1024)
                except socket.timeout:
                    continue
                try:
                    path, values = parse_osc(packet)
                except (ValueError, UnicodeDecodeError):
                    continue
                if path != address or len(values) != 1:
                    continue
                now = clock()
                bpm = int(values[0])
                received += 1
                print(f"{time.strftime('%H:%M:%S', time.localtime(now))}  {bpm:3d} bpm", file=stream, flush=True)
                if log:
                    log.write(f"{now:.3f},{bpm}\n")
                    log.flush()
        except KeyboardInterrupt:
            pass
        finally:
            if log:
                log.close()
    return received


def parse_time(text):
    text = text.strip()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    # Apple Health uses "2026-09-29 09:12:03 +1000".
    text = re.sub(r" ([+-]\d{2}):?(\d{2})$", r"\1\2", text).replace("Z", "+0000")
    for pattern in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return datetime.strptime(text, pattern).timestamp()
        except ValueError:
            pass
    raise ValueError(f"unrecognised time {text!r}")


def read_csv(path):
    """[(time, bpm or None)], sorted. A header row is skipped."""
    samples = []
    with open(path, newline="") as source:
        for row in csv.reader(source):
            if len(row) < 2:
                continue
            try:
                when, bpm = parse_time(row[0]), int(float(row[1]))
            except ValueError:
                continue  # header or unparseable line
            if len(row) > 2 and row[2].strip():
                flags = int(row[2])
                if flags & 4 and not flags & 2:
                    bpm = None  # contact supported and not detected: no reading
            samples.append((when, bpm))
    return sorted(samples, key=lambda s: s[0])


def read_health(path, start, end):
    """Heart-rate records from an Apple Health export between start and end."""
    if zipfile.is_zipfile(path):
        archive = zipfile.ZipFile(path)
        name = next(n for n in archive.namelist() if n.endswith("/export.xml") or n == "export.xml")
        source = archive.open(name)
    else:
        source = open(path, "rb")
    samples = []
    with source:
        for _, element in ElementTree.iterparse(source):
            if element.tag == "Record" and element.get("type") == "HKQuantityTypeIdentifierHeartRate":
                when = parse_time(element.get("startDate"))
                if start <= when <= end:
                    samples.append((when, round(float(element.get("value")))))
            element.clear()
    return sorted(samples)


def read_any(path, start=None, end=None):
    path = str(path)
    if path.endswith((".zip", ".xml")):
        return read_health(path, start, end)
    return read_csv(path)


def value_at(samples, when, hold):
    """Our reading at a moment: the latest sample no older than `hold` seconds."""
    times = [s[0] for s in samples]
    i = bisect.bisect_right(times, when) - 1
    if i < 0 or when - times[i] > hold:
        return None
    return samples[i][1]


def compare(ours, reference, max_lag=10.0, hold=5.0):
    """Compare our readings with the reference at each reference moment.

    `lag` is the delay added to reference times before looking up ours (our
    readings arrive after the sensor's). The best lag within ±max_lag is used.
    """
    real = [(t, b) for t, b in reference if b is not None]
    if not real or not any(b is not None for _, b in ours):
        raise ValueError("both recordings need at least one reading")
    best = None
    steps = int(max_lag * 4)
    for step in range(-steps, steps + 1):
        lag = step / 4
        pairs = [(value_at(ours, t + lag, hold), b) for t, b in real]
        pairs = [(o, r) for o, r in pairs if o is not None]
        if not pairs:
            continue
        error = sum(abs(o - r) for o, r in pairs) / len(pairs)
        key = (-len(pairs), error, abs(lag))
        if best is None or key < best[0]:
            best = (key, lag, pairs)
    if best is None:
        raise ValueError("the recordings do not overlap in time")
    _, lag, pairs = best
    differences = [o - r for o, r in pairs]
    # While the reference says "no reading" (lost skin contact), we must not
    # produce new readings. Count ours that arrive during such a stretch.
    gaps = [t for t, b in reference if b is None]
    reference_times = [t for t, _ in reference]
    shown_in_gaps = 0
    for t, bpm in ours:
        i = bisect.bisect_right(reference_times, t - lag) - 1
        if bpm is not None and i >= 0 and reference[i][1] is None:
            shown_in_gaps += 1
    return {
        "reference_readings": len(real),
        "matched": len(pairs),
        "lag_seconds": lag,
        "mean_abs_error": sum(abs(d) for d in differences) / len(differences),
        "bias": sum(differences) / len(differences),
        "max_abs_error": max(abs(d) for d in differences),
        "within_5": sum(1 for d in differences if abs(d) <= 5) / len(differences),
        "exact": sum(1 for d in differences if d == 0) / len(differences),
        "no_contact_moments": len(gaps),
        "shown_during_no_contact": shown_in_gaps,
    }


def report(result, tolerance, stream=sys.stdout):
    print(f"Reference readings: {result['reference_readings']}, matched: {result['matched']}", file=stream)
    print(f"Best alignment: ours {result['lag_seconds']:+.2f} s after the reference", file=stream)
    print(f"Mean absolute difference: {result['mean_abs_error']:.2f} BPM "
          f"(bias {result['bias']:+.2f}, worst {result['max_abs_error']})", file=stream)
    print(f"Within ±5 BPM: {result['within_5']:.0%}; identical: {result['exact']:.0%}", file=stream)
    if result["no_contact_moments"]:
        print(f"No-contact moments: {result['no_contact_moments']}, where we showed a stale "
              f"reading: {result['shown_during_no_contact']}", file=stream)
    coverage = result["matched"] / result["reference_readings"]
    passed = (result["mean_abs_error"] <= tolerance and coverage >= 0.8
              and not result["shown_during_no_contact"])
    print(("PASS" if passed else "FAIL") + f" (mean difference ≤ {tolerance} BPM, ≥80% of reference "
          f"readings matched, nothing shown without contact)", file=stream)
    return passed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    heard = commands.add_parser("listen", help="show and optionally record our OSC readings live")
    heard.add_argument("--port", type=int, default=9000)
    heard.add_argument("--address", default=ADDRESS)
    heard.add_argument("--out", help="new private CSV file")
    heard.add_argument("--seconds", type=float, default=0, help="stop after this long (default: until Ctrl-C)")
    check = commands.add_parser("compare", help="compare our recording with a reference")
    check.add_argument("ours", type=Path)
    check.add_argument("reference", type=Path)
    check.add_argument("--tolerance", type=float, default=5.0, help="allowed mean difference in BPM (default 5)")
    check.add_argument("--max-lag", type=float, default=10.0, help="largest time offset to search, seconds")
    args = parser.parse_args(argv)
    if args.command == "listen":
        count = listen(args.port, args.address, args.out, args.seconds)
        print(f"Received {count} readings.")
        return 0 if count else 3
    ours = read_csv(args.ours)
    if not ours:
        parser.error("our recording has no readings")
    reference = read_any(args.reference, ours[0][0] - 60, ours[-1][0] + 60)
    try:
        result = compare(ours, reference, args.max_lag)
    except ValueError as error:
        print(str(error))
        return 1
    return 0 if report(result, args.tolerance) else 1


if __name__ == "__main__":
    raise SystemExit(main())
