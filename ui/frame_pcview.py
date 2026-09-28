"""PC host selection behind the existing MacView tunnel/panel controller.

The public /api/macview name and viewer URL remain compatible with the Mac
base branch. Only helper launch, platform availability and source validation
are different. No second viewer, SSH supervisor or benchmark launcher.
"""
import os
import sys
from frame_macview import MacView, MacViewError, ROOT
from frame_pc_capture import LIBRARY, NATIVE


class PCView(MacView):
    host = 'windows' if sys.platform == 'win32' else 'linux'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.prefer_usb = False  # Mac networksetup probe is platform-specific

    def unavailable(self):
        if sys.platform not in ('win32', 'linux'):
            return 'PC streaming needs Windows or a Linux desktop.'
        if not LIBRARY.is_file():
            return 'The PC streaming libraries are missing from this build. See docs/pc-in-headset.md.'
        return None

    def state(self):
        result = super().state()
        result["host"] = self.host
        return result

    def build(self):
        pass  # native libraries are built and bundled with the app

    def agent_command(self):
        return [sys.executable, str(ROOT / 'ui' / 'frame_pc_agent.py')]

    def agent_environment(self):
        env = super().agent_environment()
        env['GST_PLUGIN_PATH_1_0'] = str(NATIVE / 'lib' / 'gstreamer-1.0')
        env['GST_PLUGIN_SYSTEM_PATH_1_0'] = ''
        env['GST_REGISTRY_1_0'] = str(NATIVE.parent / 'registry.bin') if os.access(NATIVE.parent, os.W_OK) else os.path.join(os.path.expanduser('~'), '.cache', 'frame-control-gst.bin')
        env['GST_REGISTRY_FORK'] = 'no'
        if sys.platform == 'win32':
            env['PATH'] = str(NATIVE / 'bin') + os.pathsep + env.get('PATH', '')
        else:
            env['LD_LIBRARY_PATH'] = str(NATIVE / 'lib') + os.pathsep + env.get('LD_LIBRARY_PATH', '')
        return env

    def _show(self, src, quality, width, height):
        if src.startswith('separate:'):
            raise MacViewError('Separate virtual displays are a Mac-only feature. Choose a window or screen.')
        return super()._show(src, quality, width, height)


def host_view(*args, **kwargs):
    return (MacView if sys.platform == 'darwin' else PCView)(*args, **kwargs)
