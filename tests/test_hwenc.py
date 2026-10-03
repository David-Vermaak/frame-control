"""The hardware H.264 stream: ui/frame_hwenc.py's framing, input handling and
kernel structure layouts, and how server.py builds and falls back from it.
No encoder needed; the real one was checked on the Frame (docs/streaming.md).

Run: python3 -m unittest discover -s tests
"""
import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import base64
import ctypes
import io
import queue
import re
import sys
import unittest
import zlib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ui"))

import frame_hwenc as hw  # noqa: E402
import server  # noqa: E402

SPS = b"\x00\x00\x00\x01\x67\x64\x00\x1f\xac"
PPS = b"\x00\x00\x00\x01\x68\xee\x3c\xb0"
IDR = b"\x00\x00\x00\x01\x65\x88\x84"
P = b"\x00\x00\x00\x01\x41\x9a\x02"


class Framing(unittest.TestCase):
    def test_nal_types(self):
        self.assertEqual([t for t, _ in hw.nal_units(SPS + PPS + IDR)], [7, 8, 5])
        self.assertEqual(hw.nal_units(b""), [])

    def test_joined_headers_pass_through_with_an_aud(self):
        f = hw.Framer()
        self.assertEqual(f(SPS + PPS + IDR), hw.AUD + SPS + PPS + IDR)
        self.assertEqual(f(P), hw.AUD + P)

    def test_separate_header_buffer_joins_the_next_picture(self):
        f = hw.Framer()
        self.assertIsNone(f(SPS + PPS))
        self.assertEqual(f(IDR), hw.AUD + SPS + PPS + IDR)

    def test_keyframe_without_headers_gets_the_last_ones(self):
        # The page configures its decoder from the SPS in a keyframe, and may join at any.
        f = hw.Framer()
        f(SPS + PPS + IDR)
        f(P)
        self.assertEqual(f(IDR), hw.AUD + SPS + PPS + IDR)


@unittest.skipIf(ctypes.sizeof(ctypes.c_long) != 8, "the Frame is LP64 (aarch64); so is this check")
class KernelLayouts(unittest.TestCase):
    """Sizes and ioctl numbers from <linux/videodev2.h> on a 64-bit kernel."""

    def test_struct_sizes(self):
        for struct, size in ((hw.Format, 208), (hw.PixFormatMplane, 192), (hw.RequestBuffers, 20),
                             (hw.Buffer, 88), (hw.Plane, 64), (hw.Control, 8), (hw.StreamParm, 204)):
            self.assertEqual(ctypes.sizeof(struct), size, struct.__name__)
        self.assertEqual(hw.Format.fmt.offset, 8)
        self.assertEqual((hw.Buffer.timestamp.offset, hw.Buffer.m.offset), (24, 64))

    def test_ioctl_numbers(self):
        self.assertEqual(hw.VIDIOC_S_FMT, 0xC0D05605)
        self.assertEqual(hw.VIDIOC_REQBUFS, 0xC0145608)
        self.assertEqual(hw.VIDIOC_QBUF, 0xC058560F)
        self.assertEqual(hw.VIDIOC_DQBUF, 0xC0585611)
        self.assertEqual(hw.VIDIOC_STREAMON, 0x40045612)
        self.assertEqual(hw.VIDIOC_S_PARM, 0xC0CC5616)
        self.assertEqual(hw.VIDIOC_S_CTRL, 0xC008561C)


