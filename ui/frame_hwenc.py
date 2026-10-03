"""Runs ON the Frame: H.264 in hardware, on the Snapdragon's video encoder.

    ffmpeg ... -vf scale=out_color_matrix=bt709,format=yuv420p -f yuv4mpegpipe - \
        | python3 frame_hwenc.py FPS BITRATE

Reads YUV4MPEG2 on stdin and writes H.264 (Annex B) on stdout, one access unit
per frame, each led by an access unit delimiter and every keyframe carrying
SPS/PPS: the stream ui/index.html splits and decodes, as libx264 made it.

The encoder is qcom-iris-encoder (/dev/video23), a V4L2 stateful mem2mem
device. ffmpeg's h264_v4l2m2m deadlocks against it (tried 2026-10-03, SteamOS
0.4.3), so this drives it directly: ctypes for the ioctls, mmap for the
buffers. Stdlib only, nothing installed. The driver takes NV12, so the 4:2:0
planes are interleaved here; that and copying rows to the driver's stride are
the only per-pixel work, done in C by slice assignment. The stream is tagged
BT.709 limited range, so ffmpeg must convert with that matrix (its default is
BT.601: greens come out wrong). The driver also takes RGBA (AB24) and converts
it itself, but to BT.709 *full* range while still tagging it limited, which
crushes blacks and oversaturates; it isn't used.

Driver behaviour this relies on (seen on kernel 6.18, and in FrameMate,
github.com/nailuj05/framemate): set the coded format before the raw one; the
raw height comes back aligned (1080 -> 1088) and the SPS crops it; buffer
timestamps must increase or rate control undershoots; never STREAMOFF a queue
that wasn't started (EBUSY).

Exit status 3: the encoder couldn't be set up (the server falls back to libx264).
"""
import ctypes as C
import errno
import mmap
import os
import select
import struct
import sys

DEVICE = "/dev/video23"
BUFFERS = 4
STALL = 2.0  # seconds the encoder may hold every buffer before we give up

# <linux/videodev2.h>
TYPE_CAPTURE_MPLANE, TYPE_OUTPUT_MPLANE = 9, 10
MEMORY_MMAP = 1
FIELD_NONE = 1
COLORSPACE_REC709, YCBCR_ENC_709, XFER_FUNC_709, QUANTIZATION_LIM_RANGE = 3, 2, 1, 2
FLAG_LAST = 0x00100000


def fourcc(s):
    return struct.unpack("<I", s.encode())[0]


H264, NV12 = fourcc("H264"), fourcc("NV12")

# Codec controls (V4L2_CID_CODEC_BASE + n), as `v4l2-ctl -d /dev/video23 -L` lists them.
CID_B_FRAMES = 0x009909CA
CID_GOP_SIZE = 0x009909CB
CID_BITRATE_MODE = 0x009909CE  # 1 = constant
CID_BITRATE = 0x009909CF
CID_PEAK_BITRATE = 0x009909D0
CID_HEADER_MODE = 0x009909D8  # 1 = SPS/PPS joined with the first frame
CID_PREPEND_SPSPPS_TO_IDR = 0x00990B84
CID_H264_PROFILE = 0x00990A6B  # 4 = High


class PlanePixFormat(C.Structure):
    _fields_ = [("sizeimage", C.c_uint32), ("bytesperline", C.c_uint32), ("reserved", C.c_uint16 * 6)]


class PixFormatMplane(C.Structure):
    _pack_ = 1
    _fields_ = [("width", C.c_uint32), ("height", C.c_uint32), ("pixelformat", C.c_uint32),
                ("field", C.c_uint32), ("colorspace", C.c_uint32), ("plane_fmt", PlanePixFormat * 8),
                ("num_planes", C.c_uint8), ("flags", C.c_uint8), ("ycbcr_enc", C.c_uint8),
                ("quantization", C.c_uint8), ("xfer_func", C.c_uint8), ("reserved", C.c_uint8 * 7)]


class FormatUnion(C.Union):
    # v4l2_window holds pointers, so the kernel's union is 8-aligned: hence _align.
    _fields_ = [("pix_mp", PixFormatMplane), ("raw", C.c_uint8 * 200), ("_align", C.c_uint64)]


class Format(C.Structure):
    _fields_ = [("type", C.c_uint32), ("fmt", FormatUnion)]


class RequestBuffers(C.Structure):
    _fields_ = [("count", C.c_uint32), ("type", C.c_uint32), ("memory", C.c_uint32),
                ("capabilities", C.c_uint32), ("flags", C.c_uint8), ("reserved", C.c_uint8 * 3)]


