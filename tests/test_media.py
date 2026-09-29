"""Owned media planning, eye isolation, decoder choice and fake-Frame ownership."""
import argparse
import io
import json
import os
import signal
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ui'))
import frame_media as media
import frame_media_player as player
import frame_media_remote as remote
import frame_splat as splat
import server


class Media(unittest.TestCase):
    def test_layout_evidence_and_override(self):
        for name, layout in [('film_SBS.mp4', 'sbs'), ('film.OU.mkv', 'ou'),
                             ('film_FSBS.mp4', 'full-sbs'), ('photo_TB.png', 'ou')]:
            self.assertEqual(media.plan(name)['layout'], layout)
        self.assertEqual(media.plan('film_SBS_OU.mp4', 'mono')['source'], 'explicit')
        self.assertEqual(media.plan('film.mkv', metadata={'stereo_mode': 'top_bottom'})['layout'], 'full-ou')
        for name in ('film.mp4', 'film_SBS_OU.mp4', 'businessbs.mp4'):
            with self.assertRaises(ValueError):
                media.plan(name)
        with self.assertRaises(ValueError):
            media.plan('film.mkv', metadata={'stereo_mode': 'right_left'})

    def test_unsupported_containers_do_not_flatten_spatial_photos(self):
        for name in ('spatial.HEIC', 'stereo.mpo', 'cloud.ply', 'cloud.spz', 'app.exe'):
            with self.assertRaises(ValueError):
                media.plan(name, 'sbs')

    def test_ou_pixels_keep_each_eye_and_row(self):
        a, b, c, d = [bytes([n])*8 for n in (1, 2, 3, 4)]
        data, width, height = media.stereo_pixels(a+b+c+d, 2, 4, 'ou')
        self.assertEqual((data, width, height), (a+c+b+d, 4, 2))
        with self.assertRaises(ValueError):
            media.stereo_pixels(b'bad', 2, 4, 'sbs')
        self.assertEqual(media.geometry(3840, 2160, 'sbs'), (1920, 1080, 2))
        self.assertEqual(media.geometry(1920, 1080, 'ou'), (1920, 1080, .5))

    def test_decode_is_hardware_and_one_clock_for_audio(self):
        for codec, decoder in [('h264', 'h264_v4l2m2m'), ('hevc', 'hevc_v4l2m2m')]:
            cmd = player.decoder_command(Path('/tmp/a file.mp4'), {'codec_name': codec}, 1280, 720, True)
            self.assertIn(decoder, cmd)
            self.assertIn('-re', cmd)
            self.assertIn('pulse', cmd)
            self.assertIn(str(Path('/tmp/a file.mp4')), cmd)
        with self.assertRaises(ValueError):
            player.decoder_command(Path('x.webm'), {'codec_name': 'vp9'}, 640, 480, False)
        cmd = player.decoder_command(Path('x.png'), {'codec_name': 'png'}, 640, 480, False, True)
        self.assertNotIn('-re', cmd)
        self.assertIn('-frames:v', cmd)

    def test_video_survives_standby_and_stop_after_end_stays_ended(self):
        # Verified 2026-09-29: an unworn Frame enters standby within seconds and
        # SetOverlayRaw then returns RequestFailed (23) until it wakes.
        calls = []

        class FakeOverlay:
            def __init__(self):
                self.closed = False

            def create(self, *a, **k):
                return len(calls)

            def call(self, *a):
                pass

            def pixels(self, handle, data, w, h):
                calls.append((handle, w, h))
                if len(calls) <= 3:
                    raise player.OverlayBusy('standby')

            def close(self):
                # A Stop arriving during cleanup must be ignored, not become an error.
                # Call the installed handler directly: a real SIGTERM kills Windows.
                handler = signal.getsignal(signal.SIGTERM)
                if callable(handler):
                    handler(signal.SIGTERM, None)

        frame = bytes(4*2*4)
        proc = unittest.mock.MagicMock()
        proc.stdout = io.BytesIO(frame*4)
        proc.wait.return_value = 0
        proc.poll.return_value = 0
        with tempfile.TemporaryDirectory() as d, \
                patch.object(player, 'Overlay', FakeOverlay), \
                patch.object(player, 'probe', return_value=({'codec_name': 'h264', 'width': 4, 'height': 2}, False)), \
                patch.object(player.subprocess, 'Popen', return_value=proc), \
                patch.object(player.time, 'sleep'):
            path = Path(d)/'clip_SBS.mp4'
            path.write_bytes(b'x')
            status = Path(d)/'status.json'
            old = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
            try:
                player.play(argparse.Namespace(file=str(path), layout='auto', theatre=True, status=str(status)))
            finally:
                signal.signal(signal.SIGTERM, old[0])
                signal.signal(signal.SIGINT, old[1])
            result = json.loads(status.read_text())
        self.assertEqual((result['state'], result['frames']), ('ended', 4))
        # Surround + first two video frames were dropped; the surround was re-sent after wake.
        self.assertEqual(result['dropped'], 3)
        self.assertIn((0, 1, 1), calls[3:])

    def test_fake_frame_library_and_traversal(self):
        with tempfile.TemporaryDirectory() as d, patch.object(remote, 'ROOT', Path(d)):
            identity = 'a'*32+'/space and quote\'.png'
            path = Path(d)/identity
            path.parent.mkdir()
            path.write_bytes(b'test')
            self.assertEqual(remote.media_path(identity), path.resolve())
            for bad in ('../../etc/passwd', '/etc/passwd', 'a'*32+'/..', 'a'*32+'/x/y', None):
                with self.assertRaises((ValueError, FileNotFoundError)):
                    remote.media_path(bad)
            link = path.parent/'link.png'
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                remote.media_path('a'*32+'/link.png')
            with patch.object(remote, 'status', return_value={'state': 'idle'}):
                files = remote.run({'action': 'list'})['files']
                self.assertEqual([f['id'] for f in files], [identity])

    def test_server_rejects_bad_actions_before_ssh(self):
        with patch.object(server, 'ssh') as ssh:
            for body in ({'action': 'delete'}, {'action': 'play', 'id': '../x'},
                         {'action': 'play', 'id': 'a'*32+'/x', 'layout': 'invalid'},
                         {'action': 'play', 'id': 'a'*32+'/x', 'theatre': 'false'}):
                with self.assertRaises(server.Failure):
                    server.media(body)
            ssh.assert_not_called()

    def test_fake_frame_stop_only_owns_our_unit(self):
        for rc in (0, 5):  # 5: already collected ("not loaded"), a no-op
            with patch.object(remote.subprocess, 'run') as run, patch.object(remote, 'status', return_value={'state': 'ended'}):
                run.return_value.returncode = rc
                self.assertEqual(remote.run({'action': 'stop'})['state'], 'ended')
                self.assertEqual(run.call_args.args[0], ['systemctl', '--user', 'stop', 'frame-control-media.service'])
        with patch.object(remote.subprocess, 'run') as run, patch.object(remote, 'status', return_value={}):
            run.return_value.returncode, run.return_value.stderr = 1, 'Access denied'
            with self.assertRaisesRegex(RuntimeError, 'Access denied'):
                remote.run({'action': 'stop'})

    def test_start_failure_reports_systemd_error(self):
        with tempfile.TemporaryDirectory() as d, patch.object(remote, 'ROOT', Path(d)), \
                patch.object(remote, 'STATUS', Path(d)/'status.json'), \
                patch.object(remote, 'active', return_value=False), \
                patch.object(remote.subprocess, 'run') as run:
            identity = 'b'*32+'/still_SBS.png'
            (Path(d)/identity).parent.mkdir()
            (Path(d)/identity).write_bytes(b'x')
            run.return_value.returncode, run.return_value.stderr = 1, 'Unit already exists'
            with patch.object(remote, 'probe', return_value=({}, False)), \
                    self.assertRaisesRegex(RuntimeError, 'Unit already exists'):
                remote.run({'action': 'play', 'id': identity})
            self.assertEqual(run.call_args.args[0][0], 'systemd-run')  # reset-failed's result is ignored

    def test_upload_rejects_unplayable_names_and_keeps_copy_error(self):
        with patch.object(server, 'ssh') as ssh, patch.object(server, 'push_file') as push:
            # On Windows a backslash is a separator, so such a name can't reach here.
            for name in ('.hidden.mp4',) + (('a\\b_SBS.mp4',) if os.sep == '/' else ()):
                with self.assertRaises(server.Failure):
                    server.push_media(Path('/tmp')/name)
            ssh.assert_not_called()
            push.side_effect = server.Failure('copy failed')
            ssh.side_effect = [None, server.Failure('link down')]
            with self.assertRaisesRegex(server.Failure, 'copy failed'):
                server.push_media(Path('/tmp/film_SBS.mp4'))

    def test_splat_invalid_records_and_stereo_parallax(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'small.splat'
            path.write_bytes(struct.pack('<6f8B', 0, 0, 0, .001, .001, .001,
                                         255, 0, 0, 255, 255, 128, 128, 128))
            data, w, h = splat.render(path, 64, 48)
            self.assertEqual((len(data), w, h), (128*48*4, 128, 48))
            def centroid(eye):
                weights = [(x, data[(y*w+x+eye*64)*4]) for y in range(h) for x in range(64)]
                return sum(x*v for x,v in weights)/sum(v for x,v in weights)
            self.assertGreater(centroid(0), centroid(1))
            for bad in (b'', b'bad', struct.pack('<6f8B', float('nan'), 0, 0, 1, 1, 1, *([128]*8))):
                path.write_bytes(bad)
                with self.assertRaises(ValueError):
                    splat.read(path)


if __name__ == '__main__':
    unittest.main()
