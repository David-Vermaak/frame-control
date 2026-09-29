"""Experimental pulse estimate from the Frame's IR eye-tracking cameras.

Skin brightens and darkens very slightly with each heartbeat as blood volume
changes (photoplethysmography). The eye cameras film the skin around each eye
under steady IR light at about 90 frames per second, so that rhythm may be
visible in the average brightness of small patches of skin.

Capture uses SteamVR's own `eyetracking --calib` mode, which writes PNG pairs
to /tmp. Each image is reduced to a grid of patch averages the moment it is
complete, then deleted; the capture directory is removed on exit. No image
leaves the Frame or outlives the run. This is an experiment, not a medical
measurement.
"""
from array import array
import bisect
import cmath
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

ET_DIR = Path("/opt/steamvr/tools/eyetracking")
ET_BIN = ET_DIR / "bin/linuxarm64/eyetracking"
ET_WEIGHTS = ET_DIR / "resources/et_dsp_20250610_03136.weights"
GRID = 16        # 16 × 16 patches of 25 × 25 pixels on the 400 × 400 image
RATE = 15.0      # analysis sample rate, Hz; the band of interest ends at 3 Hz
LOW, HIGH = 42.0, 180.0  # BPM search band
WINDOW = 15.0    # seconds per windowed estimate


# ---------------------------------------------------------------- capture ---