def y4m(width, height, frames):
    out = f"YUV4MPEG2 W{width} H{height} F30:1 Ip A1:1 C420jpeg\n".encode()
    for y, u, v in frames:
        out += b"FRAME\n" + bytes([y]) * (width * height) + bytes([u]) * (width * height // 4) \
            + bytes([v]) * (width * height // 4)
    return out


class FakeEncoder:
    made = []

    def __init__(self, width, height, fps, bitrate):
        self.args, self.frames, self.closed = (width, height, fps, bitrate), [], False
        FakeEncoder.made.append(self)

    def encode(self, frame, usec, sink):
        self.frames.append((bytes(frame), usec))
        sink(SPS + PPS + IDR if not self.frames[:-1] else P)

    def close(self):
        self.closed = True


class Main(unittest.TestCase):
    def run_main(self, stdin, argv=("30", "3000000")):
        out = io.BytesIO()
        stdin_buf = io.BufferedReader(io.BytesIO(stdin))
        with mock.patch.object(hw, "Encoder", FakeEncoder), \
             mock.patch.object(hw.sys, "stdin", mock.Mock(buffer=stdin_buf)), \
             mock.patch.object(hw.sys, "stdout", mock.Mock(buffer=out)), \
             mock.patch.object(hw.sys, "stderr", io.StringIO()):
            FakeEncoder.made.clear()
            status = hw.main(list(argv))
        return status, out.getvalue()

    def test_frames_become_nv12_with_rising_timestamps(self):
        status, out = self.run_main(y4m(4, 2, [(16, 100, 200), (17, 101, 201)]))
        self.assertEqual(status, 0)
        enc = FakeEncoder.made[0]
        self.assertEqual(enc.args, (4, 2, 30, 3000000))
        self.assertTrue(enc.closed)
        frame, usec = enc.frames[0]
        self.assertEqual(frame, bytes([16]) * 8 + bytes([100, 200, 100, 200]))  # Y, then U and V interleaved
        self.assertEqual([u for _, u in enc.frames], [0, 33333])
        self.assertEqual(out, hw.AUD + SPS + PPS + IDR + hw.AUD + P)

    def test_bad_input_is_a_setup_failure(self):
        for stdin in (b"not y4m\n", y4m(3, 2, []), b"YUV4MPEG2 W4 H2 C444\n"):
            with self.subTest(stdin=stdin[:20]):
                status, out = self.run_main(stdin)
                self.assertEqual(status, 3)
                self.assertEqual(out, b"")

    def test_missing_encoder_is_a_setup_failure(self):
        with mock.patch.object(hw, "DEVICE", "/nonexistent/video23"):
            with mock.patch.object(hw.sys, "stdin", mock.Mock(buffer=io.BufferedReader(io.BytesIO(y4m(4, 2, []))))), \
                 mock.patch.object(hw.sys, "stderr", io.StringIO()) as err:
                self.assertEqual(hw.main(["30", "3000000"]), 3)
        self.assertIn("frame_hwenc: /nonexistent/video23", err.getvalue())
        self.assertTrue(err.getvalue().startswith(server.HWENC_FAILED))

    def test_truncated_frame_ends_cleanly(self):
        status, _ = self.run_main(y4m(4, 2, [(1, 2, 3)])[:-3])
        self.assertEqual(status, 0)
        self.assertEqual(FakeEncoder.made[0].frames, [])


class StreamCommand(unittest.TestCase):
    def test_hardware_pipeline(self):
        cmd = server.stream_command("h=720&fps=30", hardware=True)
        self.assertIn("-f v4l2 -video_size 1920x1080 -i /dev/video99", cmd)
        self.assertIn("scale=-2:720:flags=fast_bilinear:out_color_matrix=bt709:out_range=tv,format=yuv420p", cmd)
        self.assertIn("-f yuv4mpegpipe - | python3 -c", cmd)
        self.assertTrue(cmd.endswith(" 30 3000000 & exec >&-; cat >/dev/null; pkill -P $$ 2>/dev/null; wait"))
        self.assertNotIn("libx264", cmd)
        # The source sent along is frame_hwenc.py itself.
        packed = re.search(r"b64decode\('([A-Za-z0-9+/=]+)'\)", cmd).group(1)
        self.assertEqual(zlib.decompress(base64.b64decode(packed)), (ROOT / "ui" / "frame_hwenc.py").read_bytes())

    def test_software_pipeline(self):
        cmd = server.stream_command("h=1080&fps=60", hardware=False)
        self.assertIn("-vf \"fps=60,scale=-2:1080,format=yuv420p\" -c:v libx264", cmd)
        self.assertIn("-b:v 6M", cmd)
        self.assertNotIn("python3", cmd)

    def test_panel_hardware_pipeline(self):
        cmd = server.stream_command("src=panel&window=10485777&display=:1&h=720&fps=30", hardware=True)
        self.assertTrue(cmd.startswith("DISPLAY=:1 ffmpeg"))
        self.assertIn("scale=-2:'trunc(min(720,ih)/2)*2':flags=fast_bilinear:out_color_matrix=bt709", cmd)
        self.assertNotIn("/dev/video99", cmd)

    def test_default_follows_the_session(self):
        with mock.patch.object(server, "_hw_encoder", False):
            self.assertIn("libx264", server.stream_command("h=720&fps=30"))
        with mock.patch.object(server, "_hw_encoder", True):
            self.assertIn("yuv4mpegpipe", server.stream_command("h=720&fps=30"))


class Fallback(unittest.TestCase):
    """A hardware stream that can't set up is retried with libx264, which then sticks."""

    def handler(self):
        h = server.Handler.__new__(server.Handler)
        h.wfile, h.sent = io.BytesIO(), []
        h.send_response = lambda code: h.sent.append(code)
        h.send_header = lambda *a: None
        h.end_headers = lambda: None
        return h

    def opened(self, *chunks):
        q = queue.Queue()
        for c in chunks:
            q.put(c)
        return mock.Mock(), mock.Mock(), q, q.get()

    def stream(self, opens, closes):
        h = self.handler()
        with mock.patch.object(server, "_hw_encoder", True), mock.patch.object(server, "ensure_master"), \
             mock.patch.object(server.Handler, "_open_stream", side_effect=opens) as open_, \
             mock.patch.object(server.Handler, "_close_stream", side_effect=closes), \
             mock.patch.object(server, "STREAM_STALL", 0.01), mock.patch("sys.stderr", io.StringIO()):
            try:
                h.stream_video("h=720&fps=30")
                error = None
            except server.Failure as e:
                error = str(e)
            return h, [c.args[0] for c in open_.call_args_list], error, server._hw_encoder

    def test_setup_failure_retries_with_x264(self):
        h, commands, error, after = self.stream(
            [self.opened(b""), self.opened(b"h264", b"")],
            ["frame_hwenc: /dev/video23: Device or resource busy", ""])
        self.assertIsNone(error)
        self.assertEqual(h.wfile.getvalue(), b"h264")
        self.assertIn("yuv4mpegpipe", commands[0])
        self.assertIn("libx264", commands[1])
        self.assertFalse(after)

    def test_other_failures_are_reported_without_a_retry(self):
        h, commands, error, after = self.stream(
            [self.opened(b"")], ["No headset view device (/dev/video99). Is SteamVR running?"])
        self.assertEqual(len(commands), 1)
        self.assertIn("Is SteamVR running?", error)
        self.assertTrue(after)

    def test_x264_failure_after_fallback_is_reported(self):
        _, commands, error, _ = self.stream(
            [self.opened(b""), self.opened(b"")], ["frame_hwenc: odd frame size 3x2", "x264 broke"])
        self.assertEqual(len(commands), 2)
        self.assertEqual(error, "x264 broke")


if __name__ == "__main__":
    unittest.main()
