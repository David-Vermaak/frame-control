"""Media transfer through the real HTTP/SSH path; the fake has no VR renderer."""
import harness
from harness import ok, ssh

harness.require()


class Media(harness.FrameTestCase):
    def test_upload_and_list_without_launching_a_viewer(self):
        # The transfer endpoint doesn't decode. Rendering belongs to the real
        # headset smoke checks documented in docs/vr-video.md.
        self.assertEqual(ssh('stat -c "%U:%G %a" ~/.local/share').strip(), 'steamos:steamos 755')
        result = ok('POST', '/api/upload', raw=b'fake-media', headers={
            'X-Mode': 'media', 'X-Filename': 'test_SBS.png'})
        identity = result['id']
        try:
            files = ok('POST', '/api/media', {'action': 'list'})['files']
            self.assertIn(identity, [f['id'] for f in files])
            self.assertEqual(ssh('cat ~/Videos/FrameControl/'+identity), 'fake-media')
        finally:
            ssh('rm -rf ~/Videos/FrameControl/'+identity.split('/')[0])
