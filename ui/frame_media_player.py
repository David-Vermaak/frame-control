#!/usr/bin/env python3
"""Frame Control's local-media OpenVR player. Runs on the Frame, no third-party app.

SteamOS ffmpeg does hardware video decoding, scaling and audio output. OpenVR
owns only our screen and optional black surround. Exiting destroys both.
"""
import argparse
import ctypes as C
import json
import os
from pathlib import Path
import signal
import subprocess
import time

import frame_media
import frame_splat

LIB = '/opt/steamvr/bin/linuxarm64/libopenvr_api.so'
H = C.c_uint64
# Slots from Valve's openvr_capi.h, IVROverlay_028. Fail closed on another ABI.
SLOTS = {
    'CreateOverlay': (1, [C.c_char_p, C.c_char_p, C.POINTER(H)]),
    'DestroyOverlay': (3, [H]),
    'SetOverlayFlag': (11, [H, C.c_int, C.c_bool]),
    'SetOverlayAlpha': (16, [H, C.c_float]),
    'SetOverlayTexelAspect': (18, [H, C.c_float]),
    'SetOverlaySortOrder': (20, [H, C.c_uint32]),
    'SetOverlayWidthInMeters': (22, [H, C.c_float]),
    'SetOverlayTransformTrackedDeviceRelative': (35, [H, C.c_uint32, C.c_void_p]),
    'ShowOverlay': (43, [H]),
    'SetOverlayRaw': (62, [H, C.c_void_p, C.c_uint32, C.c_uint32, C.c_uint32]),
}


class Overlay:
    def __init__(self):
        self.handles = []
        self.vr = C.CDLL(LIB)
        self.vr.VR_InitInternal2.argtypes = [C.POINTER(C.c_int), C.c_int, C.c_char_p]
        self.vr.VR_GetGenericInterface.argtypes = [C.c_char_p, C.POINTER(C.c_int)]
        self.vr.VR_GetGenericInterface.restype = C.c_void_p
        err = C.c_int()
        self.vr.VR_InitInternal2(C.byref(err), 2, None)
        if err.value:
            raise RuntimeError('SteamVR init failed: %s' % err.value)
        ptr = self.vr.VR_GetGenericInterface(b'FnTable:IVROverlay_028', C.byref(err))
        if not ptr or err.value:
            self.vr.VR_ShutdownInternal()
            raise RuntimeError('SteamVR needs IVROverlay_028: %s' % err.value)
        self.table = C.cast(ptr, C.POINTER(C.c_void_p))

    def call(self, name, *values):
        slot, args = SLOTS[name]
        rc = C.CFUNCTYPE(C.c_int, *args)(self.table[slot])(*values)
        if rc:
            raise RuntimeError('OpenVR %s failed: %s' % (name, rc))

    def create(self, key, width, distance, stereo=False, aspect=1, order=1):
        handle = H()
        self.call('CreateOverlay', key.encode(), b'Frame Control media', C.byref(handle))
        self.handles.append(handle)
        self.call('SetOverlayWidthInMeters', handle, width)
        self.call('SetOverlaySortOrder', handle, order)
        self.call('SetOverlayTexelAspect', handle, aspect)
        if stereo:
            self.call('SetOverlayFlag', handle, 1024, True)  # SideBySide_Parallel
        matrix = (C.c_float * 12)(1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, -distance)
        self.call('SetOverlayTransformTrackedDeviceRelative', handle, 0, matrix)
        return handle

    def pixels(self, handle, data, width, height):
        buf = C.create_string_buffer(data)
        self.call('SetOverlayRaw', handle, buf, width, height, 4)
        self.call('ShowOverlay', handle)

    def close(self):
        try:
            for h in reversed(self.handles):
                self.call('DestroyOverlay', h)
        finally:
            self.vr.VR_ShutdownInternal()


def probe(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(path)],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError(result.stderr[-2000:] or 'Cannot read media')
    streams = json.loads(result.stdout)['streams']
    video = next((s for s in streams if s['codec_type'] == 'video'), None)
    if not video:
        raise ValueError('No image or video stream')
    return video, any(s['codec_type'] == 'audio' for s in streams)