class PlaneM(C.Union):
    _fields_ = [("mem_offset", C.c_uint32), ("userptr", C.c_ulong), ("fd", C.c_int32)]


class Plane(C.Structure):
    _fields_ = [("bytesused", C.c_uint32), ("length", C.c_uint32), ("m", PlaneM),
                ("data_offset", C.c_uint32), ("reserved", C.c_uint32 * 11)]


class Timeval(C.Structure):
    _fields_ = [("sec", C.c_long), ("usec", C.c_long)]


class Timecode(C.Structure):
    _fields_ = [("type", C.c_uint32), ("flags", C.c_uint32), ("frames", C.c_uint8), ("seconds", C.c_uint8),
                ("minutes", C.c_uint8), ("hours", C.c_uint8), ("userbits", C.c_uint8 * 4)]


class BufferM(C.Union):
    _fields_ = [("offset", C.c_uint32), ("userptr", C.c_ulong), ("planes", C.POINTER(Plane)), ("fd", C.c_int32)]


class Buffer(C.Structure):
    _fields_ = [("index", C.c_uint32), ("type", C.c_uint32), ("bytesused", C.c_uint32), ("flags", C.c_uint32),
                ("field", C.c_uint32), ("timestamp", Timeval), ("timecode", Timecode), ("sequence", C.c_uint32),
                ("memory", C.c_uint32), ("m", BufferM), ("length", C.c_uint32), ("reserved2", C.c_uint32),
                ("request_fd", C.c_int32)]


class Control(C.Structure):
    _fields_ = [("id", C.c_uint32), ("value", C.c_int32)]


class StreamParm(C.Structure):
    # v4l2_outputparm: capability, outputmode, then timeperframe (numerator, denominator).
    _fields_ = [("type", C.c_uint32), ("capability", C.c_uint32), ("outputmode", C.c_uint32),
                ("numerator", C.c_uint32), ("denominator", C.c_uint32), ("raw", C.c_uint8 * 184)]


def _ioc(direction, nr, size):
    return direction << 30 | size << 16 | ord("V") << 8 | nr


RW, W = 3, 1
VIDIOC_S_FMT = _ioc(RW, 5, C.sizeof(Format))
VIDIOC_REQBUFS = _ioc(RW, 8, C.sizeof(RequestBuffers))
VIDIOC_QUERYBUF = _ioc(RW, 9, C.sizeof(Buffer))
VIDIOC_QBUF = _ioc(RW, 15, C.sizeof(Buffer))
VIDIOC_DQBUF = _ioc(RW, 17, C.sizeof(Buffer))
VIDIOC_STREAMON = _ioc(W, 18, C.sizeof(C.c_int))
VIDIOC_STREAMOFF = _ioc(W, 19, C.sizeof(C.c_int))
VIDIOC_S_PARM = _ioc(RW, 22, C.sizeof(StreamParm))
VIDIOC_S_CTRL = _ioc(RW, 28, C.sizeof(Control))

_libc = None


def ioctl(fd, request, arg):
    global _libc
    if _libc is None:  # loaded on first use, so the tests can import this anywhere
        _libc = C.CDLL(None, use_errno=True)
        _libc.ioctl.argtypes = [C.c_int, C.c_ulong, C.c_void_p]
    while _libc.ioctl(fd, request, C.byref(arg)) < 0:
        e = C.get_errno()
        if e != errno.EINTR:
            raise OSError(e, os.strerror(e))


class Setup(Exception):
    """The encoder couldn't be configured: exit 3 so the server uses libx264."""


AUD = b"\x00\x00\x00\x01\x09\xf0"  # access unit delimiter, any slice type


def nal_units(data):
    """(type, start) of each NAL unit after a 00 00 01 start code."""
    out, i = [], data.find(b"\x00\x00\x01")
    while 0 <= i < len(data) - 3:
        out.append((data[i + 3] & 0x1F, i))
        i = data.find(b"\x00\x00\x01", i + 3)
    return out


class Framer:
    """Encoder output -> one AUD-led access unit per picture, SPS/PPS on every IDR.

    The driver may hand back SPS/PPS as a buffer of their own and may not repeat
    them on later keyframes; both are covered here."""

    def __init__(self):
        self.headers = b""   # latest SPS+PPS
        self.pending = b""   # headers waiting for their picture

    def __call__(self, data):
        units = nal_units(data)
        types = [t for t, _ in units]
        if 7 in types:
            # From the SPS's start code to the first slice's, 4-byte start codes included.
            def start(i):
                return i - 1 if i and data[i - 1] == 0 else i
            first_slice = next((s for t, s in units if t in (1, 5)), len(data))
            self.headers = data[start(next(s for t, s in units if t == 7)):start(first_slice)]
        if not any(t in (1, 5) for t in types):
            self.pending += data
            return None
        if 5 in types and 7 not in types and not self.pending:
            data = self.headers + data
        out = AUD + self.pending + data
        self.pending = b""
        return out


