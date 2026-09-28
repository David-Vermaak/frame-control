"""PC host selection behind the existing MacView tunnel/panel controller.

The public /api/macview name and viewer URL remain compatible with the Mac
base branch. Only helper launch, platform availability and source validation
are different. No second viewer, SSH supervisor or benchmark launcher.
"""
import os
import sys
import subprocess
from frame_macview import MacView, MacViewError, ROOT
from frame_pc_capture import LIBRARY, NATIVE


class PCView(MacView):
    viewer_profile = 'pc-view'
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
        cache = os.path.join(os.path.expanduser('~'), '.cache', 'frame-control')
        os.makedirs(cache, exist_ok=True)
        env['GST_REGISTRY_1_0'] = os.path.join(cache, 'gstreamer-registry.bin')
        env['GST_REGISTRY_FORK'] = 'no'
        if sys.platform == 'win32':
            env['PATH'] = str(NATIVE / 'bin') + os.pathsep + env.get('PATH', '')
        else:
            env['LD_LIBRARY_PATH'] = str(NATIVE / 'lib') + os.pathsep + env.get('LD_LIBRARY_PATH', '')
            env['PIPEWIRE_MODULE_DIR'] = str(NATIVE / 'lib' / 'pipewire-0.3')
            env['SPA_PLUGIN_DIR'] = str(NATIVE / 'lib' / 'spa-0.2')
            env['PIPEWIRE_CONFIG_DIR'] = str(NATIVE / 'share' / 'pipewire')
            env['PIPEWIRE_CONFIG_NAME'] = 'client.conf'
        return env

    def shutdown(self):
        self.closing = True
        try:
            self.stop()
        except MacViewError:
            pass
        if self.tunnel and self.tunnel.poll() is None:
            self.tunnel.terminate()
        if self.agent and self.agent.poll() is None:
            # EOF gives the host a chance to release held input and portal
            # sessions, including on Windows where terminate is uncatchable.
            if self.agent.stdin:
                self.agent.stdin.close()
            try:
                self.agent.wait(10)
            except subprocess.TimeoutExpired:
                self.agent.kill()
                self.agent.wait(5)

    def _show(self, src, quality, width, height):
        if src.startswith('separate:'):
            raise MacViewError('Separate virtual displays are a Mac-only feature. Choose a window or screen.')
        return super()._show(src, quality, width, height)


def host_view(*args, **kwargs):
    return (MacView if sys.platform == 'darwin' else PCView)(*args, **kwargs)