def grid_means(pixels, width, height, stride, channels, grid=GRID):
    """Average of channel 0 in each of grid × grid equal patches."""
    bw, bh = width // grid, height // grid
    sums = [0] * (grid * grid)
    for y in range(bh * grid):
        start = y * stride
        row = pixels[start:start + width * channels:channels]
        base = (y // bh) * grid
        for bx in range(grid):
            sums[base + bx] += sum(row[bx * bw:(bx + 1) * bw])
    area = bw * bh
    return [s / area for s in sums]


def load_grid(path):
    import gi
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf
    image = GdkPixbuf.Pixbuf.new_from_file(str(path))
    return grid_means(image.read_pixel_bytes().get_data(), image.get_width(), image.get_height(),
                      image.get_rowstride(), image.get_n_channels())


def reduce_file(loader, path):
    """Reduce one image to its patch grid and delete it, whatever happens."""
    try:
        return loader(path)
    finally:
        os.unlink(path)


class Capture:
    """Run SteamVR's eye-camera capture and reduce frames as they arrive."""
    NAME = re.compile(r"^(left|right)_(\d+)\.png$")
    # Only SteamVR's own capture directories are read and removed.
    PREFIX = "/tmp/etcalib_"
    # Printed by the capture tool; its output is only flushed when it exits.
    WRITING = re.compile(r"Writing capture to: (/tmp/etcalib_[\w-]+)")
    # About five seconds of frames (~65 MB in RAM-backed /tmp). If reduction
    # falls further behind than this, the capture stops rather than letting
    # eye images pile up.
    MAX_BACKLOG = 900

    def __init__(self, seconds, runner=subprocess.Popen, loader=load_grid, workers=3):
        self.seconds, self.runner, self.loader, self.workers = seconds, runner, loader, workers
        self.grids = {"left": {}, "right": {}}
        self.directory = None
        self.pool = None
        self.submitted = set()
        self.futures = {}
        self.new = set()  # every capture directory that appeared during our run

    def candidates(self):
        parent, stem = os.path.split(self.PREFIX)
        return {Path(entry.path) for entry in os.scandir(parent)
                if entry.name.startswith(stem) and entry.is_dir(follow_symlinks=False)}

    def run(self):
        # The capture tool's output goes to a private temporary file, read for
        # failure messages. It is block-buffered, so the capture directory is
        # found by watching for a new one rather than waiting for its name.
        import tempfile
        before = self.candidates()
        process = None
        with tempfile.TemporaryFile("w+") as log:
            try:
                if self.workers:
                    # SteamVR pins its eye tracker to cores 0-1; decoding runs beside it.
                    import concurrent.futures
                    import multiprocessing
                    self.pool = concurrent.futures.ProcessPoolExecutor(
                        self.workers, mp_context=multiprocessing.get_context("fork"))
                process = self.runner([str(ET_BIN), "-b", "CDSP", "-w", str(ET_WEIGHTS), "--calib", str(self.seconds)],
                                      cwd=str(ET_BIN.parent), stdout=log, stderr=subprocess.STDOUT)
                deadline = time.monotonic() + self.seconds + 30
                while True:
                    # Checked on every poll: a second capture directory means
                    # we can't tell which images are ours, so stop.
                    self.new |= self.candidates() - before
                    if len(self.new) > 1:
                        raise RuntimeError("another eye-camera capture is running")
                    if not self.directory and self.new:
                        self.directory = next(iter(self.new))
                    finished = process.poll() is not None
                    self.reduce(final=finished)
                    if finished:
                        break
                    if time.monotonic() > deadline:
                        raise RuntimeError("eye-camera capture did not finish")
                    time.sleep(0.02)
                log.seek(0)
                text = log.read()
                if process.returncode or not self.directory:
                    reason = "cameras unavailable" if "Failed to" in text else f"exit {process.returncode}"
                    raise RuntimeError(f"eye-camera capture failed ({reason})")
                named = self.written(log)
                if named and named != self.directory:
                    raise RuntimeError("the capture wrote somewhere else; not using those images")
                return self.frames()
            finally:
                # Each step runs even if an earlier one failed: eye images must
                # be removed whatever else went wrong.
                failed = False
                for step in (lambda: self.stop(process),
                             lambda: self.pool and self.pool.shutdown(wait=True, cancel_futures=True)):
                    try:
                        step()
                    except Exception:
                        failed = True
                named = self.written(log)
                if named:
                    self.new.add(named)  # ours by the tool's own account
                self.remove()
                if failed:
                    raise RuntimeError("the eye-camera capture did not stop cleanly")

    @staticmethod
    def stop(process):
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def written(self, log):
        """The directory the capture tool reported, once its output is flushed."""
        log.seek(0)
        match = self.WRITING.search(log.read())
        return Path(match.group(1)) if match else None

    def reduce(self, final=False):
        """Reduce and delete every complete image; an image is complete once a
        later one of the same eye exists, or the capture has ended."""
        if not self.directory or not self.directory.is_dir():
            return
        pending = {"left": [], "right": []}
        for entry in os.scandir(self.directory):
            match = self.NAME.match(entry.name)
            if match:
                pending[match.group(1)].append((int(match.group(2)), entry.path))
        if sum(len(files) for files in pending.values()) > self.MAX_BACKLOG:
            raise RuntimeError("eye-image processing fell behind; capture stopped")
        for eye, files in pending.items():
            files.sort()
            ready = files if final else files[:-1]
            for index, path in ready:
                if path in self.submitted:
                    continue
                self.submitted.add(path)
                if self.pool:
                    self.futures[(eye, index)] = self.pool.submit(reduce_file, self.loader, path)
                else:
                    self.grids[eye][index] = array("f", reduce_file(self.loader, path))
        for key, future in list(self.futures.items()):
            if final or future.done():
                self.grids[key[0]][key[1]] = array("f", future.result())
                del self.futures[key]

    def frames(self):
        """[(monotonic seconds, eye, grid)] joined with the capture metadata."""
        meta = json.loads((self.directory / "meta.json").read_text())
        frames = []
        for pair in meta["frames"]:
            for eye in ("left", "right"):
                info = pair.get(eye) or {}
                match = self.NAME.match(Path(info.get("fname", "")).name)
                grid = self.grids[eye].get(int(match.group(2))) if match else None
                if info.get("valid") and grid is not None:
                    frames.append((float(info["tsMono"]), eye, grid))
        return frames

    def remove(self):
        """Remove every capture directory that appeared during the run. Eye
        images must not outlive it, so a failure to delete is an error."""
        left = []
        for directory in self.new | ({self.directory} if self.directory else set()):
            if str(directory).startswith(self.PREFIX) and directory.is_dir() and not directory.is_symlink():
                try:
                    shutil.rmtree(directory)
                except OSError:
                    left.append(str(directory))
        if left:
            raise RuntimeError("could not delete eye images in " + ", ".join(sorted(left)))


# --------------------------------------------------------------- analysis ---

def fft(values):
    """In-place iterative radix-2 FFT of a list whose length is a power of 2."""
    n = len(values)
    a = list(values)
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            a[i], a[j] = a[j], a[i]
    size = 2
    while size <= n:
        step = cmath.exp(-2j * math.pi / size)
        half = size // 2
        for start in range(0, n, size):
            w = 1
            for k in range(start, start + half):
                t = w * a[k + half]
                a[k + half] = a[k] - t
                a[k] += t
                w *= step
        size *= 2
    return a


def resample(times, values, rate=RATE):
    """Average samples into 1/rate bins from the first sample; empty bins are
    filled from the previous bin."""
    if not times:
        return []
    start = times[0]
    count = int((times[-1] - start) * rate) + 1
    sums, counts = [0.0] * count, [0] * count
    for t, v in zip(times, values):
        i = min(int((t - start) * rate), count - 1)
        sums[i] += v
        counts[i] += 1
    out, last = [], None
    for s, c in zip(sums, counts):
        last = s / c if c else last
        out.append(last)
    first = next(v for v in out if v is not None)
    return [first if v is None else v for v in out]


def clean(signal, rate=RATE):
    """Relative change with slow drift removed; None if the patch is mostly
    blinks or eye movement. Spikes (blinks, saccades) become gaps at the
    local level rather than clipped steps, which would add false rhythm."""
    mean = sum(signal) / len(signal)
    x = [v / mean - 1 for v in signal]
    half = int(rate)  # 2-second centred moving median removes drift and blinks
    trend = []
    for i in range(len(x)):
        chunk = sorted(x[max(0, i - half):i + half + 1])
        trend.append(chunk[len(chunk) // 2])
    residual = [v - m for v, m in zip(x, trend)]
    mad = sorted(abs(v) for v in residual)[len(residual) // 2] or 1e-9
    limit = 4 * 1.4826 * mad
    spikes = sum(1 for v in residual if abs(v) > limit)
    if spikes > 0.05 * len(residual):
        return None
    return [v if abs(v) <= limit else 0.0 for v in residual]


def spectrum(signal, rate=RATE):
    """[(bpm, power)] within the search band, Hann-windowed and zero-padded."""
    n = len(signal)
    size = 1
    while size < max(n * 4, 256):
        size *= 2
    window = [0.5 - 0.5 * math.cos(2 * math.pi * i / (n - 1)) for i in range(n)] if n > 1 else [1.0]
    padded = [s * w for s, w in zip(signal, window)] + [0.0] * (size - n)
    result = fft(padded)
    out = []
    for k in range(size // 2):
        bpm = k * rate / size * 60
        if LOW <= bpm <= HIGH:
            out.append((bpm, abs(result[k]) ** 2))
    return out


def peak(spec):
    """Peak BPM with parabolic interpolation between spectral bins."""
    i = max(range(len(spec)), key=lambda k: spec[k][1])
    if 0 < i < len(spec) - 1:
        a, b, c = spec[i - 1][1], spec[i][1], spec[i + 1][1]
        denominator = a - 2 * b + c
        offset = 0.5 * (a - c) / denominator if denominator else 0.0
        return spec[i][0] + offset * (spec[1][0] - spec[0][0])
    return spec[i][0]


def snr(spec, bpm, width=4.0):
    """Power near the pulse and its first harmonic against the rest of the band."""
    near = sum(p for f, p in spec if abs(f - bpm) <= width or abs(f - 2 * bpm) <= width)
    rest = sum(p for f, p in spec) - near
    if near <= 0:
        return 0.0
    return near / rest if rest > 0 else float("inf")


def normalised(spec):
    total = sum(p for _, p in spec) or 1.0
    return [p / total for _, p in spec]


def usable(values):
    """Patches that are neither dark nor saturated for the whole recording."""
    mean = sum(values) / len(values)
    return 8 <= mean <= 245


def estimate(times, patches, rate=RATE, share=0.2, window=WINDOW):
    """Estimate pulse from patch brightness traces.

    times: monotonic seconds per frame. patches: {name: [brightness per frame]}.
    Selects the share of usable patches with the clearest periodic signal,
    combines their spectra and reports the overall and windowed estimates.
    """
    cleaned = {}
    for name, values in patches.items():
        if usable(values):
            signal = clean(resample(times, values, rate), rate)
            if signal is not None and any(signal):  # flat patches carry no rhythm
                cleaned[name] = signal
    if not cleaned or len(next(iter(cleaned.values()))) < rate * 8:
        raise ValueError("need at least 8 seconds of usable eye-camera frames")
    quality = []
    for name, signal in cleaned.items():
        spec = spectrum(signal, rate)
        own = peak(spec)
        quality.append((snr(spec, own), name, own, spec))
    quality.sort(reverse=True)
    chosen = quality[:max(4, int(len(quality) * share))]
    combined = [sum(values) for values in zip(*(normalised(spec) for _, _, _, spec in chosen))]
    bins = [f for f, _ in chosen[0][3]]
    bpm = peak(list(zip(bins, combined)))
    top = chosen[:8]
    agree = sum(1 for _, _, own, _ in top if abs(own - bpm) <= 5) / len(top)
    series = []
    samples = int(window * rate)
    length = len(next(iter(cleaned.values())))
    for end in range(samples, length + 1, int(rate)):
        specs = [spectrum(cleaned[name][end - samples:end], rate) for _, name, _, _ in chosen]
        total = [sum(values) for values in zip(*(normalised(s) for s in specs))]
        window_bins = [f for f, _ in specs[0]]
        series.append((times[0] + end / rate, peak(list(zip(window_bins, total)))))
    return {
        "bpm": bpm,
        "agreement": agree,
        "patches": len(chosen),
        "usable": len(cleaned),
        "snr": snr(list(zip(bins, combined)), bpm),
        "series": series,
    }


def analyse(frames):
    """Estimate from Capture.frames(); both eyes' patches are analysed together
    on a shared time base, each sample taken from that eye's nearest frame."""
    times = sorted({t for t, _, _ in frames})
    patches = {}
    for eye in ("left", "right"):
        eye_frames = sorted((t, grid) for t, e, grid in frames if e == eye)
        if not eye_frames:
            continue
        eye_times = [t for t, _ in eye_frames]
        nearest = []
        for t in times:
            i = bisect.bisect_left(eye_times, t)
            if i == len(eye_times) or (i > 0 and t - eye_times[i - 1] <= eye_times[i] - t):
                i -= 1
            nearest.append(i)
        for k in range(len(eye_frames[0][1])):
            patches[f"{eye}{k}"] = [eye_frames[i][1][k] for i in nearest]
    return estimate(times, patches)


def reliable(result):
    """Whether the estimate is clear enough to show as a reading. Thresholds
    are provisional until checked against a reference on a real wearer."""
    return result["agreement"] >= 0.75 and result["snr"] >= 0.5