def decoder_command(path, info, width, height, audio, photo=False):
    cmd = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error']
    if not photo:
        cmd += ['-re', '-readrate_initial_burst', '0']
        codec = {'h264': 'h264_v4l2m2m', 'hevc': 'hevc_v4l2m2m'}.get(info['codec_name'])
        if not codec:
            raise ValueError('Hardware playback currently supports H.264 and H.265 only')
        cmd += ['-c:v', codec]
    cmd += ['-i', str(path), '-map', '0:v:0', '-vf', 'scale=%s:%s' % (width, height),
            '-pix_fmt', 'rgba']
    if photo:
        cmd += ['-frames:v', '1']
    else:
        cmd += ['-r', '30']
    cmd += ['-f', 'rawvideo', 'pipe:1']
    if audio and not photo:
        cmd += ['-map', '0:a:0', '-f', 'pulse', 'Frame Control Media']
    return cmd


def write_status(path, **values):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(values))
    tmp.replace(path)


def play(args):
    path = Path(args.file).resolve(strict=True)
    status = Path(args.status)
    splat = path.suffix.lower() == '.splat'
    if splat:
        data, width, height = frame_splat.render(path)
        plan = frame_media.plan(path.name)
        aspect, photo, command = 1, True, None
    else:
        info, audio = probe(path)
        plan = frame_media.plan(path.name, args.layout, info.get('tags'))
        width, height, aspect = frame_media.geometry(info['width'], info['height'], plan['layout'])
        photo = plan['kind'] == 'photo'
        command = decoder_command(path, info, width, height, audio, photo)
    vr, proc, frames, started = None, None, 0, time.monotonic()
    # systemd sends SIGTERM to the whole unit, including ffmpeg. Python unwinds
    # ownership; no unrelated Steam/SteamVR process or setting is touched.
    def stop(signum, frame):
        raise InterruptedError('Stopped')
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        vr = Overlay()
        if args.theatre:
            surround = vr.create('framecontrol.media.surround', 40, 4, order=0)
            vr.call('SetOverlayAlpha', surround, .85)
            vr.pixels(surround, b'\x00\x00\x00\xff', 1, 1)
        screen = vr.create('framecontrol.media.screen', 3 if args.theatre else 1.6, 2,
                           plan['layout'] != 'mono', aspect)
        if splat:
            vr.pixels(screen, data, width, height)
            write_status(status, state='playing', file=path.name, frames=1, **plan)
            while True:
                time.sleep(1)
        proc = subprocess.Popen(command, stdout=subprocess.PIPE)
        video_start = time.monotonic()
        while True:
            data = proc.stdout.read(width * height * 4)
            if not data:
                break
            data, outw, outh = frame_media.stereo_pixels(data, width, height, plan['layout'])
            if not photo:
                time.sleep(max(0, video_start + frames/30 - time.monotonic()))
            vr.pixels(screen, data, outw, outh)
            frames += 1
            if frames == 1 or frames % 30 == 0:
                write_status(status, state='playing', file=path.name, frames=frames,
                             seconds=time.monotonic()-started, **plan)
        if not photo:
            time.sleep(max(0, video_start + frames/30 - time.monotonic()))
        rc = proc.wait(timeout=10)
        if rc:
            raise RuntimeError('ffmpeg exited %s; see media log' % rc)
        if not frames:
            raise RuntimeError('Decoder produced no frames')
        if photo:
            while True:
                time.sleep(1)
        write_status(status, state='ended', frames=frames, seconds=time.monotonic()-started)
    except InterruptedError:
        write_status(status, state='stopped', frames=frames)
    finally:
        if proc:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            proc.stdout.close()
        if vr:
            vr.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('file')
    ap.add_argument('--layout', choices=frame_media.LAYOUTS, default='auto')
    ap.add_argument('--theatre', action='store_true')
    ap.add_argument('--status', required=True)
    args = ap.parse_args()
    try:
        play(args)
    except Exception as e:
        write_status(Path(args.status), state='error', error=str(e))
        raise


if __name__ == '__main__':
    main()
