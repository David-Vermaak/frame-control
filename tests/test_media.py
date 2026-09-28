"""Owned media planning, eye isolation, decoder choice and fake-Frame ownership."""
import json
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
        with patch.object(remote.subprocess, 'run') as run, patch.object(remote, 'status', return_value={}):
            remote.run({'action': 'stop'})
            self.assertEqual(run.call_args.args[0], ['systemctl', '--user', 'stop', 'frame-control-media.service'])

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
