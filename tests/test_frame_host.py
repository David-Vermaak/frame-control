"""Captured OpenSSH output keeps working on Windows and POSIX hosts."""
import sandbox  # noqa: F401
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ui"))
import frame_host


class CapturedSSH(unittest.TestCase):
    def run_command(self, source, **kwargs):
        with mock.patch.object(frame_host, "WINDOWS", True):
            return frame_host.run_ssh([sys.executable, "-c", source], timeout=5, **kwargs)

    def test_binary_output_and_input(self):
        result = self.run_command("import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()); "
                                  "sys.stderr.buffer.write(b'error\\r\\n')",
                                  capture_output=True, input=b"data\x00\xff")
        self.assertEqual(result.stdout, b"data\x00\xff")
        self.assertEqual(result.stderr, b"error\r\n")

    def test_text_output_normalizes_newlines(self):
        result = self.run_command("import sys; sys.stdout.write(sys.stdin.read()); "
                                  "sys.stderr.buffer.write(b'first\\r\\nsecond\\rthird\\n')",
                                  capture_output=True, input="hello\n", text=True)
        self.assertEqual(result.stdout, "hello\n")
        self.assertEqual(result.stderr, "first\nsecond\nthird\n")

    def test_explicit_encoding_and_errors(self):
        result = self.run_command("import sys; sys.stderr.buffer.write(b'\\xe9\\xff')",
                                  capture_output=True, encoding="ascii", errors="replace")
        self.assertEqual(result.stderr, "\ufffd\ufffd")

    def test_check_preserves_error_output(self):
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            self.run_command("import sys; print('out'); print('err', file=sys.stderr); sys.exit(7)",
                             capture_output=True, text=True, check=True)
        self.assertEqual(caught.exception.returncode, 7)
        self.assertEqual(caught.exception.stdout, "out\n")
        self.assertEqual(caught.exception.stderr, "err\n")

    def test_timeout_preserves_partial_stderr(self):
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            with mock.patch.object(frame_host, "WINDOWS", True):
                frame_host.run_ssh([sys.executable, "-c", "import sys, time; "
                                    "sys.stderr.write('waiting'); sys.stderr.flush(); time.sleep(10)"],
                                   capture_output=True, text=True, timeout=1)
        self.assertEqual(caught.exception.stderr, b"waiting")

    def test_streamed_stdout_is_kept_separate(self):
        with tempfile.TemporaryFile() as output:
            result = self.run_command("import sys; sys.stdout.buffer.write(b'file'); "
                                      "sys.stderr.buffer.write(b'error')",
                                      stdout=output, stderr=subprocess.PIPE)
            output.seek(0)
            self.assertEqual(output.read(), b"file")
        self.assertIsNone(result.stdout)
        self.assertEqual(result.stderr, b"error")

    def test_uncaptured_windows_call_is_unchanged(self):
        with mock.patch.object(frame_host, "WINDOWS", True), mock.patch.object(subprocess, "run") as run:
            frame_host.run_ssh(["ssh", "-V"], stderr=subprocess.DEVNULL, timeout=5)
        run.assert_called_once_with(["ssh", "-V"], stderr=subprocess.DEVNULL, timeout=5)

    def test_posix_call_is_unchanged(self):
        with mock.patch.object(frame_host, "WINDOWS", False), mock.patch.object(subprocess, "run") as run:
            frame_host.run_ssh(["ssh", "-V"], capture_output=True, check=True, timeout=5)
        run.assert_called_once_with(["ssh", "-V"], capture_output=True, check=True, timeout=5)

    def test_capture_rejects_explicit_streams(self):
        for stream in ("stdout", "stderr"):
            with self.subTest(stream=stream), self.assertRaises(ValueError):
                self.run_command("", capture_output=True, **{stream: subprocess.DEVNULL})

    @unittest.skipUnless(shutil.which("ssh"), "needs OpenSSH")
    def test_real_ssh_failure_returns_stderr_without_hanging(self):
        result = frame_host.run_ssh(["ssh", "-F", os.devnull, "-o", "BatchMode=yes",
                                    "-o", "ConnectTimeout=2", "frame-control-test.invalid", "true"],
                                   capture_output=True, stdin=subprocess.DEVNULL, text=True, timeout=5)
        self.assertEqual(result.returncode, 255)
        self.assertIn("Could not resolve hostname", result.stderr)