class Encoder:
    def __init__(self, width, height, fps, bitrate):
        try:
            self.fd = os.open(DEVICE, os.O_RDWR | os.O_NONBLOCK)
        except OSError as e:
            raise Setup(f"{DEVICE}: {e.strerror}")
        self.started = []
        try:
            self._configure(width, height, fps, bitrate)
        except OSError as e:
            self.close()
            raise Setup(f"configuring the encoder: {e}")

    def _set(self, cid, value):
        try:
            ioctl(self.fd, VIDIOC_S_CTRL, Control(cid, value))
        except OSError as e:
            print(f"frame_hwenc: control {cid:#x}={value}: {e}", file=sys.stderr)

    def _configure(self, width, height, fps, bitrate):
        coded = Format(type=TYPE_CAPTURE_MPLANE)
        coded.fmt.pix_mp.width, coded.fmt.pix_mp.height = width, height
        coded.fmt.pix_mp.pixelformat, coded.fmt.pix_mp.num_planes = H264, 1
        ioctl(self.fd, VIDIOC_S_FMT, coded)
        raw = Format(type=TYPE_OUTPUT_MPLANE)
        p = raw.fmt.pix_mp
        p.width, p.height, p.pixelformat, p.field = width, height, NV12, FIELD_NONE
        # All four, or the driver writes "reserved" matrix and transfer into the
        # SPS, which ffmpeg refuses to convert (seen 2026-10-03).
        p.colorspace, p.ycbcr_enc, p.xfer_func, p.quantization = (COLORSPACE_REC709, YCBCR_ENC_709,
                                                                  XFER_FUNC_709, QUANTIZATION_LIM_RANGE)
        p.num_planes = 1
        ioctl(self.fd, VIDIOC_S_FMT, raw)
        if p.pixelformat != NV12 or p.width < width or p.height < height:
            raise OSError(errno.EINVAL, f"encoder offered {p.width}x{p.height} {p.pixelformat:#x}")
        self.width, self.height = width, height
        self.stride = p.plane_fmt[0].bytesperline
        self.chroma = self.stride * p.height  # NV12's UV plane follows the aligned luma plane
        self.sizeimage = p.plane_fmt[0].sizeimage

        try:
            ioctl(self.fd, VIDIOC_S_PARM, StreamParm(type=TYPE_OUTPUT_MPLANE, numerator=1, denominator=fps))
        except OSError as e:
            print(f"frame_hwenc: frame rate: {e}", file=sys.stderr)
        for cid, value in ((CID_BITRATE_MODE, 1), (CID_BITRATE, bitrate), (CID_PEAK_BITRATE, bitrate),
                           (CID_GOP_SIZE, fps * 2), (CID_B_FRAMES, 0), (CID_H264_PROFILE, 4),
                           (CID_HEADER_MODE, 1), (CID_PREPEND_SPSPPS_TO_IDR, 1)):
            self._set(cid, value)

        self.inputs = self._buffers(TYPE_OUTPUT_MPLANE)
        self.outputs = self._buffers(TYPE_CAPTURE_MPLANE)
        self.free = list(range(len(self.inputs)))
        for i in range(len(self.outputs)):
            self._queue(TYPE_CAPTURE_MPLANE, i, 0, 0)
        for kind in (TYPE_OUTPUT_MPLANE, TYPE_CAPTURE_MPLANE):
            ioctl(self.fd, VIDIOC_STREAMON, C.c_int(kind))
            self.started.append(kind)

    def _buffers(self, kind):
        req = RequestBuffers(count=BUFFERS, type=kind, memory=MEMORY_MMAP)
        ioctl(self.fd, VIDIOC_REQBUFS, req)
        maps = []
        for i in range(req.count):
            plane = Plane()
            buf = Buffer(index=i, type=kind, memory=MEMORY_MMAP, length=1)
            buf.m.planes = C.pointer(plane)
            ioctl(self.fd, VIDIOC_QUERYBUF, buf)
            maps.append(mmap.mmap(self.fd, plane.length, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE,
                                  offset=plane.m.mem_offset))
        if not maps:
            raise OSError(errno.ENOMEM, "the encoder gave no buffers")
        return maps

    def _queue(self, kind, index, used, usec):
        plane = Plane(bytesused=used, length=len((self.inputs if kind == TYPE_OUTPUT_MPLANE else self.outputs)[index]))
        buf = Buffer(index=index, type=kind, memory=MEMORY_MMAP, length=1, field=FIELD_NONE)
        buf.timestamp.sec, buf.timestamp.usec = divmod(usec, 1_000_000)
        buf.m.planes = C.pointer(plane)
        ioctl(self.fd, VIDIOC_QBUF, buf)

    def _dequeue(self, kind):
        plane = Plane()
        buf = Buffer(type=kind, memory=MEMORY_MMAP, length=1)
        buf.m.planes = C.pointer(plane)
        try:
            ioctl(self.fd, VIDIOC_DQBUF, buf)
        except OSError as e:
            if e.errno == errno.EAGAIN:
                return None
            raise
        return buf, plane

    def encode(self, frame, usec, sink):
        """Queue one packed NV12 frame; sink gets each finished picture."""
        while not self.free:
            readable, writable, _ = select.select([self.fd], [self.fd], [], STALL)
            if not readable and not writable:
                raise OSError(errno.ETIMEDOUT, "the encoder stalled")
            self.service(sink)
        index = self.free.pop()
        dst, s, w, h = self.inputs[index], self.stride, self.width, self.height
        # (offset in frame, row bytes, rows, offset in buffer): luma, then interleaved chroma.
        for src, row, rows, at in ((0, w, h, 0), (w * h, w, h // 2, self.chroma)):
            if s == row:
                dst[at:at + row * rows] = frame[src:src + row * rows]
            else:  # the driver pads rows: copy each one to its stride
                for r in range(rows):
                    dst[at + r * s:at + r * s + row] = frame[src + r * row:src + r * row + row]
        self._queue(TYPE_OUTPUT_MPLANE, index, self.sizeimage, usec)
        self.service(sink)

    def service(self, sink):
        """Take back consumed inputs and pass on finished pictures, without blocking."""
        while (got := self._dequeue(TYPE_OUTPUT_MPLANE)) is not None:
            self.free.append(got[0].index)
        while (got := self._dequeue(TYPE_CAPTURE_MPLANE)) is not None:
            buf, plane = got
            if plane.bytesused > plane.data_offset:
                sink(self.outputs[buf.index][plane.data_offset:plane.bytesused])
            if not buf.flags & FLAG_LAST:
                self._queue(TYPE_CAPTURE_MPLANE, buf.index, 0, 0)

    def close(self):
        for kind in reversed(self.started):  # never a queue that wasn't started: EBUSY
            try:
                ioctl(self.fd, VIDIOC_STREAMOFF, C.c_int(kind))
            except OSError:
                pass
        os.close(self.fd)


def read_into(f, buf):
    """Fill buf from f; False at end of input."""
    view, got = memoryview(buf), 0
    while got < len(buf):
        n = f.readinto(view[got:])
        if not n:
            return False
        got += n
    return True


def y4m_header(f):
    line = f.readline()
    if not line.startswith(b"YUV4MPEG2 "):
        raise Setup("input isn't YUV4MPEG2")
    fields = {t[:1]: t[1:] for t in line.split()[1:]}
    colour = fields.get(b"C", b"420")
    if not colour.startswith(b"420"):
        raise Setup(f"input is {colour.decode()}, not 4:2:0")
    return int(fields[b"W"]), int(fields[b"H"])


def main(argv):
    fps, bitrate = int(argv[0]), int(argv[1])
    src, out = sys.stdin.buffer, sys.stdout.buffer
    try:
        width, height = y4m_header(src)
        if width % 2 or height % 2:
            raise Setup(f"odd frame size {width}x{height}")
        enc = Encoder(width, height, fps, bitrate)
    except Setup as e:
        print(f"frame_hwenc: {e}", file=sys.stderr)
        return 3
    framer = Framer()

    def sink(data):
        au = framer(data)
        if au:
            out.write(au)
            out.flush()

    luma, quarter = width * height, width * height // 4
    planar = bytearray(luma + 2 * quarter)  # Y, then U, then V, as read
    frame = bytearray(luma + 2 * quarter)   # Y, then U and V interleaved (NV12)
    n = 0
    try:
        while src.readline().startswith(b"FRAME") and read_into(src, planar):
            frame[:luma] = planar[:luma]
            frame[luma::2], frame[luma + 1::2] = planar[luma:luma + quarter], planar[luma + quarter:]
            enc.encode(frame, n * 1_000_000 // fps, sink)
            n += 1
    except BrokenPipeError:
        pass  # the viewer went away
    finally:
        enc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
