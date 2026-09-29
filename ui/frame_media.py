"""Media planning shared by Frame Control and its own Frame-side player.

No viewer dependencies. Filename hints are suggestions, never guesses from
resolution. Explicit layout wins; conflicting hints require a choice.
"""
import re
from pathlib import Path

LAYOUTS = ('auto', 'mono', 'sbs', 'ou', 'full-sbs', 'full-ou')
VIDEO = {'.mp4', '.mkv', '.mov', '.webm', '.m4v'}
PHOTO = {'.png', '.jpg', '.jpeg'}


def plan(name, layout='auto', metadata=None):
    if layout not in LAYOUTS:
        raise ValueError('Choose auto, mono, sbs, ou, full-sbs or full-ou')
    suffix = Path(name).suffix.lower()
    if suffix in {'.heic', '.heif', '.avif', '.mpo'}:
        raise ValueError('Native spatial-photo containers are not supported yet; export both eyes as SBS or OU PNG/JPEG')
    if suffix == '.splat':
        return {'kind': 'splat', 'layout': 'sbs', 'source': 'renderer'}
    if suffix not in VIDEO | PHOTO:
        raise ValueError('Use MP4/MKV/MOV/WebM video, PNG/JPEG stereo photos, or a .splat file')
    source = 'explicit'
    if layout == 'auto':
        tokens = set(re.split(r'[^a-z0-9]+', Path(name).stem.lower()))
        hints = set()
        for value, tags in [('full-sbs', {'fsbs'}), ('full-ou', {'fou', 'ftb'}),
                            ('sbs', {'sbs', 'hsbs', 'lr'}), ('ou', {'ou', 'hou', 'tb', 'htb'})]:
            if tokens & tags:
                hints.add(value)
        if len(hints) > 1:
            raise ValueError('Conflicting stereo filename tags; choose the layout explicitly')
        layout = next(iter(hints), None)
        source = 'filename'
        if not layout:
            # Matroska StereoMode/FFmpeg stereo_mode: only known left-first modes.
            mode = (metadata or {}).get('stereo_mode')
            layout = {'left_right': 'full-sbs', 'top_bottom': 'full-ou', 'mono': 'mono'}.get(mode)
            source = 'metadata'
            if mode and layout is None:
                raise ValueError('Unsupported stereo metadata; choose the eye order/layout explicitly')
        if not layout:
            raise ValueError('No stereo layout found; choose mono, SBS or OU (left/top eye first)')
    return {'kind': 'video' if suffix in VIDEO else 'photo', 'layout': layout, 'source': source}


def geometry(width, height, layout):
    """Bound transfer to 1920x1080; return packed dimensions and texel aspect."""
    if not 0 < width <= 32768 or not 0 < height <= 32768:
        raise ValueError('Invalid media dimensions')
    if layout not in LAYOUTS[1:]:
        raise ValueError('Resolve the layout before playback')
    scale = min(1, 1920 / width, 1080 / height)
    w, h = max(2, int(width * scale) // 2 * 2), max(2, int(height * scale) // 2 * 2)
    return w, h, {'mono': 1, 'sbs': 2, 'ou': .5, 'full-sbs': 1, 'full-ou': 1}[layout]


def stereo_pixels(data, width, height, layout):
    """Normalize top/bottom to OpenVR's left/right texture; preserve eye order."""
    if len(data) != width * height * 4:
        raise ValueError('Incomplete RGBA frame')
    if layout not in ('ou', 'full-ou'):
        return data, width, height
    stride, half = width * 4, height // 2
    return b''.join(data[y*stride:(y+1)*stride] +
                    data[(y+half)*stride:(y+half+1)*stride] for y in range(half)), width*2, half
